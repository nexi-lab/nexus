//! `transport` — front-door services tier.
//!
//! Hosts the VFS gRPC server (port 2028) + IPC envelope helpers —
//! the listening side of NexusFS's external network surface. Driver-
//! outgoing RPC clients (peer-blob fetch, federation peer client, VFS
//! gRPC client) live in the `rpc` driver-layer crate.
//!
//! Module layout:
//!
//! ```text
//! transport/
//!   grpc.rs        — Rust-native VFS gRPC server
//!   ipc.rs         — IPC message envelope helpers
//!   python/
//!     mod.rs       — register() + install_transport_wiring
//! ```
//!
//! Direction: `transport -> {kernel, shared/transport-primitives}`.
//! Driver-outgoing RPC clients (peer-blob, federation, VFS) live in
//! the `rpc` crate alongside other driver-layer impls; `nexus_raft::*`
//! references stay outside transport so the orthogonality invariant
//! `services ⊥ backends ⊥ transport ⊥ raft` reads directly off the
//! dep graph.

pub mod grpc;
pub mod ipc;

#[cfg(feature = "python")]
pub mod python;

// Re-export low-level primitive types under the transport crate's
// namespace so existing call sites keep working.
pub use transport_primitives::{
    compute_node_id, create_channel, hostname_to_node_id, ClientConfig, ConnectionPool,
    NodeAddress, PeerAddress, ServerConfig, TlsConfig, TransportError,
};
pub type Result<T> = transport_primitives::Result<T>;
