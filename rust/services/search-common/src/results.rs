//! [`Hit`] — the minimum shape every search-common consumer agrees
//! on, plus the shared [`BACKEND_LEG_TIMING_KEYS`] used to aggregate
//! per-leg backend phase timings.
//!
//! # Design
//!
//! Every backend has its own richer typed row (`QueryResult` in the
//! plugin proto, `SearchHit` in the http-api response body, etc.).
//! This crate deliberately does NOT try to unify all of them.  It
//! keeps a narrow struct that carries just what fusion / dedup /
//! cross-zone response building actually reads.  Callers convert
//! their own row into [`Hit`] on the way in and convert back on the
//! way out — the conversion is trivial and keeps the boundary honest.

use serde::{Deserialize, Serialize};

/// One search result at the fusion boundary.  Fields are the ones
/// fusion and the federated envelope actually read; everything else a
/// backend wants to carry rides on [`Hit::extras`] as opaque JSON so
/// this crate doesn't have to grow a field per backend.
///
/// `zone_id` is the source zone (federated search cares); `chunk_text`
/// is present so the envelope can be sent to the client without a
/// second fetch.  `score` is the fused rank score on output; on input
/// it's whatever the backend produced.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Hit {
    /// Absolute VFS path of the chunk's source file.
    pub path: String,
    /// Chunk index within the source file (0 for whole-file backends).
    pub chunk_index: u32,
    /// The chunk's text, ready to hand back to the caller.
    pub chunk_text: String,
    /// Ranker score.  Interpretation depends on the backend that
    /// produced the hit (BM25, cosine, fused, etc.).
    pub score: f64,
    /// Source zone id.  Set for cross-zone federated results so
    /// dedup can treat `zone_a:/foo` and `zone_b:/foo` as distinct
    /// hits.  `None` when the caller does not federate.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub zone_id: Option<String>,
    /// Backend-specific fields the fusion + envelope don't need to
    /// know about (title_score, tier_boost, recency_boost, etc.).
    /// Kept as a flat map so a caller reading the envelope can pull
    /// out whichever attribution field they care about without this
    /// crate growing to know it.
    #[serde(default, skip_serializing_if = "std::collections::BTreeMap::is_empty")]
    pub extras: std::collections::BTreeMap<String, serde_json::Value>,
}

impl Hit {
    /// Dedup key used by fusion.  When `zone_id` is set we prepend it
    /// so cross-zone hits at the same path don't collide; otherwise
    /// we key on `path:chunk_index` (matches the single-zone contract
    /// callers already implement).
    pub fn dedup_key(&self) -> String {
        match &self.zone_id {
            Some(zone) => format!("{zone}:{}:{}", self.path, self.chunk_index),
            None => format!("{}:{}", self.path, self.chunk_index),
        }
    }
}

/// Per-leg backend phase-timing keys surfaced on
/// `FederatedSearchResponse::search_timing` and echoed by the http-api
/// query router in its response envelope.  Federated search sums
/// per-peer legs into the aggregate under the same keys — so a cold
/// federated query surfaces the same index-load phase split as a
/// single-zone one.
///
/// Kept as a `&[&str]` (not an enum) because the keyset is a wire
/// contract observable in the response JSON: adding a key is
/// additive, and consumers that already read a hard-coded subset
/// keep working.
pub const BACKEND_LEG_TIMING_KEYS: &[&str] = &[
    "backend_ms",
    "embed_ms",
    "keyword_ms",
    "page_keyword_ms",
    "title_ms",
    "vector_ms",
    "fusion_ms",
    "rerank_ms",
    "index_load_ms",
    "fallback_ms",
];

#[cfg(test)]
mod tests {
    use super::*;

    fn hit(path: &str, chunk_index: u32, score: f64) -> Hit {
        Hit {
            path: path.to_string(),
            chunk_index,
            chunk_text: format!("body of {path}#{chunk_index}"),
            score,
            zone_id: None,
            extras: Default::default(),
        }
    }

    #[test]
    fn dedup_key_uses_path_and_chunk_when_no_zone() {
        assert_eq!(hit("/a.md", 0, 0.0).dedup_key(), "/a.md:0");
        assert_eq!(hit("/a.md", 3, 0.0).dedup_key(), "/a.md:3");
    }

    #[test]
    fn dedup_key_prepends_zone_when_set() {
        let mut h = hit("/a.md", 0, 0.0);
        h.zone_id = Some("eng".to_string());
        assert_eq!(h.dedup_key(), "eng:/a.md:0");
    }

    #[test]
    fn cross_zone_hits_at_same_path_dedup_distinctly() {
        let mut a = hit("/a.md", 0, 0.0);
        a.zone_id = Some("eng".to_string());
        let mut b = hit("/a.md", 0, 0.0);
        b.zone_id = Some("legal".to_string());
        assert_ne!(a.dedup_key(), b.dedup_key());
    }

    #[test]
    fn backend_leg_timing_keys_include_the_expected_phases() {
        // Kept small enough to exhaustively spot a rename or accidental drop.
        for k in [
            "backend_ms",
            "keyword_ms",
            "vector_ms",
            "fusion_ms",
            "index_load_ms",
            "fallback_ms",
        ] {
            assert!(BACKEND_LEG_TIMING_KEYS.contains(&k), "missing key: {k}");
        }
    }
}
