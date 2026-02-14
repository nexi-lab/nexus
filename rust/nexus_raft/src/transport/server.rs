//! gRPC server for Raft transport.
//!
//! All Raft zones (including single-zone setups) are served through
//! `ZoneRaftRegistry`. There is no separate "single-zone" code path —
//! a single-zone deployment is simply a registry with one zone.

// TransportError contains tonic types that are large; will Box in future refactor.
#![expect(
    clippy::result_large_err,
    reason = "TransportError contains tonic types; will Box large variants in transport refactor"
)]

use super::proto::nexus::raft::{
    raft_command::Command as ProtoCommandVariant,
    raft_query::Query as ProtoQueryVariant,
    raft_query_response::Result as ProtoQueryResultVariant,
    raft_response::Result as ProtoResponseResultVariant,
    zone_api_service_client::ZoneApiServiceClient,
    zone_api_service_server::{ZoneApiService, ZoneApiServiceServer},
    zone_transport_service_server::{ZoneTransportService, ZoneTransportServiceServer},
    ClusterConfig as ProtoClusterConfig, GetClusterInfoRequest, GetClusterInfoResponse,
    GetMetadataResult, InviteZoneRequest, InviteZoneResponse, JoinZoneRequest, JoinZoneResponse,
    ListMetadataResult, LockInfoResult, LockResult, NodeInfo as ProtoNodeInfo, ProposeRequest,
    ProposeResponse, QueryRequest, QueryResponse, RaftCommand, RaftQueryResponse, RaftResponse,
    ReplicateEntriesRequest, ReplicateEntriesResponse, StepMessageRequest, StepMessageResponse,
};
use super::{NodeAddress, Result, TransportError};
use crate::raft::{
    Command, CommandResult, FullStateMachine, RaftError, WitnessStateMachine, ZoneConsensus,
    ZoneConsensusDriver, ZoneRaftRegistry,
};
use crate::storage::RedbStore;
use bincode;
use prost::Message;
use protobuf::Message as ProtobufV2Message;
use std::collections::HashMap;
use std::net::SocketAddr;
use std::sync::Arc;
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
    /// This node's own gRPC address (for InviteZone → JoinZone callback).
    self_address: String,
}

impl RaftGrpcServer {
    /// Create a new server backed by the given zone registry.
    ///
    /// # Arguments
    /// * `registry` — Zone registry for routing requests.
    /// * `config` — Server configuration.
    /// * `self_address` — This node's gRPC address (e.g., "http://10.0.0.2:2026").
    pub fn new(
        registry: Arc<ZoneRaftRegistry>,
        config: ServerConfig,
        self_address: String,
    ) -> Self {
        Self {
            config,
            registry,
            self_address,
        }
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
            self_address: self.self_address.clone(),
            tls: self.config.tls.clone(),
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
            self_address: self.self_address.clone(),
            tls: self.config.tls.clone(),
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
            ttl_secs: (al.ttl_ms / 1000) as u32,
            holder_info: al.holder_id,
        }),
        ProtoCommandVariant::ReleaseLock(rl) => Some(Command::ReleaseLock {
            path: rl.lock_id.clone(),
            lock_id: rl.holder_id,
        }),
        ProtoCommandVariant::ExtendLock(el) => Some(Command::ExtendLock {
            path: el.lock_id.clone(),
            lock_id: el.holder_id,
            new_ttl_secs: (el.ttl_ms / 1000) as u32,
        }),
    }
}

/// Look up a zone's ZoneConsensus from the registry, or return a gRPC error.
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
        CommandResult::Error(e) => RaftResponse {
            success: false,
            error: Some(e.clone()),
            result: None,
        },
    }
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
    async fn step_message(
        &self,
        request: Request<StepMessageRequest>,
    ) -> std::result::Result<Response<StepMessageResponse>, Status> {
        let req = request.into_inner();
        let node = get_zone_node(&self.registry, &req.zone_id)?;

        let msg = match raft::eraftpb::Message::parse_from_bytes(&req.message) {
            Ok(m) => m,
            Err(e) => {
                return Ok(Response::new(StepMessageResponse {
                    success: false,
                    error: Some(format!("Failed to deserialize raft message: {}", e)),
                }));
            }
        };

        tracing::trace!(
            "StepMessage [zone={}]: type={:?}, from={}, to={}, term={}",
            req.zone_id,
            msg.get_msg_type(),
            msg.from,
            msg.to,
            msg.term,
        );

        if let Err(e) = node.step(msg) {
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
    /// This node's own gRPC address (e.g., "http://10.0.0.2:2026").
    /// Needed by InviteZone to tell the leader our address when calling JoinZone.
    self_address: String,
    /// TLS config for outbound connections (InviteZone → JoinZone callback).
    tls: Option<super::TlsConfig>,
}

/// Build a tonic Endpoint, optionally with TLS client config.
fn build_endpoint_with_tls(
    address: &str,
    tls: Option<&super::TlsConfig>,
) -> Result<tonic::transport::Endpoint> {
    let mut endpoint = tonic::transport::Endpoint::from_shared(address.to_string())
        .map_err(|e| TransportError::InvalidAddress(e.to_string()))?
        .connect_timeout(std::time::Duration::from_secs(10))
        .timeout(std::time::Duration::from_secs(30));

    if let Some(tls) = tls {
        let identity = tonic::transport::Identity::from_pem(&tls.cert_pem, &tls.key_pem);
        let ca = tonic::transport::Certificate::from_pem(&tls.ca_pem);
        let tls_config = tonic::transport::ClientTlsConfig::new()
            .identity(identity)
            .ca_certificate(ca);
        endpoint = endpoint
            .tls_config(tls_config)
            .map_err(|e| TransportError::Connection(format!("TLS config error: {}", e)))?;
    }

    Ok(endpoint)
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
            "Received propose request: zone={}, id={:?}",
            req.zone_id,
            req.request_id
        );

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

        let cmd = match proto_command_to_internal(proto_cmd) {
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
        };

        match node.propose(cmd).await {
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

    /// Handle an InviteZone request — inverse of JoinZone.
    ///
    /// This node is being asked to join a zone and create a DT_MOUNT locally.
    /// Steps: (1) create local zone replica, (2) call JoinZone on leader,
    /// (3) create DT_MOUNT in root zone.
    async fn invite_zone(
        &self,
        request: Request<InviteZoneRequest>,
    ) -> std::result::Result<Response<InviteZoneResponse>, Status> {
        let req = request.into_inner();
        let node_id = self.registry.node_id();

        tracing::info!(
            zone = req.zone_id,
            mount_path = req.mount_path,
            inviter_node_id = req.inviter_node_id,
            inviter_address = req.inviter_address,
            "InviteZone request received",
        );

        // Step 1: Create local zone replica (reuses registry.join_zone — DRY).
        let inviter_peer = NodeAddress {
            id: req.inviter_node_id,
            endpoint: req.inviter_address.clone(),
        };
        let runtime_handle = tokio::runtime::Handle::current();
        if let Err(e) = self
            .registry
            .join_zone(&req.zone_id, vec![inviter_peer], &runtime_handle)
        {
            return Ok(Response::new(InviteZoneResponse {
                success: false,
                error: Some(format!("Failed to create local zone replica: {}", e)),
                node_id: 0,
                node_address: String::new(),
            }));
        }

        // Step 2: Call JoinZone on the inviter (leader) to be added as Voter.
        // Uses tonic-generated client — same RPC definition, no duplication.
        let channel = match build_endpoint_with_tls(&req.inviter_address, self.tls.as_ref()) {
            Ok(ep) => match ep.connect().await {
                Ok(ch) => ch,
                Err(e) => {
                    let _ = self.registry.remove_zone(&req.zone_id);
                    return Ok(Response::new(InviteZoneResponse {
                        success: false,
                        error: Some(format!("Failed to connect to inviter: {}", e)),
                        node_id: 0,
                        node_address: String::new(),
                    }));
                }
            },
            Err(e) => {
                let _ = self.registry.remove_zone(&req.zone_id);
                return Ok(Response::new(InviteZoneResponse {
                    success: false,
                    error: Some(format!("Invalid inviter address: {}", e)),
                    node_id: 0,
                    node_address: String::new(),
                }));
            }
        };

        let mut client = ZoneApiServiceClient::new(channel);
        let join_request = tonic::Request::new(JoinZoneRequest {
            zone_id: req.zone_id.clone(),
            node_id,
            node_address: self.self_address.clone(),
        });

        match client.join_zone(join_request).await {
            Ok(resp) => {
                let join_resp = resp.into_inner();
                if !join_resp.success {
                    let _ = self.registry.remove_zone(&req.zone_id);
                    return Ok(Response::new(InviteZoneResponse {
                        success: false,
                        error: Some(format!(
                            "JoinZone rejected by leader: {}",
                            join_resp.error.unwrap_or_default()
                        )),
                        node_id: 0,
                        node_address: String::new(),
                    }));
                }
            }
            Err(e) => {
                let _ = self.registry.remove_zone(&req.zone_id);
                return Ok(Response::new(InviteZoneResponse {
                    success: false,
                    error: Some(format!("JoinZone RPC failed: {}", e)),
                    node_id: 0,
                    node_address: String::new(),
                }));
            }
        }

        // Step 3: Create DT_MOUNT in local root zone.
        // Reuses existing propose infrastructure (same as any SetMetadata).
        let root_zone_id = "root";
        let root_node = match self.registry.get_node(root_zone_id) {
            Some(node) => node,
            None => {
                // Zone joined but can't create mount — partial success.
                return Ok(Response::new(InviteZoneResponse {
                    success: false,
                    error: Some("Root zone not found on this node".to_string()),
                    node_id,
                    node_address: self.self_address.clone(),
                }));
            }
        };

        // Build DT_MOUNT FileMetadata proto
        let mount_metadata = super::proto::nexus::core::FileMetadata {
            path: req.mount_path.clone(),
            backend_name: "virtual".to_string(),
            entry_type: 2, // DT_MOUNT
            target_zone_id: req.zone_id.clone(),
            zone_id: root_zone_id.to_string(),
            ..Default::default()
        };
        let key = mount_metadata.path.clone();
        let value = prost::Message::encode_to_vec(&mount_metadata);

        match root_node.propose(Command::SetMetadata { key, value }).await {
            Ok(_) => {
                tracing::info!(
                    zone = req.zone_id,
                    mount_path = req.mount_path,
                    "InviteZone complete: zone joined + DT_MOUNT created",
                );
                Ok(Response::new(InviteZoneResponse {
                    success: true,
                    error: None,
                    node_id,
                    node_address: self.self_address.clone(),
                }))
            }
            Err(e) => {
                // Zone joined but DT_MOUNT failed — partial success.
                Ok(Response::new(InviteZoneResponse {
                    success: false,
                    error: Some(format!("Zone joined but DT_MOUNT creation failed: {}", e)),
                    node_id,
                    node_address: self.self_address.clone(),
                }))
            }
        }
    }
}

// =============================================================================
// Witness Server (lightweight, vote-only — separate StateMachine type)
// =============================================================================

/// Shared state for the Witness server, backed by `ZoneConsensus<WitnessStateMachine>` handle.
pub struct WitnessServerState {
    /// The ZoneConsensus handle (Clone + Send + Sync).
    pub node: ZoneConsensus<WitnessStateMachine>,
    /// This node's ID.
    pub node_id: u64,
    /// Known peers.
    pub peers: HashMap<u64, NodeAddress>,
}

impl WitnessServerState {
    /// Create a new witness server state.
    ///
    /// Returns `(state, driver)` — the driver must be passed to the transport loop.
    pub fn new(
        node_id: u64,
        db_path: &str,
        peers: Vec<NodeAddress>,
    ) -> Result<(Self, ZoneConsensusDriver<WitnessStateMachine>)> {
        use crate::raft::{RaftConfig, RaftStorage};
        let sm_path = std::path::Path::new(db_path).join("sm");
        let raft_path = std::path::Path::new(db_path).join("raft");

        let store = RedbStore::open(&sm_path)
            .map_err(|e| TransportError::Connection(format!("Failed to open store: {}", e)))?;

        let raft_storage = RaftStorage::open(&raft_path).map_err(|e| {
            TransportError::Connection(format!("Failed to open raft storage: {}", e))
        })?;

        let state_machine = WitnessStateMachine::new(&store).map_err(|e| {
            TransportError::Connection(format!("Failed to create witness state machine: {}", e))
        })?;

        let peer_ids: Vec<u64> = peers.iter().map(|p| p.id).collect();
        let config = RaftConfig::witness(node_id, peer_ids);

        let (handle, driver) = ZoneConsensus::new(config, raft_storage, state_machine, None)
            .map_err(|e| {
                TransportError::Connection(format!("Failed to create witness ZoneConsensus: {}", e))
            })?;

        let peer_map: HashMap<u64, NodeAddress> = peers.into_iter().map(|p| (p.id, p)).collect();

        Ok((
            Self {
                node: handle,
                node_id,
                peers: peer_map,
            },
            driver,
        ))
    }
}

/// A gRPC server for Raft witness nodes.
pub struct RaftWitnessServer {
    config: ServerConfig,
    state: Arc<WitnessServerState>,
    /// The driver, held temporarily until passed to the transport loop.
    driver: Option<ZoneConsensusDriver<WitnessStateMachine>>,
}

impl RaftWitnessServer {
    /// Create a new witness server.
    pub fn new(node_id: u64, db_path: &str, peers: Vec<NodeAddress>) -> Result<Self> {
        Self::with_config(node_id, db_path, ServerConfig::default(), peers)
    }

    /// Create a new witness server with custom configuration.
    pub fn with_config(
        node_id: u64,
        db_path: &str,
        config: ServerConfig,
        peers: Vec<NodeAddress>,
    ) -> Result<Self> {
        let (state, driver) = WitnessServerState::new(node_id, db_path, peers)?;
        Ok(Self {
            config,
            state: Arc::new(state),
            driver: Some(driver),
        })
    }

    /// Get the bind address.
    pub fn bind_address(&self) -> SocketAddr {
        self.config.bind_address
    }

    /// Get the ZoneConsensus handle.
    pub fn node(&self) -> ZoneConsensus<WitnessStateMachine> {
        self.state.node.clone()
    }

    /// Take the driver out (must be passed to the transport loop).
    pub fn take_driver(&mut self) -> ZoneConsensusDriver<WitnessStateMachine> {
        self.driver.take().expect("driver already taken")
    }

    /// Start the gRPC server with graceful shutdown.
    pub async fn serve_with_shutdown(
        self,
        shutdown: impl std::future::Future<Output = ()> + Send + 'static,
    ) -> Result<()> {
        let addr = self.config.bind_address;
        let tls_enabled = self.config.tls.is_some();
        tracing::info!(
            "Starting Raft Witness gRPC server on {} (tls={})",
            addr,
            tls_enabled,
        );

        let service = WitnessServiceImpl {
            state: self.state.clone(),
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

/// Witness implementation of ZoneTransportService — only step_message is active.
struct WitnessServiceImpl {
    state: Arc<WitnessServerState>,
}

#[tonic::async_trait]
impl ZoneTransportService for WitnessServiceImpl {
    /// Handle a raw raft-rs message forwarded from another node.
    async fn step_message(
        &self,
        request: Request<StepMessageRequest>,
    ) -> std::result::Result<Response<StepMessageResponse>, Status> {
        let req = request.into_inner();

        let msg = match raft::eraftpb::Message::parse_from_bytes(&req.message) {
            Ok(m) => m,
            Err(e) => {
                return Ok(Response::new(StepMessageResponse {
                    success: false,
                    error: Some(format!("Failed to deserialize raft message: {}", e)),
                }));
            }
        };

        tracing::trace!(
            "[Witness] StepMessage: type={:?}, from={}, term={}",
            msg.get_msg_type(),
            msg.from,
            msg.term,
        );

        if let Err(e) = self.state.node.step(msg) {
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

        let server = RaftGrpcServer::new(registry, config, "http://127.0.0.1:0".to_string());
        assert_eq!(
            server.bind_address(),
            "127.0.0.1:0".parse::<SocketAddr>().unwrap()
        );
    }
}
