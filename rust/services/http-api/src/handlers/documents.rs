//! `/v2/documents/*` — HTTP shims over the upstream
//! `nexus.search.v1.SearchService` write / meta RPCs.
//!
//! Sibling of [`crate::handlers::search`] (which owns the read RPCs
//! — glob / grep / query).  The split follows the Python
//! `nexus-server` two-router shape (search=read, documents=
//! write+meta) so callers migrating from FastAPI keep their URL
//! namespaces intact.
//!
//! Every handler here reuses [`crate::handlers::search::SearchError`]
//! (same wire mapping table — `Unimplemented → 501`,
//! `ResourceExhausted → 429`, etc.) and slots under the same
//! bearer-middleware sub-router as the search routes (see
//! [`crate::router`]).
//!
//! # Endpoints
//!
//! * `POST /v2/documents/index`   — full recursive walk from
//!   `root_path`, indexes every regular file the plugin sees.
//!   Maps to `SearchService::Index`.
//! * `POST /v2/documents/refresh` — incremental mtime-diff pass
//!   over an already-indexed subtree.  Maps to
//!   `SearchService::Refresh`.
//! * `POST /v2/documents/batch`   — explicit upsert of a list of
//!   documents (`{path, text, mtime_ms?, zone_id?}`).  Maps to
//!   `SearchService::IndexDocuments`.
//! * `GET  /v2/documents/stats`   — index diagnostics
//!   (fts_doc_count / fts_path_count / ann_chunk_count / etc.).
//!   Maps to `SearchService::Stats`.

use axum::extract::{Query, State};
use axum::routing::{get, post};
use axum::{Json, Router};
use serde::{Deserialize, Serialize};

use crate::handlers::search::SearchError;
use crate::search_proto::{
    DocumentInput as ProtoDocumentInput, IndexDocumentsRequest, IndexRequest, RefreshRequest,
    StatsRequest,
};
use crate::AppState;

// ── shared field-shape helpers ───────────────────────────────────

fn default_zone() -> String {
    // Empty ⇒ server-side default (ROOT_ZONE_ID); matches the
    // sibling search routes.  Kept as an explicit helper so a
    // future zone-required route can flip the shape without
    // touching every field.
    String::new()
}

// ── /v2/documents/index ──────────────────────────────────────────

/// JSON body for `POST /v2/documents/index`.  Field names match
/// the proto's [`IndexRequest`] snake_case names 1:1 — same policy
/// as [`crate::handlers::search::QueryBody`].
#[derive(Debug, Clone, Deserialize)]
pub struct IndexBody {
    /// Walk start.  Absolute VFS path (starts with `/`).
    pub root_path: String,
    /// Zone scope; empty ⇒ ROOT_ZONE_ID.
    #[serde(default = "default_zone")]
    pub zone_id: String,
    /// Recurse into subdirectories.  Absent / false ⇒ direct
    /// children of `root_path` only (matches Python default).
    #[serde(default)]
    pub recursive: bool,
    /// Cap on documents visited.  0 ⇒ server default (10_000);
    /// callers indexing a larger corpus call `Index` repeatedly
    /// with narrower `root_path` scopes.
    #[serde(default)]
    pub max_docs: u32,
    #[serde(default)]
    pub auth_token: String,
}

/// Response body of [`index`].  Mirrors proto `IndexResponse`; the
/// optional error rides through per the sibling-handler convention.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct IndexResponseBody {
    pub indexed_count: u32,
    pub skipped_count: u32,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

/// Handler for `POST /v2/documents/index`.
pub async fn index(
    State(state): State<AppState>,
    Json(body): Json<IndexBody>,
) -> Result<Json<IndexResponseBody>, SearchError> {
    let mut client = state.search.client().await?;
    let req = IndexRequest {
        root_path: body.root_path,
        zone_id: body.zone_id,
        recursive: body.recursive,
        max_docs: body.max_docs,
        auth_token: body.auth_token,
    };
    let resp = client
        .index(tonic::Request::new(req))
        .await
        .map_err(SearchError::Rpc)?
        .into_inner();
    Ok(Json(IndexResponseBody {
        indexed_count: resp.indexed_count,
        skipped_count: resp.skipped_count,
        error: resp.error,
    }))
}

// ── /v2/documents/refresh ────────────────────────────────────────

/// JSON body for `POST /v2/documents/refresh`.  Same field-shape as
/// [`IndexBody`] — mtime-diff Refresh + full Index take the same
/// `root_path` + `recursive` + `max_docs` scope.
#[derive(Debug, Clone, Deserialize)]
pub struct RefreshBody {
    pub root_path: String,
    #[serde(default = "default_zone")]
    pub zone_id: String,
    #[serde(default)]
    pub recursive: bool,
    #[serde(default)]
    pub max_docs: u32,
    #[serde(default)]
    pub auth_token: String,
}

/// Response body of [`refresh`].  Adds `unchanged_count` +
/// `removed_count` + `truncated` fields to the Index shape — the
/// whole point of Refresh is telling apart "reindexed / dropped /
/// still-current" so a caller can gate a "healthy-empty is real"
/// decision.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct RefreshResponseBody {
    pub reindexed_count: u32,
    pub removed_count: u32,
    pub unchanged_count: u32,
    pub skipped_count: u32,
    pub truncated: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

/// Handler for `POST /v2/documents/refresh`.
pub async fn refresh(
    State(state): State<AppState>,
    Json(body): Json<RefreshBody>,
) -> Result<Json<RefreshResponseBody>, SearchError> {
    let mut client = state.search.client().await?;
    let req = RefreshRequest {
        root_path: body.root_path,
        zone_id: body.zone_id,
        recursive: body.recursive,
        max_docs: body.max_docs,
        auth_token: body.auth_token,
    };
    let resp = client
        .refresh(tonic::Request::new(req))
        .await
        .map_err(SearchError::Rpc)?
        .into_inner();
    Ok(Json(RefreshResponseBody {
        reindexed_count: resp.reindexed_count,
        removed_count: resp.removed_count,
        unchanged_count: resp.unchanged_count,
        skipped_count: resp.skipped_count,
        truncated: resp.truncated,
        error: resp.error,
    }))
}

// ── /v2/documents/batch ──────────────────────────────────────────

/// One document in a [`BatchBody`] — the proto's `DocumentInput`
/// shape mirrored on the wire.  A batch may span zones; each doc
/// carries an optional `zone_id` that overrides the batch-wide
/// default.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DocumentInput {
    pub path: String,
    pub text: String,
    /// Optional millisecond-resolution mtime.  Absent ⇒ plugin
    /// skips the P6 recency signal for this doc.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub mtime_ms: Option<i64>,
    /// Per-doc zone override.  Empty ⇒ falls back to the
    /// batch-wide [`BatchBody::zone_id`].
    #[serde(default = "default_zone")]
    pub zone_id: String,
}

/// JSON body for `POST /v2/documents/batch`.  Explicit upsert —
/// caller ships each document's text; the plugin does not read
/// the VFS.  Used by nexus-server today for user-visible document
/// posts (as opposed to the `Index` RPC which walks the VFS).
#[derive(Debug, Clone, Deserialize)]
pub struct BatchBody {
    pub documents: Vec<DocumentInput>,
    /// Batch-wide zone default; per-doc `zone_id` overrides.
    #[serde(default = "default_zone")]
    pub zone_id: String,
    #[serde(default)]
    pub auth_token: String,
}

/// Response body of [`batch`].  `parked_paths` surfaces docs that
/// hit a transient failure and got parked for retry (see the
/// `ParkedList` RPC surface, exposed elsewhere) — kept optional-
/// but-always-present because an empty vec is the wire's "no
/// parking" signal.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct BatchResponseBody {
    pub indexed_count: u32,
    pub skipped_count: u32,
    pub parked_paths: Vec<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

/// Handler for `POST /v2/documents/batch`.
pub async fn batch(
    State(state): State<AppState>,
    Json(body): Json<BatchBody>,
) -> Result<Json<BatchResponseBody>, SearchError> {
    let mut client = state.search.client().await?;
    let req = IndexDocumentsRequest {
        documents: body
            .documents
            .into_iter()
            .map(|d| ProtoDocumentInput {
                path: d.path,
                text: d.text,
                mtime_ms: d.mtime_ms,
                zone_id: d.zone_id,
            })
            .collect(),
        zone_id: body.zone_id,
        auth_token: body.auth_token,
    };
    let resp = client
        .index_documents(tonic::Request::new(req))
        .await
        .map_err(SearchError::Rpc)?
        .into_inner();
    Ok(Json(BatchResponseBody {
        indexed_count: resp.indexed_count,
        skipped_count: resp.skipped_count,
        parked_paths: resp.parked_paths,
        error: resp.error,
    }))
}

// ── /v2/documents/stats ──────────────────────────────────────────

/// Query params for `GET /v2/documents/stats`.  GET body-less
/// because the wire shape is a bounded scalar tuple — no room for
/// a list / map that would justify a POST body.
#[derive(Debug, Clone, Deserialize)]
pub struct StatsQuery {
    /// Zone scope; empty ⇒ ROOT_ZONE_ID.
    #[serde(default = "default_zone")]
    pub zone_id: String,
    #[serde(default)]
    pub auth_token: String,
}

/// Response body of [`stats`].  Every field mirrors the proto's
/// `StatsResponse`; `error` follows the same absent-when-None
/// posture as sibling handlers.  Backend-identity fields
/// (`backend`, `embedding_model`) survive the migration from
/// Python — pre-pivot callers scripted around them (health
/// pollers, canaries) keep working.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct StatsResponseBody {
    pub fts_doc_count: u32,
    pub fts_path_count: u32,
    pub ann_chunk_count: u32,
    pub parked_count: u32,
    /// Constant `"rust-plugin"` post-P12 — distinguishes this
    /// generation from the deleted Python daemon's BM25S / pgvector
    /// stacks.  Pollers gating on backend identity keep their
    /// scripts.
    pub backend: String,
    /// Configured embedding model tag (e.g. `"mE5-small-v1"`).
    /// Empty ⇒ keyword-only mode.
    pub embedding_model: String,
    /// Non-zero ⇒ query results may be served from a partially-
    /// built index; pollers gating "healthy-empty is real"
    /// decisions wait for 0.
    pub indexing_in_progress: u32,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

/// Handler for `GET /v2/documents/stats`.
pub async fn stats(
    State(state): State<AppState>,
    Query(params): Query<StatsQuery>,
) -> Result<Json<StatsResponseBody>, SearchError> {
    let mut client = state.search.client().await?;
    let req = StatsRequest {
        zone_id: params.zone_id,
        auth_token: params.auth_token,
    };
    let resp = client
        .stats(tonic::Request::new(req))
        .await
        .map_err(SearchError::Rpc)?
        .into_inner();
    Ok(Json(StatsResponseBody {
        fts_doc_count: resp.fts_doc_count,
        fts_path_count: resp.fts_path_count,
        ann_chunk_count: resp.ann_chunk_count,
        parked_count: resp.parked_count,
        backend: resp.backend,
        embedding_model: resp.embedding_model,
        indexing_in_progress: resp.indexing_in_progress,
        error: resp.error,
    }))
}

// ── Router ────────────────────────────────────────────────────────

pub fn router() -> Router<AppState> {
    Router::new()
        .route("/v2/documents/index", post(index))
        .route("/v2/documents/refresh", post(refresh))
        .route("/v2/documents/batch", post(batch))
        .route("/v2/documents/stats", get(stats))
}
