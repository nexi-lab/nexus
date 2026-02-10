//! gRPC transport layer for Raft consensus.
//!
//! This module provides the network transport for Raft messages using gRPC.
//! It is built on [tonic](https://github.com/hyperium/tonic), a pure Rust
//! gRPC implementation.
//!
//! # Why gRPC?
//!
//! - **Streaming**: Native support for bidirectional streams (ideal for heartbeats)
//! - **Efficiency**: HTTP/2 multiplexing, long-lived connections
//! - **Code generation**: Less boilerplate than manual HTTP
//! - **Compatibility**: Works with tikv/raft-rs message patterns
//!
//! # Reusability
//!
//! The gRPC transport is designed to be reusable beyond Raft:
//!
//! - **Raft Messages**: Leader election, log replication
//! - **Webhook Streaming**: Server Streaming pattern replaces HTTP POST webhooks
//! - **Real-time Events**: Bidirectional streams for live updates
//!
//! # Streaming Patterns
//!
//! | Pattern | Use Case | Example |
//! |---------|----------|---------|
//! | Unary | Single request/response | VoteRequest/VoteResponse |
//! | Server Streaming | Push events to client | Webhook events |
//! | Client Streaming | Bulk upload | Snapshot installation |
//! | Bidirectional | Real-time sync | Heartbeats, live updates |
//!
//! # Example
//!
//! ```rust,ignore
//! use nexus_raft::transport::{RaftClient, RaftServer};
//!
//! // Create a client to talk to another node
//! let client = RaftClient::connect("http://10.0.0.2:2026").await?;
//!
//! // Send a vote request
//! let response = client.request_vote(VoteRequest {
//!     term: 1,
//!     candidate_id: 1,
//!     last_log_index: 10,
//!     last_log_term: 1,
//! }).await?;
//!
//! // Start a server
//! let server = RaftServer::new(my_raft_handler);
//! server.serve("0.0.0.0:2026").await?;
//! ```
//!
//! # Feature Flag
//!
//! This module requires the `grpc` feature:
//!
//! ```toml
//! [dependencies]
//! nexus_raft = { version = "0.1", features = ["grpc"] }
//! ```

#[cfg(all(feature = "grpc", has_protos))]
mod client;
#[cfg(all(feature = "grpc", has_protos))]
mod server;
#[cfg(all(feature = "grpc", has_protos))]
mod transport_loop;

#[cfg(all(feature = "grpc", has_protos))]
pub use client::{
    AppendEntriesResponseLocal, ClientConfig, LogEntry, RaftClient, RaftClientPool,
    VoteResponseLocal,
};
#[cfg(all(feature = "grpc", has_protos))]
pub use server::{
    RaftServer, RaftServerState, RaftWitnessServer, ServerConfig, WitnessServerState,
};
#[cfg(all(feature = "grpc", has_protos))]
pub use transport_loop::TransportLoop;

// Re-export generated types when grpc feature is enabled and protos were compiled
#[cfg(all(feature = "grpc", has_protos))]
pub mod proto {
    //! Generated protobuf types and gRPC services.
    //!
    //! This module contains the Rust types generated from proto files.
    //! Structure mirrors the proto package hierarchy:
    //!   - nexus::core - FileMetadata, PaginatedResult
    //!   - nexus::raft - RaftService, commands, transport messages

    /// Core types (FileMetadata, etc.)
    pub mod nexus {
        pub mod core {
            include!(concat!(env!("OUT_DIR"), "/nexus.core.rs"));
        }
        #[expect(
            clippy::large_enum_variant,
            reason = "generated proto code; will configure prost boxing when variants are stabilized"
        )]
        pub mod raft {
            include!(concat!(env!("OUT_DIR"), "/nexus.raft.rs"));
        }
    }

    // Re-export for convenience
    pub use nexus::core::*;
    pub use nexus::raft::*;
}

/// Transport error types.
#[derive(Debug, thiserror::Error)]
pub enum TransportError {
    /// Connection failed.
    #[error("connection error: {0}")]
    Connection(String),

    /// RPC call failed.
    #[error("rpc error: {0}")]
    Rpc(String),

    /// Invalid address.
    #[error("invalid address: {0}")]
    InvalidAddress(String),

    /// Timeout.
    #[error("timeout after {0:?}")]
    Timeout(std::time::Duration),

    /// Server not running.
    #[error("server not running")]
    ServerNotRunning,

    #[cfg(feature = "grpc")]
    /// Tonic transport error.
    #[error("tonic error: {0}")]
    Tonic(#[from] tonic::transport::Error),

    #[cfg(feature = "grpc")]
    /// Tonic status error.
    #[error("status: {0}")]
    Status(#[from] tonic::Status),
}

pub type Result<T> = std::result::Result<T, TransportError>;

/// Address of a Raft node.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct NodeAddress {
    /// Node ID (unique within the cluster).
    pub id: u64,
    /// gRPC endpoint (e.g., "http://10.0.0.1:2026").
    pub endpoint: String,
}

impl NodeAddress {
    /// Create a new node address.
    pub fn new(id: u64, endpoint: impl Into<String>) -> Self {
        Self {
            id,
            endpoint: endpoint.into(),
        }
    }

    /// Parse from "id@host:port" format.
    #[expect(
        clippy::result_large_err,
        reason = "TransportError contains tonic types; will Box in transport refactor"
    )]
    pub fn parse(s: &str) -> Result<Self> {
        let parts: Vec<&str> = s.splitn(2, '@').collect();
        if parts.len() != 2 {
            return Err(TransportError::InvalidAddress(format!(
                "expected 'id@host:port', got '{}'",
                s
            )));
        }

        let id: u64 = parts[0].parse().map_err(|_| {
            TransportError::InvalidAddress(format!("invalid node id: '{}'", parts[0]))
        })?;

        let endpoint = if parts[1].starts_with("http") {
            parts[1].to_string()
        } else {
            format!("http://{}", parts[1])
        };

        Ok(Self { id, endpoint })
    }
}

impl std::fmt::Display for NodeAddress {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}@{}", self.id, self.endpoint)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_node_address_parse() {
        let addr = NodeAddress::parse("1@localhost:2026").unwrap();
        assert_eq!(addr.id, 1);
        assert_eq!(addr.endpoint, "http://localhost:2026");

        let addr = NodeAddress::parse("2@http://10.0.0.1:2026").unwrap();
        assert_eq!(addr.id, 2);
        assert_eq!(addr.endpoint, "http://10.0.0.1:2026");
    }

    #[test]
    fn test_node_address_parse_invalid() {
        assert!(NodeAddress::parse("localhost:2026").is_err());
        assert!(NodeAddress::parse("abc@localhost:2026").is_err());
    }

    #[test]
    fn test_node_address_display() {
        let addr = NodeAddress::new(1, "http://localhost:2026");
        assert_eq!(addr.to_string(), "1@http://localhost:2026");
    }
}
