//! Route-level E2E cover for `middleware::auth::require_bearer`.
//!
//! Sibling tests (`glob_e2e.rs`, `grep_e2e.rs`, `query_e2e.rs`) wire
//! `NoAuth` so their assertions stay focused on the search chain;
//! this file wires a NON-`NoAuth` provider and exercises the auth
//! posture through the same real-TCP + reqwest + mock-tonic stack.
//!
//! Pins:
//!   * `/v2/status` remains PUBLIC even under a strict provider
//!     (liveness probes must not require a bearer).
//!   * `/v2/search/*` REJECTS every request without a well-formed
//!     bearer (401) — the upstream mock never sees the request.
//!   * A provider that accepts the bearer lets the search request
//!     through to the mock (200).
//!   * A provider that rejects the bearer surfaces 401 with the
//!     upstream `tonic::Status::message()` in the JSON body.

use std::sync::Arc;

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
    RefreshRequest, RefreshResponse, RemoveIndexedDirectoryRequest, RemoveIndexedDirectoryResponse,
    SetZoneIndexingModeRequest, SetZoneIndexingModeResponse, StatsRequest, StatsResponse,
};
use nexus_http_api::{bind_and_serve, AppState, SearchBackend};
use tokio::net::TcpListener;
use tokio::sync::Mutex;
use tokio_stream::wrappers::TcpListenerStream;
use tonic::{Request, Response, Status};
use transport::auth::{AuthCredentials, AuthProvider};

// ── Auth providers used by this file ──────────────────────────

/// Accepts one specific bearer, rejects everything else.  Enough
/// to exercise both branches of the middleware; concrete
/// `ApiKeyAuthProvider` cover lives in the transport crate's own
/// tests.
struct FixedBearer {
    accepted: &'static str,
}

impl AuthProvider for FixedBearer {
    fn resolve(&self, creds: &AuthCredentials<'_>) -> Result<OperationContext, tonic::Status> {
        if creds.token == self.accepted {
            Ok(OperationContext::new(
                "test-user",
                "root",
                true,
                None,
                false,
            ))
        } else {
            Err(tonic::Status::unauthenticated("wrong-token"))
        }
    }
}

// ── Mock SearchService — records glob hits so the test can
// prove the middleware DID or DID NOT forward the request.

#[derive(Default, Clone)]
struct RequestLog {
    globs: Arc<Mutex<Vec<GlobRequest>>>,
}

#[derive(Clone)]
struct MockSearchService {
    log: RequestLog,
}

#[tonic::async_trait]
impl SearchService for MockSearchService {
    async fn glob(&self, req: Request<GlobRequest>) -> Result<Response<GlobResponse>, Status> {
        self.log.globs.lock().await.push(req.into_inner());
        Ok(Response::new(GlobResponse {
            paths: vec!["/hit.md".into()],
            truncated: false,
            error: None,
        }))
    }

    async fn grep(&self, _: Request<GrepRequest>) -> Result<Response<GrepResponse>, Status> {
        unreachable!()
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
    async fn start(auth: Arc<dyn AuthProvider>) -> Self {
        let (grpc_target, log) = spawn_mock_grpc().await;
        let state = AppState {
            search: SearchBackend::new(grpc_target),
            auth,
            // Empty in-memory `AuthKeyStore` — this middleware
            // suite never hits `/v2/auth/keys`, so any store is fine.
            auth_key_store: std::sync::Arc::new(
                nexus_http_api::middleware::auth::empty_auth_key_store_for_tests(),
            ),
            // Under `--features rebac` (nexusd's `full` build path
            // exercises this harness transitively via CI), AppState
            // gains a `rebac_store` field.  Use the same in-memory
            // default the `for_tests` helper wires — this middleware
            // suite never hits `/v2/rebac/*`, so any store is fine.
            #[cfg(feature = "rebac")]
            rebac_store: std::sync::Arc::new(nexus_rebac::InMemoryReBACTupleStore::new()),
        };
        let (addr, fut) = bind_and_serve("127.0.0.1:0".parse().unwrap(), state)
            .await
            .expect("bind");
        tokio::spawn(async move {
            fut.await.expect("serve");
        });
        Self {
            http_base: format!("http://{addr}"),
            log,
        }
    }
}

async fn spawn_mock_grpc() -> (String, RequestLog) {
    let listener = TcpListener::bind("127.0.0.1:0").await.expect("bind");
    let addr = listener.local_addr().unwrap();
    let log = RequestLog::default();
    let mock = MockSearchService { log: log.clone() };
    tokio::spawn(async move {
        tonic::transport::Server::builder()
            .add_service(SearchServiceServer::new(mock))
            .serve_with_incoming(TcpListenerStream::new(listener))
            .await
            .expect("serve mock");
    });
    (format!("http://{addr}"), log)
}

// ── Tests ──────────────────────────────────────────────────────

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn public_status_bypasses_auth_even_under_strict_provider() {
    // A liveness probe MUST reach /v2/status without a bearer,
    // even under a provider that would reject one — the router's
    // sub-router split (public / protected) is what makes this
    // possible.
    let h = Harness::start(Arc::new(FixedBearer {
        accepted: "sk-never-sent",
    }))
    .await;
    let resp = reqwest::get(format!("{}/v2/status", h.http_base))
        .await
        .expect("get");
    assert_eq!(
        resp.status(),
        reqwest::StatusCode::OK,
        "public status route must bypass auth middleware",
    );
    let body: serde_json::Value = resp.json().await.expect("json");
    assert_eq!(body["status"], "ok");
    assert!(
        h.log.globs.lock().await.is_empty(),
        "status must never dial the search backend",
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn search_glob_without_bearer_under_strict_provider_returns_401_and_never_dials_upstream() {
    // Absent header → middleware calls `resolve` with empty token
    // → FixedBearer rejects → 401.  The 401 comes from the PROVIDER,
    // not the middleware parser (see `require_bearer_absent_header_
    // defers_to_provider_reject_becomes_401` in the lib unit tests).
    // Contrast: sibling `glob_e2e.rs` uses NoAuth and lets absent-
    // header requests through; the auth posture is a provider choice.
    let h = Harness::start(Arc::new(FixedBearer {
        accepted: "sk-anything",
    }))
    .await;
    let resp = reqwest::get(format!("{}/v2/search/glob?pattern=%2A.md", h.http_base))
        .await
        .expect("get");
    assert_eq!(resp.status(), reqwest::StatusCode::UNAUTHORIZED);
    let body: serde_json::Value = resp.json().await.expect("json");
    // Message flows through from `tonic::Status::message()` on the
    // provider's rejection — pin the exact upstream signal so an
    // operator debugging a 401 can distinguish provider-side reject
    // ("wrong-token") from middleware-side reject ("malformed…").
    assert!(
        body["error"].as_str().unwrap_or("").contains("wrong-token"),
        "provider message must ride through into JSON body; got: {body}",
    );
    assert!(
        h.log.globs.lock().await.is_empty(),
        "rejected bearer must NOT reach the backend",
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn search_glob_with_wrong_bearer_returns_401_from_provider_message() {
    let h = Harness::start(Arc::new(FixedBearer {
        accepted: "sk-real-token",
    }))
    .await;
    let resp = reqwest::Client::new()
        .get(format!("{}/v2/search/glob?pattern=%2A.md", h.http_base))
        .header(reqwest::header::AUTHORIZATION, "Bearer sk-fake-token")
        .send()
        .await
        .expect("send");
    assert_eq!(resp.status(), reqwest::StatusCode::UNAUTHORIZED);
    let body: serde_json::Value = resp.json().await.expect("json");
    assert!(
        body["error"].as_str().unwrap_or("").contains("wrong-token"),
        "provider's tonic::Status::message() must ride through into the JSON body; got: {body}",
    );
    assert!(
        h.log.globs.lock().await.is_empty(),
        "rejected bearer must NOT reach the backend",
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn search_glob_with_accepted_bearer_reaches_backend_and_returns_200() {
    let h = Harness::start(Arc::new(FixedBearer {
        accepted: "sk-real-token",
    }))
    .await;
    let resp = reqwest::Client::new()
        .get(format!("{}/v2/search/glob?pattern=%2A.md", h.http_base))
        .header(reqwest::header::AUTHORIZATION, "Bearer sk-real-token")
        .send()
        .await
        .expect("send");
    assert_eq!(resp.status(), reqwest::StatusCode::OK);
    let body: serde_json::Value = resp.json().await.expect("json");
    assert_eq!(body["paths"][0], "/hit.md");

    let logged = h.log.globs.lock().await;
    assert_eq!(
        logged.len(),
        1,
        "accepted bearer must reach the search backend exactly once",
    );
    assert_eq!(logged[0].pattern, "*.md");
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn search_glob_with_non_bearer_scheme_returns_401() {
    // `Authorization: Basic ...` is well-formed but not the Bearer
    // scheme the middleware accepts.  Must surface 401 rather than
    // silently reading the base64 body as a raw token.
    let h = Harness::start(Arc::new(FixedBearer {
        accepted: "irrelevant",
    }))
    .await;
    let resp = reqwest::Client::new()
        .get(format!("{}/v2/search/glob?pattern=%2A.md", h.http_base))
        .header(reqwest::header::AUTHORIZATION, "Basic dXNlcjpwYXNzd29yZA==")
        .send()
        .await
        .expect("send");
    assert_eq!(resp.status(), reqwest::StatusCode::UNAUTHORIZED);
    assert!(
        h.log.globs.lock().await.is_empty(),
        "non-Bearer scheme must not reach the backend",
    );
}
