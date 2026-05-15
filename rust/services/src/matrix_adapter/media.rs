//! Media repo — upload, download, thumbnail.
//!
//! Integration doc §4.2 spec'd media storage as `DT_FILE under
//! /media/{media_id}` with CAS for content + raft for the metastore
//! entry. D3 lands the surface against a DT_STREAM-backed storage
//! instead (capacity-sized to fit one upload, single push, single
//! read at offset 0) — DT_STREAM works against a stock `Kernel::new()`
//! without a separately-wired CAS backend, so the adapter ships with
//! end-to-end test coverage today. The shape is upgrade-compatible:
//! when an in-process CAS-or-PathLocal ObjectStore lands for the
//! `/media` mount, the storage primitive flips from DT_STREAM to
//! DT_FILE without any change to the HTTP surface.
//!
//! Thumbnails are a Matrix optional feature; the spec lets the server
//! return the original asset when it does not generate thumbnails.
//! D3 takes that path — proper thumbnailing (libvips / image-rs) is a
//! future concern not load-bearing for the chat surface.

use std::sync::Arc;

use axum::body::Bytes;
use axum::extract::{Extension, Path, State};
use axum::http::{header, HeaderMap, HeaderValue, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde_json::{json, Value};
use uuid::Uuid;

use crate::matrix_adapter::auth::AuthSession;
use crate::matrix_adapter::error::AdapterError;
use crate::matrix_adapter::router::AdapterState;

/// Hard upper bound on a single upload to keep memory bounded; matches
/// the Matrix spec's recommended 50 MB ceiling closely enough for the
/// chat surface without forcing a streaming upload path on D3.
const MAX_UPLOAD_BYTES: usize = 50 * 1024 * 1024;

fn require_kernel<K: kernel::abi::KernelAbi>(
    state: &AdapterState<K>,
) -> Result<&Arc<K>, AdapterError> {
    state
        .kernel
        .as_ref()
        .ok_or_else(|| AdapterError::Internal("matrix adapter has no kernel handle wired".into()))
}

/// `POST /_matrix/media/v3/upload` — store the request body at
/// `/media/{media_id}` and return the resulting `mxc://` URI. The
/// caller's `Content-Type` (best-effort) is stamped onto the
/// metastore entry so `/download` can echo it back.
pub async fn upload<K: kernel::abi::KernelAbi>(
    State(state): State<AdapterState<K>>,
    Extension(_session): Extension<AuthSession>,
    headers: HeaderMap,
    body: Bytes,
) -> Result<Json<Value>, AdapterError> {
    if body.is_empty() {
        return Err(AdapterError::BadJson("media upload body is empty".into()));
    }
    if body.len() > MAX_UPLOAD_BYTES {
        return Err(AdapterError::BadJson(format!(
            "media upload exceeds {} byte limit (got {})",
            MAX_UPLOAD_BYTES,
            body.len()
        )));
    }
    let kernel = require_kernel(&state)?;
    let mime_type = headers
        .get(header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("application/octet-stream")
        .to_string();

    let media_id = Uuid::new_v4().to_string();
    let media_path = format!("/media/{media_id}");
    let server_name = state.server_name.clone();

    let kernel_for_write = Arc::clone(kernel);
    let media_path_for_write = media_path.clone();
    let mime_for_write = mime_type.clone();
    let bytes = body.to_vec();
    let request_id = format!("upload-{media_id}");
    let payload_len = bytes.len();
    tokio::task::spawn_blocking(move || -> Result<(), AdapterError> {
        // System-context write: media uploads are persisted by the
        // adapter on behalf of the caller; the caller's identity is
        // recorded by the audit hook on the way through, but the
        // file itself is not chat envelope and so does not need
        // MailboxStampingHook.
        let ctx = kernel::kernel::OperationContext {
            user_id: "matrix-adapter".into(),
            zone_id: "root".into(),
            is_admin: false,
            agent_id: Some("matrix-adapter".into()),
            is_system: true,
            groups: vec![],
            admin_capabilities: vec![],
            subject_type: "service".into(),
            subject_id: None,
            request_id,
            context_zone_id: None,
            zone_perms: vec![],
        };
        // Plant a DT_STREAM at the media path big enough to hold the
        // upload, then push the bytes as a single entry. DT_STREAM is
        // the temporary storage primitive (see module doc); the HTTP
        // surface is upgrade-compatible with a future DT_FILE + CAS
        // backing.
        const DT_STREAM: i32 = 4;
        let capacity = payload_len.next_power_of_two().max(4096);
        kernel_for_write
            .sys_setattr(
                &media_path_for_write,
                DT_STREAM,
                /* backend_name */ "",
                /* backend */ None,
                /* metastore */ None,
                /* raft_backend */ None,
                /* io_profile */ "memory",
                /* zone_id */ "root",
                /* is_external */ false,
                capacity,
                /* read_fd */ None,
                /* write_fd */ None,
                /* mime_type */ None,
                /* modified_at_ms */ None,
                /* link_target */ None,
                /* source */ None,
                /* remote_metastore */ None,
            )
            .map_err(|e| {
                AdapterError::Internal(format!("sys_setattr DT_STREAM({media_path_for_write}): {e:?}"))
            })?;
        kernel_for_write
            .sys_write(&media_path_for_write, &ctx, &bytes, 0)
            .map_err(|e| {
                AdapterError::Internal(format!("sys_write({media_path_for_write}): {e:?}"))
            })?;
        // Stamp mime_type via the UPDATE arm (entry_type=0) on the
        // same path so /download can echo it back as Content-Type.
        kernel_for_write
            .sys_setattr(
                &media_path_for_write,
                /* entry_type */ 0,
                /* backend_name */ "",
                /* backend */ None,
                /* metastore */ None,
                /* raft_backend */ None,
                /* io_profile */ "memory",
                /* zone_id */ "root",
                /* is_external */ false,
                /* capacity */ 0,
                /* read_fd */ None,
                /* write_fd */ None,
                /* mime_type */ Some(&mime_for_write),
                /* modified_at_ms */ None,
                /* link_target */ None,
                /* source */ None,
                /* remote_metastore */ None,
            )
            .map_err(|e| {
                AdapterError::Internal(format!(
                    "sys_setattr UPDATE mime_type({media_path_for_write}): {e:?}"
                ))
            })?;
        Ok(())
    })
    .await
    .map_err(|e| AdapterError::Internal(format!("spawn_blocking join: {e}")))??;

    Ok(Json(json!({
        "content_uri": format!("mxc://{}/{}", server_name, media_id),
    })))
}

/// `GET /_matrix/media/v3/download/{server}/{media_id}` — fetch the
/// stored bytes. Matrix lets a homeserver serve media for any
/// `server_name`; D3 only serves the local one and returns
/// `M_BAD_JSON` for cross-server requests (federation media is a
/// future concern).
pub async fn download<K: kernel::abi::KernelAbi>(
    State(state): State<AdapterState<K>>,
    Extension(_session): Extension<AuthSession>,
    Path((server, media_id)): Path<(String, String)>,
) -> Result<Response, AdapterError> {
    if server != *state.server_name {
        return Err(AdapterError::BadJson(format!(
            "media server {server:?} != adapter homeserver {:?}; cross-server media is not implemented",
            state.server_name
        )));
    }
    let kernel = require_kernel(&state)?;
    let media_path = format!("/media/{media_id}");
    let kernel_for_read = Arc::clone(kernel);
    let media_path_for_read = media_path.clone();
    let (bytes, mime_type) = tokio::task::spawn_blocking(
        move || -> Result<(Vec<u8>, String), AdapterError> {
            let ctx = kernel::kernel::OperationContext {
                user_id: "matrix-adapter".into(),
                zone_id: "root".into(),
                is_admin: false,
                agent_id: Some("matrix-adapter".into()),
                is_system: true,
                groups: vec![],
                admin_capabilities: vec![],
                subject_type: "service".into(),
                subject_id: None,
                request_id: "download".into(),
                context_zone_id: None,
                zone_perms: vec![],
            };
            let read_result = kernel_for_read.sys_read(
                &media_path_for_read,
                &ctx,
                /* timeout_ms */ 0,
                0,
            );
            let read = match read_result {
                Ok(r) => r,
                Err(kernel::kernel::KernelError::FileNotFound(_)) => {
                    return Err(AdapterError::Forbidden(format!(
                        "media {media_path_for_read} not found"
                    )));
                }
                Err(e) => {
                    return Err(AdapterError::Internal(format!(
                        "sys_read({media_path_for_read}): {e:?}"
                    )));
                }
            };
            let bytes = read.data.ok_or_else(|| {
                AdapterError::Forbidden(format!("media {media_path_for_read} not found"))
            })?;
            // Mime type lives on the metastore entry; pull it back via
            // sys_stat. StatResult.mime_type is `String` (empty when
            // unset), so fall back to the standard binary default.
            let mime = kernel_for_read
                .sys_stat(&media_path_for_read, /* zone_id */ "root")
                .map(|s| s.mime_type)
                .filter(|m| !m.is_empty())
                .unwrap_or_else(|| "application/octet-stream".to_string());
            Ok((bytes, mime))
        },
    )
    .await
    .map_err(|e| AdapterError::Internal(format!("spawn_blocking join: {e}")))??;

    let mut headers = HeaderMap::new();
    headers.insert(
        header::CONTENT_TYPE,
        HeaderValue::from_str(&mime_type)
            .unwrap_or_else(|_| HeaderValue::from_static("application/octet-stream")),
    );
    Ok((StatusCode::OK, headers, bytes).into_response())
}

/// `GET /_matrix/media/v3/thumbnail/{server}/{media_id}` — D3 returns
/// the original asset. Matrix spec permits this when the homeserver
/// does not generate thumbnails.
pub async fn thumbnail<K: kernel::abi::KernelAbi>(
    state: State<AdapterState<K>>,
    session: Extension<AuthSession>,
    path: Path<(String, String)>,
) -> Result<Response, AdapterError> {
    download(state, session, path).await
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::matrix_adapter::auth::stub::StubAuthBackend;
    use crate::matrix_adapter::router::{build_router, AdapterState};
    use axum::body::{to_bytes, Body};
    use axum::http::{Method, Request, StatusCode};
    use kernel::kernel::Kernel;
    use std::sync::OnceLock;
    use tower::ServiceExt;

    const SERVER: &str = "nexus.local";

    fn shared_media_kernel() -> Arc<Kernel> {
        static SHARED: OnceLock<Arc<Kernel>> = OnceLock::new();
        SHARED
            .get_or_init(|| {
                let k = Arc::new(Kernel::new());
                k.vfs_router_arc().add_mount("/media", "root", None, false);
                k
            })
            .clone()
    }

    fn fixture(seed_users: &[(&str, &str)]) -> (Arc<Kernel>, axum::Router) {
        let kernel = shared_media_kernel();
        let backend = Arc::new(StubAuthBackend::new(SERVER));
        for (user, pw) in seed_users {
            backend.add_user(user, pw);
        }
        let state = AdapterState::new(backend, Arc::from(SERVER), Some(Arc::clone(&kernel)));
        (kernel, build_router(state))
    }

    async fn login_and_get_token(app: &axum::Router) -> String {
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
        assert_eq!(resp.status(), StatusCode::OK);
        let v: Value =
            serde_json::from_slice(&to_bytes(resp.into_body(), 64 * 1024).await.unwrap())
                .unwrap();
        v["access_token"].as_str().unwrap().to_string()
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn upload_returns_mxc_uri_and_persists_bytes() {
        let (_kernel, app) = fixture(&[("ethan", "hunter2")]);
        let token = login_and_get_token(&app).await;

        let payload = b"fake-image-bytes";
        let req = Request::builder()
            .method("POST")
            .uri("/_matrix/media/v3/upload")
            .header(header::AUTHORIZATION, format!("Bearer {token}"))
            .header(header::CONTENT_TYPE, "image/png")
            .body(Body::from(payload.to_vec()))
            .unwrap();
        let resp = app.clone().oneshot(req).await.unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let body: Value =
            serde_json::from_slice(&to_bytes(resp.into_body(), 64 * 1024).await.unwrap())
                .unwrap();
        let content_uri = body["content_uri"].as_str().unwrap().to_string();
        assert!(content_uri.starts_with(&format!("mxc://{SERVER}/")));
        let media_id = content_uri
            .strip_prefix(&format!("mxc://{SERVER}/"))
            .unwrap()
            .to_string();

        // Download the same media id and verify bytes + mime survived.
        let dl_uri = format!("/_matrix/media/v3/download/{SERVER}/{media_id}");
        let req = Request::builder()
            .method("GET")
            .uri(&dl_uri)
            .header(header::AUTHORIZATION, format!("Bearer {token}"))
            .body(Body::empty())
            .unwrap();
        let resp = app.clone().oneshot(req).await.unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        assert_eq!(
            resp.headers()
                .get(header::CONTENT_TYPE)
                .unwrap()
                .to_str()
                .unwrap(),
            "image/png"
        );
        let bytes = to_bytes(resp.into_body(), 1024 * 1024).await.unwrap();
        assert_eq!(bytes.as_ref(), payload);
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn upload_rejects_empty_body() {
        let (_kernel, app) = fixture(&[("ethan", "hunter2")]);
        let token = login_and_get_token(&app).await;
        let req = Request::builder()
            .method("POST")
            .uri("/_matrix/media/v3/upload")
            .header(header::AUTHORIZATION, format!("Bearer {token}"))
            .header(header::CONTENT_TYPE, "application/octet-stream")
            .body(Body::empty())
            .unwrap();
        let resp = app.oneshot(req).await.unwrap();
        assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn download_unknown_media_returns_forbidden() {
        let (_kernel, app) = fixture(&[("ethan", "hunter2")]);
        let token = login_and_get_token(&app).await;
        let req = Request::builder()
            .method("GET")
            .uri(&format!("/_matrix/media/v3/download/{SERVER}/no-such-id"))
            .header(header::AUTHORIZATION, format!("Bearer {token}"))
            .body(Body::empty())
            .unwrap();
        let resp = app.oneshot(req).await.unwrap();
        assert_eq!(resp.status(), StatusCode::FORBIDDEN);
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn download_cross_server_media_rejected() {
        let (_kernel, app) = fixture(&[("ethan", "hunter2")]);
        let token = login_and_get_token(&app).await;
        let req = Request::builder()
            .method("GET")
            .uri("/_matrix/media/v3/download/other.example/some-id")
            .header(header::AUTHORIZATION, format!("Bearer {token}"))
            .body(Body::empty())
            .unwrap();
        let resp = app.oneshot(req).await.unwrap();
        assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn thumbnail_falls_back_to_original_bytes() {
        let (_kernel, app) = fixture(&[("ethan", "hunter2")]);
        let token = login_and_get_token(&app).await;

        let payload = b"image-payload-x";
        let req = Request::builder()
            .method("POST")
            .uri("/_matrix/media/v3/upload")
            .header(header::AUTHORIZATION, format!("Bearer {token}"))
            .header(header::CONTENT_TYPE, "image/jpeg")
            .body(Body::from(payload.to_vec()))
            .unwrap();
        let resp = app.clone().oneshot(req).await.unwrap();
        let v: Value =
            serde_json::from_slice(&to_bytes(resp.into_body(), 64 * 1024).await.unwrap())
                .unwrap();
        let content_uri = v["content_uri"].as_str().unwrap().to_string();
        let media_id = content_uri.split('/').next_back().unwrap().to_string();

        let req = Request::builder()
            .method("GET")
            .uri(&format!(
                "/_matrix/media/v3/thumbnail/{SERVER}/{media_id}?width=32&height=32&method=scale"
            ))
            .header(header::AUTHORIZATION, format!("Bearer {token}"))
            .body(Body::empty())
            .unwrap();
        let resp = app.oneshot(req).await.unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let bytes = to_bytes(resp.into_body(), 1024 * 1024).await.unwrap();
        assert_eq!(bytes.as_ref(), payload);
    }
}
