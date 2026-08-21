//! Index build-visibility tests (#4623) + Stats identity fields (#4617).
//!
//! Pins the two behaviours the downstream health sentinels rely on:
//!
//! - an embed-heavy explicit IndexDocuments batch commits FTS
//!   incrementally, so keyword hits for early documents are visible
//!   WHILE the remainder of the batch is still embedding (pre-fix, a
//!   single end-of-batch commit answered healthy-empty for tens of
//!   seconds — indistinguishable from silent degradation);
//! - `Stats.indexing_in_progress` is non-zero exactly while explicit
//!   index ops are in flight, and the identity fields (`backend`,
//!   `embedding_model`) are always present.
//!
//! Scaffold duplicated from `batch_query_e2e.rs` per that file's note
//! (Cargo shared-test-module plumbing costs more than the duplication
//! at this size).

use std::collections::HashMap;
use std::ffi::{c_void, CStr};
use std::os::raw::c_char;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use nexus_plugin_abi::KernelHandle;
use nexus_search_plugin::embedder::{EmbedError, Embedder, MockEmbedder};
use nexus_search_plugin::index_manager::IndexManager;
use nexus_search_plugin::search_proto::search_service_server::SearchService;
use nexus_search_plugin::search_proto::{
    DocumentInput, IndexDocumentsRequest, QueryRequest, QueryType, StatsRequest,
};
use nexus_search_plugin::service::SearchServiceImpl;
use tempfile::TempDir;
use tonic::Request;

// ── Mock kernel (same shape as batch_query_e2e.rs; index_documents
//    never touches it, but the service constructor needs a handle) ──

pub struct MockKernel {
    files: HashMap<String, Vec<u8>>,
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
        Some(bytes) => {
            into_out_buf(bytes.clone(), out_buf, out_len);
            0
        }
        None => -404,
    }
}

unsafe extern "C" fn mock_sys_readdir(
    _: *const c_void,
    _: *const c_char,
    out_json: *mut *mut u8,
    out_len: *mut usize,
) -> i32 {
    into_out_buf(b"[]".to_vec(), out_json, out_len);
    0
}

unsafe extern "C" fn mock_sys_stat(
    _: *const c_void,
    _: *const c_char,
    _: *mut *mut u8,
    _: *mut usize,
) -> i32 {
    -404
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
        free_buf: nexus_plugin_abi::nexus_free,
        kernel_ptr: kernel as *const c_void,
    }
}

fn leak_kernel() -> *const MockKernel {
    Box::into_raw(Box::new(MockKernel {
        files: HashMap::new(),
    }))
}

// ── Gated embedder ──────────────────────────────────────────────

/// Passes the first `gate_after` embed_batch calls straight through,
/// then BLOCKS (bounded) until `release` flips.  Runs on the
/// blocking pool, so parking in place is safe.  Models the
/// tens-of-seconds CPU embed phase of a large batch.
struct GatedEmbedder {
    inner: MockEmbedder,
    calls: AtomicUsize,
    gate_after: usize,
    release: Arc<AtomicBool>,
}

impl GatedEmbedder {
    fn new(gate_after: usize, release: Arc<AtomicBool>) -> Self {
        Self {
            inner: MockEmbedder::with_dim(16),
            calls: AtomicUsize::new(0),
            gate_after,
            release,
        }
    }
}

impl Embedder for GatedEmbedder {
    fn embed_batch(&self, texts: &[&str]) -> Result<Vec<Vec<f32>>, EmbedError> {
        let n = self.calls.fetch_add(1, Ordering::SeqCst) + 1;
        if n > self.gate_after {
            let deadline = Instant::now() + Duration::from_secs(30);
            while !self.release.load(Ordering::SeqCst) {
                assert!(Instant::now() < deadline, "gate never released");
                std::thread::sleep(Duration::from_millis(10));
            }
        }
        self.inner.embed_batch(texts)
    }
    fn dim(&self) -> usize {
        self.inner.dim()
    }
    fn tag(&self) -> &str {
        "mock"
    }
}

fn doc(path: &str, text: &str) -> DocumentInput {
    DocumentInput {
        path: path.to_string(),
        text: text.to_string(),
        mtime_ms: Some(1_700_000_000_000),
        zone_id: String::new(),
    }
}

async fn keyword_hits(svc: &SearchServiceImpl, q: &str) -> Vec<String> {
    let resp = svc
        .query(Request::new(QueryRequest {
            q: q.to_string(),
            limit: 10,
            query_type: QueryType::Keyword as i32,
            ..Default::default()
        }))
        .await
        .expect("query rpc")
        .into_inner();
    assert!(resp.error.is_none(), "query error: {:?}", resp.error);
    resp.results.into_iter().map(|r| r.path).collect()
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn stats_identity_fields_present_and_idle() {
    let dir = TempDir::new().unwrap();
    let svc = SearchServiceImpl::builder(Arc::new(handle_for(leak_kernel())))
        .manager(Arc::new(IndexManager::with_root(dir.path().to_path_buf())))
        .embedder(Arc::new(MockEmbedder::with_dim(16)))
        .build();

    let stats = svc
        .stats(Request::new(StatsRequest::default()))
        .await
        .unwrap()
        .into_inner();
    assert_eq!(stats.backend, "rust-plugin");
    // Injected embedder is live in the slot — its tag is the identity.
    assert_eq!(stats.embedding_model, "mock");
    assert_eq!(stats.indexing_in_progress, 0);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn semantic_query_drops_ann_hits_without_fts_rows() {
    // Orphan guard: a vector whose FTS row is gone (drift — deleted /
    // content-transitioned doc) must not surface in semantic results.
    let dir = TempDir::new().unwrap();
    let manager = Arc::new(IndexManager::with_root(dir.path().to_path_buf()));
    let svc = SearchServiceImpl::builder(Arc::new(handle_for(leak_kernel())))
        .manager(Arc::clone(&manager))
        .embedder(Arc::new(MockEmbedder::with_dim(16)))
        .build();

    svc.index_documents(Request::new(IndexDocumentsRequest {
        documents: vec![
            doc("/ws/intact.md", "quantum widget assembly manual"),
            doc("/ws/orphaned.md", "quantum widget assembly notes"),
        ],
        zone_id: String::new(),
        auth_token: String::new(),
    }))
    .await
    .expect("index rpc");

    // Simulate drift: drop ONE doc's FTS rows while its vectors stay.
    let fts = manager.get_or_open("root").expect("fts open");
    fts.delete_all_chunks("/ws/orphaned.md");
    fts.commit().expect("fts commit");

    let resp = svc
        .query(Request::new(QueryRequest {
            q: "quantum widget assembly".to_string(),
            limit: 10,
            query_type: QueryType::Semantic as i32,
            ..Default::default()
        }))
        .await
        .expect("query rpc")
        .into_inner();
    assert!(resp.error.is_none(), "query error: {:?}", resp.error);
    let paths: Vec<&str> = resp.results.iter().map(|r| r.path.as_str()).collect();
    assert!(
        paths.contains(&"/ws/intact.md"),
        "intact doc must still surface: {paths:?}"
    );
    assert!(
        !paths.contains(&"/ws/orphaned.md"),
        "orphaned vector must be dropped by the read guard: {paths:?}"
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn prefix_boost_promotes_doc_from_below_the_requested_limit() {
    // Review R3: score adjustments must act on an OVER-FETCHED pool.
    // Pre-fix, a keyword query with limit=1 fetched exactly one hit,
    // so a ×10 boost on a doc ranked #2 could never promote it.
    let dir = TempDir::new().unwrap();
    let svc = SearchServiceImpl::builder(Arc::new(handle_for(leak_kernel())))
        .manager(Arc::new(IndexManager::with_root(dir.path().to_path_buf())))
        .embedder(Arc::new(MockEmbedder::with_dim(16)))
        .build();

    // doc-plain matches "target" three times (BM25 rank #1);
    // doc-boosted matches once but lives under the boosted prefix.
    svc.index_documents(Request::new(IndexDocumentsRequest {
        documents: vec![
            doc("/plain/doc.md", "target target target filler body"),
            doc("/boosted/doc.md", "target mentioned once in passing"),
        ],
        zone_id: String::new(),
        auth_token: String::new(),
    }))
    .await
    .expect("index rpc");

    let unboosted = svc
        .query(Request::new(QueryRequest {
            q: "target".to_string(),
            limit: 1,
            query_type: QueryType::Keyword as i32,
            ..Default::default()
        }))
        .await
        .expect("query rpc")
        .into_inner();
    assert_eq!(
        unboosted.results[0].path, "/plain/doc.md",
        "sanity: BM25 alone ranks the triple-match first"
    );

    let boosted = svc
        .query(Request::new(QueryRequest {
            q: "target".to_string(),
            limit: 1,
            query_type: QueryType::Keyword as i32,
            path_prefix_boosts: std::collections::HashMap::from([("/boosted/".to_string(), 10.0)]),
            ..Default::default()
        }))
        .await
        .expect("query rpc")
        .into_inner();
    assert_eq!(
        boosted.results.len(),
        1,
        "limit must still cap the response"
    );
    assert_eq!(
        boosted.results[0].path, "/boosted/doc.md",
        "a ×10 boost must promote the doc that started below the limit"
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn prefix_boost_promotes_doc_from_beyond_double_the_limit() {
    // Review R4: the adjustment pool must reach beyond a fixed 2x —
    // here the boosted doc starts at BM25 rank 4 with limit=1, so a
    // 2x over-fetch (pool of 2) could never surface it.
    let dir = TempDir::new().unwrap();
    let svc = SearchServiceImpl::builder(Arc::new(handle_for(leak_kernel())))
        .manager(Arc::new(IndexManager::with_root(dir.path().to_path_buf())))
        .embedder(Arc::new(MockEmbedder::with_dim(16)))
        .build();

    svc.index_documents(Request::new(IndexDocumentsRequest {
        documents: vec![
            doc("/plain/a.md", "target target target target dense match"),
            doc("/plain/b.md", "target target target strong match"),
            doc("/plain/c.md", "target target decent match"),
            doc("/boosted/doc.md", "target single passing mention"),
        ],
        zone_id: String::new(),
        auth_token: String::new(),
    }))
    .await
    .expect("index rpc");

    let boosted = svc
        .query(Request::new(QueryRequest {
            q: "target".to_string(),
            limit: 1,
            query_type: QueryType::Keyword as i32,
            path_prefix_boosts: std::collections::HashMap::from([("/boosted/".to_string(), 10.0)]),
            ..Default::default()
        }))
        .await
        .expect("query rpc")
        .into_inner();
    assert_eq!(boosted.results.len(), 1);
    assert_eq!(
        boosted.results[0].path, "/boosted/doc.md",
        "boost must reach a doc starting at rank 4 with limit=1"
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn keyword_hits_visible_while_batch_still_embedding() {
    let dir = TempDir::new().unwrap();
    let release = Arc::new(AtomicBool::new(false));
    // Gate after doc 10: docs 1..=8 pass the incremental-commit
    // threshold (8) before the embedder parks, so an incremental
    // FTS commit MUST have happened while the batch is in flight.
    let svc = Arc::new(
        SearchServiceImpl::builder(Arc::new(handle_for(leak_kernel())))
            .manager(Arc::new(IndexManager::with_root(dir.path().to_path_buf())))
            .embedder(Arc::new(GatedEmbedder::new(10, Arc::clone(&release))))
            .build(),
    );

    let documents: Vec<DocumentInput> = (1..=12)
        .map(|i| {
            doc(
                &format!("/ws/doc-{i}.md"),
                &format!("docword{i} shared corpus body text"),
            )
        })
        .collect();

    let svc_bg = Arc::clone(&svc);
    let batch = tokio::spawn(async move {
        svc_bg
            .index_documents(Request::new(IndexDocumentsRequest {
                documents,
                zone_id: String::new(),
                auth_token: String::new(),
            }))
            .await
            .expect("index_documents rpc")
            .into_inner()
    });

    // While the batch is parked on doc 11's embed: the in-flight
    // counter must read 1 AND keyword hits for early docs must be
    // visible (incremental commit), i.e. NOT healthy-empty.
    let deadline = Instant::now() + Duration::from_secs(30);
    loop {
        assert!(
            Instant::now() < deadline,
            "mid-flight visibility never observed"
        );
        let stats = svc
            .stats(Request::new(StatsRequest::default()))
            .await
            .unwrap()
            .into_inner();
        if stats.indexing_in_progress == 1 {
            let hits = keyword_hits(&svc, "docword1").await;
            if hits.contains(&"/ws/doc-1.md".to_string()) {
                break;
            }
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }

    // Release the gate; the batch completes and the counter drops.
    release.store(true, Ordering::SeqCst);
    let resp = batch.await.expect("join");
    assert_eq!(resp.indexed_count, 12);
    assert!(resp.error.is_none(), "batch error: {:?}", resp.error);

    let stats = svc
        .stats(Request::new(StatsRequest::default()))
        .await
        .unwrap()
        .into_inner();
    assert_eq!(stats.indexing_in_progress, 0);

    // Read-your-writes: every doc keyword-visible after return.
    let hits = keyword_hits(&svc, "docword12").await;
    assert!(
        hits.contains(&"/ws/doc-12.md".to_string()),
        "last doc not visible after IndexDocuments returned: {hits:?}"
    );
}
