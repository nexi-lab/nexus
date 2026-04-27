//! DCache — lock-free in-memory dentry cache for kernel hot-path.
//!
//! Stores a hot-path projection of FileMetadata fields needed by sys_read/sys_write:
//! backend_name, physical_path, size, content_id, version, entry_type, zone_id.
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
#[allow(dead_code)]
pub(crate) const DT_EXTERNAL_STORAGE: u8 = 5;
pub(crate) const DT_LINK: u8 = 6;

/// Errors returned by `DCache::resolve_link` when a DT_LINK lookup violates
/// the one-hop invariant. Callers translate these into syscall errno values
/// (`ELOOP`, `EINVAL`) or 4xx gRPC statuses.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum LinkResolveError {
    /// `link_target` field absent on a DT_LINK entry — bad metadata write.
    MissingTarget,
    /// `link_target` points back at the link itself.
    SelfLoop,
    /// `link_target` resolves to another DT_LINK — chained links forbidden.
    Chained,
}

/// Hot-path projection of FileMetadata.
#[derive(Clone, Debug, Default)]
#[allow(dead_code)]
pub struct CachedEntry {
    pub size: u64,
    pub content_id: Option<String>,
    pub version: u32,
    pub entry_type: u8,
    pub zone_id: Option<String>,
    pub mime_type: Option<String>,
    pub created_at_ms: Option<i64>,
    pub modified_at_ms: Option<i64>,
    pub last_writer_address: Option<String>,
    /// DT_LINK target — see `meta_store::FileMetadata::link_target`.
    pub link_target: Option<String>,
}

impl From<&crate::meta_store::FileMetadata> for CachedEntry {
    fn from(m: &crate::meta_store::FileMetadata) -> Self {
        Self {
            size: m.size,
            content_id: m.content_id.clone(),
            version: m.version,
            entry_type: m.entry_type,
            zone_id: m.zone_id.clone(),
            mime_type: m.mime_type.clone(),
            created_at_ms: m.created_at_ms,
            modified_at_ms: m.modified_at_ms,
            last_writer_address: m.last_writer_address.clone(),
            link_target: m.link_target.clone(),
        }
    }
}

/// Dentry cache — owned directly by Kernel.
///
/// All methods take `&self` (DashMap provides interior mutability).
pub struct DCache {
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
    pub fn get_entry(&self, path: &str) -> Option<CachedEntry> {
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

    // ── Write methods (called via Kernel proxy #[pymethods] +
    //    federation provider's `wire_mount` apply-side coherence) ──

    /// Insert or update a cache entry.
    pub fn put(&self, path: &str, entry: CachedEntry) {
        self.cache.insert(path.to_string(), entry);
    }

    /// Evict a single path. Returns true if the entry existed.
    pub fn evict(&self, path: &str) -> bool {
        self.cache.remove(path).is_some()
    }

    /// Evict all entries whose path starts with the given prefix.
    /// Returns the number of entries evicted.
    pub fn evict_prefix(&self, prefix: &str) -> usize {
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
    pub(crate) fn list_children(&self, prefix: &str) -> Vec<(String, u8, Option<String>)> {
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
                Some((
                    rest.to_string(),
                    entry.value().entry_type,
                    entry.value().zone_id.clone(),
                ))
            })
            .collect()
    }

    /// Resolve a path through DT_LINK indirection. Returns the path that
    /// callers should use for the actual sys_* operation.
    ///
    /// Semantics (one hop, matching Linux symlink resolution depth=1):
    ///   * If `path` has no entry or a non-DT_LINK entry → returns `path` unchanged.
    ///   * If `path` is a DT_LINK with `link_target` → returns the target.
    ///   * If the target is itself a DT_LINK → returns `Err(LinkResolveError::Chained)`.
    ///   * Self-loop (`link_target == path`) is rejected at write time, so this
    ///     method does not encounter it; defensive ELOOP returned if seen.
    ///
    /// Hot-path callers (`sys_read`, `sys_write`) call this after dcache
    /// lookup detects DT_LINK; non-link paths short-circuit so the cost is
    /// `~0` for the common case. Wiring into each sys_* call site is the
    /// next follow-up commit; the helper lives now so the wiring change
    /// stays small and reviewable.
    #[allow(dead_code)] // wired into sys_* in the follow-up commit
    pub(crate) fn resolve_link(&self, path: &str) -> Result<String, LinkResolveError> {
        let entry = match self.cache.get(path) {
            Some(e) => e,
            None => return Ok(path.to_string()),
        };
        if entry.entry_type != DT_LINK {
            return Ok(path.to_string());
        }
        let target = entry
            .link_target
            .clone()
            .ok_or(LinkResolveError::MissingTarget)?;
        drop(entry);
        if target == path {
            return Err(LinkResolveError::SelfLoop);
        }
        if let Some(next) = self.cache.get(&target) {
            if next.entry_type == DT_LINK {
                return Err(LinkResolveError::Chained);
            }
        }
        Ok(target)
    }

    /// Get hot-path tuple: (entry_type, last_writer_address).
    /// Updates hit/miss counters.
    pub(crate) fn get_hot(&self, path: &str) -> Option<(u8, Option<String>)> {
        match self.cache.get(path) {
            Some(entry) => {
                self.hits.fetch_add(1, Ordering::Relaxed);
                let e = entry.value();
                Some((e.entry_type, e.last_writer_address.clone()))
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
                size: 1024,
                content_id: Some("abc123".to_string()),
                version: 1,
                entry_type: DT_REG,
                zone_id: Some("root".to_string()),
                mime_type: Some("text/markdown".to_string()),
                created_at_ms: None,
                modified_at_ms: None,
                last_writer_address: None,
                link_target: None,
            },
        );

        let entry = dc.get_entry("/docs/readme.md").unwrap();
        assert_eq!(entry.size, 1024);
        assert_eq!(entry.content_id.as_deref(), Some("abc123"));
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
                size: 0,
                content_id: None,
                version: 1,
                entry_type: DT_DIR,
                zone_id: None,
                mime_type: None,
                created_at_ms: None,
                modified_at_ms: None,
                last_writer_address: None,
                link_target: None,
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
                size: 0,
                content_id: None,
                version: 1,
                entry_type: DT_REG,
                zone_id: None,
                mime_type: None,
                created_at_ms: None,
                modified_at_ms: None,
                last_writer_address: None,
                link_target: None,
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
                size: 0,
                content_id: None,
                version: 1,
                entry_type: DT_REG,
                zone_id: None,
                mime_type: None,
                created_at_ms: None,
                modified_at_ms: None,
                last_writer_address: None,
                link_target: None,
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
                    size: 0,
                    content_id: None,
                    version: 1,
                    entry_type: DT_REG,
                    zone_id: None,
                    mime_type: None,
                    created_at_ms: None,
                    modified_at_ms: None,
                    last_writer_address: None,
                    link_target: None,
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
                size: 100,
                content_id: Some("hash".to_string()),
                version: 1,
                entry_type: DT_REG,
                zone_id: None,
                mime_type: None,
                created_at_ms: None,
                modified_at_ms: None,
                last_writer_address: None,
                link_target: None,
            },
        );
        let (et, last_writer) = dc.get_hot("/file").unwrap();
        assert_eq!(et, DT_REG);
        assert!(last_writer.is_none());
        assert!(dc.get_hot("/missing").is_none());
    }

    #[test]
    fn test_stats() {
        let dc = make_dcache();
        dc.put(
            "/a",
            CachedEntry {
                size: 0,
                content_id: None,
                version: 1,
                entry_type: DT_REG,
                zone_id: None,
                mime_type: None,
                created_at_ms: None,
                modified_at_ms: None,
                last_writer_address: None,
                link_target: None,
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
                size: 0,
                content_id: None,
                version: 1,
                entry_type: DT_REG,
                zone_id: None,
                mime_type: None,
                created_at_ms: None,
                modified_at_ms: None,
                last_writer_address: None,
                link_target: None,
            },
        );
        dc.get_entry("/a"); // hit
        dc.clear();
        assert_eq!(dc.len(), 0);
        let (hits, misses, _) = dc.stats();
        assert_eq!(hits, 0);
        assert_eq!(misses, 0);
    }

    fn make_link_entry(target: &str) -> CachedEntry {
        CachedEntry {
            size: 0,
            etag: None,
            version: 1,
            entry_type: DT_LINK,
            zone_id: Some("root".to_string()),
            mime_type: None,
            created_at_ms: None,
            modified_at_ms: None,
            last_writer_address: None,
            link_target: Some(target.to_string()),
        }
    }

    fn make_reg_entry() -> CachedEntry {
        CachedEntry {
            size: 0,
            etag: None,
            version: 1,
            entry_type: DT_REG,
            zone_id: Some("root".to_string()),
            mime_type: None,
            created_at_ms: None,
            modified_at_ms: None,
            last_writer_address: None,
            link_target: None,
        }
    }

    #[test]
    fn test_resolve_link_passthrough_for_non_link() {
        let dc = make_dcache();
        dc.put("/regular", make_reg_entry());
        assert_eq!(dc.resolve_link("/regular").unwrap(), "/regular");
        // Missing path also passes through.
        assert_eq!(dc.resolve_link("/no/such").unwrap(), "/no/such");
    }

    #[test]
    fn test_resolve_link_one_hop() {
        let dc = make_dcache();
        dc.put("/proc/p1/agent", make_link_entry("/agents/scode-standard"));
        dc.put("/agents/scode-standard", make_reg_entry());
        assert_eq!(
            dc.resolve_link("/proc/p1/agent").unwrap(),
            "/agents/scode-standard"
        );
    }

    #[test]
    fn test_resolve_link_chained_rejected() {
        let dc = make_dcache();
        dc.put("/a", make_link_entry("/b"));
        dc.put("/b", make_link_entry("/c"));
        let err = dc.resolve_link("/a").unwrap_err();
        assert_eq!(err, LinkResolveError::Chained);
    }

    #[test]
    fn test_resolve_link_self_loop_rejected() {
        let dc = make_dcache();
        dc.put("/loop", make_link_entry("/loop"));
        let err = dc.resolve_link("/loop").unwrap_err();
        assert_eq!(err, LinkResolveError::SelfLoop);
    }

    #[test]
    fn test_resolve_link_missing_target_metadata() {
        let dc = make_dcache();
        let mut entry = make_link_entry("/x");
        entry.link_target = None; // bad write — DT_LINK without target
        dc.put("/broken", entry);
        let err = dc.resolve_link("/broken").unwrap_err();
        assert_eq!(err, LinkResolveError::MissingTarget);
    }
}
