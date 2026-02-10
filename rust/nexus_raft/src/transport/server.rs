//! gRPC server for Raft transport.
//!
//! Provides a tonic-based server to handle incoming Raft messages from other nodes.

// Workaround: these lints will be resolved when the transport layer is refactored.
// #[expect] ensures Rust warns us to remove these when the lints no longer fire.
#![expect(
    clippy::result_large_err,
    reason = "TransportError contains tonic types; will Box large variants in transport refactor"
)]
#![expect(
    clippy::if_same_then_else,
    reason = "vote logic uses separate branches for readability; will extract shared helper"
)]
#![expect(clippy::manual_ok_err, reason = "will clean up in transport refactor")]

use super::proto::nexus::raft::{
    raft_client_service_server::{RaftClientService, RaftClientServiceServer},
    raft_command::Command as ProtoCommandVariant,
    raft_query::Query as ProtoQueryVariant,
    raft_query_response::Result as ProtoQueryResultVariant,
    raft_response::Result as ProtoResponseResultVariant,
    raft_service_server::{RaftService, RaftServiceServer},
    AppendEntriesRequest, AppendEntriesResponse, GetClusterInfoRequest, GetClusterInfoResponse,
    GetMetadataResult, InstallSnapshotResponse, ListMetadataResult, LockInfoResult, LockResult,
    ProposeRequest, ProposeResponse, QueryRequest, QueryResponse, RaftCommand, RaftQueryResponse,
    RaftResponse, SnapshotChunk, TransferLeaderRequest, TransferLeaderResponse, VoteRequest,
    VoteResponse,
};
use super::{Result, TransportError};
use crate::raft::{Command, FullStateMachine, StateMachine, WitnessStateMachine};
use crate::storage::SledStore;
use prost::Message;
use std::net::SocketAddr;
use std::sync::Arc;
use tokio::sync::RwLock;
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

/// Shared state for the Raft server.
pub struct RaftServerState {
    /// Current term.
    pub current_term: u64,
    /// Node ID of current leader (if known).
    pub leader_id: Option<u64>,
    /// This node's ID.
    pub node_id: u64,
    /// Voted for in current term.
    pub voted_for: Option<u64>,
    /// Last log index.
    pub last_log_index: u64,
    /// Last log term.
    pub last_log_term: u64,
    /// Commit index.
    pub commit_index: u64,
    /// State machine for applying commands.
    pub state_machine: FullStateMachine,
    /// Underlying storage.
    pub store: SledStore,
}

impl RaftServerState {
    /// Create a new server state with the given storage path.
    pub fn new(node_id: u64, db_path: &str) -> Result<Self> {
        let store = SledStore::open(db_path)
            .map_err(|e| TransportError::Connection(format!("Failed to open store: {}", e)))?;
        let state_machine = FullStateMachine::new(&store).map_err(|e| {
            TransportError::Connection(format!("Failed to create state machine: {}", e))
        })?;

        Ok(Self {
            current_term: 0,
            leader_id: None,
            node_id,
            voted_for: None,
            last_log_index: state_machine.last_applied_index(),
            last_log_term: 0,
            commit_index: state_machine.last_applied_index(),
            state_machine,
            store,
        })
    }
}

/// A gRPC server for Raft transport.
///
/// This server handles incoming Raft RPC requests and routes them to the state machine.
pub struct RaftServer {
    config: ServerConfig,
    state: Arc<RwLock<RaftServerState>>,
}

impl RaftServer {
    /// Create a new Raft server with the given node ID and database path.
    pub fn new(node_id: u64, db_path: &str) -> Result<Self> {
        Self::with_config(node_id, db_path, ServerConfig::default())
    }

    /// Create a new Raft server with custom configuration.
    pub fn with_config(node_id: u64, db_path: &str, config: ServerConfig) -> Result<Self> {
        let state = RaftServerState::new(node_id, db_path)?;
        Ok(Self {
            config,
            state: Arc::new(RwLock::new(state)),
        })
    }

    /// Get the bind address.
    pub fn bind_address(&self) -> SocketAddr {
        self.config.bind_address
    }

    /// Get a handle to the server state (for testing).
    pub fn state(&self) -> Arc<RwLock<RaftServerState>> {
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
            // UpdateRouting is not supported in the current state machine
            tracing::warn!("UpdateRouting command not supported");
            None
        }
    }
}

/// Implementation of the RaftService gRPC trait (internal node-to-node).
struct RaftServiceImpl {
    state: Arc<RwLock<RaftServerState>>,
}

/// Implementation of the RaftClientService gRPC trait (client-facing).
struct RaftClientServiceImpl {
    state: Arc<RwLock<RaftServerState>>,
}

#[tonic::async_trait]
impl RaftService for RaftServiceImpl {
    /// Handle a vote request from a candidate.
    async fn request_vote(
        &self,
        request: Request<VoteRequest>,
    ) -> std::result::Result<Response<VoteResponse>, Status> {
        let req = request.into_inner();
        tracing::debug!(
            "Received vote request: term={}, candidate_id={}, last_log_index={}, last_log_term={}",
            req.term,
            req.candidate_id,
            req.last_log_index,
            req.last_log_term
        );

        let mut state = self.state.write().await;

        // Update term if needed
        if req.term > state.current_term {
            state.current_term = req.term;
            state.voted_for = None;
            state.leader_id = None;
        }

        // Check if we can grant the vote
        let vote_granted = if req.term < state.current_term {
            // Candidate's term is old
            false
        } else if state.voted_for.is_some() && state.voted_for != Some(req.candidate_id) {
            // Already voted for someone else this term
            false
        } else if req.last_log_term < state.last_log_term {
            // Candidate's log is not up to date (term)
            false
        } else if req.last_log_term == state.last_log_term
            && req.last_log_index < state.last_log_index
        {
            // Candidate's log is not up to date (index)
            false
        } else {
            // Grant vote
            state.voted_for = Some(req.candidate_id);
            true
        };

        tracing::debug!(
            "Vote response: term={}, granted={} (to candidate {})",
            state.current_term,
            vote_granted,
            req.candidate_id
        );

        Ok(Response::new(VoteResponse {
            term: state.current_term,
            vote_granted,
        }))
    }

    /// Handle append entries from a leader.
    async fn append_entries(
        &self,
        request: Request<AppendEntriesRequest>,
    ) -> std::result::Result<Response<AppendEntriesResponse>, Status> {
        let req = request.into_inner();
        tracing::debug!(
            "Received append entries: term={}, leader_id={}, entries={}, leader_commit={}",
            req.term,
            req.leader_id,
            req.entries.len(),
            req.leader_commit
        );

        let mut state = self.state.write().await;

        // Update term if needed
        if req.term > state.current_term {
            state.current_term = req.term;
            state.voted_for = None;
        }

        // Check if we should reject
        if req.term < state.current_term {
            return Ok(Response::new(AppendEntriesResponse {
                term: state.current_term,
                success: false,
                match_index: state.last_log_index,
            }));
        }

        // Accept leader
        state.leader_id = Some(req.leader_id);

        // Process entries
        let mut match_index = state.last_log_index;
        for entry in req.entries {
            // Deserialize command - try protobuf first, then bincode for backwards compatibility
            let cmd = if let Ok(proto_cmd) = RaftCommand::decode(entry.data.as_slice()) {
                proto_command_to_internal(proto_cmd)
            } else if let Ok(cmd) = bincode::deserialize::<Command>(&entry.data) {
                Some(cmd)
            } else {
                None
            };

            if let Some(cmd) = cmd {
                match state.state_machine.apply(entry.index, &cmd) {
                    Ok(_) => {
                        match_index = entry.index;
                        state.last_log_index = entry.index;
                        state.last_log_term = entry.term;
                    }
                    Err(e) => {
                        tracing::error!("Failed to apply command at index {}: {}", entry.index, e);
                    }
                }
            } else {
                tracing::warn!("Failed to deserialize command at index {}", entry.index);
            }
        }

        // Update commit index
        if req.leader_commit > state.commit_index {
            state.commit_index = std::cmp::min(req.leader_commit, match_index);
        }

        tracing::debug!(
            "Append entries response: term={}, success=true, match_index={}",
            state.current_term,
            match_index
        );

        Ok(Response::new(AppendEntriesResponse {
            term: state.current_term,
            success: true,
            match_index,
        }))
    }

    /// Handle snapshot installation (streaming).
    async fn install_snapshot(
        &self,
        request: Request<Streaming<SnapshotChunk>>,
    ) -> std::result::Result<Response<InstallSnapshotResponse>, Status> {
        let mut stream = request.into_inner();
        let mut snapshot_data = Vec::new();
        let mut _last_term = 0u64;

        while let Some(chunk) = stream.message().await? {
            if let Some(metadata) = chunk.metadata {
                _last_term = metadata.term;
            }
            snapshot_data.extend(chunk.data);
            if chunk.done {
                break;
            }
        }

        let mut state = self.state.write().await;

        // Apply snapshot
        match state.state_machine.restore_snapshot(&snapshot_data) {
            Ok(_) => {
                tracing::info!("Snapshot installed successfully");
                Ok(Response::new(InstallSnapshotResponse {
                    term: state.current_term,
                    success: true,
                }))
            }
            Err(e) => {
                tracing::error!("Failed to install snapshot: {}", e);
                Ok(Response::new(InstallSnapshotResponse {
                    term: state.current_term,
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
        let state = self.state.read().await;

        // Only leader can transfer leadership
        if state.leader_id != Some(state.node_id) {
            return Ok(Response::new(TransferLeaderResponse {
                success: false,
                error: "Not the leader".to_string(),
            }));
        }

        tracing::info!("Leadership transfer requested to node {}", req.target_id);

        // In a full implementation, this would:
        // 1. Stop accepting new client requests
        // 2. Catch up the target node
        // 3. Send a TimeoutNow message to the target

        Ok(Response::new(TransferLeaderResponse {
            success: true,
            error: String::new(),
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
    /// Only the leader can accept proposals. If this node is not the leader,
    /// returns the leader address for the client to redirect.
    async fn propose(
        &self,
        request: Request<ProposeRequest>,
    ) -> std::result::Result<Response<ProposeResponse>, Status> {
        let req = request.into_inner();
        tracing::debug!("Received propose request: {:?}", req.request_id);

        let mut state = self.state.write().await;

        // Check if we're the leader
        if state.leader_id != Some(state.node_id) {
            // Not the leader - return redirect
            return Ok(Response::new(ProposeResponse {
                success: false,
                error: Some("Not the leader".to_string()),
                leader_address: None, // TODO: Track leader address in state
                result: None,
                applied_index: 0,
            }));
        }

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

        let cmd = match proto_command_to_internal(proto_cmd.clone()) {
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

        // Apply the command to state machine
        let next_index = state.last_log_index + 1;
        let result = match state.state_machine.apply(next_index, &cmd) {
            Ok(cmd_result) => {
                state.last_log_index = next_index;
                state.commit_index = next_index;
                cmd_result
            }
            Err(e) => {
                return Ok(Response::new(ProposeResponse {
                    success: false,
                    error: Some(format!("Failed to apply command: {}", e)),
                    leader_address: None,
                    result: None,
                    applied_index: 0,
                }));
            }
        };

        // Convert result to proto
        let proto_result = command_result_to_proto(&result);

        Ok(Response::new(ProposeResponse {
            success: true,
            error: None,
            leader_address: None,
            result: Some(proto_result),
            applied_index: next_index,
        }))
    }

    /// Handle a client query (read operation).
    ///
    /// Reads from the local state machine. If read_from_leader is true and
    /// this node is not the leader, returns the leader address for redirect.
    async fn query(
        &self,
        request: Request<QueryRequest>,
    ) -> std::result::Result<Response<QueryResponse>, Status> {
        let req = request.into_inner();
        tracing::debug!(
            "Received query request, read_from_leader={}",
            req.read_from_leader
        );

        let state = self.state.read().await;

        // Check if linearizable read is requested
        if req.read_from_leader && state.leader_id != Some(state.node_id) {
            return Ok(Response::new(QueryResponse {
                success: false,
                error: Some("Not the leader".to_string()),
                leader_address: None, // TODO: Track leader address
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

        // Process query based on type
        let query_result = match proto_query.query {
            Some(ProtoQueryVariant::GetMetadata(gm)) => {
                match state.state_machine.get_metadata(&gm.path) {
                    Ok(Some(data)) => {
                        // Decode the stored protobuf metadata
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
                }
            }
            Some(ProtoQueryVariant::ListMetadata(lm)) => {
                match state.state_machine.list_metadata(&lm.prefix) {
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
                }
            }
            Some(ProtoQueryVariant::GetLockInfo(gli)) => {
                match state.state_machine.get_lock(&gli.lock_id) {
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
                }
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
        let state = self.state.read().await;

        Ok(Response::new(GetClusterInfoResponse {
            node_id: state.node_id,
            leader_id: state.leader_id.unwrap_or(0),
            term: state.current_term,
            config: None, // TODO: Track cluster configuration
            is_leader: state.leader_id == Some(state.node_id),
            leader_address: None, // TODO: Track leader address
        }))
    }
}

/// Convert internal CommandResult to proto RaftResponse.
fn command_result_to_proto(result: &crate::raft::CommandResult) -> RaftResponse {
    use crate::raft::CommandResult;

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
// Witness Server (lightweight, log-only)
// =============================================================================

/// Shared state for the Witness server.
pub struct WitnessServerState {
    /// Current term.
    pub current_term: u64,
    /// This node's ID.
    pub node_id: u64,
    /// Voted for in current term.
    pub voted_for: Option<u64>,
    /// Last log index.
    pub last_log_index: u64,
    /// Last log term.
    pub last_log_term: u64,
    /// Witness state machine (log only, no apply).
    pub state_machine: WitnessStateMachine,
    /// Underlying storage.
    pub store: SledStore,
}

impl WitnessServerState {
    /// Create a new witness server state.
    pub fn new(node_id: u64, db_path: &str) -> Result<Self> {
        let store = SledStore::open(db_path)
            .map_err(|e| TransportError::Connection(format!("Failed to open store: {}", e)))?;
        let state_machine = WitnessStateMachine::new(&store).map_err(|e| {
            TransportError::Connection(format!("Failed to create witness state machine: {}", e))
        })?;

        Ok(Self {
            current_term: 0,
            node_id,
            voted_for: None,
            last_log_index: state_machine.last_applied_index(),
            last_log_term: 0,
            state_machine,
            store,
        })
    }
}

/// A gRPC server for Raft witness nodes.
///
/// Witness nodes participate in voting but don't serve reads.
/// They store log entries but don't apply them to a state machine.
pub struct RaftWitnessServer {
    config: ServerConfig,
    state: Arc<RwLock<WitnessServerState>>,
}

impl RaftWitnessServer {
    /// Create a new witness server.
    pub fn new(node_id: u64, db_path: &str) -> Result<Self> {
        Self::with_config(node_id, db_path, ServerConfig::default())
    }

    /// Create a new witness server with custom configuration.
    pub fn with_config(node_id: u64, db_path: &str, config: ServerConfig) -> Result<Self> {
        let state = WitnessServerState::new(node_id, db_path)?;
        Ok(Self {
            config,
            state: Arc::new(RwLock::new(state)),
        })
    }

    /// Get the bind address.
    pub fn bind_address(&self) -> SocketAddr {
        self.config.bind_address
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
    state: Arc<RwLock<WitnessServerState>>,
}

#[tonic::async_trait]
impl RaftService for WitnessServiceImpl {
    /// Handle a vote request - witness nodes CAN vote.
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

        let mut state = self.state.write().await;

        // Update term if needed
        if req.term > state.current_term {
            state.current_term = req.term;
            state.voted_for = None;
        }

        // Check if we can grant the vote
        let vote_granted = if req.term < state.current_term {
            false
        } else if state.voted_for.is_some() && state.voted_for != Some(req.candidate_id) {
            false
        } else if req.last_log_term < state.last_log_term {
            false
        } else if req.last_log_term == state.last_log_term
            && req.last_log_index < state.last_log_index
        {
            false
        } else {
            state.voted_for = Some(req.candidate_id);
            true
        };

        tracing::debug!(
            "[Witness] Vote response: term={}, granted={}",
            state.current_term,
            vote_granted
        );

        Ok(Response::new(VoteResponse {
            term: state.current_term,
            vote_granted,
        }))
    }

    /// Handle append entries - witness stores log but doesn't apply.
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

        let mut state = self.state.write().await;

        // Update term if needed
        if req.term > state.current_term {
            state.current_term = req.term;
            state.voted_for = None;
        }

        // Check if we should reject
        if req.term < state.current_term {
            return Ok(Response::new(AppendEntriesResponse {
                term: state.current_term,
                success: false,
                match_index: state.last_log_index,
            }));
        }

        // Witness: store log entries but don't apply them
        let mut match_index = state.last_log_index;
        for entry in req.entries {
            // Validate command (protobuf or bincode), but don't apply to state machine
            let is_valid = RaftCommand::decode(entry.data.as_slice()).is_ok()
                || bincode::deserialize::<Command>(&entry.data).is_ok();

            if is_valid {
                // Store the log entry (witness keeps log for voting validation)
                if let Err(e) = state
                    .state_machine
                    .store_log_entry(entry.index, &entry.data)
                {
                    tracing::error!(
                        "[Witness] Failed to store log entry {}: {}",
                        entry.index,
                        e
                    );
                    return Ok(tonic::Response::new(response));
                }
                match_index = entry.index;
                state.last_log_index = entry.index;
                state.last_log_term = entry.term;
            } else {
                tracing::warn!(
                    "[Witness] Failed to deserialize command at index {}",
                    entry.index
                );
            }
        }

        tracing::debug!(
            "[Witness] Append entries response: term={}, success=true, match_index={}",
            state.current_term,
            match_index
        );

        Ok(Response::new(AppendEntriesResponse {
            term: state.current_term,
            success: true,
            match_index,
        }))
    }

    /// Handle snapshot installation - witness ignores snapshots.
    async fn install_snapshot(
        &self,
        _request: Request<Streaming<SnapshotChunk>>,
    ) -> std::result::Result<Response<InstallSnapshotResponse>, Status> {
        let state = self.state.read().await;

        // Witness nodes don't need snapshots since they don't maintain state machine
        tracing::info!("[Witness] Ignoring snapshot installation request");

        Ok(Response::new(InstallSnapshotResponse {
            term: state.current_term,
            success: true, // ACK but don't actually install
        }))
    }

    /// Handle leadership transfer - witness cannot become leader.
    async fn transfer_leader(
        &self,
        _request: Request<TransferLeaderRequest>,
    ) -> std::result::Result<Response<TransferLeaderResponse>, Status> {
        // Witness nodes cannot become leaders
        Ok(Response::new(TransferLeaderResponse {
            success: false,
            error: "Witness nodes cannot become leaders".to_string(),
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

        let server = RaftServer::new(1, db_path.to_str().unwrap()).unwrap();
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

        let server = RaftServer::with_config(1, db_path.to_str().unwrap(), config).unwrap();
        assert_eq!(
            server.bind_address(),
            "127.0.0.1:3000".parse::<SocketAddr>().unwrap()
        );
    }
}
