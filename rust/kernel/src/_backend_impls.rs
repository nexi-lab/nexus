//! ObjectStore impls — `CasLocalBackend`, `PathLocalBackend`,
//! `LocalConnectorBackend`. Holding pen until Phase D lifts them into
//! the parallel `backends/` crate. Trait declarations are now in
//! `crate::core::traits::object_store`; the `crate::backend` shim
//! re-exports everything so existing `use crate::backend::*` keeps
//! working through Phase B–C.

use std::fs;
use std::io;
use std::path::{Path, PathBuf};

use crate::cas_engine::CASEngine;
use crate::cas_transport::LocalCASTransport;
use crate::core::traits::object_store::{ObjectStore, StorageError, WriteResult};

/// CAS + Local transport backend (Rust equivalent of Python CASLocalBackend).
///
/// Newtype around CASEngine to implement ObjectStore trait.
pub(crate) struct CasLocalBackend(CASEngine);

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
        fetcher: std::sync::Arc<dyn crate::cas_remote::RemoteChunkFetcher>,
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
        _ctx: &crate::kernel::OperationContext,
    ) -> Result<Vec<u8>, StorageError> {
        self.0.read_content(content_id).map_err(StorageError::from)
    }

    fn write_content(
        &self,
        content: &[u8],
        content_id: &str,
        _ctx: &crate::kernel::OperationContext,
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

// ── PathLocalBackend ────────────────────────────────────────────────

/// Path-based local filesystem backend (Rust equivalent of Python PathLocalBackend).
///
/// Files are stored at their actual paths under `root_path`. No CAS
/// transformation, no deduplication. `content_id` is the blob path.
pub(crate) struct PathLocalBackend {
    root_path: PathBuf,
    fsync: bool,
}

impl PathLocalBackend {
    pub fn new(root: &Path, fsync: bool) -> io::Result<Self> {
        fs::create_dir_all(root)?;
        Ok(Self {
            root_path: root.to_path_buf(),
            fsync,
        })
    }

    /// Resolve backend_path to absolute file path under root.
    fn resolve_path(&self, backend_path: &str) -> Result<PathBuf, StorageError> {
        let clean = backend_path.trim_start_matches('/');
        if clean.contains("..") {
            return Err(StorageError::IOError(io::Error::new(
                io::ErrorKind::InvalidInput,
                format!("path traversal detected: {backend_path}"),
            )));
        }
        Ok(self.root_path.join(clean))
    }
}

impl ObjectStore for PathLocalBackend {
    fn name(&self) -> &str {
        "path_local"
    }

    fn write_content(
        &self,
        content: &[u8],
        content_id: &str,
        _ctx: &crate::kernel::OperationContext,
        offset: u64,
    ) -> Result<WriteResult, StorageError> {
        if content_id.is_empty() {
            return Err(StorageError::IOError(io::Error::new(
                io::ErrorKind::InvalidInput,
                "PathLocalBackend requires content_id (blob path)",
            )));
        }
        let file_path = self.resolve_path(content_id)?;

        // Ensure parent directory exists (only needed on create; open-for-write
        // below errors cleanly if the parent vanishes mid-op).
        if let Some(parent) = file_path.parent() {
            fs::create_dir_all(parent).map_err(StorageError::IOError)?;
        }

        if offset == 0 {
            // Fast path: truncate + write full content. Hash + size
            // come from `content` directly — no read-back needed.
            fs::write(&file_path, content).map_err(StorageError::IOError)?;
            if self.fsync {
                if let Ok(f) = fs::File::open(&file_path) {
                    let _ = f.sync_all();
                }
            }
            let hash = lib::hash::hash_content(content);
            return Ok(WriteResult {
                content_id: hash.clone(),
                version: hash,
                size: content.len() as u64,
            });
        }

        // R20.10 partial-write slow path: open for rw, extend via
        // set_len so the file system zero-fills the hole when offset >
        // current size (POSIX sparse-file semantics — ext4/xfs/ntfs
        // all honor this), then seek + write_all. `create(true)` so we
        // don't fail when backend_path was never written before —
        // matches pwrite(O_CREAT); kernel gates on "file exists" at
        // the metastore layer, not here.
        use std::io::{Seek, SeekFrom, Write};
        let mut f = fs::OpenOptions::new()
            .create(true)
            .truncate(false)
            .write(true)
            .read(false)
            .open(&file_path)
            .map_err(StorageError::IOError)?;
        let cur_len = f.metadata().map_err(StorageError::IOError)?.len();
        let new_len = cur_len.max(offset + content.len() as u64);
        if new_len > cur_len {
            f.set_len(new_len).map_err(StorageError::IOError)?;
        }
        f.seek(SeekFrom::Start(offset))
            .map_err(StorageError::IOError)?;
        f.write_all(content).map_err(StorageError::IOError)?;

        if self.fsync {
            let _ = f.sync_all();
        }
        drop(f);

        // Partial writes only: final bytes differ from `content`, so
        // we must read back to compute the post-splice hash + size.
        // Gated behind offset > 0 so the common full-overwrite path
        // stays at its pre-R20.10 cost.
        let final_bytes = fs::read(&file_path).map_err(StorageError::IOError)?;
        let hash = lib::hash::hash_content(&final_bytes);
        Ok(WriteResult {
            content_id: hash.clone(),
            version: hash,
            size: final_bytes.len() as u64,
        })
    }

    fn read_content(
        &self,
        _content_id: &str,
        backend_path: &str,
        _ctx: &crate::kernel::OperationContext,
    ) -> Result<Vec<u8>, StorageError> {
        if backend_path.is_empty() {
            return Err(StorageError::IOError(io::Error::new(
                io::ErrorKind::InvalidInput,
                "PathLocalBackend requires backend_path",
            )));
        }
        let file_path = self.resolve_path(backend_path)?;
        fs::read(&file_path).map_err(|e| {
            if e.kind() == io::ErrorKind::NotFound {
                StorageError::NotFound(backend_path.to_string())
            } else {
                StorageError::IOError(e)
            }
        })
    }

    fn delete_content(&self, content_id: &str) -> Result<(), StorageError> {
        // For PAS local, content_id is not the path — need backend_path from context.
        // In practice, kernel calls sys_unlink which does metastore.delete() + backend cleanup.
        // delete_content with just content_id (hash) is a no-op for path backends.
        let _ = content_id;
        Ok(())
    }

    fn get_content_size(&self, _content_id: &str) -> Result<u64, StorageError> {
        Err(StorageError::NotSupported(
            "PathLocalBackend.get_content_size requires backend_path",
        ))
    }

    fn mkdir(&self, path: &str, parents: bool, _exist_ok: bool) -> Result<(), StorageError> {
        let dir_path = self.resolve_path(path)?;
        if parents {
            fs::create_dir_all(&dir_path).map_err(StorageError::IOError)
        } else {
            fs::create_dir(&dir_path).map_err(StorageError::IOError)
        }
    }

    fn rmdir(&self, path: &str, recursive: bool) -> Result<(), StorageError> {
        let dir_path = self.resolve_path(path)?;
        if recursive {
            fs::remove_dir_all(&dir_path).map_err(StorageError::IOError)
        } else {
            fs::remove_dir(&dir_path).map_err(StorageError::IOError)
        }
    }

    fn delete_file(&self, path: &str) -> Result<(), StorageError> {
        let file_path = self.resolve_path(path)?;
        fs::remove_file(&file_path).map_err(|e| {
            if e.kind() == io::ErrorKind::NotFound {
                StorageError::NotFound(path.to_string())
            } else {
                StorageError::IOError(e)
            }
        })
    }

    fn rename(&self, old_path: &str, new_path: &str) -> Result<(), StorageError> {
        let old = self.resolve_path(old_path)?;
        let new = self.resolve_path(new_path)?;
        // Ensure parent directory of destination exists
        if let Some(parent) = new.parent() {
            fs::create_dir_all(parent).map_err(StorageError::IOError)?;
        }
        fs::rename(&old, &new).map_err(StorageError::IOError)
    }

    fn copy_file(&self, src_path: &str, dst_path: &str) -> Result<WriteResult, StorageError> {
        let src = self.resolve_path(src_path)?;
        let dst = self.resolve_path(dst_path)?;
        if let Some(parent) = dst.parent() {
            fs::create_dir_all(parent).map_err(StorageError::IOError)?;
        }
        let size = fs::copy(&src, &dst).map_err(StorageError::IOError)?;
        let content = fs::read(&dst).map_err(StorageError::IOError)?;
        let hash = lib::hash::hash_content(&content);
        Ok(WriteResult {
            content_id: hash.clone(),
            version: hash,
            size,
        })
    }

    fn list_dir(&self, path: &str) -> Result<Vec<String>, StorageError> {
        let dir_path = if path.is_empty() {
            self.root_path.clone()
        } else {
            self.resolve_path(path)?
        };
        let rd = fs::read_dir(&dir_path).map_err(|e| {
            if e.kind() == io::ErrorKind::NotFound {
                StorageError::NotFound(path.to_string())
            } else {
                StorageError::IOError(e)
            }
        })?;
        let mut entries = Vec::new();
        for entry in rd {
            let entry = entry.map_err(StorageError::IOError)?;
            let name = entry.file_name().to_string_lossy().into_owned();
            let ft = entry.file_type().map_err(StorageError::IOError)?;
            if ft.is_dir() {
                entries.push(format!("{name}/"));
            } else {
                entries.push(name);
            }
        }
        entries.sort();
        Ok(entries)
    }
}

// ── LocalConnectorBackend ──────────────────────────────────────────

/// Local filesystem connector backend (reference mode, no CAS).
///
/// Mounts an external local folder into Nexus. Files remain at original
/// location (Single Source of Truth). Supports symlink following with
/// escape detection (resolved path must stay within root).
pub(crate) struct LocalConnectorBackend {
    root_path: PathBuf,
    follow_symlinks: bool,
    fsync: bool,
}

impl LocalConnectorBackend {
    pub fn new(root: &Path, follow_symlinks: bool, fsync: bool) -> io::Result<Self> {
        if !root.exists() {
            return Err(io::Error::new(
                io::ErrorKind::NotFound,
                format!("local_connector root does not exist: {}", root.display()),
            ));
        }
        Ok(Self {
            root_path: fs::canonicalize(root)?,
            follow_symlinks,
            fsync,
        })
    }

    /// Resolve virtual path to physical path with escape detection.
    fn resolve_path(&self, virtual_path: &str) -> Result<PathBuf, StorageError> {
        let clean = virtual_path.trim_start_matches('/');
        if clean.contains("..") {
            return Err(StorageError::IOError(io::Error::new(
                io::ErrorKind::InvalidInput,
                format!("path traversal detected: {virtual_path}"),
            )));
        }
        let physical = self.root_path.join(clean);

        let resolved = if self.follow_symlinks {
            // Resolve symlinks, falling back to parent resolution if path doesn't exist yet
            match fs::canonicalize(&physical) {
                Ok(p) => p,
                Err(_) => {
                    // Path may not exist yet (write). Resolve parent + leaf.
                    if let Some(parent) = physical.parent() {
                        match fs::canonicalize(parent) {
                            Ok(p) => p.join(physical.file_name().unwrap_or_default()),
                            Err(_) => physical.clone(),
                        }
                    } else {
                        physical.clone()
                    }
                }
            }
        } else {
            physical.clone()
        };

        // Escape detection: resolved path must be under root
        if !resolved.starts_with(&self.root_path) {
            return Err(StorageError::IOError(io::Error::new(
                io::ErrorKind::PermissionDenied,
                format!("path escapes mount root: {virtual_path}"),
            )));
        }

        Ok(resolved)
    }
}

impl ObjectStore for LocalConnectorBackend {
    fn name(&self) -> &str {
        "local_connector"
    }

    fn write_content(
        &self,
        content: &[u8],
        content_id: &str,
        _ctx: &crate::kernel::OperationContext,
        offset: u64,
    ) -> Result<WriteResult, StorageError> {
        if content_id.is_empty() {
            return Err(StorageError::IOError(io::Error::new(
                io::ErrorKind::InvalidInput,
                "LocalConnectorBackend requires content_id (backend_path)",
            )));
        }
        let file_path = self.resolve_path(content_id)?;

        if let Some(parent) = file_path.parent() {
            fs::create_dir_all(parent).map_err(StorageError::IOError)?;
        }

        if offset == 0 {
            // Fast path: full overwrite, hash `content` directly.
            fs::write(&file_path, content).map_err(StorageError::IOError)?;
            if self.fsync {
                if let Ok(f) = fs::File::open(&file_path) {
                    let _ = f.sync_all();
                }
            }
            let hash = lib::hash::hash_content(content);
            return Ok(WriteResult {
                content_id: hash.clone(),
                version: hash,
                size: content.len() as u64,
            });
        }

        // R20.10 pwrite slow path — see PathLocalBackend for rationale.
        use std::io::{Seek, SeekFrom, Write};
        let mut f = fs::OpenOptions::new()
            .create(true)
            .truncate(false)
            .write(true)
            .open(&file_path)
            .map_err(StorageError::IOError)?;
        let cur_len = f.metadata().map_err(StorageError::IOError)?.len();
        let new_len = cur_len.max(offset + content.len() as u64);
        if new_len > cur_len {
            f.set_len(new_len).map_err(StorageError::IOError)?;
        }
        f.seek(SeekFrom::Start(offset))
            .map_err(StorageError::IOError)?;
        f.write_all(content).map_err(StorageError::IOError)?;

        if self.fsync {
            let _ = f.sync_all();
        }
        drop(f);

        let final_bytes = fs::read(&file_path).map_err(StorageError::IOError)?;
        let hash = lib::hash::hash_content(&final_bytes);
        Ok(WriteResult {
            content_id: hash.clone(),
            version: hash,
            size: final_bytes.len() as u64,
        })
    }

    fn read_content(
        &self,
        _content_id: &str,
        backend_path: &str,
        _ctx: &crate::kernel::OperationContext,
    ) -> Result<Vec<u8>, StorageError> {
        if backend_path.is_empty() {
            return Err(StorageError::IOError(io::Error::new(
                io::ErrorKind::InvalidInput,
                "LocalConnectorBackend requires backend_path",
            )));
        }
        let file_path = self.resolve_path(backend_path)?;
        fs::read(&file_path).map_err(|e| {
            if e.kind() == io::ErrorKind::NotFound {
                StorageError::NotFound(backend_path.to_string())
            } else {
                StorageError::IOError(e)
            }
        })
    }

    fn delete_content(&self, _content_id: &str) -> Result<(), StorageError> {
        // For reference-mode connector, delete_content by hash is not meaningful.
        // Actual deletion happens via backend_path through kernel sys_unlink flow.
        Ok(())
    }

    fn mkdir(&self, path: &str, parents: bool, _exist_ok: bool) -> Result<(), StorageError> {
        let dir_path = self.resolve_path(path)?;
        if parents {
            fs::create_dir_all(&dir_path).map_err(StorageError::IOError)
        } else {
            fs::create_dir(&dir_path).map_err(StorageError::IOError)
        }
    }

    fn rmdir(&self, path: &str, recursive: bool) -> Result<(), StorageError> {
        let dir_path = self.resolve_path(path)?;
        if recursive {
            fs::remove_dir_all(&dir_path).map_err(StorageError::IOError)
        } else {
            fs::remove_dir(&dir_path).map_err(StorageError::IOError)
        }
    }

    fn delete_file(&self, path: &str) -> Result<(), StorageError> {
        let file_path = self.resolve_path(path)?;
        fs::remove_file(&file_path).map_err(|e| {
            if e.kind() == io::ErrorKind::NotFound {
                StorageError::NotFound(path.to_string())
            } else {
                StorageError::IOError(e)
            }
        })
    }

    fn rename(&self, old_path: &str, new_path: &str) -> Result<(), StorageError> {
        let old = self.resolve_path(old_path)?;
        let new = self.resolve_path(new_path)?;
        if let Some(parent) = new.parent() {
            fs::create_dir_all(parent).map_err(StorageError::IOError)?;
        }
        fs::rename(&old, &new).map_err(StorageError::IOError)
    }

    fn copy_file(&self, src_path: &str, dst_path: &str) -> Result<WriteResult, StorageError> {
        let src = self.resolve_path(src_path)?;
        let dst = self.resolve_path(dst_path)?;
        if let Some(parent) = dst.parent() {
            fs::create_dir_all(parent).map_err(StorageError::IOError)?;
        }
        let size = fs::copy(&src, &dst).map_err(StorageError::IOError)?;
        let content = fs::read(&dst).map_err(StorageError::IOError)?;
        let hash = lib::hash::hash_content(&content);
        Ok(WriteResult {
            content_id: hash.clone(),
            version: hash,
            size,
        })
    }

    fn list_dir(&self, path: &str) -> Result<Vec<String>, StorageError> {
        let dir_path = if path.is_empty() {
            self.root_path.clone()
        } else {
            self.resolve_path(path)?
        };
        let rd = fs::read_dir(&dir_path).map_err(|e| {
            if e.kind() == io::ErrorKind::NotFound {
                StorageError::NotFound(path.to_string())
            } else {
                StorageError::IOError(e)
            }
        })?;
        let mut entries = Vec::new();
        for entry in rd {
            let entry = entry.map_err(StorageError::IOError)?;
            let name = entry.file_name().to_string_lossy().into_owned();
            let ft = entry.file_type().map_err(StorageError::IOError)?;
            if ft.is_dir() {
                entries.push(format!("{name}/"));
            } else {
                entries.push(name);
            }
        }
        entries.sort();
        Ok(entries)
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
