//! End-to-end tests for the hybrid title arm (#4628).
//!
//! Sibling of `semantic_query_e2e.rs` — shares the `common::MockKernel`
//! kernel double and injects a `MockEmbedder` (the hybrid title arm
//! needs the semantic leg).
//!
//! The acceptance workload mirrors the Python #4552 plan doc: a doc
//! whose TITLE matches the query but whose body is weak must enter
//! the hybrid results with `title_score` attribution; with the arm
//! off (builder pin) it must not benefit; a query with no title
//! match must return byte-identical results with the arm on and off
//! (the pass-through parity guarantee).

use std::sync::Arc;

use nexus_search_plugin::embedder::MockEmbedder;
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
    // Six more decoys spamming the query terms in their BODIES (their
    // titles share no token with the query) so chunk-BM25 buries the
    // target under the arm-off ranking — the kill-switch test asserts
    // STRICT rank benefit at a tight limit (review R3).
    for (i, name) in ["three", "four", "five", "six", "seven", "eight"]
        .iter()
        .enumerate()
    {
        mock.add_file(
            &format!("/notes/scratch-{name}.md"),
            format!(
                "# Jotting {name}\n\ndesign doc atlas design doc atlas design \
                 doc notes; atlas design doc rev {i} drafting design doc atlas."
            )
            .as_bytes(),
            4 + i as i64,
        );
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
    assert!(resp.error.is_none(), "index failed: {:?}", resp.error);
}

async fn hybrid_query_limit(
    svc: &SearchServiceImpl,
    q: &str,
    limit: u32,
) -> Vec<nexus_search_plugin::search_proto::QueryResult> {
    let resp = svc
        .query(Request::new(QueryRequest {
            q: q.into(),
            zone_id: "root".into(),
            limit,
            query_type: QueryType::Hybrid as i32,
            ..Default::default()
        }))
        .await
        .expect("query")
        .into_inner();
    assert!(resp.error.is_none(), "query failed: {:?}", resp.error);
    resp.results
}

async fn hybrid_query(
    svc: &SearchServiceImpl,
    q: &str,
) -> Vec<nexus_search_plugin::search_proto::QueryResult> {
    hybrid_query_limit(svc, q, 10).await
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
async fn txt_doc_earns_title_attribution_via_stem_fallback() {
    // #4647: mixed-format corpus — the gold doc is a .txt whose
    // FILENAME is a near-exact query match; the decoy is an .md whose
    // heading shares tokens with the query.  Pre-fallback the .txt
    // could never earn title_score and the md decoy won the arm
    // unopposed (observed on the koodle 75-doc corpus: MRR .679 vs
    // .774 with the arm off).
    let h = Harness::start(true);
    {
        let mock = h.mock_mut();
        mock.add_dir("/");
        mock.add_dir("/w");
        mock.add_file(
            "/w/08_PLRM_Q3_earnings_transcript.txt",
            b"Operator: Good afternoon, and welcome to the conference call.",
            1,
        );
        mock.add_file(
            "/w/09_post_earnings_quick_take.md",
            b"# Post-Earnings Quick Take: Q3\n\nhot take on the quarter.",
            2,
        );
    }
    index_root(&h.svc).await;

    let results = hybrid_query(&h.svc, "Q3 earnings call prepared remarks transcript").await;
    let gold = results
        .iter()
        .find(|r| r.path == "/w/08_PLRM_Q3_earnings_transcript.txt")
        .expect(".txt doc must enter the hybrid results via the title arm");
    let decoy = results
        .iter()
        .find(|r| r.path == "/w/09_post_earnings_quick_take.md")
        .expect("decoy present");
    let gold_title = gold
        .title_score
        .expect(".txt doc must carry title attribution");
    if let Some(decoy_title) = decoy.title_score {
        assert!(
            gold_title > decoy_title,
            "filename near-match must outscore the heading decoy: {gold_title} vs {decoy_title}"
        );
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

    // Tight limit + 8 decoys whose bodies spam the query terms: the
    // arm-off ranking must bury or exclude the target, and the arm
    // must STRICTLY improve it — this is the ranking-benefit
    // acceptance, not just attribution plumbing (review R3).
    let res_on = hybrid_query_limit(&svc_on, "atlas design doc", 3).await;
    let res_off = hybrid_query_limit(&svc_off, "atlas design doc", 3).await;

    assert!(res_off.iter().all(|r| r.title_score.is_none()));
    let rank = |rs: &[nexus_search_plugin::search_proto::QueryResult]| {
        rs.iter().position(|r| r.path == "/designs/atlas.md")
    };
    let rank_on = rank(&res_on).expect("arm on: target must be in the top-3");
    // MockEmbedder is deterministic and both services share one
    // index, so this comparison is stable.
    match rank(&res_off) {
        None => {} // buried below the limit without the arm — benefit shown
        Some(rank_off) => assert!(
            rank_on < rank_off,
            "title arm must STRICTLY improve the target's rank (on={rank_on}, off={rank_off})"
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
