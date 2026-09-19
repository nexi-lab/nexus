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
use nexus_search_common::Hit;

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
}

/// A backend that can run a search inside ONE zone and return a list
/// of hits ranked by the backend's own score.  Async because every
/// production impl talks a network transport (tonic gRPC to the
/// search-plugin).
#[async_trait]
pub trait LocalSearchBackend: Send + Sync {
    async fn search_zone(
        &self,
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
