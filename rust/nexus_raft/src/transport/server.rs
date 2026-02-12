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
    raft_client_service_server::{RaftClientService, RaftClientServiceServer},
    raft_command::Command as ProtoCommandVariant,
    raft_query::Query as ProtoQueryVariant,
    raft_query_response::Result as ProtoQueryResultVariant,
    raft_response::Result as ProtoResponseResultVariant,
    raft_service_server::{RaftService, RaftServiceServer},
    AppendEntriesRequest, AppendEntriesResponse, ClusterConfig as ProtoClusterConfig,
    GetClusterInfoRequest, GetClusterInfoResponse, GetMetadataResult, InstallSnapshotResponse,
    JoinZoneRequest, JoinZoneResponse, ListMetadataResult, LockInfoResult, LockResult,
    NodeInfo as ProtoNodeInfo, ProposeRequest, ProposeResponse, QueryRequest, QueryResponse,
    RaftCommand, RaftQueryResponse, RaftResponse, SnapshotChunk, StepMessageRequest,
    StepMessageResponse, TransferLeaderRequest, TransferLeaderResponse, VoteRequest, VoteResponse,
};
use super::{NodeAddress, Result, TransportError};
use crate::raft::{
    Command, CommandResult, FullStateMachine, RaftError, RaftNode, RaftNodeDriver,
    WitnessStateMachine, ZoneRaftRegistry,
};
use crate::storage::RedbStore;
use prost::Message;
use protobuf::Message as ProtobufV2Message;
use std::collections::HashMap;
use std::net::SocketAddr;
use std::sync::Arc;
use tonic::{Request, Response, Status, Streaming};

/// Configuration for Raft transport server.
#[derive(Debug, Clone)]
pub struct ServerConfig {
    /// Address to bind to (e.g., "0.0.0.0:2026").
    pub bind_address: SocketAddr,
    /// Maximum concurrent connections.
    pub max_connections: usize,
    /// Maximum message size in bytes.
    pub max_message_size: usize,
}

impl Default for ServerConfig {
    fn default() -> Self {
        Self {
            bind_address: "0.0.0.0:2026".parse().unwrap(),
            max_connections: 100,
            max_message_size: 64 * 1024 * 1024, // 64MB
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
}

impl RaftGrpcServer {
    /// Create a new server backed by the given zone registry.
    pub fn new(registry: Arc<ZoneRaftRegistry>, config: ServerConfig) -> Self {
        Self { config, registry }
    }

    /// Get the bind address.
    pub fn bind_address(&self) -> SocketAddr {
        self.config.bind_address
    }

    /// Start the gRPC server.
    pub async fn serve(self) -> Result<()> {
        let addr = self.config.bind_address;
        tracing::info!(
            "Starting Raft gRPC server on {} (zones={})",
            addr,
            self.registry.list_zones().len()
        );

        let raft_service = RaftServiceImpl {
            registry: self.registry.clone(),
        };
        let client_service = RaftClientServiceImpl {
            registry: self.registry.clone(),
        };

        tonic::transport::Server::builder()
            .add_service(RaftServiceServer::new(raft_service))
            .add_service(RaftClientServiceServer::new(client_service))
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
        tracing::info!(
            "Starting Raft gRPC server on {} (zones={}, with shutdown signal)",
            addr,
            self.registry.list_zones().len()
        );

        let raft_service = RaftServiceImpl {
            registry: self.registry.clone(),
        };
        let client_service = RaftClientServiceImpl {
            registry: self.registry.clone(),
        };

        tonic::transport::Server::builder()
            .add_service(RaftServiceServer::new(raft_service))
            .add_service(RaftClientServiceServer::new(client_service))
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
        ProtoCommandVariant::UpdateRouting(_) => {
            tracing::warn!("UpdateRouting command not supported");
            None
        }
    }
}

/// Look up a zone's RaftNode from the registry, or return a gRPC error.
fn get_zone_node(
    registry: &ZoneRaftRegistry,
    zone_id: &str,
) -> std::result::Result<RaftNode<FullStateMachine>, Status> {
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
// RaftService (internal node-to-node transport)
// =============================================================================

/// Zone-routed implementation of the RaftService gRPC trait.
///
/// Only `step_message` is used in production — all raft-rs message types
/// (~15 types including votes, heartbeats, appends) are multiplexed through
/// this single RPC as opaque protobuf v2 bytes (etcd/tikv pattern).
///
/// Legacy RPCs (request_vote, append_entries, etc.) return UNIMPLEMENTED.
struct RaftServiceImpl {
    registry: Arc<ZoneRaftRegistry>,
}

#[tonic::async_trait]
impl RaftService for RaftServiceImpl {
    async fn request_vote(
        &self,
        _request: Request<VoteRequest>,
    ) -> std::result::Result<Response<VoteResponse>, Status> {
        Err(Status::unimplemented(
            "Use step_message — legacy RPCs are not supported",
        ))
    }

    async fn append_entries(
        &self,
        _request: Request<AppendEntriesRequest>,
    ) -> std::result::Result<Response<AppendEntriesResponse>, Status> {
        Err(Status::unimplemented(
            "Use step_message — legacy RPCs are not supported",
        ))
    }

    async fn install_snapshot(
        &self,
        _request: Request<Streaming<SnapshotChunk>>,
    ) -> std::result::Result<Response<InstallSnapshotResponse>, Status> {
        Err(Status::unimplemented(
            "Use step_message — legacy RPCs are not supported",
        ))
    }

    async fn transfer_leader(
        &self,
        _request: Request<TransferLeaderRequest>,
    ) -> std::result::Result<Response<TransferLeaderResponse>, Status> {
        Err(Status::unimplemented(
            "Use step_message — legacy RPCs are not supported",
        ))
    }

    /// Handle a raw raft-rs message forwarded from another node.
    ///
    /// Routes by zone_id to the correct Raft group's RaftNode.
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
}

// =============================================================================
// RaftClientService (client-facing: Propose/Query/GetClusterInfo)
// =============================================================================

/// Zone-routed implementation of the RaftClientService gRPC trait.
struct RaftClientServiceImpl {
    registry: Arc<ZoneRaftRegistry>,
}

#[tonic::async_trait]
impl RaftClientService for RaftClientServiceImpl {
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
}

// =============================================================================
// Witness Server (lightweight, vote-only — separate StateMachine type)
// =============================================================================

/// Shared state for the Witness server, backed by `RaftNode<WitnessStateMachine>` handle.
pub struct WitnessServerState {
    /// The RaftNode handle (Clone + Send + Sync).
    pub node: RaftNode<WitnessStateMachine>,
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
    ) -> Result<(Self, RaftNodeDriver<WitnessStateMachine>)> {
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

        let (handle, driver) = RaftNode::new(config, raft_storage, state_machine).map_err(|e| {
            TransportError::Connection(format!("Failed to create witness RaftNode: {}", e))
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
    driver: Option<RaftNodeDriver<WitnessStateMachine>>,
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

    /// Get the RaftNode handle.
    pub fn node(&self) -> RaftNode<WitnessStateMachine> {
        self.state.node.clone()
    }

    /// Take the driver out (must be passed to the transport loop).
    pub fn take_driver(&mut self) -> RaftNodeDriver<WitnessStateMachine> {
        self.driver.take().expect("driver already taken")
    }

    /// Start the gRPC server with graceful shutdown.
    pub async fn serve_with_shutdown(
        self,
        shutdown: impl std::future::Future<Output = ()> + Send + 'static,
    ) -> Result<()> {
        let addr = self.config.bind_address;
        tracing::info!("Starting Raft Witness gRPC server on {}", addr);

        let service = WitnessServiceImpl {
            state: self.state.clone(),
        };

        tonic::transport::Server::builder()
            .add_service(RaftServiceServer::new(service))
            .serve_with_shutdown(addr, shutdown)
            .await
            .map_err(TransportError::Tonic)?;

        Ok(())
    }
}

/// Witness implementation of RaftService — only step_message is active.
struct WitnessServiceImpl {
    state: Arc<WitnessServerState>,
}

#[tonic::async_trait]
impl RaftService for WitnessServiceImpl {
    async fn request_vote(
        &self,
        _request: Request<VoteRequest>,
    ) -> std::result::Result<Response<VoteResponse>, Status> {
        Err(Status::unimplemented(
            "Use step_message — legacy RPCs are not supported",
        ))
    }

    async fn append_entries(
        &self,
        _request: Request<AppendEntriesRequest>,
    ) -> std::result::Result<Response<AppendEntriesResponse>, Status> {
        Err(Status::unimplemented(
            "Use step_message — legacy RPCs are not supported",
        ))
    }

    async fn install_snapshot(
        &self,
        _request: Request<Streaming<SnapshotChunk>>,
    ) -> std::result::Result<Response<InstallSnapshotResponse>, Status> {
        Err(Status::unimplemented(
            "Use step_message — legacy RPCs are not supported",
        ))
    }

    async fn transfer_leader(
        &self,
        _request: Request<TransferLeaderRequest>,
    ) -> std::result::Result<Response<TransferLeaderResponse>, Status> {
        Err(Status::unimplemented("Witness nodes cannot become leaders"))
    }

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
}
