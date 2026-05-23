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

use self::types::{
    now_unix_ms, EntryIndex, PasswordVaultError, StoredEntry, VaultEntryPlaintext,
};

/// RFC 6238 default: 30-second window.
const TOTP_PERIOD_SECONDS: u64 = 30;

/// Cache key for TOTP oracle de-duplication. `(title, window_index)`
/// — single-subject vault, so no subject_id dimension yet. Same code
/// returned for repeated calls within the same 30s window.
type TotpCacheKey = (String, u64);

/// Compute a 6-digit TOTP code per RFC 6238 (HMAC-SHA1, 30s window).
/// `secret_b32` is the user-supplied seed, base32-encoded (RFC 4648,
/// no padding — pyotp convention; case-insensitive). `time_seconds`
/// is the wall-clock at code time.
///
/// Extracted as a free function (not a method) so tests can pass
/// fixed timestamps and verify against RFC 6238 vectors.
fn compute_totp(secret_b32: &str, time_seconds: u64) -> Result<String, PasswordVaultError> {
    use hmac::{Hmac, Mac};
    use sha1::Sha1;

    // RFC 4648 base32 alphabet is uppercase; pyotp accepts mixed-case
    // by normalising first. We match that for user-friendly inputs.
    // Strip whitespace too — TOTP QR-code outputs sometimes group
    // digits with spaces.
    let normalised: String = secret_b32
        .chars()
        .filter(|c| !c.is_whitespace())
        .flat_map(char::to_uppercase)
        .collect();
    let key = base32::decode(
        base32::Alphabet::Rfc4648 { padding: false },
        &normalised,
    )
    .ok_or_else(|| PasswordVaultError::Invalid("totp_secret is not valid base32".into()))?;
    if key.is_empty() {
        return Err(PasswordVaultError::Invalid("totp_secret decoded to empty bytes".into()));
    }

    let window = time_seconds / TOTP_PERIOD_SECONDS;
    let counter_bytes = window.to_be_bytes();

    type HmacSha1 = Hmac<Sha1>;
    let mut mac = HmacSha1::new_from_slice(&key).map_err(|_| PasswordVaultError::Crypto)?;
    mac.update(&counter_bytes);
    let hmac_result = mac.finalize().into_bytes();

    // RFC 4226 dynamic truncation: low 4 bits of last byte point to
    // a 4-byte slice; mask top bit; mod 10^6 for 6 digits.
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
    storage: storage::Storage,
    master_key: crypto::MasterKey,
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
        req: Request<ListEntriesRequest>,
    ) -> Result<Response<ListEntriesResponse>, Status> {
        let req = req.into_inner();
        // Snapshot all live indexes. Soft-deleted titles are excluded
        // (Python's include_deleted=False default — surface them via
        // the dedicated 'show tombstones' tool when that lands).
        let all = self.inner.storage.list_indexes()?;
        let live: Vec<(String, EntryIndex)> = all
            .into_iter()
            .filter(|(_, idx)| idx.deleted_at_ms.is_none())
            .collect();
        let total_live = live.len() as i32;

        let query_lower = req.query.to_lowercase();
        let want_filter = !query_lower.is_empty();
        let mut matched = Vec::new();
        for (title, idx) in live {
            let stored = match self
                .inner
                .storage
                .get_version(&title, idx.current_version)?
            {
                Some(s) => s,
                None => continue, // index points at a missing version — skip silently (corruption tracker should pick this up)
            };
            let plain_bytes = crypto::open(&stored.nonce, &stored.ciphertext, &self.inner.master_key)?;
            let plain: VaultEntryPlaintext = match bincode::deserialize(&plain_bytes) {
                Ok(p) => p,
                Err(_) => continue,
            };
            if want_filter {
                // Case-insensitive substring filter over the four
                // searchable fields. Matches Python's behaviour at
                // password_agent/vault.py:71-81.
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

        // limit=0 → no limit.
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
        let idx = self
            .inner
            .storage
            .get_index(&req.title)?
            .ok_or_else(|| PasswordVaultError::NotFound(req.title.clone()))?;
        // Idempotent: deleting an already-deleted entry is a no-op
        // success, not an error. Matches REST DELETE semantics.
        let new_idx = EntryIndex {
            current_version: idx.current_version,
            deleted_at_ms: Some(idx.deleted_at_ms.unwrap_or_else(now_unix_ms)),
        };
        self.inner.storage.set_index(&req.title, &new_idx)?;
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
        let idx = self
            .inner
            .storage
            .get_index(&req.title)?
            .ok_or_else(|| PasswordVaultError::NotFound(req.title.clone()))?;
        // Idempotent: restoring a live entry is a no-op success.
        let new_idx = EntryIndex {
            current_version: idx.current_version,
            deleted_at_ms: None,
        };
        self.inner.storage.set_index(&req.title, &new_idx)?;
        Ok(Response::new(RestoreEntryResponse {
            title: req.title,
            restored: true,
            current_version: idx.current_version as i32,
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
        let idx = self
            .inner
            .storage
            .get_index(&req.title)?
            .ok_or_else(|| PasswordVaultError::NotFound(req.title.clone()))?;
        let stored = self.inner.storage.list_versions(&req.title)?;
        let active = idx.current_version;
        let is_deleted = idx.deleted_at_ms.is_some();
        // Per proto: tombstoned=true marks "the version that was active
        // when the entry was soft-deleted". For a live entry, no version
        // is tombstoned. For a soft-deleted entry, only the latest
        // (active) version carries the marker.
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

        // Resolve to current version. Soft-deleted entries can't TOTP.
        let idx = self
            .inner
            .storage
            .get_index(&req.title)?
            .ok_or_else(|| PasswordVaultError::NotFound(req.title.clone()))?;
        if idx.deleted_at_ms.is_some() {
            return Err(PasswordVaultError::NotFound(req.title).into());
        }
        let stored = self
            .inner
            .storage
            .get_version(&req.title, idx.current_version)?
            .ok_or_else(|| PasswordVaultError::NotFound(req.title.clone()))?;

        let plain_bytes = crypto::open(&stored.nonce, &stored.ciphertext, &self.inner.master_key)?;
        let plain: VaultEntryPlaintext = bincode::deserialize(&plain_bytes)
            .map_err(|_| PasswordVaultError::Crypto)?;

        if plain.totp_secret.is_empty() {
            return Err(PasswordVaultError::TotpNotConfigured(req.title).into());
        }

        let now_secs = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_secs())
            .unwrap_or(0);
        let window = now_secs / TOTP_PERIOD_SECONDS;
        let cache_key = (req.title.clone(), window);

        // Hold the cache lock for both check + insert + prune. Locks
        // are uncontended in single-user workloads; for high QPS we'd
        // split into per-shard locks later.
        let code = {
            let mut cache = self.inner.totp_cache.lock();
            if let Some(cached) = cache.get(&cache_key) {
                cached.clone()
            } else {
                let computed = compute_totp(&plain.totp_secret, now_secs)?;
                cache.insert(cache_key.clone(), computed.clone());
                // Drop entries from past windows so the map doesn't
                // grow unbounded over long server lifetimes.
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
                query: "git".into(), // matches "GitHub"
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
        // matched counts BEFORE limit truncation (per proto comment:
        // 'matched' is post-filter, pre-limit).
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
        assert_eq!(r.total_in_vault, 1); // only "b"
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
        // Latest read after soft-delete: NotFound.
        let err = svc
            .get_entry(Request::new(GetEntryRequest {
                title: "a".into(),
                version: None,
                audit: None,
            }))
            .await
            .unwrap_err();
        assert_eq!(err.code(), tonic::Code::NotFound);
        // But explicit historical version still works (rotation auditors).
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
        // GetEntry latest now works again.
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
        // Documented PutEntry behaviour: writing a new version implicitly
        // clears any tombstone. Sanity-check it works end-to-end.
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
        // Live entry — no tombstoned versions.
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
        // Only the currently-active version (v=2) is marked tombstoned.
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

    /// RFC 6238 Appendix B test vectors, HMAC-SHA1 variant.
    /// Seed is ASCII "12345678901234567890" → base32 (no padding) =
    /// "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ".
    const RFC_SEED_B32: &str = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ";

    #[test]
    fn compute_totp_matches_rfc6238_vectors() {
        // T = 59         → 94287082, low 6 digits = 287082
        assert_eq!(compute_totp(RFC_SEED_B32, 59).unwrap(), "287082");
        // T = 1111111109 → 07081804, low 6 digits = 081804
        assert_eq!(compute_totp(RFC_SEED_B32, 1_111_111_109).unwrap(), "081804");
        // T = 1234567890 → 89005924, low 6 digits = 005924
        assert_eq!(compute_totp(RFC_SEED_B32, 1_234_567_890).unwrap(), "005924");
    }

    #[test]
    fn compute_totp_lowercase_base32_works() {
        // pyotp accepts lowercase seeds; we should too (RFC 4648 is
        // case-insensitive).
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
            entry: Some(entry("aws", "pw")), // entry() leaves totp_secret=None
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
        // Within one 30s window, repeated calls return the cached code.
        // We can't easily force a window boundary in a sync test, but
        // back-to-back calls reliably stay in the same window unless
        // the test is run exactly at a boundary — accept that 1-in-30s
        // flakiness floor for now (real fix would be a clock trait).
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
        // Second delete: still success, no error.
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
}
