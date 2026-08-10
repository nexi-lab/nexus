//! Text-to-vector embedding abstraction for SemanticQuery (Phase 2
//! of the Python-parity roadmap; see `PARITY_ROADMAP.md` D2).
//!
//! # Three impls
//!
//! - [`MockEmbedder`] — deterministic hash-projection.  Always
//!   compiled; used by unit + integration tests so they never
//!   download a real model or link ONNX Runtime.  Same text →
//!   same vector, so exact-match queries return the seed doc at
//!   cosine distance 0.
//!
//! - [`FastEmbedder`] — behind the `semantic` Cargo feature (default
//!   on).  Wraps `fastembed::TextEmbedding` with ort `load-dynamic`
//!   so onnxruntime.{dll,dylib,so} is shipped alongside the plugin
//!   cdylib rather than statically linked.  Model files live under
//!   `$NEXUS_SEARCH_MODEL_DIR` (falls back to
//!   `$NEXUS_DATA_DIR/plugins/search/models/`).
//!
//! - [`RemoteEmbedder`] (#4614) — always compiled; blocking HTTP to
//!   an OpenAI-compatible `/v1/embeddings` endpoint.  Selected by
//!   setting `NEXUS_SEARCH_EMBED_API_URL` (+ `_MODEL`, `_DIM`,
//!   optional `_API_KEY`), and wins over `FastEmbedder` so a
//!   deployment migrating from the pre-P12 API-embedding stack keeps
//!   its embedding quality instead of being forced onto local
//!   mE5-small.  The AnnIndex tag derives from model+dim so the
//!   `ann-<tag>-v<n>` directory contract keeps working across
//!   embedder swaps.
//!
//! # Concurrency
//!
//! [`Embedder`] takes `&self` for `embed_batch` so callers can share
//! an `Arc<dyn Embedder>` across the tokio pool.  `MockEmbedder` is
//! stateless and thread-safe by construction.  `FastEmbedder`
//! serialises internally via a `parking_lot::Mutex` — fastembed's
//! own `embed` is `&mut self` (ort session state), and holding the
//! mutex across the CPU-bound embed call is fine because the plugin
//! runs on a `spawn_blocking` worker.  A future phase can grow a
//! pool of session handles if concurrent throughput becomes a
//! bottleneck.
//!
//! # Cold-start
//!
//! `FastEmbedder::load` is 300 ms – 1 s on typical hosts (mmap +
//! ort session build + graph opt).  The service wires a
//! `OnceCell<Arc<dyn Embedder>>` so the cost lands on the first
//! SemanticQuery rather than at plugin creation — a slim / lite
//! deployment that never issues SemanticQuery pays nothing.

use std::path::{Path, PathBuf};
use std::sync::Arc;

/// Batch text-embedding contract.  One trait so the RPC handler
/// can hand out an `Arc<dyn Embedder>` and the storage layer stays
/// oblivious to whether the vectors came from a real ONNX session
/// or a mock hash.
pub trait Embedder: Send + Sync {
    /// Embed a batch of texts.  Returns one vector per input in the
    /// same order.  Empty input yields empty output — callers do
    /// not need to short-circuit.
    fn embed_batch(&self, texts: &[&str]) -> Result<Vec<Vec<f32>>, EmbedError>;

    /// Embedding dimensionality.  Callers pin their AnnIndex to
    /// this value at open-or-create time (see `AnnIndex::dim`).
    fn dim(&self) -> usize;

    /// Short model identifier for the AnnIndex directory tag
    /// (`ann-<tag>-v<n>` per D4).  Same embedder ⇒ same vector
    /// space ⇒ same index directory; a model swap changes the tag
    /// so the new index gets its own directory alongside the old.
    fn tag(&self) -> &str;

    /// Human-readable name for logs + operator diagnostics.  May
    /// contain spaces, punctuation, model-version hints.  Distinct
    /// from `tag()` which lands in filesystem paths.
    fn display_name(&self) -> &str {
        self.tag()
    }
}

/// Errors surfaced from embedding a batch.
#[derive(Debug, thiserror::Error)]
pub enum EmbedError {
    /// The embedder was never wired up (e.g. `semantic` feature
    /// off, or the model files couldn't be located at startup).
    /// Callers surface this as an HTTP 503-ish "semantic search
    /// unavailable" — keyword search stays available.
    #[error("embedder not available: {0}")]
    NotAvailable(String),

    /// The model file / tokenizer file was found but failed to
    /// load.  Bad ONNX opset, corrupt bytes, tokenizer format
    /// mismatch, etc.  Distinct from `NotAvailable` so operators
    /// know to inspect the model directory rather than turn a
    /// feature flag on.
    #[error("embedder load failed: {0}")]
    Load(String),

    /// The embed call itself failed — session run error, OOM, etc.
    /// Rare in practice on validated models.
    #[error("embed runtime failed: {0}")]
    Runtime(String),
}

// ── MockEmbedder (always compiled) ────────────────────────────────
//
// Deterministic per-text projection.  Same text produces the same
// vector, so a query embed of `X` and an add_vector under the doc
// whose text is `X` land at cosine distance 0 — exactly what
// integration tests want to assert.  Two hash rounds mix the byte
// stream into a 64-bit seed, then each dimension gets a
// golden-ratio-shifted derivative for spatial spread; a small
// constant bias (`+ 0.001`) guarantees non-zero vectors so
// AnnIndex's zero-guard does not reject empty strings.

/// Deterministic hash-projection embedder.  Fixed 384-dim by
/// default to match `multilingual-e5-small`'s output so a test
/// harness can swap Mock → Fast without changing the AnnIndex
/// directory shape.
pub struct MockEmbedder {
    dim: usize,
}

impl MockEmbedder {
    /// Fresh mock at the default 384-dim (matches mE5-small).
    pub fn new() -> Self {
        Self::with_dim(384)
    }

    /// Custom dim — used by tests that want small vectors so the
    /// assertion output stays readable.  Panics on `dim == 0` so a
    /// misconfigured caller fails at construction, not at first
    /// embed.
    pub fn with_dim(dim: usize) -> Self {
        assert!(dim > 0, "MockEmbedder dim must be > 0");
        Self { dim }
    }
}

impl Default for MockEmbedder {
    fn default() -> Self {
        Self::new()
    }
}

impl Embedder for MockEmbedder {
    fn embed_batch(&self, texts: &[&str]) -> Result<Vec<Vec<f32>>, EmbedError> {
        Ok(texts.iter().map(|t| mock_vec(t, self.dim)).collect())
    }

    fn dim(&self) -> usize {
        self.dim
    }

    fn tag(&self) -> &str {
        "mock"
    }
}

/// FNV-1a → per-dim golden-ratio shuffle.  Deterministic, non-zero,
/// spread across `[-1, 1]`.  Non-cryptographic; the goal is
/// reproducibility, not distributional quality — tests care that
/// same input maps to same output and different inputs map to
/// different outputs.
fn mock_vec(text: &str, dim: usize) -> Vec<f32> {
    const FNV_OFFSET: u64 = 14_695_981_039_346_656_037;
    const FNV_PRIME: u64 = 1_099_511_628_211;
    const GOLDEN: u64 = 0x9E37_79B9_7F4A_7C15;

    let mut hash: u64 = FNV_OFFSET;
    for b in text.bytes() {
        hash ^= b as u64;
        hash = hash.wrapping_mul(FNV_PRIME);
    }

    (0..dim)
        .map(|i| {
            let mixed = hash.wrapping_add((i as u64).wrapping_mul(GOLDEN));
            // Map high 32 bits into (-1, 1); +0.001 bias keeps the
            // vector strictly non-zero for AnnIndex's cosine guard.
            let raw = (mixed >> 32) as u32 as f32 / u32::MAX as f32;
            0.001 + raw - 0.5
        })
        .collect()
}

// ── FastEmbedder (feature-gated) ──────────────────────────────────

#[cfg(feature = "semantic")]
mod fast {
    use super::*;

    use parking_lot::Mutex;

    /// Directory-based fastembed wrapper.  Constructed by
    /// [`FastEmbedder::load`] which locates model + tokenizer
    /// files under `dir` and initialises ort's load-dynamic path.
    ///
    /// The dir layout matches what a `huggingface-cli download`
    /// produces for e.g. `intfloat/multilingual-e5-small`:
    ///
    /// ```text
    /// <dir>/
    ///   model.onnx
    ///   tokenizer.json
    ///   config.json
    ///   special_tokens_map.json
    ///   tokenizer_config.json
    /// ```
    ///
    /// Missing any of these produces a typed `Load` error at
    /// construction time — the plugin surfaces that as "SemanticQuery
    /// unavailable, model files not found in $NEXUS_SEARCH_MODEL_DIR"
    /// so operators immediately see the fix.
    pub struct FastEmbedder {
        inner: Mutex<fastembed::TextEmbedding>,
        dim: usize,
        tag: String,
    }

    impl FastEmbedder {
        /// Load the model at `dir`, tagging the resulting index with
        /// `tag` (typically `mE5-small-v1`).  See the module doc for
        /// the expected on-disk layout.
        ///
        /// `dylib_path` names the ONNX Runtime library the ort crate
        /// will `dlopen`.  Must be an absolute path to a
        /// `.so`/`.dylib`/`.dll` that matches ort's expected version
        /// (`onnxruntime` 1.19+ for ort 2.0.0-rc.10).  Init happens
        /// exactly once per process; subsequent calls with a
        /// different path are silently ignored — set this correctly
        /// at plugin startup or you get a stale runtime.
        pub fn load(dir: &Path, tag: &str, dylib_path: &Path) -> Result<Self, EmbedError> {
            // ort init is process-global; first successful call wins.
            // `init_from` returns a Result — a prior failing init (e.g.
            // dylib not found) surfaces here; we swallow and let the
            // TextEmbedding constructor below fail with a more actionable
            // "load failed" message.  If a prior plugin already ran init
            // successfully, `commit()` on the builder is a no-op.
            if let Ok(builder) = ort::init_from(dylib_path.to_string_lossy().into_owned()) {
                let _ = builder.commit();
            }

            let read = |name: &str| -> Result<Vec<u8>, EmbedError> {
                std::fs::read(dir.join(name)).map_err(|e| {
                    EmbedError::Load(format!("read {} in {}: {}", name, dir.display(), e))
                })
            };

            let tokenizer_files = fastembed::TokenizerFiles {
                tokenizer_file: read("tokenizer.json")?,
                config_file: read("config.json")?,
                special_tokens_map_file: read("special_tokens_map.json")?,
                tokenizer_config_file: read("tokenizer_config.json")?,
            };
            let model_bytes = read("model.onnx")?;

            let udm = fastembed::UserDefinedEmbeddingModel::new(model_bytes, tokenizer_files);
            let mut model = fastembed::TextEmbedding::try_new_from_user_defined(
                udm,
                fastembed::InitOptionsUserDefined::default(),
            )
            .map_err(|e| EmbedError::Load(format!("TextEmbedding init: {e}")))?;

            // Probe dim — fastembed 5.x has no dim() accessor; run
            // a one-doc embed to discover it.  Cached for later
            // calls so the probe is amortised to zero.  We embed a
            // literal so an empty-string quirk in the model can't
            // trip the probe.
            let probe = model
                .embed(vec!["probe"], None)
                .map_err(|e| EmbedError::Load(format!("dim probe: {e}")))?;
            let dim = probe
                .first()
                .ok_or_else(|| EmbedError::Load("dim probe: empty result".to_string()))?
                .len();
            if dim == 0 {
                return Err(EmbedError::Load(
                    "dim probe: zero-length vector".to_string(),
                ));
            }

            Ok(Self {
                inner: Mutex::new(model),
                dim,
                tag: tag.to_string(),
            })
        }
    }

    impl Embedder for FastEmbedder {
        fn embed_batch(&self, texts: &[&str]) -> Result<Vec<Vec<f32>>, EmbedError> {
            if texts.is_empty() {
                return Ok(Vec::new());
            }
            // fastembed::embed takes ownership of the batch — allocate
            // once here so callers can keep their `&[&str]` shape.
            let owned: Vec<String> = texts.iter().map(|s| (*s).to_owned()).collect();
            let mut lock = self.inner.lock();
            lock.embed(owned, None)
                .map_err(|e| EmbedError::Runtime(e.to_string()))
        }

        fn dim(&self) -> usize {
            self.dim
        }

        fn tag(&self) -> &str {
            &self.tag
        }
    }
}

#[cfg(feature = "semantic")]
pub use fast::FastEmbedder;

// ── RemoteEmbedder (#4614, always compiled) ───────────────────────
//
// Blocking HTTP against an OpenAI-compatible embeddings endpoint.
// Every `embed_batch` call site in the service runs inside
// `spawn_blocking`, and `reqwest::blocking` drives its I/O on a
// dedicated internal runtime thread — the plugin's own tokio pool is
// never re-entered, so a plain synchronous `.send()` here is safe.
//
// Not gated behind `semantic`: no ONNX involved, so a build without
// the local-model feature still gets a dense lane when an API
// endpoint is configured.

/// Embeddings endpoint URL, e.g. `https://api.openai.com/v1/embeddings`
/// or a local gateway.  Presence of this var selects the remote
/// embedder over the local ONNX path.
pub const EMBED_API_URL_ENV: &str = "NEXUS_SEARCH_EMBED_API_URL";
/// Bearer token for the endpoint.  Optional — local gateways often
/// need none; when set it is sent as `Authorization: Bearer <key>`.
pub const EMBED_API_KEY_ENV: &str = "NEXUS_SEARCH_EMBED_API_KEY";
/// Model name sent in the request body (e.g. `text-embedding-3-small`).
/// Required whenever the URL is set.
pub const EMBED_MODEL_ENV: &str = "NEXUS_SEARCH_EMBED_MODEL";
/// Embedding dimensionality.  Required whenever the URL is set: it
/// pins the AnnIndex dim at open-or-create time WITHOUT a boot-time
/// network probe, and every response is validated against it so a
/// model/dim mismatch fails loud instead of corrupting the index.
pub const EMBED_DIM_ENV: &str = "NEXUS_SEARCH_EMBED_DIM";
/// Per-request timeout in seconds (default 30).
pub const EMBED_TIMEOUT_ENV: &str = "NEXUS_SEARCH_EMBED_TIMEOUT_SECONDS";

const DEFAULT_EMBED_TIMEOUT_SECONDS: u64 = 30;

/// Provider batch ceiling — one HTTP request carries at most this
/// many inputs (OpenAI caps at 2048; a conservative bound keeps
/// request bodies reasonable for local gateways too).  Larger
/// `embed_batch` calls are transparently chunked.
const REMOTE_EMBED_MAX_BATCH: usize = 512;

/// Parsed remote-embedder configuration.  `from_env` returns
/// `Ok(None)` when [`EMBED_API_URL_ENV`] is unset (caller falls back
/// to the local path) and `Err` when the URL is set but the rest of
/// the contract is incomplete — a typo'd config must fail LOUD, not
/// silently reindex the corpus into a different vector space.
#[derive(Debug, Clone)]
pub struct RemoteEmbedderConfig {
    pub url: String,
    pub api_key: Option<String>,
    pub model: String,
    pub dim: usize,
    pub timeout: std::time::Duration,
}

impl RemoteEmbedderConfig {
    /// Read the `NEXUS_SEARCH_EMBED_*` contract from process env.
    pub fn from_env() -> Result<Option<Self>, EmbedError> {
        Self::from_lookup(|k| std::env::var(k).ok())
    }

    /// Env-shape parser with an injectable lookup so unit tests never
    /// touch process-global env vars (which race across test threads).
    fn from_lookup(get: impl Fn(&str) -> Option<String>) -> Result<Option<Self>, EmbedError> {
        let url = match get(EMBED_API_URL_ENV).filter(|v| !v.trim().is_empty()) {
            Some(u) => u.trim().to_string(),
            None => return Ok(None),
        };
        let model = get(EMBED_MODEL_ENV)
            .filter(|v| !v.trim().is_empty())
            .map(|v| v.trim().to_string())
            .ok_or_else(|| {
                EmbedError::NotAvailable(format!(
                    "{EMBED_API_URL_ENV} is set but {EMBED_MODEL_ENV} is not — \
                     the remote embedder needs an explicit model name",
                ))
            })?;
        let dim_raw = get(EMBED_DIM_ENV)
            .filter(|v| !v.trim().is_empty())
            .ok_or_else(|| {
                EmbedError::NotAvailable(format!(
                    "{EMBED_API_URL_ENV} is set but {EMBED_DIM_ENV} is not — \
                     the remote embedder needs the embedding dimensionality \
                     to pin the ANN index without a boot-time network probe",
                ))
            })?;
        let dim = dim_raw
            .trim()
            .parse::<usize>()
            .ok()
            .filter(|d| *d > 0)
            .ok_or_else(|| {
                EmbedError::NotAvailable(format!(
                    "{EMBED_DIM_ENV}={dim_raw:?} is not a positive integer",
                ))
            })?;
        let timeout_secs = match get(EMBED_TIMEOUT_ENV).filter(|v| !v.trim().is_empty()) {
            Some(t) => t
                .trim()
                .parse::<u64>()
                .ok()
                .filter(|s| *s > 0)
                .ok_or_else(|| {
                    EmbedError::NotAvailable(format!(
                        "{EMBED_TIMEOUT_ENV}={t:?} is not a positive integer",
                    ))
                })?,
            None => DEFAULT_EMBED_TIMEOUT_SECONDS,
        };
        let api_key = get(EMBED_API_KEY_ENV).filter(|v| !v.trim().is_empty());
        Ok(Some(Self {
            url,
            api_key,
            model,
            dim,
            timeout: std::time::Duration::from_secs(timeout_secs),
        }))
    }

    /// AnnIndex directory tag — derived from model + dim so the
    /// `ann-<tag>-v<n>` contract keeps working: same (model, dim) ⇒
    /// same vector space ⇒ same index directory; changing either
    /// re-tags and the fresh index lands alongside the old.
    pub fn tag(&self) -> String {
        format!("api-{}-{}", sanitize_tag(&self.model), self.dim)
    }
}

/// Filesystem-safe tag fragment: keep `[A-Za-z0-9._-]`, map anything
/// else (`/` in HF-style names, spaces, `:`) to `-`.  The tag lands
/// verbatim in the `ann-<tag>-v<n>` directory name.
fn sanitize_tag(raw: &str) -> String {
    raw.chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-') {
                c
            } else {
                '-'
            }
        })
        .collect()
}

#[derive(serde::Serialize)]
struct EmbeddingsRequest<'a> {
    model: &'a str,
    input: &'a [&'a str],
}

#[derive(serde::Deserialize)]
struct EmbeddingsResponse {
    data: Vec<EmbeddingItem>,
}

#[derive(serde::Deserialize)]
struct EmbeddingItem {
    index: usize,
    embedding: Vec<f32>,
}

/// OpenAI-compatible remote embedding backend.  See the module doc
/// and [`RemoteEmbedderConfig`] for the env contract.
pub struct RemoteEmbedder {
    client: reqwest::blocking::Client,
    config: RemoteEmbedderConfig,
    tag: String,
    display: String,
}

impl RemoteEmbedder {
    /// Build the HTTP client for `config`.  No network I/O happens
    /// here — a wrong URL or key surfaces on the first embed call as
    /// a typed `Runtime` error, keeping construction cheap for the
    /// lazy `OnceCell`-style init the service uses.
    pub fn new(config: RemoteEmbedderConfig) -> Result<Self, EmbedError> {
        let client = reqwest::blocking::Client::builder()
            .timeout(config.timeout)
            .build()
            .map_err(|e| EmbedError::Load(format!("remote embedder HTTP client: {e}")))?;
        let tag = config.tag();
        let display = format!("remote:{} ({}d @ {})", config.model, config.dim, config.url);
        Ok(Self {
            client,
            config,
            tag,
            display,
        })
    }

    /// One provider round-trip for ≤ [`REMOTE_EMBED_MAX_BATCH`] texts.
    fn embed_chunk(&self, texts: &[&str]) -> Result<Vec<Vec<f32>>, EmbedError> {
        let body = EmbeddingsRequest {
            model: &self.config.model,
            input: texts,
        };
        let mut req = self.client.post(&self.config.url).json(&body);
        if let Some(key) = &self.config.api_key {
            req = req.bearer_auth(key);
        }
        let resp = req.send().map_err(|e| {
            EmbedError::Runtime(format!("embeddings request to {}: {e}", self.config.url))
        })?;

        let status = resp.status();
        if !status.is_success() {
            // Truncated body excerpt so a provider error message (quota,
            // bad model, auth) reaches the operator without dumping an
            // HTML error page into the log.
            let body = resp.text().unwrap_or_default();
            let excerpt: String = body.chars().take(300).collect();
            return Err(EmbedError::Runtime(format!(
                "embeddings endpoint returned {status}: {excerpt}",
            )));
        }

        let parsed: EmbeddingsResponse = resp
            .json()
            .map_err(|e| EmbedError::Runtime(format!("embeddings response parse: {e}")))?;
        if parsed.data.len() != texts.len() {
            return Err(EmbedError::Runtime(format!(
                "embeddings response carried {} vectors for {} inputs",
                parsed.data.len(),
                texts.len(),
            )));
        }

        // Providers document `data` as request-ordered but also carry an
        // explicit per-item index — trust the index.
        let mut out: Vec<Option<Vec<f32>>> = vec![None; texts.len()];
        for item in parsed.data {
            if item.embedding.len() != self.config.dim {
                return Err(EmbedError::Runtime(format!(
                    "embeddings response dim {} != configured {} ({}={}) — \
                     fix {} or {} so the ANN index is not built in the wrong vector space",
                    item.embedding.len(),
                    self.config.dim,
                    EMBED_MODEL_ENV,
                    self.config.model,
                    EMBED_DIM_ENV,
                    EMBED_MODEL_ENV,
                )));
            }
            let slot = out.get_mut(item.index).ok_or_else(|| {
                EmbedError::Runtime(format!(
                    "embeddings response index {} out of range for {} inputs",
                    item.index,
                    texts.len(),
                ))
            })?;
            *slot = Some(item.embedding);
        }
        out.into_iter()
            .enumerate()
            .map(|(i, v)| {
                v.ok_or_else(|| {
                    EmbedError::Runtime(format!("embeddings response missing vector for input {i}"))
                })
            })
            .collect()
    }
}

impl Embedder for RemoteEmbedder {
    fn embed_batch(&self, texts: &[&str]) -> Result<Vec<Vec<f32>>, EmbedError> {
        if texts.is_empty() {
            return Ok(Vec::new());
        }
        let mut out = Vec::with_capacity(texts.len());
        for chunk in texts.chunks(REMOTE_EMBED_MAX_BATCH) {
            out.extend(self.embed_chunk(chunk)?);
        }
        Ok(out)
    }

    fn dim(&self) -> usize {
        self.config.dim
    }

    fn tag(&self) -> &str {
        &self.tag
    }

    fn display_name(&self) -> &str {
        &self.display
    }
}

// ── Discovery helpers ─────────────────────────────────────────────

/// Env var that overrides the default model directory.
pub const MODEL_DIR_ENV: &str = "NEXUS_SEARCH_MODEL_DIR";

/// Env var that overrides the default ONNX Runtime dylib path.
/// Same convention `ort` itself honours — kept as a plugin-level
/// constant so operators only set one place.
pub const ORT_DYLIB_ENV: &str = "ORT_DYLIB_PATH";

/// Resolve the model directory — `$NEXUS_SEARCH_MODEL_DIR` if set,
/// falling back to `<data_root>/models/`.  `data_root` is what
/// [`crate::index_manager::default_root`] returns (i.e. the plugin's
/// per-node state root).
pub fn resolve_model_dir(data_root: &Path) -> PathBuf {
    std::env::var(MODEL_DIR_ENV)
        .map(PathBuf::from)
        .unwrap_or_else(|_| data_root.join("models"))
}

/// AnnIndex tag of the built-in local model — single source for
/// [`build_default_embedder`] and the Stats identity field.
#[cfg(feature = "semantic")]
const LOCAL_EMBEDDER_TAG: &str = "mE5-small-v1";

/// CONFIGURED embedder identity without loading anything (#4617).
///
/// Stats polls must never pay the ~1s ONNX session build, so this
/// reports what [`build_default_embedder`] WOULD use: the remote
/// config's tag when `NEXUS_SEARCH_EMBED_API_URL` is set, else the
/// built-in local tag when the `semantic` feature is compiled in.
/// `None` = keyword-only mode (no embedder configured, or a partial
/// remote config that build_default_embedder will reject loudly).
pub fn configured_embedder_tag() -> Option<String> {
    match RemoteEmbedderConfig::from_env() {
        Ok(Some(cfg)) => return Some(cfg.tag()),
        Ok(None) => {}
        // Partial remote config: the embedder build fails loud, so
        // reporting the local fallback here would lie.
        Err(_) => return None,
    }
    #[cfg(feature = "semantic")]
    {
        Some(LOCAL_EMBEDDER_TAG.to_string())
    }
    #[cfg(not(feature = "semantic"))]
    {
        None
    }
}

/// Best-effort embedder factory — used by the service to lazily
/// build the embedder on first SemanticQuery.  Returns
/// `NotAvailable` when no remote endpoint is configured AND the
/// `semantic` feature is off (or its discovery step didn't find a
/// usable model / dylib).
///
/// The concrete return type is `Arc<dyn Embedder>` so tests can
/// swap in `MockEmbedder` without a compile-time cfg.
pub fn build_default_embedder(data_root: &Path) -> Result<Arc<dyn Embedder>, EmbedError> {
    // #4614: a configured remote/API endpoint wins over local ONNX so
    // deployments migrating from the pre-P12 API-embedding stack keep
    // their embedding quality.  A PARTIALLY configured remote (URL set
    // but model/dim missing) errors here rather than falling through —
    // a typo must not silently reindex the corpus onto mE5-small.
    if let Some(cfg) = RemoteEmbedderConfig::from_env()? {
        tracing::info!(
            model = %cfg.model,
            dim = cfg.dim,
            url = %cfg.url,
            tag = %cfg.tag(),
            "search-plugin: using remote embedding backend",
        );
        return Ok(Arc::new(RemoteEmbedder::new(cfg)?));
    }
    #[cfg(feature = "semantic")]
    {
        let dir = resolve_model_dir(data_root);
        let dylib = std::env::var(ORT_DYLIB_ENV).map_err(|_| {
            EmbedError::NotAvailable(format!(
                "{ORT_DYLIB_ENV} not set — cluster boot must point at the shipped onnxruntime library",
            ))
        })?;
        Ok(Arc::new(FastEmbedder::load(
            &dir,
            LOCAL_EMBEDDER_TAG,
            Path::new(&dylib),
        )?))
    }
    #[cfg(not(feature = "semantic"))]
    {
        let _ = data_root;
        Err(EmbedError::NotAvailable(format!(
            "search-plugin compiled without the `semantic` feature and no \
             remote embedding endpoint configured (set {EMBED_API_URL_ENV})",
        )))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn mock_same_text_same_vector() {
        let e = MockEmbedder::with_dim(8);
        let v1 = &e.embed_batch(&["hello"]).unwrap()[0];
        let v2 = &e.embed_batch(&["hello"]).unwrap()[0];
        assert_eq!(v1, v2, "mock must be deterministic per text");
    }

    #[test]
    fn mock_different_text_different_vector() {
        let e = MockEmbedder::with_dim(8);
        let v1 = &e.embed_batch(&["hello"]).unwrap()[0];
        let v2 = &e.embed_batch(&["world"]).unwrap()[0];
        assert_ne!(v1, v2, "distinct texts must map to distinct vectors");
    }

    #[test]
    fn mock_never_produces_zero_vector() {
        // AnnIndex rejects zero vectors — the mock must never trip
        // that guard on any text, including the empty string.
        let e = MockEmbedder::with_dim(16);
        for text in ["", " ", "a", "hello world", "\0"] {
            let v = &e.embed_batch(&[text]).unwrap()[0];
            let all_zero = v.iter().all(|&x| x == 0.0);
            assert!(!all_zero, "mock produced zero vector for {text:?}");
        }
    }

    #[test]
    fn mock_dim_stable() {
        let e = MockEmbedder::with_dim(384);
        for text in ["x", "widget alpha", "α β γ"] {
            let v = &e.embed_batch(&[text]).unwrap()[0];
            assert_eq!(v.len(), 384, "dim wrong for {text:?}");
        }
    }

    #[test]
    fn empty_batch_returns_empty() {
        let e = MockEmbedder::with_dim(8);
        let out = e.embed_batch(&[]).unwrap();
        assert!(out.is_empty());
    }

    #[test]
    fn resolve_model_dir_falls_back_to_data_root() {
        // Guard the env in case the user has NEXUS_SEARCH_MODEL_DIR
        // set in their shell.
        let saved = std::env::var(MODEL_DIR_ENV).ok();
        // SAFETY: this test does not run in parallel with any other
        // test that reads MODEL_DIR_ENV.
        unsafe { std::env::remove_var(MODEL_DIR_ENV) };
        let root = PathBuf::from("/opt/nexus-data/plugins/search");
        let dir = resolve_model_dir(&root);
        assert_eq!(dir, root.join("models"));
        if let Some(v) = saved {
            unsafe { std::env::set_var(MODEL_DIR_ENV, v) };
        }
    }

    #[test]
    fn resolve_model_dir_honours_env() {
        let saved = std::env::var(MODEL_DIR_ENV).ok();
        unsafe { std::env::set_var(MODEL_DIR_ENV, "/mnt/models") };
        let dir = resolve_model_dir(&PathBuf::from("/opt/nexus-data/plugins/search"));
        assert_eq!(dir, PathBuf::from("/mnt/models"));
        match saved {
            Some(v) => unsafe { std::env::set_var(MODEL_DIR_ENV, v) },
            None => unsafe { std::env::remove_var(MODEL_DIR_ENV) },
        }
    }

    #[cfg(not(feature = "semantic"))]
    #[test]
    fn build_default_returns_not_available_when_feature_off() {
        let root = PathBuf::from("/tmp");
        match build_default_embedder(&root) {
            Err(EmbedError::NotAvailable(msg)) => assert!(msg.contains("semantic")),
            other => panic!("expected NotAvailable, got {other:?}"),
        }
    }

    // ── RemoteEmbedder (#4614) ────────────────────────────────────

    /// Injectable-lookup helper so config tests never touch
    /// process-global env (which races across parallel test threads).
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
    fn remote_config_absent_url_means_none() {
        let cfg = RemoteEmbedderConfig::from_lookup(lookup(&[])).unwrap();
        assert!(cfg.is_none());
        // Empty-string URL counts as unset, not as a broken config.
        let cfg = RemoteEmbedderConfig::from_lookup(lookup(&[(EMBED_API_URL_ENV, "  ")])).unwrap();
        assert!(cfg.is_none());
    }

    #[test]
    fn remote_config_url_without_model_or_dim_fails_loud() {
        // URL set but model missing — must error, never silently fall
        // back to the local embedder (wrong vector space on reindex).
        let err = RemoteEmbedderConfig::from_lookup(lookup(&[
            (EMBED_API_URL_ENV, "http://localhost:9/v1/embeddings"),
            (EMBED_DIM_ENV, "1536"),
        ]))
        .unwrap_err();
        assert!(matches!(err, EmbedError::NotAvailable(ref m) if m.contains(EMBED_MODEL_ENV)));

        let err = RemoteEmbedderConfig::from_lookup(lookup(&[
            (EMBED_API_URL_ENV, "http://localhost:9/v1/embeddings"),
            (EMBED_MODEL_ENV, "text-embedding-3-small"),
        ]))
        .unwrap_err();
        assert!(matches!(err, EmbedError::NotAvailable(ref m) if m.contains(EMBED_DIM_ENV)));
    }

    #[test]
    fn remote_config_rejects_bad_dim_and_timeout() {
        for bad_dim in ["abc", "0", "-3", "1.5"] {
            let err = RemoteEmbedderConfig::from_lookup(lookup(&[
                (EMBED_API_URL_ENV, "http://localhost:9/v1/embeddings"),
                (EMBED_MODEL_ENV, "m"),
                (EMBED_DIM_ENV, bad_dim),
            ]))
            .unwrap_err();
            assert!(
                matches!(err, EmbedError::NotAvailable(ref m) if m.contains(EMBED_DIM_ENV)),
                "dim {bad_dim:?} accepted"
            );
        }
        let err = RemoteEmbedderConfig::from_lookup(lookup(&[
            (EMBED_API_URL_ENV, "http://localhost:9/v1/embeddings"),
            (EMBED_MODEL_ENV, "m"),
            (EMBED_DIM_ENV, "8"),
            (EMBED_TIMEOUT_ENV, "soon"),
        ]))
        .unwrap_err();
        assert!(matches!(err, EmbedError::NotAvailable(ref m) if m.contains(EMBED_TIMEOUT_ENV)));
    }

    #[test]
    fn remote_config_full_set_parses() {
        let cfg = RemoteEmbedderConfig::from_lookup(lookup(&[
            (EMBED_API_URL_ENV, " https://api.openai.com/v1/embeddings "),
            (EMBED_MODEL_ENV, "text-embedding-3-small"),
            (EMBED_DIM_ENV, "1536"),
            (EMBED_API_KEY_ENV, "sk-test"),
        ]))
        .unwrap()
        .expect("config should be present");
        assert_eq!(cfg.url, "https://api.openai.com/v1/embeddings");
        assert_eq!(cfg.model, "text-embedding-3-small");
        assert_eq!(cfg.dim, 1536);
        assert_eq!(cfg.api_key.as_deref(), Some("sk-test"));
        assert_eq!(cfg.timeout, std::time::Duration::from_secs(30));
        assert_eq!(cfg.tag(), "api-text-embedding-3-small-1536");
    }

    #[test]
    fn remote_tag_sanitizes_filesystem_hostile_model_names() {
        // HF-style org/model names, spaces, and colons all land in the
        // ann-<tag>-v<n> directory name — keep them path-safe.
        assert_eq!(
            sanitize_tag("intfloat/multilingual e5:small"),
            "intfloat-multilingual-e5-small"
        );
        assert_eq!(
            sanitize_tag("text-embedding-3-small"),
            "text-embedding-3-small"
        );
    }

    fn remote_test_config(url: String) -> RemoteEmbedderConfig {
        RemoteEmbedderConfig {
            url,
            api_key: Some("sk-test".to_string()),
            model: "test-embed".to_string(),
            dim: 3,
            timeout: std::time::Duration::from_secs(5),
        }
    }

    /// One-request localhost HTTP server: accepts a single connection,
    /// reads the full request (headers + Content-Length body), writes
    /// the canned response, and returns the raw request text for
    /// assertions.  Plain std — no dev-dep for a mock server.
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

    #[test]
    fn remote_embed_orders_by_index_and_sends_auth() {
        // Response items deliberately out of order — the client must
        // trust the per-item index, not arrival order.
        let body = serde_json::json!({
            "object": "list",
            "data": [
                {"object": "embedding", "index": 1, "embedding": [4.0, 5.0, 6.0]},
                {"object": "embedding", "index": 0, "embedding": [1.0, 2.0, 3.0]},
            ],
        })
        .to_string();
        let (addr, handle) = spawn_one_shot_http("200 OK", body);
        let emb = RemoteEmbedder::new(remote_test_config(format!("http://{addr}/v1/embeddings")))
            .unwrap();

        let out = emb.embed_batch(&["a", "b"]).unwrap();
        assert_eq!(out, vec![vec![1.0, 2.0, 3.0], vec![4.0, 5.0, 6.0]]);
        assert_eq!(emb.dim(), 3);
        assert_eq!(emb.tag(), "api-test-embed-3");

        let request = handle.join().unwrap().to_lowercase();
        assert!(
            request.contains("authorization: bearer sk-test"),
            "missing bearer auth"
        );
        assert!(
            request.contains("\"model\":\"test-embed\""),
            "missing model in body"
        );
        assert!(
            request.contains("\"input\":[\"a\",\"b\"]"),
            "missing input in body"
        );
    }

    #[test]
    fn remote_embed_surfaces_provider_error_status() {
        let body = r#"{"error": {"message": "quota exceeded"}}"#.to_string();
        let (addr, handle) = spawn_one_shot_http("429 Too Many Requests", body);
        let emb = RemoteEmbedder::new(remote_test_config(format!("http://{addr}/v1/embeddings")))
            .unwrap();

        let err = emb.embed_batch(&["a"]).unwrap_err();
        match err {
            EmbedError::Runtime(m) => {
                assert!(m.contains("429"), "status missing from {m:?}");
                assert!(
                    m.contains("quota exceeded"),
                    "body excerpt missing from {m:?}"
                );
            }
            other => panic!("expected Runtime, got {other:?}"),
        }
        let _ = handle.join().unwrap();
    }

    #[test]
    fn remote_embed_rejects_dim_mismatch() {
        // Provider answers with 2-dim vectors for a 3-dim config —
        // must error loud, never write into the ANN index.
        let body = serde_json::json!({
            "object": "list",
            "data": [{"object": "embedding", "index": 0, "embedding": [1.0, 2.0]}],
        })
        .to_string();
        let (addr, handle) = spawn_one_shot_http("200 OK", body);
        let emb = RemoteEmbedder::new(remote_test_config(format!("http://{addr}/v1/embeddings")))
            .unwrap();

        let err = emb.embed_batch(&["a"]).unwrap_err();
        assert!(
            matches!(err, EmbedError::Runtime(ref m) if m.contains("dim 2 != configured 3")),
            "got {err:?}"
        );
        let _ = handle.join().unwrap();
    }

    #[test]
    fn remote_embed_empty_batch_skips_network() {
        // Unroutable URL — an empty batch must return without dialing.
        let emb = RemoteEmbedder::new(remote_test_config(
            "http://127.0.0.1:9/v1/embeddings".to_string(),
        ))
        .unwrap();
        assert!(emb.embed_batch(&[]).unwrap().is_empty());
    }
}
