//! `transport` — network surface tier.
//!
//! Hosts NexusFS's external network surface, both directions:
//!
//! * In-bound (server side): VFS gRPC server on port 2028, IPC
//!   envelope helpers.
//! * Out-bound (driver-side clients): peer-blob fetch client
//!   implementing `lib::transport_primitives::PeerBlobClient`,
//!   federation peer client (`PyFederationClient`) for discover/join
//!   flows.
//!
//! Module layout:
//!
//! ```text
//! transport/
//!   grpc.rs         — Rust-native VFS gRPC server (in-bound)
//!   ipc.rs          — IPC message envelope helpers
//!   peer_blob.rs    — peer-blob fetch client (out-bound)
//!   federation.rs   — federation peer client (out-bound)
//!   python/
//!     mod.rs        — register() + install_transport_wiring
//! ```
//!
//! Direction: `transport -> {kernel, lib, raft}`. Transport names
//! raft's wire-format proto stubs directly through the federation
//! client (same shape as a Postgres client crate referencing libpq);
//! raft does not import transport, so no cycle. The VFS gRPC client
//! (`RpcTransport`) lives in the kernel crate where the kernel-internal
//! `RemoteMetaStore` / `RemotePipeBackend` / `RemoteStreamBackend`
//! wrappers wrap it directly — re-exported here under
//! [`vfs::RpcTransport`] for the canonical out-bound name.

pub mod federation;
pub mod grpc;
pub mod ipc;
pub mod peer_blob;

#[cfg(feature = "python")]
pub mod python;

/// Out-bound VFS gRPC client. Re-exported from `kernel::rpc_transport`
/// where the type is declared (kernel-internal `RemoteMetaStore`
/// wrappers wrap the same struct).
pub mod vfs {
    pub use kernel::rpc_transport::{RpcTransport, TlsConfig as VfsTlsConfig};
}

// Re-export low-level primitive types under the transport crate's
// namespace so existing call sites keep working.
pub use lib::transport_primitives::{
    compute_node_id, create_channel, hostname_to_node_id, ClientConfig, ConnectionPool,
    NodeAddress, PeerAddress, ServerConfig, TlsConfig, TransportError,
};
pub type Result<T> = lib::transport_primitives::Result<T>;
