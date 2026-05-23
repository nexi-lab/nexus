//! Internal types for `password_vault`: storage representation, error
//! enum, and the proto ↔ internal conversion seam.
//!
//! Kept private to the service module: callers only see the
//! `PasswordVaultService` gRPC trait surface from the proto stubs.

use std::time::{SystemTime, UNIX_EPOCH};

/// On-disk row in the `versions` redb table. One row per (title, version)
/// pair. Only `version` and `created_at_ms` are plaintext — the entry
/// body lives encrypted in `nonce + ciphertext`.
///
/// `bincode`-serialised before being written; AES-GCM auth tag is
/// concatenated into `ciphertext` per the `aes-gcm` crate convention.
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub(crate) struct StoredEntry {
    pub version: u32,
    /// Unix milliseconds when this version was written.
    pub created_at_ms: u64,
    /// AES-GCM nonce (12 bytes per RFC 5116) — unique per write.
    pub nonce: [u8; 12],
    /// AES-256-GCM ciphertext of `bincode(VaultEntryPlaintext)`,
    /// with the 16-byte auth tag appended (aes-gcm crate convention).
    pub ciphertext: Vec<u8>,
}

/// On-disk row in the `entries` redb table — one per title. Tracks
/// which version is current and whether the title is soft-deleted.
/// `versions` table holds the actual encrypted bodies; this is the
/// per-title index that `ListEntries` iterates over.
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub(crate) struct EntryIndex {
    pub current_version: u32,
    /// Set when soft-deleted; cleared on Restore. None = live.
    pub deleted_at_ms: Option<u64>,
}

/// Plaintext form of a vault entry as serialised inside the AES-GCM
/// envelope. Mirrors the proto `VaultEntry` message fields 1:1; the
/// `extra_json` proto string is kept as-is (consumers parse JSON
/// themselves if they care).
#[derive(Debug, Clone, Default, serde::Serialize, serde::Deserialize)]
pub(crate) struct VaultEntryPlaintext {
    pub title: String,
    pub username: String,
    pub password: String,
    pub url: String,
    pub notes: String,
    pub tags: String,
    pub totp_secret: String,
    pub extra_json: String,
}

/// Errors local to the password_vault service. Converted to
/// `tonic::Status` at the RPC boundary via the `From` impl below.
#[derive(Debug, thiserror::Error)]
pub(crate) enum PasswordVaultError {
    #[error("vault entry not found: {0}")]
    NotFound(String),
    #[error("vault entry has no TOTP secret: {0}")]
    TotpNotConfigured(String),
    #[error("storage error: {0}")]
    Storage(String),
    #[error("crypto error")]
    Crypto,
    #[error("invalid input: {0}")]
    Invalid(String),
}

impl From<PasswordVaultError> for tonic::Status {
    fn from(err: PasswordVaultError) -> Self {
        match err {
            PasswordVaultError::NotFound(t) => {
                tonic::Status::not_found(format!("entry not found: {t}"))
            }
            PasswordVaultError::TotpNotConfigured(t) => {
                // Maps to HTTP 422 semantics from the existing Python service —
                // distinct from NotFound so callers can tell "entry exists but
                // no TOTP" apart from "no such entry".
                tonic::Status::failed_precondition(format!("no totp_secret on entry: {t}"))
            }
            PasswordVaultError::Storage(m) => tonic::Status::internal(format!("storage: {m}")),
            // Don't leak crypto error details — could expose oracle info.
            PasswordVaultError::Crypto => tonic::Status::internal("crypto failure"),
            PasswordVaultError::Invalid(m) => tonic::Status::invalid_argument(m),
        }
    }
}

/// Current wall-clock as unix milliseconds. Used for `created_at_ms`
/// and `deleted_at_ms`. Falls back to 0 on the (impossible-in-practice)
/// case of clock skew below the epoch.
pub(crate) fn now_unix_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}
