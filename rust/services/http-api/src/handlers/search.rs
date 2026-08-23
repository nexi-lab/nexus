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

use axum::extract::{Query, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use axum::{Json, Router};
use serde::{Deserialize, Serialize};

use crate::search_proto::{GlobRequest, GrepRequest};
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

// ── Router + shared error ────────────────────────────────────────

pub fn router() -> Router<AppState> {
    Router::new()
        .route("/v2/search/glob", get(glob))
        .route("/v2/search/grep", get(grep))
}

/// Shared handler-level error surfaced as an HTTP response.
///
/// Two-state mapping:
///   * `BackendUnavailable` → 503 (backend down; retry-friendly).
///   * `Rpc(status)` → gRPC `Code` mapped via [`grpc_status_to_http`]
///     (400 InvalidArgument, 401 Unauthenticated, 403 PermissionDenied,
///     404 NotFound, 504 DeadlineExceeded, 503 Unavailable, 500
///     fallback so an unmapped code never silently degrades to 200).
///     The upstream `tonic::Status::message()` rides through in the
///     JSON body so operators debugging a hang see the source signal.
///
/// One enum per `search.rs` — every handler in this module maps
/// its errors through here so the error contract stays uniform
/// across `glob`, `grep`, and every future route.
#[derive(Debug, thiserror::Error)]
pub enum SearchError {
    #[error("backend unavailable: {0}")]
    BackendUnavailable(#[from] BackendError),
    #[error("rpc failed: {0}")]
    Rpc(tonic::Status),
}

impl IntoResponse for SearchError {
    fn into_response(self) -> Response {
        let (status, message) = match self {
            SearchError::BackendUnavailable(e) => (StatusCode::SERVICE_UNAVAILABLE, e.to_string()),
            SearchError::Rpc(s) => (grpc_status_to_http(s.code()), s.message().to_string()),
        };
        (status, Json(serde_json::json!({ "error": message }))).into_response()
    }
}

/// gRPC → HTTP status mapping.  Only the codes handlers in this
/// module actually surface; fallback is 500 (server-side generic
/// error) so an unmapped code never silently degrades to 200.
/// Kept small on purpose — expanding it later is additive.
fn grpc_status_to_http(code: tonic::Code) -> StatusCode {
    match code {
        tonic::Code::InvalidArgument => StatusCode::BAD_REQUEST,
        tonic::Code::NotFound => StatusCode::NOT_FOUND,
        tonic::Code::PermissionDenied => StatusCode::FORBIDDEN,
        tonic::Code::Unauthenticated => StatusCode::UNAUTHORIZED,
        tonic::Code::DeadlineExceeded => StatusCode::GATEWAY_TIMEOUT,
        tonic::Code::Unavailable => StatusCode::SERVICE_UNAVAILABLE,
        _ => StatusCode::INTERNAL_SERVER_ERROR,
    }
}
