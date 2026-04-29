//! Shared transport primitives for the Nexus workspace — pure
//! utilities consumed by both raft (server-side) and rpc (client-side)
//! drivers, plus the kernel-bound front-door transport crate.
//!
//! Provides TLS configuration, peer addressing, connection pooling,
//! channel creation, the `PeerBlobClient` trait, and the TOFU
//! (Trust On First Use) trust store. The crate sits at the lowest
//! shared layer in the workspace dep graph: zero peer-crate deps,
//! every higher-layer crate (raft, rpc, transport, kernel) depends
//! on this one.

mod channel;
mod config;
mod error;
mod peer;
mod peer_blob_client;
mod pool;
mod tofu;

pub use channel::create_channel;
pub use config::{ClientConfig, ServerConfig, TlsConfig};
pub use error::{Result, TransportError};
pub use peer::{compute_node_id, hostname_to_node_id, NodeAddress, PeerAddress};
pub use peer_blob_client::{NoopPeerBlobClient, PeerBlobClient, PeerBlobResult};
pub use pool::ConnectionPool;
pub use tofu::{TofuError, TofuResult, TofuTrustStore, TrustedZone};

#[cfg(feature = "python")]
pub use tofu::{PyTofuTrustStore, PyTrustedZone};
