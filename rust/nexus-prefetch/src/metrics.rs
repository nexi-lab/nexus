//! Process-wide engine counters.  Cheap atomic stores; exported via
//! `PrefetchEngine::metrics()` for observability and `pyo3` bridge.

use std::sync::atomic::{AtomicU64, Ordering};

#[derive(Default)]
pub struct EngineMetrics {
    pub hits: AtomicU64,
    pub misses: AtomicU64,
    pub prefetched_bytes: AtomicU64,
    pub dropped_backpressure: AtomicU64,
    pub resets: AtomicU64,
}

impl EngineMetrics {
    pub fn snapshot(&self) -> MetricsSnapshot {
        MetricsSnapshot {
            hits: self.hits.load(Ordering::Relaxed),
            misses: self.misses.load(Ordering::Relaxed),
            prefetched_bytes: self.prefetched_bytes.load(Ordering::Relaxed),
            dropped_backpressure: self.dropped_backpressure.load(Ordering::Relaxed),
            resets: self.resets.load(Ordering::Relaxed),
        }
    }
}

#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct MetricsSnapshot {
    pub hits: u64,
    pub misses: u64,
    pub prefetched_bytes: u64,
    pub dropped_backpressure: u64,
    pub resets: u64,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn snapshot_reflects_current_values() {
        let m = EngineMetrics::default();
        m.hits.fetch_add(7, Ordering::Relaxed);
        m.dropped_backpressure.fetch_add(2, Ordering::Relaxed);
        let s = m.snapshot();
        assert_eq!(s.hits, 7);
        assert_eq!(s.dropped_backpressure, 2);
    }
}
