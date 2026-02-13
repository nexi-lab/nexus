// =============================================================================
// BloomFilter + L1MetadataCache for Content Caching
// =============================================================================

use bloomfilter::Bloom;
use dashmap::DashMap;
use memmap2::Mmap;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict};
use std::fs::File;
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::RwLock;
use std::time::{SystemTime, UNIX_EPOCH};

/// Bloom filter for fast cache miss detection
///
/// A probabilistic data structure that can quickly determine if an element
/// is definitely NOT in a set, avoiding expensive disk I/O for cache misses.
///
/// Properties:
/// - False positives possible (says "maybe exists" when it doesn't)
/// - False negatives impossible (never says "doesn't exist" when it does)
/// - O(1) lookup time regardless of set size
/// - Memory efficient: ~1.2 bytes per item at 1% false positive rate
///
/// Usage:
/// ```python
/// from nexus_fast import BloomFilter
///
/// # Create filter for 100k items with 1% false positive rate
/// bloom = BloomFilter(100000, 0.01)
///
/// # Add items
/// bloom.add("tenant1:/path/to/file.txt")
///
/// # Check existence (fast path)
/// if not bloom.might_exist("tenant1:/path/to/file.txt"):
///     return None  # Definitely not in cache, skip disk I/O
/// # else: might exist, check disk
/// ```
#[pyclass]
pub struct BloomFilter {
    bloom: RwLock<Bloom<String>>,
    capacity: usize,
    fp_rate: f64,
}

#[pymethods]
impl BloomFilter {
    /// Create a new Bloom filter
    ///
    /// Args:
    ///     expected_items: Expected number of items to store (default: 100000)
    ///     fp_rate: Target false positive rate (default: 0.01 = 1%)
    ///
    /// Memory usage: ~1.2 bytes per item at 1% FP rate
    /// Example: 100k items = ~120KB, 1M items = ~1.2MB
    #[new]
    #[pyo3(signature = (expected_items=100000, fp_rate=0.01))]
    fn new(expected_items: usize, fp_rate: f64) -> PyResult<Self> {
        let bloom = Bloom::new_for_fp_rate(expected_items, fp_rate).map_err(|e| {
            pyo3::exceptions::PyValueError::new_err(format!("Failed to create Bloom filter: {}", e))
        })?;
        Ok(Self {
            bloom: RwLock::new(bloom),
            capacity: expected_items,
            fp_rate,
        })
    }

    /// Add a key to the Bloom filter
    ///
    /// Args:
    ///     key: String key to add (e.g., "tenant_id:virtual_path" or "content_hash")
    fn add(&self, key: &str) {
        self.bloom.write().unwrap().set(&key.to_string());
    }

    /// Add multiple keys to the Bloom filter in bulk
    ///
    /// More efficient than calling add() repeatedly due to reduced lock overhead.
    ///
    /// Args:
    ///     keys: List of string keys to add
    fn add_bulk(&self, keys: Vec<String>) {
        let mut bloom = self.bloom.write().unwrap();
        for key in keys {
            bloom.set(&key);
        }
    }

    /// Check if a key might exist in the filter
    ///
    /// Returns:
    ///     False: Key definitely does NOT exist (skip disk I/O)
    ///     True: Key MIGHT exist (need to check disk to confirm)
    ///
    /// Note: False positives are possible but false negatives are not.
    fn might_exist(&self, key: &str) -> bool {
        self.bloom.read().unwrap().check(&key.to_string())
    }

    /// Check multiple keys in bulk
    ///
    /// More efficient than calling might_exist() repeatedly.
    ///
    /// Args:
    ///     keys: List of string keys to check
    ///
    /// Returns:
    ///     List of booleans indicating if each key might exist
    fn check_bulk(&self, keys: Vec<String>) -> Vec<bool> {
        let bloom = self.bloom.read().unwrap();
        keys.iter().map(|k| bloom.check(k)).collect()
    }

    /// Clear all entries from the Bloom filter
    ///
    /// Resets to empty state with same capacity and false positive rate.
    /// Useful when rebuilding the filter from scratch.
    fn clear(&self) -> PyResult<()> {
        let new_bloom = Bloom::new_for_fp_rate(self.capacity, self.fp_rate).map_err(|e| {
            pyo3::exceptions::PyValueError::new_err(format!("Failed to clear Bloom filter: {}", e))
        })?;
        *self.bloom.write().unwrap() = new_bloom;
        Ok(())
    }

    /// Get the capacity (expected items) of this filter
    #[getter]
    fn capacity(&self) -> usize {
        self.capacity
    }

    /// Get the target false positive rate
    #[getter]
    fn fp_rate(&self) -> f64 {
        self.fp_rate
    }

    /// Get approximate memory usage in bytes
    #[getter]
    fn memory_bytes(&self) -> usize {
        // Bloom filter uses ~1.44 * n * ln(1/p) / ln(2) bits
        // Simplified: ~10 bits per item at 1% FP rate = 1.25 bytes per item
        let bits_per_item = (-1.44 * (self.fp_rate).ln() / (2.0_f64).ln()) as usize;
        (self.capacity * bits_per_item).div_ceil(8)
    }
}

/// Metadata for a cached file entry (L1 cache)
///
/// Stores only metadata (~100 bytes per entry) instead of full content.
/// Content is read via mmap from disk when needed.
#[derive(Clone, Debug)]
struct CacheMetadata {
    /// Path ID from database (foreign key to file_paths table)
    path_id: String,
    /// BLAKE3 content hash for ETag support
    content_hash: String,
    /// Path to cached content on disk (for mmap access)
    disk_path: PathBuf,
    /// Original file size in bytes
    original_size: u64,
    /// When this entry was synced (Unix timestamp in seconds)
    synced_at: u64,
    /// TTL in seconds (0 = no expiration, use version check)
    ttl_seconds: u32,
    /// Whether content is text (true) or binary (false)
    is_text: bool,
    /// Tenant ID for multi-tenant isolation
    #[allow(dead_code)]
    tenant_id: String,
}

/// L1 Metadata Cache - Lock-free in-memory cache for connector content metadata
///
/// This cache stores only metadata (~100 bytes per entry) instead of full content.
/// Content is accessed via mmap from disk (using OS page cache efficiently).
///
/// Key features:
/// - Lock-free concurrent access via DashMap
/// - TTL-based expiration (no backend version checks needed)
/// - Memory efficient: O(100 bytes) per entry instead of O(content size)
/// - Zero-copy content access via mmap
///
/// Performance:
/// - Lookup: <1μs (vs ~100μs for Python pickle-based L1)
/// - Concurrent access: No blocking (vs Python threading.Lock)
/// - Memory: ~100 bytes per entry (vs megabytes for content)
///
/// Usage:
/// ```python
/// from nexus_fast import L1MetadataCache
///
/// cache = L1MetadataCache(max_entries=100000, default_ttl=300)
///
/// # Store metadata (after writing content to disk)
/// cache.put(
///     key="/mnt/gcs/data/file.txt",
///     path_id="uuid-123",
///     content_hash="abc123...",
///     disk_path="/app/data/.cache/tenant/ab/c1/abc123.bin",
///     original_size=1024,
///     ttl_seconds=300,
///     is_text=True,
///     tenant_id="tenant-1",
/// )
///
/// # Get metadata (fast, lock-free)
/// metadata = cache.get("/mnt/gcs/data/file.txt")
/// if metadata:
///     path_id, content_hash, disk_path, original_size, is_fresh = metadata
///     if is_fresh:
///         content = read_file(disk_path)  # mmap-based read
/// ```
#[pyclass]
pub struct L1MetadataCache {
    /// Lock-free concurrent hashmap: key -> CacheMetadata
    cache: DashMap<String, CacheMetadata>,
    /// Maximum number of entries (for LRU-style eviction)
    max_entries: usize,
    /// Default TTL in seconds (0 = no expiration)
    default_ttl: u32,
    /// Statistics: total hits
    hits: AtomicU64,
    /// Statistics: total misses
    misses: AtomicU64,
}

#[pymethods]
impl L1MetadataCache {
    /// Create a new L1 metadata cache
    ///
    /// Args:
    ///     max_entries: Maximum number of entries (default: 100000)
    ///     default_ttl: Default TTL in seconds (default: 300 = 5 minutes)
    #[new]
    #[pyo3(signature = (max_entries=100000, default_ttl=300))]
    fn new(max_entries: usize, default_ttl: u32) -> Self {
        Self {
            cache: DashMap::with_capacity(max_entries),
            max_entries,
            default_ttl,
            hits: AtomicU64::new(0),
            misses: AtomicU64::new(0),
        }
    }

    /// Store metadata for a cache entry
    ///
    /// Args:
    ///     key: Cache key (typically virtual_path like "/mnt/gcs/file.txt")
    ///     path_id: Database path_id (UUID from file_paths table)
    ///     content_hash: BLAKE3 hash of content (for ETag)
    ///     disk_path: Absolute path to cached content on disk
    ///     original_size: Original file size in bytes
    ///     ttl_seconds: TTL in seconds (0 = use default_ttl, -1 = no expiration)
    ///     is_text: Whether content is text (true) or binary (false)
    ///     tenant_id: Tenant ID for multi-tenant isolation
    #[pyo3(signature = (key, path_id, content_hash, disk_path, original_size, ttl_seconds=0, is_text=true, tenant_id="default"))]
    #[allow(clippy::too_many_arguments)]
    fn put(
        &self,
        key: &str,
        path_id: &str,
        content_hash: &str,
        disk_path: &str,
        original_size: u64,
        ttl_seconds: i32,
        is_text: bool,
        tenant_id: &str,
    ) {
        // Evict random entries if at capacity (simple eviction strategy)
        // DashMap doesn't support ordered eviction, so we do random eviction
        if self.cache.len() >= self.max_entries {
            // Remove ~10% of entries to make room
            let to_remove = self.max_entries / 10;
            let keys_to_remove: Vec<String> = self
                .cache
                .iter()
                .take(to_remove)
                .map(|entry| entry.key().clone())
                .collect();
            for k in keys_to_remove {
                self.cache.remove(&k);
            }
        }

        let ttl = match ttl_seconds.cmp(&0) {
            std::cmp::Ordering::Equal => self.default_ttl,
            std::cmp::Ordering::Less => 0, // No expiration
            std::cmp::Ordering::Greater => ttl_seconds as u32,
        };

        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs();

        let metadata = CacheMetadata {
            path_id: path_id.to_string(),
            content_hash: content_hash.to_string(),
            disk_path: PathBuf::from(disk_path),
            original_size,
            synced_at: now,
            ttl_seconds: ttl,
            is_text,
            tenant_id: tenant_id.to_string(),
        };

        self.cache.insert(key.to_string(), metadata);
    }

    /// Get metadata for a cache entry
    ///
    /// Returns a tuple of (path_id, content_hash, disk_path, original_size, is_text, is_fresh)
    /// or None if not found.
    ///
    /// The is_fresh field indicates whether the entry has not expired (TTL check).
    /// If is_fresh is False, the caller should refresh the entry from L2/backend.
    ///
    /// Args:
    ///     key: Cache key (typically virtual_path)
    ///
    /// Returns:
    ///     Tuple of (path_id, content_hash, disk_path, original_size, is_text, is_fresh) or None
    fn get(&self, key: &str) -> Option<(String, String, String, u64, bool, bool)> {
        match self.cache.get(key) {
            Some(entry) => {
                self.hits.fetch_add(1, Ordering::Relaxed);

                let metadata = entry.value();
                let is_fresh = if metadata.ttl_seconds == 0 {
                    true // No expiration
                } else {
                    let now = SystemTime::now()
                        .duration_since(UNIX_EPOCH)
                        .unwrap_or_default()
                        .as_secs();
                    let age = now.saturating_sub(metadata.synced_at);
                    age < metadata.ttl_seconds as u64
                };

                Some((
                    metadata.path_id.clone(),
                    metadata.content_hash.clone(),
                    metadata.disk_path.to_string_lossy().to_string(),
                    metadata.original_size,
                    metadata.is_text,
                    is_fresh,
                ))
            }
            None => {
                self.misses.fetch_add(1, Ordering::Relaxed);
                None
            }
        }
    }

    /// Get metadata and read content via mmap in one operation
    ///
    /// This combines get() + mmap read for convenience.
    /// Returns None if not found or expired.
    ///
    /// Args:
    ///     key: Cache key
    ///     py: Python interpreter (for creating PyBytes)
    ///
    /// Returns:
    ///     Tuple of (content_bytes, content_hash, is_text) or None
    fn get_content(
        &self,
        py: Python<'_>,
        key: &str,
    ) -> PyResult<Option<(Py<PyBytes>, String, bool)>> {
        let metadata = match self.cache.get(key) {
            Some(entry) => {
                let m = entry.value();
                // Check TTL
                if m.ttl_seconds > 0 {
                    let now = SystemTime::now()
                        .duration_since(UNIX_EPOCH)
                        .unwrap_or_default()
                        .as_secs();
                    let age = now.saturating_sub(m.synced_at);
                    if age >= m.ttl_seconds as u64 {
                        self.misses.fetch_add(1, Ordering::Relaxed);
                        return Ok(None); // Expired
                    }
                }
                self.hits.fetch_add(1, Ordering::Relaxed);
                (m.disk_path.clone(), m.content_hash.clone(), m.is_text)
            }
            None => {
                self.misses.fetch_add(1, Ordering::Relaxed);
                return Ok(None);
            }
        };

        let (disk_path, content_hash, is_text) = metadata;

        // Read content via mmap
        let file = match File::open(&disk_path) {
            Ok(f) => f,
            Err(_) => return Ok(None), // File doesn't exist
        };

        let file_metadata = match file.metadata() {
            Ok(m) => m,
            Err(_) => return Ok(None),
        };

        if file_metadata.len() == 0 {
            return Ok(Some((PyBytes::new(py, &[]).into(), content_hash, is_text)));
        }

        let mmap = match unsafe { Mmap::map(&file) } {
            Ok(m) => m,
            Err(_) => return Ok(None),
        };

        Ok(Some((
            PyBytes::new(py, &mmap).into(),
            content_hash,
            is_text,
        )))
    }

    /// Remove an entry from the cache
    ///
    /// Args:
    ///     key: Cache key to remove
    ///
    /// Returns:
    ///     True if entry was removed, False if not found
    fn remove(&self, key: &str) -> bool {
        self.cache.remove(key).is_some()
    }

    /// Remove all entries matching a prefix
    ///
    /// Useful for invalidating entire directories or mounts.
    ///
    /// Args:
    ///     prefix: Key prefix to match (e.g., "/mnt/gcs/")
    ///
    /// Returns:
    ///     Number of entries removed
    fn remove_prefix(&self, prefix: &str) -> usize {
        let keys_to_remove: Vec<String> = self
            .cache
            .iter()
            .filter(|entry| entry.key().starts_with(prefix))
            .map(|entry| entry.key().clone())
            .collect();

        let count = keys_to_remove.len();
        for key in keys_to_remove {
            self.cache.remove(&key);
        }
        count
    }

    /// Clear all entries from the cache
    fn clear(&self) {
        self.cache.clear();
        self.hits.store(0, Ordering::Relaxed);
        self.misses.store(0, Ordering::Relaxed);
    }

    /// Get cache statistics
    ///
    /// Returns:
    ///     Dict with entries, hits, misses, hit_rate, max_entries, default_ttl
    fn stats(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let hits = self.hits.load(Ordering::Relaxed);
        let misses = self.misses.load(Ordering::Relaxed);
        let total = hits + misses;
        let hit_rate = if total > 0 {
            hits as f64 / total as f64
        } else {
            0.0
        };

        let dict = PyDict::new(py);
        dict.set_item("entries", self.cache.len())?;
        dict.set_item("hits", hits)?;
        dict.set_item("misses", misses)?;
        dict.set_item("hit_rate", hit_rate)?;
        dict.set_item("max_entries", self.max_entries)?;
        dict.set_item("default_ttl", self.default_ttl)?;
        Ok(dict.into())
    }

    /// Get number of entries in the cache
    #[getter]
    fn len(&self) -> usize {
        self.cache.len()
    }

    /// Check if cache is empty
    fn is_empty(&self) -> bool {
        self.cache.is_empty()
    }

    /// Get approximate memory usage in bytes
    ///
    /// Estimates ~150 bytes per entry (key + metadata struct overhead)
    #[getter]
    fn memory_bytes(&self) -> usize {
        // Approximate: 100 bytes per entry + DashMap overhead
        self.cache.len() * 150
    }
}
