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
pub(crate) mod certgen;
#[cfg(all(feature = "grpc", has_protos))]
mod client;
#[cfg(all(feature = "grpc", has_protos))]
mod server;
#[cfg(all(feature = "grpc", has_protos))]
mod transport_loop;

#[cfg(all(feature = "grpc", has_protos))]
pub use client::{
    call_join_cluster, ClientConfig, ClusterInfoResult, JoinClusterResult, ProposeResult,
    QueryResult, RaftApiClient, RaftClient, RaftClientPool,
};
#[cfg(all(feature = "grpc", has_protos))]
pub use server::{RaftGrpcServer, RaftWitnessServer, ServerConfig, WitnessZoneRegistry};
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
    //!   - nexus::raft - ZoneTransportService, ZoneApiService, commands, transport messages

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
#[allow(clippy::result_large_err)]
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

/// Derive a deterministic node ID from a hostname.
///
/// SHA-256 of hostname, first 8 bytes as little-endian u64.
/// Maps 0 to 1 (raft-rs reserves 0 as "no node").
#[cfg(feature = "grpc")]
pub fn hostname_to_node_id(hostname: &str) -> u64 {
    use sha2::{Digest, Sha256};
    let hash = Sha256::digest(hostname.as_bytes());
    let value = u64::from_le_bytes(hash[..8].try_into().unwrap());
    if value == 0 {
        1
    } else {
        value
    }
}

/// Address of a Raft node.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct PeerAddress {
    /// Peer hostname (e.g., "nexus-1").
    pub hostname: String,
    /// Peer port (e.g., 2126).
    pub port: u16,
    /// Node ID (derived from hostname via SHA-256).
    pub id: u64,
    /// gRPC endpoint (e.g., "http://nexus-1:2126").
    pub endpoint: String,
}

impl PeerAddress {
    /// Create a new PeerAddress with explicit id and endpoint (backward compat).
    pub fn new(id: u64, endpoint: impl Into<String>) -> Self {
        let endpoint = endpoint.into();
        Self {
            hostname: String::new(),
            port: 0,
            id,
            endpoint,
        }
    }

    /// Parse from "host:port" format, deriving node_id from hostname.
    #[cfg(feature = "grpc")]
    #[allow(clippy::result_large_err)]
    pub fn parse(s: &str, use_tls: bool) -> Result<Self> {
        let s = s.trim();
        // Strip scheme prefix if present
        let addr = s
            .strip_prefix("http://")
            .or_else(|| s.strip_prefix("https://"))
            .unwrap_or(s);

        let parts: Vec<&str> = addr.rsplitn(2, ':').collect();
        if parts.len() != 2 {
            return Err(TransportError::InvalidAddress(format!(
                "expected 'host:port', got '{}'",
                s
            )));
        }

        let port: u16 = parts[0]
            .parse()
            .map_err(|_| TransportError::InvalidAddress(format!("invalid port: '{}'", parts[0])))?;
        let hostname = parts[1].to_string();
        let id = hostname_to_node_id(&hostname);

        let scheme = if use_tls { "https" } else { "http" };
        let endpoint = format!("{}://{}:{}", scheme, hostname, port);

        Ok(Self {
            hostname,
            port,
            id,
            endpoint,
        })
    }

    /// Parse a comma-separated list of "host:port" peers.
    #[cfg(feature = "grpc")]
    #[allow(clippy::result_large_err)]
    pub fn parse_peer_list(s: &str, use_tls: bool) -> Result<Vec<Self>> {
        s.split(',')
            .filter(|p| !p.trim().is_empty())
            .map(|p| Self::parse(p.trim(), use_tls))
            .collect()
    }

    /// Return "host:port" for gRPC connection target.
    pub fn grpc_target(&self) -> String {
        if self.hostname.is_empty() {
            // Backward compat: strip scheme from endpoint
            self.endpoint
                .trim_start_matches("http://")
                .trim_start_matches("https://")
                .to_string()
        } else {
            format!("{}:{}", self.hostname, self.port)
        }
    }

    /// Return "id@host:port" for Raft peer configuration.
    pub fn to_raft_peer_str(&self) -> String {
        format!("{}@{}", self.id, self.grpc_target())
    }
}

/// Backward-compatible type alias.
pub type NodeAddress = PeerAddress;

impl std::fmt::Display for PeerAddress {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}@{}", self.id, self.endpoint)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[cfg(feature = "grpc")]
    #[test]
    fn test_hostname_to_node_id_golden_values() {
        // These must match the Python implementation
        assert_eq!(hostname_to_node_id("nexus-1"), 14044926161142285152);
        assert_eq!(hostname_to_node_id("nexus-2"), 768242927742468745);
        assert_eq!(hostname_to_node_id("witness"), 10099512703796518074);
    }

    #[cfg(feature = "grpc")]
    #[test]
    fn test_peer_address_parse() {
        let addr = PeerAddress::parse("nexus-1:2126", false).unwrap();
        assert_eq!(addr.hostname, "nexus-1");
        assert_eq!(addr.port, 2126);
        assert_eq!(addr.id, hostname_to_node_id("nexus-1"));
        assert_eq!(addr.endpoint, "http://nexus-1:2126");
    }

    #[cfg(feature = "grpc")]
    #[test]
    fn test_peer_address_parse_tls() {
        let addr = PeerAddress::parse("nexus-2:2126", true).unwrap();
        assert_eq!(addr.endpoint, "https://nexus-2:2126");
    }

    #[cfg(feature = "grpc")]
    #[test]
    fn test_peer_address_parse_peer_list() {
        let peers =
            PeerAddress::parse_peer_list("nexus-1:2126,nexus-2:2126,witness:2126", false).unwrap();
        assert_eq!(peers.len(), 3);
        assert_eq!(peers[0].hostname, "nexus-1");
        assert_eq!(peers[2].hostname, "witness");
    }

    #[test]
    fn test_peer_address_grpc_target() {
        let addr = PeerAddress {
            hostname: "nexus-1".to_string(),
            port: 2126,
            id: 1,
            endpoint: "http://nexus-1:2126".to_string(),
        };
        assert_eq!(addr.grpc_target(), "nexus-1:2126");
    }

    #[test]
    fn test_peer_address_to_raft_peer_str() {
        let addr = PeerAddress {
            hostname: "nexus-1".to_string(),
            port: 2126,
            id: 42,
            endpoint: "http://nexus-1:2126".to_string(),
        };
        assert_eq!(addr.to_raft_peer_str(), "42@nexus-1:2126");
    }

    #[test]
    fn test_node_address_new_backward_compat() {
        let addr = NodeAddress::new(1, "http://localhost:2026");
        assert_eq!(addr.id, 1);
        assert_eq!(addr.endpoint, "http://localhost:2026");
    }

    #[test]
    fn test_node_address_display() {
        let addr = NodeAddress::new(1, "http://localhost:2026");
        assert_eq!(addr.to_string(), "1@http://localhost:2026");
    }
}
