//! `StdioStreamCore` — newline-framed accumulation buffer for
//! DT_STREAM backends that consume OS pipe / subprocess output.
//!
//! Each call to `feed_bytes` appends bytes; line terminators split
//! the stream into indexed messages, enabling offset-based multi-
//! reader access (`read_at` returns the message starting at or after
//! a given byte offset, non-destructively).  Cross-platform and
//! unit-testable without any OS pipes via `feed_bytes` / `feed_eof`.
//!
//! Kernel-internal: the core is composed by stream backends inside
//! the kernel (e.g. for subprocess stdout streams when a future
//! `io_profile` is added).  No PyO3 surface — Python reaches it via
//! the DT_STREAM syscalls (`sys_read` / `sys_write`).

use std::sync::{Condvar, Mutex};

// ---------------------------------------------------------------------------
// Error types
// ---------------------------------------------------------------------------

/// Errors returned by `StdioStreamCore`.
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

    /// Close the stream (no final partial flush — caller sets closed explicitly).
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
