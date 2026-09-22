//! [`LocalSearchBackend`] — the trait the dispatcher calls once per
//! zone to run the actual search RPC.  Owning the trait here (rather
//! than importing a concrete gRPC client) lets tests wire a fake
//! without the plugin gRPC stack, and lets a future cross-zone gRPC
//! impl slot in behind the same shape.
//!
//! # Request shape
//!
//! [`SearchRequest`] is the minimum the dispatcher builds from an
//! axum request.  Backend-specific tuning knobs (recency, alpha,
//! fusion method, chunks_per_page, path_prefix_boosts, …) will
//! extend this struct as they migrate off the Python surface.  Kept
//! narrow for the first landing so the shape is easy to review.

use async_trait::async_trait;
use nexus_search_common::{Hit, SearchDelegation};

/// The dispatcher's per-zone search request.  Owned (not a borrow of
/// an axum body) so each leg's spawn owns its clone.
#[derive(Debug, Clone)]
pub struct SearchRequest {
    /// Raw query text.
    pub query: String,
    /// `keyword` / `semantic` / `hybrid` — kept as a string so a
    /// caller reading a JSON body round-trips it verbatim; the
    /// backend interprets.
    pub search_type: String,
    /// Cap on hits returned per zone AND on the fused result set.
    pub limit: usize,
    /// Optional path prefix — narrow the search to a subtree.
    pub path_filter: Option<String>,
    /// The original caller a per-zone leg runs the search AS.  A
    /// remote-zone backend stamps this onto the [`SearchDelegation`]
    /// it mints so the servicer records the correct subject on its
    /// audit trail (the request runs as the original caller, NOT as
    /// the delegation minter).  Owned `(subject_type, subject_id)`
    /// so each leg's spawn owns its clone.
    ///
    /// The dispatcher stamps this from the `Subject<'_>` passed to
    /// [`crate::FederatedSearchDispatcher::search`] before spawning
    /// legs — a backend never sees an unset subject.
    pub subject: (String, String),
}

/// A backend that can run a search inside ONE zone and return a list
/// of hits ranked by the backend's own score.  Async because every
/// production impl talks a network transport (tonic gRPC to the
/// search-plugin).
///
/// # Local vs remote
///
/// This trait is the dispatcher's per-zone entry point regardless of
/// where the zone lives.  Two concrete shapes:
///
/// * The daemon's OWN zones — a thin in-process wrapper around
///   `nexus.search.v1.SearchService.Query` on the local plugin.
/// * A CROSS-daemon zone — served by [`crate::routing::RoutingBackend`], which uses
///   a [`ZoneSearchRegistry`](nexus_search_common::ZoneSearchRegistry)
///   to pick the right daemon and a [`RemoteSearchBackend`] to dial it.
///
/// The dispatcher does not know the difference — routing is a
/// composition concern owned by [`crate::routing::RoutingBackend`].
#[async_trait]
pub trait LocalSearchBackend: Send + Sync {
    async fn search_zone(
        &self,
        zone_id: &str,
        req: &SearchRequest,
    ) -> Result<Vec<Hit>, BackendError>;
}

/// Dial-a-remote-daemon-and-run-a-search abstraction.  The concrete
/// impl in PR 5 talks tonic gRPC to another daemon's
/// `nexus.search.v1.SearchService.Query`, attaching `delegation.delegation_id`
/// as the request's `auth_token`.  The trait is decl-only here so
/// [`crate::routing::RoutingBackend`] can compose against a fake in unit tests
/// without pulling the tonic stack into this crate's test build.
///
/// # Why the trait sees the delegation, not the raw subject
///
/// Delegation minting is a routing concern (source zone id, TTL,
/// delegation id generation) that must not leak into the transport.
/// [`crate::routing::RoutingBackend`] mints one delegation per zone and hands the
/// impl an ALREADY-MINTED credential — the transport only serialises
/// it and puts it on the wire.  Split responsibilities cleanly: a
/// future transport that speaks JSON-over-HTTP (or an in-process
/// stub) drops in behind the same trait unchanged.
#[async_trait]
pub trait RemoteSearchBackend: Send + Sync {
    /// Run the search on the remote daemon at `target`, presenting
    /// `delegation` as the auth credential.
    ///
    /// `target` — the string [`ZoneSearchRegistry::resolve`](nexus_search_common::ZoneSearchRegistry::resolve)
    /// returned.  Uninterpreted here; the impl's job is to decode it
    /// into a transport endpoint (gRPC URI, socket path, in-process
    /// registry key).
    async fn search_remote_zone(
        &self,
        target: &str,
        delegation: &SearchDelegation,
        zone_id: &str,
        req: &SearchRequest,
    ) -> Result<Vec<Hit>, BackendError>;
}

/// Errors a backend may surface.  Kept small — the dispatcher just
/// bubbles the message onto [`nexus_search_common::ZoneFailure`],
/// so the wire shape is a plain string on the response envelope.
#[derive(Debug, thiserror::Error, Clone, PartialEq)]
pub enum BackendError {
    /// Transport / RPC-level failure (dial refused, TLS mismatch,
    /// timeout inside the transport).
    #[error("transport: {0}")]
    Transport(String),
    /// Backend-side refusal (bad request, ResourceExhausted,
    /// FailedPrecondition, …).
    #[error("backend: {0}")]
    Backend(String),
    /// Config bug (unrecognised backend target, misconfigured
    /// registry).  Surfaced as a 500 upstream — matches how the
    /// Python dispatcher treats a mis-wired registry.
    #[error("config: {0}")]
    Config(String),
}

/// [`RemoteSearchBackend`] that refuses every dial.  Used by the
/// composition root of a single-daemon deployment (no cross-zone
/// peers wired yet) so [`crate::routing::RoutingBackend`] can still be
/// constructed — a caller who leaves the registry empty never hits
/// this arm; a mis-wired caller who registers a remote target and
/// forgot to wire a real transport gets a loud
/// [`BackendError::Config`] instead of a silent-drop.
pub struct NoOpRemoteSearchBackend;

#[async_trait]
impl RemoteSearchBackend for NoOpRemoteSearchBackend {
    async fn search_remote_zone(
        &self,
        target: &str,
        _delegation: &SearchDelegation,
        zone_id: &str,
        _req: &SearchRequest,
    ) -> Result<Vec<Hit>, BackendError> {
        Err(BackendError::Config(format!(
            "no remote search backend wired — zone {zone_id:?} resolved to \
             remote target {target:?}, but this daemon was built with \
             NoOpRemoteSearchBackend (single-daemon deployment)",
        )))
    }
}
