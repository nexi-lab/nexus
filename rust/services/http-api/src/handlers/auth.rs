//! `/v2/auth/keys` — HTTP list + revoke over the kernel-adjacent
//! `AuthKeyStore` (raft-backed, cluster-wide replicated).
//!
//! Port of Python nexus-server's `/api/v2/auth/keys` router
//! (`src/nexus/server/api/v2/routers/auth_keys.py`), R10 arc.
//! Python-side ships the full CRUD (create + get + list + delete);
//! this Rust port ships **list + revoke** in this PR — mint (POST)
//! lands in a follow-up PR that requires plumbing the API-key
//! secret through `ServiceBootCtx` (needed to HMAC the returned
//! plaintext key at mint time).
//!
//! # Endpoints
//!
//! * `GET    /v2/auth/keys` — list every credential record on the
//!   local raft-applied replica.  Optional query filters
//!   (`?subject_type=`, `?include_revoked=`, `?is_admin=`) narrow
//!   client-side; the store returns every record and this handler
//!   applies the predicates before shaping the response.
//! * `DELETE /v2/auth/keys/:key_hash` — revoke by key hash (the
//!   caller has the hash but not the plaintext key — the audit
//!   view under `/__sys__/auth/keys/` exposes hashes only).  The
//!   response reports whether a record was actually removed
//!   (advisory — the raft log is authoritative).
//!
//! # Auth
//!
//! **Admin-only** — the mint / revoke plane on the gRPC side is
//! gated by a mTLS **node** cert (a peer, not any authenticated
//! caller).  HTTP has no mTLS cert, so this router substitutes an
//! `is_admin`-required check at the middleware boundary: a bearer
//! that resolves to `ctx.is_admin = true` passes, everything else
//! gets `403`.  Matches Python nexus-server's `dependencies=
//! [Depends(require_admin)]` posture.
//!
//! # Wire shape
//!
//! JSON response body deliberately mirrors the Python auth_keys.py
//! shape 1:1 (`key_id`, `subject_type`, `subject_id`, `is_admin`,
//! `revoked`, `expires_at_ms`, `zone_perms`, `name`) so an ops
//! script that scrapes `/api/v2/auth/keys` on Python side keeps
//! working when it flips to `/v2/auth/keys` on the Rust side.

use std::sync::Arc;

use auth::record::{AuthKeyRecord, SubjectType};
use axum::extract::{Path, Query, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::{delete, get};
use axum::{Extension, Json, Router};
use contracts::operation_context::OperationContext;
use kernel::hal::auth_key_store::AuthKeyStoreError;
use serde::{Deserialize, Serialize};

use crate::AppState;

// ── error surface ────────────────────────────────────────────────

/// Errors surfaced by the /v2/auth/keys handlers.  Same shape as
/// [`crate::handlers::search::SearchError`] +
/// [`crate::handlers::rebac::RebacError`] — a typed enum with an
/// [`IntoResponse`] impl mapping variants to HTTP status.
#[derive(Debug, thiserror::Error)]
pub enum AuthKeysError {
    /// Bearer resolved but the caller is not an admin.  Maps to
    /// 403 — matches Python nexus-server's `require_admin`
    /// rejection.
    #[error("admin privilege required to manage auth keys")]
    Forbidden,

    /// The `AuthKeyStore` backend refused a read / write.  Maps to
    /// 502 — caller is well-formed, backend is not currently
    /// serving.  Preserves the store's message for the operator log.
    #[error("auth-key store backend error: {0}")]
    Backend(String),
}

impl From<AuthKeyStoreError> for AuthKeysError {
    fn from(e: AuthKeyStoreError) -> Self {
        // The trait's `Backend(String)` is the only variant today;
        // matching exhaustively guards against a silent widen where
        // a new variant would fall through the wrong status.
        match e {
            AuthKeyStoreError::Backend(msg) => Self::Backend(msg),
        }
    }
}

impl IntoResponse for AuthKeysError {
    fn into_response(self) -> Response {
        let status = match &self {
            Self::Forbidden => StatusCode::FORBIDDEN,
            Self::Backend(_) => StatusCode::BAD_GATEWAY,
        };
        (status, self.to_string()).into_response()
    }
}

/// Enforce the "admin-only" gate at the handler boundary.  A bearer
/// that did not resolve to `is_admin == true` gets 403.
///
/// Kept as a plain fn instead of a middleware so the check runs
/// AFTER the `require_bearer` middleware has stamped
/// `Extension<OperationContext>` — the middleware's job is authN,
/// the handler's job is authZ.  Same split Python nexus-server uses
/// (`Depends(require_admin)` runs after the bearer resolver).
fn require_admin(ctx: &OperationContext) -> Result<(), AuthKeysError> {
    if ctx.is_admin {
        Ok(())
    } else {
        Err(AuthKeysError::Forbidden)
    }
}

// ── wire shape ───────────────────────────────────────────────────

/// One credential's public record on the wire — no key material.
///
/// The store's raw value is an opaque `Vec<u8>` (serde-json bytes
/// of an `AuthKeyRecord`); this shape is the client-facing view
/// after decode.  `key_hash` is added on top of the decoded record
/// because it lives in the store KEY, not the value — a caller
/// listing records needs the hash to target a subsequent
/// `DELETE /v2/auth/keys/:key_hash`.
///
/// Field names + JSON shape match Python nexus-server's
/// `list_keys` response 1:1 (see `auth_keys.py::list_keys` —
/// wraps `handle_admin_list_keys`).  A polling ops script that
/// scrapes the Python endpoint reads the Rust endpoint unchanged.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct AuthKeyView {
    /// HMAC-of-key store key.  Used by `DELETE /v2/auth/keys/:key_hash`.
    pub key_hash: String,
    /// Stable id for logs + audit tooling (not derived from key
    /// material — safe to log).
    pub key_id: String,
    /// Human label ("mac-ai laptop", "ci runner").
    pub name: String,
    /// `"user"` | `"agent"` | `"service"` — matches Python side's
    /// lowercase spelling.  A caller filtering by subject type
    /// compares against this string, not an int enum.
    pub subject_type: String,
    /// The principal id ((agent) name or user id).
    pub subject_id: String,
    /// Global admin flag.
    pub is_admin: bool,
    /// Tombstone flag — a revoked record is normally deleted
    /// outright, but the flag lets a soft-revoke survive for audit.
    pub revoked: bool,
    /// Expiry (ms since epoch); absent ⇒ never expires.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub expires_at_ms: Option<u64>,
    /// Zone grants as `(zone_id, perms)` pairs.  Empty ⇒ admin-only
    /// key (a non-admin key with no zone grants is refused at mint).
    pub zone_perms: Vec<(String, String)>,
}

impl AuthKeyView {
    /// Compose from a store row `(key_hash, opaque_bytes)`.  Returns
    /// `Ok(None)` for a row that fails to decode as an
    /// `AuthKeyRecord` — the same soft-skip posture the mint layer
    /// takes (`find_active_subject` continues past unreadable rows).
    /// A row from a newer schema this build cannot parse is invisible
    /// to `list`, but does not wedge the whole listing.
    fn from_row(key_hash: String, bytes: &[u8]) -> Option<Self> {
        let record = AuthKeyRecord::decode(bytes).ok()?;
        Some(Self {
            key_hash,
            key_id: record.key_id,
            name: record.name,
            subject_type: record.subject_type.as_str().to_string(),
            subject_id: record.subject_id,
            is_admin: record.is_admin,
            revoked: record.revoked,
            expires_at_ms: record.expires_at_ms,
            zone_perms: record.zone_perms,
        })
    }
}

// ── GET /v2/auth/keys ────────────────────────────────────────────

/// Query params for [`list`].  All optional; absent ⇒ no filter.
///
/// Client-side filtering — the store's `list()` returns every
/// record on the local raft-applied replica; this handler applies
/// the predicates before shaping the response.  Matches Python
/// nexus-server's `ListKeysParams` shape (see `auth_keys.py`).
///
/// `include_revoked` defaults to `false` (audit tooling asking
/// "what's active?"); flip it to `true` to see soft-revoked rows.
#[derive(Debug, Clone, Deserialize)]
pub struct ListQuery {
    /// `"user"` | `"agent"` | `"service"`.  Anything else silently
    /// filters to nothing (parse-then-match — an unknown value can
    /// only be a client typo).
    #[serde(default)]
    pub subject_type: Option<String>,
    /// Filter by subject id (`user_id` on the Python side).
    #[serde(default)]
    pub subject_id: Option<String>,
    /// Filter by admin flag.
    #[serde(default)]
    pub is_admin: Option<bool>,
    /// Include soft-revoked rows in the response.
    #[serde(default)]
    pub include_revoked: bool,
}

/// Response body for [`list`].
#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq, Eq)]
pub struct ListResponse {
    pub keys: Vec<AuthKeyView>,
}

/// Handler for `GET /v2/auth/keys` — list every credential.  Reads
/// the local raft-applied replica; no leader round-trip, works on a
/// learner.  Matches the `KeyMinter::list_keys` semantic (see
/// `raft::key_minter`).
pub async fn list(
    State(state): State<AppState>,
    Extension(ctx): Extension<OperationContext>,
    Query(params): Query<ListQuery>,
) -> Result<Json<ListResponse>, AuthKeysError> {
    require_admin(&ctx)?;
    let store = Arc::clone(&state.auth_key_store);
    // `store.list()` bridges through raft's blocking read; run
    // under `spawn_blocking` so the axum worker stays responsive.
    let rows = tokio::task::spawn_blocking(move || store.list())
        .await
        .map_err(|e| AuthKeysError::Backend(format!("list task panicked: {e}")))??;

    let keys: Vec<AuthKeyView> = rows
        .into_iter()
        .filter_map(|(hash, bytes)| AuthKeyView::from_row(hash, &bytes))
        .filter(|k| {
            // include_revoked=false ⇒ only active records
            if !params.include_revoked && k.revoked {
                return false;
            }
            if let Some(subj_type) = &params.subject_type {
                if k.subject_type != *subj_type {
                    return false;
                }
            }
            if let Some(subj_id) = &params.subject_id {
                if k.subject_id != *subj_id {
                    return false;
                }
            }
            if let Some(is_admin) = params.is_admin {
                if k.is_admin != is_admin {
                    return false;
                }
            }
            true
        })
        .collect();
    Ok(Json(ListResponse { keys }))
}

// ── DELETE /v2/auth/keys/:key_hash ───────────────────────────────

/// Response body for [`revoke`].  `existed` is advisory — a delete
/// on a missing key returns `Ok(false)` (idempotent); the flag lets
/// a caller distinguish "removed something" from "nothing there".
#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq, Eq)]
pub struct RevokeResponse {
    pub existed: bool,
    /// Echo of the hash the caller passed — lets a caller confirm
    /// they hit the row they meant to hit (defense-in-depth against
    /// a copy-paste error hitting the wrong row).
    pub key_hash: String,
}

/// Handler for `DELETE /v2/auth/keys/:key_hash` — revoke by hash.
///
/// Path shape matches Python nexus-server's
/// `/api/v2/auth/keys/{key_id}` DELETE — one path segment carries
/// the identifier.  We use HASH here (not `key_id`) because the
/// audit view (`/__sys__/auth/keys/`) exposes hashes; a caller
/// who knows a `key_id` but not its hash uses the /v2/auth/keys
/// GET response to look up the hash first.
///
/// Matches the `KeyMinter::revoke_key` semantic (see
/// `raft::key_minter`), one gate over: node-cert gate replaced
/// with HTTP admin gate at [`require_admin`].
pub async fn revoke(
    State(state): State<AppState>,
    Extension(ctx): Extension<OperationContext>,
    Path(key_hash): Path<String>,
) -> Result<Json<RevokeResponse>, AuthKeysError> {
    require_admin(&ctx)?;
    let store = Arc::clone(&state.auth_key_store);
    let hash_for_call = key_hash.clone();
    let existed = tokio::task::spawn_blocking(move || store.delete(&hash_for_call))
        .await
        .map_err(|e| AuthKeysError::Backend(format!("revoke task panicked: {e}")))??;
    Ok(Json(RevokeResponse { existed, key_hash }))
}

// ── Router ────────────────────────────────────────────────────────

pub fn router() -> Router<AppState> {
    Router::new()
        .route("/v2/auth/keys", get(list))
        .route("/v2/auth/keys/{key_hash}", delete(revoke))
}

// `SubjectType` is unused directly here — record decode uses its
// `.as_str()` impl inside `AuthKeyView::from_row`.  Silence the
// unused-import warning by asserting the type exists.
#[allow(dead_code)]
fn _assert_subject_type_is_reachable() -> Option<SubjectType> {
    None
}
