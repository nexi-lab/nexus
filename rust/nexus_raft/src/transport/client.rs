//! gRPC client for Raft transport.
//!
//! Provides a client to communicate with other Raft nodes using tonic gRPC.

use super::proto::nexus::raft::{
    raft_service_client::RaftServiceClient,
    raft_client_service_client::RaftClientServiceClient,
    AppendEntriesRequest, LogEntry as ProtoLogEntry,
    VoteRequest, ProposeRequest, QueryRequest, GetClusterInfoRequest,
    RaftCommand, RaftQuery,
    raft_command::Command as ProtoCommandVariant,
    raft_query::Query as ProtoQueryVariant,
    PutMetadata, DeleteMetadata, AcquireLock, ReleaseLock, ExtendLock,
    GetMetadata, ListMetadata, GetLockInfo,
};
use super::{NodeAddress, Result, TransportError};
use std::collections::HashMap;
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::RwLock;
use tonic::transport::{Channel, Endpoint};

/// Configuration for Raft transport client.
#[derive(Debug, Clone)]
pub struct ClientConfig {
    /// Connection timeout.
    pub connect_timeout: Duration,
    /// Request timeout.
    pub request_timeout: Duration,
    /// Keep-alive interval.
    pub keep_alive_interval: Duration,
    /// Keep-alive timeout.
    pub keep_alive_timeout: Duration,
}

impl Default for ClientConfig {
    fn default() -> Self {
        Self {
            connect_timeout: Duration::from_secs(5),
            request_timeout: Duration::from_secs(10),
            keep_alive_interval: Duration::from_secs(10),
            keep_alive_timeout: Duration::from_secs(20),
        }
    }
}

/// A pool of gRPC clients for connecting to Raft peers.
///
/// This manages connections to multiple nodes and handles reconnection.
#[derive(Clone)]
pub struct RaftClientPool {
    config: ClientConfig,
    clients: Arc<RwLock<HashMap<u64, RaftClient>>>,
}

impl RaftClientPool {
    /// Create a new client pool with default configuration.
    pub fn new() -> Self {
        Self::with_config(ClientConfig::default())
    }

    /// Create a new client pool with custom configuration.
    pub fn with_config(config: ClientConfig) -> Self {
        Self {
            config,
            clients: Arc::new(RwLock::new(HashMap::new())),
        }
    }

    /// Get or create a client for the given node.
    pub async fn get(&self, addr: &NodeAddress) -> Result<RaftClient> {
        // Check if we have an existing client
        {
            let clients = self.clients.read().await;
            if let Some(client) = clients.get(&addr.id) {
                return Ok(client.clone());
            }
        }

        // Create new client
        let client = RaftClient::connect(&addr.endpoint, self.config.clone()).await?;

        // Store in pool
        {
            let mut clients = self.clients.write().await;
            clients.insert(addr.id, client.clone());
        }

        Ok(client)
    }

    /// Remove a client from the pool (e.g., after connection failure).
    pub async fn remove(&self, node_id: u64) {
        let mut clients = self.clients.write().await;
        clients.remove(&node_id);
    }

    /// Get the number of active connections.
    pub async fn connection_count(&self) -> usize {
        self.clients.read().await.len()
    }
}

impl Default for RaftClientPool {
    fn default() -> Self {
        Self::new()
    }
}

/// A single gRPC client for communicating with a Raft node.
#[derive(Clone)]
pub struct RaftClient {
    endpoint: String,
    #[allow(dead_code)]
    config: ClientConfig,
    inner: RaftServiceClient<Channel>,
}

impl RaftClient {
    /// Connect to a Raft node.
    pub async fn connect(endpoint: &str, config: ClientConfig) -> Result<Self> {
        tracing::info!("Connecting to Raft node at {}", endpoint);

        let channel = Endpoint::from_shared(endpoint.to_string())
            .map_err(|e| TransportError::InvalidAddress(e.to_string()))?
            .connect_timeout(config.connect_timeout)
            .timeout(config.request_timeout)
            .http2_keep_alive_interval(config.keep_alive_interval)
            .keep_alive_timeout(config.keep_alive_timeout)
            .connect()
            .await?;

        let inner = RaftServiceClient::new(channel);

        tracing::info!("Connected to Raft node at {}", endpoint);

        Ok(Self {
            endpoint: endpoint.to_string(),
            config,
            inner,
        })
    }

    /// Get the endpoint this client is connected to.
    pub fn endpoint(&self) -> &str {
        &self.endpoint
    }

    /// Send a vote request.
    pub async fn request_vote(
        &mut self,
        term: u64,
        candidate_id: u64,
        last_log_index: u64,
        last_log_term: u64,
    ) -> Result<VoteResponseLocal> {
        tracing::debug!(
            "Sending vote request to {}: term={}, candidate_id={}",
            self.endpoint,
            term,
            candidate_id
        );

        let request = tonic::Request::new(VoteRequest {
            term,
            candidate_id,
            last_log_index,
            last_log_term,
        });

        let response = self.inner.request_vote(request).await?;
        let resp = response.into_inner();

        Ok(VoteResponseLocal {
            term: resp.term,
            vote_granted: resp.vote_granted,
        })
    }

    /// Send append entries (log replication or heartbeat).
    pub async fn append_entries(
        &mut self,
        term: u64,
        leader_id: u64,
        prev_log_index: u64,
        prev_log_term: u64,
        entries: Vec<LogEntry>,
        leader_commit: u64,
    ) -> Result<AppendEntriesResponseLocal> {
        tracing::debug!(
            "Sending append entries to {}: term={}, entries={}",
            self.endpoint,
            term,
            entries.len()
        );

        let proto_entries: Vec<ProtoLogEntry> = entries
            .into_iter()
            .map(|e| ProtoLogEntry {
                term: e.term,
                index: e.index,
                entry_type: e.entry_type as i32,
                data: e.data,
            })
            .collect();

        let request = tonic::Request::new(AppendEntriesRequest {
            term,
            leader_id,
            prev_log_index,
            prev_log_term,
            entries: proto_entries,
            leader_commit,
        });

        let response = self.inner.append_entries(request).await?;
        let resp = response.into_inner();

        Ok(AppendEntriesResponseLocal {
            term: resp.term,
            success: resp.success,
            match_index: resp.match_index,
        })
    }
}

/// Response to a vote request.
#[derive(Debug, Clone)]
pub struct VoteResponseLocal {
    /// Current term of the voter.
    pub term: u64,
    /// Whether the vote was granted.
    pub vote_granted: bool,
}

/// Response to append entries request.
#[derive(Debug, Clone)]
pub struct AppendEntriesResponseLocal {
    /// Current term of the follower.
    pub term: u64,
    /// Whether the append was successful.
    pub success: bool,
    /// Hint for the next index to try.
    pub match_index: u64,
}

/// A log entry to be replicated.
#[derive(Debug, Clone)]
pub struct LogEntry {
    /// Term when entry was created.
    pub term: u64,
    /// Index in the log.
    pub index: u64,
    /// Entry type (0 = normal, 1 = conf change).
    pub entry_type: u32,
    /// Entry data (serialized command).
    pub data: Vec<u8>,
}

// =============================================================================
// Client-Facing API Client (for Python/CLI)
// =============================================================================

/// A client for the Raft cluster's client-facing API.
///
/// This client is used by Python, CLI, and other external clients to
/// interact with the Raft cluster. It uses the RaftClientService which
/// provides Propose (writes) and Query (reads) operations.
#[derive(Clone)]
pub struct RaftApiClient {
    endpoint: String,
    #[allow(dead_code)]
    config: ClientConfig,
    inner: RaftClientServiceClient<Channel>,
}

impl RaftApiClient {
    /// Connect to a Raft cluster node.
    pub async fn connect(endpoint: &str, config: ClientConfig) -> Result<Self> {
        tracing::info!("Connecting to Raft API at {}", endpoint);

        let channel = Endpoint::from_shared(endpoint.to_string())
            .map_err(|e| TransportError::InvalidAddress(e.to_string()))?
            .connect_timeout(config.connect_timeout)
            .timeout(config.request_timeout)
            .http2_keep_alive_interval(config.keep_alive_interval)
            .keep_alive_timeout(config.keep_alive_timeout)
            .connect()
            .await?;

        let inner = RaftClientServiceClient::new(channel);

        tracing::info!("Connected to Raft API at {}", endpoint);

        Ok(Self {
            endpoint: endpoint.to_string(),
            config,
            inner,
        })
    }

    /// Get the endpoint this client is connected to.
    pub fn endpoint(&self) -> &str {
        &self.endpoint
    }

    // === Propose Methods (Writes) ===

    /// Put metadata for a file path.
    pub async fn put_metadata(
        &mut self,
        metadata: super::proto::nexus::core::FileMetadata,
    ) -> Result<ProposeResult> {
        let cmd = RaftCommand {
            command: Some(ProtoCommandVariant::PutMetadata(PutMetadata {
                metadata: Some(metadata),
            })),
        };
        self.propose(cmd, None).await
    }

    /// Delete metadata for a file path.
    pub async fn delete_metadata(&mut self, path: &str, zone_id: &str) -> Result<ProposeResult> {
        let cmd = RaftCommand {
            command: Some(ProtoCommandVariant::DeleteMetadata(DeleteMetadata {
                path: path.to_string(),
                zone_id: zone_id.to_string(),
            })),
        };
        self.propose(cmd, None).await
    }

    /// Acquire a distributed lock.
    pub async fn acquire_lock(
        &mut self,
        lock_id: &str,
        holder_id: &str,
        ttl_ms: i64,
        zone_id: &str,
    ) -> Result<ProposeResult> {
        let cmd = RaftCommand {
            command: Some(ProtoCommandVariant::AcquireLock(AcquireLock {
                lock_id: lock_id.to_string(),
                holder_id: holder_id.to_string(),
                ttl_ms,
                zone_id: zone_id.to_string(),
            })),
        };
        self.propose(cmd, None).await
    }

    /// Release a distributed lock.
    pub async fn release_lock(
        &mut self,
        lock_id: &str,
        holder_id: &str,
        zone_id: &str,
    ) -> Result<ProposeResult> {
        let cmd = RaftCommand {
            command: Some(ProtoCommandVariant::ReleaseLock(ReleaseLock {
                lock_id: lock_id.to_string(),
                holder_id: holder_id.to_string(),
                zone_id: zone_id.to_string(),
            })),
        };
        self.propose(cmd, None).await
    }

    /// Extend a distributed lock's TTL.
    pub async fn extend_lock(
        &mut self,
        lock_id: &str,
        holder_id: &str,
        ttl_ms: i64,
        zone_id: &str,
    ) -> Result<ProposeResult> {
        let cmd = RaftCommand {
            command: Some(ProtoCommandVariant::ExtendLock(ExtendLock {
                lock_id: lock_id.to_string(),
                holder_id: holder_id.to_string(),
                ttl_ms,
                zone_id: zone_id.to_string(),
            })),
        };
        self.propose(cmd, None).await
    }

    /// Generic propose method.
    async fn propose(&mut self, command: RaftCommand, request_id: Option<String>) -> Result<ProposeResult> {
        let request = tonic::Request::new(ProposeRequest {
            command: Some(command),
            request_id: request_id.unwrap_or_default(),
        });

        let response = self.inner.propose(request).await?;
        let resp = response.into_inner();

        Ok(ProposeResult {
            success: resp.success,
            error: resp.error,
            leader_address: resp.leader_address,
            applied_index: resp.applied_index,
        })
    }

    // === Query Methods (Reads) ===

    /// Get metadata for a file path.
    pub async fn get_metadata(
        &mut self,
        path: &str,
        zone_id: &str,
        read_from_leader: bool,
    ) -> Result<QueryResult> {
        let query = RaftQuery {
            query: Some(ProtoQueryVariant::GetMetadata(GetMetadata {
                path: path.to_string(),
                zone_id: zone_id.to_string(),
            })),
        };
        self.query(query, read_from_leader).await
    }

    /// List metadata under a prefix.
    pub async fn list_metadata(
        &mut self,
        prefix: &str,
        zone_id: &str,
        recursive: bool,
        limit: i32,
        read_from_leader: bool,
    ) -> Result<QueryResult> {
        let query = RaftQuery {
            query: Some(ProtoQueryVariant::ListMetadata(ListMetadata {
                prefix: prefix.to_string(),
                zone_id: zone_id.to_string(),
                recursive,
                limit,
                cursor: String::new(),
            })),
        };
        self.query(query, read_from_leader).await
    }

    /// Get lock information.
    pub async fn get_lock_info(
        &mut self,
        lock_id: &str,
        zone_id: &str,
        read_from_leader: bool,
    ) -> Result<QueryResult> {
        let query = RaftQuery {
            query: Some(ProtoQueryVariant::GetLockInfo(GetLockInfo {
                lock_id: lock_id.to_string(),
                zone_id: zone_id.to_string(),
            })),
        };
        self.query(query, read_from_leader).await
    }

    /// Generic query method.
    async fn query(&mut self, query: RaftQuery, read_from_leader: bool) -> Result<QueryResult> {
        let request = tonic::Request::new(QueryRequest {
            query: Some(query),
            read_from_leader,
        });

        let response = self.inner.query(request).await?;
        let resp = response.into_inner();

        Ok(QueryResult {
            success: resp.success,
            error: resp.error,
            leader_address: resp.leader_address,
            result: resp.result,
        })
    }

    // === Cluster Info ===

    /// Get cluster information.
    pub async fn get_cluster_info(&mut self) -> Result<ClusterInfoResult> {
        let request = tonic::Request::new(GetClusterInfoRequest {});

        let response = self.inner.get_cluster_info(request).await?;
        let resp = response.into_inner();

        Ok(ClusterInfoResult {
            node_id: resp.node_id,
            leader_id: resp.leader_id,
            term: resp.term,
            is_leader: resp.is_leader,
            leader_address: resp.leader_address,
        })
    }
}

/// Result of a Propose operation.
#[derive(Debug, Clone)]
pub struct ProposeResult {
    /// Whether the proposal succeeded.
    pub success: bool,
    /// Error message if failed.
    pub error: Option<String>,
    /// Leader address if this node is not the leader.
    pub leader_address: Option<String>,
    /// Log index where the command was applied.
    pub applied_index: u64,
}

/// Result of a Query operation.
#[derive(Debug, Clone)]
pub struct QueryResult {
    /// Whether the query succeeded.
    pub success: bool,
    /// Error message if failed.
    pub error: Option<String>,
    /// Leader address if read_from_leader was requested but not leader.
    pub leader_address: Option<String>,
    /// Query result (proto message).
    pub result: Option<super::proto::nexus::raft::RaftQueryResponse>,
}

/// Result of a GetClusterInfo operation.
#[derive(Debug, Clone)]
pub struct ClusterInfoResult {
    /// This node's ID.
    pub node_id: u64,
    /// Current leader ID (0 if unknown).
    pub leader_id: u64,
    /// Current Raft term.
    pub term: u64,
    /// Whether this node is the leader.
    pub is_leader: bool,
    /// Leader address (if known).
    pub leader_address: Option<String>,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn test_client_pool() {
        let pool = RaftClientPool::new();
        assert_eq!(pool.connection_count().await, 0);
    }

    #[test]
    fn test_client_config_default() {
        let config = ClientConfig::default();
        assert_eq!(config.connect_timeout, Duration::from_secs(5));
        assert_eq!(config.request_timeout, Duration::from_secs(10));
    }
}
