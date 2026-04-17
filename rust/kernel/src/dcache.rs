//! DCache — lock-free in-memory dentry cache for kernel hot-path.
//!
//! Stores a hot-path projection of FileMetadata fields needed by sys_read/sys_write:
//! backend_name, physical_path, size, etag, version, entry_type, zone_id.
//!
//! Design:
//!   - DashMap<String, CachedEntry> for lock-free concurrent reads (~30ns).
//!   - Owned directly by Kernel (no Arc wrapper needed).
//!   - No TTL/LRU — write-through, authoritative (single-process, single-writer).
//!
//! Issue #1868: Kernel owns DCache directly. RustDCache wrapper removed.

use dashmap::DashMap;
use std::sync::atomic::{AtomicU64, Ordering};

// ── Entry type constants (mirror proto/nexus/core/metadata.proto) ───────────
pub(crate) const DT_REG: u8 = 0;
pub(crate) const DT_DIR: u8 = 1;
pub(crate) const DT_MOUNT: u8 = 2;
pub(crate) const DT_PIPE: u8 = 3;
pub(crate) const DT_STREAM: u8 = 4;

/// Hot-path projection of FileMetadata.
#[derive(Clone, Debug, Default)]
#[allow(dead_code)]
pub(crate) struct CachedEntry {
    pub(crate) backend_name: String,
    pub(crate) physical_path: String,
    pub(crate) size: u64,
    pub(crate) etag: Option<String>,
    pub(crate) version: u32,
    pub(crate) entry_type: u8,
    pub(crate) zone_id: Option<String>,
    pub(crate) mime_type: Option<String>,
    pub(crate) created_at_ms: Option<i64>,
    pub(crate) modified_at_ms: Option<i64>,
}

/// Dentry cache — owned directly by Kernel.
///
/// All methods take `&self` (DashMap provides interior mutability).
pub(crate) struct DCache {
    cache: DashMap<String, CachedEntry>,
    hits: AtomicU64,
    misses: AtomicU64,
}

impl DCache {
    pub(crate) fn new() -> Self {
        Self {
            cache: DashMap::new(),
            hits: AtomicU64::new(0),
            misses: AtomicU64::new(0),
        }
    }

    // ── Read methods (used by Kernel plan_*/sys_*) ────────────────────────

    /// Get a full CachedEntry clone (updates hit/miss counters).
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

    /// Check if path exists without updating hit/miss counters.
    pub(crate) fn contains(&self, path: &str) -> bool {
        self.cache.contains_key(path)
    }

    // ── Write methods (called via Kernel proxy #[pymethods]) ─────────────

    /// Insert or update a cache entry.
    pub(crate) fn put(&self, path: &str, entry: CachedEntry) {
        self.cache.insert(path.to_string(), entry);
    }

    /// Evict a single path. Returns true if the entry existed.
    pub(crate) fn evict(&self, path: &str) -> bool {
        self.cache.remove(path).is_some()
    }

    /// Evict all entries whose path starts with the given prefix.
    /// Returns the number of entries evicted.
    pub(crate) fn evict_prefix(&self, prefix: &str) -> usize {
        let keys: Vec<String> = self
            .cache
            .iter()
            .filter(|entry| entry.key().starts_with(prefix))
            .map(|entry| entry.key().clone())
            .collect();
        let count = keys.len();
        for k in keys {
            self.cache.remove(&k);
        }
        count
    }

    /// List immediate children under a prefix path.
    /// Returns Vec of (child_name, entry_type).
    /// Only returns direct children (no nested paths).
    pub(crate) fn list_children(&self, prefix: &str) -> Vec<(String, u8)> {
        self.cache
            .iter()
            .filter_map(|entry| {
                let path = entry.key();
                if !path.starts_with(prefix) || path.len() <= prefix.len() {
                    return None;
                }
                let rest = &path[prefix.len()..];
                // Only immediate children (no '/' in the remainder)
                if rest.contains('/') {
                    return None;
                }
                Some((rest.to_string(), entry.value().entry_type))
            })
            .collect()
    }

    /// Get hot-path tuple: (backend_name, physical_path, entry_type).
    /// Updates hit/miss counters.
    pub(crate) fn get_hot(&self, path: &str) -> Option<(String, String, u8)> {
        match self.cache.get(path) {
            Some(entry) => {
                self.hits.fetch_add(1, Ordering::Relaxed);
                let e = entry.value();
                Some((
                    e.backend_name.clone(),
                    e.physical_path.clone(),
                    e.entry_type,
                ))
            }
            None => {
                self.misses.fetch_add(1, Ordering::Relaxed);
                None
            }
        }
    }

    /// Return cache statistics: (hits, misses, size).
    pub(crate) fn stats(&self) -> (u64, u64, usize) {
        (
            self.hits.load(Ordering::Relaxed),
            self.misses.load(Ordering::Relaxed),
            self.cache.len(),
        )
    }

    /// Clear all entries and reset counters.
    pub(crate) fn clear(&self) {
        self.cache.clear();
        self.hits.store(0, Ordering::Relaxed);
        self.misses.store(0, Ordering::Relaxed);
    }

    /// Number of entries.
    pub(crate) fn len(&self) -> usize {
        self.cache.len()
    }
}

// ── Rust-only unit tests ────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    fn make_dcache() -> DCache {
        DCache::new()
    }

    #[test]
    fn test_put_and_get_entry() {
        let dc = make_dcache();
        dc.put(
            "/docs/readme.md",
            CachedEntry {
                backend_name: "local".to_string(),
                physical_path: "/data/readme.md".to_string(),
                size: 1024,
                etag: Some("abc123".to_string()),
                version: 1,
                entry_type: DT_REG,
                zone_id: Some("root".to_string()),
                mime_type: Some("text/markdown".to_string()),
                created_at_ms: None,
                modified_at_ms: None,
            },
        );

        let entry = dc.get_entry("/docs/readme.md").unwrap();
        assert_eq!(entry.backend_name, "local");
        assert_eq!(entry.physical_path, "/data/readme.md");
        assert_eq!(entry.size, 1024);
        assert_eq!(entry.etag.as_deref(), Some("abc123"));
        assert_eq!(entry.version, 1);
        assert_eq!(entry.entry_type, DT_REG);
        assert_eq!(entry.zone_id.as_deref(), Some("root"));
        assert_eq!(entry.mime_type.as_deref(), Some("text/markdown"));
    }

    #[test]
    fn test_get_entry_miss() {
        let dc = make_dcache();
        assert!(dc.get_entry("/nonexistent").is_none());
        assert_eq!(dc.misses.load(Ordering::Relaxed), 1);
    }

    #[test]
    fn test_contains() {
        let dc = make_dcache();
        dc.put(
            "/a",
            CachedEntry {
                backend_name: "local".to_string(),
                physical_path: "/a".to_string(),
                size: 0,
                etag: None,
                version: 1,
                entry_type: DT_DIR,
                zone_id: None,
                mime_type: None,
                created_at_ms: None,
                modified_at_ms: None,
            },
        );
        assert!(dc.contains("/a"));
        assert!(!dc.contains("/b"));
    }

    #[test]
    fn test_hit_miss_counters() {
        let dc = make_dcache();
        dc.put(
            "/hit",
            CachedEntry {
                backend_name: "local".to_string(),
                physical_path: "/hit".to_string(),
                size: 0,
                etag: None,
                version: 1,
                entry_type: DT_REG,
                zone_id: None,
                mime_type: None,
                created_at_ms: None,
                modified_at_ms: None,
            },
        );

        // 2 hits
        dc.get_entry("/hit");
        dc.get_entry("/hit");
        // 1 miss
        dc.get_entry("/miss");

        assert_eq!(dc.hits.load(Ordering::Relaxed), 2);
        assert_eq!(dc.misses.load(Ordering::Relaxed), 1);
    }

    #[test]
    fn test_entry_types() {
        assert_eq!(DT_REG, 0);
        assert_eq!(DT_DIR, 1);
        assert_eq!(DT_PIPE, 3);
        assert_eq!(DT_STREAM, 4);
    }

    #[test]
    fn test_evict() {
        let dc = make_dcache();
        dc.put(
            "/tmp",
            CachedEntry {
                backend_name: "local".to_string(),
                physical_path: "/tmp".to_string(),
                size: 0,
                etag: None,
                version: 1,
                entry_type: DT_REG,
                zone_id: None,
                mime_type: None,
                created_at_ms: None,
                modified_at_ms: None,
            },
        );
        assert!(dc.evict("/tmp"));
        assert!(!dc.evict("/tmp"));
        assert!(!dc.contains("/tmp"));
    }

    #[test]
    fn test_evict_prefix() {
        let dc = make_dcache();
        let paths = ["/docs/a.md", "/docs/b.md", "/src/main.rs"];
        for p in &paths {
            dc.put(
                p,
                CachedEntry {
                    backend_name: "local".to_string(),
                    physical_path: p.to_string(),
                    size: 0,
                    etag: None,
                    version: 1,
                    entry_type: DT_REG,
                    zone_id: None,
                    mime_type: None,
                    created_at_ms: None,
                    modified_at_ms: None,
                },
            );
        }

        let count = dc.evict_prefix("/docs/");
        assert_eq!(count, 2);
        assert_eq!(dc.len(), 1);
        assert!(dc.contains("/src/main.rs"));
    }

    #[test]
    fn test_get_hot() {
        let dc = make_dcache();
        dc.put(
            "/file",
            CachedEntry {
                backend_name: "local".to_string(),
                physical_path: "/data/file".to_string(),
                size: 100,
                etag: Some("hash".to_string()),
                version: 1,
                entry_type: DT_REG,
                zone_id: None,
                mime_type: None,
                created_at_ms: None,
                modified_at_ms: None,
            },
        );
        let (bn, pp, et) = dc.get_hot("/file").unwrap();
        assert_eq!(bn, "local");
        assert_eq!(pp, "/data/file");
        assert_eq!(et, DT_REG);
        assert!(dc.get_hot("/missing").is_none());
    }

    #[test]
    fn test_stats() {
        let dc = make_dcache();
        dc.put(
            "/a",
            CachedEntry {
                backend_name: "local".to_string(),
                physical_path: "/a".to_string(),
                size: 0,
                etag: None,
                version: 1,
                entry_type: DT_REG,
                zone_id: None,
                mime_type: None,
                created_at_ms: None,
                modified_at_ms: None,
            },
        );
        dc.get_entry("/a"); // hit
        dc.get_entry("/b"); // miss
        let (hits, misses, size) = dc.stats();
        assert_eq!(hits, 1);
        assert_eq!(misses, 1);
        assert_eq!(size, 1);
    }

    #[test]
    fn test_clear() {
        let dc = make_dcache();
        dc.put(
            "/a",
            CachedEntry {
                backend_name: "local".to_string(),
                physical_path: "/a".to_string(),
                size: 0,
                etag: None,
                version: 1,
                entry_type: DT_REG,
                zone_id: None,
                mime_type: None,
                created_at_ms: None,
                modified_at_ms: None,
            },
        );
        dc.get_entry("/a"); // hit
        dc.clear();
        assert_eq!(dc.len(), 0);
        let (hits, misses, _) = dc.stats();
        assert_eq!(hits, 0);
        assert_eq!(misses, 0);
    }
}
