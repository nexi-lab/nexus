//! End-to-end integration test for LLM contextual chunking
//! (Feature 3 of the "fold nexus-server search runtime into the
//! plugin" migration).
//!
//! Drives the real `SearchServiceImpl::index_documents` RPC on a
//! tempdir-rooted `IndexManager` with a mock embedder that captures
//! the exact `embed_input` strings the embedder receives.  A mock
//! `ContextGenerator` returns pre-programmed context prefixes; the
//! test asserts that:
//!
//! - `chunk.text` (what BM25 sees) is UNCHANGED, and
//! - `chunk.embed_input` (what the embedder sees) is PREFIXED with
//!   the mock's context, exactly as `apply_contexts` claims.

use std::collections::HashMap;
use std::ffi::{c_void, CStr};
use std::os::raw::c_char;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;

use nexus_plugin_abi::KernelHandle;
use nexus_search_plugin::contextual_chunker::ContextGenerator;
use nexus_search_plugin::embedder::{EmbedError, Embedder};
use nexus_search_plugin::index_manager::IndexManager;
use nexus_search_plugin::search_proto::search_service_server::SearchService;
use nexus_search_plugin::search_proto::{
    DocumentInput, IndexDocumentsRequest, IndexRequest, LocateRequest, QueryRequest, QueryType,
    RefreshRequest,
};
use nexus_search_plugin::service::SearchServiceImpl;
use tempfile::TempDir;
use tonic::Request;

// ── Poison KernelHandle — index_documents never calls sys_* ─────

unsafe extern "C" fn poison_read(
    _: *const c_void,
    _: *const c_char,
    _: *mut *mut u8,
    _: *mut usize,
) -> i32 {
    -1
}
unsafe extern "C" fn poison_write(
    _: *const c_void,
    _: *const c_char,
    _: *const u8,
    _: usize,
) -> i32 {
    -1
}
unsafe extern "C" fn poison_stat(
    _: *const c_void,
    _: *const c_char,
    _: *mut *mut u8,
    _: *mut usize,
) -> i32 {
    -1
}
unsafe extern "C" fn poison_readdir(
    _: *const c_void,
    _: *const c_char,
    _: *mut *mut u8,
    _: *mut usize,
) -> i32 {
    -1
}
unsafe extern "C" fn poison_unlink(_: *const c_void, _: *const c_char) -> i32 {
    -1
}
unsafe extern "C" fn poison_mkdir(_: *const c_void, _: *const c_char) -> i32 {
    -1
}
unsafe extern "C" fn poison_rmdir(_: *const c_void, _: *const c_char) -> i32 {
    -1
}
unsafe extern "C" fn poison_rename(_: *const c_void, _: *const c_char, _: *const c_char) -> i32 {
    -1
}
unsafe extern "C" fn poison_stat_batch(
    _: *const c_void,
    _: *const c_char,
    _: *mut *mut u8,
    _: *mut usize,
) -> i32 {
    -1
}

fn poison_handle() -> KernelHandle {
    KernelHandle {
        sys_read: poison_read,
        sys_write: poison_write,
        sys_stat: poison_stat,
        sys_readdir: poison_readdir,
        sys_unlink: poison_unlink,
        sys_mkdir: poison_mkdir,
        sys_rmdir: poison_rmdir,
        sys_rename: poison_rename,
        sys_stat_batch: poison_stat_batch,
        free_buf: nexus_plugin_abi::nexus_free,
        kernel_ptr: std::ptr::null(),
    }
}

// ── Mock embedder that captures embed_input strings ─────────────
//
// The test's whole point is checking what the embedder RECEIVES —
// so use a mock that stashes every input into a shared Vec and
// returns fixed 4-dim vectors (matching the AnnIndex dim we pin
// via `.dim()`).

struct CapturingEmbedder {
    captured: parking_lot::Mutex<Vec<String>>,
}

impl CapturingEmbedder {
    fn new() -> Arc<Self> {
        Arc::new(Self {
            captured: parking_lot::Mutex::new(Vec::new()),
        })
    }

    fn take_captured(&self) -> Vec<String> {
        std::mem::take(&mut *self.captured.lock())
    }
}

impl Embedder for CapturingEmbedder {
    fn embed_batch(&self, texts: &[&str]) -> Result<Vec<Vec<f32>>, EmbedError> {
        let mut g = self.captured.lock();
        for t in texts {
            g.push((*t).to_string());
        }
        Ok(texts.iter().map(|_| vec![0.1_f32, 0.2, 0.3, 0.4]).collect())
    }

    fn dim(&self) -> usize {
        4
    }

    fn tag(&self) -> &str {
        "capturing-mock"
    }
}

// ── Mock context generator with a call counter ──────────────────

struct MockContextGenerator {
    prefix_pattern: String,
    calls: AtomicUsize,
    fail_indices: Vec<usize>,
}

impl MockContextGenerator {
    fn new(prefix_pattern: &str) -> Arc<Self> {
        Arc::new(Self {
            prefix_pattern: prefix_pattern.to_string(),
            calls: AtomicUsize::new(0),
            fail_indices: Vec::new(),
        })
    }

    fn with_failures(prefix_pattern: &str, fail_indices: Vec<usize>) -> Arc<Self> {
        Arc::new(Self {
            prefix_pattern: prefix_pattern.to_string(),
            calls: AtomicUsize::new(0),
            fail_indices,
        })
    }

    fn calls(&self) -> usize {
        self.calls.load(Ordering::SeqCst)
    }
}

impl ContextGenerator for MockContextGenerator {
    fn generate_batch(&self, _doc: &str, chunks: &[&str]) -> Vec<Option<String>> {
        self.calls.fetch_add(1, Ordering::SeqCst);
        chunks
            .iter()
            .enumerate()
            .map(|(i, _)| {
                if self.fail_indices.contains(&i) {
                    None
                } else {
                    Some(format!("{}{i}", self.prefix_pattern))
                }
            })
            .collect()
    }
}

// ── Harness ─────────────────────────────────────────────────────

struct Harness {
    _dir: TempDir,
    svc: SearchServiceImpl,
    embedder: Arc<CapturingEmbedder>,
    manager: Arc<IndexManager>,
    generator: Arc<MockContextGenerator>,
}

impl Harness {
    fn start(generator: Arc<MockContextGenerator>) -> Self {
        let dir = TempDir::new().expect("tempdir");
        let manager = Arc::new(IndexManager::with_root(dir.path().to_path_buf()));
        let embedder = CapturingEmbedder::new();
        let svc = SearchServiceImpl::builder(Arc::new(poison_handle()))
            .manager(Arc::clone(&manager))
            .embedder(embedder.clone() as Arc<dyn Embedder>)
            .context_generator(generator.clone() as Arc<dyn ContextGenerator>)
            .build();
        Self {
            _dir: dir,
            svc,
            embedder,
            manager,
            generator,
        }
    }

    fn start_without_generator() -> Self {
        let dir = TempDir::new().expect("tempdir");
        let manager = Arc::new(IndexManager::with_root(dir.path().to_path_buf()));
        let embedder = CapturingEmbedder::new();
        let svc = SearchServiceImpl::builder(Arc::new(poison_handle()))
            .manager(Arc::clone(&manager))
            .embedder(embedder.clone() as Arc<dyn Embedder>)
            .no_context_generator()
            .build();
        // Unused sentinel — no generator wired.
        let generator = MockContextGenerator::new("unused-");
        Self {
            _dir: dir,
            svc,
            embedder,
            manager,
            generator,
        }
    }

    async fn index_doc(&self, path: &str, text: &str) {
        let resp = self
            .svc
            .index_documents(Request::new(IndexDocumentsRequest {
                documents: vec![DocumentInput {
                    path: path.to_string(),
                    text: text.to_string(),
                    mtime_ms: Some(1_700_000_000_000),
                    zone_id: "root".to_string(),
                }],
                zone_id: "root".to_string(),
                auth_token: String::new(),
            }))
            .await
            .expect("index_documents")
            .into_inner();
        assert!(
            resp.error.is_none(),
            "index_documents error: {:?}",
            resp.error
        );
        assert!(resp.indexed_count >= 1, "expected at least 1 indexed");
    }
}

// ── Tests ───────────────────────────────────────────────────────

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn contextual_prefix_lands_on_embed_input_not_chunk_text() {
    // Index a single doc long enough to force multiple chunks — the
    // chunker's CHUNK_TARGET_CHARS is 1600, so 4 × 500-char sections
    // will typically produce ≥ 2 chunks.
    let text = format!(
        "# Section 1\n\n{}\n\n# Section 2\n\n{}\n\n# Section 3\n\n{}",
        "α ".repeat(400),
        "β ".repeat(400),
        "γ ".repeat(400),
    );

    let gen = MockContextGenerator::new("CTX-");
    let h = Harness::start(gen);
    h.index_doc("/notes/big.md", &text).await;

    // Every embedder input must start with our mock's "CTX-<i>\n\n"
    // prefix — proof that apply_contexts landed BEFORE embed_batch.
    let captured = h.embedder.take_captured();
    assert!(!captured.is_empty(), "no embed inputs captured");
    for input in &captured {
        assert!(
            input.starts_with("CTX-"),
            "embed_input missing context prefix: {input:?}",
        );
        assert!(
            input.contains("\n\n"),
            "embed_input missing prefix separator: {input:?}",
        );
    }
    assert!(h.generator.calls() >= 1, "generator was never called");

    // Now check the OTHER half of the contract: chunk_text (what
    // BM25 sees, what LocateResponse returns) must NOT carry the
    // prefix.  Query for the mock prefix — it should return ZERO
    // hits because it never entered the FTS index.
    let resp = h
        .svc
        .query(Request::new(QueryRequest {
            q: "CTX".to_string(),
            zone_id: "root".to_string(),
            limit: 10,
            query_type: QueryType::Keyword as i32,
            ..Default::default()
        }))
        .await
        .expect("query")
        .into_inner();
    assert!(
        resp.results.is_empty(),
        "context prefix leaked into FTS index: {:?}",
        resp.results.iter().map(|r| &r.path).collect::<Vec<_>>(),
    );

    // And Locate — confirms the doc actually landed with the
    // expected chunk count (indirectly proves BM25 side wasn't
    // corrupted by the context wrapper).
    let loc = h
        .svc
        .locate(Request::new(LocateRequest {
            path: "/notes/big.md".to_string(),
            zone_id: "root".to_string(),
            auth_token: String::new(),
        }))
        .await
        .expect("locate")
        .into_inner();
    assert!(loc.indexed, "locate reports doc missing from FTS");
    assert!(loc.chunk_count > 0, "locate reports zero chunks");
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn no_generator_wired_is_a_transparent_pass_through() {
    let text = format!("# Heading\n\n{}", "body content ".repeat(300));
    let h = Harness::start_without_generator();
    h.index_doc("/notes/nogen.md", &text).await;

    let captured = h.embedder.take_captured();
    assert!(!captured.is_empty(), "no embed inputs captured");
    // Without a generator, chunker::Chunk::embed_input is the plain
    // heading-prefixed text — no "CTX-" or any other mock prefix.
    for input in &captured {
        assert!(
            !input.starts_with("CTX-"),
            "generator ran despite .no_context_generator(): {input:?}",
        );
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn per_chunk_generator_failure_leaves_that_chunk_unprefixed() {
    // Long doc → multiple chunks.  Generator fails on chunk index 0
    // (returns None); every other chunk gets prefixed.
    let text = format!(
        "# Section A\n\n{}\n\n# Section B\n\n{}\n\n# Section C\n\n{}",
        "alpha ".repeat(400),
        "beta ".repeat(400),
        "gamma ".repeat(400),
    );

    let gen = MockContextGenerator::with_failures("OK-", vec![0]);
    let h = Harness::start(gen);
    h.index_doc("/notes/partial.md", &text).await;

    let captured = h.embedder.take_captured();
    assert!(
        captured.len() >= 2,
        "need at least 2 chunks; got {}",
        captured.len()
    );
    // Chunk 0 must NOT start with OK- (generator returned None).
    assert!(
        !captured[0].starts_with("OK-"),
        "chunk 0 was prefixed despite generator returning None: {:?}",
        captured[0],
    );
    // Chunk 1+ MUST start with OK-.
    for (i, input) in captured.iter().enumerate().skip(1) {
        assert!(
            input.starts_with("OK-"),
            "chunk {i} missing prefix: {input:?}",
        );
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn empty_document_never_calls_generator() {
    // Whitespace-only docs skip chunking entirely, so the generator
    // must not be called for them.
    let gen = MockContextGenerator::new("EMPTY-");
    let h = Harness::start(gen);
    // index_documents accepts empty text but records it as skipped;
    // the assertion inside `index_doc` on `indexed_count >= 1` would
    // trip, so open-code this test.
    let resp = h
        .svc
        .index_documents(Request::new(IndexDocumentsRequest {
            documents: vec![DocumentInput {
                path: "/empty.md".to_string(),
                text: "  \n\n  ".to_string(),
                mtime_ms: Some(1_700_000_000_000),
                zone_id: "root".to_string(),
            }],
            zone_id: "root".to_string(),
            auth_token: String::new(),
        }))
        .await
        .expect("index_documents")
        .into_inner();
    assert_eq!(resp.indexed_count, 0);
    assert_eq!(resp.skipped_count, 1);
    assert_eq!(h.generator.calls(), 0, "generator called for empty doc");
    // Sanity: no embed calls either.
    assert!(
        h.embedder.take_captured().is_empty(),
        "embedder called for empty doc",
    );
    // Manager touch: opening the zone is fine.
    let _idx = h.manager.get_or_open("root").expect("open");
}

// ── Walker + Refresh path coverage ──────────────────────────────
//
// index_documents (tested above) uses its OWN inline chunk-then-
// index loop.  do_index (walker) and do_refresh (incremental) both
// go through index_one(), which has its OWN apply_contexts call
// site.  Testing only index_documents leaves both walker paths
// uncovered — a regression that broke apply_contexts inside
// index_one would ship green.
//
// The MockKernel below is the same shape as index_e2e.rs's fixture
// (adapted here to keep this test file self-contained; a future PR
// can extract to tests/common/ if a fourth caller shows up).

struct MockFile {
    bytes: Vec<u8>,
    mtime_ms: i64,
}

pub struct MockKernel {
    files: HashMap<String, MockFile>,
    dirs: HashMap<String, Vec<(String, u8)>>,
}

impl MockKernel {
    fn new() -> Self {
        Self {
            files: HashMap::new(),
            dirs: HashMap::new(),
        }
    }

    fn add_file(&mut self, path: &str, bytes: &[u8], mtime_ms: i64) {
        self.files.insert(
            path.to_string(),
            MockFile {
                bytes: bytes.to_vec(),
                mtime_ms,
            },
        );
        let (parent, name) = split_parent(path);
        self.dirs
            .entry(parent)
            .or_default()
            .push((name, 0 /* DT_REG */));
    }
}

fn split_parent(path: &str) -> (String, String) {
    match path.rsplit_once('/') {
        Some(("", name)) => ("/".to_string(), name.to_string()),
        Some((parent, name)) => (parent.to_string(), name.to_string()),
        None => ("/".to_string(), path.to_string()),
    }
}

fn into_out_buf(mut v: Vec<u8>, out_buf: *mut *mut u8, out_len: *mut usize) {
    v.shrink_to_fit();
    let len = v.len();
    let ptr = v.as_mut_ptr();
    std::mem::forget(v);
    unsafe {
        *out_buf = ptr;
        *out_len = len;
    }
}

unsafe extern "C" fn mock_sys_read(
    k: *const c_void,
    path: *const c_char,
    out_buf: *mut *mut u8,
    out_len: *mut usize,
) -> i32 {
    let kernel = &*(k as *const MockKernel);
    let p = match CStr::from_ptr(path).to_str() {
        Ok(s) => s,
        Err(_) => return -2,
    };
    match kernel.files.get(p) {
        Some(f) => {
            into_out_buf(f.bytes.clone(), out_buf, out_len);
            0
        }
        None => -404,
    }
}

unsafe extern "C" fn mock_sys_readdir(
    k: *const c_void,
    parent: *const c_char,
    out_json: *mut *mut u8,
    out_len: *mut usize,
) -> i32 {
    let kernel = &*(k as *const MockKernel);
    let p = match CStr::from_ptr(parent).to_str() {
        Ok(s) => s,
        Err(_) => return -2,
    };
    match kernel.dirs.get(p) {
        Some(entries) => {
            let payload: Vec<serde_json::Value> = entries
                .iter()
                .map(|(n, et)| serde_json::json!({ "name": n, "entry_type": et }))
                .collect();
            let json = serde_json::to_vec(&payload).expect("mock readdir json");
            into_out_buf(json, out_json, out_len);
            0
        }
        None => -404,
    }
}

unsafe extern "C" fn mock_sys_stat(
    k: *const c_void,
    path: *const c_char,
    out_json: *mut *mut u8,
    out_len: *mut usize,
) -> i32 {
    let kernel = &*(k as *const MockKernel);
    let p = match CStr::from_ptr(path).to_str() {
        Ok(s) => s,
        Err(_) => return -2,
    };
    if let Some(f) = kernel.files.get(p) {
        let payload = serde_json::json!({
            "path": p,
            "entry_type": 0,
            "size": f.bytes.len(),
            "zone_id": "root",
            "modified_at_ms": f.mtime_ms,
        });
        into_out_buf(serde_json::to_vec(&payload).unwrap(), out_json, out_len);
        0
    } else if kernel.dirs.contains_key(p) {
        let payload = serde_json::json!({
            "path": p,
            "entry_type": 1,
            "size": 0,
            "zone_id": "root",
            "modified_at_ms": null,
        });
        into_out_buf(serde_json::to_vec(&payload).unwrap(), out_json, out_len);
        0
    } else {
        -404
    }
}

unsafe extern "C" fn unused_write(
    _: *const c_void,
    _: *const c_char,
    _: *const u8,
    _: usize,
) -> i32 {
    -1
}
unsafe extern "C" fn unused_unlink(_: *const c_void, _: *const c_char) -> i32 {
    -1
}
unsafe extern "C" fn unused_mkdir(_: *const c_void, _: *const c_char) -> i32 {
    -1
}
unsafe extern "C" fn unused_rmdir(_: *const c_void, _: *const c_char) -> i32 {
    -1
}
unsafe extern "C" fn unused_rename(_: *const c_void, _: *const c_char, _: *const c_char) -> i32 {
    -1
}
unsafe extern "C" fn unused_stat_batch(
    _: *const c_void,
    _: *const c_char,
    _: *mut *mut u8,
    _: *mut usize,
) -> i32 {
    -1
}

fn handle_for(k: *const MockKernel) -> KernelHandle {
    KernelHandle {
        sys_read: mock_sys_read,
        sys_write: unused_write,
        sys_stat: mock_sys_stat,
        sys_readdir: mock_sys_readdir,
        sys_unlink: unused_unlink,
        sys_mkdir: unused_mkdir,
        sys_rmdir: unused_rmdir,
        sys_rename: unused_rename,
        sys_stat_batch: unused_stat_batch,
        free_buf: nexus_plugin_abi::nexus_free,
        kernel_ptr: k as *const c_void,
    }
}

/// Harness that wraps the mock kernel + service so both index and
/// refresh tests share one setup shape.
struct WalkerHarness {
    _dir: TempDir,
    mock: *mut MockKernel,
    svc: SearchServiceImpl,
    embedder: Arc<CapturingEmbedder>,
    generator: Arc<MockContextGenerator>,
}

impl WalkerHarness {
    fn start(text_at: &[(&str, &[u8])]) -> Self {
        let dir = TempDir::new().expect("tempdir");
        let mut mock = MockKernel::new();
        for (i, (path, bytes)) in text_at.iter().enumerate() {
            mock.add_file(path, bytes, 1_700_000_000_000 + i as i64 * 100);
        }
        let mock = Box::into_raw(Box::new(mock));
        let handle = handle_for(mock);
        let manager = Arc::new(IndexManager::with_root(dir.path().to_path_buf()));
        let embedder = CapturingEmbedder::new();
        let generator = MockContextGenerator::new("W-");
        let svc = SearchServiceImpl::builder(Arc::new(handle))
            .manager(Arc::clone(&manager))
            .embedder(embedder.clone() as Arc<dyn Embedder>)
            .context_generator(generator.clone() as Arc<dyn ContextGenerator>)
            .build();
        Self {
            _dir: dir,
            mock,
            svc,
            embedder,
            generator,
        }
    }
}

impl Drop for WalkerHarness {
    fn drop(&mut self) {
        // Safety: we own the MockKernel via Box::into_raw in start().
        unsafe { drop(Box::from_raw(self.mock)) };
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn contextual_prefix_applied_by_index_walker() {
    // Feed the mock kernel two files; drive the Index RPC (which
    // routes through do_index → walk_recursive → index_one), then
    // assert the mock generator was called and the embedder saw
    // prefixed inputs.  Covers the do_index insertion point.
    let h = WalkerHarness::start(&[
        (
            "/notes/a.md",
            b"# heading A\n\nbody of file A that is long enough to chunk",
        ),
        (
            "/notes/b.md",
            b"# heading B\n\nbody of file B, also non-trivial",
        ),
    ]);

    let resp = h
        .svc
        .index(Request::new(IndexRequest {
            root_path: "/notes".into(),
            zone_id: "root".into(),
            recursive: true,
            max_docs: 100,
            auth_token: String::new(),
        }))
        .await
        .expect("index")
        .into_inner();
    assert!(resp.error.is_none(), "index errored: {:?}", resp.error);
    assert!(
        resp.indexed_count >= 2,
        "expected ≥2 docs indexed, got {}",
        resp.indexed_count
    );

    assert!(
        h.generator.calls() >= 1,
        "generator was NEVER called by the Index walker path — apply_contexts is not wired",
    );
    let captured = h.embedder.take_captured();
    assert!(!captured.is_empty(), "no embed inputs captured");
    assert!(
        captured.iter().all(|s| s.starts_with("W-")),
        "walker path failed to prepend context to embed_input: {captured:?}",
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn contextual_prefix_applied_by_refresh_walker() {
    // Same as above but driving the Refresh RPC (do_refresh →
    // index_one path).  A regression that broke apply_contexts
    // inside index_one would trip this in addition to the Index
    // test — belt and suspenders since do_refresh's IndexSinks
    // constructor is a distinct call site from do_index's.
    let h = WalkerHarness::start(&[(
        "/logs/x.log",
        b"# section\n\nlogline logline logline logline logline",
    )]);

    let resp = h
        .svc
        .refresh(Request::new(RefreshRequest {
            root_path: "/logs".into(),
            zone_id: "root".into(),
            recursive: true,
            max_docs: 100,
            auth_token: String::new(),
        }))
        .await
        .expect("refresh")
        .into_inner();
    assert!(resp.error.is_none(), "refresh errored: {:?}", resp.error);
    assert!(
        resp.reindexed_count >= 1,
        "expected ≥1 doc reindexed by refresh, got reindexed={}",
        resp.reindexed_count,
    );

    assert!(
        h.generator.calls() >= 1,
        "generator was NEVER called by the Refresh walker path — apply_contexts is not wired",
    );
    let captured = h.embedder.take_captured();
    assert!(!captured.is_empty(), "no embed inputs captured by refresh");
    assert!(
        captured.iter().all(|s| s.starts_with("W-")),
        "refresh path failed to prepend context to embed_input: {captured:?}",
    );
}
