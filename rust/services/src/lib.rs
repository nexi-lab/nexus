//! `services` — kernel-adjacent service-tier impls (parallel-layers crate).
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
//!   acp/             — Rust-port of nexus.services.acp (subprocess +
//!                      ACP-over-stdio for AgentKind::UNMANAGED agents)
//!   agents/          — agent table + procfs-style status resolver
//!   audit/           — AuditHook (NativeInterceptHook) + factory
//!   managed_agent/   — ManagedAgentService (mailbox + workspace hooks
//!                      plus session lifecycle for AgentKind::MANAGED)
//!   permission/      — PermissionHook scaffolding (§11; dead today)
//!   python/          — `#[cfg(feature = "python")]` PyO3 sub-module
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

// AcpService — subprocess + ACP-over-stdio host for
// `AgentKind::UNMANAGED` agents (claude / codex / …).  Currently
// pyo3-laden internally, so the per-service gate also requires the
// `python` feature; once the pyo3-coupling is unwound it becomes
// `service-acp`-only.
#[cfg(all(feature = "service-acp", feature = "python"))]
pub mod acp;
#[cfg(feature = "service-agents")]
pub mod agents;
#[cfg(feature = "service-audit")]
pub mod audit;
// ManagedAgentService — first Rust-flavoured service. Owns the
// chat-with-me mailbox stamping hook, the workspace-boundary
// teaching hook, and the `start_session_v1` / `cancel_v1` /
// `get_session_v1` lifecycle for `AgentKind::MANAGED` agents.
#[cfg(feature = "service-managed-agent")]
pub mod managed_agent;
// `tasks` lives in this crate so the runtime ships a single Python
// wheel; `services::python::register` exposes the PyTaskEngine /
// PyTaskRecord / PyQueueStats pyclasses.  Internal pyo3 use today, so
// the per-service gate also requires `python`.
#[cfg(all(feature = "service-tasks", feature = "python"))]
pub mod tasks;
// `permission` is gated behind the `python` feature because its only
// caller path is `Python::attach(...)` → `PermissionChecker.check(...)`
// (the slow path).  Pure-Rust builds (e.g. WASM, raft-witness) drop it.
// Kernel registration of §11 PermissionHook is scaffolded here only.
#[cfg(all(feature = "service-permission", feature = "python"))]
pub mod permission;
// Matrix Client-Server v3 adapter — exposes nexus chat-with-me
// DT_STREAMs as Matrix rooms so stock chat clients (Element /
// FluffyChat / Cinny) participate in nexus conversations through the
// existing kernel surface. End-state spec lives in
// `sudowork-2/docs/tech/nexus-integration-architecture.md` §4.2; D1
// here lands skeleton + auth (`login` / `logout` / `whoami`).
#[cfg(feature = "service-matrix-adapter")]
pub mod matrix_adapter;

#[cfg(feature = "python")]
pub mod python;
