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
}
