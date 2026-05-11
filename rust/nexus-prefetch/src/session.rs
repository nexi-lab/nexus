//! Per-file-handle state — the unit of book-keeping for one open fh.
//! Owns its own detector (boxed), the readahead window (doubles on
//! every confirmed sequential hit, capped at `max_window`), the set
//! of pending block offsets (to dedupe in-flight prefetches), and the
//! deposited buffer (block offset → bytes).

use crate::config::EngineConfig;
use crate::detector::Detector;
use bytes::Bytes;
use std::collections::{BTreeMap, BTreeSet};

pub struct Session {
    pub path: String,
    pub fh: u64,
    pub file_size: Option<u64>,
    pub window: u64,
    pub max_window: u64,
    pub detector: Box<dyn Detector>,
    pub pending: BTreeSet<u64>,
    pub buffer: BTreeMap<u64, Bytes>,
    pub hits: u64,
    pub misses: u64,
    /// Globally unique session identity — issued from the engine's
    /// `next_session_id` AtomicU64 on every `on_open` AND on every
    /// `clear()` (invalidate-while-open).  Workers stamp the snapshot
    /// at enqueue and reject deposits whose token no longer matches.
    /// Two different fh lifetimes therefore never collide (the previous
    /// `generation: u64` started at 1 for every new Session and was
    /// vulnerable to fh-reuse — codex round 3 finding #1).
    pub session_id: u64,
    /// High-water mark of consumed bytes — the largest `offset+size`
    /// the caller has read so far.  Used by `take_range` to evict
    /// blocks the read cursor has fully moved past, bounding the
    /// per-fh buffer footprint under long sequential scans (round 3
    /// finding #2).
    pub consumed_end: u64,
}

impl Session {
    pub fn new(
        path: String,
        fh: u64,
        file_size: Option<u64>,
        detector: Box<dyn Detector>,
        cfg: &EngineConfig,
        session_id: u64,
    ) -> Self {
        Self {
            path,
            fh,
            file_size,
            window: cfg.initial_window,
            max_window: cfg.max_window,
            detector,
            pending: BTreeSet::new(),
            buffer: BTreeMap::new(),
            hits: 0,
            misses: 0,
            session_id,
            consumed_end: 0,
        }
    }

    /// Double the window, capped at `max_window`.  Called after a
    /// confirmed sequential observation.
    pub fn grow_window(&mut self) {
        let doubled = self.window.saturating_mul(2);
        self.window = doubled.min(self.max_window);
    }

    /// Reset window to initial — invoked on `AccessPattern::Random`
    /// from the detector.  Also clears pending so we stop awaiting
    /// blocks the stream no longer cares about.
    pub fn shrink_and_clear(&mut self, initial_window: u64) {
        self.window = initial_window;
        self.pending.clear();
    }

    /// Mark a block offset as pending prefetch.  Returns `false` if
    /// already in flight or already buffered (caller should skip).
    pub fn mark_pending(&mut self, block_offset: u64) -> bool {
        if self.pending.contains(&block_offset) || self.buffer.contains_key(&block_offset) {
            return false;
        }
        self.pending.insert(block_offset);
        true
    }

    /// Deposit a completed block; remove from pending.  Zero-length
    /// blocks are dropped (would otherwise hang `take_range`'s cursor
    /// loop with no progress).  Blocks entirely behind `consumed_end`
    /// are also dropped — the read cursor has already moved past them
    /// so they'd be evicted on the next take_range anyway, and
    /// retaining them between deposit and take_range needlessly
    /// inflates the buffer (round 4 finding #1).
    pub fn deposit(&mut self, block_offset: u64, bytes: Bytes) {
        self.pending.remove(&block_offset);
        if bytes.is_empty() {
            return;
        }
        let block_end = block_offset + bytes.len() as u64;
        if block_end <= self.consumed_end {
            return;
        }
        self.buffer.insert(block_offset, bytes);
    }

    /// Advance the consumed-bytes high-water mark for any observed
    /// read (hit OR miss).  Called from `PrefetchEngine::on_read`
    /// before `take_range`.  Round 4 finding #1.
    pub fn note_read(&mut self, offset: u64, size: u32) {
        let end = offset.saturating_add(size as u64);
        if end > self.consumed_end {
            self.consumed_end = end;
        }
    }

    /// Take a buffered range if it fully covers `[offset, offset+size)`.
    /// Returns `None` on miss.  Only blocks whose contents are fully
    /// consumed by the read are removed from the buffer; the trailing
    /// block keeps any unconsumed bytes for a subsequent overlapping
    /// read.  Defends against zero-length blocks (cursor non-progression).
    pub fn take_range(&mut self, offset: u64, size: u32, block_size: u32) -> Option<Bytes> {
        if size == 0 {
            return Some(Bytes::new());
        }
        let end = offset.checked_add(size as u64)?;
        let bs = block_size as u64;
        if bs == 0 {
            return None;
        }
        let first_block = (offset / bs) * bs;
        let mut out = Vec::with_capacity(size as usize);
        let mut cursor = first_block;
        // First pass: copy bytes out of the buffered blocks WITHOUT
        // mutating the buffer.  Any miss returns None with the buffer
        // untouched.
        while cursor < end {
            let block = self.buffer.get(&cursor)?;
            if block.is_empty() {
                // Defensive — `deposit` guards against this, but a
                // future code path might insert one.  Treat as miss
                // so we don't loop forever.
                return None;
            }
            let block_end = cursor + block.len() as u64;
            let take_from = offset.max(cursor) - cursor;
            let take_to = end.min(block_end) - cursor;
            out.extend_from_slice(&block[take_from as usize..take_to as usize]);
            cursor = block_end;
        }
        // Second pass: remove blocks the read cursor has fully moved
        // past.  `consumed_end` is set by `PrefetchEngine::on_read`
        // before this call (round 4 finding #1 — advances on miss too,
        // so backend-served reads also retire stale prefetched blocks).
        // A block is dead when its trailing edge is at or before
        // `consumed_end`; partial overlaps are preserved.
        let cursor = self.consumed_end;
        let to_remove: Vec<u64> = self
            .buffer
            .iter()
            .filter_map(|(k, v)| {
                let block_start = *k;
                let block_end = block_start + v.len() as u64;
                if block_end <= cursor {
                    Some(block_start)
                } else {
                    None
                }
            })
            .collect();
        for k in to_remove {
            self.buffer.remove(&k);
        }
        self.hits += 1;
        Some(Bytes::from(out))
    }

    /// Drop all pending and buffered state.  The caller (engine) MUST
    /// re-assign `session_id` from its global counter so in-flight
    /// prefetch jobs are rejected at deposit time.
    pub fn clear(&mut self) {
        self.pending.clear();
        self.buffer.clear();
        self.consumed_end = 0;
    }

    /// Snapshot the current session identity — workers stamp this onto
    /// the job at enqueue and check it before mutating the buffer.
    pub fn session_id(&self) -> u64 {
        self.session_id
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::detector::SequentialDetector;

    fn sess(cfg: &EngineConfig) -> Session {
        Session::new(
            "/x".into(),
            1,
            Some(1024 * 1024),
            Box::new(SequentialDetector::new(
                cfg.sequential_tolerance,
                cfg.min_sequential_count,
            )),
            cfg,
            42, // session_id, test-only
        )
    }

    #[test]
    fn window_doubles_capped_at_max() {
        let cfg = EngineConfig {
            initial_window: 1024,
            max_window: 4096,
            ..Default::default()
        };
        let mut s = sess(&cfg);
        assert_eq!(s.window, 1024);
        s.grow_window();
        assert_eq!(s.window, 2048);
        s.grow_window();
        assert_eq!(s.window, 4096);
        s.grow_window();
        assert_eq!(s.window, 4096); // capped
    }

    #[test]
    fn shrink_resets_window_and_pending() {
        let cfg = EngineConfig::default();
        let mut s = sess(&cfg);
        s.window = 1024 * 1024;
        s.mark_pending(0);
        s.mark_pending(4096);
        s.shrink_and_clear(cfg.initial_window);
        assert_eq!(s.window, cfg.initial_window);
        assert!(s.pending.is_empty());
    }

    #[test]
    fn mark_pending_dedupes() {
        let cfg = EngineConfig::default();
        let mut s = sess(&cfg);
        assert!(s.mark_pending(0));
        assert!(!s.mark_pending(0));
    }

    #[test]
    fn take_range_hits_when_blocks_present() {
        let cfg = EngineConfig {
            block_size: 16,
            ..Default::default()
        };
        let mut s = sess(&cfg);
        s.deposit(0, Bytes::from(vec![1u8; 16]));
        s.deposit(16, Bytes::from(vec![2u8; 16]));
        let out = s.take_range(8, 16, 16).expect("hit");
        assert_eq!(out.len(), 16);
        assert_eq!(out[0], 1);
        assert_eq!(out[7], 1);
        assert_eq!(out[8], 2);
    }

    #[test]
    fn take_range_misses_when_block_absent() {
        let cfg = EngineConfig {
            block_size: 16,
            ..Default::default()
        };
        let mut s = sess(&cfg);
        assert!(s.take_range(0, 16, 16).is_none());
    }

    #[test]
    fn take_range_keeps_partial_end_block() {
        // 4 MiB-style block holding multiple 4 KiB-style sub-reads: the
        // first read consumes only a slice; the block must remain in
        // the buffer so the next read still hits.
        let cfg = EngineConfig {
            block_size: 16,
            ..Default::default()
        };
        let mut s = sess(&cfg);
        s.deposit(0, Bytes::from((0u8..16).collect::<Vec<_>>()));
        // First 4-byte sub-read at offset 0..4 → block 0 must stay.
        let r1 = s.take_range(0, 4, 16).expect("sub-read 1 hits");
        assert_eq!(&r1[..], &[0, 1, 2, 3]);
        assert!(s.buffer.contains_key(&0), "partial-end block was removed");
        // Second 4-byte sub-read at offset 4..8 → still a hit.
        let r2 = s.take_range(4, 4, 16).expect("sub-read 2 hits");
        assert_eq!(&r2[..], &[4, 5, 6, 7]);
    }

    #[test]
    fn take_range_removes_only_fully_consumed_blocks() {
        let cfg = EngineConfig {
            block_size: 8,
            ..Default::default()
        };
        let mut s = sess(&cfg);
        s.deposit(0, Bytes::from(vec![1u8; 8]));
        s.deposit(8, Bytes::from(vec![2u8; 8]));
        s.deposit(16, Bytes::from(vec![3u8; 8]));
        // Consume blocks 0 and 8 fully (offsets 0..16) plus 4 bytes of
        // block 16.  Round 4: the engine advances `consumed_end` via
        // `note_read` before calling `take_range`, so we mirror that
        // here.  Blocks 0 and 8 should be removed; block 16 must stay
        // because 4 of its 8 bytes are unconsumed.
        s.note_read(0, 20);
        let out = s.take_range(0, 20, 8).expect("hit");
        assert_eq!(out.len(), 20);
        assert!(!s.buffer.contains_key(&0));
        assert!(!s.buffer.contains_key(&8));
        assert!(s.buffer.contains_key(&16));
    }

    #[test]
    fn deposit_drops_empty_block_to_avoid_hang() {
        let cfg = EngineConfig {
            block_size: 16,
            ..Default::default()
        };
        let mut s = sess(&cfg);
        s.mark_pending(0);
        s.deposit(0, Bytes::new());
        assert!(!s.buffer.contains_key(&0));
        assert!(!s.pending.contains(&0)); // pending still cleared
    }

    #[test]
    fn take_range_size_zero_returns_empty_bytes() {
        let cfg = EngineConfig::default();
        let mut s = sess(&cfg);
        // No blocks deposited; size==0 is a special case that returns
        // empty bytes (the caller already has nothing to read).
        let r = s.take_range(0, 0, 4096).expect("zero-size hit");
        assert!(r.is_empty());
    }

    #[test]
    fn clear_drops_pending_and_buffer() {
        let cfg = EngineConfig::default();
        let mut s = sess(&cfg);
        s.mark_pending(0);
        s.deposit(0, Bytes::from_static(b"abcd"));
        s.clear();
        assert!(s.pending.is_empty());
        assert!(s.buffer.is_empty());
    }

    #[test]
    fn clear_preserves_session_id_but_drops_state() {
        // Round 3: session_id is engine-issued (globally unique).  The
        // engine bumps it via a separate path after clear().  This test
        // pins clear()'s direct contract: state is dropped, but the
        // id stored on the session is left alone (the engine re-stamps).
        let cfg = EngineConfig::default();
        let mut s = sess(&cfg);
        let before = s.session_id();
        s.mark_pending(0);
        s.deposit(0, Bytes::from_static(b"abcd"));
        s.consumed_end = 1024;
        s.clear();
        assert_eq!(s.session_id(), before);
        assert!(s.pending.is_empty());
        assert!(s.buffer.is_empty());
        assert_eq!(s.consumed_end, 0);
    }

    #[test]
    fn take_range_evicts_blocks_behind_cursor() {
        // Round 3 finding #2: with consumed_end advancing past a
        // block, that block must be evicted so the buffer footprint
        // stays bounded under long sequential scans of small reads
        // through large prefetched blocks.
        let cfg = EngineConfig {
            block_size: 8,
            ..Default::default()
        };
        let mut s = sess(&cfg);
        // Block 0..8 prefetched.
        s.deposit(0, Bytes::from((0u8..8).collect::<Vec<_>>()));
        // Two sub-reads sweep the block; second read pushes
        // consumed_end past block_end, so it's evicted.
        s.note_read(0, 4);
        let _ = s.take_range(0, 4, 8).expect("hit 1");
        assert!(
            s.buffer.contains_key(&0),
            "block evicted too early at consumed_end=4"
        );
        s.note_read(4, 4);
        let _ = s.take_range(4, 4, 8).expect("hit 2");
        assert!(
            !s.buffer.contains_key(&0),
            "block 0..8 not evicted after consumed_end=8 (round 3 regression)"
        );
    }

    #[test]
    fn take_range_keeps_block_until_cursor_passes_it() {
        // Round 2 wanted "leading partial block preserved for overlapping
        // reads".  Round 3 needed "evict once the read cursor has fully
        // passed the block, else memory grows unbounded under sequential
        // 4 KiB reads through 4 MiB blocks".  Resolved by tying eviction
        // to `consumed_end`: a block stays in the buffer as long as the
        // read cursor hasn't moved past `block_end`; once it has, the
        // block is dead even if some leading bytes were never delivered.
        let cfg = EngineConfig {
            block_size: 16,
            ..Default::default()
        };
        let mut s = sess(&cfg);
        s.deposit(0, Bytes::from((0u8..16).collect::<Vec<_>>()));
        // Read 4..8 — block 0..16 end=16, consumed_end=8 < 16 → keep.
        s.note_read(4, 4);
        let r = s.take_range(4, 4, 16).expect("hit 1");
        assert_eq!(&r[..], &[4, 5, 6, 7]);
        assert!(
            s.buffer.contains_key(&0),
            "block evicted before cursor reached block_end"
        );
        // Bumping the cursor past block_end now does evict.
        s.note_read(12, 4);
        let _ = s.take_range(12, 4, 16).expect("hit 2");
        assert!(
            !s.buffer.contains_key(&0),
            "block not evicted after consumed_end >= block_end (round 3 regression)"
        );
    }

    #[test]
    fn deposit_drops_block_behind_cursor() {
        // Round 4 finding #1: a slow worker depositing after the read
        // cursor has already moved past must NOT insert the block —
        // it'd be evicted on the next take_range anyway and just
        // inflates the buffer in between.
        let cfg = EngineConfig {
            block_size: 8,
            ..Default::default()
        };
        let mut s = sess(&cfg);
        s.mark_pending(0);
        s.note_read(0, 16); // consumed_end = 16
        s.deposit(0, Bytes::from(vec![1u8; 8])); // block 0..8, end=8 <= 16
        assert!(
            !s.buffer.contains_key(&0),
            "obsolete block deposited despite cursor past it"
        );
        assert!(!s.pending.contains(&0));
    }

    #[test]
    fn note_read_advances_consumed_end_on_miss() {
        // Round 4 finding #1: cursor must advance for every observed
        // read (hit or miss), so even backend-served reads retire
        // stale prefetched blocks.
        let cfg = EngineConfig::default();
        let mut s = sess(&cfg);
        assert_eq!(s.consumed_end, 0);
        s.note_read(100, 200);
        assert_eq!(s.consumed_end, 300);
        // Smaller reads don't reduce the high-water.
        s.note_read(50, 10);
        assert_eq!(s.consumed_end, 300);
    }
}
