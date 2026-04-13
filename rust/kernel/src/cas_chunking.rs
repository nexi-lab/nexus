//! CDC chunk assembly — pluggable composition for CAS read path.
//!
//! Extracts the inline manifest detection from `CASEngine.read_content()`
//! into a composable trait. Matches the Python CDC `ChunkingStrategy` DI
//! pattern used by `CASAddressingEngine`.
//!
//! `CASEngine` takes `Option<Arc<dyn ChunkAssembler>>` via DI — if present,
//! the assembler gets first look at every blob read; if the blob is a chunked
//! manifest, the assembler reassembles the chunks and returns the original
//! content.

use crate::cas_engine::CASError;
use crate::cas_transport::LocalCASTransport;
use serde_json::Value;
use std::sync::Arc;

// ---------------------------------------------------------------------------
// ChunkAssembler trait
// ---------------------------------------------------------------------------

/// Pluggable chunk reassembly for CAS read path (DI composition).
///
/// If `try_reassemble` returns `Some(bytes)`, the caller uses those bytes
/// instead of the raw blob. If `None`, the blob is returned as-is.
pub(crate) trait ChunkAssembler: Send + Sync {
    fn try_reassemble(
        &self,
        data: &[u8],
        transport: &LocalCASTransport,
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
    ) -> Result<Option<Vec<u8>>, CASError> {
        // Fast reject: only check blobs < 500KB that start with the manifest prefix
        if data.len() >= 500 * 1024 {
            return Ok(None);
        }
        if !data
            .get(..30)
            .is_some_and(|p| p.starts_with(b"{\"type\":\"chunked_manifest"))
        {
            return Ok(None);
        }

        let manifest: Value = match serde_json::from_slice(data) {
            Ok(v) => v,
            Err(_) => return Ok(None),
        };

        let chunks = match manifest.get("chunks").and_then(|c| c.as_array()) {
            Some(c) => c,
            None => return Ok(None),
        };

        reassemble_chunks(chunks, transport).map(Some)
    }
}

/// Reassemble CDC chunks from manifest chunk array.
fn reassemble_chunks(chunks: &[Value], transport: &LocalCASTransport) -> Result<Vec<u8>, CASError> {
    let mut parts: Vec<(i64, Vec<u8>)> = Vec::with_capacity(chunks.len());
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
        let data = transport.read_blob(hash).map_err(|e| match e.kind() {
            std::io::ErrorKind::NotFound => CASError::NotFound(hash.to_string()),
            _ => CASError::IOError(e),
        })?;
        parts.push((offset, data));
    }

    parts.sort_by_key(|(offset, _)| *offset);
    let total: usize = parts.iter().map(|(_, d)| d.len()).sum();
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
