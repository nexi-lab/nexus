//! End-to-end integration test for the `nexus-http-api` crate.
//!
//! Complements the in-process `oneshot` unit tests in `src/lib.rs`
//! by exercising the full listener stack: bind an ephemeral loopback
//! port with [`axum::serve`], round-trip a real HTTP request via
//! `reqwest`, and read the parsed JSON body.  Pins the hyper + tower
//! + serde composition, not just the handler.
//!
//! Deliberately kept ONE test — every future integration case (auth
//! middleware, streaming body, error mapping) attaches here as a
//! sibling test rather than a separate binary, so the listener setup
//! is amortised.

use nexus_http_api::{bind_and_serve, AppState, SearchBackend, StatusResponse};

/// Spawn the router on an ephemeral loopback port; return the base
/// URL callers hit with `reqwest`.  Uses the crate's own
/// [`bind_and_serve`] helper — the same call the production
/// `nexusd-cluster` assembly binary will use — so a regression in
/// the helper trips this test alongside anything else that binds.
///
/// The status handler does not touch the search backend, so a
/// dummy target that never gets dialed is fine for this file's
/// tests.  Backend-touching handlers get their own test binaries
/// with a real mock server (`glob_e2e.rs`).
async fn spawn_router() -> String {
    let state = AppState {
        search: SearchBackend::new("http://127.0.0.1:1"),
    };
    let (addr, fut) = bind_and_serve("127.0.0.1:0".parse().unwrap(), state)
        .await
        .expect("bind loopback ephemeral port");
    tokio::spawn(async move {
        fut.await.expect("axum::serve");
    });
    format!("http://{addr}")
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn status_endpoint_round_trips_over_real_tcp() {
    let base = spawn_router().await;
    let client = reqwest::Client::new();

    let resp = client
        .get(format!("{base}/v2/status"))
        .send()
        .await
        .expect("send status request");
    assert_eq!(resp.status(), reqwest::StatusCode::OK);

    let body: StatusResponse = resp.json().await.expect("decode status body");
    assert_eq!(body.status, "ok");
    assert_eq!(body.version, env!("CARGO_PKG_VERSION"));
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn unknown_endpoint_returns_404_over_real_tcp() {
    let base = spawn_router().await;
    let client = reqwest::Client::new();

    let resp = client
        .get(format!("{base}/v2/does-not-exist"))
        .send()
        .await
        .expect("send 404 request");
    assert_eq!(resp.status(), reqwest::StatusCode::NOT_FOUND);
}
