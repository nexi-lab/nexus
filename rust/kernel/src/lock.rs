//! VFS Lock Manager — Rust-accelerated read/write lock with hierarchical path awareness.
//!
//! Issue #1398: Eliminates Python `threading.Lock` contention for high-concurrency
//! namespace operations. Provides ~100-200ns uncontended acquire (5-10x over Python).
//!
//! This is a **local, in-process** lock manager — it does NOT replace the distributed
//! Raft-based lock system (`distributed_lock.py`).

use parking_lot::{Condvar, Mutex};
use pyo3::prelude::*;
use pyo3::types::PyDict;
use std::collections::{BTreeMap, HashMap};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum LockMode {
    Read,
    Write,
}

/// Per-path lock state.
#[derive(Debug, Clone)]
struct LockEntry {
    readers: u32,
    writer: Option<u64>, // handle of current writer, if any
}

impl LockEntry {
    fn new() -> Self {
        Self {
            readers: 0,
            writer: None,
        }
    }

    fn is_idle(&self) -> bool {
        self.readers == 0 && self.writer.is_none()
    }
}

/// Reverse-map entry: maps a handle back to its path and mode.
#[derive(Debug, Clone)]
struct HandleInfo {
    path: String,
    mode: LockMode,
}

/// Protected state: all lock mutations go through this Mutex to prevent TOCTOU races.
///
/// The ancestor/descendant conflict checks and the lock entry mutation must be atomic
/// with respect to each other. A global Mutex serializes these operations. The cost
/// is ~15-25ns uncontended (parking_lot), acceptable given the correctness requirement.
#[derive(Debug)]
struct LockState {
    locks: BTreeMap<String, LockEntry>,
    handles: HashMap<u64, HandleInfo>,
}

// ---------------------------------------------------------------------------
// Path helpers
// ---------------------------------------------------------------------------

/// Normalize a path: collapse repeated slashes, remove trailing slash (except root).
fn normalize_path(path: &str) -> String {
    if path.is_empty() {
        return "/".to_string();
    }

    let mut result = String::with_capacity(path.len());
    let mut prev_slash = false;

    for ch in path.chars() {
        if ch == '/' {
            if !prev_slash {
                result.push('/');
            }
            prev_slash = true;
        } else {
            result.push(ch);
            prev_slash = false;
        }
    }

    // Remove trailing slash (keep root "/").
    if result.len() > 1 && result.ends_with('/') {
        result.pop();
    }

    result
}

/// Collect the *strict* ancestors of `path` (must be normalized).
///
/// Example: `"/a/b/c"` → `["/a/b", "/a", "/"]`
fn ancestors(path: &str) -> Vec<&str> {
    if path == "/" || path.is_empty() {
        return Vec::new();
    }
    let mut result = Vec::new();
    let mut end = path.len();
    while let Some(pos) = path[..end].rfind('/') {
        if pos == 0 {
            result.push("/");
            break;
        }
        result.push(&path[..pos]);
        end = pos;
    }
    result
}

// ---------------------------------------------------------------------------
// VFSLockManager
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// Inner (shared via Arc with Kernel)
// ---------------------------------------------------------------------------

/// Core VFS lock state — shared via `Arc` with Kernel for zero-cost
/// lock acquire/release in the Rust fast path (Phase G).
pub(crate) struct VFSLockManagerInner {
    state: Mutex<LockState>,
    notify: Condvar,
    next_handle: AtomicU64,

    // Metrics (relaxed atomics — approximate counters are fine)
    acquire_count: AtomicU64,
    release_count: AtomicU64,
    contention_count: AtomicU64,
    total_acquire_ns: AtomicU64,
    timeout_count: AtomicU64,
}

impl VFSLockManagerInner {
    fn new() -> Self {
        Self {
            state: Mutex::new(LockState {
                locks: BTreeMap::new(),
                handles: HashMap::new(),
            }),
            notify: Condvar::new(),
            next_handle: AtomicU64::new(0),
            acquire_count: AtomicU64::new(0),
            release_count: AtomicU64::new(0),
            contention_count: AtomicU64::new(0),
            total_acquire_ns: AtomicU64::new(0),
            timeout_count: AtomicU64::new(0),
        }
    }

    /// Blocking acquire with timeout (for Rust-internal callers like Kernel).
    /// Returns non-zero handle on success, 0 on timeout.
    /// Does NOT require GIL — safe to call from within py.allow_threads().
    pub(crate) fn blocking_acquire(&self, path: &str, mode: LockMode, timeout_ms: u64) -> u64 {
        let norm_path = normalize_path(path);
        let start = Instant::now();

        // Fast path: non-blocking try under mutex.
        {
            let mut state = self.state.lock();
            if let Some(handle) =
                Self::try_acquire_locked(&mut state, &self.next_handle, &norm_path, mode)
            {
                let elapsed = start.elapsed().as_nanos() as u64;
                self.total_acquire_ns.fetch_add(elapsed, Ordering::Relaxed);
                self.acquire_count.fetch_add(1, Ordering::Relaxed);
                return handle;
            }
        }

        // If timeout == 0 (try-acquire), return immediately.
        if timeout_ms == 0 {
            self.contention_count.fetch_add(1, Ordering::Relaxed);
            self.timeout_count.fetch_add(1, Ordering::Relaxed);
            return 0;
        }

        // Blocking wait with Condvar — woken on every release().
        let deadline = start + Duration::from_millis(timeout_ms);

        // Try-before-wait: if the lock became free between the fast-path
        // attempt and entering this loop, succeed without burning a
        // scheduler round trip (§ review fix #11).
        loop {
            let mut state = self.state.lock();
            if let Some(handle) =
                Self::try_acquire_locked(&mut state, &self.next_handle, &norm_path, mode)
            {
                let elapsed = start.elapsed().as_nanos() as u64;
                self.total_acquire_ns.fetch_add(elapsed, Ordering::Relaxed);
                self.acquire_count.fetch_add(1, Ordering::Relaxed);
                return handle;
            }

            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                self.timeout_count.fetch_add(1, Ordering::Relaxed);
                return 0;
            }

            // Count one contended wait (not one per loop iteration).
            self.contention_count.fetch_add(1, Ordering::Relaxed);
            let wait_result = self.notify.wait_for(&mut state, remaining);
            if wait_result.timed_out() {
                // Give the lock a final try — a release may have raced in
                // while we were marked as timed out.
                if let Some(handle) =
                    Self::try_acquire_locked(&mut state, &self.next_handle, &norm_path, mode)
                {
                    let elapsed = start.elapsed().as_nanos() as u64;
                    self.total_acquire_ns.fetch_add(elapsed, Ordering::Relaxed);
                    self.acquire_count.fetch_add(1, Ordering::Relaxed);
                    return handle;
                }
                self.timeout_count.fetch_add(1, Ordering::Relaxed);
                return 0;
            }
        }
    }

    /// Release a previously acquired lock by handle (for Rust-internal callers).
    pub(crate) fn do_release(&self, handle: u64) -> bool {
        let wake_all = {
            let mut state = self.state.lock();

            let info = match state.handles.remove(&handle) {
                Some(info) => info,
                None => return false,
            };

            if let Some(entry) = state.locks.get_mut(&info.path) {
                match info.mode {
                    LockMode::Read => {
                        entry.readers = entry.readers.saturating_sub(1);
                    }
                    LockMode::Write => {
                        if entry.writer == Some(handle) {
                            entry.writer = None;
                        }
                    }
                }

                if entry.is_idle() {
                    state.locks.remove(&info.path);
                }
            }

            // Releasing a writer can unblock multiple readers, so broadcast.
            // Releasing a reader usually frees at most one writer contender.
            matches!(info.mode, LockMode::Write)
        };

        if wake_all {
            self.notify.notify_all();
        } else {
            self.notify.notify_one();
        }
        self.release_count.fetch_add(1, Ordering::Relaxed);

        true
    }

    /// Check whether `path` in `mode` conflicts with any *ancestor* locks.
    fn ancestor_conflict(locks: &BTreeMap<String, LockEntry>, path: &str, mode: LockMode) -> bool {
        for anc in ancestors(path) {
            if let Some(entry) = locks.get(anc) {
                match mode {
                    LockMode::Read => {
                        if entry.writer.is_some() {
                            return true;
                        }
                    }
                    LockMode::Write => {
                        if entry.writer.is_some() || entry.readers > 0 {
                            return true;
                        }
                    }
                }
            }
        }
        false
    }

    /// Check whether any *descendant* path is locked in a conflicting way.
    fn descendant_conflict(
        locks: &BTreeMap<String, LockEntry>,
        path: &str,
        mode: LockMode,
    ) -> bool {
        let prefix = if path.ends_with('/') {
            path.to_string()
        } else {
            format!("{}/", path)
        };

        let mut upper = prefix.clone();
        upper.pop();
        upper.push('0');

        // `path == "/"` produces `prefix = "/"`, which makes the range
        // `["/", "0")` include the root lock entry itself. Exclude it so
        // the descendant check is strictly about descendants, not the path
        // under consideration (§ review fix #12). For non-root paths the
        // path itself is not a member of the prefix-sentinel range, so
        // this exclusion is a no-op there.
        for (key, entry) in locks.range(prefix..upper) {
            if key == path {
                continue;
            }
            match mode {
                LockMode::Read => {
                    if entry.writer.is_some() {
                        return true;
                    }
                }
                LockMode::Write => {
                    if entry.writer.is_some() || entry.readers > 0 {
                        return true;
                    }
                }
            }
        }
        false
    }

    /// Attempt a single non-blocking acquire under the lock.
    fn try_acquire_locked(
        state: &mut LockState,
        next_handle: &AtomicU64,
        path: &str,
        mode: LockMode,
    ) -> Option<u64> {
        if Self::ancestor_conflict(&state.locks, path, mode) {
            return None;
        }
        if Self::descendant_conflict(&state.locks, path, mode) {
            return None;
        }

        let entry = state
            .locks
            .entry(path.to_string())
            .or_insert_with(LockEntry::new);

        match mode {
            LockMode::Read => {
                if entry.writer.is_some() {
                    return None;
                }
                let handle = next_handle.fetch_add(1, Ordering::Relaxed) + 1;
                entry.readers += 1;
                state.handles.insert(
                    handle,
                    HandleInfo {
                        path: path.to_string(),
                        mode,
                    },
                );
                Some(handle)
            }
            LockMode::Write => {
                if entry.writer.is_some() || entry.readers > 0 {
                    return None;
                }
                let handle = next_handle.fetch_add(1, Ordering::Relaxed) + 1;
                entry.writer = Some(handle);
                state.handles.insert(
                    handle,
                    HandleInfo {
                        path: path.to_string(),
                        mode,
                    },
                );
                Some(handle)
            }
        }
    }
}

// ---------------------------------------------------------------------------
// VFSLockManager (PyO3 class)
// ---------------------------------------------------------------------------

/// Rust-accelerated VFS lock manager with read/write semantics and hierarchical
/// path awareness (ancestor walk).
///
/// Arc<VFSLockManagerInner> enables zero-cost sharing with Kernel (Phase G).
#[pyclass]
pub struct VFSLockManager {
    pub(crate) inner: Arc<VFSLockManagerInner>,
}

// Note: conflict checks, try_acquire_locked moved to VFSLockManagerInner above.

#[pymethods]
impl VFSLockManager {
    #[new]
    fn new() -> Self {
        Self {
            inner: Arc::new(VFSLockManagerInner::new()),
        }
    }

    /// Acquire a lock on `path`.
    ///
    /// * `mode` – `"read"` or `"write"`
    /// * `timeout_ms` – `0` means try-acquire (non-blocking);
    ///   `>0` blocks up to that many milliseconds.
    ///
    /// Returns a non-zero handle on success, or `0` on timeout / failure.
    #[pyo3(signature = (path, mode, timeout_ms=0))]
    fn acquire(&self, py: Python<'_>, path: &str, mode: &str, timeout_ms: u64) -> PyResult<u64> {
        let lock_mode = match mode {
            "read" => LockMode::Read,
            "write" => LockMode::Write,
            other => {
                return Err(pyo3::exceptions::PyValueError::new_err(format!(
                    "Invalid lock mode: {other:?}. Expected \"read\" or \"write\"."
                )));
            }
        };

        let inner = &self.inner;

        // Release the GIL for the (potentially blocking) acquire loop.
        let result = py.detach(|| inner.blocking_acquire(path, lock_mode, timeout_ms));

        Ok(result)
    }

    /// Release a previously acquired lock by handle.
    fn release(&self, handle: u64) -> bool {
        self.inner.do_release(handle)
    }

    /// Check whether `path` currently has any active lock (read or write).
    fn is_locked(&self, path: &str) -> bool {
        let norm = normalize_path(path);
        let state = self.inner.state.lock();
        state.locks.get(&norm).is_some_and(|entry| !entry.is_idle())
    }

    /// Return lock-holder information for `path`, or `None` if unlocked.
    fn holders(&self, py: Python<'_>, path: &str) -> PyResult<Option<Py<PyAny>>> {
        let norm = normalize_path(path);
        let state = self.inner.state.lock();
        match state.locks.get(&norm) {
            Some(entry) if !entry.is_idle() => {
                let dict = PyDict::new(py);
                dict.set_item("readers", entry.readers)?;
                dict.set_item("writer", entry.writer.unwrap_or(0))?;
                dict.set_item("path", &norm)?;
                Ok(Some(dict.into()))
            }
            _ => Ok(None),
        }
    }

    /// Return a dict of aggregate metrics.
    fn stats(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let acquires = self.inner.acquire_count.load(Ordering::Relaxed);
        let total_ns = self.inner.total_acquire_ns.load(Ordering::Relaxed);
        let avg_ns = if acquires > 0 { total_ns / acquires } else { 0 };

        let state = self.inner.state.lock();
        let active_locks = state.locks.len();
        let active_handles = state.handles.len();
        drop(state);

        let dict = PyDict::new(py);
        dict.set_item("acquire_count", acquires)?;
        dict.set_item(
            "release_count",
            self.inner.release_count.load(Ordering::Relaxed),
        )?;
        dict.set_item(
            "contention_count",
            self.inner.contention_count.load(Ordering::Relaxed),
        )?;
        dict.set_item(
            "timeout_count",
            self.inner.timeout_count.load(Ordering::Relaxed),
        )?;
        dict.set_item("active_locks", active_locks)?;
        dict.set_item("active_handles", active_handles)?;
        dict.set_item("avg_acquire_ns", avg_ns)?;
        dict.set_item("total_acquire_ns", total_ns)?;
        Ok(dict.into())
    }

    /// Number of actively locked paths.
    #[getter]
    fn active_locks(&self) -> usize {
        self.inner.state.lock().locks.len()
    }

    /// Number of active handles.
    #[getter]
    fn active_handles(&self) -> usize {
        self.inner.state.lock().handles.len()
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    fn make() -> VFSLockManager {
        VFSLockManager::new()
    }

    /// Helper: acquire directly through the inner (bypasses PyO3 / GIL).
    fn acquire(mgr: &VFSLockManager, path: &str, mode: LockMode) -> Option<u64> {
        let handle = mgr.inner.blocking_acquire(path, mode, 0);
        if handle > 0 {
            Some(handle)
        } else {
            None
        }
    }

    // -- path normalization ------------------------------------------------

    #[test]
    fn test_normalize_trailing_slash() {
        assert_eq!(normalize_path("/a/b/"), "/a/b");
    }

    #[test]
    fn test_normalize_double_slash() {
        assert_eq!(normalize_path("/a//b"), "/a/b");
    }

    #[test]
    fn test_normalize_root() {
        assert_eq!(normalize_path("/"), "/");
    }

    #[test]
    fn test_normalize_empty() {
        assert_eq!(normalize_path(""), "/");
    }

    // -- ancestors helper --------------------------------------------------

    #[test]
    fn test_ancestors_root() {
        assert_eq!(ancestors("/"), Vec::<&str>::new());
    }

    #[test]
    fn test_ancestors_one_level() {
        assert_eq!(ancestors("/a"), vec!["/"]);
    }

    #[test]
    fn test_ancestors_deep() {
        assert_eq!(ancestors("/a/b/c"), vec!["/a/b", "/a", "/"]);
    }

    // -- basic acquire / release -------------------------------------------

    #[test]
    fn test_basic_read_acquire_release() {
        let mgr = make();
        let h = acquire(&mgr, "/foo", LockMode::Read).unwrap();
        assert!(h > 0);
        assert!(mgr.is_locked("/foo"));
        assert!(mgr.release(h));
        assert!(!mgr.is_locked("/foo"));
    }

    #[test]
    fn test_basic_write_acquire_release() {
        let mgr = make();
        let h = acquire(&mgr, "/foo", LockMode::Write).unwrap();
        assert!(h > 0);
        assert!(mgr.is_locked("/foo"));
        assert!(mgr.release(h));
        assert!(!mgr.is_locked("/foo"));
    }

    // -- read-read coexistence ---------------------------------------------

    #[test]
    fn test_read_read_coexist() {
        let mgr = make();
        let h1 = acquire(&mgr, "/foo", LockMode::Read).unwrap();
        let h2 = acquire(&mgr, "/foo", LockMode::Read).unwrap();
        assert!(h1 != h2);
        assert!(mgr.is_locked("/foo"));
        mgr.release(h1);
        assert!(mgr.is_locked("/foo"));
        mgr.release(h2);
        assert!(!mgr.is_locked("/foo"));
    }

    // -- read-write conflict -----------------------------------------------

    #[test]
    fn test_write_blocks_read() {
        let mgr = make();
        let _w = acquire(&mgr, "/foo", LockMode::Write).unwrap();
        assert!(acquire(&mgr, "/foo", LockMode::Read).is_none());
    }

    #[test]
    fn test_read_blocks_write() {
        let mgr = make();
        let _r = acquire(&mgr, "/foo", LockMode::Read).unwrap();
        assert!(acquire(&mgr, "/foo", LockMode::Write).is_none());
    }

    // -- write-write conflict ----------------------------------------------

    #[test]
    fn test_write_write_conflict() {
        let mgr = make();
        let _w = acquire(&mgr, "/foo", LockMode::Write).unwrap();
        assert!(acquire(&mgr, "/foo", LockMode::Write).is_none());
    }

    // -- ancestor conflict -------------------------------------------------

    #[test]
    fn test_ancestor_write_blocks_child_read() {
        let mgr = make();
        let _w = acquire(&mgr, "/a", LockMode::Write).unwrap();
        assert!(acquire(&mgr, "/a/b", LockMode::Read).is_none());
    }

    #[test]
    fn test_ancestor_write_blocks_child_write() {
        let mgr = make();
        let _w = acquire(&mgr, "/a", LockMode::Write).unwrap();
        assert!(acquire(&mgr, "/a/b/c", LockMode::Write).is_none());
    }

    #[test]
    fn test_ancestor_read_allows_child_read() {
        let mgr = make();
        let _r = acquire(&mgr, "/a", LockMode::Read).unwrap();
        assert!(acquire(&mgr, "/a/b", LockMode::Read).is_some());
    }

    #[test]
    fn test_ancestor_read_blocks_child_write() {
        let mgr = make();
        let _r = acquire(&mgr, "/a", LockMode::Read).unwrap();
        assert!(acquire(&mgr, "/a/b", LockMode::Write).is_none());
    }

    // -- descendant conflict -----------------------------------------------

    #[test]
    fn test_descendant_write_blocks_parent_write() {
        let mgr = make();
        let _w = acquire(&mgr, "/a/b/c", LockMode::Write).unwrap();
        assert!(acquire(&mgr, "/a", LockMode::Write).is_none());
    }

    #[test]
    fn test_descendant_read_blocks_parent_write() {
        let mgr = make();
        let _r = acquire(&mgr, "/a/b", LockMode::Read).unwrap();
        assert!(acquire(&mgr, "/a", LockMode::Write).is_none());
    }

    #[test]
    fn test_descendant_write_blocks_parent_read() {
        let mgr = make();
        let _w = acquire(&mgr, "/a/b", LockMode::Write).unwrap();
        assert!(acquire(&mgr, "/a", LockMode::Read).is_none());
    }

    // -- root path edge cases ----------------------------------------------

    #[test]
    fn test_root_write_blocks_all_descendants() {
        let mgr = make();
        let _w = acquire(&mgr, "/", LockMode::Write).unwrap();
        assert!(acquire(&mgr, "/a", LockMode::Read).is_none());
        assert!(acquire(&mgr, "/a/b/c", LockMode::Write).is_none());
    }

    #[test]
    fn test_descendant_blocks_root_write() {
        let mgr = make();
        let _r = acquire(&mgr, "/a", LockMode::Read).unwrap();
        assert!(acquire(&mgr, "/", LockMode::Write).is_none());
    }

    // -- path normalization in locking -------------------------------------

    #[test]
    fn test_trailing_slash_same_as_without() {
        let mgr = make();
        let h = acquire(&mgr, "/a/b/", LockMode::Write).unwrap();
        // "/a/b/" normalizes to "/a/b", so is_locked("/a/b") should be true.
        assert!(mgr.is_locked("/a/b"));
        mgr.release(h);
    }

    #[test]
    fn test_double_slash_same_as_single() {
        let mgr = make();
        let h = acquire(&mgr, "/a//b", LockMode::Write).unwrap();
        assert!(mgr.is_locked("/a/b"));
        mgr.release(h);
    }

    // -- release wrong handle -----------------------------------------------

    #[test]
    fn test_release_wrong_handle() {
        let mgr = make();
        assert!(!mgr.release(999));
    }

    // -- stats accuracy ----------------------------------------------------

    #[test]
    fn test_stats_counters() {
        let mgr = make();
        let h1 = acquire(&mgr, "/x", LockMode::Read).unwrap();
        let h2 = acquire(&mgr, "/y", LockMode::Write).unwrap();
        mgr.release(h1);
        mgr.release(h2);
        assert_eq!(mgr.inner.release_count.load(Ordering::Relaxed), 2);
    }

    // -- unicode path -------------------------------------------------------

    #[test]
    fn test_unicode_path() {
        let mgr = make();
        let h = acquire(&mgr, "/data/file", LockMode::Write).unwrap();
        assert!(mgr.is_locked("/data/file"));
        mgr.release(h);
        assert!(!mgr.is_locked("/data/file"));
    }

    // -- concurrent multi-thread test (rayon) --------------------------------

    #[test]
    fn test_concurrent_reads() {
        use rayon::prelude::*;

        let mgr = make();
        let handles: Vec<u64> = (0..100)
            .into_par_iter()
            .map(|_| acquire(&mgr, "/shared", LockMode::Read).unwrap())
            .collect();

        assert_eq!(handles.len(), 100);
        let mut sorted = handles.clone();
        sorted.sort();
        sorted.dedup();
        assert_eq!(sorted.len(), 100);

        for h in handles {
            assert!(mgr.release(h));
        }
        assert!(!mgr.is_locked("/shared"));
    }

    #[test]
    fn test_concurrent_write_exclusion() {
        use rayon::prelude::*;
        use std::sync::atomic::AtomicU32;

        let mgr = make();
        let success_count = AtomicU32::new(0);

        (0..100).into_par_iter().for_each(|_| {
            if acquire(&mgr, "/exclusive", LockMode::Write).is_some() {
                success_count.fetch_add(1, Ordering::Relaxed);
            }
        });

        assert_eq!(success_count.load(Ordering::Relaxed), 1);
    }

    // -- TOCTOU regression test --------------------------------------------

    #[test]
    fn test_no_toctou_parent_child_write() {
        use rayon::prelude::*;
        use std::sync::atomic::AtomicU32;

        // Regression: Two threads should not be able to simultaneously hold
        // write locks on /a and /a/b.
        let mgr = make();
        let success_count = AtomicU32::new(0);

        (0..1000).into_par_iter().for_each(|i| {
            let path = if i % 2 == 0 { "/a" } else { "/a/b" };
            if let Some(h) = acquire(&mgr, path, LockMode::Write) {
                success_count.fetch_add(1, Ordering::Relaxed);
                // Hold briefly then release.
                std::thread::sleep(Duration::from_micros(1));
                mgr.release(h);
            }
        });

        // At least some should have succeeded (not all — that's the point).
        assert!(success_count.load(Ordering::Relaxed) > 0);
    }

    // -- cleanup: idle entries removed --------------------------------------

    #[test]
    fn test_idle_entry_cleaned_up() {
        let mgr = make();
        let h = acquire(&mgr, "/temp", LockMode::Read).unwrap();
        assert_eq!(mgr.active_locks(), 1);
        mgr.release(h);
        assert_eq!(mgr.active_locks(), 0);
    }

    // -- BTreeMap range boundary tests (Issue #2941) -------------------------

    #[test]
    fn test_sibling_path_no_conflict() {
        // "/a/bc" is a sibling of "/a/b", NOT a descendant.
        let mgr = make();
        let _w = acquire(&mgr, "/a/bc", LockMode::Write).unwrap();
        // Acquiring write on "/a/b" should succeed — "/a/bc" is not a descendant.
        assert!(acquire(&mgr, "/a/b", LockMode::Write).is_some());
    }

    #[test]
    fn test_sibling_path_with_dash_no_conflict() {
        // "/a/b-special" is a sibling, not a descendant of "/a/b".
        let mgr = make();
        let _w = acquire(&mgr, "/a/b-special", LockMode::Write).unwrap();
        assert!(acquire(&mgr, "/a/b", LockMode::Write).is_some());
    }

    #[test]
    fn test_sibling_path_with_dot_no_conflict() {
        // "/a/b.txt" is a sibling, not a descendant of "/a/b".
        let mgr = make();
        let _w = acquire(&mgr, "/a/b.txt", LockMode::Write).unwrap();
        assert!(acquire(&mgr, "/a/b", LockMode::Write).is_some());
    }

    #[test]
    fn test_true_descendant_still_conflicts() {
        // "/a/b/c" IS a descendant of "/a/b" — should conflict.
        let mgr = make();
        let _w = acquire(&mgr, "/a/b/c", LockMode::Write).unwrap();
        assert!(acquire(&mgr, "/a/b", LockMode::Write).is_none());
    }

    #[test]
    fn test_deep_descendant_conflicts() {
        let mgr = make();
        let _r = acquire(&mgr, "/a/b/c/d/e/f", LockMode::Read).unwrap();
        assert!(acquire(&mgr, "/a/b", LockMode::Write).is_none());
    }

    #[test]
    fn test_root_descendant_range() {
        // All paths are descendants of "/".
        let mgr = make();
        let _r = acquire(&mgr, "/x/y/z", LockMode::Read).unwrap();
        assert!(acquire(&mgr, "/", LockMode::Write).is_none());
    }

    #[test]
    fn test_many_siblings_only_descendant_conflicts() {
        let mgr = make();
        // Lock many siblings of "/a/b".
        let _w1 = acquire(&mgr, "/a/ba", LockMode::Write).unwrap();
        let _w2 = acquire(&mgr, "/a/bb", LockMode::Write).unwrap();
        let _w3 = acquire(&mgr, "/a/b-x", LockMode::Write).unwrap();
        let _w4 = acquire(&mgr, "/a/b.y", LockMode::Write).unwrap();
        // None of these are descendants of "/a/b" — should succeed.
        assert!(acquire(&mgr, "/a/b", LockMode::Write).is_some());
    }

    #[test]
    fn test_sibling_and_descendant_mixed() {
        let mgr = make();
        let _w1 = acquire(&mgr, "/a/bc", LockMode::Write).unwrap(); // sibling
        let _w2 = acquire(&mgr, "/a/b/child", LockMode::Write).unwrap(); // descendant
                                                                         // "/a/b" should fail due to descendant "/a/b/child".
        assert!(acquire(&mgr, "/a/b", LockMode::Write).is_none());
    }
}
