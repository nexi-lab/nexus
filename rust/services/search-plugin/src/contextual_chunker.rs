//! Anthropic-style contextual chunking — at index time, ask an LLM
//! to produce a short "context prefix" for each chunk that resolves
//! pronouns and adds surrounding section context, then prepend the
//! prefix to `Chunk::embed_input` (NOT `Chunk::text`).  Semantic
//! recall lifts on ambiguous queries; BM25 stays clean because the
//! context never enters `chunk_text`.
//!
//! # Contract
//!
//! Opt-in ONLY.  `NEXUS_SEARCH_CONTEXTUAL_CHUNKING=true` gates the
//! feature; every deployment defaults OFF because contextual
//! chunking is the most expensive of the three LLM knobs (one LLM
//! call per chunk × N chunks per doc × M docs indexed).  Once opted
//! in, all four other env vars (`_ENDPOINT`, `_MODEL`, `_API_KEY`,
//! and optionally `_CONCURRENCY`) must be present — partial config
//! is a LOUD Err (per the standing "fail loud on interdependent
//! config" rule).
//!
//! # Env config is SEPARATE from Feature 1 (query expansion)
//!
//! Query expansion benefits from strong reasoning (DeepSeek, GPT-4o,
//! Claude 3.5); contextual chunking benefits from cheap fast models
//! (Haiku, GPT-4o-mini).  Two distinct env prefixes so operators can
//! point each at the right cost/latency point.
//!
//! # Failure posture
//!
//! Per-chunk generation failure returns `None` for that slot; the
//! chunk stays unchanged (its `embed_input` = plain heading-prefixed
//! text as `chunker::chunk_document` produced it).  A broken LLM
//! endpoint MUST NOT stall indexing or drop chunks — the FTS side
//! is authoritative for keyword search, and semantic recall stays
//! functional without contextual prefixes.
//!
//! # Concurrency
//!
//! Sync API driven by `reqwest::blocking` — every call site in the
//! indexing pipeline already runs inside `spawn_blocking`, so blocking
//! HTTP keeps the plugin's tokio pool free.  `generate_batch`
//! internally fans chunks out via `std::thread::scope` bounded by
//! [`ContextualChunkingConfig::concurrency`] (default 4) so a doc
//! with 50 chunks doesn't serialise 50 × 3 s HTTP round-trips.

use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::OnceLock;
use std::time::Duration;

use parking_lot::{Condvar, Mutex};

/// Kill-switch — set to `true`/`1`/`yes` to enable contextual
/// chunking.  Absent or any other value = DISABLED (the default).
pub const CONTEXTUAL_ENABLED_ENV: &str = "NEXUS_SEARCH_CONTEXTUAL_CHUNKING";
/// OpenAI-compatible chat-completions endpoint URL.  Separate from
/// query expansion's endpoint so operators can point contextual
/// chunking at a cheaper/faster model.
pub const CONTEXTUAL_ENDPOINT_ENV: &str = "NEXUS_SEARCH_CONTEXTUAL_CHUNKING_ENDPOINT";
/// Chat model name (e.g. `anthropic/claude-3-haiku`,
/// `openai/gpt-4o-mini`).
pub const CONTEXTUAL_MODEL_ENV: &str = "NEXUS_SEARCH_CONTEXTUAL_CHUNKING_MODEL";
/// Bearer token for the endpoint.
pub const CONTEXTUAL_API_KEY_ENV: &str = "NEXUS_SEARCH_CONTEXTUAL_CHUNKING_API_KEY";
/// Per-request timeout in milliseconds (default 5000 ms).  Larger
/// than query expansion's 3 s — contextual chunking runs at
/// indexing time (background), not on the caller-visible query path.
pub const CONTEXTUAL_TIMEOUT_MS_ENV: &str = "NEXUS_SEARCH_CONTEXTUAL_CHUNKING_TIMEOUT_MS";
/// Bounded intra-doc parallelism — default 4.  A doc with N chunks
/// runs `min(N, concurrency)` HTTP round-trips at a time via a
/// `std::thread::scope` fan-out.
pub const CONTEXTUAL_CONCURRENCY_ENV: &str = "NEXUS_SEARCH_CONTEXTUAL_CHUNKING_CONCURRENCY";
/// Cap on how much of the surrounding document is sent to the LLM
/// alongside each chunk (default 8 KB).  Truncation keeps the LLM
/// bill bounded — a monster README shouldn't cost 100 KB of context
/// per chunk.
pub const CONTEXTUAL_DOC_CAP_ENV: &str = "NEXUS_SEARCH_CONTEXTUAL_CHUNKING_DOC_CAP_BYTES";
/// Cap on how many chunks per document get LLM-generated context
/// (default 100).  A 5 MB document that chunks into 500 pieces
/// would otherwise trigger 500 LLM calls — the audit's cost-warning
/// language was loud but the code had no ceiling.  Chunks past the
/// cap keep their plain `embed_input` (identical semantics to a
/// generator that returned None for them).
pub const CONTEXTUAL_MAX_CHUNKS_PER_DOC_ENV: &str =
    "NEXUS_SEARCH_CONTEXTUAL_CHUNKING_MAX_CHUNKS_PER_DOC";
/// Cap on total in-flight LLM calls across ALL concurrently-indexing
/// documents.  Per-doc concurrency × M docs indexed in parallel by
/// the outer spawn_blocking pool would otherwise scale unbounded
/// (default tokio blocking pool = 512; × per-doc 4 = 2048 concurrent
/// LLM calls in the worst case).  Default 16 — small enough to keep
/// provider rate limits happy, large enough to keep indexing
/// throughput reasonable.
pub const CONTEXTUAL_GLOBAL_CONCURRENCY_ENV: &str =
    "NEXUS_SEARCH_CONTEXTUAL_CHUNKING_GLOBAL_CONCURRENCY";

const DEFAULT_TIMEOUT_MS: u64 = 5_000;
const DEFAULT_CONCURRENCY: usize = 4;
/// Ceiling on concurrency so a runaway env value can't blow the
/// thread count on a small-corpus host.
const HARD_MAX_CONCURRENCY: usize = 32;
const DEFAULT_DOC_CAP_BYTES: usize = 8 * 1024;
const DEFAULT_MAX_CHUNKS_PER_DOC: usize = 100;
const DEFAULT_GLOBAL_CONCURRENCY: usize = 16;
/// Ceiling on the global-concurrency env value.  A runaway 10 000
/// here would still be gated by the provider's per-key rate limit,
/// but the plugin's own memory + thread footprint has an interest
/// in a hard cap.
const HARD_MAX_GLOBAL_CONCURRENCY: usize = 256;

/// Errors from the contextual-chunking layer.  All variants keep
/// indexing alive — the caller (`index_one` / `do_index_documents`)
/// logs the error and treats the chunk as un-contexted (chunk still
/// indexed with plain `embed_input`).
#[derive(Debug, thiserror::Error)]
pub enum ContextualError {
    /// Feature is enabled but env config is incomplete or malformed.
    #[error("contextual chunking misconfigured: {0}")]
    Misconfigured(String),

    /// HTTP transport / non-2xx status / body parse failure.
    #[error("contextual chunking HTTP failed: {0}")]
    Http(String),
}

/// Parsed env contract.  Same fail-loud semantics as
/// [`crate::query_expansion::QueryExpansionConfig`].
#[derive(Debug, Clone)]
pub struct ContextualChunkingConfig {
    pub endpoint: String,
    pub model: String,
    pub api_key: String,
    pub timeout: Duration,
    pub concurrency: usize,
    pub doc_cap_bytes: usize,
    pub max_chunks_per_doc: usize,
    pub global_concurrency: usize,
}

impl ContextualChunkingConfig {
    /// Read the `NEXUS_SEARCH_CONTEXTUAL_*` contract from process env.
    pub fn from_env() -> Result<Option<Self>, ContextualError> {
        Self::from_lookup(|k| std::env::var(k).ok())
    }

    /// Env-shape parser with an injectable lookup so unit tests never
    /// race process-global env across parallel test threads.
    pub fn from_lookup(
        get: impl Fn(&str) -> Option<String>,
    ) -> Result<Option<Self>, ContextualError> {
        let enabled = get(CONTEXTUAL_ENABLED_ENV)
            .map(|v| parse_bool(&v))
            .unwrap_or(false);
        if !enabled {
            return Ok(None);
        }
        let endpoint = get(CONTEXTUAL_ENDPOINT_ENV)
            .filter(|v| !v.trim().is_empty())
            .map(|v| v.trim().to_string())
            .ok_or_else(|| {
                ContextualError::Misconfigured(format!(
                    "{CONTEXTUAL_ENABLED_ENV}=true but {CONTEXTUAL_ENDPOINT_ENV} is unset — \
                     the generator needs an OpenAI-compatible chat-completions URL",
                ))
            })?;
        let model = get(CONTEXTUAL_MODEL_ENV)
            .filter(|v| !v.trim().is_empty())
            .map(|v| v.trim().to_string())
            .ok_or_else(|| {
                ContextualError::Misconfigured(format!(
                    "{CONTEXTUAL_ENABLED_ENV}=true but {CONTEXTUAL_MODEL_ENV} is unset — \
                     the generator needs an explicit chat model name",
                ))
            })?;
        let api_key = get(CONTEXTUAL_API_KEY_ENV)
            .filter(|v| !v.trim().is_empty())
            .map(|v| v.trim().to_string())
            .ok_or_else(|| {
                ContextualError::Misconfigured(format!(
                    "{CONTEXTUAL_ENABLED_ENV}=true but {CONTEXTUAL_API_KEY_ENV} is unset — \
                     the generator needs a bearer token for the endpoint",
                ))
            })?;
        let timeout_ms = parse_positive_u64(&get, CONTEXTUAL_TIMEOUT_MS_ENV, DEFAULT_TIMEOUT_MS)?;
        let concurrency =
            parse_positive_usize(&get, CONTEXTUAL_CONCURRENCY_ENV, DEFAULT_CONCURRENCY)?
                .min(HARD_MAX_CONCURRENCY);
        let doc_cap_bytes =
            parse_positive_usize(&get, CONTEXTUAL_DOC_CAP_ENV, DEFAULT_DOC_CAP_BYTES)?;
        let max_chunks_per_doc = parse_positive_usize(
            &get,
            CONTEXTUAL_MAX_CHUNKS_PER_DOC_ENV,
            DEFAULT_MAX_CHUNKS_PER_DOC,
        )?;
        let global_concurrency = parse_positive_usize(
            &get,
            CONTEXTUAL_GLOBAL_CONCURRENCY_ENV,
            DEFAULT_GLOBAL_CONCURRENCY,
        )?
        .min(HARD_MAX_GLOBAL_CONCURRENCY);
        Ok(Some(Self {
            endpoint,
            model,
            api_key,
            timeout: Duration::from_millis(timeout_ms),
            concurrency,
            doc_cap_bytes,
            max_chunks_per_doc,
            global_concurrency,
        }))
    }
}

fn parse_positive_u64(
    get: &impl Fn(&str) -> Option<String>,
    key: &str,
    default: u64,
) -> Result<u64, ContextualError> {
    match get(key).filter(|v| !v.trim().is_empty()) {
        None => Ok(default),
        Some(raw) => raw
            .trim()
            .parse::<u64>()
            .ok()
            .filter(|v| *v > 0)
            .ok_or_else(|| {
                ContextualError::Misconfigured(format!("{key}={raw:?} is not a positive integer"))
            }),
    }
}

fn parse_positive_usize(
    get: &impl Fn(&str) -> Option<String>,
    key: &str,
    default: usize,
) -> Result<usize, ContextualError> {
    match get(key).filter(|v| !v.trim().is_empty()) {
        None => Ok(default),
        Some(raw) => raw
            .trim()
            .parse::<usize>()
            .ok()
            .filter(|v| *v > 0)
            .ok_or_else(|| {
                ContextualError::Misconfigured(format!("{key}={raw:?} is not a positive integer"))
            }),
    }
}

fn parse_bool(raw: &str) -> bool {
    matches!(
        raw.trim().to_ascii_lowercase().as_str(),
        "1" | "true" | "yes" | "on"
    )
}

/// Context-prefix generator contract.  One trait so tests can swap
/// the live HTTP generator for a canned mock without stubbing
/// reqwest.  Sync API — every call site runs inside `spawn_blocking`.
pub trait ContextGenerator: Send + Sync {
    /// For each `chunk_text`, produce an optional short context
    /// prefix that summarises where the chunk sits in `document_text`.
    /// Returns one `Option<String>` per input chunk, in the same
    /// order.  `None` = generation failed for that chunk; the caller
    /// leaves the chunk's `embed_input` unchanged.
    fn generate_batch(&self, document_text: &str, chunk_texts: &[&str]) -> Vec<Option<String>>;

    /// Cap on how many chunks per document the caller sends into
    /// `generate_batch`.  The live HTTP generator returns its
    /// config's `max_chunks_per_doc`; mocks default to `usize::MAX`
    /// so they never truncate.
    fn max_chunks_per_doc(&self) -> usize {
        usize::MAX
    }
}

// ── HttpContextGenerator ─────────────────────────────────────────

use crate::llm_chat::{self, ChatMessage, ChatRequest};

/// System prompt — cargo-culted from Anthropic's original contextual-
/// retrieval blog and tightened for our indexing pipeline.  Kept
/// deliberately short so a small model (Haiku, gpt-4o-mini) can
/// follow it reliably.  The user message carries the doc + chunk.
const SYSTEM_PROMPT: &str = "\
You produce a one-to-two-sentence context prefix that situates a text \
chunk within its source document.  The prefix will be prepended to the \
chunk for a semantic-search embedder.  Resolve pronouns, add the section \
title if relevant, and note the topic.  Reply with ONLY the prefix — no \
Markdown, no explanations, no quotation marks.";

/// Ceiling on the returned context length (chars) to keep the
/// embedder input from ballooning if a chatty model over-answers.
const CONTEXT_MAX_CHARS: usize = 400;

/// Live HTTP context generator.
pub struct HttpContextGenerator {
    client: reqwest::blocking::Client,
    config: ContextualChunkingConfig,
}

impl HttpContextGenerator {
    /// Build the HTTP client for `config`.  No network I/O happens
    /// here.
    pub fn new(config: ContextualChunkingConfig) -> Result<Self, ContextualError> {
        let client = llm_chat::build_client(config.timeout)
            .map_err(|e| ContextualError::Http(e.to_string()))?;
        Ok(Self { client, config })
    }

    /// Single-chunk HTTP round-trip.  `Ok(Some(prefix))` on success,
    /// `Ok(None)` when the model returned an empty string (rare but
    /// legal), `Err(...)` on transport / status / parse failure.
    fn generate_one(
        &self,
        document_excerpt: &str,
        chunk_text: &str,
    ) -> Result<Option<String>, ContextualError> {
        let user = format!(
            "<document>\n{document_excerpt}\n</document>\n\n\
             <chunk>\n{chunk_text}\n</chunk>\n\n\
             Produce the context prefix for the chunk above."
        );
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
            response_format: None,
            temperature: Some(0.0),
            max_tokens: Some(120),
        };
        let content = llm_chat::chat_completion(
            &self.client,
            &self.config.endpoint,
            &self.config.api_key,
            &body,
        )
        .map_err(|e| ContextualError::Http(e.to_string()))?;
        let trimmed = content.trim();
        if trimmed.is_empty() {
            return Ok(None);
        }
        Ok(Some(clip_chars(trimmed, CONTEXT_MAX_CHARS)))
    }
}

impl ContextGenerator for HttpContextGenerator {
    fn max_chunks_per_doc(&self) -> usize {
        self.config.max_chunks_per_doc
    }

    fn generate_batch(&self, document_text: &str, chunk_texts: &[&str]) -> Vec<Option<String>> {
        if chunk_texts.is_empty() {
            return Vec::new();
        }
        // Truncate the document once, share it across chunks.  Keeps
        // per-chunk request size bounded regardless of doc length.
        let doc_excerpt = clip_chars(document_text, self.config.doc_cap_bytes);

        let concurrency = self.config.concurrency.min(chunk_texts.len()).max(1);
        let out: Mutex<Vec<Option<String>>> = Mutex::new(vec![None; chunk_texts.len()]);
        let next = AtomicUsize::new(0);

        // GLOBAL semaphore across all concurrently-indexing documents.
        // Per-doc `concurrency` bounds intra-doc parallelism; without
        // a global cap, N docs indexed in parallel by the outer
        // spawn_blocking pool would run N × concurrency LLM calls
        // simultaneously — enough to trip provider rate limits and
        // blow the plugin's thread footprint.  Initialized from the
        // config of the FIRST HttpContextGenerator built, on the
        // reasonable assumption that all plugin generators share
        // provider limits.
        let sem = global_llm_semaphore(self.config.global_concurrency);

        std::thread::scope(|s| {
            for _ in 0..concurrency {
                s.spawn(|| loop {
                    let i = next.fetch_add(1, Ordering::SeqCst);
                    if i >= chunk_texts.len() {
                        return;
                    }
                    // Acquire before HTTP; RAII releases on scope
                    // exit even if the request panics (Drop runs).
                    let _permit = sem.acquire();
                    match self.generate_one(&doc_excerpt, chunk_texts[i]) {
                        Ok(v) => {
                            out.lock()[i] = v;
                        }
                        Err(e) => {
                            tracing::warn!(
                                chunk_index = i,
                                err = %e,
                                "search-plugin: contextual chunking failed for chunk — leaving embed_input unchanged",
                            );
                            // out[i] already None
                        }
                    }
                });
            }
        });

        out.into_inner()
    }
}

// ── Global LLM concurrency semaphore ─────────────────────────────

/// Hand-rolled counting semaphore backed by `parking_lot::{Mutex,
/// Condvar}`.  Used to cap TOTAL in-flight contextual-chunking LLM
/// calls across every concurrently-indexing document.  We hand-roll
/// (rather than pull `tokio::sync::Semaphore`) because the caller is
/// a `std::thread::scope` closure inside `spawn_blocking` — an async
/// semaphore would need to bridge back into tokio, which is exactly
/// what the blocking design deliberately avoids.
struct BlockingSemaphore {
    permits: Mutex<usize>,
    cvar: Condvar,
}

impl BlockingSemaphore {
    fn new(permits: usize) -> Self {
        Self {
            permits: Mutex::new(permits),
            cvar: Condvar::new(),
        }
    }

    fn acquire(&self) -> SemPermit<'_> {
        let mut g = self.permits.lock();
        while *g == 0 {
            self.cvar.wait(&mut g);
        }
        *g -= 1;
        SemPermit(self)
    }
}

/// RAII guard — releases the permit on Drop.
struct SemPermit<'a>(&'a BlockingSemaphore);

impl Drop for SemPermit<'_> {
    fn drop(&mut self) {
        let mut g = self.0.permits.lock();
        *g += 1;
        self.0.cvar.notify_one();
    }
}

/// Process-wide semaphore, initialized on first use.  All
/// `HttpContextGenerator` instances in the process share it; the
/// first one initializes it with its own `global_concurrency`
/// value.  Rationale: multiple generators per plugin process are
/// unusual (the plugin builds one via `build_default_generator`);
/// when they exist, they share the same provider limits so a global
/// cap is exactly the invariant we want.
static GLOBAL_LLM_SEM: OnceLock<BlockingSemaphore> = OnceLock::new();

fn global_llm_semaphore(permits: usize) -> &'static BlockingSemaphore {
    GLOBAL_LLM_SEM.get_or_init(|| BlockingSemaphore::new(permits))
}

/// Truncate to at most `max` characters, on a char boundary.
fn clip_chars(s: &str, max: usize) -> String {
    if s.chars().count() <= max {
        return s.to_string();
    }
    s.chars().take(max).collect()
}

/// Shared handle so the service builds the generator once and
/// hands out `Arc` clones (matches [`crate::embedder::Embedder`]
/// posture).
pub type SharedContextGenerator = std::sync::Arc<dyn ContextGenerator>;

/// Best-effort builder — reads the config from env, wraps it in a
/// generator.  Returns `Ok(None)` when the kill-switch is OFF.
pub fn build_default_generator() -> Result<Option<SharedContextGenerator>, ContextualError> {
    let Some(cfg) = ContextualChunkingConfig::from_env()? else {
        return Ok(None);
    };
    tracing::info!(
        endpoint = %cfg.endpoint,
        model = %cfg.model,
        concurrency = cfg.concurrency,
        "search-plugin: contextual chunking enabled (expensive: 1 LLM call per chunk)",
    );
    let generator = HttpContextGenerator::new(cfg)?;
    Ok(Some(std::sync::Arc::new(generator)))
}

/// Apply context prefixes to chunks in-place.  Called by
/// `index_one` / `do_index_documents` right AFTER
/// `chunk_document(text)` and BEFORE the embedder runs.
///
/// Prepends `<context>\n\n` to each chunk's `embed_input`; leaves
/// `chunk.text` (which drives BM25) UNCHANGED — this is the whole
/// point of the split-input design in `chunker::Chunk`.
///
/// Chunks whose generator returned `None` pass through untouched
/// (graceful degradation).  Chunks past `max_chunks_per_doc` also
/// pass through untouched — a 5 MB document that chunks into 500
/// pieces would otherwise trigger 500 LLM calls per index pass; the
/// cap ensures the LLM bill scales with the doc count, not the
/// chunk count, above the cap.
pub fn apply_contexts(
    generator: &dyn ContextGenerator,
    document_text: &str,
    chunks: &mut [crate::chunker::Chunk],
    max_chunks_per_doc: usize,
) {
    if chunks.is_empty() {
        return;
    }
    // Send at most `max_chunks_per_doc` chunks to the LLM; the tail
    // keeps its plain heading-prefixed `embed_input`.
    let n = chunks.len().min(max_chunks_per_doc);
    if n < chunks.len() {
        tracing::debug!(
            total_chunks = chunks.len(),
            capped_at = n,
            "search-plugin: contextual chunking capped at max_chunks_per_doc — tail chunks keep plain embed_input",
        );
    }
    let chunk_texts: Vec<&str> = chunks[..n].iter().map(|c| c.text.as_str()).collect();
    let contexts = generator.generate_batch(document_text, &chunk_texts);
    for (chunk, maybe_ctx) in chunks.iter_mut().take(n).zip(contexts) {
        if let Some(ctx) = maybe_ctx {
            let new_input = format!("{ctx}\n\n{}", chunk.embed_input);
            chunk.embed_input = new_input;
        }
    }
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
        assert!(ContextualChunkingConfig::from_lookup(lookup(&[]))
            .unwrap()
            .is_none());
        assert!(ContextualChunkingConfig::from_lookup(lookup(&[(
            CONTEXTUAL_ENABLED_ENV,
            "false"
        )]))
        .unwrap()
        .is_none());
    }

    #[test]
    fn config_enabled_but_missing_pieces_errors_loud() {
        for (missing_key, present) in [
            (
                CONTEXTUAL_ENDPOINT_ENV,
                vec![CONTEXTUAL_MODEL_ENV, CONTEXTUAL_API_KEY_ENV],
            ),
            (
                CONTEXTUAL_MODEL_ENV,
                vec![CONTEXTUAL_ENDPOINT_ENV, CONTEXTUAL_API_KEY_ENV],
            ),
            (
                CONTEXTUAL_API_KEY_ENV,
                vec![CONTEXTUAL_ENDPOINT_ENV, CONTEXTUAL_MODEL_ENV],
            ),
        ] {
            let mut pairs = vec![(CONTEXTUAL_ENABLED_ENV, "true")];
            for k in present {
                pairs.push((k, "x"));
            }
            let err = ContextualChunkingConfig::from_lookup(lookup(&pairs)).unwrap_err();
            assert!(
                matches!(err, ContextualError::Misconfigured(ref m) if m.contains(missing_key)),
                "missing {missing_key}: {err:?}",
            );
        }
    }

    #[test]
    fn config_full_set_parses_and_applies_defaults() {
        let cfg = ContextualChunkingConfig::from_lookup(lookup(&[
            (CONTEXTUAL_ENABLED_ENV, "true"),
            (
                CONTEXTUAL_ENDPOINT_ENV,
                "http://localhost:9/v1/chat/completions",
            ),
            (CONTEXTUAL_MODEL_ENV, "claude-3-haiku"),
            (CONTEXTUAL_API_KEY_ENV, "sk-test"),
        ]))
        .unwrap()
        .expect("config present");
        assert_eq!(cfg.model, "claude-3-haiku");
        assert_eq!(cfg.timeout, Duration::from_millis(DEFAULT_TIMEOUT_MS));
        assert_eq!(cfg.concurrency, DEFAULT_CONCURRENCY);
        assert_eq!(cfg.doc_cap_bytes, DEFAULT_DOC_CAP_BYTES);
        assert_eq!(cfg.max_chunks_per_doc, DEFAULT_MAX_CHUNKS_PER_DOC);
        assert_eq!(cfg.global_concurrency, DEFAULT_GLOBAL_CONCURRENCY);
    }

    #[test]
    fn config_clamps_concurrency_to_hard_ceiling() {
        let cfg = ContextualChunkingConfig::from_lookup(lookup(&[
            (CONTEXTUAL_ENABLED_ENV, "true"),
            (CONTEXTUAL_ENDPOINT_ENV, "http://x/v1/chat/completions"),
            (CONTEXTUAL_MODEL_ENV, "m"),
            (CONTEXTUAL_API_KEY_ENV, "sk-x"),
            (CONTEXTUAL_CONCURRENCY_ENV, "999"),
        ]))
        .unwrap()
        .expect("config present");
        assert_eq!(cfg.concurrency, HARD_MAX_CONCURRENCY);
    }

    #[test]
    fn config_rejects_bad_numeric_env() {
        for bad_key in [
            CONTEXTUAL_TIMEOUT_MS_ENV,
            CONTEXTUAL_CONCURRENCY_ENV,
            CONTEXTUAL_DOC_CAP_ENV,
            CONTEXTUAL_MAX_CHUNKS_PER_DOC_ENV,
            CONTEXTUAL_GLOBAL_CONCURRENCY_ENV,
        ] {
            let err = ContextualChunkingConfig::from_lookup(lookup(&[
                (CONTEXTUAL_ENABLED_ENV, "true"),
                (CONTEXTUAL_ENDPOINT_ENV, "http://x/v1/chat/completions"),
                (CONTEXTUAL_MODEL_ENV, "m"),
                (CONTEXTUAL_API_KEY_ENV, "sk-x"),
                (bad_key, "not-a-number"),
            ]))
            .unwrap_err();
            assert!(
                matches!(err, ContextualError::Misconfigured(ref m) if m.contains(bad_key)),
                "bad {bad_key}: {err:?}",
            );
        }
    }

    // ── apply_contexts + MockContextGenerator ────────────────────

    struct MockContextGenerator {
        prefixes: Vec<Option<String>>,
    }

    impl ContextGenerator for MockContextGenerator {
        fn generate_batch(&self, _doc: &str, chunks: &[&str]) -> Vec<Option<String>> {
            // Pad / truncate the canned list to match the caller's
            // chunk count so a mock configured with N prefixes can
            // drive a document of any size.
            let mut out = self.prefixes.clone();
            out.resize(chunks.len(), None);
            out
        }
    }

    fn make_chunks(pairs: &[(&str, &str)]) -> Vec<crate::chunker::Chunk> {
        pairs
            .iter()
            .enumerate()
            .map(|(i, (text, embed))| crate::chunker::Chunk {
                chunk_index: i as u32,
                text: (*text).to_string(),
                embed_input: (*embed).to_string(),
            })
            .collect()
    }

    #[test]
    fn apply_contexts_prepends_to_embed_input_only() {
        let mut chunks = make_chunks(&[
            ("body A", "heading A > body A"),
            ("body B", "heading B > body B"),
        ]);
        let gen = MockContextGenerator {
            prefixes: vec![Some("ctx-A".into()), Some("ctx-B".into())],
        };
        apply_contexts(&gen, "the whole document", &mut chunks, usize::MAX);

        // text UNCHANGED.
        assert_eq!(chunks[0].text, "body A");
        assert_eq!(chunks[1].text, "body B");
        // embed_input PREPENDED with the context prefix + a blank line.
        assert_eq!(chunks[0].embed_input, "ctx-A\n\nheading A > body A");
        assert_eq!(chunks[1].embed_input, "ctx-B\n\nheading B > body B");
    }

    #[test]
    fn apply_contexts_leaves_none_slots_unchanged() {
        let mut chunks = make_chunks(&[
            ("body A", "embed A"),
            ("body B", "embed B"),
            ("body C", "embed C"),
        ]);
        let gen = MockContextGenerator {
            prefixes: vec![Some("ctx-A".into()), None, Some("ctx-C".into())],
        };
        apply_contexts(&gen, "doc", &mut chunks, usize::MAX);

        assert_eq!(chunks[0].embed_input, "ctx-A\n\nembed A");
        // None -> chunk B unchanged.
        assert_eq!(chunks[1].embed_input, "embed B");
        assert_eq!(chunks[2].embed_input, "ctx-C\n\nembed C");
    }

    #[test]
    fn apply_contexts_no_op_on_empty_chunks() {
        let mut chunks: Vec<crate::chunker::Chunk> = Vec::new();
        let gen = MockContextGenerator { prefixes: vec![] };
        apply_contexts(&gen, "doc", &mut chunks, usize::MAX);
        assert!(chunks.is_empty());
    }

    #[test]
    fn apply_contexts_caps_at_max_chunks_per_doc() {
        // 5 chunks in the input, cap at 2: first two get prefixed,
        // remaining three keep their plain embed_input.  This is
        // the cost-safety valve for large docs.
        let mut chunks = make_chunks(&[
            ("body 0", "embed 0"),
            ("body 1", "embed 1"),
            ("body 2", "embed 2"),
            ("body 3", "embed 3"),
            ("body 4", "embed 4"),
        ]);
        // Mock always returns "OK-<idx>" for whatever it's asked to
        // process — but with cap=2 it must only see chunks 0 and 1.
        let gen = CountingMockGenerator::default();
        apply_contexts(&gen, "doc", &mut chunks, 2);

        assert_eq!(chunks[0].embed_input, "OK-0\n\nembed 0");
        assert_eq!(chunks[1].embed_input, "OK-1\n\nembed 1");
        for (i, chunk) in chunks.iter().enumerate().take(5).skip(2) {
            assert_eq!(
                chunk.embed_input,
                format!("embed {i}"),
                "chunk {i} past the cap should be untouched"
            );
        }
        assert_eq!(
            gen.batches_seen.lock().len(),
            1,
            "generate_batch called exactly once with the capped slice"
        );
        assert_eq!(
            gen.batches_seen.lock()[0],
            2,
            "generate_batch received exactly 2 chunk texts",
        );
    }

    /// Mock that records how many chunk_texts each generate_batch
    /// call received, so tests can prove the cap was applied at the
    /// caller (not that the mock silently ignored the tail).
    #[derive(Default)]
    struct CountingMockGenerator {
        batches_seen: Mutex<Vec<usize>>,
    }

    impl ContextGenerator for CountingMockGenerator {
        fn generate_batch(&self, _doc: &str, chunks: &[&str]) -> Vec<Option<String>> {
            self.batches_seen.lock().push(chunks.len());
            (0..chunks.len()).map(|i| Some(format!("OK-{i}"))).collect()
        }
    }

    // ── HttpContextGenerator over one-shot server ───────────────

    use crate::llm_chat::test_http::spawn_one_shot;

    fn test_config(endpoint: String) -> ContextualChunkingConfig {
        ContextualChunkingConfig {
            endpoint,
            model: "test-model".to_string(),
            api_key: "sk-test".to_string(),
            timeout: Duration::from_secs(5),
            concurrency: 1,
            doc_cap_bytes: 4096,
            max_chunks_per_doc: usize::MAX,
            global_concurrency: 4,
        }
    }

    #[test]
    fn http_generator_parses_prefix_from_choices() {
        let body = serde_json::json!({
            "choices": [{
                "message": {"content": "This chunk describes X in section Y."}
            }]
        })
        .to_string();
        let (addr, handle) = spawn_one_shot("200 OK", body);
        let gen =
            HttpContextGenerator::new(test_config(format!("http://{addr}/v1/chat/completions")))
                .unwrap();

        let out = gen.generate_batch("full doc", &["chunk text"]);
        assert_eq!(
            out,
            vec![Some("This chunk describes X in section Y.".to_string())]
        );
        let request = handle.join().unwrap().to_lowercase();
        assert!(request.contains("authorization: bearer sk-test"));
    }

    #[test]
    fn http_generator_empty_content_returns_none_slot() {
        // 200 OK but empty content — treat as "no useful prefix" and
        // pass through; do NOT surface as an error.
        let body = serde_json::json!({
            "choices": [{"message": {"content": "  "}}]
        })
        .to_string();
        let (addr, _) = spawn_one_shot("200 OK", body);
        let gen =
            HttpContextGenerator::new(test_config(format!("http://{addr}/v1/chat/completions")))
                .unwrap();
        let out = gen.generate_batch("doc", &["chunk"]);
        assert_eq!(out, vec![None]);
    }

    #[test]
    fn http_generator_non_2xx_returns_none_slot() {
        let (addr, _) = spawn_one_shot("500 Internal Server Error", "boom".to_string());
        let gen =
            HttpContextGenerator::new(test_config(format!("http://{addr}/v1/chat/completions")))
                .unwrap();
        // The generate_batch wrapper converts the internal error to
        // a None slot + a warning log — indexing must NEVER be
        // stalled by a broken LLM endpoint.
        let out = gen.generate_batch("doc", &["chunk"]);
        assert_eq!(out, vec![None]);
    }

    #[test]
    fn http_generator_empty_chunk_list_returns_empty() {
        let gen =
            HttpContextGenerator::new(test_config("http://127.0.0.1:1/v1/chat/completions".into()))
                .unwrap();
        assert!(gen.generate_batch("doc", &[]).is_empty());
    }

    #[test]
    fn clip_chars_stays_on_char_boundary() {
        // Multi-byte chars — clip_chars must never split them.
        let s = "αβγδε".repeat(200);
        let clipped = clip_chars(&s, 50);
        assert_eq!(clipped.chars().count(), 50);
        // Round-trips as valid UTF-8 (implicit — String is always UTF-8).
        assert!(std::str::from_utf8(clipped.as_bytes()).is_ok());
    }
}
