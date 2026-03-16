//! VFS Counting Semaphore — Rust-accelerated (Issue #908).
//!
//! Name-addressed counting semaphore with holder tracking, SSOT max_holders
//! enforcement, TTL expiry, and UUID holder IDs.  Local counterpart to
//! `RaftLockManager.acquire(max_holders=N)` (~200ns vs ~5-10ms Raft).
//!
//! Semantics mirror the Raft semaphore:
//!   - holder IDs are UUID4 strings
//!   - first acquirer sets `max_holders` (SSOT); mismatch → ValueError
//!   - TTL: lazy expiry on acquire
//!   - acquire() returns Option<String> (holder_id or None)

use parking_lot::{Condvar, Mutex};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant};
use uuid::Uuid;

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

#[derive(Debug, Clone)]
struct HolderEntry {
    holder_id: String,
    acquired_at_ns: u64,
    expires_at_ns: u64,
}

#[derive(Debug, Clone)]
struct SemaphoreEntry {
    max_holders: u32,
    holders: HashMap<String, HolderEntry>,
}

impl SemaphoreEntry {
    fn new(max_holders: u32) -> Self {
        Self {
            max_holders,
            holders: HashMap::new(),
        }
    }

    fn is_empty(&self) -> bool {
        self.holders.is_empty()
    }

    /// Remove holders whose TTL has expired.
    fn evict_expired(&mut self, now_ns: u64) {
        self.holders.retain(|_, entry| entry.expires_at_ns > now_ns);
    }
}

/// Protected state: all semaphore mutations go through this Mutex.
#[derive(Debug)]
struct SemaphoreState {
    semaphores: HashMap<String, SemaphoreEntry>,
}

// ---------------------------------------------------------------------------
// Monotonic clock helper
// ---------------------------------------------------------------------------

/// Return a monotonic timestamp in nanoseconds (relative to process start).
fn monotonic_ns() -> u64 {
    // Use a lazily-initialized epoch so values are small and readable.
    use std::sync::OnceLock;
    static EPOCH: OnceLock<Instant> = OnceLock::new();
    let epoch = EPOCH.get_or_init(Instant::now);
    epoch.elapsed().as_nanos() as u64
}

// ---------------------------------------------------------------------------
// VFSSemaphore
// ---------------------------------------------------------------------------

/// Rust-accelerated VFS counting semaphore.
///
/// All mutations are serialized through a `parking_lot::Mutex`.
/// A `Condvar` wakes blocked threads on release.
#[pyclass]
pub struct VFSSemaphore {
    state: Mutex<SemaphoreState>,
    notify: Condvar,

    // Metrics (relaxed atomics)
    acquire_count: AtomicU64,
    release_count: AtomicU64,
    timeout_count: AtomicU64,
}

impl VFSSemaphore {
    /// Single non-blocking acquire attempt under the lock.
    /// Returns `Ok(Some(holder_id))` on success, `Ok(None)` if full,
    /// `Err(msg)` on SSOT mismatch.
    fn try_acquire_locked(
        state: &mut SemaphoreState,
        name: &str,
        max_holders: u32,
        ttl_ms: u64,
    ) -> Result<Option<String>, String> {
        let now_ns = monotonic_ns();
        let expires_at_ns = now_ns + ttl_ms * 1_000_000;

        let entry = state.semaphores.get_mut(name);

        if let Some(entry) = entry {
            // SSOT check
            if entry.max_holders != max_holders {
                return Err(format!(
                    "Semaphore {:?}: max_holders mismatch — existing={}, requested={}",
                    name, entry.max_holders, max_holders
                ));
            }

            // Lazy TTL expiry
            entry.evict_expired(now_ns);

            // If empty after eviction, remove and re-create below
            if entry.is_empty() {
                state.semaphores.remove(name);
            } else {
                // Capacity check
                if entry.holders.len() as u32 >= entry.max_holders {
                    return Ok(None);
                }

                let holder_id = Uuid::new_v4().to_string();
                entry.holders.insert(
                    holder_id.clone(),
                    HolderEntry {
                        holder_id: holder_id.clone(),
                        acquired_at_ns: now_ns,
                        expires_at_ns,
                    },
                );
                return Ok(Some(holder_id));
            }
        }

        // Create new entry (either first time, or emptied after eviction)
        let holder_id = Uuid::new_v4().to_string();
        let mut new_entry = SemaphoreEntry::new(max_holders);
        new_entry.holders.insert(
            holder_id.clone(),
            HolderEntry {
                holder_id: holder_id.clone(),
                acquired_at_ns: now_ns,
                expires_at_ns,
            },
        );
        state.semaphores.insert(name.to_string(), new_entry);
        Ok(Some(holder_id))
    }
}

#[pymethods]
impl VFSSemaphore {
    #[new]
    fn new() -> Self {
        Self {
            state: Mutex::new(SemaphoreState {
                semaphores: HashMap::new(),
            }),
            notify: Condvar::new(),
            acquire_count: AtomicU64::new(0),
            release_count: AtomicU64::new(0),
            timeout_count: AtomicU64::new(0),
        }
    }

    /// Acquire a semaphore slot.
    ///
    /// * `name` – semaphore name
    /// * `max_holders` – maximum concurrent holders (SSOT)
    /// * `timeout_ms` – 0 = non-blocking; >0 blocks up to that many ms
    /// * `ttl_ms` – holder auto-expires after this many ms
    ///
    /// Returns holder_id (UUID string) on success, None on timeout.
    #[pyo3(signature = (name, max_holders, timeout_ms=0, ttl_ms=30000))]
    fn acquire(
        &self,
        py: Python<'_>,
        name: &str,
        max_holders: u32,
        timeout_ms: u64,
        ttl_ms: u64,
    ) -> PyResult<Option<String>> {
        if max_holders < 1 {
            return Err(pyo3::exceptions::PyValueError::new_err(format!(
                "max_holders must be >= 1, got {max_holders}"
            )));
        }

        let name = name.to_string();

        // Release the GIL for the (potentially blocking) acquire loop.
        let result = py.detach(|| -> Result<Option<String>, String> {
            // Fast path: non-blocking try under mutex.
            {
                let mut state = self.state.lock();
                match Self::try_acquire_locked(&mut state, &name, max_holders, ttl_ms) {
                    Ok(Some(holder_id)) => {
                        self.acquire_count.fetch_add(1, Ordering::Relaxed);
                        return Ok(Some(holder_id));
                    }
                    Ok(None) => {} // full, continue to blocking path
                    Err(msg) => return Err(msg),
                }
            }

            // Non-blocking: return immediately
            if timeout_ms == 0 {
                self.timeout_count.fetch_add(1, Ordering::Relaxed);
                return Ok(None);
            }

            // Blocking wait with Condvar
            let deadline = Instant::now() + Duration::from_millis(timeout_ms);

            loop {
                let mut state = self.state.lock();
                let remaining = deadline.saturating_duration_since(Instant::now());
                if remaining.is_zero() {
                    self.timeout_count.fetch_add(1, Ordering::Relaxed);
                    return Ok(None);
                }

                let wait_result = self.notify.wait_for(&mut state, remaining);

                match Self::try_acquire_locked(&mut state, &name, max_holders, ttl_ms) {
                    Ok(Some(holder_id)) => {
                        self.acquire_count.fetch_add(1, Ordering::Relaxed);
                        return Ok(Some(holder_id));
                    }
                    Ok(None) => {} // still full
                    Err(msg) => return Err(msg),
                }

                if wait_result.timed_out() {
                    self.timeout_count.fetch_add(1, Ordering::Relaxed);
                    return Ok(None);
                }
            }
        });

        match result {
            Ok(holder_id) => Ok(holder_id),
            Err(msg) => Err(pyo3::exceptions::PyValueError::new_err(msg)),
        }
    }

    /// Release a semaphore slot by holder_id.
    fn release(&self, name: &str, holder_id: &str) -> bool {
        let released = {
            let mut state = self.state.lock();

            let entry = match state.semaphores.get_mut(name) {
                Some(e) => e,
                None => return false,
            };

            if entry.holders.remove(holder_id).is_none() {
                return false;
            }

            if entry.is_empty() {
                state.semaphores.remove(name);
            }

            true
        };

        if released {
            self.notify.notify_all();
            self.release_count.fetch_add(1, Ordering::Relaxed);
        }

        released
    }

    /// Extend TTL for a holder.
    #[pyo3(signature = (name, holder_id, ttl_ms=30000))]
    fn extend(&self, name: &str, holder_id: &str, ttl_ms: u64) -> bool {
        let now_ns = monotonic_ns();
        let mut state = self.state.lock();

        let entry = match state.semaphores.get_mut(name) {
            Some(e) => e,
            None => return false,
        };

        match entry.holders.get_mut(holder_id) {
            Some(holder) => {
                holder.expires_at_ns = now_ns + ttl_ms * 1_000_000;
                true
            }
            None => false,
        }
    }

    /// Return info about a semaphore, or None if it doesn't exist / is empty.
    fn info(&self, py: Python<'_>, name: &str) -> PyResult<Option<Py<PyAny>>> {
        let now_ns = monotonic_ns();
        let mut state = self.state.lock();

        let entry = match state.semaphores.get_mut(name) {
            Some(e) => e,
            None => return Ok(None),
        };

        // Evict expired before reporting
        entry.evict_expired(now_ns);
        if entry.is_empty() {
            state.semaphores.remove(name);
            return Ok(None);
        }

        let dict = PyDict::new(py);
        dict.set_item("name", name)?;
        dict.set_item("max_holders", entry.max_holders)?;
        dict.set_item("active_count", entry.holders.len())?;

        let holders_list = PyList::empty(py);
        for holder in entry.holders.values() {
            let h = PyDict::new(py);
            h.set_item("holder_id", &holder.holder_id)?;
            h.set_item("acquired_at_ns", holder.acquired_at_ns)?;
            h.set_item("expires_at_ns", holder.expires_at_ns)?;
            holders_list.append(h)?;
        }
        dict.set_item("holders", holders_list)?;

        Ok(Some(dict.into()))
    }

    /// Force-release all holders for a semaphore.
    fn force_release(&self, name: &str) -> bool {
        let released = {
            let mut state = self.state.lock();

            let entry = match state.semaphores.remove(name) {
                Some(e) => e,
                None => return false,
            };

            let count = entry.holders.len() as u64;
            self.release_count.fetch_add(count, Ordering::Relaxed);
            true
        };

        if released {
            self.notify.notify_all();
        }

        released
    }

    /// Return aggregate metrics.
    fn stats(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let state = self.state.lock();
        let active_semaphores = state.semaphores.len();
        let active_holders: usize = state.semaphores.values().map(|e| e.holders.len()).sum();
        drop(state);

        let dict = PyDict::new(py);
        dict.set_item("acquire_count", self.acquire_count.load(Ordering::Relaxed))?;
        dict.set_item("release_count", self.release_count.load(Ordering::Relaxed))?;
        dict.set_item("timeout_count", self.timeout_count.load(Ordering::Relaxed))?;
        dict.set_item("active_semaphores", active_semaphores)?;
        dict.set_item("active_holders", active_holders)?;
        Ok(dict.into())
    }

    /// Number of active semaphores.
    #[getter]
    fn active_semaphores(&self) -> usize {
        self.state.lock().semaphores.len()
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    fn make() -> VFSSemaphore {
        VFSSemaphore::new()
    }

    /// Helper: acquire directly through the mutex (bypasses PyO3 / GIL).
    fn acquire(sem: &VFSSemaphore, name: &str, max_holders: u32, ttl_ms: u64) -> Option<String> {
        let mut state = sem.state.lock();
        match VFSSemaphore::try_acquire_locked(&mut state, name, max_holders, ttl_ms) {
            Ok(opt) => opt,
            Err(msg) => panic!("acquire error: {msg}"),
        }
    }

    /// Helper: acquire expecting a ValueError (SSOT mismatch).
    fn acquire_err(sem: &VFSSemaphore, name: &str, max_holders: u32, ttl_ms: u64) -> String {
        let mut state = sem.state.lock();
        match VFSSemaphore::try_acquire_locked(&mut state, name, max_holders, ttl_ms) {
            Err(msg) => msg,
            Ok(_) => panic!("expected error, got success"),
        }
    }

    // -- basic acquire / release -------------------------------------------

    #[test]
    fn test_basic_acquire_release() {
        let sem = make();
        let hid = acquire(&sem, "test", 1, 30_000).unwrap();
        assert!(!hid.is_empty());
        assert!(sem.release("test", &hid));
    }

    #[test]
    fn test_acquire_returns_uuid() {
        let sem = make();
        let hid = acquire(&sem, "test", 1, 30_000).unwrap();
        // UUID4 format: 8-4-4-4-12
        assert_eq!(hid.len(), 36);
        assert_eq!(hid.chars().filter(|c| *c == '-').count(), 4);
        sem.release("test", &hid);
    }

    #[test]
    fn test_release_returns_false_for_unknown() {
        let sem = make();
        assert!(!sem.release("nonexistent", "fake-id"));
    }

    #[test]
    fn test_double_release_returns_false() {
        let sem = make();
        let hid = acquire(&sem, "test", 1, 30_000).unwrap();
        assert!(sem.release("test", &hid));
        assert!(!sem.release("test", &hid));
    }

    #[test]
    fn test_release_wrong_name() {
        let sem = make();
        let hid = acquire(&sem, "test", 1, 30_000).unwrap();
        assert!(!sem.release("other", &hid));
        sem.release("test", &hid);
    }

    // -- multiple holders --------------------------------------------------

    #[test]
    fn test_multiple_holders() {
        let sem = make();
        let h1 = acquire(&sem, "test", 3, 30_000).unwrap();
        let h2 = acquire(&sem, "test", 3, 30_000).unwrap();
        let h3 = acquire(&sem, "test", 3, 30_000).unwrap();
        assert_ne!(h1, h2);
        assert_ne!(h2, h3);

        // 4th should fail
        assert!(acquire(&sem, "test", 3, 30_000).is_none());

        // Release one, then 4th should succeed
        sem.release("test", &h1);
        let h4 = acquire(&sem, "test", 3, 30_000).unwrap();
        assert!(!h4.is_empty());

        sem.release("test", &h2);
        sem.release("test", &h3);
        sem.release("test", &h4);
    }

    #[test]
    fn test_max_holders_one_is_mutex() {
        let sem = make();
        let h1 = acquire(&sem, "mutex", 1, 30_000).unwrap();
        assert!(acquire(&sem, "mutex", 1, 30_000).is_none());
        sem.release("mutex", &h1);
        let h2 = acquire(&sem, "mutex", 1, 30_000).unwrap();
        assert!(!h2.is_empty());
        sem.release("mutex", &h2);
    }

    // -- SSOT enforcement --------------------------------------------------

    #[test]
    fn test_ssot_mismatch() {
        let sem = make();
        let _h = acquire(&sem, "test", 3, 30_000).unwrap();
        let err = acquire_err(&sem, "test", 5, 30_000);
        assert!(err.contains("max_holders mismatch"));
        assert!(err.contains("existing=3"));
        assert!(err.contains("requested=5"));
    }

    #[test]
    fn test_ssot_after_full_release() {
        let sem = make();
        let h = acquire(&sem, "test", 3, 30_000).unwrap();
        sem.release("test", &h);
        // After full release, entry is cleaned up → new max_holders is OK
        let h2 = acquire(&sem, "test", 5, 30_000).unwrap();
        assert!(!h2.is_empty());
        sem.release("test", &h2);
    }

    // -- TTL expiry --------------------------------------------------------

    #[test]
    fn test_ttl_expiry() {
        let sem = make();
        // Acquire with 1ms TTL
        let h = acquire(&sem, "test", 1, 1).unwrap();
        assert!(!h.is_empty());

        // Wait for expiry
        std::thread::sleep(Duration::from_millis(5));

        // Should succeed (expired holder evicted)
        let h2 = acquire(&sem, "test", 1, 30_000).unwrap();
        assert!(!h2.is_empty());
        sem.release("test", &h2);
    }

    // -- extend ------------------------------------------------------------

    #[test]
    fn test_extend() {
        let sem = make();
        let h = acquire(&sem, "test", 1, 10).unwrap(); // 10ms TTL
        assert!(sem.extend("test", &h, 30_000)); // extend to 30s
        std::thread::sleep(Duration::from_millis(15));
        // Should still be held (extended past 10ms)
        assert!(acquire(&sem, "test", 1, 30_000).is_none());
        sem.release("test", &h);
    }

    #[test]
    fn test_extend_unknown_returns_false() {
        let sem = make();
        assert!(!sem.extend("nonexistent", "fake-id", 30_000));
    }

    #[test]
    fn test_extend_wrong_holder() {
        let sem = make();
        let _h = acquire(&sem, "test", 1, 30_000).unwrap();
        assert!(!sem.extend("test", "wrong-id", 30_000));
    }

    // -- force_release -----------------------------------------------------

    #[test]
    fn test_force_release() {
        let sem = make();
        let _h1 = acquire(&sem, "test", 3, 30_000).unwrap();
        let _h2 = acquire(&sem, "test", 3, 30_000).unwrap();
        assert!(sem.force_release("test"));
        assert_eq!(sem.active_semaphores(), 0);
    }

    #[test]
    fn test_force_release_nonexistent() {
        let sem = make();
        assert!(!sem.force_release("nonexistent"));
    }

    // -- stats -------------------------------------------------------------

    #[test]
    fn test_stats_counters() {
        let sem = make();
        let h = acquire(&sem, "test", 1, 30_000).unwrap();
        sem.release("test", &h);
        assert_eq!(sem.release_count.load(Ordering::Relaxed), 1);
    }

    #[test]
    fn test_active_semaphores() {
        let sem = make();
        assert_eq!(sem.active_semaphores(), 0);
        let h1 = acquire(&sem, "a", 1, 30_000).unwrap();
        let h2 = acquire(&sem, "b", 1, 30_000).unwrap();
        assert_eq!(sem.active_semaphores(), 2);
        sem.release("a", &h1);
        assert_eq!(sem.active_semaphores(), 1);
        sem.release("b", &h2);
        assert_eq!(sem.active_semaphores(), 0);
    }

    // -- concurrent --------------------------------------------------------

    #[test]
    fn test_concurrent_acquire() {
        use rayon::prelude::*;
        use std::sync::atomic::AtomicU32;

        let sem = make();
        let success_count = AtomicU32::new(0);

        (0..100).into_par_iter().for_each(|_| {
            if acquire(&sem, "shared", 5, 30_000).is_some() {
                success_count.fetch_add(1, Ordering::Relaxed);
            }
        });

        // Exactly 5 should succeed (max_holders=5)
        assert_eq!(success_count.load(Ordering::Relaxed), 5);
    }

    #[test]
    fn test_concurrent_mutex() {
        use rayon::prelude::*;
        use std::sync::atomic::AtomicU32;

        let sem = make();
        let success_count = AtomicU32::new(0);

        (0..100).into_par_iter().for_each(|_| {
            if acquire(&sem, "exclusive", 1, 30_000).is_some() {
                success_count.fetch_add(1, Ordering::Relaxed);
            }
        });

        assert_eq!(success_count.load(Ordering::Relaxed), 1);
    }

    // -- empty cleanup -----------------------------------------------------

    #[test]
    fn test_empty_cleanup_on_release() {
        let sem = make();
        let h = acquire(&sem, "temp", 1, 30_000).unwrap();
        assert_eq!(sem.active_semaphores(), 1);
        sem.release("temp", &h);
        assert_eq!(sem.active_semaphores(), 0);
    }

    #[test]
    fn test_empty_cleanup_on_ttl_expiry() {
        let sem = make();
        let _h = acquire(&sem, "temp", 1, 1).unwrap(); // 1ms TTL
        assert_eq!(sem.active_semaphores(), 1);

        std::thread::sleep(Duration::from_millis(5));

        // Next acquire triggers eviction + cleanup
        let h2 = acquire(&sem, "temp", 1, 30_000).unwrap();
        assert!(!h2.is_empty());
        sem.release("temp", &h2);
    }
}
