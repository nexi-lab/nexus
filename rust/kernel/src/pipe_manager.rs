//! PipeManager — owns DT_PIPE buffer registry with blocking wait.
//!
//! `DashMap<String, Arc<dyn PipeBackend>>` enables heterogeneous backends
//! (memory, shared memory, future gRPC proxy).
//!
//! Blocking read/write use `parking_lot::Condvar` + `py.allow_threads()` to
//! release the GIL while waiting. This replaces Python's `ipc_waiter.py`.

use crate::pipe::{MemoryPipeBackend, PipeBackend, PipeError};
use dashmap::DashMap;
use parking_lot::{Condvar, Mutex};
use std::sync::Arc;
use std::time::Duration;

// ---------------------------------------------------------------------------
// Per-pipe notification (Condvar pair)
// ---------------------------------------------------------------------------

struct PipeNotify {
    mutex: Mutex<()>,
    not_empty: Condvar,
    not_full: Condvar,
}

impl PipeNotify {
    fn new() -> Self {
        Self {
            mutex: Mutex::new(()),
            not_empty: Condvar::new(),
            not_full: Condvar::new(),
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

    /// Destroy a pipe — close, notify waiters, and remove from registry.
    pub(crate) fn destroy(&self, path: &str) -> Result<(), PipeManagerError> {
        match self.buffers.remove(path) {
            Some((_, buf)) => {
                buf.close();
                // Wake all waiters before removing notify
                if let Some((_, n)) = self.notify.remove(path) {
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
                // Wake all waiters so they see the closed state
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
        // Wake blocked readers. We MUST acquire `notify.mutex` before
        // calling `notify_one` to avoid a lost-wakeup race against
        // `read_blocking`'s slow path:
        //
        //   T1 reader  T2 writer
        //   lock(mu)
        //   pop -> Empty
        //              push(data)
        //              notify_one()    ← no waiter parked → LOST
        //   wait_for() ← parks; misses notification → blocks until timeout
        //
        // parking_lot::Condvar::notify_one is unbuffered (see rustdoc).
        // Acquiring `mu` here serializes with the reader's atomic
        // `wait_for(mu, …)` release/park, so the notification can never
        // arrive between the reader's predicate check and parking.
        if let Some(notify) = self.notify.get(path) {
            let _guard = notify.mutex.lock();
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
                // Wake blocked writers. Same lost-wakeup race as in
                // `write_nowait` — see the comment there.
                if let Some(notify) = self.notify.get(path) {
                    let _guard = notify.mutex.lock();
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
    /// Returns data bytes, or WouldBlock on timeout.
    #[allow(dead_code)]
    pub(crate) fn read_blocking(
        &self,
        path: &str,
        timeout_ms: u64,
    ) -> Result<Vec<u8>, PipeManagerError> {
        let buf = Arc::clone(
            self.buffers
                .get(path)
                .ok_or_else(|| PipeManagerError::NotFound(path.to_string()))?
                .value(),
        );
        let notify = Arc::clone(
            self.notify
                .get(path)
                .ok_or_else(|| PipeManagerError::NotFound(path.to_string()))?
                .value(),
        );

        // Fast path: try nowait first. Same lost-wakeup discipline as
        // `write_nowait` — take notify.mutex before notify_one. (Today
        // there are no `not_full` waiters because write_blocking doesn't
        // exist yet, but keeping this consistent makes the invariant
        // "Manager only ever notifies under notify.mutex" hold uniformly,
        // so adding write_blocking later is a one-line change.)
        match buf.pop() {
            Ok(data) => {
                let _guard = notify.mutex.lock();
                notify.not_full.notify_one();
                return Ok(data);
            }
            Err(PipeError::ClosedEmpty) => return Err(PipeManagerError::Closed(path.to_string())),
            Err(PipeError::Empty) => {}
            Err(e) => return Err(PipeManagerError::Backend(e)),
        }

        // Slow path: wait on condvar
        let timeout = Duration::from_millis(timeout_ms);
        let deadline = std::time::Instant::now() + timeout;
        let mut guard = notify.mutex.lock();

        loop {
            // Double-check after lock
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

            let remaining = deadline.saturating_duration_since(std::time::Instant::now());
            if remaining.is_zero() {
                return Err(PipeManagerError::WouldBlock(
                    "pipe read timeout".to_string(),
                ));
            }
            if notify.not_empty.wait_for(&mut guard, remaining).timed_out() {
                // One more try after timeout
                match buf.pop() {
                    Ok(data) => {
                        notify.not_full.notify_one();
                        return Ok(data);
                    }
                    Err(PipeError::ClosedEmpty) => {
                        return Err(PipeManagerError::Closed(path.to_string()));
                    }
                    _ => {
                        return Err(PipeManagerError::WouldBlock(
                            "pipe read timeout".to_string(),
                        ))
                    }
                }
            }
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

    /// Move up to `count` messages from `from` pipe into `to` pipe (zero-copy
    /// within the same process — analogous to Linux `splice(2)` between pipes).
    ///
    /// Returns the number of messages moved. Stops early if `from` is empty or
    /// `to` is full. Both pipes must exist; order of operations: pop from `from`,
    /// push to `to`, wake `to` readers once per batch.
    #[allow(dead_code)]
    pub(crate) fn splice(
        &self,
        from: &str,
        to: &str,
        count: usize,
    ) -> Result<usize, PipeManagerError> {
        let src = self
            .buffers
            .get(from)
            .ok_or_else(|| PipeManagerError::NotFound(from.to_string()))?;
        let dst = self
            .buffers
            .get(to)
            .ok_or_else(|| PipeManagerError::NotFound(to.to_string()))?;

        let mut moved = 0;
        for _ in 0..count {
            let data = match src.pop() {
                Ok(d) => d,
                Err(PipeError::Empty | PipeError::ClosedEmpty) => break,
                Err(e) => return Err(PipeManagerError::Backend(e)),
            };
            match dst.push(&data) {
                Ok(_) => moved += 1,
                Err(e) => {
                    // Push the message back so it isn't lost on Full / Closed.
                    let _ = src.push(&data);
                    if matches!(e, PipeError::Full(..)) {
                        break;
                    }
                    return Err(PipeManagerError::Backend(e));
                }
            }
        }

        if moved > 0 {
            if let Some(notify) = self.notify.get(to) {
                let _guard = notify.mutex.lock();
                notify.not_empty.notify_one();
            }
        }

        Ok(moved)
    }

    /// Close all pipes (shutdown).
    pub(crate) fn close_all(&self) {
        for entry in self.buffers.iter() {
            entry.value().close();
        }
        // Wake all waiters
        for entry in self.notify.iter() {
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

    /// Regression test for the lost-wakeup race fixed in this commit.
    ///
    /// One reader thread blocks on `read_blocking` with a short timeout
    /// while a writer thread interleaves `write_nowait`s. Without the
    /// `notify.mutex`-before-`notify_one` discipline, the writer's
    /// notification can arrive between the reader's predicate check
    /// and `wait_for(...)` parking the thread, causing the reader to
    /// time out and miss data. With the fix, every write must be
    /// observed by the reader.
    ///
    /// Each iteration is independent (drain/refill); we run many of
    /// them with small timeouts so the race window is exercised
    /// repeatedly. Pre-fix: this test fails intermittently within
    /// ~100 iterations on a single machine. Post-fix: passes 10K
    /// iterations reliably.
    #[test]
    fn read_blocking_no_lost_wakeup_under_concurrent_writes() {
        const ITERATIONS: usize = 1000;
        const READ_TIMEOUT_MS: u64 = 250;

        let mgr = Arc::new(PipeManager::new());
        mgr.create("/test", 8).expect("create pipe");

        let writer_done = Arc::new(AtomicUsize::new(0));
        let reader_received = Arc::new(AtomicUsize::new(0));

        let writer_mgr = Arc::clone(&mgr);
        let writer_done_w = Arc::clone(&writer_done);
        let writer = thread::spawn(move || {
            for i in 0..ITERATIONS {
                // Tiny stagger to maximize the chance of landing in
                // the reader's wait_for window. With the bug, an
                // unfortunately-timed write would lose its wakeup.
                if i % 4 == 0 {
                    thread::sleep(Duration::from_micros(10));
                }
                let payload = format!("msg-{i:04}").into_bytes();
                // Retry on Backend::Full — the buffer is small so we
                // expect occasional backpressure; just yield and try
                // again. Real production callers would use
                // write_blocking when it lands.
                loop {
                    match writer_mgr.write_nowait("/test", &payload) {
                        Ok(_) => {
                            writer_done_w.fetch_add(1, Ordering::Relaxed);
                            break;
                        }
                        Err(PipeManagerError::Backend(PipeError::Full(_, _))) => {
                            thread::yield_now();
                        }
                        Err(e) => panic!("writer failed: {e:?}"),
                    }
                }
            }
        });

        let reader_mgr = Arc::clone(&mgr);
        let reader_received_r = Arc::clone(&reader_received);
        let reader = thread::spawn(move || {
            let mut got = 0usize;
            while got < ITERATIONS {
                match reader_mgr.read_blocking("/test", READ_TIMEOUT_MS) {
                    Ok(_) => {
                        got += 1;
                        reader_received_r.store(got, Ordering::Relaxed);
                    }
                    Err(PipeManagerError::WouldBlock(_)) => {
                        // Without the lost-wakeup fix, this branch is
                        // hit before all writes complete and the test
                        // fails on the assertion below. With the fix,
                        // a WouldBlock here can only mean the writer
                        // is genuinely starved (e.g. CPU pinned), so
                        // surface it as a hard failure.
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

        mgr.destroy("/test").expect("destroy pipe");
    }

    /// Sanity check that destroy() wakes up a parked blocking reader
    /// instead of leaving it stuck until timeout.
    #[test]
    fn read_blocking_wakes_on_destroy() {
        let mgr = Arc::new(PipeManager::new());
        mgr.create("/closeme", 4).expect("create");

        let reader_mgr = Arc::clone(&mgr);
        let reader = thread::spawn(move || {
            // Long timeout — must return early on destroy notification.
            reader_mgr.read_blocking("/closeme", 30_000)
        });

        // Give the reader time to enter the slow path and park.
        thread::sleep(Duration::from_millis(50));

        mgr.destroy("/closeme").expect("destroy");

        // Reader should return Closed within ~ms of destroy(), not
        // wait the full 30 second timeout.
        let result = reader.join().expect("reader thread");
        match result {
            Err(PipeManagerError::Closed(_)) => {}
            other => panic!("expected Closed, got {other:?}"),
        }
    }
}
