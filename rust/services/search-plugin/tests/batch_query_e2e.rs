//! End-to-end integration tests for the BatchQuery path (#4610).
//!
//! Sibling of `query_e2e.rs` / `semantic_query_e2e.rs` / `index_e2e.rs`.
//! Pins the two #4610 behaviours:
//!
//! - a batch repeating the SAME query text across different
//!   `path_filter`s embeds that text exactly once (pre-warm +
//!   query-embedding cache), instead of once per query — the Koodle
//!   cross-workspace fan-out shape (DeepBuildAI/koodle#2176);
//! - the batch dispatches its queries concurrently (bounded), not
//!   serially, while preserving response order and per-query error
//!   isolation exactly as the serial loop did.
//!
//! Scaffold duplicated from `semantic_query_e2e.rs` per that file's
//! note (Cargo shared-test-module plumbing costs more than the
//! duplication at this size).

use std::collections::HashMap;
use std::ffi::{c_void, CStr};
use std::os::raw::c_char;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use nexus_plugin_abi::KernelHandle;
use nexus_search_plugin::embedder::{EmbedError, Embedder, MockEmbedder};
use nexus_search_plugin::index_manager::IndexManager;
use nexus_search_plugin::search_proto::search_service_server::SearchService;
use nexus_search_plugin::search_proto::{BatchQueryRequest, IndexRequest, QueryRequest, QueryType};
use nexus_search_plugin::service::SearchServiceImpl;
use tempfile::TempDir;
use tonic::Request;

// ── Mock kernel (same shape as semantic_query_e2e.rs) ───────────

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

// ── Instrumented embedders ──────────────────────────────────────

/// MockEmbedder wrapper counting embed_batch calls, so tests can
/// assert how many embeds a batch actually performed.
struct CountingEmbedder {
    inner: MockEmbedder,
    calls: AtomicUsize,
}

impl CountingEmbedder {
    fn new() -> Self {
        Self {
            inner: MockEmbedder::with_dim(16),
            calls: AtomicUsize::new(0),
        }
    }
}

impl Embedder for CountingEmbedder {
    fn embed_batch(&self, texts: &[&str]) -> Result<Vec<Vec<f32>>, EmbedError> {
        self.calls.fetch_add(1, Ordering::SeqCst);
        self.inner.embed_batch(texts)
    }
    fn dim(&self) -> usize {
        self.inner.dim()
    }
    fn tag(&self) -> &str {
        "mock"
    }
}

/// MockEmbedder wrapper that records the PEAK number of concurrent
/// embed_batch calls.  Each call parks (bounded spin) until it has
/// observed a sibling in flight or a 2s deadline passes, so genuine
/// concurrency is observed deterministically: a serial BatchQuery
/// can never reach `max_in_flight >= 2`, it just times each call out.
/// Runs on the blocking pool, so sleeping in place is safe.
struct RendezvousEmbedder {
    inner: MockEmbedder,
    in_flight: AtomicUsize,
    max_in_flight: AtomicUsize,
}

impl RendezvousEmbedder {
    fn new() -> Self {
        Self {
            inner: MockEmbedder::with_dim(16),
            in_flight: AtomicUsize::new(0),
            max_in_flight: AtomicUsize::new(0),
        }
    }
}

impl Embedder for RendezvousEmbedder {
    fn embed_batch(&self, texts: &[&str]) -> Result<Vec<Vec<f32>>, EmbedError> {
        let cur = self.in_flight.fetch_add(1, Ordering::SeqCst) + 1;
        self.max_in_flight.fetch_max(cur, Ordering::SeqCst);
        let deadline = Instant::now() + Duration::from_secs(2);
        while self.in_flight.load(Ordering::SeqCst) < 2 && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(1));
        }
        let cur = self.in_flight.load(Ordering::SeqCst);
        self.max_in_flight.fetch_max(cur, Ordering::SeqCst);
        let out = self.inner.embed_batch(texts);
        self.in_flight.fetch_sub(1, Ordering::SeqCst);
        out
    }
    fn dim(&self) -> usize {
        self.inner.dim()
    }
    fn tag(&self) -> &str {
        "mock"
    }
}

// ── Harness ─────────────────────────────────────────────────────

struct Harness<E: Embedder + 'static> {
    _dir: TempDir,
    mock: *mut MockKernel,
    svc: SearchServiceImpl,
    embedder: Arc<E>,
}

impl<E: Embedder + 'static> Harness<E> {
    fn start(embedder: E) -> Self {
        let dir = TempDir::new().expect("tempdir");
        let mock = Box::into_raw(Box::new(MockKernel::new()));
        let handle = handle_for(mock);
        let manager = Arc::new(IndexManager::with_root(dir.path().to_path_buf()));
        let embedder = Arc::new(embedder);
        let svc = SearchServiceImpl::builder(Arc::new(handle))
            .manager(manager)
            .embedder(Arc::clone(&embedder) as Arc<dyn Embedder>)
            .build();
        Self {
            _dir: dir,
            mock,
            svc,
            embedder,
        }
    }

    fn mock_mut(&self) -> &mut MockKernel {
        unsafe { &mut *self.mock }
    }
}

impl<E: Embedder + 'static> Drop for Harness<E> {
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

fn query_req(q: &str, query_type: QueryType, path_filter: &str) -> QueryRequest {
    QueryRequest {
        q: q.into(),
        zone_id: "root".into(),
        limit: 10,
        path_filter: path_filter.into(),
        query_type: query_type as i32,
        auth_token: String::new(),
        ..Default::default()
    }
}

// ── Tests ───────────────────────────────────────────────────────

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn batch_same_q_across_path_filters_embeds_once() {
    // The koodle#2176 fan-out shape: SAME query text, one semantic
    // query per path prefix.  The dupe pre-warm must collapse all
    // three embeds into one, and every sub-response must still
    // honour its own path filter.
    let h = Harness::start(CountingEmbedder::new());
    let mock = h.mock_mut();
    mock.add_dir("/");
    mock.add_dir("/a");
    mock.add_dir("/b");
    mock.add_dir("/c");
    mock.add_file("/a/doc.md", b"widget alpha payload", 1);
    mock.add_file("/b/doc.md", b"widget alpha payload", 2);
    mock.add_file("/c/doc.md", b"orange banana grape", 3);

    let idx = index_root(&h.svc).await;
    assert!(idx.error.is_none(), "{idx:?}");
    assert_eq!(idx.indexed_count, 3);

    // Indexing embeds documents through the same embedder — count
    // only what the batch adds on top.
    let baseline = h.embedder.calls.load(Ordering::SeqCst);

    let resp = h
        .svc
        .batch_query(Request::new(BatchQueryRequest {
            auth_token: String::new(),
            queries: vec![
                query_req("widget alpha payload", QueryType::Semantic, "/a/"),
                query_req("widget alpha payload", QueryType::Semantic, "/b/"),
                query_req("widget alpha payload", QueryType::Semantic, "/c/"),
            ],
        }))
        .await
        .expect("batch_query")
        .into_inner();

    assert_eq!(resp.responses.len(), 3);
    for (i, sub) in resp.responses.iter().enumerate() {
        assert!(sub.error.is_none(), "sub {i} errored: {sub:?}");
    }
    // Order preservation + per-query path filters: sub 0 sees only
    // /a/, sub 1 only /b/, sub 2 only /c/.
    for (i, prefix) in ["/a/", "/b/", "/c/"].iter().enumerate() {
        for r in &resp.responses[i].results {
            assert!(
                r.path.starts_with(prefix),
                "sub {i} leaked path {} outside {prefix}",
                r.path,
            );
        }
    }
    assert!(
        !resp.responses[0].results.is_empty(),
        "expected /a/ hits: {:?}",
        resp.responses[0],
    );

    let batch_embeds = h.embedder.calls.load(Ordering::SeqCst) - baseline;
    assert_eq!(
        batch_embeds, 1,
        "identical query text across path filters must embed exactly once",
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn batch_preserves_order_and_isolates_errors() {
    let h = Harness::start(CountingEmbedder::new());
    let mock = h.mock_mut();
    mock.add_dir("/");
    mock.add_file("/alpha.md", b"widget alpha payload", 1);
    mock.add_file("/beta.md", b"orange banana grape", 2);

    let idx = index_root(&h.svc).await;
    assert!(idx.error.is_none(), "{idx:?}");

    let resp = h
        .svc
        .batch_query(Request::new(BatchQueryRequest {
            auth_token: String::new(),
            queries: vec![
                query_req("widget alpha", QueryType::Keyword, ""),
                // Empty q → per-query error, must not fail the batch.
                query_req("", QueryType::Keyword, ""),
                query_req("orange banana", QueryType::Keyword, ""),
            ],
        }))
        .await
        .expect("batch_query")
        .into_inner();

    assert_eq!(resp.responses.len(), 3);
    assert!(resp.responses[0].error.is_none(), "{:?}", resp.responses[0]);
    assert!(
        resp.responses[1].error.is_some(),
        "empty q must surface its own error: {:?}",
        resp.responses[1],
    );
    assert!(resp.responses[1].results.is_empty());
    assert!(resp.responses[2].error.is_none(), "{:?}", resp.responses[2]);
    // Order: sub 0 is the alpha query, sub 2 the orange query.
    assert_eq!(resp.responses[0].results[0].path, "/alpha.md");
    assert_eq!(resp.responses[2].results[0].path, "/beta.md");
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn batch_dispatches_queries_concurrently() {
    // Two DISTINCT semantic texts (no dupe pre-warm) — under the
    // bounded concurrent dispatch both embeds must be in flight at
    // once.  The rendezvous embedder parks each call until it sees a
    // sibling (or 2s passes), so a serial regression fails the
    // max_in_flight assertion instead of hanging.
    let h = Harness::start(RendezvousEmbedder::new());
    let mock = h.mock_mut();
    mock.add_dir("/");
    mock.add_file("/x.md", b"widget alpha payload", 1);

    let idx = index_root(&h.svc).await;
    assert!(idx.error.is_none(), "{idx:?}");

    let resp = h
        .svc
        .batch_query(Request::new(BatchQueryRequest {
            auth_token: String::new(),
            queries: vec![
                query_req("widget alpha payload", QueryType::Semantic, ""),
                query_req("orange banana grape", QueryType::Semantic, ""),
            ],
        }))
        .await
        .expect("batch_query")
        .into_inner();

    assert_eq!(resp.responses.len(), 2);
    for (i, sub) in resp.responses.iter().enumerate() {
        assert!(sub.error.is_none(), "sub {i} errored: {sub:?}");
    }
    assert!(
        h.embedder.max_in_flight.load(Ordering::SeqCst) >= 2,
        "batch ran its queries serially — expected >= 2 concurrent embeds",
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn batch_keyword_only_needs_no_embedder() {
    // A keyword-only batch (with duplicate texts!) must not require
    // an embedder: the dupe pre-warm only considers semantic/hybrid
    // queries.  Built WITHOUT an injected embedder, so an errant
    // get_or_init_embedder() would surface as a per-query error.
    let dir = TempDir::new().expect("tempdir");
    let mock = Box::into_raw(Box::new(MockKernel::new()));
    let handle = handle_for(mock);
    unsafe {
        (*mock).add_dir("/");
        (*mock).add_file("/x.md", b"widget alpha payload", 1);
    }
    let manager = Arc::new(IndexManager::with_root(dir.path().to_path_buf()));
    let svc = SearchServiceImpl::builder(Arc::new(handle))
        .manager(manager)
        .build();

    let idx = index_root(&svc).await;
    assert!(idx.error.is_none(), "{idx:?}");

    let resp = svc
        .batch_query(Request::new(BatchQueryRequest {
            auth_token: String::new(),
            queries: vec![
                query_req("widget alpha", QueryType::Keyword, ""),
                query_req("widget alpha", QueryType::Keyword, ""),
            ],
        }))
        .await
        .expect("batch_query")
        .into_inner();

    assert_eq!(resp.responses.len(), 2);
    for (i, sub) in resp.responses.iter().enumerate() {
        assert!(sub.error.is_none(), "sub {i} errored: {sub:?}");
        assert_eq!(sub.results[0].path, "/x.md");
    }

    unsafe {
        drop(Box::from_raw(mock));
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn batch_matches_individual_query_responses() {
    // Per-query behaviour must be identical to singles — same
    // handler, same caches, same scoring.
    let h = Harness::start(CountingEmbedder::new());
    let mock = h.mock_mut();
    mock.add_dir("/");
    mock.add_file("/alpha.md", b"widget alpha payload", 1);
    mock.add_file("/beta.md", b"orange banana grape", 2);

    let idx = index_root(&h.svc).await;
    assert!(idx.error.is_none(), "{idx:?}");

    let singles = [
        query_req("widget alpha payload", QueryType::Hybrid, ""),
        query_req("orange banana grape", QueryType::Hybrid, ""),
    ];
    let mut single_responses = Vec::new();
    for q in singles.clone() {
        single_responses.push(
            h.svc
                .query(Request::new(q))
                .await
                .expect("query")
                .into_inner(),
        );
    }

    let batch = h
        .svc
        .batch_query(Request::new(BatchQueryRequest {
            auth_token: String::new(),
            queries: singles.to_vec(),
        }))
        .await
        .expect("batch_query")
        .into_inner();

    assert_eq!(batch.responses.len(), single_responses.len());
    for (i, (b, s)) in batch.responses.iter().zip(&single_responses).enumerate() {
        let b_paths: Vec<&str> = b.results.iter().map(|r| r.path.as_str()).collect();
        let s_paths: Vec<&str> = s.results.iter().map(|r| r.path.as_str()).collect();
        assert_eq!(b_paths, s_paths, "sub {i} diverged from its single");
    }
}
