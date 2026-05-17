//! Criterion benchmarks: pure Rust kernel syscalls vs host OS syscalls.
//!
//! This is the production call path — sudocode/sudowork call
//! `kernel.sys_read()` / `kernel.sys_write()` directly in-process,
//! zero Python, zero PyO3.
//!
//! Run:  cd rust/kernel && cargo bench syscall
//!
//! Cluster profile: PathLocalBackend + redb metastore (the only
//! production-deployed profile as of 2026-05).
//!
//! Performance history (PathLocal + redb, 1KB payload):
//!   Pre-optimization:  sys_read ~24.5us, sys_write ~20ms
//!   Post-FDT:          sys_read ~2-3us (FDT pread fast path)
//!   DT_PIPE baseline:  ~246ns round-trip (in-memory ring buffer)

use std::path::Path;
use std::sync::Arc;

use std::hint::black_box;

use criterion::{criterion_group, criterion_main, BenchmarkId, Criterion};

use contracts::operation_context::OperationContext;
use kernel::abc::object_store::{ObjectStore, StorageError, WriteResult};
use kernel::kernel::{Kernel, ReadRequest, UnlinkRequest, WriteRequest};
use kernel::meta_store::{DT_DIR, DT_PIPE};

// ── Inline PathLocal for bench (avoids cyclic kernel↔backends dep) ────

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

// ── Constants ───────────────────────────────────────────────────────

const PAYLOAD_1KB: &[u8] = &[b'x'; 1024];

fn payload_64kb() -> Vec<u8> {
    vec![b'y'; 64 * 1024]
}

fn payload_1mb() -> Vec<u8> {
    vec![b'z'; 1024 * 1024]
}

fn admin_ctx() -> OperationContext {
    OperationContext::new("bench", "root", true, None, true)
}

// ── Kernel setup ────────────────────────────────────────────────────

/// Create a Kernel with PathLocalBackend mounted at "/" — mirrors
/// the cluster profile production setup.
fn setup_kernel(tmp: &Path) -> Kernel {
    let data_dir = tmp.join("data");
    std::fs::create_dir_all(&data_dir).unwrap();
    let redb_path = tmp.join("meta.redb");

    let kernel = Kernel::new();
    let _ = kernel.set_metastore_path(redb_path.to_str().unwrap());

    // Mount path-local backend at "/" via VFS router (mirrors cluster profile boot)
    let backend: Arc<dyn ObjectStore> = Arc::new(BenchPathLocal::new(&data_dir));
    kernel
        .vfs_router_arc()
        .add_mount("/", "root", Some(backend), false);

    kernel
}

/// Pre-populate kernel with test files.
fn populate(kernel: &Kernel, ctx: &OperationContext) {
    let results = kernel.sys_write(
        &[WriteRequest {
            path: "/test_1kb.bin".into(),
            content: PAYLOAD_1KB.to_vec(),
            offset: 0,
        }],
        ctx,
    );
    let w = results.into_iter().next().unwrap().expect("write 1kb");
    assert!(w.hit, "sys_write must hit (VFS route missing?)");

    kernel
        .sys_write(
            &[WriteRequest {
                path: "/test_64kb.bin".into(),
                content: payload_64kb(),
                offset: 0,
            }],
            ctx,
        )
        .into_iter()
        .next()
        .unwrap()
        .expect("write 64kb");

    kernel
        .sys_write(
            &[WriteRequest {
                path: "/test_1mb.bin".into(),
                content: payload_1mb(),
                offset: 0,
            }],
            ctx,
        )
        .into_iter()
        .next()
        .unwrap()
        .expect("write 1mb");

    // 100 files for readdir — mkdir via sys_setattr then write files
    kernel
        .sys_setattr(
            "/many_files",
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
        .expect("mkdir /many_files");
    for i in 0..100 {
        let path = format!("/many_files/file_{i:04}.txt");
        let content = format!("Content {i}");
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
            .expect("write many_files");
    }
}

// ── Benchmarks ──────────────────────────────────────────────────────

fn bench_sys_stat(c: &mut Criterion) {
    let tmp = tempfile::tempdir().unwrap();
    let kernel = setup_kernel(tmp.path());
    let ctx = admin_ctx();
    populate(&kernel, &ctx);

    c.bench_function("sys_stat (1KB file)", |b| {
        b.iter(|| {
            let result = kernel.sys_stat(black_box("/test_1kb.bin"), "root");
            black_box(result);
        })
    });
}

fn bench_sys_read(c: &mut Criterion) {
    let tmp = tempfile::tempdir().unwrap();
    let kernel = setup_kernel(tmp.path());
    let ctx = admin_ctx();
    populate(&kernel, &ctx);

    let mut group = c.benchmark_group("sys_read");
    for (label, path) in [
        ("1KB", "/test_1kb.bin"),
        ("64KB", "/test_64kb.bin"),
        ("1MB", "/test_1mb.bin"),
    ] {
        group.bench_with_input(BenchmarkId::new("nexus", label), &path, |b, &path| {
            b.iter(|| {
                let results = kernel.sys_read(
                    black_box(&[ReadRequest {
                        path: path.to_string(),
                        offset: 0,
                        len: None,
                        timeout_ms: 0,
                    }]),
                    &ctx,
                );
                black_box(results.into_iter().next().unwrap().expect("sys_read"));
            })
        });
    }

    // OS baseline: pre-opened fd
    let tmp_os = tempfile::tempdir().unwrap();
    for (label, size) in [
        ("1KB", 1024usize),
        ("64KB", 64 * 1024),
        ("1MB", 1024 * 1024),
    ] {
        let file_path = tmp_os.path().join(format!("test_{label}.bin"));
        std::fs::write(&file_path, vec![b'x'; size]).unwrap();

        let c_path = std::ffi::CString::new(file_path.to_str().unwrap()).unwrap();
        let fd = unsafe { libc::open(c_path.as_ptr(), libc::O_RDONLY) };
        assert!(fd >= 0, "failed to open OS test file");

        let mut buf = vec![0u8; size];
        group.bench_with_input(BenchmarkId::new("host_os", label), &size, |b, &sz| {
            b.iter(|| unsafe {
                libc::lseek(fd, 0, libc::SEEK_SET);
                libc::read(fd, buf.as_mut_ptr() as *mut _, sz);
                black_box(&buf);
            })
        });

        unsafe { libc::close(fd) };
    }

    group.finish();
}

fn bench_sys_write(c: &mut Criterion) {
    let tmp = tempfile::tempdir().unwrap();
    let kernel = setup_kernel(tmp.path());
    let ctx = admin_ctx();
    populate(&kernel, &ctx);

    let mut group = c.benchmark_group("sys_write");

    // Write new file (unique path per iteration)
    let counter = std::sync::atomic::AtomicU64::new(0);
    group.bench_function("nexus_new_1KB", |b| {
        b.iter(|| {
            let n = counter.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
            let path = format!("/bench_new_{n}.txt");
            let results = kernel.sys_write(
                black_box(&[WriteRequest {
                    path,
                    content: PAYLOAD_1KB.to_vec(),
                    offset: 0,
                }]),
                &ctx,
            );
            black_box(results.into_iter().next().unwrap().expect("sys_write new"));
        })
    });

    // Write overwrite
    group.bench_function("nexus_overwrite_1KB", |b| {
        b.iter(|| {
            let results = kernel.sys_write(
                black_box(&[WriteRequest {
                    path: "/test_1kb.bin".into(),
                    content: PAYLOAD_1KB.to_vec(),
                    offset: 0,
                }]),
                &ctx,
            );
            black_box(
                results
                    .into_iter()
                    .next()
                    .unwrap()
                    .expect("sys_write overwrite"),
            );
        })
    });

    // OS baseline: write new
    let tmp_os = tempfile::tempdir().unwrap();
    let os_counter = std::sync::atomic::AtomicU64::new(0);
    let os_base = tmp_os.path().to_path_buf();
    group.bench_function("host_os_new_1KB", |b| {
        b.iter(|| {
            let n = os_counter.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
            let path = os_base.join(format!("bench_new_{n}.txt"));
            std::fs::write(black_box(&path), PAYLOAD_1KB).unwrap();
        })
    });

    // OS baseline: write overwrite
    let os_overwrite_path = tmp_os.path().join("overwrite.bin");
    std::fs::write(&os_overwrite_path, PAYLOAD_1KB).unwrap();
    group.bench_function("host_os_overwrite_1KB", |b| {
        b.iter(|| {
            std::fs::write(black_box(&os_overwrite_path), PAYLOAD_1KB).unwrap();
        })
    });

    group.finish();
}

fn bench_sys_readdir(c: &mut Criterion) {
    let tmp = tempfile::tempdir().unwrap();
    let kernel = setup_kernel(tmp.path());
    let ctx = admin_ctx();
    populate(&kernel, &ctx);

    let mut group = c.benchmark_group("sys_readdir");

    group.bench_function("nexus_100_entries", |b| {
        b.iter(|| {
            let result = kernel.readdir_paged(black_box("/many_files"), "root", true, 1000, None);
            black_box(result);
        })
    });

    // OS baseline
    let tmp_os = tempfile::tempdir().unwrap();
    let os_many = tmp_os.path().join("many_files");
    std::fs::create_dir_all(&os_many).unwrap();
    for i in 0..100 {
        std::fs::write(
            os_many.join(format!("file_{i:04}.txt")),
            format!("Content {i}"),
        )
        .unwrap();
    }
    group.bench_function("host_os_100_entries", |b| {
        b.iter(|| {
            let entries: Vec<_> = std::fs::read_dir(black_box(&os_many)).unwrap().collect();
            black_box(entries);
        })
    });

    group.finish();
}

fn bench_sys_unlink(c: &mut Criterion) {
    let tmp = tempfile::tempdir().unwrap();
    let kernel = setup_kernel(tmp.path());
    let ctx = admin_ctx();

    // Pre-create many files for deletion
    for i in 0..5000 {
        let path = format!("/bench_del_{i}.txt");
        kernel
            .sys_write(
                &[WriteRequest {
                    path,
                    content: b"x".to_vec(),
                    offset: 0,
                }],
                &ctx,
            )
            .into_iter()
            .next()
            .unwrap()
            .expect("write del file");
    }

    let counter = std::sync::atomic::AtomicU64::new(0);
    c.bench_function("sys_unlink", |b| {
        b.iter(|| {
            let n = counter.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
            let path = format!("/bench_del_{n}.txt");
            let _ = kernel.sys_unlink(
                black_box(&[UnlinkRequest {
                    path,
                    recursive: false,
                }]),
                &ctx,
            );
        })
    });
}

fn bench_sys_rename(c: &mut Criterion) {
    let tmp = tempfile::tempdir().unwrap();
    let kernel = setup_kernel(tmp.path());
    let ctx = admin_ctx();

    for i in 0..5000 {
        let path = format!("/bench_ren_{i}.txt");
        kernel
            .sys_write(
                &[WriteRequest {
                    path,
                    content: b"x".to_vec(),
                    offset: 0,
                }],
                &ctx,
            )
            .into_iter()
            .next()
            .unwrap()
            .expect("write ren file");
    }

    let counter = std::sync::atomic::AtomicU64::new(0);
    c.bench_function("sys_rename", |b| {
        b.iter(|| {
            let n = counter.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
            let src = format!("/bench_ren_{n}.txt");
            let dst = format!("/bench_ren_dst_{n}.txt");
            let _ = kernel.sys_rename(black_box(&src), black_box(&dst), &ctx);
        })
    });
}

fn bench_pipe_roundtrip(c: &mut Criterion) {
    let tmp = tempfile::tempdir().unwrap();
    let kernel = setup_kernel(tmp.path());

    // Create DT_PIPE
    kernel
        .sys_setattr(
            "/bench/pipe",
            DT_PIPE as i32,
            "",
            None,
            None,
            None,
            "default",
            "root",
            false,
            4 * 1024 * 1024, // 4MB capacity
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
        .expect("create DT_PIPE");

    let payload =
        b"bench-payload-80-bytes-long-for-a-typical-audit-event-json-body-padding!!!!!!!!";

    let mut group = c.benchmark_group("pipe_roundtrip");

    group.bench_function("nexus_DT_PIPE", |b| {
        b.iter(|| {
            kernel
                .pipe_write_nowait(black_box("/bench/pipe"), black_box(payload))
                .expect("pipe write");
            let data = kernel
                .pipe_read_nowait(black_box("/bench/pipe"))
                .expect("pipe read");
            black_box(data);
        })
    });

    // OS pipe baseline
    let (r_fd, w_fd) = unsafe {
        let mut fds = [0i32; 2];
        assert_eq!(libc::pipe(fds.as_mut_ptr()), 0);
        (fds[0], fds[1])
    };
    let mut read_buf = [0u8; 128];
    group.bench_function("host_os_pipe", |b| {
        b.iter(|| unsafe {
            libc::write(w_fd, payload.as_ptr() as *const _, payload.len());
            libc::read(r_fd, read_buf.as_mut_ptr() as *mut _, payload.len());
            black_box(&read_buf);
        })
    });
    unsafe {
        libc::close(r_fd);
        libc::close(w_fd);
    }

    group.finish();
}

criterion_group!(
    benches,
    bench_sys_stat,
    bench_sys_read,
    bench_sys_write,
    bench_sys_readdir,
    bench_sys_unlink,
    bench_sys_rename,
    bench_pipe_roundtrip,
);
criterion_main!(benches);
