//! Criterion bench for vectored sys_read batch (Issue #4058).
//!
//! Run: cd rust/kernel && cargo bench read_batch
//!
//! Demonstrates the parallelism benefit of `sys_read` batch over sequential
//! `sys_read_one` calls when the backend has I/O latency. The bench uses a
//! latency-simulating in-memory backend and clears the file cache before
//! each measurement so reads reach the backend (cache-cold path).
//!
//! Expected: batch_mean ≥ 3× faster than seq_mean at 64-way concurrency.

use std::collections::HashMap;
use std::sync::Arc;
use std::time::Duration;

use criterion::{criterion_group, criterion_main, BatchSize, Criterion};
use std::hint::black_box;

use kernel::abc::object_store::{ObjectStore, StorageError, WriteResult};
use kernel::kernel::{Kernel, OperationContext, ReadRequest};

// ── Latency-simulating in-memory ObjectStore ────────────────────────────────
// Copied (not shared via feature) to keep kernel's public surface clean.
// See: rust/transport/src/grpc.rs #[cfg(test)] mod tests — same pattern.
//
// LATENCY_US: per-read sleep that makes parallel fan-out pay off. At 32-way
// concurrency the batch should retire 100 reads in ≈ ceil(100/32)*latency
// whereas sequential takes 100*latency.

const LATENCY_US: u64 = 2_000; // 2 ms / read  → seq ≈ 200 ms, batch ≈ 8 ms

/// Mutable backend: used during the write phase only (no latency).
#[derive(Default)]
struct MutableMemBackend {
    blobs: std::sync::Mutex<HashMap<String, Vec<u8>>>,
}

impl ObjectStore for MutableMemBackend {
    fn name(&self) -> &str {
        "mutable-mem"
    }

    fn write_content(
        &self,
        content: &[u8],
        content_id: &str,
        _ctx: &OperationContext,
        offset: u64,
    ) -> Result<WriteResult, StorageError> {
        let mut map = self.blobs.lock().unwrap();
        let entry = map.entry(content_id.to_string()).or_default();
        let start = offset as usize;
        if start > entry.len() {
            entry.resize(start, 0);
        }
        let end = start + content.len();
        if end > entry.len() {
            entry.resize(end, 0);
        }
        entry[start..end].copy_from_slice(content);
        let size = entry.len() as u64;
        Ok(WriteResult {
            content_id: content_id.to_string(),
            version: content_id.to_string(),
            size,
        })
    }

    fn read_content(
        &self,
        content_id: &str,
        _ctx: &OperationContext,
    ) -> Result<Vec<u8>, StorageError> {
        self.blobs
            .lock()
            .unwrap()
            .get(content_id)
            .cloned()
            .ok_or_else(|| StorageError::NotFound(content_id.into()))
    }

    fn get_content_size(&self, content_id: &str) -> Result<u64, StorageError> {
        self.blobs
            .lock()
            .unwrap()
            .get(content_id)
            .map(|d| d.len() as u64)
            .ok_or_else(|| StorageError::NotFound(content_id.into()))
    }
}

/// Read-only backend: lock-free reads with simulated I/O latency.
struct LatencyMemBackend {
    blobs: Arc<HashMap<String, Arc<Vec<u8>>>>,
}

impl LatencyMemBackend {
    fn new(blobs: HashMap<String, Vec<u8>>) -> Self {
        let frozen = blobs.into_iter().map(|(k, v)| (k, Arc::new(v))).collect();
        Self {
            blobs: Arc::new(frozen),
        }
    }
}

impl ObjectStore for LatencyMemBackend {
    fn name(&self) -> &str {
        "latency-mem"
    }

    fn write_content(
        &self,
        _content: &[u8],
        _content_id: &str,
        _ctx: &OperationContext,
        _offset: u64,
    ) -> Result<WriteResult, StorageError> {
        Err(StorageError::NotSupported("LatencyMemBackend: read-only"))
    }

    fn read_content(
        &self,
        content_id: &str,
        _ctx: &OperationContext,
    ) -> Result<Vec<u8>, StorageError> {
        // Simulate per-read I/O latency so rayon parallelism is observable.
        std::thread::sleep(Duration::from_micros(LATENCY_US));
        self.blobs
            .get(content_id)
            .map(|arc| (**arc).clone())
            .ok_or_else(|| StorageError::NotFound(content_id.into()))
    }

    fn get_content_size(&self, content_id: &str) -> Result<u64, StorageError> {
        self.blobs
            .get(content_id)
            .map(|arc| arc.len() as u64)
            .ok_or_else(|| StorageError::NotFound(content_id.into()))
    }
}

// ── Kernel setup ────────────────────────────────────────────────────────────

fn setup() -> Kernel {
    // Phase 1: write 100 files through a zero-latency mutable backend.
    let k = Kernel::new();
    let mutable = Arc::new(MutableMemBackend::default());
    k.sys_setattr(
        "/",
        2, // DT_MOUNT
        "mutable-mem",
        Some(mutable.clone() as Arc<dyn ObjectStore>),
        None,
        None,
        "",
        kernel::ROOT_ZONE_ID,
        false,
        0,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
    )
    .expect("bench setup: sys_setattr DT_MOUNT (mutable)");

    let ctx = OperationContext::new("bench", "root", true, None, true);
    for i in 0..100u32 {
        let path = format!("/bench/f{i:03}.txt");
        let payload = vec![b'x'; 1024];
        k.sys_write_one(&path, &ctx, &payload, 0)
            .expect("bench write");
    }

    // Phase 2: re-mount with the latency-simulating backend.
    let frozen_map: HashMap<String, Vec<u8>> = mutable.blobs.lock().unwrap().clone();
    let latency_backend: Arc<dyn ObjectStore> = Arc::new(LatencyMemBackend::new(frozen_map));

    k.sys_setattr(
        "/",
        2, // DT_MOUNT — replaces existing root mount
        "latency-mem",
        Some(latency_backend),
        None,
        None,
        "",
        kernel::ROOT_ZONE_ID,
        false,
        0,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
    )
    .expect("bench setup: sys_setattr DT_MOUNT (latency)");

    // Maximise rayon parallelism.
    k.set_read_batch_max_concurrency(64);

    k
}

// ── Sequential baseline ─────────────────────────────────────────────────────

fn bench_sequential(c: &mut Criterion) {
    let k = setup();
    let ctx = OperationContext::new("bench", "root", true, None, true);

    // Use iter_batched so setup (cache-clear) is excluded from measurement.
    c.bench_function("read_batch/sequential_100", |b| {
        b.iter_batched(
            || k.clear_file_cache(), // setup: flush cache before each sample
            |_| {
                for i in 0..100u32 {
                    let path = format!("/bench/f{i:03}.txt");
                    let r = k.sys_read_one(&path, &ctx, 5000, 0).expect("read");
                    black_box(r);
                }
            },
            BatchSize::PerIteration,
        );
    });
}

// ── Batched sys_read ─────────────────────────────────────────────────────

fn bench_batched(c: &mut Criterion) {
    let k = setup();
    let ctx = OperationContext::new("bench", "root", true, None, true);

    let reqs: Vec<ReadRequest> = (0..100u32)
        .map(|i| ReadRequest {
            path: format!("/bench/f{i:03}.txt"),
            offset: 0,
            len: None,
            timeout_ms: 5000,
        })
        .collect();

    c.bench_function("read_batch/batched_100", |b| {
        b.iter_batched(
            || k.clear_file_cache(), // setup: flush cache before each sample
            |_| {
                let out = k.sys_read(&reqs, &ctx);
                black_box(out);
            },
            BatchSize::PerIteration,
        );
    });
}

criterion_group!(benches, bench_sequential, bench_batched);
criterion_main!(benches);
