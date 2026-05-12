//! Criterion benchmark for write coalescing reduction (Issue #4059).

use criterion::Criterion;
use kernel::abc::object_store::{ObjectStore, StorageError, WriteResult};
use kernel::kernel::{Kernel, OperationContext};
use kernel::meta_store::DT_MOUNT;
use std::collections::HashMap;
use std::hint::black_box;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};

const BURST_PATH: &str = "/workspace/burst.txt";
const BURST_WRITES: usize = 100;

struct CountingObjectStore {
    writes: AtomicUsize,
    blobs: Mutex<HashMap<String, Vec<u8>>>,
    fail_writes: AtomicBool,
}

impl CountingObjectStore {
    fn new() -> Self {
        Self {
            writes: AtomicUsize::new(0),
            blobs: Mutex::new(HashMap::new()),
            fail_writes: AtomicBool::new(false),
        }
    }

    fn write_count(&self) -> usize {
        self.writes.load(Ordering::Relaxed)
    }
}

impl ObjectStore for CountingObjectStore {
    fn name(&self) -> &str {
        "counting"
    }

    fn write_content(
        &self,
        content: &[u8],
        content_id: &str,
        _ctx: &OperationContext,
        offset: u64,
    ) -> Result<WriteResult, StorageError> {
        if self.fail_writes.load(Ordering::Relaxed) {
            return Err(StorageError::NotSupported("injected write failure"));
        }
        if offset != 0 {
            return Err(StorageError::NotSupported("nonzero benchmark offset"));
        }

        self.writes.fetch_add(1, Ordering::Relaxed);
        self.blobs
            .lock()
            .expect("counting object store mutex poisoned")
            .insert(content_id.to_string(), content.to_vec());

        Ok(WriteResult {
            content_id: content_id.to_string(),
            version: content_id.to_string(),
            size: content.len() as u64,
        })
    }

    fn read_content(
        &self,
        content_id: &str,
        _ctx: &OperationContext,
    ) -> Result<Vec<u8>, StorageError> {
        self.blobs
            .lock()
            .expect("counting object store mutex poisoned")
            .get(content_id)
            .cloned()
            .ok_or_else(|| StorageError::NotFound(content_id.to_string()))
    }
}

fn mounted_counting_kernel() -> (Kernel, Arc<CountingObjectStore>, OperationContext) {
    let kernel = Kernel::new();
    let backend = Arc::new(CountingObjectStore::new());
    let mount_backend: Arc<dyn ObjectStore> = backend.clone();

    kernel
        .sys_setattr(
            "/workspace",
            i32::from(DT_MOUNT),
            "counting",
            Some(mount_backend),
            None,
            None,
            "",
            "root",
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
        .expect("mount counting object store");

    let ctx = OperationContext::new("bench", "root", true, None, true);
    (kernel, backend, ctx)
}

fn burst_write_count(strict: bool) -> usize {
    let (kernel, backend, ctx) = mounted_counting_kernel();
    let policy = if strict {
        contracts::WriteCoalescingPolicy::strict()
    } else {
        contracts::WriteCoalescingPolicy::latency()
    };
    kernel.set_write_coalescing_policy("/", policy);

    for idx in 0..BURST_WRITES {
        let payload = format!("payload-{idx:03}");
        kernel
            .sys_write(BURST_PATH, &ctx, payload.as_bytes(), 0)
            .expect("burst write");
    }

    kernel
        .flush_write_buffer(Some(BURST_PATH), Some("root"))
        .expect("flush write buffer");

    backend.write_count()
}

fn burst_write_count_acceptance() {
    let strict = burst_write_count(true);
    let buffered = burst_write_count(false);

    println!("write_coalescing counts: strict={strict}, buffered={buffered}");

    assert_eq!(strict, 100);
    assert!(buffered > 0, "buffered writes should include final flush");
    assert!(
        buffered <= 10,
        "buffered writes should be <= 10, got {buffered}"
    );
    assert!(
        strict >= buffered * 10,
        "expected at least 10x reduction, strict={strict}, buffered={buffered}"
    );
}

fn bench_write_coalescing(c: &mut Criterion) {
    burst_write_count_acceptance();

    c.bench_function("write_coalescing_100_write_burst", |b| {
        b.iter(|| black_box(burst_write_count(false)))
    });
}

fn main() {
    if std::env::args().any(|arg| arg == "burst_write_count_acceptance") {
        burst_write_count_acceptance();
        return;
    }

    let mut criterion = Criterion::default().configure_from_args();
    bench_write_coalescing(&mut criterion);
    criterion.final_summary();
}
