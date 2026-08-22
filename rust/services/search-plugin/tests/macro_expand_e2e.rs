//! End-to-end integration test for `expand=macro` — the
//! section-aware windowed expand algorithm in `crate::macro_expand`
//! shipped as issue #4130 review R5; the unit tests pin the
//! algorithm in isolation, this file pins it against a REAL
//! `IndexManager` + `FtsIndex` roundtrip so the algorithm's
//! chunk-shape assumptions stay honest as the FTS schema evolves.
//!
//! Structure mirrors `query_e2e.rs`: real `SearchServiceImpl` with a
//! tempdir-rooted manager, poison `KernelHandle` (Query does not
//! touch the kernel), seeded via the FtsIndex primitive.  Only the
//! result-shape assertion changes — we care about
//! `QueryResult.expanded_context`, not just the hit list.
//!
//! Env-mutating variants of this scenario (window-cap, budget-cap,
//! kill-switch) live in sibling `tests/macro_expand_*_e2e.rs` files
//! because Cargo runs each integration binary in its own process —
//! per-file isolation keeps a `set_var` in one test from bleeding
//! into a concurrent test that reads the same env.  This file is
//! reserved for the DEFAULT-config full-section case.

use std::sync::Arc;

use nexus_search_plugin::index_manager::IndexManager;
use nexus_search_plugin::search_proto::search_service_server::SearchService;
use nexus_search_plugin::search_proto::{QueryRequest, QueryType};
use nexus_search_plugin::service::SearchServiceImpl;
use tempfile::TempDir;
use tonic::Request;

mod common;
use common::poison_handle;

// ── Harness ─────────────────────────────────────────────────────

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

    /// Seed a single VFS path with `n` chunks, chunk `i` carrying
    /// text `body-<i>` and — for chunk 3 (matched by the query
    /// term `needle`) — an extra "needle" token so BM25 lands on it.
    /// mtime is a monotonic counter so recency scoring is
    /// deterministic without dragging in a clock.
    fn seed_ten_chunks(&self, zone_id: &str, path: &str) {
        let idx = self.manager.get_or_open(zone_id).expect("open zone");
        for chunk_index in 0..10u32 {
            let text = if chunk_index == 3 {
                format!("body-{chunk_index} needle")
            } else {
                format!("body-{chunk_index}")
            };
            idx.add_document(path, chunk_index, &text, Some(chunk_index as i64))
                .expect("add");
        }
        idx.commit().expect("commit");
    }
}

// ── Tests ───────────────────────────────────────────────────────

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn expand_macro_stitches_windowed_neighbours_around_anchor() {
    // 10 chunks under one .md path.  Query matches only chunk 3.
    // Default budget (1024 tokens ≈ 4096 chars) easily fits the
    // full 10-chunk section (each ~10 chars), so the window should
    // cover the whole file: chunks 0..=9 joined by `\n`.
    let h = Harness::start();
    h.seed_ten_chunks("root", "/notes/hit.md");

    let resp = h
        .svc
        .query(Request::new(QueryRequest {
            q: "needle".into(),
            zone_id: "root".into(),
            limit: 5,
            query_type: QueryType::Keyword as i32,
            expand: "macro".into(),
            ..Default::default()
        }))
        .await
        .expect("query")
        .into_inner();

    assert!(resp.error.is_none(), "unexpected error: {:?}", resp.error);
    assert!(
        !resp.results.is_empty(),
        "expected at least one hit for 'needle' query, got zero",
    );
    let hit = resp
        .results
        .iter()
        .find(|r| r.path == "/notes/hit.md")
        .expect("hit.md must be in results");
    let ctx = &hit.expanded_context;
    assert!(!ctx.is_empty(), "expanded_context must be populated");

    // Full-section case: budget covers everything, so every chunk's
    // text is stitched into the context.  Newline separator matches
    // the algorithm's `\n` join (macro_expand::stitch).
    for chunk_index in 0..10u32 {
        let expected_body = if chunk_index == 3 {
            format!("body-{chunk_index} needle")
        } else {
            format!("body-{chunk_index}")
        };
        assert!(
            ctx.contains(&expected_body),
            "expanded_context should include chunk {chunk_index} body {expected_body:?}; got:\n{ctx}",
        );
    }
}
