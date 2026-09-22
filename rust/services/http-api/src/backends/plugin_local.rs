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
use serde_json::json;

use crate::search_proto::{QueryRequest, QueryResult as ProtoQueryResult, QueryType};
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

/// Stamp every attribution field on `Hit::extras` under the key the
/// bridge decodes.  Any field the proto marks optional round-trips
/// as `None` → absent key; the bridge treats a missing key as
/// "attribution did not apply" (per [`crate::handlers::search_bridge`]).
fn hit_from_proto(r: ProtoQueryResult) -> Hit {
    let mut extras = std::collections::BTreeMap::new();
    if let Some(v) = r.mtime_ms {
        extras.insert("mtime_ms".into(), json!(v));
    }
    if !r.expanded_context.is_empty() {
        extras.insert("expanded_context".into(), json!(r.expanded_context));
    }
    if let Some(v) = r.title_score {
        extras.insert("title_score".into(), json!(v));
    }
    if let Some(v) = r.keyword_score {
        extras.insert("keyword_score".into(), json!(v));
    }
    if let Some(v) = r.vector_score {
        extras.insert("vector_score".into(), json!(v));
    }
    if let Some(v) = r.tier_boost {
        extras.insert("tier_boost".into(), json!(v));
    }
    if let Some(v) = r.recency_boost {
        extras.insert("recency_boost".into(), json!(v));
    }
    if let Some(v) = r.expansion_variant_index {
        extras.insert("expansion_variant_index".into(), json!(v));
    }
    Hit {
        path: r.path,
        chunk_index: r.chunk_index,
        chunk_text: r.chunk_text,
        score: f64::from(r.score),
        // Federated dispatch always names a source zone so hits from
        // different zones dedup distinctly (see `Hit::dedup_key`).
        // Empty `zone_id` on the proto → treat as "same zone as the
        // request", which is what the caller asked for.
        zone_id: (!r.zone_id.is_empty()).then_some(r.zone_id),
        extras,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::search_proto::QueryResult as ProtoQueryResult;

    fn proto_hit_full() -> ProtoQueryResult {
        ProtoQueryResult {
            path: "/eng/a.md".into(),
            chunk_index: 3,
            chunk_text: "hello".into(),
            score: 1.5,
            zone_id: "eng".into(),
            mtime_ms: Some(1_700_000_000_000),
            expanded_context: "prev\ncurr\nnext".into(),
            title_score: Some(0.8),
            keyword_score: Some(1.2),
            vector_score: Some(0.6),
            tier_boost: Some(1.5),
            recency_boost: Some(1.3),
            expansion_variant_index: Some(2),
        }
    }

    #[test]
    fn hit_from_proto_carries_every_typed_field_onto_extras() {
        let h = hit_from_proto(proto_hit_full());
        assert_eq!(h.path, "/eng/a.md");
        assert_eq!(h.chunk_index, 3);
        assert_eq!(h.chunk_text, "hello");
        assert!((h.score - 1.5).abs() < 1e-6);
        assert_eq!(h.zone_id.as_deref(), Some("eng"));
        // Every attribution key present under its `extras` slot.
        for k in [
            "mtime_ms",
            "expanded_context",
            "title_score",
            "keyword_score",
            "vector_score",
            "tier_boost",
            "recency_boost",
            "expansion_variant_index",
        ] {
            assert!(
                h.extras.contains_key(k),
                "missing key {k} on extras: {:?}",
                h.extras.keys().collect::<Vec<_>>(),
            );
        }
    }

    #[test]
    fn hit_from_proto_drops_absent_optional_fields() {
        // A proto row with only the core fields must produce a Hit
        // with an empty `extras` (not a Hit with every key set to
        // its zero value — that would silently claim "keyword score
        // is 0.0" for a semantic-only result).
        let mut r = proto_hit_full();
        r.mtime_ms = None;
        r.expanded_context = String::new();
        r.title_score = None;
        r.keyword_score = None;
        r.vector_score = None;
        r.tier_boost = None;
        r.recency_boost = None;
        r.expansion_variant_index = None;
        let h = hit_from_proto(r);
        assert!(h.extras.is_empty(), "expected empty, got {:?}", h.extras);
    }

    #[test]
    fn hit_from_proto_empty_zone_id_maps_to_none() {
        let mut r = proto_hit_full();
        r.zone_id = String::new();
        let h = hit_from_proto(r);
        assert_eq!(h.zone_id, None);
    }

    #[test]
    fn hit_from_proto_extras_keys_match_bridge_contract() {
        // Pin the extras key strings against the bridge's constant so
        // a rename on one side breaks the build loudly on the other.
        use crate::handlers::search_bridge::EXTRAS_KEYS;
        let h = hit_from_proto(proto_hit_full());
        // Every key we stamped must be one the bridge knows about.
        for k in h.extras.keys() {
            assert!(
                EXTRAS_KEYS.contains(&k.as_str()),
                "key {k:?} not in EXTRAS_KEYS — bridge would silently drop it",
            );
        }
    }
}
