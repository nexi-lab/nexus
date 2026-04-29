//! `JsonRpcClient` — newline-delimited JSON-RPC 2.0 client over an
//! arbitrary `AsyncRead + AsyncWrite` pair.
//!
//! Generic over the transport so the same surface drives an ACP
//! subprocess (stdin/stdout pipes) today and future managed-agent
//! loops over different transports later. Newline-framed: each
//! outbound message is `serde_json::to_vec(...) + b"\n"`, written via
//! a single `write_all` call (no partial-write windows). Inbound
//! messages are read with `read_line` so framing matches the Python
//! `AgentLoop` 1:1.
//!
//! Reader task lifecycle:
//!
//!   * `start()` spawns the reader on the current tokio runtime.
//!   * Each line is parsed; responses (`id` + `result|error`) resolve
//!     the matching `oneshot` in the pending table; requests (`id` +
//!     `method`) spawn the registered request handler in a fresh
//!     tokio task so a slow `fs/read_text_file` doesn't block other
//!     messages; notifications (no `id`) call the notification
//!     handler inline.
//!   * On EOF / decode error / `disconnect()` the reader exits and
//!     every pending `oneshot` is failed with
//!     `JsonRpcError::Connection`.
//!
//! Writer is held behind a `tokio::sync::Mutex` so the reader-task's
//! request handler can call `respond` / `respond_error` from another
//! task without racing the application's `request` calls.

#![allow(dead_code)]

use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex as StdMutex};
use std::time::Duration;

use futures::future::BoxFuture;
use serde::Serialize;
use serde_json::{json, Value};
use tokio::io::{AsyncBufReadExt, AsyncRead, AsyncWrite, AsyncWriteExt, BufReader};
use tokio::sync::{oneshot, Mutex as TokioMutex};
use tokio::task::JoinHandle;

// ── Error type ──────────────────────────────────────────────────────────

#[derive(Debug)]
pub(crate) enum JsonRpcError {
    /// `request` did not see a response within the timeout window.
    Timeout,
    /// Reader task closed (EOF, IO error, or `disconnect()`).
    Connection(String),
    /// Peer returned a JSON-RPC error response.
    Protocol {
        code: i32,
        message: String,
        data: Option<Value>,
    },
    /// Local IO error on send.
    Io(String),
}

impl std::fmt::Display for JsonRpcError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Timeout => write!(f, "json-rpc timeout"),
            Self::Connection(m) => write!(f, "json-rpc connection: {m}"),
            Self::Protocol { code, message, .. } => {
                write!(f, "json-rpc protocol [{code}]: {message}")
            }
            Self::Io(m) => write!(f, "json-rpc io: {m}"),
        }
    }
}

impl std::error::Error for JsonRpcError {}

// ── Handler types ───────────────────────────────────────────────────────

/// Outcome a request handler returns: serde_json `Value` on success,
/// or a `JsonRpcError` that maps onto the wire error response.
pub(crate) type RequestOutcome = Result<Value, JsonRpcError>;

/// Async handler for incoming requests. Returns either a `result`
/// payload (sent back as a JSON-RPC success response) or an error
/// (sent as a JSON-RPC error response with the embedded code/message).
pub(crate) type RequestHandler =
    Arc<dyn Fn(String, Value) -> BoxFuture<'static, RequestOutcome> + Send + Sync>;

/// Sync handler for incoming notifications. Returns nothing; failures
/// are swallowed (notifications have no reply channel).
pub(crate) type NotificationHandler = Arc<dyn Fn(String, Value) + Send + Sync>;

/// Pending-call table: per-id `oneshot` senders awaiting their
/// response. Factored out so the struct field doesn't trigger
/// `clippy::type_complexity`.
type PendingTable = HashMap<u64, oneshot::Sender<RequestOutcome>>;

// ── Wire helpers ────────────────────────────────────────────────────────

#[derive(Serialize)]
struct OutgoingRequest<'a> {
    jsonrpc: &'static str,
    id: u64,
    method: &'a str,
    params: &'a Value,
}

#[derive(Serialize)]
struct OutgoingNotification<'a> {
    jsonrpc: &'static str,
    method: &'a str,
    params: &'a Value,
}

// ── Client ──────────────────────────────────────────────────────────────

/// Newline-delimited JSON-RPC client. Build one per transport pair;
/// the reader task is spawned by [`JsonRpcClient::start`].
pub(crate) struct JsonRpcClient {
    writer: Arc<TokioMutex<Box<dyn AsyncWrite + Unpin + Send>>>,
    next_id: AtomicU64,
    pending: Arc<StdMutex<PendingTable>>,
    request_handler: StdMutex<Option<RequestHandler>>,
    notification_handler: StdMutex<Option<NotificationHandler>>,
    reader_task: StdMutex<Option<JoinHandle<()>>>,
    reader: StdMutex<Option<Box<dyn AsyncRead + Unpin + Send>>>,
}

impl JsonRpcClient {
    pub(crate) fn new<R, W>(reader: R, writer: W) -> Arc<Self>
    where
        R: AsyncRead + Unpin + Send + 'static,
        W: AsyncWrite + Unpin + Send + 'static,
    {
        Arc::new(Self {
            writer: Arc::new(TokioMutex::new(Box::new(writer))),
            next_id: AtomicU64::new(1),
            pending: Arc::new(StdMutex::new(HashMap::new())),
            request_handler: StdMutex::new(None),
            notification_handler: StdMutex::new(None),
            reader_task: StdMutex::new(None),
            reader: StdMutex::new(Some(Box::new(reader))),
        })
    }

    pub(crate) fn set_request_handler(&self, h: RequestHandler) {
        *self.request_handler.lock().unwrap() = Some(h);
    }

    pub(crate) fn set_notification_handler(&self, h: NotificationHandler) {
        *self.notification_handler.lock().unwrap() = Some(h);
    }

    /// Spawn the reader task on the current tokio runtime. Idempotent;
    /// a second call after the reader has already started is a no-op.
    pub(crate) fn start(self: &Arc<Self>) {
        let mut slot = self.reader_task.lock().unwrap();
        if slot.is_some() {
            return;
        }
        let reader = match self.reader.lock().unwrap().take() {
            Some(r) => r,
            None => return,
        };
        let this = Arc::clone(self);
        let handle = tokio::spawn(async move {
            this.reader_loop(reader).await;
        });
        *slot = Some(handle);
    }

    /// Send a JSON-RPC request and await the response. Times out
    /// after `timeout`; the pending entry is dropped on timeout so
    /// a late response is silently discarded.
    pub(crate) async fn request(
        self: &Arc<Self>,
        method: &str,
        params: Value,
        timeout: Duration,
    ) -> Result<Value, JsonRpcError> {
        let id = self.next_id.fetch_add(1, Ordering::Relaxed);
        let (tx, rx) = oneshot::channel();
        self.pending.lock().unwrap().insert(id, tx);

        let line = match serde_json::to_vec(&OutgoingRequest {
            jsonrpc: "2.0",
            id,
            method,
            params: &params,
        }) {
            Ok(mut b) => {
                b.push(b'\n');
                b
            }
            Err(e) => {
                self.pending.lock().unwrap().remove(&id);
                return Err(JsonRpcError::Io(e.to_string()));
            }
        };

        if let Err(e) = self.write_line(&line).await {
            self.pending.lock().unwrap().remove(&id);
            return Err(e);
        }

        match tokio::time::timeout(timeout, rx).await {
            Ok(Ok(result)) => result,
            Ok(Err(_)) => Err(JsonRpcError::Connection("oneshot dropped".into())),
            Err(_) => {
                self.pending.lock().unwrap().remove(&id);
                Err(JsonRpcError::Timeout)
            }
        }
    }

    /// Send a JSON-RPC notification (no response expected).
    pub(crate) async fn notify(&self, method: &str, params: Value) -> Result<(), JsonRpcError> {
        let mut bytes = serde_json::to_vec(&OutgoingNotification {
            jsonrpc: "2.0",
            method,
            params: &params,
        })
        .map_err(|e| JsonRpcError::Io(e.to_string()))?;
        bytes.push(b'\n');
        self.write_line(&bytes).await
    }

    /// Reply to an incoming request with a success result.
    pub(crate) async fn respond(&self, id: Value, result: Value) -> Result<(), JsonRpcError> {
        let mut bytes = serde_json::to_vec(&json!({
            "jsonrpc": "2.0",
            "id": id,
            "result": result,
        }))
        .map_err(|e| JsonRpcError::Io(e.to_string()))?;
        bytes.push(b'\n');
        self.write_line(&bytes).await
    }

    /// Reply to an incoming request with a JSON-RPC error.
    pub(crate) async fn respond_error(
        &self,
        id: Value,
        code: i32,
        message: &str,
    ) -> Result<(), JsonRpcError> {
        let mut bytes = serde_json::to_vec(&json!({
            "jsonrpc": "2.0",
            "id": id,
            "error": {"code": code, "message": message},
        }))
        .map_err(|e| JsonRpcError::Io(e.to_string()))?;
        bytes.push(b'\n');
        self.write_line(&bytes).await
    }

    /// Cancel the reader task and fail every pending request with
    /// `Connection`. Idempotent.
    pub(crate) async fn disconnect(&self) {
        let handle = self.reader_task.lock().unwrap().take();
        if let Some(h) = handle {
            h.abort();
            let _ = h.await;
        }
        self.fail_all_pending("disconnected");
    }

    // ── Internal ─────────────────────────────────────────────────────

    async fn write_line(&self, bytes: &[u8]) -> Result<(), JsonRpcError> {
        let mut w = self.writer.lock().await;
        w.write_all(bytes)
            .await
            .map_err(|e| JsonRpcError::Io(e.to_string()))?;
        w.flush().await.map_err(|e| JsonRpcError::Io(e.to_string()))
    }

    fn fail_all_pending(&self, reason: &str) {
        let drained: Vec<_> = self.pending.lock().unwrap().drain().collect();
        for (_id, tx) in drained {
            let _ = tx.send(Err(JsonRpcError::Connection(reason.to_string())));
        }
    }

    async fn reader_loop(self: Arc<Self>, reader: Box<dyn AsyncRead + Unpin + Send>) {
        let mut buf = String::new();
        let mut br = BufReader::new(reader);
        loop {
            buf.clear();
            match br.read_line(&mut buf).await {
                Ok(0) => {
                    self.fail_all_pending("EOF");
                    return;
                }
                Ok(_) => {}
                Err(e) => {
                    self.fail_all_pending(&format!("reader io: {e}"));
                    return;
                }
            }
            let line = buf.trim_end_matches(['\r', '\n']);
            if line.is_empty() {
                continue;
            }
            let msg: Value = match serde_json::from_str(line) {
                Ok(v) => v,
                Err(_) => continue, // mirror Python: log + skip non-JSON
            };
            self.dispatch(msg).await;
        }
    }

    async fn dispatch(self: &Arc<Self>, msg: Value) {
        // Response: id + (result | error)
        if let Some(id) = msg.get("id").and_then(Value::as_u64) {
            if msg.get("result").is_some() || msg.get("error").is_some() {
                let outcome = if let Some(err) = msg.get("error") {
                    Err(JsonRpcError::Protocol {
                        code: err.get("code").and_then(Value::as_i64).unwrap_or(-1) as i32,
                        message: err
                            .get("message")
                            .and_then(Value::as_str)
                            .unwrap_or("Unknown RPC error")
                            .to_string(),
                        data: err.get("data").cloned(),
                    })
                } else {
                    Ok(msg.get("result").cloned().unwrap_or(Value::Null))
                };
                if let Some(tx) = self.pending.lock().unwrap().remove(&id) {
                    let _ = tx.send(outcome);
                }
                return;
            }
        }

        let method = match msg.get("method").and_then(Value::as_str) {
            Some(m) => m.to_string(),
            None => return,
        };
        let params = msg.get("params").cloned().unwrap_or(Value::Null);
        let id = msg.get("id").cloned();

        if let Some(id_value) = id {
            // Incoming request — spawn handler so reader keeps draining.
            let handler = self.request_handler.lock().unwrap().clone();
            if let Some(h) = handler {
                let this = Arc::clone(self);
                tokio::spawn(async move {
                    let outcome = h(method, params).await;
                    match outcome {
                        Ok(result) => {
                            let _ = this.respond(id_value, result).await;
                        }
                        Err(JsonRpcError::Protocol { code, message, .. }) => {
                            let _ = this.respond_error(id_value, code, &message).await;
                        }
                        Err(other) => {
                            let _ = this
                                .respond_error(id_value, -32000, &other.to_string())
                                .await;
                        }
                    }
                });
            } else {
                let _ = self
                    .respond_error(id_value, -32601, &format!("Method not found: {method}"))
                    .await;
            }
        } else {
            // Notification.
            let handler = self.notification_handler.lock().unwrap().clone();
            if let Some(h) = handler {
                h(method, params);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::AtomicUsize;
    use tokio::io::AsyncWriteExt;

    type DuplexRead = tokio::io::ReadHalf<tokio::io::DuplexStream>;
    type DuplexWrite = tokio::io::WriteHalf<tokio::io::DuplexStream>;
    type Halves = (DuplexRead, DuplexWrite);

    fn duplex_pair() -> (Halves, Halves) {
        let (a, b) = tokio::io::duplex(8192);
        let (a_r, a_w) = tokio::io::split(a);
        let (b_r, b_w) = tokio::io::split(b);
        ((a_r, a_w), (b_r, b_w))
    }

    #[tokio::test]
    async fn request_response_round_trip() {
        let ((client_r, client_w), (server_r, mut server_w)) = duplex_pair();
        let client = JsonRpcClient::new(client_r, client_w);
        client.start();

        // Server: read one line, return a response with the same id.
        let server_task = tokio::spawn(async move {
            let mut br = BufReader::new(server_r);
            let mut line = String::new();
            br.read_line(&mut line).await.unwrap();
            let msg: Value = serde_json::from_str(line.trim()).unwrap();
            let id = msg["id"].as_u64().unwrap();
            let resp = json!({"jsonrpc":"2.0","id":id,"result":{"echo":"hi"}});
            let mut bytes = serde_json::to_vec(&resp).unwrap();
            bytes.push(b'\n');
            server_w.write_all(&bytes).await.unwrap();
            server_w.flush().await.unwrap();
        });

        let result = client
            .request("ping", json!({"x":1}), Duration::from_secs(2))
            .await
            .unwrap();
        assert_eq!(result, json!({"echo":"hi"}));
        server_task.await.unwrap();
        client.disconnect().await;
    }

    #[tokio::test]
    async fn protocol_error_surfaces_as_protocol_variant() {
        let ((client_r, client_w), (server_r, mut server_w)) = duplex_pair();
        let client = JsonRpcClient::new(client_r, client_w);
        client.start();

        let server_task = tokio::spawn(async move {
            let mut br = BufReader::new(server_r);
            let mut line = String::new();
            br.read_line(&mut line).await.unwrap();
            let msg: Value = serde_json::from_str(line.trim()).unwrap();
            let id = msg["id"].as_u64().unwrap();
            let resp = json!({
                "jsonrpc": "2.0",
                "id": id,
                "error": {"code": -32601, "message": "Method not found"},
            });
            let mut bytes = serde_json::to_vec(&resp).unwrap();
            bytes.push(b'\n');
            server_w.write_all(&bytes).await.unwrap();
            server_w.flush().await.unwrap();
        });

        let err = client
            .request("nope", json!({}), Duration::from_secs(2))
            .await
            .unwrap_err();
        match err {
            JsonRpcError::Protocol { code, message, .. } => {
                assert_eq!(code, -32601);
                assert!(message.contains("Method not found"));
            }
            other => panic!("expected Protocol, got {other:?}"),
        }
        server_task.await.unwrap();
        client.disconnect().await;
    }

    #[tokio::test]
    async fn notification_invokes_notification_handler() {
        let ((client_r, client_w), (_server_r, mut server_w)) = duplex_pair();
        let client = JsonRpcClient::new(client_r, client_w);

        let counter = Arc::new(AtomicUsize::new(0));
        let counter_clone = Arc::clone(&counter);
        client.set_notification_handler(Arc::new(move |method, params| {
            assert_eq!(method, "session/update");
            assert_eq!(params, json!({"sessionUpdate":"agent_message_chunk"}));
            counter_clone.fetch_add(1, Ordering::Relaxed);
        }));
        client.start();

        let payload = json!({
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {"sessionUpdate":"agent_message_chunk"},
        });
        let mut bytes = serde_json::to_vec(&payload).unwrap();
        bytes.push(b'\n');
        server_w.write_all(&bytes).await.unwrap();
        server_w.flush().await.unwrap();

        // Give the reader task a moment to drain the line.
        for _ in 0..50 {
            if counter.load(Ordering::Relaxed) > 0 {
                break;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
        assert_eq!(counter.load(Ordering::Relaxed), 1);
        client.disconnect().await;
    }

    #[tokio::test]
    async fn server_request_routes_through_request_handler_and_response_returns() {
        let ((client_r, client_w), (server_r, mut server_w)) = duplex_pair();
        let client = JsonRpcClient::new(client_r, client_w);

        // Handler: respond with {"content":"data"} for fs/read_text_file.
        client.set_request_handler(Arc::new(|method, params| {
            Box::pin(async move {
                assert_eq!(method, "fs/read_text_file");
                assert_eq!(params["path"], "/x");
                Ok(json!({"content":"data"}))
            })
        }));
        client.start();

        // Server side: send a request, read the response.
        let server_task = tokio::spawn(async move {
            let payload = json!({
                "jsonrpc": "2.0",
                "id": 99,
                "method": "fs/read_text_file",
                "params": {"path":"/x"},
            });
            let mut bytes = serde_json::to_vec(&payload).unwrap();
            bytes.push(b'\n');
            server_w.write_all(&bytes).await.unwrap();
            server_w.flush().await.unwrap();

            let mut br = BufReader::new(server_r);
            let mut line = String::new();
            br.read_line(&mut line).await.unwrap();
            let msg: Value = serde_json::from_str(line.trim()).unwrap();
            assert_eq!(msg["id"], 99);
            assert_eq!(msg["result"], json!({"content":"data"}));
        });

        server_task.await.unwrap();
        client.disconnect().await;
    }

    #[tokio::test]
    async fn timeout_returns_timeout_variant_and_drops_pending() {
        let ((client_r, client_w), (_server_r, _server_w)) = duplex_pair();
        let client = JsonRpcClient::new(client_r, client_w);
        client.start();

        let err = client
            .request("ping", json!({}), Duration::from_millis(50))
            .await
            .unwrap_err();
        assert!(matches!(err, JsonRpcError::Timeout));
        // Pending table should be empty after timeout.
        assert!(client.pending.lock().unwrap().is_empty());
        client.disconnect().await;
    }

    #[tokio::test]
    async fn disconnect_fails_pending_with_connection_error() {
        let ((client_r, client_w), (_server_r, _server_w)) = duplex_pair();
        let client = JsonRpcClient::new(client_r, client_w);
        client.start();

        // Start a request that will never get a response.
        let client_clone = Arc::clone(&client);
        let req = tokio::spawn(async move {
            client_clone
                .request("ping", json!({}), Duration::from_secs(10))
                .await
        });

        // Give the request time to install its pending entry.
        tokio::time::sleep(Duration::from_millis(50)).await;
        client.disconnect().await;

        let err = req.await.unwrap().unwrap_err();
        assert!(matches!(err, JsonRpcError::Connection(_)));
    }

    #[tokio::test]
    async fn fragmented_response_writes_are_assembled_into_one_line() {
        // Regression: BufReader must wait for the newline even if the
        // server flushes the response in chunks.
        let ((client_r, client_w), (server_r, mut server_w)) = duplex_pair();
        let client = JsonRpcClient::new(client_r, client_w);
        client.start();

        let server_task = tokio::spawn(async move {
            let mut br = BufReader::new(server_r);
            let mut line = String::new();
            br.read_line(&mut line).await.unwrap();
            let msg: Value = serde_json::from_str(line.trim()).unwrap();
            let id = msg["id"].as_u64().unwrap();
            let resp = json!({"jsonrpc":"2.0","id":id,"result":{"ok":true}});
            let bytes = serde_json::to_vec(&resp).unwrap();
            // Write in fragments WITHOUT the newline, then newline alone.
            let mid = bytes.len() / 2;
            server_w.write_all(&bytes[..mid]).await.unwrap();
            server_w.flush().await.unwrap();
            tokio::time::sleep(Duration::from_millis(20)).await;
            server_w.write_all(&bytes[mid..]).await.unwrap();
            server_w.flush().await.unwrap();
            tokio::time::sleep(Duration::from_millis(20)).await;
            server_w.write_all(b"\n").await.unwrap();
            server_w.flush().await.unwrap();
        });

        let result = client
            .request("ping", json!({}), Duration::from_secs(2))
            .await
            .unwrap();
        assert_eq!(result, json!({"ok":true}));
        server_task.await.unwrap();
        client.disconnect().await;
    }
}
