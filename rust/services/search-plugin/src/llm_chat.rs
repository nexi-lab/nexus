//! Shared HTTP client for OpenAI-compatible `/v1/chat/completions`
//! endpoints.  Both `query_expansion` (LLM-widened variants) and
//! `contextual_chunker` (chunk context prefixes) call the same
//! provider surface with the same auth + envelope shape; this
//! module owns that surface once so future LLM features do not
//! duplicate serde types + reqwest scaffolding a third time.
//!
//! # Contract
//!
//! - Sync blocking HTTP (`reqwest::blocking`) because both call
//!   sites already run inside `spawn_blocking` — an async client
//!   would either need to bridge back to the tokio runtime or
//!   force the caller onto async, both worse than blocking here.
//! - Returns only the assistant `content` string on success; the
//!   caller parses that string according to its own contract
//!   (JSON envelope for query expansion, plain text for contextual
//!   chunking).
//! - Errors are a single `LlmChatError::Http` variant carrying the
//!   provider excerpt — call sites already treat any failure as
//!   "degrade to fallback path" so a richer taxonomy adds no
//!   decision value.

use std::time::Duration;

/// One chat message (system/user/assistant + content).
#[derive(Debug, Clone, serde::Serialize)]
pub struct ChatMessage<'a> {
    pub role: &'a str,
    pub content: &'a str,
}

/// Provider-side response format hint.  OpenAI + OpenRouter honour
/// `type: "json_object"`; providers that don't just ignore the field.
#[derive(Debug, Clone, Copy, serde::Serialize)]
pub struct ResponseFormat<'a> {
    #[serde(rename = "type")]
    pub type_: &'a str,
}

/// Chat-completions request body.  Optional fields are skipped on
/// serialize so callers can send the minimal shape their model
/// tolerates.
#[derive(Debug, serde::Serialize)]
pub struct ChatRequest<'a> {
    pub model: &'a str,
    pub messages: Vec<ChatMessage<'a>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub response_format: Option<ResponseFormat<'a>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub temperature: Option<f32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub max_tokens: Option<u32>,
}

#[derive(serde::Deserialize)]
struct ChatResponse {
    choices: Vec<ChatChoice>,
}

#[derive(serde::Deserialize)]
struct ChatChoice {
    message: ChatChoiceMessage,
}

#[derive(serde::Deserialize)]
struct ChatChoiceMessage {
    content: String,
}

/// Single error variant — see the module doc.  Callers log-and-fall-
/// back so a richer taxonomy would just be noise.
#[derive(Debug, thiserror::Error)]
pub enum LlmChatError {
    #[error("chat-completions request failed: {0}")]
    Http(String),
}

/// Build a fresh blocking client with the given timeout.  A dedicated
/// client per call site is fine — `reqwest::blocking::Client` is
/// cheap; sharing one across features would just entangle their
/// timeouts.
pub fn build_client(timeout: Duration) -> Result<reqwest::blocking::Client, LlmChatError> {
    reqwest::blocking::Client::builder()
        .timeout(timeout)
        .build()
        .map_err(|e| LlmChatError::Http(format!("HTTP client build: {e}")))
}

/// POST the request body to `endpoint` with `Authorization: Bearer
/// <api_key>`; return the first choice's assistant `content` string.
///
/// - Non-2xx → `LlmChatError::Http` with a 300-char body excerpt
///   (enough to see a provider error like "quota exceeded" without
///   dumping an HTML error page into the log).
/// - Response with zero choices → `LlmChatError::Http` — a well-
///   formed shape returning no choices is a provider bug worth
///   surfacing to the caller.
/// - The returned content is NOT trimmed / parsed — that is the
///   caller's job because contextual chunking wants plain text and
///   query expansion wants JSON.
pub fn chat_completion(
    client: &reqwest::blocking::Client,
    endpoint: &str,
    api_key: &str,
    body: &ChatRequest<'_>,
) -> Result<String, LlmChatError> {
    let resp = client
        .post(endpoint)
        .bearer_auth(api_key)
        .json(body)
        .send()
        .map_err(|e| LlmChatError::Http(format!("request to {endpoint}: {e}")))?;
    let status = resp.status();
    if !status.is_success() {
        let excerpt: String = resp.text().unwrap_or_default().chars().take(300).collect();
        return Err(LlmChatError::Http(format!(
            "endpoint returned {status}: {excerpt}",
        )));
    }
    let parsed: ChatResponse = resp
        .json()
        .map_err(|e| LlmChatError::Http(format!("response parse: {e}")))?;
    parsed
        .choices
        .into_iter()
        .next()
        .map(|c| c.message.content)
        .ok_or_else(|| LlmChatError::Http("response had no choices".to_string()))
}
