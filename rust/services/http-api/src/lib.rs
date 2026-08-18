//! `nexus-http-api` — axum-based HTTP-API surface for the pure-Rust
//! `nexus-server` replacement (epic #4674 review R10).
//!
//! # Scope of v0
//!
//! This crate is a **PoC skeleton**.  It ships exactly ONE route
//! (`GET /v2/status`) — the router + handler + JSON-serialisation +
//! test-harness pattern is proven end-to-end before the full
//! nexus-server surface (search, documents, auth) starts landing.
//!
//! Every future route lands as an additive [`axum::Router::merge`]
//! against [`router`] — the router shape is the extension seam.
//!
//! # Why axum
//!
//! Axum shares the tower + hyper stack with the existing
//! [`service-matrix-adapter`](../services/src/matrix_adapter.rs)
//! surface, so both HTTP crates link into the same binary at a
//! shared version pin (0.8) with no dep-tree churn.  The matrix
//! adapter has already proven the "axum on top of kernel syscalls"
//! pattern in production.
//!
//! # What v0 does NOT do
//!
//! * NO auth middleware — session cookie handling stays on the
//!   nexus-server Python side until the auth port lands (part of
//!   the same epic, but a separately-scoped step).
//! * NO ReBAC filter — same story.
//! * NO wiring into `nexusd-cluster` boot — that lives one crate
//!   over (rust/nexusd) and needs the nexus-cluster team to
//!   register the HTTP listener alongside the gRPC one.  This
//!   crate is standalone-testable via `axum::serve` +
//!   `tower::ServiceExt::oneshot` so PoC verification doesn't wait
//!   on the wiring step.

use axum::{routing::get, Json, Router};
use serde::{Deserialize, Serialize};

/// Response body of [`get_status`].  Kept small on purpose — the
/// health-check surface should read cheap and always succeed unless
/// the process itself is unhealthy.  `version` is compile-time from
/// [`CARGO_PKG_VERSION`](env!("CARGO_PKG_VERSION")) so it doesn't
/// need any runtime plumbing.  Owned `String` fields so
/// deserialisation callers don't need a `'static` byte source.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct StatusResponse {
    pub status: String,
    pub version: String,
}

/// Version 0 status handler — the "does the axum stack run at all"
/// smoke test.  Returns 200 with a fixed `{status: "ok", version:
/// <crate version>}` payload.  Future health-check upgrades (raft
/// leader status, plugin load state, DB connectivity) attach here.
pub async fn get_status() -> Json<StatusResponse> {
    Json(StatusResponse {
        status: "ok".to_string(),
        version: env!("CARGO_PKG_VERSION").to_string(),
    })
}

/// Root [`Router`] carrying every v0 route.  Follow-up crates in
/// the R10 epic (search, documents, auth) will `.merge()` their
/// own routers onto this one; the assembly binary (rust/nexusd)
/// calls [`router`] once and gets the aggregate.
pub fn router() -> Router {
    Router::new().route("/v2/status", get(get_status))
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::body::to_bytes;
    use axum::http::{Request, StatusCode};
    use tower::ServiceExt; // for `.oneshot`

    #[tokio::test]
    async fn status_route_returns_ok_with_version() {
        let response = router()
            .oneshot(
                Request::builder()
                    .uri("/v2/status")
                    .body(axum::body::Body::empty())
                    .unwrap(),
            )
            .await
            .expect("router oneshot");

        assert_eq!(response.status(), StatusCode::OK);

        let body = to_bytes(response.into_body(), 4096)
            .await
            .expect("read body");
        let parsed: StatusResponse = serde_json::from_slice(&body).expect("parse json");
        assert_eq!(parsed.status, "ok");
        assert_eq!(parsed.version, env!("CARGO_PKG_VERSION"));
    }

    #[tokio::test]
    async fn unknown_route_returns_404() {
        // Pins the router's default posture — new routes must be
        // added explicitly, not by accident.  When the R10 epic
        // starts adding /v2/search etc. this test flips on a new
        // path (e.g. /v2/does-not-exist) so it doesn't fail on the
        // additions.
        let response = router()
            .oneshot(
                Request::builder()
                    .uri("/v2/does-not-exist")
                    .body(axum::body::Body::empty())
                    .unwrap(),
            )
            .await
            .expect("router oneshot");
        assert_eq!(response.status(), StatusCode::NOT_FOUND);
    }
}
