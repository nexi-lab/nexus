//! Foyer-backed hybrid cache for Nexus FUSE.
//!
//! Provides ETag-based cache invalidation to minimize network round-trips
//! while using a DRAM tier and filesystem-backed disk tier for hot-path reads.

#![allow(dead_code)]

use crate::metrics;
use anyhow::{anyhow, Context, Result};
use foyer::{
    BlockEngineConfig, Code, DeviceBuilder, FsDeviceBuilder, HybridCache, HybridCacheBuilder,
    StorageFilter,
};
use log::{debug, info, warn};
use std::collections::HashMap;
use std::future::Future;
use std::hash::{Hash, Hasher};
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};

/// Maximum age for cached content before forcing revalidation (1 hour).
const MAX_CACHE_AGE_SECS: u64 = 3600;

/// Default DRAM cache size in bytes (256 MiB).
pub const DEFAULT_MEMORY_CACHE_BYTES: usize = 256 * 1024 * 1024;

/// Default filesystem cache size in bytes (10 GiB).
pub const DEFAULT_DISK_CACHE_BYTES: usize = 10 * 1024 * 1024 * 1024;

/// Maximum file size to cache (10 MiB) - larger files bypass cache.
pub const MAX_FILE_SIZE: usize = 10 * 1024 * 1024;

/// Maximum file size to eagerly hydrate during workspace attach (128 KiB).
pub const HYDRATE_SMALL_FILE_BYTES: usize = 128 * 1024;

/// Total bytes admitted per hydration call (default 64 MiB).
pub const HYDRATE_TOTAL_BUDGET_BYTES: usize = 64 * 1024 * 1024;

/// Default concurrent backend fetches during hydration.
pub const HYDRATE_CONCURRENCY: usize = 8;

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
    pub legacy_sqlite_file: PathBuf,
}

impl CachePaths {
    /// Build cache paths namespaced by the (server_url, principal) tuple so
    /// different API keys / tenants on the same Nexus URL never share a
    /// foyer directory. The `principal` argument is the API key (or any
    /// stable per-principal identifier) — the value is hashed, never written
    /// to disk in cleartext, so it doesn't leak credentials. (#4055 R3)
    pub fn for_server(root_dir: &Path, server_url: &str, principal: &str) -> Self {
        let hash = principal_hash(server_url, principal);
        Self {
            foyer_dir: root_dir.join(format!("nexus_{hash:016x}.foyer")),
            sqlite_file: root_dir.join(format!("nexus_{hash:016x}.db")),
            legacy_sqlite_file: root_dir.join(format!("{}.db", legacy_server_filename(server_url))),
        }
    }
}

fn principal_hash(server_url: &str, principal: &str) -> u64 {
    let mut hasher = std::collections::hash_map::DefaultHasher::new();
    server_url.hash(&mut hasher);
    // Domain-separator so concatenation of (url, principal) is unambiguous.
    "|".hash(&mut hasher);
    principal.hash(&mut hasher);
    hasher.finish()
}

fn legacy_server_filename(server_url: &str) -> String {
    server_url
        .chars()
        .map(|c| if c.is_alphanumeric() { c } else { '_' })
        .collect()
}

/// Cache entry for file content.
#[derive(Debug, Clone)]
pub struct CacheEntry {
    pub content: Vec<u8>,
    pub etag: Option<String>,
    pub gen: u64,
}

/// Result of a cache lookup.
#[derive(Debug)]
pub enum CacheLookup {
    /// Cache hit with valid content.
    Hit(CacheEntry),
    /// Cache hit but needs revalidation (has etag).
    NeedsRevalidation { etag: String },
    /// Cache miss.
    Miss,
}

#[derive(Debug, Clone)]
struct CacheRecord {
    content: Vec<u8>,
    etag: Option<String>,
    gen: u64,
    cached_at_secs: u64,
}

impl Code for CacheRecord {
    fn encode(&self, writer: &mut impl std::io::Write) -> foyer::Result<()> {
        self.content.encode(writer)?;
        self.etag.is_some().encode(writer)?;
        if let Some(etag) = &self.etag {
            etag.encode(writer)?;
        }
        self.gen.encode(writer)?;
        self.cached_at_secs.encode(writer)
    }

    fn decode(reader: &mut impl std::io::Read) -> foyer::Result<Self> {
        let content = Vec::<u8>::decode(reader)?;
        let etag = if bool::decode(reader)? {
            Some(String::decode(reader)?)
        } else {
            None
        };
        let gen = u64::decode(reader)?;
        let cached_at_secs = u64::decode(reader)?;
        Ok(Self {
            content,
            etag,
            gen,
            cached_at_secs,
        })
    }

    fn estimated_size(&self) -> usize {
        std::mem::size_of::<u64>() * 2
            + std::mem::size_of::<bool>()
            + std::mem::size_of::<usize>()
            + self.content.len()
            + self
                .etag
                .as_ref()
                .map_or(0, |etag| std::mem::size_of::<usize>() + etag.len())
    }
}

#[derive(Debug, Clone)]
struct CacheMeta {
    etag: Option<String>,
    gen: u64,
    cached_at_secs: u64,
    size: usize,
}

pub struct FileCache {
    cache: HybridCache<String, CacheRecord>,
    runtime: CacheRuntime,
    metadata: Mutex<HashMap<String, CacheMeta>>,
    config: CacheConfig,
    // Exclusive flock holder on the foyer directory's lock file.
    // Held for the lifetime of the FileCache so two daemon processes for
    // the same (server_url, principal) cannot open the same foyer dir
    // concurrently and corrupt each other's writes (#4055 R6).
    _dir_lock: std::fs::File,
}

struct CacheRuntime {
    runtime: Option<tokio::runtime::Runtime>,
}

impl CacheRuntime {
    fn new(runtime: tokio::runtime::Runtime) -> Self {
        Self {
            runtime: Some(runtime),
        }
    }

    fn get(&self) -> &tokio::runtime::Runtime {
        self.runtime
            .as_ref()
            .expect("cache runtime must exist while FileCache is alive")
    }
}

impl Drop for CacheRuntime {
    fn drop(&mut self) {
        let Some(runtime) = self.runtime.take() else {
            return;
        };

        if tokio::runtime::Handle::try_current().is_ok() {
            std::thread::spawn(move || drop(runtime))
                .join()
                .expect("foyer cache runtime drop thread panicked");
        } else {
            drop(runtime);
        }
    }
}

fn block_on_foyer<Fut, T>(runtime: &tokio::runtime::Runtime, future: Fut) -> T
where
    Fut: Future<Output = T> + Send + 'static,
    T: Send + 'static,
{
    let handle = runtime.handle().clone();
    if tokio::runtime::Handle::try_current().is_ok() {
        std::thread::spawn(move || handle.block_on(future))
            .join()
            .expect("foyer cache runtime thread panicked")
    } else {
        runtime.block_on(future)
    }
}

impl FileCache {
    pub fn new(server_url: &str, principal: &str) -> Result<Self> {
        Self::new_with_config(server_url, principal, CacheConfig::default())
    }

    /// `principal` is hashed (with `server_url`) into the foyer directory so
    /// different API keys / tenants on the same Nexus URL never share a
    /// cache. Pass the API key (or any stable per-principal identifier) —
    /// the value is only hashed locally, never written in cleartext. (#4055 R3)
    pub fn new_with_config(
        server_url: &str,
        principal: &str,
        config: CacheConfig,
    ) -> Result<Self> {
        std::fs::create_dir_all(&config.root_dir).with_context(|| {
            format!("failed to create cache root {}", config.root_dir.display())
        })?;

        let paths = CachePaths::for_server(&config.root_dir, server_url, principal);
        migrate_sqlite_file(&paths.sqlite_file);
        if paths.legacy_sqlite_file != paths.sqlite_file {
            migrate_sqlite_file(&paths.legacy_sqlite_file);
        }
        std::fs::create_dir_all(&paths.foyer_dir).with_context(|| {
            format!(
                "failed to create foyer cache dir {}",
                paths.foyer_dir.display()
            )
        })?;

        // Issue #4055 R6: take an exclusive non-blocking flock on a lockfile
        // inside the foyer directory before opening it. Foyer's on-disk
        // format isn't documented as multi-process safe, and concurrent
        // daemons for the same (server_url, principal) would otherwise race
        // on the index files. If another daemon already holds the lock we
        // refuse to open this cache rather than risk corruption — the caller
        // (open_file_cache) treats this as "no cache" and runs uncached.
        let lock_path = paths.foyer_dir.join(".nexus-fuse.lock");
        let dir_lock = std::fs::OpenOptions::new()
            .create(true)
            .truncate(false)
            .read(true)
            .write(true)
            .open(&lock_path)
            .with_context(|| format!("failed to open cache lock {}", lock_path.display()))?;
        // SAFETY: libc::flock on a valid fd is sound; the fd is owned by
        // `dir_lock` and remains valid for the call.
        let lock_rc = unsafe {
            use std::os::unix::io::AsRawFd as _;
            libc::flock(dir_lock.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB)
        };
        if lock_rc != 0 {
            let err = std::io::Error::last_os_error();
            return Err(anyhow!(
                "another nexus-fuse daemon is already using {}: {}",
                paths.foyer_dir.display(),
                err
            ));
        }

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

        let foyer_dir = paths.foyer_dir.clone();
        let disk_bytes = config.disk_bytes;
        let memory_bytes = config.memory_bytes;
        let cache = block_on_foyer(&runtime, async move {
            let device = FsDeviceBuilder::new(&foyer_dir)
                .with_capacity(disk_bytes)
                .build()?;

            let cache = HybridCacheBuilder::new()
                .with_name("nexus-fuse-file-cache")
                .with_flush_on_close(true)
                .memory(memory_bytes)
                .with_weighter(|_key, value: &CacheRecord| value.estimated_size().max(1))
                .storage()
                .with_engine_config(
                    BlockEngineConfig::new(device).with_admission_filter(StorageFilter::new()),
                )
                .build()
                .await?;

            Ok::<HybridCache<String, CacheRecord>, foyer::Error>(cache)
        })?;

        Ok(Self {
            cache,
            runtime: CacheRuntime::new(runtime),
            metadata: Mutex::new(HashMap::new()),
            config,
            _dir_lock: dir_lock,
        })
    }

    fn now() -> u64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_secs())
            .unwrap_or(0)
    }

    fn update_metadata(&self, path: &str, record: &CacheRecord) {
        if let Ok(mut metadata) = self.metadata.lock() {
            metadata.insert(
                path.to_string(),
                CacheMeta {
                    etag: record.etag.clone(),
                    gen: record.gen,
                    cached_at_secs: record.cached_at_secs,
                    size: record.content.len(),
                },
            );
        }
    }

    fn remove_metadata(&self, path: &str) {
        if let Ok(mut metadata) = self.metadata.lock() {
            metadata.remove(path);
        }
    }

    fn read_record(&self, path: &str) -> Option<CacheRecord> {
        if let Some(entry) = self.cache.memory().get(path) {
            let record = entry.value().clone();
            self.update_metadata(path, &record);
            return Some(record);
        }

        let key = path.to_string();
        let cache = self.cache.clone();
        match block_on_foyer(self.runtime.get(), async move { cache.get(&key).await }) {
            Ok(Some(entry)) => {
                let record = entry.value().clone();
                self.update_metadata(path, &record);
                Some(record)
            }
            Ok(None) => {
                self.remove_metadata(path);
                None
            }
            Err(e) => {
                warn!("Foyer cache read failed for {}: {}", path, e);
                None
            }
        }
    }

    pub fn get(&self, path: &str, gen: u64) -> CacheLookup {
        let now = Self::now();
        let meta = self
            .metadata
            .lock()
            .ok()
            .and_then(|metadata| metadata.get(path).cloned());

        if let Some(meta) = meta {
            if meta.gen != gen {
                debug!(
                    "Cache generation mismatch for {} (cached={}, current={})",
                    path, meta.gen, gen
                );
                metrics::record_generation_mismatch();
                self.invalidate(path);
                metrics::record_cache_request("dram", "miss");
                return CacheLookup::Miss;
            }

            let age = now.saturating_sub(meta.cached_at_secs);
            if age >= MAX_CACHE_AGE_SECS {
                if meta.etag.is_some() {
                    let Some(record) = self.read_record(path) else {
                        debug!("Cache stale for {} but backing record is missing", path);
                        metrics::record_cache_request("dram", "miss");
                        return CacheLookup::Miss;
                    };
                    if record.gen != gen {
                        debug!(
                            "Cache generation mismatch for {} (cached={}, current={})",
                            path, record.gen, gen
                        );
                        metrics::record_generation_mismatch();
                        self.invalidate(path);
                        metrics::record_cache_request("dram", "miss");
                        return CacheLookup::Miss;
                    }
                    let Some(etag) = record.etag else {
                        debug!("Cache stale for {} with no etag in backing record", path);
                        metrics::record_cache_request("dram", "miss");
                        return CacheLookup::Miss;
                    };
                    debug!(
                        "Cache stale for {} (age: {}s), needs revalidation",
                        path, age
                    );
                    metrics::record_cache_request("dram", "stale");
                    return CacheLookup::NeedsRevalidation { etag };
                }
                debug!("Cache stale for {} with no etag", path);
                metrics::record_cache_request("dram", "miss");
                return CacheLookup::Miss;
            }
        }

        let Some(record) = self.read_record(path) else {
            metrics::record_cache_request("dram", "miss");
            return CacheLookup::Miss;
        };

        if record.gen != gen {
            debug!(
                "Cache generation mismatch for {} (cached={}, current={})",
                path, record.gen, gen
            );
            metrics::record_generation_mismatch();
            self.invalidate(path);
            metrics::record_cache_request("dram", "miss");
            return CacheLookup::Miss;
        }

        let age = now.saturating_sub(record.cached_at_secs);
        if age < MAX_CACHE_AGE_SECS {
            debug!("Cache hit for {} (age: {}s)", path, age);
            metrics::record_cache_request("dram", "hit");
            return CacheLookup::Hit(CacheEntry {
                content: record.content,
                etag: record.etag,
                gen: record.gen,
            });
        }
        if let Some(etag) = record.etag {
            metrics::record_cache_request("dram", "stale");
            return CacheLookup::NeedsRevalidation { etag };
        }
        metrics::record_cache_request("dram", "miss");
        CacheLookup::Miss
    }

    pub fn get_etag(&self, path: &str) -> Option<String> {
        if let Some(etag) = self
            .metadata
            .lock()
            .ok()
            .and_then(|metadata| metadata.get(path).map(|meta| meta.etag.clone()))
        {
            return etag;
        }

        self.read_record(path).and_then(|record| record.etag)
    }

    pub fn get_stale(&self, path: &str) -> Option<CacheEntry> {
        self.read_record(path).map(|record| CacheEntry {
            content: record.content,
            etag: record.etag,
            gen: record.gen,
        })
    }

    pub fn put(&self, path: &str, content: &[u8], etag: Option<&str>, gen: u64) {
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
            gen,
            cached_at_secs: now,
        };
        self.cache.insert(path.to_string(), record.clone());
        if let Ok(mut metadata) = self.metadata.lock() {
            metadata.insert(
                path.to_string(),
                CacheMeta {
                    etag: record.etag,
                    gen,
                    cached_at_secs: now,
                    size: content.len(),
                },
            );
        }
        self.stats();
    }

    pub fn touch(&self, path: &str) {
        let Some(mut record) = self.read_record(path) else {
            debug!("Cache touch skipped for missing entry {}", path);
            return;
        };

        record.cached_at_secs = Self::now();
        self.cache.insert(path.to_string(), record.clone());
        self.update_metadata(path, &record);
    }

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

    pub fn invalidate(&self, path: &str) {
        self.cache.remove(path);
        if let Ok(mut metadata) = self.metadata.lock() {
            metadata.remove(path);
        }
        self.stats();
        debug!("Invalidated cache for {}", path);
    }

    pub fn stats(&self) -> CacheStats {
        let Ok(metadata) = self.metadata.lock() else {
            metrics::set_cache_bytes_in_use("dram", 0);
            return CacheStats {
                file_count: 0,
                total_size: 0,
            };
        };

        let total_size = metadata.values().map(|meta| meta.size as u64).sum();
        metrics::set_cache_bytes_in_use("dram", total_size);

        CacheStats {
            file_count: metadata.len() as u64,
            total_size,
        }
    }

    #[cfg(test)]
    pub(crate) fn backdate_for_test(&self, path: &str, age_secs: u64) {
        let cached_at_secs = Self::now().saturating_sub(age_secs);
        if let Ok(mut metadata) = self.metadata.lock() {
            if let Some(meta) = metadata.get_mut(path) {
                meta.cached_at_secs = cached_at_secs;
            }
        }

        let Some(mut record) = self.read_record(path) else {
            return;
        };
        record.cached_at_secs = cached_at_secs;
        self.cache.insert(path.to_string(), record.clone());
        self.update_metadata(path, &record);
    }
}

impl Drop for FileCache {
    fn drop(&mut self) {
        let cache = self.cache.clone();
        if let Err(e) = block_on_foyer(self.runtime.get(), async move { cache.close().await }) {
            warn!("Failed to close foyer cache: {}", e);
        }
    }
}

fn migrate_sqlite_file(sqlite_file: &Path) {
    for path in [
        sqlite_file.to_path_buf(),
        sqlite_file.with_extension("db-wal"),
        sqlite_file.with_extension("db-shm"),
    ] {
        if !path.exists() {
            continue;
        }

        match std::fs::remove_file(&path) {
            Ok(()) => info!("Dropped old SQLite cache file {}", path.display()),
            Err(e) => warn!(
                "Failed to delete old SQLite cache file {}: {}",
                path.display(),
                e
            ),
        }
    }
}

/// Cache statistics.
#[derive(Debug)]
pub struct CacheStats {
    pub file_count: u64,
    pub total_size: u64,
}

#[cfg(test)]
mod tests {
    use super::*;

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
        assert!(err
            .to_string()
            .contains("memory cache size must be greater than zero"));

        let root = std::env::temp_dir().join("nexus-fuse-config-test");
        let err = CacheConfig::new(root, DEFAULT_MEMORY_CACHE_BYTES, 0, MAX_FILE_SIZE)
            .expect_err("zero disk tier must be rejected");
        assert!(err
            .to_string()
            .contains("disk cache size must be greater than zero"));
    }

    #[test]
    fn test_cache_paths_are_stable_and_distinct() {
        let root = std::env::temp_dir().join("nexus-fuse-path-test");
        let a = CachePaths::for_server(&root, "http://a:8080", "principal-x");
        let b = CachePaths::for_server(&root, "http://a/8080", "principal-x");

        assert_ne!(a.foyer_dir, b.foyer_dir);
        assert_ne!(a.sqlite_file, b.sqlite_file);
    }

    #[test]
    fn test_cache_dir_exclusive_lock_refuses_second_open() {
        // #4055 R6: a second FileCache::new for the same (url, principal)
        // must fail because the first holds an exclusive flock on the
        // foyer dir's lockfile. Without this, two daemon processes could
        // race on foyer's on-disk format.
        let dir = tempfile::tempdir().unwrap();
        let config = CacheConfig::new(
            dir.path().to_path_buf(),
            4 * 1024 * 1024,
            32 * 1024 * 1024,
            MAX_FILE_SIZE,
        )
        .unwrap();
        let url = "http://lock.test";
        let principal = "alice";
        let _first =
            FileCache::new_with_config(url, principal, config.clone()).expect("first opens");
        let err = FileCache::new_with_config(url, principal, config)
            .err()
            .expect("second open must fail while first holds the lock");
        let msg = err.to_string();
        assert!(
            msg.contains("already using"),
            "expected lock-conflict error, got: {msg}"
        );
    }

    #[test]
    fn test_cache_paths_split_by_principal() {
        // Same URL, different principals → different cache directories.
        // Locks the #4055 R3 fix that prevents cross-tenant cache sharing.
        let root = std::env::temp_dir().join("nexus-fuse-principal-test");
        let url = "http://nx.test";
        let a = CachePaths::for_server(&root, url, "alice");
        let b = CachePaths::for_server(&root, url, "bob");
        assert_ne!(a.foyer_dir, b.foyer_dir);
        assert_ne!(a.sqlite_file, b.sqlite_file);
        assert!(a
            .foyer_dir
            .file_name()
            .unwrap()
            .to_string_lossy()
            .ends_with(".foyer"));
        assert!(a
            .sqlite_file
            .file_name()
            .unwrap()
            .to_string_lossy()
            .ends_with(".db"));
    }

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
        let paths = CachePaths::for_server(dir.path(), "http://migration.test", "test");
        std::fs::write(&paths.sqlite_file, b"old sqlite cache").unwrap();
        std::fs::write(paths.sqlite_file.with_extension("db-wal"), b"old wal").unwrap();
        std::fs::write(paths.sqlite_file.with_extension("db-shm"), b"old shm").unwrap();

        let _cache =
            FileCache::new_with_config("http://migration.test", "test", config).unwrap();

        assert!(!paths.sqlite_file.exists());
        assert!(!paths.sqlite_file.with_extension("db-wal").exists());
        assert!(!paths.sqlite_file.with_extension("db-shm").exists());
        assert!(paths.foyer_dir.exists());
    }

    #[test]
    fn test_legacy_sanitized_sqlite_file_is_deleted_on_startup() {
        let dir = tempfile::tempdir().unwrap();
        let server_url = "http://legacy.example:2026";
        let config = CacheConfig::new(
            dir.path().to_path_buf(),
            4 * 1024 * 1024,
            32 * 1024 * 1024,
            MAX_FILE_SIZE,
        )
        .unwrap();
        let legacy_file =
            CachePaths::for_server(dir.path(), server_url, "test").legacy_sqlite_file;
        std::fs::write(&legacy_file, b"old sqlite cache").unwrap();
        std::fs::write(legacy_file.with_extension("db-wal"), b"old wal").unwrap();
        std::fs::write(legacy_file.with_extension("db-shm"), b"old shm").unwrap();

        let _cache = FileCache::new_with_config(server_url, "test", config).unwrap();

        assert!(!legacy_file.exists());
        assert!(!legacy_file.with_extension("db-wal").exists());
        assert!(!legacy_file.with_extension("db-shm").exists());
    }

    fn test_cache(label: &str) -> FileCache {
        let dir = tempfile::tempdir().unwrap().keep();
        let config = CacheConfig::new(
            dir.join(label),
            4 * 1024 * 1024,
            64 * 1024 * 1024,
            MAX_FILE_SIZE,
        )
        .unwrap();
        FileCache::new_with_config(&format!("http://{label}.test"), "test", config).unwrap()
    }

    fn metric_value(rendered: &str, metric: &str) -> Option<u64> {
        rendered.lines().find_map(|line| {
            line.strip_prefix(metric)
                .and_then(|value| value.trim().parse::<u64>().ok())
        })
    }

    fn assert_metric_at_least(metric: &str, expected: u64) {
        let rendered = crate::metrics::render();
        let actual = metric_value(&rendered, metric).unwrap_or(0);
        assert!(
            actual >= expected,
            "expected {metric}{expected} or greater, got {actual}\n{rendered}"
        );
    }

    #[test]
    fn test_cache_basic() {
        let _guard = crate::metrics::test_guard();
        let cache = test_cache("basic");

        // Miss on empty cache
        assert!(matches!(cache.get("/test.txt", 0), CacheLookup::Miss));

        // Put and get
        cache.put("/test.txt", b"hello world", Some("abc123"), 0);

        match cache.get("/test.txt", 0) {
            CacheLookup::Hit(entry) => {
                assert_eq!(entry.content, b"hello world");
                assert_eq!(entry.etag, Some("abc123".to_string()));
            }
            _ => panic!("Expected cache hit"),
        }

        // Invalidate
        cache.invalidate("/test.txt");
        assert!(matches!(cache.get("/test.txt", 0), CacheLookup::Miss));
    }

    #[test]
    fn test_cache_get_records_hit_miss_and_stale_metrics() {
        let _guard = crate::metrics::test_guard();
        crate::metrics::reset_for_tests();
        let cache = test_cache("metrics");

        assert!(matches!(
            cache.get("/metrics-miss.txt", 0),
            CacheLookup::Miss
        ));
        assert_metric_at_least(
            "nexus_cache_requests_total{tier=\"dram\",result=\"miss\"} ",
            1,
        );

        cache.put("/metrics-hit.txt", b"data", Some("etag-1"), 0);
        assert!(matches!(
            cache.get("/metrics-hit.txt", 0),
            CacheLookup::Hit(_)
        ));
        assert_metric_at_least(
            "nexus_cache_requests_total{tier=\"dram\",result=\"hit\"} ",
            1,
        );

        cache.backdate_for_test("/metrics-hit.txt", MAX_CACHE_AGE_SECS + 1);
        assert!(matches!(
            cache.get("/metrics-hit.txt", 0),
            CacheLookup::NeedsRevalidation { .. }
        ));
        assert_metric_at_least(
            "nexus_cache_requests_total{tier=\"dram\",result=\"stale\"} ",
            1,
        );
    }

    #[test]
    fn test_cache_put_records_bytes_in_use() {
        let _guard = crate::metrics::test_guard();
        crate::metrics::reset_for_tests();
        let cache = test_cache("bytes");

        cache.put("/metrics-bytes.txt", b"data", Some("etag-1"), 0);

        assert_eq!(
            metric_value(
                &crate::metrics::render(),
                "nexus_cache_bytes_in_use{tier=\"dram\"} "
            ),
            Some(4)
        );
    }

    #[test]
    fn test_cache_invalidate_refreshes_bytes_in_use() {
        let _guard = crate::metrics::test_guard();
        crate::metrics::reset_for_tests();
        let cache = test_cache("invalidate-bytes");

        cache.put("/metrics-invalidated.txt", b"data", Some("etag-1"), 0);
        cache.invalidate("/metrics-invalidated.txt");

        assert_eq!(
            metric_value(
                &crate::metrics::render(),
                "nexus_cache_bytes_in_use{tier=\"dram\"} "
            ),
            Some(0)
        );
    }

    #[test]
    fn test_put_without_etag() {
        let cache = test_cache("no-etag");
        cache.put("/no-etag.txt", b"data", None, 0);

        match cache.get("/no-etag.txt", 0) {
            CacheLookup::Hit(entry) => {
                assert_eq!(entry.content, b"data");
                assert_eq!(entry.etag, None);
            }
            _ => panic!("Expected cache hit"),
        }
    }

    #[test]
    fn test_overwrite_entry() {
        let cache = test_cache("overwrite");
        cache.put("/f.txt", b"v1", Some("e1"), 0);
        cache.put("/f.txt", b"v2", Some("e2"), 0);

        match cache.get("/f.txt", 0) {
            CacheLookup::Hit(entry) => {
                assert_eq!(entry.content, b"v2");
                assert_eq!(entry.etag, Some("e2".to_string()));
            }
            _ => panic!("Expected cache hit with updated content"),
        }
    }

    #[test]
    fn test_get_etag() {
        let cache = test_cache("etag");
        assert_eq!(cache.get_etag("/missing.txt"), None);

        cache.put("/e.txt", b"x", Some("etag-42"), 0);
        assert_eq!(cache.get_etag("/e.txt"), Some("etag-42".to_string()));

        cache.put("/no-e.txt", b"x", None, 0);
        assert_eq!(cache.get_etag("/no-e.txt"), None);
    }

    #[test]
    fn test_get_etag_reads_foyer_when_metadata_missing() {
        let cache = test_cache("etag-fallback");
        cache.put("/e.txt", b"x", Some("etag-42"), 0);
        cache.metadata.lock().unwrap().clear();

        assert_eq!(cache.get_etag("/e.txt"), Some("etag-42".to_string()));
    }

    #[test]
    fn test_get_rebuilds_metadata_from_foyer_record() {
        let cache = test_cache("metadata-rebuild");
        cache.put("/e.txt", b"data", Some("etag-42"), 0);
        cache.metadata.lock().unwrap().clear();

        assert!(matches!(cache.get("/e.txt", 0), CacheLookup::Hit(_)));
        assert_eq!(cache.get_etag("/e.txt"), Some("etag-42".to_string()));
    }

    #[test]
    fn test_touch_refreshes_stale_entry() {
        let cache = test_cache("touch");
        cache.put("/t.txt", b"data", Some("e1"), 0);
        cache.backdate_for_test("/t.txt", MAX_CACHE_AGE_SECS + 1);

        cache.touch("/t.txt");

        match cache.get("/t.txt", 0) {
            CacheLookup::Hit(entry) => {
                assert_eq!(entry.content, b"data");
                assert_eq!(entry.etag, Some("e1".to_string()));
            }
            _ => panic!("Expected cache hit after touch"),
        }
    }

    #[test]
    fn test_stale_entry_with_etag_needs_revalidation() {
        let cache = test_cache("stale-etag");
        cache.put("/stale.txt", b"data", Some("stale-etag"), 0);
        cache.backdate_for_test("/stale.txt", MAX_CACHE_AGE_SECS + 1);

        match cache.get("/stale.txt", 0) {
            CacheLookup::NeedsRevalidation { etag } => assert_eq!(etag, "stale-etag"),
            _ => panic!("Expected stale entry with etag to require revalidation"),
        }

        let stale = cache.get_stale("/stale.txt").expect("stale entry exists");
        assert_eq!(stale.content, b"data");
        assert_eq!(stale.etag, Some("stale-etag".to_string()));
    }

    #[test]
    fn test_stale_metadata_without_foyer_record_is_miss() {
        let cache = test_cache("stale-metadata-missing-record");
        cache.put("/missing-record.txt", b"data", Some("stale-etag"), 0);

        cache.cache.remove("/missing-record.txt");
        {
            let mut metadata = cache.metadata.lock().unwrap();
            let meta = metadata
                .get_mut("/missing-record.txt")
                .expect("metadata should exist after put");
            meta.cached_at_secs = FileCache::now().saturating_sub(MAX_CACHE_AGE_SECS + 1);
        }

        assert!(matches!(
            cache.get("/missing-record.txt", 0),
            CacheLookup::Miss
        ));
        assert_eq!(cache.get_etag("/missing-record.txt"), None);
    }

    #[test]
    fn test_stale_entry_without_etag_is_miss() {
        let cache = test_cache("stale-no-etag");
        cache.put("/stale.txt", b"data", None, 0);
        cache.backdate_for_test("/stale.txt", MAX_CACHE_AGE_SECS + 1);

        assert!(matches!(cache.get("/stale.txt", 0), CacheLookup::Miss));
    }

    #[test]
    fn test_large_file_not_cached() {
        let cache = test_cache("large");
        let big = vec![0u8; MAX_FILE_SIZE + 1];
        cache.put("/big.bin", &big, Some("e1"), 0);

        assert!(matches!(cache.get("/big.bin", 0), CacheLookup::Miss));
    }

    #[test]
    fn test_max_size_file_cached() {
        let cache = test_cache("maxsize");
        let data = vec![42u8; MAX_FILE_SIZE];
        cache.put("/exact.bin", &data, Some("e1"), 0);

        match cache.get("/exact.bin", 0) {
            CacheLookup::Hit(entry) => assert_eq!(entry.content.len(), MAX_FILE_SIZE),
            _ => panic!("Expected cache hit for file at exact max size"),
        }
    }

    #[test]
    fn test_cache_stats() {
        let _guard = crate::metrics::test_guard();
        let cache = test_cache("stats");

        cache.put("/stat-a.txt", b"aaa", Some("e1"), 0);
        cache.put("/stat-b.txt", b"bb", Some("e2"), 0);

        let stats = cache.stats();
        assert_eq!(stats.file_count, 2);
        assert_eq!(stats.total_size, 5);
    }

    #[test]
    fn test_generation_mismatch_invalidates_cache() {
        let _guard = crate::metrics::test_guard();
        crate::metrics::reset_for_tests();
        let cache = test_cache("generation-mismatch");
        cache.put("/gen.txt", b"v1", Some("e1"), 1);

        assert!(matches!(cache.get("/gen.txt", 1), CacheLookup::Hit(_)));
        assert!(matches!(cache.get("/gen.txt", 2), CacheLookup::Miss));
        assert!(matches!(cache.get("/gen.txt", 2), CacheLookup::Miss));
        assert_metric_at_least("nexus_generation_mismatch_total ", 1);
    }

    #[test]
    fn test_generation_stored_with_entry() {
        let cache = test_cache("generation-stored");
        cache.put("/gen.txt", b"v1", Some("e1"), 9);

        match cache.get("/gen.txt", 9) {
            CacheLookup::Hit(entry) => assert_eq!(entry.gen, 9),
            other => panic!("expected hit, got {other:?}"),
        }
    }

    #[test]
    fn test_stats_after_invalidation() {
        let cache = test_cache("stats-inv");
        cache.put("/x.txt", b"12345", None, 0);

        let stats = cache.stats();
        assert_eq!(stats.file_count, 1);
        assert_eq!(stats.total_size, 5);

        cache.invalidate("/x.txt");

        let stats = cache.stats();
        assert_eq!(stats.file_count, 0);
        assert_eq!(stats.total_size, 0);
    }

    #[test]
    fn test_multiple_independent_paths() {
        let cache = test_cache("multi");
        cache.put("/a.txt", b"aaa", Some("ea"), 0);
        cache.put("/b.txt", b"bbb", Some("eb"), 0);
        cache.put("/c.txt", b"ccc", Some("ec"), 0);

        // Invalidate only /b.txt
        cache.invalidate("/b.txt");

        assert!(matches!(cache.get("/a.txt", 0), CacheLookup::Hit(_)));
        assert!(matches!(cache.get("/b.txt", 0), CacheLookup::Miss));
        assert!(matches!(cache.get("/c.txt", 0), CacheLookup::Hit(_)));
    }

    #[test]
    fn test_empty_content_cached() {
        let cache = test_cache("empty");
        cache.put("/empty.txt", b"", Some("e0"), 0);

        match cache.get("/empty.txt", 0) {
            CacheLookup::Hit(entry) => {
                assert!(entry.content.is_empty());
                assert_eq!(entry.etag, Some("e0".to_string()));
            }
            _ => panic!("Expected cache hit for empty file"),
        }
    }

    #[test]
    fn test_binary_content_preserved() {
        let cache = test_cache("binary");
        let binary: Vec<u8> = (0..=255).collect();
        cache.put("/bin.dat", &binary, Some("ebin"), 0);

        match cache.get("/bin.dat", 0) {
            CacheLookup::Hit(entry) => assert_eq!(entry.content, binary),
            _ => panic!("Expected cache hit for binary content"),
        }
    }

    #[test]
    fn test_concurrent_access() {
        use std::sync::Arc;
        use std::thread;

        let cache = Arc::new(test_cache("concurrent"));

        let handles: Vec<_> = (0..8)
            .map(|i| {
                let cache = Arc::clone(&cache);
                thread::spawn(move || {
                    let path = format!("/thread-{}.txt", i);
                    let content = format!("data-{}", i);
                    cache.put(&path, content.as_bytes(), Some(&format!("e{}", i)), 0);
                    let _ = cache.get(&path, 0);
                    let _ = cache.stats();
                })
            })
            .collect();

        for h in handles {
            h.join().unwrap();
        }

        // All 8 entries should be present
        let stats = cache.stats();
        assert_eq!(stats.file_count, 8);
    }

    #[test]
    fn test_invalidate_nonexistent_is_noop() {
        let cache = test_cache("inv-noop");
        cache.invalidate("/does-not-exist.txt");
        assert!(matches!(
            cache.get("/does-not-exist.txt", 0),
            CacheLookup::Miss
        ));
    }

    #[tokio::test]
    async fn test_cache_calls_inside_tokio_runtime_do_not_panic() {
        let cache = test_cache("inside-runtime");
        cache.put("/runtime.txt", b"data", Some("runtime-etag"), 0);

        match cache.get("/runtime.txt", 0) {
            CacheLookup::Hit(entry) => assert_eq!(entry.content, b"data"),
            _ => panic!("Expected cache hit inside tokio runtime"),
        }

        cache.backdate_for_test("/runtime.txt", MAX_CACHE_AGE_SECS + 1);
        assert!(matches!(
            cache.get("/runtime.txt", 0),
            CacheLookup::NeedsRevalidation { .. }
        ));

        let stale = cache
            .get_stale("/runtime.txt")
            .expect("stale entry should be readable inside runtime");
        assert_eq!(stale.etag, Some("runtime-etag".to_string()));

        cache.touch("/runtime.txt");
        assert!(matches!(cache.get("/runtime.txt", 0), CacheLookup::Hit(_)));
    }

    #[tokio::test]
    async fn test_cache_drop_inside_tokio_runtime_flushes_to_disk() {
        let dir = tempfile::tempdir().unwrap().keep();
        let config = CacheConfig::new(
            dir.join("flush-on-drop"),
            4 * 1024 * 1024,
            64 * 1024 * 1024,
            MAX_FILE_SIZE,
        )
        .unwrap();

        {
            let cache = FileCache::new_with_config(
                "http://flush-on-drop.test",
                "test",
                config.clone(),
            )
            .unwrap();
            cache.put("/persisted.txt", b"persisted", Some("persisted-etag"), 0);
        }

        let cache =
            FileCache::new_with_config("http://flush-on-drop.test", "test", config).unwrap();
        match cache.get("/persisted.txt", 0) {
            CacheLookup::Hit(entry) => {
                assert_eq!(entry.content, b"persisted");
                assert_eq!(entry.etag, Some("persisted-etag".to_string()));
            }
            _ => panic!("Expected cache hit after drop and reopen"),
        }
    }

    #[test]
    fn test_hydrate_constants_have_expected_values() {
        assert_eq!(HYDRATE_SMALL_FILE_BYTES, 128 * 1024);
        assert_eq!(HYDRATE_TOTAL_BUDGET_BYTES, 64 * 1024 * 1024);
        assert_eq!(HYDRATE_CONCURRENCY, 8);
    }

    #[test]
    fn test_is_warm_returns_false_for_unknown_path() {
        let cache = test_cache("is_warm_unknown");
        assert!(!cache.is_warm("/nope.txt"));
    }

    #[test]
    fn test_is_warm_returns_true_after_put() {
        let cache = test_cache("is_warm_after_put");
        cache.put("/a.txt", b"hello", Some("etag-1"), 0);
        assert!(cache.is_warm("/a.txt"));
    }

    #[test]
    fn test_is_warm_returns_false_for_aged_entry() {
        let cache = test_cache("is_warm_aged");
        cache.put("/old.txt", b"x", Some("etag-old"), 0);
        {
            let mut metadata = cache.metadata.lock().unwrap();
            let meta = metadata.get_mut("/old.txt").expect("entry should exist");
            let now = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_secs();
            meta.cached_at_secs = now.saturating_sub(MAX_CACHE_AGE_SECS + 1);
        }
        assert!(!cache.is_warm("/old.txt"));
    }
}
