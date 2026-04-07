//! Shared gRPC transport primitives for the Nexus workspace.
//!
//! Provides TLS configuration, peer addressing, connection pooling, and
//! channel creation utilities. Consumed by `nexus_kernel` (ObjectStore gRPC
//! adapter) and `nexus_raft` (Raft transport).
//!
//! This crate unifies tonic across the workspace (0.13) and eliminates
//! duplicated transport boilerplate.

mod channel;
mod config;
mod error;
mod peer;
mod pool;

pub use channel::create_channel;
pub use config::{ClientConfig, ServerConfig, TlsConfig};
pub use error::{Result, TransportError};
pub use peer::{hostname_to_node_id, NodeAddress, PeerAddress};
pub use pool::ConnectionPool;
