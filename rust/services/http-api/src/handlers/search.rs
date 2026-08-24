//! `/v2/search/*` — HTTP shims over the upstream
//! `nexus.search.v1.SearchService` gRPC surface.  Each handler
//! parses query params, builds a typed proto request, dispatches
//! through the cached [`SearchBackend`] client, and serialises the
//! response as JSON.
//!
//! # Auth / ReBAC
//!
//! Deliberately absent from this crate — sits in the middleware
//! layer above once the auth port lands (see the epic issue for
//! the sequence).  Handlers here operate on the callable
//! contract only; a caller with tokenised access is presumed
//! already granted.
//!
//! # Error shape (shared)
//!
//! Every handler surfaces backend / RPC errors as [`SearchError`];
//! the [`IntoResponse`] impl maps `BackendError` → HTTP 503 and
//! `tonic::Status` → HTTP status by gRPC `Code`.  A new handler
//! reuses the enum + mapper instead of hand-rolling its own.

use std::collections::HashMap;

use axum::extract::{Query, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use serde::{Deserialize, Serialize};

use crate::search_proto::{
    FusionMethod, GlobRequest, GrepRequest, QueryRequest, QueryResult as ProtoQueryResult,
    QueryType,
};
use crate::{AppState, BackendError};

// ── /v2/search/glob ──────────────────────────────────────────────

/// Query params for `GET /v2/search/glob`.  Field names match the
/// proto's [`GlobRequest`] snake_case names 1:1 so a caller
/// hitting the HTTP surface reads the same doc as one hitting the
/// gRPC surface directly.
#[derive(Debug, Clone, Deserialize)]
pub struct GlobQuery {
    /// Directory the glob walks under.  Defaults to `/` when
    /// unspecified so a caller can drop the param for full-tree
    /// searches.
    #[serde(default = "default_root")]
    pub root_path: String,
    /// Glob pattern (fnmatch-style: `*.rs`, `**/*.md`).
    pub pattern: String,
    /// Cap on returned paths.  0 (or absent) = server default.
    #[serde(default)]
    pub max_results: u32,
    /// Auth token forwarded through to the RPC.  Optional here
    /// because auth middleware is separately scoped; when the
    /// middleware lands it will populate this field before the
    /// handler runs.
    #[serde(default)]
    pub auth_token: String,
    /// Sort results by most-recent-mtime first when `true`.
    #[serde(default)]
    pub sort_recency: bool,
}

fn default_root() -> String {
    "/".to_string()
}

/// Response body of [`glob`].  Owned fields so deserialisation
/// callers do not need a `'static` byte source; field names
/// mirror the proto response so wire ↔ HTTP round-trips are
/// name-identical (a caller migrating from the gRPC surface
/// keeps its JSON parser).
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct GlobResponse {
    pub paths: Vec<String>,
    pub truncated: bool,
    /// Present only when the upstream reported an application
    /// error (RPC transport errors surface as HTTP 5xx instead).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

/// Handler for `GET /v2/search/glob`.
pub async fn glob(
    State(state): State<AppState>,
    Query(params): Query<GlobQuery>,
) -> Result<Json<GlobResponse>, SearchError> {
    let mut client = state.search.client().await?;
    let req = GlobRequest {
        root_path: params.root_path,
        pattern: params.pattern,
        max_results: params.max_results,
        auth_token: params.auth_token,
        sort_recency: params.sort_recency,
    };
    let resp = client
        .glob(tonic::Request::new(req))
        .await
        .map_err(SearchError::Rpc)?
        .into_inner();
    Ok(Json(GlobResponse {
        paths: resp.paths,
        truncated: resp.truncated,
        error: resp.error,
    }))
}

// ── /v2/search/grep ──────────────────────────────────────────────

/// Query params for `GET /v2/search/grep`.  Field names match the
/// proto's [`GrepRequest`] snake_case names 1:1 (same policy as
/// [`GlobQuery`]).
#[derive(Debug, Clone, Deserialize)]
pub struct GrepQuery {
    /// Directory the grep walks under.  Defaults to `/` for the
    /// same reason as [`GlobQuery::root_path`].
    #[serde(default = "default_root")]
    pub root_path: String,
    /// Regex compiled with the server-side `regex` crate.  Anchors,
    /// groups, and inline flags (`(?i)`) all work.
    pub pattern: String,
    /// Optional file-name filter (globset syntax).  Empty ⇒ scan
    /// every regular file.
    #[serde(default)]
    pub file_pattern: String,
    /// Case-insensitive match — kept as a separate wire field so
    /// "explicit case-insensitive" and "pattern contains `(?i)`"
    /// stay distinguishable.
    #[serde(default)]
    pub ignore_case: bool,
    /// Safety cap; 0 (or absent) = server-side default (1_000).
    #[serde(default)]
    pub max_results: u32,
    /// Lines of context before each match (default 0).
    #[serde(default)]
    pub before_context: u32,
    /// Lines of context after each match (default 0).
    #[serde(default)]
    pub after_context: u32,
    /// Invert: return lines that do NOT match the pattern.
    #[serde(default)]
    pub invert_match: bool,
    #[serde(default)]
    pub auth_token: String,
    /// Sort matches by containing-file mtime descending.
    #[serde(default)]
    pub sort_recency: bool,
}

/// One match row in [`GrepResponse::matches`].  Same field names as
/// the proto's `GrepMatch`.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct GrepMatch {
    pub path: String,
    pub line_number: u32,
    pub line: String,
    #[serde(default)]
    pub before: Vec<String>,
    #[serde(default)]
    pub after: Vec<String>,
}

/// Response body of [`grep`].  Same shape / policy as
/// [`GlobResponse`].
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct GrepResponse {
    pub matches: Vec<GrepMatch>,
    pub truncated: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

/// Handler for `GET /v2/search/grep`.
pub async fn grep(
    State(state): State<AppState>,
    Query(params): Query<GrepQuery>,
) -> Result<Json<GrepResponse>, SearchError> {
    let mut client = state.search.client().await?;
    let req = GrepRequest {
        root_path: params.root_path,
        pattern: params.pattern,
        file_pattern: params.file_pattern,
        ignore_case: params.ignore_case,
        max_results: params.max_results,
        before_context: params.before_context,
        after_context: params.after_context,
        invert_match: params.invert_match,
        auth_token: params.auth_token,
        sort_recency: params.sort_recency,
    };
    let resp = client
        .grep(tonic::Request::new(req))
        .await
        .map_err(SearchError::Rpc)?
        .into_inner();
    Ok(Json(GrepResponse {
        matches: resp
            .matches
            .into_iter()
            .map(|m| GrepMatch {
                path: m.path,
                line_number: m.line_number,
                line: m.line,
                before: m.before,
                after: m.after,
            })
            .collect(),
        truncated: resp.truncated,
        error: resp.error,
    }))
}

// ── /v2/search/query ─────────────────────────────────────────────

/// JSON body for `POST /v2/search/query`.  Field names match the
/// proto's [`QueryRequest`] snake_case names 1:1 (same policy as
/// [`GlobQuery`] / [`GrepQuery`]).  Enum-shaped fields land as
/// wire-friendly lowercase strings so a caller reading the proto
/// enum values and a caller reading this doc reach the same shape.
#[derive(Debug, Clone, Deserialize)]
pub struct QueryBody {
    /// Query text.
    pub q: String,
    /// Zone scoping.  Empty ⇒ ROOT_ZONE_ID.
    #[serde(default)]
    pub zone_id: String,
    /// Max results returned.  0 ⇒ server-side default (10).
    #[serde(default)]
    pub limit: u32,
    /// Optional path-prefix filter.
    #[serde(default)]
    pub path_filter: String,
    /// `"keyword"` (default) / `"semantic"` / `"hybrid"`.
    /// Wire-friendly string rather than the raw enum int so callers
    /// don't have to memorise the proto numeric.  An unknown value
    /// falls through to `"keyword"` — the same fail-open posture the
    /// proto's `UNSPECIFIED = 0` treats as keyword.
    #[serde(default)]
    pub query_type: String,
    #[serde(default)]
    pub auth_token: String,
    #[serde(default)]
    pub alpha: f32,
    /// `"rrf"` (default) / `"weighted"` / `"rrf_weighted"`.
    #[serde(default)]
    pub fusion_method: String,
    #[serde(default)]
    pub rrf_k: u32,
    /// Per-document chunk cap for pooling (#4542). 0 = no pooling.
    #[serde(default)]
    pub chunks_per_page: u32,
    /// `"macro"` (neighbour context expansion) / anything else ⇒
    /// none.  Absent ⇒ none.
    #[serde(default)]
    pub expand: String,
    /// `""` / `"off"` (default) / `"on"` / `"auto"`.
    #[serde(default)]
    pub recency_mode: String,
    #[serde(default)]
    pub recency_weight: f32,
    #[serde(default)]
    pub recency_half_life_days: f32,
    /// Per-prefix score multiplier map.  Empty ⇒ no per-prefix boost.
    #[serde(default)]
    pub path_prefix_boosts: HashMap<String, f32>,
}

/// One result row in [`QueryResponseBody::results`].  Every field
/// mirrors the proto's [`QueryResult`](ProtoQueryResult) so a
/// caller migrating from the gRPC surface keeps its JSON parser.
/// Optional fields serialise as absent (`skip_serializing_if =
/// Option::is_none`) so the wire stays compact for the common case
/// where an attribution field did not apply.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct QueryHit {
    pub path: String,
    pub chunk_index: u32,
    pub chunk_text: String,
    pub score: f32,
    pub zone_id: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub mtime_ms: Option<i64>,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub expanded_context: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub title_score: Option<f32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub keyword_score: Option<f32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub vector_score: Option<f32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub tier_boost: Option<f32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub recency_boost: Option<f32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub expansion_variant_index: Option<u32>,
}

/// Response body of [`query`].  Same absent-when-none policy as
/// [`GlobResponse`] / [`GrepResponse`] for the `error` field.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct QueryResponseBody {
    pub results: Vec<QueryHit>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

/// Shared "wire-string to proto enum" parser used by
/// [`parse_query_type`] + [`parse_fusion_method`].
///
/// # Contract
///
/// * Empty `s` — returns `Ok(default)`.  Matches the proto's
///   `UNSPECIFIED = 0` posture: an absent field means "server
///   default", not an error.
/// * Case-insensitive match against `variants` — returns `Ok(variant)`.
/// * Non-empty NON-matching `s` — returns `Err` naming the field and
///   listing the accepted values.  The caller maps this to `400 Bad
///   Request` so a caller typo (`"semantik"`) surfaces as an
///   actionable error instead of silently degrading to the default.
///
/// Extracted at the extract-on-second-occurrence trigger — both
/// query_type and fusion_method share the identical
/// "empty→default, N case-insensitive branches, else Err naming the
/// field" template.
fn parse_wire_enum<T: Copy>(
    s: &str,
    field: &str,
    default: T,
    variants: &[(&str, T)],
) -> Result<T, String> {
    if s.is_empty() {
        return Ok(default);
    }
    for (name, variant) in variants {
        if s.eq_ignore_ascii_case(name) {
            return Ok(*variant);
        }
    }
    let names: Vec<String> = variants.iter().map(|(n, _)| format!("\"{n}\"")).collect();
    Err(format!(
        "unknown {field} {s:?} (expected {})",
        names.join(", ")
    ))
}

/// Parse the `query_type` wire string into the proto enum.
/// See [`parse_wire_enum`] for the shared empty-vs-typo contract.
fn parse_query_type(s: &str) -> Result<QueryType, String> {
    parse_wire_enum(
        s,
        "query_type",
        QueryType::Keyword,
        &[
            ("keyword", QueryType::Keyword),
            ("semantic", QueryType::Semantic),
            ("hybrid", QueryType::Hybrid),
        ],
    )
}

/// Parse the `fusion_method` wire string into the proto enum.
/// See [`parse_wire_enum`] for the shared empty-vs-typo contract.
fn parse_fusion_method(s: &str) -> Result<FusionMethod, String> {
    parse_wire_enum(
        s,
        "fusion_method",
        FusionMethod::Rrf,
        &[
            ("rrf", FusionMethod::Rrf),
            ("weighted", FusionMethod::Weighted),
            ("rrf_weighted", FusionMethod::RrfWeighted),
        ],
    )
}

fn hit_from_proto(r: ProtoQueryResult) -> QueryHit {
    QueryHit {
        path: r.path,
        chunk_index: r.chunk_index,
        chunk_text: r.chunk_text,
        score: r.score,
        zone_id: r.zone_id,
        mtime_ms: r.mtime_ms,
        expanded_context: r.expanded_context,
        title_score: r.title_score,
        keyword_score: r.keyword_score,
        vector_score: r.vector_score,
        tier_boost: r.tier_boost,
        recency_boost: r.recency_boost,
        expansion_variant_index: r.expansion_variant_index,
    }
}

/// Handler for `POST /v2/search/query`.  JSON body variant of the
/// proto's `Query` RPC — GET-with-query-string would work for
/// simple cases but the request shape (hybrid knobs + recency
/// tuple + `path_prefix_boosts` map) is JSON-shaped in practice,
/// so a POST body keeps the wire honest.
pub async fn query(
    State(state): State<AppState>,
    Json(body): Json<QueryBody>,
) -> Result<Json<QueryResponseBody>, SearchError> {
    let query_type = parse_query_type(&body.query_type).map_err(SearchError::BadRequest)?;
    let fusion_method =
        parse_fusion_method(&body.fusion_method).map_err(SearchError::BadRequest)?;
    let mut client = state.search.client().await?;
    let req = QueryRequest {
        q: body.q,
        zone_id: body.zone_id,
        limit: body.limit,
        path_filter: body.path_filter,
        query_type: query_type as i32,
        auth_token: body.auth_token,
        alpha: body.alpha,
        fusion_method: fusion_method as i32,
        rrf_k: body.rrf_k,
        chunks_per_page: body.chunks_per_page,
        expand: body.expand,
        recency_mode: body.recency_mode,
        recency_weight: body.recency_weight,
        recency_half_life_days: body.recency_half_life_days,
        path_prefix_boosts: body.path_prefix_boosts,
    };
    let resp = client
        .query(tonic::Request::new(req))
        .await
        .map_err(SearchError::Rpc)?
        .into_inner();
    Ok(Json(QueryResponseBody {
        results: resp.results.into_iter().map(hit_from_proto).collect(),
        error: resp.error,
    }))
}

// ── Router + shared error ────────────────────────────────────────

pub fn router() -> Router<AppState> {
    Router::new()
        .route("/v2/search/glob", get(glob))
        .route("/v2/search/grep", get(grep))
        .route("/v2/search/query", post(query))
}

/// Shared handler-level error surfaced as an HTTP response.
///
/// Three-state mapping:
///   * `BackendUnavailable` → 503 (backend down; retry-friendly).
///   * `BadRequest(msg)` → 400 (caller-side error caught before RPC —
///     e.g. an unknown `query_type` string).  Emitting `Ok` and
///     letting the request through would silently downgrade to the
///     proto's `UNSPECIFIED = 0 ⇒ keyword` posture, hiding caller
///     typos (`"semantik"`) as "keyword returned no hits" for weeks.
///   * `Rpc(status)` → gRPC `Code` mapped via [`grpc_status_to_http`]
///     — see that fn's docstring for the full table.  The upstream
///     `tonic::Status::message()` rides through in the JSON body so
///     operators debugging a hang see the source signal.
///
/// One enum per `search.rs` — every handler in this module maps
/// its errors through here so the error contract stays uniform
/// across `glob`, `grep`, `query`, and every future route.
#[derive(Debug, thiserror::Error)]
pub enum SearchError {
    #[error("backend unavailable: {0}")]
    BackendUnavailable(#[from] BackendError),
    #[error("bad request: {0}")]
    BadRequest(String),
    #[error("rpc failed: {0}")]
    Rpc(tonic::Status),
}

impl IntoResponse for SearchError {
    fn into_response(self) -> Response {
        let (status, message) = match self {
            SearchError::BackendUnavailable(e) => (StatusCode::SERVICE_UNAVAILABLE, e.to_string()),
            SearchError::BadRequest(m) => (StatusCode::BAD_REQUEST, m),
            SearchError::Rpc(s) => (grpc_status_to_http(s.code()), s.message().to_string()),
        };
        (status, Json(serde_json::json!({ "error": message }))).into_response()
    }
}

/// gRPC → HTTP status mapping.  `pub(crate)` so `middleware::auth`
/// delegates instead of maintaining its own subset table; a real
/// `ApiKeyAuthProvider` can emit `ResourceExhausted` (rate limit)
/// / `InvalidArgument` (malformed sk-key) which would otherwise
/// silently degrade to 500 under a subset mapper.  Both routes
/// (search-RPC and auth-RPC) share the same wire semantics — no
/// reason to fork the table.
///
/// Covers every `tonic::Code` variant
/// the proto SSOT (`nexus/search/v1/search.proto`) declares upstream
/// can emit — plus the ambient retryable / auth codes any RPC can
/// surface — so no legitimate upstream signal silently degrades to
/// HTTP 500 (pager-worthy).
///
/// Table:
/// - `InvalidArgument` → 400 Bad Request
/// - `NotFound` → 404 Not Found
/// - `AlreadyExists` → 409 Conflict
/// - `PermissionDenied` → 403 Forbidden
/// - `ResourceExhausted` → 429 Too Many Requests (retry-after fits)
/// - `FailedPrecondition` → 412 Precondition Failed
/// - `Aborted` → 409 Conflict (transactional abort)
/// - `OutOfRange` → 400 Bad Request
/// - `Unimplemented` → 501 Not Implemented (proto declares Query returns
///   this for non-KEYWORD query_type in P1)
/// - `Unavailable` → 503 Service Unavailable
/// - `DeadlineExceeded` → 504 Gateway Timeout
/// - `Unauthenticated` → 401 Unauthorized
/// - `Ok` / `Cancelled` / `Unknown` / `Internal` / `DataLoss` → 500
///   Internal Server Error
///
/// `Ok` in the fallback is deliberate — surfacing an "Ok" gRPC status
/// as an HTTP error at all is a caller-side contract violation
/// (`Ok` should never come through `Result::Err`), so 500 flags the
/// bug rather than silently returning 200.
pub(crate) fn grpc_status_to_http(code: tonic::Code) -> StatusCode {
    match code {
        tonic::Code::InvalidArgument => StatusCode::BAD_REQUEST,
        tonic::Code::NotFound => StatusCode::NOT_FOUND,
        tonic::Code::AlreadyExists => StatusCode::CONFLICT,
        tonic::Code::PermissionDenied => StatusCode::FORBIDDEN,
        tonic::Code::ResourceExhausted => StatusCode::TOO_MANY_REQUESTS,
        tonic::Code::FailedPrecondition => StatusCode::PRECONDITION_FAILED,
        tonic::Code::Aborted => StatusCode::CONFLICT,
        tonic::Code::OutOfRange => StatusCode::BAD_REQUEST,
        tonic::Code::Unimplemented => StatusCode::NOT_IMPLEMENTED,
        tonic::Code::Unavailable => StatusCode::SERVICE_UNAVAILABLE,
        tonic::Code::DeadlineExceeded => StatusCode::GATEWAY_TIMEOUT,
        tonic::Code::Unauthenticated => StatusCode::UNAUTHORIZED,
        // Ok / Cancelled / Unknown / Internal / DataLoss — no HTTP
        // status distinguishes these usefully to a caller.  500
        // ensures an unmapped upstream signal never surfaces as 200.
        //
        // Exhaustive rather than `_` so a tonic upgrade that adds a
        // new `Code` variant breaks the build here loudly instead of
        // silently degrading production callers to 500.  When that
        // happens: add the new variant with a considered HTTP mapping
        // (or leave it in this fallback group with a docstring
        // update) — do NOT add `_ => 500` back.
        tonic::Code::Ok
        | tonic::Code::Cancelled
        | tonic::Code::Unknown
        | tonic::Code::Internal
        | tonic::Code::DataLoss => StatusCode::INTERNAL_SERVER_ERROR,
    }
}
