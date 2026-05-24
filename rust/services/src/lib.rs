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
//!   tasks/           — durable task queue engine (fjall-backed)
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
// `AgentKind::UNMANAGED` agents (claude / codex / gemini / …).
#[cfg(feature = "service-acp")]
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
// Durable task queue engine (fjall-backed).
#[cfg(feature = "service-tasks")]
pub mod tasks;
// Matrix Client-Server v3 adapter — exposes nexus chat-with-me
// DT_STREAMs as Matrix rooms so stock chat clients (Element /
// FluffyChat / Cinny) participate in nexus conversations through the
// existing kernel surface. End-state spec lives in
// `sudowork-2/docs/tech/nexus-integration-architecture.md` §4.2; D1
// here lands skeleton + auth (`login` / `logout` / `whoami`).
#[cfg(feature = "service-matrix-adapter")]
pub mod matrix_adapter;
// PasswordVaultService — domain-wrapper gRPC service over the password
// vault (namespace="passwords"). Phase 1 Rust impl per #3923 integration
// doc. Hosted by the `vault` profile (`rust/profiles/vault/`), NOT by
// `cluster` — federation hygiene. Clients: password-agent (Python),
// sudowork-2 (TypeScript).
#[cfg(feature = "service-password-vault")]
pub mod password_vault;
