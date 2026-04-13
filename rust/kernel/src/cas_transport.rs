//! Local CAS Transport — Rust-native blob I/O for content-addressable storage.
//!
//! Reimplements the hot-path subset of Python `LocalTransport.fetch/store/exists`
//! combined with `CASAddressingEngine._blob_key()`, eliminating FFI overhead for
//! the Kernel fast path (Phase E).
//!
//! This module is `pub(crate)` — consumed only by `CASEngine` and `Kernel`,
//! never exposed as a PyO3 class.
//!
//! Storage layout:
//!     root / "cas" / hash[0..2] / hash[2..4] / hash
//!
//! References:
//!     - Python: `src/nexus/backends/base/cas_addressing_engine.py` (`_blob_key`)
//!     - Python: `src/nexus/backends/transports/local_transport.py` (`fetch/store/exists`)
//!     - Issue #1865: Phase C — Rust LocalCASTransport

use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::sync::Mutex;

use ahash::AHashSet;

/// Map content hash to CAS blob key: `cas/{h[0..2]}/{h[2..4]}/{hash}`.
///
/// Matches Python `CASAddressingEngine._blob_key()`.
#[inline]
pub(crate) fn blob_key(content_hash: &str) -> String {
    debug_assert!(
        content_hash.len() >= 4,
        "content_hash must be at least 4 chars"
    );
    format!(
        "cas/{}/{}/{}",
        &content_hash[..2],
        &content_hash[2..4],
        content_hash
    )
}

/// Pure Rust CAS transport for local filesystem blob I/O.
///
/// Thread-safe: all mutable state is behind `Mutex`. Designed to be shared
/// via `Arc` inside `Kernel`.
#[allow(dead_code)]
pub(crate) struct LocalCASTransport {
    root: PathBuf,
    fsync_on_write: bool,
    /// Monotonic parent-directory cache — once a CAS two-level dir exists,
    /// it is never deleted during normal operation. Matches Python
    /// `LocalTransport._known_parents`.
    known_parents: Mutex<AHashSet<String>>,
}

#[allow(dead_code)]
impl LocalCASTransport {
    /// Create a new LocalCASTransport rooted at `root_path`.
    ///
    /// The root directory is created if it does not exist.
    pub fn new(root_path: &Path, fsync_on_write: bool) -> io::Result<Self> {
        std::fs::create_dir_all(root_path)?;
        Ok(Self {
            root: root_path.to_path_buf(),
            fsync_on_write,
            known_parents: Mutex::new(AHashSet::new()),
        })
    }

    /// Resolve a CAS blob key to an absolute filesystem path.
    #[inline]
    fn resolve(&self, key: &str) -> PathBuf {
        self.root.join(key)
    }

    /// Ensure parent directory exists, with monotonic cache.
    ///
    /// CAS has at most 65,536 two-level dirs (`cas/ab/cd/`). Once created,
    /// they are never deleted during normal operation.
    fn ensure_parent(&self, path: &Path) -> io::Result<()> {
        let parent = match path.parent() {
            Some(p) => p,
            None => return Ok(()),
        };
        let parent_str = parent.to_string_lossy().to_string();

        // Fast path: check cache
        {
            let cache = self.known_parents.lock().unwrap();
            if cache.contains(&parent_str) {
                return Ok(());
            }
        }

        // Slow path: mkdir + insert into cache
        std::fs::create_dir_all(parent)?;
        {
            let mut cache = self.known_parents.lock().unwrap();
            cache.insert(parent_str);
        }
        Ok(())
    }

    /// Read a CAS blob by content hash.
    ///
    /// Corresponds to Python `LocalTransport.fetch(CASAddressingEngine._blob_key(hash))`.
    pub fn read_blob(&self, content_hash: &str) -> io::Result<Vec<u8>> {
        let key = blob_key(content_hash);
        let path = self.resolve(&key);
        std::fs::read(&path)
    }

    /// Write content to CAS, returning the BLAKE3 content hash.
    ///
    /// CAS dedup: if the blob already exists, the write is skipped entirely.
    ///
    /// Corresponds to Python `CASAddressingEngine.write_content()` (hash) +
    /// `LocalTransport.store()` (I/O).
    pub fn write_blob(&self, content: &[u8]) -> io::Result<String> {
        let hash = library::hash::hash_content(content);
        let key = blob_key(&hash);
        let path = self.resolve(&key);

        // CAS dedup: if blob already exists, skip write
        if path.exists() {
            return Ok(hash);
        }

        self.ensure_parent(&path)?;

        // Write content using O_CREAT | O_WRONLY | O_TRUNC semantics.
        // OpenOptions::create(true).truncate(true) matches Python's
        // os.open(path, O_CREAT | O_WRONLY | O_TRUNC, 0o644).
        let mut file = std::fs::OpenOptions::new()
            .write(true)
            .create(true)
            .truncate(true)
            .open(&path)?;

        file.write_all(content)?;

        if self.fsync_on_write {
            file.sync_all()?;
        }

        Ok(hash)
    }

    /// Write a pre-hashed blob (caller already knows the content hash).
    ///
    /// Used when the hash was computed externally (e.g., by CASEngine with
    /// bloom filter check). Returns `true` if the blob was actually written,
    /// `false` if it already existed (CAS dedup).
    pub fn write_blob_with_hash(&self, content: &[u8], content_hash: &str) -> io::Result<bool> {
        let key = blob_key(content_hash);
        let path = self.resolve(&key);

        // CAS dedup
        if path.exists() {
            return Ok(false);
        }

        self.ensure_parent(&path)?;

        let mut file = std::fs::OpenOptions::new()
            .write(true)
            .create(true)
            .truncate(true)
            .open(&path)?;

        file.write_all(content)?;

        if self.fsync_on_write {
            file.sync_all()?;
        }

        Ok(true)
    }

    /// Check if a CAS blob exists on disk.
    ///
    /// Corresponds to Python `LocalTransport.exists(CASAddressingEngine._blob_key(hash))`.
    pub fn exists(&self, content_hash: &str) -> bool {
        let key = blob_key(content_hash);
        let path = self.resolve(&key);
        path.is_file()
    }

    /// Get the absolute path of a CAS blob (for debugging/diagnostics).
    #[allow(dead_code)]
    pub fn blob_path(&self, content_hash: &str) -> PathBuf {
        let key = blob_key(content_hash);
        self.resolve(&key)
    }

    /// Get the size of a CAS blob in bytes.
    pub fn blob_size(&self, content_hash: &str) -> io::Result<u64> {
        let key = blob_key(content_hash);
        let path = self.resolve(&key);
        Ok(std::fs::metadata(&path)?.len())
    }

    /// Remove a CAS blob from disk.
    pub fn remove_blob(&self, content_hash: &str) -> io::Result<()> {
        let key = blob_key(content_hash);
        let path = self.resolve(&key);
        std::fs::remove_file(&path)
    }

    /// Root path of the transport (for diagnostics).
    #[allow(dead_code)]
    pub fn root(&self) -> &Path {
        &self.root
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    fn setup() -> (TempDir, LocalCASTransport) {
        let tmp = TempDir::new().unwrap();
        let transport = LocalCASTransport::new(tmp.path(), false).unwrap();
        (tmp, transport)
    }

    #[test]
    fn test_blob_key_format() {
        let hash = "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890";
        let key = blob_key(hash);
        assert_eq!(key, format!("cas/ab/cd/{}", hash));
    }

    #[test]
    fn test_blob_key_short_prefix() {
        let hash = "0011aabbccdd";
        let key = blob_key(hash);
        assert_eq!(key, "cas/00/11/0011aabbccdd");
    }

    #[test]
    fn test_write_and_read_blob() {
        let (_tmp, transport) = setup();
        let content = b"hello world";

        let hash = transport.write_blob(content).unwrap();
        assert_eq!(hash.len(), 64); // BLAKE3 = 64 hex chars

        let read_back = transport.read_blob(&hash).unwrap();
        assert_eq!(read_back, content);
    }

    #[test]
    fn test_write_blob_deterministic_hash() {
        let (_tmp, transport) = setup();
        let content = b"deterministic content";

        let hash1 = transport.write_blob(content).unwrap();
        let hash2 = transport.write_blob(content).unwrap();
        assert_eq!(hash1, hash2);
    }

    #[test]
    fn test_write_blob_dedup() {
        let (_tmp, transport) = setup();
        let content = b"dedup test content";

        // First write creates the file
        let hash = transport.write_blob(content).unwrap();
        let path = transport.blob_path(&hash);
        let mtime1 = std::fs::metadata(&path).unwrap().modified().unwrap();

        // Second write is a no-op (CAS dedup)
        let hash2 = transport.write_blob(content).unwrap();
        assert_eq!(hash, hash2);
        let mtime2 = std::fs::metadata(&path).unwrap().modified().unwrap();
        assert_eq!(mtime1, mtime2); // File was NOT rewritten
    }

    #[test]
    fn test_exists() {
        let (_tmp, transport) = setup();
        let content = b"existence check";

        let hash = transport.write_blob(content).unwrap();
        assert!(transport.exists(&hash));

        assert!(
            !transport.exists("0000000000000000000000000000000000000000000000000000000000000000")
        );
    }

    #[test]
    fn test_read_nonexistent_blob() {
        let (_tmp, transport) = setup();
        let result =
            transport.read_blob("deadbeef00000000deadbeef00000000deadbeef00000000deadbeef00000000");
        assert!(result.is_err());
        assert_eq!(result.unwrap_err().kind(), io::ErrorKind::NotFound);
    }

    #[test]
    fn test_empty_content() {
        let (_tmp, transport) = setup();
        let content = b"";

        let hash = transport.write_blob(content).unwrap();
        assert_eq!(hash.len(), 64);

        let read_back = transport.read_blob(&hash).unwrap();
        assert_eq!(read_back, b"");
        assert!(transport.exists(&hash));
    }

    #[test]
    fn test_large_content() {
        let (_tmp, transport) = setup();
        let content = vec![42u8; 1024 * 1024]; // 1MB

        let hash = transport.write_blob(&content).unwrap();
        let read_back = transport.read_blob(&hash).unwrap();
        assert_eq!(read_back, content);
    }

    #[test]
    fn test_write_blob_with_hash() {
        let (_tmp, transport) = setup();
        let content = b"pre-hashed content";
        let hash = library::hash::hash_content(content);

        // First write: actual write
        let written = transport.write_blob_with_hash(content, &hash).unwrap();
        assert!(written);

        // Second write: dedup
        let written = transport.write_blob_with_hash(content, &hash).unwrap();
        assert!(!written);

        // Verify content
        let read_back = transport.read_blob(&hash).unwrap();
        assert_eq!(read_back, content);
    }

    #[test]
    fn test_blob_size() {
        let (_tmp, transport) = setup();
        let content = b"size check content";

        let hash = transport.write_blob(content).unwrap();
        let size = transport.blob_size(&hash).unwrap();
        assert_eq!(size, content.len() as u64);
    }

    #[test]
    fn test_remove_blob() {
        let (_tmp, transport) = setup();
        let content = b"to be removed";

        let hash = transport.write_blob(content).unwrap();
        assert!(transport.exists(&hash));

        transport.remove_blob(&hash).unwrap();
        assert!(!transport.exists(&hash));
    }

    #[test]
    fn test_remove_nonexistent_blob() {
        let (_tmp, transport) = setup();
        let result = transport
            .remove_blob("deadbeef00000000deadbeef00000000deadbeef00000000deadbeef00000000");
        assert!(result.is_err());
    }

    #[test]
    fn test_parent_cache_works() {
        let (_tmp, transport) = setup();

        // Write two blobs that share the same 2-level parent (same first 4 hex chars)
        // BLAKE3 hashes are deterministic, so we just write different content
        let hash1 = transport.write_blob(b"content A").unwrap();
        let hash2 = transport.write_blob(b"content B").unwrap();

        // Both should be readable
        assert!(transport.exists(&hash1));
        assert!(transport.exists(&hash2));

        // Cache should have entries
        let cache = transport.known_parents.lock().unwrap();
        assert!(!cache.is_empty());
    }

    #[test]
    fn test_fsync_on_write() {
        let tmp = TempDir::new().unwrap();
        let transport = LocalCASTransport::new(tmp.path(), true).unwrap();

        // fsync=true should still produce correct output
        let content = b"fsync content";
        let hash = transport.write_blob(content).unwrap();
        let read_back = transport.read_blob(&hash).unwrap();
        assert_eq!(read_back, content);
    }

    #[test]
    fn test_different_content_different_hash() {
        let (_tmp, transport) = setup();

        let hash1 = transport.write_blob(b"content alpha").unwrap();
        let hash2 = transport.write_blob(b"content beta").unwrap();
        assert_ne!(hash1, hash2);
    }

    #[test]
    fn test_hash_matches_library() {
        // Verify our write_blob hash matches library::hash::hash_content directly
        let (_tmp, transport) = setup();
        let content = b"hash consistency check";

        let transport_hash = transport.write_blob(content).unwrap();
        let direct_hash = library::hash::hash_content(content);
        assert_eq!(transport_hash, direct_hash);
    }

    #[test]
    fn test_blob_path_layout() {
        let (_tmp, transport) = setup();
        let content = b"path layout test";
        let hash = transport.write_blob(content).unwrap();

        let path = transport.blob_path(&hash);
        let path_str = path.to_string_lossy();

        // Verify 2-level directory structure: root/cas/XX/YY/hash
        assert!(path_str.contains(&format!("cas/{}/{}/{}", &hash[..2], &hash[2..4], &hash)));
        assert!(path.is_file());
    }

    #[test]
    fn test_concurrent_writes() {
        use std::sync::Arc;
        use std::thread;

        let tmp = TempDir::new().unwrap();
        let transport = Arc::new(LocalCASTransport::new(tmp.path(), false).unwrap());

        let mut handles = vec![];
        for i in 0..10 {
            let t = Arc::clone(&transport);
            handles.push(thread::spawn(move || {
                let content = format!("concurrent content {}", i);
                t.write_blob(content.as_bytes()).unwrap()
            }));
        }

        let hashes: Vec<String> = handles.into_iter().map(|h| h.join().unwrap()).collect();

        // All blobs should be readable
        for (i, hash) in hashes.iter().enumerate() {
            let content = format!("concurrent content {}", i);
            let read_back = transport.read_blob(hash).unwrap();
            assert_eq!(read_back, content.as_bytes());
        }
    }
}
