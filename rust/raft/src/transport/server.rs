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
    ClusterConfig as ProtoClusterConfig, GetClusterInfoRequest, GetClusterInfoResponse,
    GetMetadataResult, GetSearchCapabilitiesRequest, JoinClusterRequest, JoinClusterResponse,
    JoinZoneRequest, JoinZoneResponse, ListMetadataResult, LockInfoResult, LockResult,
    NodeInfo as ProtoNodeInfo, ProposeRequest, ProposeResponse, QueryRequest, QueryResponse,
    RaftCommand, RaftQueryResponse, RaftResponse, ReplicateEntriesRequest,
    ReplicateEntriesResponse, SearchCapabilities, StepMessageRequest, StepMessageResponse,
};
use super::{NodeAddress, Result, TransportError};
use crate::raft::{
    Command, CommandResult, FullStateMachine, RaftError, WitnessStateMachine, ZoneConsensus,
    ZoneRaftRegistry,
};
use crate::storage::RedbStore;
use bincode;
use dashmap::DashMap;
use prost::Message;
use protobuf::Message as ProtobufV2Message;
use std::collections::HashMap;
use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::{Arc, Mutex, RwLock};
use tokio::task::JoinHandle;
use tonic::{Request, Response, Status};

/// Versioned contract for forwarded raw-result encoding.
const FORWARDED_RESULT_PROTOCOL_VERSION: &str = "nexus-raft-forward-v1";

/// Default safety cap for witness dynamic auto-join zone creation.
const DEFAULT_WITNESS_MAX_AUTO_JOIN_ZONES: usize = 128;
/// Marker file created for zones admitted via dynamic witness auto-join.
const WITNESS_AUTO_JOIN_MARKER_FILE: &str = ".witness_auto_join";

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
}

impl RaftGrpcServer {
    pub fn new(registry: Arc<ZoneRaftRegistry>, config: ServerConfig) -> Self {
        Self {
            config,
            registry,
            ca_key_pem: None,
            join_token_hash: None,
        }
    }

    /// Set cluster join parameters for JoinCluster RPC support.
    pub fn with_join_config(mut self, ca_key_pem: Vec<u8>, join_token_hash: String) -> Self {
        self.ca_key_pem = Some(ca_key_pem);
        self.join_token_hash = Some(join_token_hash);
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
        };

        let mut builder = tonic::transport::Server::builder();
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

        builder
            .add_service(ZoneTransportServiceServer::new(raft_service))
            .add_service(ZoneApiServiceServer::new(client_service))
            .serve(addr)
            .await
            .map_err(TransportError::Tonic)?;

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
        };

        let mut builder = tonic::transport::Server::builder();
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

        builder
            .add_service(ZoneTransportServiceServer::new(raft_service))
            .add_service(ZoneApiServiceServer::new(client_service))
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
            // Witness-path gRPC transport predates F4 C1. The
            // `AcquireLock` proto has no `mode` field; default to
            // Exclusive to preserve pre-F4 semantics.
            mode: crate::prelude::LockMode::Exclusive,
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
    let msg = match raft::eraftpb::Message::parse_from_bytes(message_bytes) {
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

/// Check that a sender node is a known member of a zone.
///
/// This check is fail-closed: if peer membership is unavailable or empty,
/// remote senders are rejected.
#[allow(clippy::result_large_err)]
fn check_zone_membership(
    registry: &ZoneRaftRegistry,
    zone_id: &str,
    sender_node_id: u64,
) -> std::result::Result<(), Status> {
    if let Some(peers) = registry.get_peers(zone_id) {
        if !peers.is_empty() {
            if peers.contains_key(&sender_node_id) {
                return Ok(());
            }

            if sender_in_persisted_conf_state(registry, zone_id, sender_node_id) {
                tracing::warn!(
                    zone = zone_id,
                    sender = sender_node_id,
                    "Accepted sender from persisted ConfState despite runtime peer map mismatch"
                );
                return Ok(());
            }

            return Err(Status::permission_denied(format!(
                "node {} is not a member of zone '{}'",
                sender_node_id, zone_id,
            )));
        }
    }

    if sender_in_persisted_conf_state(registry, zone_id, sender_node_id) {
        return Ok(());
    }

    Err(Status::permission_denied(format!(
        "zone '{}' membership unavailable; rejecting sender {}",
        zone_id, sender_node_id
    )))
}

fn sender_in_persisted_conf_state(
    registry: &ZoneRaftRegistry,
    zone_id: &str,
    sender_node_id: u64,
) -> bool {
    registry.is_persisted_member_fresh(zone_id, sender_node_id)
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
    /// Auto-joins unknown zones (dynamic federation + node restart support).
    async fn step_message(
        &self,
        request: Request<StepMessageRequest>,
    ) -> std::result::Result<Response<StepMessageResponse>, Status> {
        let req = request.into_inner();
        let node = match get_zone_node(&self.registry, &req.zone_id) {
            Ok(n) => n,
            Err(_) => {
                // Zone not found in memory — only reopen if data exists on disk
                // (node restart scenario). For new dynamic zones, return not_found
                // and let federation_create_zone RPC handle proper creation.
                //
                // Disk check contract: if `{zone_path}/raft` exists on disk, the
                // zone was previously created and persisted Raft state (WAL + snapshots).
                // This is the same recovery pattern as redb/sled: durable storage on
                // disk is the source of truth for "has this zone ever been initialised".
                // We re-open with the existing ConfState rather than bootstrapping
                // a brand-new zone, which would corrupt the Raft log.
                let zone_path = self.registry.base_path().join(&req.zone_id);
                if !zone_path.join("raft").exists() {
                    // No prior Raft data — this is genuinely a new zone.
                    // Return not_found so the caller uses create_zone / federation RPC.
                    return Err(Status::not_found(format!(
                        "zone '{}' not found on this node",
                        req.zone_id
                    )));
                }
                // Restart recovery: zone data on disk, reopen with existing ConfState
                let handle = tokio::runtime::Handle::current();
                let peers = self.registry.get_all_peers();
                match self.registry.create_zone(&req.zone_id, peers, &handle) {
                    Ok(n) => {
                        tracing::info!("Fullnode auto-joined zone '{}'", req.zone_id);
                        n
                    }
                    Err(e) => {
                        return Err(Status::internal(format!(
                            "Failed to auto-join zone '{}': {}",
                            req.zone_id, e
                        )));
                    }
                }
            }
        };

        // Zone authorization: verify sender is a known zone member.
        // Parse once just to extract `from` for the membership check, then
        // delegate to the shared parse_and_step_message helper.
        if let Ok(peek) = raft::eraftpb::Message::parse_from_bytes(&req.message) {
            check_zone_membership(&self.registry, &req.zone_id, peek.from)?;
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

        // Zone authorization: verify sender is a known zone member
        check_zone_membership(&self.registry, &req.zone_id, req.sender_node_id)?;

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
                        raw_result: Vec::new(),
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
                        raw_result: Vec::new(),
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
                        raw_result: Vec::new(),
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
                let raw_result = bincode::serialize(&result).map_err(|e| {
                    Status::internal(format!("Failed to serialize command result: {}", e))
                })?;
                Ok(Response::new(ProposeResponse {
                    success: true,
                    error: None,
                    leader_address: None,
                    result: Some(proto_result),
                    applied_index: 0,
                    raw_result,
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
                    raw_result: Vec::new(),
                }))
            }
            Err(e) => Ok(Response::new(ProposeResponse {
                success: false,
                error: Some(format!("Proposal failed: {}", e)),
                leader_address: None,
                result: None,
                applied_index: 0,
                raw_result: Vec::new(),
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
            supports_raw_result: true,
            protocol_version: FORWARDED_RESULT_PROTOCOL_VERSION.to_string(),
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

        // Propose ConfChange(AddNode) with address in context (etcd pattern).
        // This waits for the ConfChange to be committed and applied.
        use raft::eraftpb::ConfChangeType;
        match node
            .propose_conf_change(
                ConfChangeType::AddNode,
                req.node_id,
                req.node_address.into_bytes(),
            )
            .await
        {
            Ok(conf_state) => {
                // Increment zone's i_links_count (Raft-replicated, atomic).
                // AdjustCounter does read-modify-write in apply() — no lost updates.
                // This runs on the leader, so the follower's join() can skip
                // _increment_links() and avoid a Raft leader-only-write violation.
                if let Err(e) = node
                    .propose(Command::AdjustCounter {
                        key: "__i_links_count__".to_string(),
                        delta: 1,
                    })
                    .await
                {
                    tracing::warn!(zone = req.zone_id, "Failed to increment i_links_count: {e}");
                }

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

    /// Return search capabilities for a zone (Issue #3147, Phase 2).
    ///
    /// Reads real capabilities set by Python via `ZoneManager.set_search_capabilities()`.
    /// Falls back to keyword-only defaults if Python hasn't registered capabilities yet.
    async fn get_search_capabilities(
        &self,
        request: Request<GetSearchCapabilitiesRequest>,
    ) -> std::result::Result<Response<SearchCapabilities>, Status> {
        let req = request.into_inner();
        let zone_id = req.zone_id;

        if self.registry.get_node(&zone_id).is_none() {
            return Err(Status::not_found(format!("Zone '{}' not found", zone_id)));
        }

        let caps = self
            .registry
            .get_search_capabilities(&zone_id)
            .unwrap_or_default();

        Ok(Response::new(SearchCapabilities {
            zone_id,
            device_tier: caps.device_tier,
            search_modes: caps.search_modes,
            embedding_model: caps.embedding_model,
            embedding_dimensions: caps.embedding_dimensions,
            has_graph: caps.has_graph,
        }))
    }
}

// =============================================================================
// Witness Zone Registry — multi-zone witness (mirrors ZoneRaftRegistry for
// FullStateMachine, but uses WitnessStateMachine and witness RaftConfig)
// =============================================================================

/// A single witness zone entry.
struct WitnessZoneEntry {
    node: ZoneConsensus<WitnessStateMachine>,
    peers: super::SharedPeerMap,
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
    auto_joined_zones: DashMap<String, ()>,
    base_path: PathBuf,
    node_id: u64,
    tls: Arc<RwLock<Option<super::TlsConfig>>>,
    /// Cluster peer addresses — used by auto_join_zone() for transport routing.
    peers: Vec<NodeAddress>,
    /// Safety cap to prevent unbounded dynamic zone creation.
    max_auto_join_zones: usize,
    /// Serializes auto-join admission so zone cap checks are race-free.
    auto_join_lock: Mutex<()>,
}

impl WitnessZoneRegistry {
    /// Create a new empty witness zone registry.
    pub fn new(base_path: PathBuf, node_id: u64, tls: Option<super::TlsConfig>) -> Self {
        Self {
            zones: DashMap::new(),
            auto_joined_zones: DashMap::new(),
            base_path,
            node_id,
            tls: Arc::new(RwLock::new(tls)),
            peers: Vec::new(),
            max_auto_join_zones: DEFAULT_WITNESS_MAX_AUTO_JOIN_ZONES,
            auto_join_lock: Mutex::new(()),
        }
    }

    /// Set the cluster peer addresses (called after parsing NEXUS_PEERS).
    pub fn set_peers(&mut self, peers: Vec<NodeAddress>) {
        self.peers = peers;
    }

    /// Set safety cap for dynamic auto-join zone creation.
    pub fn set_max_auto_join_zones(&mut self, max_auto_join_zones: usize) {
        self.max_auto_join_zones = max_auto_join_zones.max(1);
    }

    fn count_persisted_auto_joined_zones(&self) -> usize {
        std::fs::read_dir(&self.base_path)
            .ok()
            .into_iter()
            .flat_map(|entries| entries.flatten())
            .filter_map(|entry| {
                let file_type = entry.file_type().ok()?;
                if !file_type.is_dir() {
                    return None;
                }
                Some(entry.path().join(WITNESS_AUTO_JOIN_MARKER_FILE).exists())
            })
            .filter(|is_auto_joined| *is_auto_joined)
            .count()
    }

    /// Whether witness transport has TLS authentication enabled.
    pub fn is_tls_enabled(&self) -> bool {
        self.tls.read().unwrap().is_some()
    }

    /// Check whether a Raft sender ID is a configured cluster peer.
    pub fn is_known_peer(&self, peer_id: u64) -> bool {
        if self.peers.iter().any(|peer| peer.id == peer_id) {
            return true;
        }

        self.zones.iter().any(|entry| {
            let peers = entry.peers.read().unwrap();
            peers.contains_key(&peer_id)
        })
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

        let _guard = self.auto_join_lock.lock().unwrap();

        if let Some(existing) = self.zones.get(zone_id) {
            return Ok(existing.node.clone());
        }

        let zone_path = self.base_path.join(zone_id);
        let has_persisted_state = zone_path.join("raft").exists();
        let marker_path = zone_path.join(WITNESS_AUTO_JOIN_MARKER_FILE);
        let is_known_dynamic_zone =
            marker_path.exists() || self.auto_joined_zones.contains_key(zone_id);
        if !has_persisted_state
            && !is_known_dynamic_zone
            && self.count_persisted_auto_joined_zones() >= self.max_auto_join_zones
        {
            return Err(TransportError::Connection(format!(
                "Auto-join zone limit reached (limit={})",
                self.max_auto_join_zones
            )));
        }

        let mut created_marker = false;
        if !marker_path.exists() {
            std::fs::create_dir_all(&zone_path).map_err(|e| {
                TransportError::Connection(format!(
                    "Failed to create witness auto-join marker directory: {}",
                    e
                ))
            })?;
            std::fs::write(&marker_path, b"auto-joined").map_err(|e| {
                TransportError::Connection(format!(
                    "Failed to persist witness auto-join marker: {}",
                    e
                ))
            })?;
            created_marker = true;
        }

        // skip_bootstrap=true: empty ConfState, leader sends snapshot
        let config = RaftConfig {
            id: self.node_id,
            peers: vec![],
            is_witness: true,
            skip_bootstrap: true,
            election_tick: 10_000_000, // witness never initiates election
            ..Default::default()
        };

        let peers: Vec<NodeAddress> = self.peers.clone();
        let handle = match self.setup_witness_zone(zone_id, config, peers, runtime_handle) {
            Ok(handle) => handle,
            Err(err) => {
                if created_marker {
                    let _ = std::fs::remove_file(&marker_path);
                }
                return Err(err);
            }
        };
        if marker_path.exists() {
            self.auto_joined_zones.insert(zone_id.to_string(), ());
        }
        Ok(handle)
    }

    /// Internal: open storage, create ZoneConsensus + driver, spawn transport loop, register zone.
    ///
    /// Shared by `create_zone()` (static bootstrap) and `auto_join_zone()` (dynamic federation).
    #[allow(clippy::result_large_err)]
    fn setup_witness_zone(
        &self,
        zone_id: &str,
        config: crate::raft::RaftConfig,
        peers: Vec<NodeAddress>,
        runtime_handle: &tokio::runtime::Handle,
    ) -> Result<ZoneConsensus<WitnessStateMachine>> {
        use crate::raft::RaftStorage;
        use crate::transport::{ClientConfig, RaftClientPool, TransportLoop};

        // Zone-specific storage
        let zone_path = self.base_path.join(zone_id);
        let store = RedbStore::open(zone_path.join("sm"))
            .map_err(|e| TransportError::Connection(format!("Failed to open store: {}", e)))?;
        let raft_storage = RaftStorage::open(zone_path.join("raft")).map_err(|e| {
            TransportError::Connection(format!("Failed to open raft storage: {}", e))
        })?;
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
        .with_zone_id(zone_id.to_string());

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
                peers: shared_peers.clone(),
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
        self.auto_joined_zones.clear();
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

        let mut builder = tonic::transport::Server::builder();
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
        let parsed = raft::eraftpb::Message::parse_from_bytes(&req.message).map_err(|e| {
            Status::invalid_argument(format!("Failed to deserialize witness raft message: {}", e))
        })?;

        if !self.registry.is_known_peer(parsed.from) {
            return Err(Status::permission_denied(format!(
                "Rejecting witness message for zone '{}' from unknown peer {}",
                req.zone_id, parsed.from
            )));
        }

        // Route by zone_id — auto-join if zone not found (dynamic federation)
        let node = match self.registry.get_node(&req.zone_id) {
            Some(n) => n,
            None => {
                if !self.registry.is_tls_enabled() {
                    return Err(Status::permission_denied(
                        "Witness auto-join requires mTLS-authenticated transport",
                    ));
                }

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

    #[test]
    fn test_check_zone_membership_fails_closed_when_zone_membership_unavailable() {
        use tempfile::TempDir;
        use tonic::Code;

        let tmp_dir = TempDir::new().unwrap();
        let registry = ZoneRaftRegistry::new(tmp_dir.path().to_path_buf(), 1);

        let err = check_zone_membership(&registry, "root", 2).unwrap_err();
        assert_eq!(err.code(), Code::PermissionDenied);
    }

    #[test]
    fn test_check_zone_membership_rejects_local_sender_without_peer_map() {
        use tempfile::TempDir;
        use tonic::Code;

        let tmp_dir = TempDir::new().unwrap();
        let registry = ZoneRaftRegistry::new(tmp_dir.path().to_path_buf(), 1);

        let err = check_zone_membership(&registry, "root", 1).unwrap_err();
        assert_eq!(err.code(), Code::PermissionDenied);
    }

    #[test]
    fn test_check_zone_membership_uses_persisted_conf_state_fallback() {
        use raft::eraftpb::ConfState;
        use tempfile::TempDir;

        let tmp_dir = TempDir::new().unwrap();
        let registry = ZoneRaftRegistry::new(tmp_dir.path().to_path_buf(), 1);

        let zone_raft_path = tmp_dir.path().join("root").join("raft");
        std::fs::create_dir_all(&zone_raft_path).unwrap();
        let storage = crate::raft::RaftStorage::open(&zone_raft_path).unwrap();
        storage
            .set_conf_state(&ConfState {
                voters: vec![1, 2],
                ..Default::default()
            })
            .unwrap();
        drop(storage);

        assert!(check_zone_membership(&registry, "root", 2).is_ok());
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

    #[tokio::test]
    async fn test_witness_auto_join_zone_cap_enforced_under_concurrency() {
        use tempfile::TempDir;

        let tmp_dir = TempDir::new().unwrap();
        let mut registry = WitnessZoneRegistry::new(tmp_dir.path().to_path_buf(), 1, None);
        registry.set_max_auto_join_zones(1);
        let registry = Arc::new(registry);
        let handle1 = tokio::runtime::Handle::current();
        let handle2 = tokio::runtime::Handle::current();

        let (r1, r2) = tokio::join!(
            async { registry.auto_join_zone("zone-a", &handle1).is_ok() },
            async { registry.auto_join_zone("zone-b", &handle2).is_ok() }
        );

        assert_eq!(u8::from(r1) + u8::from(r2), 1);
    }

    #[tokio::test]
    async fn test_witness_auto_join_cap_excludes_preconfigured_zones() {
        use tempfile::TempDir;

        let tmp_dir = TempDir::new().unwrap();
        let mut registry = WitnessZoneRegistry::new(tmp_dir.path().to_path_buf(), 1, None);
        registry.set_max_auto_join_zones(1);
        let handle = tokio::runtime::Handle::current();

        // Preconfigured zones should not consume dynamic auto-join budget.
        assert!(registry.create_zone("root", vec![], &handle).is_ok());
        assert!(registry.create_zone("federation", vec![], &handle).is_ok());

        assert!(registry.auto_join_zone("zone-a", &handle).is_ok());
        assert!(registry.auto_join_zone("zone-b", &handle).is_err());
    }

    #[tokio::test]
    async fn test_witness_auto_join_reopens_persisted_zone_without_marker() {
        use tempfile::TempDir;

        let tmp_dir = TempDir::new().unwrap();
        let handle = tokio::runtime::Handle::current();

        {
            let mut registry = WitnessZoneRegistry::new(tmp_dir.path().to_path_buf(), 1, None);
            registry.set_max_auto_join_zones(1);
            assert!(registry.auto_join_zone("zone-a", &handle).is_ok());
            assert!(registry.auto_join_zone("legacy-zone", &handle).is_ok());
        }

        let legacy_marker = tmp_dir
            .path()
            .join("legacy-zone")
            .join(WITNESS_AUTO_JOIN_MARKER_FILE);
        std::fs::remove_file(&legacy_marker).unwrap();

        let mut registry = WitnessZoneRegistry::new(tmp_dir.path().to_path_buf(), 1, None);
        registry.set_max_auto_join_zones(1);

        // Existing persisted zones must reopen even if marker migration is pending.
        assert!(registry.auto_join_zone("legacy-zone", &handle).is_ok());
        // Brand-new zones still respect the dynamic cap.
        assert!(registry.auto_join_zone("zone-b", &handle).is_err());
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
