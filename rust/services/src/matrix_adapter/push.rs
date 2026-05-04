//! Push gateway stubs (read-only).
//!
//! Per integration doc §4.2 D3 ships read-only push surfaces — enough
//! to keep stock chat clients (Element / FluffyChat / Cinny) from
//! erroring on startup, where they routinely probe `pushrules` and
//! `pushers` even when the user has no notifications configured.
//!
//! Active push delivery (POST /_matrix/push/v1/notify gateway, the
//! HTTP/2 push API, sygnal-style background workers) is a future PR;
//! this commit only stops the client from blowing up.

use axum::extract::Extension;
use axum::Json;
use serde_json::{json, Value};

use crate::matrix_adapter::auth::AuthSession;

/// `GET /_matrix/client/v3/pushrules` — return the canonical empty
/// rule-set. Spec requires the four ruleset categories
/// (`override`, `content`, `room`, `sender`) plus the default
/// `underride`; clients that probe this endpoint accept empty arrays.
pub async fn pushrules(Extension(_session): Extension<AuthSession>) -> Json<Value> {
    Json(json!({
        "global": {
            "override": [],
            "content": [],
            "room": [],
            "sender": [],
            "underride": [],
        },
    }))
}

/// `GET /_matrix/client/v3/pushers` — return zero pushers. Each pusher
/// would be a registered notification target (HTTP gateway, email,
/// FCM, …); D3 has none.
pub async fn pushers(Extension(_session): Extension<AuthSession>) -> Json<Value> {
    Json(json!({"pushers": []}))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::matrix_adapter::auth::stub::StubAuthBackend;
    use crate::matrix_adapter::router::{build_router, AdapterState};
    use axum::body::{to_bytes, Body};
    use axum::http::{header, Request, StatusCode};
    use std::sync::Arc;
    use tower::ServiceExt;

    const SERVER: &str = "nexus.local";

    fn fixture() -> axum::Router {
        let backend = Arc::new(StubAuthBackend::new(SERVER));
        backend.add_user("ethan", "hunter2");
        let state = AdapterState::new(backend, Arc::from(SERVER), None);
        build_router(state)
    }

    async fn login_token(app: &axum::Router) -> String {
        let payload = serde_json::json!({
            "type": "m.login.password",
            "identifier": {"type": "m.id.user", "user": "ethan"},
            "password": "hunter2",
        })
        .to_string();
        let req = Request::builder()
            .method("POST")
            .uri("/_matrix/client/v3/login")
            .header(header::CONTENT_TYPE, "application/json")
            .body(Body::from(payload))
            .unwrap();
        let resp = app.clone().oneshot(req).await.unwrap();
        let v: Value =
            serde_json::from_slice(&to_bytes(resp.into_body(), 64 * 1024).await.unwrap())
                .unwrap();
        v["access_token"].as_str().unwrap().to_string()
    }

    async fn get_with_token(app: &axum::Router, uri: &str, token: &str) -> (StatusCode, Value) {
        let resp = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("GET")
                    .uri(uri)
                    .header(header::AUTHORIZATION, format!("Bearer {token}"))
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        let status = resp.status();
        let v: Value =
            serde_json::from_slice(&to_bytes(resp.into_body(), 64 * 1024).await.unwrap())
                .unwrap();
        (status, v)
    }

    #[tokio::test]
    async fn pushrules_returns_empty_global_set() {
        let app = fixture();
        let token = login_token(&app).await;
        let (status, body) = get_with_token(&app, "/_matrix/client/v3/pushrules", &token).await;
        assert_eq!(status, StatusCode::OK);
        let global = body["global"].as_object().unwrap();
        for key in ["override", "content", "room", "sender", "underride"] {
            assert!(
                global[key].as_array().unwrap().is_empty(),
                "ruleset {key} should be empty"
            );
        }
    }

    #[tokio::test]
    async fn pushers_returns_empty_list() {
        let app = fixture();
        let token = login_token(&app).await;
        let (status, body) = get_with_token(&app, "/_matrix/client/v3/pushers", &token).await;
        assert_eq!(status, StatusCode::OK);
        assert!(body["pushers"].as_array().unwrap().is_empty());
    }

    #[tokio::test]
    async fn pushrules_requires_token() {
        let app = fixture();
        let resp = app
            .oneshot(
                Request::builder()
                    .method("GET")
                    .uri("/_matrix/client/v3/pushrules")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::UNAUTHORIZED);
    }
}
