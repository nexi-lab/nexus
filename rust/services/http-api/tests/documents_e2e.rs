//! Real-chain E2E cover for `POST /v2/documents/{index,refresh,
//! batch}` + `GET /v2/documents/stats` — same posture as
//! `query_e2e.rs`: mock gRPC server on ephemeral port + axum
//! listener pointed at it + reqwest hits the router.
//!
//! Pins the wire-shape mapping — HTTP JSON body / query params in,
//! proto request out; proto response back, HTTP JSON body out —
//! plus the shared error mapping table (`InvalidArgument → 400`,
//! `Unimplemented → 501`, upstream-down → 503) inherited from
//! `handlers::search::SearchError`.

use std::sync::{Arc, Mutex};

use nexus_http_api::search_proto::search_service_server::{SearchService, SearchServiceServer};
use nexus_http_api::search_proto::{
    AddIndexedDirectoryRequest, AddIndexedDirectoryResponse, BatchQueryRequest, BatchQueryResponse,
    GlobRequest, GlobResponse, GrepRequest, GrepResponse, HealthRequest, HealthResponse,
    IndexDocumentsRequest, IndexDocumentsResponse, IndexRequest, IndexResponse,
    ListIndexedDirectoriesRequest, ListIndexedDirectoriesResponse, ListZoneIndexingModesRequest,
    ListZoneIndexingModesResponse, LocateRequest, LocateResponse, NotifyFileChangeRequest,
    NotifyFileChangeResponse, ParkedDiscardRequest, ParkedDiscardResponse, ParkedListRequest,
    ParkedListResponse, ParkedRetryRequest, ParkedRetryResponse, QueryRequest, QueryResponse,
    RefreshRequest, RefreshResponse, RemoveIndexedDirectoryRequest, RemoveIndexedDirectoryResponse,
    SetZoneIndexingModeRequest, SetZoneIndexingModeResponse, StatsRequest, StatsResponse,
};
use nexus_http_api::{bind_and_serve, AppState};
use tokio::net::TcpListener;
use tokio_stream::wrappers::TcpListenerStream;
use tonic::{Request, Response, Status};

// ── Mock SearchService — records EVERY doc-side RPC so a test
// can prove which one the http-api routed to.  Each `Vec<_>` in
// the log is behind a `Mutex` so cross-thread appends stay sound
// on tokio's multi-thread runtime.

#[derive(Default, Clone)]
struct RequestLog {
    index: Arc<Mutex<Vec<IndexRequest>>>,
    refresh: Arc<Mutex<Vec<RefreshRequest>>>,
    index_documents: Arc<Mutex<Vec<IndexDocumentsRequest>>>,
    stats: Arc<Mutex<Vec<StatsRequest>>>,
}

/// Fixed responses the mock ships back — one per RPC.  Each test
/// picks the shape it wants; a test wanting a specific `Status`
/// wraps in `.await.map(|_| Err(...))` at a different layer.
#[derive(Clone)]
struct MockConfig {
    index_resp: IndexResponse,
    refresh_resp: RefreshResponse,
    index_documents_resp: IndexDocumentsResponse,
    stats_resp: StatsResponse,
    /// When Some, EVERY doc-side RPC returns this Status instead of
    /// its Ok response — lets a test pin the shared status mapper
    /// on this handler surface without a per-RPC variant.
    rpc_err: Option<tonic::Code>,
}

impl Default for MockConfig {
    fn default() -> Self {
        Self {
            index_resp: IndexResponse {
                indexed_count: 42,
                skipped_count: 3,
                error: None,
            },
            refresh_resp: RefreshResponse {
                reindexed_count: 5,
                removed_count: 1,
                unchanged_count: 100,
                skipped_count: 2,
                truncated: false,
                error: None,
            },
            index_documents_resp: IndexDocumentsResponse {
                indexed_count: 7,
                skipped_count: 0,
                parked_paths: vec![],
                error: None,
                index_seq: 11,
                skipped_paths: vec![],
            },
            stats_resp: StatsResponse {
                fts_doc_count: 1234,
                fts_path_count: 567,
                ann_chunk_count: 890,
                parked_count: 0,
                error: None,
                backend: "rust-plugin".into(),
                embedding_model: "mE5-small-v1".into(),
                indexing_in_progress: 0,
                last_index_seq: 11,
                pending: 0,
                last_successful_index_at_ms: 1_700_000_000_000,
            },
            rpc_err: None,
        }
    }
}

#[derive(Clone)]
struct MockSearchService {
    log: RequestLog,
    config: MockConfig,
}

macro_rules! err_or {
    ($self:expr, $ok:expr) => {
        if let Some(code) = $self.config.rpc_err {
            Err(Status::new(code, "mock injected"))
        } else {
            Ok(Response::new($ok))
        }
    };
}

#[tonic::async_trait]
impl SearchService for MockSearchService {
    async fn index(&self, req: Request<IndexRequest>) -> Result<Response<IndexResponse>, Status> {
        self.log.index.lock().unwrap().push(req.into_inner());
        err_or!(self, self.config.index_resp.clone())
    }

    async fn refresh(
        &self,
        req: Request<RefreshRequest>,
    ) -> Result<Response<RefreshResponse>, Status> {
        self.log.refresh.lock().unwrap().push(req.into_inner());
        err_or!(self, self.config.refresh_resp.clone())
    }

    async fn index_documents(
        &self,
        req: Request<IndexDocumentsRequest>,
    ) -> Result<Response<IndexDocumentsResponse>, Status> {
        self.log
            .index_documents
            .lock()
            .unwrap()
            .push(req.into_inner());
        err_or!(self, self.config.index_documents_resp.clone())
    }

    async fn stats(&self, req: Request<StatsRequest>) -> Result<Response<StatsResponse>, Status> {
        self.log.stats.lock().unwrap().push(req.into_inner());
        err_or!(self, self.config.stats_resp.clone())
    }

    // Every other RPC unreachable — documents handlers never call them.
    async fn glob(&self, _: Request<GlobRequest>) -> Result<Response<GlobResponse>, Status> {
        unreachable!()
    }
    async fn grep(&self, _: Request<GrepRequest>) -> Result<Response<GrepResponse>, Status> {
        unreachable!()
    }
    async fn query(&self, _: Request<QueryRequest>) -> Result<Response<QueryResponse>, Status> {
        unreachable!()
    }
    async fn batch_query(
        &self,
        _: Request<BatchQueryRequest>,
    ) -> Result<Response<BatchQueryResponse>, Status> {
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
}

// ── Harness ────────────────────────────────────────────────────

struct Harness {
    http_base: String,
    log: RequestLog,
}

impl Harness {
    async fn start(config: MockConfig) -> Self {
        let (grpc_target, log) = spawn_mock_grpc_server(config).await;
        let http_base = spawn_http_listener(grpc_target).await;
        Self { http_base, log }
    }

    async fn start_pointing_at_dead_backend() -> Self {
        let listener = TcpListener::bind("127.0.0.1:0")
            .await
            .expect("bind + release ephemeral port");
        let dead_addr = listener.local_addr().expect("dead addr");
        drop(listener);
        let http_base = spawn_http_listener(format!("http://{dead_addr}")).await;
        Self {
            http_base,
            log: RequestLog::default(),
        }
    }
}

async fn spawn_http_listener(grpc_target: String) -> String {
    // `for_tests`: NoAuth so this file focuses on the /v2/documents/*
    // route surface; bearer parsing / rejection has its own cover in
    // `tests/auth_middleware_e2e.rs`.
    let state = AppState::for_tests(grpc_target);
    let (http_addr, fut) = bind_and_serve("127.0.0.1:0".parse().unwrap(), state)
        .await
        .expect("bind http listener");
    tokio::spawn(async move {
        fut.await.expect("axum::serve");
    });
    format!("http://{http_addr}")
}

async fn spawn_mock_grpc_server(config: MockConfig) -> (String, RequestLog) {
    let listener = TcpListener::bind("127.0.0.1:0")
        .await
        .expect("bind grpc listener");
    let addr = listener.local_addr().expect("grpc addr");
    let log = RequestLog::default();
    let mock = MockSearchService {
        log: log.clone(),
        config,
    };
    tokio::spawn(async move {
        tonic::transport::Server::builder()
            .add_service(SearchServiceServer::new(mock))
            .serve_with_incoming(TcpListenerStream::new(listener))
            .await
            .expect("mock grpc serve");
    });
    (format!("http://{addr}"), log)
}

// ── Tests — /v2/documents/index ────────────────────────────────

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn index_happy_path_round_trips_full_chain() {
    let h = Harness::start(MockConfig::default()).await;
    let body = serde_json::json!({
        "root_path": "/notes",
        "zone_id": "root",
        "recursive": true,
        "max_docs": 500,
        "auth_token": "sk-test",
    });
    let resp = reqwest::Client::new()
        .post(format!("{}/v2/documents/index", h.http_base))
        .json(&body)
        .send()
        .await
        .expect("send");
    assert_eq!(resp.status(), reqwest::StatusCode::OK);
    let parsed: serde_json::Value = resp.json().await.expect("parse json");
    assert_eq!(parsed["indexed_count"], 42);
    assert_eq!(parsed["skipped_count"], 3);
    assert!(parsed.get("error").is_none() || parsed["error"].is_null());

    // Handler → proto boundary — every JSON field reaches the RPC.
    let recorded = h.log.index.lock().unwrap().clone();
    assert_eq!(recorded.len(), 1, "exactly one upstream Index call");
    let r = &recorded[0];
    assert_eq!(r.root_path, "/notes");
    assert_eq!(r.zone_id, "root");
    assert!(r.recursive);
    assert_eq!(r.max_docs, 500);
    assert_eq!(r.auth_token, "sk-test");
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn index_defaults_reach_backend_intact() {
    // Absent optional fields must land at the backend as their
    // proto defaults, not silently rewritten in the handler layer.
    let h = Harness::start(MockConfig::default()).await;
    let resp = reqwest::Client::new()
        .post(format!("{}/v2/documents/index", h.http_base))
        .json(&serde_json::json!({ "root_path": "/" }))
        .send()
        .await
        .expect("send");
    assert_eq!(resp.status(), reqwest::StatusCode::OK);
    let r = &h.log.index.lock().unwrap()[0];
    assert_eq!(r.root_path, "/");
    assert_eq!(r.zone_id, "");
    assert!(!r.recursive);
    assert_eq!(r.max_docs, 0);
    assert_eq!(r.auth_token, "");
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn index_upstream_unimplemented_maps_to_501() {
    // Shared `grpc_status_to_http` mapper — the reason we lifted it
    // to pub(crate).  Documents routes must speak the same wire
    // semantics as search routes.
    let h = Harness::start(MockConfig {
        rpc_err: Some(tonic::Code::Unimplemented),
        ..MockConfig::default()
    })
    .await;
    let resp = reqwest::Client::new()
        .post(format!("{}/v2/documents/index", h.http_base))
        .json(&serde_json::json!({ "root_path": "/" }))
        .send()
        .await
        .expect("send");
    assert_eq!(resp.status(), reqwest::StatusCode::NOT_IMPLEMENTED);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn index_upstream_down_maps_to_503() {
    let h = Harness::start_pointing_at_dead_backend().await;
    let resp = reqwest::Client::new()
        .post(format!("{}/v2/documents/index", h.http_base))
        .json(&serde_json::json!({ "root_path": "/" }))
        .send()
        .await
        .expect("send");
    assert_eq!(resp.status(), reqwest::StatusCode::SERVICE_UNAVAILABLE);
}

// ── Tests — /v2/documents/refresh ──────────────────────────────

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn refresh_happy_path_surfaces_full_counter_set() {
    // Refresh's counter shape distinguishes reindexed / removed /
    // unchanged / skipped — pin all four so a caller scripting on
    // this endpoint locks the wire contract.
    let h = Harness::start(MockConfig::default()).await;
    let resp = reqwest::Client::new()
        .post(format!("{}/v2/documents/refresh", h.http_base))
        .json(&serde_json::json!({
            "root_path": "/notes",
            "zone_id": "root",
            "recursive": false,
            "max_docs": 100,
        }))
        .send()
        .await
        .expect("send");
    assert_eq!(resp.status(), reqwest::StatusCode::OK);
    let parsed: serde_json::Value = resp.json().await.expect("parse json");
    assert_eq!(parsed["reindexed_count"], 5);
    assert_eq!(parsed["removed_count"], 1);
    assert_eq!(parsed["unchanged_count"], 100);
    assert_eq!(parsed["skipped_count"], 2);
    assert_eq!(parsed["truncated"], false);

    let r = &h.log.refresh.lock().unwrap()[0];
    assert_eq!(r.root_path, "/notes");
    assert_eq!(r.zone_id, "root");
    assert!(!r.recursive);
    assert_eq!(r.max_docs, 100);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn refresh_truncated_flag_rides_through_to_the_wire() {
    // Regression pin — `truncated` was originally a Refresh-only
    // field added late (review R4); a serde rename or accidental
    // omission would silently drop it.
    let h = Harness::start(MockConfig {
        refresh_resp: RefreshResponse {
            reindexed_count: 10,
            removed_count: 0,
            unchanged_count: 0,
            skipped_count: 0,
            truncated: true,
            error: None,
        },
        ..MockConfig::default()
    })
    .await;
    let resp = reqwest::Client::new()
        .post(format!("{}/v2/documents/refresh", h.http_base))
        .json(&serde_json::json!({ "root_path": "/" }))
        .send()
        .await
        .expect("send");
    let parsed: serde_json::Value = resp.json().await.expect("parse json");
    assert_eq!(parsed["truncated"], true);
    assert_eq!(parsed["reindexed_count"], 10);
}

// ── Tests — /v2/documents/batch ────────────────────────────────

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn batch_happy_path_round_trips_documents_list() {
    // Every field on each DocumentInput reaches the corresponding
    // proto slot — including the optional mtime_ms + per-doc
    // zone_id override.
    let h = Harness::start(MockConfig::default()).await;
    let body = serde_json::json!({
        "documents": [
            {
                "path": "/notes/a.md",
                "text": "the widget hums",
                "mtime_ms": 1_700_000_000_000_i64,
                "zone_id": "shared",
            },
            {
                "path": "/notes/b.md",
                "text": "the beacon glows",
            },
        ],
        "zone_id": "root",
        "auth_token": "sk-test",
    });
    let resp = reqwest::Client::new()
        .post(format!("{}/v2/documents/batch", h.http_base))
        .json(&body)
        .send()
        .await
        .expect("send");
    assert_eq!(resp.status(), reqwest::StatusCode::OK);
    let parsed: serde_json::Value = resp.json().await.expect("parse json");
    assert_eq!(parsed["indexed_count"], 7);
    assert_eq!(parsed["skipped_count"], 0);
    assert_eq!(parsed["parked_paths"].as_array().unwrap().len(), 0);
    // #4736: the plugin's sequence + per-path skips ride along.
    assert_eq!(parsed["index_seq"], 11);
    assert_eq!(parsed["skipped_paths"].as_array().unwrap().len(), 0);

    let r = &h.log.index_documents.lock().unwrap()[0];
    assert_eq!(r.documents.len(), 2);
    assert_eq!(r.documents[0].path, "/notes/a.md");
    assert_eq!(r.documents[0].text, "the widget hums");
    assert_eq!(r.documents[0].mtime_ms, Some(1_700_000_000_000));
    assert_eq!(r.documents[0].zone_id, "shared");
    assert_eq!(r.documents[1].path, "/notes/b.md");
    assert_eq!(
        r.documents[1].mtime_ms, None,
        "absent optional mtime_ms must reach the backend as None, not 0",
    );
    assert_eq!(
        r.documents[1].zone_id, "",
        "absent per-doc zone_id must reach as empty (falls back at plugin layer)",
    );
    assert_eq!(r.zone_id, "root");
    assert_eq!(r.auth_token, "sk-test");
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn batch_parked_paths_survive_round_trip() {
    // A partial-failure response surfaces `parked_paths` — a caller
    // gating on retry logic reads this list to schedule follow-ups.
    let h = Harness::start(MockConfig {
        index_documents_resp: IndexDocumentsResponse {
            indexed_count: 2,
            skipped_count: 0,
            parked_paths: vec!["/notes/a.md".into(), "/notes/c.md".into()],
            error: None,
            index_seq: 12,
            skipped_paths: vec![],
        },
        ..MockConfig::default()
    })
    .await;
    let resp = reqwest::Client::new()
        .post(format!("{}/v2/documents/batch", h.http_base))
        .json(&serde_json::json!({
            "documents": [{"path": "/x", "text": "x"}],
        }))
        .send()
        .await
        .expect("send");
    let parsed: serde_json::Value = resp.json().await.expect("parse json");
    let parked = parsed["parked_paths"].as_array().expect("parked array");
    assert_eq!(parked.len(), 2);
    assert_eq!(parked[0], "/notes/a.md");
    assert_eq!(parked[1], "/notes/c.md");
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn batch_missing_documents_body_returns_4xx() {
    // POST without a body must be rejected before the RPC fires.
    let h = Harness::start(MockConfig::default()).await;
    let resp = reqwest::Client::new()
        .post(format!("{}/v2/documents/batch", h.http_base))
        .header("content-type", "application/json")
        .body("{}")
        .send()
        .await
        .expect("send");
    assert!(
        resp.status().is_client_error(),
        "missing `documents` must produce a 4xx, got {}",
        resp.status(),
    );
    assert!(
        h.log.index_documents.lock().unwrap().is_empty(),
        "upstream RPC must NOT fire on a rejected request",
    );
}

// ── Tests — /v2/documents/stats ────────────────────────────────

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn stats_happy_path_surfaces_backend_identity_and_counters() {
    // Backend-identity fields (`backend`, `embedding_model`) survive
    // the pivot — health pollers scripted around them keep working.
    let h = Harness::start(MockConfig::default()).await;
    let resp = reqwest::Client::new()
        .get(format!("{}/v2/documents/stats?zone_id=root", h.http_base))
        .send()
        .await
        .expect("send");
    assert_eq!(resp.status(), reqwest::StatusCode::OK);
    let parsed: serde_json::Value = resp.json().await.expect("parse json");
    assert_eq!(parsed["fts_doc_count"], 1234);
    assert_eq!(parsed["fts_path_count"], 567);
    assert_eq!(parsed["ann_chunk_count"], 890);
    assert_eq!(parsed["parked_count"], 0);
    assert_eq!(parsed["backend"], "rust-plugin");
    assert_eq!(parsed["embedding_model"], "mE5-small-v1");
    assert_eq!(parsed["indexing_in_progress"], 0);
    // #4736 stall-detection triple.
    assert_eq!(parsed["last_index_seq"], 11);
    assert_eq!(parsed["pending"], 0);
    assert_eq!(parsed["last_successful_index_at_ms"], 1_700_000_000_000u64);

    let r = &h.log.stats.lock().unwrap()[0];
    assert_eq!(r.zone_id, "root");
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn stats_query_defaults_reach_backend_intact() {
    // `zone_id` param omitted ⇒ backend receives empty string
    // (falls back to ROOT_ZONE_ID at the plugin layer).
    let h = Harness::start(MockConfig::default()).await;
    let resp = reqwest::Client::new()
        .get(format!("{}/v2/documents/stats", h.http_base))
        .send()
        .await
        .expect("send");
    assert_eq!(resp.status(), reqwest::StatusCode::OK);
    let r = &h.log.stats.lock().unwrap()[0];
    assert_eq!(r.zone_id, "");
    assert_eq!(r.auth_token, "");
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn stats_upstream_permission_denied_maps_to_403() {
    // Round out the shared mapper cover — a real ApiKeyAuthProvider
    // will emit PermissionDenied when a caller queries a zone they
    // lack access to.  Stats is a natural surface for this.
    let h = Harness::start(MockConfig {
        rpc_err: Some(tonic::Code::PermissionDenied),
        ..MockConfig::default()
    })
    .await;
    let resp = reqwest::Client::new()
        .get(format!("{}/v2/documents/stats", h.http_base))
        .send()
        .await
        .expect("send");
    assert_eq!(resp.status(), reqwest::StatusCode::FORBIDDEN);
}
