//! FileWatcher — Rust-native file change notification (inotify equivalent).
//!
//! Kernel primitive for inotify-like file change notification.
//! Two roles:
//!   1. MutationObserver: dispatched by ThreadPool, matches event paths
//!      against registered patterns, notifies waiting threads via Condvar.
//!   2. Wait API: `wait_for_event(pattern, timeout)` blocks on Condvar
//!      until a matching event arrives. Called from Python (GIL released).
//!
//! Zero Py<PyAny>. Safe Drop. Pure Rust.

use crate::dispatch::{FileEvent, MutationObserver};
use globset::{Glob, GlobMatcher};
use parking_lot::{Condvar, Mutex};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Duration;

/// A registered file watch pattern.
struct WatchEntry {
    id: u64,
    #[allow(dead_code)]
    pattern: String,
    matcher: GlobMatcher,
}

/// A pending waiter blocked on Condvar.
struct Waiter {
    id: u64,
    matcher: GlobMatcher,
    event: Mutex<Option<FileEvent>>,
    condvar: Condvar,
}

/// FileWatcher — Rust-native inotify equivalent.
///
/// Registered as MutationObserver on Kernel. dispatch_observers calls
/// on_mutation from ThreadPool → match patterns → notify Condvar.
/// Python calls wait_for_event (GIL released) → blocks on Condvar.
pub(crate) struct FileWatcher {
    watches: parking_lot::RwLock<Vec<WatchEntry>>,
    waiters: Mutex<Vec<Arc<Waiter>>>,
    next_id: AtomicU64,
}

impl FileWatcher {
    pub(crate) fn new() -> Self {
        Self {
            watches: parking_lot::RwLock::new(Vec::new()),
            waiters: Mutex::new(Vec::new()),
            next_id: AtomicU64::new(1),
        }
    }

    /// Register a glob pattern watch. Returns unique watch ID.
    pub(crate) fn register(&self, pattern: &str) -> u64 {
        let id = self.next_id.fetch_add(1, Ordering::Relaxed);
        let matcher = Glob::new(pattern)
            .unwrap_or_else(|_| Glob::new(&globset::escape(pattern)).unwrap())
            .compile_matcher();
        self.watches.write().push(WatchEntry {
            id,
            pattern: pattern.to_string(),
            matcher,
        });
        id
    }

    /// Unregister a watch by ID. Returns true if found.
    pub(crate) fn unregister(&self, watch_id: u64) -> bool {
        let mut watches = self.watches.write();
        if let Some(pos) = watches.iter().position(|w| w.id == watch_id) {
            watches.swap_remove(pos);
            true
        } else {
            false
        }
    }

    /// Match a path against all registered patterns.
    pub(crate) fn match_path(&self, path: &str) -> Vec<u64> {
        let watches = self.watches.read();
        watches
            .iter()
            .filter(|w| w.matcher.is_match(path))
            .map(|w| w.id)
            .collect()
    }

    /// Number of registered watches.
    #[allow(dead_code)]
    pub(crate) fn len(&self) -> usize {
        self.watches.read().len()
    }

    /// Block until a matching event arrives or timeout expires.
    /// Creates a waiter, blocks on Condvar, returns the event.
    /// Returns None on timeout.
    pub(crate) fn wait_for_event(&self, pattern: &str, timeout_ms: u64) -> Option<FileEvent> {
        let id = self.next_id.fetch_add(1, Ordering::Relaxed);
        let matcher = Glob::new(pattern)
            .unwrap_or_else(|_| Glob::new(&globset::escape(pattern)).unwrap())
            .compile_matcher();
        let waiter = Arc::new(Waiter {
            id,
            matcher,
            event: Mutex::new(None),
            condvar: Condvar::new(),
        });

        // Register waiter
        self.waiters.lock().push(waiter.clone());

        // Block on Condvar
        let mut slot = waiter.event.lock();
        if slot.is_none() {
            waiter
                .condvar
                .wait_for(&mut slot, Duration::from_millis(timeout_ms));
        }
        let result = slot.take();

        // Unregister waiter
        let mut waiters = self.waiters.lock();
        if let Some(pos) = waiters.iter().position(|w| w.id == waiter.id) {
            waiters.swap_remove(pos);
        }

        result
    }

    /// Notify all matching waiters — called from on_mutation.
    fn notify_waiters(&self, event: &FileEvent) {
        let waiters = self.waiters.lock();
        for w in waiters.iter() {
            if w.matcher.is_match(&event.path) {
                let mut slot = w.event.lock();
                if slot.is_none() {
                    *slot = Some(event.clone());
                    w.condvar.notify_one();
                }
            }
        }
    }
}

impl MutationObserver for FileWatcher {
    fn on_mutation(&self, event: &FileEvent) {
        self.notify_waiters(event);
    }
}

/// RemoteWatchProtocol — kernel-agnostic interface for distributed watch.
#[allow(dead_code)]
pub(crate) trait RemoteWatchProtocol: Send + Sync {
    fn subscribe(&self, pattern: &str, zone_id: &str) -> Result<u64, String>;
    fn unsubscribe(&self, subscription_id: u64) -> Result<(), String>;
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::dispatch::FileEventType;

    #[test]
    fn test_register_and_match() {
        let fw = FileWatcher::new();
        let id = fw.register("/zone/files/**");
        assert!(fw.match_path("/zone/files/test.txt").contains(&id));
        assert!(fw.match_path("/zone/files/sub/deep.txt").contains(&id));
        assert!(fw.match_path("/other/path").is_empty());
    }

    #[test]
    fn test_unregister() {
        let fw = FileWatcher::new();
        let id = fw.register("/zone/**");
        assert!(fw.unregister(id));
        assert!(fw.match_path("/zone/test").is_empty());
        assert!(!fw.unregister(id));
    }

    #[test]
    fn test_mutation_observer_notifies_waiter() {
        let fw = Arc::new(FileWatcher::new());
        let fw2 = fw.clone();

        // Spawn waiter thread
        let handle = std::thread::spawn(move || fw2.wait_for_event("/test/**", 5000));

        // Give waiter time to register
        std::thread::sleep(Duration::from_millis(50));

        // Fire event
        let event = FileEvent::new(FileEventType::FileWrite, "/test/file.txt");
        fw.on_mutation(&event);

        // Waiter should return the event
        let result = handle.join().unwrap();
        assert!(result.is_some());
        assert_eq!(result.unwrap().path, "/test/file.txt");
    }

    #[test]
    fn test_waiter_timeout() {
        let fw = FileWatcher::new();
        let result = fw.wait_for_event("/never/**", 50); // 50ms timeout
        assert!(result.is_none());
    }

    #[test]
    fn test_waiter_pattern_no_match() {
        let fw = Arc::new(FileWatcher::new());
        let fw2 = fw.clone();

        let handle = std::thread::spawn(move || fw2.wait_for_event("/specific/**", 200));

        std::thread::sleep(Duration::from_millis(50));

        // Fire event that doesn't match
        let event = FileEvent::new(FileEventType::FileWrite, "/other/file.txt");
        fw.on_mutation(&event);

        // Should timeout (no match)
        let result = handle.join().unwrap();
        assert!(result.is_none());
    }
}
