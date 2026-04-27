//! ObjectStore pillar — Rust kernel `file_operations` contract.
//!
//! Rust equivalent of Python `ObjectStoreABC` (one of the Four Storage Pillars).
//! Each impl composes an addressing strategy with a transport:
//!
//!   `CasLocalBackend` = CAS addressing + LocalCASTransport
//!
//! Naming convention per `backend-architecture.md`: `<Addressing><Transport>Backend`.
//!
//! Concrete impls (`CasLocalBackend`, `PathLocalBackend`,
//! `LocalConnectorBackend`, plus the connector-feature ones) sit in
//! `_backend_impls.rs` until Phase D lifts them into the parallel
//! `backends/` crate.

use std::io;

use crate::cas_engine::CASError;

/// Error type for ObjectStore operations.
#[derive(Debug)]
#[allow(dead_code)]
pub enum StorageError {
    /// Content not found.
    NotFound(String),
    /// Underlying I/O error.
    IOError(io::Error),
    /// Operation not supported by this backend.
    NotSupported(&'static str),
}

impl From<CASError> for StorageError {
    fn from(e: CASError) -> Self {
        match e {
            CASError::NotFound(s) => StorageError::NotFound(s),
            CASError::IOError(e) => StorageError::IOError(e),
        }
    }
}

/// Result of a write operation (equivalent to Python `WriteResult`).
///
/// - `content_id`: Primary addressing key (opaque).
///   CAS backends: SHA-256 hex digest.
///   PAS backends: blob path or version ID.
/// - `version`: OCC token for conflict detection.
///   CAS: same as content_id (hash is immutable).
///   PAS: cloud version_id or content hash.
/// - `size`: Content size in bytes.
#[allow(dead_code)]
pub struct WriteResult {
    pub(crate) content_id: String,
    pub(crate) version: String,
    pub(crate) size: u64,
}

// ── ExternalTransport ──────────────────────────────────────────
// Transport-layer capability for backends that can generate direct-access
// URLs (presigned/signed). Separate from ObjectStore: not all storage
// needs this, and it belongs at the transport abstraction level, not the
// content-operations level. Only S3/GCS (and future cloud backends like
// Azure, MinIO, R2) implement this.

/// Transport-layer trait for generating direct-access URLs.
///
/// Enables clients to download/upload directly from cloud storage without
/// routing bytes through Nexus (offloading I/O, reducing memory footprint).
pub trait ExternalTransport: Send + Sync {
    /// Generate a time-limited download URL for the given object key.
    ///
    /// Returns `Ok(url_string)` on success, or `Err` if the backend cannot
    /// generate a signed URL (e.g. missing credentials).
    fn generate_download_url(
        &self,
        object_key: &str,
        expires_seconds: u64,
    ) -> Result<String, StorageError>;
}

/// ObjectStore pillar — kernel `file_operations` contract.
///
/// Rust equivalent of Python `ObjectStoreABC` (one of the Four Storage Pillars).
/// CAS/PAS agnostic: `content_id` is an opaque key whose semantics are
/// backend-defined (hash for CAS, blob path for PAS).
///
/// 6 abstract methods matching the Python ABC:
///   - write_content, read_content, delete_content, get_content_size
///   - mkdir, rmdir
///
/// Streaming (write_stream, stream_content, stream_range) and batch
/// (batch_read/write/delete) have default impls in Python; they are
/// not needed in the Rust kernel hot path and can be added later.
#[allow(dead_code)]
pub trait ObjectStore: Send + Sync {
    /// Backend identifier (e.g. "local", "gcs", "s3").
    fn name(&self) -> &str;

    /// Downcast to `&CASEngine` for CAS-specific operations. Default
    /// returns `None` for non-CAS backends (PAS, external connectors).
    /// Only `CasLocalBackend` overrides. Used by the `PyKernel::cas_*`
    /// surface so Python delegators can reach the CAS API without every
    /// backend carrying CAS-shaped noise.
    #[allow(private_interfaces)]
    fn as_cas(&self) -> Option<&crate::cas_engine::CASEngine> {
        None
    }

    /// Downcast to a streaming-capable LLM backend. Default returns `None`.
    /// Only `OpenAIBackend` (and future `AnthropicBackend`) override.
    /// Consumed by `PyKernel::llm_start_streaming` — any ObjectStore that
    /// returns `Some` must implement the full SSE → DT_STREAM →
    /// `CASEngine::write_content_tracked` pipeline.
    #[cfg(feature = "connectors")]
    fn as_llm_streaming(&self) -> Option<&dyn crate::openai_streaming::LlmStreamingBackend> {
        None
    }

    /// Write content to storage and return a `WriteResult`.
    ///
    /// - `content_id`: Target address for the content.
    ///   CAS backends: when `offset == 0`, ignored (new hash = hash of content);
    ///   when `offset > 0`, the OLD content hash the partial write is applied
    ///   against (required so CAS CDC can read+splice).
    ///   PAS backends: blob path where content will be stored.
    /// - `ctx`: Operation context (carries backend_path, auth, TTL).
    /// - `offset`: POSIX `pwrite(2)` semantics.
    ///
    ///   `offset == 0` is a full-file write (truncate + write) — current
    ///   behavior for every caller that predates the partial-write wiring
    ///   (R20.10).
    ///
    ///   `offset > 0` splices `content` starting at `offset`, preserving
    ///   bytes before `offset` and after `offset + content.len()`. When
    ///   `offset > current_size`, the gap is zero-filled (POSIX sparse-
    ///   file semantics).
    ///
    ///   Every backend that accepts `offset > 0` MUST honor this contract;
    ///   backends whose transport does not support seekable / range writes
    ///   (cloud object stores like S3, GCS — their PUT replaces the entire
    ///   object) MUST return `Err(StorageError::NotSupported)` on
    ///   `offset > 0` rather than silently falling back to read-splice-
    ///   write (that fallback would violate the caller's cost expectation
    ///   of pwrite — O(content.len()) would become O(full_blob) network
    ///   I/O).
    fn write_content(
        &self,
        content: &[u8],
        content_id: &str,
        ctx: &crate::kernel::OperationContext,
        offset: u64,
    ) -> Result<WriteResult, StorageError>;

    /// Read content by opaque identifier.
    ///
    /// `content_id`: CAS=hash, PAS=version_id.
    /// `backend_path`: mount-relative path (for path-addressed backends).
    /// `ctx`: operation credential for backends that need identity/auth.
    /// CAS backends ignore backend_path and ctx.
    fn read_content(
        &self,
        content_id: &str,
        backend_path: &str,
        ctx: &crate::kernel::OperationContext,
    ) -> Result<Vec<u8>, StorageError>;

    /// Delete content by identifier.
    fn delete_content(&self, content_id: &str) -> Result<(), StorageError> {
        let _ = content_id;
        Err(StorageError::NotSupported("delete_content"))
    }

    /// Get content size in bytes.
    fn get_content_size(&self, content_id: &str) -> Result<u64, StorageError> {
        let _ = content_id;
        Err(StorageError::NotSupported("get_content_size"))
    }

    /// Create a directory.
    fn mkdir(&self, path: &str, parents: bool, exist_ok: bool) -> Result<(), StorageError> {
        let _ = (path, parents, exist_ok);
        Err(StorageError::NotSupported("mkdir"))
    }

    /// Remove a directory.
    fn rmdir(&self, path: &str, recursive: bool) -> Result<(), StorageError> {
        let _ = (path, recursive);
        Err(StorageError::NotSupported("rmdir"))
    }

    /// Delete a file by path (PAS backends — reference-mode).
    ///
    /// CAS backends return `NotSupported` (content-addressed blobs are GC'd).
    /// PAS backends delete the physical file at the given path.
    fn delete_file(&self, path: &str) -> Result<(), StorageError> {
        let _ = path;
        Err(StorageError::NotSupported("delete_file"))
    }

    /// Rename/move a file or directory (PAS backends — reference-mode).
    ///
    /// CAS backends return `NotSupported` (paths are virtual, not physical).
    /// PAS backends rename the physical file on the host filesystem.
    fn rename(&self, old_path: &str, new_path: &str) -> Result<(), StorageError> {
        let _ = (old_path, new_path);
        Err(StorageError::NotSupported("rename"))
    }

    /// Server-side copy (PAS backends — reference-mode).
    ///
    /// Copies the physical file at `src_path` to `dst_path` within the same
    /// backend storage. CAS backends return `NotSupported` — CAS copy is
    /// metadata-only (content is deduplicated by hash), handled at the kernel
    /// layer without touching the backend.
    ///
    /// Returns `WriteResult` with the destination's content_id and size.
    fn copy_file(&self, src_path: &str, dst_path: &str) -> Result<WriteResult, StorageError> {
        let _ = (src_path, dst_path);
        Err(StorageError::NotSupported("copy_file"))
    }

    /// List direct children of a directory path.
    ///
    /// Returns entry names (not full paths). Each entry is a plain filename;
    /// directories are suffixed with `/` so callers can distinguish files
    /// from directories without a follow-up stat.
    ///
    /// Default returns `NotSupported` for backends that don't have a
    /// directory concept (CAS, remote). Filesystem backends (PathLocal,
    /// LocalConnector) use `std::fs::read_dir`; API connectors (HN, CLI, X)
    /// synthesize listings from their virtual namespace.
    fn list_dir(&self, path: &str) -> Result<Vec<String>, StorageError> {
        let _ = path;
        Err(StorageError::NotSupported("list_dir"))
    }
}
