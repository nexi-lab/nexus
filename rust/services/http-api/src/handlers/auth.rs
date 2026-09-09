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

    /// POST-body validation error surfaced by the mint layer (invalid
    /// zone grant syntax, subject-type not one of user/service,
    /// zoneless non-admin key, subject already holds a key + no
    /// `allow_existing`, unknown `subject_type`, etc.).  Maps to 400.
    /// The mint layer's message names the exact problem.
    #[error("mint request rejected: {0}")]
    BadRequest(String),

    /// The daemon was booted `--no-tls` and there is no sk- HMAC
    /// secret to sign a new key with.  Maps to 503 — a valid request
    /// against a valid endpoint, but the plane is not up.  Matches
    /// the gRPC `MintKey` posture ("returns success=false").
    #[error("mint unavailable: this daemon was booted without API-key auth (--no-tls)")]
    Unavailable,
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
            Self::BadRequest(_) => StatusCode::BAD_REQUEST,
            Self::Unavailable => StatusCode::SERVICE_UNAVAILABLE,
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

// ── POST /v2/auth/keys (mint) ────────────────────────────────────

/// JSON body for `POST /v2/auth/keys` — mint a fresh sk- key.
///
/// Field-shape mirrors Python nexus-server's `CreateKeyRequest`
/// closely; a few Python-only fields (grants → ReBAC tuples,
/// expires_days) are trimmed for a first cut and follow up.  In
/// particular:
///
/// * `zones` — list of `"<zone_id>:<perms>"` strings (e.g. `"root:rwx"`).
///   The mint layer parses them; an empty list is refused UNLESS
///   `admin: true` (the only kind of principal allowed a zoneless
///   key).
/// * `expires_at_ms` — absolute ms-since-epoch cutoff; `0` / absent
///   ⇒ never expires.  (Python uses `expires_days` relative; the
///   Rust API takes absolute to avoid clock-drift ambiguity at the
///   admin edge — an operator computes `now_ms + days*86400_000`.)
/// * `allow_existing` — rotation escape.  Off by default; on when
///   the caller is deliberately issuing a second key for an
///   already-credentialed subject (rotation).
#[derive(Debug, Clone, Deserialize)]
pub struct MintBody {
    /// Human label ("mac-ai laptop", "ci runner").  Optional; empty
    /// string is preserved as the record's `name`.
    #[serde(default)]
    pub name: String,
    /// `"user"` | `"service"`.  `"agent"` uses a separate mint plane
    /// (agent identities are cert-anchored, not sk-token-anchored).
    pub subject_type: String,
    pub subject_id: String,
    /// Zone grants as `"<zone_id>:<perms>"` strings.  Empty unless
    /// `admin=true`.
    #[serde(default)]
    pub zones: Vec<String>,
    /// Global admin flag.  Only zoneless keys are admin.
    #[serde(default)]
    pub admin: bool,
    /// Absolute ms-since-epoch cutoff; `0` / absent ⇒ never expires.
    #[serde(default)]
    pub expires_at_ms: u64,
    /// Rotation escape.
    #[serde(default)]
    pub allow_existing: bool,
}

/// Response body for [`mint`].  `key` is the one-time plaintext
/// credential the caller MUST persist immediately — the daemon
/// only stores its HMAC.  Response shape mirrors Python
/// nexus-server's `create_key` return.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct MintResponse {
    /// One-time plaintext credential.  Present ONLY in the mint
    /// response; never resurfaceable from the store afterwards.
    pub key: String,
    /// HMAC store key — the caller can revoke by this hash later.
    pub key_hash: String,
    /// Stable id for logs + audit tooling.
    pub key_id: String,
    /// Echo of the record (same shape as `list` entries; convenient
    /// for a client that wants a single round-trip mint→display).
    pub record: AuthKeyView,
}

/// Handler for `POST /v2/auth/keys` — mint a fresh sk- key.
pub async fn mint(
    State(state): State<AppState>,
    Extension(ctx): Extension<OperationContext>,
    Json(body): Json<MintBody>,
) -> Result<Json<MintResponse>, AuthKeysError> {
    require_admin(&ctx)?;

    // Fail-loud when the daemon has no HMAC secret — matches the
    // gRPC `MintKey` posture (returns success=false under NoAuth).
    let secret = state
        .api_key_secret
        .as_ref()
        .ok_or(AuthKeysError::Unavailable)?
        .clone();
    let store = Arc::clone(&state.auth_key_store);

    // Parse + validate + mint on a blocking thread — `auth::mint_key`
    // does a full `store.list()` scan for the uniqueness check + a
    // `store.put` (both blocking on the raft layer).  Errors from
    // parse (bad zone-grant syntax, unknown subject_type, empty
    // zone list on a non-admin key) come back as `BadRequest`;
    // storage failures come back as `Backend`.  The subject-already-
    // holds-a-key case surfaces from `mint_key` as `Backend(msg)` —
    // we peek the message to reclassify it to `BadRequest`, matching
    // Python's 400 shape.
    let minted = tokio::task::spawn_blocking(move || {
        let record = build_record(&body).map_err(AuthKeysError::BadRequest)?;
        auth::mint::mint_key(&store, &secret, record, body.allow_existing).map_err(|e| {
            let msg = e.to_string();
            // The mint layer's uniqueness rejection is a `Backend`
            // variant carrying a message that starts with "subject
            // ... already has an active key" — reclassify to 400
            // (client input error, not a backend fault).
            if msg.contains("already has an active key") {
                AuthKeysError::BadRequest(msg)
            } else {
                AuthKeysError::from(e)
            }
        })
    })
    .await
    .map_err(|e| AuthKeysError::Backend(format!("mint task panicked: {e}")))??;

    let view = AuthKeyView::from_row(
        minted.key_hash.clone(),
        &minted.record.encode().map_err(|e| {
            AuthKeysError::Backend(format!("encode fresh record for response: {e}"))
        })?,
    )
    .ok_or_else(|| {
        AuthKeysError::Backend(
            "decoded-just-encoded record failed — schema roundtrip broken".to_string(),
        )
    })?;
    Ok(Json(MintResponse {
        key: minted.key,
        key_hash: minted.key_hash,
        key_id: minted.record.key_id,
        record: view,
    }))
}

/// Compose an `AuthKeyRecord` from the request body.  Owns:
///
///   * subject-type parsing (rejects `agent` — separate plane)
///   * zone-grant syntax parsing (`"zone_id:perms"`)
///   * zoneless-admin-only invariant
///   * `key_id` synthesis (uuid v4-ish; delegated to `auth`'s helper
///     if any, else a random hex string)
///
/// Errors are `String`s — the caller wraps them in
/// `AuthKeysError::BadRequest`.
fn build_record(body: &MintBody) -> Result<AuthKeyRecord, String> {
    let subject_type = match body.subject_type.as_str() {
        "user" => SubjectType::User,
        "service" => SubjectType::Service,
        "agent" => {
            return Err(
                "subject_type='agent' uses the cert-anchored mint plane, not sk-".to_string(),
            )
        }
        other => {
            return Err(format!(
                "unknown subject_type={other:?} (allowed: user, service)"
            ))
        }
    };
    let zone_perms: Vec<(String, String)> = body
        .zones
        .iter()
        .map(|z| {
            z.split_once(':')
                .map(|(zone, perms)| (zone.to_string(), perms.to_string()))
                .ok_or_else(|| {
                    format!(
                        "zone grant {z:?} malformed — expected \"<zone_id>:<perms>\" \
                         (e.g. \"root:rwx\")"
                    )
                })
        })
        .collect::<Result<_, _>>()?;
    if zone_perms.is_empty() && !body.admin {
        return Err(
            "a key with no zone grants reaches nothing and is refused at authentication \
             time.  Pass zones=[\"<zone>:<perms>\"], or admin=true for a global admin \
             (the only principal allowed a zoneless key)."
                .to_string(),
        );
    }
    let expires_at_ms = if body.expires_at_ms == 0 {
        None
    } else {
        Some(body.expires_at_ms)
    };
    Ok(AuthKeyRecord {
        // Composed inline here — the `auth` crate's helper
        // (`build_sk_record`) lives in `nexus-vfs/rust/profiles/
        // cluster/src/lib.rs` alongside `DaemonKeyMinter` and is
        // not `pub`; keep the record synthesis local to this
        // handler until upstream ships a shared helper.
        key_id: generate_key_id(),
        name: body.name.clone(),
        subject_type,
        subject_id: body.subject_id.clone(),
        is_admin: body.admin,
        revoked: false,
        expires_at_ms,
        zone_perms,
    })
}

/// Stable audit-log id for a fresh sk- record.  Not a secret; the
/// mint layer's HMAC is what carries entropy — this id is a log
/// handle only.  Uses `SystemTime::now()` nanoseconds + a process-
/// local monotonically-increasing counter to avoid a `uuid` or
/// `rand`/`getrandom` dep just for this one call site.  Collisions
/// are effectively impossible: two calls in the same nanosecond
/// still differ on the counter.
fn generate_key_id() -> String {
    use std::sync::atomic::{AtomicU64, Ordering};
    use std::time::{SystemTime, UNIX_EPOCH};
    static COUNTER: AtomicU64 = AtomicU64::new(0);
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos() as u64)
        .unwrap_or(0);
    let counter = COUNTER.fetch_add(1, Ordering::Relaxed);
    format!("kid-{nanos:x}{counter:x}")
}

// ── Router ────────────────────────────────────────────────────────

pub fn router() -> Router<AppState> {
    Router::new()
        .route("/v2/auth/keys", get(list).post(mint))
        .route("/v2/auth/keys/{key_hash}", delete(revoke))
}

// `SubjectType` is unused directly here — record decode uses its
// `.as_str()` impl inside `AuthKeyView::from_row`.  Silence the
// unused-import warning by asserting the type exists.
#[allow(dead_code)]
fn _assert_subject_type_is_reachable() -> Option<SubjectType> {
    None
}
