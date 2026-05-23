//! `PasswordVaultService` — gRPC service for the password vault
//! (namespace="passwords"). Domain wrapper above `SecretsService` that
//! provides server-side TOTP, audit-tagged access, and the canonical
//! VaultEntry schema (title/username/password/url/notes/tags/...).
//!
//! Per #3923 integration doc, this is the Phase 1 Rust impl. Per the
//! `services` ⊥ `backends` ⊥ `transport` ⊥ `raft` invariant, this
//! module depends ONLY on `kernel` + `contracts` (transitively); storage
//! is a local redb file owned by the service binary, not a `backends`
//! crate import.
//!
//! Server-side TOTP is the security invariant the rewrite preserves:
//! the totp_secret never leaves the server — `GetEntry` always redacts
//! it, and clients call `GenerateTotp` to get a current code.
//!
//! Hosted by the `vault` profile (`rust/profiles/vault/`), NOT bundled
//! into `cluster` — keeps cluster pure-federation per its slim-binary
//! design goal.

pub mod proto {
    //! Generated tonic stubs from
    //! `proto/nexus/password_vault/v1/password_vault.proto`.
    tonic::include_proto!("nexus.password_vault.v1");
}

mod types;
mod crypto;
mod storage;

use std::collections::HashMap;
use std::path::Path;
use std::sync::Arc;

use parking_lot::Mutex;
use tonic::{Request, Response, Status};

use proto::password_vault_service_server::PasswordVaultService;
use proto::{
    DeleteEntryRequest, DeleteEntryResponse, GenerateTotpRequest, GenerateTotpResponse,
    GetEntryRequest, GetEntryResponse, ListEntriesRequest, ListEntriesResponse,
    ListVersionsRequest, ListVersionsResponse, PutEntryRequest, PutEntryResponse,
    RestoreEntryRequest, RestoreEntryResponse, VaultEntry as ProtoVaultEntry,
};

use self::types::{
    now_unix_ms, EntryIndex, PasswordVaultError, StoredEntry, VaultEntryPlaintext,
};

/// Cache key for TOTP oracle de-duplication. `(title, window_index)`
/// — single-subject vault, so no subject_id dimension yet. Same code
/// returned for repeated calls within the same 30s window.
type TotpCacheKey = (String, u64);

/// Service state. Wrapped in `Arc` so the tonic-required `Clone`
/// impl on `PasswordVaultServiceImpl` is cheap.
struct Inner {
    storage: storage::Storage,
    master_key: crypto::MasterKey,
    #[allow(dead_code)] // wired in T34.7 (GenerateTotp)
    totp_cache: Mutex<HashMap<TotpCacheKey, String>>,
}

/// Tonic-facing service. Cloneable (cheap: just bumps the Arc).
#[derive(Clone)]
pub struct PasswordVaultServiceImpl {
    inner: Arc<Inner>,
}

impl PasswordVaultServiceImpl {
    /// Open or create a vault at `data_dir/vault.redb`, with the master
    /// key at `master_key_path` (32 bytes, generated + persisted on
    /// first call). Both files are atomically created if absent.
    pub fn new(
        data_dir: &Path,
        master_key_path: &Path,
    ) -> Result<Self, PasswordVaultError> {
        let storage = storage::Storage::open(&data_dir.join("vault.redb"))?;
        let master_key = crypto::load_or_create_master_key(master_key_path)?;
        Ok(Self {
            inner: Arc::new(Inner {
                storage,
                master_key,
                totp_cache: Mutex::new(HashMap::new()),
            }),
        })
    }
}

// ---------------------------------------------------------------------
// Conversion helpers — proto <-> internal types.
//
// Proto VaultEntry has all non-`title` fields as `optional string`
// (proto3 explicit presence). Internal plaintext uses plain `String`
// — we lose the "field unset vs explicitly cleared" distinction at
// the storage layer. That's intentional for now: vault entries are
// always full-replace (PutEntry creates a new version with the full
// payload), so partial-update semantics don't apply yet. If
// partial-update lands later (PATCH semantics), revisit.
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

/// `plaintext_to_proto`: always redacts `totp_secret` (per proto
/// contract — "totp_secret is always redacted in the response;
/// clients call GenerateTotp"). Other fields wrap into `Some(_)`
/// preserving empty strings; "field unset" semantics would require
/// us to track presence at storage layer, which we don't yet.
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

// ---------------------------------------------------------------------
// gRPC trait impl.
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

        // Encrypt the canonical plaintext form.
        let plain = proto_to_plaintext(entry);
        let plain_bytes = bincode::serialize(&plain)
            .map_err(|e| Status::internal(format!("serialise entry: {e}")))?;
        let (nonce, ciphertext) = crypto::seal(&plain_bytes, &self.inner.master_key)?;

        // Allocate next version. Soft-deleted titles get reanimated
        // (writing a new version implicitly clears the tombstone —
        // matches user intent of "put new value here").
        let current = self.inner.storage.get_index(&title)?;
        let next_version = current.as_ref().map_or(1, |idx| idx.current_version + 1);
        let created_at_ms = now_unix_ms();

        let stored = StoredEntry {
            version: next_version,
            created_at_ms,
            nonce,
            ciphertext,
        };
        self.inner
            .storage
            .put_version(&title, next_version, &stored)?;
        self.inner.storage.set_index(
            &title,
            &EntryIndex {
                current_version: next_version,
                deleted_at_ms: None,
            },
        )?;

        Ok(Response::new(PutEntryResponse {
            id: title.clone(),
            title,
            version: next_version as i32,
            created_at: Some(unix_ms_to_proto_ts(created_at_ms)),
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

        let idx = self
            .inner
            .storage
            .get_index(&req.title)?
            .ok_or_else(|| PasswordVaultError::NotFound(req.title.clone()))?;

        // version: None (proto default for `optional`) = latest. An
        // explicit Some(n) reads a specific historical version even
        // for soft-deleted titles (rotation auditors need this).
        let version_to_read = match req.version {
            None => {
                if idx.deleted_at_ms.is_some() {
                    return Err(PasswordVaultError::NotFound(req.title).into());
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
            .get_version(&req.title, version_to_read)?
            .ok_or_else(|| PasswordVaultError::NotFound(req.title.clone()))?;

        // Decrypt + deserialise plaintext.
        let plain_bytes = crypto::open(&stored.nonce, &stored.ciphertext, &self.inner.master_key)?;
        let plain: VaultEntryPlaintext = bincode::deserialize(&plain_bytes)
            .map_err(|_| PasswordVaultError::Crypto)?;

        Ok(Response::new(GetEntryResponse {
            entry: Some(plaintext_to_proto(plain)),
            version: stored.version as i32,
        }))
    }

    async fn list_entries(
        &self,
        _req: Request<ListEntriesRequest>,
    ) -> Result<Response<ListEntriesResponse>, Status> {
        Err(Status::unimplemented("ListEntries — lands in T34.5"))
    }

    async fn delete_entry(
        &self,
        _req: Request<DeleteEntryRequest>,
    ) -> Result<Response<DeleteEntryResponse>, Status> {
        Err(Status::unimplemented("DeleteEntry — lands in T34.5"))
    }

    async fn restore_entry(
        &self,
        _req: Request<RestoreEntryRequest>,
    ) -> Result<Response<RestoreEntryResponse>, Status> {
        Err(Status::unimplemented("RestoreEntry — lands in T34.5"))
    }

    async fn list_versions(
        &self,
        _req: Request<ListVersionsRequest>,
    ) -> Result<Response<ListVersionsResponse>, Status> {
        Err(Status::unimplemented("ListVersions — lands in T34.6"))
    }

    async fn generate_totp(
        &self,
        _req: Request<GenerateTotpRequest>,
    ) -> Result<Response<GenerateTotpResponse>, Status> {
        Err(Status::unimplemented("GenerateTotp — lands in T34.7"))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    fn fresh_service() -> (TempDir, PasswordVaultServiceImpl) {
        let dir = TempDir::new().unwrap();
        let svc = PasswordVaultServiceImpl::new(
            dir.path(),
            &dir.path().join("master.key"),
        )
        .unwrap();
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
        // Per proto contract — `totp_secret` is never returned by
        // GetEntry, regardless of caller. Clients use GenerateTotp.
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
}
