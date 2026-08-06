//! End-to-end integration test for the Query RPC (Phase 1 of the
//! Python-parity roadmap; see `PARITY_ROADMAP.md`).
//!
//! Exercises the full RPC handler → \[do_query\] → \[IndexManager\]
//! → \[FtsIndex.search\] → \[QueryResult\] roundtrip against a real
//! `SearchServiceImpl` (rlib), with a tempdir-rooted `IndexManager`
//! that tests inject via `with_manager`.
//!
//! Kernel-FFI syscalls (used by Index) are NOT exercised here — a
//! poison `KernelHandle` is injected so any accidental touch from
//! the Query path fails loudly rather than silently reaching a
//! stubbed callback.  Index-path integration lives in a follow-up
//! (`index_e2e.rs`) once the mock-kernel scaffold lands.
//!
//! Follows the standard captured in
//! `cursor-projects/document-ai/.claude/skills/integration-test-generator`:
//! real service impl, real proto types, real async runtime, real
//! storage on disk — only the kernel FFI substrate is a test double,
//! and it's a `poison` double that catches accidental use, not a
//! silent stub.

use std::ffi::c_void;
use std::os::raw::c_char;
use std::sync::Arc;

use nexus_plugin_abi::KernelHandle;
use nexus_search_plugin::index_manager::IndexManager;
use nexus_search_plugin::search_proto::search_service_server::SearchService;
use nexus_search_plugin::search_proto::{QueryRequest, QueryType};
use nexus_search_plugin::service::SearchServiceImpl;
use tempfile::TempDir;
use tonic::Request;

// ── Poison KernelHandle ─────────────────────────────────────────
//
// Query does not touch the kernel; if a regression accidentally
// wires it back in, these callbacks return -1 (a plugin-ABI Internal
// error) which propagates into `IndexOne::Skipped` and shows up as a
// zero-hit result — visible enough that the query test would fail
// its result assertion.

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
        kernel_ptr: std::ptr::null(),
    }
}

// ── Harness ─────────────────────────────────────────────────────

/// Build a service + tempdir-rooted manager, seed a couple of
/// documents directly through the FtsIndex primitive, then run
/// queries against the service.  Same shape whether we're testing
/// the happy path or the not-yet-implemented gates.
struct Harness {
    _dir: TempDir,
    svc: SearchServiceImpl,
    manager: Arc<IndexManager>,
}

impl Harness {
    fn start() -> Self {
        let dir = TempDir::new().expect("tempdir");
        let manager = Arc::new(IndexManager::with_root(dir.path().to_path_buf()));
        let svc = SearchServiceImpl::builder(Arc::new(poison_handle()))
            .manager(Arc::clone(&manager))
            .build();
        Self {
            _dir: dir,
            svc,
            manager,
        }
    }

    fn seed(&self, zone_id: &str) {
        let idx = self.manager.get_or_open(zone_id).expect("open zone");
        idx.add_document(
            "/notes/hello.md",
            0,
            "hello world widget alpha",
            Some(1_700_000_000_000),
        )
        .expect("add-1");
        idx.add_document(
            "/notes/other.md",
            0,
            "goodbye world",
            Some(1_700_000_000_100),
        )
        .expect("add-2");
        idx.add_document("/logs/x.log", 0, "widget beta", Some(1_700_000_000_200))
            .expect("add-3");
        idx.commit().expect("commit");
    }
}

// ── Tests ───────────────────────────────────────────────────────

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn query_returns_bm25_hits_for_seeded_zone() {
    let h = Harness::start();
    h.seed("root");

    let resp = h
        .svc
        .query(Request::new(QueryRequest {
            q: "widget".into(),
            zone_id: "root".into(),
            limit: 10,
            path_filter: String::new(),
            query_type: QueryType::Keyword as i32,
            auth_token: String::new(),
            ..Default::default()
        }))
        .await
        .expect("query")
        .into_inner();

    assert!(resp.error.is_none(), "unexpected error: {:?}", resp.error);
    assert_eq!(
        resp.results.len(),
        2,
        "expected two 'widget' matches, got {:?}",
        resp.results.iter().map(|r| &r.path).collect::<Vec<_>>(),
    );
    for r in &resp.results {
        assert_eq!(r.zone_id, "root");
        assert!(r.score > 0.0);
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn query_empty_zone_resolves_to_root() {
    let h = Harness::start();
    h.seed("root");

    let resp = h
        .svc
        .query(Request::new(QueryRequest {
            q: "widget".into(),
            zone_id: String::new(), // ⇒ ROOT_ZONE_ID
            limit: 10,
            path_filter: String::new(),
            query_type: QueryType::Keyword as i32,
            auth_token: String::new(),
            ..Default::default()
        }))
        .await
        .expect("query")
        .into_inner();

    assert!(resp.error.is_none(), "unexpected error: {:?}", resp.error);
    assert_eq!(resp.results.len(), 2);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn query_path_filter_narrows_results() {
    let h = Harness::start();
    h.seed("root");

    let resp = h
        .svc
        .query(Request::new(QueryRequest {
            q: "widget".into(),
            zone_id: "root".into(),
            limit: 10,
            path_filter: "/notes/".into(),
            query_type: QueryType::Keyword as i32,
            auth_token: String::new(),
            ..Default::default()
        }))
        .await
        .expect("query")
        .into_inner();

    assert!(resp.error.is_none(), "unexpected error: {:?}", resp.error);
    assert_eq!(resp.results.len(), 1);
    assert_eq!(resp.results[0].path, "/notes/hello.md");
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn query_empty_q_returns_error_not_500() {
    let h = Harness::start();
    let resp = h
        .svc
        .query(Request::new(QueryRequest {
            q: String::new(),
            zone_id: "root".into(),
            limit: 10,
            path_filter: String::new(),
            query_type: QueryType::Keyword as i32,
            auth_token: String::new(),
            ..Default::default()
        }))
        .await
        .expect("query")
        .into_inner();

    assert!(resp.error.is_some(), "expected error for empty q");
    assert!(resp.results.is_empty());
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn query_semantic_gracefully_degrades_when_no_embedder() {
    // Post-P2: SEMANTIC is a supported query_type but requires an
    // embedder.  The default Harness uses `with_manager` (no
    // embedder pre-injected) → build_default_embedder tries and
    // fails because NEXUS_SEARCH_MODEL_DIR / ORT_DYLIB_PATH aren't
    // set in the test env.  The RPC must return a clean error
    // ("semantic unavailable: ...") rather than 500 or hang — that
    // preserves the D2 graceful-degradation contract: keyword still
    // works when semantic is unwired.
    let h = Harness::start();
    let resp = h
        .svc
        .query(Request::new(QueryRequest {
            q: "widget".into(),
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

    let err = resp
        .error
        .expect("semantic without embedder must error, not succeed");
    assert!(
        err.contains("semantic unavailable"),
        "semantic degradation message should mention unavailability, got: {err:?}",
    );
    assert!(resp.results.is_empty(), "no results when unavailable");
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn query_hybrid_gracefully_degrades_when_no_embedder() {
    // Post-P3: HYBRID is a supported query_type but requires an
    // embedder (semantic leg needs it).  The default Harness uses
    // `with_manager` (no embedder pre-injected) → the RPC must
    // return "hybrid unavailable: ..." rather than a P3-not-
    // supported message.  Preserves D2 graceful degradation:
    // keyword still works, hybrid errors cleanly.
    let h = Harness::start();
    let resp = h
        .svc
        .query(Request::new(QueryRequest {
            q: "widget".into(),
            zone_id: "root".into(),
            limit: 10,
            path_filter: String::new(),
            query_type: QueryType::Hybrid as i32,
            auth_token: String::new(),
            ..Default::default()
        }))
        .await
        .expect("query")
        .into_inner();

    let err = resp
        .error
        .expect("hybrid without embedder must error, not succeed");
    assert!(
        err.contains("hybrid unavailable"),
        "hybrid degradation message should mention unavailability, got: {err:?}",
    );
    assert!(resp.results.is_empty(), "no results when unavailable");
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn query_zero_limit_uses_default() {
    let h = Harness::start();
    // Seed enough docs to prove the default cap fires.
    let idx = h.manager.get_or_open("root").expect("open");
    for i in 0..25 {
        idx.add_document(&format!("/f-{i}.md"), 0, "widget", Some(i))
            .expect("add");
    }
    idx.commit().expect("commit");

    let resp = h
        .svc
        .query(Request::new(QueryRequest {
            q: "widget".into(),
            zone_id: "root".into(),
            limit: 0, // ⇒ DEFAULT_QUERY_LIMIT (10)
            path_filter: String::new(),
            query_type: QueryType::Keyword as i32,
            auth_token: String::new(),
            ..Default::default()
        }))
        .await
        .expect("query")
        .into_inner();

    assert!(resp.error.is_none());
    assert_eq!(resp.results.len(), 10, "default limit is 10");
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn query_zone_isolation_holds_at_storage_layer() {
    // Regression scaffold for D3: cross-zone read filtering is the
    // kernel driver's job, but the STORAGE layer itself must not
    // bleed docs — a query scoped to zone-a must not see zone-b's
    // corpus.  Anchors the isolation contract at the plugin-RPC
    // boundary in case a future refactor accidentally merges the
    // per-zone indices.
    let h = Harness::start();
    let a = h.manager.get_or_open("zone-a").expect("open za");
    a.add_document("/only-in-a.md", 0, "widget alpha", Some(1))
        .expect("add-a");
    a.commit().expect("commit-a");

    let b = h.manager.get_or_open("zone-b").expect("open zb");
    b.add_document("/only-in-b.md", 0, "widget beta", Some(2))
        .expect("add-b");
    b.commit().expect("commit-b");

    let resp = h
        .svc
        .query(Request::new(QueryRequest {
            q: "widget".into(),
            zone_id: "zone-a".into(),
            limit: 10,
            path_filter: String::new(),
            query_type: QueryType::Keyword as i32,
            auth_token: String::new(),
            ..Default::default()
        }))
        .await
        .expect("query")
        .into_inner();

    assert!(resp.error.is_none());
    assert_eq!(resp.results.len(), 1);
    assert_eq!(resp.results[0].path, "/only-in-a.md");
    assert_eq!(resp.results[0].zone_id, "zone-a");
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn query_returns_mtime_when_stored() {
    let h = Harness::start();
    h.seed("root");

    let resp = h
        .svc
        .query(Request::new(QueryRequest {
            q: "alpha".into(),
            zone_id: "root".into(),
            limit: 10,
            path_filter: String::new(),
            query_type: QueryType::Keyword as i32,
            auth_token: String::new(),
            ..Default::default()
        }))
        .await
        .expect("query")
        .into_inner();

    assert!(resp.error.is_none());
    assert_eq!(resp.results.len(), 1);
    assert_eq!(resp.results[0].mtime_ms, Some(1_700_000_000_000));
}
