//! OpenAI-compatible connector — pure Rust HTTP client (§10 D3).
//!
//! Implements ObjectStore trait for in-memory blob storage (CAS).
//! Provides chat_completion() for synchronous inference and
//! chat_completion_stream() for SSE streaming — both GIL-free.
//!
//! Compatible with: OpenAI, SudoRouter, OpenRouter, Ollama, vLLM.
//!
//! Architecture:
//!   - ObjectStore methods: in-memory DashMap (blob storage for sessions)
//!   - chat_completion: reqwest blocking POST → JSON response
//!   - chat_completion_stream: reqwest streaming POST → SSE token iterator

#![allow(dead_code)]

use crate::backend::{ObjectStore, StorageError, WriteResult};
use crate::kernel::OperationContext;
use dashmap::DashMap;
use std::io;
use std::sync::atomic::{AtomicU64, Ordering};

/// OpenAI-compatible backend — in-memory CAS + HTTP inference.
pub(crate) struct OpenAIBackend {
    /// Backend name for routing.
    backend_name: String,
    /// Base URL for API calls (e.g. "https://api.openai.com/v1").
    base_url: String,
    /// API key for authentication.
    api_key: String,
    /// Default model (e.g. "gpt-4o").
    default_model: String,
    /// In-memory blob storage (content_id → bytes).
    blobs: DashMap<String, Vec<u8>>,
    /// Write counter for content_id generation.
    write_counter: AtomicU64,
    /// Tokio runtime for async HTTP calls.
    runtime: tokio::runtime::Runtime,
}

impl OpenAIBackend {
    pub(crate) fn new(
        name: &str,
        base_url: &str,
        api_key: &str,
        default_model: &str,
    ) -> Result<Self, io::Error> {
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()?;
        Ok(Self {
            backend_name: name.to_string(),
            base_url: base_url.trim_end_matches('/').to_string(),
            api_key: api_key.to_string(),
            default_model: default_model.to_string(),
            blobs: DashMap::new(),
            write_counter: AtomicU64::new(0),
            runtime,
        })
    }

    /// Synchronous chat completion — blocking HTTP POST, returns full response JSON.
    ///
    /// GIL-free: caller releases GIL before calling this.
    /// Returns response body as bytes (JSON).
    #[cfg(feature = "connectors")]
    pub(crate) fn chat_completion(
        &self,
        messages_json: &[u8],
        model: Option<&str>,
        temperature: Option<f64>,
        max_tokens: Option<u32>,
    ) -> Result<Vec<u8>, StorageError> {
        let model = model.unwrap_or(&self.default_model);
        let url = format!("{}/chat/completions", self.base_url);

        // Parse messages from JSON bytes
        let messages: serde_json::Value = serde_json::from_slice(messages_json)
            .map_err(|e| StorageError::IOError(io::Error::other(format!("JSON parse: {e}"))))?;

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

        self.runtime.block_on(async {
            let client = reqwest::Client::new();
            let resp = client
                .post(&url)
                .header("Authorization", format!("Bearer {}", self.api_key))
                .header("Content-Type", "application/json")
                .body(body.to_string())
                .send()
                .await
                .map_err(|e| StorageError::IOError(io::Error::other(format!("HTTP: {e}"))))?;

            if !resp.status().is_success() {
                let status = resp.status();
                let body = resp
                    .text()
                    .await
                    .unwrap_or_else(|_| "<no body>".to_string());
                return Err(StorageError::IOError(io::Error::other(format!(
                    "OpenAI API {status}: {body}"
                ))));
            }

            resp.bytes()
                .await
                .map(|b| b.to_vec())
                .map_err(|e| StorageError::IOError(io::Error::other(format!("read body: {e}"))))
        })
    }

    /// Streaming chat completion — SSE, returns collected tokens as string.
    ///
    /// For full streaming (token-by-token), use the Python LLMStreamingService
    /// which calls this backend's chat_completion_stream_raw() and pumps tokens
    /// into a DT_STREAM.
    ///
    /// This convenience method collects all tokens and returns the full response.
    #[cfg(feature = "connectors")]
    pub(crate) fn chat_completion_collect(
        &self,
        messages_json: &[u8],
        model: Option<&str>,
        temperature: Option<f64>,
        max_tokens: Option<u32>,
    ) -> Result<String, StorageError> {
        let model = model.unwrap_or(&self.default_model);
        let url = format!("{}/chat/completions", self.base_url);

        let messages: serde_json::Value = serde_json::from_slice(messages_json)
            .map_err(|e| StorageError::IOError(io::Error::other(format!("JSON parse: {e}"))))?;

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

        self.runtime.block_on(async {
            let client = reqwest::Client::new();
            let resp = client
                .post(&url)
                .header("Authorization", format!("Bearer {}", self.api_key))
                .header("Content-Type", "application/json")
                .body(body.to_string())
                .send()
                .await
                .map_err(|e| StorageError::IOError(io::Error::other(format!("HTTP: {e}"))))?;

            if !resp.status().is_success() {
                let status = resp.status();
                let text = resp
                    .text()
                    .await
                    .unwrap_or_else(|_| "<no body>".to_string());
                return Err(StorageError::IOError(io::Error::other(format!(
                    "OpenAI API {status}: {text}"
                ))));
            }

            // Parse SSE stream and collect content tokens
            let mut collected = String::new();
            let body_text = resp
                .text()
                .await
                .map_err(|e| StorageError::IOError(io::Error::other(format!("read: {e}"))))?;

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
    }
}

// ── ObjectStore impl (in-memory CAS blob storage) ──────────────────────

impl ObjectStore for OpenAIBackend {
    fn name(&self) -> &str {
        &self.backend_name
    }

    fn write_content(
        &self,
        content: &[u8],
        content_id: &str,
        _ctx: &OperationContext,
    ) -> Result<WriteResult, StorageError> {
        // Generate content_id via BLAKE3 hash (CAS addressing)
        let hash = blake3::hash(content).to_hex().to_string();
        let cid = if content_id.is_empty() {
            hash.clone()
        } else {
            content_id.to_string()
        };
        let size = content.len() as u64;
        self.blobs.insert(cid.clone(), content.to_vec());
        self.write_counter.fetch_add(1, Ordering::Relaxed);
        Ok(WriteResult {
            content_id: cid.clone(),
            version: hash,
            size,
        })
    }

    fn read_content(
        &self,
        content_id: &str,
        _backend_path: &str,
        _ctx: &OperationContext,
    ) -> Result<Vec<u8>, StorageError> {
        self.blobs
            .get(content_id)
            .map(|r| r.value().clone())
            .ok_or_else(|| StorageError::NotFound(content_id.to_string()))
    }

    fn delete_content(&self, content_id: &str) -> Result<(), StorageError> {
        self.blobs.remove(content_id);
        Ok(())
    }

    fn get_content_size(&self, content_id: &str) -> Result<u64, StorageError> {
        self.blobs
            .get(content_id)
            .map(|r| r.value().len() as u64)
            .ok_or_else(|| StorageError::NotFound(content_id.to_string()))
    }
}
