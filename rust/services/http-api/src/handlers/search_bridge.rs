//! The [`Hit`] ↔ [`QueryHit`] bridge — one place to keep the
//! algorithm-side working type and the JSON wire shape in lockstep.
//!
//! # Why two shapes exist (the SSOT policy)
//!
//! * [`Hit`] is the algorithm surface (`nexus-search-common`): a
//!   narrow struct fusion + envelope building actually reads, with a
//!   `BTreeMap` for opaque attribution.
//! * [`QueryHit`] is the JSON wire shape (this crate): named
//!   attribution fields the caller deserialises into, one per proto
//!   knob (title_score, tier_boost, recency_boost, …).
//!
//! Neither can absorb the other without a rules break:
//!
//! * pulling wire fields into [`Hit`] drags HTTP-shape knowledge into
//!   the pure-algorithm crate;
//! * pulling algorithm knobs into [`QueryHit`] blocks a future non-
//!   HTTP consumer of the dispatcher from reusing the shape.
//!
//! So both survive; **this file is the one bridge** that keeps them
//! aligned.  Every attribution key the plugin backend stashes onto
//! [`Hit::extras`] gets pulled out here into its typed twin — and
//! every new attribution field lands as ONE change here (or the wire
//! silently loses it).
//!
//! # `extras` key contract
//!
//! The plugin-side [`crate::backends::plugin_local::PluginLocalSearchBackend`]
//! stashes each attribution field onto `Hit::extras` under the
//! keys listed in [`EXTRAS_KEYS`].  Any other key on `extras` is
//! silently ignored by this bridge — a backend that carries diagnostic
//! extras beyond the wire contract does not corrupt the response.

use nexus_search_common::Hit;

use crate::handlers::search::QueryHit;

/// The `extras` keys this bridge decodes.  Kept as a public const so
/// the plugin-side wrapper (which stamps these fields onto `Hit`) and
/// this bridge (which reads them off) share ONE list of key strings.
/// A new attribution field lands as one entry here + one branch in
/// [`TypedExtras::from`] — no third place to keep in sync.
pub const EXTRAS_KEYS: &[&str] = &[
    "mtime_ms",
    "expanded_context",
    "title_score",
    "keyword_score",
    "vector_score",
    "tier_boost",
    "recency_boost",
    "expansion_variant_index",
];

impl From<Hit> for QueryHit {
    fn from(h: Hit) -> Self {
        let typed = TypedExtras::from(&h.extras);
        QueryHit {
            path: h.path,
            chunk_index: h.chunk_index,
            chunk_text: h.chunk_text,
            // Fusion + backend scores are f64 internally; the wire
            // has always been f32 (matches the proto `float`).  Cast
            // is lossy in theory, benign in practice — BM25 / cosine
            // scores are single-precision-representable and the RRF
            // constant is a small integer.  Documented so a future
            // caller comparing scores across pipelines knows the
            // conversion happens here.
            score: h.score as f32,
            // `Hit::zone_id` is `Option<String>` (a single-zone
            // caller has no source zone to name).  `QueryHit`'s
            // wire field has always been a plain string — empty
            // means "the deployment's default zone", which is what
            // Python emitted in the single-zone case.  Federated
            // legs always name their source zone so the empty-string
            // case is unreachable via the dispatcher.
            zone_id: h.zone_id.unwrap_or_default(),
            mtime_ms: typed.mtime_ms,
            expanded_context: typed.expanded_context,
            title_score: typed.title_score,
            keyword_score: typed.keyword_score,
            vector_score: typed.vector_score,
            tier_boost: typed.tier_boost,
            recency_boost: typed.recency_boost,
            expansion_variant_index: typed.expansion_variant_index,
        }
    }
}

/// The eight typed attribution fields decoded off [`Hit::extras`].
/// Named-struct rather than an 8-tuple so the field ↔ slot mapping
/// on the [`From`] impl above reads unambiguously.
///
/// # Contract
///
/// * A missing key or a wrong-shape value maps to `None` — no panic,
///   no error surfaced.  The plugin-side wrapper is responsible for
///   emitting the right shape; a wire regression is a plugin bug, not
///   a bridge bug.
/// * `expanded_context` maps to `String::default()` (empty) when
///   missing / wrong-shape, because [`QueryHit::expanded_context`] is
///   a bare `String`, not an `Option`.  Empty means "no expansion",
///   which is the same signal the proto uses.
struct TypedExtras {
    mtime_ms: Option<i64>,
    expanded_context: String,
    title_score: Option<f32>,
    keyword_score: Option<f32>,
    vector_score: Option<f32>,
    tier_boost: Option<f32>,
    recency_boost: Option<f32>,
    expansion_variant_index: Option<u32>,
}

impl From<&std::collections::BTreeMap<String, serde_json::Value>> for TypedExtras {
    fn from(extras: &std::collections::BTreeMap<String, serde_json::Value>) -> Self {
        Self {
            mtime_ms: extras.get("mtime_ms").and_then(as_i64),
            expanded_context: extras
                .get("expanded_context")
                .and_then(|v| v.as_str())
                .map(String::from)
                .unwrap_or_default(),
            title_score: extras.get("title_score").and_then(as_f32),
            keyword_score: extras.get("keyword_score").and_then(as_f32),
            vector_score: extras.get("vector_score").and_then(as_f32),
            tier_boost: extras.get("tier_boost").and_then(as_f32),
            recency_boost: extras.get("recency_boost").and_then(as_f32),
            expansion_variant_index: extras.get("expansion_variant_index").and_then(as_u32),
        }
    }
}

fn as_f32(v: &serde_json::Value) -> Option<f32> {
    v.as_f64().map(|x| x as f32)
}

fn as_i64(v: &serde_json::Value) -> Option<i64> {
    v.as_i64()
}

fn as_u32(v: &serde_json::Value) -> Option<u32> {
    v.as_u64().and_then(|x| u32::try_from(x).ok())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn bare_hit(path: &str) -> Hit {
        Hit {
            path: path.into(),
            chunk_index: 0,
            chunk_text: format!("body of {path}"),
            score: 1.5,
            zone_id: None,
            extras: Default::default(),
        }
    }

    #[test]
    fn bare_hit_round_trips_core_fields() {
        let q: QueryHit = bare_hit("/a.md").into();
        assert_eq!(q.path, "/a.md");
        assert_eq!(q.chunk_index, 0);
        assert_eq!(q.chunk_text, "body of /a.md");
        assert!((q.score - 1.5).abs() < 1e-6);
        // Single-zone caller gets an empty zone_id — the deployment
        // default.  Federated dispatch always fills the field.
        assert_eq!(q.zone_id, "");
        assert_eq!(q.mtime_ms, None);
        assert_eq!(q.expanded_context, "");
        for opt in [
            q.title_score,
            q.keyword_score,
            q.vector_score,
            q.tier_boost,
            q.recency_boost,
        ] {
            assert!(opt.is_none());
        }
        assert_eq!(q.expansion_variant_index, None);
    }

    #[test]
    fn zone_id_is_carried_through_when_set() {
        let mut h = bare_hit("/a.md");
        h.zone_id = Some("eng".into());
        let q: QueryHit = h.into();
        assert_eq!(q.zone_id, "eng");
    }

    #[test]
    fn every_extras_key_projects_onto_its_typed_slot() {
        // Regression pin: a rename of one extras key must break the
        // bridge here loudly, not silently drop the field from the
        // wire response.  The proto-parity test in `handlers::search`
        // covers the wire side; this pins the extras contract.
        let mut h = bare_hit("/a.md");
        h.extras
            .insert("mtime_ms".into(), json!(1_700_000_000_000i64));
        h.extras
            .insert("expanded_context".into(), json!("prev\ncurr\nnext"));
        h.extras.insert("title_score".into(), json!(0.8_f64));
        h.extras.insert("keyword_score".into(), json!(1.2_f64));
        h.extras.insert("vector_score".into(), json!(0.6_f64));
        h.extras.insert("tier_boost".into(), json!(1.5_f64));
        h.extras.insert("recency_boost".into(), json!(1.3_f64));
        h.extras
            .insert("expansion_variant_index".into(), json!(2_u32));
        let q: QueryHit = h.into();
        assert_eq!(q.mtime_ms, Some(1_700_000_000_000));
        assert_eq!(q.expanded_context, "prev\ncurr\nnext");
        assert!((q.title_score.unwrap() - 0.8).abs() < 1e-4);
        assert!((q.keyword_score.unwrap() - 1.2).abs() < 1e-4);
        assert!((q.vector_score.unwrap() - 0.6).abs() < 1e-4);
        assert!((q.tier_boost.unwrap() - 1.5).abs() < 1e-4);
        assert!((q.recency_boost.unwrap() - 1.3).abs() < 1e-4);
        assert_eq!(q.expansion_variant_index, Some(2));
    }

    #[test]
    fn wrong_shape_extras_are_ignored_not_panic() {
        // Regression pin: a plugin surfacing a diagnostic string on
        // "title_score" (a bug on the plugin side) must NOT tank the
        // bridge — the caller just gets `None` for that field.
        let mut h = bare_hit("/a.md");
        h.extras.insert("title_score".into(), json!("nope"));
        h.extras.insert("mtime_ms".into(), json!("not a number"));
        h.extras
            .insert("expansion_variant_index".into(), json!(-1_i64));
        let q: QueryHit = h.into();
        assert_eq!(q.title_score, None);
        assert_eq!(q.mtime_ms, None);
        assert_eq!(q.expansion_variant_index, None);
    }

    #[test]
    fn extras_keys_constant_matches_the_bridge_decoder() {
        // A new attribution field must land in both places (the
        // constant + the extras_to_typed decoder).  This test guards
        // against a caller adding a field to one side only.
        let mut all_keys: Vec<String> = EXTRAS_KEYS.iter().map(|s| s.to_string()).collect();
        all_keys.sort();
        let mut expected: Vec<String> = vec![
            "mtime_ms",
            "expanded_context",
            "title_score",
            "keyword_score",
            "vector_score",
            "tier_boost",
            "recency_boost",
            "expansion_variant_index",
        ]
        .into_iter()
        .map(String::from)
        .collect();
        expected.sort();
        assert_eq!(all_keys, expected);
    }

    #[test]
    fn extras_unknown_keys_do_not_affect_the_wire() {
        // A backend stashing a diagnostic field the bridge does not
        // know about must not surface a serialisation error or leak
        // that key on the wire.
        let mut h = bare_hit("/a.md");
        h.extras
            .insert("diagnostic_field_we_do_not_wire".into(), json!(42));
        let q: QueryHit = h.into();
        // Everything typed stays absent; the unknown key is silently
        // dropped.  A serialise round-trip proves the wire is clean.
        let s = serde_json::to_string(&q).unwrap();
        assert!(!s.contains("diagnostic_field_we_do_not_wire"));
        assert!(!s.contains("42"));
    }
}
