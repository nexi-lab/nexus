//! `services` — kernel-adjacent service-tier impls (parallel-layers crate).
//!
//! Per `KERNEL-ARCHITECTURE.md (nexus-vfs)` §1, services sit
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

// The generic hosted-subprocess primitive (`HostedSubprocess`) moved to the
// nexus-vfs `subprocess` crate (kernel-tier) alongside the relocated
// managed-agent control plane; `acp` reaches it via `subprocess::` directly.
// AcpService — subprocess + ACP-over-stdio host for
// `AgentKind::UNMANAGED` agents (claude / codex / gemini / …).
#[cfg(feature = "service-acp")]
pub mod acp;
#[cfg(feature = "service-agents")]
pub mod agents;
// audit + audit_node modules retired 2026-09-26.  The Rust ports were
// staged behind `service-audit` / `service-audit-node` features and never
// picked up a production consumer (nexus-vfs's cluster binary — the sole
// production build after nexus-vfs#319/#321 — enables neither).  Two of
// their integration tests started failing on the current nexus-vfs pin
// (raft/topology semantics drift); the modules were the last thing making
// `cargo check --all-features` compile before the R10 β / δ arc, but a
// dead cdylib feature with broken tests and no downstream consumer earns
// no maintenance tax.  Python `AuditNode` (src/nexus/services/audit_node/)
// keeps operating in the Python nexus-server image; a fresh Rust port
// can land whenever a production consumer needs it.
// ManagedAgentService (the MANAGED-agent control plane) moved to the nexus-vfs
// `managed-agent` crate so the production nexusd-cluster carries it directly;
// the assembly reaches it via `managed_agent::` from nexus-vfs.
// Durable task queue engine (fjall-backed).
#[cfg(feature = "service-tasks")]
pub mod tasks;
// PasswordVaultService — domain-wrapper gRPC service over the password
// vault (namespace="passwords"). Phase 1 Rust impl per #3923 integration
// doc. Hosted by the `vault` service-plugin dylib (`rust/services/vault/`),
// NOT by `cluster` — federation hygiene. Clients: password-agent
// (Python), sudowork-2 (TypeScript).
#[cfg(feature = "service-password-vault")]
pub mod password_vault;
// GenericSecretsService — namespace:key encrypted KV store with versioning,
// soft-delete, and batch operations. Shares AES-256-GCM crypto and kernel
// mount with PasswordVaultService. Loaded as part of the vault cdylib plugin.
#[cfg(feature = "service-generic-secrets")]
pub mod generic_secrets;
