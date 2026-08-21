//! `PasswordVaultService` — domain wrapper above `GenericSecretsService`
//! for the password vault (namespace="passwords").
//!
//! Provides server-side TOTP, audit-tagged access, and the canonical
//! VaultEntry schema (title/username/password/url/notes/tags/...).
//! Internally delegates all storage to `GenericSecretsServiceImpl` via
//! `do_put("passwords", title, json_str)` / `do_get("passwords", title)`.
//!
//! Per #3923 integration doc, this is the Phase 1c storage merge.
//! VaultEntryPlaintext is JSON-serialised as the value string stored
//! in the unified `entries/passwords/{title}` path.
//!
//! Server-side TOTP is the security invariant the rewrite preserves:
//! the totp_secret never leaves the server — `GetEntry` always redacts
//! it, and clients call `GenerateTotp` to get a current code.
//!
//! Loaded as a dylib plugin by `nexusd-cluster` via `--plugin-dir`.

pub mod proto {
    //! Generated tonic stubs from
    //! `proto/nexus/password_vault/v1/password_vault.proto`.
    tonic::include_proto!("nexus.password_vault.v1");
}

pub mod crypto;
pub(crate) mod storage;
pub mod types;

// Re-export the public error type for binaries that host the service.
pub use types::PasswordVaultError;

use std::collections::HashMap;
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use parking_lot::Mutex;
use tonic::{Request, Response, Status};

use proto::password_vault_service_server::PasswordVaultService;
use proto::{
    DeleteEntryRequest, DeleteEntryResponse, GenerateTotpRequest, GenerateTotpResponse,
    GetEntryRequest, GetEntryResponse, ListEntriesRequest, ListEntriesResponse,
    ListVersionsRequest, ListVersionsResponse, PutEntryRequest, PutEntryResponse,
    RestoreEntryRequest, RestoreEntryResponse, VaultEntry as ProtoVaultEntry,
};

use self::types::VaultEntryPlaintext;
use crate::generic_secrets::GenericSecretsServiceImpl;

/// The namespace under which all password-vault entries are stored.
const PASSWORDS_NAMESPACE: &str = "passwords";

/// RFC 6238 default: 30-second window.
const TOTP_PERIOD_SECONDS: u64 = 30;

/// Cache key for TOTP oracle de-duplication. `(title, window_index)`
type TotpCacheKey = (String, u64);

/// Compute a 6-digit TOTP code per RFC 6238 (HMAC-SHA1, 30s window).
fn compute_totp(secret_b32: &str, time_seconds: u64) -> Result<String, PasswordVaultError> {
    use hmac::{Hmac, Mac};
    use sha1::Sha1;

    let normalised: String = secret_b32
        .chars()
        .filter(|c| !c.is_whitespace())
        .flat_map(char::to_uppercase)
        .collect();
    let key = base32::decode(base32::Alphabet::Rfc4648 { padding: false }, &normalised)
        .ok_or_else(|| PasswordVaultError::Invalid("totp_secret is not valid base32".into()))?;
    if key.is_empty() {
        return Err(PasswordVaultError::Invalid(
            "totp_secret decoded to empty bytes".into(),
        ));
    }

    let window = time_seconds / TOTP_PERIOD_SECONDS;
    let counter_bytes = window.to_be_bytes();

    type HmacSha1 = Hmac<Sha1>;
    let mut mac = HmacSha1::new_from_slice(&key).map_err(|_| PasswordVaultError::Crypto)?;
    mac.update(&counter_bytes);
    let hmac_result = mac.finalize().into_bytes();

    let offset = (hmac_result[19] & 0x0f) as usize;
    let truncated = u32::from_be_bytes([
        hmac_result[offset] & 0x7f,
        hmac_result[offset + 1],
        hmac_result[offset + 2],
        hmac_result[offset + 3],
    ]);
    Ok(format!("{:06}", truncated % 1_000_000))
}

/// Service state. Wrapped in `Arc` so the tonic-required `Clone`
/// impl on `PasswordVaultServiceImpl` is cheap.
struct Inner {
    secrets: GenericSecretsServiceImpl,
    totp_cache: Mutex<HashMap<TotpCacheKey, String>>,
}

/// Tonic-facing service. Cloneable (cheap: just bumps the Arc).
#[derive(Clone)]
pub struct PasswordVaultServiceImpl {
    inner: Arc<Inner>,
}

impl PasswordVaultServiceImpl {
    /// Convenience wrapper for tests: creates a Kernel + in-memory
    /// backend internally. Not for production — content is ephemeral.
    pub fn new(
        data_dir: &std::path::Path,
        master_key_path: &std::path::Path,
    ) -> Result<Self, PasswordVaultError> {
        let kernel = Arc::new(kernel::kernel::Kernel::new());
        let backend: Arc<dyn kernel::abc::object_store::ObjectStore> =
            Arc::new(storage::MemBackend::new());

        let meta_path = data_dir.join("vault-meta.redb");
        if let Some(p) = meta_path.to_str() {
            let _ = kernel.set_metastore_path(p);
        }

        Self::new_with_kernel(kernel, "/vault", master_key_path, backend)
    }

    /// Create a vault service on an existing kernel with a caller-
    /// provided backend. Mounts the backend, creates the
    /// `GenericSecretsServiceImpl`, and wraps it.
    pub fn new_with_kernel(
        kernel: Arc<kernel::kernel::Kernel>,
        root: &str,
        master_key_path: &std::path::Path,
        backend: Arc<dyn kernel::abc::object_store::ObjectStore>,
    ) -> Result<Self, PasswordVaultError> {
        let root = root.trim_end_matches('/');
        let backend_name = backend.name().to_string();

        // Mount the backend at root.
        kernel
            .sys_setattr(
                root,
                /* DT_MOUNT */ 2,
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
            .map_err(|e| PasswordVaultError::Storage(format!("mount {root}: {e:?}")))?;

        let master_key = crypto::load_or_create_master_key(master_key_path)?;
        let secrets = GenericSecretsServiceImpl::new_on_existing_mount(kernel, root, master_key)?;
        Ok(Self::new_with_secrets(secrets))
    }

    /// Create a password-vault service wrapping an existing
    /// `GenericSecretsServiceImpl`. Used by the vault plugin where
    /// the secrets service is created first.
    pub fn new_with_secrets(secrets: GenericSecretsServiceImpl) -> Self {
        Self {
            inner: Arc::new(Inner {
                secrets,
                totp_cache: Mutex::new(HashMap::new()),
            }),
        }
    }
}

// ---------------------------------------------------------------------
// Conversion helpers — proto <-> internal types.
// ---------------------------------------------------------------------

fn proto_to_plaintext(p: ProtoVaultEntry) -> VaultEntryPlaintext {
    VaultEntryPlaintext {
        title: p.title,
        username: p.username.unwrap_or_default(),
        password: p.password.unwrap_or_default(),
        url: p.url.unwrap_or_default(),
        notes: p.notes.unwrap_or_default(),
        tags: p.tags.unwrap_or_default(),
        totp_secret: p.totp_secret.unwrap_or_default(),
        extra_json: p.extra_json.unwrap_or_default(),
    }
}

/// `plaintext_to_proto`: always redacts `totp_secret` (security invariant).
fn plaintext_to_proto(p: VaultEntryPlaintext) -> ProtoVaultEntry {
    ProtoVaultEntry {
        title: p.title,
        username: Some(p.username),
        password: Some(p.password),
        url: Some(p.url),
        notes: Some(p.notes),
        tags: Some(p.tags),
        totp_secret: None, // ALWAYS redacted — security invariant
        extra_json: Some(p.extra_json),
    }
}

fn unix_ms_to_proto_ts(ms: u64) -> prost_types::Timestamp {
    prost_types::Timestamp {
        seconds: (ms / 1_000) as i64,
        nanos: ((ms % 1_000) * 1_000_000) as i32,
    }
}

/// JSON-serialize VaultEntryPlaintext for storage via GenericSecretsService.
fn serialize_plaintext(plain: &VaultEntryPlaintext) -> Result<String, Status> {
    serde_json::to_string(plain).map_err(|e| Status::internal(format!("serialise entry: {e}")))
}

/// JSON-deserialize VaultEntryPlaintext from GenericSecretsService value.
fn deserialize_plaintext(json: &str) -> Result<VaultEntryPlaintext, PasswordVaultError> {
    serde_json::from_str(json).map_err(|_| PasswordVaultError::Crypto)
}

// ---------------------------------------------------------------------
// gRPC trait impl — all methods delegate to GenericSecretsServiceImpl.
// ---------------------------------------------------------------------

#[tonic::async_trait]
impl PasswordVaultService for PasswordVaultServiceImpl {
    async fn put_entry(
        &self,
        req: Request<PutEntryRequest>,
    ) -> Result<Response<PutEntryResponse>, Status> {
        let req = req.into_inner();
        let entry = req
            .entry
            .ok_or_else(|| Status::invalid_argument("entry field is required"))?;
        if entry.title.is_empty() {
            return Err(Status::invalid_argument(
                "entry.title is required (non-empty)",
            ));
        }
        let title = entry.title.clone();

        let plain = proto_to_plaintext(entry);
        let json_str = serialize_plaintext(&plain)?;

        let metadata = self
            .inner
            .secrets
            .do_put(PASSWORDS_NAMESPACE, &title, &json_str, None)?;

        Ok(Response::new(PutEntryResponse {
            id: title.clone(),
            title,
            version: metadata.current_version,
            created_at: metadata.updated_at,
        }))
    }

    async fn get_entry(
        &self,
        req: Request<GetEntryRequest>,
    ) -> Result<Response<GetEntryResponse>, Status> {
        let req = req.into_inner();
        if req.title.is_empty() {
            return Err(Status::invalid_argument("title is required (non-empty)"));
        }

        let (json_str, version) =
            self.inner
                .secrets
                .do_get(PASSWORDS_NAMESPACE, &req.title, req.version)?;
        let plain = deserialize_plaintext(&json_str)?;

        Ok(Response::new(GetEntryResponse {
            entry: Some(plaintext_to_proto(plain)),
            version,
        }))
    }

    async fn list_entries(
        &self,
        req: Request<ListEntriesRequest>,
    ) -> Result<Response<ListEntriesResponse>, Status> {
        let req = req.into_inner();

        // List all live entries in the "passwords" namespace.
        let live = self
            .inner
            .secrets
            .do_list_metadata(Some(PASSWORDS_NAMESPACE), false)?;
        let total_live = live.len() as i32;

        let query_lower = req.query.to_lowercase();
        let want_filter = !query_lower.is_empty();
        let mut matched = Vec::new();

        for (_ns, title, _idx) in &live {
            let (json_str, _version) =
                match self.inner.secrets.do_get(PASSWORDS_NAMESPACE, title, None) {
                    Ok(r) => r,
                    Err(_) => continue,
                };
            let plain = match deserialize_plaintext(&json_str) {
                Ok(p) => p,
                Err(_) => continue,
            };
            if want_filter {
                let haystack = format!(
                    "{} {} {} {}",
                    plain.title.to_lowercase(),
                    plain.username.to_lowercase(),
                    plain.url.to_lowercase(),
                    plain.tags.to_lowercase()
                );
                if !haystack.contains(&query_lower) {
                    continue;
                }
            }
            matched.push(plaintext_to_proto(plain));
        }
        let matched_count = matched.len() as i32;

        if req.limit > 0 && matched.len() > req.limit as usize {
            matched.truncate(req.limit as usize);
        }

        Ok(Response::new(ListEntriesResponse {
            entries: matched,
            total_in_vault: total_live,
            matched: matched_count,
        }))
    }

    async fn delete_entry(
        &self,
        req: Request<DeleteEntryRequest>,
    ) -> Result<Response<DeleteEntryResponse>, Status> {
        let req = req.into_inner();
        if req.title.is_empty() {
            return Err(Status::invalid_argument("title is required"));
        }
        self.inner
            .secrets
            .do_delete(PASSWORDS_NAMESPACE, &req.title)?;
        Ok(Response::new(DeleteEntryResponse {
            title: req.title,
            deleted: true,
        }))
    }

    async fn restore_entry(
        &self,
        req: Request<RestoreEntryRequest>,
    ) -> Result<Response<RestoreEntryResponse>, Status> {
        let req = req.into_inner();
        if req.title.is_empty() {
            return Err(Status::invalid_argument("title is required"));
        }
        let (_restored, current_version) = self
            .inner
            .secrets
            .do_restore(PASSWORDS_NAMESPACE, &req.title)?;
        Ok(Response::new(RestoreEntryResponse {
            title: req.title,
            restored: true,
            current_version,
        }))
    }

    async fn list_versions(
        &self,
        req: Request<ListVersionsRequest>,
    ) -> Result<Response<ListVersionsResponse>, Status> {
        let req = req.into_inner();
        if req.title.is_empty() {
            return Err(Status::invalid_argument("title is required"));
        }
        let (stored, idx) = self
            .inner
            .secrets
            .do_list_versions(PASSWORDS_NAMESPACE, &req.title)?;
        let active = idx.current_version;
        let is_deleted = idx.deleted_at_ms.is_some();
        let versions: Vec<proto::Version> = stored
            .into_iter()
            .map(|s| proto::Version {
                version: s.version as i32,
                created_at: Some(unix_ms_to_proto_ts(s.created_at_ms)),
                tombstoned: is_deleted && s.version == active,
            })
            .collect();
        let count = versions.len() as i32;
        Ok(Response::new(ListVersionsResponse {
            title: req.title,
            count,
            versions,
        }))
    }

    async fn generate_totp(
        &self,
        req: Request<GenerateTotpRequest>,
    ) -> Result<Response<GenerateTotpResponse>, Status> {
        let req = req.into_inner();
        if req.title.is_empty() {
            return Err(Status::invalid_argument("title is required"));
        }

        // do_get checks soft-delete for us (None version = latest, 404 if deleted).
        let (json_str, _version) =
            self.inner
                .secrets
                .do_get(PASSWORDS_NAMESPACE, &req.title, None)?;
        let plain = deserialize_plaintext(&json_str)?;

        if plain.totp_secret.is_empty() {
            return Err(PasswordVaultError::TotpNotConfigured(req.title).into());
        }

        let now_secs = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_secs())
            .unwrap_or(0);
        let window = now_secs / TOTP_PERIOD_SECONDS;
        let cache_key = (req.title.clone(), window);

        let code = {
            let mut cache = self.inner.totp_cache.lock();
            if let Some(cached) = cache.get(&cache_key) {
                cached.clone()
            } else {
                let computed = compute_totp(&plain.totp_secret, now_secs)?;
                cache.insert(cache_key.clone(), computed.clone());
                cache.retain(|(_, w), _| *w >= window);
                computed
            }
        };

        Ok(Response::new(GenerateTotpResponse {
            code,
            expires_in_seconds: (TOTP_PERIOD_SECONDS - (now_secs % TOTP_PERIOD_SECONDS)) as i32,
            period_seconds: TOTP_PERIOD_SECONDS as i32,
        }))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    fn fresh_service() -> (TempDir, PasswordVaultServiceImpl) {
        let dir = TempDir::new().unwrap();
        let svc =
            PasswordVaultServiceImpl::new(dir.path(), &dir.path().join("master.key")).unwrap();
        (dir, svc)
    }

    fn entry(title: &str, password: &str) -> ProtoVaultEntry {
        ProtoVaultEntry {
            title: title.into(),
            username: Some("alice".into()),
            password: Some(password.into()),
            url: Some("https://example.com".into()),
            notes: None,
            tags: None,
            totp_secret: None,
            extra_json: None,
        }
    }

    #[tokio::test]
    async fn put_then_get_round_trip() {
        let (_d, svc) = fresh_service();
        let resp = svc
            .put_entry(Request::new(PutEntryRequest {
                entry: Some(entry("gmail", "hunter2")),
                audit: None,
            }))
            .await
            .unwrap()
            .into_inner();
        assert_eq!(resp.title, "gmail");
        assert_eq!(resp.version, 1);

        let got = svc
            .get_entry(Request::new(GetEntryRequest {
                title: "gmail".into(),
                version: None,
                audit: None,
            }))
            .await
            .unwrap()
            .into_inner();
        let e = got.entry.unwrap();
        assert_eq!(e.title, "gmail");
        assert_eq!(e.username.as_deref(), Some("alice"));
        assert_eq!(e.password.as_deref(), Some("hunter2"));
        assert_eq!(got.version, 1);
    }

    #[tokio::test]
    async fn put_increments_version() {
        let (_d, svc) = fresh_service();
        for (i, pw) in ["v1", "v2", "v3"].iter().enumerate() {
            let r = svc
                .put_entry(Request::new(PutEntryRequest {
                    entry: Some(entry("gmail", pw)),
                    audit: None,
                }))
                .await
                .unwrap()
                .into_inner();
            assert_eq!(r.version, (i + 1) as i32);
        }
        let got = svc
            .get_entry(Request::new(GetEntryRequest {
                title: "gmail".into(),
                version: None,
                audit: None,
            }))
            .await
            .unwrap()
            .into_inner();
        assert_eq!(got.entry.unwrap().password.as_deref(), Some("v3"));
        assert_eq!(got.version, 3);
    }

    #[tokio::test]
    async fn get_specific_historical_version() {
        let (_d, svc) = fresh_service();
        for pw in ["v1", "v2", "v3"] {
            svc.put_entry(Request::new(PutEntryRequest {
                entry: Some(entry("gmail", pw)),
                audit: None,
            }))
            .await
            .unwrap();
        }
        let got = svc
            .get_entry(Request::new(GetEntryRequest {
                title: "gmail".into(),
                version: Some(2),
                audit: None,
            }))
            .await
            .unwrap()
            .into_inner();
        assert_eq!(got.entry.unwrap().password.as_deref(), Some("v2"));
        assert_eq!(got.version, 2);
    }

    #[tokio::test]
    async fn get_always_redacts_totp_secret() {
        let (_d, svc) = fresh_service();
        let mut e = entry("aws", "pw");
        e.totp_secret = Some("JBSWY3DPEHPK3PXP".into());
        svc.put_entry(Request::new(PutEntryRequest {
            entry: Some(e),
            audit: None,
        }))
        .await
        .unwrap();

        let got = svc
            .get_entry(Request::new(GetEntryRequest {
                title: "aws".into(),
                version: None,
                audit: None,
            }))
            .await
            .unwrap()
            .into_inner();
        assert!(got.entry.unwrap().totp_secret.is_none());
    }

    #[tokio::test]
    async fn put_requires_entry() {
        let (_d, svc) = fresh_service();
        let err = svc
            .put_entry(Request::new(PutEntryRequest {
                entry: None,
                audit: None,
            }))
            .await
            .unwrap_err();
        assert_eq!(err.code(), tonic::Code::InvalidArgument);
    }

    #[tokio::test]
    async fn put_requires_nonempty_title() {
        let (_d, svc) = fresh_service();
        let err = svc
            .put_entry(Request::new(PutEntryRequest {
                entry: Some(entry("", "pw")),
                audit: None,
            }))
            .await
            .unwrap_err();
        assert_eq!(err.code(), tonic::Code::InvalidArgument);
    }

    #[tokio::test]
    async fn get_unknown_returns_not_found() {
        let (_d, svc) = fresh_service();
        let err = svc
            .get_entry(Request::new(GetEntryRequest {
                title: "nope".into(),
                version: None,
                audit: None,
            }))
            .await
            .unwrap_err();
        assert_eq!(err.code(), tonic::Code::NotFound);
    }

    // -----------------------------------------------------------------
    // ListEntries / DeleteEntry / RestoreEntry tests
    // -----------------------------------------------------------------

    async fn seed(svc: &PasswordVaultServiceImpl, titles: &[(&str, &str)]) {
        for (t, p) in titles {
            svc.put_entry(Request::new(PutEntryRequest {
                entry: Some(entry(t, p)),
                audit: None,
            }))
            .await
            .unwrap();
        }
    }

    #[tokio::test]
    async fn list_returns_all_entries() {
        let (_d, svc) = fresh_service();
        seed(&svc, &[("gmail", "pw1"), ("github", "pw2"), ("aws", "pw3")]).await;
        let r = svc
            .list_entries(Request::new(ListEntriesRequest {
                query: String::new(),
                limit: 0,
                audit: None,
            }))
            .await
            .unwrap()
            .into_inner();
        assert_eq!(r.total_in_vault, 3);
        assert_eq!(r.matched, 3);
        assert_eq!(r.entries.len(), 3);
    }

    #[tokio::test]
    async fn list_filters_by_query_case_insensitive() {
        let (_d, svc) = fresh_service();
        seed(&svc, &[("Gmail", "x"), ("GitHub", "y"), ("AWS", "z")]).await;
        let r = svc
            .list_entries(Request::new(ListEntriesRequest {
                query: "git".into(),
                limit: 0,
                audit: None,
            }))
            .await
            .unwrap()
            .into_inner();
        assert_eq!(r.total_in_vault, 3);
        assert_eq!(r.matched, 1);
        assert_eq!(r.entries.len(), 1);
        assert_eq!(r.entries[0].title, "GitHub");
    }

    #[tokio::test]
    async fn list_respects_limit() {
        let (_d, svc) = fresh_service();
        seed(&svc, &[("a", "x"), ("b", "y"), ("c", "z")]).await;
        let r = svc
            .list_entries(Request::new(ListEntriesRequest {
                query: String::new(),
                limit: 2,
                audit: None,
            }))
            .await
            .unwrap()
            .into_inner();
        assert_eq!(r.matched, 3);
        assert_eq!(r.entries.len(), 2);
    }

    #[tokio::test]
    async fn list_excludes_soft_deleted() {
        let (_d, svc) = fresh_service();
        seed(&svc, &[("a", "x"), ("b", "y")]).await;
        svc.delete_entry(Request::new(DeleteEntryRequest {
            title: "a".into(),
            audit: None,
        }))
        .await
        .unwrap();
        let r = svc
            .list_entries(Request::new(ListEntriesRequest {
                query: String::new(),
                limit: 0,
                audit: None,
            }))
            .await
            .unwrap()
            .into_inner();
        assert_eq!(r.total_in_vault, 1);
        assert_eq!(r.matched, 1);
        assert_eq!(r.entries[0].title, "b");
    }

    #[tokio::test]
    async fn delete_then_get_latest_is_not_found() {
        let (_d, svc) = fresh_service();
        seed(&svc, &[("a", "pw")]).await;
        let d = svc
            .delete_entry(Request::new(DeleteEntryRequest {
                title: "a".into(),
                audit: None,
            }))
            .await
            .unwrap()
            .into_inner();
        assert!(d.deleted);
        let err = svc
            .get_entry(Request::new(GetEntryRequest {
                title: "a".into(),
                version: None,
                audit: None,
            }))
            .await
            .unwrap_err();
        assert_eq!(err.code(), tonic::Code::NotFound);
        // Explicit historical version still works.
        let got = svc
            .get_entry(Request::new(GetEntryRequest {
                title: "a".into(),
                version: Some(1),
                audit: None,
            }))
            .await
            .unwrap()
            .into_inner();
        assert_eq!(got.entry.unwrap().password.as_deref(), Some("pw"));
    }

    #[tokio::test]
    async fn restore_revives_entry() {
        let (_d, svc) = fresh_service();
        seed(&svc, &[("a", "pw")]).await;
        svc.delete_entry(Request::new(DeleteEntryRequest {
            title: "a".into(),
            audit: None,
        }))
        .await
        .unwrap();
        let r = svc
            .restore_entry(Request::new(RestoreEntryRequest {
                title: "a".into(),
                audit: None,
            }))
            .await
            .unwrap()
            .into_inner();
        assert!(r.restored);
        assert_eq!(r.current_version, 1);
        let got = svc
            .get_entry(Request::new(GetEntryRequest {
                title: "a".into(),
                version: None,
                audit: None,
            }))
            .await
            .unwrap()
            .into_inner();
        assert_eq!(got.entry.unwrap().password.as_deref(), Some("pw"));
    }

    #[tokio::test]
    async fn put_revives_soft_deleted() {
        let (_d, svc) = fresh_service();
        seed(&svc, &[("a", "v1")]).await;
        svc.delete_entry(Request::new(DeleteEntryRequest {
            title: "a".into(),
            audit: None,
        }))
        .await
        .unwrap();
        let put = svc
            .put_entry(Request::new(PutEntryRequest {
                entry: Some(entry("a", "v2")),
                audit: None,
            }))
            .await
            .unwrap()
            .into_inner();
        assert_eq!(put.version, 2);
        let got = svc
            .get_entry(Request::new(GetEntryRequest {
                title: "a".into(),
                version: None,
                audit: None,
            }))
            .await
            .unwrap()
            .into_inner();
        assert_eq!(got.entry.unwrap().password.as_deref(), Some("v2"));
    }

    #[tokio::test]
    async fn delete_unknown_returns_not_found() {
        let (_d, svc) = fresh_service();
        let err = svc
            .delete_entry(Request::new(DeleteEntryRequest {
                title: "nope".into(),
                audit: None,
            }))
            .await
            .unwrap_err();
        assert_eq!(err.code(), tonic::Code::NotFound);
    }

    #[tokio::test]
    async fn restore_unknown_returns_not_found() {
        let (_d, svc) = fresh_service();
        let err = svc
            .restore_entry(Request::new(RestoreEntryRequest {
                title: "nope".into(),
                audit: None,
            }))
            .await
            .unwrap_err();
        assert_eq!(err.code(), tonic::Code::NotFound);
    }

    // -----------------------------------------------------------------
    // ListVersions tests
    // -----------------------------------------------------------------

    #[tokio::test]
    async fn list_versions_returns_history_in_order() {
        let (_d, svc) = fresh_service();
        for pw in ["v1", "v2", "v3"] {
            svc.put_entry(Request::new(PutEntryRequest {
                entry: Some(entry("gmail", pw)),
                audit: None,
            }))
            .await
            .unwrap();
        }
        let r = svc
            .list_versions(Request::new(ListVersionsRequest {
                title: "gmail".into(),
                audit: None,
            }))
            .await
            .unwrap()
            .into_inner();
        assert_eq!(r.title, "gmail");
        assert_eq!(r.count, 3);
        let vers: Vec<i32> = r.versions.iter().map(|v| v.version).collect();
        assert_eq!(vers, vec![1, 2, 3]);
        assert!(r.versions.iter().all(|v| !v.tombstoned));
    }

    #[tokio::test]
    async fn list_versions_marks_tombstone_on_soft_deleted() {
        let (_d, svc) = fresh_service();
        for pw in ["v1", "v2"] {
            svc.put_entry(Request::new(PutEntryRequest {
                entry: Some(entry("a", pw)),
                audit: None,
            }))
            .await
            .unwrap();
        }
        svc.delete_entry(Request::new(DeleteEntryRequest {
            title: "a".into(),
            audit: None,
        }))
        .await
        .unwrap();
        let r = svc
            .list_versions(Request::new(ListVersionsRequest {
                title: "a".into(),
                audit: None,
            }))
            .await
            .unwrap()
            .into_inner();
        assert_eq!(r.count, 2);
        assert!(!r.versions[0].tombstoned);
        assert!(r.versions[1].tombstoned);
    }

    #[tokio::test]
    async fn list_versions_unknown_returns_not_found() {
        let (_d, svc) = fresh_service();
        let err = svc
            .list_versions(Request::new(ListVersionsRequest {
                title: "nope".into(),
                audit: None,
            }))
            .await
            .unwrap_err();
        assert_eq!(err.code(), tonic::Code::NotFound);
    }

    // -----------------------------------------------------------------
    // GenerateTotp + compute_totp tests
    // -----------------------------------------------------------------

    const RFC_SEED_B32: &str = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ";

    #[test]
    fn compute_totp_matches_rfc6238_vectors() {
        assert_eq!(compute_totp(RFC_SEED_B32, 59).unwrap(), "287082");
        assert_eq!(compute_totp(RFC_SEED_B32, 1_111_111_109).unwrap(), "081804");
        assert_eq!(compute_totp(RFC_SEED_B32, 1_234_567_890).unwrap(), "005924");
    }

    #[test]
    fn compute_totp_lowercase_base32_works() {
        assert_eq!(
            compute_totp(&RFC_SEED_B32.to_lowercase(), 59).unwrap(),
            "287082"
        );
    }

    #[test]
    fn compute_totp_rejects_invalid_base32() {
        assert!(compute_totp("not-base32!@#", 0).is_err());
    }

    #[tokio::test]
    async fn generate_totp_returns_6_digits() {
        let (_d, svc) = fresh_service();
        let mut e = entry("aws", "pw");
        e.totp_secret = Some(RFC_SEED_B32.into());
        svc.put_entry(Request::new(PutEntryRequest {
            entry: Some(e),
            audit: None,
        }))
        .await
        .unwrap();
        let r = svc
            .generate_totp(Request::new(GenerateTotpRequest {
                title: "aws".into(),
                audit: None,
            }))
            .await
            .unwrap()
            .into_inner();
        assert_eq!(r.code.len(), 6);
        assert!(r.code.chars().all(|c| c.is_ascii_digit()));
        assert_eq!(r.period_seconds, 30);
        assert!(r.expires_in_seconds > 0 && r.expires_in_seconds <= 30);
    }

    #[tokio::test]
    async fn generate_totp_not_configured_when_no_seed() {
        let (_d, svc) = fresh_service();
        svc.put_entry(Request::new(PutEntryRequest {
            entry: Some(entry("aws", "pw")),
            audit: None,
        }))
        .await
        .unwrap();
        let err = svc
            .generate_totp(Request::new(GenerateTotpRequest {
                title: "aws".into(),
                audit: None,
            }))
            .await
            .unwrap_err();
        assert_eq!(err.code(), tonic::Code::FailedPrecondition);
    }

    #[tokio::test]
    async fn generate_totp_unknown_returns_not_found() {
        let (_d, svc) = fresh_service();
        let err = svc
            .generate_totp(Request::new(GenerateTotpRequest {
                title: "nope".into(),
                audit: None,
            }))
            .await
            .unwrap_err();
        assert_eq!(err.code(), tonic::Code::NotFound);
    }

    #[tokio::test]
    async fn generate_totp_soft_deleted_returns_not_found() {
        let (_d, svc) = fresh_service();
        let mut e = entry("aws", "pw");
        e.totp_secret = Some(RFC_SEED_B32.into());
        svc.put_entry(Request::new(PutEntryRequest {
            entry: Some(e),
            audit: None,
        }))
        .await
        .unwrap();
        svc.delete_entry(Request::new(DeleteEntryRequest {
            title: "aws".into(),
            audit: None,
        }))
        .await
        .unwrap();
        let err = svc
            .generate_totp(Request::new(GenerateTotpRequest {
                title: "aws".into(),
                audit: None,
            }))
            .await
            .unwrap_err();
        assert_eq!(err.code(), tonic::Code::NotFound);
    }

    #[tokio::test]
    async fn generate_totp_returns_same_code_within_window() {
        let (_d, svc) = fresh_service();
        let mut e = entry("aws", "pw");
        e.totp_secret = Some(RFC_SEED_B32.into());
        svc.put_entry(Request::new(PutEntryRequest {
            entry: Some(e),
            audit: None,
        }))
        .await
        .unwrap();
        let a = svc
            .generate_totp(Request::new(GenerateTotpRequest {
                title: "aws".into(),
                audit: None,
            }))
            .await
            .unwrap()
            .into_inner();
        let b = svc
            .generate_totp(Request::new(GenerateTotpRequest {
                title: "aws".into(),
                audit: None,
            }))
            .await
            .unwrap()
            .into_inner();
        assert_eq!(a.code, b.code);
    }

    #[tokio::test]
    async fn delete_is_idempotent() {
        let (_d, svc) = fresh_service();
        seed(&svc, &[("a", "pw")]).await;
        svc.delete_entry(Request::new(DeleteEntryRequest {
            title: "a".into(),
            audit: None,
        }))
        .await
        .unwrap();
        let r2 = svc
            .delete_entry(Request::new(DeleteEntryRequest {
                title: "a".into(),
                audit: None,
            }))
            .await
            .unwrap()
            .into_inner();
        assert!(r2.deleted);
    }

    // -----------------------------------------------------------------
    // Cross-service integration: password data visible via generic
    // secrets API (namespace="passwords").
    // -----------------------------------------------------------------

    #[tokio::test]
    async fn password_entries_visible_via_generic_secrets_namespace() {
        let (_d, svc) = fresh_service();
        seed(&svc, &[("gmail", "pw1"), ("github", "pw2")]).await;

        // The underlying GenericSecretsServiceImpl should see these
        // entries in the "passwords" namespace.
        let metadata = svc
            .inner
            .secrets
            .do_list_metadata(Some(PASSWORDS_NAMESPACE), false)
            .unwrap();
        assert_eq!(metadata.len(), 2);
        assert!(metadata.iter().all(|(ns, _, _)| ns == PASSWORDS_NAMESPACE));
    }
}

// -----------------------------------------------------------------
// Cross-repo E2E integration tests.
// -----------------------------------------------------------------

#[cfg(test)]
mod e2e_integration {
    use super::*;
    use kernel::kernel::convenience::KernelConvenience;
    use kernel::kernel::{Kernel, OperationContext};

    fn kernel_service() -> (Arc<Kernel>, PasswordVaultServiceImpl) {
        let kernel = Arc::new(Kernel::new());
        let dir = tempfile::TempDir::new().unwrap();
        let backend: Arc<dyn kernel::abc::object_store::ObjectStore> =
            Arc::new(storage::MemBackend::new());
        let svc = PasswordVaultServiceImpl::new_with_kernel(
            kernel.clone(),
            "/vault",
            &dir.path().join("master.key"),
            backend,
        )
        .unwrap();
        (kernel, svc)
    }

    fn entry(title: &str, password: &str) -> ProtoVaultEntry {
        ProtoVaultEntry {
            title: title.into(),
            username: Some("alice".into()),
            password: Some(password.into()),
            url: Some("https://example.com".into()),
            notes: None,
            tags: None,
            totp_secret: None,
            extra_json: None,
        }
    }

    // ── Scenario 1: Password rotation with audit trail ──────────────

    #[tokio::test]
    async fn password_rotation_with_audit_trail() {
        let (kernel, svc) = kernel_service();
        let ctx = OperationContext::new("test", "root", true, None, true);

        let put1 = svc
            .put_entry(Request::new(PutEntryRequest {
                entry: Some(entry("github", "initial-pw")),
                audit: None,
            }))
            .await
            .unwrap()
            .into_inner();
        assert_eq!(put1.version, 1);
        assert_eq!(put1.title, "github");

        // Kernel cross-verify: index file written to unified path.
        let index_read =
            KernelConvenience::read(&*kernel, "/vault/entries/passwords/github", &ctx, 0, 0)
                .expect("index entry should exist in VFS");
        assert!(index_read.data.is_some(), "index should have content");

        let put2 = svc
            .put_entry(Request::new(PutEntryRequest {
                entry: Some(entry(&put1.title, "rotated-pw")),
                audit: None,
            }))
            .await
            .unwrap()
            .into_inner();
        assert_eq!(put2.version, 2);

        let versions = svc
            .list_versions(Request::new(ListVersionsRequest {
                title: put1.title.clone(),
                audit: None,
            }))
            .await
            .unwrap()
            .into_inner();
        assert_eq!(versions.count, 2);
        let ver_nums: Vec<i32> = versions.versions.iter().map(|v| v.version).collect();
        assert_eq!(ver_nums, vec![1, 2]);

        let hist = svc
            .get_entry(Request::new(GetEntryRequest {
                title: put1.title.clone(),
                version: Some(versions.versions[0].version),
                audit: None,
            }))
            .await
            .unwrap()
            .into_inner();
        assert_eq!(hist.entry.unwrap().password.as_deref(), Some("initial-pw"),);

        // Kernel cross-verify: both version files under unified path.
        let version_dir = kernel.sys_readdir(
            "/vault/versions/passwords/github",
            "root",
            true,
            kernel::kernel::syscall::ReaddirOpts::default(),
        );
        assert_eq!(version_dir.len(), 2);
    }

    // ── Scenario 2: Accidental delete and recovery ───────────────

    #[tokio::test]
    async fn accidental_delete_and_recovery() {
        let (kernel, svc) = kernel_service();

        let put = svc
            .put_entry(Request::new(PutEntryRequest {
                entry: Some(entry("aws-prod", "s3cret-key")),
                audit: None,
            }))
            .await
            .unwrap()
            .into_inner();
        assert_eq!(put.version, 1);

        let del = svc
            .delete_entry(Request::new(DeleteEntryRequest {
                title: put.title.clone(),
                audit: None,
            }))
            .await
            .unwrap()
            .into_inner();
        assert!(del.deleted);

        let err = svc
            .get_entry(Request::new(GetEntryRequest {
                title: put.title.clone(),
                version: None,
                audit: None,
            }))
            .await
            .unwrap_err();
        assert_eq!(err.code(), tonic::Code::NotFound);

        let restored = svc
            .restore_entry(Request::new(RestoreEntryRequest {
                title: put.title.clone(),
                audit: None,
            }))
            .await
            .unwrap()
            .into_inner();
        assert!(restored.restored);
        assert_eq!(restored.current_version, 1);

        let got = svc
            .get_entry(Request::new(GetEntryRequest {
                title: put.title.clone(),
                version: None,
                audit: None,
            }))
            .await
            .unwrap()
            .into_inner();
        assert_eq!(got.entry.unwrap().password.as_deref(), Some("s3cret-key"),);

        // Kernel cross-check: entries under unified path.
        let entries = kernel.sys_readdir(
            "/vault/entries/passwords",
            "root",
            true,
            kernel::kernel::syscall::ReaddirOpts::default(),
        );
        assert_eq!(entries.len(), 1);
        assert!(entries[0].0.contains("aws-prod"));
    }

    // ── Scenario 3: TOTP survives password rotation ─────────────

    #[tokio::test]
    async fn totp_survives_password_rotation() {
        let (_kernel, svc) = kernel_service();

        let mut e = entry("aws", "original-pw");
        e.totp_secret = Some("JBSWY3DPEHPK3PXP".into());
        let put1 = svc
            .put_entry(Request::new(PutEntryRequest {
                entry: Some(e),
                audit: None,
            }))
            .await
            .unwrap()
            .into_inner();
        assert_eq!(put1.version, 1);

        let totp1 = svc
            .generate_totp(Request::new(GenerateTotpRequest {
                title: put1.title.clone(),
                audit: None,
            }))
            .await
            .unwrap()
            .into_inner();
        assert_eq!(totp1.code.len(), 6);
        assert!(totp1.code.chars().all(|c| c.is_ascii_digit()));

        let mut e2 = entry(&put1.title, "rotated-pw");
        e2.totp_secret = Some("JBSWY3DPEHPK3PXP".into());
        let put2 = svc
            .put_entry(Request::new(PutEntryRequest {
                entry: Some(e2),
                audit: None,
            }))
            .await
            .unwrap()
            .into_inner();
        assert_eq!(put2.version, 2);

        let totp2 = svc
            .generate_totp(Request::new(GenerateTotpRequest {
                title: put1.title.clone(),
                audit: None,
            }))
            .await
            .unwrap()
            .into_inner();
        assert_eq!(totp2.code.len(), 6);
        assert_eq!(totp1.code, totp2.code);

        let got = svc
            .get_entry(Request::new(GetEntryRequest {
                title: put1.title.clone(),
                version: None,
                audit: None,
            }))
            .await
            .unwrap()
            .into_inner();
        assert_eq!(
            got.entry.as_ref().unwrap().password.as_deref(),
            Some("rotated-pw")
        );
        assert!(got.entry.unwrap().totp_secret.is_none());
    }

    // ── Scenario 4: Multi-credential search and cleanup ──────────

    #[tokio::test]
    async fn multi_credential_search_and_cleanup() {
        let (kernel, svc) = kernel_service();

        let mut titles = Vec::new();
        for (t, p) in [("gmail", "pw1"), ("github", "pw2"), ("aws-prod", "pw3")] {
            let r = svc
                .put_entry(Request::new(PutEntryRequest {
                    entry: Some(entry(t, p)),
                    audit: None,
                }))
                .await
                .unwrap()
                .into_inner();
            titles.push(r.title);
        }
        assert_eq!(titles.len(), 3);

        let filtered = svc
            .list_entries(Request::new(ListEntriesRequest {
                query: "git".into(),
                limit: 0,
                audit: None,
            }))
            .await
            .unwrap()
            .into_inner();
        assert_eq!(filtered.matched, 1);
        let matched_title = &filtered.entries[0].title;
        assert_eq!(matched_title, "github");

        svc.delete_entry(Request::new(DeleteEntryRequest {
            title: matched_title.clone(),
            audit: None,
        }))
        .await
        .unwrap();

        let after = svc
            .list_entries(Request::new(ListEntriesRequest {
                query: String::new(),
                limit: 0,
                audit: None,
            }))
            .await
            .unwrap()
            .into_inner();
        assert_eq!(after.total_in_vault, 2);
        let remaining: Vec<&str> = after.entries.iter().map(|e| e.title.as_str()).collect();
        assert!(!remaining.contains(&"github"));

        // Kernel cross-check: 3 index files in passwords namespace
        // (soft-delete = tombstone, not removal).
        let vfs_entries = kernel.sys_readdir(
            "/vault/entries/passwords",
            "root",
            true,
            kernel::kernel::syscall::ReaddirOpts::default(),
        );
        assert_eq!(vfs_entries.len(), 3);
    }
}
