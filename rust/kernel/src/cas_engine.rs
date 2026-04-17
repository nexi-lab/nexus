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

use serde_json::{json, Value};

use crate::cas_chunking::{finalize_manifest, read_and_verify_chunk, ChunkingStrategy};
use crate::cas_remote::RemoteChunkFetcher;
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
    /// Optional scatter-gather remote-chunk fetcher. When set, any local
    /// chunk miss falls through to a bounded fan-out RPC against the
    /// file's origin set. `None` = local-only (tests, single-node).
    fetcher: Option<Arc<dyn RemoteChunkFetcher>>,
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
            fetcher: None,
        }
    }

    /// Construct with an explicit chunking strategy. LLM backends inject
    /// `MessageBoundaryStrategy` here; generic CAS defaults to FastCDC via
    /// `new()`.
    pub fn with_strategy(
        transport: LocalCASTransport,
        strategy: Arc<dyn ChunkingStrategy>,
    ) -> Self {
        Self {
            transport,
            chunk_assembler: Some(crate::cas_chunking::default_chunk_assembler()),
            chunking_strategy: Some(strategy),
            fetcher: None,
        }
    }

    /// Inject a scatter-gather fetcher. Typically called by the kernel
    /// mount-installer with the kernel-owned `Arc<GrpcChunkFetcher>`.
    pub fn set_fetcher(&mut self, fetcher: Arc<dyn RemoteChunkFetcher>) {
        self.fetcher = Some(fetcher);
    }

    /// Read content by etag (content hash). Local-only convenience — use
    /// `read_content_with_origins` when the file may have remote-only chunks.
    pub fn read_content(&self, etag: &str) -> Result<Vec<u8>, CASError> {
        self.read_content_with_origins(etag, &[])
    }

    /// Read content by etag (content hash) with optional scatter-gather fall-back.
    ///
    /// If a `ChunkAssembler` is injected, it gets first look at every blob.
    /// Chunked manifests are reassembled transparently; when a chunk is
    /// missing locally AND the engine has a `RemoteChunkFetcher` AND
    /// `origins` is non-empty, the assembler fans out to the origins.
    pub fn read_content_with_origins(
        &self,
        etag: &str,
        origins: &[String],
    ) -> Result<Vec<u8>, CASError> {
        let data = self.transport.read_blob(etag).map_err(|e| match e.kind() {
            io::ErrorKind::NotFound => CASError::NotFound(etag.to_string()),
            _ => CASError::IOError(e),
        })?;

        // CDC composition: delegate to ChunkAssembler if present
        if let Some(assembler) = &self.chunk_assembler {
            let fetcher = self.fetcher.as_deref();
            if let Some(reassembled) =
                assembler.try_reassemble(&data, &self.transport, fetcher, origins)?
            {
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

    /// Expose the chunking strategy for diagnostics / ad-hoc dispatch.
    /// Symmetric with `transport()` — returns `None` when no strategy is
    /// injected (raw single-blob-only mode).
    pub fn strategy(&self) -> Option<&Arc<dyn ChunkingStrategy>> {
        self.chunking_strategy.as_ref()
    }

    /// Is the given content hash a chunked manifest? Inspects the `.meta`
    /// sidecar. Mirrors Python `CDCEngine.is_chunked`.
    ///
    /// Fast path: `meta_exists` short-circuits — single-blob content has no
    /// `.meta` sidecar, so this is O(stat).
    pub fn is_chunked(&self, etag: &str) -> bool {
        if !self.transport.meta_exists(etag) {
            return false;
        }
        let data = match self.transport.read_meta(etag) {
            Ok(d) => d,
            Err(_) => return false,
        };
        let parsed: Value = match serde_json::from_slice(&data) {
            Ok(v) => v,
            Err(_) => return false,
        };
        parsed
            .get("is_chunked_manifest")
            .and_then(|b| b.as_bool())
            .unwrap_or(false)
    }

    /// Total content size. For chunked content, reads from the manifest's
    /// `.meta` sidecar (which records the pre-chunking size). For single
    /// blobs, delegates to `transport.blob_size`. Mirrors Python
    /// `CDCEngine.get_size` + `CASAddressingEngine.get_content_size`.
    pub fn get_size(&self, etag: &str) -> Result<u64, CASError> {
        if self.transport.meta_exists(etag) {
            let data = self.transport.read_meta(etag).map_err(|e| match e.kind() {
                io::ErrorKind::NotFound => CASError::NotFound(etag.to_string()),
                _ => CASError::IOError(e),
            })?;
            let parsed: Value = serde_json::from_slice(&data)
                .map_err(|e| CASError::IOError(io::Error::new(io::ErrorKind::InvalidData, e)))?;
            return Ok(parsed.get("size").and_then(|n| n.as_u64()).unwrap_or(0));
        }
        self.content_size(etag)
    }

    /// Delete chunked content: manifest blob + `.meta` + every chunk blob +
    /// chunk `.meta`. Best-effort (errors swallowed like Python
    /// `contextlib.suppress`) — GC reachability scan is the backstop.
    pub fn delete_chunked(&self, etag: &str) -> Result<(), CASError> {
        let manifest_data = self.transport.read_blob(etag).map_err(|e| match e.kind() {
            io::ErrorKind::NotFound => CASError::NotFound(etag.to_string()),
            _ => CASError::IOError(e),
        })?;
        let manifest: Value = serde_json::from_slice(&manifest_data)
            .map_err(|e| CASError::IOError(io::Error::new(io::ErrorKind::InvalidData, e)))?;

        if let Some(chunks) = manifest.get("chunks").and_then(|c| c.as_array()) {
            for chunk in chunks {
                if let Some(h) = chunk.get("chunk_hash").and_then(|s| s.as_str()) {
                    let _ = self.transport.remove_blob(h);
                    let _ = self.transport.remove_meta(h);
                }
            }
        }

        let _ = self.transport.remove_blob(etag);
        let _ = self.transport.remove_meta(etag);
        Ok(())
    }

    /// Local-only convenience — forwards to `read_chunked_range_with_origins`
    /// with an empty origin set.
    pub fn read_chunked_range(
        &self,
        etag: &str,
        start: u64,
        end: u64,
    ) -> Result<Vec<u8>, CASError> {
        self.read_chunked_range_with_origins(etag, start, end, &[])
    }

    /// Read byte range `[start, end)` from chunked content. Fetches + verifies
    /// only the overlapping chunks. Missing chunks fall through to the injected
    /// `RemoteChunkFetcher` against `origins`. Mirrors Python
    /// `CDCEngine.read_chunked_range`.
    pub fn read_chunked_range_with_origins(
        &self,
        etag: &str,
        start: u64,
        end: u64,
        origins: &[String],
    ) -> Result<Vec<u8>, CASError> {
        if end <= start {
            return Ok(Vec::new());
        }
        let manifest_data = self.transport.read_blob(etag).map_err(|e| match e.kind() {
            io::ErrorKind::NotFound => CASError::NotFound(etag.to_string()),
            _ => CASError::IOError(e),
        })?;
        let manifest: Value = serde_json::from_slice(&manifest_data)
            .map_err(|e| CASError::IOError(io::Error::new(io::ErrorKind::InvalidData, e)))?;
        let chunks = manifest
            .get("chunks")
            .and_then(|c| c.as_array())
            .ok_or_else(|| {
                CASError::IOError(io::Error::new(
                    io::ErrorKind::InvalidData,
                    "manifest missing chunks",
                ))
            })?;

        let mut overlapping: Vec<(u64, u64, String)> = Vec::new();
        for chunk in chunks {
            let offset = chunk.get("offset").and_then(|o| o.as_u64()).unwrap_or(0);
            let length = chunk.get("length").and_then(|l| l.as_u64()).unwrap_or(0);
            if offset < end && offset.saturating_add(length) > start {
                let hash = chunk
                    .get("chunk_hash")
                    .and_then(|h| h.as_str())
                    .ok_or_else(|| {
                        CASError::IOError(io::Error::new(
                            io::ErrorKind::InvalidData,
                            "chunk missing hash",
                        ))
                    })?;
                overlapping.push((offset, length, hash.to_string()));
            }
        }
        if overlapping.is_empty() {
            return Ok(Vec::new());
        }
        overlapping.sort_by_key(|(o, _, _)| *o);

        let fetcher = self.fetcher.as_deref();
        let mut assembled: Vec<u8> = Vec::new();
        for (_o, _l, hash) in &overlapping {
            let bytes = read_and_verify_chunk(&self.transport, hash, fetcher, origins)?;
            assembled.extend_from_slice(&bytes);
        }

        let first_offset = overlapping[0].0 as i64;
        let raw_start = (start as i64 - first_offset).max(0) as usize;
        let raw_end = (end as i64 - first_offset).max(0) as usize;
        let clamped_end = raw_end.min(assembled.len());
        let clamped_start = raw_start.min(clamped_end);
        Ok(assembled[clamped_start..clamped_end].to_vec())
    }

    /// Local-only convenience — forwards to
    /// `write_chunked_partial_with_origins` with an empty origin set.
    pub fn write_chunked_partial(
        &self,
        old_manifest_hash: &str,
        buf: &[u8],
        offset: u64,
    ) -> Result<String, CASError> {
        self.write_chunked_partial_with_origins(old_manifest_hash, buf, offset, &[])
    }

    /// Partial write into chunked content: splice `buf` at `offset`, rewriting
    /// only the affected chunks. Unaffected chunks are reused. Returns the new
    /// manifest hash. Requires an injected `ChunkingStrategy` that supports
    /// byte-offset partial writes (e.g. FastCDC). Mirrors Python
    /// `CDCEngine.write_chunked_partial`.
    ///
    /// Missing "affected" chunks fall through to the injected
    /// `RemoteChunkFetcher` against `origins` before splicing — the partial
    /// write must be able to read whatever it's going to overwrite, even if
    /// replication hasn't caught up.
    pub fn write_chunked_partial_with_origins(
        &self,
        old_manifest_hash: &str,
        buf: &[u8],
        offset: u64,
        origins: &[String],
    ) -> Result<String, CASError> {
        let strategy = self.chunking_strategy.as_ref().ok_or_else(|| {
            CASError::IOError(io::Error::other(
                "write_chunked_partial requires a ChunkingStrategy",
            ))
        })?;
        if !strategy.supports_partial_writes() {
            return Err(CASError::IOError(io::Error::other(
                "current ChunkingStrategy does not support partial writes",
            )));
        }

        let manifest_data =
            self.transport
                .read_blob(old_manifest_hash)
                .map_err(|e| match e.kind() {
                    io::ErrorKind::NotFound => CASError::NotFound(old_manifest_hash.to_string()),
                    _ => CASError::IOError(e),
                })?;
        let manifest: Value = serde_json::from_slice(&manifest_data)
            .map_err(|e| CASError::IOError(io::Error::new(io::ErrorKind::InvalidData, e)))?;
        let chunks = manifest
            .get("chunks")
            .and_then(|c| c.as_array())
            .ok_or_else(|| {
                CASError::IOError(io::Error::new(
                    io::ErrorKind::InvalidData,
                    "manifest missing chunks",
                ))
            })?;
        let old_total_size = manifest
            .get("total_size")
            .and_then(|n| n.as_u64())
            .unwrap_or(0);

        let write_end = offset.saturating_add(buf.len() as u64);

        let mut prefix: Vec<Value> = Vec::new();
        let mut affected: Vec<(u64, u64, String)> = Vec::new();
        let mut suffix: Vec<Value> = Vec::new();
        for chunk in chunks {
            let c_offset = chunk.get("offset").and_then(|o| o.as_u64()).unwrap_or(0);
            let c_length = chunk.get("length").and_then(|l| l.as_u64()).unwrap_or(0);
            let c_end = c_offset.saturating_add(c_length);
            if c_end <= offset {
                prefix.push(chunk.clone());
            } else if c_offset >= write_end {
                suffix.push(chunk.clone());
            } else {
                let hash = chunk
                    .get("chunk_hash")
                    .and_then(|h| h.as_str())
                    .ok_or_else(|| {
                        CASError::IOError(io::Error::new(
                            io::ErrorKind::InvalidData,
                            "chunk missing hash",
                        ))
                    })?
                    .to_string();
                affected.push((c_offset, c_length, hash));
            }
        }

        let mut new_entries: Vec<Value> = Vec::new();

        if affected.is_empty() {
            let mut region_data: Vec<u8> = Vec::new();
            if offset > old_total_size {
                region_data.resize((offset - old_total_size) as usize, 0);
            }
            region_data.extend_from_slice(buf);

            let base = if suffix.is_empty() {
                old_total_size
            } else {
                offset
            };
            for (rel_off, bytes) in strategy.chunk_content(&region_data)? {
                let length = bytes.len() as u64;
                let chunk_hash = self
                    .transport
                    .write_blob(&bytes)
                    .map_err(CASError::IOError)?;
                new_entries.push(json!({
                    "chunk_hash": chunk_hash,
                    "offset": base + rel_off,
                    "length": length,
                }));
            }
        } else {
            let region_start = affected[0].0;

            let fetcher = self.fetcher.as_deref();
            let mut assembled: Vec<u8> = Vec::new();
            for (_o, _l, hash) in &affected {
                let data = read_and_verify_chunk(&self.transport, hash, fetcher, origins)?;
                assembled.extend_from_slice(&data);
            }

            let splice_start = (offset - region_start) as usize;
            if splice_start > assembled.len() {
                assembled.resize(splice_start, 0);
            }
            let tail_start = (splice_start + buf.len()).min(assembled.len());
            let mut new_region: Vec<u8> = Vec::with_capacity(
                splice_start + buf.len() + assembled.len().saturating_sub(tail_start),
            );
            new_region.extend_from_slice(&assembled[..splice_start]);
            new_region.extend_from_slice(buf);
            new_region.extend_from_slice(&assembled[tail_start..]);

            for (rel_off, bytes) in strategy.chunk_content(&new_region)? {
                let length = bytes.len() as u64;
                let chunk_hash = self
                    .transport
                    .write_blob(&bytes)
                    .map_err(CASError::IOError)?;
                new_entries.push(json!({
                    "chunk_hash": chunk_hash,
                    "offset": region_start + rel_off,
                    "length": length,
                }));
            }
        }

        let mut all_chunks: Vec<Value> =
            Vec::with_capacity(prefix.len() + new_entries.len() + suffix.len());
        all_chunks.extend(prefix);
        all_chunks.extend(new_entries);
        all_chunks.extend(suffix);

        let total_size: u64 = all_chunks
            .iter()
            .map(|c| {
                let o = c.get("offset").and_then(|v| v.as_u64()).unwrap_or(0);
                let l = c.get("length").and_then(|v| v.as_u64()).unwrap_or(0);
                o.saturating_add(l)
            })
            .max()
            .unwrap_or(0);

        let chunk_count = all_chunks.len();
        finalize_manifest(
            all_chunks,
            chunk_count,
            total_size as usize,
            String::new(),
            &self.transport,
        )
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

    // ---------- R10a: is_chunked / get_size / delete_chunked / read_chunked_range ----------

    fn setup_msg_boundary() -> (TempDir, CASEngine) {
        let tmp = TempDir::new().unwrap();
        let transport = LocalCASTransport::new(tmp.path(), false).unwrap();
        let engine = CASEngine::with_strategy(
            transport,
            Arc::new(crate::cas_chunking::MessageBoundaryStrategy),
        );
        (tmp, engine)
    }

    fn sample_conversation() -> Vec<u8> {
        br#"[{"role":"user","content":"hi"},{"role":"assistant","content":"hello"}]"#.to_vec()
    }

    #[test]
    fn test_is_chunked_false_for_plain_blob() {
        let (_tmp, engine) = setup();
        let hash = engine.write_content(b"not chunked").unwrap();
        assert!(!engine.is_chunked(&hash));
    }

    #[test]
    fn test_is_chunked_true_for_manifest() {
        let (_tmp, engine) = setup_msg_boundary();
        let content = sample_conversation();
        let manifest_hash = engine.write_content(&content).unwrap();
        assert!(engine.is_chunked(&manifest_hash));
    }

    #[test]
    fn test_is_chunked_missing_returns_false() {
        let (_tmp, engine) = setup();
        assert!(
            !engine.is_chunked("0000000000000000000000000000000000000000000000000000000000000000")
        );
    }

    #[test]
    fn test_get_size_chunked_reads_meta() {
        let (_tmp, engine) = setup_msg_boundary();
        let content = sample_conversation();
        let manifest_hash = engine.write_content(&content).unwrap();
        let size = engine.get_size(&manifest_hash).unwrap();
        assert_eq!(size, content.len() as u64);
    }

    #[test]
    fn test_get_size_plain_blob_falls_back_to_blob_size() {
        let (_tmp, engine) = setup();
        let content = b"plain blob size";
        let hash = engine.write_content(content).unwrap();
        let size = engine.get_size(&hash).unwrap();
        assert_eq!(size, content.len() as u64);
    }

    #[test]
    fn test_delete_chunked_removes_manifest_and_chunks() {
        let (_tmp, engine) = setup_msg_boundary();
        let content = sample_conversation();
        let manifest_hash = engine.write_content(&content).unwrap();

        // Discover chunk hashes through the manifest blob.
        let manifest_bytes = engine.transport().read_blob(&manifest_hash).unwrap();
        let manifest: Value = serde_json::from_slice(&manifest_bytes).unwrap();
        let chunk_hashes: Vec<String> = manifest["chunks"]
            .as_array()
            .unwrap()
            .iter()
            .map(|c| c["chunk_hash"].as_str().unwrap().to_string())
            .collect();
        assert!(!chunk_hashes.is_empty());
        for h in &chunk_hashes {
            assert!(engine.transport().exists(h));
        }

        engine.delete_chunked(&manifest_hash).unwrap();

        assert!(!engine.transport().exists(&manifest_hash));
        assert!(!engine.transport().meta_exists(&manifest_hash));
        for h in &chunk_hashes {
            assert!(!engine.transport().exists(h));
            assert!(!engine.transport().meta_exists(h));
        }
    }

    #[test]
    fn test_read_chunked_range_full() {
        let (_tmp, engine) = setup_msg_boundary();
        let content = sample_conversation();
        let manifest_hash = engine.write_content(&content).unwrap();

        // Compute re-serialized concatenation: MessageBoundary re-encodes each
        // message via serde_json::to_vec, which may differ from the input
        // bytes. Build the expected concatenation the same way.
        let parsed: Value = serde_json::from_slice(&content).unwrap();
        let mut concat: Vec<u8> = Vec::new();
        for msg in parsed.as_array().unwrap() {
            concat.extend_from_slice(&serde_json::to_vec(msg).unwrap());
        }

        let full = engine
            .read_chunked_range(&manifest_hash, 0, concat.len() as u64)
            .unwrap();
        assert_eq!(full, concat);
    }

    #[test]
    fn test_read_chunked_range_partial() {
        let (_tmp, engine) = setup_msg_boundary();
        let content = sample_conversation();
        let manifest_hash = engine.write_content(&content).unwrap();

        let parsed: Value = serde_json::from_slice(&content).unwrap();
        let mut concat: Vec<u8> = Vec::new();
        for msg in parsed.as_array().unwrap() {
            concat.extend_from_slice(&serde_json::to_vec(msg).unwrap());
        }

        // Middle slice — skip first two bytes, take 10.
        let start = 2u64;
        let end = 12u64.min(concat.len() as u64);
        let slice = engine
            .read_chunked_range(&manifest_hash, start, end)
            .unwrap();
        assert_eq!(slice, &concat[start as usize..end as usize]);
    }

    #[test]
    fn test_read_chunked_range_empty_when_beyond() {
        let (_tmp, engine) = setup_msg_boundary();
        let content = sample_conversation();
        let manifest_hash = engine.write_content(&content).unwrap();
        let slice = engine
            .read_chunked_range(&manifest_hash, 10_000, 11_000)
            .unwrap();
        assert!(slice.is_empty());
    }

    #[test]
    fn test_write_chunked_partial_rejects_non_partial_strategy() {
        let (_tmp, engine) = setup_msg_boundary();
        let content = sample_conversation();
        let manifest_hash = engine.write_content(&content).unwrap();
        let err = engine
            .write_chunked_partial(&manifest_hash, b"xyz", 0)
            .unwrap_err();
        match err {
            CASError::IOError(e) => assert!(e.to_string().to_lowercase().contains("partial")),
            other => panic!("expected IOError, got {:?}", other),
        }
    }

    // ---------- R10-SG: scatter-gather fetcher DI ----------

    use crate::cas_remote::RemoteChunkFetcher as _RemoteFetcher;

    struct RecordingFetcher {
        store: std::sync::Mutex<std::collections::HashMap<String, Vec<u8>>>,
        calls: std::sync::Mutex<Vec<String>>,
    }

    impl RecordingFetcher {
        fn new() -> Self {
            Self {
                store: std::sync::Mutex::new(std::collections::HashMap::new()),
                calls: std::sync::Mutex::new(Vec::new()),
            }
        }
        fn put(&self, hash: &str, bytes: Vec<u8>) {
            self.store.lock().unwrap().insert(hash.to_string(), bytes);
        }
    }

    impl _RemoteFetcher for RecordingFetcher {
        fn fetch_chunk(&self, chunk_hash: &str, _origins: &[String]) -> Option<Vec<u8>> {
            self.calls.lock().unwrap().push(chunk_hash.to_string());
            self.store.lock().unwrap().get(chunk_hash).cloned()
        }
    }

    #[test]
    fn test_read_content_scatter_gather_recovers_missing_chunk() {
        // Write a chunked manifest, remove one chunk locally, inject a fetcher
        // that knows the bytes — read should recover and write-back.
        let (_tmp, mut engine) = setup_msg_boundary();
        let content = sample_conversation();
        let manifest_hash = engine.write_content(&content).unwrap();

        // Grab first chunk hash + content, then delete it locally.
        let manifest_bytes = engine.transport().read_blob(&manifest_hash).unwrap();
        let manifest: Value = serde_json::from_slice(&manifest_bytes).unwrap();
        let first_chunk_hash = manifest["chunks"][0]["chunk_hash"]
            .as_str()
            .unwrap()
            .to_string();
        let first_chunk_bytes = engine.transport().read_blob(&first_chunk_hash).unwrap();
        engine.transport().remove_blob(&first_chunk_hash).unwrap();
        assert!(!engine.transport().exists(&first_chunk_hash));

        // Reading without fetcher fails.
        let err = engine
            .read_content_with_origins(&manifest_hash, &["peer1:2126".to_string()])
            .unwrap_err();
        matches!(err, CASError::NotFound(_));

        // Inject fetcher + origins → read succeeds, write-back caches chunk.
        let fetcher = Arc::new(RecordingFetcher::new());
        fetcher.put(&first_chunk_hash, first_chunk_bytes.clone());
        engine.set_fetcher(Arc::clone(&fetcher) as Arc<dyn _RemoteFetcher>);

        let origins = vec!["peer1:2126".to_string()];
        let read_back = engine
            .read_content_with_origins(&manifest_hash, &origins)
            .unwrap();

        let parsed: Value = serde_json::from_slice(&content).unwrap();
        let mut concat: Vec<u8> = Vec::new();
        for msg in parsed.as_array().unwrap() {
            concat.extend_from_slice(&serde_json::to_vec(msg).unwrap());
        }
        assert_eq!(read_back, concat);
        // Fetcher was consulted exactly once for the missing chunk.
        let calls = fetcher.calls.lock().unwrap();
        assert_eq!(calls.len(), 1);
        assert_eq!(calls[0], first_chunk_hash);
        drop(calls);
        // Chunk was written back locally — the next read is local-only.
        assert!(engine.transport().exists(&first_chunk_hash));
    }

    #[test]
    fn test_read_chunked_range_scatter_gather_recovers_missing_chunk() {
        let (_tmp, mut engine) = setup_msg_boundary();
        let content = sample_conversation();
        let manifest_hash = engine.write_content(&content).unwrap();

        let manifest_bytes = engine.transport().read_blob(&manifest_hash).unwrap();
        let manifest: Value = serde_json::from_slice(&manifest_bytes).unwrap();
        let first_chunk_hash = manifest["chunks"][0]["chunk_hash"]
            .as_str()
            .unwrap()
            .to_string();
        let first_chunk_bytes = engine.transport().read_blob(&first_chunk_hash).unwrap();
        engine.transport().remove_blob(&first_chunk_hash).unwrap();

        let fetcher = Arc::new(RecordingFetcher::new());
        fetcher.put(&first_chunk_hash, first_chunk_bytes);
        engine.set_fetcher(Arc::clone(&fetcher) as Arc<dyn _RemoteFetcher>);

        let origins = vec!["peer1:2126".to_string()];
        let slice = engine
            .read_chunked_range_with_origins(&manifest_hash, 0, 4, &origins)
            .unwrap();
        assert_eq!(slice.len(), 4);
        assert!(engine.transport().exists(&first_chunk_hash));
    }

    #[test]
    fn test_write_chunked_partial_fastcdc_roundtrip() {
        // Build a content that FastCDC will chunk (> 16 MiB threshold).
        let (_tmp, engine) = setup();
        let mut content: Vec<u8> = Vec::with_capacity(17 * 1024 * 1024);
        for i in 0..content.capacity() {
            content.push((i as u8).wrapping_mul(31));
        }
        let manifest_hash = engine.write_content(&content).unwrap();
        assert!(engine.is_chunked(&manifest_hash));

        // Splice a 1 KiB region at offset 1 MiB.
        let buf = vec![0xEFu8; 1024];
        let splice_offset = 1024u64 * 1024;
        let new_hash = engine
            .write_chunked_partial(&manifest_hash, &buf, splice_offset)
            .unwrap();
        assert_ne!(new_hash, manifest_hash);
        assert!(engine.is_chunked(&new_hash));

        // Read back via read_content (ChunkAssembler reassembles).
        let read_back = engine.read_content(&new_hash).unwrap();
        let mut expected = content.clone();
        let start = splice_offset as usize;
        expected[start..start + buf.len()].copy_from_slice(&buf);
        assert_eq!(read_back.len(), expected.len());
        assert_eq!(&read_back[..start], &expected[..start]);
        assert_eq!(
            &read_back[start..start + buf.len()],
            &expected[start..start + buf.len()]
        );
        assert_eq!(
            &read_back[start + buf.len()..],
            &expected[start + buf.len()..]
        );
    }
}
