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

use nexus_http_api::{bind_and_serve, AppState, StatusResponse};

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
    // Status is a PUBLIC route; `for_tests` gives us a fully-wired
    // AppState (dummy backend + NoAuth) with zero test-glue.
    let state = AppState::for_tests("http://127.0.0.1:1");
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

/// Regression pin — the `service_decl` install path must not
/// re-enter the tokio runtime.
///
/// The R10-audit-followup PR (#4717) initially used
/// `runtime.block_on(tokio::net::TcpListener::bind(addr))` inside
/// the install closure — WRONG: `bring_up_services` is invoked
/// FROM a runtime worker, so `block_on` panicked with
/// `Cannot start a runtime from within a runtime` — caught only
/// by the docker E2E (#4715), post-push.  This test invokes the
/// same code path (`install_impl` — the install-closure body
/// factored out of `service_decl` so tests can drive it without
/// constructing a full `Arc<Kernel>`) UNDER a real
/// `#[tokio::test(flavor = "multi_thread")]` runtime, so a
/// regression trips locally instead of live.
///
/// Uses the `default_no_auth_provider` + a dummy upstream that
/// never gets dialed (the install path only touches the
/// listener, not the search backend), so a passing test proves
/// EXACTLY the "install completes cleanly from a runtime thread"
/// invariant — nothing else.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn service_decl_install_body_runs_under_a_tokio_runtime() {
    use nexus_http_api::middleware::auth::default_no_auth_provider;
    use tokio::net::TcpListener;

    // Bind ephemeral to get an unused port, then drop the
    // listener BEFORE handing the addr to `install_impl` so its
    // own bind succeeds against the same port (SO_REUSEADDR
    // handles the TIME_WAIT window on the OS).
    let probe = TcpListener::bind("127.0.0.1:0").await.expect("probe bind");
    let addr = probe.local_addr().expect("probe local_addr");
    drop(probe);

    let handle = tokio::runtime::Handle::current();
    // Direct call — same path `ServiceDecl::install` takes at
    // daemon boot, minus the `_kernel: &Arc<Kernel>` param the
    // body ignores.  A runtime-in-runtime panic here would
    // abort the test with "Cannot start a runtime from within a
    // runtime".
    let result = nexus_http_api::install_impl(
        addr,
        "http://127.0.0.1:1".to_string(),
        default_no_auth_provider(),
        handle,
    );
    assert!(
        result.is_ok(),
        "install must succeed under a runtime; got {result:?}",
    );

    // Positive-side check: the listener really is bound + serving.
    // Give the spawned serve task a tick to start accepting.
    tokio::task::yield_now().await;
    let resp = reqwest::get(format!("http://{addr}/v2/status"))
        .await
        .expect("GET /v2/status after install");
    assert_eq!(resp.status(), reqwest::StatusCode::OK);
}

/// Bind-failure branch: a second install on the SAME address must
/// return `Err` (port in use), not `Ok(())`-then-`tracing::error!`
/// on a background task.  Pins the "fail-loud at install" contract
/// the #4713 audit's finding #2 was about.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn service_decl_install_body_returns_err_on_bind_failure() {
    use nexus_http_api::middleware::auth::default_no_auth_provider;

    // First install: succeeds, holds the port.
    let probe = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("probe bind");
    let addr = probe.local_addr().expect("probe local_addr");
    drop(probe);

    let handle = tokio::runtime::Handle::current();
    let first = nexus_http_api::install_impl(
        addr,
        "http://127.0.0.1:1".to_string(),
        default_no_auth_provider(),
        handle.clone(),
    );
    assert!(first.is_ok(), "first install must succeed; got {first:?}");

    // Second install on the SAME address — must Err loudly
    // (address in use) instead of the pre-fix silent-log posture.
    let second = nexus_http_api::install_impl(
        addr,
        "http://127.0.0.1:1".to_string(),
        default_no_auth_provider(),
        handle,
    );
    let err = second.expect_err("second install on same addr must Err");
    assert!(
        err.contains("bind") && err.contains(&addr.to_string()),
        "bind error must name the operation + address so an operator can debug; got {err:?}",
    );
}
