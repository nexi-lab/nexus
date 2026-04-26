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
        // Wake blocked readers. We MUST acquire `notify.mutex` before
        // calling `notify_all` to avoid a lost-wakeup race against
        // `read_at_blocking`'s slow path:
        //
        //   T1 reader     T2 writer
        //   lock(mu)
        //   read_at -> Empty
        //                 push(data)
        //                 notify_all()    ← no waiter parked → LOST
        //   wait_for() ← parks; misses notification → blocks until timeout
        //
        // parking_lot::Condvar notifications are unbuffered. Acquiring
        // `mu` here serializes with the reader's atomic
        // `wait_for(mu, …)` release/park, so the notification can never
        // arrive between the reader's predicate check and parking.
        if let Some(notify) = self.notify.get(path) {
            let _guard = notify.mutex.lock();
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
        let buf = Arc::clone(
            self.buffers
                .get(path)
                .ok_or_else(|| StreamManagerError::NotFound(path.to_string()))?
                .value(),
        );
        let notify = Arc::clone(
            self.notify
                .get(path)
                .ok_or_else(|| StreamManagerError::NotFound(path.to_string()))?
                .value(),
        );

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

    /// Collect all message payloads from offset 0, concatenated into one Vec.
    ///
    /// Walks the entire stream from the beginning, joining payload bytes
    /// (without the per-frame length prefix). Equivalent to repeated
    /// `read_at` until empty, but in a single Rust call (no per-frame
    /// PyO3 round-trip).
    ///
    /// Returns empty Vec if the stream has no data. Used by Python LLM
    /// backends for `collect_all + CAS persist` pattern after the producer
    /// finishes pumping tokens.
    pub(crate) fn collect_all_payloads(&self, path: &str) -> Result<Vec<u8>, StreamManagerError> {
        let buf = self
            .buffers
            .get(path)
            .ok_or_else(|| StreamManagerError::NotFound(path.to_string()))?;
        let tail = buf.tail_offset();
        let mut out = Vec::with_capacity(tail);
        let mut offset = 0usize;
        loop {
            match buf.read_at(offset) {
                Ok((data, next)) => {
                    out.extend_from_slice(&data);
                    offset = next;
                }
                Err(StreamError::Empty) | Err(StreamError::ClosedEmpty) => break,
                Err(e) => return Err(StreamManagerError::Backend(e)),
            }
        }
        Ok(out)
    }

    /// Get a backend reference (for sys_read/sys_write fast-path).
    pub(crate) fn get(&self, path: &str) -> Option<Arc<dyn StreamBackend>> {
        self.buffers.get(path).map(|r| Arc::clone(r.value()))
    }

    /// Append all entries from `from` (starting at `from_offset`) into `to`.
    ///
    /// Analogous to a read-then-write splice between two DT_STREAMs. `to` must
    /// already exist. Reads `from` with `read_batch` and appends each entry to
    /// `to` with `push`. Returns `(messages_forwarded, next_from_offset)`.
    ///
    /// Non-destructive: `from` is not modified (DT_STREAM reads are always
    /// offset-based, never consuming).
    #[allow(dead_code)]
    pub(crate) fn forward(
        &self,
        from: &str,
        to: &str,
        from_offset: usize,
    ) -> Result<(usize, usize), StreamManagerError> {
        let src = self
            .buffers
            .get(from)
            .ok_or_else(|| StreamManagerError::NotFound(from.to_string()))?;
        let dst = self
            .buffers
            .get(to)
            .ok_or_else(|| StreamManagerError::NotFound(to.to_string()))?;

        let mut offset = from_offset;
        let mut forwarded = 0usize;

        loop {
            match src.read_at(offset) {
                Ok((data, next)) => {
                    dst.push(&data).map_err(StreamManagerError::Backend)?;
                    offset = next;
                    forwarded += 1;
                }
                Err(StreamError::Empty | StreamError::ClosedEmpty) => break,
                Err(e) => return Err(StreamManagerError::Backend(e)),
            }
        }

        if forwarded > 0 {
            if let Some(notify) = self.notify.get(to) {
                let _guard = notify.mutex.lock();
                notify.not_empty.notify_one();
            }
        }

        Ok((forwarded, offset))
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

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Arc;
    use std::thread;
    use std::time::Duration;

    #[test]
    fn test_collect_all_payloads_empty() {
        let sm = StreamManager::new();
        sm.create("/s/empty", 1024).unwrap();
        let data = sm.collect_all_payloads("/s/empty").unwrap();
        assert!(data.is_empty());
    }

    #[test]
    fn test_collect_all_payloads_single() {
        let sm = StreamManager::new();
        sm.create("/s/one", 1024).unwrap();
        sm.write_nowait("/s/one", b"hello").unwrap();
        let data = sm.collect_all_payloads("/s/one").unwrap();
        assert_eq!(data, b"hello");
    }

    #[test]
    fn test_collect_all_payloads_multi() {
        let sm = StreamManager::new();
        sm.create("/s/multi", 4096).unwrap();
        sm.write_nowait("/s/multi", b"aaa").unwrap();
        sm.write_nowait("/s/multi", b"bbb").unwrap();
        sm.write_nowait("/s/multi", b"ccc").unwrap();
        let data = sm.collect_all_payloads("/s/multi").unwrap();
        assert_eq!(data, b"aaabbbccc");
    }

    #[test]
    fn test_collect_all_payloads_after_close() {
        let sm = StreamManager::new();
        sm.create("/s/closed", 1024).unwrap();
        sm.write_nowait("/s/closed", b"before").unwrap();
        sm.close("/s/closed").unwrap();
        let data = sm.collect_all_payloads("/s/closed").unwrap();
        assert_eq!(data, b"before");
    }

    #[test]
    fn test_collect_all_payloads_not_found() {
        let sm = StreamManager::new();
        let result = sm.collect_all_payloads("/s/nope");
        assert!(result.is_err());
    }

    /// Regression test for the lost-wakeup race.
    #[test]
    fn read_at_blocking_no_lost_wakeup_under_concurrent_writes() {
        const ITERATIONS: usize = 1000;
        const READ_TIMEOUT_MS: u64 = 250;

        let mgr = Arc::new(StreamManager::new());
        // Larger capacity than pipe equivalent — stream stores all
        // messages until close, so the buffer must hold all 1000.
        mgr.create("/stream", 64 * 1024).expect("create stream");

        let writer_done = Arc::new(AtomicUsize::new(0));
        let reader_received = Arc::new(AtomicUsize::new(0));

        let writer_mgr = Arc::clone(&mgr);
        let writer_done_w = Arc::clone(&writer_done);
        let writer = thread::spawn(move || {
            for i in 0..ITERATIONS {
                if i % 4 == 0 {
                    thread::sleep(Duration::from_micros(10));
                }
                let payload = format!("msg-{i:04}").into_bytes();
                writer_mgr
                    .write_nowait("/stream", &payload)
                    .expect("write_nowait");
                writer_done_w.fetch_add(1, Ordering::Relaxed);
            }
        });

        let reader_mgr = Arc::clone(&mgr);
        let reader_received_r = Arc::clone(&reader_received);
        let reader = thread::spawn(move || {
            let mut offset = 0usize;
            let mut got = 0usize;
            while got < ITERATIONS {
                match reader_mgr.read_at_blocking("/stream", offset, READ_TIMEOUT_MS) {
                    Ok((_data, next)) => {
                        offset = next;
                        got += 1;
                        reader_received_r.store(got, Ordering::Relaxed);
                    }
                    Err(StreamManagerError::WouldBlock(_)) => {
                        panic!("reader timed out after {got} reads; lost-wakeup race regression?");
                    }
                    Err(e) => panic!("reader failed: {e:?}"),
                }
            }
        });

        writer.join().expect("writer thread");
        reader.join().expect("reader thread");

        assert_eq!(writer_done.load(Ordering::Relaxed), ITERATIONS);
        assert_eq!(reader_received.load(Ordering::Relaxed), ITERATIONS);

        mgr.destroy("/stream").expect("destroy");
    }

    /// Sanity check that destroy() wakes a parked blocking reader
    /// instead of leaving it stuck until timeout.
    #[test]
    fn read_at_blocking_wakes_on_destroy() {
        let mgr = Arc::new(StreamManager::new());
        mgr.create("/closeme", 1024).expect("create");

        let reader_mgr = Arc::clone(&mgr);
        let reader = thread::spawn(move || {
            // Long timeout — must return early on destroy notification.
            reader_mgr.read_at_blocking("/closeme", 0, 30_000)
        });

        thread::sleep(Duration::from_millis(50));

        mgr.destroy("/closeme").expect("destroy");

        let result = reader.join().expect("reader thread");
        match result {
            Err(StreamManagerError::Closed(_)) => {}
            other => panic!("expected Closed, got {other:?}"),
        }
    }
}
