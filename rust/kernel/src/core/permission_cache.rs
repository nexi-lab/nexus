//! Permission lease cache — pure Rust, no PyO3 dependency.
//!
//! DashMap-based (path, agent_id) → Instant lease table. On hit, the
//! full ReBAC bitmap check is skipped entirely (~100-200ns vs
//! ~50-200μs). Same algorithm as the Python `PermissionLeaseTable`
//! (permission_lease.py) but in pure Rust with DashMap.
//!
//! Inheritance-aware: `check` walks up the path hierarchy
//! (O(depth) DashMap lookups) so a parent directory lease covers
//! child files — matching the Python implementation exactly.

use dashmap::DashMap;
use std::time::{Duration, Instant};

/// Permission lease cache — (path, agent_id) → granted_at.
///
/// Lock-free concurrent reads (DashMap sharded buckets). Writes are
/// infrequent (only on lease miss → ReBAC check → stamp).
pub struct PermissionLeaseCache {
    leases: DashMap<(String, String), Instant>,
    ttl: Duration,
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
    /// `/docs/` covers `/docs/readme.md`. Returns true on first hit.
    /// Expired entries are lazily removed on access.
    pub fn check(&self, path: &str, agent_id: &str) -> bool {
        if agent_id.is_empty() {
            return false;
        }

        // Walk up path segments: /a/b/c → /a/b/c, /a/b, /a, /
        let mut current = path;
        loop {
            let key = (current.to_string(), agent_id.to_string());
            if let Some(entry) = self.leases.get(&key) {
                if entry.value().elapsed() < self.ttl {
                    return true;
                }
                // Expired — remove lazily
                drop(entry);
                self.leases.remove(&key);
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
    /// Triggers lazy eviction at 90% capacity: expired entries are
    /// removed first; if still over cap, the table is cleared (cold
    /// start equivalent — same strategy as the Python implementation).
    pub fn stamp(&self, path: &str, agent_id: &str) {
        if agent_id.is_empty() {
            return;
        }

        // Lazy eviction at 90% capacity
        if self.leases.len() >= self.max_entries * 9 / 10 {
            self.evict_expired();
            if self.leases.len() >= self.max_entries {
                self.leases.clear();
            }
        }

        self.leases
            .insert((path.to_string(), agent_id.to_string()), Instant::now());
    }

    /// Invalidate all leases matching the given path prefix.
    pub fn invalidate_path(&self, path: &str) {
        self.leases.retain(|k, _| k.0 != path);
    }

    /// Invalidate all leases for a specific agent.
    pub fn invalidate_agent(&self, agent_id: &str) {
        self.leases.retain(|k, _| k.1 != agent_id);
    }

    /// Clear all leases.
    pub fn invalidate_all(&self) {
        self.leases.clear();
    }

    /// Remove expired entries.
    fn evict_expired(&self) {
        let ttl = self.ttl;
        self.leases.retain(|_, v| v.elapsed() < ttl);
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
