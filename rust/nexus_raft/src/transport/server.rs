//! gRPC server for Raft transport.
//!
//! Provides a tonic-based server to handle incoming Raft messages from other nodes.
//! The server is backed by `RaftNode` (tikv/raft-rs) for actual consensus.

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
    ListMetadataResult, LockInfoResult, LockResult, NodeInfo as ProtoNodeInfo, ProposeRequest,
    ProposeResponse, QueryRequest, QueryResponse, RaftCommand, RaftQueryResponse, RaftResponse,
    SnapshotChunk, StepMessageRequest, StepMessageResponse, TransferLeaderRequest,
    TransferLeaderResponse, VoteRequest, VoteResponse,
};
use super::{NodeAddress, Result, TransportError};
use crate::raft::{
    Command, CommandResult, FullStateMachine, RaftConfig, RaftError, RaftNode, RaftStorage,
    StateMachine, WitnessStateMachine,
};
use crate::storage::SledStore;
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

/// Shared state for the Raft server, backed by `RaftNode`.
pub struct RaftServerState {
    /// The RaftNode that drives consensus.
    pub node: Arc<RaftNode<FullStateMachine>>,
    /// This node's ID.
    pub node_id: u64,
    /// Known peers in the cluster (node_id → address).
    pub peers: HashMap<u64, NodeAddress>,
}

impl RaftServerState {
    /// Create a new server state with the given storage path and peers.
    pub fn new(node_id: u64, db_path: &str, peers: Vec<NodeAddress>) -> Result<Self> {
        // Use separate sub-paths to avoid sled lock conflicts:
        // - db_path/sm  → state machine (metadata + locks)
        // - db_path/raft → raft log storage
        let sm_path = std::path::Path::new(db_path).join("sm");
        let raft_path = std::path::Path::new(db_path).join("raft");

        let store = SledStore::open(&sm_path)
            .map_err(|e| TransportError::Connection(format!("Failed to open store: {}", e)))?;

        let raft_storage = RaftStorage::open(&raft_path).map_err(|e| {
            TransportError::Connection(format!("Failed to open raft storage: {}", e))
        })?;

        let state_machine = FullStateMachine::new(&store).map_err(|e| {
            TransportError::Connection(format!("Failed to create state machine: {}", e))
        })?;

        let peer_ids: Vec<u64> = peers.iter().map(|p| p.id).collect();
        let config = RaftConfig {
            id: node_id,
            peers: peer_ids,
            ..Default::default()
        };

        let node = RaftNode::new(config, raft_storage, state_machine)
            .map_err(|e| TransportError::Connection(format!("Failed to create RaftNode: {}", e)))?;

        let peer_map: HashMap<u64, NodeAddress> = peers.into_iter().map(|p| (p.id, p)).collect();

        Ok(Self {
            node,
            node_id,
            peers: peer_map,
        })
    }
}

/// A gRPC server for Raft transport, backed by `RaftNode`.
pub struct RaftServer {
    config: ServerConfig,
    state: Arc<RaftServerState>,
}

impl RaftServer {
    /// Create a new Raft server with the given node ID, database path, and peers.
    pub fn new(node_id: u64, db_path: &str, peers: Vec<NodeAddress>) -> Result<Self> {
        Self::with_config(node_id, db_path, ServerConfig::default(), peers)
    }

    /// Create a new Raft server with custom configuration.
    pub fn with_config(
        node_id: u64,
        db_path: &str,
        config: ServerConfig,
        peers: Vec<NodeAddress>,
    ) -> Result<Self> {
        let state = RaftServerState::new(node_id, db_path, peers)?;
        Ok(Self {
            config,
            state: Arc::new(state),
        })
    }

    /// Get the bind address.
    pub fn bind_address(&self) -> SocketAddr {
        self.config.bind_address
    }

    /// Get the RaftNode (for transport loop integration).
    pub fn node(&self) -> Arc<RaftNode<FullStateMachine>> {
        self.state.node.clone()
    }

    /// Get the server state.
    pub fn state(&self) -> Arc<RaftServerState> {
        self.state.clone()
    }

    /// Start the gRPC server.
    pub async fn serve(self) -> Result<()> {
        let addr = self.config.bind_address;
        tracing::info!("Starting Raft gRPC server on {}", addr);

        let raft_service = RaftServiceImpl {
            state: self.state.clone(),
        };
        let client_service = RaftClientServiceImpl {
            state: self.state.clone(),
        };

        tonic::transport::Server::builder()
            .add_service(RaftServiceServer::new(raft_service))
            .add_service(RaftClientServiceServer::new(client_service))
            .serve(addr)
            .await
            .map_err(TransportError::Tonic)?;

        Ok(())
    }

    /// Start the server and return a handle for graceful shutdown.
    pub async fn serve_with_shutdown(
        self,
        shutdown: impl std::future::Future<Output = ()> + Send + 'static,
    ) -> Result<()> {
        let addr = self.config.bind_address;
        tracing::info!(
            "Starting Raft gRPC server on {} (with shutdown signal)",
            addr
        );

        let raft_service = RaftServiceImpl {
            state: self.state.clone(),
        };
        let client_service = RaftClientServiceImpl {
            state: self.state.clone(),
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

/// Convert protobuf RaftCommand to internal Command enum.
fn proto_command_to_internal(proto: RaftCommand) -> Option<Command> {
    match proto.command? {
        ProtoCommandVariant::PutMetadata(pm) => {
            let metadata = pm.metadata?;
            // Store protobuf bytes directly (SSOT: metadata.proto is the source of truth)
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

/// Implementation of the RaftService gRPC trait (internal node-to-node).
struct RaftServiceImpl {
    state: Arc<RaftServerState>,
}

/// Implementation of the RaftClientService gRPC trait (client-facing).
struct RaftClientServiceImpl {
    state: Arc<RaftServerState>,
}

#[tonic::async_trait]
impl RaftService for RaftServiceImpl {
    /// Handle a vote request from a candidate.
    ///
    /// Legacy RPC — in the new architecture, all raft-rs messages are routed
    /// through `StepMessage`. This RPC converts the request to an eraftpb::Message
    /// and delegates to `node.step()`.
    async fn request_vote(
        &self,
        request: Request<VoteRequest>,
    ) -> std::result::Result<Response<VoteResponse>, Status> {
        let req = request.into_inner();
        tracing::debug!(
            "Received vote request: term={}, candidate_id={}",
            req.term,
            req.candidate_id,
        );

        // Convert to eraftpb::Message and step
        let mut msg = raft::eraftpb::Message::default();
        msg.set_msg_type(raft::eraftpb::MessageType::MsgRequestVote);
        msg.term = req.term;
        msg.from = req.candidate_id;
        msg.to = self.state.node_id;
        msg.index = req.last_log_index;
        msg.log_term = req.last_log_term;

        if let Err(e) = self.state.node.step(msg).await {
            tracing::warn!("Failed to step vote request: {}", e);
        }

        // After step, advance to process the message and get response
        if let Err(e) = self.state.node.advance().await {
            tracing::warn!("Failed to advance after vote: {}", e);
        }

        // Return current node state — the actual vote response was sent
        // via the transport loop's outgoing messages
        let term = self.state.node.term().await;

        Ok(Response::new(VoteResponse {
            term,
            vote_granted: false, // Actual grant is in outgoing messages
        }))
    }

    /// Handle append entries from a leader.
    ///
    /// Legacy RPC — see `step_message` for the primary transport path.
    async fn append_entries(
        &self,
        request: Request<AppendEntriesRequest>,
    ) -> std::result::Result<Response<AppendEntriesResponse>, Status> {
        let req = request.into_inner();
        tracing::debug!(
            "Received append entries: term={}, leader_id={}, entries={}",
            req.term,
            req.leader_id,
            req.entries.len(),
        );

        // Convert to eraftpb::Message and step
        let mut msg = raft::eraftpb::Message::default();
        msg.set_msg_type(raft::eraftpb::MessageType::MsgAppend);
        msg.term = req.term;
        msg.from = req.leader_id;
        msg.to = self.state.node_id;
        msg.index = req.prev_log_index;
        msg.log_term = req.prev_log_term;
        msg.commit = req.leader_commit;

        // Convert entries
        for entry in req.entries {
            msg.entries.push(raft::eraftpb::Entry {
                term: entry.term,
                index: entry.index,
                data: entry.data.into(),
                ..Default::default()
            });
        }

        if let Err(e) = self.state.node.step(msg).await {
            tracing::warn!("Failed to step append entries: {}", e);
        }

        if let Err(e) = self.state.node.advance().await {
            tracing::warn!("Failed to advance after append: {}", e);
        }

        let term = self.state.node.term().await;

        Ok(Response::new(AppendEntriesResponse {
            term,
            success: true,
            match_index: 0, // Actual match_index is in outgoing messages
        }))
    }

    /// Handle snapshot installation (streaming).
    async fn install_snapshot(
        &self,
        request: Request<Streaming<SnapshotChunk>>,
    ) -> std::result::Result<Response<InstallSnapshotResponse>, Status> {
        let mut stream = request.into_inner();
        let mut snapshot_data = Vec::new();

        while let Some(chunk) = stream.message().await? {
            snapshot_data.extend(chunk.data);
            if chunk.done {
                break;
            }
        }

        // Apply snapshot to state machine (needs mutable access)
        let result = self
            .state
            .node
            .with_state_machine_mut(|sm| sm.restore_snapshot(&snapshot_data))
            .await;

        let term = self.state.node.term().await;

        match result {
            Ok(_) => {
                tracing::info!("Snapshot installed successfully");
                Ok(Response::new(InstallSnapshotResponse {
                    term,
                    success: true,
                }))
            }
            Err(e) => {
                tracing::error!("Failed to install snapshot: {}", e);
                Ok(Response::new(InstallSnapshotResponse {
                    term,
                    success: false,
                }))
            }
        }
    }

    /// Handle leadership transfer request.
    async fn transfer_leader(
        &self,
        request: Request<TransferLeaderRequest>,
    ) -> std::result::Result<Response<TransferLeaderResponse>, Status> {
        let req = request.into_inner();

        if !self.state.node.is_leader().await {
            return Ok(Response::new(TransferLeaderResponse {
                success: false,
                error: "Not the leader".to_string(),
            }));
        }

        tracing::info!("Leadership transfer requested to node {}", req.target_id);

        Ok(Response::new(TransferLeaderResponse {
            success: true,
            error: String::new(),
        }))
    }

    /// Handle a raw raft-rs message forwarded from another node.
    ///
    /// This is the PRIMARY transport method. All raft-rs internal messages
    /// (~15 types including votes, heartbeats, appends) are routed through
    /// this single RPC as opaque protobuf v2 bytes. This is the standard
    /// pattern used by etcd and tikv.
    async fn step_message(
        &self,
        request: Request<StepMessageRequest>,
    ) -> std::result::Result<Response<StepMessageResponse>, Status> {
        let req = request.into_inner();

        // Deserialize the eraftpb::Message from protobuf v2 bytes
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
            "StepMessage: type={:?}, from={}, to={}, term={}",
            msg.get_msg_type(),
            msg.from,
            msg.to,
            msg.term,
        );

        // Step the message into the raft node
        if let Err(e) = self.state.node.step(msg).await {
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
// Client-Facing Service (Propose/Query/GetClusterInfo)
// =============================================================================

#[tonic::async_trait]
impl RaftClientService for RaftClientServiceImpl {
    /// Handle a client proposal (write operation).
    ///
    /// Delegates to `RaftNode::propose()` which goes through Raft consensus.
    /// Only the leader can accept proposals; followers return a redirect.
    async fn propose(
        &self,
        request: Request<ProposeRequest>,
    ) -> std::result::Result<Response<ProposeResponse>, Status> {
        let req = request.into_inner();
        tracing::debug!("Received propose request: {:?}", req.request_id);

        // Extract and convert the command
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

        // Propose through RaftNode (goes through Raft consensus)
        match self.state.node.propose(cmd).await {
            Ok(result) => {
                let proto_result = command_result_to_proto(&result);
                Ok(Response::new(ProposeResponse {
                    success: true,
                    error: None,
                    leader_address: None,
                    result: Some(proto_result),
                    applied_index: 0, // TODO: Return actual applied index
                }))
            }
            Err(RaftError::NotLeader { leader_hint }) => {
                let addr = leader_hint
                    .and_then(|id| self.state.peers.get(&id))
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
    ///
    /// Reads from the local state machine via `RaftNode::with_state_machine()`.
    async fn query(
        &self,
        request: Request<QueryRequest>,
    ) -> std::result::Result<Response<QueryResponse>, Status> {
        let req = request.into_inner();
        tracing::debug!(
            "Received query request, read_from_leader={}",
            req.read_from_leader
        );

        // Check if linearizable read is requested
        if req.read_from_leader && !self.state.node.is_leader().await {
            let leader_addr = self
                .state
                .node
                .leader_id()
                .await
                .and_then(|id| self.state.peers.get(&id))
                .map(|a| a.endpoint.clone());
            return Ok(Response::new(QueryResponse {
                success: false,
                error: Some("Not the leader".to_string()),
                leader_address: leader_addr,
                result: None,
            }));
        }

        // Extract query
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

        // Process query by reading from the state machine
        let query_result = match proto_query.query {
            Some(ProtoQueryVariant::GetMetadata(gm)) => {
                self.state
                    .node
                    .with_state_machine(|sm| match sm.get_metadata(&gm.path) {
                        Ok(Some(data)) => {
                            let metadata =
                                super::proto::nexus::core::FileMetadata::decode(data.as_slice())
                                    .ok();
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
                self.state
                    .node
                    .with_state_machine(|sm| match sm.list_metadata(&lm.prefix) {
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
                self.state
                    .node
                    .with_state_machine(|sm| match sm.get_lock(&gli.lock_id) {
                        Ok(Some(lock_info)) => {
                            let first_holder = lock_info.holders.first();
                            RaftQueryResponse {
                                success: true,
                                error: None,
                                result: Some(ProtoQueryResultVariant::LockInfoResult(
                                    LockInfoResult {
                                        exists: true,
                                        holder_id: first_holder.map(|h| h.holder_info.clone()),
                                        expires_at_ms: first_holder
                                            .map(|h| (h.expires_at * 1000) as i64)
                                            .unwrap_or(0),
                                        max_holders: lock_info.max_holders as i32,
                                        current_holders: lock_info.holders.len() as i32,
                                    },
                                )),
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

    /// Get cluster information.
    async fn get_cluster_info(
        &self,
        _request: Request<GetClusterInfoRequest>,
    ) -> std::result::Result<Response<GetClusterInfoResponse>, Status> {
        let is_leader = self.state.node.is_leader().await;
        let leader_id = self.state.node.leader_id().await.unwrap_or(0);
        let term = self.state.node.term().await;

        let leader_addr = self.state.peers.get(&leader_id).map(|a| a.endpoint.clone());

        // Build cluster config from known peers
        let mut voters = vec![ProtoNodeInfo {
            id: self.state.node_id,
            address: self
                .state
                .peers
                .get(&self.state.node_id)
                .map(|a| a.endpoint.clone())
                .unwrap_or_default(),
            role: 0, // ROLE_VOTER
        }];
        for (id, addr) in &self.state.peers {
            voters.push(ProtoNodeInfo {
                id: *id,
                address: addr.endpoint.clone(),
                role: 0, // ROLE_VOTER
            });
        }

        Ok(Response::new(GetClusterInfoResponse {
            node_id: self.state.node_id,
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
// Witness Server (lightweight, vote-only)
// =============================================================================

/// Shared state for the Witness server, backed by `RaftNode<WitnessStateMachine>`.
pub struct WitnessServerState {
    /// The RaftNode that handles consensus (voting only).
    pub node: Arc<RaftNode<WitnessStateMachine>>,
    /// This node's ID.
    pub node_id: u64,
    /// Known peers.
    pub peers: HashMap<u64, NodeAddress>,
}

impl WitnessServerState {
    /// Create a new witness server state.
    pub fn new(node_id: u64, db_path: &str, peers: Vec<NodeAddress>) -> Result<Self> {
        let sm_path = std::path::Path::new(db_path).join("sm");
        let raft_path = std::path::Path::new(db_path).join("raft");

        let store = SledStore::open(&sm_path)
            .map_err(|e| TransportError::Connection(format!("Failed to open store: {}", e)))?;

        let raft_storage = RaftStorage::open(&raft_path).map_err(|e| {
            TransportError::Connection(format!("Failed to open raft storage: {}", e))
        })?;

        let state_machine = WitnessStateMachine::new(&store).map_err(|e| {
            TransportError::Connection(format!("Failed to create witness state machine: {}", e))
        })?;

        let peer_ids: Vec<u64> = peers.iter().map(|p| p.id).collect();
        let config = RaftConfig {
            id: node_id,
            peers: peer_ids,
            ..Default::default()
        };

        let node = RaftNode::new(config, raft_storage, state_machine).map_err(|e| {
            TransportError::Connection(format!("Failed to create witness RaftNode: {}", e))
        })?;

        let peer_map: HashMap<u64, NodeAddress> = peers.into_iter().map(|p| (p.id, p)).collect();

        Ok(Self {
            node,
            node_id,
            peers: peer_map,
        })
    }
}

/// A gRPC server for Raft witness nodes.
pub struct RaftWitnessServer {
    config: ServerConfig,
    state: Arc<WitnessServerState>,
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
        let state = WitnessServerState::new(node_id, db_path, peers)?;
        Ok(Self {
            config,
            state: Arc::new(state),
        })
    }

    /// Get the bind address.
    pub fn bind_address(&self) -> SocketAddr {
        self.config.bind_address
    }

    /// Get the RaftNode (for transport loop integration).
    pub fn node(&self) -> Arc<RaftNode<WitnessStateMachine>> {
        self.state.node.clone()
    }

    /// Start the gRPC server.
    pub async fn serve(self) -> Result<()> {
        let addr = self.config.bind_address;
        tracing::info!("Starting Raft Witness gRPC server on {}", addr);

        let service = WitnessServiceImpl {
            state: self.state.clone(),
        };

        tonic::transport::Server::builder()
            .add_service(RaftServiceServer::new(service))
            .serve(addr)
            .await
            .map_err(TransportError::Tonic)?;

        Ok(())
    }

    /// Start the server with graceful shutdown.
    pub async fn serve_with_shutdown(
        self,
        shutdown: impl std::future::Future<Output = ()> + Send + 'static,
    ) -> Result<()> {
        let addr = self.config.bind_address;
        tracing::info!(
            "Starting Raft Witness gRPC server on {} (with shutdown signal)",
            addr
        );

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

/// Implementation of the RaftService gRPC trait for witness nodes.
struct WitnessServiceImpl {
    state: Arc<WitnessServerState>,
}

#[tonic::async_trait]
impl RaftService for WitnessServiceImpl {
    /// Handle a vote request — witness nodes CAN vote.
    async fn request_vote(
        &self,
        request: Request<VoteRequest>,
    ) -> std::result::Result<Response<VoteResponse>, Status> {
        let req = request.into_inner();
        tracing::debug!(
            "[Witness] Received vote request: term={}, candidate_id={}",
            req.term,
            req.candidate_id
        );

        let mut msg = raft::eraftpb::Message::default();
        msg.set_msg_type(raft::eraftpb::MessageType::MsgRequestVote);
        msg.term = req.term;
        msg.from = req.candidate_id;
        msg.to = self.state.node_id;
        msg.index = req.last_log_index;
        msg.log_term = req.last_log_term;

        if let Err(e) = self.state.node.step(msg).await {
            tracing::warn!("[Witness] Failed to step vote request: {}", e);
        }

        if let Err(e) = self.state.node.advance().await {
            tracing::warn!("[Witness] Failed to advance after vote: {}", e);
        }

        let term = self.state.node.term().await;

        Ok(Response::new(VoteResponse {
            term,
            vote_granted: false,
        }))
    }

    /// Handle append entries — witness accepts log but doesn't apply.
    async fn append_entries(
        &self,
        request: Request<AppendEntriesRequest>,
    ) -> std::result::Result<Response<AppendEntriesResponse>, Status> {
        let req = request.into_inner();
        tracing::debug!(
            "[Witness] Received append entries: term={}, entries={}",
            req.term,
            req.entries.len()
        );

        let mut msg = raft::eraftpb::Message::default();
        msg.set_msg_type(raft::eraftpb::MessageType::MsgAppend);
        msg.term = req.term;
        msg.from = req.leader_id;
        msg.to = self.state.node_id;
        msg.index = req.prev_log_index;
        msg.log_term = req.prev_log_term;
        msg.commit = req.leader_commit;

        for entry in req.entries {
            msg.entries.push(raft::eraftpb::Entry {
                term: entry.term,
                index: entry.index,
                data: entry.data.into(),
                ..Default::default()
            });
        }

        if let Err(e) = self.state.node.step(msg).await {
            tracing::warn!("[Witness] Failed to step append entries: {}", e);
        }

        if let Err(e) = self.state.node.advance().await {
            tracing::warn!("[Witness] Failed to advance after append: {}", e);
        }

        let term = self.state.node.term().await;

        Ok(Response::new(AppendEntriesResponse {
            term,
            success: true,
            match_index: 0,
        }))
    }

    /// Handle snapshot installation — witness ignores snapshots.
    async fn install_snapshot(
        &self,
        _request: Request<Streaming<SnapshotChunk>>,
    ) -> std::result::Result<Response<InstallSnapshotResponse>, Status> {
        let term = self.state.node.term().await;
        tracing::info!("[Witness] Ignoring snapshot installation request");

        Ok(Response::new(InstallSnapshotResponse {
            term,
            success: true,
        }))
    }

    /// Handle leadership transfer — witness cannot become leader.
    async fn transfer_leader(
        &self,
        _request: Request<TransferLeaderRequest>,
    ) -> std::result::Result<Response<TransferLeaderResponse>, Status> {
        Ok(Response::new(TransferLeaderResponse {
            success: false,
            error: "Witness nodes cannot become leaders".to_string(),
        }))
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

        if let Err(e) = self.state.node.step(msg).await {
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
    use tempfile::TempDir;

    #[tokio::test]
    async fn test_server_creation() {
        let tmp_dir = TempDir::new().unwrap();
        let db_path = tmp_dir.path().join("test_db");

        let server = RaftServer::new(1, db_path.to_str().unwrap(), vec![]).unwrap();
        assert_eq!(
            server.bind_address(),
            "0.0.0.0:2026".parse::<SocketAddr>().unwrap()
        );
    }

    #[tokio::test]
    async fn test_custom_config() {
        let tmp_dir = TempDir::new().unwrap();
        let db_path = tmp_dir.path().join("test_db");

        let config = ServerConfig {
            bind_address: "127.0.0.1:3000".parse().unwrap(),
            max_connections: 50,
            max_message_size: 32 * 1024 * 1024,
        };

        let server = RaftServer::with_config(1, db_path.to_str().unwrap(), config, vec![]).unwrap();
        assert_eq!(
            server.bind_address(),
            "127.0.0.1:3000".parse::<SocketAddr>().unwrap()
        );
    }
}
