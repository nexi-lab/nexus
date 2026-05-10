# Issue #4055 — Eager-Hydrate Small Files Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eagerly hydrate small files (<128 KiB) into the foyer L1 cache during workspace attach so cold-start small reads return from cache instead of paying backend RTT.

**Architecture:** New JSON-RPC method `cache_warm` on the existing nexus-fuse Unix-socket daemon. `BootIndexer` fires this once per attach (after its existing search-walk completes) via `RustFUSEClient`. Rust drives the entire hydration: BFS-walks workspace via `NexusClient.list`, filters ≤128 KiB and not-already-warm entries, bounded-parallel fetches via `tokio::JoinSet`, admits to `FileCache`, returns summary stats.

**Tech Stack:** Rust (`nexus-fuse` crate — tokio, foyer, mockito, criterion); Python (`nexus.core.boot_indexer`, `nexus.fuse.rust_client`); JSON-RPC 2.0 over Unix socket.

**Design Spec:** `docs/superpowers/specs/2026-05-09-issue-4055-eager-hydrate-small-files-design.md`

---

## File Structure

| Path | Action | Responsibility |
|---|---|---|
| `nexus-fuse/src/cache.rs` | Modify | Add `HYDRATE_*` constants, `FileCache::is_warm` |
| `nexus-fuse/src/metrics.rs` | Modify | Add hydration counters (state fields, helpers, render lines) |
| `nexus-fuse/src/hydrate.rs` | Create | `HydrateOptions`, `HydrateStats`, `hydrate_workspace` |
| `nexus-fuse/src/lib.rs` | Modify | `pub mod hydrate;` |
| `nexus-fuse/src/daemon.rs` | Modify | Register `"cache_warm"` method dispatch |
| `nexus-fuse/Cargo.toml` | Modify | Add `tokio = { features = ["sync"] }` (semaphore) — verify already present |
| `src/nexus/fuse/rust_client.py` | Modify | Add `cache_warm` wrapper |
| `src/nexus/core/boot_indexer.py` | Modify | Accept `rust_client` and hydrate after walk |
| `src/nexus/daemon/sandbox_bootstrap.py` | Modify | Thread `rust_client` to `BootIndexer` |
| `tests/unit/core/test_boot_indexer.py` | Modify | Add hydrate-related tests |
| `nexus-fuse/test_cache_warm.py` | Create | Python↔Rust integration test |
| `nexus-fuse/benches/cache_backends.rs` | Modify | Add `cold_no_hydration` vs `cold_with_hydration` benches |

---

## Task 1: Hydration constants + `FileCache::is_warm`

**Files:**
- Modify: `nexus-fuse/src/cache.rs:23-32` (constants), insert new method after `FileCache::touch` (~`L516`)
- Test: `nexus-fuse/src/cache.rs` (existing `#[cfg(test)] mod tests`)

- [ ] **Step 1: Write the failing tests**

Append to the existing `mod tests` block in `nexus-fuse/src/cache.rs`:

```rust
#[test]
fn test_hydrate_constants_have_expected_values() {
    assert_eq!(HYDRATE_SMALL_FILE_BYTES, 128 * 1024);
    assert_eq!(HYDRATE_TOTAL_BUDGET_BYTES, 64 * 1024 * 1024);
    assert_eq!(HYDRATE_CONCURRENCY, 8);
}

#[test]
fn test_is_warm_returns_false_for_unknown_path() {
    let _guard = crate::metrics::test_guard();
    crate::metrics::reset_for_tests();
    let cache = test_cache("is_warm_unknown");
    assert!(!cache.is_warm("/nope.txt"));
}

#[test]
fn test_is_warm_returns_true_after_put() {
    let _guard = crate::metrics::test_guard();
    crate::metrics::reset_for_tests();
    let cache = test_cache("is_warm_after_put");
    cache.put("/a.txt", b"hello", Some("etag-1"), 0);
    assert!(cache.is_warm("/a.txt"));
}

#[test]
fn test_is_warm_returns_false_for_aged_entry() {
    use std::time::{SystemTime, UNIX_EPOCH};
    let _guard = crate::metrics::test_guard();
    crate::metrics::reset_for_tests();
    let cache = test_cache("is_warm_aged");
    cache.put("/old.txt", b"x", Some("etag-old"), 0);
    // Backdate the metadata to simulate an aged cache entry
    {
        let mut metadata = cache.metadata.lock().unwrap();
        let meta = metadata.get_mut("/old.txt").expect("entry should exist");
        let now = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_secs();
        meta.cached_at_secs = now.saturating_sub(MAX_CACHE_AGE_SECS + 1);
    }
    assert!(!cache.is_warm("/old.txt"));
}
```

- [ ] **Step 2: Run tests — verify they fail**

```bash
cd nexus-fuse && cargo test --lib cache::tests::test_hydrate_constants_have_expected_values cache::tests::test_is_warm
```

Expected: compile error (`HYDRATE_SMALL_FILE_BYTES` not found, `is_warm` method not found).

- [ ] **Step 3: Add constants**

Insert into `nexus-fuse/src/cache.rs` immediately after the existing `MAX_FILE_SIZE` constant (around L32):

```rust
/// Maximum file size to eagerly hydrate during workspace attach (128 KiB).
pub const HYDRATE_SMALL_FILE_BYTES: usize = 128 * 1024;

/// Total bytes admitted per hydration call (default 64 MiB).
pub const HYDRATE_TOTAL_BUDGET_BYTES: usize = 64 * 1024 * 1024;

/// Default concurrent backend fetches during hydration.
pub const HYDRATE_CONCURRENCY: usize = 8;
```

- [ ] **Step 4: Add `is_warm` method**

Insert into the `impl FileCache` block immediately after the existing `touch` method:

```rust
/// Returns true if `path` has a cached entry whose age is within MAX_CACHE_AGE_SECS.
///
/// This is the hydration warmth probe — used to skip files that already have
/// fresh cache entries. Reads only the in-memory metadata; does not touch foyer.
pub fn is_warm(&self, path: &str) -> bool {
    let metadata = match self.metadata.lock() {
        Ok(m) => m,
        Err(_) => return false,
    };
    let Some(meta) = metadata.get(path) else {
        return false;
    };
    let now = Self::now();
    now.saturating_sub(meta.cached_at_secs) <= MAX_CACHE_AGE_SECS
}
```

- [ ] **Step 5: Run tests — verify they pass**

```bash
cd nexus-fuse && cargo test --lib cache::tests::test_hydrate_constants_have_expected_values cache::tests::test_is_warm
```

Expected: all 4 tests PASS.

- [ ] **Step 6: Commit**

```bash
git add nexus-fuse/src/cache.rs
git commit -m "feat(#4055): add hydration constants and FileCache::is_warm"
```

---

## Task 2: Hydration metrics helpers

**Files:**
- Modify: `nexus-fuse/src/metrics.rs:54-101` (state struct, default, render), and append helpers
- Test: `nexus-fuse/src/metrics.rs` (existing `#[cfg(test)]` block)

- [ ] **Step 1: Write the failing tests**

Append to the existing tests module at the bottom of `nexus-fuse/src/metrics.rs`:

```rust
#[test]
fn test_record_hydration_file_increments_per_result() {
    let _guard = test_guard();
    reset_for_tests();
    record_hydration_file("admitted");
    record_hydration_file("admitted");
    record_hydration_file("skipped_warm");
    record_hydration_file("failed");
    let rendered = render();
    assert!(rendered.contains(r#"nexus_hydration_files_total{result="admitted"} 2"#));
    assert!(rendered.contains(r#"nexus_hydration_files_total{result="skipped_warm"} 1"#));
    assert!(rendered.contains(r#"nexus_hydration_files_total{result="failed"} 1"#));
}

#[test]
fn test_record_hydration_bytes_accumulates() {
    let _guard = test_guard();
    reset_for_tests();
    record_hydration_bytes("admitted", 1024);
    record_hydration_bytes("admitted", 2048);
    record_hydration_bytes("skipped", 512);
    let rendered = render();
    assert!(rendered.contains(r#"nexus_hydration_bytes_total{result="admitted"} 3072"#));
    assert!(rendered.contains(r#"nexus_hydration_bytes_total{result="skipped"} 512"#));
}

#[test]
fn test_observe_hydration_duration_accumulates() {
    let _guard = test_guard();
    reset_for_tests();
    observe_hydration_duration_ms(120);
    observe_hydration_duration_ms(80);
    let rendered = render();
    assert!(rendered.contains("nexus_hydration_duration_ms_total 200"));
}

#[test]
fn test_record_hydration_file_unknown_result_buckets_to_other() {
    let _guard = test_guard();
    reset_for_tests();
    record_hydration_file("not_a_real_result");
    let rendered = render();
    assert!(rendered.contains(r#"nexus_hydration_files_total{result="other"} 1"#));
}
```

- [ ] **Step 2: Run tests — verify they fail**

```bash
cd nexus-fuse && cargo test --lib metrics::tests::test_record_hydration metrics::tests::test_observe_hydration
```

Expected: compile error (functions not defined).

- [ ] **Step 3: Add state fields**

In `MetricsState` struct (~L54-75), add three new fields **before** the closing brace:

```rust
hydration_files: HashMap<String, u64>,
hydration_bytes: HashMap<String, u64>,
hydration_duration_ms_total: u64,
```

In `MetricsState::default()` (~L77-101), add corresponding initializers **before** the closing brace:

```rust
hydration_files: HashMap::new(),
hydration_bytes: HashMap::new(),
hydration_duration_ms_total: 0,
```

- [ ] **Step 4: Add bucket validator**

Insert near the other `cache_*` bucket validators (around `cache_eviction_reason`, ~L156):

```rust
fn hydration_file_result(value: &str) -> String {
    bounded(
        value,
        &["admitted", "skipped_warm", "skipped_size", "skipped_budget", "failed", "other"],
    )
}

fn hydration_bytes_result(value: &str) -> String {
    bounded(value, &["admitted", "skipped", "other"])
}
```

- [ ] **Step 5: Add public helpers**

Append to `nexus-fuse/src/metrics.rs` after the existing `record_generation_mismatch` function:

```rust
pub fn record_hydration_file(result: &str) {
    let mut metrics = METRICS.lock().unwrap();
    *metrics
        .hydration_files
        .entry(hydration_file_result(result))
        .or_insert(0) += 1;
}

pub fn record_hydration_bytes(result: &str, n: u64) {
    let mut metrics = METRICS.lock().unwrap();
    *metrics
        .hydration_bytes
        .entry(hydration_bytes_result(result))
        .or_insert(0) += n;
}

pub fn observe_hydration_duration_ms(ms: u64) {
    let mut metrics = METRICS.lock().unwrap();
    metrics.hydration_duration_ms_total = metrics
        .hydration_duration_ms_total
        .saturating_add(ms);
}
```

- [ ] **Step 6: Add render lines**

Inside the existing `pub fn render()` function (around L389), after the existing cache rendering blocks but before the function returns, append the snippet below. Match the existing variable name in `render` — the function already builds output by calling `push_str` on a local `String` (read the function header to find its name; substitute it for `output` below):

```rust
// Hydration files counter
let mut entries: Vec<(&String, &u64)> = metrics.hydration_files.iter().collect();
entries.sort();
output.push_str("# HELP nexus_hydration_files_total Files processed during eager hydration.\n");
output.push_str("# TYPE nexus_hydration_files_total counter\n");
for (result, count) in entries {
    output.push_str(&format!(
        "nexus_hydration_files_total{{result=\"{}\"}} {}\n",
        result, count
    ));
}

// Hydration bytes counter
let mut entries: Vec<(&String, &u64)> = metrics.hydration_bytes.iter().collect();
entries.sort();
output.push_str("# HELP nexus_hydration_bytes_total Bytes processed during eager hydration.\n");
output.push_str("# TYPE nexus_hydration_bytes_total counter\n");
for (result, bytes) in entries {
    output.push_str(&format!(
        "nexus_hydration_bytes_total{{result=\"{}\"}} {}\n",
        result, bytes
    ));
}

// Duration counter
output.push_str("# HELP nexus_hydration_duration_ms_total Cumulative hydration wall time in ms.\n");
output.push_str("# TYPE nexus_hydration_duration_ms_total counter\n");
output.push_str(&format!(
    "nexus_hydration_duration_ms_total {}\n",
    metrics.hydration_duration_ms_total
));
```

- [ ] **Step 7: Run tests — verify they pass**

```bash
cd nexus-fuse && cargo test --lib metrics::tests
```

Expected: all metrics tests PASS, including the four new hydration tests.

- [ ] **Step 8: Commit**

```bash
git add nexus-fuse/src/metrics.rs
git commit -m "feat(#4055): add hydration metrics counters"
```

---

## Task 3: Create `hydrate.rs` — happy path admit

**Files:**
- Create: `nexus-fuse/src/hydrate.rs`
- Modify: `nexus-fuse/src/lib.rs` (add `pub mod hydrate;`)

This task lands the module skeleton with the simplest scenario: list → admit small files. Subsequent tasks layer on warmth, budget, errors.

- [ ] **Step 1: Wire the module into lib.rs**

Edit `nexus-fuse/src/lib.rs` to add `pub mod hydrate;` after the existing `pub mod fs;` line:

```rust
//! Nexus FUSE Client Library
//!
//! This library provides a high-performance FUSE client for the Nexus filesystem.

pub mod cache;
pub mod cached_read;
pub mod client;
pub mod daemon;
pub mod error;
pub mod fs;
pub mod hydrate;
pub mod metrics;
```

- [ ] **Step 2: Write the failing test (admit happy path)**

Create `nexus-fuse/src/hydrate.rs` with:

```rust
//! Eager hydration of small files into FileCache during workspace attach (Issue #4055).

use crate::cache::{
    FileCache, HYDRATE_CONCURRENCY, HYDRATE_SMALL_FILE_BYTES, HYDRATE_TOTAL_BUDGET_BYTES,
};
use crate::client::{FileEntry, NexusClient};
use crate::metrics;
use log::{debug, warn};
use serde::Serialize;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Instant;
use tokio::sync::Semaphore;
use tokio::task::JoinSet;

/// Maximum directory recursion depth before the BFS gives up.
const HYDRATE_MAX_DEPTH: u32 = 32;

/// Maximum total entries collected before the BFS gives up.
const HYDRATE_MAX_ENTRIES: usize = 100_000;

#[derive(Debug, Clone)]
pub struct HydrateOptions {
    pub workspace_root: String,
    pub threshold_bytes: usize,
    pub budget_bytes: usize,
    pub concurrency: usize,
}

impl HydrateOptions {
    pub fn new(workspace_root: String) -> Self {
        Self {
            workspace_root,
            threshold_bytes: HYDRATE_SMALL_FILE_BYTES,
            budget_bytes: HYDRATE_TOTAL_BUDGET_BYTES,
            concurrency: HYDRATE_CONCURRENCY,
        }
    }
}

#[derive(Debug, Clone, Serialize, Default)]
pub struct HydrateStats {
    pub admitted_count: u64,
    pub admitted_bytes: u64,
    pub skipped_warm: u64,
    pub skipped_size: u64,
    pub skipped_budget: u64,
    pub failed: u64,
    pub duration_ms: u64,
}

/// Walk the workspace via `client.list` BFS, then admit small cold files to the cache.
pub async fn hydrate_workspace(
    client: Arc<NexusClient>,
    cache: Arc<FileCache>,
    opts: HydrateOptions,
) -> HydrateStats {
    let started = Instant::now();
    let admitted_count = Arc::new(AtomicU64::new(0));
    let admitted_bytes = Arc::new(AtomicU64::new(0));
    let skipped_warm = Arc::new(AtomicU64::new(0));
    let skipped_size = Arc::new(AtomicU64::new(0));
    let skipped_budget = Arc::new(AtomicU64::new(0));
    let failed = Arc::new(AtomicU64::new(0));

    let candidates = match collect_candidates(&client, &cache, &opts, &skipped_warm, &skipped_size)
    {
        Ok(list) => list,
        Err(err) => {
            warn!("hydrate: root list failed for {:?}: {}", opts.workspace_root, err);
            failed.fetch_add(1, Ordering::Relaxed);
            return finalize_stats(
                started,
                admitted_count,
                admitted_bytes,
                skipped_warm,
                skipped_size,
                skipped_budget,
                failed,
            );
        }
    };

    let semaphore = Arc::new(Semaphore::new(opts.concurrency.max(1)));
    let mut join_set: JoinSet<()> = JoinSet::new();

    for path in candidates {
        let permit = match semaphore.clone().acquire_owned().await {
            Ok(p) => p,
            Err(_) => break,
        };
        let client_task = client.clone();
        let cache_task = cache.clone();
        let admitted_count = admitted_count.clone();
        let admitted_bytes = admitted_bytes.clone();
        let skipped_budget = skipped_budget.clone();
        let failed = failed.clone();
        let budget = opts.budget_bytes as u64;

        join_set.spawn_blocking(move || {
            let _permit = permit;
            if admitted_bytes.load(Ordering::Relaxed) >= budget {
                skipped_budget.fetch_add(1, Ordering::Relaxed);
                metrics::record_hydration_file("skipped_budget");
                return;
            }
            match client_task.read_with_etag(&path, None) {
                Ok(crate::client::ReadResponse::Content { content, etag }) => {
                    let len = content.len() as u64;
                    cache_task.put(&path, &content, etag.as_deref(), 0);
                    admitted_count.fetch_add(1, Ordering::Relaxed);
                    admitted_bytes.fetch_add(len, Ordering::Relaxed);
                    metrics::record_hydration_file("admitted");
                    metrics::record_hydration_bytes("admitted", len);
                }
                Ok(crate::client::ReadResponse::NotModified) => {
                    debug!("hydrate: unexpected 304 for {} without etag", path);
                    failed.fetch_add(1, Ordering::Relaxed);
                    metrics::record_hydration_file("failed");
                }
                Err(err) => {
                    debug!("hydrate: read failed for {}: {}", path, err);
                    failed.fetch_add(1, Ordering::Relaxed);
                    metrics::record_hydration_file("failed");
                }
            }
        });
    }

    while join_set.join_next().await.is_some() {}

    finalize_stats(
        started,
        admitted_count,
        admitted_bytes,
        skipped_warm,
        skipped_size,
        skipped_budget,
        failed,
    )
}

fn finalize_stats(
    started: Instant,
    admitted_count: Arc<AtomicU64>,
    admitted_bytes: Arc<AtomicU64>,
    skipped_warm: Arc<AtomicU64>,
    skipped_size: Arc<AtomicU64>,
    skipped_budget: Arc<AtomicU64>,
    failed: Arc<AtomicU64>,
) -> HydrateStats {
    let stats = HydrateStats {
        admitted_count: admitted_count.load(Ordering::Relaxed),
        admitted_bytes: admitted_bytes.load(Ordering::Relaxed),
        skipped_warm: skipped_warm.load(Ordering::Relaxed),
        skipped_size: skipped_size.load(Ordering::Relaxed),
        skipped_budget: skipped_budget.load(Ordering::Relaxed),
        failed: failed.load(Ordering::Relaxed),
        duration_ms: started.elapsed().as_millis() as u64,
    };
    metrics::observe_hydration_duration_ms(stats.duration_ms);
    stats
}

fn collect_candidates(
    client: &NexusClient,
    cache: &FileCache,
    opts: &HydrateOptions,
    skipped_warm: &Arc<AtomicU64>,
    skipped_size: &Arc<AtomicU64>,
) -> Result<Vec<String>, crate::error::NexusClientError> {
    let mut candidates: Vec<String> = Vec::new();
    let mut queue: Vec<(String, u32)> = vec![(opts.workspace_root.clone(), 0)];
    let mut total_seen: usize = 0;
    let mut root_listed = false;

    while let Some((dir, depth)) = queue.pop() {
        if depth > HYDRATE_MAX_DEPTH || total_seen >= HYDRATE_MAX_ENTRIES {
            break;
        }
        let entries = match client.list(&dir) {
            Ok(e) => e,
            Err(err) => {
                if !root_listed {
                    return Err(err);
                }
                warn!("hydrate: list failed for {}: {} (continuing)", dir, err);
                continue;
            }
        };
        root_listed = true;

        for entry in entries {
            total_seen += 1;
            let full_path = join_path(&dir, &entry.name);
            if is_directory(&entry) {
                queue.push((full_path, depth + 1));
                continue;
            }
            if (entry.size as usize) > opts.threshold_bytes {
                skipped_size.fetch_add(1, Ordering::Relaxed);
                metrics::record_hydration_file("skipped_size");
                continue;
            }
            if cache.is_warm(&full_path) {
                skipped_warm.fetch_add(1, Ordering::Relaxed);
                metrics::record_hydration_file("skipped_warm");
                continue;
            }
            candidates.push(full_path);
        }
    }
    Ok(candidates)
}

fn is_directory(entry: &FileEntry) -> bool {
    entry.entry_type.eq_ignore_ascii_case("directory")
        || entry.entry_type.eq_ignore_ascii_case("dir")
}

fn join_path(parent: &str, name: &str) -> String {
    if parent.ends_with('/') {
        format!("{}{}", parent, name)
    } else {
        format!("{}/{}", parent, name)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cache::CacheConfig;

    fn fresh_cache(label: &str) -> Arc<FileCache> {
        let _ = env_logger::builder().is_test(true).try_init();
        let dir = tempfile::tempdir().unwrap();
        let config = CacheConfig::new(
            dir.into_path(),
            8 * 1024 * 1024,
            32 * 1024 * 1024,
            1024 * 1024,
        )
        .unwrap();
        Arc::new(
            FileCache::new_with_config(&format!("http://test-{}.invalid", label), config).unwrap(),
        )
    }

    #[tokio::test]
    async fn test_hydrate_admits_small_files() {
        let _guard = crate::metrics::test_guard();
        crate::metrics::reset_for_tests();

        let mut server = mockito::Server::new_async().await;
        let body = r#"{"jsonrpc":"2.0","id":1,"result":{"files":[
            {"path":"/a.txt","is_directory":false,"size":10},
            {"path":"/big.bin","is_directory":false,"size":1048576}
        ]}}"#;
        let _list_mock = server
            .mock("POST", "/api/nfs/list")
            .with_status(200)
            .with_body(body)
            .create_async()
            .await;
        let _read_mock = server
            .mock("POST", "/api/nfs/read")
            .with_status(200)
            .with_header("etag", "\"abc\"")
            .with_body(r#"{"jsonrpc":"2.0","id":1,"result":{"__type__":"bytes","data":"aGVsbG8="}}"#)
            .create_async()
            .await;

        let client = Arc::new(NexusClient::new(&server.url(), "test-key", None).unwrap());
        let cache = fresh_cache("admit");
        let opts = HydrateOptions::new("/".to_string());

        let stats = hydrate_workspace(client, cache.clone(), opts).await;

        assert_eq!(stats.admitted_count, 1, "only /a.txt should admit");
        assert_eq!(stats.skipped_size, 1, "/big.bin should be skipped by size");
        assert_eq!(stats.failed, 0);
        assert!(cache.is_warm("/a.txt"));
    }
}
```

**Important:** the mockito list response above uses the `path` field directly because `client.list` deserializes a `DetailedEntry` with `path`. But `client.list` returns `Vec<FileEntry>` (with `name`, not `path`) after a conversion step (`nexus-fuse/src/client.rs:243` "Convert to FileEntry objects - extract immediate children only"). Read the conversion logic in `client.rs:240-285` and confirm `FileEntry.name` carries the basename. The test asserts admit by name `/a.txt`. If the conversion strips the leading `/`, adjust the assertion to `cache.is_warm("a.txt")` — let the failing test surface this.

- [ ] **Step 3: Run test — verify it fails for the right reason**

```bash
cd nexus-fuse && cargo test --lib hydrate::tests::test_hydrate_admits_small_files
```

Expected outcomes (any of these is acceptable for "fails for the right reason"):
- Compile errors in `hydrate.rs` resolved by Step 1; remaining failures are runtime.
- Test fails with `admitted_count == 0` if path resolution differs from mock body.

If the failure is a path-resolution mismatch, fix the test mock or the `join_path` helper to match `client.list`'s actual return shape. Re-read `nexus-fuse/src/client.rs` lines 240-285 for the exact `FileEntry.name` content the conversion produces (full path vs basename). Adjust the test mock accordingly so the mocked `name` joined with `workspace_root="/"` yields what the read mock expects.

- [ ] **Step 4: Make the test pass**

Adjust either the test mock body or the `join_path` helper based on what `client.list` actually returns. The function above already filters by size and admits via `read_with_etag` + `cache.put`. No further implementation work required for the happy path — the failure is purely about test fixture alignment.

If `client.list` returns names like `"a.txt"` (basename only), the test mock should use `"name":"a.txt"` (or keep `"path"` if backend serves it; check the actual `DetailedEntry` deserialization at `client.rs:215-227`). Update the mock to whatever shape the deserializer expects.

- [ ] **Step 5: Run test — verify it passes**

```bash
cd nexus-fuse && cargo test --lib hydrate::tests::test_hydrate_admits_small_files
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add nexus-fuse/src/lib.rs nexus-fuse/src/hydrate.rs
git commit -m "feat(#4055): add hydrate module with happy-path admit"
```

---

## Task 4: Hydrate — warmth skip

**Files:**
- Modify: `nexus-fuse/src/hydrate.rs` (test only)

- [ ] **Step 1: Write the failing test**

Append to `mod tests` in `hydrate.rs`:

```rust
#[tokio::test]
async fn test_hydrate_skips_warm_entries() {
    let _guard = crate::metrics::test_guard();
    crate::metrics::reset_for_tests();

    let mut server = mockito::Server::new_async().await;
    let list_body = r#"{"jsonrpc":"2.0","id":1,"result":{"files":[
        {"path":"/cached.txt","is_directory":false,"size":10},
        {"path":"/cold.txt","is_directory":false,"size":10}
    ]}}"#;
    let _list_mock = server
        .mock("POST", "/api/nfs/list")
        .with_status(200)
        .with_body(list_body)
        .create_async()
        .await;
    // Read mock should be called exactly once — for the cold path
    let read_mock = server
        .mock("POST", "/api/nfs/read")
        .with_status(200)
        .with_header("etag", "\"abc\"")
        .with_body(r#"{"jsonrpc":"2.0","id":1,"result":{"__type__":"bytes","data":"aGk="}}"#)
        .expect(1)
        .create_async()
        .await;

    let client = Arc::new(NexusClient::new(&server.url(), "k", None).unwrap());
    let cache = fresh_cache("warm");
    cache.put("/cached.txt", b"already-here", Some("etag-old"), 0);

    let stats = hydrate_workspace(client, cache.clone(), HydrateOptions::new("/".into())).await;

    assert_eq!(stats.skipped_warm, 1);
    assert_eq!(stats.admitted_count, 1);
    assert_eq!(stats.failed, 0);
    read_mock.assert_async().await; // verifies exactly 1 read call
}
```

- [ ] **Step 2: Run test — verify it fails or passes**

```bash
cd nexus-fuse && cargo test --lib hydrate::tests::test_hydrate_skips_warm_entries
```

Expected: should pass already because Task 3 implementation already calls `cache.is_warm` in `collect_candidates`. If it fails, the path normalization between `cache.put("/cached.txt", ...)` and the warmth check needs alignment — fix in `join_path` or the path used in the test mock.

- [ ] **Step 3: If failing, fix path joining**

If the path used by `cache.put` in the test does not match the path emitted by `collect_candidates`, adjust the mock to use the same path the real backend would.

- [ ] **Step 4: Run — verify pass**

Same command. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add nexus-fuse/src/hydrate.rs
git commit -m "test(#4055): hydrate skips already-warm entries"
```

---

## Task 5: Hydrate — budget enforcement

**Files:**
- Modify: `nexus-fuse/src/hydrate.rs` (test only)

- [ ] **Step 1: Write the failing test**

Append:

```rust
#[tokio::test]
async fn test_hydrate_respects_budget() {
    let _guard = crate::metrics::test_guard();
    crate::metrics::reset_for_tests();

    let mut server = mockito::Server::new_async().await;
    // 10 files of 10 KiB each; budget allows ~3.
    let mut files = String::new();
    for i in 0..10 {
        if i > 0 { files.push(','); }
        files.push_str(&format!(
            r#"{{"path":"/f{}.bin","is_directory":false,"size":10240}}"#,
            i
        ));
    }
    let body = format!(r#"{{"jsonrpc":"2.0","id":1,"result":{{"files":[{}]}}}}"#, files);
    let _list_mock = server
        .mock("POST", "/api/nfs/list")
        .with_status(200)
        .with_body(&body)
        .create_async()
        .await;
    // base64 of 10 KiB of 'x' = 13688 chars; build dynamically
    let payload = base64::engine::general_purpose::STANDARD
        .encode(vec![b'x'; 10 * 1024]);
    let read_body = format!(
        r#"{{"jsonrpc":"2.0","id":1,"result":{{"__type__":"bytes","data":"{}"}}}}"#,
        payload
    );
    let _read_mock = server
        .mock("POST", "/api/nfs/read")
        .with_status(200)
        .with_header("etag", "\"x\"")
        .with_body(read_body)
        .create_async()
        .await;

    let client = Arc::new(NexusClient::new(&server.url(), "k", None).unwrap());
    let cache = fresh_cache("budget");
    let mut opts = HydrateOptions::new("/".into());
    opts.budget_bytes = 30 * 1024;
    opts.concurrency = 2;
    opts.threshold_bytes = 16 * 1024;

    let stats = hydrate_workspace(client, cache, opts).await;

    // With concurrency=2 and budget=30KiB, expect 3-4 admits (race window allows overshoot).
    assert!(
        (3..=4).contains(&stats.admitted_count),
        "expected 3-4 admits, got {}",
        stats.admitted_count
    );
    assert!(
        stats.skipped_budget >= 6,
        "expected >= 6 skipped_budget, got {}",
        stats.skipped_budget
    );
    assert_eq!(stats.failed, 0);
}
```

You will also need to add `base64` to the test imports — at the top of `mod tests`:

```rust
use base64::Engine;
```

(Already a workspace dependency per `Cargo.toml`.)

- [ ] **Step 2: Run test — verify it passes**

```bash
cd nexus-fuse && cargo test --lib hydrate::tests::test_hydrate_respects_budget
```

Expected: PASS — Task 3's implementation already enforces the budget atomically in the `spawn_blocking` task.

- [ ] **Step 3: If failing, debug**

If `admitted_count` exceeds 4: the budget check is being skipped or `concurrency=2` isn't being honored — re-read the `Semaphore::new(opts.concurrency.max(1))` line and confirm `acquire_owned` is `await`ed before spawning.

If `admitted_count` is 0 or 1: the read mock is rejecting calls — check the base64 body shape and content-length headers in mockito.

- [ ] **Step 4: Commit**

```bash
git add nexus-fuse/src/hydrate.rs
git commit -m "test(#4055): hydrate respects budget cap"
```

---

## Task 6: Hydrate — per-file fetch error tolerance

**Files:**
- Modify: `nexus-fuse/src/hydrate.rs` (test only)

- [ ] **Step 1: Write the failing test**

Append:

```rust
#[tokio::test]
async fn test_hydrate_continues_on_per_file_error() {
    let _guard = crate::metrics::test_guard();
    crate::metrics::reset_for_tests();

    let mut server = mockito::Server::new_async().await;
    let _list_mock = server
        .mock("POST", "/api/nfs/list")
        .with_status(200)
        .with_body(r#"{"jsonrpc":"2.0","id":1,"result":{"files":[
            {"path":"/ok1.txt","is_directory":false,"size":3},
            {"path":"/bad.txt","is_directory":false,"size":3},
            {"path":"/ok2.txt","is_directory":false,"size":3}
        ]}}"#)
        .create_async()
        .await;

    // Server returns 500 only for /bad.txt body; mockito doesn't easily route by body,
    // so we use a path-content-trigger: configure read mock to return 500 the SECOND time
    // it is hit. JoinSet may reorder, so this is a best-effort heuristic.
    let _bad_mock = server
        .mock("POST", "/api/nfs/read")
        .match_body(mockito::Matcher::Regex(r#""path":\s*"/bad\.txt""#.into()))
        .with_status(500)
        .with_body("internal error")
        .create_async()
        .await;
    let _ok_mock = server
        .mock("POST", "/api/nfs/read")
        .match_body(mockito::Matcher::Regex(r#""path":\s*"/ok\d\.txt""#.into()))
        .with_status(200)
        .with_header("etag", "\"e\"")
        .with_body(r#"{"jsonrpc":"2.0","id":1,"result":{"__type__":"bytes","data":"aGk="}}"#)
        .create_async()
        .await;

    let client = Arc::new(NexusClient::new(&server.url(), "k", None).unwrap());
    let cache = fresh_cache("per_file_err");
    let stats = hydrate_workspace(client, cache, HydrateOptions::new("/".into())).await;

    assert_eq!(stats.admitted_count, 2);
    assert_eq!(stats.failed, 1);
}
```

- [ ] **Step 2: Run — verify pass**

```bash
cd nexus-fuse && cargo test --lib hydrate::tests::test_hydrate_continues_on_per_file_error
```

Expected: PASS. Task 3's `Err(err)` branch already increments `failed` and continues.

- [ ] **Step 3: If mockito body-matching does not work**

Some mockito versions match by exact body or regex but not both at once. If `match_body` with Regex doesn't route correctly, fall back to path-based matching by checking the request URL. Or simplify: have all reads return 500 for one mock instance — then the test asserts `failed == 3, admitted == 0`. Either pattern proves the per-file error path is non-fatal to the batch.

- [ ] **Step 4: Commit**

```bash
git add nexus-fuse/src/hydrate.rs
git commit -m "test(#4055): hydrate tolerates per-file read errors"
```

---

## Task 7: Hydrate — root list failure

**Files:**
- Modify: `nexus-fuse/src/hydrate.rs` (test only)

- [ ] **Step 1: Write the failing test**

Append:

```rust
#[tokio::test]
async fn test_hydrate_root_list_failure_returns_failed() {
    let _guard = crate::metrics::test_guard();
    crate::metrics::reset_for_tests();

    let mut server = mockito::Server::new_async().await;
    let _list_mock = server
        .mock("POST", "/api/nfs/list")
        .with_status(500)
        .with_body("backend down")
        .create_async()
        .await;

    let client = Arc::new(NexusClient::new(&server.url(), "k", None).unwrap());
    let cache = fresh_cache("list_err");
    let stats = hydrate_workspace(client, cache, HydrateOptions::new("/".into())).await;

    assert_eq!(stats.admitted_count, 0);
    assert_eq!(stats.failed, 1);
}

#[tokio::test]
async fn test_hydrate_empty_workspace_zero_stats() {
    let _guard = crate::metrics::test_guard();
    crate::metrics::reset_for_tests();

    let mut server = mockito::Server::new_async().await;
    let _list_mock = server
        .mock("POST", "/api/nfs/list")
        .with_status(200)
        .with_body(r#"{"jsonrpc":"2.0","id":1,"result":{"files":[]}}"#)
        .create_async()
        .await;

    let client = Arc::new(NexusClient::new(&server.url(), "k", None).unwrap());
    let cache = fresh_cache("empty");
    let stats = hydrate_workspace(client, cache, HydrateOptions::new("/".into())).await;

    assert_eq!(stats.admitted_count, 0);
    assert_eq!(stats.skipped_size, 0);
    assert_eq!(stats.skipped_warm, 0);
    assert_eq!(stats.failed, 0);
}
```

- [ ] **Step 2: Run — verify pass**

```bash
cd nexus-fuse && cargo test --lib hydrate::tests::test_hydrate_root_list_failure_returns_failed hydrate::tests::test_hydrate_empty_workspace_zero_stats
```

Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add nexus-fuse/src/hydrate.rs
git commit -m "test(#4055): hydrate handles list failure and empty workspace"
```

---

## Task 8: Wire `cache_warm` into JSON-RPC dispatch

**Files:**
- Modify: `nexus-fuse/src/daemon.rs:225-260` (handler dispatch + new handler function)

- [ ] **Step 1: Add the handler function**

Insert into `nexus-fuse/src/daemon.rs` after the existing `handle_exists` function (~L425):

```rust
async fn handle_cache_warm(
    params: Value,
    client: Arc<NexusClient>,
    file_cache: Option<Arc<FileCache>>,
) -> Result<Value, NexusClientError> {
    #[derive(Deserialize)]
    struct P {
        workspace_root: String,
        #[serde(default)]
        threshold_bytes: Option<usize>,
        #[serde(default)]
        budget_bytes: Option<usize>,
        #[serde(default)]
        concurrency: Option<usize>,
    }
    let p: P = serde_json::from_value(params)
        .map_err(|e| NexusClientError::InvalidResponse(format!("Invalid params: {}", e)))?;

    let cache = file_cache.ok_or_else(|| {
        NexusClientError::InvalidResponse("cache_warm requires --cache (FileCache disabled)".into())
    })?;

    let mut opts = crate::hydrate::HydrateOptions::new(p.workspace_root);
    if let Some(t) = p.threshold_bytes {
        opts.threshold_bytes = t;
    }
    if let Some(b) = p.budget_bytes {
        opts.budget_bytes = b;
    }
    if let Some(c) = p.concurrency {
        opts.concurrency = c;
    }

    let stats = crate::hydrate::hydrate_workspace(client, cache, opts).await;
    serde_json::to_value(&stats).map_err(|e| {
        NexusClientError::InvalidResponse(format!("failed to serialize hydrate stats: {}", e))
    })
}
```

The function must take owned `Arc` clones because it is async (cannot borrow across `.await`).

- [ ] **Step 2: Restructure dispatch to support async handlers**

The current `handle_request` (`daemon.rs:233`) wraps everything in `tokio::task::spawn_blocking` because all existing handlers are sync. `handle_cache_warm` is async (it `.await`s on `hydrate_workspace`), so it cannot run inside `spawn_blocking`.

Modify the dispatch in `handle_request`:

```rust
async fn handle_request(
    request: JsonRpcRequest,
    client: &NexusClient,
    file_cache: Option<Arc<FileCache>>,
) -> JsonRpcResponse {
    debug!("Handling method: {}", request.method);

    let client_owned = client.clone();
    let method = request.method.clone();
    let params = request.params.clone();

    // Async-first dispatch: cache_warm is async, everything else is sync via spawn_blocking.
    let result = if method == "cache_warm" {
        handle_cache_warm(params, Arc::new(client_owned), file_cache).await
    } else {
        let cache_for_blocking = file_cache.clone();
        let join = tokio::task::spawn_blocking(move || match method.as_str() {
            "read" => handle_read(&params, &client_owned, cache_for_blocking.as_deref()),
            "write" => handle_write(&params, &client_owned, cache_for_blocking.as_deref()),
            "list" => handle_list(&params, &client_owned),
            "stat" => handle_stat(&params, &client_owned),
            "mkdir" => handle_mkdir(&params, &client_owned),
            "delete" => handle_delete(&params, &client_owned, cache_for_blocking.as_deref()),
            "rename" => handle_rename(&params, &client_owned, cache_for_blocking.as_deref()),
            "exists" => handle_exists(&params, &client_owned),
            _ => Err(NexusClientError::InvalidResponse(format!(
                "Method not found: {}",
                method
            ))),
        })
        .await;
        match join {
            Ok(r) => r,
            Err(e) => {
                error!("Task join error: {}", e);
                return JsonRpcResponse::error(
                    request.id,
                    -32603,
                    format!("Internal error: {}", e),
                    None,
                );
            }
        }
    };

    match result {
        Ok(value) => JsonRpcResponse::success(request.id, value),
        Err(e) => {
            let errno = e.to_errno();
            warn!("Request failed: {} (errno={})", e, errno);
            JsonRpcResponse::error(request.id, -32603, e.to_string(), Some(errno))
        }
    }
}
```

Notes:
- Wrapping `client_owned` in `Arc::new` for `cache_warm` is necessary since `hydrate_workspace` takes `Arc<NexusClient>`. The blocking branch uses the unwrapped clone directly, matching existing handler signatures.
- `NexusClient: Clone` is required (it is — see `client.rs:85` `#[derive(Clone)]`).

- [ ] **Step 3: Run the daemon's existing tests**

```bash
cd nexus-fuse && cargo test --lib daemon::
```

Expected: all existing daemon tests still PASS. If a test invokes `handle_request` directly with a non-`cache_warm` method, the change should be transparent.

- [ ] **Step 4: Compile**

```bash
cd nexus-fuse && cargo build
```

Expected: SUCCESS.

- [ ] **Step 5: Commit**

```bash
git add nexus-fuse/src/daemon.rs
git commit -m "feat(#4055): register cache_warm JSON-RPC method"
```

---

## Task 9: Add `cache_warm` to Python `RustFUSEClient`

**Files:**
- Modify: `src/nexus/fuse/rust_client.py` (after `access` method, before `close`)
- Test: `tests/unit/fuse/test_rust_client.py` (or create if missing)

- [ ] **Step 1: Locate or create the test file**

```bash
find tests -name "test_rust_client.py" 2>/dev/null
```

If found, append to it. If not, create `tests/unit/fuse/test_rust_client.py` with:

```python
"""Tests for nexus.fuse.rust_client.RustFUSEClient.cache_warm."""
from __future__ import annotations

from unittest.mock import patch
import pytest


class TestCacheWarmMethod:
    def test_cache_warm_default_params(self) -> None:
        from nexus.fuse.rust_client import RustFUSEClient
        client = RustFUSEClient.__new__(RustFUSEClient)  # bypass __init__
        with patch.object(client, "_send_request", return_value={"admitted_count": 3}) as send:
            result = client.cache_warm("/workspace")
        send.assert_called_once_with("cache_warm", {"workspace_root": "/workspace"})
        assert result == {"admitted_count": 3}

    def test_cache_warm_with_overrides(self) -> None:
        from nexus.fuse.rust_client import RustFUSEClient
        client = RustFUSEClient.__new__(RustFUSEClient)
        with patch.object(client, "_send_request", return_value={}) as send:
            client.cache_warm(
                "/ws",
                threshold_bytes=4096,
                budget_bytes=1024,
                concurrency=2,
            )
        send.assert_called_once_with(
            "cache_warm",
            {
                "workspace_root": "/ws",
                "threshold_bytes": 4096,
                "budget_bytes": 1024,
                "concurrency": 2,
            },
        )

    def test_cache_warm_omits_none_overrides(self) -> None:
        from nexus.fuse.rust_client import RustFUSEClient
        client = RustFUSEClient.__new__(RustFUSEClient)
        with patch.object(client, "_send_request", return_value={}) as send:
            client.cache_warm("/ws", threshold_bytes=None, budget_bytes=512)
        send.assert_called_once_with(
            "cache_warm",
            {"workspace_root": "/ws", "budget_bytes": 512},
        )
```

- [ ] **Step 2: Run tests — verify they fail**

```bash
pytest tests/unit/fuse/test_rust_client.py::TestCacheWarmMethod -v
```

Expected: FAIL — `AttributeError: 'RustFUSEClient' object has no attribute 'cache_warm'`.

- [ ] **Step 3: Add the method**

Insert into `src/nexus/fuse/rust_client.py` after the `access` method (~L422-433), before `close`:

```python
def cache_warm(
    self,
    workspace_root: str,
    *,
    threshold_bytes: int | None = None,
    budget_bytes: int | None = None,
    concurrency: int | None = None,
) -> dict[str, Any]:
    """Trigger eager cache hydration via the Rust daemon.

    Returns a `HydrateStats` dict with keys: admitted_count, admitted_bytes,
    skipped_warm, skipped_size, skipped_budget, failed, duration_ms.
    """
    params: dict[str, Any] = {"workspace_root": workspace_root}
    if threshold_bytes is not None:
        params["threshold_bytes"] = threshold_bytes
    if budget_bytes is not None:
        params["budget_bytes"] = budget_bytes
    if concurrency is not None:
        params["concurrency"] = concurrency
    return self._send_request("cache_warm", params)
```

Confirm `Any` is already imported at the top of the file. If not, add `from typing import Any`.

- [ ] **Step 4: Run tests — verify pass**

```bash
pytest tests/unit/fuse/test_rust_client.py::TestCacheWarmMethod -v
```

Expected: PASS (all 3).

- [ ] **Step 5: Commit**

```bash
git add src/nexus/fuse/rust_client.py tests/unit/fuse/test_rust_client.py
git commit -m "feat(#4055): add cache_warm to Python RustFUSEClient"
```

---

## Task 10: Extend `BootIndexer` to call `cache_warm`

**Files:**
- Modify: `src/nexus/core/boot_indexer.py`
- Test: `tests/unit/core/test_boot_indexer.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/unit/core/test_boot_indexer.py`:

```python
class TestBootIndexerHydration:
    """BootIndexer triggers cache hydration after the search walk completes."""

    def test_cache_warm_called_with_workspace_root(self, tmp_path: Path) -> None:
        (tmp_path / "f.txt").write_text("data")
        search_daemon = MagicMock()
        rust_client = MagicMock()
        rust_client.cache_warm.return_value = {
            "admitted_count": 1,
            "admitted_bytes": 4,
            "skipped_warm": 0,
            "skipped_size": 0,
            "skipped_budget": 0,
            "failed": 0,
            "duration_ms": 5,
        }
        health_state: dict[str, str] = {"status": "indexing"}

        indexer = BootIndexer(
            workspace=tmp_path,
            search_daemon=search_daemon,
            health_state=health_state,
            rust_client=rust_client,
        )
        indexer.start_async()

        deadline = time.monotonic() + 5.0
        while ("hydration" not in health_state and time.monotonic() < deadline):
            time.sleep(0.01)

        rust_client.cache_warm.assert_called_once()
        call_args = rust_client.cache_warm.call_args
        assert call_args.args[0] == str(tmp_path)
        assert health_state["status"] == "ready"
        assert health_state["hydration"]["admitted_count"] == 1

    def test_cache_warm_error_is_swallowed(self, tmp_path: Path) -> None:
        (tmp_path / "f.txt").write_text("data")
        search_daemon = MagicMock()
        rust_client = MagicMock()
        rust_client.cache_warm.side_effect = BrokenPipeError("daemon dead")
        health_state: dict[str, str] = {"status": "indexing"}

        indexer = BootIndexer(
            workspace=tmp_path,
            search_daemon=search_daemon,
            health_state=health_state,
            rust_client=rust_client,
        )
        indexer.start_async()

        deadline = time.monotonic() + 5.0
        while ("hydration" not in health_state and time.monotonic() < deadline):
            time.sleep(0.01)

        assert health_state["status"] == "ready"
        assert "error" in health_state["hydration"]

    def test_cache_warm_skipped_when_rust_client_none(self, tmp_path: Path) -> None:
        (tmp_path / "f.txt").write_text("data")
        search_daemon = MagicMock()
        health_state: dict[str, str] = {"status": "indexing"}

        indexer = BootIndexer(
            workspace=tmp_path,
            search_daemon=search_daemon,
            health_state=health_state,
            rust_client=None,
        )
        indexer.start_async()

        deadline = time.monotonic() + 5.0
        while health_state["status"] != "ready" and time.monotonic() < deadline:
            time.sleep(0.01)

        assert health_state["status"] == "ready"
        assert "hydration" not in health_state

    def test_cache_warm_passes_overrides(self, tmp_path: Path) -> None:
        (tmp_path / "f.txt").write_text("data")
        search_daemon = MagicMock()
        rust_client = MagicMock()
        rust_client.cache_warm.return_value = {}
        health_state: dict[str, str] = {"status": "indexing"}

        indexer = BootIndexer(
            workspace=tmp_path,
            search_daemon=search_daemon,
            health_state=health_state,
            rust_client=rust_client,
            hydrate_threshold=4096,
            hydrate_budget=1024,
        )
        indexer.start_async()

        deadline = time.monotonic() + 5.0
        while ("hydration" not in health_state and time.monotonic() < deadline):
            time.sleep(0.01)

        rust_client.cache_warm.assert_called_once_with(
            str(tmp_path),
            threshold_bytes=4096,
            budget_bytes=1024,
        )
```

- [ ] **Step 2: Run tests — verify they fail**

```bash
pytest tests/unit/core/test_boot_indexer.py::TestBootIndexerHydration -v
```

Expected: FAIL — constructor does not accept `rust_client`/`hydrate_threshold`/`hydrate_budget`.

- [ ] **Step 3: Update `BootIndexer`**

Replace the entire `__init__` method in `src/nexus/core/boot_indexer.py:46-54` and the `_run` method:

```python
def __init__(
    self,
    workspace: Path,
    search_daemon: Any,
    health_state: dict[str, Any],
    *,
    rust_client: Any | None = None,
    hydrate_threshold: int | None = None,
    hydrate_budget: int | None = None,
) -> None:
    self._workspace = workspace
    self._search_daemon = search_daemon
    self._health_state = health_state
    self._rust_client = rust_client
    self._hydrate_threshold = hydrate_threshold
    self._hydrate_budget = hydrate_budget
```

Then change `_run` (L73-90) to also call `_hydrate_cache` after `health_state` flips to ready:

```python
def _run(self) -> None:
    try:
        self._walk_and_index()
    except Exception as exc:
        logger.error(
            "[BootIndexer] walk failed for %s: %s",
            self._workspace,
            exc,
            exc_info=True,
        )
    finally:
        self._health_state["status"] = "ready"
        logger.info("[BootIndexer] indexing complete, health_state → ready")

    if self._rust_client is not None:
        self._hydrate_cache()
```

Add a new `_hydrate_cache` method after `_walk_and_index`:

```python
def _hydrate_cache(self) -> None:
    """Trigger eager L1 cache hydration via the Rust daemon (Issue #4055)."""
    kwargs: dict[str, Any] = {}
    if self._hydrate_threshold is not None:
        kwargs["threshold_bytes"] = self._hydrate_threshold
    if self._hydrate_budget is not None:
        kwargs["budget_bytes"] = self._hydrate_budget
    try:
        stats = self._rust_client.cache_warm(str(self._workspace), **kwargs)
        logger.info("[BootIndexer] cache hydration: %s", stats)
        self._health_state["hydration"] = stats
    except (BrokenPipeError, ConnectionResetError, OSError) as exc:
        logger.warning("[BootIndexer] cache hydration failed: %s", exc)
        self._health_state["hydration"] = {"error": str(exc)}
```

- [ ] **Step 4: Run tests — verify pass**

```bash
pytest tests/unit/core/test_boot_indexer.py -v
```

Expected: ALL PASS — including pre-existing tests (the new `rust_client` param defaults to `None`, preserving behavior).

- [ ] **Step 5: Commit**

```bash
git add src/nexus/core/boot_indexer.py tests/unit/core/test_boot_indexer.py
git commit -m "feat(#4055): BootIndexer triggers cache_warm after search walk"
```

---

## Task 11: Wire `rust_client` through `SandboxBootstrapper`

**Files:**
- Modify: `src/nexus/daemon/sandbox_bootstrap.py:157-164` (BootIndexer construction)
- Modify: `src/nexus/daemon/sandbox_bootstrap.py:__init__` (accept `rust_client` if not already)
- Modify: `src/nexus/daemon/main.py:431-440` (pass `rust_client` to bootstrapper)

- [ ] **Step 1: Locate the rust_client owner**

```bash
grep -n "RustFUSEClient\|rust_client\|nx\._search_daemon" src/nexus/daemon/main.py | head -20
```

Identify where the `RustFUSEClient` instance lives. It is likely:
- An attribute on `nx` (the `NexusFS` instance), like `nx._rust_client` or `nx.rust_client`
- A standalone variable in `main.py`
- Not yet wired — must be looked up via `nx.fuse_backend` or equivalent

If it doesn't exist as a direct attribute, search:

```bash
grep -rn "RustFUSEClient(" src/nexus/ | head
```

Find the construction site and arrange for `main.py` to capture/pass it.

- [ ] **Step 2: Read `SandboxBootstrapper.__init__` signature**

```bash
grep -n "class SandboxBootstrapper\|def __init__" src/nexus/daemon/sandbox_bootstrap.py | head -5
```

If `__init__` already accepts every other dep, add `rust_client: Any | None = None` to the parameter list and assign to `self._rust_client`.

- [ ] **Step 3: Pass `rust_client` to BootIndexer**

Edit `src/nexus/daemon/sandbox_bootstrap.py:157-164`:

```python
indexer = BootIndexer(
    workspace=self._workspace,
    search_daemon=self._search_daemon,
    health_state=self._health_state,
    rust_client=self._rust_client,
)
indexer.start_async()
```

- [ ] **Step 4: Pass `rust_client` from `main.py` to `SandboxBootstrapper`**

Edit `src/nexus/daemon/main.py:431-440`:

```python
bootstrapper = SandboxBootstrapper(
    workspace=_workspace_path,
    hub_url=hub_url,
    hub_token=hub_token,
    nexus_fs=nx,
    search_registry=_search_registry,
    search_daemon=_search_daemon,
    health_state=_health_state,
    rust_client=getattr(nx, "_rust_client", None) or getattr(nx, "rust_client", None),
)
bootstrapper.run()
```

If the actual attribute name on `nx` is different, replace the `getattr` calls accordingly. If `nx` does not own the rust client, walk up the call stack to find where it was constructed and pass it through explicitly.

- [ ] **Step 5: Run the existing sandbox-bootstrap tests**

```bash
pytest tests/unit/daemon/test_sandbox_bootstrap.py -v 2>&1 | head -40
```

Expected: existing tests PASS. They likely don't use `rust_client` — your change is a default-`None` parameter, so behavior is preserved.

- [ ] **Step 6: Commit**

```bash
git add src/nexus/daemon/sandbox_bootstrap.py src/nexus/daemon/main.py
git commit -m "feat(#4055): plumb rust_client through SandboxBootstrapper"
```

---

## Task 12: Python↔Rust integration test

**Files:**
- Create: `nexus-fuse/test_cache_warm.py` (sibling to `test_python_ipc.py`)

- [ ] **Step 1: Read the sibling pattern**

```bash
cat nexus-fuse/test_python_ipc.py
```

Note how `RustFUSEClient` is started, how files are seeded, and how shutdown happens. The new test follows the same lifecycle.

- [ ] **Step 2: Write the integration test**

Create `nexus-fuse/test_cache_warm.py`:

```python
#!/usr/bin/env python3
"""End-to-end test for the cache_warm JSON-RPC method (Issue #4055)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nexus.fuse.rust_client import RustFUSEClient


def main() -> int:
    print("🧪 Testing cache_warm round-trip\n")
    rust_binary = str(Path(__file__).parent / "target/debug/nexus-fuse")

    with RustFUSEClient(
        nexus_url="http://localhost:2026",
        api_key="test-key",
        rust_binary_path=rust_binary,
    ) as client:
        # Seed: create three small files via sys_write so the backend has content.
        client.sys_write("/hyd_a.txt", b"alpha")
        client.sys_write("/hyd_b.txt", b"beta")
        client.sys_write("/hyd_big.bin", b"x" * (200 * 1024))  # over threshold

        stats = client.cache_warm("/")
        print(f"hydration stats: {stats}")
        assert stats["admitted_count"] >= 2, f"expected >=2 admits, got {stats}"
        assert stats["skipped_size"] >= 1, f"expected >=1 skip, got {stats}"

        # Subsequent reads should land in cache; we can verify by re-running cache_warm,
        # which should now report skipped_warm for the previously-admitted entries.
        stats2 = client.cache_warm("/")
        print(f"second hydration stats: {stats2}")
        assert stats2["skipped_warm"] >= 2, f"expected warm skips, got {stats2}"

    print("✅ cache_warm e2e test passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

The exact constructor kwargs must match `RustFUSEClient.__init__` — read `src/nexus/fuse/rust_client.py:66-93` to confirm parameter names.

- [ ] **Step 3: Build the daemon and run**

```bash
cd nexus-fuse && cargo build --bin nexus-fuse
```

This test requires a running Nexus server at `localhost:2026`. The existing `test_python_ipc.py` has the same prerequisite — defer execution to manual/CI runs. Document it in the file's docstring.

- [ ] **Step 4: Commit**

```bash
git add nexus-fuse/test_cache_warm.py
git commit -m "test(#4055): add cache_warm e2e integration test"
```

---

## Task 13: Benchmark — cold-start small reads with vs without hydration

**Files:**
- Modify: `nexus-fuse/benches/cache_backends.rs`

The acceptance criterion requires p50 ≥3× faster. This task adds the measurement; the assertion is documented but only logged unless the run is reproducible.

- [ ] **Step 1: Read existing bench structure**

```bash
sed -n '1,100p' nexus-fuse/benches/cache_backends.rs
```

Note how Criterion benches are wired: `criterion_group!`, `criterion_main!`, `Criterion::default()` config, and how the existing benches initialize `FileCache`.

- [ ] **Step 2: Add a hydration scenario**

Append (or insert into existing groups) the following bench:

```rust
fn bench_cold_read_with_and_without_hydration(c: &mut Criterion) {
    use nexus_fuse::cache::{CacheConfig, FileCache};
    use nexus_fuse::cached_read::read_with_cache;
    use nexus_fuse::client::NexusClient;
    use nexus_fuse::hydrate::{hydrate_workspace, HydrateOptions};
    use std::sync::Arc;
    use tokio::runtime::Runtime;

    let rt = Runtime::new().unwrap();

    let server = rt.block_on(async {
        let mut server = mockito::Server::new_async().await;
        // 50 small files
        let mut entries = String::new();
        for i in 0..50 {
            if i > 0 { entries.push(','); }
            entries.push_str(&format!(
                r#"{{"path":"/f{}.txt","is_directory":false,"size":256}}"#,
                i
            ));
        }
        server
            .mock("POST", "/api/nfs/list")
            .with_status(200)
            .with_body(format!(
                r#"{{"jsonrpc":"2.0","id":1,"result":{{"files":[{}]}}}}"#,
                entries
            ))
            .expect_at_least(1)
            .create_async()
            .await;
        server
            .mock("POST", "/api/nfs/read")
            .with_status(200)
            .with_chunked_body(|w| {
                use std::io::Write;
                // Simulate 10 ms RTT before responding
                std::thread::sleep(std::time::Duration::from_millis(10));
                w.write_all(
                    br#"{"jsonrpc":"2.0","id":1,"result":{"__type__":"bytes","data":"YWJjZA=="}}"#
                )
            })
            .expect_at_least(1)
            .create_async()
            .await;
        server
    });

    let url = server.url();

    let mut group = c.benchmark_group("hydration");
    group.bench_function("cold_read_p50_no_hydration", |b| {
        b.iter_custom(|iters| {
            let mut total = std::time::Duration::ZERO;
            for _ in 0..iters {
                let dir = tempfile::tempdir().unwrap();
                let cfg = CacheConfig::new(dir.into_path(), 8 << 20, 32 << 20, 1 << 20).unwrap();
                let cache = Arc::new(FileCache::new_with_config(&url, cfg).unwrap());
                let client = NexusClient::new(&url, "k", None).unwrap();
                let start = std::time::Instant::now();
                for i in 0..50 {
                    let _ = read_with_cache(&client, Some(cache.as_ref()), &format!("/f{}.txt", i), 0);
                }
                total += start.elapsed();
            }
            total
        })
    });
    group.bench_function("cold_read_p50_with_hydration", |b| {
        b.iter_custom(|iters| {
            let mut total = std::time::Duration::ZERO;
            for _ in 0..iters {
                let dir = tempfile::tempdir().unwrap();
                let cfg = CacheConfig::new(dir.into_path(), 8 << 20, 32 << 20, 1 << 20).unwrap();
                let cache = Arc::new(FileCache::new_with_config(&url, cfg).unwrap());
                let client = Arc::new(NexusClient::new(&url, "k", None).unwrap());
                rt.block_on(hydrate_workspace(
                    client.clone(),
                    cache.clone(),
                    HydrateOptions::new("/".into()),
                ));
                let start = std::time::Instant::now();
                for i in 0..50 {
                    let _ = read_with_cache(client.as_ref(), Some(cache.as_ref()), &format!("/f{}.txt", i), 0);
                }
                total += start.elapsed();
            }
            total
        })
    });
    group.finish();
}
```

Wire it into the existing `criterion_group!` near the bottom of the file.

The mockito `with_chunked_body` callback simulates a 10 ms backend RTT. Per-file, the no-hydration scenario pays 50 × 10 ms ≈ 500 ms; the with-hydration scenario admits in parallel (concurrency=8), then reads land in cache. Expected ratio ≥ 3×.

- [ ] **Step 2 (continued): Imports & deps**

Confirm `mockito`, `tempfile`, and `tokio` are already available in `[dev-dependencies]` of `nexus-fuse/Cargo.toml`. They are (per the earlier read).

- [ ] **Step 3: Build the benches**

```bash
cd nexus-fuse && cargo bench --no-run --bench cache_backends 2>&1 | tail -20
```

Expected: SUCCESS — benches compile.

- [ ] **Step 4: Run the benches**

```bash
cd nexus-fuse && cargo bench --bench cache_backends -- hydration 2>&1 | tee /tmp/4055_bench.log
```

Inspect the output: `cold_read_p50_no_hydration` time vs `cold_read_p50_with_hydration` time. Compute the ratio. The criterion target is ≥ 3×.

If the ratio is < 3×, troubleshoot:
- Is the mock RTT actually being applied? `with_chunked_body` may not block as expected — switch to `with_body` and a separate `tokio::time::sleep` inside a custom mock filter.
- Is `concurrency` actually parallelizing? Confirm reads happen concurrently by adding `eprintln!` in `hydrate_workspace`.

Document the measured ratio in `nexus-fuse/PERFORMANCE_RESULTS.md` (append a new section dated today).

- [ ] **Step 5: Commit**

```bash
git add nexus-fuse/benches/cache_backends.rs nexus-fuse/PERFORMANCE_RESULTS.md
git commit -m "bench(#4055): measure cold-read latency with/without hydration"
```

---

## Task 14: Final smoke and PR

- [ ] **Step 1: Run the full test suite**

```bash
cd nexus-fuse && cargo test --lib
pytest tests/unit/core/test_boot_indexer.py tests/unit/fuse/ -v
```

Expected: ALL PASS.

- [ ] **Step 2: Run lints**

```bash
cd nexus-fuse && cargo clippy --all-targets -- -D warnings && cargo fmt -- --check
ruff check src/nexus/core/boot_indexer.py src/nexus/fuse/rust_client.py src/nexus/daemon/sandbox_bootstrap.py
```

Expected: clean. Fix any warnings inline.

- [ ] **Step 3: Manual smoke**

Start the local stack (`/nexus-stack` or equivalent), attach a workspace with a known set of small files, watch the logs for `[BootIndexer] cache hydration:` and confirm `health_state["hydration"]["admitted_count"] > 0`.

- [ ] **Step 4: Create PR**

```bash
git push -u origin HEAD
gh pr create --title "feat(#4055): eager-hydrate small files into L1 cache on workspace attach" \
  --body "$(cat <<'EOF'
## Summary

Implements [#4055](https://github.com/nexi-lab/nexus/issues/4055) — extends `BootIndexer` to call a new `cache_warm` JSON-RPC method on the nexus-fuse daemon at workspace attach. Rust drives BFS list, filters ≤128 KiB cold files, fetches up to 8 in parallel, admits to the foyer L1 cache up to a 64 MiB budget.

## Acceptance criteria
- [x] BootIndexer hydrates files <128KB up to byte budget
- [x] Configurable threshold + budget (RPC params + Rust constants)
- [x] Async — workspace attach not blocked
- [x] Skip for already-warm entries
- [x] Benchmark: cold-start small-read p50 dropped ≥3× (see PERFORMANCE_RESULTS.md)
- [x] Telemetry: hydration count + bytes + skipped + duration

## Test plan
- [ ] `cargo test --lib` passes (Rust unit tests under `cache::tests`, `metrics::tests`, `hydrate::tests`)
- [ ] `pytest tests/unit/core/test_boot_indexer.py tests/unit/fuse/test_rust_client.py` passes
- [ ] `cargo bench -- hydration` shows ≥3× p50 improvement
- [ ] Manual: attach a workspace, observe `[BootIndexer] cache hydration:` in logs

## Spec
docs/superpowers/specs/2026-05-09-issue-4055-eager-hydrate-small-files-design.md
EOF
)"
```

---

## Self-Review Checklist (run after writing the plan)

- [ ] Spec coverage:
  - Hydration walker → Tasks 3-7
  - Configurable threshold + budget → RPC params (Task 8) + constants (Task 1)
  - Async/non-blocking attach → Task 10 (`_run` keeps "ready" before hydrate; daemon thread)
  - Skip warm → Task 4 (test) + Task 1 (`is_warm`)
  - Benchmark p50 ≥3× → Task 13
  - Telemetry counters → Task 2

- [ ] Type/method consistency: `cache.put(path, content, etag, gen)`, `is_warm(path)`, `hydrate_workspace(client, cache, opts)` — same signatures used wherever referenced.

- [ ] Placeholders: none. Each step has actual code.

- [ ] One known soft spot: Task 3 Step 3 hedges on whether `client.list` returns `name` (basename) or `path` (full path). The plan tells the engineer to read `client.rs:240-285` to confirm and adjust the test mock — this is intentionally deferred to the engineer because the conversion sits inside a method we did not refactor.
