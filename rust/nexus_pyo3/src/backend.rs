//! Kernel storage backend — pure Rust, no Python, no GIL.
//!
//! `StorageBackend` trait is the backend-level ABC for the Rust kernel.
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

/// Error type for storage backend operations.
#[derive(Debug)]
#[allow(dead_code)]
pub(crate) enum StorageError {
    /// Content hash not found in storage.
    NotFound(String),
    /// Underlying I/O error.
    IOError(io::Error),
}

impl From<CASError> for StorageError {
    fn from(e: CASError) -> Self {
        match e {
            CASError::NotFound(s) => StorageError::NotFound(s),
            CASError::IOError(e) => StorageError::IOError(e),
        }
    }
}

/// Kernel storage backend — pure Rust, no Python, no GIL.
pub(crate) trait StorageBackend: Send + Sync {
    fn read_content(&self, content_id: &str) -> Result<Vec<u8>, StorageError>;
    fn write_content(&self, content: &[u8]) -> Result<String, StorageError>;
}

/// CAS + Local transport backend (Rust equivalent of Python CASLocalBackend).
///
/// Newtype around CASEngine to implement StorageBackend trait
/// (avoids method name collision with CASEngine::read_content).
pub(crate) struct CasLocalBackend(CASEngine);

impl CasLocalBackend {
    pub fn new(root: &Path, fsync: bool) -> io::Result<Self> {
        let transport = LocalCASTransport::new(root, fsync)?;
        Ok(Self(CASEngine::new(transport)))
    }
}

impl StorageBackend for CasLocalBackend {
    fn read_content(&self, content_id: &str) -> Result<Vec<u8>, StorageError> {
        self.0.read_content(content_id).map_err(StorageError::from)
    }

    fn write_content(&self, content: &[u8]) -> Result<String, StorageError> {
        self.0.write_content(content).map_err(StorageError::from)
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

    #[test]
    fn test_cas_local_backend_write_and_read() {
        let (_tmp, backend) = setup();
        let content = b"hello via StorageBackend";

        let hash = backend.write_content(content).unwrap();
        assert_eq!(hash.len(), 64);

        let read_back = backend.read_content(&hash).unwrap();
        assert_eq!(read_back, content);
    }

    #[test]
    fn test_cas_local_backend_not_found() {
        let (_tmp, backend) = setup();
        let result = backend
            .read_content("0000000000000000000000000000000000000000000000000000000000000000");
        assert!(result.is_err());
        assert!(matches!(result.unwrap_err(), StorageError::NotFound(_)));
    }

    #[test]
    fn test_cas_local_backend_dedup() {
        let (_tmp, backend) = setup();
        let content = b"dedup via StorageBackend";

        let hash1 = backend.write_content(content).unwrap();
        let hash2 = backend.write_content(content).unwrap();
        assert_eq!(hash1, hash2);
    }
}
