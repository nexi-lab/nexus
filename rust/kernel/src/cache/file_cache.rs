use parking_lot::{Mutex, MutexGuard, RwLock};
use std::collections::hash_map::DefaultHasher;
use std::collections::HashMap;
use std::hash::{Hash, Hasher};
use std::time::{Duration, Instant};

const FILL_LOCK_STRIPES: usize = 64;

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

pub struct FileCache {
    entries: RwLock<HashMap<FileCacheKey, FileEntry>>,
    fill_locks: Vec<Mutex<()>>,
}

impl Default for FileCache {
    fn default() -> Self {
        Self {
            entries: RwLock::new(HashMap::new()),
            fill_locks: (0..FILL_LOCK_STRIPES).map(|_| Mutex::new(())).collect(),
        }
    }
}

impl FileCache {
    pub fn get(&self, key: &FileCacheKey, expected_fingerprint: Option<&str>) -> Option<Vec<u8>> {
        let now = Instant::now();
        {
            let entries = self.entries.read();
            let entry = entries.get(key)?;
            if let Some(expires_at) = entry.expires_at {
                if expires_at <= now {
                    drop(entries);
                    self.entries.write().remove(key);
                    return None;
                }
            }
            if let Some(expected) = expected_fingerprint {
                return (entry.fingerprint.as_deref() == Some(expected))
                    .then(|| entry.content.clone());
            }
            entry.expires_at?;
            Some(entry.content.clone())
        }
    }

    pub fn put(
        &self,
        key: FileCacheKey,
        content: Vec<u8>,
        fingerprint: Option<String>,
        ttl: Option<Duration>,
    ) {
        let expires_at = ttl.map(|ttl| Instant::now() + ttl);
        self.entries.write().insert(
            key,
            FileEntry {
                content,
                fingerprint,
                expires_at,
            },
        );
    }

    pub fn lock(&self, key: &FileCacheKey) -> FileCacheFillGuard<'_> {
        let stripe = fill_lock_stripe(key);
        FileCacheFillGuard {
            _guard: self.fill_locks[stripe].lock(),
        }
    }

    pub fn invalidate_path(&self, scope_id: &str, path: &str, namespace: &str) {
        self.entries.write().retain(|key, _| {
            !(key.scope_id == scope_id && key.path == path && key.namespace == namespace)
        });
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
            for _ in 0..20 {
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
}
