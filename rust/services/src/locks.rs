//! Per-key writer-serialization locks.
//!
//! [`KeyLockMap`] hands out an `Arc<Mutex<()>>` per key so concurrent
//! writers to the SAME key serialize while writers to DIFFERENT keys
//! proceed in parallel. Readers do not touch the map.
//!
//! **When to reach for this.** Any writer whose shape is
//! "open → mutate in-memory copy → save whole snapshot" against a
//! per-key on-disk sidecar. Atomic-rename alone does NOT prevent
//! lost writes there: two racing writers each open their own copy,
//! each mutate it, each save — one's save silently overwrites the
//! other's changes. Wrap the entire open→mutate→save transaction
//! under `let _write = map.get(&key).lock();`.
//!
//! The inner [`parking_lot::Mutex`] must be held on blocking-pool
//! threads only. Never hold it across `.await`.
//!
//! The map itself has no eviction: it grows with the number of
//! distinct keys ever locked. Acceptable for bounded key sets
//! (zones, mailboxes, tenants). If a caller needs eviction, layer
//! it on top rather than teaching this type about lifecycle.
//!
//! # Example
//!
//! ```
//! use services::locks::KeyLockMap;
//! use std::sync::Arc;
//!
//! let map: KeyLockMap<String> = KeyLockMap::new();
//! let lock = map.get(&"zone-a".to_string());
//! let _guard = lock.lock();
//! // ... open sidecar, mutate, save ...
//! ```

use parking_lot::Mutex;
use std::collections::HashMap;
use std::hash::Hash;
use std::sync::Arc;

/// Map from key → per-key writer lock.
pub struct KeyLockMap<K: Eq + Hash + Clone> {
    inner: Mutex<HashMap<K, Arc<Mutex<()>>>>,
}

impl<K: Eq + Hash + Clone> Default for KeyLockMap<K> {
    fn default() -> Self {
        Self {
            inner: Mutex::new(HashMap::new()),
        }
    }
}

impl<K: Eq + Hash + Clone> KeyLockMap<K> {
    pub fn new() -> Self {
        Self::default()
    }

    /// Return the writer lock for `key`, creating it on first access.
    ///
    /// Calls with the same `key` return `Arc`s to the same `Mutex`.
    pub fn get(&self, key: &K) -> Arc<Mutex<()>> {
        Arc::clone(self.inner.lock().entry(key.clone()).or_default())
    }

    /// How many distinct keys have ever been locked (map cardinality).
    /// Diagnostic helper — the map has no eviction.
    pub fn tracked_keys(&self) -> usize {
        self.inner.lock().len()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU32, Ordering};
    use std::sync::Barrier;
    use std::thread;
    use std::time::{Duration, Instant};

    #[test]
    fn get_same_key_returns_same_lock() {
        let map: KeyLockMap<String> = KeyLockMap::new();
        let a = map.get(&"z".to_string());
        let b = map.get(&"z".to_string());
        assert!(Arc::ptr_eq(&a, &b));
        assert_eq!(map.tracked_keys(), 1);
    }

    #[test]
    fn distinct_keys_get_distinct_locks() {
        let map: KeyLockMap<String> = KeyLockMap::new();
        let a = map.get(&"z1".to_string());
        let b = map.get(&"z2".to_string());
        assert!(!Arc::ptr_eq(&a, &b));
        assert_eq!(map.tracked_keys(), 2);
    }

    #[test]
    fn writers_on_same_key_serialize() {
        let map: Arc<KeyLockMap<String>> = Arc::new(KeyLockMap::new());
        let counter = Arc::new(AtomicU32::new(0));
        let max_concurrent = Arc::new(AtomicU32::new(0));
        let barrier = Arc::new(Barrier::new(4));

        let mut handles = vec![];
        for _ in 0..4 {
            let map = Arc::clone(&map);
            let counter = Arc::clone(&counter);
            let max_concurrent = Arc::clone(&max_concurrent);
            let barrier = Arc::clone(&barrier);
            handles.push(thread::spawn(move || {
                barrier.wait();
                let lock = map.get(&"same".to_string());
                let _g = lock.lock();
                let now = counter.fetch_add(1, Ordering::SeqCst) + 1;
                max_concurrent.fetch_max(now, Ordering::SeqCst);
                thread::sleep(Duration::from_millis(20));
                counter.fetch_sub(1, Ordering::SeqCst);
            }));
        }
        for h in handles {
            h.join().unwrap();
        }
        assert_eq!(
            max_concurrent.load(Ordering::SeqCst),
            1,
            "same-key writers must serialize"
        );
    }

    #[test]
    fn writers_on_distinct_keys_run_concurrently() {
        let map: Arc<KeyLockMap<String>> = Arc::new(KeyLockMap::new());
        let barrier = Arc::new(Barrier::new(4));

        let start = Instant::now();
        let mut handles = vec![];
        for i in 0..4 {
            let map = Arc::clone(&map);
            let barrier = Arc::clone(&barrier);
            handles.push(thread::spawn(move || {
                let key = format!("k{i}");
                let lock = map.get(&key);
                barrier.wait();
                let _g = lock.lock();
                thread::sleep(Duration::from_millis(50));
            }));
        }
        for h in handles {
            h.join().unwrap();
        }
        let elapsed = start.elapsed();
        // Four 50ms sleeps in parallel finish well under the serial
        // 200ms; give a wide margin for CI jitter.
        assert!(
            elapsed < Duration::from_millis(180),
            "distinct-key writers should run concurrently (took {elapsed:?})"
        );
    }
}
