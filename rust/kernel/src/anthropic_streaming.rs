//! Anthropic streaming pipeline — SSE event decode → DT_STREAM → CAS persist.
//!
//! Called from `PyKernel::llm_start_streaming` under `py.detach(...)`, same as
//! the OpenAI path. Writes text deltas to the DT_STREAM, persists the session
//! envelope via `CASEngine::write_content_tracked`, emits a terminal `done`
//! control frame carrying the session hash. All under the shared tokio
//! runtime — no per-backend worker pools.
//!
//! Wire shape (Anthropic Messages API):
//!   - POST `{base_url}/v1/messages` with `x-api-key`, `anthropic-version`.
//!   - Request body: top-level `system` string, `tools[].input_schema`,
//!     assistant tool_use blocks, user `tool_result` blocks — converted from
//!     OpenAI-style `messages` arrays by `convert_messages`/`convert_tools`.
//!   - SSE frames arrive as `event: X\ndata: Y\n\n` pairs. A small state
//!     machine pairs each `event:` line with its next `data:` JSON. Event
//!     types: `message_start`, `content_block_start`, `content_block_delta`,
//!     `content_block_stop`, `message_delta`, `message_stop`. Text deltas
//!     land via `text_delta`; tool JSON accumulates via `input_json_delta`.
//!
//! Stop reason mapping: `end_turn`/`stop_sequence` → `stop`, `tool_use` →
//! `tool_calls`, `max_tokens` → `length`.

#![allow(dead_code)]

use std::sync::Arc;

use futures::StreamExt;
use serde_json::{json, Map, Value};

use crate::anthropic_backend::AnthropicBackend;
use crate::openai_streaming::LlmStreamingBackend;
use crate::stream_manager::StreamManager;

impl LlmStreamingBackend for AnthropicBackend {
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

impl AnthropicBackend {
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
        let max_tokens = request
            .get("max_tokens")
            .and_then(|v| v.as_u64())
            .unwrap_or(8192);

        // Build the Anthropic-shape body. `system` is extracted either from
        // the explicit top-level `system` field or from an inline
        // {"role":"system"} message (matches Python shim semantics).
        let mut body = Map::new();
        body.insert("model".to_string(), Value::String(model.clone()));
        body.insert(
            "messages".to_string(),
            Value::Array(convert_messages(&messages)),
        );
        body.insert("max_tokens".to_string(), Value::from(max_tokens));
        body.insert("stream".to_string(), Value::Bool(true));

        let system = request
            .get("system")
            .and_then(|s| s.as_str())
            .map(|s| s.to_string())
            .or_else(|| extract_inline_system(&messages));
        if let Some(s) = system {
            if !s.is_empty() {
                body.insert("system".to_string(), Value::String(s));
            }
        }

        if let Some(tools) = request.get("tools") {
            if let Some(arr) = tools.as_array() {
                body.insert("tools".to_string(), Value::Array(convert_tools(arr)));
            }
        }

        for key in ["temperature", "top_p", "top_k", "stop_sequences"] {
            if let Some(v) = request.get(key) {
                body.insert(key.to_string(), v.clone());
            }
        }

        let url = format!("{}/v1/messages", self.base_url);
        let http = self.http.clone();
        let api_key = self.api_key.clone();

        let start = std::time::Instant::now();
        let stream_manager_clone = Arc::clone(stream_manager);
        let stream_path_owned = stream_path.to_string();

        let mut collected_text = String::new();
        let mut finish_reason: Option<String> = None;
        let mut collected_model = model.clone();
        let mut input_tokens: Option<u64> = None;
        let mut output_tokens: Option<u64> = None;
        // Anthropic streams tool blocks by block-index; each block runs through
        // `content_block_start` → many `content_block_delta` → `content_block_stop`.
        let mut current_tool: Option<Value> = None;
        let mut tool_calls: Vec<Value> = Vec::new();

        let sse_outcome: Result<(), String> = self.runtime.block_on(async {
            let resp = http
                .post(&url)
                .header("x-api-key", api_key)
                .header("anthropic-version", "2023-06-01")
                .header("Content-Type", "application/json")
                .header("Accept", "text/event-stream")
                .body(serde_json::to_vec(&Value::Object(body)).map_err(|e| format!("body: {e}"))?)
                .send()
                .await
                .map_err(|e| format!("HTTP: {e}"))?;

            if !resp.status().is_success() {
                let status = resp.status();
                let body = resp.text().await.unwrap_or_default();
                return Err(format!("Anthropic API {status}: {body}"));
            }

            let mut byte_stream = resp.bytes_stream();
            let mut buf: Vec<u8> = Vec::with_capacity(4096);

            while let Some(chunk) = byte_stream.next().await {
                let chunk = chunk.map_err(|e| format!("SSE read: {e}"))?;
                buf.extend_from_slice(&chunk);

                while let Some(sep) = twoline(&buf) {
                    let frame = buf[..sep].to_vec();
                    buf.drain(..sep + 2);

                    // Each frame carries one `event: <name>` line followed by
                    // one `data: <json>` line. Within a frame we track the
                    // event name so the data line can be routed.
                    let mut frame_event: Option<String> = None;
                    let mut frame_data: Option<Vec<u8>> = None;
                    for line in frame.split(|b| *b == b'\n') {
                        let line = strip_cr(line);
                        if let Some(name) = line.strip_prefix(b"event: ") {
                            frame_event = Some(std::str::from_utf8(name).unwrap_or("").to_string());
                        } else if let Some(data) = line.strip_prefix(b"data: ") {
                            frame_data = Some(data.to_vec());
                        }
                    }
                    let (Some(event), Some(data)) = (frame_event, frame_data) else {
                        continue;
                    };
                    let parsed: Value = match serde_json::from_slice(&data) {
                        Ok(v) => v,
                        Err(_) => continue,
                    };

                    match event.as_str() {
                        "message_start" => {
                            if let Some(msg) = parsed.get("message") {
                                if let Some(m) = msg.get("model").and_then(|m| m.as_str()) {
                                    collected_model = m.to_string();
                                }
                                if let Some(u) = msg
                                    .get("usage")
                                    .and_then(|u| u.get("input_tokens"))
                                    .and_then(|i| i.as_u64())
                                {
                                    input_tokens = Some(u);
                                }
                            }
                        }
                        "content_block_start" => {
                            if let Some(block) = parsed.get("content_block") {
                                if block.get("type").and_then(|t| t.as_str()) == Some("tool_use") {
                                    let id = block
                                        .get("id")
                                        .and_then(|i| i.as_str())
                                        .unwrap_or("")
                                        .to_string();
                                    let name = block
                                        .get("name")
                                        .and_then(|n| n.as_str())
                                        .unwrap_or("")
                                        .to_string();
                                    current_tool = Some(json!({
                                        "id": id,
                                        "type": "function",
                                        "function": {
                                            "name": name,
                                            "arguments": "",
                                        },
                                    }));
                                }
                            }
                        }
                        "content_block_delta" => {
                            if let Some(delta) = parsed.get("delta") {
                                let dt = delta.get("type").and_then(|t| t.as_str());
                                if dt == Some("text_delta") {
                                    if let Some(text) = delta.get("text").and_then(|t| t.as_str()) {
                                        if !text.is_empty() {
                                            collected_text.push_str(text);
                                            if let Err(e) = stream_manager_clone
                                                .write_nowait(&stream_path_owned, text.as_bytes())
                                            {
                                                return Err(format!("stream write: {e:?}"));
                                            }
                                        }
                                    }
                                } else if dt == Some("input_json_delta") {
                                    if let (Some(tool), Some(frag)) = (
                                        current_tool.as_mut(),
                                        delta.get("partial_json").and_then(|p| p.as_str()),
                                    ) {
                                        let func = tool
                                            .get_mut("function")
                                            .and_then(|f| f.as_object_mut())
                                            .unwrap();
                                        let existing = func
                                            .get("arguments")
                                            .and_then(|a| a.as_str())
                                            .unwrap_or("")
                                            .to_string();
                                        func.insert(
                                            "arguments".to_string(),
                                            Value::String(existing + frag),
                                        );
                                    }
                                }
                            }
                        }
                        "content_block_stop" => {
                            if let Some(tool) = current_tool.take() {
                                tool_calls.push(tool);
                            }
                        }
                        "message_delta" => {
                            if let Some(delta) = parsed.get("delta") {
                                if let Some(reason) =
                                    delta.get("stop_reason").and_then(|r| r.as_str())
                                {
                                    finish_reason = Some(reason.to_string());
                                }
                            }
                            if let Some(u) = parsed
                                .get("usage")
                                .and_then(|u| u.get("output_tokens"))
                                .and_then(|i| i.as_u64())
                            {
                                output_tokens = Some(u);
                            }
                        }
                        "message_stop" => {
                            return Ok(());
                        }
                        _ => {}
                    }
                }
            }
            Ok(())
        });

        sse_outcome?;

        let latency_ms = start.elapsed().as_secs_f64() * 1000.0;
        let mapped_finish = map_finish_reason(finish_reason.as_deref());

        // Persist request + response + envelope (3 CAS writes, same as
        // OpenAIBackend). The envelope keys the session by content hash so
        // identical exchanges dedup to a single session entry.
        let (request_hash, _) = self
            .engine
            .write_content_tracked(request_bytes)
            .map_err(|e| format!("persist request: {e}"))?;

        let prompt_tokens = input_tokens.unwrap_or(0);
        let completion_tokens = output_tokens.unwrap_or(0);
        let usage = json!({
            "input_tokens": prompt_tokens,
            "output_tokens": completion_tokens,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        });

        let response_payload = json!({
            "model": collected_model,
            "content": collected_text,
            "finish_reason": mapped_finish,
            "usage": usage,
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

        let mut done = json!({
            "type": "done",
            "session_hash": session_hash,
            "model": collected_model,
            "latency_ms": round_one_decimal(latency_ms),
            "finish_reason": mapped_finish,
            "usage": usage,
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

/// Convert OpenAI-shaped messages to Anthropic Messages API shape.
///
/// - `role:"system"` entries are stripped (hoisted to the top-level
///   `system` field by `extract_inline_system`).
/// - `role:"tool"` entries become `user` messages with a single
///   `tool_result` content block carrying the tool_call_id + content.
/// - `role:"assistant"` with `tool_calls` becomes a message whose content
///   is `[{type:"text",...}, {type:"tool_use",...}]` blocks — the tool_use
///   block's `input` is the parsed JSON of `function.arguments` (falling
///   back to `{}` if parsing fails).
/// - Anthropic requires the first message role to be `user` — if the
///   converted list doesn't start with one, prepend `{"role":"user",
///   "content":"Continue."}` (matches the Python shim).
pub(crate) fn convert_messages(messages: &Value) -> Vec<Value> {
    let Some(arr) = messages.as_array() else {
        return vec![];
    };
    let mut converted: Vec<Value> = Vec::with_capacity(arr.len());
    for msg in arr {
        let role = msg.get("role").and_then(|r| r.as_str()).unwrap_or("");
        if role == "system" {
            continue;
        }
        if role == "tool" {
            let tool_use_id = msg
                .get("tool_call_id")
                .and_then(|i| i.as_str())
                .unwrap_or("")
                .to_string();
            let content = msg
                .get("content")
                .cloned()
                .unwrap_or_else(|| Value::String(String::new()));
            converted.push(json!({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": content,
                }],
            }));
            continue;
        }

        let has_tool_calls = role == "assistant"
            && msg
                .get("tool_calls")
                .and_then(|t| t.as_array())
                .map(|a| !a.is_empty())
                .unwrap_or(false);

        if has_tool_calls {
            let mut blocks: Vec<Value> = Vec::new();
            if let Some(text) = msg.get("content").and_then(|c| c.as_str()) {
                if !text.is_empty() {
                    blocks.push(json!({"type": "text", "text": text}));
                }
            }
            if let Some(tcs) = msg.get("tool_calls").and_then(|t| t.as_array()) {
                for tc in tcs {
                    let id = tc
                        .get("id")
                        .and_then(|i| i.as_str())
                        .unwrap_or("")
                        .to_string();
                    let func = tc.get("function").cloned().unwrap_or(Value::Null);
                    let name = func
                        .get("name")
                        .and_then(|n| n.as_str())
                        .unwrap_or("")
                        .to_string();
                    let args_str = func
                        .get("arguments")
                        .and_then(|a| a.as_str())
                        .unwrap_or("{}");
                    let input: Value = serde_json::from_str(args_str).unwrap_or_else(|_| json!({}));
                    blocks.push(json!({
                        "type": "tool_use",
                        "id": id,
                        "name": name,
                        "input": input,
                    }));
                }
            }
            converted.push(json!({
                "role": "assistant",
                "content": blocks,
            }));
        } else {
            let content = msg
                .get("content")
                .cloned()
                .unwrap_or_else(|| Value::String(String::new()));
            converted.push(json!({
                "role": role,
                "content": content,
            }));
        }
    }
    if let Some(first) = converted.first() {
        if first.get("role").and_then(|r| r.as_str()) != Some("user") {
            converted.insert(0, json!({"role": "user", "content": "Continue."}));
        }
    }
    converted
}

/// Convert OpenAI tool-schema array into Anthropic `tools` shape.
pub(crate) fn convert_tools(tools: &[Value]) -> Vec<Value> {
    tools
        .iter()
        .filter(|t| t.get("type").and_then(|v| v.as_str()) == Some("function"))
        .map(|t| {
            let func = t.get("function").cloned().unwrap_or(Value::Null);
            let name = func
                .get("name")
                .and_then(|n| n.as_str())
                .unwrap_or("")
                .to_string();
            let description = func
                .get("description")
                .and_then(|d| d.as_str())
                .unwrap_or("")
                .to_string();
            let schema = func
                .get("parameters")
                .cloned()
                .unwrap_or_else(|| json!({"type": "object"}));
            json!({
                "name": name,
                "description": description,
                "input_schema": schema,
            })
        })
        .collect()
}

fn extract_inline_system(messages: &Value) -> Option<String> {
    messages.as_array()?.iter().find_map(|m| {
        if m.get("role").and_then(|r| r.as_str()) == Some("system") {
            m.get("content").and_then(|c| c.as_str()).map(String::from)
        } else {
            None
        }
    })
}

fn map_finish_reason(reason: Option<&str>) -> String {
    match reason {
        Some("end_turn") => "stop",
        Some("tool_use") => "tool_calls",
        Some("max_tokens") => "length",
        Some("stop_sequence") => "stop",
        Some(other) => other,
        None => "stop",
    }
    .to_string()
}

fn twoline(buf: &[u8]) -> Option<usize> {
    buf.windows(2).position(|w| w == b"\n\n")
}

fn strip_cr(line: &[u8]) -> &[u8] {
    if line.last() == Some(&b'\r') {
        &line[..line.len() - 1]
    } else {
        line
    }
}

fn round_one_decimal(x: f64) -> f64 {
    (x * 10.0).round() / 10.0
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Arc;
    use tempfile::TempDir;

    fn build_backend(tmp: &TempDir, base_url: &str) -> AnthropicBackend {
        let rt = crate::peer_blob_client::build_kernel_runtime();
        AnthropicBackend::new(
            "anthropic_native",
            base_url,
            "sk-ant-test",
            "claude-sonnet-4-20250514",
            tmp.path(),
            rt,
        )
        .unwrap()
    }

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

    fn sse_frame(event: &str, data: &str) -> String {
        format!("event: {event}\ndata: {data}\n\n")
    }

    fn sse_happy() -> String {
        let mut out = String::new();
        out.push_str(&sse_frame(
            "message_start",
            r#"{"type":"message_start","message":{"model":"claude-sonnet-4-20250514","usage":{"input_tokens":3}}}"#,
        ));
        out.push_str(&sse_frame(
            "content_block_start",
            r#"{"index":0,"content_block":{"type":"text","text":""}}"#,
        ));
        out.push_str(&sse_frame(
            "content_block_delta",
            r#"{"index":0,"delta":{"type":"text_delta","text":"Hel"}}"#,
        ));
        out.push_str(&sse_frame(
            "content_block_delta",
            r#"{"index":0,"delta":{"type":"text_delta","text":"lo"}}"#,
        ));
        out.push_str(&sse_frame("content_block_stop", r#"{"index":0}"#));
        out.push_str(&sse_frame(
            "message_delta",
            r#"{"delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":2}}"#,
        ));
        out.push_str(&sse_frame("message_stop", r#"{}"#));
        out
    }

    fn sse_with_tool_use() -> String {
        let mut out = String::new();
        out.push_str(&sse_frame(
            "message_start",
            r#"{"message":{"model":"claude-sonnet-4-20250514","usage":{"input_tokens":5}}}"#,
        ));
        out.push_str(&sse_frame(
            "content_block_start",
            r#"{"index":0,"content_block":{"type":"tool_use","id":"toolu_1","name":"run_it","input":{}}}"#,
        ));
        out.push_str(&sse_frame(
            "content_block_delta",
            r#"{"index":0,"delta":{"type":"input_json_delta","partial_json":"{\"arg"}}"#,
        ));
        out.push_str(&sse_frame(
            "content_block_delta",
            r#"{"index":0,"delta":{"type":"input_json_delta","partial_json":"\":1}"}}"#,
        ));
        out.push_str(&sse_frame("content_block_stop", r#"{"index":0}"#));
        out.push_str(&sse_frame(
            "message_delta",
            r#"{"delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":7}}"#,
        ));
        out.push_str(&sse_frame("message_stop", r#"{}"#));
        out
    }

    fn build_request() -> Vec<u8> {
        br#"{"messages":[{"role":"user","content":"hi"}],"model":"claude-sonnet-4-20250514"}"#
            .to_vec()
    }

    #[test]
    fn test_anthropic_backend_run_streaming_happy_path() {
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

        let payload = sm.collect_all_payloads(stream_path).unwrap();
        let s = String::from_utf8(payload).unwrap();
        assert!(s.starts_with("Hello"), "got: {s}");
        let done_idx = s.find('{').expect("missing done frame");
        let done: Value = serde_json::from_str(&s[done_idx..]).unwrap();
        assert_eq!(done["type"], "done");
        assert_eq!(done["model"], "claude-sonnet-4-20250514");
        assert_eq!(done["finish_reason"], "stop");
        assert!(done["session_hash"].as_str().unwrap().len() == 64);

        let session_hash = done["session_hash"].as_str().unwrap();
        let bytes = backend.engine.read_content(session_hash).unwrap();
        let session: Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(session["type"], "llm_session_v1");
        let req_hash = session["request_hash"].as_str().unwrap();
        let req_bytes = backend.engine.read_content(req_hash).unwrap();
        assert_eq!(req_bytes, req);
    }

    #[test]
    fn test_anthropic_backend_run_streaming_tool_use() {
        let tmp = TempDir::new().unwrap();
        let (_h, url) = serve_once(sse_with_tool_use());
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
        assert_eq!(done["finish_reason"], "tool_calls");
        let tool_calls = done["tool_calls"].as_array().expect("tool_calls present");
        assert_eq!(tool_calls.len(), 1);
        let tc0 = &tool_calls[0];
        assert_eq!(tc0["id"], "toolu_1");
        assert_eq!(tc0["type"], "function");
        assert_eq!(tc0["function"]["name"], "run_it");
        assert_eq!(tc0["function"]["arguments"], "{\"arg\":1}");
    }

    #[test]
    fn test_anthropic_backend_run_streaming_error_path() {
        let tmp = TempDir::new().unwrap();
        let (_h, url) = serve_once_status(500, r#"{"error":"boom"}"#.to_string());
        let backend = build_backend(&tmp, &url);
        let sm = Arc::new(StreamManager::new());
        let stream_path = "/llm/stream/err";
        sm.create(stream_path, 1024 * 16).unwrap();

        let req = build_request();
        let err = backend.run_streaming(&req, stream_path, &sm).unwrap_err();
        assert!(err.contains("500"));

        let payload = sm.collect_all_payloads(stream_path).unwrap();
        let s = String::from_utf8(payload).unwrap();
        let err_idx = s.find('{').expect("missing error frame");
        let err_json: Value = serde_json::from_str(&s[err_idx..]).unwrap();
        assert_eq!(err_json["type"], "error");
    }

    #[test]
    fn test_anthropic_convert_messages_system_extraction() {
        // system role message gets stripped from the messages array (it
        // gets hoisted via extract_inline_system separately).
        let messages = json!([
            {"role": "system", "content": "you are helpful"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]);
        let converted = convert_messages(&messages);
        assert_eq!(converted.len(), 2);
        assert_eq!(converted[0]["role"], "user");
        assert_eq!(converted[0]["content"], "hi");
        assert_eq!(converted[1]["role"], "assistant");

        let system = extract_inline_system(&messages).unwrap();
        assert_eq!(system, "you are helpful");
    }

    #[test]
    fn test_anthropic_convert_messages_tool_result_mapping() {
        let messages = json!([
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "run_it", "arguments": "{\"arg\":1}"},
                }],
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "content": "42",
            },
        ]);
        let converted = convert_messages(&messages);
        assert_eq!(converted.len(), 3);

        // Assistant message w/ tool_calls becomes content-block array.
        let blocks = converted[1]["content"].as_array().unwrap();
        assert_eq!(blocks.len(), 1);
        assert_eq!(blocks[0]["type"], "tool_use");
        assert_eq!(blocks[0]["id"], "call_1");
        assert_eq!(blocks[0]["name"], "run_it");
        assert_eq!(blocks[0]["input"]["arg"], 1);

        // Tool role becomes user message w/ tool_result block.
        assert_eq!(converted[2]["role"], "user");
        let tr_blocks = converted[2]["content"].as_array().unwrap();
        assert_eq!(tr_blocks[0]["type"], "tool_result");
        assert_eq!(tr_blocks[0]["tool_use_id"], "call_1");
        assert_eq!(tr_blocks[0]["content"], "42");
    }

    #[test]
    fn test_anthropic_convert_messages_prepends_user_when_first_is_assistant() {
        // Anthropic Messages API rejects leading assistant turn — the
        // converter prepends a {"role":"user","content":"Continue."} msg.
        let messages = json!([
            {"role": "assistant", "content": "I was here first"},
            {"role": "user", "content": "oh"},
        ]);
        let converted = convert_messages(&messages);
        assert_eq!(converted.len(), 3);
        assert_eq!(converted[0]["role"], "user");
        assert_eq!(converted[0]["content"], "Continue.");
        assert_eq!(converted[1]["role"], "assistant");
    }

    #[test]
    fn test_anthropic_convert_tools() {
        let tools = vec![json!({
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Return the weather for a city",
                "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
            },
        })];
        let converted = convert_tools(&tools);
        assert_eq!(converted.len(), 1);
        assert_eq!(converted[0]["name"], "get_weather");
        assert_eq!(converted[0]["description"], "Return the weather for a city");
        assert_eq!(
            converted[0]["input_schema"]["properties"]["city"]["type"],
            "string"
        );
    }
}
