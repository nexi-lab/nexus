//! Kernel-backed storage for password_vault.
//!
//! All data stored via kernel syscalls (`sys_read`, `write`,
//! `sys_readdir`), using the VFS metastore as the storage backend.
//! This is the cross-repo integration seam: nexus repo (services
//! crate) → nexus-vfs repo (kernel crate, git dep).
//!
//! VFS path convention:
//!   - `{root}/entries/{title}` → bincode(EntryIndex)
//!   - `{root}/versions/{title}/{version:010}` → bincode(StoredEntry)
//!     Zero-padded version ensures lexicographic = numeric sort.
//!
//! Replaces the previous redb-backed implementation: the HARD
//! INVARIANT in `services/Cargo.toml` requires services to reach
//! backends through `kernel.sys_*` syscalls, never via direct
//! cross-crate imports.

use std::collections::HashMap;
use std::sync::Arc;

use kernel::abc::object_store::{ObjectStore, StorageError, WriteResult};
use kernel::kernel::convenience::KernelConvenience;
use kernel::kernel::{Kernel, KernelError, OperationContext};

use super::types::{EntryIndex, PasswordVaultError, StoredEntry};

const DT_DIR: i32 = 1;
const DT_MOUNT: i32 = 2;

// ── In-memory ObjectStore for vault content ──────────────────────────

/// Minimal in-memory ObjectStore — stores vault entry blobs in a
/// HashMap keyed by content_id. Thread-safe via `parking_lot::Mutex`.
/// No persistence; the kernel metastore (redb) provides durable
/// metadata, and the vault binary can swap in a persistent backend
/// (CasLocal, PathLocal) when data survival across restarts is needed.
struct MemBackend {
    data: parking_lot::Mutex<HashMap<String, Vec<u8>>>,
}

impl MemBackend {
    fn new() -> Self {
        Self {
            data: parking_lot::Mutex::new(HashMap::new()),
        }
    }
}

impl ObjectStore for MemBackend {
    fn name(&self) -> &str {
        "vault-mem"
    }

    fn write_content(
        &self,
        content: &[u8],
        content_id: &str,
        _ctx: &OperationContext,
        _offset: u64,
    ) -> Result<WriteResult, StorageError> {
        let size = content.len() as u64;
        self.data
            .lock()
            .insert(content_id.to_string(), content.to_vec());
        Ok(WriteResult {
            content_id: content_id.to_string(),
            version: content_id.to_string(),
            size,
        })
    }

    fn read_content(
        &self,
        content_id: &str,
        _ctx: &OperationContext,
    ) -> Result<Vec<u8>, StorageError> {
        self.data
            .lock()
            .get(content_id)
            .cloned()
            .ok_or_else(|| StorageError::NotFound(content_id.to_string()))
    }

    fn delete_content(&self, content_id: &str) -> Result<(), StorageError> {
        self.data.lock().remove(content_id);
        Ok(())
    }
}

// ── Storage ──────────────────────────────────────────────────────────

pub(crate) struct Storage {
    kernel: Arc<Kernel>,
    ctx: OperationContext,
    root: String,
}

impl Storage {
    /// Create storage backed by kernel syscalls. Mounts an in-memory
    /// ObjectStore backend and creates directory structure (`entries/`,
    /// `versions/`) under `root`.
    pub(crate) fn new(kernel: Arc<Kernel>, root: &str) -> Result<Self, PasswordVaultError> {
        let root = root.trim_end_matches('/').to_string();

        // Mount with an in-memory backend so DT_REG content (bincode
        // blobs) can be written/read via kernel syscalls.
        kernel
            .sys_setattr(
                &root,
                DT_MOUNT,
                /* backend_name */ "vault-mem",
                /* backend */ Some(Arc::new(MemBackend::new())),
                /* metastore */ None,
                /* raft_backend */ None,
                /* io_profile */ "memory",
                /* zone_id */ "root",
                /* is_external */ false,
                /* capacity */ 0,
                /* read_fd */ None,
                /* write_fd */ None,
                /* mime_type */ None,
                /* modified_at_ms */ None,
                /* content_id */ None,
                /* size */ None,
                /* version */ None,
                /* created_at_ms */ None,
                /* link_target */ None,
                /* source */ None,
                /* remote_metastore */ None,
            )
            .map_err(|e| PasswordVaultError::Storage(format!("mount {root}: {e:?}")))?;

        let ctx = OperationContext::new(
            /* user_id */ "vault-storage",
            /* zone_id */ "root",
            /* is_admin */ true,
            /* agent_id */ Some("vault-storage"),
            /* is_system */ true,
        );

        let storage = Self { kernel, ctx, root };

        // Ensure directory structure exists.
        storage.ensure_dir(&format!("{}/entries", storage.root))?;
        storage.ensure_dir(&format!("{}/versions", storage.root))?;

        Ok(storage)
    }

    /// Kernel handle accessor — allows callers (e.g. E2E tests) to
    /// cross-verify storage via direct kernel syscalls.
    pub(crate) fn kernel(&self) -> &Arc<Kernel> {
        &self.kernel
    }

    fn ensure_dir(&self, path: &str) -> Result<(), PasswordVaultError> {
        self.kernel
            .sys_setattr(
                path, DT_DIR, /* backend_name */ "", /* backend */ None,
                /* metastore */ None, /* raft_backend */ None,
                /* io_profile */ "memory", /* zone_id */ "root",
                /* is_external */ false, /* capacity */ 0, /* read_fd */ None,
                /* write_fd */ None, /* mime_type */ None,
                /* modified_at_ms */ None, /* content_id */ None, /* size */ None,
                /* version */ None, /* created_at_ms */ None,
                /* link_target */ None, /* source */ None,
                /* remote_metastore */ None,
            )
            .map(|_| ())
            .map_err(|e| PasswordVaultError::Storage(format!("mkdir {path}: {e:?}")))
    }

    fn entries_path(&self, title: &str) -> String {
        format!("{}/entries/{}", self.root, title)
    }

    fn version_path(&self, title: &str, version: u32) -> String {
        format!("{}/versions/{}/{:010}", self.root, title, version)
    }

    fn versions_dir(&self, title: &str) -> String {
        format!("{}/versions/{}", self.root, title)
    }

    pub(crate) fn get_index(&self, title: &str) -> Result<Option<EntryIndex>, PasswordVaultError> {
        let path = self.entries_path(title);
        match KernelConvenience::read(&*self.kernel, &path, &self.ctx, 0, 0) {
            Ok(result) => {
                let data = result.data.ok_or_else(|| {
                    PasswordVaultError::Storage(format!("get_index {title}: empty read"))
                })?;
                let idx: EntryIndex = bincode::deserialize(&data).map_err(|e| {
                    PasswordVaultError::Storage(format!("decode index {title}: {e}"))
                })?;
                Ok(Some(idx))
            }
            Err(KernelError::FileNotFound(_)) => Ok(None),
            Err(e) => Err(PasswordVaultError::Storage(format!(
                "get_index {title}: {e:?}"
            ))),
        }
    }

    pub(crate) fn list_indexes(&self) -> Result<Vec<(String, EntryIndex)>, PasswordVaultError> {
        let dir = format!("{}/entries", self.root);
        let entries = self.kernel.sys_readdir(&dir, "root", true);
        let mut out = Vec::new();
        for (child_path, _etype) in entries {
            let title = child_path.rsplit('/').next().unwrap_or("").to_string();
            if title.is_empty() {
                continue;
            }
            match KernelConvenience::read(&*self.kernel, &child_path, &self.ctx, 0, 0) {
                Ok(result) => {
                    if let Some(data) = result.data {
                        let idx: EntryIndex = bincode::deserialize(&data).map_err(|e| {
                            PasswordVaultError::Storage(format!("decode index {title}: {e}"))
                        })?;
                        out.push((title, idx));
                    }
                }
                Err(_) => continue,
            }
        }
        Ok(out)
    }

    pub(crate) fn set_index(
        &self,
        title: &str,
        idx: &EntryIndex,
    ) -> Result<(), PasswordVaultError> {
        let path = self.entries_path(title);
        let encoded = bincode::serialize(idx)
            .map_err(|e| PasswordVaultError::Storage(format!("encode index {title}: {e}")))?;
        self.kernel
            .write(&path, &self.ctx, &encoded, 0)
            .map_err(|e| PasswordVaultError::Storage(format!("set_index {title}: {e:?}")))?;
        Ok(())
    }

    pub(crate) fn put_version(
        &self,
        title: &str,
        version: u32,
        entry: &StoredEntry,
    ) -> Result<(), PasswordVaultError> {
        // Ensure per-title version directory exists.
        let title_dir = self.versions_dir(title);
        self.ensure_dir(&title_dir)?;

        let path = self.version_path(title, version);
        let encoded = bincode::serialize(entry)
            .map_err(|e| PasswordVaultError::Storage(format!("encode version: {e}")))?;
        self.kernel
            .write(&path, &self.ctx, &encoded, 0)
            .map_err(|e| {
                PasswordVaultError::Storage(format!("put_version {title}/{version}: {e:?}"))
            })?;
        Ok(())
    }

    pub(crate) fn get_version(
        &self,
        title: &str,
        version: u32,
    ) -> Result<Option<StoredEntry>, PasswordVaultError> {
        let path = self.version_path(title, version);
        match KernelConvenience::read(&*self.kernel, &path, &self.ctx, 0, 0) {
            Ok(result) => {
                let data = result.data.ok_or_else(|| {
                    PasswordVaultError::Storage(format!(
                        "get_version {title}/{version}: empty read"
                    ))
                })?;
                let entry: StoredEntry = bincode::deserialize(&data).map_err(|e| {
                    PasswordVaultError::Storage(format!("decode version {title}/{version}: {e}"))
                })?;
                Ok(Some(entry))
            }
            Err(KernelError::FileNotFound(_)) => Ok(None),
            Err(e) => Err(PasswordVaultError::Storage(format!(
                "get_version {title}/{version}: {e:?}"
            ))),
        }
    }

    /// List all versions of a single title, sorted by ascending
    /// version number.
    pub(crate) fn list_versions(
        &self,
        title: &str,
    ) -> Result<Vec<StoredEntry>, PasswordVaultError> {
        let dir = self.versions_dir(title);
        let entries = self.kernel.sys_readdir(&dir, "root", true);
        let mut out = Vec::new();
        for (child_path, _etype) in entries {
            match KernelConvenience::read(&*self.kernel, &child_path, &self.ctx, 0, 0) {
                Ok(result) => {
                    if let Some(data) = result.data {
                        let stored: StoredEntry = bincode::deserialize(&data).map_err(|e| {
                            PasswordVaultError::Storage(format!("decode version row: {e}"))
                        })?;
                        out.push(stored);
                    }
                }
                Err(_) => continue,
            }
        }
        out.sort_by_key(|e| e.version);
        Ok(out)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fresh() -> Storage {
        let kernel = Arc::new(Kernel::new());
        Storage::new(kernel, "/vault").unwrap()
    }

    fn entry(version: u32, ct: &[u8]) -> StoredEntry {
        StoredEntry {
            version,
            created_at_ms: 1_000,
            nonce: [0u8; 12],
            ciphertext: ct.to_vec(),
        }
    }

    fn index(version: u32, deleted: bool) -> EntryIndex {
        EntryIndex {
            current_version: version,
            deleted_at_ms: if deleted { Some(2_000) } else { None },
        }
    }

    #[test]
    fn empty_db_returns_none() {
        let s = fresh();
        assert!(s.get_index("nope").unwrap().is_none());
        assert!(s.get_version("nope", 1).unwrap().is_none());
        assert!(s.list_indexes().unwrap().is_empty());
        assert!(s.list_versions("nope").unwrap().is_empty());
    }

    #[test]
    fn index_round_trip() {
        let s = fresh();
        s.set_index("gmail", &index(3, false)).unwrap();
        let got = s.get_index("gmail").unwrap().unwrap();
        assert_eq!(got.current_version, 3);
        assert!(got.deleted_at_ms.is_none());
    }

    #[test]
    fn version_round_trip() {
        let s = fresh();
        s.put_version("gmail", 1, &entry(1, &[1, 2, 3])).unwrap();
        let got = s.get_version("gmail", 1).unwrap().unwrap();
        assert_eq!(got.version, 1);
        assert_eq!(got.ciphertext, vec![1, 2, 3]);
    }

    #[test]
    fn list_versions_orders_by_version_and_filters_by_title() {
        let s = fresh();
        s.put_version("gmail", 1, &entry(1, b"a")).unwrap();
        s.put_version("gmail", 3, &entry(3, b"c")).unwrap();
        s.put_version("gmail", 2, &entry(2, b"b")).unwrap();
        // unrelated title — must not appear in gmail's history
        s.put_version("github", 1, &entry(1, b"x")).unwrap();

        let versions = s.list_versions("gmail").unwrap();
        let vers: Vec<u32> = versions.iter().map(|e| e.version).collect();
        assert_eq!(vers, vec![1, 2, 3]);
    }

    #[test]
    fn list_indexes_multi() {
        let s = fresh();
        s.set_index("gmail", &index(1, false)).unwrap();
        s.set_index("github", &index(2, true)).unwrap();
        let mut idxs = s.list_indexes().unwrap();
        idxs.sort_by(|a, b| a.0.cmp(&b.0));
        assert_eq!(idxs.len(), 2);
        assert_eq!(idxs[0].0, "github");
        assert!(idxs[0].1.deleted_at_ms.is_some());
        assert_eq!(idxs[1].0, "gmail");
        assert!(idxs[1].1.deleted_at_ms.is_none());
    }

    #[test]
    fn version_key_encoding_sorts_naturally() {
        // Verify zero-padded version filenames sort correctly.
        let a1 = format!("{:010}", 1u32);
        let a2 = format!("{:010}", 2u32);
        let b1 = format!("{:010}", 10u32);
        assert!(a1 < a2);
        assert!(a2 < b1);
    }
}
