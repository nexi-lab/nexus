//! StreamManager — owns DT_STREAM buffer registry with blocking wait.
//!
//! `DashMap<String, Arc<dyn StreamBackend>>` enables heterogeneous backends
//! (memory, shared memory, future gRPC proxy).
//!
//! Blocking read uses `parking_lot::Condvar` + `py.allow_threads()` to
//! release the GIL while waiting. This replaces Python's `ipc_waiter.py`.

use crate::stream::{MemoryStreamBackend, StreamBackend, StreamError};
use dashmap::DashMap;
use parking_lot::{Condvar, Mutex};
use std::sync::Arc;
use std::time::Duration;

// ---------------------------------------------------------------------------
// Per-stream notification
// ---------------------------------------------------------------------------

struct StreamNotify {
    mutex: Mutex<()>,
    not_empty: Condvar,
}

impl StreamNotify {
    fn new() -> Self {
        Self {
            mutex: Mutex::new(()),
            not_empty: Condvar::new(),
        }
    }
}

// ---------------------------------------------------------------------------
// StreamManager
// ---------------------------------------------------------------------------

/// Registry of active DT_STREAM buffers with blocking wait support.
pub(crate) struct StreamManager {
    buffers: DashMap<String, Arc<dyn StreamBackend>>,
    notify: DashMap<String, Arc<StreamNotify>>,
}

impl StreamManager {
    pub(crate) fn new() -> Self {
        Self {
            buffers: DashMap::new(),
            notify: DashMap::new(),
        }
    }

    /// Create a new in-memory stream backend and register it.
    pub(crate) fn create(&self, path: &str, capacity: usize) -> Result<(), StreamManagerError> {
        if self.buffers.contains_key(path) {
            return Err(StreamManagerError::Exists(path.to_string()));
        }
        let buf = MemoryStreamBackend::new(capacity);
        self.buffers.insert(path.to_string(), Arc::new(buf));
        self.notify
            .insert(path.to_string(), Arc::new(StreamNotify::new()));
        Ok(())
    }

    /// Register an external backend (SHM, gRPC, etc.).
    #[allow(dead_code)]
    pub(crate) fn register(
        &self,
        path: &str,
        backend: Arc<dyn StreamBackend>,
    ) -> Result<(), StreamManagerError> {
        if self.buffers.contains_key(path) {
            return Err(StreamManagerError::Exists(path.to_string()));
        }
        self.buffers.insert(path.to_string(), backend);
        self.notify
            .insert(path.to_string(), Arc::new(StreamNotify::new()));
        Ok(())
    }

    /// Destroy a stream — close, notify waiters, and remove from registry.
    pub(crate) fn destroy(&self, path: &str) -> Result<(), StreamManagerError> {
        match self.buffers.remove(path) {
            Some((_, buf)) => {
                buf.close();
                if let Some((_, n)) = self.notify.remove(path) {
                    let _guard = n.mutex.lock();
                    n.not_empty.notify_all();
                }
                Ok(())
            }
            None => Err(StreamManagerError::NotFound(path.to_string())),
        }
    }

    /// Signal close (keep in registry for drain).
    pub(crate) fn close(&self, path: &str) -> Result<(), StreamManagerError> {
        match self.buffers.get(path) {
            Some(buf) => {
                buf.close();
                if let Some(n) = self.notify.get(path) {
                    let _guard = n.mutex.lock();
                    n.not_empty.notify_all();
                }
                Ok(())
            }
            None => Err(StreamManagerError::NotFound(path.to_string())),
        }
    }

    /// Check if a stream exists.
    pub(crate) fn has(&self, path: &str) -> bool {
        self.buffers.contains_key(path)
    }

    /// Non-blocking write. Returns byte offset.
    pub(crate) fn write_nowait(
        &self,
        path: &str,
        data: &[u8],
    ) -> Result<usize, StreamManagerError> {
        let buf = self
            .buffers
            .get(path)
            .ok_or_else(|| StreamManagerError::NotFound(path.to_string()))?;
        let offset = buf.push(data).map_err(StreamManagerError::Backend)?;
        // Wake blocked readers
        if let Some(notify) = self.notify.get(path) {
            notify.not_empty.notify_all();
        }
        Ok(offset)
    }

    /// Read one message at byte offset. Returns (data, next_offset) or None if empty.
    pub(crate) fn read_at(
        &self,
        path: &str,
        offset: usize,
    ) -> Result<Option<(Vec<u8>, usize)>, StreamManagerError> {
        let buf = self
            .buffers
            .get(path)
            .ok_or_else(|| StreamManagerError::NotFound(path.to_string()))?;
        match buf.read_at(offset) {
            Ok((data, next)) => Ok(Some((data, next))),
            Err(StreamError::Empty) => Ok(None),
            Err(StreamError::ClosedEmpty) => Err(StreamManagerError::Closed(path.to_string())),
            Err(e) => Err(StreamManagerError::Backend(e)),
        }
    }

    /// Blocking read at offset — waits for data with Condvar (GIL-free).
    ///
    /// Called via `py.allow_threads()` from PyO3 wrapper (generated_pyo3.rs).
    #[allow(dead_code)]
    pub(crate) fn read_at_blocking(
        &self,
        path: &str,
        offset: usize,
        timeout_ms: u64,
    ) -> Result<(Vec<u8>, usize), StreamManagerError> {
        let buf = self
            .buffers
            .get(path)
            .ok_or_else(|| StreamManagerError::NotFound(path.to_string()))?;
        let notify = self
            .notify
            .get(path)
            .ok_or_else(|| StreamManagerError::NotFound(path.to_string()))?;

        // Fast path
        match buf.read_at(offset) {
            Ok((data, next)) => return Ok((data, next)),
            Err(StreamError::ClosedEmpty) => {
                return Err(StreamManagerError::Closed(path.to_string()));
            }
            Err(StreamError::Empty) => {}
            Err(e) => return Err(StreamManagerError::Backend(e)),
        }

        // Slow path: wait on condvar
        let timeout = Duration::from_millis(timeout_ms);
        let deadline = std::time::Instant::now() + timeout;
        let mut guard = notify.mutex.lock();

        loop {
            match buf.read_at(offset) {
                Ok((data, next)) => return Ok((data, next)),
                Err(StreamError::ClosedEmpty) => {
                    return Err(StreamManagerError::Closed(path.to_string()));
                }
                Err(StreamError::Empty) => {}
                Err(e) => return Err(StreamManagerError::Backend(e)),
            }

            let remaining = deadline.saturating_duration_since(std::time::Instant::now());
            if remaining.is_zero() {
                return Err(StreamManagerError::WouldBlock(
                    "stream read timeout".to_string(),
                ));
            }
            if notify.not_empty.wait_for(&mut guard, remaining).timed_out() {
                match buf.read_at(offset) {
                    Ok((data, next)) => return Ok((data, next)),
                    Err(StreamError::ClosedEmpty) => {
                        return Err(StreamManagerError::Closed(path.to_string()));
                    }
                    _ => {
                        return Err(StreamManagerError::WouldBlock(
                            "stream read timeout".to_string(),
                        ));
                    }
                }
            }
        }
    }

    /// Read up to `count` messages starting from byte offset.
    pub(crate) fn read_batch(
        &self,
        path: &str,
        offset: usize,
        count: usize,
    ) -> Result<(Vec<Vec<u8>>, usize), StreamManagerError> {
        let buf = self
            .buffers
            .get(path)
            .ok_or_else(|| StreamManagerError::NotFound(path.to_string()))?;
        buf.read_batch(offset, count)
            .map_err(StreamManagerError::Backend)
    }

    /// Get a backend reference (for sys_read/sys_write fast-path).
    pub(crate) fn get(&self, path: &str) -> Option<Arc<dyn StreamBackend>> {
        self.buffers.get(path).map(|r| Arc::clone(r.value()))
    }

    /// List all stream paths.
    pub(crate) fn list(&self) -> Vec<String> {
        self.buffers.iter().map(|r| r.key().clone()).collect()
    }

    /// Close all streams (shutdown).
    pub(crate) fn close_all(&self) {
        for entry in self.buffers.iter() {
            entry.value().close();
        }
        for entry in self.notify.iter() {
            let _guard = entry.mutex.lock();
            entry.not_empty.notify_all();
        }
    }
}

// ---------------------------------------------------------------------------
// StreamManagerError
// ---------------------------------------------------------------------------

#[derive(Debug)]
#[allow(dead_code)]
pub(crate) enum StreamManagerError {
    Exists(String),
    NotFound(String),
    Closed(String),
    WouldBlock(String),
    Backend(StreamError),
}
