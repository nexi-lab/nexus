//! Kernel-backed storage for generic_secrets.
//!
//! Two-level namespace layout under the vault mount:
//!   - `{root}/secret_entries/{namespace}/{key}` → bincode(SecretIndex)
//!   - `{root}/secret_versions/{namespace}/{key}/{version:010}` → bincode(StoredEntry)
//!
//! Shares the same kernel mount as `PasswordVaultService` — created by
//! the vault plugin at `/vault`. The directory trees are disjoint
//! (`entries/` vs `secret_entries/`), so no path conflicts.

use std::sync::Arc;

use kernel::kernel::convenience::KernelConvenience;
use kernel::kernel::{Kernel, KernelError, OperationContext};

use crate::password_vault::types::{PasswordVaultError, SecretIndex, StoredEntry};

const DT_DIR: i32 = 1;

pub(crate) struct SecretStorage {
    kernel: Arc<Kernel>,
    ctx: OperationContext,
    root: String,
}

impl SecretStorage {
    /// Create storage on an existing kernel mount. Does NOT mount a
    /// backend — expects the caller (vault plugin) to have already
    /// mounted one at `root`. Only creates the directory structure.
    pub(crate) fn new_on_existing_mount(
        kernel: Arc<Kernel>,
        root: &str,
    ) -> Result<Self, PasswordVaultError> {
        let root = root.trim_end_matches('/').to_string();
        let ctx = OperationContext::new("vault-storage", "root", true, Some("vault-storage"), true);
        let storage = Self { kernel, ctx, root };
        storage.ensure_dir(&format!("{}/secret_entries", storage.root))?;
        storage.ensure_dir(&format!("{}/secret_versions", storage.root))?;
        Ok(storage)
    }

    fn ensure_dir(&self, path: &str) -> Result<(), PasswordVaultError> {
        self.kernel
            .sys_setattr(
                path, DT_DIR, "", None, None, None, "memory", "root", false, 0, None, None, None,
                None, None, None, None, None, None, None, None,
            )
            .map(|_| ())
            .map_err(|e| PasswordVaultError::Storage(format!("mkdir {path}: {e:?}")))
    }

    fn entries_path(&self, namespace: &str, key: &str) -> String {
        format!("{}/secret_entries/{}/{}", self.root, namespace, key)
    }

    fn ns_dir_path(&self, namespace: &str) -> String {
        format!("{}/secret_entries/{}", self.root, namespace)
    }

    fn version_path(&self, namespace: &str, key: &str, version: u32) -> String {
        format!(
            "{}/secret_versions/{}/{}/{:010}",
            self.root, namespace, key, version
        )
    }

    fn versions_dir(&self, namespace: &str, key: &str) -> String {
        format!("{}/secret_versions/{}/{}", self.root, namespace, key)
    }

    fn ns_version_dir(&self, namespace: &str) -> String {
        format!("{}/secret_versions/{}", self.root, namespace)
    }

    pub(crate) fn get_index(
        &self,
        namespace: &str,
        key: &str,
    ) -> Result<Option<SecretIndex>, PasswordVaultError> {
        let path = self.entries_path(namespace, key);
        match KernelConvenience::read(&*self.kernel, &path, &self.ctx, 0, 0) {
            Ok(result) => {
                let data = result.data.ok_or_else(|| {
                    PasswordVaultError::Storage(format!("get_index {namespace}/{key}: empty read"))
                })?;
                let idx: SecretIndex = bincode::deserialize(&data).map_err(|e| {
                    PasswordVaultError::Storage(format!(
                        "decode secret index {namespace}/{key}: {e}"
                    ))
                })?;
                Ok(Some(idx))
            }
            Err(KernelError::FileNotFound(_)) => Ok(None),
            Err(e) => Err(PasswordVaultError::Storage(format!(
                "get_index {namespace}/{key}: {e:?}"
            ))),
        }
    }

    pub(crate) fn set_index(
        &self,
        namespace: &str,
        key: &str,
        idx: &SecretIndex,
    ) -> Result<(), PasswordVaultError> {
        // Ensure namespace directory exists.
        self.ensure_dir(&self.ns_dir_path(namespace))?;
        let path = self.entries_path(namespace, key);
        let encoded = bincode::serialize(idx).map_err(|e| {
            PasswordVaultError::Storage(format!("encode secret index {namespace}/{key}: {e}"))
        })?;
        self.kernel
            .write(&path, &self.ctx, &encoded, 0)
            .map_err(|e| {
                PasswordVaultError::Storage(format!("set_index {namespace}/{key}: {e:?}"))
            })?;
        Ok(())
    }

    pub(crate) fn put_version(
        &self,
        namespace: &str,
        key: &str,
        version: u32,
        entry: &StoredEntry,
    ) -> Result<(), PasswordVaultError> {
        self.ensure_dir(&self.ns_version_dir(namespace))?;
        let key_dir = self.versions_dir(namespace, key);
        self.ensure_dir(&key_dir)?;
        let path = self.version_path(namespace, key, version);
        let encoded = bincode::serialize(entry)
            .map_err(|e| PasswordVaultError::Storage(format!("encode version: {e}")))?;
        self.kernel
            .write(&path, &self.ctx, &encoded, 0)
            .map_err(|e| {
                PasswordVaultError::Storage(format!(
                    "put_version {namespace}/{key}/{version}: {e:?}"
                ))
            })?;
        Ok(())
    }

    pub(crate) fn get_version(
        &self,
        namespace: &str,
        key: &str,
        version: u32,
    ) -> Result<Option<StoredEntry>, PasswordVaultError> {
        let path = self.version_path(namespace, key, version);
        match KernelConvenience::read(&*self.kernel, &path, &self.ctx, 0, 0) {
            Ok(result) => {
                let data = result.data.ok_or_else(|| {
                    PasswordVaultError::Storage(format!(
                        "get_version {namespace}/{key}/{version}: empty read"
                    ))
                })?;
                let entry: StoredEntry = bincode::deserialize(&data).map_err(|e| {
                    PasswordVaultError::Storage(format!(
                        "decode version {namespace}/{key}/{version}: {e}"
                    ))
                })?;
                Ok(Some(entry))
            }
            Err(KernelError::FileNotFound(_)) => Ok(None),
            Err(e) => Err(PasswordVaultError::Storage(format!(
                "get_version {namespace}/{key}/{version}: {e:?}"
            ))),
        }
    }

    /// Physically delete a specific version file.
    pub(crate) fn delete_version(
        &self,
        namespace: &str,
        key: &str,
        version: u32,
    ) -> Result<bool, PasswordVaultError> {
        let path = self.version_path(namespace, key, version);
        let req = kernel::kernel::UnlinkRequest {
            path,
            recursive: false,
        };
        let results = self.kernel.sys_unlink(&[req], &self.ctx);
        match results.into_iter().next() {
            Some(Ok(_)) => Ok(true),
            Some(Err(KernelError::FileNotFound(_))) => Ok(false),
            Some(Err(e)) => Err(PasswordVaultError::Storage(format!(
                "delete_version {namespace}/{key}/{version}: {e:?}"
            ))),
            None => Ok(false),
        }
    }

    /// List all versions of a secret, sorted ascending.
    pub(crate) fn list_versions(
        &self,
        namespace: &str,
        key: &str,
    ) -> Result<Vec<StoredEntry>, PasswordVaultError> {
        let dir = self.versions_dir(namespace, key);
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

    /// List all secret indexes, optionally filtered by namespace.
    /// Returns `(namespace, key, SecretIndex)` triples.
    pub(crate) fn list_indexes(
        &self,
        namespace_filter: Option<&str>,
    ) -> Result<Vec<(String, String, SecretIndex)>, PasswordVaultError> {
        let mut out = Vec::new();

        let namespaces = match namespace_filter {
            Some(ns) => vec![ns.to_string()],
            None => {
                // List all namespace directories.
                let dir = format!("{}/secret_entries", self.root);
                self.kernel
                    .sys_readdir(&dir, "root", true)
                    .into_iter()
                    .filter_map(|(path, _)| path.rsplit('/').next().map(|s| s.to_string()))
                    .filter(|s| !s.is_empty())
                    .collect()
            }
        };

        for ns in &namespaces {
            let ns_dir = self.ns_dir_path(ns);
            let entries = self.kernel.sys_readdir(&ns_dir, "root", true);
            for (child_path, _etype) in entries {
                let key = match child_path.rsplit('/').next() {
                    Some(k) if !k.is_empty() => k.to_string(),
                    _ => continue,
                };
                match KernelConvenience::read(&*self.kernel, &child_path, &self.ctx, 0, 0) {
                    Ok(result) => {
                        if let Some(data) = result.data {
                            let idx: SecretIndex = bincode::deserialize(&data).map_err(|e| {
                                PasswordVaultError::Storage(format!(
                                    "decode secret index {ns}/{key}: {e}"
                                ))
                            })?;
                            out.push((ns.clone(), key, idx));
                        }
                    }
                    Err(_) => continue,
                }
            }
        }

        Ok(out)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::password_vault::types::now_unix_ms;
    use kernel::abc::object_store::{ObjectStore, StorageError, WriteResult};
    use std::collections::HashMap;

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
            "secret-mem"
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

    fn mount_and_create() -> SecretStorage {
        let kernel = Arc::new(Kernel::new());
        let backend: Arc<dyn ObjectStore> = Arc::new(MemBackend::new());
        let backend_name = backend.name().to_string();
        kernel
            .sys_setattr(
                "/vault",
                2,
                /* DT_MOUNT */ &backend_name,
                Some(backend),
                None,
                None,
                "memory",
                "root",
                false,
                0,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
            )
            .unwrap();
        SecretStorage::new_on_existing_mount(kernel, "/vault").unwrap()
    }

    fn index(version: u32, deleted: bool) -> SecretIndex {
        SecretIndex {
            current_version: version,
            deleted_at_ms: if deleted { Some(2_000) } else { None },
            description: String::new(),
            created_at_ms: 1_000,
            updated_at_ms: 1_000,
        }
    }

    fn entry(version: u32, ct: &[u8]) -> StoredEntry {
        StoredEntry {
            version,
            created_at_ms: now_unix_ms(),
            nonce: [0u8; 12],
            ciphertext: ct.to_vec(),
        }
    }

    #[test]
    fn empty_returns_none() {
        let s = mount_and_create();
        assert!(s.get_index("ns", "key").unwrap().is_none());
        assert!(s.get_version("ns", "key", 1).unwrap().is_none());
        assert!(s.list_indexes(None).unwrap().is_empty());
        assert!(s.list_versions("ns", "key").unwrap().is_empty());
    }

    #[test]
    fn index_round_trip() {
        let s = mount_and_create();
        s.set_index("provider:openai", "api_key", &index(1, false))
            .unwrap();
        let got = s.get_index("provider:openai", "api_key").unwrap().unwrap();
        assert_eq!(got.current_version, 1);
        assert!(got.deleted_at_ms.is_none());
    }

    #[test]
    fn version_round_trip() {
        let s = mount_and_create();
        s.put_version("auth:jwt", "webui_secret", 1, &entry(1, b"secret"))
            .unwrap();
        let got = s
            .get_version("auth:jwt", "webui_secret", 1)
            .unwrap()
            .unwrap();
        assert_eq!(got.version, 1);
        assert_eq!(got.ciphertext, b"secret");
    }

    #[test]
    fn list_indexes_filters_by_namespace() {
        let s = mount_and_create();
        s.set_index("ns1", "a", &index(1, false)).unwrap();
        s.set_index("ns1", "b", &index(2, false)).unwrap();
        s.set_index("ns2", "c", &index(1, false)).unwrap();

        let all = s.list_indexes(None).unwrap();
        assert_eq!(all.len(), 3);

        let ns1 = s.list_indexes(Some("ns1")).unwrap();
        assert_eq!(ns1.len(), 2);
        assert!(ns1.iter().all(|(ns, _, _)| ns == "ns1"));
    }

    #[test]
    fn list_versions_orders_ascending() {
        let s = mount_and_create();
        s.put_version("ns", "k", 3, &entry(3, b"c")).unwrap();
        s.put_version("ns", "k", 1, &entry(1, b"a")).unwrap();
        s.put_version("ns", "k", 2, &entry(2, b"b")).unwrap();

        let vers = s.list_versions("ns", "k").unwrap();
        let nums: Vec<u32> = vers.iter().map(|e| e.version).collect();
        assert_eq!(nums, vec![1, 2, 3]);
    }
}
