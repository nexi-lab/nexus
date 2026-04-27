//! Kernel `core/` — kernel primitives only (§4 of
//! `docs/architecture/KERNEL-ARCHITECTURE.md`).
//!
//! Phase 1 enforced a strict 3-way split inside `kernel/src/`:
//!
//! * `crate::abc::*` — §3 ABC pillars (`ObjectStore`, `MetaStore`,
//!   `CacheStore`).  Three trait files, period.
//! * `crate::hal::*` — kernel-defined extension interfaces that aren't
//!   §3 pillars (`LlmStreamingBackend`, `PeerBlobClient`).
//! * `crate::core::*` — §4 kernel primitives (this module).  No traits,
//!   no extension interfaces — only the runtime mechanisms the syscall
//!   layer needs (vfs_router, dlc, dcache, locks, dispatch, plus the
//!   in-memory reference impls of the §3 pillars that are too small to
//!   justify their own crate).
//!
//! The `lib.rs` crate root still exposes the pre-Phase-C flat names
//! (`crate::vfs_router::*`, `crate::pipe::*`, `crate::stream::*`, …)
//! via `pub use core::… as <flat>` shims, so callers do not churn;
//! later phases retire the shims as impls migrate to parallel crates.

// §4.0 — agent table SSOT (Phase 3 moved here from services/).
pub mod agents;

// §4.1 — VFS routing + dcache + DLC mount lifecycle.
pub mod dcache;
pub mod dlc;
pub mod vfs_router;

// §4.3 — kernel runtime services (registry, watch).
pub mod file_watch;
pub mod service_registry;

// §4.4 — locking primitives (I/O lock + advisory lock).
pub mod lock;

// §4.5 — dispatch + hook / observer registry.
pub mod dispatch;

// §4.6 — metastore primitive impls (MemoryMetaStore + LocalMetaStore +
// remote proxy).  The trait declaration itself lives in
// `crate::abc::meta_store` after Phase 1; this module only holds the
// kernel-internal concrete impls.
pub mod meta_store;

// §4.2 — DT_PIPE / DT_STREAM IPC pillars.
pub mod pipe;
pub mod stream;
