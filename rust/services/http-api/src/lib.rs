//! `nexus-http-api` — axum-based HTTP-API surface for the pure-Rust
//! `nexus-server` migration (epic #4674 review R10).
//!
//! # What this crate does
//!
//! Serves the `/v2/*` HTTP surface, translating each route into a
//! typed gRPC call against the appropriate upstream service (search,
//! documents, auth — one route domain per file under `handlers/`).
//! Handlers are pure axum functions taking an `axum::extract::State`
//! holding the upstream client cache; the router itself is a plain
//! `axum::Router` assembled by [`router`].
//!
//! # Extension seam
//!
//! Every new route domain lands as an additive `Router::merge` on
//! [`router`] and a new file under `handlers/`.  The state grows by
//! adding a new field to [`AppState`] — a fresh domain does not
//! rewire existing handlers.
//!
//! # Deliberately absent
//!
//! * NO auth middleware — session cookie handling is a separate epic
//!   step (part of the same #4674 arc).
//! * NO ReBAC post-filter — same story.
//! * NO wiring into `nexusd-cluster` boot — the assembly binary in
//!   `rust/nexusd` picks the crate up once the router surface
//!   justifies the wiring step.  Standalone-testable today via
//!   `axum::serve` + `tests/*_e2e.rs`.

use std::io;
use std::net::SocketAddr;

use axum::Router;
use tokio::net::TcpListener;

pub mod handlers;
pub mod search_backend;

/// Client stubs for the workspace's `nexus.search.v1` proto SSOT
/// — generated at build time by `build.rs` from
/// `rust/services/proto/nexus/search/v1/search.proto`.  Client-only
/// (this crate never implements the service).
pub mod search_proto {
    #![allow(clippy::all)]
    #![allow(unused_qualifications)]
    tonic::include_proto!("nexus.search.v1");
}

pub use handlers::status::StatusResponse;
pub use search_backend::{BackendError, SearchBackend};

/// Shared state handed to every axum handler through
/// `axum::extract::State`.  Cheap to clone (`Arc` fields inside
/// each backend); one instance per process.
#[derive(Clone)]
pub struct AppState {
    pub search: SearchBackend,
}

/// Root [`Router`] carrying every configured route domain.  Callers
/// supply the [`AppState`] (which owns the upstream client caches)
/// so the same crate can be exercised in tests against a mock
/// backend and in production against the real search-plugin.
pub fn router(state: AppState) -> Router {
    Router::new()
        .merge(handlers::status::router())
        .merge(handlers::search::router())
        .with_state(state)
}

/// Bind `addr` and serve [`router(state)`](router) until the returned
/// future completes.  Convenience wrapper over
/// [`axum::serve`] + [`tokio::net::TcpListener::bind`] so callers
/// (integration tests, the future `nexusd-cluster` assembly
/// binary) do not each re-implement the two-line startup dance.
///
/// Returns the [`SocketAddr`] actually bound so callers who pass
/// `127.0.0.1:0` can learn the OS-picked port.  A shutdown hook is
/// deliberately absent from this signature — tests drop the future
/// when the runtime tears down, and the production binary wires
/// its own graceful-shutdown signal on top of the raw future via
/// [`axum::serve::Serve::with_graceful_shutdown`].
pub async fn serve(addr: SocketAddr, state: AppState) -> io::Result<()> {
    let listener = TcpListener::bind(addr).await?;
    axum::serve(listener, router(state)).await
}

/// Same shape as [`serve`] but binds ahead of time so the caller
/// can read `local_addr()` (the OS-picked port when `addr.port() == 0`)
/// before the serve future starts.  Convenience for tests + any
/// production caller that needs to log the bound port before the
/// event loop runs.
pub async fn bind_and_serve(
    addr: SocketAddr,
    state: AppState,
) -> io::Result<(
    SocketAddr,
    impl std::future::Future<Output = io::Result<()>>,
)> {
    let listener = TcpListener::bind(addr).await?;
    let bound = listener.local_addr()?;
    let fut = async move { axum::serve(listener, router(state)).await };
    Ok((bound, fut))
}
