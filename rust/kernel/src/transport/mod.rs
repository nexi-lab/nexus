//! Kernel transport layer — gRPC server, IPC envelope, federation client.
//!
//! Linux precedent: networking lives in the kernel (`kernel/net/`,
//! `net/ipv4/`, etc.).  Per `KERNEL-ARCHITECTURE.md`, these files were
//! intended to move to the parallel `rust/transport/` crate as
//! `transport::{grpc, ipc, federation}` — that crate's docstring
//! describes why the move was deferred (`raft -> transport ->
//! kernel -> raft` Cargo cycle).
//!
//! Until the transport-primitives split lands, the high-level
//! transport modules sit here at `kernel::transport::*`.  Their source
//! is unchanged from what they would be in the transport crate; only
//! their crate path differs.
//!
//! Module layout:
//!
//! ```text
//! kernel/src/transport/
//!   grpc.rs        — Rust-native VFS gRPC server (was kernel::grpc_server)
//!   ipc.rs         — IPC message envelope helpers (was kernel::ipc)
//!   federation.rs  — PyFederationClient + peer JoinZone client
//!                    (was kernel::federation_client)
//! ```

pub mod federation;
pub mod grpc;
pub mod ipc;
