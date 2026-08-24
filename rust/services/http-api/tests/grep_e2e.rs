//! Real-chain E2E cover for `GET /v2/search/grep` — same posture
//! as `glob_e2e.rs`: mock gRPC server on ephemeral port + axum
//! listener pointed at it + reqwest hits the router.
//!
//! Pins:
//!   * handler → proto mapping: HTTP query params translate to the
//!     right [`GrepRequest`] fields at the gRPC boundary (checked
//!     via the mock's request log)
//!   * proto → HTTP mapping: [`GrepResponse.matches`] round-trips
//!     with `line_number`, `line`, `before`, `after` intact
//!   * error mapping: `InvalidArgument` → 400, upstream-down → 503
//!   * defaults: absent `root_path` → `/`; absent numeric knobs → 0

use std::sync::{Arc, Mutex};

use nexus_http_api::search_proto::search_service_server::{SearchService, SearchServiceServer};
use nexus_http_api::search_proto::{
    AddIndexedDirectoryRequest, AddIndexedDirectoryResponse, BatchQueryRequest, BatchQueryResponse,
    GlobRequest, GlobResponse, GrepMatch, GrepRequest, GrepResponse, HealthRequest, HealthResponse,
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

// ── Mock SearchService ─────────────────────────────────────────

#[derive(Default, Clone)]
struct RequestLog {
    greps: Arc<Mutex<Vec<GrepRequest>>>,
}

#[derive(Clone)]
enum MockBehaviour {
    Success {
        matches: Vec<GrepMatch>,
        truncated: bool,
        error: Option<String>,
    },
    InvalidArgument(String),
}

#[derive(Clone)]
struct MockSearchService {
    log: RequestLog,
    behaviour: MockBehaviour,
}

#[tonic::async_trait]
impl SearchService for MockSearchService {
    async fn grep(&self, req: Request<GrepRequest>) -> Result<Response<GrepResponse>, Status> {
        let inner = req.into_inner();
        self.log.greps.lock().unwrap().push(inner);
        match &self.behaviour {
            MockBehaviour::Success {
                matches,
                truncated,
                error,
            } => Ok(Response::new(GrepResponse {
                matches: matches.clone(),
                truncated: *truncated,
                error: error.clone(),
            })),
            MockBehaviour::InvalidArgument(msg) => Err(Status::invalid_argument(msg.clone())),
        }
    }

    // Every other RPC must be unreachable — grep handler never calls them.
    async fn glob(&self, _: Request<GlobRequest>) -> Result<Response<GlobResponse>, Status> {
        unreachable!("grep handler must not call glob")
    }
    async fn query(&self, _: Request<QueryRequest>) -> Result<Response<QueryResponse>, Status> {
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

// ── Harness ────────────────────────────────────────────────────

struct Harness {
    http_base: String,
    log: RequestLog,
}

impl Harness {
    async fn start(behaviour: MockBehaviour) -> Self {
        let (grpc_target, log) = spawn_mock_grpc_server(behaviour).await;
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
    // `for_tests`: NoAuth so this file focuses on the grep route
    // surface (bearer cover lives in `tests/auth_middleware_e2e.rs`).
    let state = AppState::for_tests(grpc_target);
    let (http_addr, fut) = bind_and_serve("127.0.0.1:0".parse().unwrap(), state)
        .await
        .expect("bind http listener");
    tokio::spawn(async move {
        fut.await.expect("axum::serve");
    });
    format!("http://{http_addr}")
}

async fn spawn_mock_grpc_server(behaviour: MockBehaviour) -> (String, RequestLog) {
    let listener = TcpListener::bind("127.0.0.1:0")
        .await
        .expect("bind grpc listener");
    let addr = listener.local_addr().expect("grpc addr");
    let log = RequestLog::default();
    let mock = MockSearchService {
        log: log.clone(),
        behaviour,
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

// ── Tests ──────────────────────────────────────────────────────

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn grep_happy_path_round_trips_full_chain() {
    let h = Harness::start(MockBehaviour::Success {
        matches: vec![
            GrepMatch {
                path: "/notes/a.md".to_string(),
                line_number: 12,
                line: "the widget hums along".to_string(),
                before: vec!["previous line".to_string()],
                after: vec!["following line".to_string()],
            },
            GrepMatch {
                path: "/logs/x.log".to_string(),
                line_number: 4,
                line: "widget assembled".to_string(),
                before: vec![],
                after: vec![],
            },
        ],
        truncated: false,
        error: None,
    })
    .await;

    let resp = reqwest::Client::new()
        .get(format!(
            "{}/v2/search/grep?root_path=/&pattern=widget&file_pattern=*.md&ignore_case=true&max_results=50&before_context=1&after_context=1&sort_recency=true",
            h.http_base
        ))
        .send()
        .await
        .expect("send");
    assert_eq!(resp.status(), reqwest::StatusCode::OK);

    let body: serde_json::Value = resp.json().await.expect("parse json");
    assert_eq!(body["matches"].as_array().unwrap().len(), 2);
    assert_eq!(body["matches"][0]["path"], "/notes/a.md");
    assert_eq!(body["matches"][0]["line_number"], 12);
    assert_eq!(body["matches"][0]["line"], "the widget hums along");
    assert_eq!(
        body["matches"][0]["before"],
        serde_json::json!(["previous line"]),
    );
    assert_eq!(
        body["matches"][0]["after"],
        serde_json::json!(["following line"]),
    );
    assert_eq!(body["truncated"], serde_json::Value::Bool(false));
    assert!(body.get("error").is_none() || body["error"].is_null());

    // Handler → proto boundary: every field must reach the backend.
    let recorded = h.log.greps.lock().unwrap().clone();
    assert_eq!(recorded.len(), 1, "exactly one upstream Grep call");
    let r = &recorded[0];
    assert_eq!(r.root_path, "/");
    assert_eq!(r.pattern, "widget");
    assert_eq!(r.file_pattern, "*.md");
    assert!(r.ignore_case);
    assert_eq!(r.max_results, 50);
    assert_eq!(r.before_context, 1);
    assert_eq!(r.after_context, 1);
    assert!(r.sort_recency);
    assert!(
        !r.invert_match,
        "invert_match defaults to false when absent"
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn grep_missing_pattern_returns_400() {
    let h = Harness::start(MockBehaviour::Success {
        matches: vec![],
        truncated: false,
        error: None,
    })
    .await;

    let resp = reqwest::Client::new()
        .get(format!("{}/v2/search/grep?root_path=/", h.http_base))
        .send()
        .await
        .expect("send");
    assert_eq!(
        resp.status(),
        reqwest::StatusCode::BAD_REQUEST,
        "missing required `pattern` must not reach the backend",
    );
    assert!(
        h.log.greps.lock().unwrap().is_empty(),
        "upstream RPC must NOT fire on a rejected request",
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn grep_upstream_invalid_argument_maps_to_400() {
    let h = Harness::start(MockBehaviour::InvalidArgument(
        "bad regex: unclosed character class".to_string(),
    ))
    .await;

    let resp = reqwest::Client::new()
        .get(format!(
            "{}/v2/search/grep?root_path=/&pattern=%5Bunclosed",
            h.http_base
        ))
        .send()
        .await
        .expect("send");
    assert_eq!(
        resp.status(),
        reqwest::StatusCode::BAD_REQUEST,
        "tonic InvalidArgument must map to HTTP 400, not 500",
    );
    let body: serde_json::Value = resp.json().await.expect("parse json");
    assert!(
        body["error"]
            .as_str()
            .unwrap_or_default()
            .contains("unclosed"),
        "error body must include upstream message; got: {body}",
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn grep_upstream_down_maps_to_503() {
    let h = Harness::start_pointing_at_dead_backend().await;
    let resp = reqwest::Client::new()
        .get(format!(
            "{}/v2/search/grep?root_path=/&pattern=widget",
            h.http_base
        ))
        .send()
        .await
        .expect("send");
    assert_eq!(
        resp.status(),
        reqwest::StatusCode::SERVICE_UNAVAILABLE,
        "connection refused must surface as 503, not 500 or hang",
    );
    let body: serde_json::Value = resp.json().await.expect("parse json");
    assert!(
        body["error"].as_str().is_some(),
        "503 body must carry an error message for operators; got: {body}",
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn grep_defaults_reach_backend_intact() {
    // Absent optional params: root_path defaults to `/`; every
    // numeric knob defaults to 0 (backend interprets 0 as "server
    // default" for cap fields).  Verify the handler doesn't
    // silently rewrite any of them.
    let h = Harness::start(MockBehaviour::Success {
        matches: vec![],
        truncated: false,
        error: None,
    })
    .await;

    let resp = reqwest::Client::new()
        .get(format!("{}/v2/search/grep?pattern=needle", h.http_base))
        .send()
        .await
        .expect("send");
    assert_eq!(resp.status(), reqwest::StatusCode::OK);

    let recorded = h.log.greps.lock().unwrap().clone();
    assert_eq!(recorded.len(), 1);
    let r = &recorded[0];
    assert_eq!(r.root_path, "/", "absent root_path defaults to '/'");
    assert_eq!(r.pattern, "needle");
    assert_eq!(r.file_pattern, "");
    assert!(!r.ignore_case);
    assert_eq!(
        r.max_results, 0,
        "absent max_results defaults to 0 (server default)"
    );
    assert_eq!(r.before_context, 0);
    assert_eq!(r.after_context, 0);
    assert!(!r.invert_match);
    assert!(!r.sort_recency);
}
