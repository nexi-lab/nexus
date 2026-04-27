//! OpenAI streaming pipeline — SSE decode → DT_STREAM → CAS persist.
//!
//! Called from `PyKernel::llm_start_streaming` under `py.detach(...)`.
//! Everything below runs without the GIL; the only Python round-trip is the
//! return value of the containing syscall.
//!
//! The state machine mirrors the Python `CASOpenAIBackend._run_stream` that
//! this replaces:
//!   1. HTTP POST `{base_url}/chat/completions` with `stream=true`.
//!   2. SSE decode — accumulate `choices[].delta.content` into `token`
//!      frames, append each to the DT_STREAM at `stream_path`. Tool-call
//!      fragments are accumulated by index.
//!   3. On `[DONE]` — persist `(request, response, envelope)` via
//!      `CASEngine::write_content_tracked` and write a final `done` control
//!      JSON frame carrying the session hash.
//!   4. On transport / status error — emit an `error` control frame and
//!      close the stream. Nothing is persisted on error (matches Python
//!      contract).

#![allow(dead_code)]

use std::sync::Arc;

use futures::StreamExt;
use serde_json::{json, Map, Value};

use crate::openai_backend::OpenAIBackend;
use crate::stream_manager::StreamManager;

// Trait declaration moved to `crate::hal::llm_streaming` (Phase 1
// — was `core::traits::llm_streaming` from Phase B/D preparation).
// Re-exported here so `crate::openai_streaming::LlmStreamingBackend`
// keeps working for callers (notably the `ObjectStore::as_llm_streaming`
// trait method) until Phase 2 moves the openai_* impls into `backends/`.
pub use crate::hal::llm_streaming::LlmStreamingBackend;

impl LlmStreamingBackend for OpenAIBackend {
    #[allow(private_interfaces)]
    fn run_streaming(
        &self,
        request_bytes: &[u8],
        stream_path: &str,
        stream_manager: &Arc<StreamManager>,
    ) -> Result<(), String> {
        match self.run_streaming_inner(request_bytes, stream_path, stream_manager) {
            Ok(()) => Ok(()),
            Err(err) => {
                // Best-effort error signalling into the stream. The caller is
                // already bubbling the Err up — we just want clients reading
                // the DT_STREAM to see a structured termination frame before
                // the stream closes.
                let payload = json!({
                    "type": "error",
                    "message": err,
                });
                let msg = serde_json::to_vec(&payload).unwrap_or_default();
                let _ = stream_manager.write_nowait(stream_path, &msg);
                let _ = stream_manager.close(stream_path);
                Err(err)
            }
        }
    }
}

impl OpenAIBackend {
    fn run_streaming_inner(
        &self,
        request_bytes: &[u8],
        stream_path: &str,
        stream_manager: &Arc<StreamManager>,
    ) -> Result<(), String> {
        let request: Value = serde_json::from_slice(request_bytes)
            .map_err(|e| format!("request JSON parse: {e}"))?;
        let messages = request
            .get("messages")
            .cloned()
            .ok_or_else(|| "request missing 'messages'".to_string())?;
        let model = request
            .get("model")
            .and_then(|m| m.as_str())
            .unwrap_or(&self.default_model)
            .to_string();

        // Build the forwarded body: copy every request kwarg (tools, temperature,
        // max_tokens, …) and force stream=true + include_usage.
        let mut body = Map::new();
        if let Some(obj) = request.as_object() {
            for (k, v) in obj {
                if k == "stream" || k == "stream_options" {
                    continue;
                }
                body.insert(k.clone(), v.clone());
            }
        }
        body.insert("model".to_string(), Value::String(model.clone()));
        body.insert("messages".to_string(), messages);
        body.insert("stream".to_string(), Value::Bool(true));
        body.insert("stream_options".to_string(), json!({"include_usage": true}));

        let url = format!("{}/chat/completions", self.base_url);
        let http = self.http.clone();
        let api_key = self.api_key.clone();

        let start = std::time::Instant::now();
        let stream_manager_clone = Arc::clone(stream_manager);
        let stream_path_owned = stream_path.to_string();

        // Collected state during SSE decode.
        let mut collected_text = String::new();
        let mut finish_reason: Option<String> = None;
        let mut collected_model = model.clone();
        let mut usage: Option<Value> = None;
        let mut tool_calls_accum: std::collections::BTreeMap<u64, Value> =
            std::collections::BTreeMap::new();

        let sse_outcome: Result<(), String> = self.runtime.block_on(async {
            let resp = http
                .post(&url)
                .header("Authorization", format!("Bearer {api_key}"))
                .header("Content-Type", "application/json")
                .header("Accept", "text/event-stream")
                .body(serde_json::to_vec(&Value::Object(body)).map_err(|e| format!("body: {e}"))?)
                .send()
                .await
                .map_err(|e| format!("HTTP: {e}"))?;

            if !resp.status().is_success() {
                let status = resp.status();
                let body = resp.text().await.unwrap_or_default();
                return Err(format!("OpenAI API {status}: {body}"));
            }

            let mut byte_stream = resp.bytes_stream();
            let mut buf: Vec<u8> = Vec::with_capacity(4096);

            while let Some(chunk) = byte_stream.next().await {
                let chunk = chunk.map_err(|e| format!("SSE read: {e}"))?;
                buf.extend_from_slice(&chunk);

                // SSE frames are separated by "\n\n". Split eagerly; anything
                // remaining after the last separator stays in `buf` for the
                // next iteration (partial frame).
                while let Some(sep) = twoline(&buf) {
                    let frame = buf[..sep].to_vec();
                    buf.drain(..sep + 2);

                    // Each frame may hold multiple `data: ` lines; OpenAI
                    // currently sends one per frame.
                    for line in frame.split(|b| *b == b'\n') {
                        let line = strip_cr(line);
                        let Some(data) = line.strip_prefix(b"data: ") else {
                            continue;
                        };
                        if data == b"[DONE]" {
                            return Ok(());
                        }
                        let parsed: Value = match serde_json::from_slice(data) {
                            Ok(v) => v,
                            Err(_) => continue,
                        };

                        if let Some(m) = parsed.get("model").and_then(|m| m.as_str()) {
                            collected_model = m.to_string();
                        }

                        if let Some(u) = parsed.get("usage") {
                            if !u.is_null() {
                                usage = Some(u.clone());
                            }
                        }

                        let choices = parsed
                            .get("choices")
                            .and_then(|c| c.as_array())
                            .cloned()
                            .unwrap_or_default();
                        for choice in &choices {
                            if let Some(fr) = choice.get("finish_reason").and_then(|f| f.as_str()) {
                                finish_reason = Some(fr.to_string());
                            }
                            let Some(delta) = choice.get("delta") else {
                                continue;
                            };
                            if let Some(content) = delta.get("content").and_then(|c| c.as_str()) {
                                if !content.is_empty() {
                                    collected_text.push_str(content);
                                    if let Err(e) = stream_manager_clone
                                        .write_nowait(&stream_path_owned, content.as_bytes())
                                    {
                                        return Err(format!("stream write: {e:?}"));
                                    }
                                }
                            }
                            if let Some(tc_arr) = delta.get("tool_calls").and_then(|t| t.as_array())
                            {
                                for tc in tc_arr {
                                    accumulate_tool_call(tc, &mut tool_calls_accum);
                                }
                            }
                        }
                    }
                }
            }
            Ok(())
        });

        sse_outcome?;

        // Convert accumulated tool_calls into a sorted array. Matches Python
        // `sorted(tool_calls_accum)` semantics.
        let tool_calls: Vec<Value> = tool_calls_accum.into_values().collect();
        let latency_ms = start.elapsed().as_secs_f64() * 1000.0;

        // Persist request + response payload + session envelope. Each call is
        // a separate CAS write — the envelope links the two by hash so the
        // session-level CAS entry deduplicates across replays of the same
        // exchange.
        let (request_hash, _) = self
            .engine
            .write_content_tracked(request_bytes)
            .map_err(|e| format!("persist request: {e}"))?;

        let response_payload = json!({
            "model": collected_model,
            "content": collected_text,
            "finish_reason": finish_reason.clone().unwrap_or_else(|| "stop".to_string()),
            "usage": usage.clone().unwrap_or(Value::Object(Map::new())),
            "latency_ms": round_one_decimal(latency_ms),
        });
        let response_bytes = serde_json::to_vec(&response_payload)
            .map_err(|e| format!("response serialize: {e}"))?;
        let (response_hash, _) = self
            .engine
            .write_content_tracked(&response_bytes)
            .map_err(|e| format!("persist response: {e}"))?;

        let session = json!({
            "type": "llm_session_v1",
            "request_hash": request_hash,
            "response_hash": response_hash,
            "model": collected_model,
            "latency_ms": round_one_decimal(latency_ms),
        });
        let session_bytes =
            serde_json::to_vec(&session).map_err(|e| format!("session serialize: {e}"))?;
        let (session_hash, _) = self
            .engine
            .write_content_tracked(&session_bytes)
            .map_err(|e| format!("persist session: {e}"))?;

        // Emit the terminal `done` control frame so clients can correlate the
        // stream back to the persisted session envelope.
        let mut done = json!({
            "type": "done",
            "session_hash": session_hash,
            "model": collected_model,
            "latency_ms": round_one_decimal(latency_ms),
            "finish_reason": finish_reason.unwrap_or_else(|| "stop".to_string()),
            "usage": usage.unwrap_or(Value::Object(Map::new())),
        });
        if !tool_calls.is_empty() {
            if let Some(obj) = done.as_object_mut() {
                obj.insert("tool_calls".to_string(), Value::Array(tool_calls));
            }
        }
        let done_bytes = serde_json::to_vec(&done).unwrap_or_default();
        let _ = stream_manager.write_nowait(stream_path, &done_bytes);
        let _ = stream_manager.close(stream_path);

        Ok(())
    }
}

/// Fold an incremental OpenAI `tool_calls[]` delta into the accumulator.
///
/// OpenAI streams tool calls by `index`; each frame may carry a fresh `id`,
/// a partial `function.name`, or a partial `function.arguments` fragment
/// that must be concatenated. The accumulator stores one `Value` per index.
fn accumulate_tool_call(tc: &Value, accum: &mut std::collections::BTreeMap<u64, Value>) {
    let idx = tc.get("index").and_then(|i| i.as_u64()).unwrap_or(0);
    let entry = accum.entry(idx).or_insert_with(|| {
        json!({
            "id": "",
            "type": "function",
            "function": {"name": "", "arguments": ""},
        })
    });

    if let Some(id) = tc.get("id").and_then(|i| i.as_str()) {
        if let Some(obj) = entry.as_object_mut() {
            obj.insert("id".to_string(), Value::String(id.to_string()));
        }
    }
    if let Some(ty) = tc.get("type").and_then(|t| t.as_str()) {
        if let Some(obj) = entry.as_object_mut() {
            obj.insert("type".to_string(), Value::String(ty.to_string()));
        }
    }
    if let Some(func) = tc.get("function") {
        let entry_func = entry
            .get_mut("function")
            .and_then(|f| f.as_object_mut())
            .unwrap();
        if let Some(name) = func.get("name").and_then(|n| n.as_str()) {
            let existing = entry_func
                .get("name")
                .and_then(|n| n.as_str())
                .unwrap_or("")
                .to_string();
            entry_func.insert("name".to_string(), Value::String(existing + name));
        }
        if let Some(args) = func.get("arguments").and_then(|a| a.as_str()) {
            let existing = entry_func
                .get("arguments")
                .and_then(|a| a.as_str())
                .unwrap_or("")
                .to_string();
            entry_func.insert("arguments".to_string(), Value::String(existing + args));
        }
    }
}

/// Find the first "\n\n" in `buf`, returning its offset.
fn twoline(buf: &[u8]) -> Option<usize> {
    buf.windows(2).position(|w| w == b"\n\n")
}

/// Strip a trailing '\r' from `line` if present — SSE frames use CRLF.
fn strip_cr(line: &[u8]) -> &[u8] {
    if line.last() == Some(&b'\r') {
        &line[..line.len() - 1]
    } else {
        line
    }
}

/// Round to one decimal place to match Python `round(x, 1)` JSON output.
fn round_one_decimal(x: f64) -> f64 {
    (x * 10.0).round() / 10.0
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Arc;
    use tempfile::TempDir;

    fn build_backend(tmp: &TempDir, base_url: &str) -> OpenAIBackend {
        let rt = crate::peer_blob_client::build_kernel_runtime();
        OpenAIBackend::new(
            "openai_compatible",
            base_url,
            "sk-test",
            "gpt-4o",
            tmp.path(),
            rt,
        )
        .unwrap()
    }

    fn spin_up_sse(body: &'static str) -> (std::net::TcpListener, String) {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        let url = format!("http://{}", addr);
        std::thread::spawn(move || {
            use std::io::{Read, Write};
            if let Ok((mut sock, _)) = listener.accept() {
                let mut buf = [0u8; 4096];
                let _ = sock.read(&mut buf);
                let resp = format!(
                    "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: {}\r\n\r\n{}",
                    body.len(),
                    body
                );
                let _ = sock.write_all(resp.as_bytes());
                let _ = sock.flush();
            }
        });
        (std::net::TcpListener::bind("127.0.0.1:0").unwrap(), url)
    }

    // NOTE: the SSE integration tests run a single-shot HTTP server on a
    // background thread. We wire a minimal listener so the rustls/reqwest
    // HTTP client has something to connect to over plain HTTP.

    fn serve_once(body: String) -> (std::thread::JoinHandle<()>, String) {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        let url = format!("http://{}", addr);
        let handle = std::thread::spawn(move || {
            use std::io::{Read, Write};
            if let Ok((mut sock, _)) = listener.accept() {
                let mut buf = vec![0u8; 8192];
                let _ = sock.read(&mut buf);
                let resp = format!(
                    "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                    body.len(),
                    body
                );
                let _ = sock.write_all(resp.as_bytes());
                let _ = sock.flush();
            }
        });
        (handle, url)
    }

    fn serve_once_status(status: u16, body: String) -> (std::thread::JoinHandle<()>, String) {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        let url = format!("http://{}", addr);
        let handle = std::thread::spawn(move || {
            use std::io::{Read, Write};
            if let Ok((mut sock, _)) = listener.accept() {
                let mut buf = vec![0u8; 8192];
                let _ = sock.read(&mut buf);
                let reason = match status {
                    500 => "Internal Server Error",
                    429 => "Too Many Requests",
                    _ => "Error",
                };
                let resp = format!(
                    "HTTP/1.1 {} {}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                    status,
                    reason,
                    body.len(),
                    body
                );
                let _ = sock.write_all(resp.as_bytes());
                let _ = sock.flush();
            }
        });
        (handle, url)
    }

    fn sse_happy() -> String {
        // Two token frames + finish + usage + [DONE]. Deliberately uses
        // mixed framing (CRLF vs LF) to exercise the frame parser.
        let frames = vec![
            r#"data: {"model":"gpt-4o","choices":[{"index":0,"delta":{"content":"Hel"}}]}"#,
            r#"data: {"model":"gpt-4o","choices":[{"index":0,"delta":{"content":"lo"}}]}"#,
            r#"data: {"model":"gpt-4o","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}"#,
            r#"data: {"model":"gpt-4o","usage":{"prompt_tokens":3,"completion_tokens":2,"total_tokens":5}}"#,
            r#"data: [DONE]"#,
        ];
        let mut out = String::new();
        for f in frames {
            out.push_str(f);
            out.push_str("\n\n");
        }
        out
    }

    fn sse_with_tool_calls() -> String {
        let frames = vec![
            r#"data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_1","type":"function","function":{"name":"run_","arguments":"{\"arg"}}]}}]}"#,
            r#"data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"name":"it","arguments":"\":1}"}}]}}]}"#,
            r#"data: {"choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}"#,
            r#"data: [DONE]"#,
        ];
        let mut out = String::new();
        for f in frames {
            out.push_str(f);
            out.push_str("\n\n");
        }
        out
    }

    fn build_request() -> Vec<u8> {
        br#"{"messages":[{"role":"user","content":"hi"}],"model":"gpt-4o"}"#.to_vec()
    }

    #[test]
    fn test_openai_backend_run_streaming_happy_path() {
        let tmp = TempDir::new().unwrap();
        let (_h, url) = serve_once(sse_happy());
        let backend = build_backend(&tmp, &url);
        let sm = Arc::new(StreamManager::new());
        let stream_path = "/llm/stream/happy";
        sm.create(stream_path, 1024 * 64).unwrap();

        let req = build_request();
        backend
            .run_streaming(&req, stream_path, &sm)
            .expect("streaming succeeds");

        // Stream should contain "Hello" followed by a JSON `done` control frame.
        let payload = sm.collect_all_payloads(stream_path).unwrap();
        let payload_str = String::from_utf8(payload).unwrap();
        assert!(payload_str.starts_with("Hello"), "got: {payload_str}");
        // The done frame is a single serde_json::to_vec(&done) call at the
        // end — key order follows serde_json's default (alphabetical), so we
        // locate it by the first `{` after the content prefix.
        let done_idx = payload_str.find('{').expect("missing done frame");
        let done_json: Value =
            serde_json::from_str(&payload_str[done_idx..]).expect("done frame is valid JSON");
        assert_eq!(done_json["type"], "done");
        assert_eq!(done_json["model"], "gpt-4o");
        assert_eq!(done_json["finish_reason"], "stop");
        assert!(done_json["session_hash"].as_str().unwrap().len() == 64);

        // Envelope is persisted + readable via the CAS surface.
        let session_hash = done_json["session_hash"].as_str().unwrap();
        let bytes = backend.engine.read_content(session_hash).unwrap();
        let session: Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(session["type"], "llm_session_v1");
        let req_hash = session["request_hash"].as_str().unwrap();
        let req_bytes = backend.engine.read_content(req_hash).unwrap();
        assert_eq!(req_bytes, req);
    }

    #[test]
    fn test_openai_backend_run_streaming_tool_calls() {
        let tmp = TempDir::new().unwrap();
        let (_h, url) = serve_once(sse_with_tool_calls());
        let backend = build_backend(&tmp, &url);
        let sm = Arc::new(StreamManager::new());
        let stream_path = "/llm/stream/tools";
        sm.create(stream_path, 1024 * 64).unwrap();

        let req = build_request();
        backend.run_streaming(&req, stream_path, &sm).unwrap();

        let payload = sm.collect_all_payloads(stream_path).unwrap();
        let s = String::from_utf8(payload).unwrap();
        let done_idx = s.find('{').unwrap();
        let done: Value = serde_json::from_str(&s[done_idx..]).unwrap();
        assert_eq!(done["type"], "done");
        let tool_calls = done["tool_calls"].as_array().expect("tool_calls present");
        assert_eq!(tool_calls.len(), 1);
        let tc0 = &tool_calls[0];
        assert_eq!(tc0["id"], "call_1");
        assert_eq!(tc0["type"], "function");
        assert_eq!(tc0["function"]["name"], "run_it");
        assert_eq!(tc0["function"]["arguments"], "{\"arg\":1}");
    }

    #[test]
    fn test_openai_backend_run_streaming_error_path() {
        let tmp = TempDir::new().unwrap();
        let (_h, url) = serve_once_status(500, r#"{"error":"boom"}"#.to_string());
        let backend = build_backend(&tmp, &url);
        let sm = Arc::new(StreamManager::new());
        let stream_path = "/llm/stream/err";
        sm.create(stream_path, 1024 * 16).unwrap();

        let req = build_request();
        let err = backend.run_streaming(&req, stream_path, &sm).unwrap_err();
        assert!(err.contains("500"));

        // Error frame present, no partial persist (no session hash at all).
        let payload = sm.collect_all_payloads(stream_path).unwrap();
        let s = String::from_utf8(payload).unwrap();
        // serde_json sorts keys alphabetically so "type" may not be first.
        let err_idx = s.find('{').expect("missing error frame");
        let err: Value = serde_json::from_str(&s[err_idx..]).expect("error frame is valid JSON");
        assert_eq!(err["type"], "error");
    }
}
