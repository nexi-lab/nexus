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
        registry
            .dispatch_pre(ctx)
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
}
