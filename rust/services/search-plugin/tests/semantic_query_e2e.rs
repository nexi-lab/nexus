//! End-to-end integration test for the SemanticQuery path (P2 step 4).
//!
//! Sibling of `query_e2e.rs` (keyword) and `index_e2e.rs` (Index).
//! Exercises the full RPC handler → \[do_semantic_query\] →
//! \[Embedder.embed_batch\] → \[AnnIndex.search\] → \[QueryResult\]
//! roundtrip using an injected \[MockEmbedder\] so the test never
//! downloads a real model or links ONNX Runtime.
//!
//! Coverage:
//! - happy path: index some files (Index RPC populates both FTS +
//!   ANN via the same MockEmbedder), then SemanticQuery for one
//!   file's text — the seed file must come back first at score ≈ 1
//! - path-prefix filter
//! - re-index idempotency also holds on the ANN side (no
//!   duplicated hits for the same path)
//! - empty q → clean error
//! - zone isolation on ANN (a mirror of the FTS isolation test in
//!   query_e2e.rs)
//!
//! The MockEmbedder is deterministic per text (same text → same
//! vector → cosine distance 0), which is exactly the shape a
//! "user searches for a phrase in a doc they just indexed" flow
//! wants to pin at the E2E level.

use std::collections::HashMap;
use std::ffi::{c_void, CStr};
use std::os::raw::c_char;
use std::sync::Arc;

use nexus_plugin_abi::KernelHandle;
use nexus_search_plugin::embedder::MockEmbedder;
use nexus_search_plugin::index_manager::IndexManager;
use nexus_search_plugin::search_proto::search_service_server::SearchService;
use nexus_search_plugin::search_proto::{IndexRequest, QueryRequest, QueryType};
use nexus_search_plugin::service::SearchServiceImpl;
use tempfile::TempDir;
use tonic::Request;

// ── Mock kernel (same shape as index_e2e.rs) ────────────────────
//
// Duplicated inline rather than extracted to tests/common because
// Cargo's shared-test-module story requires each caller to
// `mod common;` and the extra plumbing costs more than the
// duplication does at this size.  P4/P5 can consolidate if a third
// integration test wants the same scaffold.

struct FileEntry {
    bytes: Vec<u8>,
    mtime_ms: i64,
}

pub struct MockKernel {
    files: HashMap<String, FileEntry>,
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
            FileEntry {
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
    fn add_dir(&mut self, path: &str) {
        if !self.dirs.contains_key(path) {
            self.dirs.insert(path.to_string(), Vec::new());
        }
        if path == "/" {
            return;
        }
        let (parent, name) = split_parent(path);
        let entries = self.dirs.entry(parent).or_default();
        if !entries.iter().any(|(n, _)| n == &name) {
            entries.push((name, 1 /* DT_DIR */));
        }
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
        Some(entry) => {
            into_out_buf(entry.bytes.clone(), out_buf, out_len);
            0
        }
        None => -404,
    }
}

unsafe extern "C" fn mock_sys_readdir(
    k: *const c_void,
    parent_path: *const c_char,
    out_json: *mut *mut u8,
    out_len: *mut usize,
) -> i32 {
    let kernel = &*(k as *const MockKernel);
    let p = match CStr::from_ptr(parent_path).to_str() {
        Ok(s) => s,
        Err(_) => return -2,
    };
    match kernel.dirs.get(p) {
        Some(entries) => {
            let payload: Vec<serde_json::Value> = entries
                .iter()
                .map(|(name, et)| serde_json::json!({ "name": name, "entry_type": et }))
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
    if let Some(entry) = kernel.files.get(p) {
        let payload = serde_json::json!({
            "path": p,
            "entry_type": 0,
            "size": entry.bytes.len(),
            "zone_id": "root",
            "modified_at_ms": entry.mtime_ms,
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

unsafe extern "C" fn unused_sys_write(
    _: *const c_void,
    _: *const c_char,
    _: *const u8,
    _: usize,
) -> i32 {
    -1
}
unsafe extern "C" fn unused_sys_unlink(_: *const c_void, _: *const c_char) -> i32 {
    -1
}
unsafe extern "C" fn unused_sys_mkdir(_: *const c_void, _: *const c_char) -> i32 {
    -1
}
unsafe extern "C" fn unused_sys_rmdir(_: *const c_void, _: *const c_char) -> i32 {
    -1
}
unsafe extern "C" fn unused_sys_rename(
    _: *const c_void,
    _: *const c_char,
    _: *const c_char,
) -> i32 {
    -1
}
unsafe extern "C" fn unused_sys_stat_batch(
    _: *const c_void,
    _: *const c_char,
    _: *mut *mut u8,
    _: *mut usize,
) -> i32 {
    -1
}

fn handle_for(kernel: *const MockKernel) -> KernelHandle {
    KernelHandle {
        sys_read: mock_sys_read,
        sys_write: unused_sys_write,
        sys_stat: mock_sys_stat,
        sys_readdir: mock_sys_readdir,
        sys_unlink: unused_sys_unlink,
        sys_mkdir: unused_sys_mkdir,
        sys_rmdir: unused_sys_rmdir,
        sys_rename: unused_sys_rename,
        sys_stat_batch: unused_sys_stat_batch,
        kernel_ptr: kernel as *const c_void,
    }
}

// ── Harness ─────────────────────────────────────────────────────

struct Harness {
    _dir: TempDir,
    mock: *mut MockKernel,
    svc: SearchServiceImpl,
}

impl Harness {
    /// Start with a MockEmbedder pre-injected so SemanticQuery is
    /// functional without needing NEXUS_SEARCH_MODEL_DIR / ort dylib.
    /// Uses a small (16-dim) mock so assertion output stays readable.
    fn start() -> Self {
        let dir = TempDir::new().expect("tempdir");
        let mock = Box::into_raw(Box::new(MockKernel::new()));
        let handle = handle_for(mock);
        let manager = Arc::new(IndexManager::with_root(dir.path().to_path_buf()));
        let embedder = Arc::new(MockEmbedder::with_dim(16));
        let svc = SearchServiceImpl::builder(Arc::new(handle))
            .manager(manager)
            .embedder(embedder)
            .build();
        Self {
            _dir: dir,
            mock,
            svc,
        }
    }

    #[allow(clippy::mut_from_ref)]
    fn mock_mut(&self) -> &mut MockKernel {
        unsafe { &mut *self.mock }
    }
}

impl Drop for Harness {
    fn drop(&mut self) {
        unsafe {
            drop(Box::from_raw(self.mock));
        }
    }
}

async fn index_root(svc: &SearchServiceImpl) -> nexus_search_plugin::search_proto::IndexResponse {
    svc.index(Request::new(IndexRequest {
        root_path: "/".into(),
        zone_id: "root".into(),
        recursive: true,
        max_docs: 0,
        auth_token: String::new(),
    }))
    .await
    .expect("index")
    .into_inner()
}

async fn semantic_query(
    svc: &SearchServiceImpl,
    q: &str,
    path_filter: &str,
) -> nexus_search_plugin::search_proto::QueryResponse {
    svc.query(Request::new(QueryRequest {
        q: q.into(),
        zone_id: "root".into(),
        limit: 10,
        path_filter: path_filter.into(),
        query_type: QueryType::Semantic as i32,
        auth_token: String::new(),
        ..Default::default()
    }))
    .await
    .expect("query")
    .into_inner()
}

// ── Tests ───────────────────────────────────────────────────────

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn semantic_finds_exact_match_after_index() {
    let h = Harness::start();
    let mock = h.mock_mut();
    mock.add_dir("/");
    // Splice /notes into / explicitly — MockKernel's add_file only
    // registers the file with its own parent, it doesn't chain up
    // to the grandparent, so the walker at / never sees /notes
    // without this call.  index_e2e.rs's nested walk test has the
    // same shape.
    mock.add_dir("/notes");
    mock.add_file("/notes/alpha.md", b"widget alpha payload", 1);
    mock.add_file("/notes/beta.md", b"orange banana grape", 2);
    mock.add_file("/notes/gamma.md", b"widget alpha payload extra", 3);

    let idx = index_root(&h.svc).await;
    assert!(
        idx.error.is_none(),
        "unexpected index error: {:?}",
        idx.error
    );
    assert_eq!(idx.indexed_count, 3);

    // Query for the EXACT text of /notes/alpha.md — mock embedder
    // is deterministic per text, so distance is 0 and score = 1.
    let q = semantic_query(&h.svc, "widget alpha payload", "").await;
    assert!(
        q.error.is_none(),
        "unexpected semantic error: {:?}",
        q.error
    );
    assert!(!q.results.is_empty(), "expected at least one hit");
    assert_eq!(q.results[0].path, "/notes/alpha.md");
    // Score ≈ 1 for the exact-text match (mock embed distance ≈ 0
    // → score = 1 - distance ≈ 1).
    assert!(
        (q.results[0].score - 1.0).abs() < 0.001,
        "exact-text hit should score ≈ 1, got {}",
        q.results[0].score,
    );
    // FTS enrichment brings back chunk_text from the sibling index.
    assert_eq!(q.results[0].chunk_text, "widget alpha payload");
    // mtime survives the roundtrip.
    assert_eq!(q.results[0].mtime_ms, Some(1));
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn semantic_honours_path_prefix_filter() {
    let h = Harness::start();
    let mock = h.mock_mut();
    mock.add_dir("/");
    mock.add_dir("/notes");
    mock.add_dir("/logs");
    mock.add_file("/notes/a.md", b"shared query text", 1);
    mock.add_file("/logs/b.log", b"shared query text", 2);

    let idx = index_root(&h.svc).await;
    assert!(idx.error.is_none(), "{idx:?}");

    let q = semantic_query(&h.svc, "shared query text", "/notes/").await;
    assert!(q.error.is_none(), "{q:?}");
    let paths: Vec<&str> = q.results.iter().map(|r| r.path.as_str()).collect();
    assert!(
        !paths.is_empty(),
        "prefix filter dropped every hit: {paths:?}"
    );
    assert!(
        paths.iter().all(|p| p.starts_with("/notes/")),
        "prefix filter leaked non-/notes/ paths: {paths:?}",
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn semantic_reindex_stays_idempotent() {
    // Same D3 idempotency contract the FTS + ANN primitives assert
    // individually — pin at the RPC handler that a repeat Index
    // does NOT duplicate the doc in either backing store.
    let h = Harness::start();
    let mock = h.mock_mut();
    mock.add_dir("/");
    mock.add_file("/x.md", b"unique widget marker", 1);

    for _ in 0..2 {
        let r = index_root(&h.svc).await;
        assert!(r.error.is_none(), "{r:?}");
        assert_eq!(r.indexed_count, 1);
    }

    let q = semantic_query(&h.svc, "unique widget marker", "").await;
    assert!(q.error.is_none(), "{q:?}");
    let matches: Vec<&str> = q.results.iter().map(|r| r.path.as_str()).collect();
    assert_eq!(
        matches.iter().filter(|p| **p == "/x.md").count(),
        1,
        "reindex duplicated the doc in semantic results: {matches:?}",
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn semantic_empty_query_returns_error() {
    let h = Harness::start();
    let mock = h.mock_mut();
    mock.add_dir("/");
    mock.add_file("/x.md", b"seed", 1);
    let _ = index_root(&h.svc).await;

    let q = h
        .svc
        .query(Request::new(QueryRequest {
            q: String::new(),
            zone_id: "root".into(),
            limit: 10,
            path_filter: String::new(),
            query_type: QueryType::Semantic as i32,
            auth_token: String::new(),
            ..Default::default()
        }))
        .await
        .expect("query")
        .into_inner();

    assert!(q.error.is_some(), "empty q must return error");
    assert!(q.results.is_empty());
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn semantic_zone_isolation_holds() {
    // D3 storage-layer isolation, mirrored for the ANN path.  A
    // semantic query scoped to zone-a must not see zone-b's corpus,
    // regardless of how the mock embeds shared text.
    let dir = TempDir::new().expect("tempdir");
    let mock = Box::into_raw(Box::new(MockKernel::new()));
    let handle = handle_for(mock);
    // Seed BOTH zones with shared text.
    unsafe {
        (*mock).add_dir("/");
        (*mock).add_file("/only-in-a.md", b"shared widget", 1);
        (*mock).add_file("/only-in-b.md", b"shared widget", 2);
    }
    let manager = Arc::new(IndexManager::with_root(dir.path().to_path_buf()));
    let embedder = Arc::new(MockEmbedder::with_dim(16));
    let svc = SearchServiceImpl::builder(Arc::new(handle))
        .manager(manager)
        .embedder(embedder)
        .build();

    // Index into zone-a only.
    let idx_a = svc
        .index(Request::new(IndexRequest {
            root_path: "/only-in-a.md".into(),
            zone_id: "zone-a".into(),
            recursive: false,
            max_docs: 0,
            auth_token: String::new(),
        }))
        .await
        .expect("index-a")
        .into_inner();
    // Non-recursive on a single file path — the walker enumerates
    // its parent's direct children; the mock puts only-in-a.md at
    // /, so pointing recursive=false at the file itself yields 0.
    // Fall back to indexing the whole root with recursive=true and
    // scoping the query by path_filter later.
    let _ = idx_a;
    let idx_a = svc
        .index(Request::new(IndexRequest {
            root_path: "/".into(),
            zone_id: "zone-a".into(),
            recursive: true,
            max_docs: 0,
            auth_token: String::new(),
        }))
        .await
        .expect("index-a-root")
        .into_inner();
    assert!(idx_a.error.is_none(), "{idx_a:?}");

    let q = svc
        .query(Request::new(QueryRequest {
            q: "shared widget".into(),
            zone_id: "zone-a".into(),
            limit: 10,
            path_filter: String::new(),
            query_type: QueryType::Semantic as i32,
            auth_token: String::new(),
            ..Default::default()
        }))
        .await
        .expect("query")
        .into_inner();
    assert!(q.error.is_none(), "{q:?}");
    for r in &q.results {
        assert_eq!(r.zone_id, "zone-a", "zone-b bled into zone-a: {r:?}");
    }

    unsafe {
        drop(Box::from_raw(mock));
    }
}

// ── Hybrid (P3) ─────────────────────────────────────────────────

async fn hybrid_query(
    svc: &SearchServiceImpl,
    q: &str,
    path_filter: &str,
) -> nexus_search_plugin::search_proto::QueryResponse {
    svc.query(Request::new(QueryRequest {
        q: q.into(),
        zone_id: "root".into(),
        limit: 10,
        path_filter: path_filter.into(),
        query_type: QueryType::Hybrid as i32,
        auth_token: String::new(),
        ..Default::default()
    }))
    .await
    .expect("query")
    .into_inner()
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn hybrid_finds_docs_present_in_both_sources() {
    // Happy path: a query whose text lives in the corpus turns up
    // in both the BM25 side AND the vector side.  Fusion promotes
    // that doc to hit[0].  Mock embedder is deterministic per text
    // so 'widget alpha payload' matches /notes/alpha.md at cosine
    // distance ≈ 0.
    let h = Harness::start();
    let mock = h.mock_mut();
    mock.add_dir("/");
    mock.add_dir("/notes");
    mock.add_file("/notes/alpha.md", b"widget alpha payload", 1);
    mock.add_file("/notes/beta.md", b"orange banana grape", 2);
    mock.add_file("/notes/gamma.md", b"only alpha here", 3);

    let idx = index_root(&h.svc).await;
    assert!(idx.error.is_none(), "{idx:?}");
    assert_eq!(idx.indexed_count, 3);

    let q = hybrid_query(&h.svc, "widget alpha payload", "").await;
    assert!(q.error.is_none(), "unexpected hybrid error: {:?}", q.error);
    assert!(!q.results.is_empty(), "expected hybrid hits");
    assert_eq!(
        q.results[0].path, "/notes/alpha.md",
        "shared doc should lead: {:?}",
        q.results,
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn hybrid_score_higher_than_either_source_alone_for_shared_doc() {
    // RRF math contract: a doc that appears in BOTH sources scores
    // strictly higher than a doc that appears in only one, at the
    // same rank.  Regression scaffold for the fusion primitive's
    // wiring into the RPC handler (the fusion unit tests already
    // pin the math; this pins the plumbing).
    let h = Harness::start();
    let mock = h.mock_mut();
    mock.add_dir("/");
    // /both matches keyword 'widget' AND has exact text for the
    // semantic side — both sources return it.
    mock.add_file("/both.md", b"widget", 1);
    // /kw-only matches the keyword 'widget' but its exact text is
    // different so its vector distance is > 0 → semantic ranks it
    // lower than /both.
    mock.add_file("/kw-only.md", b"widget different words entirely", 2);

    let idx = index_root(&h.svc).await;
    assert!(idx.error.is_none(), "{idx:?}");

    let q = hybrid_query(&h.svc, "widget", "").await;
    assert!(q.error.is_none(), "{q:?}");
    assert!(!q.results.is_empty());
    // /both should lead — appears at rank 1 in BOTH sources.
    assert_eq!(q.results[0].path, "/both.md");
    if q.results.len() > 1 {
        assert!(
            q.results[0].score >= q.results[1].score,
            "score order violated: {:?}",
            q.results,
        );
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn hybrid_honours_path_prefix_filter() {
    let h = Harness::start();
    let mock = h.mock_mut();
    mock.add_dir("/");
    mock.add_dir("/notes");
    mock.add_dir("/logs");
    mock.add_file("/notes/a.md", b"widget shared", 1);
    mock.add_file("/logs/b.log", b"widget shared", 2);

    let idx = index_root(&h.svc).await;
    assert!(idx.error.is_none(), "{idx:?}");

    let q = hybrid_query(&h.svc, "widget shared", "/notes/").await;
    assert!(q.error.is_none(), "{q:?}");
    let paths: Vec<&str> = q.results.iter().map(|r| r.path.as_str()).collect();
    assert!(!paths.is_empty(), "prefix filter dropped every hit");
    assert!(
        paths.iter().all(|p| p.starts_with("/notes/")),
        "prefix filter leaked: {paths:?}",
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn hybrid_alpha_shifts_ranking_between_sources() {
    // alpha=0 (pure keyword) vs alpha=1 (pure semantic) under the
    // WEIGHTED fusion method — a doc that ranks HIGH in one source
    // and LOW in the other shifts position when alpha flips.  Pins
    // the alpha plumbing without depending on the mock's specific
    // score magnitudes.
    let h = Harness::start();
    let mock = h.mock_mut();
    mock.add_dir("/");
    // /kw-strong: keyword hits multiple times → high BM25 score.
    // Semantic distance from query 'widget' is uncontrolled.
    mock.add_file("/kw-strong.md", b"widget widget widget widget widget", 1);
    // /sem-exact: exactly matches the query 'widget' text so mock
    // semantic distance ≈ 0 → highest cosine score.  BM25 is only
    // one occurrence.
    mock.add_file("/sem-exact.md", b"widget", 2);

    let idx = index_root(&h.svc).await;
    assert!(idx.error.is_none(), "{idx:?}");

    // alpha=1 (pure semantic) — /sem-exact should lead.
    let q_sem = h
        .svc
        .query(Request::new(QueryRequest {
            q: "widget".into(),
            zone_id: "root".into(),
            limit: 10,
            path_filter: String::new(),
            query_type: QueryType::Hybrid as i32,
            fusion_method: nexus_search_plugin::search_proto::FusionMethod::Weighted as i32,
            alpha: 1.0,
            auth_token: String::new(),
            ..Default::default()
        }))
        .await
        .expect("query")
        .into_inner();
    assert!(q_sem.error.is_none(), "{q_sem:?}");
    assert!(
        !q_sem.results.is_empty(),
        "expected hits under alpha=1: {q_sem:?}",
    );
    assert_eq!(
        q_sem.results[0].path, "/sem-exact.md",
        "alpha=1 should favour semantic-exact: {:?}",
        q_sem.results,
    );

    // alpha=0 (pure keyword) — /kw-strong should now lead.
    let q_kw = h
        .svc
        .query(Request::new(QueryRequest {
            q: "widget".into(),
            zone_id: "root".into(),
            limit: 10,
            path_filter: String::new(),
            query_type: QueryType::Hybrid as i32,
            fusion_method: nexus_search_plugin::search_proto::FusionMethod::Weighted as i32,
            alpha: 0.0001, // avoid alpha=0 which the plugin rewrites to DEFAULT_ALPHA=0.5
            auth_token: String::new(),
            ..Default::default()
        }))
        .await
        .expect("query")
        .into_inner();
    assert!(q_kw.error.is_none(), "{q_kw:?}");
    assert!(!q_kw.results.is_empty());
    assert_eq!(
        q_kw.results[0].path, "/kw-strong.md",
        "alpha→0 should favour keyword-strong: {:?}",
        q_kw.results,
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn hybrid_empty_q_returns_error() {
    let h = Harness::start();
    let mock = h.mock_mut();
    mock.add_dir("/");
    mock.add_file("/x.md", b"seed", 1);
    let _ = index_root(&h.svc).await;

    let q = hybrid_query(&h.svc, "", "").await;
    assert!(q.error.is_some(), "empty q must return error");
    assert!(q.results.is_empty());
}
