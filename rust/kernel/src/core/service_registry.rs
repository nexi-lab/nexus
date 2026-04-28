//! ServiceRegistry — Rust kernel service symbol table.
//!
//! Manages service instances with DashMap for lock-free concurrent access.
//! Holds two flavours of service:
//!
//!   * `ServiceInstance::Python(Py<PyAny>)` — the original storage; every
//!     pre-existing nexus service (ReBAC, Mount, Auth, AgentRegistry,
//!     AcpService, …) is a Python class registered through the
//!     `sys_setattr("/__sys__/services/X")` syscall. Lifecycle methods
//!     (`start` / `stop`) are Python coroutines dispatched via
//!     `asyncio.run`.
//!   * `ServiceInstance::Rust(Arc<dyn RustService>)` — services
//!     implemented in Rust (e.g. ManagedAgentService) are registered
//!     through the Rust-callable `Kernel::register_rust_service`
//!     surface, mirroring the way `Kernel::add_mount` is the Rust
//!     parallel of `sys_setattr(DT_MOUNT)`. Lifecycle methods are plain
//!     Rust trait calls — no PyO3 boundary on start/stop.
//!
//! Thread-safe: all methods take `&self` (interior mutability via DashMap/atomics).

use dashmap::DashMap;
use parking_lot::{Condvar, Mutex};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;

use pyo3::prelude::*;

// ── RustService trait ───────────────────────────────────────────────────

/// Error returned by `RustService::dispatch` and surfaced through
/// `Kernel::dispatch_rust_call`. Maps onto JSON-RPC-shaped wire error
/// codes by the gRPC `Call` handler (commit 13).
#[derive(Debug)]
pub enum RustCallError {
    /// Method name is not handled by this service. The default
    /// `RustService::dispatch` impl returns this so existing services
    /// compile without an explicit override.
    NotFound,
    /// Payload could not be parsed, or its fields are out of range.
    InvalidArgument(String),
    /// Service-internal failure (state corruption, downstream IO error).
    Internal(String),
}

impl std::fmt::Display for RustCallError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::NotFound => write!(f, "method not found"),
            Self::InvalidArgument(m) => write!(f, "invalid argument: {m}"),
            Self::Internal(m) => write!(f, "internal: {m}"),
        }
    }
}

impl std::error::Error for RustCallError {}

/// Surface a Rust-implemented service exposes to ServiceRegistry.
///
/// Mirrors the Python `BackgroundService` protocol but with synchronous
/// Rust signatures — the Rust path skips the asyncio.run trampoline the
/// Python path needs. `start` / `stop` are still called by
/// `start_all` / `stop_all`; `name` is the canonical service name used
/// for `nx.service("…")` lookups.
///
/// Implementors live in `rust/kernel/src/<service>/` (or post-3932,
/// `rust/services/src/<service>/`). They must be `Send + Sync` so the
/// registry can hand `Arc<dyn RustService>` to multiple consumers.
pub trait RustService: Send + Sync {
    fn name(&self) -> &str;

    /// Start the service. Called once at bootstrap (or at enlist time
    /// for services registered post-bootstrap). Blocking is fine — the
    /// Rust path does not run on the asyncio loop.
    fn start(&self) -> Result<(), String> {
        Ok(())
    }

    /// Stop the service. Called once at shutdown, in reverse
    /// registration order.
    fn stop(&self) -> Result<(), String> {
        Ok(())
    }

    /// Dispatch a JSON-encoded RPC. The gRPC `Call` handler routes
    /// `NexusVFSService.Call(method, payload)` requests to a Rust
    /// service first via `Kernel::dispatch_rust_call`; on `NotFound`
    /// the handler falls through to the Python `dispatch_method` path,
    /// preserving compatibility with `@rpc_expose` services.
    ///
    /// `method` is the bare method name (no service prefix). `payload`
    /// is the raw JSON request body — implementations parse and encode
    /// with `serde_json` and surface decode failures as
    /// `RustCallError::InvalidArgument`.
    ///
    /// Default impl returns `NotFound` so services that do not yet
    /// expose any RPCs continue to compile.
    fn dispatch(&self, _method: &str, _payload: &[u8]) -> Result<Vec<u8>, RustCallError> {
        Err(RustCallError::NotFound)
    }
}

// ── ServiceInstance + ServiceEntry ──────────────────────────────────────

/// A registered service instance — either a Python class or a Rust
/// trait object. The two flavours share lookup / refcount / drain
/// machinery; only the lifecycle dispatch in `start_all` / `stop_all` /
/// `close_all` branches on the variant.
pub(crate) enum ServiceInstance {
    Python(Py<PyAny>),
    Rust(Arc<dyn RustService>),
}

impl ServiceInstance {
    fn clone_inst(&self) -> Self {
        match self {
            Self::Python(obj) => Python::attach(|py| ServiceInstance::Python(obj.clone_ref(py))),
            Self::Rust(svc) => ServiceInstance::Rust(Arc::clone(svc)),
        }
    }
}

/// A registered service: name + instance + declared exports.
pub(crate) struct ServiceEntry {
    pub name: String,
    pub instance: ServiceInstance,
    pub exports: Vec<String>,
}

impl Clone for ServiceEntry {
    fn clone(&self) -> Self {
        ServiceEntry {
            name: self.name.clone(),
            instance: self.instance.clone_inst(),
            exports: self.exports.clone(),
        }
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

/// Run a Python coroutine to completion via stdlib asyncio. No nexus imports.
fn run_coro(py: Python<'_>, coro: &Bound<'_, PyAny>, timeout_secs: f64) -> PyResult<()> {
    let asyncio = py.import("asyncio")?;
    let timed = asyncio.call_method1("wait_for", (coro, timeout_secs))?;
    asyncio.call_method1("run", (&timed,))?;
    Ok(())
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
            instance: ServiceInstance::Python(instance.clone().unbind()),
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
                    run_coro(py, &coro, 30.0)?;
                }
            }
        }

        Ok(())
    }

    /// Register a Rust-flavoured service. Rust-callable parallel of
    /// [`enlist`](Self::enlist); same exports / overwrite semantics, but
    /// the instance is an `Arc<dyn RustService>` and lifecycle methods
    /// dispatch via the trait directly (no asyncio.run trampoline).
    ///
    /// Auto-starts the service when called post-bootstrap — same
    /// behaviour as the Python path.
    pub(crate) fn enlist_rust(
        &self,
        name: &str,
        instance: Arc<dyn RustService>,
        exports: Vec<String>,
        allow_overwrite: bool,
    ) -> Result<(), String> {
        if !allow_overwrite && self.services.contains_key(name) {
            return Err(format!("services: {name:?} already registered"));
        }

        let entry = ServiceEntry {
            name: name.to_string(),
            instance: ServiceInstance::Rust(Arc::clone(&instance)),
            exports,
        };

        let is_new = !self.services.contains_key(name);
        self.services.insert(name.to_string(), entry);
        if is_new {
            self.insertion_order.lock().push(name.to_string());
        }

        if self.bootstrapped.load(Ordering::Relaxed) {
            instance.start()?;
        }
        Ok(())
    }

    /// Kernel-internal lookup by name for Python-flavoured services.
    /// Reached from Python through the `Kernel::service_lookup` PyO3
    /// method (which `nx.service(name)` delegates to). Rust services
    /// have a parallel surface — `ServiceRegistry::lookup_rust`,
    /// reached through `Kernel::service_lookup_rust`. Both `lookup`
    /// methods are `pub(crate)`: callers always go through `Kernel`,
    /// not the registry directly (KERNEL-ARCHITECTURE §4 — registry
    /// is a kernel primitive).
    ///
    /// Returns the Python instance for `Python`-flavoured services;
    /// returns `None` for `Rust`-flavoured services so
    /// `nx.service(name)` stays well-typed (Python sees only services
    /// it can call methods on).
    pub(crate) fn lookup(&self, py: Python<'_>, name: &str) -> Option<Py<PyAny>> {
        self.services.get(name).and_then(|e| match &e.instance {
            ServiceInstance::Python(obj) => Some(obj.clone_ref(py)),
            ServiceInstance::Rust(_) => None,
        })
    }

    /// Kernel-internal lookup by name for Rust-flavoured services.
    /// **Not the call surface for in-crate Rust callers** — they go
    /// through [`Kernel::service_lookup_rust`], the syscall-shaped
    /// parallel of the Python-facing [`Self::lookup`] (reached from
    /// Python via `nx.service(name)`). Going through `Kernel` keeps
    /// `ServiceRegistry` a kernel primitive (KERNEL-ARCHITECTURE §4)
    /// rather than a directly-poked module.
    ///
    /// Returns the registered `Arc<dyn RustService>` for `Rust`-flavoured
    /// entries; returns `None` for `Python`-flavoured entries (Python
    /// services are reached via `Self::lookup`) and for unknown names.
    #[allow(dead_code)]
    pub(crate) fn lookup_rust(&self, name: &str) -> Option<Arc<dyn RustService>> {
        self.services.get(name).and_then(|e| match &e.instance {
            ServiceInstance::Rust(svc) => Some(Arc::clone(svc)),
            ServiceInstance::Python(_) => None,
        })
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
        _py: Python<'_>,
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
            instance: ServiceInstance::Python(new_instance.clone().unbind()),
            exports: final_exports,
        };
        self.services.insert(name.to_string(), entry);

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

        let mut started = Vec::new();
        for name in self.names() {
            if let Some(entry) = self.services.get(&name) {
                match &entry.instance {
                    ServiceInstance::Python(py_obj) => {
                        let instance = py_obj.bind(py);
                        if instance.is_instance(&bg_cls)? {
                            match instance.call_method0("start") {
                                Ok(coro) => {
                                    if let Err(e) = run_coro(py, &coro, timeout_secs) {
                                        tracing::error!(
                                            "[COORDINATOR] failed to start {name:?}: {e}"
                                        );
                                        continue;
                                    }
                                    started.push(name);
                                }
                                Err(e) => {
                                    tracing::error!("[COORDINATOR] failed to start {name:?}: {e}");
                                }
                            }
                        }
                    }
                    ServiceInstance::Rust(svc) => match svc.start() {
                        Ok(()) => started.push(name),
                        Err(e) => {
                            tracing::error!("[COORDINATOR] failed to start {name:?}: {e}");
                        }
                    },
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

        let mut stopped = Vec::new();
        for name in self.names_reversed() {
            if let Some(entry) = self.services.get(&name) {
                match &entry.instance {
                    ServiceInstance::Python(py_obj) => {
                        let instance = py_obj.bind(py);
                        if instance.is_instance(&bg_cls)? {
                            match instance.call_method0("stop") {
                                Ok(coro) => {
                                    if let Err(e) = run_coro(py, &coro, timeout_secs) {
                                        tracing::error!(
                                            "[COORDINATOR] failed to stop {name:?}: {e}"
                                        );
                                        continue;
                                    }
                                    stopped.push(name);
                                }
                                Err(e) => {
                                    tracing::error!("[COORDINATOR] failed to stop {name:?}: {e}");
                                }
                            }
                        }
                    }
                    ServiceInstance::Rust(svc) => match svc.stop() {
                        Ok(()) => stopped.push(name),
                        Err(e) => {
                            tracing::error!("[COORDINATOR] failed to stop {name:?}: {e}");
                        }
                    },
                }
            }
        }
        Ok(stopped)
    }

    /// Close all services that have a close() method (reverse order).
    /// Rust services don't expose a close() method — `stop_all` is the
    /// shutdown hook for them.
    pub(crate) fn close_all(&self, py: Python<'_>) {
        for name in self.names_reversed() {
            if let Some(entry) = self.services.get(&name) {
                if let ServiceInstance::Python(py_obj) = &entry.instance {
                    let instance = py_obj.bind(py);
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
                let type_name = match &entry.instance {
                    ServiceInstance::Python(py_obj) => py_obj
                        .bind(py)
                        .get_type()
                        .name()
                        .map(|n| n.to_string())
                        .unwrap_or_else(|_| "?".to_string()),
                    ServiceInstance::Rust(svc) => format!("rust::{}", svc.name()),
                };
                result.push((name, type_name, entry.exports.clone()));
            }
        }
        result
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

    // ── Rust service tests ──────────────────────────────────────────

    use std::sync::atomic::AtomicUsize;

    struct TestRustService {
        svc_name: String,
        start_count: AtomicUsize,
        stop_count: AtomicUsize,
    }

    impl TestRustService {
        fn new(name: &str) -> Self {
            Self {
                svc_name: name.to_string(),
                start_count: AtomicUsize::new(0),
                stop_count: AtomicUsize::new(0),
            }
        }
    }

    impl RustService for TestRustService {
        fn name(&self) -> &str {
            &self.svc_name
        }
        fn start(&self) -> Result<(), String> {
            self.start_count.fetch_add(1, Ordering::Relaxed);
            Ok(())
        }
        fn stop(&self) -> Result<(), String> {
            self.stop_count.fetch_add(1, Ordering::Relaxed);
            Ok(())
        }
    }

    #[test]
    fn rust_enlist_round_trip() {
        let reg = ServiceRegistry::new();
        let svc = Arc::new(TestRustService::new("managed_agent"));
        reg.enlist_rust(
            "managed_agent",
            Arc::clone(&svc) as Arc<dyn RustService>,
            vec![],
            false,
        )
        .expect("enlist_rust should succeed");
        assert_eq!(reg.count(), 1);
        assert!(reg.contains("managed_agent"));
        assert_eq!(reg.names(), vec!["managed_agent".to_string()]);

        let looked = reg.lookup_rust("managed_agent").expect("present");
        assert_eq!(looked.name(), "managed_agent");
    }

    #[test]
    fn rust_enlist_post_bootstrap_auto_starts() {
        let reg = ServiceRegistry::new();
        reg.mark_bootstrapped();
        let svc = Arc::new(TestRustService::new("managed_agent"));
        reg.enlist_rust(
            "managed_agent",
            Arc::clone(&svc) as Arc<dyn RustService>,
            vec![],
            false,
        )
        .unwrap();
        assert_eq!(svc.start_count.load(Ordering::Relaxed), 1);
    }

    #[test]
    fn rust_enlist_pre_bootstrap_does_not_auto_start() {
        let reg = ServiceRegistry::new();
        let svc = Arc::new(TestRustService::new("managed_agent"));
        reg.enlist_rust(
            "managed_agent",
            Arc::clone(&svc) as Arc<dyn RustService>,
            vec![],
            false,
        )
        .unwrap();
        // Pre-bootstrap path defers start to start_all (which the kernel
        // boot calls explicitly).
        assert_eq!(svc.start_count.load(Ordering::Relaxed), 0);
    }

    #[test]
    fn rust_enlist_rejects_duplicate_without_overwrite() {
        let reg = ServiceRegistry::new();
        let a = Arc::new(TestRustService::new("managed_agent"));
        reg.enlist_rust("managed_agent", a as Arc<dyn RustService>, vec![], false)
            .unwrap();
        let b = Arc::new(TestRustService::new("managed_agent"));
        let err = reg
            .enlist_rust("managed_agent", b as Arc<dyn RustService>, vec![], false)
            .expect_err("duplicate should be rejected");
        assert!(err.contains("already registered"));
    }

    #[test]
    fn lookup_rust_returns_none_for_unknown() {
        let reg = ServiceRegistry::new();
        assert!(reg.lookup_rust("nope").is_none());
    }

    #[test]
    fn unregister_drops_rust_service() {
        let reg = ServiceRegistry::new();
        let svc = Arc::new(TestRustService::new("managed_agent"));
        reg.enlist_rust("managed_agent", svc as Arc<dyn RustService>, vec![], false)
            .unwrap();
        assert!(reg.unregister("managed_agent"));
        assert_eq!(reg.count(), 0);
        assert!(reg.lookup_rust("managed_agent").is_none());
    }
}
