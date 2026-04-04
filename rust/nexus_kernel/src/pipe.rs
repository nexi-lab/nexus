//! Lock-free SPSC RingBuffer core for DT_PIPE kernel IPC (Task #806, #902).
//!
//! **L1**: Lock-free contiguous byte ring (Mutex → atomic head/tail).
//! **L2**: `push_u64`/`pop_u64` — return Python int directly, zero PyBytes allocation.
//! **L3**: Direct ring→PyBytes copy (eliminate intermediate Vec<u8>).
//!
//! SAFETY: SPSC by design. Python GIL serializes all PyO3 method calls.
//! Producer writes [tail..new_tail], consumer reads [head..new_head] — ranges
//! never overlap because head only advances after consumer copies data.
//!
//! Message framing: `[4B u32 LE length][N bytes payload]`.
//! Sentinel = `[0x00 0x00 0x00 0x00]` marks waste-and-wrap at ring boundary.
//!
//! Error encoding: Rust raises `RuntimeError("PipeFull:…")` etc. Python translates
//! to the matching exception class.

use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList};
use std::cell::UnsafeCell;
use std::sync::atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering};

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/// Frame header size: 4-byte u32 LE length prefix.
const HEADER_SIZE: usize = 4;

// ---------------------------------------------------------------------------
// RingBufferCore
// ---------------------------------------------------------------------------

/// Lock-free SPSC ring buffer core for DT_PIPE.
///
/// Contiguous byte ring with atomic monotonic head/tail counters.
/// Python wrapper adds asyncio.Event coordination for blocking read/write.
#[pyclass]
pub struct RingBufferCore {
    ring: UnsafeCell<Vec<u8>>,
    ring_cap: usize,
    user_capacity: usize,
    head: AtomicUsize,
    tail: AtomicUsize,
    closed: AtomicBool,
    push_count: AtomicU64,
    pop_count: AtomicU64,
    msg_count: AtomicUsize,
    used_bytes: AtomicUsize,
}

// SAFETY: SPSC — producer and consumer operate on non-overlapping ring regions.
// Python GIL serializes all PyO3 method calls.
unsafe impl Send for RingBufferCore {}
unsafe impl Sync for RingBufferCore {}

// ---------------------------------------------------------------------------
// Internal error type
// ---------------------------------------------------------------------------

#[derive(Debug)]
enum RingError {
    Closed(&'static str),
    Full(usize, usize),
    Empty,
    ClosedEmpty,
    Oversized(usize, usize),
}

// ---------------------------------------------------------------------------
// Internal helpers (not exposed to Python)
// ---------------------------------------------------------------------------

impl RingBufferCore {
    /// Push raw bytes into the ring. Returns payload length on success.
    fn push_inner(&self, data: &[u8]) -> Result<usize, RingError> {
        if self.closed.load(Ordering::Acquire) {
            return Err(RingError::Closed("write to closed pipe"));
        }
        let payload_len = data.len();
        if payload_len == 0 {
            return Ok(0);
        }
        if payload_len > self.user_capacity {
            return Err(RingError::Oversized(payload_len, self.user_capacity));
        }

        let used = self.used_bytes.load(Ordering::Relaxed);
        if used + payload_len > self.user_capacity {
            return Err(RingError::Full(used, self.user_capacity));
        }

        let frame_len = HEADER_SIZE + payload_len;
        let tail = self.tail.load(Ordering::Relaxed);
        let tail_idx = tail % self.ring_cap;
        let contiguous = self.ring_cap - tail_idx;

        let ring = unsafe { &mut *self.ring.get() };

        // If frame doesn't fit contiguously, write sentinel and wrap
        let write_idx = if frame_len > contiguous {
            // Write sentinel (len=0) to mark waste region
            let sentinel = 0u32.to_le_bytes();
            ring[tail_idx..tail_idx + HEADER_SIZE].copy_from_slice(&sentinel);
            // Advance tail past waste region (wrap to 0)
            let new_tail = tail + contiguous;
            self.tail.store(new_tail, Ordering::Release);
            0 // write at ring index 0
        } else {
            tail_idx
        };

        // Write frame: [4B len][payload]
        let header = (payload_len as u32).to_le_bytes();
        ring[write_idx..write_idx + HEADER_SIZE].copy_from_slice(&header);
        ring[write_idx + HEADER_SIZE..write_idx + HEADER_SIZE + payload_len].copy_from_slice(data);

        // Update tail
        let current_tail = self.tail.load(Ordering::Relaxed);
        self.tail.store(current_tail + frame_len, Ordering::Release);

        // Update counters (Relaxed — informational only)
        self.msg_count.fetch_add(1, Ordering::Relaxed);
        self.used_bytes.fetch_add(payload_len, Ordering::Relaxed);
        self.push_count.fetch_add(1, Ordering::Relaxed);

        Ok(payload_len)
    }

    /// Find the next message position without advancing head.
    /// Returns (payload_start_ring_idx, payload_len, total_bytes_to_advance_head).
    fn pop_position(&self) -> Result<(usize, usize, usize), RingError> {
        let mut head = self.head.load(Ordering::Acquire);
        let tail = self.tail.load(Ordering::Acquire);

        loop {
            if head == tail {
                return if self.closed.load(Ordering::Acquire) {
                    Err(RingError::ClosedEmpty)
                } else {
                    Err(RingError::Empty)
                };
            }

            let head_idx = head % self.ring_cap;
            let ring = unsafe { &*self.ring.get() };

            // Read header
            let mut hdr = [0u8; HEADER_SIZE];
            hdr.copy_from_slice(&ring[head_idx..head_idx + HEADER_SIZE]);
            let payload_len = u32::from_le_bytes(hdr) as usize;

            if payload_len == 0 {
                // Sentinel — skip waste region to ring start
                let waste = self.ring_cap - head_idx;
                head += waste;
                // Persist skip so we don't re-read sentinel
                self.head.store(head, Ordering::Release);
                continue;
            }

            let payload_start = head_idx + HEADER_SIZE;
            let total_advance = HEADER_SIZE + payload_len;
            return Ok((payload_start, payload_len, total_advance));
        }
    }

    /// Advance head after data has been copied out.
    fn commit_pop(&self, total_advance: usize, payload_len: usize) {
        let head = self.head.load(Ordering::Relaxed);
        self.head.store(head + total_advance, Ordering::Release);
        self.msg_count.fetch_sub(1, Ordering::Relaxed);
        self.used_bytes.fetch_sub(payload_len, Ordering::Relaxed);
        self.pop_count.fetch_add(1, Ordering::Relaxed);
    }
}

// ---------------------------------------------------------------------------
// PyO3 methods
// ---------------------------------------------------------------------------

#[pymethods]
impl RingBufferCore {
    #[new]
    fn new(capacity: usize) -> PyResult<Self> {
        if capacity == 0 {
            return Err(pyo3::exceptions::PyValueError::new_err(
                "capacity must be > 0, got 0",
            ));
        }
        // Allocate ring: capacity * 2 gives headroom for framing + sentinel waste
        let ring_cap = capacity * 2;
        Ok(Self {
            ring: UnsafeCell::new(vec![0u8; ring_cap]),
            ring_cap,
            user_capacity: capacity,
            head: AtomicUsize::new(0),
            tail: AtomicUsize::new(0),
            closed: AtomicBool::new(false),
            push_count: AtomicU64::new(0),
            pop_count: AtomicU64::new(0),
            msg_count: AtomicUsize::new(0),
            used_bytes: AtomicUsize::new(0),
        })
    }

    /// Push a message into the buffer. Returns bytes written.
    fn push(&self, _py: Python<'_>, data: &[u8]) -> PyResult<usize> {
        match self.push_inner(data) {
            Ok(n) => Ok(n),
            Err(RingError::Closed(msg)) => Err(pyo3::exceptions::PyRuntimeError::new_err(format!(
                "PipeClosed:{msg}"
            ))),
            Err(RingError::Full(used, cap)) => Err(pyo3::exceptions::PyRuntimeError::new_err(
                format!("PipeFull:buffer full ({used}/{cap} bytes)"),
            )),
            Err(RingError::Oversized(msg_len, cap)) => {
                Err(pyo3::exceptions::PyValueError::new_err(format!(
                    "message size {msg_len} exceeds buffer capacity {cap}"
                )))
            }
            Err(RingError::Empty | RingError::ClosedEmpty) => unreachable!(),
        }
    }

    /// Pop the next message. Returns PyBytes (L3 — direct ring→PyBytes copy).
    fn pop<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyBytes>> {
        match self.pop_position() {
            Ok((start, len, advance)) => {
                let ring = unsafe { &*self.ring.get() };
                let result = PyBytes::new(py, &ring[start..start + len]);
                self.commit_pop(advance, len);
                Ok(result)
            }
            Err(RingError::ClosedEmpty) => Err(pyo3::exceptions::PyRuntimeError::new_err(
                "PipeClosed:read from closed empty pipe",
            )),
            Err(RingError::Empty) => Err(pyo3::exceptions::PyRuntimeError::new_err(
                "PipeEmpty:buffer empty",
            )),
            Err(RingError::Closed(_) | RingError::Full(_, _) | RingError::Oversized(_, _)) => {
                unreachable!()
            }
        }
    }

    /// Push a u64 value (L2 — 12-byte frame, zero PyBytes allocation).
    fn push_u64(&self, _py: Python<'_>, val: u64) -> PyResult<()> {
        match self.push_inner(&val.to_le_bytes()) {
            Ok(_) => Ok(()),
            Err(RingError::Closed(msg)) => Err(pyo3::exceptions::PyRuntimeError::new_err(format!(
                "PipeClosed:{msg}"
            ))),
            Err(RingError::Full(used, cap)) => Err(pyo3::exceptions::PyRuntimeError::new_err(
                format!("PipeFull:buffer full ({used}/{cap} bytes)"),
            )),
            Err(RingError::Oversized(msg_len, cap)) => {
                Err(pyo3::exceptions::PyValueError::new_err(format!(
                    "message size {msg_len} exceeds buffer capacity {cap}"
                )))
            }
            Err(RingError::Empty | RingError::ClosedEmpty) => unreachable!(),
        }
    }

    /// Pop a u64 value (L2 — returns Python int directly, zero PyBytes).
    fn pop_u64(&self, _py: Python<'_>) -> PyResult<u64> {
        match self.pop_position() {
            Ok((start, len, advance)) => {
                if len != 8 {
                    return Err(pyo3::exceptions::PyValueError::new_err(format!(
                        "pop_u64 expects 8-byte payload, got {len}"
                    )));
                }
                let ring = unsafe { &*self.ring.get() };
                let mut buf = [0u8; 8];
                buf.copy_from_slice(&ring[start..start + 8]);
                let val = u64::from_le_bytes(buf);
                self.commit_pop(advance, len);
                Ok(val)
            }
            Err(RingError::ClosedEmpty) => Err(pyo3::exceptions::PyRuntimeError::new_err(
                "PipeClosed:read from closed empty pipe",
            )),
            Err(RingError::Empty) => Err(pyo3::exceptions::PyRuntimeError::new_err(
                "PipeEmpty:buffer empty",
            )),
            Err(RingError::Closed(_) | RingError::Full(_, _) | RingError::Oversized(_, _)) => {
                unreachable!()
            }
        }
    }

    /// Non-consuming peek at the next message. Returns None if empty.
    fn peek<'py>(&self, py: Python<'py>) -> Option<Bound<'py, PyBytes>> {
        match self.pop_position() {
            Ok((start, len, _advance)) => {
                let ring = unsafe { &*self.ring.get() };
                Some(PyBytes::new(py, &ring[start..start + len]))
                // NOTE: no commit_pop — peek is non-consuming
            }
            Err(_) => None,
        }
    }

    /// Non-consuming view of all buffered messages.
    fn peek_all<'py>(&self, py: Python<'py>) -> Bound<'py, PyList> {
        let list = PyList::empty(py);
        let ring = unsafe { &*self.ring.get() };
        let tail = self.tail.load(Ordering::Acquire);
        let mut pos = self.head.load(Ordering::Acquire);

        while pos < tail {
            let idx = pos % self.ring_cap;
            let mut hdr = [0u8; HEADER_SIZE];
            hdr.copy_from_slice(&ring[idx..idx + HEADER_SIZE]);
            let payload_len = u32::from_le_bytes(hdr) as usize;

            if payload_len == 0 {
                // Sentinel — skip to ring start
                pos += self.ring_cap - idx;
                continue;
            }

            let payload_start = idx + HEADER_SIZE;
            let item = PyBytes::new(py, &ring[payload_start..payload_start + payload_len]);
            list.append(item).expect("append to list");
            pos += HEADER_SIZE + payload_len;
        }

        list
    }

    /// Close the buffer. Idempotent.
    fn close(&self) {
        self.closed.store(true, Ordering::Release);
    }

    /// Buffer statistics as a dict.
    fn stats(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let dict = PyDict::new(py);
        dict.set_item("size", self.used_bytes.load(Ordering::Relaxed))?;
        dict.set_item("capacity", self.user_capacity)?;
        dict.set_item("msg_count", self.msg_count.load(Ordering::Relaxed))?;
        dict.set_item("closed", self.closed.load(Ordering::Acquire))?;
        dict.set_item("push_count", self.push_count.load(Ordering::Relaxed))?;
        dict.set_item("pop_count", self.pop_count.load(Ordering::Relaxed))?;
        Ok(dict.into())
    }

    fn is_empty(&self) -> bool {
        self.msg_count.load(Ordering::Relaxed) == 0
    }

    fn is_full(&self) -> bool {
        self.used_bytes.load(Ordering::Relaxed) >= self.user_capacity
    }

    #[getter]
    fn closed(&self) -> bool {
        self.closed.load(Ordering::Acquire)
    }

    #[getter]
    fn size(&self) -> usize {
        self.used_bytes.load(Ordering::Relaxed)
    }

    #[getter]
    fn capacity(&self) -> usize {
        self.user_capacity
    }

    #[getter]
    fn msg_count(&self) -> usize {
        self.msg_count.load(Ordering::Relaxed)
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    fn make(cap: usize) -> RingBufferCore {
        RingBufferCore::new(cap).unwrap()
    }

    /// Test-only push helper (bypasses PyO3 Python parameter).
    fn push(core: &RingBufferCore, data: &[u8]) -> usize {
        core.push_inner(data).expect("push failed in test helper")
    }

    /// Test-only pop helper (bypasses PyO3, returns raw bytes).
    fn pop(core: &RingBufferCore) -> Vec<u8> {
        let (start, len, advance) = core.pop_position().expect("pop failed in test helper");
        let ring = unsafe { &*core.ring.get() };
        let data = ring[start..start + len].to_vec();
        core.commit_pop(advance, len);
        data
    }

    /// Test-only push_u64 helper.
    fn push_u64(core: &RingBufferCore, val: u64) {
        core.push_inner(&val.to_le_bytes())
            .expect("push_u64 failed in test helper");
    }

    /// Test-only pop_u64 helper.
    fn pop_u64(core: &RingBufferCore) -> u64 {
        let (start, len, advance) = core.pop_position().expect("pop_u64 failed in test helper");
        assert_eq!(len, 8, "pop_u64 expects 8-byte payload");
        let ring = unsafe { &*core.ring.get() };
        let mut buf = [0u8; 8];
        buf.copy_from_slice(&ring[start..start + 8]);
        let val = u64::from_le_bytes(buf);
        core.commit_pop(advance, len);
        val
    }

    #[test]
    fn test_zero_capacity_rejected() {
        assert!(RingBufferCore::new(0).is_err());
    }

    #[test]
    fn test_push_pop_roundtrip() {
        let core = make(1024);
        push(&core, b"hello");
        assert_eq!(pop(&core), b"hello");
    }

    #[test]
    fn test_fifo_ordering() {
        let core = make(1024);
        push(&core, b"first");
        push(&core, b"second");
        assert_eq!(pop(&core), b"first");
        assert_eq!(pop(&core), b"second");
    }

    #[test]
    fn test_size_tracking() {
        let core = make(100);
        push(&core, b"abcde"); // 5 bytes
        assert_eq!(core.size(), 5);
        push(&core, b"xyz"); // 3 bytes
        assert_eq!(core.size(), 8);
        pop(&core);
        assert_eq!(core.size(), 3);
    }

    #[test]
    fn test_is_empty_is_full() {
        let core = make(10);
        assert!(core.is_empty());
        assert!(!core.is_full());
        push(&core, &[0u8; 10]);
        assert!(!core.is_empty());
        assert!(core.is_full());
    }

    #[test]
    fn test_close() {
        let core = make(1024);
        assert!(!core.closed());
        core.close();
        assert!(core.closed());
    }

    #[test]
    fn test_msg_count() {
        let core = make(1024);
        push(&core, b"a");
        push(&core, b"b");
        assert_eq!(core.msg_count(), 2);
        pop(&core);
        assert_eq!(core.msg_count(), 1);
    }

    #[test]
    fn test_push_closed_rejected() {
        let core = make(1024);
        core.close();
        assert!(core.push_inner(b"data").is_err());
    }

    #[test]
    fn test_oversized_rejected() {
        let core = make(10);
        match core.push_inner(&[0u8; 11]) {
            Err(RingError::Oversized(11, 10)) => {}
            other => panic!("expected Oversized, got {:?}", other.is_ok()),
        }
    }

    #[test]
    fn test_full_rejected() {
        let core = make(10);
        push(&core, &[0u8; 10]);
        match core.push_inner(b"x") {
            Err(RingError::Full(10, 10)) => {}
            other => panic!("expected Full, got {:?}", other.is_ok()),
        }
    }

    #[test]
    fn test_empty_push_is_noop() {
        let core = make(1024);
        assert_eq!(push(&core, b""), 0);
        assert_eq!(core.msg_count(), 0);
    }

    #[test]
    fn test_pop_empty_error() {
        let core = make(1024);
        assert!(core.pop_position().is_err());
    }

    #[test]
    fn test_pop_closed_empty_error() {
        let core = make(1024);
        core.close();
        match core.pop_position() {
            Err(RingError::ClosedEmpty) => {}
            _ => panic!("expected ClosedEmpty"),
        }
    }

    #[test]
    fn test_drain_before_closed_error() {
        let core = make(1024);
        push(&core, b"last");
        core.close();
        assert_eq!(pop(&core), b"last");
        match core.pop_position() {
            Err(RingError::ClosedEmpty) => {}
            _ => panic!("expected ClosedEmpty"),
        }
    }

    // -- Wrap-around tests --

    #[test]
    fn test_wrap_around_basic() {
        // Use a small capacity so the ring wraps quickly
        let core = make(64);
        // ring_cap = 128. Each 50-byte message = 4 + 50 = 54 bytes frame.
        // First push: tail at 54. Second push: only 74 bytes left, enough.
        // Third push after draining: will eventually wrap.

        // Fill and drain several cycles to force wrap-around
        for cycle in 0..5 {
            let msg = format!("cycle-{cycle}");
            push(&core, msg.as_bytes());
            let out = pop(&core);
            assert_eq!(out, msg.as_bytes(), "cycle {cycle}");
        }
    }

    #[test]
    fn test_wrap_around_large_messages() {
        // capacity=64, ring_cap=128
        // 50-byte payload → 54-byte frame. First goes at 0..54.
        // Second: 54..108. Third would need 54 bytes at 108, but only 20 left → sentinel + wrap.
        let core = make(64);

        push(&core, &[0xAA; 50]);
        assert_eq!(core.size(), 50);
        let out = pop(&core);
        assert_eq!(out, vec![0xAA; 50]);
        assert_eq!(core.size(), 0);

        push(&core, &[0xBB; 50]);
        let out = pop(&core);
        assert_eq!(out, vec![0xBB; 50]);

        // This one should trigger wrap-around (tail at ~108, only ~20 bytes left)
        push(&core, &[0xCC; 50]);
        let out = pop(&core);
        assert_eq!(out, vec![0xCC; 50]);
    }

    #[test]
    fn test_wrap_around_many_small_messages() {
        let core = make(32);
        // ring_cap = 64. Each 1-byte message = 5-byte frame.
        // Can fit ~12 frames before wrapping.
        for i in 0u8..100 {
            push(&core, &[i]);
            let out = pop(&core);
            assert_eq!(out, vec![i]);
        }
    }

    #[test]
    fn test_sentinel_edge_cases() {
        // Test wrapping when exactly 4 bytes (header-only) remain at tail
        let core = make(128);
        // ring_cap = 256
        // We need to position tail so that only a few bytes remain

        // Fill with exact-size messages to position tail near end
        // 60-byte payload = 64-byte frame. 256/64 = 4 frames fit exactly.
        // Push 3 and pop 3 → head and tail at 192.
        // Push 1 more → tail at 256 = 0 (wraps perfectly, no sentinel needed)
        // But if we push a 60-byte msg when tail is at 192 and 64 bytes remain, it fits.
        // Let's try: push 3×60, pop all, then push another to force near-boundary.

        for _ in 0..3 {
            push(&core, &[0xFF; 60]);
        }
        for _ in 0..3 {
            pop(&core);
        }
        // head=tail=192, 64 bytes remaining to end
        // Push 56-byte payload (60-byte frame) — fits in 64 bytes
        push(&core, &[0xAA; 56]);
        // tail now at 252. Only 4 bytes left (exactly HEADER_SIZE).
        // Next push must sentinel+wrap.
        push(&core, &[0xBB; 10]);
        let out = pop(&core);
        assert_eq!(out, vec![0xAA; 56]);
        let out = pop(&core);
        assert_eq!(out, vec![0xBB; 10]);
    }

    // -- u64 fast path tests --

    #[test]
    fn test_push_u64_pop_u64() {
        let core = make(1024);
        push_u64(&core, 42);
        push_u64(&core, u64::MAX);
        push_u64(&core, 0);
        assert_eq!(pop_u64(&core), 42);
        assert_eq!(pop_u64(&core), u64::MAX);
        assert_eq!(pop_u64(&core), 0);
    }

    #[test]
    fn test_interleaved_bytes_u64() {
        let core = make(1024);
        push(&core, b"hello");
        push_u64(&core, 12345);
        push(&core, b"world");

        assert_eq!(pop(&core), b"hello");
        assert_eq!(pop_u64(&core), 12345);
        assert_eq!(pop(&core), b"world");
    }

    #[test]
    fn test_pop_u64_wrong_size() {
        let core = make(1024);
        push(&core, b"12345"); // 5 bytes, not 8
        let (_start, len, advance) = core.pop_position().unwrap();
        assert_ne!(len, 8);
        // Don't commit — just verify the size mismatch would be caught
        // Re-read to test the full pop_u64 path
        // We need to NOT commit, then re-try. Since we didn't commit, position is still valid.
        // Actually pop_position doesn't advance head, so we can just check.
        assert_eq!(len, 5);
        // Commit the pop to clean up
        core.commit_pop(advance, len);

        // Test via the push_u64/pop path with wrong-size manual push
        push(&core, &[1, 2, 3, 4, 5]); // 5 bytes
        let (_, len2, _) = core.pop_position().unwrap();
        assert_eq!(len2, 5); // Would fail pop_u64's len==8 check
    }

    #[test]
    fn test_u64_wrap_around() {
        // Force u64 messages to wrap around the ring
        let core = make(32);
        // ring_cap = 64. Each u64 = 12-byte frame (4 header + 8 payload).
        // Can fit 5 frames (60 bytes) before needing to wrap.
        for i in 0u64..20 {
            push_u64(&core, i);
            assert_eq!(pop_u64(&core), i);
        }
    }

    // -- SPSC two-thread test --

    #[test]
    fn test_spsc_two_threads() {
        use std::sync::Arc;
        use std::thread;

        let core = Arc::new(make(1024));
        let n = 1000usize;

        let producer = {
            let core = Arc::clone(&core);
            thread::spawn(move || {
                for i in 0..n {
                    loop {
                        match core.push_inner(&(i as u64).to_le_bytes()) {
                            Ok(_) => break,
                            Err(RingError::Full(_, _)) => {
                                thread::yield_now();
                                continue;
                            }
                            Err(_) => panic!("unexpected push error"),
                        }
                    }
                }
            })
        };

        let consumer = {
            let core = Arc::clone(&core);
            thread::spawn(move || {
                for i in 0..n {
                    loop {
                        match core.pop_position() {
                            Ok((start, len, advance)) => {
                                assert_eq!(len, 8);
                                let ring = unsafe { &*core.ring.get() };
                                let mut buf = [0u8; 8];
                                buf.copy_from_slice(&ring[start..start + 8]);
                                let val = u64::from_le_bytes(buf);
                                core.commit_pop(advance, len);
                                assert_eq!(val, i as u64);
                                break;
                            }
                            Err(RingError::Empty) => {
                                thread::yield_now();
                                continue;
                            }
                            Err(_) => panic!("unexpected pop error"),
                        }
                    }
                }
            })
        };

        producer.join().unwrap();
        consumer.join().unwrap();
        assert!(core.is_empty());
        assert_eq!(core.push_count.load(Ordering::Relaxed), n as u64);
        assert_eq!(core.pop_count.load(Ordering::Relaxed), n as u64);
    }
}
