//! Linear append-only buffer for DT_STREAM kernel IPC (Task #1574).
//!
//! Unlike `pipe.rs` (circular ring, destructive pop), StreamBufferCore is a
//! linear append-only buffer where reads are non-destructive and offset-based.
//! Multiple readers maintain independent cursors (fan-out).
//!
//! Message framing: `[4B u32 LE length][N bytes payload]`.
//! No sentinel, no wrap-around — fundamentally simpler than the ring buffer.
//!
//! Error encoding: Rust raises `RuntimeError("StreamFull:…")` etc. Python
//! translates to the matching exception class.

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
// StreamBufferCore
// ---------------------------------------------------------------------------

/// Linear append-only buffer for DT_STREAM.
///
/// Pre-allocated linear buffer with monotonic tail. Reads are non-destructive
/// and offset-based — each reader supplies its own byte offset.
/// Python wrapper provides asyncio.Event coordination for blocked writers.
#[pyclass]
pub struct StreamBufferCore {
    buf: UnsafeCell<Vec<u8>>,
    capacity: usize,
    tail: AtomicUsize,
    closed: AtomicBool,
    push_count: AtomicU64,
    msg_count: AtomicUsize,
}

// SAFETY: Append-only buffer. Writes extend [tail..new_tail], reads access
// [offset..offset+len] which is already committed. Python GIL serializes
// all PyO3 method calls.
unsafe impl Send for StreamBufferCore {}
unsafe impl Sync for StreamBufferCore {}

// ---------------------------------------------------------------------------
// Internal error type
// ---------------------------------------------------------------------------

#[derive(Debug)]
enum StreamError {
    Closed(&'static str),
    Full(usize, usize),
    Empty,
    ClosedEmpty,
    Oversized(usize, usize),
    InvalidOffset(usize, usize),
}

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

impl StreamBufferCore {
    /// Push raw bytes into the buffer. Returns byte offset where message starts.
    fn push_inner(&self, data: &[u8]) -> Result<usize, StreamError> {
        if self.closed.load(Ordering::Acquire) {
            return Err(StreamError::Closed("write to closed stream"));
        }
        let payload_len = data.len();
        if payload_len == 0 {
            return Ok(self.tail.load(Ordering::Relaxed));
        }
        if payload_len > self.capacity {
            return Err(StreamError::Oversized(payload_len, self.capacity));
        }

        let frame_len = HEADER_SIZE + payload_len;
        let tail = self.tail.load(Ordering::Relaxed);

        if tail + frame_len > self.capacity {
            return Err(StreamError::Full(tail, self.capacity));
        }

        let buf = unsafe { &mut *self.buf.get() };

        // Write frame: [4B len][payload]
        let header = (payload_len as u32).to_le_bytes();
        buf[tail..tail + HEADER_SIZE].copy_from_slice(&header);
        buf[tail + HEADER_SIZE..tail + HEADER_SIZE + payload_len].copy_from_slice(data);

        // Record the start offset before advancing tail
        let msg_offset = tail;

        // Update tail
        self.tail.store(tail + frame_len, Ordering::Release);

        // Update counters
        self.msg_count.fetch_add(1, Ordering::Relaxed);
        self.push_count.fetch_add(1, Ordering::Relaxed);

        Ok(msg_offset)
    }

    /// Read one message at the given byte offset. Returns (payload, next_offset).
    fn read_at_inner(&self, byte_offset: usize) -> Result<(usize, usize, usize), StreamError> {
        let tail = self.tail.load(Ordering::Acquire);

        if byte_offset >= tail {
            return if self.closed.load(Ordering::Acquire) {
                Err(StreamError::ClosedEmpty)
            } else {
                Err(StreamError::Empty)
            };
        }

        if byte_offset + HEADER_SIZE > tail {
            return Err(StreamError::InvalidOffset(byte_offset, tail));
        }

        let buf = unsafe { &*self.buf.get() };

        // Read header
        let mut hdr = [0u8; HEADER_SIZE];
        hdr.copy_from_slice(&buf[byte_offset..byte_offset + HEADER_SIZE]);
        let payload_len = u32::from_le_bytes(hdr) as usize;

        let payload_start = byte_offset + HEADER_SIZE;
        let next_offset = payload_start + payload_len;

        if next_offset > tail {
            return Err(StreamError::InvalidOffset(byte_offset, tail));
        }

        Ok((payload_start, payload_len, next_offset))
    }
}

// ---------------------------------------------------------------------------
// PyO3 methods
// ---------------------------------------------------------------------------

#[pymethods]
impl StreamBufferCore {
    #[new]
    fn new(capacity: usize) -> PyResult<Self> {
        if capacity == 0 {
            return Err(pyo3::exceptions::PyValueError::new_err(
                "capacity must be > 0, got 0",
            ));
        }
        Ok(Self {
            buf: UnsafeCell::new(vec![0u8; capacity]),
            capacity,
            tail: AtomicUsize::new(0),
            closed: AtomicBool::new(false),
            push_count: AtomicU64::new(0),
            msg_count: AtomicUsize::new(0),
        })
    }

    /// Push a message. Returns byte offset where the message starts.
    fn push(&self, _py: Python<'_>, data: &[u8]) -> PyResult<usize> {
        match self.push_inner(data) {
            Ok(offset) => Ok(offset),
            Err(StreamError::Closed(msg)) => Err(pyo3::exceptions::PyRuntimeError::new_err(
                format!("StreamClosed:{msg}"),
            )),
            Err(StreamError::Full(used, cap)) => Err(pyo3::exceptions::PyRuntimeError::new_err(
                format!("StreamFull:buffer full ({used}/{cap} bytes)"),
            )),
            Err(StreamError::Oversized(msg_len, cap)) => {
                Err(pyo3::exceptions::PyValueError::new_err(format!(
                    "message size {msg_len} exceeds buffer capacity {cap}"
                )))
            }
            Err(
                StreamError::Empty | StreamError::ClosedEmpty | StreamError::InvalidOffset(_, _),
            ) => {
                unreachable!()
            }
        }
    }

    /// Read one message at the given byte offset. Returns (data, next_offset).
    fn read_at<'py>(
        &self,
        py: Python<'py>,
        byte_offset: usize,
    ) -> PyResult<(Bound<'py, PyBytes>, usize)> {
        match self.read_at_inner(byte_offset) {
            Ok((start, len, next)) => {
                let buf = unsafe { &*self.buf.get() };
                let data = PyBytes::new(py, &buf[start..start + len]);
                Ok((data, next))
            }
            Err(StreamError::ClosedEmpty) => Err(pyo3::exceptions::PyRuntimeError::new_err(
                "StreamClosed:read from closed empty stream",
            )),
            Err(StreamError::Empty) => Err(pyo3::exceptions::PyRuntimeError::new_err(
                "StreamEmpty:no data at offset",
            )),
            Err(StreamError::InvalidOffset(off, tail)) => {
                Err(pyo3::exceptions::PyValueError::new_err(format!(
                    "invalid offset {off} (tail={tail})"
                )))
            }
            Err(
                StreamError::Closed(_) | StreamError::Full(_, _) | StreamError::Oversized(_, _),
            ) => {
                unreachable!()
            }
        }
    }

    /// Read up to `count` messages starting at byte_offset.
    /// Returns (list_of_bytes, next_offset).
    fn read_batch<'py>(
        &self,
        py: Python<'py>,
        byte_offset: usize,
        count: usize,
    ) -> PyResult<(Bound<'py, PyList>, usize)> {
        let list = PyList::empty(py);
        let buf = unsafe { &*self.buf.get() };
        let tail = self.tail.load(Ordering::Acquire);
        let mut pos = byte_offset;
        let mut read = 0;

        while read < count && pos < tail {
            if pos + HEADER_SIZE > tail {
                break;
            }
            let mut hdr = [0u8; HEADER_SIZE];
            hdr.copy_from_slice(&buf[pos..pos + HEADER_SIZE]);
            let payload_len = u32::from_le_bytes(hdr) as usize;
            let payload_start = pos + HEADER_SIZE;
            let next = payload_start + payload_len;
            if next > tail {
                break;
            }
            let item = PyBytes::new(py, &buf[payload_start..payload_start + payload_len]);
            list.append(item).expect("append to list");
            pos = next;
            read += 1;
        }

        if read == 0 && byte_offset >= tail {
            if self.closed.load(Ordering::Acquire) {
                return Err(pyo3::exceptions::PyRuntimeError::new_err(
                    "StreamClosed:read from closed empty stream",
                ));
            }
            return Err(pyo3::exceptions::PyRuntimeError::new_err(
                "StreamEmpty:no data at offset",
            ));
        }

        Ok((list, pos))
    }

    /// Push a u64 value. Returns byte offset where the message starts.
    fn push_u64(&self, _py: Python<'_>, val: u64) -> PyResult<usize> {
        match self.push_inner(&val.to_le_bytes()) {
            Ok(offset) => Ok(offset),
            Err(StreamError::Closed(msg)) => Err(pyo3::exceptions::PyRuntimeError::new_err(
                format!("StreamClosed:{msg}"),
            )),
            Err(StreamError::Full(used, cap)) => Err(pyo3::exceptions::PyRuntimeError::new_err(
                format!("StreamFull:buffer full ({used}/{cap} bytes)"),
            )),
            Err(StreamError::Oversized(msg_len, cap)) => {
                Err(pyo3::exceptions::PyValueError::new_err(format!(
                    "message size {msg_len} exceeds buffer capacity {cap}"
                )))
            }
            Err(
                StreamError::Empty | StreamError::ClosedEmpty | StreamError::InvalidOffset(_, _),
            ) => {
                unreachable!()
            }
        }
    }

    /// Close the buffer. Idempotent.
    fn close(&self) {
        self.closed.store(true, Ordering::Release);
    }

    /// Buffer statistics as a dict.
    fn stats(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let dict = PyDict::new(py);
        dict.set_item("tail", self.tail.load(Ordering::Relaxed))?;
        dict.set_item("capacity", self.capacity)?;
        dict.set_item("msg_count", self.msg_count.load(Ordering::Relaxed))?;
        dict.set_item("closed", self.closed.load(Ordering::Acquire))?;
        dict.set_item("push_count", self.push_count.load(Ordering::Relaxed))?;
        Ok(dict.into())
    }

    #[getter]
    fn closed(&self) -> bool {
        self.closed.load(Ordering::Acquire)
    }

    #[getter]
    fn size(&self) -> usize {
        self.tail.load(Ordering::Relaxed)
    }

    #[getter]
    fn capacity(&self) -> usize {
        self.capacity
    }

    #[getter]
    fn msg_count(&self) -> usize {
        self.msg_count.load(Ordering::Relaxed)
    }

    #[getter]
    fn tail(&self) -> usize {
        self.tail.load(Ordering::Relaxed)
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    fn make(cap: usize) -> StreamBufferCore {
        StreamBufferCore::new(cap).unwrap()
    }

    fn push(core: &StreamBufferCore, data: &[u8]) -> usize {
        core.push_inner(data).expect("push failed")
    }

    fn read_at(core: &StreamBufferCore, offset: usize) -> (Vec<u8>, usize) {
        let (start, len, next) = core.read_at_inner(offset).expect("read_at failed");
        let buf = unsafe { &*core.buf.get() };
        (buf[start..start + len].to_vec(), next)
    }

    #[test]
    fn test_zero_capacity_rejected() {
        assert!(StreamBufferCore::new(0).is_err());
    }

    #[test]
    fn test_push_read_roundtrip() {
        let core = make(1024);
        let offset = push(&core, b"hello");
        assert_eq!(offset, 0);
        let (data, next) = read_at(&core, offset);
        assert_eq!(data, b"hello");
        assert_eq!(next, HEADER_SIZE + 5);
    }

    #[test]
    fn test_ordering() {
        let core = make(1024);
        let o1 = push(&core, b"first");
        let o2 = push(&core, b"second");
        assert!(o2 > o1);
        let (d1, n1) = read_at(&core, o1);
        let (d2, _n2) = read_at(&core, n1);
        assert_eq!(d1, b"first");
        assert_eq!(d2, b"second");
    }

    #[test]
    fn test_non_destructive_replay() {
        let core = make(1024);
        let offset = push(&core, b"replay");
        let (d1, _) = read_at(&core, offset);
        let (d2, _) = read_at(&core, offset);
        assert_eq!(d1, d2);
        assert_eq!(d1, b"replay");
    }

    #[test]
    fn test_multi_reader() {
        let core = make(1024);
        push(&core, b"msg1");
        push(&core, b"msg2");
        push(&core, b"msg3");

        // Reader A starts at 0, reads all
        let (d1, n1) = read_at(&core, 0);
        let (d2, n2) = read_at(&core, n1);
        let (d3, _) = read_at(&core, n2);
        assert_eq!(d1, b"msg1");
        assert_eq!(d2, b"msg2");
        assert_eq!(d3, b"msg3");

        // Reader B starts at 0, same result
        let (d1b, _) = read_at(&core, 0);
        assert_eq!(d1b, b"msg1");
    }

    #[test]
    fn test_stats() {
        let core = make(100);
        push(&core, b"abcde");
        assert_eq!(core.msg_count(), 1);
        assert_eq!(core.tail(), HEADER_SIZE + 5);
        push(&core, b"xyz");
        assert_eq!(core.msg_count(), 2);
    }

    #[test]
    fn test_close() {
        let core = make(1024);
        assert!(!core.closed());
        core.close();
        assert!(core.closed());
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
            Err(StreamError::Oversized(11, 10)) => {}
            other => panic!("expected Oversized, got {:?}", other.is_ok()),
        }
    }

    #[test]
    fn test_full_rejected() {
        let core = make(20);
        // Push 12 bytes payload = 16 bytes frame. Remaining: 4 bytes.
        push(&core, &[0u8; 12]);
        // Next push of 1 byte needs 5 bytes frame, only 4 available.
        match core.push_inner(b"x") {
            Err(StreamError::Full(_, _)) => {}
            other => panic!("expected Full, got {:?}", other.is_ok()),
        }
    }

    #[test]
    fn test_empty_push_is_noop() {
        let core = make(1024);
        let offset = push(&core, b"");
        assert_eq!(offset, 0);
        assert_eq!(core.msg_count(), 0);
    }

    #[test]
    fn test_read_empty_error() {
        let core = make(1024);
        assert!(core.read_at_inner(0).is_err());
    }

    #[test]
    fn test_read_closed_empty_error() {
        let core = make(1024);
        core.close();
        match core.read_at_inner(0) {
            Err(StreamError::ClosedEmpty) => {}
            _ => panic!("expected ClosedEmpty"),
        }
    }

    #[test]
    fn test_drain_before_closed() {
        let core = make(1024);
        let offset = push(&core, b"last");
        core.close();
        let (data, next) = read_at(&core, offset);
        assert_eq!(data, b"last");
        match core.read_at_inner(next) {
            Err(StreamError::ClosedEmpty) => {}
            _ => panic!("expected ClosedEmpty"),
        }
    }

    #[test]
    fn test_exact_capacity() {
        // capacity=12: one frame of 8 bytes payload = 4+8 = 12 bytes exactly
        let core = make(12);
        let offset = push(&core, &[0xAA; 8]);
        assert_eq!(offset, 0);
        let (data, _) = read_at(&core, 0);
        assert_eq!(data, vec![0xAA; 8]);
        // Buffer is now full
        match core.push_inner(b"x") {
            Err(StreamError::Full(_, _)) => {}
            other => panic!("expected Full, got {:?}", other.is_ok()),
        }
    }

    #[test]
    fn test_u64_push_read() {
        let core = make(1024);
        let o1 = core.push_inner(&42u64.to_le_bytes()).unwrap();
        let o2 = core.push_inner(&u64::MAX.to_le_bytes()).unwrap();
        let (d1, _) = read_at(&core, o1);
        let (d2, _) = read_at(&core, o2);
        assert_eq!(u64::from_le_bytes(d1.try_into().unwrap()), 42);
        assert_eq!(u64::from_le_bytes(d2.try_into().unwrap()), u64::MAX);
    }
}
