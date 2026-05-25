//! ServiceRegistry — Rust kernel service symbol table.
//!
//! Manages service instances with DashMap for lock-free concurrent access.
//! Holds two flavours of service:
//!
//!   * `ServiceInstance::Managed(Box<dyn ServiceLifecycle>)` — language-
//!     agnostic lifecycle wrapper. The slot for foreign-language
//!     service runtimes that the host wraps in a `ServiceLifecycle`
//!     impl before enlistment.
//!   * `ServiceInstance::Rust(Arc<dyn RustService>)` — services
//!     implemented in Rust (e.g. ManagedAgentService) are registered
//!     through the Rust-callable `Kernel::register_rust_service`
//!     surface. Lifecycle methods are plain Rust trait calls.
//!
//! Thread-safe: all methods take `&self` (interior mutability via DashMap/atomics).

use dashmap::DashMap;
use parking_lot::Condvar;
use parking_lot::Mutex;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;

// ── RustService trait ───────────────────────────────────────────────────
pub use contracts::rust_service::{RustCallError, RustService};

// ── ServiceLifecycle trait ──────────────────────────────────────────────

/// Language-agnostic service lifecycle. Implementors must be `Send +
/// Sync` (held in DashMap across threads). The `Any` super-trait
/// preserves downcasting for hosts that wrap a foreign-language
/// service object and need to recover the concrete wrapper type.
pub trait ServiceLifecycle: Send + Sync + std::any::Any {
    fn start(&self, timeout_secs: f64) -> Result<(), String>;
    fn stop(&self, timeout_secs: f64) -> Result<(), String>;
    fn close(&self) -> Result<(), String>;
    /// Human-readable type name for diagnostic `snapshot()`.
    fn type_name(&self) -> String;
    /// Clone into a new Box (object-safe clone).
    fn clone_box(&self) -> Box<dyn ServiceLifecycle>;
}

// ── ServiceInstance + ServiceEntry ──────────────────────────────────────

/// A registered service instance — either a managed (language-agnostic)
/// or a Rust trait object.
pub(crate) enum ServiceInstance {
    Managed(Box<dyn ServiceLifecycle>),
    Rust(Arc<dyn RustService>),
}

impl ServiceInstance {
    fn clone_inst(&self) -> Self {
        match self {
            Self::Managed(lc) => ServiceInstance::Managed(lc.clone_box()),
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

    /// Register a managed (language-agnostic) service.
    ///
    /// The caller wraps its foreign-language service object in a
    /// `ServiceLifecycle` impl and validates exports before invoking
    /// this entry point.
    pub(crate) fn enlist(
        &self,
        name: &str,
        instance: Box<dyn ServiceLifecycle>,
        exports: Vec<String>,
        allow_overwrite: bool,
    ) -> Result<(), String> {
        if !allow_overwrite && self.services.contains_key(name) {
            return Err(format!("services: {name:?} already registered"));
        }

        // Auto-start post-bootstrap
        let auto_start = self.bootstrapped.load(Ordering::Relaxed);

        let entry = ServiceEntry {
            name: name.to_string(),
            instance: ServiceInstance::Managed(instance),
            exports,
        };

        // `insert().is_none()` distinguishes "new key" from "overwrite"
        // atomically — a separate contains_key check would race with
        // a concurrent enlist() of the same name and double-push to
        // insertion_order.
        let is_new = self.services.insert(name.to_string(), entry).is_none();
        if is_new {
            self.insertion_order.lock().push(name.to_string());
        }

        if auto_start {
            if let Some(entry) = self.services.get(name) {
                if let ServiceInstance::Managed(lc) = &entry.instance {
                    if let Err(e) = lc.start(30.0) {
                        tracing::error!("[COORDINATOR] auto-start {name:?} failed: {e}");
                    }
                }
            }
        }

        Ok(())
    }

    /// Register a Rust-flavoured service.
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

        // See `enlist` — atomic is_new via the insert return value.
        let is_new = self.services.insert(name.to_string(), entry).is_none();
        if is_new {
            self.insertion_order.lock().push(name.to_string());
        }

        if self.bootstrapped.load(Ordering::Relaxed) {
            instance.start()?;
        }
        Ok(())
    }

    /// Kernel-internal lookup for managed services, returning a fresh
    /// clone of the `ServiceLifecycle` trait object. Hosts that need
    /// the concrete wrapper type downcast through the `Any`
    /// super-trait.
    pub(crate) fn lookup_managed(&self, name: &str) -> Option<Box<dyn ServiceLifecycle>> {
        self.services.get(name).and_then(|e| match &e.instance {
            ServiceInstance::Managed(lc) => Some(lc.clone_box()),
            ServiceInstance::Rust(_) => None,
        })
    }

    /// Kernel-internal lookup by name for Rust-flavoured services.
    #[allow(dead_code)]
    pub(crate) fn lookup_rust(&self, name: &str) -> Option<Arc<dyn RustService>> {
        self.services.get(name).and_then(|e| match &e.instance {
            ServiceInstance::Rust(svc) => Some(Arc::clone(svc)),
            ServiceInstance::Managed(_) => None,
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

    /// Hot-swap a managed service: drain → replace.
    pub(crate) fn swap(
        &self,
        name: &str,
        new_instance: Box<dyn ServiceLifecycle>,
        exports: Vec<String>,
        timeout_ms: u64,
    ) -> Result<(), String> {
        if !self.services.contains_key(name) {
            return Err(format!("swap_service: {name:?} not registered"));
        }

        self.drain(name, timeout_ms);

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

        let entry = ServiceEntry {
            name: name.to_string(),
            instance: ServiceInstance::Managed(new_instance),
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
    ///
    /// Takes `drain_mutex` briefly before `notify_all` to close the
    /// classic Mesa-condvar lost-wakeup window: parking_lot's Condvar
    /// does not store notifications, so a notify that fires while a
    /// drainer is between its refcount check and `wait_for` would be
    /// dropped on the floor. Acquiring the same mutex the drainer is
    /// about to wait on serializes the notify with the wait.
    pub(crate) fn ref_release(&self, name: &str) {
        if let Some(rc) = self.refcounts.get(name) {
            let prev = rc.fetch_sub(1, Ordering::Release);
            if prev <= 1 {
                let _g = self.drain_mutex.lock();
                self.drain_condvar.notify_all();
            }
        }
    }

    /// Drain: wait for refcount on `name` to reach 0.
    ///
    /// Loop discipline: parking_lot's `wait_for` can return early on
    /// a spurious wake, and `notify_all` from [`Self::ref_release`]
    /// wakes drainers for every service (not just `name`). Re-check
    /// the target's refcount on every wake and only return when it
    /// reaches zero or the deadline expires.
    pub(crate) fn drain(&self, name: &str, timeout_ms: u64) {
        let deadline =
            std::time::Instant::now().checked_add(std::time::Duration::from_millis(timeout_ms));
        let mut guard = self.drain_mutex.lock();
        loop {
            let current = self
                .refcounts
                .get(name)
                .map(|r| r.load(Ordering::Acquire))
                .unwrap_or(0);
            if current == 0 {
                return;
            }
            let remaining = match deadline {
                Some(d) => match d.checked_duration_since(std::time::Instant::now()) {
                    Some(r) if !r.is_zero() => r,
                    _ => return,
                },
                None => std::time::Duration::from_millis(timeout_ms),
            };
            let _ = self.drain_condvar.wait_for(&mut guard, remaining);
        }
    }

    /// Start all services (managed + Rust).
    pub(crate) fn start_all(&self, timeout_secs: f64) -> Result<Vec<String>, String> {
        let mut started = Vec::new();
        for name in self.names() {
            if let Some(entry) = self.services.get(&name) {
                let result = match &entry.instance {
                    ServiceInstance::Managed(lc) => lc.start(timeout_secs),
                    ServiceInstance::Rust(svc) => svc.start(),
                };
                match result {
                    Ok(()) => started.push(name),
                    Err(e) => {
                        tracing::error!("[COORDINATOR] failed to start {name:?}: {e}");
                    }
                }
            }
        }
        Ok(started)
    }

    /// Stop all services (reverse order).
    pub(crate) fn stop_all(&self, timeout_secs: f64) -> Result<Vec<String>, String> {
        let mut stopped = Vec::new();
        for name in self.names_reversed() {
            if let Some(entry) = self.services.get(&name) {
                let result = match &entry.instance {
                    ServiceInstance::Managed(lc) => lc.stop(timeout_secs),
                    ServiceInstance::Rust(svc) => svc.stop(),
                };
                match result {
                    Ok(()) => stopped.push(name),
                    Err(e) => {
                        tracing::error!("[COORDINATOR] failed to stop {name:?}: {e}");
                    }
                }
            }
        }
        Ok(stopped)
    }

    /// Close all managed services (reverse order).
    pub(crate) fn close_all(&self) {
        for name in self.names_reversed() {
            if let Some(entry) = self.services.get(&name) {
                if let ServiceInstance::Managed(lc) = &entry.instance {
                    if let Err(e) = lc.close() {
                        tracing::debug!("[COORDINATOR] close({name:?}) failed: {e}");
                    }
                }
            }
        }
    }

    /// Mark bootstrap complete — future enlist() auto-starts.
    pub(crate) fn mark_bootstrapped(&self) {
        self.bootstrapped.store(true, Ordering::Relaxed);
    }

    /// Snapshot: list of (name, type_name, exports) for diagnostics.
    pub(crate) fn snapshot(&self) -> Vec<(String, String, Vec<String>)> {
        let mut result = Vec::new();
        for name in self.names() {
            if let Some(entry) = self.services.get(&name) {
                let type_name = match &entry.instance {
                    ServiceInstance::Managed(lc) => lc.type_name(),
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
        reg.drain("nonexistent", 100);
    }

    #[test]
    fn drain_waits_until_ref_release_then_returns() {
        use std::thread;
        use std::time::{Duration, Instant};

        let reg = Arc::new(ServiceRegistry::new());
        reg.ref_acquire("svc-a");

        let releaser = {
            let reg = Arc::clone(&reg);
            thread::spawn(move || {
                thread::sleep(Duration::from_millis(50));
                reg.ref_release("svc-a");
            })
        };

        let started = Instant::now();
        reg.drain("svc-a", 5_000);
        let elapsed = started.elapsed();
        // Released at ~50ms; drain should wake well before the 5s
        // ceiling. Allow generous slack for slow CI runners.
        assert!(
            elapsed < Duration::from_millis(1_000),
            "drain blocked past release: {elapsed:?}"
        );
        releaser.join().unwrap();
    }

    #[test]
    fn drain_returns_at_deadline_when_ref_not_released() {
        use std::time::{Duration, Instant};

        let reg = ServiceRegistry::new();
        reg.ref_acquire("svc-b");

        let started = Instant::now();
        reg.drain("svc-b", 100);
        let elapsed = started.elapsed();
        // Caller asked for 100ms; we should not wait substantially
        // longer (spurious wake loop must respect the deadline).
        assert!(
            elapsed >= Duration::from_millis(100),
            "drain returned before deadline: {elapsed:?}"
        );
        assert!(
            elapsed < Duration::from_millis(500),
            "drain blocked far past deadline: {elapsed:?}"
        );
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
