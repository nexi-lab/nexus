//! Kernel ABC pillars — the canonical "Linux `struct file_operations`"
//! analogues from `docs/architecture/KERNEL-ARCHITECTURE.md` §3.
//!
//! Strict 3-way split: this directory holds **only** the §3.A pillar
//! trait declarations that any compliant driver impl must satisfy.
//! §3.B Control-Plane HAL DI surfaces (`DistributedCoordinator`,
//! `ObjectStoreProvider`) live in `crate::hal::*`. ObjectStore
//! extension hooks (`LlmStreamingBackend`) live at the crate root
//! (`crate::llm_streaming`). Peer-blob fetch
//! (`crate::hal::peer::PeerBlobClient`) is a transport-layer
//! abstraction reached through the kernel's peer_client slot. Kernel
//! primitives (vfs_router, dlc, dcache, locks, dispatch, …) live in
//! `crate::core::*` and never declare traits.
//!
//! The three pillars — `ObjectStore`, `MetaStore`, `CacheStore` — are
//! co-equal: each is its own trait file and travels with its associated
//! error / result types so dependent crates can import a pillar with a
//! single `use` line. Concrete impls live in their respective parallel
//! crate (`backends/` for object stores, kernel-internal for the
//! in-memory reference metastore, …).
//!
//! Phase 1 lifted these out of `core/traits/`. The relocation
//! disambiguates the doc invariant — anything inside `abc/` is one of
//! the three §3 storage pillars; nothing else qualifies.

pub mod cache_store;
pub mod meta_store;
pub mod object_store;
