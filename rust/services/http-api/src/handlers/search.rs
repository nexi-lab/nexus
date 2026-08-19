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

use axum::extract::{Query, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use axum::{Json, Router};
use serde::{Deserialize, Serialize};

use crate::search_proto::GlobRequest;
use crate::{AppState, BackendError};

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
) -> Result<Json<GlobResponse>, GlobError> {
    let mut client = state
        .search
        .client()
        .await
        .map_err(GlobError::BackendUnavailable)?;

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
        .map_err(GlobError::Rpc)?
        .into_inner();

    Ok(Json(GlobResponse {
        paths: resp.paths,
        truncated: resp.truncated,
        error: resp.error,
    }))
}

pub fn router() -> Router<AppState> {
    Router::new().route("/v2/search/glob", get(glob))
}

/// Handler-level errors surfaced as HTTP responses.
///
/// Two-state mapping:
///   * `BackendUnavailable` → 503 (backend down; retry-friendly).
///   * `Rpc(status)` → status.code() mapped to the closest HTTP
///     status (400 for client-side bad request, 500 for anything
///     else) with the tonic message in the body so operators
///     debugging a hang can see the underlying signal.
#[derive(Debug, thiserror::Error)]
pub enum GlobError {
    #[error("backend unavailable: {0}")]
    BackendUnavailable(#[from] BackendError),
    #[error("rpc failed: {0}")]
    Rpc(tonic::Status),
}

impl IntoResponse for GlobError {
    fn into_response(self) -> Response {
        let (status, message) = match self {
            GlobError::BackendUnavailable(e) => (StatusCode::SERVICE_UNAVAILABLE, e.to_string()),
            GlobError::Rpc(s) => (grpc_status_to_http(s.code()), s.message().to_string()),
        };
        (status, Json(serde_json::json!({ "error": message }))).into_response()
    }
}

/// gRPC → HTTP status mapping.  Only the codes glob actually
/// surfaces at the handler; the fallback is 500 (server-side
/// generic error) so an unmapped code never silently degrades to
/// 200.  Kept small on purpose — expanding it later is additive.
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
