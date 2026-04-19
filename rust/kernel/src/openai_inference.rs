//! OpenAI inference pyfunctions — GIL-free HTTP calls (§10 D3).
//!
//! Standalone pyfunctions that release the GIL during HTTP I/O.
//! Used by Python LLMStreamingService to avoid holding GIL
//! during network round-trips to OpenAI-compatible APIs.

#![allow(dead_code)]

use pyo3::prelude::*;
use pyo3::types::PyBytes;
use std::sync::OnceLock;
use std::time::Duration;

/// Default per-request timeout for OpenAI-compatible endpoints. Chosen to
/// be longer than most hosted models' p99 latency but short enough that a
/// stuck endpoint cannot deadlock a Python caller forever (§ review fix #8).
const DEFAULT_HTTP_TIMEOUT: Duration = Duration::from_secs(120);

/// Lazily-initialized multi-thread tokio runtime. Reused across every call
/// instead of building a fresh single-threaded runtime per request (which
/// previously cost ~milliseconds and defeated the GIL-free goal).
fn runtime() -> &'static tokio::runtime::Runtime {
    static RT: OnceLock<tokio::runtime::Runtime> = OnceLock::new();
    RT.get_or_init(|| {
        tokio::runtime::Builder::new_multi_thread()
            .worker_threads(2)
            .enable_all()
            .thread_name("nexus-openai")
            .build()
            .expect("failed to build tokio runtime for openai_inference")
    })
}

/// Shared `reqwest::Client` — reuses TLS sessions and HTTP/2 connections
/// across calls. Constructing a client per call was prohibitively expensive.
fn http_client() -> &'static reqwest::Client {
    static CLIENT: OnceLock<reqwest::Client> = OnceLock::new();
    CLIENT.get_or_init(|| {
        reqwest::Client::builder()
            .pool_idle_timeout(Duration::from_secs(90))
            .tcp_keepalive(Duration::from_secs(30))
            .timeout(DEFAULT_HTTP_TIMEOUT)
            .build()
            .expect("failed to build reqwest client for openai_inference")
    })
}

/// Synchronous OpenAI chat completion — releases GIL during HTTP.
///
/// Returns response JSON as bytes. Caller parses in Python.
/// Compatible with: OpenAI, SudoRouter, OpenRouter, Ollama, vLLM.
#[pyfunction]
#[pyo3(signature = (base_url, api_key, messages_json, model="gpt-4o", temperature=None, max_tokens=None))]
pub fn openai_chat_completion<'py>(
    py: Python<'py>,
    base_url: &str,
    api_key: &str,
    messages_json: &[u8],
    model: &str,
    temperature: Option<f64>,
    max_tokens: Option<u32>,
) -> PyResult<Bound<'py, PyBytes>> {
    let url = format!("{}/chat/completions", base_url.trim_end_matches('/'));
    let msgs = messages_json.to_vec();
    let api_key = api_key.to_string();
    let model = model.to_string();

    let result = py.detach(move || -> Result<Vec<u8>, String> {
        runtime().block_on(async {
            let messages: serde_json::Value =
                serde_json::from_slice(&msgs).map_err(|e| format!("JSON parse: {e}"))?;

            let mut body = serde_json::json!({
                "model": model,
                "messages": messages,
                "stream": false,
            });
            if let Some(t) = temperature {
                body["temperature"] = serde_json::json!(t);
            }
            if let Some(mt) = max_tokens {
                body["max_tokens"] = serde_json::json!(mt);
            }

            let resp = http_client()
                .post(&url)
                .header("Authorization", format!("Bearer {api_key}"))
                .header("Content-Type", "application/json")
                .body(body.to_string())
                .send()
                .await
                .map_err(|e| format!("HTTP: {e}"))?;

            if !resp.status().is_success() {
                let status = resp.status();
                let text = resp.text().await.unwrap_or_default();
                return Err(format!("OpenAI API {status}: {text}"));
            }

            resp.bytes()
                .await
                .map(|b| b.to_vec())
                .map_err(|e| format!("read: {e}"))
        })
    });

    match result {
        Ok(data) => Ok(PyBytes::new(py, &data)),
        Err(msg) => Err(pyo3::exceptions::PyRuntimeError::new_err(msg)),
    }
}

/// Streaming OpenAI chat completion — collects tokens, releases GIL.
///
/// Returns collected content string. For true token-by-token streaming,
/// use Python LLMStreamingService which pumps tokens into DT_STREAM.
#[pyfunction]
#[pyo3(signature = (base_url, api_key, messages_json, model="gpt-4o", temperature=None, max_tokens=None))]
pub fn openai_chat_completion_stream(
    py: Python<'_>,
    base_url: &str,
    api_key: &str,
    messages_json: &[u8],
    model: &str,
    temperature: Option<f64>,
    max_tokens: Option<u32>,
) -> PyResult<String> {
    let url = format!("{}/chat/completions", base_url.trim_end_matches('/'));
    let msgs = messages_json.to_vec();
    let api_key = api_key.to_string();
    let model = model.to_string();

    let result = py.detach(move || -> Result<String, String> {
        runtime().block_on(async {
            use futures_util::StreamExt;

            let messages: serde_json::Value =
                serde_json::from_slice(&msgs).map_err(|e| format!("JSON parse: {e}"))?;

            let mut body = serde_json::json!({
                "model": model,
                "messages": messages,
                "stream": true,
            });
            if let Some(t) = temperature {
                body["temperature"] = serde_json::json!(t);
            }
            if let Some(mt) = max_tokens {
                body["max_tokens"] = serde_json::json!(mt);
            }

            let resp = http_client()
                .post(&url)
                .header("Authorization", format!("Bearer {api_key}"))
                .header("Content-Type", "application/json")
                .body(body.to_string())
                .send()
                .await
                .map_err(|e| format!("HTTP: {e}"))?;

            if !resp.status().is_success() {
                let status = resp.status();
                let text = resp.text().await.unwrap_or_default();
                return Err(format!("OpenAI API {status}: {text}"));
            }

            // True streaming: parse SSE chunks as they arrive instead of
            // buffering the whole response (§ review fix #8).
            let mut collected = String::new();
            let mut pending: Vec<u8> = Vec::new();
            let mut stream = resp.bytes_stream();
            'outer: while let Some(chunk) = stream.next().await {
                let bytes = chunk.map_err(|e| format!("stream: {e}"))?;
                pending.extend_from_slice(&bytes);
                // Process complete lines; keep any trailing partial.
                while let Some(nl) = pending.iter().position(|&b| b == b'\n') {
                    let mut line_bytes: Vec<u8> = pending.drain(..=nl).collect();
                    // Drop trailing '\n' (and optional '\r') from this line.
                    if line_bytes.last() == Some(&b'\n') {
                        line_bytes.pop();
                    }
                    if line_bytes.last() == Some(&b'\r') {
                        line_bytes.pop();
                    }
                    let line = match std::str::from_utf8(&line_bytes) {
                        Ok(s) => s,
                        Err(_) => continue,
                    };
                    let data = match line.strip_prefix("data: ") {
                        Some(d) => d,
                        None => continue,
                    };
                    if data == "[DONE]" {
                        break 'outer;
                    }
                    if let Ok(chunk_json) = serde_json::from_str::<serde_json::Value>(data) {
                        if let Some(content) = chunk_json
                            .get("choices")
                            .and_then(|c| c.get(0))
                            .and_then(|c| c.get("delta"))
                            .and_then(|d| d.get("content"))
                            .and_then(|c| c.as_str())
                        {
                            collected.push_str(content);
                        }
                    }
                }
            }

            Ok(collected)
        })
    });

    result.map_err(pyo3::exceptions::PyRuntimeError::new_err)
}
