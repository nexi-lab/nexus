//! StdioStreamBackend — stream backend over OS subprocess pipes.
//!
//! 1:1 behavior port of Python `StdioStreamBackend`
//! (src/nexus/core/stdio_stream.py). Newline-framed accumulation
//! buffer: each call to `feed_bytes` appends bytes; line terminators
//! split the stream into indexed messages, enabling offset-based
//! multi-reader access (`read_at` returns the message starting at or
//! after a given byte offset, non-destructively).
//!
//! Architecture choice (plan v18 §R19.1c option B):
//!   sync I/O + background thread for the pump. Python callers wrap
//!   the `read_at` / `read_batch` / `write` calls with
//!   `asyncio.to_thread` — standard pattern in the codebase — to stay
//!   async-friendly without bridging asyncio ↔ tokio runtimes.
//!
//! Core logic (`StdioStreamCore`) is cross-platform and unit-testable
//! without any OS pipes via `feed_bytes` / `feed_eof`. The PyO3 class
//! `StdioStreamBackend` is `#[cfg(unix)]` because it drives the pump
//! via `libc::read` on a raw fd (matches `stdio_pipe.rs` pattern).

#[cfg(unix)]
use pyo3::exceptions::PyRuntimeError;
#[cfg(unix)]
use pyo3::prelude::*;
#[cfg(unix)]
use pyo3::types::{PyBytes, PyDict, PyList};
#[cfg(unix)]
use std::sync::atomic::{AtomicBool, Ordering};
#[cfg(unix)]
use std::sync::Arc;
use std::sync::{Condvar, Mutex};

// ---------------------------------------------------------------------------
// Error types
// ---------------------------------------------------------------------------

/// Errors returned by `StdioStreamCore`. The PyO3 binding maps these onto
/// `RuntimeError` with message prefixes matching the Python exceptions
/// (`StreamEmptyError` / `StreamClosedError`) so error-path tests that
/// check message contents continue to work after the Python wrapper is
/// deleted in R19.1e.
#[cfg_attr(not(any(unix, test)), allow(dead_code))]
#[derive(Debug)]
pub enum StdioStreamError {
    /// No data at this offset, stream still open (non-terminal).
    Empty(u64),
    /// Stream closed; no more data will arrive at this offset.
    Closed(u64),
}

impl std::fmt::Display for StdioStreamError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Empty(off) => write!(f, "no data at offset {off}"),
            Self::Closed(off) => write!(f, "stream closed, no data at offset {off}"),
        }
    }
}

// ---------------------------------------------------------------------------
// Core (cross-platform, no I/O)
// ---------------------------------------------------------------------------

#[cfg_attr(not(any(unix, test)), allow(dead_code))]
struct StdioStreamInner {
    buffer: Vec<Vec<u8>>,
    /// Start byte offset of each message. `byte_offsets[i]` is the start
    /// of `buffer[i]`. Monotonically increasing.
    byte_offsets: Vec<u64>,
    total_bytes: u64,
    closed: bool,
}

/// Cross-platform core: buffer + offset index + blocking-read condvar.
///
/// `feed_bytes` appends bytes and splits on `\n` boundaries. Each
/// newline-terminated line becomes a separate message. Any trailing
/// bytes without a newline accumulate in a pending partial until more
/// data arrives — matching `asyncio.StreamReader.readline` semantics.
#[cfg_attr(not(any(unix, test)), allow(dead_code))]
pub struct StdioStreamCore {
    inner: Mutex<StdioStreamInner>,
    wake: Condvar,
    /// Partial line buffer for bytes received without a terminating
    /// `\n`. Flushed into `inner.buffer` when the newline arrives, or
    /// on `feed_eof` as a final partial message.
    partial: Mutex<Vec<u8>>,
}

#[cfg_attr(not(any(unix, test)), allow(dead_code))]
impl StdioStreamCore {
    pub fn new() -> Self {
        Self {
            inner: Mutex::new(StdioStreamInner {
                buffer: Vec::new(),
                byte_offsets: Vec::new(),
                total_bytes: 0,
                closed: false,
            }),
            wake: Condvar::new(),
            partial: Mutex::new(Vec::new()),
        }
    }

    /// Append raw bytes. Splits on `\n`; each `\n`-terminated slice
    /// becomes a single message. Trailing bytes without `\n` are
    /// retained as partial until the next feed or `feed_eof`.
    pub fn feed_bytes(&self, data: &[u8]) {
        if data.is_empty() {
            return;
        }
        let mut partial = self.partial.lock().unwrap();
        let mut inner = self.inner.lock().unwrap();
        if inner.closed {
            return;
        }
        let mut start = 0;
        for (i, &b) in data.iter().enumerate() {
            if b == b'\n' {
                let mut line = std::mem::take(&mut *partial);
                line.extend_from_slice(&data[start..=i]);
                let off = inner.total_bytes;
                inner.total_bytes += line.len() as u64;
                inner.byte_offsets.push(off);
                inner.buffer.push(line);
                start = i + 1;
            }
        }
        if start < data.len() {
            partial.extend_from_slice(&data[start..]);
        }
        self.wake.notify_all();
    }

    /// Mark the stream closed. Flushes any trailing partial line as a
    /// final message (matches Python `readline` behavior at EOF).
    pub fn feed_eof(&self) {
        let mut partial = self.partial.lock().unwrap();
        let mut inner = self.inner.lock().unwrap();
        if !partial.is_empty() {
            let line = std::mem::take(&mut *partial);
            let off = inner.total_bytes;
            inner.total_bytes += line.len() as u64;
            inner.byte_offsets.push(off);
            inner.buffer.push(line);
        }
        inner.closed = true;
        self.wake.notify_all();
    }

    /// Close the stream (no final partial flush — caller sets closed
    /// explicitly, e.g. via `StdioStreamBackend::close`).
    pub fn close(&self) {
        let mut inner = self.inner.lock().unwrap();
        inner.closed = true;
        self.wake.notify_all();
    }

    pub fn is_closed(&self) -> bool {
        self.inner.lock().unwrap().closed
    }

    /// Read one message starting at `byte_offset`. Returns
    /// `(data, next_offset)` on success. Matches Python `read_at` logic
    /// (`bisect_right - 1` with boundary check + fallback to next-message
    /// lookup for mid-message offsets).
    pub fn read_at(&self, byte_offset: u64) -> Result<(Vec<u8>, u64), StdioStreamError> {
        let inner = self.inner.lock().unwrap();
        if inner.buffer.is_empty() {
            return if inner.closed {
                Err(StdioStreamError::Closed(byte_offset))
            } else {
                Err(StdioStreamError::Empty(byte_offset))
            };
        }

        // bisect_right(offsets, x) == partition_point(|&v| v <= x)
        let br = inner.byte_offsets.partition_point(|&v| v <= byte_offset);
        let idx = if br == 0 { 0 } else { br - 1 };
        let exact = idx < inner.byte_offsets.len() && inner.byte_offsets[idx] == byte_offset;
        let final_idx = if exact {
            idx
        } else if byte_offset >= inner.total_bytes {
            return if inner.closed {
                Err(StdioStreamError::Closed(byte_offset))
            } else {
                Err(StdioStreamError::Empty(byte_offset))
            };
        } else {
            // Mid-message offset — round up to next boundary.
            let next = br;
            if next >= inner.buffer.len() {
                return if inner.closed {
                    Err(StdioStreamError::Closed(byte_offset))
                } else {
                    Err(StdioStreamError::Empty(byte_offset))
                };
            }
            next
        };

        let data = inner.buffer[final_idx].clone();
        let next_offset = inner.byte_offsets[final_idx] + data.len() as u64;
        Ok((data, next_offset))
    }

    /// Read up to `count` messages starting at `byte_offset`. Returns
    /// `(items, next_offset)`. Raises if no data yet (unlike `read_at`,
    /// does not round mid-message offsets — uses `bisect_left`).
    pub fn read_batch(
        &self,
        byte_offset: u64,
        count: usize,
    ) -> Result<(Vec<Vec<u8>>, u64), StdioStreamError> {
        let inner = self.inner.lock().unwrap();
        if inner.buffer.is_empty() {
            return if inner.closed {
                Err(StdioStreamError::Closed(byte_offset))
            } else {
                Err(StdioStreamError::Empty(byte_offset))
            };
        }

        // bisect_left(offsets, x) == partition_point(|&v| v < x)
        let idx = inner.byte_offsets.partition_point(|&v| v < byte_offset);
        if idx >= inner.buffer.len() {
            return if inner.closed {
                Err(StdioStreamError::Closed(byte_offset))
            } else {
                Err(StdioStreamError::Empty(byte_offset))
            };
        }
        let end = (idx + count).min(inner.buffer.len());
        let items: Vec<Vec<u8>> = inner.buffer[idx..end].to_vec();
        let next_offset = if let Some(last) = items.last() {
            inner.byte_offsets[end - 1] + last.len() as u64
        } else {
            byte_offset
        };
        Ok((items, next_offset))
    }

    pub fn tail(&self) -> u64 {
        self.inner.lock().unwrap().total_bytes
    }

    pub fn stats_snapshot(&self) -> (usize, u64, bool) {
        let inner = self.inner.lock().unwrap();
        (inner.buffer.len(), inner.total_bytes, inner.closed)
    }
}

impl Default for StdioStreamCore {
    fn default() -> Self {
        Self::new()
    }
}

// ---------------------------------------------------------------------------
// PyO3 binding (Unix-only — drives pump via libc::read on raw fd)
// ---------------------------------------------------------------------------

/// Stream backend over OS subprocess pipes.
///
/// Constructor takes raw file descriptors (extracted from an asyncio
/// subprocess via `process.stdout.transport.get_extra_info('pipe').fileno()`
/// or equivalent). A background thread pumps `read_fd` into the
/// internal buffer using `libc::read` + newline framing.
///
/// Python wrappers that need an async-friendly read should call
/// `read_at` / `read_batch` from `asyncio.to_thread(...)` — writes via
/// `write_nowait` are already non-blocking.
#[cfg(unix)]
#[pyclass(name = "StdioStreamBackend")]
pub struct StdioStreamBackend {
    core: Arc<StdioStreamCore>,
    write_fd: i32,
    pump_running: Arc<AtomicBool>,
}

#[cfg(unix)]
#[pymethods]
impl StdioStreamBackend {
    /// Create a new StdioStreamBackend from raw fds.
    ///
    /// Args:
    ///     read_fd: fd to read from (-1 for write-only — no pump spawned).
    ///     write_fd: fd to write to (default -1 = read-only).
    #[new]
    #[pyo3(signature = (read_fd, write_fd=-1))]
    fn new(read_fd: i32, write_fd: i32) -> Self {
        let core = Arc::new(StdioStreamCore::new());
        let pump_running = Arc::new(AtomicBool::new(false));
        if read_fd >= 0 {
            pump_running.store(true, Ordering::Release);
            let core_pump = Arc::clone(&core);
            let running = Arc::clone(&pump_running);
            std::thread::Builder::new()
                .name("stdio-stream-pump".into())
                .spawn(move || {
                    pump_loop(read_fd, core_pump.as_ref(), running.as_ref());
                })
                .expect("spawn stdio-stream-pump");
        }
        Self {
            core,
            write_fd,
            pump_running,
        }
    }

    /// Write `data` to stdin. Appends `\n` if not present. Returns bytes written.
    fn write_nowait(&self, py: Python<'_>, data: &[u8]) -> PyResult<usize> {
        if self.core.is_closed() {
            return Err(PyRuntimeError::new_err("write to closed stdio stream"));
        }
        if self.write_fd < 0 {
            return Err(PyRuntimeError::new_err("no writer (read-only stream)"));
        }
        let fd = self.write_fd;
        let owned: Vec<u8> = if data.ends_with(b"\n") {
            data.to_vec()
        } else {
            let mut v = Vec::with_capacity(data.len() + 1);
            v.extend_from_slice(data);
            v.push(b'\n');
            v
        };
        py.detach(|| {
            let n = unsafe { libc::write(fd, owned.as_ptr() as *const _, owned.len()) };
            if n < 0 {
                Err(PyRuntimeError::new_err("write failed"))
            } else {
                Ok(n as usize)
            }
        })
    }

    /// Async-shape alias for `write_nowait`. The `blocking` kwarg is
    /// accepted for StreamBackend protocol parity; raw-fd writes are
    /// synchronous either way (caller supplies the async shell via
    /// `asyncio.to_thread` if needed).
    #[pyo3(signature = (data, *, blocking=true))]
    fn write(&self, py: Python<'_>, data: &[u8], blocking: bool) -> PyResult<usize> {
        let _ = blocking;
        self.write_nowait(py, data)
    }

    /// Read one message at `byte_offset`. Returns `(bytes, next_offset)`.
    /// Raises `RuntimeError` with "no data" (open-tail) or "stream closed"
    /// (closed-tail) prefixes — matches Python StreamEmptyError /
    /// StreamClosedError message text.
    fn read_at<'py>(
        &self,
        py: Python<'py>,
        byte_offset: u64,
    ) -> PyResult<(Bound<'py, PyBytes>, u64)> {
        let core = Arc::clone(&self.core);
        let result = py.detach(move || core.read_at(byte_offset));
        match result {
            Ok((data, next)) => Ok((PyBytes::new(py, &data), next)),
            Err(e) => Err(PyRuntimeError::new_err(e.to_string())),
        }
    }

    /// Read up to `count` messages starting at `byte_offset`.
    #[pyo3(signature = (byte_offset=0, count=10))]
    fn read_batch<'py>(
        &self,
        py: Python<'py>,
        byte_offset: u64,
        count: usize,
    ) -> PyResult<(Bound<'py, PyList>, u64)> {
        let core = Arc::clone(&self.core);
        let result = py.detach(move || core.read_batch(byte_offset, count));
        match result {
            Ok((items, next)) => {
                let list = PyList::empty(py);
                for item in items {
                    list.append(PyBytes::new(py, &item))?;
                }
                Ok((list, next))
            }
            Err(e) => Err(PyRuntimeError::new_err(e.to_string())),
        }
    }

    fn close(&self) {
        self.core.close();
        self.pump_running.store(false, Ordering::Release);
        if self.write_fd >= 0 {
            unsafe {
                libc::close(self.write_fd);
            }
        }
    }

    #[getter]
    fn closed(&self) -> bool {
        self.core.is_closed()
    }

    #[getter]
    fn stats<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let (msg_count, total_bytes, closed) = self.core.stats_snapshot();
        let dict = PyDict::new(py);
        dict.set_item("backend", "stdio_stream")?;
        dict.set_item("msg_count", msg_count)?;
        dict.set_item("total_bytes", total_bytes)?;
        dict.set_item("closed", closed)?;
        Ok(dict)
    }

    #[getter]
    fn tail(&self) -> u64 {
        self.core.tail()
    }
}

// ---------------------------------------------------------------------------
// Pump loop (Unix)
// ---------------------------------------------------------------------------

/// Read from `fd` in 4 KiB chunks, feeding bytes into `core` until the
/// fd reports EOF (read returns 0) or an error, or until
/// `running` flips to false.
#[cfg(unix)]
fn pump_loop(fd: i32, core: &StdioStreamCore, running: &AtomicBool) {
    let mut buf = [0u8; 4096];
    while running.load(Ordering::Acquire) {
        let n = unsafe { libc::read(fd, buf.as_mut_ptr() as *mut _, buf.len()) };
        if n > 0 {
            core.feed_bytes(&buf[..n as usize]);
        } else {
            // 0 == EOF, <0 == error; in both cases the stream is terminal.
            break;
        }
    }
    core.feed_eof();
}

// ---------------------------------------------------------------------------
// Unit tests (core only — no real fds needed)
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    fn core_with(lines: &[&[u8]]) -> StdioStreamCore {
        let c = StdioStreamCore::new();
        for line in lines {
            c.feed_bytes(line);
        }
        c
    }

    #[test]
    fn initial_state() {
        let c = StdioStreamCore::new();
        assert_eq!(c.tail(), 0);
        assert!(!c.is_closed());
        let (n, bytes, closed) = c.stats_snapshot();
        assert_eq!((n, bytes, closed), (0, 0, false));
    }

    #[test]
    fn feed_single_line() {
        let c = core_with(&[b"hello\n"]);
        assert_eq!(c.tail(), 6);
        let (data, next) = c.read_at(0).unwrap();
        assert_eq!(data, b"hello\n");
        assert_eq!(next, 6);
    }

    #[test]
    fn feed_multiple_lines_and_read() {
        let c = core_with(&[b"hello\nworld\n"]);
        let (d1, off1) = c.read_at(0).unwrap();
        assert_eq!(d1, b"hello\n");
        let (d2, off2) = c.read_at(off1).unwrap();
        assert_eq!(d2, b"world\n");
        assert_eq!(off2, 12);
    }

    #[test]
    fn feed_incremental_partial_line() {
        let c = StdioStreamCore::new();
        c.feed_bytes(b"hel");
        assert_eq!(c.stats_snapshot().0, 0); // not yet flushed
        c.feed_bytes(b"lo\n");
        assert_eq!(c.stats_snapshot().0, 1);
        let (d, _) = c.read_at(0).unwrap();
        assert_eq!(d, b"hello\n");
    }

    #[test]
    fn feed_eof_flushes_trailing_partial() {
        let c = StdioStreamCore::new();
        c.feed_bytes(b"nofinal");
        c.feed_eof();
        assert_eq!(c.stats_snapshot().0, 1);
        let (d, _) = c.read_at(0).unwrap();
        assert_eq!(d, b"nofinal");
        assert!(c.is_closed());
    }

    #[test]
    fn read_at_empty_open_returns_empty() {
        let c = StdioStreamCore::new();
        assert!(matches!(c.read_at(0), Err(StdioStreamError::Empty(0))));
    }

    #[test]
    fn read_at_empty_closed_returns_closed() {
        let c = StdioStreamCore::new();
        c.close();
        assert!(matches!(c.read_at(0), Err(StdioStreamError::Closed(0))));
    }

    #[test]
    fn read_at_past_end_when_closed() {
        let c = core_with(&[b"only\n"]);
        c.close();
        assert!(matches!(c.read_at(999), Err(StdioStreamError::Closed(999))));
    }

    #[test]
    fn read_at_past_end_when_open() {
        let c = core_with(&[b"only\n"]);
        // offset past tail — open stream returns Empty, not Closed.
        assert!(matches!(c.read_at(999), Err(StdioStreamError::Empty(999))));
    }

    #[test]
    fn read_at_midmessage_rounds_to_next() {
        let c = core_with(&[b"a\nbb\n"]); // "a\n" at 0, "bb\n" at 2
        let (data, next) = c.read_at(1).unwrap();
        assert_eq!(data, b"bb\n");
        assert_eq!(next, 5);
    }

    #[test]
    fn read_batch_all() {
        let c = core_with(&[b"a\nb\nc\nd\ne\n"]);
        let (items, next) = c.read_batch(0, 10).unwrap();
        assert_eq!(items.len(), 5);
        assert_eq!(items[0], b"a\n");
        assert_eq!(items[4], b"e\n");
        assert_eq!(next, 10);
    }

    #[test]
    fn read_batch_partial_then_continue() {
        let c = core_with(&[b"a\nb\nc\nd\ne\n"]);
        let (items, next) = c.read_batch(0, 2).unwrap();
        assert_eq!(items.len(), 2);
        assert_eq!(items[0], b"a\n");
        let (items2, _) = c.read_batch(next, 2).unwrap();
        assert_eq!(items2.len(), 2);
        assert_eq!(items2[0], b"c\n");
    }

    #[test]
    fn read_batch_empty_open_returns_empty() {
        let c = StdioStreamCore::new();
        assert!(matches!(
            c.read_batch(0, 10),
            Err(StdioStreamError::Empty(0))
        ));
    }

    #[test]
    fn read_batch_past_end_returns_empty_or_closed() {
        let c = core_with(&[b"only\n"]);
        assert!(matches!(
            c.read_batch(999, 10),
            Err(StdioStreamError::Empty(999))
        ));
        c.close();
        assert!(matches!(
            c.read_batch(999, 10),
            Err(StdioStreamError::Closed(999))
        ));
    }

    #[test]
    fn tail_monotonic() {
        let c = core_with(&[b"msg1\n"]);
        assert_eq!(c.tail(), 5);
        c.feed_bytes(b"second\n");
        assert_eq!(c.tail(), 12);
    }

    #[test]
    fn stats_track_msg_count_and_bytes() {
        let c = core_with(&[b"ab\n", b"cdef\n"]);
        let (n, bytes, closed) = c.stats_snapshot();
        assert_eq!(n, 2);
        assert_eq!(bytes, 3 + 5);
        assert!(!closed);
    }

    #[test]
    fn feed_after_close_is_noop() {
        let c = StdioStreamCore::new();
        c.close();
        c.feed_bytes(b"late\n");
        assert_eq!(c.stats_snapshot().0, 0);
    }

    #[test]
    fn multireader_independent_cursors() {
        let c = core_with(&[b"a\nb\nc\n"]);
        let (_, off1) = c.read_at(0).unwrap();
        let (d1b, _) = c.read_at(0).unwrap(); // second reader re-reads
        assert_eq!(d1b, b"a\n");
        let (d2, _) = c.read_at(off1).unwrap();
        assert_eq!(d2, b"b\n");
    }
}
