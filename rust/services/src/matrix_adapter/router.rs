//! Axum router assembly + endpoint handlers for the auth surface.

use std::sync::Arc;

use axum::extract::{Extension, Json, State};
use axum::middleware::from_fn_with_state;
use axum::response::IntoResponse;
use axum::routing::{get, post};
use axum::Router;

use crate::matrix_adapter::auth::{AuthBackendRef, AuthError, AuthSession};
use crate::matrix_adapter::error::AdapterError;
use crate::matrix_adapter::middleware::require_access_token;
use crate::matrix_adapter::rooms::{
    create_room, joined_members, room_join, room_leave, room_messages, room_send, room_state,
    room_state_event,
};
use crate::matrix_adapter::types::{EmptyResponse, LoginRequest, LoginResponse, WhoAmIResponse};

/// Shared adapter state — composed once at boot and cloned into each
/// handler. Cheap to clone (`Arc` and `String`).
#[derive(Clone)]
pub struct AdapterState {
    pub auth: AuthBackendRef,
    /// Matrix server-name suffix for the homeserver. Used in
    /// `LoginResponse.home_server` and the room-id ↔ stream-path
    /// codec. Configured at adapter boot.
    pub server_name: Arc<str>,
    /// Kernel handle the room read/write endpoints route through.
    /// Optional so the auth-only configuration (D1 surface tests)
    /// keeps building without a kernel dep.
    pub kernel: Option<Arc<kernel::kernel::Kernel>>,
}

/// Build the adapter's Matrix C-S router. Composes the public auth
/// endpoints with the token-protected ones; downstream PRs (D2/D3)
/// add room read/write + sync routes underneath the same shared
/// state without restructuring the boot wire-up.
pub fn build_router(state: AdapterState) -> Router {
    // Public — no token middleware. `login` is the only way to get one.
    let public = Router::new().route("/_matrix/client/v3/login", post(login));

    // Token-protected. `whoami` is the canonical "is my token still
    // valid?" probe; `logout` invalidates the token in the backend;
    // the rooms surface routes every chat-with-me read/write through
    // the kernel.
    let protected = Router::new()
        .route("/_matrix/client/v3/account/whoami", get(whoami))
        .route("/_matrix/client/v3/logout", post(logout))
        .route("/_matrix/client/v3/createRoom", post(create_room))
        .route("/_matrix/client/v3/rooms/:room_id/state", get(room_state))
        .route(
            "/_matrix/client/v3/rooms/:room_id/state/:event_type/:state_key",
            get(room_state_event),
        )
        .route(
            "/_matrix/client/v3/rooms/:room_id/messages",
            get(room_messages),
        )
        .route(
            "/_matrix/client/v3/rooms/:room_id/joined_members",
            get(joined_members),
        )
        .route(
            "/_matrix/client/v3/rooms/:room_id/send/:event_type/:txn_id",
            axum::routing::put(room_send),
        )
        .route("/_matrix/client/v3/rooms/:room_id/join", post(room_join))
        .route("/_matrix/client/v3/rooms/:room_id/leave", post(room_leave))
        .route_layer(from_fn_with_state(state.clone(), require_access_token));

    public.merge(protected).with_state(state)
}

async fn login(
    State(state): State<AdapterState>,
    Json(req): Json<LoginRequest>,
) -> Result<Json<LoginResponse>, AdapterError> {
    if req.login_type != "m.login.password" {
        return Err(AdapterError::Unrecognized(format!(
            "login type {:?} not supported (only m.login.password at D1)",
            req.login_type
        )));
    }
    if req.identifier.id_type != "m.id.user" {
        return Err(AdapterError::Unrecognized(format!(
            "identifier type {:?} not supported (only m.id.user at D1)",
            req.identifier.id_type
        )));
    }
    let user = req
        .identifier
        .user
        .ok_or_else(|| AdapterError::BadJson("identifier.user is required".into()))?;
    let password = req
        .password
        .ok_or_else(|| AdapterError::BadJson("password is required".into()))?;

    let session = state
        .auth
        .login_password(&user, &password)
        .await
        .map_err(map_auth_err)?;

    Ok(Json(LoginResponse {
        user_id: session.user_id,
        access_token: session.access_token,
        device_id: session.device_id,
        home_server: state.server_name.to_string(),
    }))
}

async fn logout(
    State(state): State<AdapterState>,
    Extension(session): Extension<AuthSession>,
) -> Result<Json<EmptyResponse>, AdapterError> {
    state
        .auth
        .logout(&session.access_token)
        .await
        .map_err(map_auth_err)?;
    Ok(Json(EmptyResponse::default()))
}

async fn whoami(
    Extension(session): Extension<AuthSession>,
) -> Json<WhoAmIResponse> {
    Json(WhoAmIResponse {
        user_id: session.user_id,
        device_id: session.device_id,
        is_guest: false,
    })
}

fn map_auth_err(err: AuthError) -> AdapterError {
    match err {
        AuthError::Forbidden(m) => AdapterError::Forbidden(m),
        AuthError::UnknownToken => AdapterError::UnknownToken,
        AuthError::Backend(m) => AdapterError::Internal(m),
    }
}

// Suppress unused-import warning when the adapter is built without
// callers — the `IntoResponse` import is used by the trait bound on
// `AdapterError` through the handlers' return types.
const _: fn() = || {
    fn assert_into_response<T: IntoResponse>() {}
    assert_into_response::<AdapterError>();
};

#[cfg(test)]
mod tests {
    use super::*;
    use crate::matrix_adapter::auth::stub::StubAuthBackend;
    use axum::body::{to_bytes, Body};
    use axum::http::{header, Request, StatusCode};
    use serde_json::{json, Value};
    use tower::ServiceExt;

    const SERVER_NAME: &str = "nexus.local";

    fn fixture(seed_users: &[(&str, &str)]) -> Router {
        let backend = Arc::new(StubAuthBackend::new(SERVER_NAME));
        for (user, pw) in seed_users {
            backend.add_user(user, pw);
        }
        let state = AdapterState {
            auth: backend,
            server_name: Arc::from(SERVER_NAME),
            kernel: None,
        };
        build_router(state)
    }

    async fn json_body(resp: axum::response::Response) -> Value {
        let body = to_bytes(resp.into_body(), 64 * 1024)
            .await
            .expect("body bytes");
        serde_json::from_slice(&body).expect("response is JSON")
    }

    fn login_request(user: &str, password: &str) -> Request<Body> {
        let payload = json!({
            "type": "m.login.password",
            "identifier": {"type": "m.id.user", "user": user},
            "password": password,
        });
        Request::builder()
            .method("POST")
            .uri("/_matrix/client/v3/login")
            .header(header::CONTENT_TYPE, "application/json")
            .body(Body::from(payload.to_string()))
            .unwrap()
    }

    #[tokio::test]
    async fn login_password_success_returns_access_token() {
        let app = fixture(&[("ethan", "hunter2")]);
        let resp = app
            .oneshot(login_request("ethan", "hunter2"))
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let body = json_body(resp).await;
        assert_eq!(body["user_id"], "@ethan:nexus.local");
        assert_eq!(body["device_id"], "stub-device-ethan");
        assert_eq!(body["home_server"], "nexus.local");
        assert_eq!(body["access_token"], "stub-token-ethan");
    }

    #[tokio::test]
    async fn login_wrong_password_is_forbidden() {
        let app = fixture(&[("ethan", "hunter2")]);
        let resp = app
            .oneshot(login_request("ethan", "wrong"))
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::FORBIDDEN);
        let body = json_body(resp).await;
        assert_eq!(body["errcode"], "M_FORBIDDEN");
    }

    #[tokio::test]
    async fn login_unknown_user_is_forbidden() {
        let app = fixture(&[]);
        let resp = app
            .oneshot(login_request("ghost", "x"))
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::FORBIDDEN);
    }

    #[tokio::test]
    async fn unsupported_login_type_returns_unrecognized() {
        let app = fixture(&[("ethan", "hunter2")]);
        let payload = json!({
            "type": "m.login.token",
            "identifier": {"type": "m.id.user", "user": "ethan"},
            "token": "x",
        });
        let req = Request::builder()
            .method("POST")
            .uri("/_matrix/client/v3/login")
            .header(header::CONTENT_TYPE, "application/json")
            .body(Body::from(payload.to_string()))
            .unwrap();
        let resp = app.oneshot(req).await.unwrap();
        assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
        let body = json_body(resp).await;
        assert_eq!(body["errcode"], "M_UNRECOGNIZED");
    }

    #[tokio::test]
    async fn whoami_with_valid_token_returns_session_identity() {
        let app = fixture(&[("ethan", "hunter2")]);
        let login_resp = app
            .clone()
            .oneshot(login_request("ethan", "hunter2"))
            .await
            .unwrap();
        let login_json = json_body(login_resp).await;
        let token = login_json["access_token"].as_str().unwrap().to_string();

        let req = Request::builder()
            .method("GET")
            .uri("/_matrix/client/v3/account/whoami")
            .header(header::AUTHORIZATION, format!("Bearer {token}"))
            .body(Body::empty())
            .unwrap();
        let resp = app.oneshot(req).await.unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let body = json_body(resp).await;
        assert_eq!(body["user_id"], "@ethan:nexus.local");
        assert_eq!(body["is_guest"], false);
    }

    #[tokio::test]
    async fn whoami_without_token_is_unauthorized() {
        let app = fixture(&[]);
        let req = Request::builder()
            .method("GET")
            .uri("/_matrix/client/v3/account/whoami")
            .body(Body::empty())
            .unwrap();
        let resp = app.oneshot(req).await.unwrap();
        assert_eq!(resp.status(), StatusCode::UNAUTHORIZED);
        let body = json_body(resp).await;
        assert_eq!(body["errcode"], "M_MISSING_TOKEN");
    }

    #[tokio::test]
    async fn whoami_with_unknown_token_is_unauthorized() {
        let app = fixture(&[]);
        let req = Request::builder()
            .method("GET")
            .uri("/_matrix/client/v3/account/whoami")
            .header(header::AUTHORIZATION, "Bearer not-a-real-token")
            .body(Body::empty())
            .unwrap();
        let resp = app.oneshot(req).await.unwrap();
        assert_eq!(resp.status(), StatusCode::UNAUTHORIZED);
        let body = json_body(resp).await;
        assert_eq!(body["errcode"], "M_UNKNOWN_TOKEN");
    }

    #[tokio::test]
    async fn logout_invalidates_token_so_followup_whoami_fails() {
        let app = fixture(&[("ethan", "hunter2")]);
        let login_resp = app
            .clone()
            .oneshot(login_request("ethan", "hunter2"))
            .await
            .unwrap();
        let token = json_body(login_resp).await["access_token"]
            .as_str()
            .unwrap()
            .to_string();

        let logout_req = Request::builder()
            .method("POST")
            .uri("/_matrix/client/v3/logout")
            .header(header::AUTHORIZATION, format!("Bearer {token}"))
            .body(Body::empty())
            .unwrap();
        let logout_resp = app.clone().oneshot(logout_req).await.unwrap();
        assert_eq!(logout_resp.status(), StatusCode::OK);

        // Token should no longer resolve.
        let probe_req = Request::builder()
            .method("GET")
            .uri("/_matrix/client/v3/account/whoami")
            .header(header::AUTHORIZATION, format!("Bearer {token}"))
            .body(Body::empty())
            .unwrap();
        let probe_resp = app.oneshot(probe_req).await.unwrap();
        assert_eq!(probe_resp.status(), StatusCode::UNAUTHORIZED);
    }
}
