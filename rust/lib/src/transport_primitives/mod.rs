//! Transport primitives — TLS / pool / addressing / TOFU trust store /
//! `PeerBlobClient` trait. Lowest shared utilities in the workspace
//! dep graph: pure-Rust, zero peer-crate deps. Consumed by every
//! peer crate that speaks raft or VFS gRPC (raft itself, transport,
//! and the kernel rlib through the peer-client slot).
//!
//! Lives under `lib` per §6 — `lib` is the tier-neutral implementation
//! crate, mirror of `src/nexus/lib/`. The TOFU pyclass surface
//! (`PyTofuTrustStore`, `PyTrustedZone`) sits behind the lib-wide
//! `python` feature alongside the algorithm pyclasses.

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
