//! Access-token middleware — turns `Authorization: Bearer ...` into
//! a resolved [`AuthSession`] stored in request extensions. Downstream
//! handlers read it back via `Extension<AuthSession>`.
//!
//! Public endpoints (`login`) skip this layer; the router applies the
//! middleware only to handler groups that require an authenticated
//! caller. The middleware never panics: missing / unknown tokens
//! short-circuit with the matching `AdapterError` JSON response.

use axum::{
    extract::{Request, State},
    http::header,
    middleware::Next,
    response::Response,
};

use crate::matrix_adapter::auth::AuthSession;
use crate::matrix_adapter::error::AdapterError;
use crate::matrix_adapter::router::AdapterState;

/// Extract the bearer token from an `Authorization` header. Returns
/// `None` for missing / malformed headers — the caller decides whether
/// the absence is fatal.
pub fn parse_bearer(headers: &axum::http::HeaderMap) -> Option<&str> {
    let value = headers.get(header::AUTHORIZATION)?.to_str().ok()?;
    let trimmed = value.strip_prefix("Bearer ")?.trim();
    if trimmed.is_empty() {
        None
    } else {
        Some(trimmed)
    }
}

/// Axum `from_fn_with_state` middleware. Reads the bearer token,
/// resolves it through the [`AuthBackend`], stamps the resolved
/// [`AuthSession`] into request extensions, and delegates to `next`.
pub async fn require_access_token(
    State(state): State<AdapterState>,
    mut req: Request,
    next: Next,
) -> Result<Response, AdapterError> {
    let token = parse_bearer(req.headers()).ok_or(AdapterError::MissingToken)?;
    let session = state
        .auth
        .resolve_token(token)
        .await
        .map_err(|e| match e {
            crate::matrix_adapter::auth::AuthError::UnknownToken => AdapterError::UnknownToken,
            crate::matrix_adapter::auth::AuthError::Forbidden(m) => AdapterError::Forbidden(m),
            crate::matrix_adapter::auth::AuthError::Backend(m) => AdapterError::Internal(m),
        })?;
    req.extensions_mut().insert::<AuthSession>(session);
    Ok(next.run(req).await)
}
