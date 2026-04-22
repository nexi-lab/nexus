//! ServiceRegistry — Rust kernel service symbol table.
//!
//! Manages service instances (Python objects) with DashMap for lock-free
//! concurrent access. Replaces Python `service_registry.py`.
//!
//! Thread-safe: all methods take `&self` (interior mutability via DashMap/atomics).

use dashmap::DashMap;
use parking_lot::{Condvar, Mutex};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;

use pyo3::prelude::*;

// ── ServiceEntry ────────────────────────────────────────────────────────

/// A registered service: name + Python instance + declared exports.
pub(crate) struct ServiceEntry {
    pub name: String,
    pub instance: Py<PyAny>,
    pub exports: Vec<String>,
}

impl Clone for ServiceEntry {
    fn clone(&self) -> Self {
        Python::attach(|py| ServiceEntry {
            name: self.name.clone(),
            instance: self.instance.clone_ref(py),
            exports: self.exports.clone(),
        })
    }
}

// ── ServiceRegistry ─────────────────────────────────────────────────────

/// Kernel service symbol table — DashMap<name, ServiceEntry>.
pub(crate) struct ServiceRegistry {
    services: DashMap<String, ServiceEntry>,
    /// Per-service refcounts for drain-before-swap.
    refcounts: DashMap<String, Arc<AtomicU64>>,
    /// Condvar for drain waiters.
    drain_condvar: Condvar,
    drain_mutex: Mutex<()>,
    /// True after bootstrap() completes.
    bootstrapped: AtomicBool,
    /// Insertion-order tracking for ordered iteration.
    insertion_order: Mutex<Vec<String>>,
}

impl ServiceRegistry {
    pub(crate) fn new() -> Self {
        Self {
            services: DashMap::new(),
            refcounts: DashMap::new(),
            drain_condvar: Condvar::new(),
            drain_mutex: Mutex::new(()),
            bootstrapped: AtomicBool::new(false),
            insertion_order: Mutex::new(Vec::new()),
        }
    }

    /// Register a service. Returns Ok(()) on success.
    /// Fails if name exists and allow_overwrite is false.
    pub(crate) fn enlist(
        &self,
        py: Python<'_>,
        name: &str,
        instance: &Bound<'_, PyAny>,
        exports: Vec<String>,
        allow_overwrite: bool,
    ) -> PyResult<()> {
        // Validate exports
        for exp in &exports {
            if !instance.hasattr(exp.as_str())? {
                return Err(pyo3::exceptions::PyValueError::new_err(format!(
                    "services: {name:?} declares exports not found on instance: [{exp}]"
                )));
            }
        }

        // Duplicate check
        if !allow_overwrite && self.services.contains_key(name) {
            return Err(pyo3::exceptions::PyValueError::new_err(format!(
                "services: {name:?} already registered"
            )));
        }

        let entry = ServiceEntry {
            name: name.to_string(),
            instance: instance.clone().unbind(),
            exports,
        };

        let is_new = !self.services.contains_key(name);
        self.services.insert(name.to_string(), entry);
        if is_new {
            self.insertion_order.lock().push(name.to_string());
        }

        // Auto-start BackgroundService (post-bootstrap only)
        if self.bootstrapped.load(Ordering::Relaxed) {
            if let Ok(bg_cls) = py
                .import("nexus.contracts.protocols.service_lifecycle")
                .and_then(|m| m.getattr("BackgroundService"))
            {
                if instance.is_instance(&bg_cls)? {
                    let coro = instance.call_method0("start")?;
                    // If it's a coroutine, run synchronously
                    if let Ok(inspect) = py.import("inspect") {
                        if let Ok(is_coro) = inspect.call_method1("iscoroutine", (&coro,)) {
                            if is_coro.is_truthy()? {
                                let run_sync =
                                    py.import("nexus.lib.sync_bridge")?.getattr("run_sync")?;
                                run_sync.call1((&coro, 30.0))?;
                            }
                        }
                    }
                }
            }
        }

        // Auto-capture hooks via duck-typed hook_spec()
        self.auto_capture_hooks(py, name, instance)?;

        Ok(())
    }

    /// Look up a service instance by name.
    pub(crate) fn lookup(&self, py: Python<'_>, name: &str) -> Option<Py<PyAny>> {
        self.services.get(name).map(|e| e.instance.clone_ref(py))
    }

    /// Check if a service is registered.
    pub(crate) fn contains(&self, name: &str) -> bool {
        self.services.contains_key(name)
    }

    /// Number of registered services.
    pub(crate) fn count(&self) -> usize {
        self.services.len()
    }

    /// Service names in registration order.
    pub(crate) fn names(&self) -> Vec<String> {
        self.insertion_order.lock().clone()
    }

    /// Service names in reverse registration order.
    pub(crate) fn names_reversed(&self) -> Vec<String> {
        let mut names = self.insertion_order.lock().clone();
        names.reverse();
        names
    }

    /// Unregister a service. Returns true if found.
    pub(crate) fn unregister(&self, name: &str) -> bool {
        let removed = self.services.remove(name).is_some();
        if removed {
            self.insertion_order.lock().retain(|n| n != name);
            self.refcounts.remove(name);
        }
        removed
    }

    /// Full unregister: unhook + remove.
    /// The Python-side dispatch hook unregistration is handled by the caller
    /// (PyKernel wrapper) since it needs the dispatch object.
    pub(crate) fn unregister_full(&self, name: &str) -> bool {
        self.unregister(name)
    }

    /// Hot-swap a service: drain → replace (hook management done by caller).
    pub(crate) fn swap(
        &self,
        py: Python<'_>,
        name: &str,
        new_instance: &Bound<'_, PyAny>,
        exports: Vec<String>,
        timeout_ms: u64,
    ) -> PyResult<()> {
        // Check old exists
        if !self.services.contains_key(name) {
            return Err(pyo3::exceptions::PyKeyError::new_err(format!(
                "swap_service: {name:?} not registered"
            )));
        }

        // Validate exports
        for exp in &exports {
            if !new_instance.hasattr(exp.as_str())? {
                return Err(pyo3::exceptions::PyValueError::new_err(format!(
                    "services: {name:?} replacement declares invalid exports: [{exp}]"
                )));
            }
        }

        // Step 1: Drain refcount
        self.drain(name, timeout_ms);

        // Step 2: Get old exports to inherit if new exports empty
        let old_exports = self
            .services
            .get(name)
            .map(|e| e.exports.clone())
            .unwrap_or_default();

        let final_exports = if exports.is_empty() {
            old_exports
        } else {
            exports
        };

        // Step 3: Atomic replace
        let entry = ServiceEntry {
            name: name.to_string(),
            instance: new_instance.clone().unbind(),
            exports: final_exports,
        };
        self.services.insert(name.to_string(), entry);

        // Step 4: Auto-capture hooks on new instance (caller handles
        // unregistering old hooks + registering new ones via dispatch)
        self.auto_capture_hooks(py, name, new_instance)?;

        Ok(())
    }

    /// Acquire a refcount for a service (for ServiceRef proxy).
    pub(crate) fn ref_acquire(&self, name: &str) {
        self.refcounts
            .entry(name.to_string())
            .or_insert_with(|| Arc::new(AtomicU64::new(0)))
            .fetch_add(1, Ordering::Relaxed);
    }

    /// Release a refcount. Notifies drain waiters if count reaches 0.
    pub(crate) fn ref_release(&self, name: &str) {
        if let Some(rc) = self.refcounts.get(name) {
            let prev = rc.fetch_sub(1, Ordering::Relaxed);
            if prev <= 1 {
                self.drain_condvar.notify_all();
            }
        }
    }

    /// Drain: wait for refcount on `name` to reach 0.
    pub(crate) fn drain(&self, name: &str, timeout_ms: u64) {
        let current = self
            .refcounts
            .get(name)
            .map(|r| r.load(Ordering::Relaxed))
            .unwrap_or(0);
        if current == 0 {
            return;
        }

        let timeout = std::time::Duration::from_millis(timeout_ms);
        let mut guard = self.drain_mutex.lock();
        let _result = self.drain_condvar.wait_for(&mut guard, timeout);
    }

    /// Start all BackgroundService instances.
    pub(crate) fn start_all(&self, py: Python<'_>, timeout_secs: f64) -> PyResult<Vec<String>> {
        let bg_cls = py
            .import("nexus.contracts.protocols.service_lifecycle")?
            .getattr("BackgroundService")?;
        let run_sync = py.import("nexus.lib.sync_bridge")?.getattr("run_sync")?;
        let inspect = py.import("inspect")?;

        let mut started = Vec::new();
        for name in self.names() {
            if let Some(entry) = self.services.get(&name) {
                let instance = entry.instance.bind(py);
                if instance.is_instance(&bg_cls)? {
                    match instance.call_method0("start") {
                        Ok(coro) => {
                            if let Ok(is_coro) = inspect.call_method1("iscoroutine", (&coro,)) {
                                if is_coro.is_truthy()? {
                                    if let Err(e) = run_sync.call1((&coro, timeout_secs)) {
                                        tracing::error!(
                                            "[COORDINATOR] failed to start {name:?}: {e}"
                                        );
                                        continue;
                                    }
                                }
                            }
                            started.push(name);
                        }
                        Err(e) => {
                            tracing::error!("[COORDINATOR] failed to start {name:?}: {e}");
                        }
                    }
                }
            }
        }
        Ok(started)
    }

    /// Stop all BackgroundService instances (reverse order).
    pub(crate) fn stop_all(&self, py: Python<'_>, timeout_secs: f64) -> PyResult<Vec<String>> {
        let bg_cls = py
            .import("nexus.contracts.protocols.service_lifecycle")?
            .getattr("BackgroundService")?;
        let run_sync = py.import("nexus.lib.sync_bridge")?.getattr("run_sync")?;
        let inspect = py.import("inspect")?;

        let mut stopped = Vec::new();
        for name in self.names_reversed() {
            if let Some(entry) = self.services.get(&name) {
                let instance = entry.instance.bind(py);
                if instance.is_instance(&bg_cls)? {
                    match instance.call_method0("stop") {
                        Ok(coro) => {
                            if let Ok(is_coro) = inspect.call_method1("iscoroutine", (&coro,)) {
                                if is_coro.is_truthy()? {
                                    if let Err(e) = run_sync.call1((&coro, timeout_secs)) {
                                        tracing::error!(
                                            "[COORDINATOR] failed to stop {name:?}: {e}"
                                        );
                                        continue;
                                    }
                                }
                            }
                            stopped.push(name);
                        }
                        Err(e) => {
                            tracing::error!("[COORDINATOR] failed to stop {name:?}: {e}");
                        }
                    }
                }
            }
        }
        Ok(stopped)
    }

    /// Close all services that have a close() method (reverse order).
    pub(crate) fn close_all(&self, py: Python<'_>) {
        for name in self.names_reversed() {
            if let Some(entry) = self.services.get(&name) {
                let instance = entry.instance.bind(py);
                if let Ok(close_fn) = instance.getattr("close") {
                    if close_fn.is_callable() {
                        if let Err(e) = close_fn.call0() {
                            tracing::debug!("[COORDINATOR] close({name:?}) failed: {e}");
                        }
                    }
                }
            }
        }
    }

    /// Mark bootstrap complete — future enlist() auto-starts BackgroundService.
    pub(crate) fn mark_bootstrapped(&self) {
        self.bootstrapped.store(true, Ordering::Relaxed);
    }

    /// Snapshot: list of (name, type_name, exports) for diagnostics.
    pub(crate) fn snapshot(&self, py: Python<'_>) -> Vec<(String, String, Vec<String>)> {
        let mut result = Vec::new();
        for name in self.names() {
            if let Some(entry) = self.services.get(&name) {
                let type_name = entry
                    .instance
                    .bind(py)
                    .get_type()
                    .name()
                    .map(|n| n.to_string())
                    .unwrap_or_else(|_| "?".to_string());
                result.push((name, type_name, entry.exports.clone()));
            }
        }
        result
    }

    // ── Hook auto-capture (duck-typed hook_spec) ─────────────────────

    /// Check for hook_spec() and auto-capture. Returns the HookSpec if found.
    fn auto_capture_hooks(
        &self,
        py: Python<'_>,
        _name: &str,
        instance: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        // Use inspect.getattr_static to avoid __getattr__ proxies
        let inspect = py.import("inspect")?;
        let getattr_static = inspect.getattr("getattr_static")?;

        let has_spec = match getattr_static.call1((instance, "hook_spec")) {
            Ok(attr) => attr.is_callable(),
            Err(_) => false,
        };

        if !has_spec {
            return Ok(());
        }

        // hook_spec exists and is callable — the dispatch-side hook
        // registration is handled by the Python enlist() caller which
        // calls _register_hooks_for_spec on the dispatch object.
        // We just verified it exists here.
        Ok(())
    }
}

// ── Tests ───────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_new_registry_is_empty() {
        let reg = ServiceRegistry::new();
        assert_eq!(reg.count(), 0);
        assert!(reg.names().is_empty());
    }

    #[test]
    fn test_drain_returns_immediately_when_zero() {
        let reg = ServiceRegistry::new();
        // Should not block
        reg.drain("nonexistent", 100);
    }

    #[test]
    fn test_mark_bootstrapped() {
        let reg = ServiceRegistry::new();
        assert!(!reg.bootstrapped.load(Ordering::Relaxed));
        reg.mark_bootstrapped();
        assert!(reg.bootstrapped.load(Ordering::Relaxed));
    }
}
