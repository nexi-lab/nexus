//! End-to-end integration test for the LLM query-expansion wrapper
//! (Feature 1 of the "fold nexus-server search runtime into the
//! plugin" migration).
//!
//! Real `SearchServiceImpl` + real `IndexManager` on a tempdir + real
//! tokio async runtime + real `fusion::rrf_multi` — the ONLY test
//! double is a canned `QueryExpander` implementation that returns
//! pre-programmed variant lists and counts how many times the wrapper
//! called it.  Every test therefore drives the exact code path a
//! production request takes, up to and including the recursive
//! re-entry that the `IN_EXPANSION` task-local guards.

use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;

use nexus_search_plugin::index_manager::IndexManager;
use nexus_search_plugin::query_expansion::QueryExpansionConfig;
use nexus_search_plugin::query_expansion::{ExpansionError, QueryExpander};
use nexus_search_plugin::search_proto::search_service_server::SearchService;
use nexus_search_plugin::search_proto::{QueryRequest, QueryType};
use nexus_search_plugin::service::{ExpanderHandle, SearchServiceImpl};
use tempfile::TempDir;
use tonic::Request;

mod common;
use common::poison_handle;

// ── Mock expander ───────────────────────────────────────────────
//
// Canned variants + call counter.  Behaviour is fully deterministic;
// each test constructs one, wraps it in an ExpanderHandle, hands it
// to the builder, and inspects `calls()` after the query.

enum MockBehaviour {
    /// Return this fixed list of variants regardless of the query.
    Fixed(Vec<String>),
    /// Return an ExpansionError (simulates HTTP failure / timeout).
    Fail(String),
}

struct MockExpander {
    behaviour: MockBehaviour,
    calls: AtomicUsize,
    last_query: parking_lot::Mutex<Option<String>>,
}

impl MockExpander {
    fn fixed(variants: Vec<&str>) -> Arc<Self> {
        Arc::new(Self {
            behaviour: MockBehaviour::Fixed(variants.into_iter().map(String::from).collect()),
            calls: AtomicUsize::new(0),
            last_query: parking_lot::Mutex::new(None),
        })
    }

    fn failing(msg: &str) -> Arc<Self> {
        Arc::new(Self {
            behaviour: MockBehaviour::Fail(msg.to_string()),
            calls: AtomicUsize::new(0),
            last_query: parking_lot::Mutex::new(None),
        })
    }

    fn calls(&self) -> usize {
        self.calls.load(Ordering::SeqCst)
    }

    fn last_query(&self) -> Option<String> {
        self.last_query.lock().clone()
    }
}

impl QueryExpander for MockExpander {
    fn expand(&self, query: &str, _max_variants: usize) -> Result<Vec<String>, ExpansionError> {
        self.calls.fetch_add(1, Ordering::SeqCst);
        *self.last_query.lock() = Some(query.to_string());
        match &self.behaviour {
            MockBehaviour::Fixed(v) => Ok(v.clone()),
            MockBehaviour::Fail(m) => Err(ExpansionError::Http(m.clone())),
        }
    }
}

// ── Harness ─────────────────────────────────────────────────────

struct Harness {
    _dir: TempDir,
    svc: SearchServiceImpl,
    manager: Arc<IndexManager>,
}

impl Harness {
    fn start(expander: Arc<MockExpander>, max_variants: usize) -> Self {
        let dir = TempDir::new().expect("tempdir");
        let manager = Arc::new(IndexManager::with_root(dir.path().to_path_buf()));
        let config = QueryExpansionConfig {
            endpoint: "http://mock.invalid/v1/chat/completions".to_string(),
            model: "mock".to_string(),
            api_key: "sk-mock".to_string(),
            timeout: std::time::Duration::from_secs(5),
            max_variants,
        };
        let handle = Arc::new(ExpanderHandle {
            expander: expander as Arc<dyn QueryExpander>,
            config,
        });
        let svc = SearchServiceImpl::builder(Arc::new(poison_handle()))
            .manager(Arc::clone(&manager))
            .expander(handle)
            .build();
        Self {
            _dir: dir,
            svc,
            manager,
        }
    }

    fn start_without_expander() -> Self {
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

    fn seed_default_corpus(&self) {
        // Chosen so each query-variant touches a DIFFERENT subset:
        //   "widget"       → hello.md, x.log
        //   "authentication" → auth.md
        //   "login"          → auth.md, login.md
        // Fusion should union all three.
        let idx = self.manager.get_or_open("root").expect("open zone");
        idx.add_document(
            "/notes/hello.md",
            0,
            "hello world widget alpha",
            Some(1_700_000_000_000),
        )
        .expect("add-1");
        idx.add_document(
            "/logs/x.log",
            0,
            "widget beta arrived",
            Some(1_700_000_000_100),
        )
        .expect("add-2");
        idx.add_document(
            "/notes/auth.md",
            0,
            "how authentication and login work together",
            Some(1_700_000_000_200),
        )
        .expect("add-3");
        idx.add_document(
            "/notes/login.md",
            0,
            "login form design and password reset",
            Some(1_700_000_000_300),
        )
        .expect("add-4");
        idx.commit().expect("commit");
    }

    async fn query(&self, q: &str) -> nexus_search_plugin::search_proto::QueryResponse {
        self.svc
            .query(Request::new(QueryRequest {
                q: q.to_string(),
                zone_id: "root".to_string(),
                limit: 20,
                query_type: QueryType::Keyword as i32,
                ..Default::default()
            }))
            .await
            .expect("query")
            .into_inner()
    }
}

// ── Tests ───────────────────────────────────────────────────────

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn expansion_fans_out_and_unions_variant_hits() {
    // Original "authentication" alone matches only auth.md;
    // variants add coverage of login.md.  Fused result must union
    // the two, and the expander must have been called exactly once
    // (the ORIGINAL query — not once per variant re-entry).
    let mock = MockExpander::fixed(vec!["login", "password reset"]);
    let h = Harness::start(mock.clone(), 3);
    h.seed_default_corpus();

    let resp = h.query("authentication").await;
    assert!(resp.error.is_none(), "unexpected error: {:?}", resp.error);

    let paths: Vec<&str> = resp.results.iter().map(|r| r.path.as_str()).collect();
    assert!(
        paths.contains(&"/notes/auth.md"),
        "auth.md missing from {paths:?}"
    );
    assert!(
        paths.contains(&"/notes/login.md"),
        "login.md missing from {paths:?}"
    );

    assert_eq!(
        mock.calls(),
        1,
        "expander must be called exactly once (outer request only, re-entries skipped)",
    );
    assert_eq!(mock.last_query().as_deref(), Some("authentication"));
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn expansion_disabled_when_no_expander_wired() {
    // No .expander() on the builder → get_or_init_expander() returns
    // None → the query drops straight to the single-query pipeline.
    // Result parity: exactly what a query without expansion looks like.
    let h = Harness::start_without_expander();
    h.seed_default_corpus();

    let resp = h.query("widget").await;
    assert!(resp.error.is_none(), "unexpected error: {:?}", resp.error);

    let paths: Vec<&str> = resp.results.iter().map(|r| r.path.as_str()).collect();
    assert!(paths.contains(&"/notes/hello.md"));
    assert!(paths.contains(&"/logs/x.log"));
    assert!(!paths.contains(&"/notes/auth.md"));
    assert!(!paths.contains(&"/notes/login.md"));
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn expansion_llm_failure_degrades_to_single_query() {
    // Mock returns Err(Http) — wrapper must log the warning and
    // fall through to a single-shot original query.  Result parity:
    // same as the "no expander wired" test.
    let mock = MockExpander::failing("simulated 500");
    let h = Harness::start(mock.clone(), 3);
    h.seed_default_corpus();

    let resp = h.query("widget").await;
    assert!(resp.error.is_none(), "unexpected error: {:?}", resp.error);

    let paths: Vec<&str> = resp.results.iter().map(|r| r.path.as_str()).collect();
    assert!(paths.contains(&"/notes/hello.md"));
    assert!(paths.contains(&"/logs/x.log"));
    // No expansion actually reached fusion — auth.md must NOT appear.
    assert!(!paths.contains(&"/notes/auth.md"));

    assert_eq!(
        mock.calls(),
        1,
        "expander must have been called once (and failed)"
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn expansion_empty_variants_falls_through() {
    // Mock returns an empty variant list — nothing useful to fuse,
    // wrapper straight-throughs to the single-query path.
    let mock = MockExpander::fixed(vec![]);
    let h = Harness::start(mock.clone(), 3);
    h.seed_default_corpus();

    let resp = h.query("widget").await;
    assert!(resp.error.is_none());
    let paths: Vec<&str> = resp.results.iter().map(|r| r.path.as_str()).collect();
    assert!(paths.contains(&"/notes/hello.md"));
    assert!(paths.contains(&"/logs/x.log"));
    assert_eq!(
        mock.calls(),
        1,
        "expander called exactly once (returned empty)"
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn expansion_echo_variants_are_dropped() {
    // Model politely re-returns the original query as one of its
    // "variants".  We must NOT re-run the original arm twice — that
    // would double-weight it in the fusion.  Fusion still fires
    // (there's a real variant "login") but the "authentication"
    // echo is skipped.
    let mock = MockExpander::fixed(vec!["authentication", "login"]);
    let h = Harness::start(mock.clone(), 3);
    h.seed_default_corpus();

    let resp = h.query("authentication").await;
    assert!(resp.error.is_none());
    let paths: Vec<&str> = resp.results.iter().map(|r| r.path.as_str()).collect();
    assert!(paths.contains(&"/notes/auth.md"));
    assert!(paths.contains(&"/notes/login.md"));

    assert_eq!(mock.calls(), 1);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn expansion_second_identical_query_hits_the_cache() {
    // ExpansionCache dedupes repeated identical queries.  Second
    // query with the same text must not call the mock a second time.
    let mock = MockExpander::fixed(vec!["login"]);
    let h = Harness::start(mock.clone(), 3);
    h.seed_default_corpus();

    let _ = h.query("authentication").await;
    assert_eq!(mock.calls(), 1, "first query must call the expander");

    let _ = h.query("authentication").await;
    assert_eq!(mock.calls(), 1, "second identical query must hit the cache");

    // Distinct query is a miss and calls the expander again.
    let _ = h.query("widget").await;
    assert_eq!(mock.calls(), 2, "distinct query bypasses the cache");
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn expansion_wrapper_preserves_empty_q_early_return() {
    // The empty-q guard in the trait method must fire BEFORE the
    // expander check — otherwise we'd waste an LLM round-trip on a
    // query the server is going to reject anyway.
    let mock = MockExpander::fixed(vec!["something"]);
    let h = Harness::start(mock.clone(), 3);

    let resp = h.query("").await;
    assert!(resp.error.is_some(), "empty q must error");
    assert_eq!(mock.calls(), 0, "expander must NOT be called for empty q");
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn expansion_stamps_variant_index_on_fused_hits() {
    // Issue #4130 review R7: every hit that survives fusion carries
    // an `expansion_variant_index` telling the caller which arm
    // surfaced it — 0 = original, 1..N = LLM variant N.  When a doc
    // is voted for by both the original and a variant, the ORIGINAL
    // wins the attribution because `rrf_multi` keeps the first-seen
    // template and arms are pushed in `all_queries` order.
    //
    // Corpus: seed_default_corpus() is arranged so
    //   "authentication" (arm 0)      → auth.md
    //   "widget"          (arm 1, var) → hello.md, x.log
    //   "password reset"  (arm 2, var) → login.md
    // and no doc is shared across arms, so each surviving arm
    // stamps its own hits and we can assert the exact mapping.
    let mock = MockExpander::fixed(vec!["widget", "password reset"]);
    let h = Harness::start(mock.clone(), 3);
    h.seed_default_corpus();

    let resp = h.query("authentication").await;
    assert!(resp.error.is_none(), "unexpected error: {:?}", resp.error);

    let mut by_path: std::collections::HashMap<&str, Option<u32>> =
        std::collections::HashMap::new();
    for r in &resp.results {
        by_path.insert(r.path.as_str(), r.expansion_variant_index);
    }

    assert_eq!(
        by_path.get("/notes/auth.md"),
        Some(&Some(0)),
        "auth.md must be stamped as arm 0 (original 'authentication'); got {by_path:?}",
    );
    assert_eq!(
        by_path.get("/notes/hello.md"),
        Some(&Some(1)),
        "hello.md must be stamped as arm 1 (variant 'widget'); got {by_path:?}",
    );
    assert_eq!(
        by_path.get("/logs/x.log"),
        Some(&Some(1)),
        "x.log must be stamped as arm 1 (variant 'widget'); got {by_path:?}",
    );
    assert_eq!(
        by_path.get("/notes/login.md"),
        Some(&Some(2)),
        "login.md must be stamped as arm 2 (variant 'password reset'); got {by_path:?}",
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn expansion_variant_index_absent_on_single_shot_path() {
    // Fallback single-shot paths — no expander wired, empty variants,
    // echo-only variants, LLM failure — must NOT stamp
    // expansion_variant_index (there is no fusion arm to attribute
    // to).  Assert absence explicitly so callers can distinguish
    // "expansion ran, stamped arm 0" from "expansion never ran".
    let h = Harness::start_without_expander();
    h.seed_default_corpus();

    let resp = h.query("widget").await;
    assert!(resp.error.is_none());
    assert!(!resp.results.is_empty(), "widget must find hits");
    for r in &resp.results {
        assert!(
            r.expansion_variant_index.is_none(),
            "single-shot hit {} must not carry an expansion index, got {:?}",
            r.path,
            r.expansion_variant_index,
        );
    }
}
