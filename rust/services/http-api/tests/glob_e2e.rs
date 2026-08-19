//! Real-chain E2E cover for `GET /v2/search/glob` — exercises
//!
//!   reqwest client → axum listener → tonic client → gRPC server
//!
//! end-to-end.  The gRPC "server" is an in-process
//! [`nexus_search_plugin_proto::SearchService`] implementation
//! that returns canned data; the axum listener runs the real
//! [`nexus_http_api::router`] against a [`SearchBackend`]
//! pointed at the mock server's port.
//!
//! Pins:
//!   * router → backend dial: `SearchBackend` actually reaches the
//!     configured target and gets a routable Channel.
//!   * handler → proto mapping: HTTP query params translate to the
//!     right [`GlobRequest`] fields at the gRPC boundary.
//!   * proto → HTTP mapping: [`GlobResponse`] fields round-trip
//!     into the JSON body callers observe.
//!   * error mapping: an `InvalidArgument` from the upstream
//!     surfaces as HTTP 400, not 500.

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
use nexus_http_api::{router, AppState, SearchBackend};
use tokio::net::TcpListener;
use tokio_stream::wrappers::TcpListenerStream;
use tonic::{Request, Response, Status};

// ── Mock SearchService ──────────────────────────────────────────
//
// Only `Glob` has real logic; every other RPC is `unimplemented!`
// because the glob handler never touches them.  The mock records
// each incoming request so tests can assert the handler mapped
// query params correctly at the gRPC boundary.

#[derive(Default, Clone)]
struct RequestLog {
    globs: Arc<Mutex<Vec<GlobRequest>>>,
}

#[derive(Clone)]
enum MockBehaviour {
    Success {
        paths: Vec<String>,
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
    async fn glob(&self, req: Request<GlobRequest>) -> Result<Response<GlobResponse>, Status> {
        let inner = req.into_inner();
        self.log.globs.lock().unwrap().push(inner);
        match &self.behaviour {
            MockBehaviour::Success {
                paths,
                truncated,
                error,
            } => Ok(Response::new(GlobResponse {
                paths: paths.clone(),
                truncated: *truncated,
                error: error.clone(),
            })),
            MockBehaviour::InvalidArgument(msg) => Err(Status::invalid_argument(msg.clone())),
        }
    }

    // ── unused RPCs — every other method is unreachable from the
    // glob handler; loud panic beats silent stub if a regression
    // rewires. ──────────────────────────────────────────────────

    async fn grep(&self, _: Request<GrepRequest>) -> Result<Response<GrepResponse>, Status> {
        unreachable!("glob handler must not call grep")
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

// ── Harness ─────────────────────────────────────────────────────

struct Harness {
    http_base: String,
    log: RequestLog,
}

impl Harness {
    async fn start(behaviour: MockBehaviour) -> Self {
        // 1. Mock gRPC server on ephemeral port.
        let grpc_listener = TcpListener::bind("127.0.0.1:0")
            .await
            .expect("bind grpc listener");
        let grpc_addr = grpc_listener.local_addr().expect("grpc addr");
        let grpc_target = format!("http://{grpc_addr}");
        let log = RequestLog::default();
        let mock = MockSearchService {
            log: log.clone(),
            behaviour,
        };
        tokio::spawn(async move {
            tonic::transport::Server::builder()
                .add_service(SearchServiceServer::new(mock))
                .serve_with_incoming(TcpListenerStream::new(grpc_listener))
                .await
                .expect("mock grpc serve");
        });

        // 2. axum listener with SearchBackend pointed at the mock.
        let http_listener = TcpListener::bind("127.0.0.1:0")
            .await
            .expect("bind http listener");
        let http_addr = http_listener.local_addr().expect("http addr");
        let state = AppState {
            search: SearchBackend::new(grpc_target),
        };
        tokio::spawn(async move {
            axum::serve(http_listener, router(state))
                .await
                .expect("axum::serve");
        });

        Self {
            http_base: format!("http://{http_addr}"),
            log,
        }
    }
}

// ── Tests ───────────────────────────────────────────────────────

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn glob_happy_path_round_trips_full_chain() {
    let h = Harness::start(MockBehaviour::Success {
        paths: vec!["/a.rs".to_string(), "/nested/b.rs".to_string()],
        truncated: false,
        error: None,
    })
    .await;

    let resp = reqwest::Client::new()
        .get(format!(
            "{}/v2/search/glob?root_path=/&pattern=*.rs&max_results=10&sort_recency=true",
            h.http_base
        ))
        .send()
        .await
        .expect("send");
    assert_eq!(resp.status(), reqwest::StatusCode::OK);

    let body: serde_json::Value = resp.json().await.expect("parse json");
    assert_eq!(
        body["paths"],
        serde_json::json!(["/a.rs", "/nested/b.rs"]),
        "paths must round-trip verbatim",
    );
    assert_eq!(body["truncated"], serde_json::Value::Bool(false));
    // `error` is skip_serializing_if=None on GlobResponse — absent
    // rather than JSON null so the callers reading serde_json's
    // `get("error")` see `None` in the happy path.
    assert!(body.get("error").is_none() || body["error"].is_null());

    // Handler-to-proto boundary — verify every query param mapped
    // to the corresponding GlobRequest field.
    let recorded = h.log.globs.lock().unwrap().clone();
    assert_eq!(recorded.len(), 1, "exactly one upstream Glob call");
    assert_eq!(recorded[0].root_path, "/");
    assert_eq!(recorded[0].pattern, "*.rs");
    assert_eq!(recorded[0].max_results, 10);
    assert!(recorded[0].sort_recency);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn glob_missing_pattern_returns_400() {
    let h = Harness::start(MockBehaviour::Success {
        paths: vec![],
        truncated: false,
        error: None,
    })
    .await;

    let resp = reqwest::Client::new()
        .get(format!("{}/v2/search/glob?root_path=/", h.http_base))
        .send()
        .await
        .expect("send");
    // axum's Query extractor rejects the request with 400 when a
    // required param is absent (pattern has no default).  The
    // mock never sees the RPC.
    assert_eq!(
        resp.status(),
        reqwest::StatusCode::BAD_REQUEST,
        "missing required pattern must not reach the backend",
    );
    assert!(
        h.log.globs.lock().unwrap().is_empty(),
        "upstream RPC must NOT fire on a rejected request",
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn glob_upstream_invalid_argument_maps_to_400() {
    let h = Harness::start(MockBehaviour::InvalidArgument(
        "bad pattern: unbalanced bracket".to_string(),
    ))
    .await;

    let resp = reqwest::Client::new()
        .get(format!(
            "{}/v2/search/glob?root_path=/&pattern=%5Bunbalanced",
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

    // Body carries the upstream message so operators can debug.
    let body: serde_json::Value = resp.json().await.expect("parse json");
    assert!(
        body["error"]
            .as_str()
            .unwrap_or_default()
            .contains("unbalanced"),
        "error body must include upstream message; got: {body}",
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn glob_defaults_root_path_to_slash_when_absent() {
    // root_path has serde default = "/"; pattern is required.
    let h = Harness::start(MockBehaviour::Success {
        paths: vec![],
        truncated: false,
        error: None,
    })
    .await;

    let resp = reqwest::Client::new()
        .get(format!("{}/v2/search/glob?pattern=*.md", h.http_base))
        .send()
        .await
        .expect("send");
    assert_eq!(resp.status(), reqwest::StatusCode::OK);

    let recorded = h.log.globs.lock().unwrap().clone();
    assert_eq!(recorded.len(), 1);
    assert_eq!(
        recorded[0].root_path, "/",
        "absent root_path must default to '/'",
    );
    assert_eq!(recorded[0].pattern, "*.md");
}
