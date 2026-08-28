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
//! # Auth
//!
//! `/v2/status` is PUBLIC (health-probe target that runs before any
//! bearer exists); every `/v2/search/*` route is protected by
//! [`middleware::auth::require_bearer`], which resolves the incoming
//! `Authorization: Bearer <token>` through the shared
//! [`transport::auth::AuthProvider`] on [`AppState`] and stamps the
//! resulting `contracts::OperationContext` into request extensions.
//! Handlers that need per-request identity extract it via
//! `axum::Extension<OperationContext>`; handlers that do not simply
//! ignore it.  Default provider is `NoAuth` (single-node dev pass-
//! through); real deployments swap it for `auth::ApiKeyAuthProvider`
//! at the composition root.
//!
//! # Deliberately absent
//!
//! * NO ReBAC post-filter — separate epic step (`bricks/permissions/
//!   rebac.py` → Rust); the middleware here is authN only.
//! * NO mTLS peer plane — waits on this crate's HTTP listener
//!   growing a rustls stack (see the middleware module docs).
//! * NO wiring into `nexusd-cluster` boot — the assembly binary in
//!   `rust/nexusd` picks the crate up once the router surface
//!   justifies the wiring step.  Standalone-testable today via
//!   `axum::serve` + `tests/*_e2e.rs`.

use std::io;
use std::net::SocketAddr;
use std::sync::Arc;

use axum::middleware::from_fn_with_state;
use axum::Router;
use tokio::net::TcpListener;
use transport::auth::AuthProvider;

pub mod handlers;
pub mod middleware;
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
///
/// # `auth`
///
/// The `AuthProvider` used by [`middleware::auth::require_bearer`]
/// to resolve incoming bearer tokens.  Trait-object so a single
/// binary can pick `NoAuth` for dev, `ApiKeyAuthProvider` for
/// production, or a test double.  See
/// [`middleware::auth::default_no_auth_provider`] for the
/// single-node default.
#[derive(Clone)]
pub struct AppState {
    pub search: SearchBackend,
    pub auth: Arc<dyn AuthProvider>,
}

impl AppState {
    /// Convenience constructor for tests / probes that need a fully-
    /// wired [`AppState`] but do not care about the exact backend
    /// target or auth policy.  Wires the search backend at
    /// `grpc_target` (dial-on-first-use — never dialed if the test
    /// does not hit a search route) and the default `NoAuth`
    /// provider so bearer parsing / rejection can be tested
    /// separately.  Cheap: both fields are just `Arc` allocations.
    ///
    /// Not for production — the composition root in `nexusd` builds
    /// the same struct field-by-field with the real `SearchBackend`
    /// target + `ApiKeyAuthProvider`.
    pub fn for_tests(grpc_target: impl Into<Arc<str>>) -> Self {
        Self {
            search: SearchBackend::new(grpc_target),
            auth: middleware::auth::default_no_auth_provider(),
        }
    }
}

/// Root [`Router`] carrying every configured route domain.  Callers
/// supply the [`AppState`] (which owns the upstream client caches)
/// so the same crate can be exercised in tests against a mock
/// backend and in production against the real search-plugin.
///
/// # Layering
///
/// The router splits into two sub-routers by auth posture:
///
/// * `public_router` — routes callable BEFORE a bearer exists
///   (liveness probes, unauth-metadata endpoints).  Currently just
///   `/v2/status`.
/// * `protected_router` — routes wrapped by
///   [`middleware::auth::require_bearer`].  Every `/v2/search/*`
///   handler lives here.
///
/// Both sub-routers share the same [`AppState`]; only the middleware
/// stack differs.  A new domain lands as an additive merge on
/// whichever sub-router matches its auth posture — handlers stay
/// unaware of the split beyond optionally extracting
/// `Extension<OperationContext>`.
pub fn router(state: AppState) -> Router {
    let public_router = Router::new()
        .merge(handlers::status::router())
        .with_state(state.clone());
    let protected_router = Router::new()
        .merge(handlers::search::router())
        .layer(from_fn_with_state(
            state.clone(),
            middleware::auth::require_bearer,
        ))
        .with_state(state);
    Router::new().merge(public_router).merge(protected_router)
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

/// Build a [`kernel::kernel::ServiceDecl`] that spawns the HTTP-API
/// listener on `addr` at daemon bring-up.  The install closure
/// captures the resolved auth provider + runtime handle at
/// decl-BUILD time so the daemon does not have to plumb them
/// through the kernel's install signature (which only exposes
/// `&Arc<Kernel>`).
///
/// # Wiring
///
/// Callers construct this from `nexus_cluster::ServiceBootCtx`:
///
/// ```ignore
/// nexus_http_api::service_decl(
///     addr,                              // parsed from NEXUS_HTTP_ADDR
///     "http://127.0.0.1:2126".into(),    // co-hosted gRPC target
///     std::sync::Arc::clone(&ctx.auth),
///     ctx.runtime.clone(),
/// )
/// ```
///
/// Args are taken RAW (not `&ServiceBootCtx`) so this crate does
/// not need `nexus-cluster` as a dep — tier separation stays clean.
///
/// # Failure mode — fail-loud on bind, then detach serve
///
/// The install closure BINDS the TCP listener synchronously
/// (via `runtime.block_on(TcpListener::bind(addr))`) so a bind
/// failure — port in use, permission denied, malformed addr —
/// returns `Err` from `install` and `Kernel::bring_up_services`
/// fails the whole daemon boot with a nameable error.  The prior
/// posture ("bind inside a detached `runtime.spawn`, only log on
/// error") silently degraded a broken HTTP bind to "daemon looks
/// alive but no HTTP surface" — a bad operator experience the
/// standing `feedback_fail_loud_interdependent_config` rule
/// forbids.
///
/// Only the SERVE loop is detached: once bind succeeds, the
/// listener is handed to a spawned task that runs `axum::serve`
/// until the daemon shuts down.  A serve error mid-flight is
/// still a `tracing::error!` (nothing sensible we can do about a
/// half-served request), but the "listener is up" invariant is
/// established synchronously — matches `a2a::install_a2a_stamp_hook`
/// which also fails install synchronously if setup errors.
pub fn service_decl(
    addr: SocketAddr,
    upstream_grpc: String,
    auth: Arc<dyn AuthProvider>,
    runtime: tokio::runtime::Handle,
) -> kernel::kernel::ServiceDecl {
    kernel::kernel::ServiceDecl {
        name: "http_api".to_string(),
        install: Box::new(move |_kernel| {
            let state = AppState {
                search: SearchBackend::new(upstream_grpc),
                auth,
            };
            // Bind synchronously so `install` surfaces the failure —
            // port-in-use / EACCES / bad interface all become an
            // `install` `Err` instead of a stray `tracing::error!`
            // on a background task.  `runtime.block_on` on a
            // multi-thread runtime is safe from a synchronous
            // caller thread; if we were already ON a runtime thread
            // this would deadlock, but `bring_up_services` runs
            // from the daemon's synchronous boot path.
            let listener = runtime
                .block_on(TcpListener::bind(addr))
                .map_err(|e| format!("nexus-http-api: bind {addr}: {e}"))?;
            let bound = listener
                .local_addr()
                .map_err(|e| format!("nexus-http-api: local_addr after bind: {e}"))?;
            tracing::info!(addr = %bound, "nexus-http-api: axum listener bound");
            // Detach the serve loop — the listener has a life of
            // its own from here, running until the daemon shuts down.
            runtime.spawn(async move {
                if let Err(e) = axum::serve(listener, router(state)).await {
                    tracing::error!(
                        addr = %bound,
                        error = %e,
                        "nexus-http-api: axum serve loop terminated",
                    );
                }
            });
            Ok(())
        }),
    }
}
