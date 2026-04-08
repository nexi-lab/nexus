//! PipeManager — owns DT_PIPE buffer registry with blocking wait.
//!
//! `DashMap<String, Arc<dyn PipeBackend>>` enables heterogeneous backends
//! (memory, shared memory, future gRPC proxy).
//!
//! Blocking read/write use `parking_lot::Condvar` + `py.allow_threads()` to
//! release the GIL while waiting. This replaces Python's `ipc_waiter.py`.
//!
//! ## Cancellation
//!
//! `read_blocking()` uses cooperative cancellation via `AtomicBool` flag +
//! 100ms Condvar poll intervals. Python calls `cancel_read(path)` to set the
//! flag; the blocking thread wakes within 100ms and returns `WouldBlock`.
//!
//! PERF NOTE (for future AI): The 100ms poll is a workaround for Python asyncio's
//! inability to interrupt OS threads. When all callers of `read_blocking` are
//! migrated to pure Rust (no Python asyncio involvement), replace the 100ms poll
//! with a single `Condvar::wait_for(full_timeout)` — the Rust caller can simply
//! `notify_all()` on the condvar to cancel, no polling needed. The `cancelled`
//! AtomicBool and `CANCEL_POLL_INTERVAL` can be removed at that point.

use crate::pipe::{MemoryPipeBackend, PipeBackend, PipeError};
use dashmap::DashMap;
use parking_lot::{Condvar, Mutex};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

/// Poll interval for checking cancellation flag during blocking read.
/// See module-level PERF NOTE for removal criteria.
const CANCEL_POLL_INTERVAL: Duration = Duration::from_millis(100);

// ---------------------------------------------------------------------------
// Per-pipe notification (Condvar pair + cancellation flag)
// ---------------------------------------------------------------------------

struct PipeNotify {
    mutex: Mutex<()>,
    not_empty: Condvar,
    not_full: Condvar,
    /// Cooperative cancellation flag. Set by `cancel_read()`, checked by
    /// `read_blocking()` every `CANCEL_POLL_INTERVAL`. Reset on next `create()`.
    cancelled: AtomicBool,
}

impl PipeNotify {
    fn new() -> Self {
        Self {
            mutex: Mutex::new(()),
            not_empty: Condvar::new(),
            not_full: Condvar::new(),
            cancelled: AtomicBool::new(false),
        }
    }
}

// ---------------------------------------------------------------------------
// PipeManager
// ---------------------------------------------------------------------------

/// Registry of active DT_PIPE buffers with blocking wait support.
pub(crate) struct PipeManager {
    buffers: DashMap<String, Arc<dyn PipeBackend>>,
    notify: DashMap<String, Arc<PipeNotify>>,
}

impl PipeManager {
    pub(crate) fn new() -> Self {
        Self {
            buffers: DashMap::new(),
            notify: DashMap::new(),
        }
    }

    /// Create a new in-memory pipe backend and register it.
    pub(crate) fn create(&self, path: &str, capacity: usize) -> Result<(), PipeManagerError> {
        if self.buffers.contains_key(path) {
            return Err(PipeManagerError::Exists(path.to_string()));
        }
        let buf = MemoryPipeBackend::new(capacity);
        self.buffers.insert(path.to_string(), Arc::new(buf));
        self.notify
            .insert(path.to_string(), Arc::new(PipeNotify::new()));
        Ok(())
    }

    /// Register an external backend (SHM, gRPC, etc.).
    #[allow(dead_code)]
    pub(crate) fn register(
        &self,
        path: &str,
        backend: Arc<dyn PipeBackend>,
    ) -> Result<(), PipeManagerError> {
        if self.buffers.contains_key(path) {
            return Err(PipeManagerError::Exists(path.to_string()));
        }
        self.buffers.insert(path.to_string(), backend);
        self.notify
            .insert(path.to_string(), Arc::new(PipeNotify::new()));
        Ok(())
    }

    /// Destroy a pipe — close, cancel waiters, and remove from registry.
    pub(crate) fn destroy(&self, path: &str) -> Result<(), PipeManagerError> {
        match self.buffers.remove(path) {
            Some((_, buf)) => {
                buf.close();
                if let Some((_, n)) = self.notify.remove(path) {
                    n.cancelled.store(true, Ordering::Release);
                    let _guard = n.mutex.lock();
                    n.not_empty.notify_all();
                    n.not_full.notify_all();
                }
                Ok(())
            }
            None => Err(PipeManagerError::NotFound(path.to_string())),
        }
    }

    /// Signal close (keep in registry for drain).
    pub(crate) fn close(&self, path: &str) -> Result<(), PipeManagerError> {
        match self.buffers.get(path) {
            Some(buf) => {
                buf.close();
                if let Some(n) = self.notify.get(path) {
                    let _guard = n.mutex.lock();
                    n.not_empty.notify_all();
                    n.not_full.notify_all();
                }
                Ok(())
            }
            None => Err(PipeManagerError::NotFound(path.to_string())),
        }
    }

    /// Cancel any blocking read on this pipe. The blocked thread will
    /// return `WouldBlock` within `CANCEL_POLL_INTERVAL` (100ms).
    ///
    /// Called by Python when asyncio task is cancelled or during shutdown.
    pub(crate) fn cancel_read(&self, path: &str) {
        if let Some(n) = self.notify.get(path) {
            n.cancelled.store(true, Ordering::Release);
            let _guard = n.mutex.lock();
            n.not_empty.notify_all();
        }
    }

    /// Check if a pipe exists.
    pub(crate) fn has(&self, path: &str) -> bool {
        self.buffers.contains_key(path)
    }

    /// Non-blocking write. Returns bytes written.
    pub(crate) fn write_nowait(&self, path: &str, data: &[u8]) -> Result<usize, PipeManagerError> {
        let buf = self
            .buffers
            .get(path)
            .ok_or_else(|| PipeManagerError::NotFound(path.to_string()))?;
        let n = buf.push(data).map_err(PipeManagerError::Backend)?;
        // Wake blocked readers
        if let Some(notify) = self.notify.get(path) {
            notify.not_empty.notify_one();
        }
        Ok(n)
    }

    /// Non-blocking read. Returns data or None if empty.
    pub(crate) fn read_nowait(&self, path: &str) -> Result<Option<Vec<u8>>, PipeManagerError> {
        let buf = self
            .buffers
            .get(path)
            .ok_or_else(|| PipeManagerError::NotFound(path.to_string()))?;
        match buf.pop() {
            Ok(data) => {
                // Wake blocked writers
                if let Some(notify) = self.notify.get(path) {
                    notify.not_full.notify_one();
                }
                Ok(Some(data))
            }
            Err(PipeError::Empty) => Ok(None),
            Err(PipeError::ClosedEmpty) => Err(PipeManagerError::Closed(path.to_string())),
            Err(e) => Err(PipeManagerError::Backend(e)),
        }
    }

    /// Blocking read — waits for data with Condvar (GIL-free).
    ///
    /// Called via `py.allow_threads()` from PyO3 wrapper (generated_pyo3.rs).
    /// Returns data bytes, `Closed` if pipe closed, or `WouldBlock` on
    /// timeout/cancellation.
    ///
    /// Uses 100ms poll intervals to check the `cancelled` AtomicBool flag,
    /// enabling Python asyncio task cancellation to propagate within 100ms.
    /// See module-level PERF NOTE for future optimization path.
    #[allow(dead_code)]
    pub(crate) fn read_blocking(
        &self,
        path: &str,
        timeout_ms: u64,
    ) -> Result<Vec<u8>, PipeManagerError> {
        let buf = self
            .buffers
            .get(path)
            .ok_or_else(|| PipeManagerError::NotFound(path.to_string()))?;
        let notify = self
            .notify
            .get(path)
            .ok_or_else(|| PipeManagerError::NotFound(path.to_string()))?;

        // Reset cancellation flag (may have been set by a previous cancel)
        notify.cancelled.store(false, Ordering::Release);

        // Fast path: try nowait first
        match buf.pop() {
            Ok(data) => {
                notify.not_full.notify_one();
                return Ok(data);
            }
            Err(PipeError::ClosedEmpty) => return Err(PipeManagerError::Closed(path.to_string())),
            Err(PipeError::Empty) => {}
            Err(e) => return Err(PipeManagerError::Backend(e)),
        }

        // Slow path: poll with short Condvar waits for cancellation support
        let deadline = std::time::Instant::now() + Duration::from_millis(timeout_ms);
        let mut guard = notify.mutex.lock();

        loop {
            // Check cancellation
            if notify.cancelled.load(Ordering::Acquire) {
                return Err(PipeManagerError::WouldBlock(
                    "pipe read cancelled".to_string(),
                ));
            }

            // Try read
            match buf.pop() {
                Ok(data) => {
                    notify.not_full.notify_one();
                    return Ok(data);
                }
                Err(PipeError::ClosedEmpty) => {
                    return Err(PipeManagerError::Closed(path.to_string()));
                }
                Err(PipeError::Empty) => {}
                Err(e) => return Err(PipeManagerError::Backend(e)),
            }

            // Check deadline
            let remaining = deadline.saturating_duration_since(std::time::Instant::now());
            if remaining.is_zero() {
                return Err(PipeManagerError::WouldBlock(
                    "pipe read timeout".to_string(),
                ));
            }

            // Wait for data or cancellation, capped at CANCEL_POLL_INTERVAL
            let wait_dur = remaining.min(CANCEL_POLL_INTERVAL);
            notify.not_empty.wait_for(&mut guard, wait_dur);
        }
    }

    /// Get a backend reference (for sys_read/sys_write fast-path).
    pub(crate) fn get(&self, path: &str) -> Option<Arc<dyn PipeBackend>> {
        self.buffers.get(path).map(|r| Arc::clone(r.value()))
    }

    /// List all pipe paths.
    pub(crate) fn list(&self) -> Vec<String> {
        self.buffers.iter().map(|r| r.key().clone()).collect()
    }

    /// Close all pipes (shutdown).
    pub(crate) fn close_all(&self) {
        for entry in self.buffers.iter() {
            entry.value().close();
        }
        for entry in self.notify.iter() {
            entry.cancelled.store(true, Ordering::Release);
            let _guard = entry.mutex.lock();
            entry.not_empty.notify_all();
            entry.not_full.notify_all();
        }
    }
}

// ---------------------------------------------------------------------------
// PipeManagerError
// ---------------------------------------------------------------------------

#[derive(Debug)]
#[allow(dead_code)]
pub(crate) enum PipeManagerError {
    Exists(String),
    NotFound(String),
    Closed(String),
    WouldBlock(String),
    Backend(PipeError),
}
