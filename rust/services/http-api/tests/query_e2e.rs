//! Real-chain E2E cover for `POST /v2/search/query` — same posture
//! as `glob_e2e.rs` / `grep_e2e.rs`: mock gRPC server on ephemeral
//! port + axum listener pointed at it + reqwest hits the router
//! with a JSON body.
//!
//! Pins:
//!   * JSON body → proto mapping: every field on `QueryBody` reaches
//!     the corresponding `QueryRequest` field at the gRPC boundary,
//!     including the enum-shaped ones (`query_type` "hybrid" →
//!     `QueryType::Hybrid`; `fusion_method` "rrf_weighted" →
//!     `FusionMethod::RrfWeighted`) and the `path_prefix_boosts`
//!     map.
//!   * proto → HTTP mapping: `QueryResult`'s optional attribution
//!     fields (title_score, keyword_score, vector_score, tier_boost,
//!     recency_boost, expansion_variant_index) survive round-trip
//!     into JSON, with absent-when-None omission for compactness.
//!   * error mapping: `InvalidArgument` → 400; upstream-down → 503.
//!   * defaults: absent optional params fall through with proto
//!     defaults, not silently rewritten.

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
    QueryResult, RefreshRequest, RefreshResponse, RemoveIndexedDirectoryRequest,
    RemoveIndexedDirectoryResponse, SetZoneIndexingModeRequest, SetZoneIndexingModeResponse,
    StatsRequest, StatsResponse,
};
use nexus_http_api::{bind_and_serve, AppState, SearchBackend};
use tokio::net::TcpListener;
use tokio_stream::wrappers::TcpListenerStream;
use tonic::{Request, Response, Status};

// ── Mock SearchService ─────────────────────────────────────────

#[derive(Default, Clone)]
struct RequestLog {
    queries: Arc<Mutex<Vec<QueryRequest>>>,
}

#[derive(Clone)]
enum MockBehaviour {
    Success {
        results: Vec<QueryResult>,
        error: Option<String>,
    },
    /// Upstream returns a specific `tonic::Code`.  Lets a test pin
    /// the full `code → HTTP status` mapping table in
    /// `handlers/search.rs::grpc_status_to_http` without a
    /// per-variant enum blow-up here.  Subsumes the earlier
    /// `InvalidArgument(String)` variant (which was a strict subset
    /// of `RpcCode { code: tonic::Code::InvalidArgument, .. }`).
    RpcCode { code: tonic::Code, message: String },
}

#[derive(Clone)]
struct MockSearchService {
    log: RequestLog,
    behaviour: MockBehaviour,
}

#[tonic::async_trait]
impl SearchService for MockSearchService {
    async fn query(&self, req: Request<QueryRequest>) -> Result<Response<QueryResponse>, Status> {
        let inner = req.into_inner();
        self.log.queries.lock().unwrap().push(inner);
        match &self.behaviour {
            MockBehaviour::Success { results, error } => Ok(Response::new(QueryResponse {
                results: results.clone(),
                error: error.clone(),
            })),
            MockBehaviour::RpcCode { code, message } => Err(Status::new(*code, message.clone())),
        }
    }

    // Every other RPC unreachable — query handler never calls them.
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
    let state = AppState {
        search: SearchBackend::new(grpc_target),
        // NoAuth so this file's tests stay focused on the query
        // route surface; bearer parsing / rejection has its own
        // cover in `tests/auth_middleware_e2e.rs`.
        auth: nexus_http_api::middleware::auth::default_no_auth_provider(),
    };
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

fn full_hit() -> QueryResult {
    QueryResult {
        path: "/notes/a.md".to_string(),
        chunk_index: 3,
        chunk_text: "the widget hums along".to_string(),
        score: 0.87,
        zone_id: "root".to_string(),
        mtime_ms: Some(1_700_000_000_000),
        expanded_context: "prior chunk\n\nthe widget hums along\n\nfollowing chunk".to_string(),
        title_score: Some(4.5),
        keyword_score: Some(2.1),
        vector_score: Some(0.83),
        tier_boost: Some(1.5),
        recency_boost: Some(1.2),
        expansion_variant_index: Some(2),
    }
}

// ── Tests ──────────────────────────────────────────────────────

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn query_happy_path_round_trips_full_chain() {
    let h = Harness::start(MockBehaviour::Success {
        results: vec![full_hit()],
        error: None,
    })
    .await;

    let body = serde_json::json!({
        "q": "widget",
        "zone_id": "root",
        "limit": 15,
        "path_filter": "/notes",
        "query_type": "hybrid",
        "auth_token": "sk-test",
        "alpha": 0.6,
        "fusion_method": "rrf_weighted",
        "rrf_k": 42,
        "chunks_per_page": 3,
        "expand": "macro",
        "recency_mode": "auto",
        "recency_weight": 0.4,
        "recency_half_life_days": 7.0,
        "path_prefix_boosts": {"/notes/": 1.5, "/tmp/": 0.25},
    });
    let resp = reqwest::Client::new()
        .post(format!("{}/v2/search/query", h.http_base))
        .json(&body)
        .send()
        .await
        .expect("send");
    assert_eq!(resp.status(), reqwest::StatusCode::OK);

    let parsed: serde_json::Value = resp.json().await.expect("parse json");
    let hit = &parsed["results"][0];
    assert_eq!(hit["path"], "/notes/a.md");
    assert_eq!(hit["chunk_index"], 3);
    assert_eq!(hit["chunk_text"], "the widget hums along");
    assert_eq!(hit["score"], 0.87);
    assert_eq!(hit["zone_id"], "root");
    assert_eq!(hit["mtime_ms"], 1_700_000_000_000i64);
    assert!(hit["expanded_context"]
        .as_str()
        .unwrap()
        .contains("prior chunk"));
    assert_eq!(hit["title_score"], 4.5);
    assert_eq!(hit["keyword_score"], 2.1);
    assert_eq!(hit["vector_score"], 0.83);
    assert_eq!(hit["tier_boost"], 1.5);
    assert_eq!(hit["recency_boost"], 1.2);
    assert_eq!(hit["expansion_variant_index"], 2);
    assert!(parsed.get("error").is_none() || parsed["error"].is_null());

    // Handler → proto boundary — every JSON field reaches the RPC.
    let recorded = h.log.queries.lock().unwrap().clone();
    assert_eq!(recorded.len(), 1, "exactly one upstream Query call");
    let r = &recorded[0];
    assert_eq!(r.q, "widget");
    assert_eq!(r.zone_id, "root");
    assert_eq!(r.limit, 15);
    assert_eq!(r.path_filter, "/notes");
    assert_eq!(
        r.query_type,
        nexus_http_api::search_proto::QueryType::Hybrid as i32,
        "query_type=hybrid must map to proto Hybrid enum",
    );
    assert_eq!(r.auth_token, "sk-test");
    assert_eq!(r.alpha, 0.6);
    assert_eq!(
        r.fusion_method,
        nexus_http_api::search_proto::FusionMethod::RrfWeighted as i32,
        "fusion_method=rrf_weighted must map to proto RrfWeighted enum",
    );
    assert_eq!(r.rrf_k, 42);
    assert_eq!(r.chunks_per_page, 3);
    assert_eq!(r.expand, "macro");
    assert_eq!(r.recency_mode, "auto");
    assert_eq!(r.recency_weight, 0.4);
    assert_eq!(r.recency_half_life_days, 7.0);
    assert_eq!(r.path_prefix_boosts.get("/notes/"), Some(&1.5));
    assert_eq!(r.path_prefix_boosts.get("/tmp/"), Some(&0.25));
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn query_optional_attribution_fields_absent_when_none() {
    // A hit with every Option<f32>/mtime attribution field as None
    // must serialise WITHOUT those keys (skip_serializing_if =
    // Option::is_none), keeping the wire compact for the common
    // case where a boost / arm did not apply.
    let bare = QueryResult {
        path: "/x.md".to_string(),
        chunk_index: 0,
        chunk_text: "hello".to_string(),
        score: 1.0,
        zone_id: "root".to_string(),
        mtime_ms: None,
        expanded_context: String::new(),
        title_score: None,
        keyword_score: None,
        vector_score: None,
        tier_boost: None,
        recency_boost: None,
        expansion_variant_index: None,
    };
    let h = Harness::start(MockBehaviour::Success {
        results: vec![bare],
        error: None,
    })
    .await;
    let resp = reqwest::Client::new()
        .post(format!("{}/v2/search/query", h.http_base))
        .json(&serde_json::json!({"q": "hello"}))
        .send()
        .await
        .expect("send");
    assert_eq!(resp.status(), reqwest::StatusCode::OK);
    let parsed: serde_json::Value = resp.json().await.expect("parse json");
    let hit = &parsed["results"][0];
    for absent_key in [
        "mtime_ms",
        "title_score",
        "keyword_score",
        "vector_score",
        "tier_boost",
        "recency_boost",
        "expansion_variant_index",
    ] {
        assert!(
            hit.get(absent_key).is_none(),
            "{absent_key} must be absent from JSON when None (skip_serializing_if); got: {hit}",
        );
    }
    // expanded_context is also skip-serializing-if empty.
    assert!(
        hit.get("expanded_context").is_none(),
        "empty expanded_context must be absent",
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn query_defaults_reach_backend_intact() {
    // Absent optional fields (everything but `q`) must reach the
    // backend as their proto defaults, not silently rewritten.
    let h = Harness::start(MockBehaviour::Success {
        results: vec![],
        error: None,
    })
    .await;
    let resp = reqwest::Client::new()
        .post(format!("{}/v2/search/query", h.http_base))
        .json(&serde_json::json!({"q": "needle"}))
        .send()
        .await
        .expect("send");
    assert_eq!(resp.status(), reqwest::StatusCode::OK);

    let recorded = h.log.queries.lock().unwrap().clone();
    assert_eq!(recorded.len(), 1);
    let r = &recorded[0];
    assert_eq!(r.q, "needle");
    assert_eq!(r.zone_id, "");
    assert_eq!(r.limit, 0, "absent limit defaults to 0 (server default)");
    assert_eq!(r.path_filter, "");
    assert_eq!(
        r.query_type,
        nexus_http_api::search_proto::QueryType::Keyword as i32,
        "absent/empty query_type defaults to keyword",
    );
    assert_eq!(
        r.fusion_method,
        nexus_http_api::search_proto::FusionMethod::Rrf as i32,
        "absent/empty fusion_method defaults to RRF",
    );
    assert_eq!(r.rrf_k, 0);
    assert_eq!(r.chunks_per_page, 0);
    assert_eq!(r.expand, "");
    assert_eq!(r.recency_mode, "");
    assert_eq!(r.recency_weight, 0.0);
    assert_eq!(r.recency_half_life_days, 0.0);
    assert!(r.path_prefix_boosts.is_empty());
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn query_unknown_query_type_returns_400_and_never_reaches_backend() {
    // A typo like "semantik" must be REJECTED at the handler with
    // 400 + a caller-readable message.  Fail-loud is required — the
    // pre-fix silent fall-through to Keyword hid typos as "keyword
    // returned no hits" for weeks.  Empty / absent query_type still
    // defaults to Keyword (proto UNSPECIFIED posture) — that path
    // is pinned by `query_defaults_reach_backend_intact`.
    let h = Harness::start(MockBehaviour::Success {
        results: vec![],
        error: None,
    })
    .await;
    let resp = reqwest::Client::new()
        .post(format!("{}/v2/search/query", h.http_base))
        .json(&serde_json::json!({"q": "x", "query_type": "semantik"}))
        .send()
        .await
        .expect("send");
    assert_eq!(resp.status(), reqwest::StatusCode::BAD_REQUEST);
    let body: serde_json::Value = resp.json().await.expect("parse json");
    assert!(
        body["error"]
            .as_str()
            .unwrap_or_default()
            .contains("query_type"),
        "error body must name the offending field so the caller sees the typo; got: {body}",
    );
    assert!(
        h.log.queries.lock().unwrap().is_empty(),
        "upstream RPC must NOT fire for a caller-side field error",
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn query_query_type_parse_is_case_insensitive() {
    // "SEMANTIC" (proto enum-name casing) must also work — callers
    // eyeballing the proto and picking a value should not have to
    // memorise the lowercase-only wire convention.
    let h = Harness::start(MockBehaviour::Success {
        results: vec![],
        error: None,
    })
    .await;
    let resp = reqwest::Client::new()
        .post(format!("{}/v2/search/query", h.http_base))
        .json(&serde_json::json!({"q": "x", "query_type": "SEMANTIC"}))
        .send()
        .await
        .expect("send");
    assert_eq!(resp.status(), reqwest::StatusCode::OK);
    let recorded = h.log.queries.lock().unwrap().clone();
    assert_eq!(
        recorded[0].query_type,
        nexus_http_api::search_proto::QueryType::Semantic as i32,
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn query_unknown_fusion_method_returns_400_and_never_reaches_backend() {
    // Same fail-loud contract as query_type — empty defaults to RRF
    // (proto UNSPECIFIED), a typo returns 400 naming the field.
    let h = Harness::start(MockBehaviour::Success {
        results: vec![],
        error: None,
    })
    .await;
    let resp = reqwest::Client::new()
        .post(format!("{}/v2/search/query", h.http_base))
        .json(&serde_json::json!({"q": "x", "fusion_method": "rrrf"}))
        .send()
        .await
        .expect("send");
    assert_eq!(resp.status(), reqwest::StatusCode::BAD_REQUEST);
    let body: serde_json::Value = resp.json().await.expect("parse json");
    assert!(
        body["error"]
            .as_str()
            .unwrap_or_default()
            .contains("fusion_method"),
        "error body must name the offending field; got: {body}",
    );
    assert!(h.log.queries.lock().unwrap().is_empty());
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn query_upstream_unimplemented_maps_to_501() {
    // The proto SSOT declares Query returns Unimplemented for
    // query_type=Semantic/Hybrid in P1 (the vector path is not
    // wired yet).  Pre-fix that surfaced as HTTP 500 — pager-worthy
    // for a documented P1 signal.  Must map to 501 Not Implemented
    // so the caller distinguishes "not built yet" from "server
    // exploded".
    let h = Harness::start(MockBehaviour::RpcCode {
        code: tonic::Code::Unimplemented,
        message: "semantic search not wired in P1".to_string(),
    })
    .await;
    let resp = reqwest::Client::new()
        .post(format!("{}/v2/search/query", h.http_base))
        .json(&serde_json::json!({"q": "x", "query_type": "semantic"}))
        .send()
        .await
        .expect("send");
    assert_eq!(resp.status(), reqwest::StatusCode::NOT_IMPLEMENTED);
    let body: serde_json::Value = resp.json().await.expect("parse json");
    assert!(body["error"]
        .as_str()
        .unwrap_or_default()
        .contains("semantic"));
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn query_upstream_resource_exhausted_maps_to_429() {
    // Standard mapping: ResourceExhausted → 429 Too Many Requests
    // so caller-side retry logic ("back off + retry") kicks in
    // rather than treating the response as a server-broken 500.
    let h = Harness::start(MockBehaviour::RpcCode {
        code: tonic::Code::ResourceExhausted,
        message: "rate limit hit".to_string(),
    })
    .await;
    let resp = reqwest::Client::new()
        .post(format!("{}/v2/search/query", h.http_base))
        .json(&serde_json::json!({"q": "x"}))
        .send()
        .await
        .expect("send");
    assert_eq!(resp.status(), reqwest::StatusCode::TOO_MANY_REQUESTS);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn query_upstream_permission_denied_and_unauthenticated_map_correctly() {
    // Auth-shape codes surface as their HTTP equivalents:
    //   * Unauthenticated → 401 (caller needs a valid token)
    //   * PermissionDenied → 403 (caller has a token but not the grant)
    for (code, expected) in [
        (
            tonic::Code::Unauthenticated,
            reqwest::StatusCode::UNAUTHORIZED,
        ),
        (
            tonic::Code::PermissionDenied,
            reqwest::StatusCode::FORBIDDEN,
        ),
    ] {
        let h = Harness::start(MockBehaviour::RpcCode {
            code,
            message: format!("{code:?} test"),
        })
        .await;
        let resp = reqwest::Client::new()
            .post(format!("{}/v2/search/query", h.http_base))
            .json(&serde_json::json!({"q": "x"}))
            .send()
            .await
            .expect("send");
        assert_eq!(
            resp.status(),
            expected,
            "code {code:?} must map to {expected}"
        );
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn query_upstream_not_found_and_deadline_and_failed_precondition_map_correctly() {
    // The rest of the documented mapping table.  Batched into one
    // test to keep the file under 700 lines while still pinning
    // every explicit branch of `grpc_status_to_http`.
    for (code, expected) in [
        (tonic::Code::NotFound, reqwest::StatusCode::NOT_FOUND),
        (tonic::Code::AlreadyExists, reqwest::StatusCode::CONFLICT),
        (
            tonic::Code::FailedPrecondition,
            reqwest::StatusCode::PRECONDITION_FAILED,
        ),
        (tonic::Code::Aborted, reqwest::StatusCode::CONFLICT),
        (tonic::Code::OutOfRange, reqwest::StatusCode::BAD_REQUEST),
        (
            tonic::Code::DeadlineExceeded,
            reqwest::StatusCode::GATEWAY_TIMEOUT,
        ),
    ] {
        let h = Harness::start(MockBehaviour::RpcCode {
            code,
            message: format!("{code:?} test"),
        })
        .await;
        let resp = reqwest::Client::new()
            .post(format!("{}/v2/search/query", h.http_base))
            .json(&serde_json::json!({"q": "x"}))
            .send()
            .await
            .expect("send");
        assert_eq!(
            resp.status(),
            expected,
            "code {code:?} must map to {expected}"
        );
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn query_missing_body_returns_400() {
    // POST without a body (or with a body missing the required `q`
    // field) must be rejected before the mock sees it.
    let h = Harness::start(MockBehaviour::Success {
        results: vec![],
        error: None,
    })
    .await;
    let resp = reqwest::Client::new()
        .post(format!("{}/v2/search/query", h.http_base))
        .header("content-type", "application/json")
        .body("{}")
        .send()
        .await
        .expect("send");
    // axum Json extractor returns 422 for missing-required-field
    // deserialisation errors; matches the pattern the query
    // extractor uses for missing GET params (which returns 400).
    // Either is acceptable; assert on the 4xx family.
    assert!(
        resp.status().is_client_error(),
        "missing `q` must produce a 4xx, got {}",
        resp.status(),
    );
    assert!(
        h.log.queries.lock().unwrap().is_empty(),
        "upstream RPC must NOT fire on a rejected request",
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn query_upstream_invalid_argument_maps_to_400() {
    let h = Harness::start(MockBehaviour::RpcCode {
        code: tonic::Code::InvalidArgument,
        message: "bad recency_weight: out of range".to_string(),
    })
    .await;
    let resp = reqwest::Client::new()
        .post(format!("{}/v2/search/query", h.http_base))
        .json(&serde_json::json!({"q": "x", "recency_weight": 99.0}))
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
            .contains("recency_weight"),
        "error body must include upstream message; got: {body}",
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn query_upstream_down_maps_to_503() {
    let h = Harness::start_pointing_at_dead_backend().await;
    let resp = reqwest::Client::new()
        .post(format!("{}/v2/search/query", h.http_base))
        .json(&serde_json::json!({"q": "x"}))
        .send()
        .await
        .expect("send");
    assert_eq!(
        resp.status(),
        reqwest::StatusCode::SERVICE_UNAVAILABLE,
        "connection refused must surface as 503, not 500/hang",
    );
}
