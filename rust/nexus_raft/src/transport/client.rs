//! gRPC client for Raft transport.
//!
//! Provides a client to communicate with other Raft nodes.

use super::{NodeAddress, Result};
use std::collections::HashMap;
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::RwLock;

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
    config: ClientConfig,
    // The actual tonic client would be stored here
    // For now, this is a placeholder that will be filled in Commit 3
    #[allow(dead_code)]
    inner: Arc<()>,
}

impl RaftClient {
    /// Connect to a Raft node.
    pub async fn connect(endpoint: &str, config: ClientConfig) -> Result<Self> {
        // In Commit 3, this will actually establish a gRPC connection
        // For now, just store the endpoint
        tracing::info!("Creating Raft client for {}", endpoint);

        Ok(Self {
            endpoint: endpoint.to_string(),
            config,
            inner: Arc::new(()),
        })
    }

    /// Get the endpoint this client is connected to.
    pub fn endpoint(&self) -> &str {
        &self.endpoint
    }

    /// Send a vote request.
    ///
    /// This is a placeholder that will be implemented in Commit 3.
    pub async fn request_vote(
        &self,
        term: u64,
        candidate_id: u64,
        last_log_index: u64,
        last_log_term: u64,
    ) -> Result<VoteResponse> {
        tracing::debug!(
            "Sending vote request to {}: term={}, candidate_id={}",
            self.endpoint,
            term,
            candidate_id
        );

        // Placeholder implementation
        // In Commit 3, this will use the generated proto client
        let _ = (term, candidate_id, last_log_index, last_log_term);
        let _ = self.config.request_timeout;

        Ok(VoteResponse {
            term: 0,
            vote_granted: false,
        })
    }

    /// Send append entries (log replication or heartbeat).
    ///
    /// This is a placeholder that will be implemented in Commit 3.
    pub async fn append_entries(
        &self,
        term: u64,
        leader_id: u64,
        prev_log_index: u64,
        prev_log_term: u64,
        entries: Vec<LogEntry>,
        leader_commit: u64,
    ) -> Result<AppendEntriesResponse> {
        tracing::debug!(
            "Sending append entries to {}: term={}, entries={}",
            self.endpoint,
            term,
            entries.len()
        );

        // Placeholder implementation
        let _ = (
            term,
            leader_id,
            prev_log_index,
            prev_log_term,
            entries,
            leader_commit,
        );

        Ok(AppendEntriesResponse {
            term: 0,
            success: false,
            match_index: 0,
        })
    }
}

/// Response to a vote request.
#[derive(Debug, Clone)]
pub struct VoteResponse {
    /// Current term of the voter.
    pub term: u64,
    /// Whether the vote was granted.
    pub vote_granted: bool,
}

/// Response to append entries request.
#[derive(Debug, Clone)]
pub struct AppendEntriesResponse {
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
    /// Entry data (serialized command).
    pub data: Vec<u8>,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn test_client_pool() {
        let pool = RaftClientPool::new();

        let addr1 = NodeAddress::new(1, "http://localhost:2026");
        let addr2 = NodeAddress::new(2, "http://localhost:2027");

        // Get clients (creates new ones)
        let _client1 = pool.get(&addr1).await.unwrap();
        let _client2 = pool.get(&addr2).await.unwrap();

        assert_eq!(pool.connection_count().await, 2);

        // Get again (reuses existing)
        let _client1_again = pool.get(&addr1).await.unwrap();
        assert_eq!(pool.connection_count().await, 2);

        // Remove
        pool.remove(1).await;
        assert_eq!(pool.connection_count().await, 1);
    }

    #[tokio::test]
    async fn test_client_connect() {
        let config = ClientConfig::default();
        let client = RaftClient::connect("http://localhost:2026", config)
            .await
            .unwrap();
        assert_eq!(client.endpoint(), "http://localhost:2026");
    }
}
