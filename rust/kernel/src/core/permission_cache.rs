//! Permission lease cache — pure Rust, no PyO3 dependency.
//!
//! Two-level DashMap: outer keyed by path, inner keyed by agent_id.
//! The two-level structure lets `check`'s inheritance walk look up
//! `(path, agent_id)` with zero String allocations on a hit — both
//! `outer.get(&str)` and `inner.get(&str)` borrow via
//! `String: Borrow<str>`. On hit the full ReBAC bitmap check is
//! skipped entirely (~100-200ns vs ~50-200μs). Same algorithm as the
//! Python `PermissionLeaseTable` (permission_lease.py).
//!
//! Inheritance-aware: `check` walks up the path hierarchy
//! (O(depth) outer lookups) so a parent directory lease covers
//! child files.

use dashmap::DashMap;
use std::time::{Duration, Instant};

/// Permission lease cache — path → (agent_id → granted_at).
///
/// Lock-free concurrent reads on both levels (DashMap sharded
/// buckets). Writes are infrequent (only on lease miss → ReBAC
/// check → stamp).
pub struct PermissionLeaseCache {
    leases: DashMap<String, DashMap<String, Instant>>,
    ttl: Duration,
    /// Soft cap on unique paths. When the outer map reaches 90% of
    /// this bound, [`Self::stamp`] runs an `evict_expired` pass; if
    /// the cap is still hit, the whole table is cleared (cold-start
    /// fallback, same strategy as the Python `PermissionLeaseTable`).
    max_entries: usize,
}

impl PermissionLeaseCache {
    /// Create a new lease cache with the given TTL and max capacity.
    pub fn new(ttl: Duration, max_entries: usize) -> Self {
        Self {
            leases: DashMap::with_capacity(1024),
            ttl,
            max_entries,
        }
    }

    /// Check whether a valid lease exists for (path, agent_id).
    ///
    /// Walks up the path hierarchy (inheritance-aware): a lease on
    /// `/docs` covers `/docs/readme.md`. Returns true on first hit.
    /// Expired entries are lazily removed on access.
    ///
    /// Hot path: zero String allocations on a hit. Both
    /// `outer.get(&str)` and `inner.get(&str)` borrow via
    /// `String: Borrow<str>`.
    pub fn check(&self, path: &str, agent_id: &str) -> bool {
        if agent_id.is_empty() {
            return false;
        }

        // Walk up path segments: /a/b/c → /a/b/c, /a/b, /a, /
        let mut current = path;
        loop {
            if let Some(inner) = self.leases.get(current) {
                if let Some(stamped) = inner.get(agent_id) {
                    if stamped.value().elapsed() < self.ttl {
                        return true;
                    }
                    // Expired — release inner Ref before write to avoid
                    // shared-vs-exclusive contention on the same shard.
                    drop(stamped);
                    inner.remove(agent_id);
                }
            }

            // Walk up to parent
            match current.rfind('/') {
                Some(0) if current.len() > 1 => {
                    // Try root "/" as last resort
                    current = "/";
                }
                Some(pos) if pos > 0 => {
                    current = &current[..pos];
                }
                _ => break,
            }
        }

        false
    }

    /// Record a successful permission check as a lease.
    ///
    /// Triggers lazy eviction at 90% of the unique-path cap: expired
    /// entries are removed first; if still over cap, the table is
    /// cleared (cold start equivalent — same strategy as the Python
    /// implementation).
    pub fn stamp(&self, path: &str, agent_id: &str) {
        if agent_id.is_empty() {
            return;
        }

        // Lazy eviction at 90% of unique-path cap.
        if self.leases.len() >= self.max_entries * 9 / 10 {
            self.evict_expired();
            if self.leases.len() >= self.max_entries {
                self.leases.clear();
            }
        }

        self.leases
            .entry(path.to_string())
            .or_default()
            .insert(agent_id.to_string(), Instant::now());
    }

    /// Invalidate all leases stamped for the exact given path.
    ///
    /// Note: this does **not** propagate to descendants. A stale lease
    /// stamped on `/docs` will still satisfy a `check("/docs/file")`
    /// via the inheritance walk in [`Self::check`]. Callers that need
    /// to invalidate a whole subtree must either invalidate each
    /// stamped path explicitly or use [`Self::invalidate_all`]. This
    /// asymmetry mirrors the Python `PermissionLeaseTable`.
    pub fn invalidate_path(&self, path: &str) {
        self.leases.remove(path);
    }

    /// Invalidate all leases for a specific agent across every path.
    pub fn invalidate_agent(&self, agent_id: &str) {
        for entry in self.leases.iter() {
            entry.value().remove(agent_id);
        }
    }

    /// Clear all leases.
    pub fn invalidate_all(&self) {
        self.leases.clear();
    }

    /// Remove expired entries and drop now-empty inner maps.
    fn evict_expired(&self) {
        let ttl = self.ttl;
        for entry in self.leases.iter() {
            entry.value().retain(|_, v| v.elapsed() < ttl);
        }
        self.leases.retain(|_, inner| !inner.is_empty());
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_stamp_and_check() {
        let cache = PermissionLeaseCache::new(Duration::from_secs(30), 100_000);
        cache.stamp("/docs/readme.md", "agent-1");
        assert!(cache.check("/docs/readme.md", "agent-1"));
        assert!(!cache.check("/docs/readme.md", "agent-2"));
        assert!(!cache.check("/other/file.txt", "agent-1"));
    }

    #[test]
    fn test_inheritance_walk() {
        let cache = PermissionLeaseCache::new(Duration::from_secs(30), 100_000);
        cache.stamp("/docs", "agent-1");
        // Child paths should match via parent walk
        assert!(cache.check("/docs/readme.md", "agent-1"));
        assert!(cache.check("/docs/sub/deep/file.txt", "agent-1"));
        // Unrelated paths should not
        assert!(!cache.check("/other/file.txt", "agent-1"));
    }

    #[test]
    fn test_empty_agent_id() {
        let cache = PermissionLeaseCache::new(Duration::from_secs(30), 100_000);
        cache.stamp("/docs/readme.md", "");
        assert!(!cache.check("/docs/readme.md", ""));
    }

    #[test]
    fn test_expired_lease() {
        let cache = PermissionLeaseCache::new(Duration::from_millis(1), 100_000);
        cache.stamp("/docs/readme.md", "agent-1");
        std::thread::sleep(Duration::from_millis(5));
        assert!(!cache.check("/docs/readme.md", "agent-1"));
    }

    #[test]
    fn test_invalidate_path() {
        let cache = PermissionLeaseCache::new(Duration::from_secs(30), 100_000);
        cache.stamp("/docs/readme.md", "agent-1");
        cache.stamp("/other/file.txt", "agent-1");
        cache.invalidate_path("/docs/readme.md");
        assert!(!cache.check("/docs/readme.md", "agent-1"));
        assert!(cache.check("/other/file.txt", "agent-1"));
    }

    #[test]
    fn test_invalidate_agent() {
        let cache = PermissionLeaseCache::new(Duration::from_secs(30), 100_000);
        cache.stamp("/docs/readme.md", "agent-1");
        cache.stamp("/docs/readme.md", "agent-2");
        cache.invalidate_agent("agent-1");
        assert!(!cache.check("/docs/readme.md", "agent-1"));
        assert!(cache.check("/docs/readme.md", "agent-2"));
    }

    #[test]
    fn test_invalidate_all() {
        let cache = PermissionLeaseCache::new(Duration::from_secs(30), 100_000);
        cache.stamp("/docs/readme.md", "agent-1");
        cache.stamp("/other/file.txt", "agent-2");
        cache.invalidate_all();
        assert!(!cache.check("/docs/readme.md", "agent-1"));
        assert!(!cache.check("/other/file.txt", "agent-2"));
    }

    #[test]
    fn test_eviction_at_capacity() {
        let cache = PermissionLeaseCache::new(Duration::from_secs(30), 10);
        // Fill to 90% capacity (9 entries)
        for i in 0..9 {
            cache.stamp(&format!("/file-{i}"), "agent-1");
        }
        // This should trigger eviction check (9 >= 10*9/10=9)
        cache.stamp("/file-9", "agent-1");
        // All entries should still be valid (none expired)
        // but capacity was reached, so table was cleared then re-inserted
        assert!(cache.check("/file-9", "agent-1"));
    }
}
