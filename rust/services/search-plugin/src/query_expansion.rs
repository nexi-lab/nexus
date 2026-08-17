//! LLM-driven query expansion — optional pre-dispatch middleware
//! that widens a single user query into N paraphrased variants
//! before the Query pipeline fans them out and fuses the union.
//!
//! # Contract
//!
//! Opt-in ONLY.  `NEXUS_SEARCH_QUERY_EXPANSION=true` gates the
//! feature; every deployment defaults OFF because LLM calls cost
//! money and add latency.  Once opted in, all four other env vars
//! (`_ENDPOINT`, `_MODEL`, `_API_KEY`, and optionally `_TIMEOUT_MS` /
//! `_MAX_VARIANTS`) must be present — partial config is a LOUD Err
//! (per the standing "fail loud on interdependent config" rule) so
//! a typo cannot silently degrade the query pipeline into a slow
//! LLM round-trip that does nothing.
//!
//! # Middleware position
//!
//! The wrapper lives in `service::SearchServiceImpl::query` and
//! runs BEFORE the cache lookup / spawn_blocking fan-out.  On the
//! success path it invokes the existing single-query pipeline N+1
//! times (original + N variants), each of which pays its own cache
//! bill, then fuses the ranked lists with `fusion::rrf_multi`.  On
//! any failure (LLM timeout / non-200 / parse error) the wrapper
//! degrades transparently to the single-query path — a broken LLM
//! endpoint must never take base search down with it.
//!
//! # Cache
//!
//! An in-process content-hash LRU on the expansion result deflates
//! repeated identical queries from an LLM call + N pipeline runs
//! down to a cheap hashmap lookup + N pipeline runs (each of which
//! then hits the existing per-zone query cache).  Bounded at
//! [`EXPANSION_CACHE_MAX`] to keep memory predictable — an operator
//! flooding a plugin with unique queries never grows the cache
//! unboundedly.

use std::collections::HashMap;
use std::collections::VecDeque;
use std::time::Duration;

use parking_lot::Mutex;

/// Kill-switch — set to `true`/`1`/`yes` to enable expansion.
/// Absent or any other value = DISABLED (the default).
pub const EXPANSION_ENABLED_ENV: &str = "NEXUS_SEARCH_QUERY_EXPANSION";
/// OpenAI-compatible chat-completions endpoint URL (e.g.
/// `https://openrouter.ai/api/v1/chat/completions` or
/// `https://api.openai.com/v1/chat/completions`).
pub const EXPANSION_ENDPOINT_ENV: &str = "NEXUS_SEARCH_QUERY_EXPANSION_ENDPOINT";
/// Chat model name sent in the request body (e.g.
/// `deepseek/deepseek-chat` or `anthropic/claude-3-haiku`).
pub const EXPANSION_MODEL_ENV: &str = "NEXUS_SEARCH_QUERY_EXPANSION_MODEL";
/// Bearer token for the endpoint.  Sent as `Authorization: Bearer <key>`.
pub const EXPANSION_API_KEY_ENV: &str = "NEXUS_SEARCH_QUERY_EXPANSION_API_KEY";
/// Per-request timeout in milliseconds (default 3000 ms — LLMs are
/// slow, but a query pipeline that stalls the whole response for
/// 30 s to widen a query is worse than no expansion).
pub const EXPANSION_TIMEOUT_MS_ENV: &str = "NEXUS_SEARCH_QUERY_EXPANSION_TIMEOUT_MS";
/// Cap on the number of variants the LLM is asked to produce
/// (default 3).  A larger N linearly multiplies pipeline cost per
/// query; ≤5 keeps the tail latency civil.
pub const EXPANSION_MAX_VARIANTS_ENV: &str = "NEXUS_SEARCH_QUERY_EXPANSION_MAX_VARIANTS";

const DEFAULT_TIMEOUT_MS: u64 = 3_000;
const DEFAULT_MAX_VARIANTS: usize = 3;
/// Bound the per-query variant count so a runaway env / LLM cannot
/// blow the fan-out.  Requests above this cap are clamped.
const HARD_MAX_VARIANTS: usize = 8;
/// Cache size — bounded FIFO on (query → variants).  A few hundred
/// entries covers the working set for a single plugin instance; the
/// aim is deflating repeated identical queries, not memoising the
/// whole corpus.
const EXPANSION_CACHE_MAX: usize = 256;

/// Errors from the expansion layer.  All variants keep base search
/// alive — the query() wrapper logs the error and falls through to
/// the single-query path, so `Misconfigured` at boot / `Http` at
/// runtime never returns an RPC-level failure to the caller.
#[derive(Debug, thiserror::Error)]
pub enum ExpansionError {
    /// Feature is enabled but env config is incomplete or malformed.
    /// Surfaced at Query-time (lazy: no boot-time check) but stable
    /// for the process lifetime — the caller can log once and cache
    /// the failure to avoid a log flood.
    #[error("query expansion misconfigured: {0}")]
    Misconfigured(String),

    /// HTTP transport / non-2xx status / body parse failure.  All of
    /// these are transient from the plugin's perspective and must
    /// not surface as a Query RPC error.
    #[error("query expansion HTTP failed: {0}")]
    Http(String),
}

/// Parsed env contract.  `from_env` returns `Ok(None)` when the
/// kill-switch is OFF and `Err(Misconfigured)` when the switch is ON
/// but one of the required companion vars is missing — per the
/// standing "fail loud on interdependent config" rule.  A silent
/// fallback would mean an operator who ticked the box gets a slow
/// query pipeline that quietly does no expansion.
#[derive(Debug, Clone)]
pub struct QueryExpansionConfig {
    pub endpoint: String,
    pub model: String,
    pub api_key: String,
    pub timeout: Duration,
    pub max_variants: usize,
}

impl QueryExpansionConfig {
    /// Read the `NEXUS_SEARCH_QUERY_EXPANSION*` contract from
    /// process env.
    pub fn from_env() -> Result<Option<Self>, ExpansionError> {
        Self::from_lookup(|k| std::env::var(k).ok())
    }

    /// Env-shape parser with an injectable lookup so unit tests
    /// never race process-global env across parallel test threads.
    pub fn from_lookup(
        get: impl Fn(&str) -> Option<String>,
    ) -> Result<Option<Self>, ExpansionError> {
        let enabled = get(EXPANSION_ENABLED_ENV)
            .map(|v| parse_bool(&v))
            .unwrap_or(false);
        if !enabled {
            return Ok(None);
        }
        let endpoint = get(EXPANSION_ENDPOINT_ENV)
            .filter(|v| !v.trim().is_empty())
            .map(|v| v.trim().to_string())
            .ok_or_else(|| {
                ExpansionError::Misconfigured(format!(
                    "{EXPANSION_ENABLED_ENV}=true but {EXPANSION_ENDPOINT_ENV} is unset — \
                     the expander needs an OpenAI-compatible chat-completions URL",
                ))
            })?;
        let model = get(EXPANSION_MODEL_ENV)
            .filter(|v| !v.trim().is_empty())
            .map(|v| v.trim().to_string())
            .ok_or_else(|| {
                ExpansionError::Misconfigured(format!(
                    "{EXPANSION_ENABLED_ENV}=true but {EXPANSION_MODEL_ENV} is unset — \
                     the expander needs an explicit chat model name",
                ))
            })?;
        let api_key = get(EXPANSION_API_KEY_ENV)
            .filter(|v| !v.trim().is_empty())
            .map(|v| v.trim().to_string())
            .ok_or_else(|| {
                ExpansionError::Misconfigured(format!(
                    "{EXPANSION_ENABLED_ENV}=true but {EXPANSION_API_KEY_ENV} is unset — \
                     the expander needs a bearer token for the endpoint",
                ))
            })?;
        let timeout_ms = match get(EXPANSION_TIMEOUT_MS_ENV).filter(|v| !v.trim().is_empty()) {
            Some(t) => t
                .trim()
                .parse::<u64>()
                .ok()
                .filter(|s| *s > 0)
                .ok_or_else(|| {
                    ExpansionError::Misconfigured(format!(
                        "{EXPANSION_TIMEOUT_MS_ENV}={t:?} is not a positive integer",
                    ))
                })?,
            None => DEFAULT_TIMEOUT_MS,
        };
        let max_variants = match get(EXPANSION_MAX_VARIANTS_ENV).filter(|v| !v.trim().is_empty()) {
            Some(v) => v
                .trim()
                .parse::<usize>()
                .ok()
                .filter(|n| *n > 0)
                .ok_or_else(|| {
                    ExpansionError::Misconfigured(format!(
                        "{EXPANSION_MAX_VARIANTS_ENV}={v:?} is not a positive integer",
                    ))
                })?,
            None => DEFAULT_MAX_VARIANTS,
        }
        .min(HARD_MAX_VARIANTS);
        Ok(Some(Self {
            endpoint,
            model,
            api_key,
            timeout: Duration::from_millis(timeout_ms),
            max_variants,
        }))
    }
}

fn parse_bool(raw: &str) -> bool {
    matches!(
        raw.trim().to_ascii_lowercase().as_str(),
        "1" | "true" | "yes" | "on"
    )
}

/// Batch query-widening contract.  One trait so tests can swap the
/// live HTTP expander for a canned mock without stubbing reqwest.
pub trait QueryExpander: Send + Sync {
    /// Return up to `max_variants` paraphrased alternatives of
    /// `query`.  Duplicates of the original are the caller's
    /// responsibility to strip — the trait is intentionally
    /// permissive so a well-behaved LLM's occasional near-echo does
    /// not surface as an error.
    fn expand(&self, query: &str, max_variants: usize) -> Result<Vec<String>, ExpansionError>;
}

// ── HttpQueryExpander ─────────────────────────────────────────────

use crate::llm_chat::{self, ChatMessage, ChatRequest, ResponseFormat};

/// Live HTTP query expander.  Sends a single chat-completion request
/// asking for JSON `{"variants": [...]}` and parses the assistant's
/// content out of the response envelope.  Fails LOUD on any HTTP or
/// parse error — the wrapper in service.rs converts those to a
/// tracing warning and falls through to the single-query path.
pub struct HttpQueryExpander {
    client: reqwest::blocking::Client,
    config: QueryExpansionConfig,
}

impl HttpQueryExpander {
    /// Build the HTTP client for `config`.  No network I/O happens
    /// here.
    pub fn new(config: QueryExpansionConfig) -> Result<Self, ExpansionError> {
        let client = llm_chat::build_client(config.timeout)
            .map_err(|e| ExpansionError::Http(e.to_string()))?;
        Ok(Self { client, config })
    }
}

/// Prompt sent as the SYSTEM message.  Kept short and provider-
/// agnostic — OpenRouter / OpenAI / any downstream OpenAI-compatible
/// gateway will happily forward it.  JSON response requested via
/// both the system prompt AND the `response_format: json_object`
/// hint; providers that ignore the hint still parse fine because
/// the prompt itself instructs strict JSON output.
const SYSTEM_PROMPT: &str = "\
You rewrite a search query into short paraphrased variants that a \
BM25 keyword index and a dense vector index will both retrieve well. \
Preserve intent; vary wording, synonyms, and named-entity forms. \
Do not add commentary. Reply ONLY with a strict JSON object of the \
shape {\"variants\": [\"variant one\", \"variant two\", ...]}, no \
Markdown, no code fences.";

impl QueryExpander for HttpQueryExpander {
    fn expand(&self, query: &str, max_variants: usize) -> Result<Vec<String>, ExpansionError> {
        if query.trim().is_empty() || max_variants == 0 {
            return Ok(Vec::new());
        }
        let capped = max_variants.min(HARD_MAX_VARIANTS);
        let user = format!("Original query: {query}\nProduce up to {capped} paraphrased variants.");
        let body = ChatRequest {
            model: &self.config.model,
            messages: vec![
                ChatMessage {
                    role: "system",
                    content: SYSTEM_PROMPT,
                },
                ChatMessage {
                    role: "user",
                    content: &user,
                },
            ],
            response_format: Some(ResponseFormat {
                type_: "json_object",
            }),
            temperature: Some(0.3),
            max_tokens: None,
        };
        let content =
            llm_chat::chat_completion(&self.client, &self.config.endpoint, &self.config.api_key, &body)
                .map_err(|e| ExpansionError::Http(e.to_string()))?;
        parse_variants(&content, capped)
            .map_err(|e| ExpansionError::Http(format!("expand content parse: {e}")))
    }
}

/// Extract the variant list from an LLM's content string.  Accepts
/// strict `{"variants":[...]}`, tolerates surrounding whitespace or
/// stray Markdown fences that a model may emit despite the system
/// prompt, and hard-caps the returned Vec to `max`.
fn parse_variants(raw: &str, max: usize) -> Result<Vec<String>, String> {
    #[derive(serde::Deserialize)]
    struct Envelope {
        variants: Vec<String>,
    }
    let cleaned = strip_code_fences(raw.trim());
    let env: Envelope = serde_json::from_str(cleaned)
        .map_err(|e| format!("expected JSON with `variants` array: {e} — got: {cleaned}"))?;
    let out: Vec<String> = env
        .variants
        .into_iter()
        .filter_map(|v| {
            let t = v.trim();
            if t.is_empty() {
                None
            } else {
                Some(t.to_string())
            }
        })
        .take(max)
        .collect();
    Ok(out)
}

/// Trim ```json … ``` fences a chatty model may still emit.  Also
/// handles a bare ``` opening.  Returns a slice into the original.
fn strip_code_fences(s: &str) -> &str {
    let s = s.trim();
    // Common shapes: ```json\n...\n```   or   ```\n...\n```
    if let Some(rest) = s.strip_prefix("```json") {
        let rest = rest.trim_start_matches('\n').trim_start();
        if let Some(inner) = rest.strip_suffix("```") {
            return inner.trim();
        }
    }
    if let Some(rest) = s.strip_prefix("```") {
        let rest = rest.trim_start_matches('\n').trim_start();
        if let Some(inner) = rest.strip_suffix("```") {
            return inner.trim();
        }
    }
    s
}

// ── Cache ─────────────────────────────────────────────────────────
//
// Bounded FIFO on `query → Vec<String>`.  Not an LRU — LRU accounting
// under a Mutex is a non-trivial win at this bound (≤ 256 entries),
// and a FIFO gives predictable eviction: the oldest entry drops when
// we cross the ceiling.  parking_lot::Mutex avoids poisoning; the
// critical section is a hashmap lookup + optional insert, so a
// single mutex is fine even under a moderate query burst.

/// Thread-safe expansion cache — see the module comment for why FIFO
/// is chosen over LRU.
pub struct ExpansionCache {
    inner: Mutex<CacheInner>,
    max: usize,
}

struct CacheInner {
    map: HashMap<String, Vec<String>>,
    order: VecDeque<String>,
}

impl ExpansionCache {
    /// Fresh cache bounded at [`EXPANSION_CACHE_MAX`].
    pub fn new() -> Self {
        Self::with_capacity(EXPANSION_CACHE_MAX)
    }

    /// Test hook — tighter bound for eviction-behavior assertions.
    pub fn with_capacity(max: usize) -> Self {
        assert!(max > 0, "ExpansionCache capacity must be > 0");
        Self {
            inner: Mutex::new(CacheInner {
                map: HashMap::with_capacity(max),
                order: VecDeque::with_capacity(max),
            }),
            max,
        }
    }

    /// Lookup — clones the value on hit so callers do not hold the
    /// mutex across the (long-running) pipeline fan-out.
    pub fn get(&self, query: &str) -> Option<Vec<String>> {
        let g = self.inner.lock();
        g.map.get(query).cloned()
    }

    /// Insert — evicts the oldest entry when the cache would exceed
    /// its capacity.  Re-inserting an existing key OVERWRITES the
    /// value in place: the previous entry keeps its position in the
    /// FIFO order (no O(n) order-vec scan on every touch) and the
    /// FIFO length is unchanged, so it does NOT force an eviction.
    pub fn insert(&self, query: String, variants: Vec<String>) {
        let mut g = self.inner.lock();
        // Re-insert of an existing key: overwrite in place, keep FIFO
        // position, no eviction.  The map's `insert` doubles as
        // "update if present" so a single call covers both — the
        // hit-rate case for the cache never touches `order`.
        if let std::collections::hash_map::Entry::Occupied(mut e) = g.map.entry(query.clone()) {
            e.insert(variants);
            return;
        }
        // Vacant path: evict the oldest before inserting so the
        // FIFO stays at ≤ `max`.
        if g.order.len() >= self.max {
            if let Some(evicted) = g.order.pop_front() {
                g.map.remove(&evicted);
            }
        }
        g.order.push_back(query.clone());
        g.map.insert(query, variants);
    }

    /// Live entry count — for tests only.
    #[cfg(test)]
    fn len(&self) -> usize {
        self.inner.lock().map.len()
    }
}

impl Default for ExpansionCache {
    fn default() -> Self {
        Self::new()
    }
}

// ── Best-effort builder ───────────────────────────────────────────

/// Live expander + the config it was built from.  Returned by
/// [`build_default_expander`] so the caller has both the concrete
/// expander to wire into the service and the config fields (e.g.
/// `max_variants`) that the wrapper needs at query time.
pub type BuiltExpander = (Box<dyn QueryExpander>, QueryExpansionConfig);

/// Build the configured expander from env, if any.  Returns
/// `Ok(None)` when the kill-switch is off (the common case) and
/// `Err` when the switch is on but the config is malformed — the
/// caller's job is to log the error and disable expansion for this
/// process to avoid a log flood.
pub fn build_default_expander() -> Result<Option<BuiltExpander>, ExpansionError> {
    let Some(cfg) = QueryExpansionConfig::from_env()? else {
        return Ok(None);
    };
    let expander = HttpQueryExpander::new(cfg.clone())?;
    Ok(Some((Box::new(expander), cfg)))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn lookup(pairs: &[(&str, &str)]) -> impl Fn(&str) -> Option<String> {
        let owned: Vec<(String, String)> = pairs
            .iter()
            .map(|(k, v)| ((*k).to_string(), (*v).to_string()))
            .collect();
        move |k| {
            owned
                .iter()
                .find(|(key, _)| key == k)
                .map(|(_, v)| v.clone())
        }
    }

    #[test]
    fn config_disabled_by_default() {
        let cfg = QueryExpansionConfig::from_lookup(lookup(&[])).unwrap();
        assert!(cfg.is_none());
        let cfg =
            QueryExpansionConfig::from_lookup(lookup(&[(EXPANSION_ENABLED_ENV, "false")])).unwrap();
        assert!(cfg.is_none());
    }

    #[test]
    fn config_enabled_but_missing_endpoint_errors() {
        let err = QueryExpansionConfig::from_lookup(lookup(&[
            (EXPANSION_ENABLED_ENV, "true"),
            (EXPANSION_MODEL_ENV, "m"),
            (EXPANSION_API_KEY_ENV, "sk-x"),
        ]))
        .unwrap_err();
        assert!(
            matches!(err, ExpansionError::Misconfigured(m) if m.contains(EXPANSION_ENDPOINT_ENV))
        );
    }

    #[test]
    fn config_enabled_but_missing_model_errors() {
        let err = QueryExpansionConfig::from_lookup(lookup(&[
            (EXPANSION_ENABLED_ENV, "1"),
            (EXPANSION_ENDPOINT_ENV, "http://x/v1/chat/completions"),
            (EXPANSION_API_KEY_ENV, "sk-x"),
        ]))
        .unwrap_err();
        assert!(matches!(err, ExpansionError::Misconfigured(m) if m.contains(EXPANSION_MODEL_ENV)));
    }

    #[test]
    fn config_enabled_but_missing_key_errors() {
        let err = QueryExpansionConfig::from_lookup(lookup(&[
            (EXPANSION_ENABLED_ENV, "yes"),
            (EXPANSION_ENDPOINT_ENV, "http://x/v1/chat/completions"),
            (EXPANSION_MODEL_ENV, "m"),
        ]))
        .unwrap_err();
        assert!(
            matches!(err, ExpansionError::Misconfigured(m) if m.contains(EXPANSION_API_KEY_ENV))
        );
    }

    #[test]
    fn config_full_set_parses_and_applies_defaults() {
        let cfg = QueryExpansionConfig::from_lookup(lookup(&[
            (EXPANSION_ENABLED_ENV, "true"),
            (
                EXPANSION_ENDPOINT_ENV,
                " http://localhost:9/v1/chat/completions ",
            ),
            (EXPANSION_MODEL_ENV, " deepseek-chat "),
            (EXPANSION_API_KEY_ENV, "sk-test"),
        ]))
        .unwrap()
        .expect("config present");
        assert_eq!(cfg.endpoint, "http://localhost:9/v1/chat/completions");
        assert_eq!(cfg.model, "deepseek-chat");
        assert_eq!(cfg.api_key, "sk-test");
        assert_eq!(cfg.timeout, Duration::from_millis(DEFAULT_TIMEOUT_MS));
        assert_eq!(cfg.max_variants, DEFAULT_MAX_VARIANTS);
    }

    #[test]
    fn config_rejects_bad_numeric_env() {
        let err = QueryExpansionConfig::from_lookup(lookup(&[
            (EXPANSION_ENABLED_ENV, "true"),
            (EXPANSION_ENDPOINT_ENV, "http://x/v1/chat/completions"),
            (EXPANSION_MODEL_ENV, "m"),
            (EXPANSION_API_KEY_ENV, "sk-x"),
            (EXPANSION_TIMEOUT_MS_ENV, "soon"),
        ]))
        .unwrap_err();
        assert!(
            matches!(err, ExpansionError::Misconfigured(m) if m.contains(EXPANSION_TIMEOUT_MS_ENV))
        );

        let err = QueryExpansionConfig::from_lookup(lookup(&[
            (EXPANSION_ENABLED_ENV, "true"),
            (EXPANSION_ENDPOINT_ENV, "http://x/v1/chat/completions"),
            (EXPANSION_MODEL_ENV, "m"),
            (EXPANSION_API_KEY_ENV, "sk-x"),
            (EXPANSION_MAX_VARIANTS_ENV, "0"),
        ]))
        .unwrap_err();
        assert!(
            matches!(err, ExpansionError::Misconfigured(m) if m.contains(EXPANSION_MAX_VARIANTS_ENV))
        );
    }

    #[test]
    fn config_clamps_max_variants_to_hard_ceiling() {
        let cfg = QueryExpansionConfig::from_lookup(lookup(&[
            (EXPANSION_ENABLED_ENV, "true"),
            (EXPANSION_ENDPOINT_ENV, "http://x/v1/chat/completions"),
            (EXPANSION_MODEL_ENV, "m"),
            (EXPANSION_API_KEY_ENV, "sk-x"),
            (EXPANSION_MAX_VARIANTS_ENV, "999"),
        ]))
        .unwrap()
        .expect("config present");
        assert_eq!(cfg.max_variants, HARD_MAX_VARIANTS);
    }

    #[test]
    fn parse_variants_accepts_strict_json() {
        let out = parse_variants(r#"{"variants":["a","b","c"]}"#, 5).unwrap();
        assert_eq!(out, vec!["a", "b", "c"]);
    }

    #[test]
    fn parse_variants_strips_code_fences() {
        let out = parse_variants("```json\n{\"variants\":[\"alpha\",\"beta\"]}\n```", 5).unwrap();
        assert_eq!(out, vec!["alpha", "beta"]);

        let out = parse_variants("```\n{\"variants\":[\"x\"]}\n```", 5).unwrap();
        assert_eq!(out, vec!["x"]);
    }

    #[test]
    fn parse_variants_caps_and_trims_empty() {
        let out =
            parse_variants(r#"{"variants":["one","", "  ","two","three","four"]}"#, 2).unwrap();
        assert_eq!(out, vec!["one", "two"]);
    }

    #[test]
    fn parse_variants_rejects_non_json() {
        let err = parse_variants("not json at all", 5).unwrap_err();
        assert!(err.contains("expected JSON"));
    }

    #[test]
    fn cache_hit_returns_clone() {
        let c = ExpansionCache::with_capacity(4);
        c.insert("q".into(), vec!["a".into(), "b".into()]);
        assert_eq!(c.get("q"), Some(vec!["a".into(), "b".into()]));
        assert_eq!(c.get("missing"), None);
    }

    #[test]
    fn cache_evicts_oldest_at_capacity() {
        let c = ExpansionCache::with_capacity(2);
        c.insert("a".into(), vec!["1".into()]);
        c.insert("b".into(), vec!["2".into()]);
        c.insert("c".into(), vec!["3".into()]);
        assert_eq!(c.len(), 2, "cache must stay at ceiling");
        assert_eq!(c.get("a"), None, "oldest entry must evict");
        assert_eq!(c.get("b"), Some(vec!["2".into()]));
        assert_eq!(c.get("c"), Some(vec!["3".into()]));
    }

    #[test]
    fn cache_reinsert_updates_value_without_growth() {
        let c = ExpansionCache::with_capacity(2);
        c.insert("q".into(), vec!["v1".into()]);
        c.insert("q".into(), vec!["v2".into()]);
        assert_eq!(c.len(), 1);
        assert_eq!(c.get("q"), Some(vec!["v2".into()]));
    }

    // ── HttpQueryExpander end-to-end via one-shot HTTP server ─────

    /// One-request localhost HTTP server — mirrors the same helper
    /// in embedder.rs tests.  Accepts a single connection, reads the
    /// full request (headers + Content-Length body), writes the
    /// canned response, returns the raw request text for assertions.
    fn spawn_one_shot_http(
        status_line: &'static str,
        body: String,
    ) -> (std::net::SocketAddr, std::thread::JoinHandle<String>) {
        use std::io::{Read, Write};

        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        let handle = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut buf = Vec::new();
            let mut tmp = [0u8; 4096];
            let header_end = loop {
                let n = stream.read(&mut tmp).unwrap();
                assert!(n > 0, "client closed before end of headers");
                buf.extend_from_slice(&tmp[..n]);
                if let Some(pos) = buf.windows(4).position(|w| w == b"\r\n\r\n") {
                    break pos + 4;
                }
            };
            let headers = String::from_utf8_lossy(&buf[..header_end]).to_string();
            let content_length = headers
                .lines()
                .find_map(|l| {
                    let (k, v) = l.split_once(':')?;
                    k.eq_ignore_ascii_case("content-length")
                        .then(|| v.trim().parse::<usize>().ok())
                        .flatten()
                })
                .unwrap_or(0);
            while buf.len() < header_end + content_length {
                let n = stream.read(&mut tmp).unwrap();
                assert!(n > 0, "client closed mid-body");
                buf.extend_from_slice(&tmp[..n]);
            }
            let request = String::from_utf8_lossy(&buf).to_string();
            let resp = format!(
                "HTTP/1.1 {status_line}\r\nContent-Type: application/json\r\n\
                 Content-Length: {}\r\nConnection: close\r\n\r\n{body}",
                body.len(),
            );
            stream.write_all(resp.as_bytes()).unwrap();
            request
        });
        (addr, handle)
    }

    fn test_config(endpoint: String) -> QueryExpansionConfig {
        QueryExpansionConfig {
            endpoint,
            model: "test-chat".to_string(),
            api_key: "sk-test".to_string(),
            timeout: Duration::from_secs(5),
            max_variants: 3,
        }
    }

    #[test]
    fn http_expander_sends_auth_and_parses_variants() {
        let body = serde_json::json!({
            "choices": [{
                "message": {
                    "content": "{\"variants\": [\"first alt\", \"second alt\", \"third alt\"]}"
                }
            }]
        })
        .to_string();
        let (addr, handle) = spawn_one_shot_http("200 OK", body);
        let expander =
            HttpQueryExpander::new(test_config(format!("http://{addr}/v1/chat/completions")))
                .unwrap();

        let out = expander.expand("how does auth work", 3).unwrap();
        assert_eq!(out, vec!["first alt", "second alt", "third alt"]);

        let request = handle.join().unwrap().to_lowercase();
        assert!(
            request.contains("authorization: bearer sk-test"),
            "missing bearer auth in {request}",
        );
        assert!(
            request.contains("\"model\":\"test-chat\""),
            "missing model in body: {request}",
        );
        assert!(
            request.contains("how does auth work"),
            "missing original query in body: {request}",
        );
    }

    #[test]
    fn http_expander_surfaces_non_2xx_as_http_error() {
        let (addr, handle) = spawn_one_shot_http(
            "429 Too Many Requests",
            r#"{"error":{"message":"quota exceeded"}}"#.to_string(),
        );
        let expander =
            HttpQueryExpander::new(test_config(format!("http://{addr}/v1/chat/completions")))
                .unwrap();
        let err = expander.expand("q", 3).unwrap_err();
        match err {
            ExpansionError::Http(m) => {
                assert!(m.contains("429"), "status missing: {m}");
                assert!(m.contains("quota exceeded"), "body excerpt missing: {m}");
            }
            other => panic!("expected Http, got {other:?}"),
        }
        let _ = handle.join().unwrap();
    }

    #[test]
    fn http_expander_surfaces_bad_content_as_http_error() {
        // 200 OK but the assistant returned garbage — must surface
        // as ExpansionError::Http so the wrapper degrades gracefully.
        let body = serde_json::json!({
            "choices": [{"message": {"content": "sorry, I can't do that"}}]
        })
        .to_string();
        let (addr, handle) = spawn_one_shot_http("200 OK", body);
        let expander =
            HttpQueryExpander::new(test_config(format!("http://{addr}/v1/chat/completions")))
                .unwrap();
        let err = expander.expand("q", 3).unwrap_err();
        assert!(matches!(err, ExpansionError::Http(_)));
        let _ = handle.join().unwrap();
    }

    #[test]
    fn http_expander_empty_query_returns_empty_without_dialing() {
        // Unroutable URL — an empty query must not dial the network.
        let expander =
            HttpQueryExpander::new(test_config("http://127.0.0.1:9/v1/chat/completions".into()))
                .unwrap();
        assert!(expander.expand("   ", 3).unwrap().is_empty());
        assert!(expander.expand("q", 0).unwrap().is_empty());
    }
}
