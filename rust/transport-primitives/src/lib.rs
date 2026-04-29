//! Shared gRPC transport primitives for the Nexus workspace.
//!
//! Provides TLS configuration, peer addressing, connection pooling, and
//! channel creation utilities. Consumed by `nexus_runtime` (ObjectStore gRPC
//! adapter) and `nexus_raft` (Raft transport).
//!
//! This crate unifies tonic across the workspace (0.13) and eliminates
//! duplicated transport boilerplate.
//!
//! ## Phase 4 note
//!
//! Per `KERNEL-ARCHITECTURE.md`, the kernel-bound high-level transport
//! modules (gRPC server, IPC envelope, federation client) **were
//! intended to live here** as `transport::{grpc, ipc, federation}`.
//! They didn't move out of kernel in this Phase 4 pass because the
//! pre-existing `raft -> transport` edge plus the planned
//! `transport -> kernel` edge plus the existing `kernel -> raft` edge
//! would close a Cargo dependency cycle.  Resolving that cleanly
//! requires splitting this crate into `transport-primitives` (raft
//! consumes) + `transport` (kernel-bound) — a follow-up surgery
//! whose churn would dwarf the rest of Phase 4.
//!
//! Today the high-level modules live at `kernel::transport::*` (Linux
//! precedent — networking lives in the kernel).  When the
//! transport-primitives split lands, those files migrate here without
//! source changes (only the crate name in their `use` paths).

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
