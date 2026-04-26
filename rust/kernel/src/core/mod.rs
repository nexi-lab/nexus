//! Kernel `core/` — primitives + HAL trait declarations.
//!
//! Per `docs/architecture/KERNEL-ARCHITECTURE.md` §3 / §4, the kernel
//! crate hosts both kernel primitives (vfs_router, dlc, dcache,
//! service_registry, file_watch, lock manager, dispatch) and the HAL
//! trait declarations that the parallel `backends/`, `services/`,
//! `transport/`, `raft/` crates implement.
//!
//! This module groups those primitives + traits. Phase B (initial
//! lift) introduces `core::traits` for the three trait declarations
//! that previously sat inline in `backend.rs`, `stream.rs`, `pipe.rs`.
//! Phase C will move the rest of the §4 primitives into `core::*`
//! sub-modules so the kernel-only file set is unambiguous.

pub mod traits;
