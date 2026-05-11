//! Engine tunables.  Defaults track the Python ReadaheadConfig values
//! at `src/nexus/fuse/readahead.py:66–74` so behavior is unchanged
//! when the engine swaps in transparently.

#[derive(Debug, Clone)]
pub struct EngineConfig {
    /// Per-block size for prefetch range GETs.
    pub block_size: u32,
    /// Initial readahead window in bytes.
    pub initial_window: u64,
    /// Max readahead window in bytes.  Capped at 2 GiB per acceptance.
    pub max_window: u64,
    /// Worker pool size (concurrent in-flight range GETs).
    pub max_workers: usize,
    /// Bounded mpsc capacity — once full, new hints are dropped
    /// (backpressure).
    pub queue_capacity: usize,
    /// Max blocks issued per prefetch trigger.
    pub max_blocks_per_trigger: u32,
    /// Bytes of slack for sequential detection.
    pub sequential_tolerance: u64,
    /// Confirmations before a pattern triggers prefetch.
    pub min_sequential_count: u32,
    /// Upper bound on how long `PrefetchEngine::shutdown` will wait
    /// for in-flight `spawn_blocking` reads to finish.  After the
    /// timeout, the blocking pool is detached and the runtime exits.
    /// Default 2 seconds is well above CAS-local IO but short enough
    /// not to wedge FUSE unmount on degraded backends.  Round 4
    /// finding #4 — was hardcoded 500ms which can be shorter than a
    /// real network range read.
    pub shutdown_timeout_ms: u64,
}

impl Default for EngineConfig {
    fn default() -> Self {
        Self {
            block_size: 4 * 1024 * 1024,
            initial_window: 512 * 1024,
            max_window: 64 * 1024 * 1024,
            max_workers: 8,
            queue_capacity: 1024,
            max_blocks_per_trigger: 8,
            sequential_tolerance: 64 * 1024,
            min_sequential_count: 2,
            shutdown_timeout_ms: 2000,
        }
    }
}

const MAX_ALLOWED_WINDOW: u64 = 2 * 1024 * 1024 * 1024; // 2 GiB

impl EngineConfig {
    /// Clamp `max_window` to the 2 GiB ceiling required by the spec.
    pub fn clamp(mut self) -> Self {
        if self.max_window > MAX_ALLOWED_WINDOW {
            self.max_window = MAX_ALLOWED_WINDOW;
        }
        self
    }

    /// Normalize all invariants so downstream code can divide /
    /// allocate without runtime panics (round 3 finding #4) AND
    /// re-applies the 2 GiB ceiling at the end so an `initial_window`
    /// above the cap can't drag `max_window` back over it (round 4
    /// finding #3).  We saturate-to-minimum rather than panic so
    /// Python callers can pass slightly malformed config dicts.
    pub fn normalize(mut self) -> Self {
        if self.block_size == 0 {
            self.block_size = 4 * 1024;
        }
        if self.max_workers == 0 {
            self.max_workers = 1;
        }
        if self.queue_capacity == 0 {
            self.queue_capacity = 1;
        }
        if self.initial_window == 0 {
            self.initial_window = self.block_size as u64;
        }
        // Re-cap *both* windows AFTER any inflation so the 2 GiB
        // ceiling holds end-to-end.
        if self.initial_window > MAX_ALLOWED_WINDOW {
            self.initial_window = MAX_ALLOWED_WINDOW;
        }
        if self.max_window < self.initial_window {
            self.max_window = self.initial_window;
        }
        if self.max_window > MAX_ALLOWED_WINDOW {
            self.max_window = MAX_ALLOWED_WINDOW;
        }
        if self.max_blocks_per_trigger == 0 {
            self.max_blocks_per_trigger = 1;
        }
        if self.min_sequential_count == 0 {
            self.min_sequential_count = 1;
        }
        self
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn defaults_match_python_constants() {
        let cfg = EngineConfig::default();
        assert_eq!(cfg.block_size, 4 * 1024 * 1024);
        assert_eq!(cfg.initial_window, 512 * 1024);
        assert_eq!(cfg.max_window, 64 * 1024 * 1024);
        assert_eq!(cfg.max_workers, 8);
        assert_eq!(cfg.max_blocks_per_trigger, 8);
        assert_eq!(cfg.sequential_tolerance, 64 * 1024);
        assert_eq!(cfg.min_sequential_count, 2);
    }

    #[test]
    fn clamp_caps_max_window_at_2gib() {
        let cfg = EngineConfig {
            max_window: 4 * 1024 * 1024 * 1024,
            ..Default::default()
        }
        .clamp();
        assert_eq!(cfg.max_window, 2 * 1024 * 1024 * 1024);
    }

    #[test]
    fn clamp_leaves_window_below_ceiling_unchanged() {
        let cfg = EngineConfig::default().clamp();
        assert_eq!(cfg.max_window, 64 * 1024 * 1024);
    }

    #[test]
    fn normalize_replaces_zero_block_size() {
        // Round 3 finding #4: block_size=0 used to make enqueue_prefetch
        // divide by zero on the first confirmed sequential read.
        let cfg = EngineConfig {
            block_size: 0,
            ..Default::default()
        }
        .normalize();
        assert!(cfg.block_size > 0);
    }

    #[test]
    fn normalize_replaces_zero_workers_and_queue() {
        let cfg = EngineConfig {
            max_workers: 0,
            queue_capacity: 0,
            ..Default::default()
        }
        .normalize();
        assert!(cfg.max_workers >= 1);
        assert!(cfg.queue_capacity >= 1);
    }

    #[test]
    fn normalize_lifts_max_window_when_below_initial() {
        let cfg = EngineConfig {
            initial_window: 64 * 1024,
            max_window: 4 * 1024, // smaller than initial — nonsense
            ..Default::default()
        }
        .normalize();
        assert!(cfg.max_window >= cfg.initial_window);
    }

    #[test]
    fn normalize_caps_both_windows_at_2gib() {
        // Round 4 finding #3: an initial_window above the 2 GiB
        // ceiling must not drag max_window back over the cap.
        let huge = 8 * 1024 * 1024 * 1024;
        let cfg = EngineConfig {
            initial_window: huge,
            max_window: huge,
            ..Default::default()
        }
        .normalize();
        assert_eq!(cfg.initial_window, MAX_ALLOWED_WINDOW);
        assert_eq!(cfg.max_window, MAX_ALLOWED_WINDOW);
    }
}
