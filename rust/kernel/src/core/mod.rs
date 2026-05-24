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

// Agent table SSOT (§1 Service Lifecycle).
pub mod agents;

// VFS routing + DLC mount lifecycle.
pub mod dlc;
pub mod vfs_router;

// File-watch waiters + kernel-owned service registry.
pub mod file_watch;
pub mod service_registry;

// Unified locking — I/O lock + advisory lock (§4.1).
pub mod lock;

// VFS dispatch + hook/observer registry (§2.4).
pub mod dispatch;

// Permission lease cache, DashMap-based, pure Rust (§2.4.1 + §4 PermissionGate).
pub mod permission_cache;

// MetaStore primitive impls — LocalMetaStore + remote proxy.
// The trait declaration lives in `crate::abc::meta_store` (§3.A.1);
// this module only holds the kernel-internal concrete impls.
pub mod meta_store;

// DT_PIPE / DT_STREAM IPC pillars (§4.2).
pub mod pipe;
pub mod stream;

// Shared mmap header accessors used by both pipe/shm and stream/shm.
#[cfg(unix)]
pub(crate) mod shm_header;

// File-descriptor table — pre-opened fds for PAS fast-path reads
// (§4 PermissionGate row's FileDescriptorTable peer).
pub mod fdt;
