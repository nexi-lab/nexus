//! End-to-end integration test for the Index RPC (Phase 1).
//!
//! Companion to `query_e2e.rs` — where that test poisons the
//! kernel callbacks (Query is read-only, must not touch them), this
//! test wires a **mock KernelHandle** whose callbacks read from an
//! in-memory filesystem.  Exercises the whole Index path:
//!
//!   RPC handler → do_index → walk_recursive (sys_readdir + sys_stat)
//!   → index_one (sys_read) → FtsIndex.add_document → commit
//!
//! Then a follow-up Query verifies the docs indexed are searchable.
//!
//! Same integration-test-generator standard as query_e2e.rs: real
//! service impl, real proto types, real async runtime, real tantivy
//! segments on disk.  Only the kernel FFI is a test double, and it
//! speaks the same JSON contract as the real nexus-vfs callbacks —
//! `sys_readdir` returns `[{"name","entry_type"}, ...]` and
//! `sys_stat` returns `{path, entry_type, size, zone_id, modified_at_ms}`.

use std::collections::HashMap;
use std::ffi::{c_void, CStr};
use std::os::raw::c_char;
use std::sync::Arc;

use nexus_plugin_abi::KernelHandle;
use nexus_search_plugin::index_manager::IndexManager;
use nexus_search_plugin::search_proto::search_service_server::SearchService;
use nexus_search_plugin::search_proto::{IndexRequest, QueryRequest, QueryType};
use nexus_search_plugin::service::SearchServiceImpl;
use tempfile::TempDir;
use tonic::Request;

// ── Mock kernel ─────────────────────────────────────────────────
//
// The plugin's kernel_io wrappers use these extern "C" callbacks to
// enumerate + read files.  The mock is a Box'd MockKernel whose raw
// pointer lives in `KernelHandle.kernel_ptr`; every callback casts
// the pointer back and answers from the in-memory maps.
//
// Memory contract: buffers returned via `out_buf` must be `Vec`s
// with `cap == len` because the plugin frees via `nexus_free`, which
// does `Vec::from_raw_parts(ptr, len, len)`.  We `shrink_to_fit()`
// before `mem::forget` to match.

/// Entry type: 0 = regular file, 1 = directory.  Same constants the
/// kernel_io module uses (DT_REG / DT_DIR).
struct FileEntry {
    bytes: Vec<u8>,
    mtime_ms: i64,
}

pub struct MockKernel {
    // path → file (regular files only; dirs are implicit in `dirs`).
    files: HashMap<String, FileEntry>,
    // parent dir → children (name + entry_type).
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
        // Splice into parent dir listing.
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
        // Idempotent: don't double-splice if the dir was already
        // registered via a nested add_file call.
        let entries = self.dirs.entry(parent).or_default();
        if !entries.iter().any(|(n, _)| n == &name) {
            entries.push((name, 1 /* DT_DIR */));
        }
    }
}

/// Split `/a/b/c.md` into (`/a/b`, `c.md`).  `/foo` yields (`/`, `foo`).
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
        None => -404, // NotFound sentinel per plugin-ABI convention
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
            // JSON encoded per the plugin ABI: [{"name":str,"entry_type":u8}].
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

// The write-side callbacks are unused by Index but the plugin ABI
// requires the full KernelHandle table.  Same poison shape as
// query_e2e.rs — return -1 so any accidental use fails loudly.
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

/// Owns the boxed MockKernel + tempdir root; hands out a
/// SearchServiceImpl whose KernelHandle points into the mock.
///
/// Drop cleans up both the boxed kernel and the tempdir.
struct Harness {
    _dir: TempDir,
    mock: *mut MockKernel,
    svc: SearchServiceImpl,
    manager: Arc<IndexManager>,
}

impl Harness {
    fn start() -> Self {
        let dir = TempDir::new().expect("tempdir");
        let mock = Box::into_raw(Box::new(MockKernel::new()));
        let handle = handle_for(mock);
        let manager = Arc::new(IndexManager::with_root(dir.path().to_path_buf()));
        let svc = SearchServiceImpl::with_manager(Arc::new(handle), Arc::clone(&manager));
        Self {
            _dir: dir,
            mock,
            svc,
            manager,
        }
    }

    fn mock_mut(&self) -> &mut MockKernel {
        // SAFETY: `self` owns the box; no other reference exists
        // (tests are single-threaded per-test).
        unsafe { &mut *self.mock }
    }
}

impl Drop for Harness {
    fn drop(&mut self) {
        // SAFETY: `mock` came from `Box::into_raw`; the box owns the
        // memory and no callback holds a lingering pointer past the
        // last RPC.
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

async fn query(
    svc: &SearchServiceImpl,
    q: &str,
) -> nexus_search_plugin::search_proto::QueryResponse {
    svc.query(Request::new(QueryRequest {
        q: q.into(),
        zone_id: "root".into(),
        limit: 10,
        path_filter: String::new(),
        query_type: QueryType::Keyword as i32,
        auth_token: String::new(),
    }))
    .await
    .expect("query")
    .into_inner()
}

// ── Tests ───────────────────────────────────────────────────────

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn index_then_query_roundtrip() {
    let h = Harness::start();
    let mock = h.mock_mut();
    mock.add_dir("/");
    mock.add_dir("/notes");
    mock.add_file(
        "/notes/hello.md",
        b"hello world widget alpha",
        1_700_000_000_000,
    );
    mock.add_file("/notes/other.md", b"goodbye world", 1_700_000_000_100);
    mock.add_file("/README.md", b"widget project overview", 1_700_000_000_200);

    let r = index_root(&h.svc).await;
    assert!(r.error.is_none(), "unexpected index error: {:?}", r.error);
    assert_eq!(r.indexed_count, 3, "expected 3 docs indexed");
    assert_eq!(r.skipped_count, 0);

    let q = query(&h.svc, "widget").await;
    assert!(q.error.is_none());
    assert_eq!(
        q.results.len(),
        2,
        "'widget' should hit hello.md + README.md"
    );
    let paths: Vec<&str> = q.results.iter().map(|r| r.path.as_str()).collect();
    assert!(paths.contains(&"/notes/hello.md"));
    assert!(paths.contains(&"/README.md"));
    // mtime round-trip through the walker.
    let hello = q
        .results
        .iter()
        .find(|r| r.path == "/notes/hello.md")
        .unwrap();
    assert_eq!(hello.mtime_ms, Some(1_700_000_000_000));
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn reindex_is_idempotent() {
    // Regression for the delete_term-before-add idempotency contract:
    // running Index twice on the same corpus must NOT duplicate the
    // documents (BM25 inflation) — proven end-to-end through the
    // walker + writer + reader path.
    let h = Harness::start();
    let mock = h.mock_mut();
    mock.add_dir("/");
    mock.add_file("/x.md", b"widget", 1);
    mock.add_file("/y.md", b"widget", 2);

    let r1 = index_root(&h.svc).await;
    assert_eq!(r1.indexed_count, 2);
    let r2 = index_root(&h.svc).await;
    assert_eq!(r2.indexed_count, 2);

    let q = query(&h.svc, "widget").await;
    assert_eq!(q.results.len(), 2, "after two Index calls, still 2 docs");
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn non_recursive_index_only_touches_direct_children() {
    let h = Harness::start();
    let mock = h.mock_mut();
    mock.add_dir("/");
    mock.add_dir("/nested");
    mock.add_file("/top.md", b"widget", 1);
    mock.add_file("/nested/deep.md", b"widget", 2);

    let r = h
        .svc
        .index(Request::new(IndexRequest {
            root_path: "/".into(),
            zone_id: "root".into(),
            recursive: false,
            max_docs: 0,
            auth_token: String::new(),
        }))
        .await
        .expect("index")
        .into_inner();
    assert!(r.error.is_none());
    assert_eq!(r.indexed_count, 1, "non-recursive must NOT index nested/");
    assert_eq!(r.skipped_count, 0);

    let q = query(&h.svc, "widget").await;
    assert_eq!(q.results.len(), 1);
    assert_eq!(q.results[0].path, "/top.md");
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn skip_empty_binary_and_oversized_files() {
    let h = Harness::start();
    let mock = h.mock_mut();
    mock.add_dir("/");
    // Empty file — skipped, count = 0.
    mock.add_file("/empty.md", b"", 1);
    // Non-UTF8 payload — skipped (P1 is text-only).
    mock.add_file("/binary.bin", &[0xff, 0xfe, 0xfd], 2);
    // Oversize — skipped (> INDEX_MAX_FILE_BYTES = 8 MiB).
    let big = vec![b'a'; 9 * 1024 * 1024];
    mock.add_file("/big.md", &big, 3);
    // Valid file — indexed.
    mock.add_file("/ok.md", b"widget alpha", 4);

    let r = index_root(&h.svc).await;
    assert!(r.error.is_none());
    assert_eq!(r.indexed_count, 1);
    assert_eq!(r.skipped_count, 3, "empty + binary + oversize = 3 skips");
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn max_docs_caps_walker() {
    let h = Harness::start();
    let mock = h.mock_mut();
    mock.add_dir("/");
    for i in 0..10 {
        mock.add_file(&format!("/f{i}.md"), b"widget", i as i64);
    }

    let r = h
        .svc
        .index(Request::new(IndexRequest {
            root_path: "/".into(),
            zone_id: "root".into(),
            recursive: true,
            max_docs: 3,
            auth_token: String::new(),
        }))
        .await
        .expect("index")
        .into_inner();
    assert!(r.error.is_none());
    assert_eq!(r.indexed_count, 3);

    let q = query(&h.svc, "widget").await;
    assert_eq!(q.results.len(), 3);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn nested_walk_indexes_deep_files() {
    let h = Harness::start();
    let mock = h.mock_mut();
    mock.add_dir("/");
    mock.add_dir("/a");
    mock.add_dir("/a/b");
    mock.add_dir("/a/b/c");
    mock.add_file("/a/b/c/deep.md", b"widget deeply nested", 1);
    mock.add_file("/root.md", b"widget top level", 2);

    let r = index_root(&h.svc).await;
    assert!(r.error.is_none());
    assert_eq!(r.indexed_count, 2);

    let q = query(&h.svc, "widget").await;
    assert_eq!(q.results.len(), 2);
    let paths: Vec<&str> = q.results.iter().map(|r| r.path.as_str()).collect();
    assert!(paths.contains(&"/a/b/c/deep.md"));
    assert!(paths.contains(&"/root.md"));
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn missing_root_returns_error_not_500() {
    let h = Harness::start();
    // mock has no dirs registered — root is missing.
    let r = h
        .svc
        .index(Request::new(IndexRequest {
            root_path: "/does-not-exist".into(),
            zone_id: "root".into(),
            recursive: true,
            max_docs: 0,
            auth_token: String::new(),
        }))
        .await
        .expect("index")
        .into_inner();
    assert!(r.error.is_some());
    assert_eq!(r.indexed_count, 0);
}
