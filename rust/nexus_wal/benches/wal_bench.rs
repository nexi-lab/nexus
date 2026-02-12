//! WAL benchmarks using criterion.

use criterion::{criterion_group, criterion_main, Criterion};
use tempfile::TempDir;

// The crate is named `_nexus_wal` for cdylib, but we use the package name for rlib.
use _nexus_wal::wal::{SyncMode, WalEngine};

fn bench_append_single(c: &mut Criterion) {
    let dir = TempDir::new().unwrap();
    let wal = WalEngine::open(dir.path(), 64 * 1024 * 1024, SyncMode::None).unwrap();

    c.bench_function("append_single", |b| {
        b.iter(|| {
            wal.append(b"zone-1", b"{\"type\":\"file_write\",\"path\":\"/test.txt\"}")
                .unwrap();
        });
    });
}

fn bench_append_batch_1k(c: &mut Criterion) {
    let dir = TempDir::new().unwrap();
    let wal = WalEngine::open(dir.path(), 64 * 1024 * 1024, SyncMode::None).unwrap();

    let events: Vec<(Vec<u8>, Vec<u8>)> = (0..1000)
        .map(|i| {
            (
                b"zone-1".to_vec(),
                format!("{{\"type\":\"file_write\",\"path\":\"/file-{i}.txt\"}}").into_bytes(),
            )
        })
        .collect();

    c.bench_function("append_batch_1k", |b| {
        b.iter(|| {
            wal.append_batch(&events).unwrap();
        });
    });
}

fn bench_append_batch_10k(c: &mut Criterion) {
    let dir = TempDir::new().unwrap();
    let wal = WalEngine::open(dir.path(), 64 * 1024 * 1024, SyncMode::None).unwrap();

    let events: Vec<(Vec<u8>, Vec<u8>)> = (0..10_000)
        .map(|i| {
            (
                b"zone-1".to_vec(),
                format!("{{\"type\":\"file_write\",\"path\":\"/file-{i}.txt\"}}").into_bytes(),
            )
        })
        .collect();

    c.bench_function("append_batch_10k", |b| {
        b.iter(|| {
            wal.append_batch(&events).unwrap();
        });
    });
}

fn bench_read_1k(c: &mut Criterion) {
    let dir = TempDir::new().unwrap();
    let wal = WalEngine::open(dir.path(), 64 * 1024 * 1024, SyncMode::None).unwrap();

    // Pre-populate with 10K records
    let events: Vec<(Vec<u8>, Vec<u8>)> = (0..10_000)
        .map(|i| {
            (
                b"zone-1".to_vec(),
                format!("{{\"type\":\"file_write\",\"path\":\"/file-{i}.txt\"}}").into_bytes(),
            )
        })
        .collect();
    wal.append_batch(&events).unwrap();

    c.bench_function("read_1k_from_middle", |b| {
        b.iter(|| {
            wal.read_from(5000, 1000, None).unwrap();
        });
    });
}

criterion_group!(
    benches,
    bench_append_single,
    bench_append_batch_1k,
    bench_append_batch_10k,
    bench_read_1k,
);
criterion_main!(benches);
