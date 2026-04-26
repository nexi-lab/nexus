//! FileWatchRegistry — Rust-native file watch pattern matching (§10 A3).
//!
//! Kernel primitive for inotify-like file change notification.
//! Stores watch patterns (glob-style) with unique IDs.
//! On mutation, pure Rust pattern match returns matching watch IDs.
//! Python receives Vec<WatchId> and wakes corresponding asyncio.Futures.
//!
//! RemoteWatchProtocol trait defined here — impl deferred.

use globset::{Glob, GlobMatcher};
use parking_lot::RwLock;
use std::sync::atomic::{AtomicU64, Ordering};

/// A registered file watch entry.
#[allow(dead_code)]
struct WatchEntry {
    id: u64,
    pattern: String,
    matcher: GlobMatcher,
}

/// Kernel file watch registry — pattern matching without GIL.
pub(crate) struct FileWatchRegistry {
    watches: RwLock<Vec<WatchEntry>>,
    next_id: AtomicU64,
}

impl FileWatchRegistry {
    pub(crate) fn new() -> Self {
        Self {
            watches: RwLock::new(Vec::new()),
            next_id: AtomicU64::new(1),
        }
    }

    /// Register a glob pattern watch. Returns unique watch ID.
    pub(crate) fn register(&self, pattern: &str) -> u64 {
        let id = self.next_id.fetch_add(1, Ordering::Relaxed);
        // Build glob matcher — fallback to literal match if glob parse fails
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
    /// Returns list of matching watch IDs (pure Rust, no GIL).
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

    /// Block until a file event matching the pattern arrives, or timeout.
    /// Stub — returns None (blocking watch not yet implemented).
    #[allow(dead_code)]
    pub(crate) fn wait_for_event(
        &self,
        _pattern: &str,
        _timeout_ms: u64,
    ) -> Option<crate::dispatch::FileEvent> {
        None
    }
}

/// RemoteWatchProtocol — kernel-agnostic interface for distributed watch.
///
/// Implementations deferred to another AI doing DT_STREAM migration.
/// Defined here so kernel can hold `Option<Box<dyn RemoteWatchProtocol>>`.
#[allow(dead_code)]
pub(crate) trait RemoteWatchProtocol: Send + Sync {
    /// Subscribe to remote watch events for a path pattern.
    fn subscribe(&self, pattern: &str, zone_id: &str) -> Result<u64, String>;
    /// Unsubscribe from remote watch.
    fn unsubscribe(&self, subscription_id: u64) -> Result<(), String>;
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_register_and_match() {
        let registry = FileWatchRegistry::new();
        let id = registry.register("/zone/files/**");
        assert!(registry.match_path("/zone/files/test.txt").contains(&id));
        assert!(registry
            .match_path("/zone/files/sub/deep.txt")
            .contains(&id));
        assert!(registry.match_path("/other/path").is_empty());
    }

    #[test]
    fn test_unregister() {
        let registry = FileWatchRegistry::new();
        let id = registry.register("/zone/**");
        assert!(registry.unregister(id));
        assert!(registry.match_path("/zone/test").is_empty());
        assert!(!registry.unregister(id)); // Already removed
    }

    #[test]
    fn test_multiple_watches() {
        let registry = FileWatchRegistry::new();
        let id1 = registry.register("/a/**");
        let id2 = registry.register("/a/b/**");
        let matches = registry.match_path("/a/b/c.txt");
        assert!(matches.contains(&id1));
        assert!(matches.contains(&id2));
    }

    #[test]
    fn test_literal_pattern() {
        let registry = FileWatchRegistry::new();
        let id = registry.register("/exact/path.txt");
        assert!(registry.match_path("/exact/path.txt").contains(&id));
        assert!(registry.match_path("/exact/other.txt").is_empty());
    }
}
