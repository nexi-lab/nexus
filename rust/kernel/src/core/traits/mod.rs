//! Kernel HAL trait declarations.
//!
//! Each storage / IPC pillar declares its kernel-side abstract interface
//! here. Concrete impls live in their respective parallel crates
//! (`backends/`, future `streams/`, future `pipes/`) — Phase D moves
//! `_backend_impls.rs` into `backends/`; the `MemoryStreamBackend` /
//! `MemoryPipeBackend` impls stay kernel-internal as the in-memory
//! reference implementations.
//!
//! Convention: trait declaration + its associated error / result types
//! live together in a per-pillar file so dependent crates can pull a
//! single `use` line.

pub mod object_store;
pub mod pipe_backend;
pub mod stream_backend;

// Flat re-export of the ObjectStore pillar so callers (and Phase D's
// `backends/` crate) can write `use crate::core::traits::ObjectStore`
// without naming the per-pillar module. StreamBackend / PipeBackend
// are `pub(crate)` and only consumed via the `crate::stream` /
// `crate::pipe` re-export points; no flat re-export needed here yet.
pub use object_store::{ExternalTransport, ObjectStore, StorageError, WriteResult};
