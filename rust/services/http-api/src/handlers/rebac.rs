//! `/v2/rebac/tuples` — HTTP grant / list / revoke over the
//! kernel-adjacent tuple store.
//!
//! Callers write tuples through these routes; the enforcer
//! ([`nexus_rebac::RebacPermissionProvider`], installed at boot in
//! nexusd behind `--features rebac`) reads the same store on every
//! syscall.  One SSOT — the raft-backed `RaftReBACTupleStore` — with
//! two consumer surfaces (this HTTP router + the kernel gate).
//!
//! # Feature gate
//!
//! This whole module is `#[cfg(feature = "rebac")]` — the crate ships
//! empty rebac routes when the caller (nexusd) builds
//! `--features http-api` alone.  Composing `--features http-api,rebac`
//! (or `--features full`) links the [`nexus_rebac`] crate + registers
//! the routes.  Rationale: the `nexus-rebac` dep pulls in
//! `lib::rebac`'s Zanzibar substrate + `ahash` transitively — real
//! weight the slim `http-api` build does not need for search /
//! documents callers.
//!
//! # Endpoints
//!
//! * `POST   /v2/rebac/tuples` — grant a tuple.  Idempotent (a repeat
//!   put on the same key is a no-op at storage; the enforcer's graph
//!   cache re-materialises the same subject on rebuild).
//! * `DELETE /v2/rebac/tuples` — revoke a tuple.  Response reports
//!   whether the tuple existed at propose time (advisory — the
//!   revoke is idempotent, the raft log is authoritative).
//! * `GET    /v2/rebac/tuples?zone=<zone>` — list tuples in `zone`.
//!   Full scan (bounded per-zone in real deployments, O(100k) worst-
//!   case per the store contract).  No pagination — matches the
//!   graph-rebuild scan the enforcer already pays.
//!
//! # Auth
//!
//! Slotted under the same bearer sub-router as `/v2/search/*` +
//! `/v2/documents/*` (see [`crate::router`]).  Any authenticated
//! caller can grant / revoke — grant-authority is not further
//! gated here.  A later revision may enforce
//! `is_admin OR (grant delegates)` at this layer; today the tuples
//! are cluster-wide-trusted (control-plane consensus, not user
//! data).
//!
//! # Wire shape
//!
//! Body / query / response types mirror the tuple key convention
//! documented on [`nexus_rebac::tuple_key`] — pipe-delimited
//! `<zone>|<obj_type>|<obj_id>|<relation>|<subj_type>|<subj_id>
//! [|<subj_relation>]`.  Callers send / receive the 6-or-7 fields
//! as a typed JSON object; this handler encodes/decodes at the
//! boundary so the wire form stays operator-inspectable in raft
//! log dumps.

use std::sync::Arc;

use axum::extract::{Query, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::{delete, get, post};
use axum::{Json, Router};
use lib::types::ReBACTuple;
use nexus_rebac::{tuple_key, ReBACTupleStoreError};
use serde::{Deserialize, Serialize};

use crate::AppState;

// ── error surface ────────────────────────────────────────────────

/// Errors surfaced by the rebac handlers.  Same shape convention as
/// [`crate::handlers::search::SearchError`] — a typed enum with a
/// [`IntoResponse`] impl that maps each variant to an HTTP status
/// so the axum extractor lifts them to the client without a manual
/// `.map_err` per handler.
#[derive(Debug, thiserror::Error)]
pub enum RebacError {
    /// The store rejected the read/write (raft not-leader, backend
    /// unreachable, storage failure).  Maps to 502 — the caller is
    /// well-formed, the backend is not currently serving.
    #[error("rebac store backend error: {0}")]
    Backend(String),

    /// The submitted tuple contains the reserved pipe delimiter in a
    /// segment.  Maps to 400 — the client sent malformed input; a
    /// retry with escaped ids will succeed.  The store never sees
    /// this — [`tuple_key::encode`] catches it at the boundary.
    #[error("rebac tuple segment contains reserved delimiter '|': {0}")]
    BadSegment(String),
}

impl From<ReBACTupleStoreError> for RebacError {
    fn from(e: ReBACTupleStoreError) -> Self {
        match e {
            ReBACTupleStoreError::Backend(msg) => {
                // The tuple-key encoder wraps its own segment-check
                // failures in Backend(_) — distinguish them here so
                // the client gets 400 (their input is wrong) instead
                // of 502 (backend fault).  The encoder's message is
                // stable ("rebac tuple segment contains reserved
                // delimiter '|': ...") so a substring match is safe.
                if msg.contains("reserved delimiter") {
                    Self::BadSegment(msg)
                } else {
                    Self::Backend(msg)
                }
            }
        }
    }
}

impl IntoResponse for RebacError {
    fn into_response(self) -> Response {
        let status = match &self {
            Self::Backend(_) => StatusCode::BAD_GATEWAY,
            Self::BadSegment(_) => StatusCode::BAD_REQUEST,
        };
        (status, self.to_string()).into_response()
    }
}

// ── wire shape ───────────────────────────────────────────────────

/// One tuple on the wire — the 6-or-7 segments of the encoded key
/// as a typed JSON object.  Both request bodies (grant / revoke)
/// and response items use this shape; a repeat client that reads
/// a listing and immediately re-grants a subset does not translate
/// between shapes.
///
/// Field names match the Zanzibar convention documented on
/// [`lib::types::ReBACTuple`] one-to-one — the only extra field
/// here is `zone`, which lives in the store's key prefix (not on
/// the upstream `ReBACTuple`).
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct TupleBody {
    pub zone: String,
    pub object_type: String,
    pub object_id: String,
    pub relation: String,
    pub subject_type: String,
    pub subject_id: String,
    /// Optional userset-as-subject relation (the Zanzibar "members
    /// of `subject_type:subject_id` have this relation on the
    /// object" pattern).  Absent ⇒ direct tuple.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub subject_relation: Option<String>,
}

impl TupleBody {
    /// Split the wire shape into `(zone, ReBACTuple)`.
    fn split(self) -> (String, ReBACTuple) {
        (
            self.zone,
            ReBACTuple {
                object_type: self.object_type,
                object_id: self.object_id,
                relation: self.relation,
                subject_type: self.subject_type,
                subject_id: self.subject_id,
                subject_relation: self.subject_relation,
            },
        )
    }

    /// Compose from a `(zone, ReBACTuple)` — the shape a store
    /// listing produces.
    fn from_pair(zone: String, tuple: ReBACTuple) -> Self {
        Self {
            zone,
            object_type: tuple.object_type,
            object_id: tuple.object_id,
            relation: tuple.relation,
            subject_type: tuple.subject_type,
            subject_id: tuple.subject_id,
            subject_relation: tuple.subject_relation,
        }
    }
}

// ── POST /v2/rebac/tuples (grant) ────────────────────────────────

/// Response body for [`grant`].  Empty on success — the grant is
/// idempotent, and the caller already knows the tuple it sent.
#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq, Eq)]
pub struct GrantResponse {}

/// Handler for `POST /v2/rebac/tuples` — grant a tuple.
///
/// Encodes the wire shape into a store key + puts it.  Idempotent:
/// a repeat put on the same key is a no-op at storage; the
/// enforcer's graph cache re-materialises the same subject.  502
/// on a backend failure; 400 on a segment containing the reserved
/// `|` delimiter (fail-loud at the boundary, before the store sees
/// a malformed key).
pub async fn grant(
    State(state): State<AppState>,
    Json(body): Json<TupleBody>,
) -> Result<Json<GrantResponse>, RebacError> {
    let store = Arc::clone(&state.rebac_store);
    let (zone, tuple) = body.split();
    // Encode + put on a blocking thread — the store `put` bridges
    // through raft's `propose` (a `bridge_block_on` inside the
    // typed wrapper), which is a blocking call under the hood.
    // Same shape the search-plugin's write RPCs use.
    tokio::task::spawn_blocking(move || {
        let key = tuple_key::encode(&zone, &tuple)?;
        store.put(&key, b"")?;
        Ok::<_, ReBACTupleStoreError>(())
    })
    .await
    .map_err(|e| RebacError::Backend(format!("rebac grant task panicked: {e}")))??;
    Ok(Json(GrantResponse::default()))
}

// ── DELETE /v2/rebac/tuples (revoke) ─────────────────────────────

/// Response body for [`revoke`].  `existed` is advisory — the
/// revoke is idempotent (a delete on a missing key is not an
/// error); the flag lets a caller distinguish "removed something"
/// from "nothing was there" for their own audit trail.  The raft
/// log is authoritative regardless.
#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq, Eq)]
pub struct RevokeResponse {
    pub existed: bool,
}

/// Handler for `DELETE /v2/rebac/tuples` — revoke a tuple.
pub async fn revoke(
    State(state): State<AppState>,
    Json(body): Json<TupleBody>,
) -> Result<Json<RevokeResponse>, RebacError> {
    let store = Arc::clone(&state.rebac_store);
    let (zone, tuple) = body.split();
    let existed = tokio::task::spawn_blocking(move || {
        let key = tuple_key::encode(&zone, &tuple)?;
        store.delete(&key)
    })
    .await
    .map_err(|e| RebacError::Backend(format!("rebac revoke task panicked: {e}")))??;
    Ok(Json(RevokeResponse { existed }))
}

// ── GET /v2/rebac/tuples?zone=<zone> (list) ──────────────────────

/// Query params for [`list`].  `zone` is required — a zone-less
/// listing would return every grant on the cluster, which is not
/// what any real caller wants (and would be expensive on a large
/// deployment).  Missing / empty ⇒ 400 (the axum extractor
/// rejects an unset required field automatically).
#[derive(Debug, Clone, Deserialize)]
pub struct ListQuery {
    pub zone: String,
}

/// Response body for [`list`].
#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq, Eq)]
pub struct ListResponse {
    pub tuples: Vec<TupleBody>,
}

/// Handler for `GET /v2/rebac/tuples?zone=<zone>`.  Walks the
/// full store snapshot, filters by zone prefix, decodes each
/// matching key.  Malformed keys are soft-skipped (see
/// [`nexus_rebac::tuple_key::decode`] docstring) — a bad row in
/// the store does not wedge the whole listing.
pub async fn list(
    State(state): State<AppState>,
    Query(params): Query<ListQuery>,
) -> Result<Json<ListResponse>, RebacError> {
    if params.zone.is_empty() {
        return Err(RebacError::BadSegment(
            "zone query param is required and non-empty".to_string(),
        ));
    }
    let store = Arc::clone(&state.rebac_store);
    let zone = params.zone;
    let tuples = tokio::task::spawn_blocking(move || {
        let entries = store.list()?;
        let tuples: Vec<TupleBody> = entries
            .into_iter()
            .filter_map(|(key, _val)| {
                // Fast prefix compare — avoids the full 6/7-segment
                // split for out-of-zone keys.
                if tuple_key::zone_of(&key) != Some(zone.as_str()) {
                    return None;
                }
                // Soft-skip on decode failure — one malformed row
                // does not wedge the listing.  A caller auditing
                // for malformed rows should scan `store.list()`
                // directly.
                tuple_key::decode(&key).map(|(z, t)| TupleBody::from_pair(z, t))
            })
            .collect();
        Ok::<_, ReBACTupleStoreError>(tuples)
    })
    .await
    .map_err(|e| RebacError::Backend(format!("rebac list task panicked: {e}")))??;
    Ok(Json(ListResponse { tuples }))
}

// ── Router ────────────────────────────────────────────────────────

pub fn router() -> Router<AppState> {
    Router::new()
        .route("/v2/rebac/tuples", post(grant))
        .route("/v2/rebac/tuples", delete(revoke))
        .route("/v2/rebac/tuples", get(list))
}
