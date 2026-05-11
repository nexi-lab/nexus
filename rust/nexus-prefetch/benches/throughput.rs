//! Throughput bench — feeds N sequential + N strided reads into the
//! engine to measure overall wall time.  Used as the acceptance gate
//! (sequential ≥1.5×, stride ≥1.3× vs no-prefetch baseline).

use bytes::Bytes;
use criterion::{black_box, criterion_group, criterion_main, Criterion};
use nexus_prefetch::range_reader::{RangeReader, SharedRangeReader};
use nexus_prefetch::{EngineConfig, PrefetchEngine};
use std::sync::Arc;

struct LatencyReader(Bytes, std::time::Duration);
impl RangeReader for LatencyReader {
    fn read(&self, _: &str, off: u64, sz: u32) -> Result<Bytes, nexus_prefetch::PrefetchError> {
        std::thread::sleep(self.1);
        let start = off as usize;
        let end = (off + sz as u64) as usize;
        Ok(self.0.slice(start..end.min(self.0.len())))
    }
}

fn bench_sequential(c: &mut Criterion) {
    let file = Bytes::from(vec![1u8; 64 * 1024 * 1024]);
    let cfg = EngineConfig {
        block_size: 64 * 1024,
        initial_window: 256 * 1024,
        max_window: 4 * 1024 * 1024,
        queue_capacity: 256,
        max_blocks_per_trigger: 8,
        max_workers: 8,
        ..Default::default()
    };

    c.bench_function("sequential_1mb_with_5ms_backend", |b| {
        b.iter(|| {
            let reader: SharedRangeReader = Arc::new(LatencyReader(
                file.clone(),
                std::time::Duration::from_millis(5),
            ));
            let rt = tokio::runtime::Builder::new_multi_thread()
                .worker_threads(4)
                .enable_all()
                .build()
                .unwrap();
            let engine = PrefetchEngine::new(cfg.clone(), reader, Some(rt));
            engine.on_open(1, "/big", Some(file.len() as u64));
            for i in 0..16u64 {
                let off = i * 64 * 1024;
                let _ = black_box(engine.on_read(1, off, 64 * 1024));
            }
        })
    });
}

fn bench_stride(c: &mut Criterion) {
    let file = Bytes::from(vec![1u8; 64 * 1024 * 1024]);
    let cfg = EngineConfig {
        block_size: 64 * 1024,
        initial_window: 256 * 1024,
        max_window: 4 * 1024 * 1024,
        queue_capacity: 256,
        max_blocks_per_trigger: 8,
        max_workers: 8,
        ..Default::default()
    };

    c.bench_function("stride_2x_with_5ms_backend", |b| {
        b.iter(|| {
            let reader: SharedRangeReader = Arc::new(LatencyReader(
                file.clone(),
                std::time::Duration::from_millis(5),
            ));
            let rt = tokio::runtime::Builder::new_multi_thread()
                .worker_threads(4)
                .enable_all()
                .build()
                .unwrap();
            let engine = PrefetchEngine::new(cfg.clone(), reader, Some(rt));
            engine.on_open(1, "/big", Some(file.len() as u64));
            for i in 0..16u64 {
                // Stride-2 pattern: read every other block.
                let off = i * 2 * 64 * 1024;
                let _ = black_box(engine.on_read(1, off, 64 * 1024));
            }
        })
    });
}

fn bench_random_small_file(c: &mut Criterion) {
    let file = Bytes::from(vec![1u8; 256 * 1024]); // 256 KiB — small file
    let cfg = EngineConfig {
        block_size: 4 * 1024,
        initial_window: 16 * 1024,
        max_window: 64 * 1024,
        queue_capacity: 64,
        max_blocks_per_trigger: 4,
        max_workers: 4,
        ..Default::default()
    };

    c.bench_function("random_small_file_with_2ms_backend", |b| {
        b.iter(|| {
            let reader: SharedRangeReader = Arc::new(LatencyReader(
                file.clone(),
                std::time::Duration::from_millis(2),
            ));
            let rt = tokio::runtime::Builder::new_multi_thread()
                .worker_threads(2)
                .enable_all()
                .build()
                .unwrap();
            let engine = PrefetchEngine::new(cfg.clone(), reader, Some(rt));
            engine.on_open(1, "/small", Some(file.len() as u64));
            // Pseudo-random offsets — no detector should latch
            let offs: [u64; 16] = [
                100, 42_000, 7_000, 199_000, 88_000, 12_000, 250_000, 33_000, 155_000, 4_000,
                220_000, 67_000, 188_000, 1_000, 211_000, 99_000,
            ];
            for off in offs {
                let _ = black_box(engine.on_read(1, off, 4 * 1024));
            }
        })
    });
}

criterion_group!(
    benches,
    bench_sequential,
    bench_stride,
    bench_random_small_file
);
criterion_main!(benches);
