use hashlink::LinkedHashMap;
use parking_lot::{Mutex, MutexGuard, RwLock};
use std::collections::hash_map::DefaultHasher;
use std::hash::{Hash, Hasher};
use std::time::{Duration, Instant};

const FILL_LOCK_STRIPES: usize = 64;
const DEFAULT_MAX_BYTES: usize = 512 * 1024 * 1024;

#[derive(Clone, Debug, Eq, Hash, PartialEq)]
pub struct FileCacheKey {
    pub scope_id: String,
    pub path: String,
    pub namespace: String,
}

impl FileCacheKey {
    pub fn new(
        scope_id: impl Into<String>,
        path: impl Into<String>,
        namespace: impl Into<String>,
    ) -> Self {
        Self {
            scope_id: scope_id.into(),
            path: path.into(),
            namespace: namespace.into(),
        }
    }
}

#[derive(Clone)]
struct FileEntry {
    content: Vec<u8>,
    fingerprint: Option<String>,
    expires_at: Option<Instant>,
}

struct CacheInner {
    entries: LinkedHashMap<FileCacheKey, FileEntry>,
    total_bytes: usize,
}

pub struct FileCache {
    inner: RwLock<CacheInner>,
    max_bytes: usize,
    fill_locks: Vec<Mutex<()>>,
}

impl Default for FileCache {
    fn default() -> Self {
        Self::with_max_bytes(DEFAULT_MAX_BYTES)
    }
}

impl FileCache {
    pub fn with_max_bytes(max_bytes: usize) -> Self {
        Self {
            inner: RwLock::new(CacheInner {
                entries: LinkedHashMap::new(),
                total_bytes: 0,
            }),
            max_bytes,
            fill_locks: (0..FILL_LOCK_STRIPES).map(|_| Mutex::new(())).collect(),
        }
    }

    pub fn total_bytes(&self) -> usize {
        self.inner.read().total_bytes
    }

    pub fn get(&self, key: &FileCacheKey, expected_fingerprint: Option<&str>) -> Option<Vec<u8>> {
        let now = Instant::now();
        let mut inner = self.inner.write();
        let entry = inner.entries.get(key)?;
        if let Some(expires_at) = entry.expires_at {
            if expires_at <= now {
                let removed = inner.entries.remove(key).expect("entry just observed");
                inner.total_bytes -= removed.content.len();
                return None;
            }
        }
        let fp_match = match expected_fingerprint {
            Some(expected) => entry.fingerprint.as_deref() == Some(expected),
            None => entry.expires_at.is_some(),
        };
        if !fp_match {
            return None;
        }
        let content = entry.content.clone();
        inner.entries.to_back(key);
        Some(content)
    }

    pub fn put(
        &self,
        key: FileCacheKey,
        content: Vec<u8>,
        fingerprint: Option<String>,
        ttl: Option<Duration>,
    ) {
        let size = content.len();
        if size > self.max_bytes {
            tracing::warn!(
                target: "kernel::cache::file_cache",
                key = ?key,
                size,
                max = self.max_bytes,
                "rejecting oversize entry",
            );
            let mut inner = self.inner.write();
            if let Some(existing) = inner.entries.remove(&key) {
                inner.total_bytes -= existing.content.len();
            }
            return;
        }
        let expires_at = ttl.map(|ttl| Instant::now() + ttl);
        let mut inner = self.inner.write();
        if let Some(existing) = inner.entries.remove(&key) {
            inner.total_bytes -= existing.content.len();
        }
        inner.entries.insert(
            key,
            FileEntry {
                content,
                fingerprint,
                expires_at,
            },
        );
        inner.total_bytes += size;
        while inner.total_bytes > self.max_bytes {
            let (_, removed) = inner
                .entries
                .pop_front()
                .expect("non-empty cache with over-cap bytes");
            inner.total_bytes -= removed.content.len();
        }
    }

    pub fn lock(&self, key: &FileCacheKey) -> FileCacheFillGuard<'_> {
        let stripe = fill_lock_stripe(key);
        FileCacheFillGuard {
            _guard: self.fill_locks[stripe].lock(),
        }
    }

    /// Evict every entry from the cache.
    ///
    /// Primarily useful for benchmarks and tests that need cache-cold
    /// reads without destroying the `Kernel` instance.
    pub fn clear(&self) {
        let mut inner = self.inner.write();
        inner.entries.clear();
        inner.total_bytes = 0;
    }

    pub fn invalidate_path(&self, scope_id: &str, path: &str, namespace: &str) {
        let mut inner = self.inner.write();
        let to_remove: Vec<FileCacheKey> = inner
            .entries
            .iter()
            .filter(|(k, _)| k.scope_id == scope_id && k.path == path && k.namespace == namespace)
            .map(|(k, _)| k.clone())
            .collect();
        for key in to_remove {
            if let Some(removed) = inner.entries.remove(&key) {
                inner.total_bytes -= removed.content.len();
            }
        }
    }
}

fn fill_lock_stripe(key: &FileCacheKey) -> usize {
    let mut hasher = DefaultHasher::new();
    key.hash(&mut hasher);
    hasher.finish() as usize % FILL_LOCK_STRIPES
}

pub struct FileCacheFillGuard<'a> {
    _guard: MutexGuard<'a, ()>,
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Arc;
    use std::thread;

    #[test]
    fn rejects_mismatched_fingerprint() {
        let cache = FileCache::default();
        let key = FileCacheKey::new("root", "/mnt/foo.txt", "raw");
        cache.put(key.clone(), b"old".to_vec(), Some("etag:old".into()), None);
        assert_eq!(cache.get(&key, Some("etag:new")), None);
    }

    #[test]
    fn singleflight_allows_one_fill() {
        let cache = Arc::new(FileCache::default());
        let key = FileCacheKey::new("root", "/mnt/foo.txt", "raw");
        let fills = Arc::new(AtomicUsize::new(0));
        thread::scope(|scope| {
            for _ in 0..100 {
                let cache = Arc::clone(&cache);
                let key = key.clone();
                let fills = Arc::clone(&fills);
                scope.spawn(move || {
                    let _guard = cache.lock(&key);
                    if cache.get(&key, Some("etag:1")).is_none() {
                        fills.fetch_add(1, Ordering::SeqCst);
                        cache.put(
                            key.clone(),
                            b"payload".to_vec(),
                            Some("etag:1".into()),
                            None,
                        );
                    }
                    assert_eq!(cache.get(&key, Some("etag:1")), Some(b"payload".to_vec()));
                });
            }
        });
        assert_eq!(fills.load(Ordering::SeqCst), 1);
    }

    #[test]
    fn evicts_lru_when_over_byte_cap() {
        let cache = FileCache::with_max_bytes(300);
        for path in ["/a", "/b", "/c"] {
            cache.put(
                FileCacheKey::new("root", path, "raw"),
                vec![0u8; 100],
                Some(format!("fp:{path}")),
                None,
            );
        }
        cache.put(
            FileCacheKey::new("root", "/d", "raw"),
            vec![0u8; 100],
            Some("fp:/d".into()),
            None,
        );
        assert_eq!(
            cache.get(&FileCacheKey::new("root", "/a", "raw"), Some("fp:/a")),
            None,
        );
        assert_eq!(
            cache.get(&FileCacheKey::new("root", "/d", "raw"), Some("fp:/d")),
            Some(vec![0u8; 100]),
        );
        assert_eq!(cache.total_bytes(), 300);
    }

    #[test]
    fn get_touches_lru() {
        let cache = FileCache::with_max_bytes(300);
        for path in ["/a", "/b", "/c"] {
            cache.put(
                FileCacheKey::new("root", path, "raw"),
                vec![0u8; 100],
                Some(format!("fp:{path}")),
                None,
            );
        }
        let _ = cache.get(&FileCacheKey::new("root", "/a", "raw"), Some("fp:/a"));
        cache.put(
            FileCacheKey::new("root", "/d", "raw"),
            vec![0u8; 100],
            Some("fp:/d".into()),
            None,
        );
        assert!(cache
            .get(&FileCacheKey::new("root", "/a", "raw"), Some("fp:/a"))
            .is_some());
        assert!(cache
            .get(&FileCacheKey::new("root", "/b", "raw"), Some("fp:/b"))
            .is_none());
    }

    #[test]
    fn oversize_entry_rejected() {
        let cache = FileCache::with_max_bytes(100);
        cache.put(
            FileCacheKey::new("root", "/big", "raw"),
            vec![0u8; 500],
            Some("fp:big".into()),
            None,
        );
        assert_eq!(
            cache.get(&FileCacheKey::new("root", "/big", "raw"), Some("fp:big")),
            None,
        );
        assert_eq!(cache.total_bytes(), 0);
    }

    #[test]
    fn total_bytes_decrements_on_invalidate() {
        let cache = FileCache::with_max_bytes(1024);
        cache.put(
            FileCacheKey::new("root", "/a", "raw"),
            vec![0u8; 100],
            Some("fp:a".into()),
            None,
        );
        cache.invalidate_path("root", "/a", "raw");
        assert_eq!(cache.total_bytes(), 0);
    }

    #[test]
    fn oversize_replacement_drops_prior_entry() {
        // A rejected oversized put must not leave stale bytes behind.
        let cache = FileCache::with_max_bytes(100);
        let key = FileCacheKey::new("root", "/x", "raw");
        cache.put(
            key.clone(),
            b"old".to_vec(),
            Some("fp:old".into()),
            Some(Duration::from_secs(60)),
        );
        assert_eq!(cache.get(&key, Some("fp:old")), Some(b"old".to_vec()));
        cache.put(
            key.clone(),
            vec![0u8; 500],
            Some("fp:new".into()),
            Some(Duration::from_secs(60)),
        );
        assert_eq!(cache.get(&key, Some("fp:old")), None);
        assert_eq!(cache.get(&key, Some("fp:new")), None);
        assert_eq!(cache.total_bytes(), 0);
    }
}
