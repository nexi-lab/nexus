//! Hook/Observer registries — PyO3-dependent dispatch infrastructure.
//!
//! Extracted from dispatch.rs (PR 22) so dispatch.rs is pure Rust.
//!
//! Contains:
//!   - InterceptHook trait (methods use Py<PyAny> / PyErr)
//!   - HookEntry + HookRegistry (stores Box<dyn InterceptHook> + Py<PyAny>)
//!   - ObserverEntry + ObserverRegistry (stores Py<PyAny>)
//!
//! Owned by PyKernel wrapper (generated_pyo3.rs), NOT by pure Rust Kernel.

use pyo3::prelude::*;
use pyo3::types::PyAny;
use std::collections::HashMap;

// ── InterceptHook trait ──────────────────────────────────────────────

/// INTERCEPT hook — called before/after each syscall.
///
/// Rust equivalent of Python `VFSReadHook`/`VFSWriteHook`/etc.
/// Pre-hooks can abort by returning Err. Post-hooks are fire-and-forget.
///
/// Each method receives opaque context (PyObject) — kernel never inspects it.
///
/// NOTE: PyInterceptHookAdapter (in generated_pyo3.rs, codegen output) is
/// the only production impl of this trait. Once the kernel boundary collapses
/// to pure Rust (syscall-design.md §7), PyInterceptHookAdapter should be
/// gated behind #[cfg(test)] — production hooks will be native Rust impls
/// that don't cross the PyO3 boundary.
#[allow(dead_code)]
pub(crate) trait InterceptHook: Send + Sync {
    fn name(&self) -> &str;
    fn on_pre_read(&self, ctx: &Py<PyAny>) -> Result<(), PyErr>;
    fn on_post_read(&self, ctx: &Py<PyAny>);
    fn on_pre_write(&self, ctx: &Py<PyAny>) -> Result<(), PyErr>;
    fn on_post_write(&self, ctx: &Py<PyAny>);
    fn on_pre_delete(&self, ctx: &Py<PyAny>) -> Result<(), PyErr>;
    fn on_post_delete(&self, ctx: &Py<PyAny>);
    fn on_pre_rename(&self, ctx: &Py<PyAny>) -> Result<(), PyErr>;
    fn on_post_rename(&self, ctx: &Py<PyAny>);
    fn on_pre_mkdir(&self, ctx: &Py<PyAny>) -> Result<(), PyErr>;
    fn on_post_mkdir(&self, ctx: &Py<PyAny>);
    fn on_pre_rmdir(&self, ctx: &Py<PyAny>) -> Result<(), PyErr>;
    fn on_post_rmdir(&self, ctx: &Py<PyAny>);
    fn on_pre_copy(&self, ctx: &Py<PyAny>) -> Result<(), PyErr>;
    fn on_post_copy(&self, ctx: &Py<PyAny>);
    fn on_pre_stat(&self, ctx: &Py<PyAny>) -> Result<(), PyErr>;
    fn on_post_stat(&self, ctx: &Py<PyAny>);
    fn on_pre_access(&self, ctx: &Py<PyAny>) -> Result<(), PyErr>;
    fn on_post_access(&self, ctx: &Py<PyAny>);
    fn on_pre_write_batch(&self, ctx: &Py<PyAny>) -> Result<(), PyErr>;
    fn on_post_write_batch(&self, ctx: &Py<PyAny>);
}

// ── HookRegistry ────────────────────────────────────────────────────

/// Cached metadata for a single hook.
pub(crate) struct HookEntry {
    /// Rust trait object — used by kernel dispatch (language-agnostic).
    pub(crate) hook: Box<dyn InterceptHook>,
    /// Original Python object — returned to Python callers via get_pre_hooks().
    pub(crate) hook_py: Py<PyAny>,
    pub(crate) has_pre: bool,
    pub(crate) is_async_post: bool,
    #[allow(dead_code)]
    pub(crate) name: String,
}

/// Registry that caches hook metadata at registration time.
///
/// Eliminates per-dispatch `getattr()` and `inspect.iscoroutinefunction()`
/// overhead by detecting these properties once at `register()` time.
pub(crate) struct HookRegistry {
    ops: HashMap<String, Vec<HookEntry>>,
}

impl HookRegistry {
    pub(crate) fn new() -> Self {
        Self {
            ops: HashMap::new(),
        }
    }

    /// Register a hook for the given operation.
    #[cfg(feature = "py-hook-adapters")]
    pub(crate) fn register(
        &mut self,
        op: &str,
        hook_impl: Box<dyn InterceptHook>,
        hook_py: Py<PyAny>,
        has_pre: bool,
        is_async_post: bool,
        name: String,
    ) {
        self.ops.entry(op.to_string()).or_default().push(HookEntry {
            hook: hook_impl,
            hook_py,
            has_pre,
            is_async_post,
            name,
        });
    }

    /// Remove a hook by identity (`is` check on original Python object).
    pub(crate) fn unregister(&mut self, py: Python<'_>, op: &str, hook: &Bound<'_, PyAny>) -> bool {
        if let Some(entries) = self.ops.get_mut(op) {
            let hook_ptr = hook.as_ptr();
            if let Some(pos) = entries
                .iter()
                .position(|e| e.hook_py.bind(py).as_ptr() == hook_ptr)
            {
                entries.remove(pos);
                return true;
            }
        }
        false
    }

    /// Return Python hook objects that have `on_pre_{op}` (for Python callers).
    pub(crate) fn get_pre_hooks(&self, py: Python<'_>, op: &str) -> Vec<Py<PyAny>> {
        self.ops
            .get(op)
            .map(|entries| {
                entries
                    .iter()
                    .filter(|e| e.has_pre)
                    .map(|e| e.hook_py.clone_ref(py))
                    .collect()
            })
            .unwrap_or_default()
    }

    /// Return Rust trait references for pre-hooks (for kernel dispatch).
    pub(crate) fn get_pre_hook_impls(&self, op: &str) -> Vec<&dyn InterceptHook> {
        self.ops
            .get(op)
            .map(|entries| {
                entries
                    .iter()
                    .filter(|e| e.has_pre)
                    .map(|e| e.hook.as_ref())
                    .collect()
            })
            .unwrap_or_default()
    }

    /// Return Rust trait references for sync post-hooks (kernel dispatch).
    pub(crate) fn get_post_hook_impls(&self, op: &str) -> Vec<&dyn InterceptHook> {
        self.ops
            .get(op)
            .map(|entries| {
                entries
                    .iter()
                    .filter(|e| !e.is_async_post)
                    .map(|e| e.hook.as_ref())
                    .collect()
            })
            .unwrap_or_default()
    }

    /// Return (sync_post_hooks, async_post_hooks) as Python objects.
    pub(crate) fn get_post_hooks(
        &self,
        py: Python<'_>,
        op: &str,
    ) -> (Vec<Py<PyAny>>, Vec<Py<PyAny>>) {
        let entries = match self.ops.get(op) {
            Some(e) => e,
            None => return (Vec::new(), Vec::new()),
        };
        let sync: Vec<Py<PyAny>> = entries
            .iter()
            .filter(|e| !e.is_async_post)
            .map(|e| e.hook_py.clone_ref(py))
            .collect();
        let async_: Vec<Py<PyAny>> = entries
            .iter()
            .filter(|e| e.is_async_post)
            .map(|e| e.hook_py.clone_ref(py))
            .collect();
        (sync, async_)
    }

    /// Return all Python hook objects for the given operation.
    pub(crate) fn get_all_hooks(&self, py: Python<'_>, op: &str) -> Vec<Py<PyAny>> {
        self.ops
            .get(op)
            .map(|entries| entries.iter().map(|e| e.hook_py.clone_ref(py)).collect())
            .unwrap_or_default()
    }

    /// Number of hooks registered for the given operation.
    pub(crate) fn count(&self, op: &str) -> usize {
        self.ops.get(op).map(|e| e.len()).unwrap_or(0)
    }
}

// Observer registration and dispatch live in pure Python
// (DispatchMixin._observers). Rust KernelObserverRegistry (kernel.rs)
// is retained for future Rust-native observers.
