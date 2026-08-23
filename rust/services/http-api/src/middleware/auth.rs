//! Bearer-token auth middleware for the `/v2/*` protected routes.
//!
//! # What this does
//!
//! Reads `Authorization: Bearer <token>` off the incoming request,
//! resolves it through the shared [`AuthProvider`] on [`AppState`],
//! and — on success — stamps the resulting [`OperationContext`] into
//! request extensions.  Downstream handlers pull the context via
//! `axum::Extension<OperationContext>`; a caller that does not need
//! per-request identity simply does not extract it.  A missing /
//! malformed / rejected token short-circuits with the appropriate
//! HTTP status; the underlying handler never runs.
//!
//! # Why this crate reuses the transport trait
//!
//! [`transport::auth::AuthProvider`] is the SSOT trait every
//! authenticated `nexus.*.v1` gRPC surface already consumes via
//! `transport::grpc::VfsServiceImpl::authenticate`, and its resolved
//! [`OperationContext`] is the SSOT struct every `kernel::sys_*` in
//! turn consumes.  A local trait would be a needless divergence — the
//! bearer plane the middleware validates is exactly the plane the
//! kernel + gRPC surface already accept.
//!
//! # First-cut scope (per epic #4674 R10 "auth middleware" step)
//!
//! * `Authorization: Bearer <token>` header extraction only —
//!   plaintext axum stack ships no rustls today, so mTLS peer plane
//!   is out of scope until the HTTP surface gains a TLS listener.
//! * `NoAuth` default keeps single-node dev unblocked (matches the
//!   `nexusd` kernel default).
//! * `/v2/status` stays PUBLIC (health endpoint used by liveness
//!   probes before any token exists); every `/v2/search/*` route is
//!   protected.
//!
//! # Explicit non-goals (documented so a follow-up PR knows the
//! # cutline and does not re-tread ground):
//!
//! * mTLS peer plane — pending the HTTP surface's rustls wiring
//!   (needs a `TlsConnectInfo` shape equivalent to the gRPC path's
//!   `transport::peer_identity::from_request`).
//! * Cookie-session handling — Python `nexus-server` does NOT use
//!   session cookies (the only cookie is an OAuth-CSRF binding, not
//!   an identity carrier); nothing to port.
//! * ReBAC post-filter — separate epic step (`src/nexus/bricks/
//!   permissions/rebac.py` → Rust); this middleware does authN only,
//!   never authZ.
//! * Cache invalidation observer — `ApiKeyAuthProvider` HMAC-caches
//!   at the transport layer already; when a real provider is wired
//!   into `AppState` its invalidation stream rides on the same raft
//!   observer path the gRPC interceptor uses.

use std::sync::Arc;

use axum::extract::{Request, State};
use axum::http::{header, StatusCode};
use axum::middleware::Next;
use axum::response::{IntoResponse, Response};
use axum::Json;
use transport::auth::{AuthCredentials, AuthProvider};

use crate::AppState;

/// Extract the bearer token from an `Authorization` header.
///
/// # Return shape
///
/// * `Ok(Some(tok))` — well-formed `Authorization: Bearer <token>`
///   with a non-empty token after trim.
/// * `Ok(None)` — the header is ABSENT.  Callers pass this to the
///   provider as an empty token; matches
///   `transport::grpc::VfsServiceImpl::authenticate`, which always
///   calls `resolve` with `AuthCredentials::from_token(inner.auth_token())`
///   (an empty string when the caller supplied no token).  The
///   provider decides — `NoAuth` accepts, `ApiKeyAuthProvider`
///   rejects with `Unauthenticated`.
/// * `Err(AuthRejection::MalformedHeader)` — the header is PRESENT
///   but not a well-formed `Bearer <non-empty-token>` (wrong
///   scheme, non-ASCII bytes, empty after `Bearer`).  A caller
///   that ships an `Authorization` header CLEARLY intended to
///   authenticate; a silent fall-through to empty-token would
///   read a `Basic dXNlcjpwYXNz` header as "no credentials" and
///   admit the request under `NoAuth`.  Reject immediately.
///
/// Case-insensitive scheme match — RFC 7235 §2.1 says the scheme
/// is case-insensitive, so `bearer` / `BEARER` are equally valid.
pub fn parse_bearer(headers: &axum::http::HeaderMap) -> Result<Option<&str>, AuthRejection> {
    let Some(raw_value) = headers.get(header::AUTHORIZATION) else {
        return Ok(None);
    };
    let raw = raw_value
        .to_str()
        .map_err(|_| AuthRejection::MalformedHeader)?;
    let scheme_end = raw.find(' ').ok_or(AuthRejection::MalformedHeader)?;
    let (scheme, rest) = raw.split_at(scheme_end);
    if !scheme.eq_ignore_ascii_case("Bearer") {
        return Err(AuthRejection::MalformedHeader);
    }
    let token = rest.trim_start();
    if token.is_empty() {
        return Err(AuthRejection::MalformedHeader);
    }
    Ok(Some(token))
}

/// Middleware entry point wired via `axum::middleware::from_fn_with_state`
/// under [`crate::router`].  Runs BEFORE the handler; a rejected
/// request never reaches upstream tonic (saves the RPC round-trip
/// and denies a token-guessing attacker any per-attempt latency
/// signal from the upstream).
///
/// Status mapping matches the shared search-error contract:
///   * malformed `Authorization` header → 401 Unauthorized
///   * `tonic::Code::Unauthenticated` → 401 Unauthorized
///   * `tonic::Code::PermissionDenied` → 403 Forbidden
///   * `tonic::Code::Unavailable` → 503 Service Unavailable
///   * `tonic::Code::DeadlineExceeded` → 504 Gateway Timeout
///   * anything else (`Internal`, `Unknown`, etc.) → 500
///
/// Absent-header behaviour is provider-driven (see [`parse_bearer`]).
pub async fn require_bearer(
    State(state): State<AppState>,
    mut req: Request,
    next: Next,
) -> Result<Response, AuthRejection> {
    let token = parse_bearer(req.headers())?.unwrap_or("");
    let ctx = state
        .auth
        .resolve(&AuthCredentials::from_token(token))
        .map_err(AuthRejection::Rpc)?;
    req.extensions_mut().insert(ctx);
    Ok(next.run(req).await)
}

/// Rejections the middleware surfaces before a protected handler
/// runs.  Local to this module — the shape is "auth failed", not
/// "search / query returned an error".
#[derive(Debug, thiserror::Error)]
pub enum AuthRejection {
    /// `Authorization` header is present but not a well-formed
    /// `Bearer <non-empty-token>`.  ABSENT headers do NOT map here —
    /// they pass through to the provider as an empty token (matches
    /// gRPC's `authenticate` posture).
    #[error("malformed Authorization header; expected `Bearer <token>`")]
    MalformedHeader,
    #[error("bearer resolution failed: {0}")]
    Rpc(tonic::Status),
}

impl IntoResponse for AuthRejection {
    fn into_response(self) -> Response {
        let (status, message) = match self {
            AuthRejection::MalformedHeader => (
                StatusCode::UNAUTHORIZED,
                "malformed Authorization header; expected `Bearer <token>`".to_string(),
            ),
            AuthRejection::Rpc(s) => (status_for_auth_code(s.code()), s.message().to_string()),
        };
        (status, Json(serde_json::json!({ "error": message }))).into_response()
    }
}

/// gRPC → HTTP mapping for auth resolution.  A tighter table than
/// the general search mapper: an `AuthProvider` cannot legitimately
/// emit `NotFound` / `AlreadyExists` / `ResourceExhausted` — its
/// entire contract is "resolve credentials or reject" — so we surface
/// only the codes it can meaningfully return + the transport-level
/// (`Unavailable` / `DeadlineExceeded`) codes any RPC can produce.
fn status_for_auth_code(code: tonic::Code) -> StatusCode {
    match code {
        tonic::Code::Unauthenticated => StatusCode::UNAUTHORIZED,
        tonic::Code::PermissionDenied => StatusCode::FORBIDDEN,
        tonic::Code::Unavailable => StatusCode::SERVICE_UNAVAILABLE,
        tonic::Code::DeadlineExceeded => StatusCode::GATEWAY_TIMEOUT,
        _ => StatusCode::INTERNAL_SERVER_ERROR,
    }
}

/// Default `Arc<dyn AuthProvider>` used by callers that have no
/// real provider wired yet.  Matches the kernel's `NoAuth` default:
/// every token → an admin `OperationContext` — SAFE ONLY for
/// single-node dev / tests.  Callers that federate or serve agents
/// MUST swap this out for a real provider (e.g.
/// `auth::ApiKeyAuthProvider`) before serving traffic.
pub fn default_no_auth_provider() -> Arc<dyn AuthProvider> {
    Arc::new(transport::auth::NoAuth)
}

#[cfg(test)]
mod tests {
    use super::*;
    use contracts::operation_context::OperationContext;

    fn headers_with(auth: &str) -> axum::http::HeaderMap {
        let mut h = axum::http::HeaderMap::new();
        h.insert(header::AUTHORIZATION, auth.parse().unwrap());
        h
    }

    #[test]
    fn parse_bearer_returns_token_for_well_formed_header() {
        let h = headers_with("Bearer sk-abcdef123456");
        assert_eq!(parse_bearer(&h).unwrap(), Some("sk-abcdef123456"));
    }

    #[test]
    fn parse_bearer_is_case_insensitive_on_scheme() {
        // RFC 7235 §2.1 — scheme is case-insensitive.
        for header in ["bearer sk-x", "BEARER sk-x", "Bearer sk-x"] {
            let h = headers_with(header);
            assert_eq!(parse_bearer(&h).unwrap(), Some("sk-x"), "scheme {header:?}");
        }
    }

    #[test]
    fn parse_bearer_returns_ok_none_when_header_absent() {
        // Absent header ≠ malformed header — absent falls through to
        // the provider as empty-token (matches gRPC posture).  The
        // middleware then relies on the provider to accept or reject.
        let h = axum::http::HeaderMap::new();
        assert!(matches!(parse_bearer(&h), Ok(None)));
    }

    #[test]
    fn parse_bearer_rejects_non_bearer_scheme() {
        // `Basic ...` was clearly INTENDED to authenticate; treating
        // it as "no credentials" would be a silent contract break
        // (the base64 body would slip past a NoAuth provider as
        // "any token").  Reject as malformed → 401.
        for header in ["Basic dXNlcjpwYXNz", "Digest realm=\"x\""] {
            let h = headers_with(header);
            assert!(
                matches!(parse_bearer(&h), Err(AuthRejection::MalformedHeader)),
                "scheme {header:?} must reject",
            );
        }
    }

    #[test]
    fn parse_bearer_rejects_empty_token_after_bearer() {
        // A caller sending `Bearer ` shipped the header — same
        // "intent to authenticate" argument as non-Bearer schemes.
        for header in ["Bearer ", "Bearer   ", "Bearer\t"] {
            let h = headers_with(header);
            assert!(
                matches!(parse_bearer(&h), Err(AuthRejection::MalformedHeader)),
                "header {header:?} must reject",
            );
        }
    }

    #[test]
    fn status_for_auth_code_maps_the_documented_table() {
        assert_eq!(
            status_for_auth_code(tonic::Code::Unauthenticated),
            StatusCode::UNAUTHORIZED,
        );
        assert_eq!(
            status_for_auth_code(tonic::Code::PermissionDenied),
            StatusCode::FORBIDDEN,
        );
        assert_eq!(
            status_for_auth_code(tonic::Code::Unavailable),
            StatusCode::SERVICE_UNAVAILABLE,
        );
        assert_eq!(
            status_for_auth_code(tonic::Code::DeadlineExceeded),
            StatusCode::GATEWAY_TIMEOUT,
        );
        // Unmapped codes fall to 500 rather than degrade to 200 —
        // matches the search-side mapper's fallback contract.
        assert_eq!(
            status_for_auth_code(tonic::Code::Internal),
            StatusCode::INTERNAL_SERVER_ERROR,
        );
    }

    #[test]
    fn default_no_auth_provider_returns_admin_context_for_any_token() {
        let p = default_no_auth_provider();
        for token in ["", "any", "sk-fake"] {
            let ctx = p.resolve(&AuthCredentials::from_token(token)).unwrap();
            assert_eq!(ctx.user_id, "cluster-internal");
            assert!(ctx.is_admin, "NoAuth default must remain admin");
        }
    }

    /// Reject-all provider — pins the rejection branch of
    /// `require_bearer` without dragging a real key store into the
    /// crate.  The `RejectAll` in `transport::auth::tests` is
    /// gated `#[cfg(test)]` and therefore not visible cross-crate,
    /// so this crate carries its own tiny copy.
    struct RejectAll;
    impl AuthProvider for RejectAll {
        fn resolve(&self, _: &AuthCredentials<'_>) -> Result<OperationContext, tonic::Status> {
            Err(tonic::Status::unauthenticated("bearer rejected"))
        }
    }

    #[tokio::test]
    async fn require_bearer_absent_header_defers_to_provider_reject_becomes_401() {
        use axum::body::Body;
        use axum::http::Request;
        use axum::Router;
        use tower::ServiceExt;

        // Absent header → empty token passed to provider.  A strict
        // provider (RejectAll) then returns Unauthenticated → 401.
        // Pins the "absent header = provider decides" contract:
        // NoAuth would let the same request through (proven by the
        // sibling glob_e2e / grep_e2e / query_e2e files which run
        // against NoAuth and never send a bearer).
        async fn inner_ok() -> &'static str {
            "reached"
        }
        let state = AppState {
            search: crate::SearchBackend::new("http://127.0.0.1:1"),
            auth: Arc::new(RejectAll),
        };
        let app: Router = Router::new()
            .route("/protected", axum::routing::get(inner_ok))
            .layer(axum::middleware::from_fn_with_state(
                state.clone(),
                require_bearer,
            ))
            .with_state(state);

        let resp = app
            .oneshot(
                Request::builder()
                    .uri("/protected")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::UNAUTHORIZED);
    }

    #[tokio::test]
    async fn require_bearer_absent_header_with_noauth_provider_lets_request_through() {
        use axum::body::Body;
        use axum::http::Request;
        use axum::Router;
        use tower::ServiceExt;

        // Absent header under NoAuth: provider accepts empty token
        // → middleware lets the request reach the handler.  This is
        // the single-node dev posture the sibling e2e files rely on.
        async fn inner_ok() -> &'static str {
            "reached"
        }
        let state = AppState {
            search: crate::SearchBackend::new("http://127.0.0.1:1"),
            auth: default_no_auth_provider(),
        };
        let app: Router = Router::new()
            .route("/protected", axum::routing::get(inner_ok))
            .layer(axum::middleware::from_fn_with_state(
                state.clone(),
                require_bearer,
            ))
            .with_state(state);

        let resp = app
            .oneshot(
                Request::builder()
                    .uri("/protected")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
    }

    #[tokio::test]
    async fn require_bearer_returns_401_on_malformed_header_regardless_of_provider() {
        // A malformed header (non-Bearer scheme) is rejected at the
        // parser BEFORE the provider is called — so even a permissive
        // NoAuth provider does NOT admit the request.  Prevents a
        // `Basic <base64>` header from being silently read as
        // "no credentials".
        use axum::body::Body;
        use axum::http::Request;
        use axum::Router;
        use tower::ServiceExt;

        async fn inner_ok() -> &'static str {
            "reached"
        }
        let state = AppState {
            search: crate::SearchBackend::new("http://127.0.0.1:1"),
            auth: default_no_auth_provider(),
        };
        let app: Router = Router::new()
            .route("/protected", axum::routing::get(inner_ok))
            .layer(axum::middleware::from_fn_with_state(
                state.clone(),
                require_bearer,
            ))
            .with_state(state);

        let resp = app
            .oneshot(
                Request::builder()
                    .uri("/protected")
                    .header(header::AUTHORIZATION, "Basic dXNlcjpwYXNz")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::UNAUTHORIZED);
    }

    #[tokio::test]
    async fn require_bearer_returns_401_when_provider_rejects_the_token() {
        use axum::body::Body;
        use axum::http::Request;
        use axum::Router;
        use tower::ServiceExt;

        async fn inner_ok() -> &'static str {
            "reached"
        }
        let state = AppState {
            search: crate::SearchBackend::new("http://127.0.0.1:1"),
            auth: Arc::new(RejectAll),
        };
        let app: Router = Router::new()
            .route("/protected", axum::routing::get(inner_ok))
            .layer(axum::middleware::from_fn_with_state(
                state.clone(),
                require_bearer,
            ))
            .with_state(state);

        let resp = app
            .oneshot(
                Request::builder()
                    .uri("/protected")
                    .header(header::AUTHORIZATION, "Bearer sk-does-not-resolve")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::UNAUTHORIZED);
    }

    #[tokio::test]
    async fn require_bearer_injects_operation_context_when_provider_accepts() {
        use axum::body::Body;
        use axum::http::Request;
        use axum::response::IntoResponse;
        use axum::Extension;
        use axum::Router;
        use tower::ServiceExt;

        // The handler MUST see the resolved OperationContext — proves
        // the middleware inserted it into extensions before the
        // handler ran.  Returns the user_id in the body so the test
        // can pin the exact identity that landed.
        async fn echo_user_id(Extension(ctx): Extension<OperationContext>) -> impl IntoResponse {
            ctx.user_id.clone()
        }
        let state = AppState {
            search: crate::SearchBackend::new("http://127.0.0.1:1"),
            auth: default_no_auth_provider(), // returns "cluster-internal"
        };
        let app: Router = Router::new()
            .route("/protected", axum::routing::get(echo_user_id))
            .layer(axum::middleware::from_fn_with_state(
                state.clone(),
                require_bearer,
            ))
            .with_state(state);

        let resp = app
            .oneshot(
                Request::builder()
                    .uri("/protected")
                    .header(header::AUTHORIZATION, "Bearer any-token")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let bytes = axum::body::to_bytes(resp.into_body(), 1024).await.unwrap();
        assert_eq!(&bytes[..], b"cluster-internal");
    }
}
