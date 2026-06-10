//! `GenericSecretsService` — namespace:key encrypted KV with versioning,
//! soft-delete, and batch operations.
//!
//! Extends the vault plugin to serve general-purpose secrets alongside
//! `PasswordVaultService`. Shares the same AES-256-GCM master key and
//! kernel mount; only the data model differs (simple value string vs
//! `VaultEntry` schema, no TOTP).
//!
//! Storage layout (under the vault mount at `/vault`):
//!   - `/vault/entries/{namespace}/{key}` → bincode(SecretIndex)
//!   - `/vault/versions/{namespace}/{key}/{version:010}` → bincode(StoredEntry)
//!
//! Consumer: sudowork `SecretStoreClient` (replaces HTTP `/api/v2/secrets`).

pub mod proto {
    tonic::include_proto!("nexus.secrets.v1");
}

mod storage;

use std::sync::Arc;

use tonic::{Request, Response, Status};

use proto::generic_secrets_service_server::GenericSecretsService;
use proto::*;

use crate::password_vault::crypto;
use crate::password_vault::types::{now_unix_ms, PasswordVaultError, SecretIndex, StoredEntry};

use self::storage::SecretStorage;

fn unix_ms_to_proto_ts(ms: u64) -> prost_types::Timestamp {
    prost_types::Timestamp {
        seconds: (ms / 1_000) as i64,
        nanos: ((ms % 1_000) * 1_000_000) as i32,
    }
}

fn index_to_metadata(ns: &str, key: &str, idx: &SecretIndex) -> SecretMetadata {
    SecretMetadata {
        namespace: ns.to_string(),
        key: key.to_string(),
        description: if idx.description.is_empty() {
            None
        } else {
            Some(idx.description.clone())
        },
        current_version: idx.current_version as i32,
        deleted: idx.deleted_at_ms.is_some(),
        created_at: Some(unix_ms_to_proto_ts(idx.created_at_ms)),
        updated_at: Some(unix_ms_to_proto_ts(idx.updated_at_ms)),
    }
}

struct Inner {
    storage: SecretStorage,
    master_key: crypto::MasterKey,
}

#[derive(Clone)]
pub struct GenericSecretsServiceImpl {
    inner: Arc<Inner>,
}

impl GenericSecretsServiceImpl {
    /// Create a generic secrets service on an existing kernel mount.
    /// The caller (vault plugin) must have already mounted a backend
    /// at `root`.
    pub fn new_on_existing_mount(
        kernel: Arc<kernel::kernel::Kernel>,
        root: &str,
        master_key: crypto::MasterKey,
    ) -> Result<Self, PasswordVaultError> {
        let storage = SecretStorage::new_on_existing_mount(kernel, root)?;
        Ok(Self {
            inner: Arc::new(Inner {
                storage,
                master_key,
            }),
        })
    }

    /// Internal put — shared by single put, batch put, and
    /// `PasswordVaultServiceImpl` (namespace="passwords").
    pub(crate) fn do_put(
        &self,
        namespace: &str,
        key: &str,
        value: &str,
        description: Option<&str>,
    ) -> Result<SecretMetadata, Status> {
        if namespace.is_empty() {
            return Err(Status::invalid_argument("namespace is required"));
        }
        if key.is_empty() {
            return Err(Status::invalid_argument("key is required"));
        }

        let (nonce, ciphertext) = crypto::seal(value.as_bytes(), &self.inner.master_key)?;

        let current = self.inner.storage.get_index(namespace, key)?;
        let now = now_unix_ms();
        let next_version = current.as_ref().map_or(1, |idx| idx.current_version + 1);

        let stored = StoredEntry {
            version: next_version,
            created_at_ms: now,
            nonce,
            ciphertext,
        };
        self.inner
            .storage
            .put_version(namespace, key, next_version, &stored)?;

        let idx = SecretIndex {
            current_version: next_version,
            deleted_at_ms: None,
            description: description
                .map(|d| d.to_string())
                .or_else(|| current.as_ref().map(|c| c.description.clone()))
                .unwrap_or_default(),
            created_at_ms: current.as_ref().map_or(now, |c| c.created_at_ms),
            updated_at_ms: now,
        };
        self.inner.storage.set_index(namespace, key, &idx)?;

        Ok(index_to_metadata(namespace, key, &idx))
    }

    /// Internal get — shared by single get, batch get, and
    /// `PasswordVaultServiceImpl` (namespace="passwords").
    pub(crate) fn do_get(
        &self,
        namespace: &str,
        key: &str,
        version: Option<i32>,
    ) -> Result<(String, i32), Status> {
        if namespace.is_empty() {
            return Err(Status::invalid_argument("namespace is required"));
        }
        if key.is_empty() {
            return Err(Status::invalid_argument("key is required"));
        }

        let idx = self
            .inner
            .storage
            .get_index(namespace, key)?
            .ok_or_else(|| PasswordVaultError::NotFound(format!("{namespace}/{key}")))?;

        let version_to_read = match version {
            None => {
                if idx.deleted_at_ms.is_some() {
                    return Err(PasswordVaultError::NotFound(format!("{namespace}/{key}")).into());
                }
                idx.current_version
            }
            Some(v) if v < 0 => {
                return Err(Status::invalid_argument("version must be >= 0"));
            }
            Some(v) => v as u32,
        };

        let stored = self
            .inner
            .storage
            .get_version(namespace, key, version_to_read)?
            .ok_or_else(|| {
                PasswordVaultError::NotFound(format!("{namespace}/{key}/v{version_to_read}"))
            })?;

        let plain_bytes = crypto::open(&stored.nonce, &stored.ciphertext, &self.inner.master_key)?;
        let value = String::from_utf8(plain_bytes)
            .map_err(|_| PasswordVaultError::Storage("secret value is not valid UTF-8".into()))?;

        Ok((value, stored.version as i32))
    }

    /// Internal delete — sets tombstone. Used by `PasswordVaultServiceImpl`.
    pub(crate) fn do_delete(&self, namespace: &str, key: &str) -> Result<(), Status> {
        if namespace.is_empty() || key.is_empty() {
            return Err(Status::invalid_argument("namespace and key are required"));
        }
        let idx = self
            .inner
            .storage
            .get_index(namespace, key)?
            .ok_or_else(|| PasswordVaultError::NotFound(format!("{namespace}/{key}")))?;
        let new_idx = SecretIndex {
            deleted_at_ms: Some(idx.deleted_at_ms.unwrap_or_else(now_unix_ms)),
            updated_at_ms: now_unix_ms(),
            ..idx
        };
        self.inner.storage.set_index(namespace, key, &new_idx)?;
        Ok(())
    }

    /// Internal restore — clears tombstone. Returns `(restored, current_version)`.
    pub(crate) fn do_restore(&self, namespace: &str, key: &str) -> Result<(bool, i32), Status> {
        if namespace.is_empty() || key.is_empty() {
            return Err(Status::invalid_argument("namespace and key are required"));
        }
        let idx = self
            .inner
            .storage
            .get_index(namespace, key)?
            .ok_or_else(|| PasswordVaultError::NotFound(format!("{namespace}/{key}")))?;
        let new_idx = SecretIndex {
            deleted_at_ms: None,
            updated_at_ms: now_unix_ms(),
            ..idx
        };
        self.inner.storage.set_index(namespace, key, &new_idx)?;
        Ok((true, new_idx.current_version as i32))
    }

    /// Internal list_versions — returns `(version, created_at_ms, tombstoned)` triples.
    pub(crate) fn do_list_versions(
        &self,
        namespace: &str,
        key: &str,
    ) -> Result<(Vec<StoredEntry>, SecretIndex), Status> {
        if namespace.is_empty() || key.is_empty() {
            return Err(Status::invalid_argument("namespace and key are required"));
        }
        let idx = self
            .inner
            .storage
            .get_index(namespace, key)?
            .ok_or_else(|| PasswordVaultError::NotFound(format!("{namespace}/{key}")))?;
        let stored = self.inner.storage.list_versions(namespace, key)?;
        Ok((stored, idx))
    }

    /// Internal list metadata — returns `(namespace, key, SecretIndex)` triples.
    pub(crate) fn do_list_metadata(
        &self,
        namespace: Option<&str>,
        include_deleted: bool,
    ) -> Result<Vec<(String, String, SecretIndex)>, Status> {
        let all = self.inner.storage.list_indexes(namespace)?;
        let filtered: Vec<_> = all
            .into_iter()
            .filter(|(_, _, idx)| include_deleted || idx.deleted_at_ms.is_none())
            .collect();
        Ok(filtered)
    }
}

#[tonic::async_trait]
impl GenericSecretsService for GenericSecretsServiceImpl {
    async fn put_secret(
        &self,
        req: Request<PutSecretRequest>,
    ) -> Result<Response<PutSecretResponse>, Status> {
        let req = req.into_inner();
        let metadata = self.do_put(
            &req.namespace,
            &req.key,
            &req.value,
            req.description.as_deref(),
        )?;
        Ok(Response::new(PutSecretResponse {
            metadata: Some(metadata),
        }))
    }

    async fn get_secret(
        &self,
        req: Request<GetSecretRequest>,
    ) -> Result<Response<GetSecretResponse>, Status> {
        let req = req.into_inner();
        let (value, version) = self.do_get(&req.namespace, &req.key, req.version)?;
        Ok(Response::new(GetSecretResponse {
            namespace: req.namespace,
            key: req.key,
            value,
            version,
        }))
    }

    async fn delete_secret(
        &self,
        req: Request<DeleteSecretRequest>,
    ) -> Result<Response<DeleteSecretResponse>, Status> {
        let req = req.into_inner();
        self.do_delete(&req.namespace, &req.key)?;
        Ok(Response::new(DeleteSecretResponse {
            namespace: req.namespace,
            key: req.key,
            deleted: true,
        }))
    }

    async fn restore_secret(
        &self,
        req: Request<RestoreSecretRequest>,
    ) -> Result<Response<RestoreSecretResponse>, Status> {
        let req = req.into_inner();
        let (restored, current_version) = self.do_restore(&req.namespace, &req.key)?;
        Ok(Response::new(RestoreSecretResponse {
            namespace: req.namespace,
            key: req.key,
            restored,
            current_version,
        }))
    }

    async fn list_secrets(
        &self,
        req: Request<ListSecretsRequest>,
    ) -> Result<Response<ListSecretsResponse>, Status> {
        let req = req.into_inner();
        let filtered = self.do_list_metadata(req.namespace.as_deref(), req.include_deleted)?;
        let secrets: Vec<SecretMetadata> = filtered
            .iter()
            .map(|(ns, key, idx)| index_to_metadata(ns, key, idx))
            .collect();
        let count = secrets.len() as i32;
        Ok(Response::new(ListSecretsResponse { secrets, count }))
    }

    async fn list_secret_versions(
        &self,
        req: Request<ListSecretVersionsRequest>,
    ) -> Result<Response<ListSecretVersionsResponse>, Status> {
        let req = req.into_inner();
        let (stored, idx) = self.do_list_versions(&req.namespace, &req.key)?;
        let active = idx.current_version;
        let is_deleted = idx.deleted_at_ms.is_some();
        let versions: Vec<SecretVersion> = stored
            .into_iter()
            .map(|s| SecretVersion {
                version: s.version as i32,
                created_at: Some(unix_ms_to_proto_ts(s.created_at_ms)),
                tombstoned: is_deleted && s.version == active,
            })
            .collect();
        let count = versions.len() as i32;
        Ok(Response::new(ListSecretVersionsResponse {
            namespace: req.namespace,
            key: req.key,
            count,
            versions,
        }))
    }

    async fn batch_put_secrets(
        &self,
        req: Request<BatchPutSecretsRequest>,
    ) -> Result<Response<BatchPutSecretsResponse>, Status> {
        let req = req.into_inner();
        let mut results = Vec::with_capacity(req.secrets.len());
        for s in &req.secrets {
            let metadata = self.do_put(&s.namespace, &s.key, &s.value, s.description.as_deref())?;
            results.push(metadata);
        }
        let count = results.len() as i32;
        Ok(Response::new(BatchPutSecretsResponse { results, count }))
    }

    async fn batch_get_secrets(
        &self,
        req: Request<BatchGetSecretsRequest>,
    ) -> Result<Response<BatchGetSecretsResponse>, Status> {
        let req = req.into_inner();
        let mut secrets = std::collections::HashMap::new();
        for q in &req.queries {
            match self.do_get(&q.namespace, &q.key, q.version) {
                Ok((value, _version)) => {
                    let compound_key = format!("{}:{}", q.namespace, q.key);
                    secrets.insert(compound_key, value);
                }
                // Missing/deleted secrets are silently omitted in batch get.
                Err(status) if status.code() == tonic::Code::NotFound => continue,
                Err(e) => return Err(e),
            }
        }
        let count = secrets.len() as i32;
        Ok(Response::new(BatchGetSecretsResponse { secrets, count }))
    }

    async fn delete_secret_version(
        &self,
        req: Request<DeleteSecretVersionRequest>,
    ) -> Result<Response<DeleteSecretVersionResponse>, Status> {
        let req = req.into_inner();
        if req.namespace.is_empty() || req.key.is_empty() {
            return Err(Status::invalid_argument("namespace and key are required"));
        }
        if req.version <= 0 {
            return Err(Status::invalid_argument("version must be > 0"));
        }
        let idx = self
            .inner
            .storage
            .get_index(&req.namespace, &req.key)?
            .ok_or_else(|| {
                PasswordVaultError::NotFound(format!("{}/{}", req.namespace, req.key))
            })?;

        let v = req.version as u32;

        // Cannot delete the last remaining version — use DeleteSecret.
        let versions = self.inner.storage.list_versions(&req.namespace, &req.key)?;
        if versions.len() <= 1 {
            return Err(Status::failed_precondition(
                "cannot delete the last remaining version; use DeleteSecret instead",
            ));
        }

        let deleted = self
            .inner
            .storage
            .delete_version(&req.namespace, &req.key, v)?;
        if !deleted {
            return Err(PasswordVaultError::NotFound(format!(
                "{}/{}/v{v}",
                req.namespace, req.key
            ))
            .into());
        }

        // If we deleted the current version, roll back index to previous.
        if idx.current_version == v {
            let remaining = self.inner.storage.list_versions(&req.namespace, &req.key)?;
            if let Some(latest) = remaining.last() {
                let new_idx = SecretIndex {
                    current_version: latest.version,
                    updated_at_ms: now_unix_ms(),
                    ..idx
                };
                self.inner
                    .storage
                    .set_index(&req.namespace, &req.key, &new_idx)?;
            }
        }

        Ok(Response::new(DeleteSecretVersionResponse {
            namespace: req.namespace,
            key: req.key,
            version: req.version,
            deleted: true,
        }))
    }

    async fn update_secret_description(
        &self,
        req: Request<UpdateSecretDescriptionRequest>,
    ) -> Result<Response<UpdateSecretDescriptionResponse>, Status> {
        let req = req.into_inner();
        if req.namespace.is_empty() || req.key.is_empty() {
            return Err(Status::invalid_argument("namespace and key are required"));
        }
        let idx = self
            .inner
            .storage
            .get_index(&req.namespace, &req.key)?
            .ok_or_else(|| {
                PasswordVaultError::NotFound(format!("{}/{}", req.namespace, req.key))
            })?;
        let new_idx = SecretIndex {
            description: req.description.clone(),
            updated_at_ms: now_unix_ms(),
            ..idx
        };
        self.inner
            .storage
            .set_index(&req.namespace, &req.key, &new_idx)?;
        Ok(Response::new(UpdateSecretDescriptionResponse {
            namespace: req.namespace,
            key: req.key,
            description: req.description,
        }))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use kernel::abc::object_store::{ObjectStore, StorageError, WriteResult};
    use kernel::kernel::{Kernel, OperationContext};
    use std::collections::HashMap;
    use std::sync::Arc;

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
            "secret-test-mem"
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

    fn fresh_service() -> GenericSecretsServiceImpl {
        let kernel = Arc::new(Kernel::new());
        let backend: Arc<dyn ObjectStore> = Arc::new(MemBackend::new());
        let backend_name = backend.name().to_string();
        kernel
            .sys_setattr(
                "/vault",
                2,
                &backend_name,
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
        let master_key = crypto::MasterKey::generate();
        GenericSecretsServiceImpl::new_on_existing_mount(kernel, "/vault", master_key).unwrap()
    }

    #[tokio::test]
    async fn put_then_get_round_trip() {
        let svc = fresh_service();
        let resp = svc
            .put_secret(Request::new(PutSecretRequest {
                namespace: "provider:openai".into(),
                key: "api_key".into(),
                value: "sk-test-123".into(),
                description: Some("OpenAI API key".into()),
            }))
            .await
            .unwrap()
            .into_inner();
        let meta = resp.metadata.unwrap();
        assert_eq!(meta.namespace, "provider:openai");
        assert_eq!(meta.key, "api_key");
        assert_eq!(meta.current_version, 1);
        assert!(!meta.deleted);
        assert_eq!(meta.description.as_deref(), Some("OpenAI API key"));

        let got = svc
            .get_secret(Request::new(GetSecretRequest {
                namespace: "provider:openai".into(),
                key: "api_key".into(),
                version: None,
            }))
            .await
            .unwrap()
            .into_inner();
        assert_eq!(got.value, "sk-test-123");
        assert_eq!(got.version, 1);
    }

    #[tokio::test]
    async fn put_increments_version() {
        let svc = fresh_service();
        for (i, val) in ["v1", "v2", "v3"].iter().enumerate() {
            let resp = svc
                .put_secret(Request::new(PutSecretRequest {
                    namespace: "ns".into(),
                    key: "k".into(),
                    value: val.to_string(),
                    description: None,
                }))
                .await
                .unwrap()
                .into_inner();
            assert_eq!(resp.metadata.unwrap().current_version, (i + 1) as i32);
        }
        let got = svc
            .get_secret(Request::new(GetSecretRequest {
                namespace: "ns".into(),
                key: "k".into(),
                version: None,
            }))
            .await
            .unwrap()
            .into_inner();
        assert_eq!(got.value, "v3");
    }

    #[tokio::test]
    async fn get_specific_version() {
        let svc = fresh_service();
        for val in ["v1", "v2"] {
            svc.put_secret(Request::new(PutSecretRequest {
                namespace: "ns".into(),
                key: "k".into(),
                value: val.into(),
                description: None,
            }))
            .await
            .unwrap();
        }
        let got = svc
            .get_secret(Request::new(GetSecretRequest {
                namespace: "ns".into(),
                key: "k".into(),
                version: Some(1),
            }))
            .await
            .unwrap()
            .into_inner();
        assert_eq!(got.value, "v1");
        assert_eq!(got.version, 1);
    }

    #[tokio::test]
    async fn delete_restore_lifecycle() {
        let svc = fresh_service();
        svc.put_secret(Request::new(PutSecretRequest {
            namespace: "ns".into(),
            key: "k".into(),
            value: "secret".into(),
            description: None,
        }))
        .await
        .unwrap();

        // Delete
        let del = svc
            .delete_secret(Request::new(DeleteSecretRequest {
                namespace: "ns".into(),
                key: "k".into(),
            }))
            .await
            .unwrap()
            .into_inner();
        assert!(del.deleted);

        // Get latest should fail
        let err = svc
            .get_secret(Request::new(GetSecretRequest {
                namespace: "ns".into(),
                key: "k".into(),
                version: None,
            }))
            .await
            .unwrap_err();
        assert_eq!(err.code(), tonic::Code::NotFound);

        // Restore
        let res = svc
            .restore_secret(Request::new(RestoreSecretRequest {
                namespace: "ns".into(),
                key: "k".into(),
            }))
            .await
            .unwrap()
            .into_inner();
        assert!(res.restored);

        // Get should work again
        let got = svc
            .get_secret(Request::new(GetSecretRequest {
                namespace: "ns".into(),
                key: "k".into(),
                version: None,
            }))
            .await
            .unwrap()
            .into_inner();
        assert_eq!(got.value, "secret");
    }

    #[tokio::test]
    async fn list_filters_by_namespace_and_deleted() {
        let svc = fresh_service();
        for (ns, k, v) in [("ns1", "a", "1"), ("ns1", "b", "2"), ("ns2", "c", "3")] {
            svc.put_secret(Request::new(PutSecretRequest {
                namespace: ns.into(),
                key: k.into(),
                value: v.into(),
                description: None,
            }))
            .await
            .unwrap();
        }
        // Delete one
        svc.delete_secret(Request::new(DeleteSecretRequest {
            namespace: "ns1".into(),
            key: "a".into(),
        }))
        .await
        .unwrap();

        // List ns1, no deleted
        let resp = svc
            .list_secrets(Request::new(ListSecretsRequest {
                namespace: Some("ns1".into()),
                include_deleted: false,
            }))
            .await
            .unwrap()
            .into_inner();
        assert_eq!(resp.count, 1);
        assert_eq!(resp.secrets[0].key, "b");

        // List ns1, with deleted
        let resp = svc
            .list_secrets(Request::new(ListSecretsRequest {
                namespace: Some("ns1".into()),
                include_deleted: true,
            }))
            .await
            .unwrap()
            .into_inner();
        assert_eq!(resp.count, 2);

        // List all, no deleted
        let resp = svc
            .list_secrets(Request::new(ListSecretsRequest {
                namespace: None,
                include_deleted: false,
            }))
            .await
            .unwrap()
            .into_inner();
        assert_eq!(resp.count, 2);
    }

    #[tokio::test]
    async fn list_versions_with_tombstone() {
        let svc = fresh_service();
        for v in ["v1", "v2"] {
            svc.put_secret(Request::new(PutSecretRequest {
                namespace: "ns".into(),
                key: "k".into(),
                value: v.into(),
                description: None,
            }))
            .await
            .unwrap();
        }
        svc.delete_secret(Request::new(DeleteSecretRequest {
            namespace: "ns".into(),
            key: "k".into(),
        }))
        .await
        .unwrap();

        let resp = svc
            .list_secret_versions(Request::new(ListSecretVersionsRequest {
                namespace: "ns".into(),
                key: "k".into(),
            }))
            .await
            .unwrap()
            .into_inner();
        assert_eq!(resp.count, 2);
        assert!(!resp.versions[0].tombstoned);
        assert!(resp.versions[1].tombstoned);
    }

    #[tokio::test]
    async fn batch_put_and_batch_get() {
        let svc = fresh_service();
        let resp = svc
            .batch_put_secrets(Request::new(BatchPutSecretsRequest {
                secrets: vec![
                    PutSecretRequest {
                        namespace: "channel:telegram:1".into(),
                        key: "token".into(),
                        value: "tok-123".into(),
                        description: None,
                    },
                    PutSecretRequest {
                        namespace: "provider:openai".into(),
                        key: "api_key".into(),
                        value: "sk-456".into(),
                        description: Some("prod key".into()),
                    },
                ],
            }))
            .await
            .unwrap()
            .into_inner();
        assert_eq!(resp.count, 2);

        let got = svc
            .batch_get_secrets(Request::new(BatchGetSecretsRequest {
                queries: vec![
                    GetSecretRequest {
                        namespace: "channel:telegram:1".into(),
                        key: "token".into(),
                        version: None,
                    },
                    GetSecretRequest {
                        namespace: "provider:openai".into(),
                        key: "api_key".into(),
                        version: None,
                    },
                    GetSecretRequest {
                        namespace: "nonexistent".into(),
                        key: "nope".into(),
                        version: None,
                    },
                ],
            }))
            .await
            .unwrap()
            .into_inner();
        assert_eq!(got.count, 2);
        assert_eq!(got.secrets["channel:telegram:1:token"], "tok-123");
        assert_eq!(got.secrets["provider:openai:api_key"], "sk-456");
        assert!(!got.secrets.contains_key("nonexistent:nope"));
    }

    #[tokio::test]
    async fn put_revives_soft_deleted() {
        let svc = fresh_service();
        svc.put_secret(Request::new(PutSecretRequest {
            namespace: "ns".into(),
            key: "k".into(),
            value: "v1".into(),
            description: None,
        }))
        .await
        .unwrap();
        svc.delete_secret(Request::new(DeleteSecretRequest {
            namespace: "ns".into(),
            key: "k".into(),
        }))
        .await
        .unwrap();
        let resp = svc
            .put_secret(Request::new(PutSecretRequest {
                namespace: "ns".into(),
                key: "k".into(),
                value: "v2".into(),
                description: None,
            }))
            .await
            .unwrap()
            .into_inner();
        let meta = resp.metadata.unwrap();
        assert_eq!(meta.current_version, 2);
        assert!(!meta.deleted);

        let got = svc
            .get_secret(Request::new(GetSecretRequest {
                namespace: "ns".into(),
                key: "k".into(),
                version: None,
            }))
            .await
            .unwrap()
            .into_inner();
        assert_eq!(got.value, "v2");
    }

    #[tokio::test]
    async fn description_preserved_on_put_without_description() {
        let svc = fresh_service();
        svc.put_secret(Request::new(PutSecretRequest {
            namespace: "ns".into(),
            key: "k".into(),
            value: "v1".into(),
            description: Some("my desc".into()),
        }))
        .await
        .unwrap();

        // Put again without description — should preserve the old one
        let resp = svc
            .put_secret(Request::new(PutSecretRequest {
                namespace: "ns".into(),
                key: "k".into(),
                value: "v2".into(),
                description: None,
            }))
            .await
            .unwrap()
            .into_inner();
        assert_eq!(
            resp.metadata.unwrap().description.as_deref(),
            Some("my desc")
        );
    }

    #[tokio::test]
    async fn get_unknown_returns_not_found() {
        let svc = fresh_service();
        let err = svc
            .get_secret(Request::new(GetSecretRequest {
                namespace: "ns".into(),
                key: "nope".into(),
                version: None,
            }))
            .await
            .unwrap_err();
        assert_eq!(err.code(), tonic::Code::NotFound);
    }

    #[tokio::test]
    async fn namespace_with_colons_works() {
        let svc = fresh_service();
        svc.put_secret(Request::new(PutSecretRequest {
            namespace: "channel:lark:456".into(),
            key: "appSecret".into(),
            value: "lark-secret-value".into(),
            description: None,
        }))
        .await
        .unwrap();
        let got = svc
            .get_secret(Request::new(GetSecretRequest {
                namespace: "channel:lark:456".into(),
                key: "appSecret".into(),
                version: None,
            }))
            .await
            .unwrap()
            .into_inner();
        assert_eq!(got.value, "lark-secret-value");
    }

    #[tokio::test]
    async fn delete_version_removes_specific_version() {
        let svc = fresh_service();
        for v in ["v1", "v2", "v3"] {
            svc.put_secret(Request::new(PutSecretRequest {
                namespace: "ns".into(),
                key: "k".into(),
                value: v.into(),
                description: None,
            }))
            .await
            .unwrap();
        }

        // Delete v2
        let resp = svc
            .delete_secret_version(Request::new(DeleteSecretVersionRequest {
                namespace: "ns".into(),
                key: "k".into(),
                version: 2,
            }))
            .await
            .unwrap()
            .into_inner();
        assert!(resp.deleted);

        // list_versions should show 2 remaining
        let vers = svc
            .list_secret_versions(Request::new(ListSecretVersionsRequest {
                namespace: "ns".into(),
                key: "k".into(),
            }))
            .await
            .unwrap()
            .into_inner();
        assert_eq!(vers.count, 2);
        let nums: Vec<i32> = vers.versions.iter().map(|v| v.version).collect();
        assert_eq!(nums, vec![1, 3]);

        // latest still v3
        let got = svc
            .get_secret(Request::new(GetSecretRequest {
                namespace: "ns".into(),
                key: "k".into(),
                version: None,
            }))
            .await
            .unwrap()
            .into_inner();
        assert_eq!(got.value, "v3");
    }

    #[tokio::test]
    async fn delete_current_version_rolls_back_index() {
        let svc = fresh_service();
        for v in ["v1", "v2"] {
            svc.put_secret(Request::new(PutSecretRequest {
                namespace: "ns".into(),
                key: "k".into(),
                value: v.into(),
                description: None,
            }))
            .await
            .unwrap();
        }

        // Delete v2 (current)
        svc.delete_secret_version(Request::new(DeleteSecretVersionRequest {
            namespace: "ns".into(),
            key: "k".into(),
            version: 2,
        }))
        .await
        .unwrap();

        // latest should now be v1
        let got = svc
            .get_secret(Request::new(GetSecretRequest {
                namespace: "ns".into(),
                key: "k".into(),
                version: None,
            }))
            .await
            .unwrap()
            .into_inner();
        assert_eq!(got.value, "v1");
        assert_eq!(got.version, 1);
    }

    #[tokio::test]
    async fn delete_last_version_fails() {
        let svc = fresh_service();
        svc.put_secret(Request::new(PutSecretRequest {
            namespace: "ns".into(),
            key: "k".into(),
            value: "only".into(),
            description: None,
        }))
        .await
        .unwrap();

        let err = svc
            .delete_secret_version(Request::new(DeleteSecretVersionRequest {
                namespace: "ns".into(),
                key: "k".into(),
                version: 1,
            }))
            .await
            .unwrap_err();
        assert_eq!(err.code(), tonic::Code::FailedPrecondition);
    }

    #[tokio::test]
    async fn update_description_metadata_only() {
        let svc = fresh_service();
        svc.put_secret(Request::new(PutSecretRequest {
            namespace: "ns".into(),
            key: "k".into(),
            value: "secret".into(),
            description: Some("original".into()),
        }))
        .await
        .unwrap();

        let resp = svc
            .update_secret_description(Request::new(UpdateSecretDescriptionRequest {
                namespace: "ns".into(),
                key: "k".into(),
                description: "updated desc".into(),
            }))
            .await
            .unwrap()
            .into_inner();
        assert_eq!(resp.description, "updated desc");

        // Version should NOT have changed (still 1)
        let list = svc
            .list_secret_versions(Request::new(ListSecretVersionsRequest {
                namespace: "ns".into(),
                key: "k".into(),
            }))
            .await
            .unwrap()
            .into_inner();
        assert_eq!(list.count, 1);

        // Description visible in list
        let secrets = svc
            .list_secrets(Request::new(ListSecretsRequest {
                namespace: Some("ns".into()),
                include_deleted: false,
            }))
            .await
            .unwrap()
            .into_inner();
        assert_eq!(
            secrets.secrets[0].description.as_deref(),
            Some("updated desc")
        );
    }
}
