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
//! # Architecture
//!
//! All raft-rs message types (~15 types including votes, heartbeats, appends)
//! are multiplexed through a single `StepMessage` RPC as opaque protobuf v2
//! bytes (etcd/tikv pattern). EC replication uses a separate `ReplicateEntries`
//! RPC for async peer sync.
//!
//! # Example
//!
//! ```rust,ignore
//! use nexus_raft::transport::{RaftClient, ClientConfig};
//!
//! // Create a client to talk to another node
//! let mut client = RaftClient::connect("http://10.0.0.2:2026", ClientConfig::default()).await?;
//!
//! // Send a raw raft-rs message via step_message
//! client.step_message(message_bytes, "my-zone".to_string()).await?;
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
    ClientConfig, ClusterInfoResult, ProposeResult, QueryResult, RaftApiClient, RaftClient,
    RaftClientPool,
};
#[cfg(all(feature = "grpc", has_protos))]
pub use server::{RaftGrpcServer, RaftWitnessServer, ServerConfig, WitnessServerState};
#[cfg(all(feature = "grpc", has_protos))]
pub use transport_loop::TransportLoop;

/// TLS configuration for gRPC transport (mTLS).
///
/// All fields are PEM-encoded bytes (read from files by the caller).
/// Rust core holds bytes, not paths — file I/O happens at the boundary
/// (PyO3 reads files, CLI reads files). This makes the core testable
/// with in-memory certs.
#[cfg(feature = "grpc")]
#[derive(Debug, Clone)]
pub struct TlsConfig {
    /// Server/client certificate (PEM).
    pub cert_pem: Vec<u8>,
    /// Private key (PEM).
    pub key_pem: Vec<u8>,
    /// CA certificate for verifying the peer (PEM).
    pub ca_pem: Vec<u8>,
}

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

/// Shared peer map that can be updated at runtime (e.g., when new nodes join via ConfChange).
///
/// Uses `std::sync::RwLock` (not tokio) because:
/// - Read/write operations are very fast (HashMap insert/lookup)
/// - Accessed from both sync (DashMap guard) and async (transport loop) contexts
/// - Write-rarely, read-often pattern — no contention in practice
pub type SharedPeerMap =
    std::sync::Arc<std::sync::RwLock<std::collections::HashMap<u64, NodeAddress>>>;

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
        Self::parse_with_tls(s, false)
    }

    /// Parse from "id@host:port" format, using `https://` scheme when TLS is active.
    #[expect(
        clippy::result_large_err,
        reason = "TransportError contains tonic types; will Box in transport refactor"
    )]
    pub fn parse_with_tls(s: &str, use_tls: bool) -> Result<Self> {
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
            let scheme = if use_tls { "https" } else { "http" };
            format!("{}://{}", scheme, parts[1])
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
