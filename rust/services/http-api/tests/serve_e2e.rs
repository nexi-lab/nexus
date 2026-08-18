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

use nexus_http_api::{router, StatusResponse};
use tokio::net::TcpListener;

/// Spawn the router on an ephemeral loopback port; return the base
/// URL callers hit with `reqwest`.  Test-only helper — the binary
/// entry point (once R10 wires this into `nexusd-cluster`) will
/// take an operator-supplied bind address, not an OS-picked one.
async fn spawn_router() -> String {
    // Bind port 0 → the kernel picks a free port; read it back so
    // the client knows where to connect.
    let listener = TcpListener::bind("127.0.0.1:0")
        .await
        .expect("bind loopback ephemeral port");
    let addr = listener.local_addr().expect("read bound addr");
    let base_url = format!("http://{addr}");
    tokio::spawn(async move {
        // `axum::serve` runs until the listener is dropped (which
        // happens when the tokio runtime tears down at test end),
        // so no explicit shutdown handshake is needed.
        axum::serve(listener, router()).await.expect("axum::serve");
    });
    base_url
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
