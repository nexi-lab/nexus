//! Native INTERCEPT hook dispatch — `dispatch_native_pre`,
//! `dispatch_native_post`, `register_native_hook`.
//!
//! Every method stays a member of [`Kernel`] via this submodule's
//! `impl Kernel { ... }` block.

use crate::dispatch::{HookContext, NativeInterceptHook};

use super::{Kernel, KernelError, RwLockExt};

impl Kernel {
    // ── Native INTERCEPT hook dispatch ─────────────────

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
        // The hook chain may return a HookOutcome::Replace; the
        // accept/reject path drops the replacement — `sys_write` calls
        // `dispatch_native_pre_with_replacement` instead so it can
        // thread the replacement bytes through to the EXECUTE phase.
        registry
            .dispatch_pre(ctx)
            .map(|_replacement| ())
            .map_err(KernelError::PermissionDenied)
    }

    /// Like [`Self::dispatch_native_pre`] but returns the
    /// `HookOutcome::Replace` payload so callers can substitute write
    /// content at the EXECUTE phase. `sys_write` is the only consumer
    /// today — `MailboxStampingHook` (registered for `*/chat-with-me`)
    /// rewrites the envelope's `from` field through this path, and the
    /// caller passes `replacement.unwrap_or(content)` into DT_STREAM
    /// push / DT_FILE backend write. Empty registry returns
    /// `Ok(None)` so the no-hook hot path stays allocation-free.
    pub fn dispatch_native_pre_with_replacement(
        &self,
        ctx: &HookContext,
    ) -> Result<Option<Vec<u8>>, KernelError> {
        let registry = self.native_hooks.read_unconditional();
        if registry.count() == 0 {
            return Ok(None);
        }
        registry
            .dispatch_pre(ctx)
            .map_err(KernelError::PermissionDenied)
    }

    /// Returns true when at least one registered hook declared a
    /// `mutating_path_suffix` that matches `path`. `sys_write` uses
    /// this as a clone gate: only when a mutating hook matches does the
    /// dispatcher clone the write content into `WriteHookCtx`. The
    /// steady-state path (no mutating hooks) returns false on the
    /// empty-Vec check before any string comparison.
    pub fn has_mutating_hook_match(&self, path: &str) -> bool {
        self.native_hooks
            .read_unconditional()
            .has_mutating_match(path)
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
    /// their own hook impls — services own their hook lifecycle
    /// (services::audit, etc.) and call this from their PyO3 entry
    /// points.
    pub fn register_native_hook(&self, hook: Box<dyn NativeInterceptHook>) {
        self.native_hooks.write().register(hook);
    }

    // ── Service registry facade ───────────────────────────────────────
    //
    // Every ServiceRegistry method is exposed through Kernel so that
    // consumers (PyKernel wrapper, peer crates) never reach the
    // pub(crate) ServiceRegistry directly.

    /// Register a managed (language-agnostic) service.
    pub fn register_managed_service(
        &self,
        name: &str,
        instance: Box<dyn crate::service_registry::ServiceLifecycle>,
        exports: Vec<String>,
        allow_overwrite: bool,
    ) -> Result<(), String> {
        self.service_registry
            .enlist(name, instance, exports, allow_overwrite)
    }

    /// Unregister a service by name. Returns true if found.
    pub fn unregister_service(&self, name: &str) -> bool {
        self.service_registry.unregister(name)
    }

    /// Hot-swap a managed service: drain → replace.
    pub fn swap_managed_service(
        &self,
        name: &str,
        new_instance: Box<dyn crate::service_registry::ServiceLifecycle>,
        exports: Vec<String>,
        timeout_ms: u64,
    ) -> Result<(), String> {
        self.service_registry
            .swap(name, new_instance, exports, timeout_ms)
    }

    /// Look up a managed service by name.
    pub fn service_lookup_managed(
        &self,
        name: &str,
    ) -> Option<Box<dyn crate::service_registry::ServiceLifecycle>> {
        self.service_registry.lookup_managed(name)
    }

    /// Start all services (managed + Rust).
    pub fn service_start_all(&self, timeout_secs: f64) -> Result<Vec<String>, String> {
        self.service_registry.start_all(timeout_secs)
    }

    /// Stop all services (reverse order).
    pub fn service_stop_all(&self, timeout_secs: f64) -> Result<Vec<String>, String> {
        self.service_registry.stop_all(timeout_secs)
    }

    /// Close all managed services (reverse order).
    pub fn service_close_all(&self) {
        self.service_registry.close_all()
    }

    /// Mark bootstrap complete — future enlist() auto-starts.
    pub fn service_mark_bootstrapped(&self) {
        self.service_registry.mark_bootstrapped()
    }

    /// Check if a service is registered.
    pub fn service_contains(&self, name: &str) -> bool {
        self.service_registry.contains(name)
    }

    /// Number of registered services.
    pub fn service_count(&self) -> usize {
        self.service_registry.count()
    }

    /// Service names in registration order.
    pub fn service_names(&self) -> Vec<String> {
        self.service_registry.names()
    }

    /// Snapshot: list of (name, type_name, exports) for diagnostics.
    pub fn service_snapshot(&self) -> Vec<(String, String, Vec<String>)> {
        self.service_registry.snapshot()
    }

    /// Acquire a refcount for a service (for ServiceRef proxy).
    pub fn service_ref_acquire(&self, name: &str) {
        self.service_registry.ref_acquire(name)
    }

    /// Release a refcount. Notifies drain waiters if count reaches 0.
    pub fn service_ref_release(&self, name: &str) {
        self.service_registry.ref_release(name)
    }

    /// Drain: wait for refcount on `name` to reach 0.
    pub fn service_drain(&self, name: &str, timeout_ms: u64) {
        self.service_registry.drain(name, timeout_ms)
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
