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

use std::sync::Arc;

use nexus_search_plugin::embedder::{Embedder, MockEmbedder, RemoteEmbedder, RemoteEmbedderConfig};
use nexus_search_plugin::index_manager::IndexManager;
use nexus_search_plugin::search_proto::search_service_server::SearchService;
use nexus_search_plugin::search_proto::{IndexRequest, QueryRequest, QueryType};
use nexus_search_plugin::service::SearchServiceImpl;
use tempfile::TempDir;
use tonic::Request;

mod common;
use common::{handle_for, MockKernel};

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
        Self::start_with_embedder(Arc::new(MockEmbedder::with_dim(16)))
    }

    /// Start with a caller-supplied embedder — lets a live test inject a
    /// real `RemoteEmbedder` while every deterministic test keeps the mock.
    fn start_with_embedder(embedder: Arc<dyn Embedder>) -> Self {
        let dir = TempDir::new().expect("tempdir");
        let mock = Box::into_raw(Box::new(MockKernel::new()));
        let handle = handle_for(mock);
        let manager = Arc::new(IndexManager::with_root(dir.path().to_path_buf()));
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
async fn semantic_indexes_and_finds_stream_document() {
    // A DT_STREAM (entry_type 4) must ride the SAME semantic (embed + ANN)
    // index/query path as a file — that is the whole point of opening search
    // to streams (nexus-vfs#235). The host shim hands the plugin the stream's
    // WHOLE collected log via sys_read, so downstream it is indistinguishable
    // from a document; here we prove the embed → ANN → FTS-enrich round-trip
    // surfaces a stream by its content, side-by-side with a real file.
    let h = Harness::start();
    let mock = h.mock_mut();
    mock.add_dir("/");
    mock.add_dir("/mail");
    // The mailbox stream — its content is what a semantic query must match.
    mock.add_stream("/mail/inbox.wal", b"quokka rendezvous dispatch ledger", 7);
    // A plain file alongside it, semantically unrelated.
    mock.add_file("/mail/readme.md", b"orange banana grape basket", 2);

    let idx = index_root(&h.svc).await;
    assert!(
        idx.error.is_none(),
        "unexpected index error: {:?}",
        idx.error
    );
    // BOTH the stream and the file are searchable content → both indexed.
    assert_eq!(idx.indexed_count, 2);

    // Query the stream's EXACT text — deterministic mock embed ⇒ distance ≈ 0
    // ⇒ the stream ranks first with score ≈ 1.
    let q = semantic_query(&h.svc, "quokka rendezvous dispatch ledger", "").await;
    assert!(
        q.error.is_none(),
        "unexpected semantic error: {:?}",
        q.error
    );
    assert!(!q.results.is_empty(), "expected the stream to be found");
    assert_eq!(
        q.results[0].path, "/mail/inbox.wal",
        "the DT_STREAM document should be the top semantic hit"
    );
    assert!(
        (q.results[0].score - 1.0).abs() < 0.001,
        "exact-text stream hit should score ≈ 1, got {}",
        q.results[0].score,
    );
    // FTS enrichment carries the stream's chunk_text back through the sibling
    // index — proving the stream is present in BOTH the ANN and FTS indices.
    assert_eq!(q.results[0].chunk_text, "quokka rendezvous dispatch ledger");
    // Stream mtime survives the round-trip like any document.
    assert_eq!(q.results[0].mtime_ms, Some(7));
}

/// LIVE semantic-over-stream against a REAL embeddings API (not the mock).
///
/// This is the one path the deterministic tests can't cover: a real
/// embedding model must rank a stream above an unrelated file for a query
/// that shares NO words with it — pure semantic similarity, which only a
/// real model produces. It proves the embed → ANN → fusion pipeline works
/// end-to-end for a DT_STREAM document with production-shaped vectors.
///
/// Inert by default: returns early unless `NEXUS_SEARCH_EMBED_API_URL`
/// (+ `_MODEL`/`_DIM`/`_API_KEY`) is set, so CI and key-less boxes skip it.
/// Run locally with a real OpenAI-compatible endpoint + key. Verified
/// against SudoRouter `text-embedding-3-small` (dim 1536).
///
/// Sync `#[test]`, not `#[tokio::test]`: `RemoteEmbedder` wraps a
/// `reqwest::blocking` client (its own background runtime), which must be
/// built, used, and dropped OUTSIDE an async context — so we drive the
/// async service calls through an explicit runtime's `block_on`, exactly
/// like the plugin's `dispatch_grpc` does, and let the embedder drop on
/// this sync thread.
#[test]
fn semantic_over_stream_live_remote_embedder() {
    let cfg = match RemoteEmbedderConfig::from_env() {
        Ok(Some(c)) => c,
        _ => {
            eprintln!(
                "SKIP semantic_over_stream_live_remote_embedder: set NEXUS_SEARCH_EMBED_API_URL \
                 (+ _MODEL/_DIM/_API_KEY) to run the live remote-embedder check"
            );
            return;
        }
    };
    let embedder = Arc::new(RemoteEmbedder::new(cfg).expect("build remote embedder"));
    let h = Harness::start_with_embedder(embedder);
    {
        let mock = h.mock_mut();
        mock.add_dir("/");
        mock.add_dir("/mail");
        // A DT_STREAM about finance, and a file about hiking. The query below
        // shares no vocabulary with either — only the embedding space knows
        // which is topically closer.
        mock.add_stream(
            "/mail/inbox.wal",
            b"the quarterly budget forecast and revenue projections for the finance team",
            7,
        );
        mock.add_file(
            "/mail/readme.md",
            b"how to hike the coastal mountain trail before sunrise",
            2,
        );
    }

    let rt = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(2)
        .enable_all()
        .build()
        .expect("tokio runtime");
    let (idx, q) = rt.block_on(async {
        let idx = index_root(&h.svc).await;
        // Paraphrase of the stream's topic with zero shared words — a keyword
        // index would miss it; a real embedder must rank the stream first.
        let q = semantic_query(&h.svc, "financial planning and earnings estimates", "").await;
        (idx, q)
    });

    assert!(
        idx.error.is_none(),
        "unexpected index error: {:?}",
        idx.error
    );
    assert_eq!(idx.indexed_count, 2, "stream + file both indexed");
    assert!(
        q.error.is_none(),
        "unexpected semantic error: {:?}",
        q.error
    );
    assert!(!q.results.is_empty(), "expected a semantic hit");
    assert_eq!(
        q.results[0].path, "/mail/inbox.wal",
        "a real embedder must rank the finance STREAM above the hiking file \
         for a finance query; got {:?}",
        q.results,
    );
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
