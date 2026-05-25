//! Criterion benchmark: `sys_readdir` baseline at varying directory sizes.
//!
//! Regression guard for the readdir hot path. Measures absolute call cost
//! at 10 / 100 / 1000 child entries with the cluster-profile backend
//! (PathLocal + redb), no warmup discipline — each iteration is a normal
//! sys_readdir call from the caller's view.
//!
//! Run:  cd rust/kernel && cargo bench --bench readdir_cache_bench

use std::hint::black_box;
use std::path::Path;
use std::sync::Arc;

use criterion::{criterion_group, criterion_main, BenchmarkId, Criterion};

use contracts::operation_context::OperationContext;
use kernel::abc::object_store::{ObjectStore, StorageError, WriteResult};
use kernel::kernel::{Kernel, WriteRequest};
use kernel::meta_store::DT_DIR;

// ── Inline PathLocal for bench (mirrors syscall_bench.rs) ──────────────

struct BenchPathLocal {
    root: std::path::PathBuf,
}

impl BenchPathLocal {
    fn new(root: &Path) -> Self {
        std::fs::create_dir_all(root).unwrap();
        Self {
            root: root.to_path_buf(),
        }
    }
    fn resolve(&self, id: &str) -> std::path::PathBuf {
        self.root.join(id.trim_start_matches('/'))
    }
}

impl ObjectStore for BenchPathLocal {
    fn name(&self) -> &str {
        "bench_path_local"
    }
    fn write_content(
        &self,
        content: &[u8],
        content_id: &str,
        _ctx: &kernel::kernel::OperationContext,
        _offset: u64,
    ) -> Result<WriteResult, StorageError> {
        let p = self.resolve(content_id);
        if let Some(parent) = p.parent() {
            std::fs::create_dir_all(parent).map_err(StorageError::IOError)?;
        }
        std::fs::write(&p, content).map_err(StorageError::IOError)?;
        Ok(WriteResult {
            content_id: content_id.to_string(),
            version: content_id.to_string(),
            size: content.len() as u64,
        })
    }
    fn read_content(
        &self,
        content_id: &str,
        _ctx: &kernel::kernel::OperationContext,
    ) -> Result<Vec<u8>, StorageError> {
        std::fs::read(self.resolve(content_id)).map_err(|e| StorageError::NotFound(e.to_string()))
    }
    fn resolve_physical_path(&self, content_id: &str) -> Option<std::path::PathBuf> {
        Some(self.resolve(content_id))
    }
}

fn admin_ctx() -> OperationContext {
    OperationContext::new("bench", "root", true, None, true)
}

fn setup_kernel(tmp: &Path) -> Kernel {
    let data_dir = tmp.join("data");
    std::fs::create_dir_all(&data_dir).unwrap();
    let redb_path = tmp.join("meta.redb");

    let kernel = Kernel::new();
    let _ = kernel.set_metastore_path(redb_path.to_str().unwrap());
    let backend: Arc<dyn ObjectStore> = Arc::new(BenchPathLocal::new(&data_dir));
    kernel
        .vfs_router_arc()
        .add_mount("/", "root", Some(backend), false);
    kernel
}

fn populate_dir(kernel: &Kernel, ctx: &OperationContext, dir: &str, n: usize) {
    kernel
        .sys_setattr(
            dir,
            DT_DIR as i32,
            "",
            None,
            None,
            None,
            "default",
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
            None,
            None,
            None,
            None,
        )
        .unwrap_or_else(|_| panic!("mkdir {dir}"));
    for i in 0..n {
        let path = format!("{dir}/file_{i:05}.txt");
        let content = format!("{i}");
        kernel
            .sys_write(
                &[WriteRequest {
                    path,
                    content: content.into_bytes(),
                    offset: 0,
                }],
                ctx,
            )
            .into_iter()
            .next()
            .unwrap()
            .unwrap_or_else(|e| panic!("populate write: {e:?}"));
    }
}

const SIZES: &[usize] = &[10, 100, 1000];

/// Baseline `sys_readdir` cost at varying directory sizes.
fn bench_readdir(c: &mut Criterion) {
    let mut group = c.benchmark_group("sys_readdir");
    for &n in SIZES {
        let tmp = tempfile::tempdir().unwrap();
        let kernel = setup_kernel(tmp.path());
        let ctx = admin_ctx();
        let dir = format!("/dir{n}");
        populate_dir(&kernel, &ctx, &dir, n);

        group.bench_with_input(BenchmarkId::from_parameter(n), &dir, |b, dir| {
            b.iter(|| {
                let entries = kernel.sys_readdir(black_box(dir), "root", true);
                black_box(entries);
            })
        });
        drop(kernel);
    }
    group.finish();
}

criterion_group!(benches, bench_readdir);
criterion_main!(benches);
