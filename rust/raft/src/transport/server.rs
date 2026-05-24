//! gRPC server for Raft transport.
//!
//! All Raft zones (including single-zone setups) are served through
//! `ZoneRaftRegistry`. There is no separate "single-zone" code path —
//! a single-zone deployment is simply a registry with one zone.

use super::proto::nexus::raft::{
    raft_command::Command as ProtoCommandVariant,
    raft_query::Query as ProtoQueryVariant,
    raft_query_response::Result as ProtoQueryResultVariant,
    raft_response::Result as ProtoResponseResultVariant,
    zone_api_service_server::{ZoneApiService, ZoneApiServiceServer},
    zone_transport_service_server::{ZoneTransportService, ZoneTransportServiceServer},
    ClusterConfig as ProtoClusterConfig, DeleteZoneRequest, DeleteZoneResponse,
    GetClusterInfoRequest, GetClusterInfoResponse, GetMetadataResult, GetSearchCapabilitiesRequest,
    JoinClusterRequest, JoinClusterResponse, JoinZoneRequest, JoinZoneResponse, ListMetadataResult,
    LockInfoResult, LockResult, NodeInfo as ProtoNodeInfo, ProposeRequest, ProposeResponse,
    QueryRequest, QueryResponse, RaftCommand, RaftQueryResponse, RaftResponse, ReadBlobRequest,
    ReadBlobResponse, ReplicateEntriesRequest, ReplicateEntriesResponse, SearchCapabilities,
    StepMessageRequest, StepMessageResponse,
};
use super::{NodeAddress, Result, SharedPeerMap, TransportError};
use crate::blob_fetcher::BlobFetcherSlot;
use crate::raft::{
    reconcile_peers_with_conf_state, Command, CommandResult, FullStateMachine, RaftError,
    WitnessStateMachine, ZoneConsensus, ZoneRaftRegistry,
};
use crate::storage::RedbStore;
use bincode;
use dashmap::DashMap;
use prost::Message;
use protobuf::Message as ProtobufV2Message;
use std::collections::HashMap;
use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::{Arc, RwLock};
use tokio::task::JoinHandle;
use tonic::{Request, Response, Status};

/// Configuration for Raft transport server.
#[derive(Debug, Clone)]
pub struct ServerConfig {
    /// Address to bind to (e.g., "0.0.0.0:2026").
    pub bind_address: SocketAddr,
    /// Maximum concurrent connections.
    pub max_connections: usize,
    /// Maximum message size in bytes.
    pub max_message_size: usize,
    /// Optional TLS configuration for mTLS. None = plain HTTP/2.
    pub tls: Option<super::TlsConfig>,
}

impl Default for ServerConfig {
    fn default() -> Self {
        Self {
            bind_address: "0.0.0.0:2026".parse().unwrap(),
            max_connections: 100,
            max_message_size: 64 * 1024 * 1024, // 64MB
            tls: None,
        }
    }
}

// =============================================================================
// Raft gRPC Server (zone-routed, serves all zones on one port)
// =============================================================================

/// A gRPC server that routes requests to Raft zones via `ZoneRaftRegistry`.
///
/// All setups — single-zone and multi-zone — use this server.
/// A single-zone deployment is just a registry with one zone.
pub struct RaftGrpcServer {
    config: ServerConfig,
    registry: Arc<ZoneRaftRegistry>,
    /// CA private key bytes — read once at startup, held in memory for JoinCluster cert signing.
    ca_key_pem: Option<Vec<u8>>,
    /// SHA-256 hash of the join token password — for JoinCluster verification.
    join_token_hash: Option<String>,
    /// Slot the kernel binds a `BlobFetcher` into after its root mount
    /// backend is wired. `None` while the slot is empty —
    /// `ReadBlob` returns `NotFound` until the kernel installs one.
    blob_fetcher_slot: Option<BlobFetcherSlot>,
    /// Additional gRPC services co-hosted on the same port.
    /// Constructed externally (e.g. VFS gRPC by the cluster binary)
    /// and passed in as type-erased `tonic::service::Routes` so this
    /// crate has no dependency on the transport crate.
    extra_services: Option<tonic::service::Routes>,
}

impl RaftGrpcServer {
    pub fn new(registry: Arc<ZoneRaftRegistry>, config: ServerConfig) -> Self {
        Self {
            config,
            registry,
            ca_key_pem: None,
            join_token_hash: None,
            blob_fetcher_slot: None,
            extra_services: None,
        }
    }

    /// Set cluster join parameters for JoinCluster RPC support.
    pub fn with_join_config(mut self, ca_key_pem: Vec<u8>, join_token_hash: String) -> Self {
        self.ca_key_pem = Some(ca_key_pem);
        self.join_token_hash = Some(join_token_hash);
        self
    }

    /// Attach the late-bindable `BlobFetcher` slot so
    /// `ZoneApiService::read_blob` can serve CAS reads once the kernel
    /// installs an impl. Callers typically share the slot with the
    /// owning `ZoneManager` so both halves reach the same `Arc`.
    pub fn with_blob_fetcher_slot(mut self, slot: BlobFetcherSlot) -> Self {
        self.blob_fetcher_slot = Some(slot);
        self
    }

    /// Co-host additional gRPC services on the same port.
    ///
    /// The caller builds the services externally (e.g. VFS gRPC via
    /// `transport::grpc::build_vfs_service`) and passes them as
    /// type-erased `tonic::service::Routes`. This avoids a circular
    /// dependency between `raft` and `transport` crates.
    pub fn with_extra_services(mut self, routes: tonic::service::Routes) -> Self {
        self.extra_services = Some(routes);
        self
    }

    /// Get the bind address.
    pub fn bind_address(&self) -> SocketAddr {
        self.config.bind_address
    }

    /// Start the gRPC server.
    pub async fn serve(self) -> Result<()> {
        let addr = self.config.bind_address;
        let tls_enabled = self.config.tls.is_some();
        tracing::info!(
            "Starting Raft gRPC server on {} (zones={}, tls={})",
            addr,
            self.registry.list_zones().len(),
            tls_enabled,
        );

        let raft_service = ZoneTransportServiceImpl {
            registry: self.registry.clone(),
        };
        let client_service = ZoneApiServiceImpl {
            registry: self.registry.clone(),
            tls: self.config.tls.clone(),
            ca_key_pem: self.ca_key_pem.clone(),
            join_token_hash: self.join_token_hash.clone(),
            blob_fetcher_slot: self.blob_fetcher_slot.clone(),
        };

        let mut builder =
            lib::transport_primitives::apply_server_limits(tonic::transport::Server::builder());
        if let Some(ref tls) = self.config.tls {
            let identity = tonic::transport::Identity::from_pem(&tls.cert_pem, &tls.key_pem);
            let client_ca = tonic::transport::Certificate::from_pem(&tls.ca_pem);
            let tls_config = tonic::transport::ServerTlsConfig::new()
                .identity(identity)
                .client_ca_root(client_ca);
            builder = builder
                .tls_config(tls_config)
                .map_err(|e| TransportError::Connection(format!("TLS config error: {}", e)))?;
            tracing::info!("TLS mode: mTLS (client auth required)");
        }

        // If extra services are provided, add them first via add_routes
        // (which returns a Router), then add the raft services.
        // Otherwise, add raft services directly.
        let router = if let Some(extra) = self.extra_services {
            builder
                .add_routes(extra)
                .add_service(ZoneTransportServiceServer::new(raft_service))
                .add_service(ZoneApiServiceServer::new(client_service))
        } else {
            builder
                .add_service(ZoneTransportServiceServer::new(raft_service))
                .add_service(ZoneApiServiceServer::new(client_service))
        };

        router.serve(addr).await.map_err(TransportError::Tonic)?;

        Ok(())
    }

    /// Start the gRPC server with graceful shutdown.
    pub async fn serve_with_shutdown(
        self,
        shutdown: impl std::future::Future<Output = ()> + Send + 'static,
    ) -> Result<()> {
        let addr = self.config.bind_address;
        let tls_enabled = self.config.tls.is_some();
        tracing::info!(
            "Starting Raft gRPC server on {} (zones={}, tls={}, with shutdown signal)",
            addr,
            self.registry.list_zones().len(),
            tls_enabled,
        );

        let raft_service = ZoneTransportServiceImpl {
            registry: self.registry.clone(),
        };
        let client_service = ZoneApiServiceImpl {
            registry: self.registry.clone(),
            tls: self.config.tls.clone(),
            ca_key_pem: self.ca_key_pem.clone(),
            join_token_hash: self.join_token_hash.clone(),
            blob_fetcher_slot: self.blob_fetcher_slot.clone(),
        };

        let mut builder =
            lib::transport_primitives::apply_server_limits(tonic::transport::Server::builder());
        if let Some(ref tls) = self.config.tls {
            let identity = tonic::transport::Identity::from_pem(&tls.cert_pem, &tls.key_pem);
            let client_ca = tonic::transport::Certificate::from_pem(&tls.ca_pem);
            let tls_config = tonic::transport::ServerTlsConfig::new()
                .identity(identity)
                .client_ca_root(client_ca);
            builder = builder
                .tls_config(tls_config)
                .map_err(|e| TransportError::Connection(format!("TLS config error: {}", e)))?;
            tracing::info!("TLS mode: mTLS (client auth required)");
        }

        let router = if let Some(extra) = self.extra_services {
            builder
                .add_routes(extra)
                .add_service(ZoneTransportServiceServer::new(raft_service))
                .add_service(ZoneApiServiceServer::new(client_service))
        } else {
            builder
                .add_service(ZoneTransportServiceServer::new(raft_service))
                .add_service(ZoneApiServiceServer::new(client_service))
        };

        router
            .serve_with_shutdown(addr, shutdown)
            .await
            .map_err(TransportError::Tonic)?;

        Ok(())
    }
}

// =============================================================================
// Helpers
// =============================================================================

/// Convert milliseconds to seconds using ceiling division.
///
/// Prevents sub-second TTLs from silently truncating to zero.
/// E.g., 999ms → 1s, 1000ms → 1s, 1001ms → 2s.
/// Negative values clamp to 0.
fn ms_to_secs_ceil(ms: i64) -> u32 {
    if ms <= 0 {
        return 0;
    }
    (ms as u64).div_ceil(1000) as u32
}

/// Convert protobuf RaftCommand to internal Command enum.
fn proto_command_to_internal(proto: RaftCommand) -> Option<Command> {
    match proto.command? {
        ProtoCommandVariant::PutMetadata(pm) => {
            let metadata = pm.metadata?;
            let key = metadata.path.clone();
            Some(Command::SetMetadata {
                key,
                value: prost::Message::encode_to_vec(&metadata),
            })
        }
        ProtoCommandVariant::DeleteMetadata(dm) => Some(Command::DeleteMetadata { key: dm.path }),
        ProtoCommandVariant::AcquireLock(al) => Some(Command::AcquireLock {
            path: al.lock_id.clone(),
            lock_id: al.holder_id.clone(),
            max_holders: 1, // Default to mutex
            ttl_secs: ms_to_secs_ceil(al.ttl_ms),
            holder_info: al.holder_id,
            now_secs: crate::prelude::FullStateMachine::now(),
        }),
        ProtoCommandVariant::ReleaseLock(rl) => Some(Command::ReleaseLock {
            path: rl.lock_id.clone(),
            lock_id: rl.holder_id,
        }),
        ProtoCommandVariant::ExtendLock(el) => Some(Command::ExtendLock {
            path: el.lock_id.clone(),
            lock_id: el.holder_id,
            new_ttl_secs: ms_to_secs_ceil(el.ttl_ms),
            now_secs: crate::prelude::FullStateMachine::now(),
        }),
    }
}

/// Look up a zone's ZoneConsensus from the registry, or return a gRPC error.
#[allow(clippy::result_large_err)]
fn get_zone_node(
    registry: &ZoneRaftRegistry,
    zone_id: &str,
) -> std::result::Result<ZoneConsensus<FullStateMachine>, Status> {
    registry.get_node(zone_id).ok_or_else(|| {
        Status::not_found(format!(
            "zone '{}' not found on this node",
            if zone_id.is_empty() {
                "<empty>"
            } else {
                zone_id
            }
        ))
    })
}

/// Convert internal CommandResult to proto RaftResponse.
fn command_result_to_proto(result: &CommandResult) -> RaftResponse {
    match result {
        CommandResult::Success => RaftResponse {
            success: true,
            error: None,
            result: None,
        },
        CommandResult::Value(_) => RaftResponse {
            success: true,
            error: None,
            result: None,
        },
        CommandResult::LockResult(lock_state) => {
            let first_holder = lock_state.holders.first();
            RaftResponse {
                success: true,
                error: None,
                result: Some(ProtoResponseResultVariant::LockResult(LockResult {
                    acquired: lock_state.acquired,
                    current_holder: first_holder.map(|h| h.holder_info.clone()),
                    expires_at_ms: first_holder
                        .map(|h| (h.expires_at * 1000) as i64)
                        .unwrap_or(0),
                })),
            }
        }
        CommandResult::CasResult { success, .. } => RaftResponse {
            success: *success,
            error: if *success {
                None
            } else {
                Some("CAS conflict".to_string())
            },
            result: None,
        },
        CommandResult::Error(e) => RaftResponse {
            success: false,
            error: Some(e.clone()),
            result: None,
        },
    }
}

/// Parse raw raft-rs message bytes, step into the given ZoneConsensus node,
/// and return a StepMessageResponse.
///
/// Shared by both `ZoneTransportServiceImpl::step_message` (fullnode) and
/// `WitnessServiceImpl::step_message` (witness) to avoid duplicated parsing
/// and stepping logic.
async fn parse_and_step_message<S: crate::raft::StateMachine + Send + Sync + 'static>(
    node: &ZoneConsensus<S>,
    message_bytes: &[u8],
    zone_id: &str,
    log_prefix: &str,
) -> std::result::Result<Response<StepMessageResponse>, Status> {
    let mut msg = match raft::eraftpb::Message::parse_from_bytes(message_bytes) {
        Ok(m) => m,
        Err(e) => {
            return Ok(Response::new(StepMessageResponse {
                success: false,
                error: Some(format!("Failed to deserialize raft message: {}", e)),
            }));
        }
    };

    tracing::trace!(
        "{} StepMessage [zone={}]: type={:?}, from={}, to={}, term={}",
        log_prefix,
        zone_id,
        msg.get_msg_type(),
        msg.from,
        msg.to,
        msg.term,
    );

    // raft-rs asserts when a heartbeat/append carries a committed index
    // beyond the follower's local log. Fresh full-node auto-join can see
    // exactly that window: the leader has compacted and will send a snapshot,
    // but ordinary heartbeats/appends may arrive first. Clamp the commit hint
    // to the highest index this message/local handle can actually account for;
    // the later snapshot or append will advance commit normally.
    if matches!(
        msg.get_msg_type(),
        raft::eraftpb::MessageType::MsgAppend | raft::eraftpb::MessageType::MsgHeartbeat
    ) {
        let local_last = node.last_index();
        let msg_last = msg.get_entries().last().map(|e| e.index).unwrap_or(0);
        let safe_commit = local_last.max(msg_last).max(node.applied_index());
        if msg.get_commit() > safe_commit {
            tracing::warn!(
                zone = %zone_id,
                from = msg.from,
                to = msg.to,
                msg_type = ?msg.get_msg_type(),
                leader_commit = msg.get_commit(),
                safe_commit,
                local_last,
                msg_last,
                "Clamping inbound raft commit hint until follower catches up",
            );
            msg.set_commit(safe_commit);
        }
    }

    if let Err(e) = node.step(msg).await {
        return Ok(Response::new(StepMessageResponse {
            success: false,
            error: Some(format!("Failed to step message: {}", e)),
        }));
    }

    Ok(Response::new(StepMessageResponse {
        success: true,
        error: None,
    }))
}

/// Check that a sender node is a known member of a zone.
///
/// Extract hostnames from a node_address for cert SAN inclusion.
///
/// Parses addresses like "http://nexus-1:2126" or "0.0.0.0:2126" and returns
/// the hostname/IP portion. Multiple formats are handled gracefully.
fn extract_hostnames(node_address: &str) -> Vec<String> {
    let addr = node_address
        .trim_start_matches("http://")
        .trim_start_matches("https://");

    // Split off port
    let host = if let Some((h, _)) = addr.rsplit_once(':') {
        h
    } else {
        addr
    };

    if host.is_empty() || host == "0.0.0.0" || host == "localhost" || host == "127.0.0.1" {
        return vec![];
    }

    vec![host.to_string()]
}

// =============================================================================
// ZoneTransportService (internal node-to-node transport)
// =============================================================================

/// Zone-routed implementation of the ZoneTransportService gRPC trait.
///
/// All raft-rs message types (~15 types including votes, heartbeats, appends)
/// are multiplexed through `step_message` as opaque protobuf v2 bytes
/// (etcd/tikv pattern).
struct ZoneTransportServiceImpl {
    registry: Arc<ZoneRaftRegistry>,
}

#[tonic::async_trait]
impl ZoneTransportService for ZoneTransportServiceImpl {
    /// Handle a raw raft-rs message forwarded from another node.
    ///
    /// Routes by zone_id to the correct Raft group's ZoneConsensus.
    /// Unknown zones return `NotFound` — the local storage enumeration at
    /// `PyZoneManager::new` (R15.e) is the authority for which groups this
    /// node hosts. New dynamic zones arrive via `federation_create_zone`;
    /// new replicas of existing zones arrive via leader snapshot delivery
    /// after the ConfChange commits. No side-effectful auto-reopen from
    /// disk in the message hot path.
    async fn step_message(
        &self,
        request: Request<StepMessageRequest>,
    ) -> std::result::Result<Response<StepMessageResponse>, Status> {
        let req = request.into_inner();
        let peek = raft::eraftpb::Message::parse_from_bytes(&req.message).ok();
        let node = match self.registry.get_node(&req.zone_id) {
            Some(n) => n,
            None => {
                if self.registry.is_auto_join_suppressed(&req.zone_id) {
                    return Err(Status::not_found(format!(
                        "zone '{}' was recently removed on this node",
                        req.zone_id
                    )));
                }
                let root_peers = self
                    .registry
                    .get_peers(contracts::ROOT_ZONE_ID)
                    .ok_or_else(|| {
                        Status::not_found(format!("zone '{}' not found on this node", req.zone_id))
                    })?;
                // Auto-join membership is gated by the transport
                // layer's mTLS (when enabled).  Sender address is
                // learned from `req.sender_address` after the
                // join — under the opaque-ID contract the
                // peer-map is the runtime SSOT (populated by inbound
                // StepMessage), so a static "is sender in root
                // peer-map" check was self-defeating: it rejected
                // legit fresh joiners whose addresses had not yet
                // been learned.
                let peers: Vec<NodeAddress> = root_peers.values().cloned().collect();
                let handle = tokio::runtime::Handle::current();
                self.registry
                    .join_zone(&req.zone_id, peers, false, &handle)
                    .map_err(|e| {
                        Status::internal(format!(
                            "failed to auto-join zone '{}': {}",
                            req.zone_id, e
                        ))
                    })?
            }
        };

        // Learn the sender's advertise address from this inbound
        // StepMessage — every received message proves the sender's
        // reachable address, satisfying the transport peer-map's
        // runtime SSOT under the opaque-ID contract.  Done BEFORE
        // any raft step so outbound responses route correctly.
        //
        // No separate membership check: the peer-map *is* the
        // membership SSOT (populated via inbound StepMessage +
        // ConfChange apply).  The OLD env-seeded `peer_map` was
        // strict-fail under random data-plane ids — a legit
        // post-JoinZone follower would have its leader's first
        // heartbeat rejected before it could install ConfState.
        // Authentication is the transport layer's job (mTLS when
        // enabled).
        if let Some(peek) = peek.as_ref() {
            self.registry
                .learn_peer_address(&req.zone_id, peek.from, &req.sender_address);
        }

        parse_and_step_message(&node, &req.message, &req.zone_id, "").await
    }

    /// Handle EC replication entries from a peer (Phase C).
    ///
    /// Deserializes each entry's command bytes and applies to the local state
    /// machine via `apply_ec_from_peer`. Returns the highest seq applied.
    async fn replicate_entries(
        &self,
        request: Request<ReplicateEntriesRequest>,
    ) -> std::result::Result<Response<ReplicateEntriesResponse>, Status> {
        let req = request.into_inner();
        let node = get_zone_node(&self.registry, &req.zone_id)?;

        // EC replication membership: like step_message, the peer-map
        // is the runtime SSOT under the opaque-ID contract; a static
        // "sender in env-derived peer set" check rejected legit
        // post-JoinZone replicas before they could learn each
        // other's addresses.  Authentication is the transport
        // layer's job (mTLS).
        let mut max_applied: u64 = 0;

        for entry in &req.entries {
            let command: Command = match bincode::deserialize(&entry.command) {
                Ok(cmd) => cmd,
                Err(e) => {
                    tracing::warn!(seq = entry.seq, "Failed to deserialize EC entry: {}", e);
                    continue;
                }
            };

            match node.apply_ec_from_peer(command, entry.timestamp).await {
                Ok(_) => {
                    max_applied = max_applied.max(entry.seq);
                    tracing::trace!(
                        seq = entry.seq,
                        zone = req.zone_id,
                        from = req.sender_node_id,
                        "Applied EC entry from peer"
                    );
                }
                Err(e) => {
                    tracing::warn!(seq = entry.seq, "Failed to apply EC entry from peer: {}", e);
                    // Return what we've applied so far
                    return Ok(Response::new(ReplicateEntriesResponse {
                        success: false,
                        error: Some(format!("Failed at seq {}: {}", entry.seq, e)),
                        applied_up_to: max_applied,
                    }));
                }
            }
        }

        Ok(Response::new(ReplicateEntriesResponse {
            success: true,
            error: None,
            applied_up_to: max_applied,
        }))
    }
}

// =============================================================================
// ZoneApiService (client-facing: Propose/Query/GetClusterInfo)
// =============================================================================

/// Zone-routed implementation of the ZoneApiService gRPC trait.
struct ZoneApiServiceImpl {
    registry: Arc<ZoneRaftRegistry>,
    /// TLS config (for CA cert access in JoinCluster handler).
    tls: Option<super::TlsConfig>,
    /// CA private key bytes — held in memory for server-side cert signing.
    ca_key_pem: Option<Vec<u8>>,
    /// SHA-256 hash of the join token password — for JoinCluster verification.
    join_token_hash: Option<String>,
    /// Optional late-bound `BlobFetcher` for `ReadBlob`. Empty slot
    /// (or `None` here) → `read_blob` returns `NotFound`.
    blob_fetcher_slot: Option<BlobFetcherSlot>,
}

#[tonic::async_trait]
impl ZoneApiService for ZoneApiServiceImpl {
    /// Handle a client proposal (write operation).
    async fn propose(
        &self,
        request: Request<ProposeRequest>,
    ) -> std::result::Result<Response<ProposeResponse>, Status> {
        let req = request.into_inner();
        let node = get_zone_node(&self.registry, &req.zone_id)?;
        let peers = self.registry.get_peers(&req.zone_id).unwrap_or_default();

        tracing::debug!(
            "Received propose request: zone={}, id={:?}, forwarded={}",
            req.zone_id,
            req.request_id,
            req.forwarded,
        );

        // Deserialize command: prefer raw_command (bincode, from internal forwarding)
        // over proto command (from external clients).
        let cmd = if !req.raw_command.is_empty() {
            match bincode::deserialize::<Command>(&req.raw_command) {
                Ok(c) => c,
                Err(e) => {
                    return Ok(Response::new(ProposeResponse {
                        success: false,
                        error: Some(format!("Failed to deserialize raw command: {}", e)),
                        leader_address: None,
                        result: None,
                        applied_index: 0,
                    }));
                }
            }
        } else {
            let proto_cmd = match req.command {
                Some(cmd) => cmd,
                None => {
                    return Ok(Response::new(ProposeResponse {
                        success: false,
                        error: Some("No command provided".to_string()),
                        leader_address: None,
                        result: None,
                        applied_index: 0,
                    }));
                }
            };

            match proto_command_to_internal(proto_cmd) {
                Some(c) => c,
                None => {
                    return Ok(Response::new(ProposeResponse {
                        success: false,
                        error: Some("Unsupported command type".to_string()),
                        leader_address: None,
                        result: None,
                        applied_index: 0,
                    }));
                }
            }
        };

        // Use submit_to_channel (leader-only, no forwarding) for forwarded
        // requests to prevent infinite loops. For direct requests, use
        // propose() which may forward to leader transparently.
        let result = if req.forwarded {
            // Forwarded request: must be handled locally (no re-forwarding)
            match node.submit_to_channel(cmd) {
                Ok(rx) => {
                    match tokio::time::timeout(std::time::Duration::from_secs(10), rx).await {
                        Ok(Ok(r)) => r,
                        Ok(Err(_)) => Err(RaftError::ProposalDropped),
                        Err(_) => Err(RaftError::Timeout(10)),
                    }
                }
                Err(e) => Err(e),
            }
        } else {
            node.propose(cmd).await
        };

        match result {
            Ok(result) => {
                let proto_result = command_result_to_proto(&result);
                Ok(Response::new(ProposeResponse {
                    success: true,
                    error: None,
                    leader_address: None,
                    result: Some(proto_result),
                    applied_index: 0,
                }))
            }
            Err(RaftError::NotLeader { leader_hint }) => {
                let addr = leader_hint
                    .and_then(|id| peers.get(&id))
                    .map(|a| a.endpoint.clone());
                Ok(Response::new(ProposeResponse {
                    success: false,
                    error: Some("Not the leader".to_string()),
                    leader_address: addr,
                    result: None,
                    applied_index: 0,
                }))
            }
            Err(e) => Ok(Response::new(ProposeResponse {
                success: false,
                error: Some(format!("Proposal failed: {}", e)),
                leader_address: None,
                result: None,
                applied_index: 0,
            })),
        }
    }

    /// Handle a client query (read operation).
    async fn query(
        &self,
        request: Request<QueryRequest>,
    ) -> std::result::Result<Response<QueryResponse>, Status> {
        let req = request.into_inner();
        let node = get_zone_node(&self.registry, &req.zone_id)?;
        let peers = self.registry.get_peers(&req.zone_id).unwrap_or_default();

        tracing::debug!(
            "Received query request: zone={}, read_from_leader={}",
            req.zone_id,
            req.read_from_leader
        );

        if req.read_from_leader && !node.is_leader() {
            let leader_addr = node
                .leader_id()
                .and_then(|id| peers.get(&id))
                .map(|a| a.endpoint.clone());
            return Ok(Response::new(QueryResponse {
                success: false,
                error: Some("Not the leader".to_string()),
                leader_address: leader_addr,
                result: None,
            }));
        }

        let proto_query = match req.query {
            Some(q) => q,
            None => {
                return Ok(Response::new(QueryResponse {
                    success: false,
                    error: Some("No query provided".to_string()),
                    leader_address: None,
                    result: None,
                }));
            }
        };

        let query_result = match proto_query.query {
            Some(ProtoQueryVariant::GetMetadata(gm)) => {
                node.with_state_machine(|sm| match sm.get_metadata(&gm.path) {
                    Ok(Some(data)) => {
                        let metadata =
                            super::proto::nexus::core::FileMetadata::decode(data.as_slice()).ok();
                        RaftQueryResponse {
                            success: true,
                            error: None,
                            result: Some(ProtoQueryResultVariant::GetMetadataResult(
                                GetMetadataResult { metadata },
                            )),
                        }
                    }
                    Ok(None) => RaftQueryResponse {
                        success: true,
                        error: None,
                        result: Some(ProtoQueryResultVariant::GetMetadataResult(
                            GetMetadataResult { metadata: None },
                        )),
                    },
                    Err(e) => RaftQueryResponse {
                        success: false,
                        error: Some(format!("Query failed: {}", e)),
                        result: None,
                    },
                })
                .await
            }
            Some(ProtoQueryVariant::ListMetadata(lm)) => {
                node.with_state_machine(|sm| match sm.list_metadata(&lm.prefix) {
                    Ok(items) => {
                        let proto_items: Vec<_> = items
                            .into_iter()
                            .filter_map(|(_, data)| {
                                super::proto::nexus::core::FileMetadata::decode(data.as_slice())
                                    .ok()
                            })
                            .take(if lm.limit > 0 {
                                lm.limit as usize
                            } else {
                                usize::MAX
                            })
                            .collect();
                        RaftQueryResponse {
                            success: true,
                            error: None,
                            result: Some(ProtoQueryResultVariant::ListMetadataResult(
                                ListMetadataResult {
                                    items: proto_items,
                                    next_cursor: None,
                                    has_more: false,
                                },
                            )),
                        }
                    }
                    Err(e) => RaftQueryResponse {
                        success: false,
                        error: Some(format!("List failed: {}", e)),
                        result: None,
                    },
                })
                .await
            }
            Some(ProtoQueryVariant::GetLockInfo(gli)) => {
                node.with_state_machine(|sm| match sm.get_lock(&gli.lock_id) {
                    Ok(Some(lock_info)) => {
                        let first_holder = lock_info.holders.first();
                        RaftQueryResponse {
                            success: true,
                            error: None,
                            result: Some(ProtoQueryResultVariant::LockInfoResult(LockInfoResult {
                                exists: true,
                                holder_id: first_holder.map(|h| h.holder_info.clone()),
                                expires_at_ms: first_holder
                                    .map(|h| (h.expires_at * 1000) as i64)
                                    .unwrap_or(0),
                                max_holders: lock_info.max_holders as i32,
                                current_holders: lock_info.holders.len() as i32,
                            })),
                        }
                    }
                    Ok(None) => RaftQueryResponse {
                        success: true,
                        error: None,
                        result: Some(ProtoQueryResultVariant::LockInfoResult(LockInfoResult {
                            exists: false,
                            holder_id: None,
                            expires_at_ms: 0,
                            max_holders: 0,
                            current_holders: 0,
                        })),
                    },
                    Err(e) => RaftQueryResponse {
                        success: false,
                        error: Some(format!("Lock query failed: {}", e)),
                        result: None,
                    },
                })
                .await
            }
            None => RaftQueryResponse {
                success: false,
                error: Some("Unknown query type".to_string()),
                result: None,
            },
        };

        let error = query_result.error.clone();
        Ok(Response::new(QueryResponse {
            success: query_result.success,
            error,
            leader_address: None,
            result: Some(query_result),
        }))
    }

    /// Get cluster information for a zone.
    async fn get_cluster_info(
        &self,
        request: Request<GetClusterInfoRequest>,
    ) -> std::result::Result<Response<GetClusterInfoResponse>, Status> {
        let req = request.into_inner();
        let node = get_zone_node(&self.registry, &req.zone_id)?;
        let peers = self.registry.get_peers(&req.zone_id).unwrap_or_default();
        let node_id = self.registry.node_id();

        let is_leader = node.is_leader();
        let leader_id = node.leader_id().unwrap_or(0);
        let term = node.term();
        let leader_addr = peers.get(&leader_id).map(|a| a.endpoint.clone());

        let mut voters = vec![ProtoNodeInfo {
            id: node_id,
            address: peers
                .get(&node_id)
                .map(|a| a.endpoint.clone())
                .unwrap_or_default(),
            role: 0,
        }];
        for (id, addr) in &peers {
            voters.push(ProtoNodeInfo {
                id: *id,
                address: addr.endpoint.clone(),
                role: 0,
            });
        }

        Ok(Response::new(GetClusterInfoResponse {
            node_id,
            leader_id,
            term,
            config: Some(ProtoClusterConfig {
                voters,
                learners: vec![],
                witnesses: vec![],
            }),
            is_leader,
            leader_address: leader_addr,
        }))
    }

    async fn join_zone(
        &self,
        request: Request<JoinZoneRequest>,
    ) -> std::result::Result<Response<JoinZoneResponse>, Status> {
        let req = request.into_inner();
        let node = get_zone_node(&self.registry, &req.zone_id)?;

        // Only leader can process JoinZone — redirect followers
        if !node.is_leader() {
            let peers = self.registry.get_peers(&req.zone_id).unwrap_or_default();
            let leader_id = node.leader_id().unwrap_or(0);
            let leader_addr = peers.get(&leader_id).map(|a| a.endpoint.clone());
            return Ok(Response::new(JoinZoneResponse {
                success: false,
                error: Some("not leader".to_string()),
                leader_address: leader_addr,
                config: None,
            }));
        }

        tracing::info!(
            zone = req.zone_id,
            node_id = req.node_id,
            address = req.node_address,
            "JoinZone request received",
        );

        // Propose ConfChange with address in context (etcd pattern).
        // This waits for the ConfChange to be committed and applied.
        use raft::eraftpb::ConfChangeType;
        let change_type = if req.as_learner {
            ConfChangeType::AddLearnerNode
        } else {
            ConfChangeType::AddNode
        };
        match node
            .propose_conf_change(change_type, req.node_id, req.node_address.into_bytes())
            .await
        {
            Ok(conf_state) => {
                // Note: under the OLD dynamic-bootstrap contract,
                // JoinZone was synonymous with "the caller is mounting
                // this zone", so the handler bumped `i_links_count`
                // here.  Under the opaque-ID contract JoinZone is
                // voter-membership only — `bootstrap_or_join_root` and
                // the root-leader-gated `coordinator.create_zone`
                // follower path call it for raft membership without a
                // mount reference.  The mount-reference counter is
                // maintained at the actual mount-creation site (the
                // DT_MOUNT entry in the parent zone's metastore), not
                // here, so JoinZone no longer touches it.

                // Build ClusterConfig from the resulting ConfState + peer map
                let peers = self.registry.get_peers(&req.zone_id).unwrap_or_default();
                let voters: Vec<ProtoNodeInfo> = conf_state
                    .voters
                    .iter()
                    .map(|&id| ProtoNodeInfo {
                        id,
                        address: peers
                            .get(&id)
                            .map(|a| a.endpoint.clone())
                            .unwrap_or_default(),
                        role: 0,
                    })
                    .collect();

                Ok(Response::new(JoinZoneResponse {
                    success: true,
                    error: None,
                    leader_address: None,
                    config: Some(ProtoClusterConfig {
                        voters,
                        learners: vec![],
                        witnesses: vec![],
                    }),
                }))
            }
            Err(RaftError::NotLeader { leader_hint }) => {
                let peers = self.registry.get_peers(&req.zone_id).unwrap_or_default();
                let addr = leader_hint
                    .and_then(|id| peers.get(&id))
                    .map(|a| a.endpoint.clone());
                Ok(Response::new(JoinZoneResponse {
                    success: false,
                    error: Some("not leader".to_string()),
                    leader_address: addr,
                    config: None,
                }))
            }
            Err(e) => Ok(Response::new(JoinZoneResponse {
                success: false,
                error: Some(format!("JoinZone failed: {}", e)),
                leader_address: None,
                config: None,
            })),
        }
    }

    /// Remove this node's local replica for a zone.
    ///
    /// This is intentionally local-only. The caller is responsible for
    /// coordinating peer fan-out; handling the RPC this way prevents recursive
    /// delete storms while still making remove→recreate safe for dynamic zones.
    async fn delete_zone(
        &self,
        request: Request<DeleteZoneRequest>,
    ) -> std::result::Result<Response<DeleteZoneResponse>, Status> {
        let req = request.into_inner();
        let zone_id = req.zone_id.trim();
        if zone_id.is_empty() {
            return Ok(Response::new(DeleteZoneResponse {
                success: false,
                error: Some("zone_id must not be empty".to_string()),
            }));
        }
        if zone_id == contracts::ROOT_ZONE_ID {
            return Ok(Response::new(DeleteZoneResponse {
                success: false,
                error: Some("root zone cannot be deleted by DeleteZone".to_string()),
            }));
        }

        if self.registry.get_node(zone_id).is_none() {
            return Ok(Response::new(DeleteZoneResponse {
                success: true,
                error: None,
            }));
        }

        match self.registry.remove_zone(zone_id).await {
            Ok(()) => Ok(Response::new(DeleteZoneResponse {
                success: true,
                error: None,
            })),
            Err(e) => Ok(Response::new(DeleteZoneResponse {
                success: false,
                error: Some(e.to_string()),
            })),
        }
    }

    /// Handle a JoinCluster request — TLS certificate provisioning.
    ///
    /// Two auth modes:
    /// Authenticates with join token password (K3s-style).
    ///
    /// In both modes, the server signs a node certificate and returns CA + cert + key.
    /// The CA private key never leaves this process.
    async fn join_cluster(
        &self,
        request: Request<JoinClusterRequest>,
    ) -> std::result::Result<Response<JoinClusterResponse>, Status> {
        let req = request.into_inner();
        let err_resp = |msg: &str| {
            Response::new(JoinClusterResponse {
                success: false,
                error: Some(msg.to_string()),
                ca_pem: Vec::new(),
                node_cert_pem: Vec::new(),
                node_key_pem: Vec::new(),
            })
        };

        tracing::info!(
            node_id = req.node_id,
            node_address = req.node_address,
            zone_id = req.zone_id,
            "JoinCluster request received",
        );

        // --- Token-based authentication (K3s-style) ---
        let stored_hash = match &self.join_token_hash {
            Some(h) => h,
            None => {
                return Ok(err_resp(
                    "This node does not accept join requests (no join token configured)",
                ));
            }
        };
        let candidate_hash = {
            use sha2::{Digest, Sha256};
            use std::fmt::Write;
            let digest = Sha256::digest(req.password.as_bytes());
            let mut hex = String::with_capacity(64);
            for byte in &digest[..] {
                let _ = write!(hex, "{:02x}", byte);
            }
            hex
        };
        if candidate_hash != *stored_hash {
            tracing::warn!(node_id = req.node_id, "JoinCluster: invalid password");
            return Ok(err_resp("Invalid join token password"));
        }

        // --- Get CA material (static — set at startup) ---
        let (ca_pem, ca_key_pem) = match (&self.ca_key_pem, &self.tls) {
            (Some(ca_key), Some(tls)) => (tls.ca_pem.clone(), ca_key.clone()),
            _ => {
                return Ok(err_resp("CA material not configured on this node"));
            }
        };

        // --- Sign node certificate ---
        let zone_id = if req.zone_id.is_empty() {
            contracts::ROOT_ZONE_ID
        } else {
            &req.zone_id
        };
        let extra_hostnames = extract_hostnames(&req.node_address);
        let peer_hostname = extra_hostnames.first().map(|s| s.as_str());
        let (node_cert_pem, node_key_pem) = match super::certgen::generate_node_cert(
            req.node_id,
            zone_id,
            &ca_pem,
            &ca_key_pem,
            &extra_hostnames,
            peer_hostname,
        ) {
            Ok(pair) => pair,
            Err(e) => {
                tracing::error!("Failed to generate node cert: {}", e);
                return Ok(err_resp(&format!("Failed to generate node cert: {}", e)));
            }
        };

        tracing::info!(
            node_id = req.node_id,
            node_address = req.node_address,
            "JoinCluster: node certificate signed and provisioned successfully",
        );

        Ok(Response::new(JoinClusterResponse {
            success: true,
            error: None,
            ca_pem,
            node_cert_pem,
            node_key_pem,
        }))
    }

    /// Return search capabilities for a zone.
    ///
    /// Reads `{base_path}/{zone_id}/search_caps.json` on each RPC.
    /// Python search daemon writes the file at startup. Falls back to
    /// keyword-only defaults if the file is missing or malformed.
    async fn get_search_capabilities(
        &self,
        request: Request<GetSearchCapabilitiesRequest>,
    ) -> std::result::Result<Response<SearchCapabilities>, Status> {
        let req = request.into_inner();
        let zone_id = req.zone_id;

        if self.registry.get_node(&zone_id).is_none() {
            return Err(Status::not_found(format!("Zone '{}' not found", zone_id)));
        }

        let caps =
            crate::raft::read_search_caps(self.registry.base_path(), &zone_id).unwrap_or_default();

        Ok(Response::new(SearchCapabilities {
            zone_id,
            device_tier: caps.device_tier,
            search_modes: caps.search_modes,
            embedding_model: caps.embedding_model,
            embedding_dimensions: caps.embedding_dimensions,
            has_graph: caps.has_graph,
        }))
    }

    /// Serve a peer's content fetch — store-and-forward.
    ///
    /// One addressing mode: ``content_id`` is opaque to the kernel; the
    /// installed ``BlobFetcher`` impl drives the local read path which
    /// routes through ``VFSRouter`` exactly like a local ``sys_read``,
    /// letting each backend interpret ``content_id`` however it likes
    /// (CAS=hash, PAS=backend_path). No CAS-vs-PAS branch lives here.
    ///
    /// Delegates to the kernel-installed ``BlobFetcher``. The slot is
    /// late-bound: ``ZoneManager::new`` spawns the server before the
    /// kernel's root-mount backend is wired, so early calls arrive
    /// before the slot is populated. We treat every "no fetcher / not
    /// found" path as a ``NotFound`` result carried in ``error``.
    async fn read_blob(
        &self,
        request: Request<ReadBlobRequest>,
    ) -> std::result::Result<Response<ReadBlobResponse>, Status> {
        let req = request.into_inner();
        let fetcher = self
            .blob_fetcher_slot
            .as_ref()
            .and_then(|slot| slot.read().as_ref().cloned());
        let Some(fetcher) = fetcher else {
            return Ok(Response::new(ReadBlobResponse {
                content: Vec::new(),
                error: "blob fetcher not installed".to_string(),
            }));
        };
        match fetcher.read(&req.content_id).await {
            Ok(bytes) => Ok(Response::new(ReadBlobResponse {
                content: bytes,
                error: String::new(),
            })),
            Err(e) => Ok(Response::new(ReadBlobResponse {
                content: Vec::new(),
                error: e,
            })),
        }
    }
}

// =============================================================================
// Witness Zone Registry — multi-zone witness (mirrors ZoneRaftRegistry for
// FullStateMachine, but uses WitnessStateMachine and witness RaftConfig)
// =============================================================================

/// A single witness zone entry.
struct WitnessZoneEntry {
    node: ZoneConsensus<WitnessStateMachine>,
    /// Per-zone peer map — kept here so ``auto_join_zone`` can seed
    /// new child zones from root's *current* peer map (which has
    /// applied conf_change rotations) rather than the cold-start
    /// ``self.peers`` snapshot.  Per-zone autonomy stays intact: a
    /// child zone's later conf_change Removes only affect its own
    /// peer_map, never another zone's.
    peers: SharedPeerMap,
    shutdown_tx: tokio::sync::watch::Sender<bool>,
    _transport_handle: JoinHandle<()>,
}

/// Multi-zone registry for witness nodes.
///
/// Each zone gets its own `ZoneConsensus<WitnessStateMachine>` + `TransportLoop`.
/// The witness participates in leader election for every zone but never applies
/// state machine entries or serves reads.
pub struct WitnessZoneRegistry {
    zones: DashMap<String, WitnessZoneEntry>,
    base_path: PathBuf,
    node_id: u64,
    tls: Arc<RwLock<Option<super::TlsConfig>>>,
    /// Cluster peer addresses — used by auto_join_zone() for transport routing.
    peers: Vec<NodeAddress>,
    /// This node's advertise address — carried in outbound StepMessage.
    self_address: String,
    /// Serializes ``auto_join_zone`` so concurrent step_messages from
    /// multiple data nodes for the same fresh zone don't both pass the
    /// ``zones.get()`` check, both call ``setup_witness_zone``, and
    /// race on opening the same redb file (second open fails with
    /// "Database already open. Cannot acquire lock.").
    ///
    /// Coarse-grained — serializes auto-join across ALL zones — but
    /// setup is just a few ms of I/O and the witness handles only
    /// raft-membership traffic, so per-zone scaling isn't worth the
    /// complexity. Held across the entire ``zones.get`` →
    /// ``setup_witness_zone`` → ``zones.insert`` window so concurrent
    /// callers serialize and the second one finds the zone already
    /// inserted.
    auto_join_lock: parking_lot::Mutex<()>,
}

impl WitnessZoneRegistry {
    /// Create a new empty witness zone registry.
    pub fn new(base_path: PathBuf, node_id: u64, tls: Option<super::TlsConfig>) -> Self {
        Self {
            zones: DashMap::new(),
            base_path,
            node_id,
            tls: Arc::new(RwLock::new(tls)),
            peers: Vec::new(),
            self_address: String::new(),
            auto_join_lock: parking_lot::Mutex::new(()),
        }
    }

    /// Set the cluster peer addresses (called after parsing NEXUS_PEERS).
    pub fn set_peers(&mut self, peers: Vec<NodeAddress>) {
        self.peers = peers;
    }

    /// Set this node's advertise address — carried in outbound
    /// `StepMessageRequest.sender_address` so peers learn
    /// `(self.node_id -> address)` from inbound messages.
    pub fn set_self_address(&mut self, address: String) {
        self.self_address = address;
    }

    /// Get this node's advertise address (empty when unset).
    pub fn self_address(&self) -> &str {
        &self.self_address
    }

    /// Record a peer's advertise address learned from an inbound
    /// `StepMessage`.  Mirrors `ZoneRaftRegistry::learn_peer_address`
    /// so the witness peer-map converges via the network SSOT under
    /// the opaque-ID contract.  Returns `true` if the map changed.
    pub fn learn_peer_address(&self, zone_id: &str, peer_id: u64, endpoint: &str) -> bool {
        if endpoint.is_empty() || peer_id == 0 {
            return false;
        }
        let Some(entry) = self.zones.get(zone_id) else {
            return false;
        };
        let mut peers = entry.peers.write().unwrap();
        if let Some(existing) = peers.get(&peer_id) {
            if existing.endpoint == endpoint {
                return false;
            }
        }
        let use_tls = endpoint.starts_with("https://");
        let parsed = match NodeAddress::parse(endpoint, use_tls) {
            Ok(mut p) => {
                p.id = peer_id;
                p
            }
            Err(_) => return false,
        };
        peers.insert(peer_id, parsed);
        true
    }

    /// Create a witness Raft group for a zone (static bootstrap).
    ///
    /// Opens zone-specific storage at `{base_path}/{zone_id}/`, creates a
    /// `ZoneConsensus<WitnessStateMachine>`, and spawns a `TransportLoop` task.
    #[allow(clippy::result_large_err)]
    pub fn create_zone(
        &self,
        zone_id: &str,
        peers: Vec<NodeAddress>,
        runtime_handle: &tokio::runtime::Handle,
    ) -> Result<ZoneConsensus<WitnessStateMachine>> {
        use crate::raft::RaftConfig;

        if self.zones.contains_key(zone_id) {
            return Err(TransportError::Connection(format!(
                "Witness zone '{}' already exists",
                zone_id
            )));
        }

        // Witness RaftConfig (no replication log, cannot become leader)
        let peer_ids: Vec<u64> = peers.iter().map(|p| p.id).collect();
        let config = RaftConfig::witness(self.node_id, peer_ids);

        self.setup_witness_zone(zone_id, config, peers, runtime_handle)
    }

    /// Bootstrap or join a zone at boot time under the opaque-ID
    /// contract.  Sends `JoinZone` RPC against each configured peer
    /// until one succeeds, then locally registers the zone with
    /// `skip_bootstrap=true` so the leader's snapshot installs the
    /// authoritative ConfState.  Looping is indefinite — misconfig
    /// (no reachable peer) surfaces as "witness stays up retrying"
    /// rather than a silent exit.
    ///
    /// Witness joins as a **voter** (`as_learner=false`) by default,
    /// matching TiKV's witness-as-voter pattern: votes in elections
    /// without applying state-machine entries (`is_witness=true` in
    /// the local RaftConfig).  Pass `as_learner=true` for log-shipping
    /// observers that don't count toward quorum.
    ///
    /// Returns the joined zone's `ZoneConsensus` handle.
    #[allow(clippy::result_large_err)]
    pub async fn bootstrap_or_join_zone(
        self: &Arc<Self>,
        zone_id: &str,
        as_learner: bool,
        timeout_secs: u64,
        retry_interval: std::time::Duration,
    ) -> Result<ZoneConsensus<WitnessStateMachine>> {
        use super::call_join_zone_rpc;

        if let Some(existing) = self.zones.get(zone_id) {
            return Ok(existing.node.clone());
        }

        let peers = self.peers.clone();
        let self_address = self.self_address.clone();
        loop {
            for peer in &peers {
                if peer.id == self.node_id {
                    continue;
                }
                let mut endpoint = peer.endpoint.clone();
                let mut redirected_once = false;
                loop {
                    match call_join_zone_rpc(
                        &endpoint,
                        zone_id,
                        self.node_id,
                        &self_address,
                        as_learner,
                        timeout_secs,
                    )
                    .await
                    {
                        Ok(result) if result.success => {
                            tracing::info!(
                                zone = %zone_id,
                                endpoint = %endpoint,
                                witness_id = self.node_id,
                                "Witness joined zone via leader",
                            );
                            return self
                                .auto_join_zone(zone_id, &tokio::runtime::Handle::current());
                        }
                        Ok(result) => {
                            if let Some(addr) = result.leader_address.as_ref() {
                                if !redirected_once && !addr.is_empty() && addr != &endpoint {
                                    tracing::info!(
                                        from = %endpoint,
                                        to = %addr,
                                        "Witness JoinZone redirect to leader",
                                    );
                                    endpoint = addr.clone();
                                    redirected_once = true;
                                    continue;
                                }
                            }
                            tracing::debug!(
                                endpoint = %endpoint,
                                error = ?result.error,
                                "Witness JoinZone non-success; trying next peer",
                            );
                            break;
                        }
                        Err(e) => {
                            tracing::debug!(
                                endpoint = %endpoint,
                                error = %e,
                                "Witness JoinZone RPC error; trying next peer",
                            );
                            break;
                        }
                    }
                }
            }
            tokio::time::sleep(retry_interval).await;
        }
    }

    /// Auto-join a zone when receiving Raft messages for an unknown zone.
    ///
    /// Creates a witness Raft group with `skip_bootstrap=true` (empty ConfState).
    /// The leader will send a snapshot with the correct ConfState — this is the
    /// standard raft-rs contract for late-joining nodes.
    ///
    /// Used by step_message() handler for dynamic zone support.
    #[allow(clippy::result_large_err)]
    pub fn auto_join_zone(
        &self,
        zone_id: &str,
        runtime_handle: &tokio::runtime::Handle,
    ) -> Result<ZoneConsensus<WitnessStateMachine>> {
        use crate::raft::RaftConfig;

        // Fast path: zone already joined, no need to take the auto_join
        // lock. Lock acquisition still races on the bucket but DashMap
        // handles that, and the common case (subsequent step_messages
        // for an already-joined zone) stays uncontended.
        if let Some(existing) = self.zones.get(zone_id) {
            return Ok(existing.node.clone());
        }

        // Slow path: hold the auto-join lock across the entire
        // re-check + setup + insert sequence so two concurrent
        // step_messages for a fresh zone can't both reach
        // ``setup_witness_zone`` and race on the redb file lock.
        // Without this, the second caller hits "Database already
        // open" and the witness ends up with no entry for the zone
        // (subsequent step_messages keep retrying auto-join, never
        // landing in the registry, witness never votes).
        let _guard = self.auto_join_lock.lock();
        if let Some(existing) = self.zones.get(zone_id) {
            return Ok(existing.node.clone());
        }

        let peers = self.current_auto_join_peers();
        let peer_ids: Vec<u64> = peers.iter().map(|p| p.id).collect();
        let config = if peer_ids.is_empty() {
            // No known roster yet: start uninitialized and wait for a
            // snapshot to carry ConfState.
            RaftConfig {
                id: self.node_id,
                peers: vec![],
                is_witness: true,
                skip_bootstrap: true,
                election_tick: 10_000_000, // witness never initiates election
                ..Default::default()
            }
        } else {
            // Dynamic federation zones are initially bootstrapped from a
            // static voter roster; that ConfState is not a log entry. If the
            // witness auto-joins with an empty ConfState it can replay appends
            // but never learn that it may vote, so failover loses quorum.
            RaftConfig::witness(self.node_id, peer_ids)
        };
        self.setup_witness_zone(zone_id, config, peers, runtime_handle)
    }

    fn current_auto_join_peers(&self) -> Vec<NodeAddress> {
        if let Some(root) = self.zones.get(contracts::ROOT_ZONE_ID) {
            let peers = root.peers.read().unwrap();
            if !peers.is_empty() {
                return peers.values().cloned().collect();
            }
        }
        self.peers.clone()
    }

    /// Internal: open storage, create ZoneConsensus + driver, spawn transport loop, register zone.
    ///
    /// Shared by `create_zone()` (static bootstrap) and `auto_join_zone()` (dynamic federation).
    #[allow(clippy::result_large_err)]
    fn setup_witness_zone(
        &self,
        zone_id: &str,
        config: crate::raft::RaftConfig,
        mut peers: Vec<NodeAddress>,
        runtime_handle: &tokio::runtime::Handle,
    ) -> Result<ZoneConsensus<WitnessStateMachine>> {
        use crate::raft::RaftStorage;
        use crate::transport::{ClientConfig, RaftClientPool, TransportLoop};
        use raft::Storage;

        // Zone-specific storage
        let zone_path = self.base_path.join(zone_id);
        let store = RedbStore::open(zone_path.join("sm"))
            .map_err(|e| TransportError::Connection(format!("Failed to open store: {}", e)))?;
        let raft_storage = RaftStorage::open(zone_path.join("raft")).map_err(|e| {
            TransportError::Connection(format!("Failed to open raft storage: {}", e))
        })?;
        if let Ok(initial_state) = raft_storage.initial_state() {
            reconcile_peers_with_conf_state(zone_id, &mut peers, &initial_state.conf_state);
        }
        let state_machine = WitnessStateMachine::new(&store).map_err(|e| {
            TransportError::Connection(format!("Failed to create witness state machine: {}", e))
        })?;

        let (handle, mut driver) = ZoneConsensus::new(config, raft_storage, state_machine, None)
            .map_err(|e| {
                TransportError::Connection(format!("Failed to create witness ZoneConsensus: {}", e))
            })?;

        // Shared peer map
        let peer_map: HashMap<u64, NodeAddress> = peers.into_iter().map(|p| (p.id, p)).collect();
        let shared_peers: super::SharedPeerMap = Arc::new(RwLock::new(peer_map));
        driver.set_peer_map(shared_peers.clone());

        // Spawn transport loop with zone_id routing
        let client_config = ClientConfig {
            tls: self.tls.clone(),
            ..Default::default()
        };
        let transport_loop = TransportLoop::new(
            driver,
            shared_peers.clone(),
            RaftClientPool::with_config(client_config),
        )
        .with_zone_id(zone_id.to_string())
        .with_self_address(self.self_address.clone());

        let (shutdown_tx, shutdown_rx) = tokio::sync::watch::channel(false);
        let transport_handle = runtime_handle.spawn(transport_loop.run(shutdown_rx));

        tracing::info!(
            "Witness zone '{}' registered (node_id={})",
            zone_id,
            self.node_id,
        );

        self.zones.insert(
            zone_id.to_string(),
            WitnessZoneEntry {
                node: handle.clone(),
                peers: shared_peers,
                shutdown_tx,
                _transport_handle: transport_handle,
            },
        );

        Ok(handle)
    }

    /// Get the ZoneConsensus handle for a zone.
    pub fn get_node(&self, zone_id: &str) -> Option<ZoneConsensus<WitnessStateMachine>> {
        self.zones.get(zone_id).map(|e| e.node.clone())
    }

    /// List all zone IDs.
    pub fn list_zones(&self) -> Vec<String> {
        self.zones.iter().map(|e| e.key().clone()).collect()
    }

    /// Shutdown all zones.
    pub fn shutdown_all(&self) {
        for entry in self.zones.iter() {
            let _ = entry.shutdown_tx.send(true);
        }
        self.zones.clear();
        tracing::info!("All witness zones shut down");
    }
}

// =============================================================================
// Witness gRPC Server (zone-routed, serves all witness zones on one port)
// =============================================================================

/// A gRPC server for multi-zone Raft witness nodes.
///
/// Routes incoming `step_message` requests to the correct zone's
/// `ZoneConsensus<WitnessStateMachine>` by `zone_id`.
pub struct RaftWitnessServer {
    config: ServerConfig,
    registry: Arc<WitnessZoneRegistry>,
}

impl RaftWitnessServer {
    /// Create a witness server backed by a multi-zone registry.
    pub fn new(registry: Arc<WitnessZoneRegistry>, config: ServerConfig) -> Self {
        Self { config, registry }
    }

    /// Get the bind address.
    pub fn bind_address(&self) -> SocketAddr {
        self.config.bind_address
    }

    /// Start the gRPC server with graceful shutdown.
    pub async fn serve_with_shutdown(
        self,
        shutdown: impl std::future::Future<Output = ()> + Send + 'static,
    ) -> Result<()> {
        let addr = self.config.bind_address;
        let tls_enabled = self.config.tls.is_some();
        let zone_count = self.registry.list_zones().len();
        tracing::info!(
            "Starting Raft Witness gRPC server on {} (tls={}, zones={})",
            addr,
            tls_enabled,
            zone_count,
        );

        let service = WitnessServiceImpl {
            registry: self.registry.clone(),
        };

        let mut builder =
            lib::transport_primitives::apply_server_limits(tonic::transport::Server::builder());
        if let Some(ref tls) = self.config.tls {
            let identity = tonic::transport::Identity::from_pem(&tls.cert_pem, &tls.key_pem);
            let client_ca = tonic::transport::Certificate::from_pem(&tls.ca_pem);
            let tls_config = tonic::transport::ServerTlsConfig::new()
                .identity(identity)
                .client_ca_root(client_ca);
            builder = builder
                .tls_config(tls_config)
                .map_err(|e| TransportError::Connection(format!("TLS config error: {}", e)))?;
        }

        builder
            .add_service(ZoneTransportServiceServer::new(service))
            .serve_with_shutdown(addr, shutdown)
            .await
            .map_err(TransportError::Tonic)?;

        Ok(())
    }
}

/// Witness implementation of ZoneTransportService — routes by zone_id.
struct WitnessServiceImpl {
    registry: Arc<WitnessZoneRegistry>,
}

#[tonic::async_trait]
impl ZoneTransportService for WitnessServiceImpl {
    /// Handle a raw raft-rs message forwarded from another node.
    ///
    /// Routes to the correct zone's ZoneConsensus by `req.zone_id`.
    /// Auto-joins unknown zones (dynamic federation support).
    async fn step_message(
        &self,
        request: Request<StepMessageRequest>,
    ) -> std::result::Result<Response<StepMessageResponse>, Status> {
        let req = request.into_inner();

        // Route by zone_id — auto-join if zone not found (dynamic federation)
        let node = match self.registry.get_node(&req.zone_id) {
            Some(n) => n,
            None => {
                // Auto-join: create witness zone with skip_bootstrap=true.
                // Leader will send snapshot with correct ConfState.
                let handle = tokio::runtime::Handle::current();
                match self.registry.auto_join_zone(&req.zone_id, &handle) {
                    Ok(n) => n,
                    Err(e) => {
                        return Err(Status::internal(format!(
                            "Failed to auto-join zone '{}': {}",
                            req.zone_id, e
                        )));
                    }
                }
            }
        };

        // Learn sender's advertise address from this inbound StepMessage.
        if let Ok(peek) = raft::eraftpb::Message::parse_from_bytes(&req.message) {
            self.registry
                .learn_peer_address(&req.zone_id, peek.from, &req.sender_address);
        }

        parse_and_step_message(&node, &req.message, &req.zone_id, "[Witness]").await
    }

    /// Witness nodes do not participate in EC replication.
    async fn replicate_entries(
        &self,
        _request: Request<ReplicateEntriesRequest>,
    ) -> std::result::Result<Response<ReplicateEntriesResponse>, Status> {
        Err(Status::unimplemented(
            "Witness nodes do not support EC replication",
        ))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_server_config_default() {
        let config = ServerConfig::default();
        assert_eq!(
            config.bind_address,
            "0.0.0.0:2026".parse::<SocketAddr>().unwrap()
        );
        assert_eq!(config.max_message_size, 64 * 1024 * 1024);
    }

    #[tokio::test]
    async fn test_zone_registry_server() {
        use tempfile::TempDir;

        let tmp_dir = TempDir::new().unwrap();
        let registry = Arc::new(ZoneRaftRegistry::new(tmp_dir.path().to_path_buf(), 1));

        let config = ServerConfig {
            bind_address: "127.0.0.1:0".parse().unwrap(),
            ..Default::default()
        };

        let server = RaftGrpcServer::new(registry, config);
        assert_eq!(
            server.bind_address(),
            "127.0.0.1:0".parse::<SocketAddr>().unwrap()
        );
    }

    /// R15.e: unknown zones must NOT be side-effectfully reopened on the
    /// message hot path. A step_message for a zone this node doesn't host
    /// returns NotFound without touching disk or mutating the registry.
    #[tokio::test]
    async fn test_step_message_unknown_zone_returns_not_found() {
        use tempfile::TempDir;

        let tmp_dir = TempDir::new().unwrap();
        let registry = Arc::new(ZoneRaftRegistry::new(tmp_dir.path().to_path_buf(), 1));
        let svc = super::ZoneTransportServiceImpl {
            registry: registry.clone(),
        };

        let req = Request::new(StepMessageRequest {
            zone_id: "corp-eng".to_string(),
            message: Vec::new(),
            sender_address: String::new(),
        });
        let err = svc.step_message(req).await.unwrap_err();
        assert_eq!(err.code(), tonic::Code::NotFound);
        // Registry must remain empty — no side-effect reopen.
        assert!(registry.list_zones().is_empty());
    }

    #[test]
    fn test_witness_auto_join_bootstraps_known_peer_roster() {
        use tempfile::TempDir;

        let tmp_dir = TempDir::new().unwrap();
        let runtime = tokio::runtime::Runtime::new().unwrap();
        let witness_id = NodeAddress::parse("witness:2126", false).unwrap().id;
        let mut registry = WitnessZoneRegistry::new(tmp_dir.path().to_path_buf(), witness_id, None);
        registry.set_peers(vec![
            NodeAddress::parse("nexus-1:2126", false).unwrap(),
            NodeAddress::parse("nexus-2:2126", false).unwrap(),
            NodeAddress::parse("witness:2126", false).unwrap(),
        ]);

        let node = registry
            .auto_join_zone("corp-eng", runtime.handle())
            .expect("witness auto-join");

        assert!(
            !node.config().skip_bootstrap,
            "known witness rosters must bootstrap ConfState so the witness can vote"
        );
        let mut peers = node.config().peers.clone();
        peers.sort_unstable();
        let mut expected = vec![
            NodeAddress::parse("nexus-1:2126", false).unwrap().id,
            NodeAddress::parse("nexus-2:2126", false).unwrap().id,
        ];
        expected.sort_unstable();
        assert_eq!(
            peers, expected,
            "witness RaftConfig.peers must include full voters and exclude self"
        );

        registry.shutdown_all();
    }

    // ---------------------------------------------------------------
    // TTL conversion boundary-value tests (Issue #3031 / 11A)
    // ---------------------------------------------------------------

    #[test]
    fn test_ms_to_secs_ceil_boundary_values() {
        // Zero stays zero
        assert_eq!(super::ms_to_secs_ceil(0), 0);

        // Sub-second values round UP to 1 (not down to 0)
        assert_eq!(super::ms_to_secs_ceil(1), 1);
        assert_eq!(super::ms_to_secs_ceil(500), 1);
        assert_eq!(super::ms_to_secs_ceil(999), 1);

        // Exact second boundary
        assert_eq!(super::ms_to_secs_ceil(1000), 1);

        // Just above boundary rounds up
        assert_eq!(super::ms_to_secs_ceil(1001), 2);
        assert_eq!(super::ms_to_secs_ceil(1500), 2);
        assert_eq!(super::ms_to_secs_ceil(1999), 2);
        assert_eq!(super::ms_to_secs_ceil(2000), 2);

        // Larger values
        assert_eq!(super::ms_to_secs_ceil(5000), 5);
        assert_eq!(super::ms_to_secs_ceil(5001), 6);
        assert_eq!(super::ms_to_secs_ceil(30_000), 30);

        // Negative values clamp to 0
        assert_eq!(super::ms_to_secs_ceil(-1), 0);
        assert_eq!(super::ms_to_secs_ceil(-1000), 0);
    }
}
