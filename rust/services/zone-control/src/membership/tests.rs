use super::{ConfigurationError, MossMembershipVerifier, Snapshot, WorkerCache, CACHE_CAPACITY};
use axum::extract::{Query, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::IntoResponse;
use axum::routing::get;
use axum::{Json, Router};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use tokio::net::TcpListener;
use tokio::task::JoinHandle;

struct HostState {
    response: (StatusCode, Value),
    calls: Vec<(HeaderMap, HashMap<String, String>)>,
}

struct Host {
    endpoint: String,
    state: Arc<Mutex<HostState>>,
    task: JoinHandle<()>,
}

impl Host {
    async fn start() -> Self {
        let state = Arc::new(Mutex::new(HostState {
            response: (StatusCode::OK, json!({"status": "active", "revision": 1})),
            calls: Vec::new(),
        }));
        let app = Router::new()
            .route(
                "/membership",
                get(
                    |State(state): State<Arc<Mutex<HostState>>>,
                     headers: HeaderMap,
                     Query(query): Query<HashMap<String, String>>| async move {
                        let mut state = state.lock().unwrap();
                        state.calls.push((headers, query));
                        (state.response.0, Json(state.response.1.clone())).into_response()
                    },
                ),
            )
            .with_state(Arc::clone(&state));
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let endpoint = format!("http://{}/membership", listener.local_addr().unwrap());
        let task = tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
        Self {
            endpoint,
            state,
            task,
        }
    }

    fn respond(&self, status: StatusCode, body: Value) {
        self.state.lock().unwrap().response = (status, body);
    }
}

impl Drop for Host {
    fn drop(&mut self) {
        self.task.abort();
    }
}

#[tokio::test]
async fn each_access_revalidates_membership_and_encodes_identity_without_query_injection() {
    let host = Host::start().await;
    let verifier = MossMembershipVerifier::new(&host.endpoint, "fixture-only-secret").unwrap();
    let user = "user &org_id=forged/用户";
    assert!(verifier.check(user, "org+1", "r1").await.unwrap());
    host.respond(StatusCode::OK, json!({"status": "disabled", "revision": 2}));
    assert!(!verifier.check(user, "org+1", "r1").await.unwrap());
    let state = host.state.lock().unwrap();
    assert_eq!(state.calls.len(), 2);
    for (headers, query) in &state.calls {
        assert_eq!(headers["authorization"], "Bearer fixture-only-secret");
        assert_eq!(query["user_id"], user);
        assert_eq!(query["org_id"], "org+1");
        assert_eq!(query.len(), 2);
    }
}

#[tokio::test]
async fn inactive_and_unavailable_remain_distinct_and_failures_are_not_cached() {
    let host = Host::start().await;
    let verifier = MossMembershipVerifier::new(&host.endpoint, "fixture").unwrap();
    for (status, body) in [
        (StatusCode::OK, json!({"status": "active", "revision": 2})),
        (StatusCode::OK, json!({"status": "disabled", "revision": 1})),
        (StatusCode::NOT_FOUND, json!(null)),
    ] {
        host.respond(status, body);
        assert!(!verifier.check("user", "org", "r1").await.unwrap());
    }
    for (status, body) in [
        (
            StatusCode::SERVICE_UNAVAILABLE,
            json!({"secret": "fixture"}),
        ),
        (
            StatusCode::UNAUTHORIZED,
            json!({"status": "active", "revision": 1}),
        ),
        (
            StatusCode::FOUND,
            json!({"status": "active", "revision": 1}),
        ),
        (StatusCode::OK, json!({"status": "active"})),
        (
            StatusCode::OK,
            json!({"status": "active", "revision": true}),
        ),
    ] {
        host.respond(status, body);
        let error = verifier.check("user", "org", "r1").await.unwrap_err();
        assert_eq!(error.to_string(), "Moss membership lookup unavailable");
    }
    let cached = MossMembershipVerifier::new(&host.endpoint, "fixture")
        .unwrap()
        .with_worker_cache();
    assert!(cached.check("user", "org", "r1").await.is_err());
    host.respond(StatusCode::OK, json!({"status": "active", "revision": 1}));
    assert!(cached.check("user", "org", "r1").await.unwrap());
}

#[tokio::test]
async fn worker_cache_is_scoped_to_identity_pair_and_does_not_replace_access_freshness() {
    let host = Host::start().await;
    let mut worker = MossMembershipVerifier::new(&host.endpoint, "fixture")
        .unwrap()
        .with_worker_cache();
    let frozen = Instant::now();
    worker.worker_cache.as_mut().unwrap().now = Box::new(move || frozen);
    let access = MossMembershipVerifier::new(&host.endpoint, "fixture").unwrap();
    assert!(worker.check("user", "org", "r1").await.unwrap());
    host.respond(StatusCode::OK, json!({"status": "active", "revision": 2}));
    assert!(worker.check("user", "org", "r1").await.unwrap());
    assert!(!worker.check("user", "org", "r2").await.unwrap());
    assert!(!access.check("user", "org", "r1").await.unwrap());
    assert!(worker.check("other-user", "org", "r2").await.unwrap());
    assert!(worker.check("user", "other-org", "r2").await.unwrap());
    assert_eq!(host.state.lock().unwrap().calls.len(), 4);
}

#[test]
fn worker_cache_expires_at_the_lookup_start_deadline_without_sleeping() {
    let cache = WorkerCache::new();
    let started = Instant::now();
    let key = ("user".into(), "org".into());
    cache.insert(key.clone(), None, started).unwrap();
    assert!(matches!(
        cache.get(&key, started + Duration::from_secs(4)).unwrap(),
        Some(None)
    ));
    assert!(cache
        .get(&key, started + Duration::from_secs(5))
        .unwrap()
        .is_none());
}

#[test]
fn an_older_response_cannot_replace_a_newer_cached_lookup() {
    let cache = WorkerCache::new();
    let started = Instant::now();
    let key = ("user".into(), "org".into());
    cache
        .insert(
            key.clone(),
            Some(Snapshot {
                status: "disabled".into(),
                revision: 2,
            }),
            started + Duration::from_secs(1),
        )
        .unwrap();
    cache
        .insert(
            key.clone(),
            Some(Snapshot {
                status: "active".into(),
                revision: 1,
            }),
            started,
        )
        .unwrap();
    let snapshot = cache.get(&key, started).unwrap().unwrap().unwrap();
    assert_eq!(snapshot.status, "disabled");
    assert_eq!(snapshot.revision, 2);
}

#[test]
fn a_full_cache_evicts_the_oldest_snapshot_and_stays_bounded() {
    let frozen = Instant::now();
    let mut cache = WorkerCache::new();
    cache.now = Box::new(move || frozen);
    for index in 0..=CACHE_CAPACITY {
        cache
            .insert(
                (format!("user-{index}"), "org".into()),
                None,
                frozen + Duration::from_nanos(index as u64),
            )
            .unwrap();
    }
    assert_eq!(cache.entries.lock().unwrap().len(), CACHE_CAPACITY);
    assert!(cache
        .get(&("user-0".into(), "org".into()), frozen)
        .unwrap()
        .is_none());
    assert!(cache
        .get(&(format!("user-{CACHE_CAPACITY}"), "org".into()), frozen)
        .unwrap()
        .is_some());
}

#[tokio::test]
async fn redirect_cannot_forward_the_credential_or_turn_a_redirect_body_into_authority() {
    let destination = Host::start().await;
    let destination_url = destination.endpoint.clone();
    let app = Router::new().route(
        "/membership",
        get(move || {
            let destination_url = destination_url.clone();
            async move {
                (
                    StatusCode::FOUND,
                    [("location", destination_url)],
                    Json(json!({"status": "active", "revision": 1})),
                )
            }
        }),
    );
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let endpoint = format!("http://{}/membership", listener.local_addr().unwrap());
    let task = tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
    let verifier = MossMembershipVerifier::new(&endpoint, "fixture-only-secret").unwrap();
    assert!(verifier.check("user", "org", "r1").await.is_err());
    assert!(destination.state.lock().unwrap().calls.is_empty());
    task.abort();
    assert!(task.await.unwrap_err().is_cancelled());
}

#[test]
fn invalid_configuration_never_reflects_endpoint_credentials_or_token_bytes() {
    for endpoint in [
        "file:///private",
        "https://user:fixture-secret@moss/",
        "http://moss/#token",
    ] {
        let error = MossMembershipVerifier::new(endpoint, "fixture-only-secret")
            .err()
            .unwrap();
        assert_eq!(error, ConfigurationError::InvalidEndpoint);
        assert!(!error.to_string().contains("fixture"));
    }
    for token in ["", "  ", "fixture\r\nheader:value"] {
        let error = MossMembershipVerifier::new("http://127.0.0.1/membership", token)
            .err()
            .unwrap();
        assert_eq!(error, ConfigurationError::InvalidCredential);
        assert!(!error.to_string().contains("fixture"));
    }
}
