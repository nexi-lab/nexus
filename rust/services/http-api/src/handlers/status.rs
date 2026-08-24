//! `GET /v2/status` — smoke handler.  Returns `{status: "ok",
//! version: <crate version>}`.  Compile-time version from
//! `CARGO_PKG_VERSION` so no runtime plumbing is needed for the
//! health check.
//!
//! Future upgrades (raft leader status, plugin load state, DB
//! connectivity) attach to [`StatusResponse`] and the handler body,
//! not the route wiring.

use axum::{routing::get, Json, Router};
use serde::{Deserialize, Serialize};

use crate::AppState;

/// Response body of [`get_status`].  Owned `String` fields so
/// deserialisation callers do not need a `'static` byte source.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct StatusResponse {
    pub status: String,
    pub version: String,
}

pub async fn get_status() -> Json<StatusResponse> {
    Json(StatusResponse {
        status: "ok".to_string(),
        version: env!("CARGO_PKG_VERSION").to_string(),
    })
}

pub fn router() -> Router<AppState> {
    Router::new().route("/v2/status", get(get_status))
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::body::to_bytes;
    use axum::http::{Request, StatusCode};
    use tower::ServiceExt;

    fn test_state() -> AppState {
        // Status handler does not touch the backend or auth; the
        // `for_tests` helper's dummy target + NoAuth default fit
        // exactly.
        AppState::for_tests("http://127.0.0.1:1")
    }

    #[tokio::test]
    async fn status_route_returns_ok_with_version() {
        let response = router()
            .with_state(test_state())
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
        let response = router()
            .with_state(test_state())
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
