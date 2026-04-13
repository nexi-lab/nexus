//! OpenAI inference pyfunctions — GIL-free HTTP calls (§10 D3).
//!
//! Standalone pyfunctions that release the GIL during HTTP I/O.
//! Used by Python LLMStreamingService to avoid holding GIL
//! during network round-trips to OpenAI-compatible APIs.

#![allow(dead_code)]

use pyo3::prelude::*;
use pyo3::types::PyBytes;

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
        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .map_err(|e| format!("tokio: {e}"))?;

        rt.block_on(async {
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

            let client = reqwest::Client::new();
            let resp = client
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
        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .map_err(|e| format!("tokio: {e}"))?;

        rt.block_on(async {
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

            let client = reqwest::Client::new();
            let resp = client
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

            let body_text = resp.text().await.map_err(|e| format!("read: {e}"))?;
            let mut collected = String::new();

            for line in body_text.lines() {
                if let Some(data) = line.strip_prefix("data: ") {
                    if data == "[DONE]" {
                        break;
                    }
                    if let Ok(chunk) = serde_json::from_str::<serde_json::Value>(data) {
                        if let Some(content) = chunk
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
