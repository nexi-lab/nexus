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

// Phase C nested layout (§4.2 — DT_PIPE pillar):
//   core/pipe/mod.rs     — MemoryPipeBackend  (was kernel/src/pipe.rs)
//   core/pipe/manager.rs — PipeManager        (was kernel/src/pipe_manager.rs)
//   core/pipe/shm.rs     — SharedMemoryPipe   (was kernel/src/shm_pipe.rs)
//   core/pipe/stdio.rs   — StdioPipeBackend   (was kernel/src/stdio_pipe.rs)
//   core/pipe/remote.rs  — RemotePipeBackend  (was kernel/src/remote_pipe.rs)
pub mod backend;
pub mod manager;
pub mod remote;
#[cfg(unix)]
pub mod shm;
#[cfg(unix)]
pub mod stdio;
// `pub mod wal;` deferred — WalPipeCore composed `WalStreamCore` from
// the kernel-side `wal_stream` module, but Phase H of the rust-workspace
// restructure moved that module to `nexus_raft::wal_stream_backend`
// (kernel↔raft Cargo edge flip).  Re-introducing WAL pipe support
// requires wiring it through the new
// `kernel::hal::federation::FederationProvider::{append_wal_entry,
// get_wal_entry}` trait surface (added in this PR for exactly this
// purpose).  Tracked as a follow-up so #3933's WAL pipe semantics can
// land cleanly on top of Phase H without re-introducing the kernel→raft
// edge.

use std::cell::UnsafeCell;
use std::sync::atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering};

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/// Frame header size: 4-byte u32 LE length prefix.
const HEADER_SIZE: usize = 4;

// ---------------------------------------------------------------------------
// MemoryPipeBackend
// ---------------------------------------------------------------------------

/// Lock-free SPSC ring buffer core for DT_PIPE.
///
/// Contiguous byte ring with atomic monotonic head/tail counters.
/// Kernel-internal only — no PyO3 export. Python uses kernel.create_pipe().
pub struct MemoryPipeBackend {
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
unsafe impl Send for MemoryPipeBackend {}
unsafe impl Sync for MemoryPipeBackend {}

// ---------------------------------------------------------------------------
// PipeBackend / PipeError live in this directory's `backend.rs` sub-file
// (Phase 1 — moved out of the now-deleted `core/traits/`).  The trait is
// kernel-internal — not a §3 ABC pillar, just an abstraction for the IPC
// subsystem — so it sits with its primitive impl rather than under
// `crate::abc/` or `crate::hal/`.  Re-exported here so
// `crate::pipe::PipeBackend` / `crate::pipe::PipeError` paths used
// throughout the kernel keep resolving without per-caller churn.
// ---------------------------------------------------------------------------

pub(crate) use backend::{PipeBackend, PipeError};

// ---------------------------------------------------------------------------
// Internal helpers — pub(crate) for direct Kernel IPC registry access
// ---------------------------------------------------------------------------

impl PipeBackend for MemoryPipeBackend {
    fn push(&self, data: &[u8]) -> Result<usize, PipeError> {
        MemoryPipeBackend::push(self, data)
    }
    fn pop(&self) -> Result<Vec<u8>, PipeError> {
        MemoryPipeBackend::pop(self)
    }
    fn close(&self) {
        MemoryPipeBackend::close(self)
    }
    fn is_closed(&self) -> bool {
        self.closed.load(Ordering::Acquire)
    }
    fn is_empty(&self) -> bool {
        MemoryPipeBackend::is_empty(self)
    }
    fn size(&self) -> usize {
        MemoryPipeBackend::size(self)
    }
    fn msg_count(&self) -> usize {
        self.msg_count.load(Ordering::Relaxed)
    }
}

impl MemoryPipeBackend {
    /// Push raw bytes into the ring. Returns payload length on success.
    pub(crate) fn push(&self, data: &[u8]) -> Result<usize, PipeError> {
        if self.closed.load(Ordering::Acquire) {
            return Err(PipeError::Closed("write to closed pipe"));
        }
        let payload_len = data.len();
        if payload_len == 0 {
            return Ok(0);
        }
        if payload_len > self.user_capacity {
            return Err(PipeError::Oversized(payload_len, self.user_capacity));
        }

        let used = self.used_bytes.load(Ordering::Relaxed);
        if used + payload_len > self.user_capacity {
            return Err(PipeError::Full(used, self.user_capacity));
        }

        let frame_len = HEADER_SIZE + payload_len;
        let tail = self.tail.load(Ordering::Relaxed);
        let head = self.head.load(Ordering::Acquire);
        let tail_idx = tail % self.ring_cap;
        let contiguous = self.ring_cap - tail_idx;

        // Physical ring-space check: payloads alone cannot be trusted
        // because `user_capacity` ignores 4-byte frame headers and
        // potential wrap sentinels. For tiny payloads (< HEADER_SIZE)
        // the header overhead alone exceeds the slack between
        // `user_capacity` and `ring_cap`. Enforce the invariant
        //   (tail - head) + <bytes we are about to write> <= ring_cap
        // where we must include a potential sentinel AND the sentinel
        // itself has to fit in the contiguous trailing region
        // (HEADER_SIZE bytes). Without the contiguous >= HEADER_SIZE
        // guard, the sentinel write below would panic on a slice
        // out-of-range for very small rings.
        let need = if frame_len > contiguous {
            if contiguous < HEADER_SIZE {
                // Not enough tail-contiguous room for the sentinel
                // header; rather than partial-writing or silently
                // wrapping, fail cleanly.
                return Err(PipeError::Full(used, self.user_capacity));
            }
            contiguous + frame_len
        } else {
            frame_len
        };
        if tail.saturating_sub(head) + need > self.ring_cap {
            return Err(PipeError::Full(used, self.user_capacity));
        }

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
    pub(crate) fn pop_position(&self) -> Result<(usize, usize, usize), PipeError> {
        let mut head = self.head.load(Ordering::Acquire);
        let tail = self.tail.load(Ordering::Acquire);

        loop {
            if head == tail {
                return if self.closed.load(Ordering::Acquire) {
                    Err(PipeError::ClosedEmpty)
                } else {
                    Err(PipeError::Empty)
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
    pub(crate) fn commit_pop(&self, total_advance: usize, payload_len: usize) {
        let head = self.head.load(Ordering::Relaxed);
        self.head.store(head + total_advance, Ordering::Release);
        self.msg_count.fetch_sub(1, Ordering::Relaxed);
        self.used_bytes.fetch_sub(payload_len, Ordering::Relaxed);
        self.pop_count.fetch_add(1, Ordering::Relaxed);
    }

    /// Pop one message from the ring, returning owned bytes (no PyBytes dependency).
    /// Combines pop_position + ring copy + commit_pop.
    pub(crate) fn pop(&self) -> Result<Vec<u8>, PipeError> {
        let (payload_start, payload_len, total_advance) = self.pop_position()?;
        let ring = unsafe { &*self.ring.get() };
        let data = ring[payload_start..payload_start + payload_len].to_vec();
        self.commit_pop(total_advance, payload_len);
        Ok(data)
    }

    /// Check if the pipe is closed.
    #[allow(dead_code)]
    pub(crate) fn is_closed(&self) -> bool {
        self.closed.load(Ordering::Acquire)
    }

    /// Create a new MemoryPipeBackend (pub(crate), no PyO3).
    pub(crate) fn new(capacity: usize) -> Self {
        let ring_cap = capacity * 2;
        Self {
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
        }
    }

    /// Signal close.
    pub(crate) fn close(&self) {
        self.closed.store(true, Ordering::Release);
    }

    /// Check if buffer is empty (no messages).
    #[allow(dead_code)]
    pub(crate) fn is_empty(&self) -> bool {
        self.msg_count.load(Ordering::Relaxed) == 0
    }

    /// Check if buffer is full (used bytes >= capacity).
    #[allow(dead_code)]
    pub(crate) fn is_full(&self) -> bool {
        self.used_bytes.load(Ordering::Relaxed) >= self.user_capacity
    }

    /// Current used bytes.
    #[allow(dead_code)]
    pub(crate) fn size(&self) -> usize {
        self.used_bytes.load(Ordering::Relaxed)
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
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    fn make(cap: usize) -> MemoryPipeBackend {
        MemoryPipeBackend::new(cap)
    }

    /// Test-only push helper (bypasses PyO3 Python parameter).
    fn push(core: &MemoryPipeBackend, data: &[u8]) -> usize {
        core.push(data).expect("push failed in test helper")
    }

    /// Test-only pop helper (bypasses PyO3, returns raw bytes).
    fn pop(core: &MemoryPipeBackend) -> Vec<u8> {
        let (start, len, advance) = core.pop_position().expect("pop failed in test helper");
        let ring = unsafe { &*core.ring.get() };
        let data = ring[start..start + len].to_vec();
        core.commit_pop(advance, len);
        data
    }

    /// Test-only push_u64 helper.
    fn push_u64(core: &MemoryPipeBackend, val: u64) {
        core.push(&val.to_le_bytes())
            .expect("push_u64 failed in test helper");
    }

    /// Test-only pop_u64 helper.
    fn pop_u64(core: &MemoryPipeBackend) -> u64 {
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
        assert!(core.push(b"data").is_err());
    }

    #[test]
    fn test_oversized_rejected() {
        let core = make(10);
        match core.push(&[0u8; 11]) {
            Err(PipeError::Oversized(11, 10)) => {}
            other => panic!("expected Oversized, got {:?}", other.is_ok()),
        }
    }

    #[test]
    fn test_full_rejected() {
        let core = make(10);
        push(&core, &[0u8; 10]);
        match core.push(b"x") {
            Err(PipeError::Full(10, 10)) => {}
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
            Err(PipeError::ClosedEmpty) => {}
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
            Err(PipeError::ClosedEmpty) => {}
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
        // user_capacity=128, ring_cap=256.
        // Push+pop one at a time to advance head/tail without
        // exceeding user_capacity.
        let core = make(128);

        // 60-byte payload → 64-byte frame (4-byte header).
        // Push+pop 3× to advance head=tail to 192.
        for _ in 0..3 {
            push(&core, &[0xFF; 60]);
            pop(&core);
        }
        // head=tail=192, 64 bytes remaining to ring end.
        // Push 56-byte payload (60-byte frame) — fits in 64 bytes.
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

    // -- Ring-physical-space regression (§ review fix #1) --

    #[test]
    fn test_many_tiny_pushes_cannot_overwrite_unread() {
        // user_capacity=32, ring_cap=64. Each 1-byte payload consumes a
        // 5-byte frame in the ring. Without the physical-space guard, 13+
        // consecutive pushes would wrap past head=0 and corrupt data.
        let core = make(32);
        let mut accepted = 0usize;
        for _ in 0..64 {
            match core.push(&[0xAAu8]) {
                Ok(_) => accepted += 1,
                Err(PipeError::Full(_, _)) => break,
                Err(e) => panic!("unexpected push error: {e:?}"),
            }
        }
        // Floor: floor(ring_cap / frame_len) = 64 / 5 = 12. Must reject the 13th.
        assert!(
            accepted <= 12,
            "ring overflow accepted {accepted} pushes of frame_len=5 into ring_cap=64"
        );
        // Every accepted payload must still be readable in order.
        for _ in 0..accepted {
            assert_eq!(pop(&core), vec![0xAAu8]);
        }
    }

    #[test]
    fn test_tiny_push_fails_before_overwriting_with_sentinel() {
        let core = make(16);
        // Fill to near capacity then force sentinel + wrap, verifying the
        // sentinel-requiring push is rejected when the ring cannot absorb
        // the waste + new frame without overtaking head.
        for _ in 0..5 {
            let _ = core.push(b"x");
        }
        // Pop just enough to leave head < tail but keep ring nearly full.
        let _ = core.pop();
        // Do not panic; whatever the implementation decides must preserve
        // the invariant tail - head <= ring_cap after the next push.
        for _ in 0..100 {
            let _ = core.push(b"y");
        }
        // Invariant check: size() <= user_capacity and remaining pops match.
        let used = core.msg_count();
        for _ in 0..used {
            let v = pop(&core);
            assert_eq!(v.len(), 1);
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
                        match core.push(&(i as u64).to_le_bytes()) {
                            Ok(_) => break,
                            Err(PipeError::Full(_, _)) => {
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
                            Err(PipeError::Empty) => {
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
