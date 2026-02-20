//! DT_PIPE ring buffer — Rust SPSC hot path (Issue #806, Phase 2).
//!
//! Implements the sync hot path only: `write_nowait`, `read_nowait`, `peek`,
//! `peek_all`, `close`, `stats`. Python `AsyncRingBuffer` in `pipe_fast.py`
//! wraps this with `asyncio.Event` for blocking semantics.
//!
//! Design: message-oriented `VecDeque<Vec<u8>>` behind `parking_lot::Mutex`.
//! All ops are non-blocking (~50-100ns), so no GIL release (`py.detach()`)
//! needed — unlike `lock.rs` which blocks on Condvar.
//!
//! Exception mapping: `import_exception!` raises `nexus.core.pipe` Python
//! exceptions directly so `isinstance(e, PipeFullError)` works identically.

use parking_lot::Mutex;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict};
use std::collections::VecDeque;
use std::sync::atomic::{AtomicBool, Ordering};

// Import Python exception classes from nexus.core.pipe so Rust raises the
// same types that Python code catches.
pyo3::import_exception!(nexus.core.pipe, PipeClosedError);
pyo3::import_exception!(nexus.core.pipe, PipeFullError);
pyo3::import_exception!(nexus.core.pipe, PipeEmptyError);

// ---------------------------------------------------------------------------
// Internal state
// ---------------------------------------------------------------------------

struct Inner {
    buf: VecDeque<Vec<u8>>,
    size: usize, // total bytes across all buffered messages
}

// ---------------------------------------------------------------------------
// RingBuffer
// ---------------------------------------------------------------------------

/// Rust-accelerated SPSC message ring buffer for DT_PIPE kernel IPC.
///
/// Sync-only — async signaling lives in Python (`pipe_fast.py`).
/// ~50-100ns per `write_nowait`/`read_nowait` vs ~5μs Python.
#[pyclass(name = "RingBuffer", module = "nexus_fast")]
pub struct RingBuffer {
    state: Mutex<Inner>,
    capacity: usize,
    closed: AtomicBool,
}

#[pymethods]
impl RingBuffer {
    /// Create a ring buffer with the given byte capacity.
    ///
    /// Raises `ValueError` if capacity is 0.
    #[new]
    fn new(capacity: usize) -> PyResult<Self> {
        if capacity == 0 {
            return Err(pyo3::exceptions::PyValueError::new_err(
                "capacity must be > 0, got 0",
            ));
        }
        Ok(Self {
            state: Mutex::new(Inner {
                buf: VecDeque::new(),
                size: 0,
            }),
            capacity,
            closed: AtomicBool::new(false),
        })
    }

    /// Synchronous non-blocking write. Returns bytes written.
    ///
    /// Empty data is a no-op (returns 0).
    ///
    /// Raises:
    ///   - `PipeClosedError`: buffer is closed.
    ///   - `PipeFullError`: insufficient space.
    ///   - `ValueError`: message larger than capacity.
    fn write_nowait(&self, data: &[u8]) -> PyResult<usize> {
        if self.closed.load(Ordering::Acquire) {
            return Err(PipeClosedError::new_err("write to closed pipe"));
        }
        if data.is_empty() {
            return Ok(0);
        }
        let msg_len = data.len();
        if msg_len > self.capacity {
            return Err(pyo3::exceptions::PyValueError::new_err(format!(
                "message size {} exceeds buffer capacity {}",
                msg_len, self.capacity
            )));
        }
        let mut state = self.state.lock();
        if state.size + msg_len > self.capacity {
            return Err(PipeFullError::new_err(format!(
                "buffer full ({}/{} bytes)",
                state.size, self.capacity
            )));
        }
        state.buf.push_back(data.to_vec());
        state.size += msg_len;
        Ok(msg_len)
    }

    /// Synchronous non-blocking read. Returns the next message as bytes.
    ///
    /// Raises:
    ///   - `PipeClosedError`: buffer is closed AND empty.
    ///   - `PipeEmptyError`: buffer is empty (not closed).
    fn read_nowait<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyBytes>> {
        let msg = {
            let mut state = self.state.lock();
            match state.buf.pop_front() {
                Some(msg) => {
                    state.size -= msg.len();
                    msg
                }
                None if self.closed.load(Ordering::Acquire) => {
                    return Err(PipeClosedError::new_err("read from closed empty pipe"));
                }
                None => {
                    return Err(PipeEmptyError::new_err("buffer empty"));
                }
            }
        }; // Mutex dropped here
        Ok(PyBytes::new(py, &msg))
    }

    /// Non-consuming peek at the next message. Returns `None` if empty.
    fn peek<'py>(&self, py: Python<'py>) -> Option<Bound<'py, PyBytes>> {
        let state = self.state.lock();
        state.buf.front().map(|data| PyBytes::new(py, data))
    }

    /// Non-consuming snapshot of all buffered messages.
    fn peek_all<'py>(&self, py: Python<'py>) -> Vec<Bound<'py, PyBytes>> {
        let state = self.state.lock();
        state
            .buf
            .iter()
            .map(|data| PyBytes::new(py, data))
            .collect()
    }

    /// Close the buffer. Subsequent writes raise `PipeClosedError`.
    /// Reads can still drain remaining messages.
    fn close(&self) {
        self.closed.store(true, Ordering::Release);
    }

    /// Whether the buffer is closed.
    #[getter]
    fn closed(&self) -> bool {
        self.closed.load(Ordering::Acquire)
    }

    /// Buffer statistics: `{"size", "capacity", "msg_count", "closed"}`.
    #[getter]
    fn stats<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let (size, msg_count) = {
            let state = self.state.lock();
            (state.size, state.buf.len())
        };
        let dict = PyDict::new(py);
        dict.set_item("size", size)?;
        dict.set_item("capacity", self.capacity)?;
        dict.set_item("msg_count", msg_count)?;
        dict.set_item("closed", self.closed.load(Ordering::Acquire))?;
        Ok(dict)
    }
}

// ---------------------------------------------------------------------------
// Rust unit tests (no PyO3, test raw logic via internal helpers)
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    fn make(cap: usize) -> RingBuffer {
        RingBuffer::new(cap).unwrap()
    }

    // Helpers that bypass PyO3 for pure Rust logic testing.
    fn write_raw(rb: &RingBuffer, data: &[u8]) -> Result<usize, String> {
        if rb.closed.load(Ordering::Acquire) {
            return Err("closed".into());
        }
        if data.is_empty() {
            return Ok(0);
        }
        let msg_len = data.len();
        if msg_len > rb.capacity {
            return Err("oversized".into());
        }
        let mut state = rb.state.lock();
        if state.size + msg_len > rb.capacity {
            return Err("full".into());
        }
        state.buf.push_back(data.to_vec());
        state.size += msg_len;
        Ok(msg_len)
    }

    fn read_raw(rb: &RingBuffer) -> Result<Vec<u8>, String> {
        let mut state = rb.state.lock();
        match state.buf.pop_front() {
            Some(msg) => {
                state.size -= msg.len();
                Ok(msg)
            }
            None if rb.closed.load(Ordering::Acquire) => Err("closed".into()),
            None => Err("empty".into()),
        }
    }

    fn peek_raw(rb: &RingBuffer) -> Option<Vec<u8>> {
        rb.state.lock().buf.front().cloned()
    }

    fn stats_raw(rb: &RingBuffer) -> (usize, usize, usize, bool) {
        let state = rb.state.lock();
        (
            state.size,
            rb.capacity,
            state.buf.len(),
            rb.closed.load(Ordering::Acquire),
        )
    }

    #[test]
    fn test_new_zero_capacity_fails() {
        assert!(RingBuffer::new(0).is_err());
    }

    #[test]
    fn test_write_read_roundtrip() {
        let rb = make(1024);
        assert_eq!(write_raw(&rb, b"hello").unwrap(), 5);
        assert_eq!(read_raw(&rb).unwrap(), b"hello");
    }

    #[test]
    fn test_fifo_ordering() {
        let rb = make(1024);
        write_raw(&rb, b"first").unwrap();
        write_raw(&rb, b"second").unwrap();
        assert_eq!(read_raw(&rb).unwrap(), b"first");
        assert_eq!(read_raw(&rb).unwrap(), b"second");
    }

    #[test]
    fn test_capacity_tracking() {
        let rb = make(100);
        write_raw(&rb, b"hello").unwrap(); // 5 bytes
        let (size, cap, count, _) = stats_raw(&rb);
        assert_eq!(size, 5);
        assert_eq!(cap, 100);
        assert_eq!(count, 1);

        read_raw(&rb).unwrap();
        let (size, _, count, _) = stats_raw(&rb);
        assert_eq!(size, 0);
        assert_eq!(count, 0);
    }

    #[test]
    fn test_full_error() {
        let rb = make(10);
        write_raw(&rb, &[0u8; 10]).unwrap();
        assert_eq!(write_raw(&rb, b"x").unwrap_err(), "full");
    }

    #[test]
    fn test_empty_error() {
        let rb = make(1024);
        assert_eq!(read_raw(&rb).unwrap_err(), "empty");
    }

    #[test]
    fn test_oversized_error() {
        let rb = make(10);
        assert_eq!(write_raw(&rb, &[0u8; 11]).unwrap_err(), "oversized");
    }

    #[test]
    fn test_empty_write_is_noop() {
        let rb = make(1024);
        assert_eq!(write_raw(&rb, b"").unwrap(), 0);
        let (size, _, count, _) = stats_raw(&rb);
        assert_eq!(size, 0);
        assert_eq!(count, 0);
    }

    #[test]
    fn test_close_sets_flag() {
        let rb = make(1024);
        assert!(!rb.closed.load(Ordering::Acquire));
        rb.close();
        assert!(rb.closed.load(Ordering::Acquire));
    }

    #[test]
    fn test_write_after_close() {
        let rb = make(1024);
        rb.close();
        assert_eq!(write_raw(&rb, b"data").unwrap_err(), "closed");
    }

    #[test]
    fn test_read_closed_empty() {
        let rb = make(1024);
        rb.close();
        assert_eq!(read_raw(&rb).unwrap_err(), "closed");
    }

    #[test]
    fn test_drain_before_close_error() {
        let rb = make(1024);
        write_raw(&rb, b"last").unwrap();
        rb.close();
        // Should still be able to read buffered message
        assert_eq!(read_raw(&rb).unwrap(), b"last");
        // Now empty + closed → error
        assert_eq!(read_raw(&rb).unwrap_err(), "closed");
    }

    #[test]
    fn test_peek_none_on_empty() {
        let rb = make(1024);
        assert!(peek_raw(&rb).is_none());
    }

    #[test]
    fn test_peek_does_not_consume() {
        let rb = make(1024);
        write_raw(&rb, b"msg").unwrap();
        assert_eq!(peek_raw(&rb).unwrap(), b"msg");
        let (_, _, count, _) = stats_raw(&rb);
        assert_eq!(count, 1); // still there
    }

    #[test]
    fn test_space_freed_after_read() {
        let rb = make(10);
        write_raw(&rb, &[0u8; 10]).unwrap();
        assert_eq!(write_raw(&rb, b"x").unwrap_err(), "full");
        read_raw(&rb).unwrap();
        // Space freed — can write again
        assert_eq!(write_raw(&rb, b"y").unwrap(), 1);
    }

    #[test]
    fn test_exact_capacity() {
        let rb = make(5);
        assert_eq!(write_raw(&rb, b"12345").unwrap(), 5);
        let (size, _, _, _) = stats_raw(&rb);
        assert_eq!(size, 5);
    }
}
