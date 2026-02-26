//! SQLite-based persistent cache for Nexus FUSE.
//!
//! Provides ETag-based cache invalidation to minimize network round-trips
//! while ensuring data consistency with the Nexus server.

#![allow(dead_code)]

use anyhow::{anyhow, Result};
use log::{debug, error, info, warn};
use rusqlite::{params, Connection, OptionalExtension};
use std::path::PathBuf;
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};

/// Maximum age for cached content before forcing revalidation (1 hour).
const MAX_CACHE_AGE_SECS: u64 = 3600;

/// Maximum cache size in bytes (500 MB).
const MAX_CACHE_SIZE: u64 = 500 * 1024 * 1024;

/// Maximum file size to cache (10 MB) - larger files bypass cache.
const MAX_FILE_SIZE: usize = 10 * 1024 * 1024;

/// Cache entry for file content.
#[derive(Debug, Clone)]
pub struct CacheEntry {
    pub content: Vec<u8>,
    pub etag: Option<String>,
}

/// Result of a cache lookup.
#[derive(Debug)]
pub enum CacheLookup {
    /// Cache hit with valid content.
    Hit(CacheEntry),
    /// Cache hit but needs revalidation (has etag).
    NeedsRevalidation { etag: String },
    /// Cache miss.
    Miss,
}

/// Persistent SQLite cache for file content and metadata.
pub struct FileCache {
    conn: Mutex<Connection>,
}

impl FileCache {
    /// Create or open a cache database.
    pub fn new(server_url: &str) -> Result<Self> {
        let cache_path = Self::cache_path(server_url)?;

        // Ensure cache directory exists
        if let Some(parent) = cache_path.parent() {
            std::fs::create_dir_all(parent)?;
        }

        info!("Opening cache at: {}", cache_path.display());

        let conn = Connection::open(&cache_path)?;

        // Configure SQLite for performance
        conn.execute_batch(
            "
            PRAGMA journal_mode = WAL;
            PRAGMA synchronous = NORMAL;
            PRAGMA cache_size = -64000;  -- 64MB cache
            PRAGMA temp_store = MEMORY;
            ",
        )?;

        // Create tables
        conn.execute_batch(
            "
            CREATE TABLE IF NOT EXISTS file_cache (
                path TEXT PRIMARY KEY,
                content BLOB NOT NULL,
                etag TEXT,
                size INTEGER NOT NULL,
                cached_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS metadata_cache (
                path TEXT PRIMARY KEY,
                is_dir INTEGER NOT NULL,
                size INTEGER NOT NULL,
                entry_type TEXT NOT NULL,
                cached_at INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_file_cache_cached_at ON file_cache(cached_at);
            CREATE INDEX IF NOT EXISTS idx_file_cache_size ON file_cache(size);
            ",
        )?;

        let cache = Self {
            conn: Mutex::new(conn),
        };

        // Cleanup old entries on startup
        if let Err(e) = cache.cleanup() {
            warn!("Cache cleanup failed: {}", e);
        }

        Ok(cache)
    }

    /// Get cache file path based on server URL.
    fn cache_path(server_url: &str) -> Result<PathBuf> {
        let cache_dir = dirs::cache_dir()
            .ok_or_else(|| anyhow!("Could not determine cache directory"))?
            .join("nexus-fuse");

        // Create a safe filename from the server URL
        let safe_name: String = server_url
            .chars()
            .map(|c| if c.is_alphanumeric() { c } else { '_' })
            .collect();

        Ok(cache_dir.join(format!("{}.db", safe_name)))
    }

    /// Get current timestamp.
    fn now() -> u64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_secs())
            .unwrap_or(0)
    }

    /// Look up a file in the cache.
    pub fn get(&self, path: &str) -> CacheLookup {
        let conn = self.conn.lock().unwrap();
        let now = Self::now();

        let result: Option<(Vec<u8>, Option<String>, u64)> = conn
            .query_row(
                "SELECT content, etag, cached_at FROM file_cache WHERE path = ?",
                params![path],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .optional()
            .ok()
            .flatten();

        match result {
            Some((content, etag, cached_at)) => {
                let age = now.saturating_sub(cached_at);

                if age < MAX_CACHE_AGE_SECS {
                    // Fresh cache hit
                    debug!("Cache hit for {} (age: {}s)", path, age);
                    CacheLookup::Hit(CacheEntry {
                        content,
                        etag,
                    })
                } else if let Some(etag) = etag {
                    // Stale but has etag - can revalidate
                    debug!("Cache stale for {} (age: {}s), needs revalidation", path, age);
                    CacheLookup::NeedsRevalidation { etag }
                } else {
                    // Stale with no etag - treat as miss
                    debug!("Cache stale for {} with no etag", path);
                    CacheLookup::Miss
                }
            }
            None => {
                debug!("Cache miss for {}", path);
                CacheLookup::Miss
            }
        }
    }

    /// Get etag for a cached file (for conditional requests).
    pub fn get_etag(&self, path: &str) -> Option<String> {
        let conn = self.conn.lock().unwrap();

        conn.query_row(
            "SELECT etag FROM file_cache WHERE path = ?",
            params![path],
            |row| row.get(0),
        )
        .optional()
        .ok()
        .flatten()
    }

    /// Store file content in the cache.
    /// Files larger than MAX_FILE_SIZE are not cached.
    pub fn put(&self, path: &str, content: &[u8], etag: Option<&str>) {
        // Skip caching large files
        if content.len() > MAX_FILE_SIZE {
            debug!(
                "Skipping cache for {} ({} bytes > {} limit)",
                path,
                content.len(),
                MAX_FILE_SIZE
            );
            return;
        }

        let conn = self.conn.lock().unwrap();
        let now = Self::now();

        if let Err(e) = conn.execute(
            "INSERT OR REPLACE INTO file_cache (path, content, etag, size, cached_at)
             VALUES (?, ?, ?, ?, ?)",
            params![path, content, etag, content.len() as i64, now],
        ) {
            error!("Failed to cache {}: {}", path, e);
        } else {
            debug!("Cached {} ({} bytes, etag: {:?})", path, content.len(), etag);
        }
    }

    /// Mark a cached entry as still valid (update timestamp without re-storing content).
    pub fn touch(&self, path: &str) {
        let conn = self.conn.lock().unwrap();
        let now = Self::now();

        if let Err(e) = conn.execute(
            "UPDATE file_cache SET cached_at = ? WHERE path = ?",
            params![now, path],
        ) {
            warn!("Failed to touch cache entry {}: {}", path, e);
        } else {
            debug!("Touched cache entry {}", path);
        }
    }

    /// Invalidate a specific path.
    pub fn invalidate(&self, path: &str) {
        let conn = self.conn.lock().unwrap();

        let _ = conn.execute("DELETE FROM file_cache WHERE path = ?", params![path]);
        let _ = conn.execute("DELETE FROM metadata_cache WHERE path = ?", params![path]);

        debug!("Invalidated cache for {}", path);
    }

    /// Cleanup old and oversized cache entries.
    fn cleanup(&self) -> Result<()> {
        let conn = self.conn.lock().unwrap();
        let now = Self::now();
        let max_age = now.saturating_sub(MAX_CACHE_AGE_SECS * 24); // 24 hours

        // Delete very old entries
        conn.execute(
            "DELETE FROM file_cache WHERE cached_at < ?",
            params![max_age],
        )?;

        // Check total cache size
        let total_size: u64 = conn.query_row(
            "SELECT COALESCE(SUM(size), 0) FROM file_cache",
            [],
            |row| row.get(0),
        )?;

        if total_size > MAX_CACHE_SIZE {
            info!(
                "Cache size {} MB exceeds limit {} MB, pruning...",
                total_size / 1024 / 1024,
                MAX_CACHE_SIZE / 1024 / 1024
            );

            // Delete oldest entries until under limit
            conn.execute(
                "DELETE FROM file_cache WHERE path IN (
                    SELECT path FROM file_cache ORDER BY cached_at ASC
                    LIMIT (SELECT COUNT(*) / 2 FROM file_cache)
                )",
                [],
            )?;
        }

        // Cleanup metadata cache
        conn.execute(
            "DELETE FROM metadata_cache WHERE cached_at < ?",
            params![max_age],
        )?;

        Ok(())
    }

    /// Get cache statistics.
    pub fn stats(&self) -> CacheStats {
        let conn = self.conn.lock().unwrap();

        let file_count: u64 = conn
            .query_row("SELECT COUNT(*) FROM file_cache", [], |row| row.get(0))
            .unwrap_or(0);

        let total_size: u64 = conn
            .query_row("SELECT COALESCE(SUM(size), 0) FROM file_cache", [], |row| {
                row.get(0)
            })
            .unwrap_or(0);

        CacheStats {
            file_count,
            total_size,
        }
    }
}

/// Cache statistics.
#[derive(Debug)]
pub struct CacheStats {
    pub file_count: u64,
    pub total_size: u64,
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Helper: create an in-memory cache for testing (unique URL per test).
    fn test_cache(label: &str) -> FileCache {
        FileCache::new(&format!("http://test-{}.local:2026", label)).unwrap()
    }

    // ──────────────────────────────────────────────────────
    // 1. Basic put / get / invalidate (existing test, kept)
    // ──────────────────────────────────────────────────────

    #[test]
    fn test_cache_basic() {
        let cache = test_cache("basic");

        // Miss on empty cache
        assert!(matches!(cache.get("/test.txt"), CacheLookup::Miss));

        // Put and get
        cache.put("/test.txt", b"hello world", Some("abc123"));

        match cache.get("/test.txt") {
            CacheLookup::Hit(entry) => {
                assert_eq!(entry.content, b"hello world");
                assert_eq!(entry.etag, Some("abc123".to_string()));
            }
            _ => panic!("Expected cache hit"),
        }

        // Invalidate
        cache.invalidate("/test.txt");
        assert!(matches!(cache.get("/test.txt"), CacheLookup::Miss));
    }

    // ──────────────────────────────────────────────────────
    // 2. Put without etag → stale entries become Miss (not NeedsRevalidation)
    // ──────────────────────────────────────────────────────

    #[test]
    fn test_put_without_etag() {
        let cache = test_cache("no-etag");
        cache.put("/no-etag.txt", b"data", None);

        match cache.get("/no-etag.txt") {
            CacheLookup::Hit(entry) => {
                assert_eq!(entry.content, b"data");
                assert_eq!(entry.etag, None);
            }
            _ => panic!("Expected cache hit"),
        }
    }

    // ──────────────────────────────────────────────────────
    // 3. Overwrite existing entry
    // ──────────────────────────────────────────────────────

    #[test]
    fn test_overwrite_entry() {
        let cache = test_cache("overwrite");
        cache.put("/f.txt", b"v1", Some("e1"));
        cache.put("/f.txt", b"v2", Some("e2"));

        match cache.get("/f.txt") {
            CacheLookup::Hit(entry) => {
                assert_eq!(entry.content, b"v2");
                assert_eq!(entry.etag, Some("e2".to_string()));
            }
            _ => panic!("Expected cache hit with updated content"),
        }
    }

    // ──────────────────────────────────────────────────────
    // 4. get_etag returns stored etag (or None)
    // ──────────────────────────────────────────────────────

    #[test]
    fn test_get_etag() {
        let cache = test_cache("etag");
        assert_eq!(cache.get_etag("/missing.txt"), None);

        cache.put("/e.txt", b"x", Some("etag-42"));
        assert_eq!(cache.get_etag("/e.txt"), Some("etag-42".to_string()));

        cache.put("/no-e.txt", b"x", None);
        assert_eq!(cache.get_etag("/no-e.txt"), None);
    }

    // ──────────────────────────────────────────────────────
    // 5. Touch refreshes timestamp (keeps entry fresh)
    // ──────────────────────────────────────────────────────

    #[test]
    fn test_touch_refreshes_entry() {
        let cache = test_cache("touch");
        cache.put("/t.txt", b"data", Some("e1"));

        // Touch should succeed and entry should still be a hit
        cache.touch("/t.txt");

        match cache.get("/t.txt") {
            CacheLookup::Hit(entry) => assert_eq!(entry.content, b"data"),
            _ => panic!("Expected cache hit after touch"),
        }
    }

    // ──────────────────────────────────────────────────────
    // 6. Large files are NOT cached (>MAX_FILE_SIZE)
    // ──────────────────────────────────────────────────────

    #[test]
    fn test_large_file_not_cached() {
        let cache = test_cache("large");
        let big = vec![0u8; MAX_FILE_SIZE + 1];
        cache.put("/big.bin", &big, Some("e1"));

        // Should still be a miss — file was too large
        assert!(matches!(cache.get("/big.bin"), CacheLookup::Miss));
    }

    // ──────────────────────────────────────────────────────
    // 7. File exactly at MAX_FILE_SIZE IS cached
    // ──────────────────────────────────────────────────────

    #[test]
    fn test_max_size_file_cached() {
        let cache = test_cache("maxsize");
        let data = vec![42u8; MAX_FILE_SIZE];
        cache.put("/exact.bin", &data, Some("e1"));

        match cache.get("/exact.bin") {
            CacheLookup::Hit(entry) => assert_eq!(entry.content.len(), MAX_FILE_SIZE),
            _ => panic!("Expected cache hit for file at exact max size"),
        }
    }

    // ──────────────────────────────────────────────────────
    // 8. Stats report correct counts and sizes
    // ──────────────────────────────────────────────────────

    #[test]
    fn test_cache_stats() {
        let cache = test_cache("stats");

        // Ensure clean slate (DB persists on disk between runs)
        cache.invalidate("/stat-a.txt");
        cache.invalidate("/stat-b.txt");
        let baseline = cache.stats();

        cache.put("/stat-a.txt", b"aaa", Some("e1"));
        cache.put("/stat-b.txt", b"bb", Some("e2"));

        let stats = cache.stats();
        assert_eq!(stats.file_count, baseline.file_count + 2);
        assert_eq!(stats.total_size, baseline.total_size + 5); // 3 + 2
    }

    // ──────────────────────────────────────────────────────
    // 9. Stats after invalidation
    // ──────────────────────────────────────────────────────

    #[test]
    fn test_stats_after_invalidation() {
        let cache = test_cache("stats-inv");
        cache.put("/x.txt", b"12345", None);

        let stats = cache.stats();
        assert_eq!(stats.file_count, 1);
        assert_eq!(stats.total_size, 5);

        cache.invalidate("/x.txt");

        let stats = cache.stats();
        assert_eq!(stats.file_count, 0);
        assert_eq!(stats.total_size, 0);
    }

    // ──────────────────────────────────────────────────────
    // 10. Multiple paths are independent
    // ──────────────────────────────────────────────────────

    #[test]
    fn test_multiple_independent_paths() {
        let cache = test_cache("multi");
        cache.put("/a.txt", b"aaa", Some("ea"));
        cache.put("/b.txt", b"bbb", Some("eb"));
        cache.put("/c.txt", b"ccc", Some("ec"));

        // Invalidate only /b.txt
        cache.invalidate("/b.txt");

        assert!(matches!(cache.get("/a.txt"), CacheLookup::Hit(_)));
        assert!(matches!(cache.get("/b.txt"), CacheLookup::Miss));
        assert!(matches!(cache.get("/c.txt"), CacheLookup::Hit(_)));
    }

    // ──────────────────────────────────────────────────────
    // 11. Empty content is valid and cached
    // ──────────────────────────────────────────────────────

    #[test]
    fn test_empty_content_cached() {
        let cache = test_cache("empty");
        cache.put("/empty.txt", b"", Some("e0"));

        match cache.get("/empty.txt") {
            CacheLookup::Hit(entry) => {
                assert!(entry.content.is_empty());
                assert_eq!(entry.etag, Some("e0".to_string()));
            }
            _ => panic!("Expected cache hit for empty file"),
        }
    }

    // ──────────────────────────────────────────────────────
    // 12. Binary content is preserved exactly
    // ──────────────────────────────────────────────────────

    #[test]
    fn test_binary_content_preserved() {
        let cache = test_cache("binary");
        let binary: Vec<u8> = (0..=255).collect();
        cache.put("/bin.dat", &binary, Some("ebin"));

        match cache.get("/bin.dat") {
            CacheLookup::Hit(entry) => assert_eq!(entry.content, binary),
            _ => panic!("Expected cache hit for binary content"),
        }
    }

    // ──────────────────────────────────────────────────────
    // 13. Concurrent access doesn't panic (basic safety check)
    // ──────────────────────────────────────────────────────

    #[test]
    fn test_concurrent_access() {
        use std::sync::Arc;
        use std::thread;

        let cache = Arc::new(test_cache("concurrent"));

        let handles: Vec<_> = (0..8)
            .map(|i| {
                let cache = Arc::clone(&cache);
                thread::spawn(move || {
                    let path = format!("/thread-{}.txt", i);
                    let content = format!("data-{}", i);
                    cache.put(&path, content.as_bytes(), Some(&format!("e{}", i)));
                    let _ = cache.get(&path);
                    let _ = cache.stats();
                })
            })
            .collect();

        for h in handles {
            h.join().unwrap();
        }

        // All 8 entries should be present
        let stats = cache.stats();
        assert_eq!(stats.file_count, 8);
    }

    // ──────────────────────────────────────────────────────
    // 14. Cleanup removes very old entries
    // ──────────────────────────────────────────────────────

    #[test]
    fn test_cleanup_removes_old_entries() {
        let cache = test_cache("cleanup");

        // Insert an entry, then manually backdate it
        cache.put("/old.txt", b"old-data", Some("e1"));

        {
            let conn = cache.conn.lock().unwrap();
            // Set cached_at to 0 (epoch) so it's very old
            conn.execute(
                "UPDATE file_cache SET cached_at = 0 WHERE path = '/old.txt'",
                [],
            )
            .unwrap();
        }

        // Insert a fresh entry
        cache.put("/new.txt", b"new-data", Some("e2"));

        // Run cleanup — should remove the backdated entry
        cache.cleanup().unwrap();

        assert!(matches!(cache.get("/old.txt"), CacheLookup::Miss));
        assert!(matches!(cache.get("/new.txt"), CacheLookup::Hit(_)));
    }

    // ──────────────────────────────────────────────────────
    // 15. Invalidate on non-existent path is a no-op
    // ──────────────────────────────────────────────────────

    #[test]
    fn test_invalidate_nonexistent_is_noop() {
        let cache = test_cache("inv-noop");
        // Should not panic or error
        cache.invalidate("/does-not-exist.txt");
        assert!(matches!(cache.get("/does-not-exist.txt"), CacheLookup::Miss));
    }
}
