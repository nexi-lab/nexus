//! Benchmarks for Nexus FUSE operations
//!
//! These benchmarks measure the performance of hot path operations:
//! - read() - File reads (cached and cold)
//! - write() - File writes
//! - list() - Directory listing
//! - stat() - File metadata
//! - mkdir() - Directory creation
//! - delete() - File deletion
//! - rename() - File rename
//!
//! Run with: cargo bench

use criterion::{black_box, criterion_group, criterion_main, Criterion, BenchmarkId};
use nexus_fuse::client::NexusClient;
use std::time::Duration;

/// Setup test server connection
fn setup_client() -> NexusClient {
    // Use local test server (must be running)
    NexusClient::new(
        "http://localhost:2026",
        "sk-test-key-123",
        None,
    )
    .expect("Failed to create NexusClient")
}

/// Benchmark file read operations (cached)
fn bench_read_cached(c: &mut Criterion) {
    let client = setup_client();

    // Create a test file
    let test_content = vec![0u8; 1024]; // 1KB
    client.write("/bench-read.txt", &test_content).unwrap();

    // First read to warm cache
    let _ = client.read("/bench-read.txt").unwrap();

    c.bench_function("read_1kb_cached", |b| {
        b.iter(|| {
            let content = client.read(black_box("/bench-read.txt")).unwrap();
            black_box(content);
        });
    });

    // Cleanup
    let _ = client.delete("/bench-read.txt");
}

/// Benchmark file read operations (cold - different files each time)
fn bench_read_cold(c: &mut Criterion) {
    let client = setup_client();

    // Pre-create 100 test files
    let test_content = vec![0u8; 1024]; // 1KB
    for i in 0..100 {
        let path = format!("/bench-read-cold-{}.txt", i);
        client.write(&path, &test_content).unwrap();
    }

    let mut counter = 0;
    c.bench_function("read_1kb_cold", |b| {
        b.iter(|| {
            let path = format!("/bench-read-cold-{}.txt", counter % 100);
            counter += 1;
            let content = client.read(black_box(&path)).unwrap();
            black_box(content);
        });
    });

    // Cleanup
    for i in 0..100 {
        let path = format!("/bench-read-cold-{}.txt", i);
        let _ = client.delete(&path);
    }
}

/// Benchmark file read with different sizes
fn bench_read_sizes(c: &mut Criterion) {
    let client = setup_client();
    let mut group = c.benchmark_group("read_by_size");

    for size_kb in [1, 10, 100, 1000].iter() {
        let size = size_kb * 1024;
        let test_content = vec![0u8; size];
        let path = format!("/bench-read-{}kb.txt", size_kb);

        // Create and warm cache
        client.write(&path, &test_content).unwrap();
        let _ = client.read(&path).unwrap();

        group.bench_with_input(
            BenchmarkId::from_parameter(format!("{}kb", size_kb)),
            size_kb,
            |b, _| {
                b.iter(|| {
                    let content = client.read(black_box(&path)).unwrap();
                    black_box(content);
                });
            },
        );

        // Cleanup
        let _ = client.delete(&path);
    }

    group.finish();
}

/// Benchmark file write operations
fn bench_write(c: &mut Criterion) {
    let client = setup_client();
    let test_content = vec![0u8; 1024]; // 1KB

    let mut counter = 0;
    c.bench_function("write_1kb", |b| {
        b.iter(|| {
            let path = format!("/bench-write-{}.txt", counter);
            counter += 1;
            client.write(black_box(&path), black_box(&test_content)).unwrap();
        });
    });

    // Cleanup
    for i in 0..counter {
        let path = format!("/bench-write-{}.txt", i);
        let _ = client.delete(&path);
    }
}

/// Benchmark write with different sizes
fn bench_write_sizes(c: &mut Criterion) {
    let client = setup_client();
    let mut group = c.benchmark_group("write_by_size");

    for size_kb in [1, 10, 100, 1000].iter() {
        let size = size_kb * 1024;
        let test_content = vec![0u8; size];

        let mut counter = 0;
        group.bench_with_input(
            BenchmarkId::from_parameter(format!("{}kb", size_kb)),
            size_kb,
            |b, _| {
                b.iter(|| {
                    let path = format!("/bench-write-{}kb-{}.txt", size_kb, counter);
                    counter += 1;
                    client.write(black_box(&path), black_box(&test_content)).unwrap();
                });
            },
        );

        // Cleanup
        for i in 0..counter {
            let path = format!("/bench-write-{}kb-{}.txt", size_kb, i);
            let _ = client.delete(&path);
        }
    }

    group.finish();
}

/// Benchmark directory listing
fn bench_list(c: &mut Criterion) {
    let client = setup_client();

    // Create a directory with 100 files
    client.mkdir("/bench-list").unwrap();
    for i in 0..100 {
        let path = format!("/bench-list/file{}.txt", i);
        client.write(&path, b"test").unwrap();
    }

    c.bench_function("list_100_files", |b| {
        b.iter(|| {
            let entries = client.list(black_box("/bench-list")).unwrap();
            black_box(entries);
        });
    });

    // Cleanup
    for i in 0..100 {
        let path = format!("/bench-list/file{}.txt", i);
        let _ = client.delete(&path);
    }
    let _ = client.delete("/bench-list");
}

/// Benchmark stat operations
fn bench_stat(c: &mut Criterion) {
    let client = setup_client();

    // Create a test file
    client.write("/bench-stat.txt", b"test content").unwrap();

    c.bench_function("stat", |b| {
        b.iter(|| {
            let metadata = client.stat(black_box("/bench-stat.txt")).unwrap();
            black_box(metadata);
        });
    });

    // Cleanup
    let _ = client.delete("/bench-stat.txt");
}

/// Benchmark mkdir operations
fn bench_mkdir(c: &mut Criterion) {
    let client = setup_client();

    let mut counter = 0;
    c.bench_function("mkdir", |b| {
        b.iter(|| {
            let path = format!("/bench-mkdir-{}", counter);
            counter += 1;
            client.mkdir(black_box(&path)).unwrap();
        });
    });

    // Cleanup
    for i in 0..counter {
        let path = format!("/bench-mkdir-{}", i);
        let _ = client.delete(&path);
    }
}

/// Benchmark delete operations
fn bench_delete(c: &mut Criterion) {
    let client = setup_client();

    // Pre-create files to delete
    for i in 0..100 {
        let path = format!("/bench-delete-{}.txt", i);
        client.write(&path, b"test").unwrap();
    }

    let mut counter = 0;
    c.bench_function("delete", |b| {
        b.iter(|| {
            let path = format!("/bench-delete-{}.txt", counter % 100);
            counter += 1;
            // Recreate before each delete
            client.write(&path, b"test").unwrap();
            client.delete(black_box(&path)).unwrap();
        });
    });
}

/// Benchmark rename operations
fn bench_rename(c: &mut Criterion) {
    let client = setup_client();

    let mut counter = 0;
    c.bench_function("rename", |b| {
        b.iter(|| {
            let old_path = format!("/bench-rename-old-{}.txt", counter);
            let new_path = format!("/bench-rename-new-{}.txt", counter);
            counter += 1;

            // Create file
            client.write(&old_path, b"test").unwrap();

            // Rename
            client.rename(black_box(&old_path), black_box(&new_path)).unwrap();

            // Cleanup
            let _ = client.delete(&new_path);
        });
    });
}

/// Benchmark exists checks
fn bench_exists(c: &mut Criterion) {
    let client = setup_client();

    // Create a test file
    client.write("/bench-exists.txt", b"test").unwrap();

    c.bench_function("exists_true", |b| {
        b.iter(|| {
            let exists = client.exists(black_box("/bench-exists.txt"));
            black_box(exists);
        });
    });

    c.bench_function("exists_false", |b| {
        b.iter(|| {
            let exists = client.exists(black_box("/bench-nonexistent.txt"));
            black_box(exists);
        });
    });

    // Cleanup
    let _ = client.delete("/bench-exists.txt");
}

// Configure criterion
criterion_group! {
    name = benches;
    config = Criterion::default()
        .measurement_time(Duration::from_secs(10))
        .sample_size(100);
    targets =
        bench_read_cached,
        bench_read_cold,
        bench_read_sizes,
        bench_write,
        bench_write_sizes,
        bench_list,
        bench_stat,
        bench_mkdir,
        bench_delete,
        bench_rename,
        bench_exists
}

criterion_main!(benches);
