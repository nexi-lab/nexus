//! Acceptance criterion (Issue #4058): batched read ≥ 3× sequential.
//!
//! Skipped unless `NEXUS_BENCH=1` is set (timing is flaky on shared CI).
//!
//! Run (skip):   cargo test -p kernel read_batch_meets_3x_speedup_target -- --nocapture
//! Run (assert): NEXUS_BENCH=1 cargo test -p kernel --release read_batch_meets_3x_speedup_target -- --nocapture

use std::collections::HashMap;
use std::sync::Arc;
use std::time::{Duration, Instant};

use kernel::abc::object_store::{ObjectStore, StorageError, WriteResult};
use kernel::kernel::{Kernel, OperationContext, ReadRequest};

// ── Helpers shared with benches/read_batch.rs ───────────────────────────────
// Duplicated by design: benches and integration tests live in different
// compilation units and can't share `mod` across the crate boundary without
// a feature flag. The duplication is intentional and acceptable.

const LATENCY_US: u64 = 2_000; // 2 ms / read — same value as bench

/// Mutable backend for the write phase (no latency).
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

/// Read-only backend with simulated I/O latency.
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

fn setup_kernel_with_100_files() -> Kernel {
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
        None, // created_at_ms
        None, // link_target
        None, // source
        None, // metastore
    )
    .expect("test setup: sys_setattr DT_MOUNT (mutable)");

    let ctx = OperationContext::new("bench", "root", true, None, true);
    for i in 0..100u32 {
        let path = format!("/bench/f{i:03}.txt");
        let payload = vec![b'x'; 1024];
        k.sys_write_one(&path, &ctx, &payload, 0).expect("write");
    }

    let frozen_map: HashMap<String, Vec<u8>> = mutable.blobs.lock().unwrap().clone();
    let latency_backend: Arc<dyn ObjectStore> = Arc::new(LatencyMemBackend::new(frozen_map));

    k.sys_setattr(
        "/",
        2,
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
        None, // created_at_ms
        None, // link_target
        None, // source
        None, // metastore
    )
    .expect("test setup: sys_setattr DT_MOUNT (latency)");

    k.set_read_batch_max_concurrency(64);
    k
}

// ── Test ─────────────────────────────────────────────────────────────────────

#[test]
fn read_batch_meets_3x_speedup_target() {
    if std::env::var("NEXUS_BENCH").ok().as_deref() != Some("1") {
        eprintln!("skipping: set NEXUS_BENCH=1 to run");
        return;
    }

    let k = setup_kernel_with_100_files();
    let ctx = OperationContext::new("bench", "root", true, None, true);

    // ── Warmup — one full pass each to settle the cache and rayon pool ────
    k.clear_file_cache();
    for i in 0..100u32 {
        let _ = k
            .sys_read_one(&format!("/bench/f{i:03}.txt"), &ctx, 5000, 0)
            .expect("warmup read");
    }
    let warmup_reqs: Vec<ReadRequest> = (0..100u32)
        .map(|i| ReadRequest {
            path: format!("/bench/f{i:03}.txt"),
            offset: 0,
            len: None,
            timeout_ms: 5000,
        })
        .collect();
    k.clear_file_cache();
    let _ = k.sys_read(&warmup_reqs, &ctx);

    // ── Sequential measurement ────────────────────────────────────────────
    let seq_iters = 5usize;
    let mut seq_total = Duration::ZERO;
    for _ in 0..seq_iters {
        k.clear_file_cache();
        let t = Instant::now();
        for i in 0..100u32 {
            let _ = k
                .sys_read_one(&format!("/bench/f{i:03}.txt"), &ctx, 5000, 0)
                .expect("read");
        }
        seq_total += t.elapsed();
    }
    let seq_mean = seq_total.as_secs_f64() / seq_iters as f64;

    // ── Batched measurement ───────────────────────────────────────────────
    let reqs: Vec<ReadRequest> = (0..100u32)
        .map(|i| ReadRequest {
            path: format!("/bench/f{i:03}.txt"),
            offset: 0,
            len: None,
            timeout_ms: 5000,
        })
        .collect();

    let batch_iters = 5usize;
    let mut batch_total = Duration::ZERO;
    for _ in 0..batch_iters {
        k.clear_file_cache();
        let t = Instant::now();
        let _ = k.sys_read(&reqs, &ctx);
        batch_total += t.elapsed();
    }
    let batch_mean = batch_total.as_secs_f64() / batch_iters as f64;

    let ratio = seq_mean / batch_mean;
    eprintln!("seq_mean={seq_mean:.6}s  batch_mean={batch_mean:.6}s  ratio={ratio:.2}x");
    assert!(
        ratio >= 3.0,
        "expected batched read >= 3x faster than sequential, got {ratio:.2}x \
         (seq={seq_mean:.4}s batch={batch_mean:.4}s)"
    );
}
