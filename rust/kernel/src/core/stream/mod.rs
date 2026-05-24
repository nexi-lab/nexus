//! Linear append-only buffer for DT_STREAM kernel IPC (Task #1574).
//!
//! Semantic: log/topic (Kafka, Redis Streams, NATS JetStream). Reads are
//! non-destructive and offset-based; the caller owns the cursor. Multiple
//! readers fan out from independent offsets. This is the inverse of
//! DT_PIPE (FIFO, destructive pop, single consumer): a stream replays.
//!
//! Message framing: `[4B u32 LE length][N bytes payload]`.
//! No sentinel, no wrap-around — fundamentally simpler than the ring buffer.
//!
//! Read contract from Python: ``sys_read(path, offset)`` on a DT_STREAM
//! returns ``{"data": bytes, "next_offset": int}`` so callers can advance
//! their cursor without decoding the 4-byte LE frame header. ``next_offset``
//! is the byte offset of the *next* frame, suitable to pass back as
//! ``offset`` on the following ``sys_read``. DT_REG / DT_PIPE return raw
//! bytes — the dict shape is gated on the DT_STREAM entry type.
//!
//! Error encoding: Rust raises `RuntimeError("StreamFull:…")` etc. Python
//! translates to the matching exception class.

// §4.2 — DT_STREAM pillar.
// `wal.rs` — WAL-replicated stream backend.  Kernel primitive that
// composes whatever distributed `MetaStore` impl federation has DI'd
// (typically `ZoneMetaStore` from the raft crate) — kernel does not
// name raft types directly, layering stays clean.
pub mod backend;
pub mod manager;
pub mod observer;
pub mod remote;
#[cfg(unix)]
pub mod shm;
pub mod stdio;
pub mod wal;

use std::cell::UnsafeCell;
use std::sync::atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering};

use parking_lot::Mutex;

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/// Frame header size: 4-byte u32 LE length prefix.
const HEADER_SIZE: usize = 4;

// ---------------------------------------------------------------------------
// MemoryStreamBackend
// ---------------------------------------------------------------------------

/// Linear append-only buffer for DT_STREAM.
///
/// Pre-allocated linear buffer with monotonic tail. Reads are
/// non-destructive and offset-based — each reader supplies its own
/// byte offset and any number of readers can run concurrently.
/// Writers are serialized internally so multi-threaded `sys_write`
/// to the same DT_STREAM path is safe without any external lock.
/// Kernel-internal — callers go through `StreamManager` via the
/// `Kernel::create_stream` / `Kernel::stream_write_nowait` surface.
pub struct MemoryStreamBackend {
    buf: UnsafeCell<Vec<u8>>,
    capacity: usize,
    tail: AtomicUsize,
    closed: AtomicBool,
    push_count: AtomicU64,
    msg_count: AtomicUsize,
    /// Serializes writers so the exclusive `&mut [u8]` borrow inside
    /// `push()` is the only one alive for that frame. Readers do not
    /// take this lock — they only Acquire-load `tail` and read
    /// already-committed bytes.
    writer: Mutex<()>,
}

// SAFETY: The `writer` mutex serializes all writers, so the only
// thread that holds the exclusive `&mut [u8]` borrow via
// `UnsafeCell::get()` is the current writer. Readers do not touch
// `&mut`: they Acquire-load `tail` and read `[offset..offset+len]`
// for `offset + len <= tail`, and those bytes are already committed
// by the writer's Release-store of `tail` before the mutex was
// dropped. `Send + Sync` is therefore sound under arbitrary
// multi-thread use.
unsafe impl Send for MemoryStreamBackend {}
unsafe impl Sync for MemoryStreamBackend {}

// ---------------------------------------------------------------------------
// StreamBackend / StreamError live in this directory's `backend.rs`.
// The trait is kernel-internal — not a §3 ABC pillar, just an
// abstraction for the IPC subsystem — so it sits with its primitive
// impl rather than under `crate::abc/` or `crate::hal/`. Re-exported
// here so `crate::stream::StreamBackend` / `crate::stream::StreamError`
// paths used throughout the kernel keep resolving without per-caller
// churn.
// ---------------------------------------------------------------------------

pub use backend::{StreamBackend, StreamError};

// ---------------------------------------------------------------------------
// StreamBackend impl for MemoryStreamBackend
// ---------------------------------------------------------------------------

impl StreamBackend for MemoryStreamBackend {
    fn push(&self, data: &[u8]) -> Result<usize, StreamError> {
        MemoryStreamBackend::push(self, data)
    }
    fn read_at(&self, offset: usize) -> Result<(Vec<u8>, usize), StreamError> {
        MemoryStreamBackend::read_at(self, offset)
    }
    fn read_batch(
        &self,
        offset: usize,
        count: usize,
    ) -> Result<(Vec<Vec<u8>>, usize), StreamError> {
        MemoryStreamBackend::read_batch(self, offset, count)
    }
    fn close(&self) {
        MemoryStreamBackend::close(self)
    }
    fn is_closed(&self) -> bool {
        self.closed.load(Ordering::Acquire)
    }
    fn tail_offset(&self) -> usize {
        self.tail.load(Ordering::Acquire)
    }
    fn msg_count(&self) -> usize {
        self.msg_count.load(Ordering::Relaxed)
    }
}

// ---------------------------------------------------------------------------
// Internal helpers — pub(crate) for direct Kernel IPC registry access
// ---------------------------------------------------------------------------

impl MemoryStreamBackend {
    /// Push raw bytes into the buffer. Returns byte offset where message starts.
    pub(crate) fn push(&self, data: &[u8]) -> Result<usize, StreamError> {
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

        // Serialize writers so the `&mut [u8]` borrow below is the
        // only one in flight; held across the Release-store of `tail`
        // so readers see a fully-published frame.
        let _writer_guard = self.writer.lock();

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

    /// Read one message at the given byte offset. Returns (payload_start, payload_len, next_offset).
    pub(crate) fn read_at_position(
        &self,
        byte_offset: usize,
    ) -> Result<(usize, usize, usize), StreamError> {
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

    /// Read one message at byte offset, returning owned bytes and next offset.
    pub(crate) fn read_at(&self, byte_offset: usize) -> Result<(Vec<u8>, usize), StreamError> {
        let (payload_start, payload_len, next_offset) = self.read_at_position(byte_offset)?;
        let buf = unsafe { &*self.buf.get() };
        let data = buf[payload_start..payload_start + payload_len].to_vec();
        Ok((data, next_offset))
    }

    /// Read up to `count` messages starting from byte offset.
    /// Returns (messages, next_offset after last message).
    pub(crate) fn read_batch(
        &self,
        byte_offset: usize,
        count: usize,
    ) -> Result<(Vec<Vec<u8>>, usize), StreamError> {
        let mut results = Vec::with_capacity(count);
        let mut offset = byte_offset;
        for _ in 0..count {
            match self.read_at(offset) {
                Ok((data, next)) => {
                    results.push(data);
                    offset = next;
                }
                Err(StreamError::Empty) | Err(StreamError::ClosedEmpty) => break,
                Err(e) => return Err(e),
            }
        }
        Ok((results, offset))
    }

    /// Check if the stream is closed.
    #[allow(dead_code)]
    pub(crate) fn is_closed(&self) -> bool {
        self.closed.load(Ordering::Acquire)
    }

    /// Create a new MemoryStreamBackend.
    pub(crate) fn new(capacity: usize) -> Self {
        Self {
            buf: UnsafeCell::new(vec![0u8; capacity]),
            capacity,
            tail: AtomicUsize::new(0),
            closed: AtomicBool::new(false),
            push_count: AtomicU64::new(0),
            msg_count: AtomicUsize::new(0),
            writer: Mutex::new(()),
        }
    }

    /// Signal close.
    pub(crate) fn close(&self) {
        self.closed.store(true, Ordering::Release);
    }

    /// Current write position (monotonic tail offset).
    #[allow(dead_code)]
    pub(crate) fn tail_offset(&self) -> usize {
        self.tail.load(Ordering::Acquire)
    }

    /// Is the buffer closed?
    #[allow(dead_code)]
    pub(crate) fn closed(&self) -> bool {
        self.closed.load(Ordering::Acquire)
    }

    /// Number of messages in the buffer.
    #[allow(dead_code)]
    pub(crate) fn msg_count(&self) -> usize {
        self.msg_count.load(Ordering::Relaxed)
    }

    /// Current tail position.
    #[allow(dead_code)]
    pub(crate) fn tail(&self) -> usize {
        self.tail.load(Ordering::Relaxed)
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    fn make(cap: usize) -> MemoryStreamBackend {
        MemoryStreamBackend::new(cap)
    }

    fn push(core: &MemoryStreamBackend, data: &[u8]) -> usize {
        core.push(data).expect("push failed")
    }

    fn read_at(core: &MemoryStreamBackend, offset: usize) -> (Vec<u8>, usize) {
        let (start, len, next) = core.read_at_position(offset).expect("read_at failed");
        let buf = unsafe { &*core.buf.get() };
        (buf[start..start + len].to_vec(), next)
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
        assert!(core.push(b"data").is_err());
    }

    #[test]
    fn test_oversized_rejected() {
        let core = make(10);
        match core.push(&[0u8; 11]) {
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
        match core.push(b"x") {
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
        assert!(core.read_at_position(0).is_err());
    }

    #[test]
    fn test_read_closed_empty_error() {
        let core = make(1024);
        core.close();
        match core.read_at_position(0) {
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
        match core.read_at_position(next) {
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
        match core.push(b"x") {
            Err(StreamError::Full(_, _)) => {}
            other => panic!("expected Full, got {:?}", other.is_ok()),
        }
    }

    #[test]
    fn test_u64_push_read() {
        let core = make(1024);
        let o1 = core.push(&42u64.to_le_bytes()).unwrap();
        let o2 = core.push(&u64::MAX.to_le_bytes()).unwrap();
        let (d1, _) = read_at(&core, o1);
        let (d2, _) = read_at(&core, o2);
        assert_eq!(u64::from_le_bytes(d1.try_into().unwrap()), 42);
        assert_eq!(u64::from_le_bytes(d2.try_into().unwrap()), u64::MAX);
    }

    /// Concurrent multi-writer push must not corrupt the buffer:
    /// every pushed message frame must be readable from its returned
    /// offset, and the count of distinct payloads observed by a
    /// reader must equal the number of pushes. Before the writer
    /// mutex was added, the `&mut [u8]` borrow inside `push` could
    /// alias across threads.
    #[test]
    fn test_concurrent_writers_do_not_corrupt() {
        use std::sync::Arc;
        use std::thread;

        const WRITERS: usize = 8;
        const PER_WRITER: usize = 64;
        // Frame = 4-byte header + 8-byte u64 payload = 12 bytes per push.
        let capacity = WRITERS * PER_WRITER * 12;
        let core = Arc::new(make(capacity));

        let handles: Vec<_> = (0..WRITERS)
            .map(|w| {
                let core = Arc::clone(&core);
                thread::spawn(move || {
                    let mut offsets = Vec::with_capacity(PER_WRITER);
                    for i in 0..PER_WRITER {
                        let val = (w as u64) << 32 | (i as u64);
                        offsets.push(core.push(&val.to_le_bytes()).expect("push failed"));
                    }
                    offsets
                })
            })
            .collect();

        let mut all_offsets: Vec<usize> = handles
            .into_iter()
            .flat_map(|h| h.join().expect("writer thread panicked"))
            .collect();
        assert_eq!(all_offsets.len(), WRITERS * PER_WRITER);

        // Every returned offset addresses a valid 8-byte frame.
        for off in &all_offsets {
            let (data, _) = read_at(&core, *off);
            assert_eq!(data.len(), 8);
        }
        // Offsets are pairwise distinct — no two writers got the same
        // slot.
        all_offsets.sort_unstable();
        all_offsets.dedup();
        assert_eq!(all_offsets.len(), WRITERS * PER_WRITER);

        // Walking the buffer linearly from offset 0 must yield exactly
        // WRITERS * PER_WRITER frames, in the order writers' Release
        // stores published them.
        let mut walked = 0usize;
        let mut offset = 0usize;
        while let Ok((data, next)) = core.read_at(offset) {
            assert_eq!(data.len(), 8);
            walked += 1;
            if next == offset {
                break;
            }
            offset = next;
        }
        assert_eq!(walked, WRITERS * PER_WRITER);
    }
}
