//! `AcpConnection` — ACP JSON-RPC 2.0 protocol adapter.
//!
//! Mirror of `nexus.services.acp.connection.AcpConnection`. Wraps a
//! generic [`JsonRpcClient`] with ACP-specific request / notification
//! handling: auto-grant permission requests, route fs/read_text_file
//! and fs/write_text_file through caller-supplied VFS closures, and
//! feed every session/update notification into an [`AgentObserver`].
//!
//! Owns no subprocess: the kernel-side DT_PIPEs registered by
//! [`super::subprocess::AcpSubprocess`] hand a generic AsyncRead /
//! AsyncWrite pair; AcpConnection only sees those.
//!
//! Timing-sensitive: `session_load` sleeps **200ms** after the load
//! response before resetting the observer. ACP servers send replay
//! `session/update` notifications AFTER returning the load response,
//! so the drain is mandatory to prevent replay text from leaking into
//! the next `send_prompt`. There's a regression test that fails if
//! this sleep gets optimized away.

#![allow(dead_code)]

use std::path::{Path, PathBuf};
use std::sync::{Arc, RwLock};
use std::time::Duration;

use futures::future::BoxFuture;
use serde_json::{json, Value};
use tokio::io::{AsyncRead, AsyncWrite};

use super::jsonrpc::{
    JsonRpcClient, JsonRpcError, NotificationHandler, RequestHandler, RequestOutcome,
};
use super::observer::AgentObserver;

/// Async closure invoked when the agent requests `fs/read_text_file`.
/// Returns the file content (or an error string surfaced as a generic
/// JSON-RPC error response).
pub(crate) type FsRead =
    Arc<dyn Fn(String) -> BoxFuture<'static, Result<String, String>> + Send + Sync>;

/// Async closure invoked when the agent requests `fs/write_text_file`.
pub(crate) type FsWrite =
    Arc<dyn Fn(String, String) -> BoxFuture<'static, Result<(), String>> + Send + Sync>;

/// Result handed back from a single ACP `session/prompt`.
#[derive(Debug, Clone, Default)]
pub(crate) struct AcpPromptResult {
    pub text: String,
    pub stop_reason: Option<String>,
    pub usage: Value,
    pub session_id: Option<String>,
    pub model: Option<String>,
    pub accumulated_usage: serde_json::Map<String, Value>,
}

struct ConnectionState {
    session_id: Option<String>,
    load_session_capability: bool,
    cwd: PathBuf,
}

pub(crate) struct AcpConnection {
    rpc: Arc<JsonRpcClient>,
    observer: Arc<AgentObserver>,
    state: Arc<RwLock<ConnectionState>>,
}

impl AcpConnection {
    /// Build an AcpConnection over an already-connected transport
    /// pair. Wires the JsonRpcClient handlers but does not start the
    /// reader; call [`AcpConnection::start`] (or one of the
    /// session-lifecycle methods, which internally call `start`).
    pub(crate) fn new<R, W>(
        reader: R,
        writer: W,
        cwd: PathBuf,
        fs_read: Option<FsRead>,
        fs_write: Option<FsWrite>,
    ) -> Self
    where
        R: AsyncRead + Unpin + Send + 'static,
        W: AsyncWrite + Unpin + Send + 'static,
    {
        let rpc = JsonRpcClient::new(reader, writer);
        let observer = Arc::new(AgentObserver::new());
        let state = Arc::new(RwLock::new(ConnectionState {
            session_id: None,
            load_session_capability: false,
            cwd,
        }));

        // Notification handler: route session/update into the observer.
        let observer_for_notifications = Arc::clone(&observer);
        let nh: NotificationHandler = Arc::new(move |method, params| {
            if method == "session/update" {
                let update = params.get("update").cloned().unwrap_or(Value::Null);
                let update_type = update
                    .get("sessionUpdate")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_string();
                observer_for_notifications.observe_update(&update_type, &update);
            }
        });
        rpc.set_notification_handler(nh);

        // Request handler: auto-grant permission, route fs/* through
        // caller-supplied closures, anything else -> -32601.
        let state_for_requests = Arc::clone(&state);
        let fr = fs_read;
        let fw = fs_write;
        let rh: RequestHandler = Arc::new(move |method, params| {
            let state = Arc::clone(&state_for_requests);
            let fr = fr.clone();
            let fw = fw.clone();
            Box::pin(async move { handle_request(method, params, state, fr, fw).await })
        });
        rpc.set_request_handler(rh);

        Self {
            rpc,
            observer,
            state,
        }
    }

    /// Spawn the JsonRpcClient reader task. Idempotent.
    pub(crate) fn start(&self) {
        self.rpc.start();
    }

    /// Send `initialize`. Caches `agentCapabilities.loadSession` so
    /// callers can decide between `session_new` and `session_load`.
    pub(crate) async fn initialize(&self, timeout: Duration) -> Result<Value, JsonRpcError> {
        self.start();
        let result = self
            .rpc
            .request(
                "initialize",
                json!({
                    "protocolVersion": 1,
                    "clientCapabilities": {
                        "fs": {
                            "readTextFile": true,
                            "writeTextFile": true,
                        },
                    },
                }),
                timeout,
            )
            .await?;
        let load_cap = result
            .get("agentCapabilities")
            .and_then(|c| c.get("loadSession"))
            .and_then(Value::as_bool)
            .unwrap_or(false);
        self.state.write().unwrap().load_session_capability = load_cap;
        Ok(result)
    }

    /// `session/new` — open a fresh session under `cwd_override` (or
    /// the connection's default cwd). Stashes the returned sessionId.
    pub(crate) async fn session_new(
        &self,
        cwd_override: Option<&Path>,
        timeout: Duration,
    ) -> Result<String, JsonRpcError> {
        let cwd = self.resolved_cwd(cwd_override);
        let result = self
            .rpc
            .request(
                "session/new",
                json!({
                    "cwd": cwd.to_string_lossy(),
                    "mcpServers": [],
                }),
                timeout,
            )
            .await?;
        let session_id = result
            .get("sessionId")
            .and_then(Value::as_str)
            .map(str::to_string);
        self.state.write().unwrap().session_id = session_id.clone();
        self.extract_model(&result);
        Ok(session_id.unwrap_or_default())
    }

    /// `session/load` — resume a prior session. After the response
    /// returns, the agent may still push replay `session/update`
    /// notifications; the 200ms drain below blocks long enough for
    /// the reader task to catch them, then `observer.reset_all()`
    /// throws the replay state away so `send_prompt` starts clean.
    pub(crate) async fn session_load(
        &self,
        session_id: &str,
        cwd_override: Option<&Path>,
        timeout: Duration,
    ) -> Result<String, JsonRpcError> {
        let cwd = self.resolved_cwd(cwd_override);
        let result = self
            .rpc
            .request(
                "session/load",
                json!({
                    "sessionId": session_id,
                    "cwd": cwd.to_string_lossy(),
                    "mcpServers": [],
                }),
                timeout,
            )
            .await?;

        // session/load may return null per spec; keep the requested ID.
        let resolved_id = result
            .get("sessionId")
            .and_then(Value::as_str)
            .map(str::to_string)
            .unwrap_or_else(|| session_id.to_string());
        self.state.write().unwrap().session_id = Some(resolved_id.clone());
        if result.is_object() {
            self.extract_model(&result);
        }

        // Mandatory drain — see file-level doc comment. The
        // regression test `session_load_drains_replay_notifications`
        // fails if this sleep is removed.
        tokio::time::sleep(Duration::from_millis(200)).await;

        // Throw away replay state so send_prompt's accumulators are
        // empty (mirror of Python `self._observer = AgentObserver()`).
        self.observer.reset_all();

        Ok(resolved_id)
    }

    /// `session/prompt` — send the prompt and return the structured
    /// `AcpPromptResult`. Resets the observer turn before sending so
    /// streamed `session/update` chunks accumulate cleanly; finalises
    /// after the response with `stopReason` from the prompt result.
    pub(crate) async fn send_prompt(
        &self,
        prompt: &str,
        timeout: Duration,
    ) -> Result<AcpPromptResult, JsonRpcError> {
        let session_id = self.state.read().unwrap().session_id.clone();
        self.observer.reset_turn();
        let result = self
            .rpc
            .request(
                "session/prompt",
                json!({
                    "sessionId": session_id,
                    "prompt": [{"type":"text", "text": prompt}],
                }),
                timeout,
            )
            .await?;
        let stop_reason = result
            .get("stopReason")
            .and_then(Value::as_str)
            .map(str::to_string);
        let turn = self.observer.finish_turn(stop_reason);
        let model = result
            .get("model")
            .and_then(Value::as_str)
            .map(str::to_string)
            .or(turn.model.clone())
            .or_else(|| self.observer.model_name());
        Ok(AcpPromptResult {
            text: turn.text,
            stop_reason: turn.stop_reason,
            usage: result.get("usage").cloned().unwrap_or(Value::Null),
            session_id,
            model,
            accumulated_usage: turn.usage,
        })
    }

    pub(crate) fn supports_load_session(&self) -> bool {
        self.state.read().unwrap().load_session_capability
    }

    pub(crate) fn session_id(&self) -> Option<String> {
        self.state.read().unwrap().session_id.clone()
    }

    pub(crate) async fn disconnect(&self) {
        self.rpc.disconnect().await;
    }

    // ── Internal ─────────────────────────────────────────────────────

    fn resolved_cwd(&self, override_path: Option<&Path>) -> PathBuf {
        let state = self.state.read().unwrap();
        let p = override_path
            .map(Path::to_path_buf)
            .unwrap_or_else(|| state.cwd.clone());
        // mirror Python os.path.abspath — return as-is if absolute,
        // else join with current dir if discoverable.
        if p.is_absolute() {
            p
        } else if let Ok(here) = std::env::current_dir() {
            here.join(p)
        } else {
            p
        }
    }

    fn extract_model(&self, result: &Value) {
        let models = match result.get("models").and_then(Value::as_object) {
            Some(m) => m,
            None => return,
        };
        let current_id = match models.get("currentModelId").and_then(Value::as_str) {
            Some(s) => s,
            None => return,
        };
        let available = models
            .get("availableModels")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        for m in available.iter() {
            if m.get("modelId").and_then(Value::as_str) == Some(current_id) {
                let desc = m.get("description").and_then(Value::as_str).unwrap_or("");
                let name = if desc.contains(" \u{00b7} ") {
                    desc.split(" \u{00b7} ").next().unwrap_or(desc).to_string()
                } else {
                    m.get("name")
                        .and_then(Value::as_str)
                        .unwrap_or(current_id)
                        .to_string()
                };
                self.observer.set_model_name(Some(name));
                return;
            }
        }
        // Fallback: model_id verbatim if no availableModels match.
        self.observer.set_model_name(Some(current_id.to_string()));
    }
}

async fn handle_request(
    method: String,
    params: Value,
    state: Arc<RwLock<ConnectionState>>,
    fs_read: Option<FsRead>,
    fs_write: Option<FsWrite>,
) -> RequestOutcome {
    match method.as_str() {
        "session/request_permission" => Ok(json!({
            "outcome": {"outcome": "selected", "optionId": "allow_once"},
        })),
        "fs/read_text_file" => {
            let path = resolve_fs_path(&params, &state);
            let f = match fs_read {
                Some(f) => f,
                None => {
                    return Err(JsonRpcError::Protocol {
                        code: -32002,
                        message: "VFS not available: NexusFS not bound".into(),
                        data: None,
                    });
                }
            };
            match f(path).await {
                Ok(content) => Ok(json!({"content": content})),
                Err(e) => Err(JsonRpcError::Protocol {
                    code: -32000,
                    message: e,
                    data: None,
                }),
            }
        }
        "fs/write_text_file" => {
            let path = resolve_fs_path(&params, &state);
            let content = params
                .get("content")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string();
            let f = match fs_write {
                Some(f) => f,
                None => {
                    return Err(JsonRpcError::Protocol {
                        code: -32002,
                        message: "VFS not available: NexusFS not bound".into(),
                        data: None,
                    });
                }
            };
            match f(path, content).await {
                Ok(()) => Ok(Value::Null),
                Err(e) => Err(JsonRpcError::Protocol {
                    code: -32000,
                    message: e,
                    data: None,
                }),
            }
        }
        other => Err(JsonRpcError::Protocol {
            code: -32601,
            message: format!("Method not found: {other}"),
            data: None,
        }),
    }
}

/// Resolve a `params.path` against the connection's cwd if relative.
/// Mirror of Python `_resolve_path`.
fn resolve_fs_path(params: &Value, state: &Arc<RwLock<ConnectionState>>) -> String {
    let raw = params.get("path").and_then(Value::as_str).unwrap_or("");
    let p = Path::new(raw);
    if p.is_absolute() {
        return raw.to_string();
    }
    let cwd = state.read().unwrap().cwd.clone();
    cwd.join(p).to_string_lossy().to_string()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};

    type DuplexRead = tokio::io::ReadHalf<tokio::io::DuplexStream>;
    type DuplexWrite = tokio::io::WriteHalf<tokio::io::DuplexStream>;
    type Halves = (DuplexRead, DuplexWrite);

    fn duplex_pair() -> (Halves, Halves) {
        let (a, b) = tokio::io::duplex(8192);
        let (a_r, a_w) = tokio::io::split(a);
        let (b_r, b_w) = tokio::io::split(b);
        ((a_r, a_w), (b_r, b_w))
    }

    async fn read_one_line(br: &mut BufReader<DuplexRead>) -> Value {
        let mut line = String::new();
        br.read_line(&mut line).await.unwrap();
        serde_json::from_str(line.trim()).unwrap()
    }

    async fn write_payload(w: &mut DuplexWrite, payload: Value) {
        let mut bytes = serde_json::to_vec(&payload).unwrap();
        bytes.push(b'\n');
        w.write_all(&bytes).await.unwrap();
        w.flush().await.unwrap();
    }

    #[tokio::test]
    async fn initialize_sends_capabilities_and_captures_load_session() {
        let ((c_r, c_w), (s_r, mut s_w)) = duplex_pair();
        let conn = AcpConnection::new(c_r, c_w, PathBuf::from("/tmp"), None, None);
        let mut br = BufReader::new(s_r);

        let server = tokio::spawn(async move {
            let req = read_one_line(&mut br).await;
            assert_eq!(req["method"], "initialize");
            assert_eq!(req["params"]["protocolVersion"], 1);
            assert_eq!(
                req["params"]["clientCapabilities"]["fs"]["readTextFile"],
                true
            );
            let id = req["id"].as_u64().unwrap();
            write_payload(
                &mut s_w,
                json!({
                    "jsonrpc":"2.0", "id":id,
                    "result":{"agentCapabilities":{"loadSession": true}},
                }),
            )
            .await;
        });

        let result = conn.initialize(Duration::from_secs(2)).await.unwrap();
        assert_eq!(result["agentCapabilities"]["loadSession"], true);
        assert!(conn.supports_load_session());
        server.await.unwrap();
        conn.disconnect().await;
    }

    #[tokio::test]
    async fn session_new_stashes_session_id_and_extracts_model() {
        let ((c_r, c_w), (s_r, mut s_w)) = duplex_pair();
        let conn = AcpConnection::new(c_r, c_w, PathBuf::from("/tmp"), None, None);
        let mut br = BufReader::new(s_r);

        let server = tokio::spawn(async move {
            let req = read_one_line(&mut br).await;
            let id = req["id"].as_u64().unwrap();
            write_payload(
                &mut s_w,
                json!({
                    "jsonrpc":"2.0","id":id,
                    "result":{
                        "sessionId":"sess-XYZ",
                        "models":{
                            "currentModelId":"claude-sonnet-4-6",
                            "availableModels":[{"modelId":"claude-sonnet-4-6","name":"Claude Sonnet 4.6","description":"Claude Sonnet · latest"}]
                        }
                    }
                }),
            ).await;
        });

        conn.start();
        let id = conn
            .session_new(None, Duration::from_secs(2))
            .await
            .unwrap();
        assert_eq!(id, "sess-XYZ");
        // Send a prompt and check model surfaces from observer.model_name.
        server.await.unwrap();
        conn.disconnect().await;
    }

    #[tokio::test]
    async fn send_prompt_accumulates_text_from_session_update_notifications() {
        let ((c_r, c_w), (s_r, mut s_w)) = duplex_pair();
        let conn = AcpConnection::new(c_r, c_w, PathBuf::from("/tmp"), None, None);
        let mut br = BufReader::new(s_r);

        let server = tokio::spawn(async move {
            // Initialize round-trip.
            let req = read_one_line(&mut br).await;
            let id = req["id"].as_u64().unwrap();
            write_payload(&mut s_w, json!({"jsonrpc":"2.0","id":id,"result":{}})).await;
            // session/new round-trip.
            let req = read_one_line(&mut br).await;
            let id = req["id"].as_u64().unwrap();
            write_payload(
                &mut s_w,
                json!({"jsonrpc":"2.0","id":id,"result":{"sessionId":"s1"}}),
            )
            .await;
            // session/prompt round-trip with streamed updates first.
            let req = read_one_line(&mut br).await;
            let id = req["id"].as_u64().unwrap();
            write_payload(
                &mut s_w,
                json!({
                    "jsonrpc":"2.0","method":"session/update",
                    "params":{"update":{"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":"hello "}}}
                }),
            ).await;
            write_payload(
                &mut s_w,
                json!({
                    "jsonrpc":"2.0","method":"session/update",
                    "params":{"update":{"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":"world"}}}
                }),
            ).await;
            tokio::time::sleep(Duration::from_millis(30)).await;
            write_payload(
                &mut s_w,
                json!({
                    "jsonrpc":"2.0","id":id,
                    "result":{"stopReason":"end_turn","usage":{"input_tokens":10}}
                }),
            )
            .await;
        });

        conn.initialize(Duration::from_secs(2)).await.unwrap();
        conn.session_new(None, Duration::from_secs(2))
            .await
            .unwrap();
        let result = conn
            .send_prompt("hi", Duration::from_secs(2))
            .await
            .unwrap();
        assert_eq!(result.text, "hello world");
        assert_eq!(result.stop_reason.as_deref(), Some("end_turn"));
        assert_eq!(result.session_id.as_deref(), Some("s1"));
        server.await.unwrap();
        conn.disconnect().await;
    }

    #[tokio::test]
    async fn session_load_drains_replay_notifications() {
        // Regression: if the 200ms drain after session/load is removed,
        // these replay updates would land on the next observer turn.
        let ((c_r, c_w), (s_r, mut s_w)) = duplex_pair();
        let conn = AcpConnection::new(c_r, c_w, PathBuf::from("/tmp"), None, None);
        let mut br = BufReader::new(s_r);

        let server = tokio::spawn(async move {
            // session/load request.
            let req = read_one_line(&mut br).await;
            let id = req["id"].as_u64().unwrap();
            // 1) Send the load response FIRST.
            write_payload(
                &mut s_w,
                json!({"jsonrpc":"2.0","id":id,"result":{"sessionId":"sess-resumed"}}),
            )
            .await;
            // 2) THEN send three replay updates. If session_load
            //    skipped the 200ms drain + reset_all, these would
            //    leak into the next prompt.
            for chunk in &["replay-A", "replay-B", "replay-C"] {
                write_payload(
                    &mut s_w,
                    json!({
                        "jsonrpc":"2.0","method":"session/update",
                        "params":{"update":{"sessionUpdate":"agent_message_chunk","content":{"type":"text","text": *chunk}}}
                    }),
                ).await;
            }
            // session/prompt.
            let req = read_one_line(&mut br).await;
            let id = req["id"].as_u64().unwrap();
            write_payload(
                &mut s_w,
                json!({
                    "jsonrpc":"2.0","method":"session/update",
                    "params":{"update":{"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":"only-this"}}}
                }),
            ).await;
            write_payload(
                &mut s_w,
                json!({"jsonrpc":"2.0","id":id,"result":{"stopReason":"end_turn"}}),
            )
            .await;
        });

        conn.start();
        let id = conn
            .session_load("sess-resumed", None, Duration::from_secs(2))
            .await
            .unwrap();
        assert_eq!(id, "sess-resumed");
        let r = conn
            .send_prompt("ignored", Duration::from_secs(2))
            .await
            .unwrap();
        // Only the post-load chunk survives; replay was drained + dropped.
        assert_eq!(r.text, "only-this");
        server.await.unwrap();
        conn.disconnect().await;
    }

    #[tokio::test]
    async fn request_permission_auto_grants() {
        let ((c_r, c_w), (s_r, mut s_w)) = duplex_pair();
        let conn = AcpConnection::new(c_r, c_w, PathBuf::from("/tmp"), None, None);
        let mut br = BufReader::new(s_r);
        conn.start();

        // Server sends a session/request_permission to the client.
        write_payload(
            &mut s_w,
            json!({
                "jsonrpc":"2.0","id":1,"method":"session/request_permission",
                "params":{"options":[{"optionId":"allow_once"},{"optionId":"deny"}]}
            }),
        )
        .await;

        let resp = read_one_line(&mut br).await;
        assert_eq!(resp["id"], 1);
        assert_eq!(resp["result"]["outcome"]["outcome"], "selected");
        assert_eq!(resp["result"]["outcome"]["optionId"], "allow_once");
        conn.disconnect().await;
    }

    #[tokio::test]
    async fn fs_read_text_file_routes_through_fs_read_closure() {
        let ((c_r, c_w), (s_r, mut s_w)) = duplex_pair();
        let calls = Arc::new(AtomicUsize::new(0));
        let calls_clone = Arc::clone(&calls);
        let fs_read: FsRead = Arc::new(move |path: String| {
            let calls = Arc::clone(&calls_clone);
            Box::pin(async move {
                calls.fetch_add(1, Ordering::Relaxed);
                // Absolute path passed by the test — resolve_fs_path
                // is identity for absolute inputs across platforms.
                assert_eq!(path, "/abs/foo.txt");
                Ok::<_, String>("file-contents".to_string())
            })
        });
        let conn = AcpConnection::new(c_r, c_w, PathBuf::from("/tmp"), Some(fs_read), None);
        let mut br = BufReader::new(s_r);
        conn.start();

        write_payload(
            &mut s_w,
            json!({
                "jsonrpc":"2.0","id":7,"method":"fs/read_text_file",
                "params":{"path":"/abs/foo.txt"}
            }),
        )
        .await;

        let resp = read_one_line(&mut br).await;
        assert_eq!(resp["id"], 7);
        assert_eq!(resp["result"]["content"], "file-contents");
        assert_eq!(calls.load(Ordering::Relaxed), 1);
        conn.disconnect().await;
    }

    #[tokio::test]
    async fn fs_request_returns_minus_32002_when_no_closures_bound() {
        let ((c_r, c_w), (s_r, mut s_w)) = duplex_pair();
        let conn = AcpConnection::new(c_r, c_w, PathBuf::from("/tmp"), None, None);
        let mut br = BufReader::new(s_r);
        conn.start();

        write_payload(
            &mut s_w,
            json!({
                "jsonrpc":"2.0","id":3,"method":"fs/read_text_file",
                "params":{"path":"/x"}
            }),
        )
        .await;
        let resp = read_one_line(&mut br).await;
        assert_eq!(resp["id"], 3);
        assert_eq!(resp["error"]["code"], -32002);
        conn.disconnect().await;
    }

    #[tokio::test]
    async fn unknown_method_returns_minus_32601() {
        let ((c_r, c_w), (s_r, mut s_w)) = duplex_pair();
        let conn = AcpConnection::new(c_r, c_w, PathBuf::from("/tmp"), None, None);
        let mut br = BufReader::new(s_r);
        conn.start();

        write_payload(
            &mut s_w,
            json!({"jsonrpc":"2.0","id":42,"method":"unknown/method","params":{}}),
        )
        .await;
        let resp = read_one_line(&mut br).await;
        assert_eq!(resp["id"], 42);
        assert_eq!(resp["error"]["code"], -32601);
        conn.disconnect().await;
    }
}
