//! HTTP-boundary tests for the revision fence — proves the fence
//! extractor is wired into the search handlers and that the wire
//! contract (412 body, `X-Nexus-Revision` header) reaches the client
//! intact.
//!
//! Unit tests in `middleware::revision` already cover parsing and the
//! poll loop; these tests guard against a future handler refactor
//! silently dropping the `fence.enforce(...)` call — the compiler
//! only proves the handler *takes* the extractor, not that it *uses* it.

use std::sync::Arc;

use nexus_http_api::middleware::revision::StatGen;
use nexus_http_api::{bind_and_serve, AppState};
use tokio::net::TcpListener;

/// Fixed-gen `StatGen` for the fence-boundary tests.
struct FixedGen(u64);
impl StatGen for FixedGen {
    fn stat_gen(&self, _path: &str, _zone_id: &str) -> u64 {
        self.0
    }
}

/// Boot the router with a `FixedGen` kernel; return the base URL.
/// Uses `AppState::for_tests` for auth/search/rebac/key-store defaults
/// and swaps in the caller's kernel so the fence behaviour is the
/// only variable.
async fn spawn_with_kernel(kernel: Arc<dyn StatGen>) -> String {
    // We do not hit /v2/search/query's backend — the fence trips
    // BEFORE the tonic call.  A dial to 127.0.0.1:1 would only be
    // opened lazily on a real backend request and never triggers
    // here.
    let mut state = AppState::for_tests("http://127.0.0.1:1");
    state.kernel = kernel;
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    drop(listener);
    let (addr, fut) = bind_and_serve(addr, state).await.expect("bind");
    tokio::spawn(async move {
        let _ = fut.await;
    });
    format!("http://{addr}")
}

/// A fence that stays below the required gen must 412 with the
/// observed revision echoed both in the body and as
/// `X-Nexus-Revision`.  This is the "read-your-writes hard fail"
/// contract — the caller can retry without re-issuing the read to
/// discover where the node actually is.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn fenced_query_412s_when_gen_below_required() {
    let base = spawn_with_kernel(Arc::new(FixedGen(3))).await;
    tokio::task::yield_now().await;
    let client = reqwest::Client::new();
    let resp = client
        .post(format!("{base}/v2/search/query"))
        .header("X-Nexus-Min-Revision", "/ws/a.txt@7")
        .header("X-Nexus-Revision-Timeout-Ms", "80")
        .json(&serde_json::json!({"q": "anything"}))
        .send()
        .await
        .expect("post");
    assert_eq!(resp.status(), reqwest::StatusCode::PRECONDITION_FAILED);
    let observed = resp
        .headers()
        .get("X-Nexus-Revision")
        .and_then(|v| v.to_str().ok())
        .map(str::to_string);
    let body: serde_json::Value = resp.json().await.unwrap();
    let detail = &body["detail"];
    assert_eq!(detail["error"], "revision_not_applied");
    assert_eq!(detail["min_revision"], "/ws/a.txt@7");
    assert_eq!(detail["current_revision"], "/ws/a.txt@3");
    assert_eq!(observed.as_deref(), Some("/ws/a.txt@3"));
}

/// A malformed `min_revision` must 400 at the extractor, BEFORE any
/// backend call — no fence-timeout budget spent.  Guards the "fail
/// fast on bad input" contract.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn fenced_query_400s_on_malformed_min_revision() {
    let base = spawn_with_kernel(Arc::new(FixedGen(100))).await;
    tokio::task::yield_now().await;
    let client = reqwest::Client::new();
    let resp = client
        .post(format!("{base}/v2/search/query"))
        .header("X-Nexus-Min-Revision", "/ws/a@notanumber")
        .json(&serde_json::json!({"q": "anything"}))
        .send()
        .await
        .expect("post");
    assert_eq!(resp.status(), reqwest::StatusCode::BAD_REQUEST);
}

/// A zone-anchored fence (`root@1234`) answers 501 today —
/// `nexusd-cluster` does not expose `federation_cluster_info`.
/// Guards the "no silent zone-fence" contract so a client that asked
/// for zone-anchored freshness learns the guarantee is absent instead
/// of silently getting a stale answer stamped with a path token.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn zone_anchored_fence_501s() {
    let base = spawn_with_kernel(Arc::new(FixedGen(0))).await;
    tokio::task::yield_now().await;
    let client = reqwest::Client::new();
    let resp = client
        .post(format!("{base}/v2/search/query"))
        .header("X-Nexus-Min-Revision", "root@1234")
        .json(&serde_json::json!({"q": "anything"}))
        .send()
        .await
        .expect("post");
    assert_eq!(resp.status(), reqwest::StatusCode::NOT_IMPLEMENTED);
    let body: serde_json::Value = resp.json().await.unwrap();
    assert_eq!(body["detail"]["error"], "zone_revision_unavailable");
}

/// GET /v2/search/glob honours the fence the same way as query —
/// query-param form, since glob is a GET.  Confirms the extractor
/// works on the query-string boundary too, not just the header.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn fenced_glob_reads_min_revision_from_query_param() {
    let base = spawn_with_kernel(Arc::new(FixedGen(1))).await;
    tokio::task::yield_now().await;
    let resp = reqwest::get(format!(
        "{base}/v2/search/glob?pattern=*.md&min_revision=/ws/a.txt@9&revision_timeout_ms=60"
    ))
    .await
    .expect("get");
    assert_eq!(resp.status(), reqwest::StatusCode::PRECONDITION_FAILED);
    let body: serde_json::Value = resp.json().await.unwrap();
    assert_eq!(body["detail"]["current_revision"], "/ws/a.txt@1");
}

/// GET /v2/search/grep — same fence contract as glob.  Kept as a
/// separate test so a future regression on one handler does not
/// silently pass because the other still fences.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn fenced_grep_reads_min_revision_from_header() {
    let base = spawn_with_kernel(Arc::new(FixedGen(2))).await;
    tokio::task::yield_now().await;
    let client = reqwest::Client::new();
    let resp = client
        .get(format!("{base}/v2/search/grep?pattern=anything"))
        .header("X-Nexus-Min-Revision", "/ws/a.txt@42")
        .header("X-Nexus-Revision-Timeout-Ms", "60")
        .send()
        .await
        .expect("get");
    assert_eq!(resp.status(), reqwest::StatusCode::PRECONDITION_FAILED);
    let body: serde_json::Value = resp.json().await.unwrap();
    assert_eq!(body["detail"]["current_revision"], "/ws/a.txt@2");
}
