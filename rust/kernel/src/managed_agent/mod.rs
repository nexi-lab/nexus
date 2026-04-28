//! `ManagedAgentService` — Rust-flavoured service that owns the
//! managed-agent surface (mailbox stamping, workspace boundary, and —
//! in a follow-up commit — session lifecycle behind the
//! `proto/nexus/grpc/managed_agent` gRPC contract).
//!
//! Registered to the kernel `ServiceRegistry` as a Rust service via the
//! `Kernel::register_rust_service` surface (parallel of `add_mount` for
//! drivers). Pre-existing services (AcpService for unmanaged agents,
//! AgentRegistry, ReBAC, …) keep their Python implementations; this is
//! the first Rust-flavoured service to land alongside them.
//!
//! Today's responsibilities, all generic to `AgentKind::MANAGED` (not
//! sudo-code-specific):
//!
//!   * On `install`, register `MailboxStampingHook` and
//!     `WorkspaceBoundaryHook` into the kernel's `KernelDispatch` so
//!     every */chat-with-me write is stamped and every cross-owner
//!     `/proc/{pid}/workspace/` write is rejected.
//!   * On `enlist_rust`, take the place in the registry that
//!     `nx.service("managed_agent")` resolves to (Python lookup
//!     returns None — this service is reachable from Rust callers via
//!     `service_registry.lookup_rust("managed_agent")`).
//!
//! The session lifecycle (`StartSession` / `Cancel` / `GetSession`
//! tonic gRPC handlers, the session_id ↔ pid map, the call into
//! AgentRegistry.spawn) lands in a follow-up commit alongside the
//! tonic gRPC wiring. This commit is only the service shell + hook
//! ownership transfer.

use std::sync::Arc;

use crate::service_registry::RustService;

pub(crate) mod mailbox_stamping_hook;
pub(crate) mod mailbox_stamping_policy;
pub(crate) mod workspace_boundary_hook;

/// Service shell. Empty struct today; session map + AgentRegistry
/// handle + tonic gRPC server-streaming-friendly state land here in
/// the follow-up commit.
pub(crate) struct ManagedAgentService;

impl ManagedAgentService {
    pub(crate) const NAME: &'static str = "managed_agent";

    pub(crate) fn new() -> Self {
        Self
    }

    /// Install the service into a freshly-constructed kernel:
    ///
    ///   1. Register the chat-with-me + workspace-boundary hooks into
    ///      the kernel's `KernelDispatch`.
    ///   2. Enlist the service into `ServiceRegistry` so future tonic
    ///      gRPC handlers + Python factory wiring can resolve it via
    ///      `service_registry.lookup_rust(NAME)`.
    ///
    /// Called from `Kernel::new()`. Idempotency: ServiceRegistry rejects
    /// duplicate registrations, so installing twice surfaces a clear
    /// error. The hook registry has no dedup, so repeated installs
    /// would double-fire hooks — the kernel construction path is the
    /// only caller, called once.
    pub(crate) fn install(kernel: &crate::kernel::Kernel) -> Result<(), String> {
        // Pre-write workspace boundary teaching hook — rejects writes
        // into /proc/{pid}/workspace/ from non-owner agents with the
        // structured chat-with-me redirect payload. Stateless, scoped
        // at on_pre by path prefix so non-workspace writes pay zero
        // cost.
        kernel.register_native_hook(Box::new(
            workspace_boundary_hook::WorkspaceBoundaryHook::new(),
        ));
        // Pre-write mailbox envelope stamping — rewrites the `from`
        // field on chat-with-me writes to the caller's authenticated
        // agent_id so LLMs cannot forge identity. Mutating hook
        // (declares mutating_path_suffix = "/chat-with-me"); the
        // dispatcher only clones write content into WriteHookCtx for
        // matching paths.
        kernel.register_native_hook(Box::new(mailbox_stamping_hook::MailboxStampingHook::new()));

        let svc: Arc<dyn RustService> = Arc::new(Self::new());
        kernel.register_rust_service(Self::NAME, svc, Vec::new())
    }
}

impl RustService for ManagedAgentService {
    fn name(&self) -> &str {
        Self::NAME
    }

    fn start(&self) -> Result<(), String> {
        // Hooks were registered at `install` time so they're live from
        // kernel boot. No async state to spin up today; tonic gRPC
        // handler goes here once that wiring lands.
        Ok(())
    }

    fn stop(&self) -> Result<(), String> {
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn service_has_canonical_name() {
        let svc = ManagedAgentService::new();
        assert_eq!(svc.name(), "managed_agent");
        assert_eq!(ManagedAgentService::NAME, "managed_agent");
    }

    #[test]
    fn lifecycle_methods_succeed_on_empty_service() {
        let svc = ManagedAgentService::new();
        svc.start().expect("start should succeed");
        svc.stop().expect("stop should succeed");
    }
}
