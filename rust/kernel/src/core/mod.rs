//! Kernel `core/` — primitives + HAL trait declarations.
//!
//! Per `docs/architecture/KERNEL-ARCHITECTURE.md` §3 / §4, the kernel
//! crate hosts both kernel primitives (vfs_router, dlc, dcache,
//! service_registry, file_watch, lock manager, dispatch) and the HAL
//! trait declarations that the parallel `backends/`, `services/`,
//! `transport/`, `raft/` crates implement.
//!
//! Phase C nested every kernel-internal pillar under this module so
//! the kernel-only file set is unambiguous. The `lib.rs` crate root
//! still exposes the pre-Phase-C flat names (`crate::vfs_router::*`,
//! `crate::pipe::*`, `crate::stream::*`, …) via `pub use core::… as
//! <flat>` shims, so callers do not churn during Phase C–G; later
//! phases retire the shims as impls migrate to parallel crates.

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

// §4.6 — metastore pillar (trait + memory + remote proxy).
pub mod metastore;

// §4.2 — DT_PIPE / DT_STREAM IPC pillars.
pub mod pipe;
pub mod stream;

// §3 — HAL trait declarations consumed by parallel crates.
pub mod traits;
