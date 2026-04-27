//! `services` — kernel-adjacent service-tier impls (Phase 3 parallel-layers crate).
//!
//! Per `docs/architecture/KERNEL-ARCHITECTURE.md` §1, services sit
//! parallel to the kernel: they consume kernel primitives (syscalls,
//! `NativeInterceptHook`, `PathResolver`, `ServiceRegistry`) without
//! adding new kernel surface.  The line between "kernel primitive"
//! (lives in `kernel/src/core/`) and "service" (lives here) is whether
//! the code is part of the syscall path itself (kernel) or layered on
//! top of it (service).
//!
//! Module layout:
//!
//! ```text
//! services/
//!   agents/       — agent table + procfs-style status resolver
//!   audit/        — AuditHook (NativeInterceptHook) + factory
//!   permission/   — PermissionHook scaffolding (§11 Phase 11; dead today)
//!   python/       — `#[cfg(feature = "python")]` PyO3 sub-module
//! ```
//!
//! ## Hard invariant: `services` ⊥ `backends`
//!
//! `services` MUST NOT depend on `backends` — the two are co-equal
//! peers under `kernel`, and any service that needs backend behaviour
//! must reach it through `kernel.sys_*` syscalls (the same path
//! Python takes).  Cargo enforces this at the workspace level:
//! [`services/Cargo.toml`] does NOT list `backends` as a dependency.
//! A future CI lint can grep for `use backends` inside this crate to
//! catch accidental violations.
//!
//! Direction summary:
//!
//! ```text
//!   contracts <- lib <- kernel <- services    (one-way; no cycle)
//!                          ^
//!                          +--- backends     (peer; never crosses to services)
//! ```

pub mod agents;
pub mod audit;
// `permission` is gated behind the `python` feature because its only
// caller path is `Python::attach(...)` → `PermissionChecker.check(...)`
// (the slow path).  Pure-Rust builds (e.g. WASM, raft-witness) drop it.
// §11 Phase 11 will wire up the kernel registration; today this is
// scaffolding only.
#[cfg(feature = "python")]
pub mod permission;

#[cfg(feature = "python")]
pub mod python;
