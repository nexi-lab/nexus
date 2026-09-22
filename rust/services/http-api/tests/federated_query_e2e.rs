//! `POST /v2/search/query` federated fanout — end-to-end cover.
//!
//! # What this pins
//!
//! * Multi-zone caller (ReBAC has granted `reader` on ≥2 zones) hits
//!   `/v2/search/query` with an empty `zone_id` and the handler
//!   dispatches through [`nexus_federated_search::FederatedSearchDispatcher`]
//!   instead of a single-zone gRPC call.
//! * The mock plugin sees ONE `Query` call per accessible zone —
//!   proves the fanout actually happens over the real chain.
//! * Fused results come back as [`nexus_http_api::handlers::search::QueryResponseBody`]
//!   with contributions from every zone — proves the bridge +
//!   dispatcher + local backend chain composes.
//! * A caller pinning `zone_id` explicitly bypasses the federated
//!   branch and hits ONE zone (matches the handler docstring's rule).
//!
//! # Why this file (not query_e2e.rs)
//!
//! `query_e2e.rs` covers the single-zone path with `NoAuth` + no
//! ReBAC grants; adding the multi-zone shape there would mix two
//! auth stances in one harness.  This file wires a subject-stamping
//! auth provider + seeds ReBAC grants — one concern per file.
//!
//! Gate: `#[cfg(feature = "rebac")]` — non-rebac builds skip the
//! federated branch unconditionally.

#![cfg(feature = "rebac")]

use std::sync::{Arc, Mutex};

use contracts::operation_context::OperationContext;
use nexus_http_api::search_proto::search_service_server::{SearchService, SearchServiceServer};
use nexus_http_api::search_proto::{
    AddIndexedDirectoryRequest, AddIndexedDirectoryResponse, BatchQueryRequest, BatchQueryResponse,
    GlobRequest, GlobResponse, GrepRequest, GrepResponse, HealthRequest, HealthResponse,
    IndexDocumentsRequest, IndexDocumentsResponse, IndexRequest, IndexResponse,
    ListIndexedDirectoriesRequest, ListIndexedDirectoriesResponse, ListZoneIndexingModesRequest,
    ListZoneIndexingModesResponse, LocateRequest, LocateResponse, NotifyFileChangeRequest,
    NotifyFileChangeResponse, ParkedDiscardRequest, ParkedDiscardResponse, ParkedListRequest,
    ParkedListResponse, ParkedRetryRequest, ParkedRetryResponse, QueryRequest, QueryResponse,
    QueryResult, RefreshRequest, RefreshResponse, RemoveIndexedDirectoryRequest,
    RemoveIndexedDirectoryResponse, SetZoneIndexingModeRequest, SetZoneIndexingModeResponse,
    StatsRequest, StatsResponse,
};
use nexus_http_api::{bind_and_serve, AppState};
use nexus_rebac::{tuple_key, InMemoryReBACTupleStore, ReBACTupleStore};
use tokio::net::TcpListener;
use tokio_stream::wrappers::TcpListenerStream;
use tonic::{Request, Response, Status};
use transport::auth::{AuthCredentials, AuthProvider};

// ── Mock plugin ────────────────────────────────────────────────

#[derive(Default, Clone)]
struct RequestLog {
    queries: Arc<Mutex<Vec<QueryRequest>>>,
}

/// Mock that returns one canned hit per zone_id so the fused
/// response shows contributions from every fanout leg.  Returning
/// distinct paths per zone lets the test spot missing / duplicate
/// legs at a glance.
#[derive(Clone)]
struct FanoutMock {
    log: RequestLog,
}

#[tonic::async_trait]
impl SearchService for FanoutMock {
    async fn query(&self, req: Request<QueryRequest>) -> Result<Response<QueryResponse>, Status> {
        let inner = req.into_inner();
        let zone = inner.zone_id.clone();
        self.log.queries.lock().unwrap().push(inner);
        let result = QueryResult {
            path: format!("/{zone}/hit.md"),
            chunk_index: 0,
            chunk_text: format!("body of /{zone}/hit.md"),
            score: 1.0,
            zone_id: zone,
            mtime_ms: None,
            expanded_context: String::new(),
            title_score: None,
            keyword_score: None,
            vector_score: None,
            tier_boost: None,
            recency_boost: None,
            expansion_variant_index: None,
        };
        Ok(Response::new(QueryResponse {
            results: vec![result],
            error: None,
        }))
    }

    // Every other RPC unreachable — federated dispatch only calls
    // `query`.  Kept explicit rather than `_` so a proto surface
    // change breaks the build here instead of silently drifting.
    async fn glob(&self, _: Request<GlobRequest>) -> Result<Response<GlobResponse>, Status> {
        unreachable!()
    }
    async fn grep(&self, _: Request<GrepRequest>) -> Result<Response<GrepResponse>, Status> {
        unreachable!()
    }
    async fn index(&self, _: Request<IndexRequest>) -> Result<Response<IndexResponse>, Status> {
        unreachable!()
    }
    async fn refresh(
        &self,
        _: Request<RefreshRequest>,
    ) -> Result<Response<RefreshResponse>, Status> {
        unreachable!()
    }
    async fn batch_query(
        &self,
        _: Request<BatchQueryRequest>,
    ) -> Result<Response<BatchQueryResponse>, Status> {
        unreachable!()
    }
    async fn index_documents(
        &self,
        _: Request<IndexDocumentsRequest>,
    ) -> Result<Response<IndexDocumentsResponse>, Status> {
        unreachable!()
    }
    async fn notify_file_change(
        &self,
        _: Request<NotifyFileChangeRequest>,
    ) -> Result<Response<NotifyFileChangeResponse>, Status> {
        unreachable!()
    }
    async fn locate(&self, _: Request<LocateRequest>) -> Result<Response<LocateResponse>, Status> {
        unreachable!()
    }
    async fn parked_list(
        &self,
        _: Request<ParkedListRequest>,
    ) -> Result<Response<ParkedListResponse>, Status> {
        unreachable!()
    }
    async fn parked_retry(
        &self,
        _: Request<ParkedRetryRequest>,
    ) -> Result<Response<ParkedRetryResponse>, Status> {
        unreachable!()
    }
    async fn parked_discard(
        &self,
        _: Request<ParkedDiscardRequest>,
    ) -> Result<Response<ParkedDiscardResponse>, Status> {
        unreachable!()
    }
    async fn add_indexed_directory(
        &self,
        _: Request<AddIndexedDirectoryRequest>,
    ) -> Result<Response<AddIndexedDirectoryResponse>, Status> {
        unreachable!()
    }
    async fn remove_indexed_directory(
        &self,
        _: Request<RemoveIndexedDirectoryRequest>,
    ) -> Result<Response<RemoveIndexedDirectoryResponse>, Status> {
        unreachable!()
    }
    async fn list_indexed_directories(
        &self,
        _: Request<ListIndexedDirectoriesRequest>,
    ) -> Result<Response<ListIndexedDirectoriesResponse>, Status> {
        unreachable!()
    }
    async fn set_zone_indexing_mode(
        &self,
        _: Request<SetZoneIndexingModeRequest>,
    ) -> Result<Response<SetZoneIndexingModeResponse>, Status> {
        unreachable!()
    }
    async fn list_zone_indexing_modes(
        &self,
        _: Request<ListZoneIndexingModesRequest>,
    ) -> Result<Response<ListZoneIndexingModesResponse>, Status> {
        unreachable!()
    }
    async fn health(&self, _: Request<HealthRequest>) -> Result<Response<HealthResponse>, Status> {
        unreachable!()
    }
    async fn stats(&self, _: Request<StatsRequest>) -> Result<Response<StatsResponse>, Status> {
        unreachable!()
    }
}

// ── Subject-stamping auth provider ─────────────────────────────

/// Every bearer resolves to admin `user:alice`.  Admin so the zone
/// gate (`effective_zone`) passes without an explicit `zone_perms`
/// claim; the ReBAC-derived accessible zone set drives the federated
/// branch.
struct AliceAdmin;

impl AuthProvider for AliceAdmin {
    fn resolve(&self, _: &AuthCredentials<'_>) -> Result<OperationContext, Status> {
        let mut ctx = OperationContext::new(
            "alice", // user_id
            "root",  // zone_id (default; admin can name any)
            true,    // is_system — admin passthrough on effective_zone
            None,    // agent_id
            true,    // is_admin
        );
        // The federated fast-out reads `subject_id` + `subject_type`
        // — set them so `list_accessible_zones("user", "alice")`
        // finds the grants we seed below.
        ctx.subject_type = "user".to_string();
        ctx.subject_id = Some("alice".to_string());
        Ok(ctx)
    }
}

// ── Harness ────────────────────────────────────────────────────

async fn spawn_mock_plugin() -> (String, RequestLog) {
    let listener = TcpListener::bind("127.0.0.1:0").await.expect("bind");
    let addr = listener.local_addr().unwrap();
    let log = RequestLog::default();
    let mock = FanoutMock { log: log.clone() };
    tokio::spawn(async move {
        tonic::transport::Server::builder()
            .add_service(SearchServiceServer::new(mock))
            .serve_with_incoming(TcpListenerStream::new(listener))
            .await
            .expect("plugin serve");
    });
    (format!("http://{addr}"), log)
}

/// Grant `user:alice` `reader` on `zone` in `store`.  Uses the same
/// tuple encoder the production store uses so the grants are
/// discoverable by [`nexus_rebac::list_accessible_zones`] which the
/// dispatcher's cache reads through.
fn grant_alice_reader(store: &InMemoryReBACTupleStore, zone: &str) {
    let t = lib::types::ReBACTuple {
        object_type: "zone".into(),
        object_id: zone.into(),
        // `member` is one of `READ_GRANTING_RELATIONS`
        // (`{member, owner, admin, viewer}`) that
        // `list_accessible_zones` recognises as a read grant on a
        // zone object.  `reader` is NOT in that set — an easy
        // mistake for a first reader; the constant lives in
        // `nexus_rebac::list_zones::READ_GRANTING_RELATIONS`.
        relation: "member".into(),
        subject_type: "user".into(),
        subject_id: "alice".into(),
        subject_relation: None,
    };
    let key = tuple_key::encode("root", &t).expect("encode");
    store.put(&key, b"").expect("put");
}

async fn spawn_harness(zones: &[&str]) -> (String, RequestLog) {
    let (plugin_target, log) = spawn_mock_plugin().await;
    let mut state = AppState::for_tests(plugin_target);
    state.auth = Arc::new(AliceAdmin);
    // Rebuild the ReBAC store with grants BEFORE constructing the
    // dispatcher — the dispatcher's `AccessibleZonesCache` reads
    // through the store on every miss so subsequent grants take
    // effect within the TTL window.  Wire the fresh store here.
    let rebac = Arc::new(InMemoryReBACTupleStore::new());
    for z in zones {
        grant_alice_reader(&rebac, z);
    }
    state.rebac_store = Arc::clone(&rebac) as Arc<dyn ReBACTupleStore>;
    // Rewire the federated dispatcher to read the SAME store the
    // AppState holds — `for_tests` built one against its own store,
    // and swapping `rebac_store` here would otherwise leave the
    // dispatcher pointed at the original empty one.
    {
        use nexus_federated_search::{DispatcherConfig, FederatedSearchDispatcher, RoutingBackend};
        use nexus_search_common::transport::{PeerChannelCache, PeerChannelConfig};
        use nexus_search_common::InMemoryZoneSearchRegistry;
        let local = Arc::new(
            nexus_http_api::backends::plugin_local::PluginLocalSearchBackend::new(
                state.search.clone(),
            ),
        );
        // Same `TonicRemoteSearchBackend` production wires; never
        // dialed in this test because the registry stays empty.
        let peer_cache = Arc::new(PeerChannelCache::new(PeerChannelConfig::default()));
        let remote = Arc::new(
            nexus_http_api::backends::tonic_remote::TonicRemoteSearchBackend::new(peer_cache),
        );
        let registry: Arc<InMemoryZoneSearchRegistry> = Arc::new(InMemoryZoneSearchRegistry::new());
        let routing = RoutingBackend::new(
            local,
            remote,
            Arc::clone(&registry) as Arc<dyn nexus_search_common::ZoneSearchRegistry>,
            "test",
            std::iter::empty::<String>(),
        );
        state.federated = Arc::new(FederatedSearchDispatcher::new(
            Arc::new(routing),
            Arc::clone(&rebac) as Arc<dyn ReBACTupleStore>,
            Arc::clone(&state.accessible_zones),
            registry,
            DispatcherConfig::default(),
        ));
    }
    let (bound, fut) = bind_and_serve("127.0.0.1:0".parse().unwrap(), state)
        .await
        .expect("bind http");
    tokio::spawn(async move {
        fut.await.expect("http serve");
    });
    (format!("http://{bound}"), log)
}

// ── Tests ──────────────────────────────────────────────────────

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn multi_zone_caller_with_empty_zone_id_fans_out() {
    // alice has reader on eng + legal — the handler's federated
    // fast-out fires because accessible_zones.len() > 1.
    let (base, log) = spawn_harness(&["eng", "legal"]).await;

    let body = serde_json::json!({
        "q": "widget",
        "query_type": "keyword",
    });
    let resp = reqwest::Client::new()
        .post(format!("{base}/v2/search/query"))
        .bearer_auth("anything")
        .json(&body)
        .send()
        .await
        .expect("http");
    let status = resp.status();
    let body_text = resp.text().await.expect("body");
    assert_eq!(status.as_u16(), 200, "want 200; got {status}: {body_text}");
    let json: serde_json::Value = serde_json::from_str(&body_text).expect("json");

    // Mock plugin got ONE Query per accessible zone (2 total).  The
    // fanout is what the federated dispatcher does; a regression here
    // (e.g. single-zone fast path swallowing the multi-zone caller)
    // would drop the count back to 1.
    let queries = log.queries.lock().unwrap();
    assert_eq!(
        queries.len(),
        2,
        "expected 2 fanout legs (eng + legal), got {:?}",
        queries.iter().map(|q| &q.zone_id).collect::<Vec<_>>(),
    );
    let mut zones: Vec<&str> = queries.iter().map(|q| q.zone_id.as_str()).collect();
    zones.sort();
    assert_eq!(zones, vec!["eng", "legal"]);

    // Fused results carry ONE hit per zone (mock stamps one per
    // request), both surviving the RRF fusion + bridge.
    let results = json["results"].as_array().expect("results");
    assert_eq!(results.len(), 2, "want 2 fused hits, got {results:?}");
    let mut paths: Vec<&str> = results
        .iter()
        .map(|r| r["path"].as_str().unwrap())
        .collect();
    paths.sort();
    assert_eq!(paths, vec!["/eng/hit.md", "/legal/hit.md"]);
    // Each hit's zone_id survives the bridge — federated dispatch
    // always names the source zone.
    for r in results {
        assert!(!r["zone_id"].as_str().unwrap().is_empty());
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn multi_zone_caller_with_explicit_zone_id_stays_single_zone() {
    // Same alice, same 2-zone grants, but wire body pins
    // `zone_id`.  The federated branch MUST NOT fire — the caller
    // explicitly narrowed the scope.  Regression pin: an over-eager
    // check that fanned out regardless would surface here.
    let (base, log) = spawn_harness(&["eng", "legal"]).await;

    let body = serde_json::json!({
        "q": "widget",
        "zone_id": "eng",
        "query_type": "keyword",
    });
    let resp = reqwest::Client::new()
        .post(format!("{base}/v2/search/query"))
        .bearer_auth("anything")
        .json(&body)
        .send()
        .await
        .expect("http");
    assert_eq!(resp.status().as_u16(), 200);

    // Exactly ONE Query call — the single-zone path — and it's the
    // zone the caller pinned.
    let queries = log.queries.lock().unwrap();
    assert_eq!(queries.len(), 1, "want single-zone dispatch");
    assert_eq!(queries[0].zone_id, "eng");
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn single_zone_caller_never_fans_out() {
    // alice has reader on ONE zone — accessible_zones.len() == 1,
    // no fanout condition, single-zone path.  Regression pin:
    // a check misreading "1" as "> 1" would fire the dispatcher
    // for a caller with a single grant.
    let (base, log) = spawn_harness(&["eng"]).await;

    let body = serde_json::json!({
        "q": "widget",
        "query_type": "keyword",
    });
    let resp = reqwest::Client::new()
        .post(format!("{base}/v2/search/query"))
        .bearer_auth("anything")
        .json(&body)
        .send()
        .await
        .expect("http");
    assert_eq!(resp.status().as_u16(), 200);

    let queries = log.queries.lock().unwrap();
    assert_eq!(queries.len(), 1, "want single-zone dispatch");
}
