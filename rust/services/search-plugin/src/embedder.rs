//! Text-to-vector embedding abstraction for SemanticQuery (Phase 2
//! of the Python-parity roadmap; see `PARITY_ROADMAP.md` D2).
//!
//! # Two impls
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

/// FNV-1a → per-dim golden-ratio shuffle.  Deterministic + non-zero
/// + spread across [-1, 1].  Non-cryptographic; the goal is
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
            // If a prior plugin already ran init the ok() branch is
            // hit and this is a no-op.
            let _ = ort::init_from(dylib_path.to_string_lossy().into_owned()).commit();

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

/// Best-effort embedder factory — used by the service to lazily
/// build the embedder on first SemanticQuery.  Returns
/// `NotAvailable` when the `semantic` feature is off or the
/// discovery step didn't find a usable model / dylib.
///
/// The concrete return type is `Arc<dyn Embedder>` so tests can
/// swap in `MockEmbedder` without a compile-time cfg.
pub fn build_default_embedder(data_root: &Path) -> Result<Arc<dyn Embedder>, EmbedError> {
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
            "mE5-small-v1",
            Path::new(&dylib),
        )?))
    }
    #[cfg(not(feature = "semantic"))]
    {
        let _ = data_root;
        Err(EmbedError::NotAvailable(
            "search-plugin compiled without the `semantic` feature".to_string(),
        ))
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
}
