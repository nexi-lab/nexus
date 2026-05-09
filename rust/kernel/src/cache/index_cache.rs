use parking_lot::{Mutex, RwLock};
use std::collections::HashMap;
use std::time::{Duration, Instant};

#[derive(Clone, Debug, Eq, Hash, PartialEq)]
pub enum IndexKind {
    Stat,
    Listing,
    Negative,
}

#[derive(Clone, Debug, Eq, Hash, PartialEq)]
pub struct IndexCacheKey {
    pub scope_id: String,
    pub path: String,
    pub kind: IndexKind,
}

impl IndexCacheKey {
    pub fn new(scope_id: impl Into<String>, path: impl Into<String>, kind: IndexKind) -> Self {
        Self {
            scope_id: scope_id.into(),
            path: path.into(),
            kind,
        }
    }
}

#[derive(Clone)]
struct IndexEntry {
    listing: Vec<(String, u8)>,
    expires_at: Instant,
}

pub struct IndexCache {
    entries: RwLock<HashMap<IndexCacheKey, IndexEntry>>,
    test_now: Mutex<Option<Instant>>,
}

impl Default for IndexCache {
    fn default() -> Self {
        Self {
            entries: RwLock::new(HashMap::new()),
            test_now: Mutex::new(None),
        }
    }
}

impl IndexCache {
    pub fn new_for_tests(now: Instant) -> Self {
        Self {
            entries: RwLock::new(HashMap::new()),
            test_now: Mutex::new(Some(now)),
        }
    }

    pub fn set_now_for_tests(&self, now: Instant) {
        *self.test_now.lock() = Some(now);
    }

    fn now(&self) -> Instant {
        self.test_now.lock().unwrap_or_else(Instant::now)
    }

    pub fn get_listing(&self, key: &IndexCacheKey) -> Option<Vec<(String, u8)>> {
        let now = self.now();
        {
            let entries = self.entries.read();
            let entry = entries.get(key)?;
            if entry.expires_at > now {
                return Some(entry.listing.clone());
            }
        }
        self.entries.write().remove(key);
        None
    }

    pub fn put_listing(&self, key: IndexCacheKey, listing: Vec<(String, u8)>, ttl: Duration) {
        self.entries.write().insert(
            key,
            IndexEntry {
                listing,
                expires_at: self.now() + ttl,
            },
        );
    }

    pub fn invalidate_parent_listing(&self, scope_id: &str, path: &str) {
        let parent = parent_path(path);
        let key = IndexCacheKey::new(scope_id, parent, IndexKind::Listing);
        self.entries.write().remove(&key);
    }
}

pub fn ttl_for_backend(_backend_id: &str) -> Duration {
    Duration::from_secs(60)
}

fn parent_path(path: &str) -> String {
    let trimmed = path.trim_end_matches('/');
    match trimmed.rfind('/') {
        Some(0) | None => "/".to_string(),
        Some(index) => trimmed[..index].to_string(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{Duration, Instant};

    #[test]
    fn expires_listing_entry_after_ttl() {
        let now = Instant::now();
        let cache = IndexCache::new_for_tests(now);
        let key = IndexCacheKey::new("root", "/a/b", IndexKind::Listing);
        cache.put_listing(
            key.clone(),
            vec![("a.txt".into(), 1)],
            Duration::from_secs(1),
        );
        assert_eq!(cache.get_listing(&key), Some(vec![("a.txt".into(), 1)]));
        cache.set_now_for_tests(now + Duration::from_secs(2));
        assert_eq!(cache.get_listing(&key), None);
    }
}
