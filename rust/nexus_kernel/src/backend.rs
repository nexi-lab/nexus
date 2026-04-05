//! ObjectStore pillar — Rust kernel `file_operations` contract.
//!
//! Rust equivalent of Python `ObjectStoreABC` (one of the Four Storage Pillars).
//! Each impl composes an addressing strategy with a transport:
//!
//!   `CasLocalBackend` = CAS addressing + LocalCASTransport
//!
//! Naming convention per `backend-architecture.md`: `<Addressing><Transport>Backend`.
//!
//! Future backends:
//!   `CasGcsBackend` = CAS addressing + GCS transport
//!   `PathLocalBackend` = Path addressing + Local transport

use std::io;
use std::path::Path;

use crate::cas_engine::{CASEngine, CASError};
use crate::cas_transport::LocalCASTransport;

/// Error type for ObjectStore operations.
#[derive(Debug)]
#[allow(dead_code)]
pub(crate) enum StorageError {
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
pub(crate) struct WriteResult {
    pub(crate) content_id: String,
    pub(crate) version: String,
    pub(crate) size: u64,
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
pub(crate) trait ObjectStore: Send + Sync {
    /// Backend identifier (e.g. "local", "gcs", "s3").
    fn name(&self) -> &str;

    /// Write content to storage and return a `WriteResult`.
    ///
    /// - `content_id`: Target address for the content.
    ///   CAS backends: ignored (address = hash of content).
    ///   PAS backends: blob path where content will be stored.
    /// - `ctx`: Operation context (carries backend_path, auth, TTL).
    fn write_content(
        &self,
        content: &[u8],
        content_id: &str,
        ctx: &crate::kernel::OperationContext,
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
}

/// CAS + Local transport backend (Rust equivalent of Python CASLocalBackend).
///
/// Newtype around CASEngine to implement ObjectStore trait.
pub(crate) struct CasLocalBackend(CASEngine);

impl CasLocalBackend {
    pub fn new(root: &Path, fsync: bool) -> io::Result<Self> {
        let transport = LocalCASTransport::new(root, fsync)?;
        Ok(Self(CASEngine::new(transport)))
    }
}

impl ObjectStore for CasLocalBackend {
    fn name(&self) -> &str {
        "local"
    }

    fn read_content(
        &self,
        content_id: &str,
        _backend_path: &str,
        _ctx: &crate::kernel::OperationContext,
    ) -> Result<Vec<u8>, StorageError> {
        self.0.read_content(content_id).map_err(StorageError::from)
    }

    fn write_content(
        &self,
        content: &[u8],
        _content_id: &str,
        _ctx: &crate::kernel::OperationContext,
    ) -> Result<WriteResult, StorageError> {
        let hash = self.0.write_content(content).map_err(StorageError::from)?;
        Ok(WriteResult {
            version: hash.clone(),
            size: content.len() as u64,
            content_id: hash,
        })
    }

    fn delete_content(&self, content_id: &str) -> Result<(), StorageError> {
        self.0
            .delete_content(content_id)
            .map_err(StorageError::from)
    }

    fn get_content_size(&self, content_id: &str) -> Result<u64, StorageError> {
        self.0.content_size(content_id).map_err(StorageError::from)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    fn setup() -> (TempDir, CasLocalBackend) {
        let tmp = TempDir::new().unwrap();
        let backend = CasLocalBackend::new(tmp.path(), false).unwrap();
        (tmp, backend)
    }

    fn test_ctx() -> crate::kernel::OperationContext {
        crate::kernel::OperationContext::new("test", "root", false, None, false)
    }

    #[test]
    fn test_cas_local_backend_write_and_read() {
        let (_tmp, backend) = setup();
        let ctx = test_ctx();
        let content = b"hello via ObjectStore";

        let result = backend.write_content(content, "", &ctx).unwrap();
        assert_eq!(result.content_id.len(), 64);
        assert_eq!(result.size, content.len() as u64);
        assert_eq!(result.version, result.content_id); // CAS: version == hash

        let read_back = backend.read_content(&result.content_id, "", &ctx).unwrap();
        assert_eq!(read_back, content);
    }

    #[test]
    fn test_cas_local_backend_not_found() {
        let (_tmp, backend) = setup();
        let result = backend.read_content(
            "0000000000000000000000000000000000000000000000000000000000000000",
            "",
            &test_ctx(),
        );
        assert!(result.is_err());
        assert!(matches!(result.unwrap_err(), StorageError::NotFound(_)));
    }

    #[test]
    fn test_cas_local_backend_dedup() {
        let (_tmp, backend) = setup();
        let ctx = test_ctx();
        let content = b"dedup via ObjectStore";

        let r1 = backend.write_content(content, "", &ctx).unwrap();
        let r2 = backend.write_content(content, "", &ctx).unwrap();
        assert_eq!(r1.content_id, r2.content_id);
    }

    #[test]
    fn test_cas_local_backend_name() {
        let (_tmp, backend) = setup();
        assert_eq!(backend.name(), "local");
    }

    #[test]
    fn test_cas_local_backend_delete() {
        let (_tmp, backend) = setup();
        let ctx = test_ctx();
        let r = backend.write_content(b"to delete", "", &ctx).unwrap();
        assert!(backend.delete_content(&r.content_id).is_ok());
        assert!(matches!(
            backend.read_content(&r.content_id, "", &ctx).unwrap_err(),
            StorageError::NotFound(_)
        ));
    }

    #[test]
    fn test_cas_local_backend_get_content_size() {
        let (_tmp, backend) = setup();
        let ctx = test_ctx();
        let content = b"size check";
        let r = backend.write_content(content, "", &ctx).unwrap();
        assert_eq!(
            backend.get_content_size(&r.content_id).unwrap(),
            content.len() as u64
        );
    }

    #[test]
    fn test_default_mkdir_not_supported() {
        // CasLocalBackend doesn't override mkdir/rmdir defaults
        // (CAS backends have no directory concept)
        let (_tmp, backend) = setup();
        assert!(matches!(
            backend.mkdir("/foo", false, false).unwrap_err(),
            StorageError::NotSupported("mkdir")
        ));
    }
}
