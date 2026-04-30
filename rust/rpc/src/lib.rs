//! Driver-outgoing RPC clients for Nexus.
//!
//! Driver-layer crate alongside `backends/` and `raft/` — implements
//! the kernel-defined HAL surfaces that consume RPC clients. Three
//! client modules:
//!
//! * [`vfs`] — gRPC client used by `backends::storage::remote::RemoteBackend`
//!   to reach a remote `nexusd` over the VFS gRPC service. Re-exports
//!   `kernel::rpc_transport::RpcTransport` so the canonical path is
//!   `rpc::vfs::RpcTransport`.
//! * [`peer_blob`] — peer-blob fetch client implementing
//!   `transport_primitives::PeerBlobClient` (CAS chunk fetch + cross-zone
//!   blob reads against the raft `ReadBlob` server-side handler).
//! * [`federation`] — federation peer client (`PyFederationClient`) used
//!   by the Python federation_rpc shim for discover / join flows.
//!
//! Cargo edges: `rpc -> kernel`, `rpc -> raft`, `rpc -> transport-primitives`.
//! The `rpc -> raft` edge is the only directed edge between driver-layer
//! crates and is what lets `rpc` name raft's wire-format proto stubs
//! (JoinZoneRequest etc.) for the federation client.

pub mod federation;
pub mod peer_blob;
pub mod vfs;

#[cfg(feature = "python")]
pub mod python;
