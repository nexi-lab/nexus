//! Benchmark-only comparison between the foyer-backed file cache and a small
//! in-memory SQLite baseline.
//!
//! Run with: cargo bench --bench cache_backends

use criterion::{
    black_box, criterion_group, criterion_main, BatchSize, BenchmarkId, Criterion, Throughput,
};
use nexus_fuse::cache::{CacheConfig, CacheLookup, FileCache, MAX_FILE_SIZE};
use rusqlite::{params, Connection, OptionalExtension};
use std::path::PathBuf;
use std::time::Duration;

struct SqliteBaseline {
    conn: Connection,
}

impl SqliteBaseline {
    fn new() -> Self {
        let conn = Connection::open_in_memory().expect("sqlite in-memory cache opens");
        conn.execute_batch(
            "CREATE TABLE file_cache (
                path TEXT PRIMARY KEY NOT NULL,
                content BLOB NOT NULL,
                etag TEXT
            );",
        )
        .expect("sqlite cache table is created");
        Self { conn }
    }

    fn put(&self, path: &str, content: &[u8], etag: Option<&str>) {
        self.conn
            .execute(
                "INSERT INTO file_cache(path, content, etag)
                 VALUES (?1, ?2, ?3)
                 ON CONFLICT(path) DO UPDATE SET
                    content = excluded.content,
                    etag = excluded.etag",
                params![path, content, etag],
            )
            .expect("sqlite cache put succeeds");
    }

    fn get(&self, path: &str) -> Option<Vec<u8>> {
        self.conn
            .query_row(
                "SELECT content FROM file_cache WHERE path = ?1",
                params![path],
                |row| row.get(0),
            )
            .optional()
            .expect("sqlite cache get succeeds")
    }
}

fn kept_tempdir_path() -> PathBuf {
    let dir = tempfile::tempdir().expect("temporary cache directory is created");
    let path = dir.path().to_path_buf();
    std::mem::forget(dir);
    path
}

fn foyer_cache(label: &str, memory_bytes: usize) -> FileCache {
    let dir = kept_tempdir_path();
    let config = CacheConfig::new(dir, memory_bytes, 256 * 1024 * 1024, MAX_FILE_SIZE)
        .expect("foyer cache config is valid");
    FileCache::new_with_config(&format!("http://bench-{label}.test"), config)
        .expect("foyer cache opens")
}

fn payload(size: usize) -> Vec<u8> {
    (0..size).map(|idx| (idx % 251) as u8).collect()
}

fn expect_foyer_hit(cache: &FileCache, path: &str) -> Vec<u8> {
    match cache.get(path, 0) {
        CacheLookup::Hit(entry) => entry.content,
        CacheLookup::NeedsRevalidation { .. } => panic!("foyer entry unexpectedly stale"),
        CacheLookup::Miss => panic!("foyer entry unexpectedly missing"),
    }
}

fn bench_warm_reads(c: &mut Criterion) {
    let mut group = c.benchmark_group("cache_warm_reads");

    for (label, size) in [
        ("1kib", 1024),
        ("10kib", 10 * 1024),
        ("100kib", 100 * 1024),
        ("1mib", 1024 * 1024),
    ] {
        let path = format!("/warm/{label}.bin");
        let content = payload(size);
        let foyer = foyer_cache(&format!("warm-{label}"), 32 * 1024 * 1024);
        let sqlite = SqliteBaseline::new();

        foyer.put(&path, &content, Some("warm-etag"), 0);
        sqlite.put(&path, &content, Some("warm-etag"));

        group.throughput(Throughput::Bytes(size as u64));
        group.bench_with_input(
            BenchmarkId::new("foyer_warm_read", label),
            &path,
            |b, path| {
                b.iter(|| {
                    let content = expect_foyer_hit(&foyer, black_box(path));
                    black_box(content);
                });
            },
        );
        group.bench_with_input(
            BenchmarkId::new("sqlite_warm_read", label),
            &path,
            |b, path| {
                b.iter(|| {
                    let content = sqlite.get(black_box(path)).expect("sqlite warm hit");
                    black_box(content);
                });
            },
        );
    }

    group.finish();
}

fn bench_agent_churn(c: &mut Criterion) {
    const OBJECTS: usize = 192;
    const HOT_SET: usize = 32;
    const OBJECT_SIZE: usize = 64 * 1024;
    const MEMORY_BYTES: usize = HOT_SET * OBJECT_SIZE;

    let paths = (0..OBJECTS)
        .map(|idx| format!("/agent/object-{idx:04}.bin"))
        .collect::<Vec<_>>();
    let content = payload(OBJECT_SIZE);
    let foyer = foyer_cache("agent-churn", MEMORY_BYTES);
    let sqlite = SqliteBaseline::new();

    for path in &paths {
        foyer.put(path, &content, Some("churn-etag"), 0);
        sqlite.put(path, &content, Some("churn-etag"));
    }

    let trace = (0..4096)
        .map(|idx| {
            if idx % 5 == 0 {
                (idx * 37) % OBJECTS
            } else {
                idx % HOT_SET
            }
        })
        .collect::<Vec<_>>();

    let mut group = c.benchmark_group("cache_agent_churn");
    group.throughput(Throughput::Bytes(OBJECT_SIZE as u64));

    group.bench_function("foyer_agent_churn", |b| {
        let mut idx = 0;
        b.iter_batched(
            || {
                let path = &paths[trace[idx % trace.len()]];
                idx += 1;
                path
            },
            |path| {
                let content = expect_foyer_hit(&foyer, black_box(path));
                black_box(content);
            },
            BatchSize::SmallInput,
        );
    });

    group.bench_function("sqlite_agent_churn", |b| {
        let mut idx = 0;
        b.iter_batched(
            || {
                let path = &paths[trace[idx % trace.len()]];
                idx += 1;
                path
            },
            |path| {
                let content = sqlite.get(black_box(path)).expect("sqlite churn hit");
                black_box(content);
            },
            BatchSize::SmallInput,
        );
    });

    group.finish();
}

criterion_group! {
    name = benches;
    config = Criterion::default()
        .sample_size(10)
        .warm_up_time(Duration::from_millis(500))
        .measurement_time(Duration::from_secs(1));
    targets = bench_warm_reads, bench_agent_churn
}
criterion_main!(benches);
