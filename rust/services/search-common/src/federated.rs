//! Cross-zone federated response envelope + the degrade-guard
//! predicate.
//!
//! The federated dispatcher fans a query out to N zones and returns
//! one envelope covering the whole set: the fused results, the zones
//! it reached, the zones it tried to reach and failed, and — for
//! SANDBOX-profile deployments — a `semantic_degraded` flag that
//! survives an empty result list so downstream consumers know the
//! answer is lossy.
//!
//! The predicate [`is_all_peers_failed`] tells the caller "this
//! response reflects zero reachable peers" so a SANDBOX profile can
//! fall back to local BM25S under a single deterministic check.

use serde::{Deserialize, Serialize};

use crate::results::Hit;

/// One zone the dispatcher tried to reach but failed on.
///
/// `error` is the operator-facing string the transport bubbled up —
/// kept as a plain `String` so a caller building an HTTP body can
/// serialise it verbatim without teaching this crate a wire enum.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ZoneFailure {
    pub zone_id: String,
    pub error: String,
}

/// The response envelope for a cross-zone federated search.
///
/// * `results` — the fused hit list (`Hit::zone_id` distinguishes
///   per-zone rows so a client rendering the list keeps provenance).
/// * `zones_searched` / `zones_failed` — the two disjoint sets a
///   caller inspects to compute completeness (`|failed| < |searched|`
///   ⇒ partial success; both empty ⇒ no peers configured).
/// * `zones_skipped` — zones the dispatcher intentionally did not
///   query (capability mismatch, no per-zone daemon wired).
/// * `latency_ms` — total dispatcher wall time (per-leg timings live
///   on [`Self::search_timing`]).
/// * `search_timing` — per-leg backend phase timings SUMMED across
///   local zones, keyed by [`crate::results::BACKEND_LEG_TIMING_KEYS`].
/// * `semantic_degraded` — set once (never cleared by fusion) when
///   any zone served this query with a degraded dense leg.  Survives
///   an empty results list so the signal reaches the client even
///   when a fully-filtered response returns zero rows.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct FederatedSearchResponse {
    pub results: Vec<Hit>,
    pub zones_searched: Vec<String>,
    pub zones_failed: Vec<ZoneFailure>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub zones_skipped: Vec<String>,
    #[serde(default)]
    pub latency_ms: f64,
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub cached: bool,
    #[serde(default, skip_serializing_if = "std::collections::BTreeMap::is_empty")]
    pub search_timing: std::collections::BTreeMap<String, f64>,
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub semantic_degraded: bool,
}

/// `true` when the response reflects zero reachable peers.
///
/// Two branches: (a) no peers were queried at all — the whole
/// dispatch produced an empty `zones_searched` and `zones_failed`,
/// so treat as "no peers configured, nothing to reach"; (b) at least
/// one peer was tried, none returned rows, and every tried peer is
/// in `zones_failed`.
///
/// The SANDBOX profile uses this to decide when to fall back to
/// local BM25S under the `semantic_degraded` flag.
pub fn is_all_peers_failed(response: &FederatedSearchResponse) -> bool {
    if response.zones_searched.is_empty() && response.zones_failed.is_empty() {
        return true;
    }
    response.results.is_empty() && response.zones_failed.len() >= response.zones_searched.len()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn no_peers_configured_reports_all_failed() {
        assert!(is_all_peers_failed(&FederatedSearchResponse::default()));
    }

    #[test]
    fn every_tried_peer_failed_reports_all_failed() {
        let r = FederatedSearchResponse {
            zones_searched: vec!["eng".into(), "legal".into()],
            zones_failed: vec![
                ZoneFailure {
                    zone_id: "eng".into(),
                    error: "conn refused".into(),
                },
                ZoneFailure {
                    zone_id: "legal".into(),
                    error: "timeout".into(),
                },
            ],
            ..Default::default()
        };
        assert!(is_all_peers_failed(&r));
    }

    #[test]
    fn partial_success_is_not_all_failed() {
        let r = FederatedSearchResponse {
            zones_searched: vec!["eng".into(), "legal".into()],
            zones_failed: vec![ZoneFailure {
                zone_id: "legal".into(),
                error: "timeout".into(),
            }],
            results: vec![Hit {
                path: "/x.md".into(),
                chunk_index: 0,
                chunk_text: "hit".into(),
                score: 1.0,
                zone_id: Some("eng".into()),
                extras: Default::default(),
            }],
            ..Default::default()
        };
        assert!(!is_all_peers_failed(&r));
    }

    #[test]
    fn some_peers_tried_all_failed_but_no_results_reports_all_failed() {
        // e.g. dispatcher tried 2, both timed out, no rows — the
        // SANDBOX guard MUST see this as "all peers down" so it
        // falls back to local BM25S.
        let r = FederatedSearchResponse {
            zones_searched: vec!["eng".into()],
            zones_failed: vec![ZoneFailure {
                zone_id: "eng".into(),
                error: "down".into(),
            }],
            ..Default::default()
        };
        assert!(is_all_peers_failed(&r));
    }

    #[test]
    fn partial_failure_with_zero_results_is_still_all_failed() {
        // zones_failed >= zones_searched with empty results means
        // every tried peer failed to produce anything usable.  The
        // guard fires the same way as the "no peers" branch.
        let r = FederatedSearchResponse {
            zones_searched: vec!["eng".into(), "legal".into()],
            zones_failed: vec![
                ZoneFailure {
                    zone_id: "eng".into(),
                    error: "e".into(),
                },
                ZoneFailure {
                    zone_id: "legal".into(),
                    error: "e".into(),
                },
            ],
            ..Default::default()
        };
        assert!(is_all_peers_failed(&r));
    }
}
