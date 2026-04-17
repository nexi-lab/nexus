//! CAS Engine — content-addressable read/write combining Transport + BLAKE3 hash.
//!
//! Reimplements the hot-path subset of Python `CASAddressingEngine.read_content()`
//! and `write_content()`, eliminating FFI overhead for the Kernel fast path.
//!
//! This module is `pub(crate)` — consumed only by `Kernel`, never exposed
//! as a PyO3 class. External callers (Python) still use `CASAddressingEngine`.
//!
//! Not included (stays in Python):
//! - CDC chunked write (split into chunks) — read reassembly via ChunkAssembler DI
//! - Content cache (LRU)
//! - TTL routing
//! - Multipart upload
//! - Write callbacks (e.g., Zoekt reindex)
//!
//! References:
//!     - Python: `src/nexus/backends/base/cas_addressing_engine.py`
//!     - Issue #1866: Phase D — Rust CASEngine

use std::io;
use std::sync::Arc;

use crate::cas_transport::LocalCASTransport;

/// Error type for CAS operations.
#[derive(Debug)]
pub(crate) enum CASError {
    /// Content hash not found in storage.
    NotFound(String),
    /// Underlying I/O error.
    IOError(io::Error),
}

impl std::fmt::Display for CASError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            CASError::NotFound(hash) => write!(f, "CAS content not found: {}", hash),
            CASError::IOError(e) => write!(f, "CAS I/O error: {}", e),
        }
    }
}

impl From<io::Error> for CASError {
    fn from(e: io::Error) -> Self {
        match e.kind() {
            io::ErrorKind::NotFound => CASError::NotFound(format!("(io::NotFound) {}", e)),
            _ => CASError::IOError(e),
        }
    }
}

/// Pure Rust CAS engine: hash + dedup + local blob I/O.
///
/// Combines `LocalCASTransport` (Phase C) with BLAKE3 hashing to provide
/// complete content-addressable read/write without Python involvement.
///
/// CDC chunk reassembly uses the `ChunkAssembler` DI trait (composition,
/// matching Python's `CASAddressingEngine(cdc_engine=...)` pattern).
///
/// Thread-safe: all mutable state is in `LocalCASTransport` (which uses Mutex).
#[allow(dead_code)]
pub(crate) struct CASEngine {
    transport: LocalCASTransport,
    chunk_assembler: Option<Arc<dyn crate::cas_chunking::ChunkAssembler>>,
    chunking_strategy: Option<Arc<dyn crate::cas_chunking::ChunkingStrategy>>,
}

#[allow(dead_code)]
impl CASEngine {
    /// Create a new CASEngine backed by a local filesystem transport.
    /// CDC chunk reassembly (read) + chunked-manifest write are enabled by
    /// default via `ChunkedManifestAssembler` + `FastCDCStrategy`.
    pub fn new(transport: LocalCASTransport) -> Self {
        Self {
            transport,
            chunk_assembler: Some(crate::cas_chunking::default_chunk_assembler()),
            chunking_strategy: Some(crate::cas_chunking::default_chunking_strategy()),
        }
    }

    /// Read content by etag (content hash).
    ///
    /// If a `ChunkAssembler` is injected, it gets first look at every blob.
    /// Chunked manifests are reassembled transparently; single blobs pass through.
    pub fn read_content(&self, etag: &str) -> Result<Vec<u8>, CASError> {
        let data = self.transport.read_blob(etag).map_err(|e| match e.kind() {
            io::ErrorKind::NotFound => CASError::NotFound(etag.to_string()),
            _ => CASError::IOError(e),
        })?;

        // CDC composition: delegate to ChunkAssembler if present
        if let Some(assembler) = &self.chunk_assembler {
            if let Some(reassembled) = assembler.try_reassemble(&data, &self.transport)? {
                return Ok(reassembled);
            }
        }

        Ok(data)
    }

    /// Write content and return its BLAKE3 hash (single blob) or the manifest
    /// hash (chunked). CDC composition: if a `ChunkWriter` is injected and it
    /// says the content should be chunked, we split into CDC chunks, write
    /// the manifest + `.meta` sidecar, and return the manifest hash. Single
    /// blobs pass through `transport.write_blob` unchanged.
    ///
    /// CAS dedup: if the resulting blob already exists, the write is skipped
    /// (implemented inside the transport).
    pub fn write_content(&self, content: &[u8]) -> Result<String, CASError> {
        if let Some(strategy) = &self.chunking_strategy {
            if strategy.should_chunk(content) {
                return strategy.write_chunked(content, &self.transport);
            }
        }
        Ok(self.transport.write_blob(content)?)
    }

    /// Check if content exists by hash.
    ///
    /// Corresponds to Python `CASAddressingEngine.content_exists(content_id)`.
    pub fn content_exists(&self, etag: &str) -> bool {
        self.transport.exists(etag)
    }

    /// Get content size by hash.
    pub fn content_size(&self, etag: &str) -> Result<u64, CASError> {
        self.transport.blob_size(etag).map_err(|e| match e.kind() {
            io::ErrorKind::NotFound => CASError::NotFound(etag.to_string()),
            _ => CASError::IOError(e),
        })
    }

    /// Delete content by hash.
    pub fn delete_content(&self, etag: &str) -> Result<(), CASError> {
        self.transport
            .remove_blob(etag)
            .map_err(|e| match e.kind() {
                io::ErrorKind::NotFound => CASError::NotFound(etag.to_string()),
                _ => CASError::IOError(e),
            })
    }

    /// Expose transport for direct access (used by Kernel for
    /// pre-hashed writes where the hash is already known from dcache).
    pub fn transport(&self) -> &LocalCASTransport {
        &self.transport
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    fn setup() -> (TempDir, CASEngine) {
        let tmp = TempDir::new().unwrap();
        let transport = LocalCASTransport::new(tmp.path(), false).unwrap();
        let engine = CASEngine::new(transport);
        (tmp, engine)
    }

    #[test]
    fn test_write_and_read_content() {
        let (_tmp, engine) = setup();
        let content = b"hello CAS engine";

        let hash = engine.write_content(content).unwrap();
        assert_eq!(hash.len(), 64);

        let read_back = engine.read_content(&hash).unwrap();
        assert_eq!(read_back, content);
    }

    #[test]
    fn test_write_dedup() {
        let (_tmp, engine) = setup();
        let content = b"dedup through engine";

        let hash1 = engine.write_content(content).unwrap();
        let hash2 = engine.write_content(content).unwrap();
        assert_eq!(hash1, hash2);
    }

    #[test]
    fn test_read_not_found() {
        let (_tmp, engine) = setup();
        let result =
            engine.read_content("0000000000000000000000000000000000000000000000000000000000000000");
        assert!(result.is_err());
        match result.unwrap_err() {
            CASError::NotFound(_) => {} // expected
            other => panic!("Expected NotFound, got: {:?}", other),
        }
    }

    #[test]
    fn test_content_exists() {
        let (_tmp, engine) = setup();
        let content = b"existence through engine";

        let hash = engine.write_content(content).unwrap();
        assert!(engine.content_exists(&hash));
        assert!(!engine
            .content_exists("0000000000000000000000000000000000000000000000000000000000000000"));
    }

    #[test]
    fn test_content_size() {
        let (_tmp, engine) = setup();
        let content = b"size check through engine";

        let hash = engine.write_content(content).unwrap();
        let size = engine.content_size(&hash).unwrap();
        assert_eq!(size, content.len() as u64);
    }

    #[test]
    fn test_delete_content() {
        let (_tmp, engine) = setup();
        let content = b"to be deleted through engine";

        let hash = engine.write_content(content).unwrap();
        assert!(engine.content_exists(&hash));

        engine.delete_content(&hash).unwrap();
        assert!(!engine.content_exists(&hash));
    }

    #[test]
    fn test_delete_not_found() {
        let (_tmp, engine) = setup();
        let result = engine
            .delete_content("0000000000000000000000000000000000000000000000000000000000000000");
        assert!(result.is_err());
    }

    #[test]
    fn test_empty_content() {
        let (_tmp, engine) = setup();
        let hash = engine.write_content(b"").unwrap();
        let read_back = engine.read_content(&hash).unwrap();
        assert_eq!(read_back, b"");
    }

    #[test]
    fn test_large_content() {
        let (_tmp, engine) = setup();
        let content = vec![0xABu8; 512 * 1024]; // 512KB

        let hash = engine.write_content(&content).unwrap();
        let read_back = engine.read_content(&hash).unwrap();
        assert_eq!(read_back, content);
    }

    #[test]
    fn test_hash_consistency_with_library() {
        let (_tmp, engine) = setup();
        let content = b"hash consistency check";

        let engine_hash = engine.write_content(content).unwrap();
        let direct_hash = library::hash::hash_content(content);
        assert_eq!(engine_hash, direct_hash);
    }
}
