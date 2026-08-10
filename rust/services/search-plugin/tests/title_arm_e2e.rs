//! End-to-end tests for the hybrid title arm (#4628).
//!
//! Sibling of `semantic_query_e2e.rs` — same MockKernel + injected
//! MockEmbedder scaffold (duplicated per that file's note: Cargo's
//! shared-test-module story costs more than the duplication).
//!
//! The acceptance workload mirrors the Python #4552 plan doc: a doc
//! whose TITLE matches the query but whose body is weak must enter
//! the hybrid results with `title_score` attribution; with the arm
//! off (builder pin) it must not benefit; a query with no title
//! match must return byte-identical results with the arm on and off
//! (the pass-through parity guarantee).

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

// ── Harness ─────────────────────────────────────────────────────

struct Harness {
    _dir: TempDir,
    mock: *mut MockKernel,
    svc: SearchServiceImpl,
}

impl Harness {
    /// MockEmbedder pre-injected (hybrid needs the semantic leg);
    /// `title_arm` pinned via the builder so the tests never race
    /// the process env.
    fn start(title_arm: bool) -> Self {
        let dir = TempDir::new().expect("tempdir");
        let mock = Box::into_raw(Box::new(MockKernel::new()));
        let handle = handle_for(mock);
        let manager = Arc::new(IndexManager::with_root(dir.path().to_path_buf()));
        let embedder = Arc::new(MockEmbedder::with_dim(16));
        let svc = SearchServiceImpl::builder(Arc::new(handle))
            .manager(manager)
            .embedder(embedder)
            .title_arm(title_arm)
            .build();
        Self {
            _dir: dir,
            mock,
            svc,
        }
    }

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

/// The acceptance corpus: /designs/atlas.md is titled "Atlas Design
/// Doc" but its body never mentions "design" or "doc"; the decoys
/// repeat the query words in their bodies so chunk-BM25 favours
/// them.  Pre-title-arm, "atlas design doc" buried the target.
fn seed_corpus(mock: &mut MockKernel) {
    mock.add_dir("/");
    mock.add_dir("/designs");
    mock.add_dir("/notes");
    mock.add_file(
        "/designs/atlas.md",
        b"# Atlas Design Doc\n\nservice topology overview and rollout phases.",
        1,
    );
    mock.add_file(
        "/notes/scratch1.md",
        b"# Scratch\n\ndesign doc design doc design doc atlas mention here.",
        2,
    );
    mock.add_file(
        "/notes/scratch2.md",
        b"# Scratch Two\n\nanother design doc draft, atlas atlas design notes.",
        3,
    );
}

async fn index_root(svc: &SearchServiceImpl) {
    let resp = svc
        .index(Request::new(IndexRequest {
            root_path: "/".into(),
            zone_id: "root".into(),
            recursive: true,
            max_docs: 0,
            auth_token: String::new(),
        }))
        .await
        .expect("index")
        .into_inner();
    assert!(resp.error.is_none(), "index failed: {:?}", resp.error);
}

async fn hybrid_query(
    svc: &SearchServiceImpl,
    q: &str,
) -> Vec<nexus_search_plugin::search_proto::QueryResult> {
    let resp = svc
        .query(Request::new(QueryRequest {
            q: q.into(),
            zone_id: "root".into(),
            limit: 10,
            query_type: QueryType::Hybrid as i32,
            ..Default::default()
        }))
        .await
        .expect("query")
        .into_inner();
    assert!(resp.error.is_none(), "query failed: {:?}", resp.error);
    resp.results
}

// ── Tests ───────────────────────────────────────────────────────

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn title_shaped_query_surfaces_weak_body_doc_with_attribution() {
    let h = Harness::start(true);
    seed_corpus(h.mock_mut());
    index_root(&h.svc).await;

    let results = hybrid_query(&h.svc, "atlas design doc").await;
    let atlas = results
        .iter()
        .find(|r| r.path == "/designs/atlas.md")
        .expect("title-matched doc must enter the hybrid results");
    // locate score: 3 title-token overlaps × 2.0 + 1 path-token
    // overlap ("atlas") = 7.0 — the same pin the Python acceptance
    // test used.
    assert_eq!(atlas.title_score, Some(7.0));
    // Hydration: the doc has an FTS chunk-0 row, so text is real.
    assert!(!atlas.chunk_text.is_empty(), "hydrated chunk text expected");
    // The decoys must not carry title attribution ("Scratch" /
    // "Scratch Two" share no title token with the query).
    for r in results.iter().filter(|r| r.path != "/designs/atlas.md") {
        assert_eq!(r.title_score, None, "unexpected attribution on {}", r.path);
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn arm_off_no_attribution_and_no_title_benefit() {
    // Shared IndexManager (same FTS + ANN graph) — see the parity
    // test below for why per-instance ANN builds would add hnsw_rs
    // RNG variance unrelated to the arm.
    let dir = TempDir::new().expect("tempdir");
    let mock = Box::into_raw(Box::new(MockKernel::new()));
    seed_corpus(unsafe { &mut *mock });
    let manager = Arc::new(IndexManager::with_root(dir.path().to_path_buf()));
    let embedder = Arc::new(MockEmbedder::with_dim(16));
    let svc_on = SearchServiceImpl::builder(Arc::new(handle_for(mock)))
        .manager(Arc::clone(&manager))
        .embedder(embedder.clone())
        .title_arm(true)
        .build();
    let svc_off = SearchServiceImpl::builder(Arc::new(handle_for(mock)))
        .manager(manager)
        .embedder(embedder)
        .title_arm(false)
        .build();
    index_root(&svc_on).await;

    let res_on = hybrid_query(&svc_on, "atlas design doc").await;
    let res_off = hybrid_query(&svc_off, "atlas design doc").await;

    assert!(res_off.iter().all(|r| r.title_score.is_none()));
    let rank = |rs: &[nexus_search_plugin::search_proto::QueryResult]| {
        rs.iter().position(|r| r.path == "/designs/atlas.md")
    };
    let rank_on = rank(&res_on).expect("arm on: target present");
    // MockEmbedder is deterministic, so this comparison is stable:
    // with the arm off the target either drops out of the list or
    // ranks no better than with the arm on (the arm only ADDS
    // evidence for the target).
    match rank(&res_off) {
        None => {}
        Some(rank_off) => assert!(
            rank_on <= rank_off,
            "title arm must never worsen the target's rank (on={rank_on}, off={rank_off})"
        ),
    }
    unsafe { drop(Box::from_raw(mock)) };
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn non_title_query_is_byte_identical_with_arm_on_and_off() {
    // Locate has no hits for this query (no title-token overlap ≥
    // MIN_SCORE) → the keyword lane passes through untouched and
    // the responses must match EXACTLY — the pass-through parity
    // guarantee that keeps the arm invisible off its target.
    //
    // Both services SHARE one IndexManager (same FTS + same ANN
    // graph): the guarantee under test is that the fusion pipeline
    // is arm-invariant GIVEN identical leg inputs.  Two separately
    // built ANN graphs would differ by hnsw_rs's per-instance RNG
    // (level assignment), which is ANN recall variance, not an arm
    // effect.
    let dir = TempDir::new().expect("tempdir");
    let mock = Box::into_raw(Box::new(MockKernel::new()));
    seed_corpus(unsafe { &mut *mock });
    let manager = Arc::new(IndexManager::with_root(dir.path().to_path_buf()));
    let embedder = Arc::new(MockEmbedder::with_dim(16));
    let svc_on = SearchServiceImpl::builder(Arc::new(handle_for(mock)))
        .manager(Arc::clone(&manager))
        .embedder(embedder.clone())
        .title_arm(true)
        .build();
    let svc_off = SearchServiceImpl::builder(Arc::new(handle_for(mock)))
        .manager(manager)
        .embedder(embedder)
        .title_arm(false)
        .build();
    index_root(&svc_on).await;

    let res_on = hybrid_query(&svc_on, "rollout topology overview").await;
    let res_off = hybrid_query(&svc_off, "rollout topology overview").await;
    assert_eq!(res_on, res_off);
    unsafe { drop(Box::from_raw(mock)) };
}
