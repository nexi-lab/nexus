//! RustDCache — lock-free in-memory dentry cache for MetastoreABC hot-path.
//!
//! Stores a hot-path projection of FileMetadata fields needed by sys_read/sys_write:
//! backend_name, physical_path, size, etag, version, entry_type, zone_id.
//!
//! The Python dcache (dict[str, FileMetadata]) is retained for non-hot-path callers
//! that need full FileMetadata objects (sys_stat, list, etc.).  RustDCache is dual-written
//! alongside the Python dict, and is the source of truth for the Rust SyscallEngine (#1817).
//!
//! Design:
//!   - DashMap<String, CachedEntry> for lock-free concurrent reads (~30ns).
//!   - Arc<RustDCacheInner> enables zero-cost sharing with SyscallEngine (#1817).
//!   - No TTL/LRU — write-through, authoritative (single-process, single-writer).

use dashmap::DashMap;
use pyo3::prelude::*;
use pyo3::types::PyDict;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

// ── Entry type constants (mirror proto/nexus/core/metadata.proto) ───────────
// Used by #[cfg(test)] and reserved for SyscallEngine (#1817).
#[allow(dead_code)]
pub(crate) const DT_REG: u8 = 0;
#[allow(dead_code)]
pub(crate) const DT_DIR: u8 = 1;
#[allow(dead_code)]
pub(crate) const DT_MOUNT: u8 = 2;
#[allow(dead_code)]
pub(crate) const DT_PIPE: u8 = 3;
#[allow(dead_code)]
pub(crate) const DT_STREAM: u8 = 4;
#[allow(dead_code)]
pub(crate) const DT_EXTERNAL: u8 = 5;

/// Hot-path projection of FileMetadata.
#[derive(Clone, Debug)]
pub(crate) struct CachedEntry {
    pub(crate) backend_name: String,
    pub(crate) physical_path: String,
    pub(crate) size: u64,
    pub(crate) etag: Option<String>,
    pub(crate) version: u32,
    pub(crate) entry_type: u8,
    pub(crate) zone_id: Option<String>,
}

/// Inner state shared via Arc with SyscallEngine (#1817).
pub(crate) struct RustDCacheInner {
    cache: DashMap<String, CachedEntry>,
    hits: AtomicU64,
    misses: AtomicU64,
}

#[allow(dead_code)] // pub(crate) API reserved for SyscallEngine (#1817)
impl RustDCacheInner {
    fn new() -> Self {
        Self {
            cache: DashMap::new(),
            hits: AtomicU64::new(0),
            misses: AtomicU64::new(0),
        }
    }

    /// Get a full CachedEntry clone (pub(crate) for SyscallEngine).
    pub(crate) fn get_entry(&self, path: &str) -> Option<CachedEntry> {
        match self.cache.get(path) {
            Some(entry) => {
                self.hits.fetch_add(1, Ordering::Relaxed);
                Some(entry.value().clone())
            }
            None => {
                self.misses.fetch_add(1, Ordering::Relaxed);
                None
            }
        }
    }

    /// Get just the entry_type (pub(crate) for SyscallEngine).
    pub(crate) fn get_entry_type(&self, path: &str) -> Option<u8> {
        self.cache.get(path).map(|e| e.value().entry_type)
    }

    /// Get just the etag (pub(crate) for SyscallEngine).
    pub(crate) fn get_etag(&self, path: &str) -> Option<Option<String>> {
        self.cache.get(path).map(|e| e.value().etag.clone())
    }

    /// Check if path exists without updating hit/miss counters.
    pub(crate) fn contains(&self, path: &str) -> bool {
        self.cache.contains_key(path)
    }
}

/// Python-facing dentry cache backed by DashMap.
///
/// Mirrors the Python ``_dcache: dict[str, FileMetadata]`` in MetastoreABC,
/// storing only the hot-path fields needed by sys_read/sys_write.
#[pyclass]
pub struct RustDCache {
    pub(crate) inner: Arc<RustDCacheInner>,
}

#[pymethods]
impl RustDCache {
    #[new]
    fn new() -> Self {
        Self {
            inner: Arc::new(RustDCacheInner::new()),
        }
    }

    /// Insert or update a cache entry.
    #[pyo3(signature = (path, backend_name, physical_path, size, entry_type, version=1, etag=None, zone_id=None))]
    #[allow(clippy::too_many_arguments)]
    fn put(
        &self,
        path: &str,
        backend_name: &str,
        physical_path: &str,
        size: u64,
        entry_type: u8,
        version: u32,
        etag: Option<&str>,
        zone_id: Option<&str>,
    ) {
        let entry = CachedEntry {
            backend_name: backend_name.to_string(),
            physical_path: physical_path.to_string(),
            size,
            etag: etag.map(|s| s.to_string()),
            version,
            entry_type,
            zone_id: zone_id.map(|s| s.to_string()),
        };
        self.inner.cache.insert(path.to_string(), entry);
    }

    /// Get hot-path tuple: (backend_name, physical_path, entry_type).
    ///
    /// Returns None on miss.  This is the fast path for sys_read routing.
    fn get(&self, path: &str) -> Option<(String, String, u8)> {
        match self.inner.cache.get(path) {
            Some(entry) => {
                self.inner.hits.fetch_add(1, Ordering::Relaxed);
                let e = entry.value();
                Some((
                    e.backend_name.clone(),
                    e.physical_path.clone(),
                    e.entry_type,
                ))
            }
            None => {
                self.inner.misses.fetch_add(1, Ordering::Relaxed);
                None
            }
        }
    }

    /// Get full entry as dict (for Python callers needing all fields).
    fn get_full(&self, py: Python<'_>, path: &str) -> PyResult<Option<Py<PyAny>>> {
        match self.inner.cache.get(path) {
            Some(entry) => {
                self.inner.hits.fetch_add(1, Ordering::Relaxed);
                let e = entry.value();
                let dict = PyDict::new(py);
                dict.set_item("backend_name", &e.backend_name)?;
                dict.set_item("physical_path", &e.physical_path)?;
                dict.set_item("size", e.size)?;
                dict.set_item("etag", e.etag.as_deref())?;
                dict.set_item("version", e.version)?;
                dict.set_item("entry_type", e.entry_type)?;
                dict.set_item("zone_id", e.zone_id.as_deref())?;
                Ok(Some(dict.into()))
            }
            None => {
                self.inner.misses.fetch_add(1, Ordering::Relaxed);
                Ok(None)
            }
        }
    }

    /// Evict a single path. Returns True if the entry existed.
    fn evict(&self, path: &str) -> bool {
        self.inner.cache.remove(path).is_some()
    }

    /// Evict all entries whose path starts with the given prefix.
    ///
    /// Used by mount/unmount to invalidate stale cross-zone entries.
    /// Returns the number of entries evicted.
    fn evict_prefix(&self, prefix: &str) -> usize {
        let keys: Vec<String> = self
            .inner
            .cache
            .iter()
            .filter(|entry| entry.key().starts_with(prefix))
            .map(|entry| entry.key().clone())
            .collect();
        let count = keys.len();
        for k in keys {
            self.inner.cache.remove(&k);
        }
        count
    }

    /// Check if path exists in cache (no hit/miss counting).
    fn contains(&self, path: &str) -> bool {
        self.inner.cache.contains_key(path)
    }

    /// Return cache statistics as a dict.
    fn stats(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let hits = self.inner.hits.load(Ordering::Relaxed);
        let misses = self.inner.misses.load(Ordering::Relaxed);
        let total = hits + misses;
        let hit_rate = if total > 0 {
            hits as f64 / total as f64
        } else {
            0.0
        };

        let dict = PyDict::new(py);
        dict.set_item("hits", hits)?;
        dict.set_item("misses", misses)?;
        dict.set_item("size", self.inner.cache.len())?;
        dict.set_item("hit_rate", hit_rate)?;
        Ok(dict.into())
    }

    /// Clear all entries and reset counters.
    fn clear(&self) {
        self.inner.cache.clear();
        self.inner.hits.store(0, Ordering::Relaxed);
        self.inner.misses.store(0, Ordering::Relaxed);
    }

    fn __len__(&self) -> usize {
        self.inner.cache.len()
    }

    fn __repr__(&self) -> String {
        format!(
            "RustDCache(size={}, hits={}, misses={})",
            self.inner.cache.len(),
            self.inner.hits.load(Ordering::Relaxed),
            self.inner.misses.load(Ordering::Relaxed),
        )
    }
}

// ── Rust-only unit tests ────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    fn make_inner() -> RustDCacheInner {
        RustDCacheInner::new()
    }

    #[test]
    fn test_put_and_get_entry() {
        let inner = make_inner();
        inner.cache.insert(
            "/docs/readme.md".to_string(),
            CachedEntry {
                backend_name: "local".to_string(),
                physical_path: "/data/readme.md".to_string(),
                size: 1024,
                etag: Some("abc123".to_string()),
                version: 1,
                entry_type: DT_REG,
                zone_id: Some("root".to_string()),
            },
        );

        let entry = inner.get_entry("/docs/readme.md").unwrap();
        assert_eq!(entry.backend_name, "local");
        assert_eq!(entry.physical_path, "/data/readme.md");
        assert_eq!(entry.size, 1024);
        assert_eq!(entry.etag.as_deref(), Some("abc123"));
        assert_eq!(entry.version, 1);
        assert_eq!(entry.entry_type, DT_REG);
        assert_eq!(entry.zone_id.as_deref(), Some("root"));
    }

    #[test]
    fn test_get_entry_miss() {
        let inner = make_inner();
        assert!(inner.get_entry("/nonexistent").is_none());
        assert_eq!(inner.misses.load(Ordering::Relaxed), 1);
    }

    #[test]
    fn test_get_entry_type() {
        let inner = make_inner();
        inner.cache.insert(
            "/mnt/remote".to_string(),
            CachedEntry {
                backend_name: "gcs".to_string(),
                physical_path: "/bucket/remote".to_string(),
                size: 0,
                etag: None,
                version: 1,
                entry_type: DT_MOUNT,
                zone_id: None,
            },
        );
        assert_eq!(inner.get_entry_type("/mnt/remote"), Some(DT_MOUNT));
        assert_eq!(inner.get_entry_type("/nonexistent"), None);
    }

    #[test]
    fn test_get_etag() {
        let inner = make_inner();
        inner.cache.insert(
            "/file.txt".to_string(),
            CachedEntry {
                backend_name: "s3".to_string(),
                physical_path: "/bucket/file.txt".to_string(),
                size: 512,
                etag: Some("hash456".to_string()),
                version: 2,
                entry_type: DT_REG,
                zone_id: None,
            },
        );
        assert_eq!(
            inner.get_etag("/file.txt"),
            Some(Some("hash456".to_string()))
        );
        assert_eq!(inner.get_etag("/missing"), None);
    }

    #[test]
    fn test_contains() {
        let inner = make_inner();
        inner.cache.insert(
            "/a".to_string(),
            CachedEntry {
                backend_name: "local".to_string(),
                physical_path: "/a".to_string(),
                size: 0,
                etag: None,
                version: 1,
                entry_type: DT_DIR,
                zone_id: None,
            },
        );
        assert!(inner.contains("/a"));
        assert!(!inner.contains("/b"));
    }

    #[test]
    fn test_hit_miss_counters() {
        let inner = make_inner();
        inner.cache.insert(
            "/hit".to_string(),
            CachedEntry {
                backend_name: "local".to_string(),
                physical_path: "/hit".to_string(),
                size: 0,
                etag: None,
                version: 1,
                entry_type: DT_REG,
                zone_id: None,
            },
        );

        // 2 hits
        inner.get_entry("/hit");
        inner.get_entry("/hit");
        // 1 miss
        inner.get_entry("/miss");

        assert_eq!(inner.hits.load(Ordering::Relaxed), 2);
        assert_eq!(inner.misses.load(Ordering::Relaxed), 1);
    }

    #[test]
    fn test_entry_types() {
        // Verify constants match proto values
        assert_eq!(DT_REG, 0);
        assert_eq!(DT_DIR, 1);
        assert_eq!(DT_MOUNT, 2);
        assert_eq!(DT_PIPE, 3);
        assert_eq!(DT_STREAM, 4);
        assert_eq!(DT_EXTERNAL, 5);
    }

    #[test]
    fn test_evict_prefix_via_dashmap() {
        let inner = make_inner();
        let paths = ["/docs/a.md", "/docs/b.md", "/src/main.rs"];
        for p in &paths {
            inner.cache.insert(
                p.to_string(),
                CachedEntry {
                    backend_name: "local".to_string(),
                    physical_path: p.to_string(),
                    size: 0,
                    etag: None,
                    version: 1,
                    entry_type: DT_REG,
                    zone_id: None,
                },
            );
        }

        // Evict /docs/ prefix
        let keys: Vec<String> = inner
            .cache
            .iter()
            .filter(|e| e.key().starts_with("/docs/"))
            .map(|e| e.key().clone())
            .collect();
        for k in &keys {
            inner.cache.remove(k);
        }

        assert_eq!(keys.len(), 2);
        assert_eq!(inner.cache.len(), 1);
        assert!(inner.contains("/src/main.rs"));
    }
}
