//! `PathLocalBackend` — path-addressed local filesystem ObjectStore impl.
//!
//! Phase 2 lifted this out of `kernel/src/_backend_impls.rs` along
//! with `CasLocalBackend` and `LocalConnectorBackend`.  Path-addressed
//! storage: files live at `root_path/<content_id>` where `content_id`
//! is the blob path, no CAS hashing.  Used by mounts that want
//! reference-mode local storage (the data already lives at the path
//! you give us; we don't move it).

use std::fs;
use std::io;
use std::path::{Path, PathBuf};

use kernel::abc::object_store::{ObjectStore, StorageError, WriteResult};

// ── PathLocalBackend ────────────────────────────────────────────────

/// Path-based local filesystem backend (Rust equivalent of Python PathLocalBackend).
///
/// Files are stored at their actual paths under `root_path`. No CAS
/// transformation, no deduplication. `content_id` is the blob path.
pub struct PathLocalBackend {
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
    pub(crate) fn resolve_path(&self, backend_path: &str) -> Result<PathBuf, StorageError> {
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
        _ctx: &kernel::kernel::OperationContext,
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
        _ctx: &kernel::kernel::OperationContext,
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
