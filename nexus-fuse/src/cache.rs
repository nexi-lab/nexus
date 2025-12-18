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

            CREATE TABLE IF NOT EXISTS dir_cache (
                path TEXT PRIMARY KEY,
                entries TEXT NOT NULL,  -- JSON array
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
        let _ = conn.execute("DELETE FROM dir_cache WHERE path = ?", params![path]);

        // Also invalidate parent directory cache
        if let Some(parent) = std::path::Path::new(path).parent() {
            let parent_str = parent.to_string_lossy();
            let parent_path = if parent_str.is_empty() { "/" } else { &parent_str };
            let _ = conn.execute("DELETE FROM dir_cache WHERE path = ?", params![parent_path]);
        }

        debug!("Invalidated cache for {}", path);
    }

    /// Store directory listing in cache.
    pub fn put_dir(&self, path: &str, entries_json: &str) {
        let conn = self.conn.lock().unwrap();
        let now = Self::now();

        if let Err(e) = conn.execute(
            "INSERT OR REPLACE INTO dir_cache (path, entries, cached_at) VALUES (?, ?, ?)",
            params![path, entries_json, now],
        ) {
            error!("Failed to cache dir {}: {}", path, e);
        }
    }

    /// Get cached directory listing.
    pub fn get_dir(&self, path: &str, max_age_secs: u64) -> Option<String> {
        let conn = self.conn.lock().unwrap();
        let now = Self::now();

        let result: Option<(String, u64)> = conn
            .query_row(
                "SELECT entries, cached_at FROM dir_cache WHERE path = ?",
                params![path],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .optional()
            .ok()
            .flatten();

        match result {
            Some((entries, cached_at)) if now.saturating_sub(cached_at) < max_age_secs => {
                Some(entries)
            }
            _ => None,
        }
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

        // Cleanup metadata and dir caches
        conn.execute(
            "DELETE FROM metadata_cache WHERE cached_at < ?",
            params![max_age],
        )?;
        conn.execute(
            "DELETE FROM dir_cache WHERE cached_at < ?",
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

        let dir_count: u64 = conn
            .query_row("SELECT COUNT(*) FROM dir_cache", [], |row| row.get(0))
            .unwrap_or(0);

        CacheStats {
            file_count,
            total_size,
            dir_count,
        }
    }
}

/// Cache statistics.
#[derive(Debug)]
pub struct CacheStats {
    pub file_count: u64,
    pub total_size: u64,
    pub dir_count: u64,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_cache_basic() {
        let cache = FileCache::new("http://test.local:8080").unwrap();

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
}
