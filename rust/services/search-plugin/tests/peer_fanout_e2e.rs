//! End-to-end integration test for the search-plugin peer-fanout
//! wrapper.
//!
//! Two SearchServiceImpl instances live in-process — one is the
//! "local" node under test, one is served as the "peer" over a
//! genuine tonic Channel bound to a loopback ephemeral port.  Every
//! test drives the local node's Query handler and asserts the fused
//! result matches (local ∪ peer) with per-peer failures logged and
//! excluded from the fusion, not surfaced as an RPC error.
//!
//! The peer is deliberately a REAL tonic server on a REAL port —
//! not a mock trait object — because the recursion-guard that
//! prevents fan-out loops lives in tonic metadata (see
//! `peer_fanout::PEER_FANOUT_MARKER_HEADER`), and a trait mock would
//! bypass it and hide the loop-prevention bug the test is here to
//! catch.

use std::collections::HashSet;
use std::sync::Arc;
use std::time::Duration;

use nexus_search_plugin::index_manager::IndexManager;
use nexus_search_plugin::peer_fanout::PeerFanoutDispatcher;
use nexus_search_plugin::peer_registry::{PeerAddress, PeerRegistry};
use nexus_search_plugin::search_proto::search_service_server::{
    SearchService, SearchServiceServer,
};
use nexus_search_plugin::search_proto::{QueryRequest, QueryType};
use nexus_search_plugin::service::SearchServiceImpl;
use tempfile::TempDir;
use tokio::net::TcpListener;
use tonic::transport::Server;
use tonic::Request;

mod common;
use common::poison_handle;

// ── Build a peer plugin, bind a REAL tonic server ────────────────

/// Spawn a fresh SearchServiceImpl behind a tonic server on a
/// loopback ephemeral port.  Returns the bound address + a tempdir
/// (kept alive as long as the peer runs) + the manager handle so
/// tests can seed the peer's zone.
async fn spawn_peer_plugin(seed_paths: &[(&str, &str)]) -> PeerFixture {
    let dir = TempDir::new().expect("tempdir");
    let manager = Arc::new(IndexManager::with_root(dir.path().to_path_buf()));
    // A peer's own service must NOT federate — else two peers with
    // each other in their registries loop through the metadata guard
    // twice per query.  Belt-and-suspenders: also call .no_peer_fanout().
    let svc = SearchServiceImpl::builder(Arc::new(poison_handle()))
        .manager(Arc::clone(&manager))
        .no_peer_fanout()
        .build();

    // Seed the peer's root zone with (path, text) pairs.
    {
        let idx = manager.get_or_open("root").expect("open peer zone");
        for (i, (p, t)) in seed_paths.iter().enumerate() {
            idx.add_document(p, 0, t, Some(1_700_000_000_000 + i as i64 * 100))
                .expect("seed peer doc");
        }
        idx.commit().expect("peer commit");
    }

    let listener = TcpListener::bind("127.0.0.1:0").await.expect("bind");
    let addr = listener.local_addr().expect("addr");
    let incoming = tokio_stream::wrappers::TcpListenerStream::new(listener);

    let _handle = tokio::spawn(async move {
        Server::builder()
            .add_service(SearchServiceServer::new(svc))
            .serve_with_incoming(incoming)
            .await
            .expect("peer serve");
    });
    // Give the server a moment to enter its accept loop before the
    // test kicks a client at it.  20 ms is generous on loopback.
    tokio::time::sleep(Duration::from_millis(20)).await;

    PeerFixture {
        _dir: dir,
        addr,
        _manager: manager,
    }
}

struct PeerFixture {
    _dir: TempDir,
    addr: std::net::SocketAddr,
    _manager: Arc<IndexManager>,
}

// ── Local node harness ──────────────────────────────────────────

struct LocalNode {
    _dir: TempDir,
    svc: SearchServiceImpl,
    manager: Arc<IndexManager>,
}

impl LocalNode {
    fn new(peers: Vec<PeerAddress>, fanout_zones: Vec<&str>) -> Self {
        let dir = TempDir::new().expect("tempdir");
        let manager = Arc::new(IndexManager::with_root(dir.path().to_path_buf()));
        let zones: HashSet<String> = fanout_zones.into_iter().map(String::from).collect();
        // allow_insecure_peer=true so plaintext loopback dials pass
        // the standing "refuse plaintext off-loopback" gate (the
        // in-test peers are on 127.0.0.1 anyway; this is defence in
        // depth for CI systems whose 127.0.0.1 detection might miss).
        let registry = PeerRegistry::new(peers, zones, false, true);
        let fed = Arc::new(PeerFanoutDispatcher::new(registry));
        let svc = SearchServiceImpl::builder(Arc::new(poison_handle()))
            .manager(Arc::clone(&manager))
            .peer_fanout(fed)
            .build();
        Self {
            _dir: dir,
            svc,
            manager,
        }
    }

    fn new_without_peer_fanout() -> Self {
        let dir = TempDir::new().expect("tempdir");
        let manager = Arc::new(IndexManager::with_root(dir.path().to_path_buf()));
        let svc = SearchServiceImpl::builder(Arc::new(poison_handle()))
            .manager(Arc::clone(&manager))
            .no_peer_fanout()
            .build();
        Self {
            _dir: dir,
            svc,
            manager,
        }
    }

    fn seed(&self, docs: &[(&str, &str)]) {
        let idx = self.manager.get_or_open("root").expect("open local zone");
        for (i, (p, t)) in docs.iter().enumerate() {
            idx.add_document(p, 0, t, Some(1_700_000_000_000 + i as i64 * 100))
                .expect("seed local");
        }
        idx.commit().expect("local commit");
    }

    async fn query(&self, q: &str) -> nexus_search_plugin::search_proto::QueryResponse {
        self.svc
            .query(Request::new(QueryRequest {
                q: q.to_string(),
                zone_id: "root".into(),
                limit: 20,
                query_type: QueryType::Keyword as i32,
                ..Default::default()
            }))
            .await
            .expect("query")
            .into_inner()
    }
}

fn peer_addr(fixture: &PeerFixture) -> PeerAddress {
    PeerAddress {
        host: fixture.addr.ip().to_string(),
        port: fixture.addr.port(),
    }
}

// ── Tests ───────────────────────────────────────────────────────

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn peer_fanout_unions_local_and_peer_hits() {
    // Local has doc-a + doc-b(overlap); Peer has doc-b(overlap) + doc-c.
    // The fused response must contain all three, with doc-b deduped.
    let peer =
        spawn_peer_plugin(&[("/b.md", "beta and gamma widget"), ("/c.md", "gamma delta")]).await;
    let local = LocalNode::new(vec![peer_addr(&peer)], vec!["root"]);
    local.seed(&[
        ("/a.md", "alpha widget"),
        ("/b.md", "beta and gamma widget"),
    ]);

    let resp = local.query("widget").await;
    assert!(resp.error.is_none(), "unexpected error: {:?}", resp.error);

    let paths: Vec<&str> = resp.results.iter().map(|r| r.path.as_str()).collect();
    assert!(
        paths.contains(&"/a.md"),
        "local-only doc missing: {paths:?}"
    );
    assert!(paths.contains(&"/b.md"), "shared doc missing: {paths:?}");
    // Peer fan-out dedupes on (path, chunk_index).
    assert_eq!(
        paths.iter().filter(|p| **p == "/b.md").count(),
        1,
        "shared doc must appear once, got {paths:?}",
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn peer_fanout_pulls_peer_only_hits_when_local_has_nothing() {
    // "gamma" matches only in the peer's corpus.  Peer fan-out must
    // surface it even though local has zero hits.
    let peer = spawn_peer_plugin(&[("/c.md", "gamma widget in peer")]).await;
    let local = LocalNode::new(vec![peer_addr(&peer)], vec!["root"]);
    local.seed(&[("/a.md", "alpha widget")]);

    let resp = local.query("gamma").await;
    assert!(resp.error.is_none(), "unexpected error: {:?}", resp.error);

    let paths: Vec<&str> = resp.results.iter().map(|r| r.path.as_str()).collect();
    assert!(paths.contains(&"/c.md"), "peer-only doc missing: {paths:?}");
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn peer_fanout_degrades_gracefully_when_peer_is_offline() {
    // Point the local node at an ephemeral port that nothing is
    // bound to → peer dial FAILS on connect.  Fan-out must log
    // the warning and return LOCAL-only results, not surface an
    // RPC-level error.
    let dead_peer = PeerAddress {
        host: "127.0.0.1".to_string(),
        // Port 1 is (nearly) always closed on Linux/Windows/Mac —
        // more reliable than "any high port might be taken by CI
        // parallelism".
        port: 1,
    };
    let local = LocalNode::new(vec![dead_peer], vec!["root"]);
    local.seed(&[("/a.md", "alpha widget beta")]);

    let resp = local.query("widget").await;
    assert!(resp.error.is_none(), "unexpected error: {:?}", resp.error);
    let paths: Vec<&str> = resp.results.iter().map(|r| r.path.as_str()).collect();
    assert!(
        paths.contains(&"/a.md"),
        "local-only hit dropped: {paths:?}"
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn peer_fanout_skipped_for_zones_not_in_allowlist() {
    // Fan-out IS active (peer + zone list configured), but the
    // query targets a zone NOT on the allowlist.  Expected: peer is
    // NOT dialled, only local results come back.
    let peer = spawn_peer_plugin(&[("/peer-only.md", "the peer widget")]).await;
    let local = LocalNode::new(vec![peer_addr(&peer)], vec!["shared"]);
    local.seed(&[("/a.md", "alpha widget")]);

    // Query targets root, not shared — fan-out should skip.
    let resp = local
        .svc
        .query(Request::new(QueryRequest {
            q: "widget".to_string(),
            zone_id: "root".into(),
            limit: 20,
            query_type: QueryType::Keyword as i32,
            ..Default::default()
        }))
        .await
        .expect("query")
        .into_inner();

    let paths: Vec<&str> = resp.results.iter().map(|r| r.path.as_str()).collect();
    assert!(paths.contains(&"/a.md"), "local hit missing: {paths:?}");
    assert!(
        !paths.contains(&"/peer-only.md"),
        "peer hit leaked into non-fanout zone: {paths:?}",
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn peer_fanout_no_op_when_no_peers_configured() {
    // No .peer_fanout() and .no_peer_fanout() explicitly — validates
    // that a single-node deployment costs nothing in the query path.
    let local = LocalNode::new_without_peer_fanout();
    local.seed(&[("/a.md", "alpha widget")]);

    let resp = local.query("widget").await;
    assert!(resp.error.is_none());
    let paths: Vec<&str> = resp.results.iter().map(|r| r.path.as_str()).collect();
    assert_eq!(paths, vec!["/a.md"]);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn peer_fanout_marker_header_short_circuits_receiver() {
    // Regression guard for the loop-prevention header — a call
    // arriving WITH the marker must skip its own peer fan-out.
    // We drive the peer directly (bypassing the local node) with a
    // request that carries the marker; the peer has NO peers
    // configured anyway, so this test is defence-in-depth against a
    // future misconfig where a peer accidentally federates back.
    let peer_a = spawn_peer_plugin(&[("/pa.md", "widget in peer A")]).await;
    let peer_b = spawn_peer_plugin(&[("/pb.md", "widget in peer B")]).await;

    // Peer-A configured with peer-B as ITS peer — a bad config that
    // could loop without the marker guard.
    let dir = TempDir::new().expect("tempdir");
    let manager = Arc::new(IndexManager::with_root(dir.path().to_path_buf()));
    let idx = manager.get_or_open("root").expect("open");
    idx.add_document("/x.md", 0, "local widget", Some(1_700_000_000_000))
        .expect("seed");
    idx.commit().expect("commit");
    let registry = PeerRegistry::new(
        vec![peer_addr(&peer_a), peer_addr(&peer_b)],
        HashSet::from(["root".to_string()]),
        false,
        true,
    );
    let fed = Arc::new(PeerFanoutDispatcher::new(registry));
    let svc = SearchServiceImpl::builder(Arc::new(poison_handle()))
        .manager(Arc::clone(&manager))
        .peer_fanout(fed)
        .build();

    // Query with the marker header — must NOT federate; must return
    // ONLY local results, not the peer-A or peer-B docs.
    let mut req = Request::new(QueryRequest {
        q: "widget".to_string(),
        zone_id: "root".into(),
        limit: 20,
        query_type: QueryType::Keyword as i32,
        ..Default::default()
    });
    req.metadata_mut().insert(
        nexus_search_plugin::peer_fanout::PEER_FANOUT_MARKER_HEADER,
        tonic::metadata::MetadataValue::try_from("1").unwrap(),
    );

    let resp = svc.query(req).await.expect("query").into_inner();
    let paths: Vec<&str> = resp.results.iter().map(|r| r.path.as_str()).collect();
    assert!(paths.contains(&"/x.md"), "local hit missing: {paths:?}");
    assert!(
        !paths.contains(&"/pa.md") && !paths.contains(&"/pb.md"),
        "marker header failed to suppress peer fan-out: {paths:?}",
    );
}
