//! `transport` — Phase 4 parallel-layers crate.
//!
//! High-level transport tier — gRPC server, IPC envelope helpers,
//! federation peer client, peer blob fetcher.  Depends on
//! `transport-primitives` (low-level TLS / connection-pool primitives;
//! also consumed by raft) plus the kernel rlib.
//!
//! Module layout:
//!
//! ```text
//! transport/
//!   grpc.rs         — Rust-native VFS gRPC server (was kernel::grpc_server)
//!   ipc.rs          — IPC message envelope helpers (was kernel::ipc)
//!   federation.rs   — PyFederationClient + peer JoinZone client
//!                     (was kernel::federation_client)
//!   blob/
//!     peer_client.rs — `PeerBlobClient` (was kernel::peer_blob_client),
//!                      impls `kernel::hal::peer::PeerBlobClient` trait
//!     fetcher.rs     — `KernelBlobFetcher` (was kernel::blob_fetcher)
//!   python/
//!     mod.rs         — register() — installs PyVfsGrpcServerHandle +
//!                      start_vfs_grpc_server + PyFederationClient on
//!                      the cdylib's nexus_runtime module.
//! ```
//!
//! Direction: `transport -> {kernel, transport-primitives, raft}`.
//! Kernel never depends on transport — peer blob fetch goes through
//! `kernel::hal::peer::PeerBlobClient` (trait); the concrete impl
//! lives here in `blob::peer_client`.
//!
//! Cycle break (vs the original Phase 4 attempt that put these files
//! in the same crate as `transport-primitives`):
//!
//!   - `raft → transport-primitives` (low-level only, no kernel dep)
//!   - `transport → kernel`           (Phase 4 wants this for sys_*)
//!   - `kernel → raft`                (existing, ZoneManager / federation)
//!   - `transport → transport-primitives` (composition, no cycle)
//!
//! No cycle closes because raft doesn't reach the high-level
//! `transport` crate at all.

pub mod blob;
pub mod federation;
pub mod grpc;
pub mod ipc;

#[cfg(feature = "python")]
pub mod python;

// Re-export low-level primitive types under the high-level transport
// crate's namespace.  Existing call sites that pre-Phase-4 used
// `crate::TlsConfig` / `crate::create_channel` keep working
// once they're inside this crate.
pub use transport_primitives::{
    create_channel, hostname_to_node_id, ClientConfig, ConnectionPool, NodeAddress, PeerAddress,
    ServerConfig, TlsConfig, TransportError,
};
pub type Result<T> = transport_primitives::Result<T>;
