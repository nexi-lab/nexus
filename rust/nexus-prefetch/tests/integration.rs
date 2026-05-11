//! End-to-end: synthetic file → sequential read pattern → confirm
//! hit-ratio rises above 50% after warm-up window.

use bytes::Bytes;
use nexus_prefetch::range_reader::SharedRangeReader;
use nexus_prefetch::{EngineConfig, PrefetchEngine, RangeReader};
use std::sync::Arc;
use std::time::Duration;

struct VecReader(Bytes);

impl RangeReader for VecReader {
    fn read(&self, _: &str, off: u64, sz: u32) -> Result<Bytes, nexus_prefetch::PrefetchError> {
        let start = off as usize;
        let end = (off + sz as u64) as usize;
        if start >= self.0.len() {
            return Err(nexus_prefetch::PrefetchError::OutOfRange {
                offset: off,
                size: sz,
                file_size: self.0.len() as u64,
            });
        }
        Ok(self.0.slice(start..end.min(self.0.len())))
    }
}

#[test]
fn sequential_workload_majority_hits() {
    let file = vec![42u8; 1 << 20]; // 1 MiB
    let reader: SharedRangeReader = Arc::new(VecReader(Bytes::from(file)));
    let cfg = EngineConfig {
        block_size: 4096,
        initial_window: 16 * 1024,
        max_window: 256 * 1024,
        queue_capacity: 64,
        max_blocks_per_trigger: 8,
        sequential_tolerance: 0,
        min_sequential_count: 2,
        max_workers: 4,
        shutdown_timeout_ms: 500,
    };
    let rt = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(2)
        .enable_all()
        .build()
        .unwrap();
    let engine = PrefetchEngine::new(cfg, reader, Some(rt));
    engine.on_open(1, "/big", Some(1 << 20));

    // 64 sequential 4-KiB reads.
    let mut hits = 0u32;
    let mut misses = 0u32;
    for i in 0..64u64 {
        let off = i * 4096;
        // Give workers a moment to settle after issuance.
        if i > 0 && i % 4 == 0 {
            std::thread::sleep(Duration::from_millis(20));
        }
        if engine.on_read(1, off, 4096).is_some() {
            hits += 1;
        } else {
            misses += 1;
        }
    }
    let ratio = hits as f64 / (hits + misses) as f64;
    assert!(
        ratio > 0.5,
        "expected majority hits after warmup, got hits={hits} misses={misses} ratio={ratio}"
    );
}
