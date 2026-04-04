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

/// ObjectStore pillar — kernel `file_operations` contract.
///
/// Rust equivalent of Python `ObjectStoreABC`.
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

    /// Write content and return `(content_id, size)`.
    fn write_content(&self, content: &[u8]) -> Result<String, StorageError>;

    /// Read content by opaque identifier.
    ///
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

    fn write_content(&self, content: &[u8]) -> Result<String, StorageError> {
        self.0.write_content(content).map_err(StorageError::from)
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
        let content = b"hello via ObjectStore";

        let hash = backend.write_content(content).unwrap();
        assert_eq!(hash.len(), 64);

        let read_back = backend.read_content(&hash, "", &test_ctx()).unwrap();
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
        let content = b"dedup via ObjectStore";

        let hash1 = backend.write_content(content).unwrap();
        let hash2 = backend.write_content(content).unwrap();
        assert_eq!(hash1, hash2);
    }

    #[test]
    fn test_cas_local_backend_name() {
        let (_tmp, backend) = setup();
        assert_eq!(backend.name(), "local");
    }

    #[test]
    fn test_cas_local_backend_delete() {
        let (_tmp, backend) = setup();
        let hash = backend.write_content(b"to delete").unwrap();
        assert!(backend.delete_content(&hash).is_ok());
        assert!(matches!(
            backend.read_content(&hash, "", &test_ctx()).unwrap_err(),
            StorageError::NotFound(_)
        ));
    }

    #[test]
    fn test_cas_local_backend_get_content_size() {
        let (_tmp, backend) = setup();
        let content = b"size check";
        let hash = backend.write_content(content).unwrap();
        assert_eq!(
            backend.get_content_size(&hash).unwrap(),
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
