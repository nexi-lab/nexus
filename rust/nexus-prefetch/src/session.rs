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
}

impl Session {
    pub fn new(
        path: String,
        fh: u64,
        file_size: Option<u64>,
        detector: Box<dyn Detector>,
        cfg: &EngineConfig,
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
    /// loop with no progress).
    pub fn deposit(&mut self, block_offset: u64, bytes: Bytes) {
        self.pending.remove(&block_offset);
        if bytes.is_empty() {
            return;
        }
        self.buffer.insert(block_offset, bytes);
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
        // Second pass: only remove blocks the read fully consumed.
        // A block that extends past `end` keeps its tail for the next
        // read (e.g. successive 4 KiB reads through a 4 MiB prefetched
        // block).  We materialise the keys before removal so we can
        // mutate while iterating.
        let to_remove: Vec<u64> = self
            .buffer
            .range(first_block..end)
            .filter_map(|(k, v)| {
                let block_end = k + v.len() as u64;
                if block_end <= end {
                    Some(*k)
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

    /// Drop all pending and buffered state — invoked on invalidation
    /// (file write/delete) and on detector-driven Random resets that
    /// also need to discard stale prefetched bytes.
    pub fn clear(&mut self) {
        self.pending.clear();
        self.buffer.clear();
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
        // block 16. Blocks 0 and 8 should be removed; block 16 must
        // stay because 4 of its 8 bytes are unconsumed.
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
}
