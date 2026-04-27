//! `CasLocalBackend` — CAS-addressed local-disk ObjectStore impl.
//!
//! Phase 2 lifted this out of `kernel/src/_backend_impls.rs`.
//! Composes the kernel's CAS primitive
//! ([`kernel::cas_engine::CASEngine`]) with
//! [`kernel::cas_transport::LocalCASTransport`] for the on-disk
//! blob layout, exposing the result as an `ObjectStore` impl that
//! mounts plug into via the `BackendFactory`.

use std::fs;
use std::io;
use std::path::{Path, PathBuf};

use kernel::abc::object_store::{ObjectStore, StorageError, WriteResult};
use kernel::cas_engine::CASEngine;
use kernel::cas_transport::LocalCASTransport;

/// CAS + Local transport backend (Rust equivalent of Python CASLocalBackend).
///
/// Newtype around CASEngine to implement ObjectStore trait.
pub struct CasLocalBackend(CASEngine);

impl CasLocalBackend {
    #[allow(dead_code)]
    pub fn new(root: &Path, fsync: bool) -> io::Result<Self> {
        let transport = LocalCASTransport::new(root, fsync)?;
        Ok(Self(CASEngine::new(transport)))
    }

    /// Build a backend with a scatter-gather fetcher pre-wired. Used by
    /// `add_mount` so every per-mount `CASEngine` can fall through to
    /// peer RPCs on local chunk miss.
    pub fn new_with_fetcher(
        root: &Path,
        fsync: bool,
        fetcher: std::sync::Arc<dyn kernel::cas_remote::RemoteChunkFetcher>,
    ) -> io::Result<Self> {
        let transport = LocalCASTransport::new(root, fsync)?;
        let mut engine = CASEngine::new(transport);
        engine.set_fetcher(fetcher);
        Ok(Self(engine))
    }
}

impl ObjectStore for CasLocalBackend {
    fn name(&self) -> &str {
        "local"
    }

    #[allow(private_interfaces)]
    fn as_cas(&self) -> Option<&CASEngine> {
        Some(&self.0)
    }

    fn read_content(
        &self,
        content_id: &str,
        _backend_path: &str,
        _ctx: &kernel::kernel::OperationContext,
    ) -> Result<Vec<u8>, StorageError> {
        self.0.read_content(content_id).map_err(StorageError::from)
    }

    fn write_content(
        &self,
        content: &[u8],
        content_id: &str,
        _ctx: &kernel::kernel::OperationContext,
        offset: u64,
    ) -> Result<WriteResult, StorageError> {
        if offset == 0 {
            // Fast path: full-content write (new hash = hash(content)).
            let hash = self.0.write_content(content).map_err(StorageError::from)?;
            return Ok(WriteResult {
                version: hash.clone(),
                size: content.len() as u64,
                content_id: hash,
            });
        }
        // R20.10 partial write: splice `content` at `offset` against the
        // OLD CAS object identified by `content_id`. CASEngine handles
        // both chunked (CDC re-chunk affected region) and non-chunked
        // (RMW single blob) cases, and honors POSIX zero-fill when
        // offset > old size.
        if content_id.is_empty() {
            return Err(StorageError::IOError(io::Error::new(
                io::ErrorKind::InvalidInput,
                "CasLocalBackend partial write requires content_id (old hash)",
            )));
        }
        let new_hash = self
            .0
            .write_partial(content_id, content, offset, &[])
            .map_err(StorageError::from)?;
        // New size = max(old_size, offset + content.len()). For the common
        // case of splice-within-bounds we'd need a get_content_size call;
        // read_content_size is available so use it.
        let new_size = self
            .0
            .content_size(&new_hash)
            .map_err(StorageError::from)
            .unwrap_or(offset + content.len() as u64);
        Ok(WriteResult {
            version: new_hash.clone(),
            size: new_size,
            content_id: new_hash,
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
    use crate::storage::local_connector::LocalConnectorBackend;
    use crate::storage::path_local::PathLocalBackend;
    use tempfile::TempDir;

    fn setup() -> (TempDir, CasLocalBackend) {
        let tmp = TempDir::new().unwrap();
        let backend = CasLocalBackend::new(tmp.path(), false).unwrap();
        (tmp, backend)
    }

    fn test_ctx() -> kernel::kernel::OperationContext {
        kernel::kernel::OperationContext::new("test", "root", false, None, false)
    }

    #[test]
    fn test_cas_local_backend_write_and_read() {
        let (_tmp, backend) = setup();
        let ctx = test_ctx();
        let content = b"hello via ObjectStore";

        let result = backend.write_content(content, "", &ctx, 0).unwrap();
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

        let r1 = backend.write_content(content, "", &ctx, 0).unwrap();
        let r2 = backend.write_content(content, "", &ctx, 0).unwrap();
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
        let r = backend.write_content(b"to delete", "", &ctx, 0).unwrap();
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
        let r = backend.write_content(content, "", &ctx, 0).unwrap();
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

    // ── PathLocalBackend tests ────────────────────────────────────────

    fn setup_path() -> (TempDir, PathLocalBackend) {
        let tmp = TempDir::new().unwrap();
        let backend = PathLocalBackend::new(tmp.path(), false).unwrap();
        (tmp, backend)
    }

    #[test]
    fn test_path_local_write_and_read() {
        let (_tmp, backend) = setup_path();
        let ctx = test_ctx();
        let content = b"hello via path backend";

        let wr = backend
            .write_content(content, "docs/file.txt", &ctx, 0)
            .unwrap();
        assert_eq!(wr.size, content.len() as u64);
        assert_eq!(wr.content_id.len(), 64); // hash

        let data = backend
            .read_content(&wr.content_id, "docs/file.txt", &ctx)
            .unwrap();
        assert_eq!(data, content);
    }

    #[test]
    fn test_path_local_overwrite() {
        let (_tmp, backend) = setup_path();
        let ctx = test_ctx();

        backend.write_content(b"v1", "file.txt", &ctx, 0).unwrap();
        backend.write_content(b"v2", "file.txt", &ctx, 0).unwrap();

        let data = backend.read_content("", "file.txt", &ctx).unwrap();
        assert_eq!(data, b"v2");
    }

    #[test]
    fn test_path_local_not_found() {
        let (_tmp, backend) = setup_path();
        let ctx = test_ctx();
        let result = backend.read_content("", "nonexistent.txt", &ctx);
        assert!(matches!(result.unwrap_err(), StorageError::NotFound(_)));
    }

    #[test]
    fn test_path_local_mkdir_rmdir() {
        let (_tmp, backend) = setup_path();
        backend.mkdir("mydir", false, false).unwrap();
        assert!(backend.resolve_path("mydir").unwrap().is_dir());
        backend.rmdir("mydir", false).unwrap();
        assert!(!backend.resolve_path("mydir").unwrap().exists());
    }

    #[test]
    fn test_path_local_rejects_traversal() {
        let (_tmp, backend) = setup_path();
        let ctx = test_ctx();
        let result = backend.write_content(b"evil", "../../etc/passwd", &ctx, 0);
        assert!(result.is_err());
    }

    #[test]
    fn test_path_local_name() {
        let (_tmp, backend) = setup_path();
        assert_eq!(backend.name(), "path_local");
    }

    #[test]
    fn test_path_local_empty_content_id_errors() {
        let (_tmp, backend) = setup_path();
        let ctx = test_ctx();
        let result = backend.write_content(b"data", "", &ctx, 0);
        assert!(result.is_err());
    }

    // ── LocalConnectorBackend tests ─────────────────────────────────

    fn setup_connector() -> (TempDir, LocalConnectorBackend) {
        let tmp = TempDir::new().unwrap();
        let backend = LocalConnectorBackend::new(tmp.path(), true, false).unwrap();
        (tmp, backend)
    }

    #[test]
    fn test_connector_write_and_read() {
        let (_tmp, backend) = setup_connector();
        let ctx = test_ctx();
        let content = b"hello via connector";

        let wr = backend
            .write_content(content, "docs/file.txt", &ctx, 0)
            .unwrap();
        assert_eq!(wr.size, content.len() as u64);

        let data = backend
            .read_content(&wr.content_id, "docs/file.txt", &ctx)
            .unwrap();
        assert_eq!(data, content);
    }

    #[test]
    fn test_connector_name() {
        let (_tmp, backend) = setup_connector();
        assert_eq!(backend.name(), "local_connector");
    }

    #[test]
    fn test_connector_rejects_traversal() {
        let (_tmp, backend) = setup_connector();
        let ctx = test_ctx();
        let result = backend.write_content(b"evil", "../../etc/passwd", &ctx, 0);
        assert!(result.is_err());
    }

    #[test]
    fn test_connector_mkdir_rmdir() {
        let (_tmp, backend) = setup_connector();
        backend.mkdir("subdir", false, false).unwrap();
        assert!(backend.resolve_path("subdir").unwrap().is_dir());
        backend.rmdir("subdir", false).unwrap();
        assert!(!backend.resolve_path("subdir").unwrap().exists());
    }

    #[test]
    fn test_connector_nonexistent_root_errors() {
        let result = LocalConnectorBackend::new(Path::new("/nonexistent/root"), true, false);
        assert!(result.is_err());
    }

    // ── R20.10: partial-write (pwrite semantics) tests ────────────────

    #[test]
    fn test_path_local_partial_write_splices_middle() {
        let (_tmp, backend) = setup_path();
        let ctx = test_ctx();
        backend
            .write_content(b"hello world!", "file.txt", &ctx, 0)
            .unwrap();
        // Splice "RUST!" at offset 6 → "hello RUST!!"
        backend
            .write_content(b"RUST!", "file.txt", &ctx, 6)
            .unwrap();
        let data = backend.read_content("", "file.txt", &ctx).unwrap();
        assert_eq!(data, b"hello RUST!!");
    }

    #[test]
    fn test_path_local_partial_write_zero_fills_gap() {
        // POSIX pwrite semantic: offset past EOF zero-fills the hole.
        let (_tmp, backend) = setup_path();
        let ctx = test_ctx();
        backend.write_content(b"ab", "sparse.txt", &ctx, 0).unwrap();
        backend
            .write_content(b"xyz", "sparse.txt", &ctx, 5)
            .unwrap();
        let data = backend.read_content("", "sparse.txt", &ctx).unwrap();
        assert_eq!(data, b"ab\x00\x00\x00xyz");
    }

    #[test]
    fn test_path_local_partial_write_extends_past_end() {
        // offset+len exceeds current size but offset <= size: splice + extend.
        let (_tmp, backend) = setup_path();
        let ctx = test_ctx();
        backend.write_content(b"head", "ext.txt", &ctx, 0).unwrap();
        backend.write_content(b"TAIL", "ext.txt", &ctx, 2).unwrap();
        let data = backend.read_content("", "ext.txt", &ctx).unwrap();
        assert_eq!(data, b"heTAIL");
    }

    #[test]
    fn test_cas_local_partial_write_non_chunked() {
        let (_tmp, backend) = setup();
        let ctx = test_ctx();
        let original = b"hello world!";
        let wr = backend.write_content(original, "", &ctx, 0).unwrap();
        // Splice "RUST!" at offset 6 using the old hash as content_id.
        let new_wr = backend
            .write_content(b"RUST!", &wr.content_id, &ctx, 6)
            .unwrap();
        assert_ne!(new_wr.content_id, wr.content_id); // new blob → new hash
        let data = backend.read_content(&new_wr.content_id, "", &ctx).unwrap();
        assert_eq!(data, b"hello RUST!!");
    }

    #[test]
    fn test_cas_local_partial_write_zero_fills_gap() {
        let (_tmp, backend) = setup();
        let ctx = test_ctx();
        let wr = backend.write_content(b"ab", "", &ctx, 0).unwrap();
        let new_wr = backend
            .write_content(b"xyz", &wr.content_id, &ctx, 5)
            .unwrap();
        let data = backend.read_content(&new_wr.content_id, "", &ctx).unwrap();
        assert_eq!(data, b"ab\x00\x00\x00xyz");
    }
}
