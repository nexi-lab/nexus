//! Kernel-backed storage for the unified secrets layer.
//!
//! Two-level namespace layout under the vault mount:
//!   - `{root}/entries/{namespace}/{key}` → bincode(SecretIndex)
//!   - `{root}/versions/{namespace}/{key}/{version:010}` → bincode(StoredEntry)
//!
//! Shared by both `GenericSecretsService` and `PasswordVaultService`
//! (the latter uses namespace="passwords"). Created by the vault
//! plugin at `/vault`.

use std::sync::Arc;

use kernel::kernel::convenience::KernelConvenience;
use kernel::kernel::{Kernel, KernelError, OperationContext};

use crate::password_vault::types::{PasswordVaultError, SecretIndex, StoredEntry};

const DT_DIR: i32 = 1;

/// Percent-encode characters that are illegal in Windows NTFS path components
/// (and the leading `%` to keep the round-trip unambiguous).
///
/// Encoded set: `% : / \ < > " | ? *` + ASCII control chars 0x00..=0x1F.
/// Output uses uppercase `%HH` (matches URL percent-encoding convention).
///
/// Applied uniformly across all platforms — there is no `cfg(windows)` branch:
/// on-disk layout must round-trip between Linux/macOS/Windows. See
/// `zazzy-chasing-pizza.md` "头号约束" for the rationale.
fn escape_path_component(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for b in s.bytes() {
        let needs_encode = matches!(
            b,
            b'%' | b':' | b'/' | b'\\' | b'<' | b'>' | b'"' | b'|' | b'?' | b'*' | 0x00..=0x1F
        );
        if needs_encode {
            out.push('%');
            out.push_str(&format!("{:02X}", b));
        } else {
            out.push(b as char);
        }
    }
    out
}

/// Inverse of `escape_path_component`. Decodes `%HH` (two hex digits, either
/// case) back to the original byte. Tolerant of malformed input: a `%` not
/// followed by two hex digits (e.g. `%ZZ`, `%A`, trailing `%`) is passed
/// through verbatim rather than panicking or returning a `Result`. Rationale:
/// readdir may surface foreign directories (manual `mv`, future tooling) and
/// `list` must not crash on them.
fn unescape_path_component(s: &str) -> String {
    let bytes = s.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%' && i + 2 < bytes.len() {
            let h1 = (bytes[i + 1] as char).to_digit(16);
            let h2 = (bytes[i + 2] as char).to_digit(16);
            if let (Some(h1), Some(h2)) = (h1, h2) {
                out.push((h1 * 16 + h2) as u8);
                i += 3;
                continue;
            }
        }
        out.push(bytes[i]);
        i += 1;
    }
    // Encoded bytes are always valid UTF-8 since we only encode ASCII-range
    // bytes and pass others through. So from_utf8_lossy is a safety net,
    // not load-bearing.
    String::from_utf8_lossy(&out).into_owned()
}

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
        storage.ensure_dir(&format!("{}/entries", storage.root))?;
        storage.ensure_dir(&format!("{}/versions", storage.root))?;
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
        format!(
            "{}/entries/{}/{}",
            self.root,
            escape_path_component(namespace),
            escape_path_component(key)
        )
    }

    fn ns_dir_path(&self, namespace: &str) -> String {
        format!("{}/entries/{}", self.root, escape_path_component(namespace))
    }

    fn version_path(&self, namespace: &str, key: &str, version: u32) -> String {
        format!(
            "{}/versions/{}/{}/{:010}",
            self.root,
            escape_path_component(namespace),
            escape_path_component(key),
            version
        )
    }

    fn versions_dir(&self, namespace: &str, key: &str) -> String {
        format!(
            "{}/versions/{}/{}",
            self.root,
            escape_path_component(namespace),
            escape_path_component(key)
        )
    }

    fn ns_version_dir(&self, namespace: &str) -> String {
        format!(
            "{}/versions/{}",
            self.root,
            escape_path_component(namespace)
        )
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
                let dir = format!("{}/entries", self.root);
                self.kernel
                    .sys_readdir(&dir, "root", true)
                    .into_iter()
                    .filter_map(|(path, _)| path.rsplit('/').next().map(unescape_path_component))
                    .filter(|s| !s.is_empty())
                    .collect()
            }
        };

        for ns in &namespaces {
            let ns_dir = self.ns_dir_path(ns);
            let entries = self.kernel.sys_readdir(&ns_dir, "root", true);
            for (child_path, _etype) in entries {
                let key = match child_path.rsplit('/').next() {
                    Some(k) if !k.is_empty() => unescape_path_component(k),
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

    // ── Percent-encoding tests for path-component escape/unescape ─────
    //
    // These tests target the pure string-transform functions defined at
    // the top of this module. They must stay platform-agnostic — there is
    // intentionally no `cfg(target_os = ...)` anywhere in the encoder.

    #[test]
    fn escape_l1_character_set_explicit_assertions() {
        // L1 character set (Windows NTFS-illegal + `%` self-encoding).
        // Each character listed in zazzy-chasing-pizza.md is asserted
        // individually so the test fails loudly if any one is dropped
        // from the encoder match arm.
        assert_eq!(escape_path_component("%"), "%25");
        assert_eq!(escape_path_component(":"), "%3A");
        assert_eq!(escape_path_component("/"), "%2F");
        assert_eq!(escape_path_component("\\"), "%5C");
        assert_eq!(escape_path_component("<"), "%3C");
        assert_eq!(escape_path_component(">"), "%3E");
        assert_eq!(escape_path_component("\""), "%22");
        assert_eq!(escape_path_component("|"), "%7C");
        assert_eq!(escape_path_component("?"), "%3F");
        assert_eq!(escape_path_component("*"), "%2A");

        // ASCII control chars 0x00..=0x1F — loop instead of enumerating.
        for b in 0x00u8..=0x1F {
            let input = (b as char).to_string();
            let got = escape_path_component(&input);
            let want = format!("%{:02X}", b);
            assert_eq!(
                got, want,
                "control char 0x{:02X} did not encode to {}",
                b, want
            );
        }
    }

    #[test]
    fn escape_unescape_round_trip() {
        let cases: &[&str] = &[
            "service:shareone",
            "channel:lark:456",
            "path/with/slash",
            "mix:and/match",
            "percent%inline",
            "back\\slash",
            "",
            "abc123",
            "no-special",
            "control\x00\x1Fbytes",
            "quote\"and|pipe?star*<lt>gt",
        ];
        for s in cases {
            let round = unescape_path_component(&escape_path_component(s));
            assert_eq!(&round, s, "round-trip failed for {:?}", s);
        }
    }

    #[test]
    fn escape_is_identity_on_safe_alphanumeric() {
        // Pure alphanumeric / safe-ASCII inputs must pass through unchanged.
        // Documents the invariant that encoding overhead is paid only when
        // namespaces actually contain illegal chars.
        for s in &["abc123", "service-x", "X-API-Key", "passwords", "auth_jwt"] {
            assert_eq!(
                escape_path_component(s),
                *s,
                "non-identity for safe {:?}",
                s
            );
        }
    }

    #[test]
    fn unescape_tolerates_invalid_percent_sequences() {
        // Malformed inputs must pass through verbatim, not panic, not
        // return Result. This protects `list_indexes` from crashing on
        // foreign / hand-placed directories.
        assert_eq!(unescape_path_component("%ZZ"), "%ZZ");
        assert_eq!(unescape_path_component("%A"), "%A");
        assert_eq!(unescape_path_component("100%"), "100%");
        assert_eq!(unescape_path_component("a%2Gb"), "a%2Gb");
        // Sanity: valid mixed with invalid still decodes the valid part.
        assert_eq!(unescape_path_component("a%3Ab%ZZc"), "a:b%ZZc");
    }
}
