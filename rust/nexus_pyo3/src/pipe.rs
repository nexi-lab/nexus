//! Rust-accelerated RingBuffer core for DT_PIPE kernel IPC (Issue #806).
//!
//! Provides the data-plane operations (push/pop/peek) behind a `parking_lot::Mutex`.
//! Python keeps asyncio.Event coordination; Rust owns the buffer + metrics.
//!
//! Error encoding: Rust raises `RuntimeError("PipeFull:…")` etc. Python translates
//! to the matching exception class.

use parking_lot::Mutex;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList};
use std::collections::VecDeque;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};

// ---------------------------------------------------------------------------
// Internal state
// ---------------------------------------------------------------------------

struct BufferState {
    buf: VecDeque<Vec<u8>>,
    size: usize,
    capacity: usize,
}

// ---------------------------------------------------------------------------
// RingBufferCore
// ---------------------------------------------------------------------------

/// Rust-accelerated ring buffer core for DT_PIPE.
///
/// Sync-only data operations behind a Mutex.  Python wrapper adds
/// asyncio.Event coordination for blocking read/write.
#[pyclass]
pub struct RingBufferCore {
    state: Mutex<BufferState>,
    closed: AtomicBool,
    push_count: AtomicU64,
    pop_count: AtomicU64,
}

#[pymethods]
impl RingBufferCore {
    #[new]
    fn new(capacity: usize) -> PyResult<Self> {
        if capacity == 0 {
            return Err(pyo3::exceptions::PyValueError::new_err(
                "capacity must be > 0, got 0",
            ));
        }
        Ok(Self {
            state: Mutex::new(BufferState {
                buf: VecDeque::new(),
                size: 0,
                capacity,
            }),
            closed: AtomicBool::new(false),
            push_count: AtomicU64::new(0),
            pop_count: AtomicU64::new(0),
        })
    }

    /// Push a message into the buffer. Returns bytes written.
    ///
    /// Raises RuntimeError on closed/full/oversized — Python translates.
    fn push(&self, py: Python<'_>, data: &[u8]) -> PyResult<usize> {
        if self.closed.load(Ordering::Acquire) {
            return Err(pyo3::exceptions::PyRuntimeError::new_err(
                "PipeClosed:write to closed pipe",
            ));
        }
        if data.is_empty() {
            return Ok(0);
        }
        let msg_len = data.len();

        let mut st = self.state.lock();
        if msg_len > st.capacity {
            return Err(pyo3::exceptions::PyValueError::new_err(format!(
                "message size {} exceeds buffer capacity {}",
                msg_len, st.capacity
            )));
        }
        if st.size + msg_len > st.capacity {
            return Err(pyo3::exceptions::PyRuntimeError::new_err(format!(
                "PipeFull:buffer full ({}/{} bytes)",
                st.size, st.capacity
            )));
        }

        st.buf.push_back(data.to_vec());
        st.size += msg_len;
        drop(st);

        self.push_count.fetch_add(1, Ordering::Relaxed);
        let _ = py; // GIL held throughout — fine for sync ops
        Ok(msg_len)
    }

    /// Pop the next message. Returns PyBytes.
    ///
    /// Raises RuntimeError on closed-empty / empty — Python translates.
    fn pop<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyBytes>> {
        let mut st = self.state.lock();
        if let Some(msg) = st.buf.pop_front() {
            st.size -= msg.len();
            drop(st);
            self.pop_count.fetch_add(1, Ordering::Relaxed);
            Ok(PyBytes::new(py, &msg))
        } else if self.closed.load(Ordering::Acquire) {
            Err(pyo3::exceptions::PyRuntimeError::new_err(
                "PipeClosed:read from closed empty pipe",
            ))
        } else {
            Err(pyo3::exceptions::PyRuntimeError::new_err(
                "PipeEmpty:buffer empty",
            ))
        }
    }

    /// Non-consuming peek at the next message. Returns None if empty.
    fn peek<'py>(&self, py: Python<'py>) -> Option<Bound<'py, PyBytes>> {
        let st = self.state.lock();
        st.buf.front().map(|msg| PyBytes::new(py, msg))
    }

    /// Non-consuming view of all buffered messages.
    fn peek_all<'py>(&self, py: Python<'py>) -> Bound<'py, PyList> {
        let st = self.state.lock();
        let items: Vec<Bound<'py, PyBytes>> =
            st.buf.iter().map(|msg| PyBytes::new(py, msg)).collect();
        // Convert Vec<Bound<PyBytes>> to a PyList
        let list = PyList::empty(py);
        for item in items {
            list.append(item).expect("append to list");
        }
        list
    }

    /// Close the buffer. Idempotent.
    fn close(&self) {
        self.closed.store(true, Ordering::Release);
    }

    /// Buffer statistics as a dict.
    fn stats(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let st = self.state.lock();
        let dict = PyDict::new(py);
        dict.set_item("size", st.size)?;
        dict.set_item("capacity", st.capacity)?;
        dict.set_item("msg_count", st.buf.len())?;
        dict.set_item("closed", self.closed.load(Ordering::Acquire))?;
        dict.set_item("push_count", self.push_count.load(Ordering::Relaxed))?;
        dict.set_item("pop_count", self.pop_count.load(Ordering::Relaxed))?;
        Ok(dict.into())
    }

    fn is_empty(&self) -> bool {
        self.state.lock().buf.is_empty()
    }

    fn is_full(&self) -> bool {
        let st = self.state.lock();
        st.size >= st.capacity
    }

    #[getter]
    fn closed(&self) -> bool {
        self.closed.load(Ordering::Acquire)
    }

    #[getter]
    fn size(&self) -> usize {
        self.state.lock().size
    }

    #[getter]
    fn capacity(&self) -> usize {
        self.state.lock().capacity
    }

    #[getter]
    fn msg_count(&self) -> usize {
        self.state.lock().buf.len()
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

    fn push(core: &RingBufferCore, data: &[u8]) -> usize {
        let mut st = core.state.lock();
        if core.closed.load(Ordering::Acquire) {
            panic!("push on closed");
        }
        let msg_len = data.len();
        assert!(
            st.size + msg_len <= st.capacity,
            "buffer full in test helper"
        );
        st.buf.push_back(data.to_vec());
        st.size += msg_len;
        core.push_count.fetch_add(1, Ordering::Relaxed);
        msg_len
    }

    fn pop(core: &RingBufferCore) -> Vec<u8> {
        let mut st = core.state.lock();
        let msg = st.buf.pop_front().expect("buffer empty in test helper");
        st.size -= msg.len();
        core.pop_count.fetch_add(1, Ordering::Relaxed);
        msg
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
    fn test_concurrent_push() {
        use rayon::prelude::*;
        use std::sync::atomic::AtomicU32;

        // 100 threads each try to push 1 byte into a 50-byte buffer
        let core = make(50);
        let success = AtomicU32::new(0);

        (0..100u32).into_par_iter().for_each(|_| {
            let mut st = core.state.lock();
            if st.size + 1 <= st.capacity {
                st.buf.push_back(vec![0x42]);
                st.size += 1;
                success.fetch_add(1, Ordering::Relaxed);
            }
        });

        assert_eq!(success.load(Ordering::Relaxed), 50);
        assert_eq!(core.size(), 50);
        assert!(core.is_full());
    }
}
