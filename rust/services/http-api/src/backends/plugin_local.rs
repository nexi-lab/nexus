//! [`PluginLocalSearchBackend`] — [`LocalSearchBackend`] impl that
//! dials the daemon's OWN plugin (`nexus.search.v1.SearchService`)
//! through the shared [`crate::SearchBackend`] tonic-channel cache.
//!
//! # Why this shape
//!
//! The federated dispatcher's per-zone `search_zone(zone_id, req)`
//! call needs to reach the plugin.  Rather than growing the dispatcher
//! to know how to talk gRPC, we adapt the plugin's typed proto
//! response into a [`Hit`] here, once, at the seam.  Consequences:
//!
//! * the dispatcher stays transport-agnostic (a future in-process
//!   plugin path, an in-memory test double, etc. all fit the same
//!   trait);
//! * the attribution-field-to-`extras` stamping lives in ONE place —
//!   the bridge (`handlers::search_bridge`) reads back the same keys.
//!
//! # `extras` contract
//!
//! Every attribution field the plugin surfaces on `QueryResult`
//! lands on `Hit::extras` under the key names in
//! [`crate::handlers::search_bridge::EXTRAS_KEYS`].  The bridge
//! decodes back from those same keys, so a new attribution field
//! lands as one line here + one line in the bridge — no third
//! place to keep in sync.

use async_trait::async_trait;
use nexus_federated_search::{BackendError, LocalSearchBackend, SearchRequest};
use nexus_search_common::Hit;

use crate::backends::proto_bridge::hit_from_proto;
use crate::search_proto::{QueryRequest, QueryType};
use crate::SearchBackend;

/// Dispatches per-zone search requests through the shared
/// [`crate::SearchBackend`] tonic-channel cache.  Cheap to clone —
/// the underlying [`crate::SearchBackend`] is already `Arc`-backed.
pub struct PluginLocalSearchBackend {
    inner: SearchBackend,
}

impl PluginLocalSearchBackend {
    /// Wrap a shared [`crate::SearchBackend`].  The dispatcher's
    /// per-leg `search_zone` calls reuse the backend's cached
    /// tonic Channel — no per-leg dial.
    pub fn new(inner: SearchBackend) -> Self {
        Self { inner }
    }
}

#[async_trait]
impl LocalSearchBackend for PluginLocalSearchBackend {
    async fn search_zone(
        &self,
        zone_id: &str,
        req: &SearchRequest,
    ) -> Result<Vec<Hit>, BackendError> {
        // Map the wire `search_type` string onto the proto enum.  A
        // typo maps to the proto default (KEYWORD) — the axum handler
        // already rejects typos with 400 at the caller boundary via
        // `parse_query_type`, so by the time a request reaches this
        // impl the string was already normalised.  We're conservative
        // here anyway: unknown values → keyword.
        let query_type = match req.search_type.as_str() {
            "semantic" => QueryType::Semantic,
            "hybrid" => QueryType::Hybrid,
            _ => QueryType::Keyword,
        };
        let mut client = self
            .inner
            .client()
            .await
            .map_err(|e| BackendError::Transport(e.to_string()))?;
        let proto = QueryRequest {
            q: req.query.clone(),
            zone_id: zone_id.to_string(),
            limit: u32::try_from(req.limit).unwrap_or(u32::MAX),
            path_filter: req.path_filter.clone().unwrap_or_default(),
            query_type: query_type as i32,
            // The plugin uses `auth_token` for its own OperationContext
            // read-side gate.  Federated legs against the LOCAL plugin
            // pass through with no token — the axum handler has already
            // enforced the caller's zone allowlist upstream, so the
            // plugin trusts what it's asked.  A cross-daemon backend
            // would instead put its `SearchDelegation` here (PR-followup
            // task; see `backends::mod`).
            auth_token: String::new(),
            alpha: 0.0,
            fusion_method: 0,
            rrf_k: 0,
            chunks_per_page: 0,
            expand: String::new(),
            recency_mode: String::new(),
            recency_weight: 0.0,
            recency_half_life_days: 0.0,
            path_prefix_boosts: std::collections::HashMap::new(),
            path_filters: Vec::new(),
        };
        let resp = client
            .query(tonic::Request::new(proto))
            .await
            .map_err(|s| BackendError::Backend(s.message().to_string()))?
            .into_inner();
        if let Some(err) = resp.error {
            // The plugin's typed response carries an application-
            // level error string (e.g. "no index for zone X") on the
            // `error` field even on gRPC-OK.  Surface it as a
            // BackendError so the dispatcher lands the leg in
            // `zones_failed` rather than a silently-empty hit list.
            return Err(BackendError::Backend(err));
        }
        Ok(resp.results.into_iter().map(hit_from_proto).collect())
    }
}

// `hit_from_proto` moved to `crate::backends::proto_bridge` so
// `tonic_remote` shares the same impl — see that module for the
// SSOT rule and the extras-key contract.
