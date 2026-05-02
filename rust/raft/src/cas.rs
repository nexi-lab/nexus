//! Local-disk content-addressable store (CAS).
//!
//! Stores blobs at `<base_dir>/cas/<blake3-hash>` on disk. Content is
//! addressed by its BLAKE3 hex digest. Writes are atomic (write to temp
//! file then rename) to avoid partial reads.

use std::path::{Path, PathBuf};

/// A simple local-disk content-addressable store.
pub struct LocalCAS {
    base_dir: PathBuf,
}

impl LocalCAS {
    /// Create a new CAS rooted at `<data_dir>/cas/`.
    ///
    /// Creates the directory if it does not exist.
    pub fn new(data_dir: &Path) -> Self {
        let base_dir = data_dir.join("cas");
        std::fs::create_dir_all(&base_dir).ok();
        Self { base_dir }
    }

    /// Store `content` and return its BLAKE3 hex digest.
    ///
    /// If the blob already exists on disk the write is skipped
    /// (content-addressed dedup).
    pub fn put(&self, content: &[u8]) -> String {
        let hash = blake3::hash(content).to_hex().to_string();
        let dest = self.base_dir.join(&hash);
        if dest.exists() {
            return hash;
        }
        // Atomic write: temp file in same dir then rename.
        let tmp = self.base_dir.join(format!(".tmp-{}", &hash));
        if let Err(e) = std::fs::write(&tmp, content) {
            tracing::error!(hash = %hash, "CAS write failed: {e}");
            return hash;
        }
        if let Err(e) = std::fs::rename(&tmp, &dest) {
            tracing::error!(hash = %hash, "CAS rename failed: {e}");
            let _ = std::fs::remove_file(&tmp);
        }
        hash
    }

    /// Read a blob by its content hash. Returns `None` if the blob
    /// does not exist on disk.
    pub fn get(&self, content_id: &str) -> Option<Vec<u8>> {
        let path = self.base_dir.join(content_id);
        std::fs::read(path).ok()
    }
}
