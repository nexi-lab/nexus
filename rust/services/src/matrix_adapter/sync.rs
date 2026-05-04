//! `GET /_matrix/client/v3/sync` — long-poll endpoint that drives the
//! Matrix client's main loop. Per integration doc §4.2:
//!
//!   * Since-token = base64-encoded JSON `{stream_path: offset}` map,
//!     so cross-restart sync resumes from the same point.
//!   * Per-room timeline events come from `kernel.sys_read` at the
//!     stored offset.
//!   * If no rooms had new events and `timeout > 0`, register a
//!     `sys_watch` on every joined stream path; block until one fires
//!     or the timeout elapses. The kernel re-pumps via the existing
//!     `FileWatchRegistry` wakeup path.
//!
//! The adapter's only in-process state is the per-user joined-rooms
//! map (stream-path set per Matrix user) — same surface as the rest of
//! D3, no parallel SSOT outside the kernel.

use std::collections::HashMap;
use std::sync::Arc;
use std::time::Duration;

use axum::extract::{Extension, Query, State};
use axum::Json;
use serde::Deserialize;
use serde_json::{json, Value};

use crate::matrix_adapter::auth::AuthSession;
use crate::matrix_adapter::error::AdapterError;
use crate::matrix_adapter::pdu::chat_envelope_to_pdu_event;
use crate::matrix_adapter::room_id::encode_room_id;
use crate::matrix_adapter::router::AdapterState;

/// Query params for `GET /_matrix/client/v3/sync`. Matrix clients
/// echo `since` from the previous `next_batch`; `timeout` is in
/// milliseconds and `0` means "return immediately, even if empty".
#[derive(Debug, Deserialize, Default)]
pub struct SyncQuery {
    pub since: Option<String>,
    #[serde(default)]
    pub timeout: u64,
}

/// Hard cap on long-poll wait (Matrix spec recommends 30s, allows up
/// to ~10 min). The adapter caps at 30s so a misbehaving client can't
/// pin a thread forever.
const MAX_LONG_POLL_MS: u64 = 30_000;

pub async fn sync(
    State(state): State<AdapterState>,
    Extension(session): Extension<AuthSession>,
    Query(query): Query<SyncQuery>,
) -> Result<Json<Value>, AdapterError> {
    let kernel = require_kernel(&state)?;
    let server_name = state.server_name.clone();

    let mut offsets = decode_since(query.since.as_deref())?;

    let joined: Vec<String> = state
        .joined_rooms
        .read()
        .get(&session.user_id)
        .map(|set| set.iter().cloned().collect())
        .unwrap_or_default();

    let timeout_ms = query.timeout.min(MAX_LONG_POLL_MS);
    let ctx = super::rooms::ctx_from_session(&session);

    // First read pass — drains anything that's already past `since`.
    let mut rooms_with_events = pump_rooms(
        kernel,
        &joined,
        &mut offsets,
        &server_name,
        &ctx,
    )
    .await?;

    // No new events on any joined room AND the client asked us to
    // wait → poll-loop until something arrives or the timeout
    // elapses. The principled implementation here is `sys_watch`,
    // but the kernel's `FileWatchRegistry::wait_for_event` is
    // currently a stub returning `None` (see
    // `kernel/src/core/file_watch.rs`); polling is the pragmatic
    // bridge until that lands. Each iteration sleeps for a small
    // slice and re-pumps the joined-room read offsets.
    if rooms_with_events.is_empty() && timeout_ms > 0 && !joined.is_empty() {
        let deadline = tokio::time::Instant::now() + Duration::from_millis(timeout_ms);
        let slice = Duration::from_millis(50);
        loop {
            let now = tokio::time::Instant::now();
            if now >= deadline {
                break;
            }
            tokio::time::sleep(slice.min(deadline - now)).await;
            rooms_with_events =
                pump_rooms(kernel, &joined, &mut offsets, &server_name, &ctx).await?;
            if !rooms_with_events.is_empty() {
                break;
            }
        }
    }

    let next_batch = encode_since(&offsets);
    let rooms_join: serde_json::Map<String, Value> = rooms_with_events
        .into_iter()
        .map(|(stream_path, events)| {
            let room_id = encode_room_id(&stream_path, &server_name);
            (
                room_id,
                json!({
                    "timeline": {
                        "events": events,
                        "limited": false,
                        "prev_batch": offsets
                            .get(&stream_path)
                            .copied()
                            .unwrap_or(0)
                            .to_string(),
                    },
                }),
            )
        })
        .collect();

    Ok(Json(json!({
        "next_batch": next_batch,
        "rooms": {
            "join": Value::Object(rooms_join),
            "invite": {},
            "leave": {},
        },
    })))
}

// ── since-token codec ─────────────────────────────────────────────

/// Decode the client-supplied `since` token. None / empty / unparseable
/// tokens map to "start at offset 0 for every joined room" rather than
/// erroring — matches Matrix client behaviour after a fresh login or
/// session reset.
fn decode_since(since: Option<&str>) -> Result<HashMap<String, u64>, AdapterError> {
    let Some(token) = since.filter(|s| !s.is_empty()) else {
        return Ok(HashMap::new());
    };
    let decoded = base32::decode(base32::Alphabet::Rfc4648 { padding: true }, token)
        .ok_or_else(|| AdapterError::BadJson(format!("since token {token:?} is not valid base32")))?;
    serde_json::from_slice(&decoded)
        .map_err(|e| AdapterError::BadJson(format!("since token JSON: {e}")))
}

fn encode_since(offsets: &HashMap<String, u64>) -> String {
    let bytes = serde_json::to_vec(offsets).unwrap_or_default();
    base32::encode(base32::Alphabet::Rfc4648 { padding: true }, &bytes)
}

// ── room pump ─────────────────────────────────────────────────────

async fn pump_rooms(
    kernel: &Arc<kernel::kernel::Kernel>,
    joined: &[String],
    offsets: &mut HashMap<String, u64>,
    server_name: &str,
    ctx: &kernel::kernel::OperationContext,
) -> Result<Vec<(String, Vec<Value>)>, AdapterError> {
    let mut rooms_with_events: Vec<(String, Vec<Value>)> = Vec::new();
    for stream_path in joined {
        let start = offsets.get(stream_path).copied().unwrap_or(0);
        let kernel_for_read = Arc::clone(kernel);
        let stream_path_owned = stream_path.clone();
        let server_name_owned = server_name.to_string();
        let ctx_owned = ctx.clone();
        let pumped = tokio::task::spawn_blocking(move || {
            pump_one_room(
                &kernel_for_read,
                &stream_path_owned,
                &server_name_owned,
                start,
                &ctx_owned,
            )
        })
        .await
        .map_err(|e| AdapterError::Internal(format!("spawn_blocking join: {e}")))??;
        let (events, end) = pumped;
        if !events.is_empty() {
            rooms_with_events.push((stream_path.clone(), events));
        }
        if end != start {
            offsets.insert(stream_path.clone(), end);
        }
    }
    Ok(rooms_with_events)
}

fn pump_one_room(
    kernel: &Arc<kernel::kernel::Kernel>,
    stream_path: &str,
    server_name: &str,
    start: u64,
    ctx: &kernel::kernel::OperationContext,
) -> Result<(Vec<Value>, u64), AdapterError> {
    let mut events = Vec::new();
    let mut next_offset = start;
    // Cap one /sync's contribution from a single room to keep the
    // response bounded; the client paginates with /messages for older
    // history.
    const PER_ROOM_CAP: usize = 100;
    while events.len() < PER_ROOM_CAP {
        let read = kernel
            .sys_read(stream_path, ctx, /* timeout_ms */ 0, next_offset)
            .map_err(|e| AdapterError::Internal(format!("sys_read({stream_path}): {e:?}")))?;
        let Some(bytes) = read.data else {
            break;
        };
        let event = chat_envelope_to_pdu_event(stream_path, server_name, next_offset, &bytes)?;
        events.push(event);
        match read.stream_next_offset {
            Some(n) if (n as u64) > next_offset => next_offset = n as u64,
            _ => break,
        }
    }
    Ok((events, next_offset))
}

fn require_kernel(state: &AdapterState) -> Result<&Arc<kernel::kernel::Kernel>, AdapterError> {
    state
        .kernel
        .as_ref()
        .ok_or_else(|| AdapterError::Internal("matrix adapter has no kernel handle wired".into()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn since_codec_round_trips_offset_map() {
        let mut offsets = HashMap::new();
        offsets.insert("/agents/human-bob/chat-with-me".to_string(), 17_u64);
        offsets.insert("/proc/p_42/chat-with-me".to_string(), 99_u64);
        let token = encode_since(&offsets);
        let decoded = decode_since(Some(&token)).unwrap();
        assert_eq!(decoded, offsets);
    }

    #[test]
    fn since_decode_empty_returns_empty_map() {
        let decoded = decode_since(None).unwrap();
        assert!(decoded.is_empty());
        let decoded = decode_since(Some("")).unwrap();
        assert!(decoded.is_empty());
    }

    #[test]
    fn since_decode_rejects_bad_base32() {
        let err = decode_since(Some("not!base32!")).unwrap_err();
        assert!(matches!(err, AdapterError::BadJson(_)));
    }

    #[test]
    fn since_decode_rejects_non_json_payload() {
        let token = base32::encode(
            base32::Alphabet::Rfc4648 { padding: true },
            b"this is not json",
        );
        let err = decode_since(Some(&token)).unwrap_err();
        assert!(matches!(err, AdapterError::BadJson(_)));
    }

    // ── e2e tests against axum router + real kernel ───────────────

    use crate::matrix_adapter::auth::stub::StubAuthBackend;
    use crate::matrix_adapter::router::{build_router, AdapterState};
    use axum::body::{to_bytes, Body};
    use axum::http::{header, Method, Request, StatusCode};
    use kernel::kernel::Kernel;
    use tower::ServiceExt;

    const SERVER: &str = "nexus.local";

    fn shared_sync_kernel() -> Arc<Kernel> {
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

    fn sync_fixture(seed_users: &[(&str, &str)]) -> (Arc<Kernel>, axum::Router) {
        let kernel = shared_sync_kernel();
        let backend = Arc::new(StubAuthBackend::new(SERVER));
        for (user, pw) in seed_users {
            backend.add_user(user, pw);
        }
        let state = AdapterState::new(backend, Arc::from(SERVER), Some(Arc::clone(&kernel)));
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
    async fn sync_returns_empty_immediately_for_user_with_no_joined_rooms() {
        let (_kernel, app) = sync_fixture(&[("alice", "pw")]);
        let token = login_and_get_token(&app, "alice", "pw").await;
        let (status, body) = json_request(
            &app,
            Method::GET,
            "/_matrix/client/v3/sync?timeout=0",
            &token,
            None,
        )
        .await;
        assert_eq!(status, StatusCode::OK);
        assert!(body["next_batch"].as_str().is_some());
        assert!(body["rooms"]["join"].as_object().unwrap().is_empty());
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn sync_pumps_messages_for_joined_rooms_and_advances_token() {
        let (_kernel, app) = sync_fixture(&[("bob", "pw")]);
        let token = login_and_get_token(&app, "bob", "pw").await;

        // createRoom auto-joins bob.
        let (_, body) = json_request(
            &app,
            Method::POST,
            "/_matrix/client/v3/createRoom",
            &token,
            Some(serde_json::json!({"name": "sync-room-bob"})),
        )
        .await;
        let room_id = body["room_id"].as_str().unwrap().to_string();
        // Send three messages.
        for body_text in ["one", "two", "three"] {
            let send_uri = format!(
                "/_matrix/client/v3/rooms/{room_id}/send/m.room.message/txn-{body_text}"
            );
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

        // First sync — no `since`, returns all three.
        let (status, body) = json_request(
            &app,
            Method::GET,
            "/_matrix/client/v3/sync?timeout=0",
            &token,
            None,
        )
        .await;
        assert_eq!(status, StatusCode::OK);
        let join_map = body["rooms"]["join"].as_object().unwrap();
        let timeline = &join_map[&room_id]["timeline"]["events"]
            .as_array()
            .unwrap()
            .clone();
        assert_eq!(timeline.len(), 3);
        let bodies: Vec<&str> = timeline
            .iter()
            .map(|e| e["content"]["body"].as_str().unwrap())
            .collect();
        assert_eq!(bodies, vec!["one", "two", "three"]);
        let next_batch = body["next_batch"].as_str().unwrap().to_string();
        assert!(!next_batch.is_empty());

        // Second sync with the advancing token — empty timeline since
        // no new messages landed.
        let uri = format!("/_matrix/client/v3/sync?timeout=0&since={next_batch}");
        let (status, body) = json_request(&app, Method::GET, &uri, &token, None).await;
        assert_eq!(status, StatusCode::OK);
        // Empty timeline → the room is not in `join` at all (only
        // rooms with new events are surfaced).
        assert!(body["rooms"]["join"].as_object().unwrap().is_empty());
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn sync_long_polls_until_a_message_arrives() {
        let (_kernel, app) = sync_fixture(&[("carol", "pw")]);
        let token = login_and_get_token(&app, "carol", "pw").await;

        let (_, body) = json_request(
            &app,
            Method::POST,
            "/_matrix/client/v3/createRoom",
            &token,
            Some(serde_json::json!({"name": "sync-longpoll-carol"})),
        )
        .await;
        let room_id = body["room_id"].as_str().unwrap().to_string();
        // Drain whatever createRoom-time state exists.
        let (_, body) = json_request(
            &app,
            Method::GET,
            "/_matrix/client/v3/sync?timeout=0",
            &token,
            None,
        )
        .await;
        let since = body["next_batch"].as_str().unwrap().to_string();

        // Spawn the long-poll sync; it should block until the next
        // /send arrives.
        let app_for_sync = app.clone();
        let token_for_sync = token.clone();
        let since_for_sync = since.clone();
        let sync_handle = tokio::spawn(async move {
            let uri =
                format!("/_matrix/client/v3/sync?timeout=5000&since={since_for_sync}");
            json_request(&app_for_sync, Method::GET, &uri, &token_for_sync, None).await
        });

        // Give the long-poll a moment to register the watch, then
        // send a message.
        tokio::time::sleep(Duration::from_millis(200)).await;
        let send_uri = format!("/_matrix/client/v3/rooms/{room_id}/send/m.room.message/txn-late");
        let (send_status, _) = json_request(
            &app,
            Method::PUT,
            &send_uri,
            &token,
            Some(serde_json::json!({"body": "wake up", "msgtype": "m.text"})),
        )
        .await;
        assert_eq!(send_status, StatusCode::OK);

        let (sync_status, sync_body) = sync_handle.await.unwrap();
        assert_eq!(sync_status, StatusCode::OK);
        let join_map = sync_body["rooms"]["join"].as_object().unwrap();
        let timeline = &join_map[&room_id]["timeline"]["events"]
            .as_array()
            .unwrap()
            .clone();
        assert_eq!(timeline.len(), 1);
        assert_eq!(timeline[0]["content"]["body"], "wake up");
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn sync_long_poll_returns_after_timeout_when_no_messages_arrive() {
        let (_kernel, app) = sync_fixture(&[("dave", "pw")]);
        let token = login_and_get_token(&app, "dave", "pw").await;

        // No createRoom — dave has no joined rooms, so even with a
        // long timeout the call returns immediately because the
        // joined set is empty.
        let started = std::time::Instant::now();
        let (status, body) = json_request(
            &app,
            Method::GET,
            "/_matrix/client/v3/sync?timeout=300",
            &token,
            None,
        )
        .await;
        assert_eq!(status, StatusCode::OK);
        assert!(body["rooms"]["join"].as_object().unwrap().is_empty());
        // Empty-joined fast-path completes well under the timeout.
        assert!(started.elapsed() < Duration::from_millis(250));

        // With a joined room and no /send during the wait, the call
        // should block roughly the timeout and then return empty.
        let (_, body) = json_request(
            &app,
            Method::POST,
            "/_matrix/client/v3/createRoom",
            &token,
            Some(serde_json::json!({"name": "sync-empty-dave"})),
        )
        .await;
        let _room_id = body["room_id"].as_str().unwrap().to_string();

        let started = std::time::Instant::now();
        let (status, body) = json_request(
            &app,
            Method::GET,
            "/_matrix/client/v3/sync?timeout=300",
            &token,
            None,
        )
        .await;
        assert_eq!(status, StatusCode::OK);
        assert!(body["rooms"]["join"].as_object().unwrap().is_empty());
        let elapsed = started.elapsed();
        // Allow generous slack (CI scheduler jitter); the lower
        // bound proves the long-poll actually waited.
        assert!(
            elapsed >= Duration::from_millis(200),
            "long-poll should wait for at least most of the timeout, got {elapsed:?}"
        );
    }
}
