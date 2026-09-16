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
//! Attachments: an entry version records attachment metadata only; the
//! bytes are sealed into content-addressed blobs
//! (`GenericSecretsServiceImpl::do_put_blob`), so ListEntries never
//! decrypts files and unchanged attachments cost nothing on re-put.
//! `GetAttachment` is the only RPC that returns bytes.
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

use std::collections::{HashMap, HashSet};
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use parking_lot::Mutex;
use tonic::{Request, Response, Status};

use proto::password_vault_service_server::{PasswordVaultService, PasswordVaultServiceServer};
use proto::{
    Attachment as ProtoAttachment, DeleteEntryRequest, DeleteEntryResponse, GenerateTotpRequest,
    GenerateTotpResponse, GetAttachmentRequest, GetAttachmentResponse, GetEntryRequest,
    GetEntryResponse, ListEntriesRequest, ListEntriesResponse, ListVersionsRequest,
    ListVersionsResponse, PutEntryRequest, PutEntryResponse, RestoreEntryRequest,
    RestoreEntryResponse, VaultEntry as ProtoVaultEntry,
};

use self::types::{AttachmentMeta, VaultEntryPlaintext};
use crate::generic_secrets::GenericSecretsServiceImpl;

/// The namespace under which all password-vault entries are stored.
const PASSWORDS_NAMESPACE: &str = "passwords";

/// Largest single attachment `PutEntry` accepts.
pub const MAX_ATTACHMENT_BYTES: usize = 2 * 1024 * 1024;

/// Largest combined size of the attachments one entry version may hold.
pub const MAX_ENTRY_ATTACHMENT_BYTES: usize = 8 * 1024 * 1024;

/// Most attachments one entry version may hold.
pub const MAX_ATTACHMENTS_PER_ENTRY: usize = 32;

/// Longest attachment filename or content_type, in bytes.
const MAX_ATTACHMENT_LABEL_BYTES: usize = 255;

/// Inbound gRPC message cap for hosts of this service: a `PutEntry`
/// uploading `MAX_ENTRY_ATTACHMENT_BYTES`, plus 1 MiB for the text fields
/// and framing. tonic's 4 MiB default would reject a legal upload before
/// validation runs. Applied by [`PasswordVaultServiceImpl::into_server`].
pub const MAX_REQUEST_BYTES: usize = MAX_ENTRY_ATTACHMENT_BYTES + 1024 * 1024;

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

    /// The tonic server for this service, with the inbound message cap
    /// attachment uploads need ([`MAX_REQUEST_BYTES`]). Hosts use this
    /// rather than `PasswordVaultServiceServer::new` so the limit has a
    /// single source.
    pub fn into_server(self) -> PasswordVaultServiceServer<Self> {
        PasswordVaultServiceServer::new(self).max_decoding_message_size(MAX_REQUEST_BYTES)
    }

    /// Validate a `PutEntry`'s attachments, store any uploaded bytes, and
    /// return the metadata to record in the new version. Nothing is
    /// written unless the whole list is valid.
    fn store_attachments(
        &self,
        attachments: Vec<ProtoAttachment>,
    ) -> Result<Vec<AttachmentMeta>, Status> {
        if attachments.len() > MAX_ATTACHMENTS_PER_ENTRY {
            return Err(Status::invalid_argument(format!(
                "at most {MAX_ATTACHMENTS_PER_ENTRY} attachments per entry, got {}",
                attachments.len()
            )));
        }
        let mut filenames = HashSet::new();
        let mut total_bytes = 0usize;
        let mut metas = Vec::with_capacity(attachments.len());
        let mut uploads = Vec::new();

        for a in attachments {
            validate_attachment_filename(&a.filename)?;
            if !filenames.insert(a.filename.clone()) {
                return Err(Status::invalid_argument(format!(
                    "duplicate attachment filename {:?}",
                    a.filename
                )));
            }
            if a.content_type.len() > MAX_ATTACHMENT_LABEL_BYTES {
                return Err(Status::invalid_argument(format!(
                    "attachment {:?}: content_type longer than {MAX_ATTACHMENT_LABEL_BYTES} bytes",
                    a.filename
                )));
            }
            let claimed_sha256 = a.sha256.to_ascii_lowercase();

            let (sha256, size) = if a.data.is_empty() {
                // Keep an attachment the vault already stores.
                if claimed_sha256.is_empty() {
                    return Err(Status::invalid_argument(format!(
                        "attachment {:?}: data is empty and sha256 is unset — upload \
                         non-empty data, or set sha256 to keep an attachment the vault \
                         already stores",
                        a.filename
                    )));
                }
                let stored = self
                    .inner
                    .secrets
                    .do_get_blob(PASSWORDS_NAMESPACE, &claimed_sha256)?
                    .ok_or_else(|| {
                        Status::invalid_argument(format!(
                            "attachment {:?}: the vault stores no attachment with sha256 \
                             {claimed_sha256}; upload its data",
                            a.filename
                        ))
                    })?;
                (claimed_sha256, stored.len())
            } else {
                if a.data.len() > MAX_ATTACHMENT_BYTES {
                    return Err(Status::invalid_argument(format!(
                        "attachment {:?}: {} bytes exceeds the {MAX_ATTACHMENT_BYTES}-byte limit",
                        a.filename,
                        a.data.len()
                    )));
                }
                let digest = crypto::sha256_hex(&a.data);
                if !claimed_sha256.is_empty() && claimed_sha256 != digest {
                    return Err(Status::invalid_argument(format!(
                        "attachment {:?}: sha256 {claimed_sha256} does not match data ({digest})",
                        a.filename
                    )));
                }
                let size = a.data.len();
                uploads.push((digest.clone(), a.data));
                (digest, size)
            };

            total_bytes += size;
            if total_bytes > MAX_ENTRY_ATTACHMENT_BYTES {
                return Err(Status::invalid_argument(format!(
                    "attachments exceed the {MAX_ENTRY_ATTACHMENT_BYTES}-byte per-entry limit"
                )));
            }
            metas.push(AttachmentMeta {
                filename: a.filename,
                content_type: a.content_type,
                size_bytes: size as u64,
                sha256,
            });
        }

        for (sha256, data) in uploads {
            self.inner
                .secrets
                .do_put_blob(PASSWORDS_NAMESPACE, &sha256, &data)?;
        }
        Ok(metas)
    }
}

/// Filenames are lookup keys and become file names when clients save
/// attachments, so reject anything that could act as a path.
fn validate_attachment_filename(name: &str) -> Result<(), Status> {
    let valid = !name.is_empty()
        && name.len() <= MAX_ATTACHMENT_LABEL_BYTES
        && name != "."
        && name != ".."
        && !name
            .chars()
            .any(|c| c == '/' || c == '\\' || c.is_control());
    if valid {
        Ok(())
    } else {
        Err(Status::invalid_argument(format!(
            "attachment filename {name:?} must be 1-{MAX_ATTACHMENT_LABEL_BYTES} bytes with no \
             path separators or control characters"
        )))
    }
}

// ---------------------------------------------------------------------
// Conversion helpers — proto <-> internal types.
// ---------------------------------------------------------------------

/// `attachments` is left empty: resolving them needs storage, so
/// `put_entry` fills it from `store_attachments`.
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
        attachments: Vec::new(),
    }
}

/// `plaintext_to_proto`: always redacts `totp_secret` (security invariant)
/// and carries attachment metadata without bytes.
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
        attachments: p
            .attachments
            .into_iter()
            .map(|a| ProtoAttachment {
                filename: a.filename,
                content_type: a.content_type,
                data: Vec::new(),
                size_bytes: a.size_bytes,
                sha256: a.sha256,
            })
            .collect(),
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
        let mut entry = req
            .entry
            .ok_or_else(|| Status::invalid_argument("entry field is required"))?;
        if entry.title.is_empty() {
            return Err(Status::invalid_argument(
                "entry.title is required (non-empty)",
            ));
        }
        let title = entry.title.clone();

        let attachments = self.store_attachments(std::mem::take(&mut entry.attachments))?;
        let plain = VaultEntryPlaintext {
            attachments,
            ..proto_to_plaintext(entry)
        };
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

    async fn get_attachment(
        &self,
        req: Request<GetAttachmentRequest>,
    ) -> Result<Response<GetAttachmentResponse>, Status> {
        let req = req.into_inner();
        if req.title.is_empty() || req.filename.is_empty() {
            return Err(Status::invalid_argument("title and filename are required"));
        }

        let (json_str, version) =
            self.inner
                .secrets
                .do_get(PASSWORDS_NAMESPACE, &req.title, req.version)?;
        let meta = deserialize_plaintext(&json_str)?
            .attachments
            .into_iter()
            .find(|a| a.filename == req.filename)
            .ok_or_else(|| PasswordVaultError::AttachmentNotFound {
                title: req.title.clone(),
                filename: req.filename.clone(),
            })?;
        let data = self
            .inner
            .secrets
            .do_get_blob(PASSWORDS_NAMESPACE, &meta.sha256)?
            .ok_or_else(|| {
                PasswordVaultError::Storage(format!(
                    "attachment {:?} on {:?} v{version}: blob missing",
                    req.filename, req.title
                ))
            })?;

        Ok(Response::new(GetAttachmentResponse {
            attachment: Some(ProtoAttachment {
                filename: meta.filename,
                content_type: meta.content_type,
                data,
                size_bytes: meta.size_bytes,
                sha256: meta.sha256,
            }),
            version,
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
            attachments: vec![],
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

    // -----------------------------------------------------------------
    // Attachments
    // -----------------------------------------------------------------

    const QR_PNG: &[u8] = b"\x89PNG\r\n\x1a\n fake qr payload";

    fn attachment(filename: &str, data: &[u8]) -> ProtoAttachment {
        ProtoAttachment {
            filename: filename.into(),
            content_type: "image/png".into(),
            data: data.to_vec(),
            size_bytes: 0,
            sha256: String::new(),
        }
    }

    fn with_attachments(title: &str, attachments: Vec<ProtoAttachment>) -> ProtoVaultEntry {
        ProtoVaultEntry {
            attachments,
            ..entry(title, "pw")
        }
    }

    async fn put(
        svc: &PasswordVaultServiceImpl,
        e: ProtoVaultEntry,
    ) -> Result<PutEntryResponse, Status> {
        svc.put_entry(Request::new(PutEntryRequest {
            entry: Some(e),
            audit: None,
        }))
        .await
        .map(Response::into_inner)
    }

    async fn get(svc: &PasswordVaultServiceImpl, title: &str) -> ProtoVaultEntry {
        svc.get_entry(Request::new(GetEntryRequest {
            title: title.into(),
            version: None,
            audit: None,
        }))
        .await
        .unwrap()
        .into_inner()
        .entry
        .unwrap()
    }

    async fn fetch_attachment(
        svc: &PasswordVaultServiceImpl,
        title: &str,
        filename: &str,
        version: Option<i32>,
    ) -> Result<GetAttachmentResponse, Status> {
        svc.get_attachment(Request::new(GetAttachmentRequest {
            title: title.into(),
            filename: filename.into(),
            version,
            audit: None,
        }))
        .await
        .map(Response::into_inner)
    }

    #[tokio::test]
    async fn attachment_bytes_only_come_from_get_attachment() {
        let (_d, svc) = fresh_service();
        put(
            &svc,
            with_attachments("bank", vec![attachment("qr.png", QR_PNG)]),
        )
        .await
        .unwrap();

        let got = get(&svc, "bank").await;
        assert_eq!(got.attachments.len(), 1);
        let meta = &got.attachments[0];
        assert!(meta.data.is_empty());
        assert_eq!(meta.filename, "qr.png");
        assert_eq!(meta.content_type, "image/png");
        assert_eq!(meta.size_bytes, QR_PNG.len() as u64);
        assert_eq!(meta.sha256, crypto::sha256_hex(QR_PNG));

        let listed = svc
            .list_entries(Request::new(ListEntriesRequest {
                query: String::new(),
                limit: 0,
                audit: None,
            }))
            .await
            .unwrap()
            .into_inner();
        assert!(listed.entries[0].attachments[0].data.is_empty());
        assert_eq!(listed.entries[0].attachments[0].sha256, meta.sha256);

        let resp = fetch_attachment(&svc, "bank", "qr.png", None)
            .await
            .unwrap();
        assert_eq!(resp.version, 1);
        let full = resp.attachment.unwrap();
        assert_eq!(full.data, QR_PNG);
        assert_eq!(full.sha256, meta.sha256);
        assert_eq!(full.size_bytes, meta.size_bytes);
    }

    #[tokio::test]
    async fn read_modify_write_keeps_attachment_and_history_resolves_it() {
        let (_d, svc) = fresh_service();
        put(
            &svc,
            with_attachments("bank", vec![attachment("qr.png", QR_PNG)]),
        )
        .await
        .unwrap();

        // Pass back the metadata GetEntry returned (no bytes) with a new password.
        let mut e = get(&svc, "bank").await;
        e.password = Some("rotated".into());
        assert_eq!(put(&svc, e).await.unwrap().version, 2);
        let v2 = fetch_attachment(&svc, "bank", "qr.png", None)
            .await
            .unwrap();
        assert_eq!(v2.version, 2);
        assert_eq!(v2.attachment.unwrap().data, QR_PNG);

        // Detach in v3: latest no longer has it, v1 still does.
        let mut e = get(&svc, "bank").await;
        e.attachments.clear();
        assert_eq!(put(&svc, e).await.unwrap().version, 3);
        let err = fetch_attachment(&svc, "bank", "qr.png", None)
            .await
            .unwrap_err();
        assert_eq!(err.code(), tonic::Code::NotFound);
        let v1 = fetch_attachment(&svc, "bank", "qr.png", Some(1))
            .await
            .unwrap();
        assert_eq!(v1.attachment.unwrap().data, QR_PNG);
    }

    #[tokio::test]
    async fn reference_must_name_a_stored_attachment() {
        let (_d, svc) = fresh_service();
        let unstored = ProtoAttachment {
            sha256: crypto::sha256_hex(b"never uploaded"),
            ..attachment("qr.png", b"")
        };
        let err = put(&svc, with_attachments("bank", vec![unstored]))
            .await
            .unwrap_err();
        assert_eq!(err.code(), tonic::Code::InvalidArgument);

        let err = put(
            &svc,
            with_attachments("bank", vec![attachment("qr.png", b"")]),
        )
        .await
        .unwrap_err();
        assert_eq!(err.code(), tonic::Code::InvalidArgument);

        // Rejected puts create no version.
        let err = svc
            .get_entry(Request::new(GetEntryRequest {
                title: "bank".into(),
                version: None,
                audit: None,
            }))
            .await
            .unwrap_err();
        assert_eq!(err.code(), tonic::Code::NotFound);
    }

    #[tokio::test]
    async fn supplied_sha256_must_match_data() {
        let (_d, svc) = fresh_service();
        let wrong = ProtoAttachment {
            sha256: crypto::sha256_hex(b"other bytes"),
            ..attachment("qr.png", QR_PNG)
        };
        let err = put(&svc, with_attachments("bank", vec![wrong]))
            .await
            .unwrap_err();
        assert_eq!(err.code(), tonic::Code::InvalidArgument);

        // Digest comparison is case-insensitive; stored form is lowercase.
        let upper = ProtoAttachment {
            sha256: crypto::sha256_hex(QR_PNG).to_uppercase(),
            ..attachment("qr.png", QR_PNG)
        };
        put(&svc, with_attachments("bank", vec![upper]))
            .await
            .unwrap();
        assert_eq!(
            get(&svc, "bank").await.attachments[0].sha256,
            crypto::sha256_hex(QR_PNG)
        );
    }

    #[tokio::test]
    async fn attachment_size_and_count_limits_are_enforced() {
        let (_d, svc) = fresh_service();

        let at_limit = vec![7u8; MAX_ATTACHMENT_BYTES];
        put(
            &svc,
            with_attachments("ok", vec![attachment("max.bin", &at_limit)]),
        )
        .await
        .unwrap();

        let over = vec![7u8; MAX_ATTACHMENT_BYTES + 1];
        let err = put(
            &svc,
            with_attachments("big", vec![attachment("big.bin", &over)]),
        )
        .await
        .unwrap_err();
        assert_eq!(err.code(), tonic::Code::InvalidArgument);

        // Five max-size files: each legal, together past the per-entry total.
        let files: Vec<ProtoAttachment> = (0u8..5)
            .map(|i| attachment(&format!("f{i}.bin"), &vec![i; MAX_ATTACHMENT_BYTES]))
            .collect();
        let err = put(&svc, with_attachments("total", files))
            .await
            .unwrap_err();
        assert_eq!(err.code(), tonic::Code::InvalidArgument);

        let many: Vec<ProtoAttachment> = (0..=MAX_ATTACHMENTS_PER_ENTRY)
            .map(|i| attachment(&format!("f{i}.bin"), &[i as u8]))
            .collect();
        let err = put(&svc, with_attachments("many", many)).await.unwrap_err();
        assert_eq!(err.code(), tonic::Code::InvalidArgument);
    }

    #[tokio::test]
    async fn invalid_or_duplicate_filenames_are_rejected() {
        let (_d, svc) = fresh_service();
        let too_long = "x".repeat(MAX_ATTACHMENT_LABEL_BYTES + 1);
        for name in ["", ".", "..", "a/b", "a\\b", "tab\tname", too_long.as_str()] {
            let err = put(
                &svc,
                with_attachments("bank", vec![attachment(name, QR_PNG)]),
            )
            .await
            .unwrap_err();
            assert_eq!(
                err.code(),
                tonic::Code::InvalidArgument,
                "filename {name:?}"
            );
        }
        let dup = vec![attachment("qr.png", QR_PNG), attachment("qr.png", b"other")];
        let err = put(&svc, with_attachments("bank", dup)).await.unwrap_err();
        assert_eq!(err.code(), tonic::Code::InvalidArgument);
    }

    #[tokio::test]
    async fn get_attachment_unknown_filename_is_not_found() {
        let (_d, svc) = fresh_service();
        put(
            &svc,
            with_attachments("bank", vec![attachment("qr.png", QR_PNG)]),
        )
        .await
        .unwrap();
        let err = fetch_attachment(&svc, "bank", "missing.png", None)
            .await
            .unwrap_err();
        assert_eq!(err.code(), tonic::Code::NotFound);
    }

    #[test]
    fn attachment_free_entries_keep_the_pre_attachment_json_format() {
        // Entries written before attachments existed still decode...
        let old = r#"{"title":"t","username":"","password":"p","url":"","notes":"","tags":"","totp_secret":"","extra_json":""}"#;
        let plain = deserialize_plaintext(old).unwrap();
        assert!(plain.attachments.is_empty());
        // ...and attachment-free entries re-encode byte-identically, so older
        // binaries sharing a synced data dir keep reading them.
        assert_eq!(serialize_plaintext(&plain).unwrap(), old);
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
            attachments: vec![],
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

    // ── Scenario 4: Attachment blobs on disk ─────────────────────

    fn attached(title: &str, filename: &str, data: &[u8]) -> PutEntryRequest {
        PutEntryRequest {
            entry: Some(ProtoVaultEntry {
                attachments: vec![ProtoAttachment {
                    filename: filename.into(),
                    content_type: "image/png".into(),
                    data: data.to_vec(),
                    size_bytes: 0,
                    sha256: String::new(),
                }],
                ..entry(title, "pw")
            }),
            audit: None,
        }
    }

    fn blob_paths(kernel: &Kernel) -> Vec<String> {
        kernel
            .sys_readdir(
                "/vault/blobs/passwords",
                "root",
                true,
                kernel::kernel::syscall::ReaddirOpts::default(),
            )
            .into_iter()
            .map(|(path, _)| path)
            .collect()
    }

    #[tokio::test]
    async fn identical_attachments_share_one_blob_not_named_by_digest() {
        let (kernel, svc) = kernel_service();
        let qr = b"same qr bytes";
        for title in ["bank-a", "bank-b", "bank-a"] {
            svc.put_entry(Request::new(attached(title, "qr.png", qr)))
                .await
                .unwrap();
        }
        let blobs = blob_paths(&kernel);
        assert_eq!(blobs.len(), 1);
        assert!(!blobs[0].contains(&crypto::sha256_hex(qr)));
    }

    #[tokio::test]
    async fn swapped_blob_file_fails_the_integrity_check() {
        let (kernel, svc) = kernel_service();
        let ctx = OperationContext::new("test", "root", true, None, true);
        svc.put_entry(Request::new(attached("a", "a.png", b"bytes of a")))
            .await
            .unwrap();
        svc.put_entry(Request::new(attached("b", "b.png", b"bytes of b")))
            .await
            .unwrap();

        // Overwrite one blob file with the other's (validly sealed) contents.
        let blobs = blob_paths(&kernel);
        assert_eq!(blobs.len(), 2);
        let donor = KernelConvenience::read(&*kernel, &blobs[0], &ctx, 0, 0)
            .unwrap()
            .data
            .unwrap();
        KernelConvenience::write(&*kernel, &blobs[1], &ctx, &donor, 0).unwrap();

        let mut outcomes = Vec::new();
        for (title, filename) in [("a", "a.png"), ("b", "b.png")] {
            outcomes.push(
                svc.get_attachment(Request::new(GetAttachmentRequest {
                    title: title.into(),
                    filename: filename.into(),
                    version: None,
                    audit: None,
                }))
                .await
                .map(|_| ())
                .map_err(|s| s.code()),
            );
        }
        outcomes.sort_by_key(|o| o.is_err());
        assert_eq!(outcomes, vec![Ok(()), Err(tonic::Code::Internal)]);
    }

    // ── Scenario 5: Multi-credential search and cleanup ──────────

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
