//! [`hit_from_proto`] — the ONE place converting the
//! [`crate::search_proto::QueryResult`] wire row into
//! [`nexus_search_common::Hit`] the federated dispatcher +
//! [`crate::handlers::search_bridge`] work with.
//!
//! # Why extracted
//!
//! Both [`crate::backends::plugin_local::PluginLocalSearchBackend`]
//! (dials the local plugin) and
//! [`crate::backends::tonic_remote::TonicRemoteSearchBackend`] (dials
//! a peer daemon's plugin) receive the identical
//! [`crate::search_proto::QueryResponse`] shape.  Duplicating the
//! attribution-to-extras mapping across both callers is a DRY break;
//! sharing one impl here means a new proto attribution field lands
//! as one branch here + one entry in
//! [`crate::handlers::search_bridge::EXTRAS_KEYS`].  Two places, not
//! three.
//!
//! # `extras` contract
//!
//! Every non-`None` optional field on
//! [`crate::search_proto::QueryResult`] lands on
//! [`nexus_search_common::Hit::extras`] under the key names in
//! [`crate::handlers::search_bridge::EXTRAS_KEYS`].  Absent optional
//! fields drop off — a proto row with only the core fields produces
//! a [`nexus_search_common::Hit`] with an empty `extras` (not zero-
//! valued defaults that would silently claim attribution never
//! applied).

use nexus_search_common::Hit;
use serde_json::json;

use crate::search_proto::QueryResult as ProtoQueryResult;

/// Convert a [`ProtoQueryResult`] into a [`Hit`], stamping every
/// present attribution field onto [`Hit::extras`] under the wire-
/// contract keys.  See the module docstring for the SSOT rule.
pub(crate) fn hit_from_proto(r: ProtoQueryResult) -> Hit {
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
        // Federated dispatch always names a source zone so hits
        // from different zones dedup distinctly (see
        // `Hit::dedup_key`).  Empty `zone_id` on the proto → treat
        // as "same zone as the request", which is what the caller
        // asked for.
        zone_id: (!r.zone_id.is_empty()).then_some(r.zone_id),
        extras,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

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
    fn carries_every_typed_field_onto_extras() {
        let h = hit_from_proto(proto_hit_full());
        assert_eq!(h.path, "/eng/a.md");
        assert_eq!(h.chunk_index, 3);
        assert_eq!(h.chunk_text, "hello");
        assert!((h.score - 1.5).abs() < 1e-6);
        assert_eq!(h.zone_id.as_deref(), Some("eng"));
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
    fn drops_absent_optional_fields() {
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
    fn empty_zone_id_maps_to_none() {
        let mut r = proto_hit_full();
        r.zone_id = String::new();
        let h = hit_from_proto(r);
        assert_eq!(h.zone_id, None);
    }

    #[test]
    fn extras_keys_match_bridge_contract() {
        // Pin the extras key strings against the bridge's constant so
        // a rename on one side breaks the build loudly on the other.
        use crate::handlers::search_bridge::EXTRAS_KEYS;
        let h = hit_from_proto(proto_hit_full());
        for k in h.extras.keys() {
            assert!(
                EXTRAS_KEYS.contains(&k.as_str()),
                "key {k:?} not in EXTRAS_KEYS — bridge would silently drop it",
            );
        }
    }
}
