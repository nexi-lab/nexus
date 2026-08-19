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

use axum::Router;

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
