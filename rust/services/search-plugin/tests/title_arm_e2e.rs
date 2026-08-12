//! End-to-end test for the hybrid title arm (#4552 mirror / #4628).
//!
//! Exercises the full RPC handler → parallel legs (chunk_kw +
//! semantic + title) → `fuse_hybrid_with_title` → `QueryResult`
//! roundtrip against an injected [`MockEmbedder`] so the test never
//! downloads a real model or links ONNX Runtime.
//!
//! Acceptance mapping (issue #4628):
//! - `title_match_doc_enters_top_n_with_title_score` — a doc whose
//!   title matches the query but whose body chunks are weak enters
//!   the hybrid top-N carrying `title_score = Some(_)`.
//! - `disabling_via_wire_flag_reverts_pre_arm_ranking` — set
//!   `QueryRequest.title_arm_disabled = true` and the title-only
//!   match drops out (parity gate for the wire kill-switch).
//! - `chunkless_doc_still_returned_via_title` — a doc whose body
//!   text is empty (heading-only) still surfaces via the title arm
//!   with `chunk_text = ""`.
//!
//! The MockKernel scaffold is duplicated inline rather than
//! extracted to `tests/common` — same rationale the sibling
//! `semantic_query_e2e.rs` and `index_e2e.rs` cite: Cargo's shared
//! test-module story requires a `mod common;` at each caller, and
//! the extra plumbing costs more than the duplication does at this
//! size.

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
unsafe extern "C" fn unused_rename(
    _: *const c_void,
    _: *const c_char,
    _: *const c_char,
) -> i32 {
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

fn handle_for(kernel: *const MockKernel) -> KernelHandle {
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
    assert!(resp.error.is_none(), "index error: {:?}", resp.error);
}

async fn hybrid_query(
    svc: &SearchServiceImpl,
    q: &str,
    disable_title: bool,
) -> nexus_search_plugin::search_proto::QueryResponse {
    svc.query(Request::new(QueryRequest {
        q: q.into(),
        zone_id: "root".into(),
        limit: 10,
        query_type: QueryType::Hybrid as i32,
        title_arm_disabled: disable_title,
        ..Default::default()
    }))
    .await
    .expect("query")
    .into_inner()
}

// ── Tests ───────────────────────────────────────────────────────

/// Acceptance criterion 1 (issue #4628): a doc whose title matches
/// the query but whose body text is weak enters the hybrid top-N
/// carrying `title_score = Some(_)`.  Locks in the ranking-quality
/// improvement the title arm exists to deliver.
#[tokio::test(flavor = "multi_thread", worker_threads = 3)]
async fn title_match_doc_enters_top_n_with_title_score() {
    let h = Harness::start();
    let mock = h.mock_mut();
    mock.add_dir("/");
    mock.add_dir("/misc");
    mock.add_dir("/reports");
    // Doc A: body mentions "rocket" but title is unrelated.
    mock.add_file(
        "/reports/weekly.md",
        b"# Weekly Digest\n\nvarious rocket updates below\n",
        1,
    );
    // Doc B: body has NO "rocket" text; title is "Rocket Launch
    // Retrospective" — only the title arm can surface this on a
    // "rocket" query.
    mock.add_file(
        "/misc/target.md",
        b"# Rocket Launch Retrospective\n\ngeneric content with no matching body tokens\n",
        2,
    );

    index_root(&h.svc).await;

    let resp = hybrid_query(&h.svc, "rocket", false).await;
    assert!(resp.error.is_none(), "query error: {:?}", resp.error);
    assert!(!resp.results.is_empty(), "expected hits: {resp:?}");

    let paths: Vec<&str> = resp.results.iter().map(|r| r.path.as_str()).collect();
    assert!(
        paths.contains(&"/misc/target.md"),
        "title-arm target must enter top-N; got {paths:?}",
    );

    // The title-only doc carries title_score attribution.
    let target = resp
        .results
        .iter()
        .find(|r| r.path == "/misc/target.md")
        .expect("target present");
    assert!(
        target.title_score.is_some(),
        "target must carry title_score: {target:?}",
    );
    assert!(
        target.title_score.unwrap() > 0.0,
        "title_score must be positive: {target:?}",
    );
}

/// Acceptance criterion 2 (issue #4628): setting
/// `QueryRequest.title_arm_disabled = true` reverts to the pre-arm
/// two-source hybrid.  Non-title queries stay ranking-neutral (that
/// property is covered by `fusion::rrf_multi_fusion_empty_title_arm_
/// ranks_like_two_arm_rrf`).  This test covers the wire kill-switch:
/// the title-only doc must drop below the arm-on doc when the arm
/// is off, and lose its `title_score` attribution.
#[tokio::test(flavor = "multi_thread", worker_threads = 3)]
async fn disabling_via_wire_flag_reverts_pre_arm_ranking() {
    let h = Harness::start();
    let mock = h.mock_mut();
    mock.add_dir("/");
    mock.add_dir("/misc");
    mock.add_file(
        "/misc/title_only.md",
        b"# Rocket Launch Retrospective\n\nno body match for the query\n",
        1,
    );
    // Body-only doc so the arm-off case still has at least one hit.
    mock.add_file(
        "/misc/body_only.md",
        b"# Weekly Digest\n\nlots of rocket updates and rocket news\n",
        2,
    );

    index_root(&h.svc).await;

    let with_arm = hybrid_query(&h.svc, "rocket", false).await;
    let without_arm = hybrid_query(&h.svc, "rocket", true).await;

    // Title-only doc: present + attributed with arm on; absent OR
    // sans attribution with arm off.
    let with_target = with_arm
        .results
        .iter()
        .find(|r| r.path == "/misc/title_only.md");
    assert!(
        with_target.is_some() && with_target.unwrap().title_score.is_some(),
        "arm-on: title-only doc must be present with title_score: {with_arm:?}",
    );

    let without_target = without_arm
        .results
        .iter()
        .find(|r| r.path == "/misc/title_only.md");
    match without_target {
        Some(h) => assert!(
            h.title_score.is_none(),
            "arm-off: title_score must not be stamped: {h:?}",
        ),
        None => {
            // Ranking dropped it entirely — also valid: the title-only
            // doc's only pathway to top-N was the title arm.
        }
    }
}

/// Acceptance criterion 3 (issue #4628): a heading-only doc with no
/// meaningful body text is still retrievable via the title arm.
/// Chunkless docs come through with an empty `chunk_text` — the
/// hydration path preserves the "doc still discoverable" contract.
#[tokio::test(flavor = "multi_thread", worker_threads = 3)]
async fn chunkless_doc_still_returned_via_title() {
    let h = Harness::start();
    let mock = h.mock_mut();
    mock.add_dir("/");
    mock.add_dir("/lonely");
    // Title-only file: just the heading, no body paragraph.
    mock.add_file("/lonely/pure_title.md", b"# Nova Prime Overview\n", 1);

    index_root(&h.svc).await;

    let resp = hybrid_query(&h.svc, "nova prime", false).await;
    assert!(resp.error.is_none(), "query error: {:?}", resp.error);
    let hit = resp
        .results
        .iter()
        .find(|r| r.path == "/lonely/pure_title.md");
    assert!(
        hit.is_some(),
        "title-only doc must surface via title arm: {resp:?}",
    );
    let hit = hit.unwrap();
    assert!(
        hit.title_score.is_some(),
        "title-only surfacing must carry title_score: {hit:?}",
    );
    // chunk_text may be empty or hold the single heading chunk —
    // either shape satisfies "doc still discoverable"; we assert on
    // the retrievability contract, not the exact snippet.
}
