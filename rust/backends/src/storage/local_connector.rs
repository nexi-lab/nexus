//! `LocalConnectorBackend` — local-folder reference connector.
//!
//! Phase 2 lifted this out of `kernel/src/_backend_impls.rs`.
//! Reference mode: mounts an external local folder into Nexus
//! without copying.  Files stay at their original location (Single
//! Source of Truth).  Optional symlink following with escape
//! detection (resolved path must stay within the mount root).

use std::fs;
use std::io;
use std::path::{Path, PathBuf};

use kernel::abc::object_store::{ObjectStore, StorageError, WriteResult};

///
/// Mounts an external local folder into Nexus. Files remain at original
/// location (Single Source of Truth). Supports symlink following with
/// escape detection (resolved path must stay within root).
pub struct LocalConnectorBackend {
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
    pub(crate) fn resolve_path(&self, virtual_path: &str) -> Result<PathBuf, StorageError> {
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
        _ctx: &kernel::kernel::OperationContext,
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
                content_id: content_id.to_string(),
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
            content_id: content_id.to_string(),
            version: hash,
            size: final_bytes.len() as u64,
        })
    }

    fn read_content(
        &self,
        content_id: &str,
        _ctx: &kernel::kernel::OperationContext,
    ) -> Result<Vec<u8>, StorageError> {
        if content_id.is_empty() {
            return Err(StorageError::IOError(io::Error::new(
                io::ErrorKind::InvalidInput,
                "LocalConnectorBackend requires content_id",
            )));
        }
        let file_path = self.resolve_path(content_id)?;
        fs::read(&file_path).map_err(|e| {
            if e.kind() == io::ErrorKind::NotFound {
                StorageError::NotFound(content_id.to_string())
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
            content_id: dst_path.to_string(),
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
