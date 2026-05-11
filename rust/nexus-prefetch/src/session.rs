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

    /// Deposit a completed block; remove from pending.
    pub fn deposit(&mut self, block_offset: u64, bytes: Bytes) {
        self.pending.remove(&block_offset);
        self.buffer.insert(block_offset, bytes);
    }

    /// Take a buffered range if it fully covers `[offset, offset+size)`.
    /// Returns `None` on miss.  Removes consumed blocks from the buffer.
    pub fn take_range(&mut self, offset: u64, size: u32, block_size: u32) -> Option<Bytes> {
        let end = offset + size as u64;
        let first_block = (offset / block_size as u64) * block_size as u64;
        let mut out = Vec::with_capacity(size as usize);
        let mut cursor = first_block;
        while cursor < end {
            let block = self.buffer.get(&cursor)?;
            let block_end = cursor + block.len() as u64;
            let take_from = offset.max(cursor) - cursor;
            let take_to = (end.min(block_end)) - cursor;
            out.extend_from_slice(&block[take_from as usize..take_to as usize]);
            cursor = block_end;
        }
        // Drop consumed blocks so the buffer doesn't grow unbounded.
        let consumed: Vec<u64> = self
            .buffer
            .range(first_block..end)
            .map(|(k, _)| *k)
            .collect();
        for k in consumed {
            self.buffer.remove(&k);
        }
        self.hits += 1;
        Some(Bytes::from(out))
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
}
