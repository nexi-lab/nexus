//! Native INTERCEPT hook dispatch — `dispatch_native_pre`,
//! `dispatch_native_post`, `register_native_hook`.
//!
//! Phase G of Phase 3 restructure plan extracted these methods from
//! the monolithic `kernel.rs`.  The split is a file-organization
//! change — every method stays a member of [`Kernel`] via the
//! submodule's `impl Kernel { ... }` block.

use crate::dispatch::{HookContext, NativeInterceptHook};

use super::{Kernel, KernelError, RwLockExt};

impl Kernel {
    // ── Native INTERCEPT hook dispatch (§11 Phase 14) ─────────────────

    /// Dispatch PRE-INTERCEPT hooks from NativeHookRegistry.
    /// Returns Err(KernelError) if any hook aborts.
    /// No-op when registry is empty (zero-cost lock check).
    ///
    /// Uses ``read_unconditional()`` (not the writer-fair variant) so a
    /// hook that re-enters ``sys_read`` — typical of ReBAC's permission_hook
    /// reading its own ``/__sys__/rebac/namespaces/...`` config during a
    /// permission check — does not deadlock on the recursive shared lock.
    /// The only writer here is ``register_native_hook`` at startup, so the
    /// usual writer-starvation concern doesn't apply.
    pub fn dispatch_native_pre(&self, ctx: &HookContext) -> Result<(), KernelError> {
        let registry = self.native_hooks.read_unconditional();
        if registry.count() == 0 {
            return Ok(());
        }
        // The hook chain may return a HookOutcome::Replace; today only
        // sys_write threads it through (separate dispatch entry point).
        // For accept/reject hooks we drop the replacement.
        registry
            .dispatch_pre(ctx)
            .map(|_replacement| ())
            .map_err(KernelError::PermissionDenied)
    }

    /// Dispatch POST-INTERCEPT hooks from NativeHookRegistry (fire-and-forget).
    /// No-op when registry is empty (zero-cost lock check).
    /// Uses ``read_unconditional`` for the same recursion reason as the pre dispatch.
    pub fn dispatch_native_post(&self, ctx: &HookContext) {
        let registry = self.native_hooks.read_unconditional();
        if registry.count() == 0 {
            return;
        }
        registry.dispatch_post(ctx);
    }

    /// Register a native Rust hook (e.g. `services::audit::AuditHook`)
    /// with the kernel.  The hook receives pre/post callbacks for every
    /// VFS operation.
    ///
    /// Visibility is `pub` (not `pub(crate)`) so peer crates can install
    /// their own hook impls — Phase 3 onwards services own their hook
    /// lifecycle (services::audit, etc.) and call this from their PyO3
    /// entry points.
    pub fn register_native_hook(&self, hook: Box<dyn NativeInterceptHook>) {
        self.native_hooks.write().register(hook);
    }

    /// Register a Rust-flavoured service with the kernel's
    /// `ServiceRegistry`. The Rust-callable parallel of the
    /// `sys_setattr("/__sys__/services/X", service=…)` syscall —
    /// mirrors the way `Kernel::add_mount` is the Rust parallel of
    /// `sys_setattr(DT_MOUNT)` for backends.
    ///
    /// Cdylib boot wiring calls this after the kernel finishes
    /// constructing itself; for services that pull hooks into the
    /// `KernelDispatch` chain, register the hooks inside the service's
    /// `start()` (called by the registry on enlist).
    #[allow(dead_code)]
    pub fn register_rust_service(
        &self,
        name: &str,
        instance: std::sync::Arc<dyn crate::service_registry::RustService>,
        exports: Vec<String>,
    ) -> Result<(), String> {
        self.service_registry
            .enlist_rust(name, instance, exports, false)
    }

    /// Look up a Rust-flavoured service by canonical name. The
    /// Rust-callable parallel of the Python-facing `service_lookup`
    /// (which Python reaches via `nx.service(name)`); both end up at
    /// the kernel-internal `ServiceRegistry`, but in-crate Rust
    /// callers go through this Kernel method so `ServiceRegistry`
    /// stays a kernel primitive (`pub(crate)`, KERNEL-ARCHITECTURE
    /// §4) — same layering that keeps callers off direct
    /// `vfs_router` / `lock_manager` / `dispatch` access.
    ///
    /// Returns `None` for unknown names and for names registered as
    /// `ServiceInstance::Python` (Python services are reached via
    /// `service_lookup`).
    #[allow(dead_code)]
    pub(crate) fn service_lookup_rust(
        &self,
        name: &str,
    ) -> Option<std::sync::Arc<dyn crate::service_registry::RustService>> {
        self.service_registry.lookup_rust(name)
    }

    /// Dispatch a JSON-encoded RPC to a Rust-flavoured service.
    ///
    /// `Some(Ok(bytes))` — service handled the call and returned a
    /// JSON response.
    /// `Some(Err(RustCallError))` — service exists but rejected the
    /// call (NotFound / InvalidArgument / Internal).
    /// `None` — `name` does not resolve as a Rust-flavoured service;
    /// the gRPC `Call` handler falls through to the Python
    /// `dispatch_method` path so `@rpc_expose` services keep working.
    ///
    /// Mirrors `service_lookup_rust` in keeping in-crate Rust callers
    /// off `ServiceRegistry`; the registry stays a kernel primitive
    /// (KERNEL-ARCHITECTURE §4) and consumers go through `Kernel`.
    #[allow(dead_code)]
    pub fn dispatch_rust_call(
        &self,
        name: &str,
        method: &str,
        payload: &[u8],
    ) -> Option<Result<Vec<u8>, crate::service_registry::RustCallError>> {
        let svc = self.service_registry.lookup_rust(name)?;
        Some(svc.dispatch(method, payload))
    }
}
