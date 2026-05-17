//! Kernel `core/` — kernel primitives only (§4 of
//! `docs/architecture/KERNEL-ARCHITECTURE.md`).
//!
//! Strict split inside `kernel/src/`:
//!
//! * `crate::abc::*` — §3.A Storage HAL pillars (`ObjectStore`,
//!   `MetaStore`, `CacheStore`).
//! * `crate::hal::*` — §3.B Control-Plane HAL DI surfaces
//!   (`DistributedCoordinator`, `ObjectStoreProvider`,
//!   `PeerBlobClient`).
//! * `crate::core::*` — §4 kernel primitives (this module). No traits,
//!   no extension interfaces — only the runtime mechanisms the syscall
//!   layer needs (vfs_router, dlc, locks, dispatch, plus the in-memory
//!   reference impls of the §3.A pillars that are too small to justify
//!   their own crate).
//!
//! The `lib.rs` crate root re-exposes the flat names
//! (`crate::vfs_router::*`, `crate::pipe::*`, `crate::stream::*`, …)
//! via `pub use core::… as <flat>` shims, so callers can name a single
//! canonical path regardless of the internal `core/` nesting.

// §4.0 — agent table SSOT.
pub mod agents;

// §4.1 — VFS routing + DLC mount lifecycle.
pub mod dlc;
pub mod vfs_router;

// §4.3 — kernel runtime services (registry, watch).
pub mod file_watch;
pub mod service_registry;

// §4.4 — locking primitives (I/O lock + advisory lock).
pub mod lock;

// §4.5 — dispatch + hook / observer registry.
pub mod dispatch;

// §4.7 — permission lease cache (DashMap-based, pure Rust).
pub mod permission_cache;

// §4.6 — metastore primitive impls (LocalMetaStore + remote proxy).
// The trait declaration lives in `crate::abc::meta_store`; this module
// only holds the kernel-internal concrete impls.
pub mod meta_store;

// §4.2 — DT_PIPE / DT_STREAM IPC pillars.
pub mod pipe;
pub mod stream;

// §4.8 — FileDescriptorTable (pre-opened fds for PAS fast-path reads).
pub mod fdt;
