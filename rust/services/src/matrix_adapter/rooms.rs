//! Room read/write endpoints — the Matrix C-S surface that drives a
//! chat client's main loop. Per integration doc §4.2:
//!
//!   * `GET  /_matrix/client/v3/rooms/{rid}/state`             — room state snapshot
//!   * `GET  /_matrix/client/v3/rooms/{rid}/state/{type}/{key}` — single state event
//!   * `GET  /_matrix/client/v3/rooms/{rid}/messages`           — back-paginate timeline
//!   * `GET  /_matrix/client/v3/rooms/{rid}/joined_members`     — ReBAC-derived membership
//!   * `PUT  /_matrix/client/v3/rooms/{rid}/send/{event_type}/{txn_id}` — append message
//!   * `POST /_matrix/client/v3/rooms/{rid}/join`               — ReBAC grant (stub at D2)
//!   * `POST /_matrix/client/v3/rooms/{rid}/leave`              — ReBAC revoke (stub at D2)
//!   * `POST /_matrix/client/v3/createRoom`                     — bind new chat-with-me
//!
//! Read endpoints walk the chat-with-me DT_STREAM through
//! `kernel.sys_read`; write endpoints go through `kernel.sys_write` so
//! `MailboxStampingHook` rewrites the envelope's `from` field. Room
//! state is synthesised from the path + (D3) ReBAC membership rather
//! than persisted as Matrix state events — DT_STREAM linear order is
//! the SSOT, so the adapter renders Matrix's DAG-shaped state on read
//! from in-memory facts.

use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use axum::extract::{Extension, Path, Query, State};
use axum::Json;
use serde::Deserialize;
use serde_json::{json, Value};

use crate::matrix_adapter::auth::AuthSession;
use crate::matrix_adapter::error::AdapterError;
use crate::matrix_adapter::pdu::chat_envelope_to_pdu_event;
use crate::matrix_adapter::pdu::pdu_send_to_chat_envelope;
use crate::matrix_adapter::room_id::{decode_room_id, encode_room_id};
use crate::matrix_adapter::router::AdapterState;

// ── shared helpers ─────────────────────────────────────────────────

/// Build an `OperationContext` from the resolved Matrix session so
/// every kernel syscall the adapter issues carries the authenticated
/// identity. `agent_id = user_id` so MailboxStampingHook stamps the
/// Matrix user as the envelope `from` on `/send`.
fn ctx_from_session(session: &AuthSession) -> kernel::kernel::OperationContext {
    kernel::kernel::OperationContext {
        user_id: session.user_id.clone(),
        zone_id: "root".into(),
        is_admin: false,
        agent_id: Some(session.user_id.clone()),
        is_system: false,
        groups: vec![],
        admin_capabilities: vec![],
        subject_type: "user".into(),
        subject_id: None,
        request_id: format!("matrix-{}", session.access_token),
        context_zone_id: None,
    }
}

fn now_ms() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or(0)
}

fn require_kernel(state: &AdapterState) -> Result<&Arc<kernel::kernel::Kernel>, AdapterError> {
    state
        .kernel
        .as_ref()
        .ok_or_else(|| AdapterError::Internal("matrix adapter has no kernel handle wired".into()))
}

// ── read endpoints ─────────────────────────────────────────────────

/// `GET /_matrix/client/v3/rooms/{rid}/state` — return the synthesised
/// state events. The stream path round-trips through the room id, and
/// we synthesise a minimal `m.room.create` + `m.room.member` set so
/// stock clients see a sensible room. ReBAC-derived membership is a
/// D3 follow-up; today the requesting user is reported as joined.
pub async fn room_state(
    State(state): State<AdapterState>,
    Extension(session): Extension<AuthSession>,
    Path(room_id): Path<String>,
) -> Result<Json<Value>, AdapterError> {
    let stream_path = decode_room_id(&room_id, &state.server_name)?;
    let events = synth_state_events(&stream_path, &state.server_name, &session.user_id);
    Ok(Json(Value::Array(events)))
}

/// `GET /_matrix/client/v3/rooms/{rid}/state/{event_type}/{state_key}`
/// — single state event. Same synthesis as `room_state`, filtered by
/// `event_type` + `state_key`.
pub async fn room_state_event(
    State(state): State<AdapterState>,
    Extension(session): Extension<AuthSession>,
    Path((room_id, event_type, state_key)): Path<(String, String, String)>,
) -> Result<Json<Value>, AdapterError> {
    let stream_path = decode_room_id(&room_id, &state.server_name)?;
    let events = synth_state_events(&stream_path, &state.server_name, &session.user_id);
    let hit = events.into_iter().find(|ev| {
        ev.get("type").and_then(|v| v.as_str()) == Some(event_type.as_str())
            && ev.get("state_key").and_then(|v| v.as_str()) == Some(state_key.as_str())
    });
    match hit {
        Some(ev) => Ok(Json(ev["content"].clone())),
        None => Err(AdapterError::Forbidden(format!(
            "no state event of type {event_type:?} with key {state_key:?}"
        ))),
    }
}

#[derive(Debug, Deserialize)]
pub struct MessagesQuery {
    /// Pagination direction — `b` for back, `f` for forward.
    /// Matrix clients send `dir=b` for the canonical "load older
    /// history" path, `dir=f` for live tail.
    #[serde(default = "default_dir")]
    pub dir: String,
    /// Stream offset to start from, encoded as a decimal string. None
    /// means "start at the beginning" for `dir=f` and "start at the
    /// stream tail" for `dir=b`. Matrix clients echo whatever
    /// adapter-emitted token they last saw.
    pub from: Option<String>,
    /// Maximum events to return. Matrix default is 10, capped at
    /// 1000; the adapter caps at 100 to keep response size bounded.
    pub limit: Option<usize>,
}

fn default_dir() -> String {
    "b".into()
}

/// `GET /_matrix/client/v3/rooms/{rid}/messages` — paginate stream
/// entries as Matrix events. D2 walks from `offset = from` forward;
/// `dir=b` is honoured by reading the same range and reversing the
/// chunk so the client sees newest-first. Real backwards seek lands
/// in D3 alongside `/sync` once the stream offsets surface a "tail"
/// query.
pub async fn room_messages(
    State(state): State<AdapterState>,
    Extension(session): Extension<AuthSession>,
    Path(room_id): Path<String>,
    Query(query): Query<MessagesQuery>,
) -> Result<Json<Value>, AdapterError> {
    let stream_path = decode_room_id(&room_id, &state.server_name)?;
    let kernel = require_kernel(&state)?;
    let ctx = ctx_from_session(&session);

    let limit = query.limit.unwrap_or(10).min(100);
    let start_offset: u64 = query
        .from
        .as_deref()
        .and_then(|s| s.parse::<u64>().ok())
        .unwrap_or(0);

    // kernel.sys_read is synchronous and may take the VFS lock /
    // touch raft state; wrap in spawn_blocking so we don't pin an
    // axum executor thread.
    let kernel_for_read = Arc::clone(kernel);
    let stream_path_for_read = stream_path.clone();
    let server_name_for_read = state.server_name.clone();
    let chunk_result: Result<(Vec<Value>, u64), AdapterError> =
        tokio::task::spawn_blocking(move || {
            let mut chunk: Vec<Value> = Vec::with_capacity(limit);
            let mut next_offset = start_offset;
            while chunk.len() < limit {
                let read = kernel_for_read
                    .sys_read(&stream_path_for_read, &ctx, 0, next_offset)
                    .map_err(|e| {
                        AdapterError::Internal(format!(
                            "sys_read({stream_path_for_read}): {e:?}"
                        ))
                    })?;
                let bytes = match read.data {
                    Some(b) => b,
                    None => break, // no more entries; stream tail.
                };
                let event_offset = next_offset;
                let event = chat_envelope_to_pdu_event(
                    &stream_path_for_read,
                    &server_name_for_read,
                    event_offset,
                    &bytes,
                )?;
                chunk.push(event);
                match read.stream_next_offset {
                    Some(n) if (n as u64) > next_offset => next_offset = n as u64,
                    _ => break,
                }
            }
            Ok((chunk, next_offset))
        })
        .await
        .map_err(|e| AdapterError::Internal(format!("spawn_blocking join: {e}")))?;

    let (mut chunk, next_offset) = chunk_result?;
    if query.dir == "b" {
        chunk.reverse();
    }

    Ok(Json(json!({
        "start": start_offset.to_string(),
        "end": next_offset.to_string(),
        "chunk": chunk,
    })))
}

/// `GET /_matrix/client/v3/rooms/{rid}/joined_members` — D2 returns
/// the requesting user only. D3 derives this from ReBAC.
pub async fn joined_members(
    State(state): State<AdapterState>,
    Extension(session): Extension<AuthSession>,
    Path(room_id): Path<String>,
) -> Result<Json<Value>, AdapterError> {
    let _ = decode_room_id(&room_id, &state.server_name)?;
    Ok(Json(json!({
        "joined": {
            session.user_id.clone(): {
                "display_name": session.user_id.clone(),
                "avatar_url": Value::Null,
            },
        },
    })))
}

// ── write endpoints ────────────────────────────────────────────────

/// `PUT /_matrix/client/v3/rooms/{rid}/send/{event_type}/{txn_id}` —
/// append the message to the chat-with-me DT_STREAM. The kernel's
/// MailboxStampingHook rewrites `from` from OperationContext, so the
/// adapter cannot forge sender even if the client's PDU body claims
/// otherwise.
pub async fn room_send(
    State(state): State<AdapterState>,
    Extension(session): Extension<AuthSession>,
    Path((room_id, _event_type, _txn_id)): Path<(String, String, String)>,
    Json(content): Json<Value>,
) -> Result<Json<Value>, AdapterError> {
    let stream_path = decode_room_id(&room_id, &state.server_name)?;
    let kernel = require_kernel(&state)?;
    let ctx = ctx_from_session(&session);

    let envelope = pdu_send_to_chat_envelope(&content, now_ms())?;
    let envelope_len = envelope.len();
    // kernel.sys_write is synchronous (VFS lock + dispatch + raft);
    // wrap in spawn_blocking to keep the axum executor unblocked.
    let kernel_for_write = Arc::clone(kernel);
    let stream_path_for_write = stream_path.clone();
    let result: kernel::kernel::SysWriteResult = tokio::task::spawn_blocking(move || {
        kernel_for_write
            .sys_write(&stream_path_for_write, &ctx, &envelope, 0)
            .map_err(|e| {
                AdapterError::Internal(format!("sys_write({stream_path_for_write}): {e:?}"))
            })
    })
    .await
    .map_err(|e| AdapterError::Internal(format!("spawn_blocking join: {e}")))??;

    // Stream offset before the append is `result.size - envelope.len()`;
    // the kernel's stream_manager push returns the post-append cursor
    // in `size`. event_id mirrors what /messages will report on read,
    // keyed off the offset where the entry begins so cross-restart
    // lookups stay stable.
    let offset_after = result.size;
    let entry_offset = offset_after.saturating_sub(envelope_len as u64);
    let event_id = format!("$offset_{entry_offset}:{}", state.server_name);
    Ok(Json(json!({"event_id": event_id})))
}

#[derive(Debug, Deserialize)]
pub struct CreateRoomRequest {
    /// Bind the new room to `/agents/{name}/chat-with-me` — `name`
    /// matches the agent profile name (e.g. `human-bob`). D2 keeps
    /// the surface narrow; richer create flags (`preset`,
    /// `room_version`, `topic`, …) come later.
    pub name: String,
}

/// `POST /_matrix/client/v3/createRoom` — bind a new
/// `/agents/{name}/chat-with-me` DT_STREAM. The Matrix client sees a
/// fresh room_id; subsequent `/send` writes round-trip through the
/// path-based codec.
pub async fn create_room(
    State(state): State<AdapterState>,
    Extension(_session): Extension<AuthSession>,
    Json(req): Json<CreateRoomRequest>,
) -> Result<Json<Value>, AdapterError> {
    if req.name.is_empty() {
        return Err(AdapterError::BadJson("createRoom: name is required".into()));
    }
    let kernel = require_kernel(&state)?;
    let stream_path = format!("/agents/{}/chat-with-me", req.name);
    // io_profile=memory at D2 — D3 picks wal when federation is up,
    // matching managed_agent::proc_entry::chat_stream_profile.
    let kernel_for_create = Arc::clone(kernel);
    let stream_path_for_create = stream_path.clone();
    tokio::task::spawn_blocking(move || create_chat_stream(&kernel_for_create, &stream_path_for_create))
        .await
        .map_err(|e| AdapterError::Internal(format!("spawn_blocking join: {e}")))??;

    let room_id = encode_room_id(&stream_path, &state.server_name);
    Ok(Json(json!({"room_id": room_id})))
}

/// `POST /_matrix/client/v3/rooms/{rid}/join` — D2 stub. The
/// authenticated user is treated as joined for the duration of the
/// session; D3 wires ReBAC mutations.
pub async fn room_join(
    State(state): State<AdapterState>,
    Extension(_session): Extension<AuthSession>,
    Path(room_id): Path<String>,
) -> Result<Json<Value>, AdapterError> {
    // Validate the room id parses; we don't persist anything yet.
    let _ = decode_room_id(&room_id, &state.server_name)?;
    Ok(Json(json!({"room_id": room_id})))
}

/// `POST /_matrix/client/v3/rooms/{rid}/leave` — D2 stub. ReBAC
/// revocation is a D3 follow-up.
pub async fn room_leave(
    State(state): State<AdapterState>,
    Extension(_session): Extension<AuthSession>,
    Path(room_id): Path<String>,
) -> Result<Json<Value>, AdapterError> {
    let _ = decode_room_id(&room_id, &state.server_name)?;
    Ok(Json(json!({})))
}

// ── internal helpers ───────────────────────────────────────────────

/// Synthesise the minimum Matrix room state stock clients need to
/// render a usable room: `m.room.create` (room version + creator) and
/// one `m.room.member` for the requesting user. Pure function of the
/// stream path + the resolved Matrix user; no kernel call.
fn synth_state_events(stream_path: &str, server_name: &str, user_id: &str) -> Vec<Value> {
    let room_id = encode_room_id(stream_path, server_name);
    let create_event_id = format!("$create:{server_name}");
    let member_event_id = format!("$member-{user_id}:{server_name}");
    vec![
        json!({
            "event_id": create_event_id,
            "type": "m.room.create",
            "room_id": room_id,
            "sender": user_id,
            "state_key": "",
            "content": {
                "creator": user_id,
                "room_version": "10",
            },
        }),
        json!({
            "event_id": member_event_id,
            "type": "m.room.member",
            "room_id": room_id,
            "sender": user_id,
            "state_key": user_id,
            "content": {
                "membership": "join",
                "displayname": user_id,
            },
        }),
    ]
}

/// Plant the canonical chat-with-me DT_STREAM at `path`. Mirrors
/// `services::managed_agent::proc_entry::create_dt_stream` — the two
/// callers (managed-agent spawn vs Matrix createRoom) share the same
/// DT_STREAM contract, just at different paths.
fn create_chat_stream(
    kernel: &std::sync::Arc<kernel::kernel::Kernel>,
    path: &str,
) -> Result<(), AdapterError> {
    const DT_STREAM: i32 = 4;
    const CAPACITY: usize = 65_536;
    kernel
        .sys_setattr(
            path,
            DT_STREAM,
            "",
            None,
            None,
            None,
            "memory",
            "root",
            false,
            CAPACITY,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        )
        .map(|_| ())
        .map_err(|e| AdapterError::Internal(format!("createRoom sys_setattr({path}): {e:?}")))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::matrix_adapter::auth::stub::StubAuthBackend;
    use crate::matrix_adapter::router::build_router;
    use axum::body::{to_bytes, Body};
    use axum::http::{header, Method, Request, StatusCode};
    use kernel::kernel::Kernel;
    use std::sync::Arc;
    use tower::ServiceExt;

    const SERVER: &str = "nexus.local";

    /// Shared kernel for the rooms test suite. Lives across all tests
    /// in this module so the `Arc<Kernel>` is never dropped inside an
    /// async test body — `Kernel::drop` shuts down its inner tokio
    /// runtime which panics under `#[tokio::test]`. Each test uses a
    /// unique room/path key to avoid state leakage.
    fn shared_kernel() -> Arc<Kernel> {
        use std::sync::OnceLock;
        static SHARED: OnceLock<Arc<Kernel>> = OnceLock::new();
        SHARED
            .get_or_init(|| {
                let k = Arc::new(Kernel::new());
                k.vfs_router_arc().add_mount("/agents", "root", None, false);
                k.vfs_router_arc().add_mount("/proc", "root", None, false);
                k.register_native_hook(Box::new(
                    crate::managed_agent::mailbox_stamping_hook::MailboxStampingHook::new(),
                ));
                k
            })
            .clone()
    }

    fn fixture_with_kernel(seed_users: &[(&str, &str)]) -> (Arc<Kernel>, axum::Router) {
        let kernel = shared_kernel();
        let backend = Arc::new(StubAuthBackend::new(SERVER));
        for (user, pw) in seed_users {
            backend.add_user(user, pw);
        }
        let state = AdapterState {
            auth: backend,
            server_name: Arc::from(SERVER),
            kernel: Some(Arc::clone(&kernel)),
        };
        (kernel, build_router(state))
    }

    async fn login_and_get_token(app: &axum::Router, user: &str, password: &str) -> String {
        let payload = serde_json::json!({
            "type": "m.login.password",
            "identifier": {"type": "m.id.user", "user": user},
            "password": password,
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
        let bytes = to_bytes(resp.into_body(), 64 * 1024).await.unwrap();
        let v: Value = serde_json::from_slice(&bytes).unwrap();
        v["access_token"].as_str().unwrap().to_string()
    }

    async fn json_request(
        app: &axum::Router,
        method: Method,
        uri: &str,
        token: &str,
        body: Option<Value>,
    ) -> (StatusCode, Value) {
        let mut builder = Request::builder()
            .method(method)
            .uri(uri)
            .header(header::AUTHORIZATION, format!("Bearer {token}"));
        let body = match body {
            Some(v) => {
                builder = builder.header(header::CONTENT_TYPE, "application/json");
                Body::from(v.to_string())
            }
            None => Body::empty(),
        };
        let resp = app
            .clone()
            .oneshot(builder.body(body).unwrap())
            .await
            .unwrap();
        let status = resp.status();
        let bytes = to_bytes(resp.into_body(), 1024 * 1024).await.unwrap();
        let v = if bytes.is_empty() {
            Value::Null
        } else {
            serde_json::from_slice(&bytes).unwrap_or(Value::Null)
        };
        (status, v)
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn create_room_then_send_then_messages_round_trip() {
        let (_kernel, app) = fixture_with_kernel(&[("ethan", "hunter2")]);
        let token = login_and_get_token(&app, "ethan", "hunter2").await;

        // createRoom binds /agents/human-bob/chat-with-me.
        let (status, body) = json_request(
            &app,
            Method::POST,
            "/_matrix/client/v3/createRoom",
            &token,
            Some(serde_json::json!({"name": "human-bob"})),
        )
        .await;
        assert_eq!(status, StatusCode::OK);
        let room_id = body["room_id"].as_str().unwrap().to_string();
        assert_eq!(
            room_id,
            encode_room_id("/agents/human-bob/chat-with-me", SERVER)
        );

        // Send a message — kernel stamps `from` from the resolved
        // Matrix user id (`@ethan:nexus.local`).
        let send_uri = format!("/_matrix/client/v3/rooms/{room_id}/send/m.room.message/txn-1");
        let (status, body) = json_request(
            &app,
            Method::PUT,
            &send_uri,
            &token,
            Some(serde_json::json!({"body": "hi bob", "msgtype": "m.text"})),
        )
        .await;
        assert_eq!(status, StatusCode::OK);
        let event_id = body["event_id"].as_str().unwrap().to_string();
        assert!(event_id.starts_with('$'));

        // Read it back via /messages.
        let messages_uri = format!("/_matrix/client/v3/rooms/{room_id}/messages?dir=f&from=0&limit=10");
        let (status, body) = json_request(&app, Method::GET, &messages_uri, &token, None).await;
        assert_eq!(status, StatusCode::OK);
        let chunk = body["chunk"].as_array().unwrap();
        assert_eq!(chunk.len(), 1, "expected exactly one event");
        let event = &chunk[0];
        assert_eq!(event["type"], "m.room.message");
        assert_eq!(event["sender"], "@ethan:nexus.local");
        assert_eq!(event["content"]["body"], "hi bob");
        assert_eq!(event["content"]["msgtype"], "m.text");
        assert_eq!(event["room_id"], room_id);
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn room_send_rejects_invalid_room_id() {
        let (_kernel, app) = fixture_with_kernel(&[("ethan", "hunter2")]);
        let token = login_and_get_token(&app, "ethan", "hunter2").await;
        let (status, body) = json_request(
            &app,
            Method::PUT,
            "/_matrix/client/v3/rooms/!nope/send/m.room.message/txn-1",
            &token,
            Some(serde_json::json!({"body": "x", "msgtype": "m.text"})),
        )
        .await;
        assert_eq!(status, StatusCode::BAD_REQUEST);
        assert_eq!(body["errcode"], "M_BAD_JSON");
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn room_state_returns_synthesised_create_and_member_events() {
        let (_kernel, app) = fixture_with_kernel(&[("ethan", "hunter2")]);
        let token = login_and_get_token(&app, "ethan", "hunter2").await;
        let room_id = encode_room_id("/agents/human-bob/chat-with-me", SERVER);

        let uri = format!("/_matrix/client/v3/rooms/{room_id}/state");
        let (status, body) = json_request(&app, Method::GET, &uri, &token, None).await;
        assert_eq!(status, StatusCode::OK);
        let events = body.as_array().unwrap();
        assert_eq!(events.len(), 2);
        let types: Vec<&str> = events
            .iter()
            .map(|e| e["type"].as_str().unwrap())
            .collect();
        assert!(types.contains(&"m.room.create"));
        assert!(types.contains(&"m.room.member"));
        for ev in events {
            assert_eq!(ev["sender"], "@ethan:nexus.local");
            assert_eq!(ev["room_id"], room_id);
        }
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn room_state_event_filters_by_type_and_key() {
        let (_kernel, app) = fixture_with_kernel(&[("ethan", "hunter2")]);
        let token = login_and_get_token(&app, "ethan", "hunter2").await;
        let room_id = encode_room_id("/agents/human-bob/chat-with-me", SERVER);

        let uri = format!("/_matrix/client/v3/rooms/{room_id}/state/m.room.member/@ethan:nexus.local");
        let (status, body) = json_request(&app, Method::GET, &uri, &token, None).await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(body["membership"], "join");
        assert_eq!(body["displayname"], "@ethan:nexus.local");

        // Type that the synthesised state set never contains — handler
        // returns FORBIDDEN ("no state event of type ... with key ...").
        // Matrix's URL grammar requires a non-empty state_key segment
        // when present; axum's path extractor matches accordingly.
        let uri = format!("/_matrix/client/v3/rooms/{room_id}/state/m.room.topic/whatever");
        let (status, _body) = json_request(&app, Method::GET, &uri, &token, None).await;
        assert_eq!(status, StatusCode::FORBIDDEN);
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn joined_members_returns_resolving_user() {
        let (_kernel, app) = fixture_with_kernel(&[("ethan", "hunter2")]);
        let token = login_and_get_token(&app, "ethan", "hunter2").await;
        let room_id = encode_room_id("/agents/human-bob/chat-with-me", SERVER);

        let uri = format!("/_matrix/client/v3/rooms/{room_id}/joined_members");
        let (status, body) = json_request(&app, Method::GET, &uri, &token, None).await;
        assert_eq!(status, StatusCode::OK);
        let joined = body["joined"].as_object().unwrap();
        assert!(joined.contains_key("@ethan:nexus.local"));
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn join_and_leave_validate_room_id_but_dont_persist() {
        let (_kernel, app) = fixture_with_kernel(&[("ethan", "hunter2")]);
        let token = login_and_get_token(&app, "ethan", "hunter2").await;
        let room_id = encode_room_id("/agents/human-bob/chat-with-me", SERVER);

        let join_uri = format!("/_matrix/client/v3/rooms/{room_id}/join");
        let (status, body) = json_request(&app, Method::POST, &join_uri, &token, None).await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(body["room_id"], room_id);

        let leave_uri = format!("/_matrix/client/v3/rooms/{room_id}/leave");
        let (status, _) = json_request(&app, Method::POST, &leave_uri, &token, None).await;
        assert_eq!(status, StatusCode::OK);

        // Bogus room id is rejected at decode time.
        let (status, body) = json_request(
            &app,
            Method::POST,
            "/_matrix/client/v3/rooms/!nope/join",
            &token,
            None,
        )
        .await;
        assert_eq!(status, StatusCode::BAD_REQUEST);
        assert_eq!(body["errcode"], "M_BAD_JSON");
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn create_room_requires_name() {
        let (_kernel, app) = fixture_with_kernel(&[("ethan", "hunter2")]);
        let token = login_and_get_token(&app, "ethan", "hunter2").await;
        let (status, body) = json_request(
            &app,
            Method::POST,
            "/_matrix/client/v3/createRoom",
            &token,
            Some(serde_json::json!({"name": ""})),
        )
        .await;
        assert_eq!(status, StatusCode::BAD_REQUEST);
        assert_eq!(body["errcode"], "M_BAD_JSON");
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn messages_paginates_back_returns_newest_first() {
        let (_kernel, app) = fixture_with_kernel(&[("ethan", "hunter2")]);
        let token = login_and_get_token(&app, "ethan", "hunter2").await;
        let (_, body) = json_request(
            &app,
            Method::POST,
            "/_matrix/client/v3/createRoom",
            &token,
            Some(serde_json::json!({"name": "human-carol"})),
        )
        .await;
        let room_id = body["room_id"].as_str().unwrap().to_string();

        // Send three messages.
        for body_text in ["one", "two", "three"] {
            let send_uri =
                format!("/_matrix/client/v3/rooms/{room_id}/send/m.room.message/txn-{body_text}");
            let (status, _) = json_request(
                &app,
                Method::PUT,
                &send_uri,
                &token,
                Some(serde_json::json!({"body": body_text, "msgtype": "m.text"})),
            )
            .await;
            assert_eq!(status, StatusCode::OK);
        }

        // dir=b reverses chunk → newest first.
        let messages_uri =
            format!("/_matrix/client/v3/rooms/{room_id}/messages?dir=b&from=0&limit=10");
        let (status, body) = json_request(&app, Method::GET, &messages_uri, &token, None).await;
        assert_eq!(status, StatusCode::OK);
        let chunk = body["chunk"].as_array().unwrap();
        assert_eq!(chunk.len(), 3);
        let bodies: Vec<&str> = chunk
            .iter()
            .map(|e| e["content"]["body"].as_str().unwrap())
            .collect();
        assert_eq!(bodies, vec!["three", "two", "one"]);
    }
}
