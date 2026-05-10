# Issue #4053 Foyer Cache Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the `nexus-fuse` SQLite file-content cache with a foyer hybrid DRAM+filesystem cache, preserve ETag revalidation, wire it into both direct Rust FUSE and Python-spawned daemon reads, and record benchmark evidence for issue #4053.

**Architecture:** Keep the public `FileCache` boundary synchronous and hide foyer's async build/get path behind an internal Tokio runtime. Store path-keyed binary records in `HybridCache<String, Vec<u8>>`, maintain lightweight metadata for staleness/stats, and extract the current read-through-cache flow into a shared helper used by `fs.rs` and `daemon.rs`. Keep SQLite only as a dev/benchmark baseline, not production cache code.

**Tech Stack:** Rust 2021, `foyer 0.22.3`, `tokio`, `clap`, `serde`, `bincode`, `criterion`, benchmark-only `rusqlite`, existing `nexus-fuse` FUSE/client/daemon modules.

---

## File Structure

| Path | Change | Responsibility |
| --- | --- | --- |
| `nexus-fuse/Cargo.toml` | modify | Replace production `rusqlite` with `foyer` and `bincode`; keep `rusqlite` as benchmark-only/dev dependency; add cache benchmark target. |
| `nexus-fuse/src/cache.rs` | replace internals | Foyer-backed `FileCache`, cache config, path hashing, SQLite migration deletion, record encode/decode, metadata/stats, behavior tests. |
| `nexus-fuse/src/cached_read.rs` | create | Shared read-through-cache function preserving ETag revalidation and stale fallback. |
| `nexus-fuse/src/lib.rs` | modify | Export the new `cached_read` module. |
| `nexus-fuse/src/fs.rs` | modify | Hold `Arc<FileCache>`, use shared cached-read helper, update SQLite log/comment text. |
| `nexus-fuse/src/daemon.rs` | modify | Hold `Arc<FileCache>`, use shared cached-read helper for `read`, invalidate cache on mutating RPCs. |
| `nexus-fuse/src/main.rs` | modify | Add cache flags/env parsing, create shared cache for mount and daemon commands, log foyer tier config. |
| `nexus-fuse/benches/cache_backends.rs` | create | Criterion benchmark comparing foyer cache operations against benchmark-only SQLite baseline. |
| `nexus-fuse/ARCHITECTURE.md` | modify | Replace SQLite cache wording with foyer hybrid cache wording. |
| `nexus-fuse/PERFORMANCE_RESULTS.md` | modify | Add migration note, benchmark command, and measured issue #4053 result. |

## External API Notes

Use the current foyer 0.22.3 APIs verified before this plan:

```rust
use foyer::{
    BlockEngineConfig, DeviceBuilder, FsDeviceBuilder, HybridCache, HybridCacheBuilder,
    StorageFilter,
};

let device = FsDeviceBuilder::new(cache_dir)
    .with_capacity(disk_bytes)
    .build()?;

let cache: HybridCache<String, Vec<u8>> = HybridCacheBuilder::new()
    .with_name("nexus-fuse-file-cache")
    .memory(memory_bytes)
    .with_weighter(|_key, value: &Vec<u8>| value.len())
    .storage()
    .with_engine_config(
        BlockEngineConfig::new(device)
            .with_admission_filter(StorageFilter::new()),
    )
    .build()
    .await?;
```

`HybridCache::insert` and `HybridCache::remove` are synchronous. `HybridCache::get` is async and must be called through the runtime handle.

### Task 1: Cache Configuration, Dependencies, And Path Migration Tests

**Files:**
- Modify: `nexus-fuse/Cargo.toml`
- Modify: `nexus-fuse/src/cache.rs`

- [ ] **Step 1: Write failing config and path tests**

Append these tests inside `#[cfg(test)] mod tests` in `nexus-fuse/src/cache.rs`, replacing SQLite-specific helper usage where necessary:

```rust
#[test]
fn test_cache_config_defaults() {
    let config = CacheConfig::default();
    assert_eq!(config.memory_bytes, DEFAULT_MEMORY_CACHE_BYTES);
    assert_eq!(config.disk_bytes, DEFAULT_DISK_CACHE_BYTES);
    assert_eq!(config.max_file_size, MAX_FILE_SIZE);
    assert!(config.root_dir.ends_with("nexus-fuse"));
}

#[test]
fn test_cache_config_rejects_zero_tiers() {
    let root = std::env::temp_dir().join("nexus-fuse-config-test");
    let err = CacheConfig::new(root, 0, DEFAULT_DISK_CACHE_BYTES, MAX_FILE_SIZE)
        .expect_err("zero memory tier must be rejected");
    assert!(err.to_string().contains("memory cache size must be greater than zero"));

    let root = std::env::temp_dir().join("nexus-fuse-config-test");
    let err = CacheConfig::new(root, DEFAULT_MEMORY_CACHE_BYTES, 0, MAX_FILE_SIZE)
        .expect_err("zero disk tier must be rejected");
    assert!(err.to_string().contains("disk cache size must be greater than zero"));
}

#[test]
fn test_cache_paths_are_stable_and_distinct() {
    let root = std::env::temp_dir().join("nexus-fuse-path-test");
    let a = CachePaths::for_server(&root, "http://a:8080");
    let b = CachePaths::for_server(&root, "http://a/8080");

    assert_ne!(a.foyer_dir, b.foyer_dir);
    assert_ne!(a.sqlite_file, b.sqlite_file);
    assert!(a.foyer_dir.file_name().unwrap().to_string_lossy().ends_with(".foyer"));
    assert!(a.sqlite_file.file_name().unwrap().to_string_lossy().ends_with(".db"));
}
```

- [ ] **Step 2: Run tests and verify they fail for missing types**

Run:

```bash
cd /Users/tafeng/.codex/worktrees/bf70/nexus/nexus-fuse
cargo test cache::tests::test_cache_config_defaults cache::tests::test_cache_config_rejects_zero_tiers cache::tests::test_cache_paths_are_stable_and_distinct --lib
```

Expected: compile failure naming missing `CacheConfig`, `DEFAULT_MEMORY_CACHE_BYTES`, `DEFAULT_DISK_CACHE_BYTES`, or `CachePaths`.

- [ ] **Step 3: Add test dependency**

Add these under `[dev-dependencies]`:

```toml
# Temporary directories for cache tests
tempfile = "3"
```

- [ ] **Step 4: Add cache config and path helpers**

In `nexus-fuse/src/cache.rs`, keep the existing SQLite imports for now and add `Path` to the `PathBuf` import:

```rust
use std::hash::{Hash, Hasher};
use std::path::{Path, PathBuf};
```

Replace the current cache-size constants with:

```rust
/// Maximum age for cached content before forcing revalidation (1 hour).
const MAX_CACHE_AGE_SECS: u64 = 3600;

/// Default DRAM cache size in bytes (256 MiB).
pub const DEFAULT_MEMORY_CACHE_BYTES: usize = 256 * 1024 * 1024;

/// Default filesystem cache size in bytes (10 GiB).
pub const DEFAULT_DISK_CACHE_BYTES: usize = 10 * 1024 * 1024 * 1024;

/// Maximum cache size in bytes (500 MB). Used only by the legacy SQLite
/// implementation until Task 2 removes it.
const MAX_CACHE_SIZE: u64 = 500 * 1024 * 1024;

/// Maximum file size to cache (10 MiB) - larger files bypass cache.
pub const MAX_FILE_SIZE: usize = 10 * 1024 * 1024;
```

Add these config/path types below the constants:

```rust
#[derive(Debug, Clone)]
pub struct CacheConfig {
    pub root_dir: PathBuf,
    pub memory_bytes: usize,
    pub disk_bytes: usize,
    pub max_file_size: usize,
}

impl CacheConfig {
    pub fn new(
        root_dir: PathBuf,
        memory_bytes: usize,
        disk_bytes: usize,
        max_file_size: usize,
    ) -> Result<Self> {
        if memory_bytes == 0 {
            return Err(anyhow!("memory cache size must be greater than zero"));
        }
        if disk_bytes == 0 {
            return Err(anyhow!("disk cache size must be greater than zero"));
        }
        if max_file_size == 0 {
            return Err(anyhow!("max file size must be greater than zero"));
        }
        Ok(Self {
            root_dir,
            memory_bytes,
            disk_bytes,
            max_file_size,
        })
    }
}

impl Default for CacheConfig {
    fn default() -> Self {
        let root_dir = dirs::cache_dir()
            .unwrap_or_else(std::env::temp_dir)
            .join("nexus-fuse");
        Self {
            root_dir,
            memory_bytes: DEFAULT_MEMORY_CACHE_BYTES,
            disk_bytes: DEFAULT_DISK_CACHE_BYTES,
            max_file_size: MAX_FILE_SIZE,
        }
    }
}

#[derive(Debug, Clone)]
pub struct CachePaths {
    pub foyer_dir: PathBuf,
    pub sqlite_file: PathBuf,
}

impl CachePaths {
    pub fn for_server(root_dir: &Path, server_url: &str) -> Self {
        let hash = server_hash(server_url);
        Self {
            foyer_dir: root_dir.join(format!("nexus_{hash:016x}.foyer")),
            sqlite_file: root_dir.join(format!("nexus_{hash:016x}.db")),
        }
    }
}

fn server_hash(server_url: &str) -> u64 {
    let mut hasher = std::collections::hash_map::DefaultHasher::new();
    server_url.hash(&mut hasher);
    hasher.finish()
}
```

- [ ] **Step 5: Run config tests and verify they pass**

Run the same command from Step 2.

Expected: the three tests pass.

- [ ] **Step 6: Commit**

```bash
git add nexus-fuse/Cargo.toml nexus-fuse/src/cache.rs
git commit -m "test(#4053): define foyer cache config behavior"
```

### Task 2: Foyer-Backed FileCache Core

**Files:**
- Modify: `nexus-fuse/src/cache.rs`

- [ ] **Step 1: Replace SQLite tests with public cache behavior tests**

Keep behavior tests for basic cache use and remove tests that directly manipulate `cache.conn`. The final `tests` module should contain these test names:

```rust
fn test_cache_config_defaults() {}
fn test_cache_config_rejects_zero_tiers() {}
fn test_cache_paths_are_stable_and_distinct() {}
fn test_old_sqlite_file_is_deleted_on_startup() {}
fn test_cache_basic() {}
fn test_put_without_etag() {}
fn test_overwrite_entry() {}
fn test_get_etag() {}
fn test_touch_refreshes_stale_entry() {}
fn test_stale_entry_with_etag_needs_revalidation() {}
fn test_stale_entry_without_etag_is_miss() {}
fn test_large_file_not_cached() {}
fn test_max_size_file_cached() {}
fn test_cache_stats() {}
fn test_stats_after_invalidation() {}
fn test_multiple_independent_paths() {}
fn test_empty_content_cached() {}
fn test_binary_content_preserved() {}
fn test_concurrent_access() {}
fn test_invalidate_nonexistent_is_noop() {}
```

Use this helper for tests:

```rust
fn test_cache(label: &str) -> FileCache {
    let dir = tempfile::tempdir().unwrap();
    let config = CacheConfig::new(
        dir.path().join(label),
        4 * 1024 * 1024,
        64 * 1024 * 1024,
        MAX_FILE_SIZE,
    )
    .unwrap();
    FileCache::new_with_config(&format!("http://{label}.test"), config).unwrap()
}
```

For stale tests, use `cache.backdate_for_test("/path", MAX_CACHE_AGE_SECS + 1);`.

Use this exact migration test:

```rust
#[test]
fn test_old_sqlite_file_is_deleted_on_startup() {
    let dir = tempfile::tempdir().unwrap();
    let config = CacheConfig::new(
        dir.path().to_path_buf(),
        4 * 1024 * 1024,
        32 * 1024 * 1024,
        MAX_FILE_SIZE,
    )
    .unwrap();
    let paths = CachePaths::for_server(dir.path(), "http://migration.test");
    std::fs::write(&paths.sqlite_file, b"old sqlite cache").unwrap();

    let _cache = FileCache::new_with_config("http://migration.test", config).unwrap();

    assert!(!paths.sqlite_file.exists());
    assert!(paths.foyer_dir.exists());
}
```

- [ ] **Step 2: Run tests and verify they fail against SQLite implementation**

Run:

```bash
cd /Users/tafeng/.codex/worktrees/bf70/nexus/nexus-fuse
cargo test cache::tests --lib
```

Expected: compile failure from missing foyer `FileCache` fields and `backdate_for_test`, plus SQLite-specific tests no longer matching internals.

- [ ] **Step 3: Update production dependencies and imports**

In `nexus-fuse/Cargo.toml`, replace:

```toml
# SQLite for persistent caching
rusqlite = { version = "0.31", features = ["bundled"] }
```

with:

```toml
# Hybrid DRAM + filesystem cache for file content
foyer = "0.22.3"

# Compact cache record encoding
bincode = "1.3"
```

Add this under `[dev-dependencies]`:

```toml
# Benchmark-only baseline for issue #4053 acceptance comparison
rusqlite = { version = "0.31", features = ["bundled"] }
```

Replace the module docs/imports at the top of `nexus-fuse/src/cache.rs` with:

```rust
//! Foyer-backed hybrid cache for Nexus FUSE.
//!
//! Provides ETag-based cache invalidation to minimize network round-trips
//! while using a DRAM tier and filesystem-backed disk tier for hot-path reads.

#![allow(dead_code)]

use anyhow::{anyhow, Context, Result};
use foyer::{
    BlockEngineConfig, DeviceBuilder, FsDeviceBuilder, HybridCache, HybridCacheBuilder,
    StorageFilter,
};
use log::{debug, error, info, warn};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::hash::{Hash, Hasher};
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};
```

- [ ] **Step 4: Implement foyer cache data structures**

In `nexus-fuse/src/cache.rs`, keep `CacheEntry`, `CacheLookup`, and `CacheStats`, then add:

```rust
#[derive(Debug, Clone, Serialize, Deserialize)]
struct CacheRecord {
    content: Vec<u8>,
    etag: Option<String>,
    cached_at_secs: u64,
}

#[derive(Debug, Clone)]
struct CacheMeta {
    etag: Option<String>,
    cached_at_secs: u64,
    size: usize,
}

pub struct FileCache {
    cache: HybridCache<String, Vec<u8>>,
    runtime: tokio::runtime::Runtime,
    metadata: Mutex<HashMap<String, CacheMeta>>,
    config: CacheConfig,
}
```

- [ ] **Step 5: Implement constructors and migration**

Add these methods to `impl FileCache`:

```rust
pub fn new(server_url: &str) -> Result<Self> {
    Self::new_with_config(server_url, CacheConfig::default())
}

pub fn new_with_config(server_url: &str, config: CacheConfig) -> Result<Self> {
    std::fs::create_dir_all(&config.root_dir)
        .with_context(|| format!("failed to create cache root {}", config.root_dir.display()))?;

    let paths = CachePaths::for_server(&config.root_dir, server_url);
    migrate_sqlite_file(&paths.sqlite_file);
    std::fs::create_dir_all(&paths.foyer_dir)
        .with_context(|| format!("failed to create foyer cache dir {}", paths.foyer_dir.display()))?;

    info!(
        "Opening foyer cache at: {} (memory={} MB, disk={} GB)",
        paths.foyer_dir.display(),
        config.memory_bytes / 1024 / 1024,
        config.disk_bytes / 1024 / 1024 / 1024,
    );

    let runtime = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .thread_name("nexus-fuse-cache")
        .worker_threads(2)
        .build()
        .context("failed to create foyer cache runtime")?;

    let cache = runtime.block_on(async {
        let device = FsDeviceBuilder::new(&paths.foyer_dir)
            .with_capacity(config.disk_bytes)
            .build()?;

        let cache = HybridCacheBuilder::new()
            .with_name("nexus-fuse-file-cache")
            .memory(config.memory_bytes)
            .with_weighter(|_key, value: &Vec<u8>| value.len())
            .storage()
            .with_engine_config(
                BlockEngineConfig::new(device)
                    .with_admission_filter(StorageFilter::new()),
            )
            .build()
            .await?;

        Ok::<HybridCache<String, Vec<u8>>, foyer::Error>(cache)
    })?;

    Ok(Self {
        cache,
        runtime,
        metadata: Mutex::new(HashMap::new()),
        config,
    })
}
```

Add this helper outside `impl FileCache`:

```rust
fn migrate_sqlite_file(sqlite_file: &Path) {
    if !sqlite_file.exists() {
        return;
    }

    match std::fs::remove_file(sqlite_file) {
        Ok(()) => info!("Dropped old SQLite cache file {}", sqlite_file.display()),
        Err(e) => warn!(
            "Failed to delete old SQLite cache file {}: {}",
            sqlite_file.display(),
            e
        ),
    }
}
```

- [ ] **Step 6: Implement record encode/decode and lookup behavior**

Add these methods:

```rust
fn now() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

fn encode_record(record: &CacheRecord) -> Result<Vec<u8>> {
    bincode::serialize(record).context("failed to encode cache record")
}

fn decode_record(bytes: &[u8]) -> Result<CacheRecord> {
    bincode::deserialize(bytes).context("failed to decode cache record")
}

pub fn get(&self, path: &str) -> CacheLookup {
    let now = Self::now();
    let meta = self
        .metadata
        .lock()
        .ok()
        .and_then(|metadata| metadata.get(path).cloned());

    if let Some(meta) = meta {
        let age = now.saturating_sub(meta.cached_at_secs);
        if age >= MAX_CACHE_AGE_SECS {
            if let Some(etag) = meta.etag {
                debug!("Cache stale for {} (age: {}s), needs revalidation", path, age);
                return CacheLookup::NeedsRevalidation { etag };
            }
            debug!("Cache stale for {} with no etag", path);
            return CacheLookup::Miss;
        }
    }

    let key = path.to_string();
    match self.runtime.block_on(self.cache.get(&key)) {
        Ok(Some(entry)) => match Self::decode_record(entry.value()) {
            Ok(record) => {
                let age = now.saturating_sub(record.cached_at_secs);
                if age < MAX_CACHE_AGE_SECS {
                    debug!("Cache hit for {} (age: {}s)", path, age);
                    return CacheLookup::Hit(CacheEntry {
                        content: record.content,
                        etag: record.etag,
                    });
                }
                if let Some(etag) = record.etag {
                    return CacheLookup::NeedsRevalidation { etag };
                }
                CacheLookup::Miss
            }
            Err(e) => {
                warn!("Failed to decode cached entry {}: {}", path, e);
                CacheLookup::Miss
            }
        },
        Ok(None) => CacheLookup::Miss,
        Err(e) => {
            warn!("Foyer cache read failed for {}: {}", path, e);
            CacheLookup::Miss
        }
    }
}
```

- [ ] **Step 7: Implement mutation, stats, and test-only backdating**

Add these methods:

```rust
pub fn get_etag(&self, path: &str) -> Option<String> {
    self.metadata
        .lock()
        .ok()
        .and_then(|metadata| metadata.get(path).and_then(|meta| meta.etag.clone()))
}

pub fn put(&self, path: &str, content: &[u8], etag: Option<&str>) {
    if content.len() > self.config.max_file_size {
        debug!(
            "Skipping cache for {} ({} bytes > {} limit)",
            path,
            content.len(),
            self.config.max_file_size
        );
        return;
    }

    let now = Self::now();
    let record = CacheRecord {
        content: content.to_vec(),
        etag: etag.map(str::to_string),
        cached_at_secs: now,
    };
    let encoded = match Self::encode_record(&record) {
        Ok(encoded) => encoded,
        Err(e) => {
            error!("Failed to encode cache entry {}: {}", path, e);
            return;
        }
    };

    self.cache.insert(path.to_string(), encoded);
    if let Ok(mut metadata) = self.metadata.lock() {
        metadata.insert(
            path.to_string(),
            CacheMeta {
                etag: record.etag,
                cached_at_secs: now,
                size: content.len(),
            },
        );
    }
}

pub fn touch(&self, path: &str) {
    let Some(entry) = self.runtime.block_on(self.cache.get(path)).ok().flatten() else {
        debug!("Cache touch skipped for missing entry {}", path);
        return;
    };

    let mut record = match Self::decode_record(entry.value()) {
        Ok(record) => record,
        Err(e) => {
            warn!("Failed to decode cache entry during touch {}: {}", path, e);
            return;
        }
    };
    record.cached_at_secs = Self::now();
    let encoded = match Self::encode_record(&record) {
        Ok(encoded) => encoded,
        Err(e) => {
            warn!("Failed to encode cache entry during touch {}: {}", path, e);
            return;
        }
    };
    self.cache.insert(path.to_string(), encoded);
    if let Ok(mut metadata) = self.metadata.lock() {
        metadata.insert(
            path.to_string(),
            CacheMeta {
                etag: record.etag,
                cached_at_secs: record.cached_at_secs,
                size: record.content.len(),
            },
        );
    }
}

pub fn invalidate(&self, path: &str) {
    self.cache.remove(path);
    if let Ok(mut metadata) = self.metadata.lock() {
        metadata.remove(path);
    }
    debug!("Invalidated cache for {}", path);
}

pub fn stats(&self) -> CacheStats {
    let Ok(metadata) = self.metadata.lock() else {
        return CacheStats {
            file_count: 0,
            total_size: 0,
        };
    };

    CacheStats {
        file_count: metadata.len() as u64,
        total_size: metadata.values().map(|meta| meta.size as u64).sum(),
    }
}

#[cfg(test)]
fn backdate_for_test(&self, path: &str, age_secs: u64) {
    let cached_at_secs = Self::now().saturating_sub(age_secs);
    if let Ok(mut metadata) = self.metadata.lock() {
        if let Some(meta) = metadata.get_mut(path) {
            meta.cached_at_secs = cached_at_secs;
        }
    }

    let Some(entry) = self.runtime.block_on(self.cache.get(path)).ok().flatten() else {
        return;
    };
    let mut record = Self::decode_record(entry.value()).unwrap();
    record.cached_at_secs = cached_at_secs;
    let encoded = Self::encode_record(&record).unwrap();
    self.cache.insert(path.to_string(), encoded);
}
```

- [ ] **Step 8: Run cache tests**

Run:

```bash
cd /Users/tafeng/.codex/worktrees/bf70/nexus/nexus-fuse
cargo test cache::tests --lib
```

Expected: all cache tests pass. If foyer creates background task logs, test output may include logs but must exit 0.

- [ ] **Step 9: Commit**

```bash
git add nexus-fuse/Cargo.toml nexus-fuse/src/cache.rs
git commit -m "feat(#4053): replace fuse file cache with foyer"
```

### Task 3: Shared ETag Read-Through Cache Helper

**Files:**
- Create: `nexus-fuse/src/cached_read.rs`
- Modify: `nexus-fuse/src/lib.rs`
- Modify: `nexus-fuse/src/fs.rs`

- [ ] **Step 1: Write the shared helper**

Create `nexus-fuse/src/cached_read.rs`:

```rust
//! Shared read-through-cache flow for nexus-fuse mount and daemon reads.

use crate::cache::{CacheLookup, FileCache};
use crate::client::{NexusClient, ReadResponse};
use crate::error::NexusClientError;
use log::{debug, error};

pub fn read_with_cache(
    client: &NexusClient,
    cache: Option<&FileCache>,
    path: &str,
) -> Result<(Vec<u8>, Option<String>), NexusClientError> {
    if let Some(cache) = cache {
        match cache.get(path) {
            CacheLookup::Hit(entry) => {
                debug!("Foyer cache hit for {}", path);
                return Ok((entry.content, entry.etag));
            }
            CacheLookup::NeedsRevalidation { etag } => {
                debug!("Revalidating cache for {} with etag {}", path, etag);
                match client.read_with_etag(path, Some(&etag)) {
                    Ok(ReadResponse::NotModified) => {
                        cache.touch(path);
                        match cache.get(path) {
                            CacheLookup::Hit(entry) => return Ok((entry.content, entry.etag)),
                            _ => error!("Cache inconsistency after 304 for {}", path),
                        }
                    }
                    Ok(ReadResponse::Content { content, etag }) => {
                        cache.put(path, &content, etag.as_deref());
                        return Ok((content, etag));
                    }
                    Err(e) => {
                        debug!("Revalidation failed for {}: {}, using stale cache", path, e);
                        if let CacheLookup::Hit(entry) = cache.get(path) {
                            return Ok((entry.content, entry.etag));
                        }
                        return Err(e);
                    }
                }
            }
            CacheLookup::Miss => {}
        }
    }

    match client.read_with_etag(path, None) {
        Ok(ReadResponse::Content { content, etag }) => {
            if let Some(cache) = cache {
                cache.put(path, &content, etag.as_deref());
            }
            Ok((content, etag))
        }
        Ok(ReadResponse::NotModified) => Err(NexusClientError::InvalidResponse(
            "Unexpected 304 response".to_string(),
        )),
        Err(e) => Err(e),
    }
}
```

- [ ] **Step 2: Export the module**

In `nexus-fuse/src/lib.rs`, add:

```rust
pub mod cached_read;
```

- [ ] **Step 3: Update `fs.rs` to use the helper**

Change imports:

```rust
use crate::cache::FileCache;
use crate::cached_read::read_with_cache;
```

Remove `CacheLookup` and `ReadResponse` from `fs.rs` imports.

Change `NexusFs` field and constructor:

```rust
file_cache: Option<std::sync::Arc<FileCache>>,

pub fn new(client: NexusClient, file_cache: Option<std::sync::Arc<FileCache>>) -> Self {
```

Replace the body of `fn read_cached(&self, path: &str)` with:

```rust
read_with_cache(&self.client, self.file_cache.as_deref(), path).map_err(Into::into)
```

Update comments near `read_cached` and `read` from `SQLite cache` to `foyer cache`.

- [ ] **Step 4: Run compile check for mount path**

Run:

```bash
cd /Users/tafeng/.codex/worktrees/bf70/nexus/nexus-fuse
cargo test cache::tests --lib
cargo check
```

Expected: tests pass and `cargo check` exits 0. If `NexusFs::new` call sites fail, fix them in Task 4 where cache construction is updated.

- [ ] **Step 5: Commit**

```bash
git add nexus-fuse/src/cached_read.rs nexus-fuse/src/lib.rs nexus-fuse/src/fs.rs
git commit -m "refactor(#4053): share fuse cached read flow"
```

### Task 4: CLI, Mount, And Daemon Cache Wiring

**Files:**
- Modify: `nexus-fuse/src/main.rs`
- Modify: `nexus-fuse/src/daemon.rs`

- [ ] **Step 1: Write compile-failing CLI config code path**

In `nexus-fuse/src/main.rs`, add cache arguments to both `Commands::Mount` and `Commands::Daemon`:

```rust
/// Foyer DRAM cache size in MiB
#[arg(long, env = "NEXUS_FUSE_CACHE_MEMORY_MB", default_value_t = 256)]
cache_memory_mb: usize,

/// Foyer filesystem cache size in GiB
#[arg(long, env = "NEXUS_FUSE_CACHE_DISK_GB", default_value_t = 10)]
cache_disk_gb: usize,

/// Override cache root directory
#[arg(long, env = "NEXUS_FUSE_CACHE_DIR")]
cache_dir: Option<PathBuf>,
```

Update match destructuring to include these three fields for both commands.

Run:

```bash
cd /Users/tafeng/.codex/worktrees/bf70/nexus/nexus-fuse
cargo check
```

Expected: compile failure until `build_cache_config` and daemon config fields are added.

- [ ] **Step 2: Add cache config builder in `main.rs`**

Add imports:

```rust
use std::sync::Arc;
```

Add helpers above `main`:

```rust
fn mib_to_bytes(mib: usize) -> anyhow::Result<usize> {
    mib.checked_mul(1024 * 1024)
        .ok_or_else(|| anyhow::anyhow!("cache memory size overflows usize"))
}

fn gib_to_bytes(gib: usize) -> anyhow::Result<usize> {
    gib.checked_mul(1024 * 1024 * 1024)
        .ok_or_else(|| anyhow::anyhow!("cache disk size overflows usize"))
}

fn build_cache_config(
    cache_memory_mb: usize,
    cache_disk_gb: usize,
    cache_dir: Option<PathBuf>,
) -> anyhow::Result<cache::CacheConfig> {
    let root_dir = cache_dir.unwrap_or_else(|| cache::CacheConfig::default().root_dir);
    cache::CacheConfig::new(
        root_dir,
        mib_to_bytes(cache_memory_mb)?,
        gib_to_bytes(cache_disk_gb)?,
        cache::MAX_FILE_SIZE,
    )
}

fn open_file_cache(
    url: &str,
    config: cache::CacheConfig,
) -> Option<Arc<cache::FileCache>> {
    match cache::FileCache::new_with_config(url, config) {
        Ok(cache) => {
            let stats = cache.stats();
            info!(
                "Foyer cache ready: {} current-process files ({} MB)",
                stats.file_count,
                stats.total_size / 1024 / 1024
            );
            Some(Arc::new(cache))
        }
        Err(e) => {
            error!("Failed to initialize foyer cache: {} (continuing without cache)", e);
            None
        }
    }
}
```

- [ ] **Step 3: Wire mount cache creation**

Replace the existing mount cache block:

```rust
let file_cache = match cache::FileCache::new(&url) {
    Ok(cache) => {
        let stats = cache.stats();
        info!(
            "Cache loaded: {} files ({} MB)",
            stats.file_count,
            stats.total_size / 1024 / 1024
        );
        Some(cache)
    }
    Err(e) => {
        error!("Failed to initialize cache: {} (continuing without cache)", e);
        None
    }
};
```

with:

```rust
let cache_config = build_cache_config(cache_memory_mb, cache_disk_gb, cache_dir)?;
let file_cache = open_file_cache(&url, cache_config);
```

`NexusFs::new(client, file_cache)` now receives `Option<Arc<FileCache>>`.

- [ ] **Step 4: Wire daemon config and cache use**

In `nexus-fuse/src/daemon.rs`, add imports:

```rust
use crate::cache::FileCache;
use crate::cached_read::read_with_cache;
use std::sync::Arc;
```

Update structs:

```rust
pub struct DaemonConfig {
    pub socket_path: PathBuf,
    pub nexus_url: String,
    pub api_key: String,
    pub agent_id: Option<String>,
    pub file_cache: Option<Arc<FileCache>>,
}

pub struct Daemon {
    config: DaemonConfig,
    client: NexusClient,
    file_cache: Option<Arc<FileCache>>,
}
```

Update `Daemon::new`:

```rust
Ok(Self {
    file_cache: config.file_cache.clone(),
    config,
    client,
})
```

Update connection spawn:

```rust
let client = self.client.clone();
let file_cache = self.file_cache.clone();
tokio::spawn(async move {
    if let Err(e) = handle_connection(stream, client, file_cache).await {
        error!("Connection error: {}", e);
    }
});
```

Update function signatures and request dispatch:

```rust
async fn handle_connection(
    stream: UnixStream,
    client: NexusClient,
    file_cache: Option<Arc<FileCache>>,
) -> anyhow::Result<()> {
```

```rust
let response = match serde_json::from_str::<JsonRpcRequest>(&line) {
    Ok(request) => handle_request(request, &client, file_cache.clone()).await,
```

```rust
async fn handle_request(
    request: JsonRpcRequest,
    client: &NexusClient,
    file_cache: Option<Arc<FileCache>>,
) -> JsonRpcResponse {
```

Inside `spawn_blocking`, clone the cache into the closure and dispatch:

```rust
let result = tokio::task::spawn_blocking(move || match method.as_str() {
    "read" => handle_read(&params, &client, file_cache.as_deref()),
    "write" => handle_write(&params, &client, file_cache.as_deref()),
    "list" => handle_list(&params, &client),
    "stat" => handle_stat(&params, &client),
    "mkdir" => handle_mkdir(&params, &client),
    "delete" => handle_delete(&params, &client, file_cache.as_deref()),
    "rename" => handle_rename(&params, &client, file_cache.as_deref()),
    "exists" => handle_exists(&params, &client),
    _ => Err(NexusClientError::InvalidResponse(format!(
        "Method not found: {}",
        method
    ))),
})
.await;
```

Replace daemon handlers:

```rust
fn handle_read(
    params: &Value,
    client: &NexusClient,
    file_cache: Option<&FileCache>,
) -> Result<Value, NexusClientError> {
    #[derive(Deserialize)]
    struct P { path: String }
    let p: P = extract_params(params)?;

    let (content, _) = read_with_cache(client, file_cache, &p.path)?;
    let encoded = base64::engine::general_purpose::STANDARD.encode(&content);

    Ok(json!({
        "__type__": "bytes",
        "data": encoded
    }))
}

fn handle_write(
    params: &Value,
    client: &NexusClient,
    file_cache: Option<&FileCache>,
) -> Result<Value, NexusClientError> {
    #[derive(Deserialize)]
    struct ContentBytes {
        #[serde(rename = "__type__")]
        type_tag: String,
        data: String,
    }
    #[derive(Deserialize)]
    struct P { path: String, content: ContentBytes }
    let p: P = extract_params(params)?;

    let content = base64::engine::general_purpose::STANDARD
        .decode(&p.content.data)
        .map_err(|e| NexusClientError::InvalidResponse(format!("Invalid base64: {}", e)))?;

    client.write(&p.path, &content)?;
    if let Some(cache) = file_cache {
        cache.invalidate(&p.path);
    }
    Ok(json!({}))
}

fn handle_delete(
    params: &Value,
    client: &NexusClient,
    file_cache: Option<&FileCache>,
) -> Result<Value, NexusClientError> {
    #[derive(Deserialize)]
    struct P { path: String }
    let p: P = extract_params(params)?;

    client.delete(&p.path)?;
    if let Some(cache) = file_cache {
        cache.invalidate(&p.path);
    }
    Ok(json!({}))
}

fn handle_rename(
    params: &Value,
    client: &NexusClient,
    file_cache: Option<&FileCache>,
) -> Result<Value, NexusClientError> {
    #[derive(Deserialize)]
    struct P { old_path: String, new_path: String }
    let p: P = extract_params(params)?;

    client.rename(&p.old_path, &p.new_path)?;
    if let Some(cache) = file_cache {
        cache.invalidate(&p.old_path);
        cache.invalidate(&p.new_path);
    }
    Ok(json!({}))
}
```

Update daemon command config construction in `main.rs`:

```rust
let cache_config = build_cache_config(cache_memory_mb, cache_disk_gb, cache_dir)?;
let file_cache = open_file_cache(&url, cache_config);

let config = daemon::DaemonConfig {
    socket_path,
    nexus_url: url,
    api_key,
    agent_id,
    file_cache,
};
```

- [ ] **Step 5: Run checks**

Run:

```bash
cd /Users/tafeng/.codex/worktrees/bf70/nexus/nexus-fuse
cargo test cache::tests --lib
cargo check
```

Expected: all tests pass and `cargo check` exits 0.

- [ ] **Step 6: Commit**

```bash
git add nexus-fuse/src/main.rs nexus-fuse/src/daemon.rs nexus-fuse/src/fs.rs
git commit -m "feat(#4053): wire foyer cache into mount and daemon reads"
```

### Task 5: Benchmark-Only SQLite Baseline And Foyer Cache Benchmark

**Files:**
- Modify: `nexus-fuse/Cargo.toml`
- Create: `nexus-fuse/benches/cache_backends.rs`

- [ ] **Step 1: Add benchmark target**

Append to `nexus-fuse/Cargo.toml`:

```toml
[[bench]]
name = "cache_backends"
harness = false
```

- [ ] **Step 2: Create the benchmark file**

Create `nexus-fuse/benches/cache_backends.rs`:

```rust
use criterion::{criterion_group, criterion_main, BatchSize, Criterion};
use nexus_fuse::cache::{CacheConfig, FileCache, MAX_FILE_SIZE};
use rusqlite::{params, Connection, OptionalExtension};
use std::time::{Duration, Instant};

struct SqliteBaseline {
    conn: Connection,
}

impl SqliteBaseline {
    fn new() -> Self {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "
            CREATE TABLE file_cache (
                path TEXT PRIMARY KEY,
                content BLOB NOT NULL,
                etag TEXT,
                size INTEGER NOT NULL,
                cached_at INTEGER NOT NULL
            );
            ",
        )
        .unwrap();
        Self { conn }
    }

    fn put(&self, path: &str, content: &[u8], etag: &str) {
        self.conn
            .execute(
                "INSERT OR REPLACE INTO file_cache (path, content, etag, size, cached_at)
                 VALUES (?, ?, ?, ?, ?)",
                params![path, content, etag, content.len() as i64, 0_i64],
            )
            .unwrap();
    }

    fn get(&self, path: &str) -> Option<Vec<u8>> {
        self.conn
            .query_row(
                "SELECT content FROM file_cache WHERE path = ?",
                params![path],
                |row| row.get(0),
            )
            .optional()
            .unwrap()
    }
}

fn foyer_cache(label: &str, memory_bytes: usize) -> FileCache {
    let dir = tempfile::tempdir().unwrap().into_path();
    let config = CacheConfig::new(
        dir,
        memory_bytes,
        256 * 1024 * 1024,
        MAX_FILE_SIZE,
    )
    .unwrap();
    FileCache::new_with_config(&format!("http://bench-{label}.test"), config).unwrap()
}

fn bench_warm_reads(c: &mut Criterion) {
    for size in [1024_usize, 10 * 1024, 100 * 1024, 1024 * 1024] {
        let data = vec![42_u8; size];
        let name = format!("warm_read_{}b", size);

        c.bench_function(&format!("foyer_{name}"), |b| {
            b.iter_batched(
                || {
                    let cache = foyer_cache(&name, 32 * 1024 * 1024);
                    cache.put("/warm.bin", &data, Some("etag"));
                    cache
                },
                |cache| {
                    let entry = cache.get("/warm.bin");
                    std::hint::black_box(entry);
                },
                BatchSize::SmallInput,
            );
        });

        c.bench_function(&format!("sqlite_{name}"), |b| {
            b.iter_batched(
                || {
                    let cache = SqliteBaseline::new();
                    cache.put("/warm.bin", &data, "etag");
                    cache
                },
                |cache| {
                    let entry = cache.get("/warm.bin");
                    std::hint::black_box(entry);
                },
                BatchSize::SmallInput,
            );
        });
    }
}

fn bench_agent_churn_p99(c: &mut Criterion) {
    let object_count = 2_000;
    let hot_count = 128;
    let data = vec![7_u8; 16 * 1024];
    let paths: Vec<String> = (0..object_count)
        .map(|i| format!("/trace/object-{i}.bin"))
        .collect();

    c.bench_function("foyer_agent_churn_trace", |b| {
        b.iter_batched(
            || {
                let cache = foyer_cache("agent-churn", 8 * 1024 * 1024);
                for path in paths.iter().take(hot_count) {
                    cache.put(path, &data, Some("etag"));
                }
                cache
            },
            |cache| {
                let mut latencies = Vec::with_capacity(object_count);
                let mut hits = 0_u64;
                for (i, path) in paths.iter().enumerate() {
                    if i % 4 == 0 {
                        cache.put(path, &data, Some("etag"));
                    }
                    let start = Instant::now();
                    if matches!(cache.get(path), nexus_fuse::cache::CacheLookup::Hit(_)) {
                        hits += 1;
                    }
                    latencies.push(start.elapsed());
                }
                latencies.sort_unstable();
                let p99 = latencies[latencies.len() * 99 / 100];
                std::hint::black_box((hits, p99));
            },
            BatchSize::SmallInput,
        );
    });

    c.bench_function("sqlite_agent_churn_trace", |b| {
        b.iter_batched(
            || {
                let cache = SqliteBaseline::new();
                for path in paths.iter().take(hot_count) {
                    cache.put(path, &data, "etag");
                }
                cache
            },
            |cache| {
                let mut latencies = Vec::with_capacity(object_count);
                let mut hits = 0_u64;
                for (i, path) in paths.iter().enumerate() {
                    if i % 4 == 0 {
                        cache.put(path, &data, "etag");
                    }
                    let start = Instant::now();
                    if cache.get(path).is_some() {
                        hits += 1;
                    }
                    latencies.push(start.elapsed());
                }
                latencies.sort_unstable();
                let p99 = latencies[latencies.len() * 99 / 100];
                std::hint::black_box((hits, p99));
            },
            BatchSize::SmallInput,
        );
    });
}

criterion_group! {
    name = benches;
    config = Criterion::default()
        .warm_up_time(Duration::from_secs(1))
        .measurement_time(Duration::from_secs(5));
    targets = bench_warm_reads, bench_agent_churn_p99
}
criterion_main!(benches);
```

- [ ] **Step 3: Run benchmark compile**

Run:

```bash
cd /Users/tafeng/.codex/worktrees/bf70/nexus/nexus-fuse
cargo bench --bench cache_backends --no-run
```

Expected: benchmark compiles. If `tempfile::TempDir::into_path` emits a deprecation warning, replace it with `tempfile::TempDir::keep` if available in the installed `tempfile` version.

- [ ] **Step 4: Run benchmark and save output**

Run:

```bash
cd /Users/tafeng/.codex/worktrees/bf70/nexus/nexus-fuse
cargo bench --bench cache_backends
```

Expected: Criterion reports `foyer_*` and `sqlite_*` timings. Save the console summary for Task 6.

- [ ] **Step 5: Commit**

```bash
git add nexus-fuse/Cargo.toml nexus-fuse/benches/cache_backends.rs
git commit -m "bench(#4053): compare foyer cache against sqlite baseline"
```

### Task 6: Documentation And Performance Results

**Files:**
- Modify: `nexus-fuse/ARCHITECTURE.md`
- Modify: `nexus-fuse/PERFORMANCE_RESULTS.md`

- [ ] **Step 1: Update architecture references**

In `nexus-fuse/ARCHITECTURE.md`, replace:

```text
- SQLite cache (persistent)
```

with:

```text
- Foyer hybrid cache (DRAM + filesystem tier)
```

Replace:

```text
- Maintain persistent SQLite cache
```

with:

```text
- Maintain foyer-backed file-content cache with ETag revalidation
```

Replace:

```text
- Persistent cache (SQLite vs in-memory)
```

with:

```text
- Persistent hybrid cache (foyer DRAM tier plus filesystem tier)
```

- [ ] **Step 2: Capture benchmark environment**

Run:

```bash
cd /Users/tafeng/.codex/worktrees/bf70/nexus
{
  date "+%Y-%m-%d"
  uname -a
  rustc --version
} > /tmp/nexus-fuse-4053-benchmark-env.txt
```

Expected: `/tmp/nexus-fuse-4053-benchmark-env.txt` contains the benchmark date, OS kernel string, and Rust compiler version.

- [ ] **Step 3: Add migration and benchmark result**

Append a section named `Issue #4053 - foyer Hybrid Cache` to `nexus-fuse/PERFORMANCE_RESULTS.md`. It must include:

- the command `cd nexus-fuse && cargo bench --bench cache_backends`
- the date, OS, and Rust version captured in Step 2
- foyer version `0.22.3`
- benchmark tier sizes: 8 MiB and 32 MiB DRAM cases, 256 MiB filesystem tier
- baseline description: benchmark-only in-memory SQLite table mirroring old `FileCache` hot operations
- a result table with rows for warm 1 KiB, warm 10 KiB, warm 100 KiB, warm 1 MiB, and agent churn trace p99
- an acceptance sentence naming the achieved issue #4053 criterion
- this migration sentence:

```text
Existing SQLite cache files under the nexus-fuse cache root named `nexus_HASH.db` are dropped on cache startup. New foyer cache content is stored under a sibling `nexus_HASH.foyer/` directory.
```

The committed section must contain concrete benchmark numbers from Task 5 for every result row.

- [ ] **Step 4: Run docs grep**

Run:

```bash
cd /Users/tafeng/.codex/worktrees/bf70/nexus
rg -n "SQLite cache|sqlite cache|Persistent cache \\(SQLite" nexus-fuse
```

Expected: no stale production documentation references. Benchmark-only references to SQLite baseline are allowed.

- [ ] **Step 5: Commit**

```bash
git add nexus-fuse/ARCHITECTURE.md nexus-fuse/PERFORMANCE_RESULTS.md
git commit -m "docs(#4053): document foyer cache migration and benchmark"
```

### Task 7: Final Verification And Cleanup

**Files:**
- Review: all files changed in Tasks 1-6

- [ ] **Step 1: Run Rust formatting**

Run:

```bash
cd /Users/tafeng/.codex/worktrees/bf70/nexus/nexus-fuse
cargo fmt --check
```

Expected: exits 0. If it fails, run `cargo fmt`, inspect the diff, and re-run `cargo fmt --check`.

- [ ] **Step 2: Run focused tests**

Run:

```bash
cd /Users/tafeng/.codex/worktrees/bf70/nexus/nexus-fuse
cargo test cache::tests --lib
```

Expected: all cache tests pass.

- [ ] **Step 3: Run full nexus-fuse tests**

Run:

```bash
cd /Users/tafeng/.codex/worktrees/bf70/nexus/nexus-fuse
cargo test
```

Expected: all `nexus-fuse` tests pass.

- [ ] **Step 4: Run clippy**

Run:

```bash
cd /Users/tafeng/.codex/worktrees/bf70/nexus/nexus-fuse
cargo clippy --all-targets -- -D warnings
```

Expected: exits 0. Fix warnings in changed Rust code.

- [ ] **Step 5: Run benchmark compile**

Run:

```bash
cd /Users/tafeng/.codex/worktrees/bf70/nexus/nexus-fuse
cargo bench --bench cache_backends --no-run
```

Expected: benchmark compiles.

- [ ] **Step 6: Confirm production SQLite removal**

Run:

```bash
cd /Users/tafeng/.codex/worktrees/bf70/nexus
rg -n "rusqlite|Connection|OptionalExtension|file_cache \\(" nexus-fuse/src nexus-fuse/Cargo.toml
```

Expected: no `rusqlite`, `Connection`, or `OptionalExtension` references in `nexus-fuse/src`. `rusqlite` may appear only in `nexus-fuse/Cargo.toml` under `[dev-dependencies]` and in `nexus-fuse/benches/cache_backends.rs`.

- [ ] **Step 7: Review diff**

Run:

```bash
cd /Users/tafeng/.codex/worktrees/bf70/nexus
git status --short
git diff --stat HEAD~6..HEAD
git log --oneline -6
```

Expected: only intended issue #4053 files changed, with commits for config/tests, foyer cache, shared read flow, wiring, benchmark, and docs.

- [ ] **Step 8: Final commit for verification fixes if needed**

If formatting or clippy fixes changed files, commit them:

```bash
git add nexus-fuse
git commit -m "fix(#4053): address foyer cache verification findings"
```

If no files changed, do not create an empty commit.

## Self-Review Checklist

- Spec coverage:
  - foyer wired into hot path: Tasks 2, 3, and 4.
  - DRAM tier default/config: Tasks 1 and 4.
  - filesystem/NVMe tier default/config: Tasks 1 and 4.
  - ETag revalidation preserved: Task 3.
  - admission filter before flash writes: Task 2 uses `BlockEngineConfig::with_admission_filter`.
  - benchmark evidence: Task 5 and Task 6.
  - migration path documented: Task 1 and Task 6.
- Placeholder scan: no unfilled code blocks and no benchmark template text left in committed documentation.
- Type consistency: `CacheConfig`, `CachePaths`, `FileCache`, `read_with_cache`, and `Option<Arc<FileCache>>` are used consistently across tasks.
