//! CDC chunking — pluggable composition for CAS read + write paths.
//!
//! Two DI traits mirror the Python `ChunkingStrategy` Protocol:
//!   - `ChunkingStrategy` (write): if content should be chunked, split it,
//!     store chunks + manifest + `.meta` sidecar, return the manifest hash.
//!     Two implementations — `FastCDCStrategy` (content-defined chunking for
//!     large blobs) and `MessageBoundaryStrategy` (LLM-conversation JSON,
//!     one chunk per message for cross-conversation prefix dedup).
//!   - `ChunkAssembler` (read): if a blob is a chunked manifest, reassemble
//!     into the original content. Shared across strategies because all
//!     writers emit the same `{"type":"chunked_manifest","chunks":[...]}`
//!     format. `CASEngine.read_content()` delegates here.
//!
//! Split traits (Rust composition) instead of one big Python-style Protocol:
//! the write path and read path have no overlapping state, and many callers
//! need only the reader.

use crate::cas_engine::CASError;
use crate::cas_remote::RemoteChunkFetcher;
use crate::cas_transport::LocalCASTransport;
use serde_json::{json, Value};
use std::sync::Arc;

// ---------------------------------------------------------------------------
// CDC tuning constants (must match Python `nexus.backends.engines.cdc`)
// ---------------------------------------------------------------------------

/// Minimum content size to trigger CDC chunking (16 MiB).
pub(crate) const CDC_THRESHOLD_BYTES: usize = 16 * 1024 * 1024;
/// FastCDC minimum chunk size (256 KiB).
const CDC_MIN_CHUNK_SIZE: u32 = 256 * 1024;
/// FastCDC average chunk size target (1 MiB).
const CDC_AVG_CHUNK_SIZE: u32 = 1024 * 1024;
/// FastCDC maximum chunk size (4 MiB).
const CDC_MAX_CHUNK_SIZE: u32 = 4 * 1024 * 1024;

// ---------------------------------------------------------------------------
// ChunkAssembler trait
// ---------------------------------------------------------------------------

/// Pluggable chunk reassembly for CAS read path (DI composition).
///
/// If `try_reassemble` returns `Some(bytes)`, the caller uses those bytes
/// instead of the raw blob. If `None`, the blob is returned as-is.
///
/// `fetcher + origins` enable scatter-gather: when a chunk is missing
/// locally, the assembler calls `fetcher.fetch_chunk(hash, origins)` to
/// pull it from a peer, hash-verifies, writes back to local CAS, and
/// retries. `fetcher: None` preserves local-only behaviour (unit tests).
pub(crate) trait ChunkAssembler: Send + Sync {
    fn try_reassemble(
        &self,
        data: &[u8],
        transport: &LocalCASTransport,
        fetcher: Option<&dyn RemoteChunkFetcher>,
        origins: &[String],
    ) -> Result<Option<Vec<u8>>, CASError>;
}

// ---------------------------------------------------------------------------
// ChunkedManifestAssembler — default implementation
// ---------------------------------------------------------------------------

/// Default assembler: detects `{"type":"chunked_manifest...` JSON prefix
/// and reassembles chunks from the transport.
///
/// This is the same logic that was previously inlined in
/// `CASEngine::read_content()` — now extracted for composition.
pub(crate) struct ChunkedManifestAssembler;

impl ChunkAssembler for ChunkedManifestAssembler {
    fn try_reassemble(
        &self,
        data: &[u8],
        transport: &LocalCASTransport,
        fetcher: Option<&dyn RemoteChunkFetcher>,
        origins: &[String],
    ) -> Result<Option<Vec<u8>>, CASError> {
        // Fast reject: only check blobs < 500KB (manifests are always small —
        // ~100 bytes per chunk entry). Parse as JSON and check the `type`
        // field directly — key order is not guaranteed across serializers
        // (serde_json default sorts alphabetically, Python dict preserves
        // insertion order, so anchored prefix matches are unreliable).
        if data.len() >= 500 * 1024 {
            return Ok(None);
        }
        // Cheap pre-check: must start with `{` to even be JSON object.
        if data.first() != Some(&b'{') {
            return Ok(None);
        }

        let manifest: Value = match serde_json::from_slice(data) {
            Ok(v) => v,
            Err(_) => return Ok(None),
        };

        // Only act on blobs whose top-level `type` is `chunked_manifest`;
        // leaves every other JSON blob untouched.
        let is_manifest = manifest
            .get("type")
            .and_then(|t| t.as_str())
            .is_some_and(|t| t == "chunked_manifest");
        if !is_manifest {
            return Ok(None);
        }

        let chunks = match manifest.get("chunks").and_then(|c| c.as_array()) {
            Some(c) => c,
            None => return Ok(None),
        };

        reassemble_chunks(chunks, transport, fetcher, origins).map(Some)
    }
}

/// Read a single chunk + verify its BLAKE3 hash matches the expected value.
///
/// Mirrors Python `CDCEngine._read_and_verify_chunk`. Shared by
/// `reassemble_chunks` and `CASEngine::read_chunked_range`.
///
/// On a local miss, falls back to `fetcher.fetch_chunk(hash, origins)` —
/// used when metadata has Raft-replicated ahead of the content. The peer
/// response is already hash-verified inside the fetcher; we still double-check
/// here before writing it back so local CAS never holds bad bytes.
pub(crate) fn read_and_verify_chunk(
    transport: &LocalCASTransport,
    expected_hash: &str,
    fetcher: Option<&dyn RemoteChunkFetcher>,
    origins: &[String],
) -> Result<Vec<u8>, CASError> {
    match transport.read_blob(expected_hash) {
        Ok(data) => {
            let actual = lib::hash::hash_content(&data);
            if actual != expected_hash {
                return Err(CASError::IOError(std::io::Error::new(
                    std::io::ErrorKind::InvalidData,
                    format!(
                        "chunk hash mismatch: expected {}, got {}",
                        expected_hash, actual
                    ),
                )));
            }
            Ok(data)
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            // Scatter-gather fall-back: pull from a peer, hash-verify again,
            // write-back to local CAS, return. Idempotent for CAS.
            if let Some(f) = fetcher {
                if !origins.is_empty() {
                    if let Some(bytes) = f.fetch_chunk(expected_hash, origins) {
                        let actual = lib::hash::hash_content(&bytes);
                        if actual != expected_hash {
                            return Err(CASError::IOError(std::io::Error::new(
                                std::io::ErrorKind::InvalidData,
                                format!(
                                    "remote chunk hash mismatch: expected {}, got {}",
                                    expected_hash, actual
                                ),
                            )));
                        }
                        // Write-back. `write_blob_with_hash` is dedup-aware.
                        let _ = transport.write_blob_with_hash(&bytes, expected_hash);
                        return Ok(bytes);
                    }
                }
            }
            Err(CASError::NotFound(expected_hash.to_string()))
        }
        Err(e) => Err(CASError::IOError(e)),
    }
}

/// Reassemble CDC chunks from manifest chunk array (full-content path).
///
/// Validates that chunks cover `[0, total)` exactly — no gaps, no overlaps,
/// no negative offsets (develop § review fix #7). A malformed manifest
/// that once produced silently corrupt content now surfaces as
/// `CASError::IOError`.
pub(crate) fn reassemble_chunks(
    chunks: &[Value],
    transport: &LocalCASTransport,
    fetcher: Option<&dyn RemoteChunkFetcher>,
    origins: &[String],
) -> Result<Vec<u8>, CASError> {
    let mut parts: Vec<(u64, Vec<u8>)> = Vec::with_capacity(chunks.len());
    for chunk in chunks {
        let hash = chunk
            .get("chunk_hash")
            .and_then(|h| h.as_str())
            .ok_or_else(|| {
                CASError::IOError(std::io::Error::new(
                    std::io::ErrorKind::InvalidData,
                    "missing chunk_hash",
                ))
            })?;
        let offset = chunk.get("offset").and_then(|o| o.as_i64()).unwrap_or(0);
        if offset < 0 {
            return Err(CASError::IOError(std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                format!("negative chunk offset {offset} for {hash}"),
            )));
        }
        let data = read_and_verify_chunk(transport, hash, fetcher, origins)?;
        parts.push((offset as u64, data));
    }

    parts.sort_by_key(|(offset, _)| *offset);

    // Reject gaps / overlaps. We require chunks to start at 0 and each
    // subsequent chunk to begin exactly where the previous chunk ended.
    let mut expected: u64 = 0;
    for (offset, data) in &parts {
        if *offset != expected {
            return Err(CASError::IOError(std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                format!(
                    "chunked manifest has {} at offset {offset} (expected {expected})",
                    if *offset > expected { "gap" } else { "overlap" },
                ),
            )));
        }
        expected = offset.checked_add(data.len() as u64).ok_or_else(|| {
            CASError::IOError(std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                "chunked manifest total size overflows u64",
            ))
        })?;
    }

    let total: usize = expected.try_into().map_err(|_| {
        CASError::IOError(std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            "chunked manifest total size exceeds addressable memory",
        ))
    })?;
    let mut result = Vec::with_capacity(total);
    for (_, data) in parts {
        result.extend_from_slice(&data);
    }
    Ok(result)
}

/// Create the default chunk assembler (convenience constructor).
#[allow(dead_code)]
pub(crate) fn default_chunk_assembler() -> Arc<dyn ChunkAssembler> {
    Arc::new(ChunkedManifestAssembler)
}

// ---------------------------------------------------------------------------
// ChunkingStrategy trait (write-side composition)
// ---------------------------------------------------------------------------

/// Pluggable chunked-write strategy for the CAS write path (DI composition).
///
/// Name matches the Python `ChunkingStrategy` Protocol so the two sides stay
/// aligned even though the Rust trait is write-only (reads share the single
/// `ChunkAssembler` above since every strategy emits the same manifest format).
pub trait ChunkingStrategy: Send + Sync {
    fn should_chunk(&self, content: &[u8]) -> bool;

    /// Chunked write. Returns `(manifest_hash, is_new)` where `is_new`
    /// tracks whether the **top-level manifest hash** was freshly written
    /// (`true`) or already existed (`false`). Individual chunk dedup hits
    /// inside the manifest do not affect this bit — they are normal CAS
    /// deduplication, invisible to the caller.
    fn write_chunked(
        &self,
        content: &[u8],
        transport: &LocalCASTransport,
    ) -> Result<(String, bool), CASError>;

    /// Split `content` into chunks without touching storage.
    /// Returned tuples are `(offset, bytes)`; "offset" means "input byte
    /// offset" for content-defined strategies (FastCDC) and "cumulative
    /// re-serialized offset" for message-boundary strategies.
    ///
    /// Used by `CASEngine::write_chunked_partial` to re-chunk an affected
    /// region without duplicating the boundary-detection logic.
    fn chunk_content(&self, content: &[u8]) -> Result<Vec<(u64, Vec<u8>)>, CASError>;

    /// Whether this strategy supports byte-offset partial writes. True for
    /// FastCDC (content-byte offsets meaningful), false for MessageBoundary
    /// (offsets are virtual — partial writes have no semantic meaning on a
    /// message-array conversation).
    fn supports_partial_writes(&self) -> bool {
        false
    }
}

// ---------------------------------------------------------------------------
// Manifest writer helper — shared by all strategies
// ---------------------------------------------------------------------------

/// Package `chunk_entries` into the canonical
/// `{"type":"chunked_manifest","chunks":[...]}` blob + `.meta` sidecar.
/// Strategies decide boundaries; this helper guarantees every strategy
/// produces something the single `ChunkAssembler` reader can rebuild.
pub(crate) fn finalize_manifest(
    chunk_entries: Vec<Value>,
    chunk_count: usize,
    total_size: usize,
    full_content_hash: String,
    transport: &LocalCASTransport,
) -> Result<(String, bool), CASError> {
    let avg_chunk_size = total_size.checked_div(chunk_count).unwrap_or(0);

    let manifest = json!({
        "type": "chunked_manifest",
        "total_size": total_size,
        "chunk_count": chunk_count,
        "avg_chunk_size": avg_chunk_size,
        "content_id": full_content_hash,
        "chunks": chunk_entries,
    });
    let manifest_bytes =
        serde_json::to_vec(&manifest).map_err(|e| CASError::IOError(std::io::Error::other(e)))?;

    let (manifest_hash, is_new) = transport
        .write_blob_tracked(&manifest_bytes)
        .map_err(CASError::IOError)?;

    // `.meta` sidecar — lets Python `CDCEngine.is_chunked(hash)` recognise
    // Rust-written manifests without opening the blob. Format matches
    // `CASAddressingEngine._write_meta()`.
    let sidecar = json!({
        "size": total_size,
        "is_chunked_manifest": true,
        "chunk_count": chunk_count,
    });
    let sidecar_bytes =
        serde_json::to_vec(&sidecar).map_err(|e| CASError::IOError(std::io::Error::other(e)))?;
    transport
        .write_meta(&manifest_hash, &sidecar_bytes)
        .map_err(CASError::IOError)?;

    Ok((manifest_hash, is_new))
}

// ---------------------------------------------------------------------------
// FastCDCStrategy — content-defined chunking for large blobs
// ---------------------------------------------------------------------------

/// FastCDC + BLAKE3 chunking for generic large content (≥ 16 MiB).
///
/// Byte-compatible with Python `nexus.backends.engines.cdc.CDCEngine` output
/// so a Rust-written chunked file reads fine on a Python side and vice versa
/// (they share the manifest format).
pub(crate) struct FastCDCStrategy;

impl ChunkingStrategy for FastCDCStrategy {
    fn should_chunk(&self, content: &[u8]) -> bool {
        content.len() > CDC_THRESHOLD_BYTES
    }

    fn supports_partial_writes(&self) -> bool {
        true
    }

    fn write_chunked(
        &self,
        content: &[u8],
        transport: &LocalCASTransport,
    ) -> Result<(String, bool), CASError> {
        let total_size = content.len();

        // FastCDC content-defined chunking. Walks `content`, emitting
        // variable-size chunks whose boundaries depend on content (not
        // position), so insert/delete edits only re-hash adjacent chunks.
        let chunker = fastcdc::v2020::FastCDC::new(
            content,
            CDC_MIN_CHUNK_SIZE,
            CDC_AVG_CHUNK_SIZE,
            CDC_MAX_CHUNK_SIZE,
        );

        let mut chunk_entries: Vec<Value> = Vec::new();
        let mut chunk_count = 0usize;
        for chunk in chunker {
            let chunk_bytes = &content[chunk.offset..chunk.offset + chunk.length];
            let chunk_hash = transport
                .write_blob(chunk_bytes)
                .map_err(CASError::IOError)?;
            chunk_entries.push(json!({
                "chunk_hash": chunk_hash,
                "offset": chunk.offset as u64,
                "length": chunk.length as u64,
            }));
            chunk_count += 1;
        }

        let full_content_hash = lib::hash::hash_content(content);
        finalize_manifest(
            chunk_entries,
            chunk_count,
            total_size,
            full_content_hash,
            transport,
        )
    }

    fn chunk_content(&self, content: &[u8]) -> Result<Vec<(u64, Vec<u8>)>, CASError> {
        let chunker = fastcdc::v2020::FastCDC::new(
            content,
            CDC_MIN_CHUNK_SIZE,
            CDC_AVG_CHUNK_SIZE,
            CDC_MAX_CHUNK_SIZE,
        );
        let mut out: Vec<(u64, Vec<u8>)> = Vec::new();
        for chunk in chunker {
            let slice = &content[chunk.offset..chunk.offset + chunk.length];
            out.push((chunk.offset as u64, slice.to_vec()));
        }
        Ok(out)
    }
}

// ---------------------------------------------------------------------------
// MessageBoundaryStrategy — one chunk per message for LLM conversation JSON
// ---------------------------------------------------------------------------

/// Chunk LLM-conversation JSON at message boundaries (one chunk per message)
/// so conversations sharing a prefix dedup via CAS. Always-chunk mode:
/// `should_chunk()` returns true for any valid message array (not size-gated),
/// because LLM conversations are small but benefit heavily from per-message
/// dedup. Returns false for non-conversation content — caller falls back to
/// single-blob CAS storage.
///
/// Mirrors Python
/// `nexus.backends.compute.message_chunking.MessageBoundaryStrategy`.
#[allow(dead_code)]
pub struct MessageBoundaryStrategy;

impl ChunkingStrategy for MessageBoundaryStrategy {
    fn should_chunk(&self, content: &[u8]) -> bool {
        // Valid conversation = JSON array of ≥2 message dicts with a `role`
        // key. Anything else falls through to single-blob.
        let v: Value = match serde_json::from_slice(content) {
            Ok(v) => v,
            Err(_) => return false,
        };
        let Some(arr) = v.as_array() else {
            return false;
        };
        if arr.len() < 2 {
            return false;
        }
        arr.first()
            .and_then(|m| m.as_object())
            .is_some_and(|o| o.contains_key("role"))
    }

    fn write_chunked(
        &self,
        content: &[u8],
        transport: &LocalCASTransport,
    ) -> Result<(String, bool), CASError> {
        let total_size = content.len();
        let full_content_hash = lib::hash::hash_content(content);

        let parsed: Value = serde_json::from_slice(content)
            .map_err(|e| CASError::IOError(std::io::Error::other(e)))?;
        let messages = parsed.as_array().ok_or_else(|| {
            CASError::IOError(std::io::Error::other("expected JSON array of messages"))
        })?;

        // Each message → its own chunk. Offset tracks byte position of the
        // message in the re-encoded concatenation (deterministic ordering
        // for reassembly + dedup parity with Python's implementation).
        let mut chunk_entries: Vec<Value> = Vec::new();
        let mut offset: u64 = 0;
        for msg in messages {
            let msg_bytes =
                serde_json::to_vec(msg).map_err(|e| CASError::IOError(std::io::Error::other(e)))?;
            let chunk_hash = transport
                .write_blob(&msg_bytes)
                .map_err(CASError::IOError)?;
            let length = msg_bytes.len() as u64;
            chunk_entries.push(json!({
                "chunk_hash": chunk_hash,
                "offset": offset,
                "length": length,
            }));
            offset += length;
        }

        finalize_manifest(
            chunk_entries,
            messages.len(),
            total_size,
            full_content_hash,
            transport,
        )
    }

    fn chunk_content(&self, content: &[u8]) -> Result<Vec<(u64, Vec<u8>)>, CASError> {
        let parsed: Value = serde_json::from_slice(content)
            .map_err(|e| CASError::IOError(std::io::Error::other(e)))?;
        let messages = parsed.as_array().ok_or_else(|| {
            CASError::IOError(std::io::Error::other("expected JSON array of messages"))
        })?;
        let mut out: Vec<(u64, Vec<u8>)> = Vec::new();
        let mut offset: u64 = 0;
        for msg in messages {
            let msg_bytes =
                serde_json::to_vec(msg).map_err(|e| CASError::IOError(std::io::Error::other(e)))?;
            let length = msg_bytes.len() as u64;
            out.push((offset, msg_bytes));
            offset += length;
        }
        Ok(out)
    }
}

/// Create the default chunking strategy (FastCDC — generic CAS writes).
/// LLM backends explicitly inject `MessageBoundaryStrategy`.
#[allow(dead_code)]
pub(crate) fn default_chunking_strategy() -> Arc<dyn ChunkingStrategy> {
    Arc::new(FastCDCStrategy)
}
