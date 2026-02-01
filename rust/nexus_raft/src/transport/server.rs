//! gRPC server for Raft transport.
//!
//! Provides a server to handle incoming Raft messages from other nodes.

use super::{Result, TransportError};
use std::net::SocketAddr;
use std::sync::Arc;

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

/// Trait for handling Raft RPC requests.
///
/// Implement this trait to provide the actual Raft logic.
/// This will be implemented by the Raft state machine in Commit 3.
#[allow(async_fn_in_trait)]
pub trait RaftHandler: Send + Sync + 'static {
    /// Handle a vote request from a candidate.
    async fn handle_vote_request(
        &self,
        term: u64,
        candidate_id: u64,
        last_log_index: u64,
        last_log_term: u64,
    ) -> Result<(u64, bool)>; // (term, vote_granted)

    /// Handle append entries from a leader.
    async fn handle_append_entries(
        &self,
        term: u64,
        leader_id: u64,
        prev_log_index: u64,
        prev_log_term: u64,
        entries: Vec<super::client::LogEntry>,
        leader_commit: u64,
    ) -> Result<(u64, bool, u64)>; // (term, success, match_index)

    /// Handle snapshot installation (optional, for advanced use).
    async fn handle_install_snapshot(
        &self,
        _term: u64,
        _leader_id: u64,
        _last_included_index: u64,
        _last_included_term: u64,
        _data: Vec<u8>,
    ) -> Result<u64> {
        // Default: return current term, not supported
        Err(TransportError::Rpc("snapshot not supported".into()))
    }
}

/// A gRPC server for Raft transport.
///
/// This server handles incoming Raft RPC requests and routes them to the handler.
pub struct RaftServer<H: RaftHandler> {
    config: ServerConfig,
    handler: Arc<H>,
    // In Commit 3, this will hold the tonic Server
    #[allow(dead_code)]
    running: bool,
}

impl<H: RaftHandler> RaftServer<H> {
    /// Create a new Raft server with the given handler.
    pub fn new(handler: H) -> Self {
        Self::with_config(handler, ServerConfig::default())
    }

    /// Create a new Raft server with custom configuration.
    pub fn with_config(handler: H, config: ServerConfig) -> Self {
        Self {
            config,
            handler: Arc::new(handler),
            running: false,
        }
    }

    /// Get the bind address.
    pub fn bind_address(&self) -> SocketAddr {
        self.config.bind_address
    }

    /// Start the server.
    ///
    /// This is a placeholder that will be implemented in Commit 3.
    pub async fn serve(self) -> Result<()> {
        tracing::info!("Starting Raft server on {}", self.config.bind_address);

        // In Commit 3, this will actually start a tonic server
        // For now, just log and return
        let _ = self.handler;

        // Placeholder: simulate server running
        // In production, this would be:
        // tonic::transport::Server::builder()
        //     .add_service(RaftServiceServer::new(self))
        //     .serve(self.config.bind_address)
        //     .await?;

        Ok(())
    }

    /// Gracefully shutdown the server.
    pub async fn shutdown(&self) -> Result<()> {
        tracing::info!("Shutting down Raft server");
        Ok(())
    }
}

/// A simple handler that rejects all requests.
///
/// Useful for testing or as a starting point.
pub struct RejectAllHandler;

impl RaftHandler for RejectAllHandler {
    async fn handle_vote_request(
        &self,
        term: u64,
        _candidate_id: u64,
        _last_log_index: u64,
        _last_log_term: u64,
    ) -> Result<(u64, bool)> {
        // Reject vote, return current term as 0
        Ok((term, false))
    }

    async fn handle_append_entries(
        &self,
        term: u64,
        _leader_id: u64,
        _prev_log_index: u64,
        _prev_log_term: u64,
        _entries: Vec<super::client::LogEntry>,
        _leader_commit: u64,
    ) -> Result<(u64, bool, u64)> {
        // Reject entries
        Ok((term, false, 0))
    }
}

/// Event stream handler trait for webhook-style streaming.
///
/// This demonstrates how gRPC Server Streaming can be used for webhooks.
#[allow(async_fn_in_trait)]
pub trait EventStreamHandler: Send + Sync + 'static {
    /// Subscribe to events.
    ///
    /// Returns a stream of events for the given topics.
    async fn subscribe(
        &self,
        topics: Vec<String>,
        filter: Option<String>,
        from_sequence: u64,
    ) -> Result<EventStream>;
}

/// A stream of events (placeholder for actual implementation).
pub struct EventStream {
    // In a real implementation, this would be a tokio channel receiver
    #[allow(dead_code)]
    topics: Vec<String>,
}

impl EventStream {
    /// Create a new event stream.
    pub fn new(topics: Vec<String>) -> Self {
        Self { topics }
    }

    /// Get the next event (placeholder).
    pub async fn next(&mut self) -> Option<Event> {
        // Placeholder: would actually receive from channel
        None
    }
}

/// An event in the stream.
#[derive(Debug, Clone)]
pub struct Event {
    /// Sequence number.
    pub sequence: u64,
    /// Topic.
    pub topic: String,
    /// Event type.
    pub event_type: String,
    /// Payload.
    pub payload: Vec<u8>,
    /// Timestamp in milliseconds.
    pub timestamp_ms: i64,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn test_server_creation() {
        let server = RaftServer::new(RejectAllHandler);
        assert_eq!(
            server.bind_address(),
            "0.0.0.0:2026".parse::<SocketAddr>().unwrap()
        );
    }

    #[tokio::test]
    async fn test_reject_all_handler() {
        let handler = RejectAllHandler;

        // Test vote rejection
        let (term, granted) = handler
            .handle_vote_request(1, 1, 0, 0)
            .await
            .unwrap();
        assert_eq!(term, 1);
        assert!(!granted);

        // Test append entries rejection
        let (term, success, _) = handler
            .handle_append_entries(1, 1, 0, 0, vec![], 0)
            .await
            .unwrap();
        assert_eq!(term, 1);
        assert!(!success);
    }

    #[tokio::test]
    async fn test_custom_config() {
        let config = ServerConfig {
            bind_address: "127.0.0.1:3000".parse().unwrap(),
            max_connections: 50,
            max_message_size: 32 * 1024 * 1024,
        };

        let server = RaftServer::with_config(RejectAllHandler, config);
        assert_eq!(
            server.bind_address(),
            "127.0.0.1:3000".parse::<SocketAddr>().unwrap()
        );
    }
}
