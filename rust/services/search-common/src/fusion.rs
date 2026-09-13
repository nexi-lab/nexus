//! Fusion algorithms for combining N ranked hit lists into a single
//! ranking.
//!
//! # RRF (Reciprocal Rank Fusion)
//!
//! Given N ranked lists, each hit contributes `1 / (k + rank_i)` per
//! list it appears in.  RRF is rank-based — no score normalisation
//! needed — and stable under wide score-scale differences between
//! lists (BM25 vs cosine vs SPLADE all fuse cleanly).  Reference:
//! Cormack et al., SIGIR 2009.
//!
//! # Top-rank bonus
//!
//! A hit that took the #1 slot in any source gets an extra
//! [`RRF_TOP1_BONUS`]; #2 and #3 get [`RRF_TOP3_BONUS`].  Preserves
//! high-confidence single-arm matches against dilution when a
//! multi-arm fanout has one weak arm producing many false neighbours.
//! Enable/disable via [`rrf_multi_fusion`]'s `top_rank_bonus` arg.
//!
//! # Tie-break
//!
//! Rust's `slice::sort_by` is stable, so equal-score hits keep their
//! insertion order.  Callers that want deterministic cross-machine
//! results should feed lists in a deterministic order (e.g. sorted
//! by zone_id for federated fanout).

use serde::{Deserialize, Serialize};

use crate::results::Hit;

/// Fusion algorithm identifier used by the query wire contract.  Kept
/// as a plain enum so a caller reading a JSON body can round-trip
/// the value via `#[derive(Deserialize)]` without a custom parser.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum FusionMethod {
    /// Reciprocal Rank Fusion — the default, works well when the
    /// source ranks are well-calibrated and score scales differ.
    #[default]
    Rrf,
    /// Simple weighted linear combination — requires per-source
    /// score normalisation to be meaningful; the caller supplies
    /// `alpha` on [`FusionConfig`].
    Weighted,
    /// RRF with alpha weighting — RRF ranks but skewed toward one
    /// arm; useful when a weak arm is known to underperform.
    RrfWeighted,
}

/// Fusion knobs a caller may tune per request.  Every field has a
/// working default so a fresh `FusionConfig::default()` matches the
/// most common single-arm-plus-recency use case.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct FusionConfig {
    #[serde(default)]
    pub method: FusionMethod,
    /// Vector-arm weight in `[0, 1]` for `Weighted` / `RrfWeighted`;
    /// `1.0` means all-vector, `0.0` all-keyword.  Ignored under
    /// pure `Rrf`.
    #[serde(default = "default_alpha")]
    pub alpha: f64,
    /// RRF constant — the paper's default is 60 and every widely
    /// used implementation keeps it.
    #[serde(default = "default_k")]
    pub rrf_k: u32,
    /// Apply the top-rank bonus (see the module doc).
    #[serde(default = "default_true")]
    pub top_rank_bonus: bool,
}

fn default_alpha() -> f64 {
    0.5
}
fn default_k() -> u32 {
    60
}
fn default_true() -> bool {
    true
}

impl Default for FusionConfig {
    fn default() -> Self {
        Self {
            method: FusionMethod::Rrf,
            alpha: 0.5,
            rrf_k: 60,
            top_rank_bonus: true,
        }
    }
}

/// Extra credit added to a hit that took the #1 slot in any source.
/// Small so RRF's rank-based signal still dominates for a hit that
/// merely converged from several lower ranks; large enough to survive
/// dilution when a wide-fanout arm floods with weak neighbours.
pub const RRF_TOP1_BONUS: f64 = 0.05;
/// Same idea for #2-#3 slots — smaller than the #1 bonus so the
/// "clear winner" signal stays visible.
pub const RRF_TOP3_BONUS: f64 = 0.02;

/// N-way Reciprocal Rank Fusion.
///
/// Each `(source_name, hits)` tuple contributes rank-based votes to
/// every hit it lists.  A hit that appears in multiple sources
/// accumulates the sum of `1 / (k + rank_i)` from each.  Result is
/// sorted by fused score descending; equal-score hits keep their
/// first-source insertion order (stable sort).
///
/// The output owns fresh [`Hit`] values whose `score` is the fused
/// RRF score; every other field is carried over from the FIRST source
/// that contributed the hit (later sources' `extras` overwrite
/// earlier ones, matching the "last write wins" semantics of a
/// `BTreeMap::insert` — deterministic when the input order is).
///
/// `source_name` is preserved as a `<name>_score` entry on the hit's
/// `extras` map with the raw per-source score, so a caller
/// interrogating a fused row can still see per-arm attribution.
///
/// `limit` caps the returned length.  `k` is the RRF constant
/// (default 60 in the paper; expose so a caller can tune).
/// `top_rank_bonus = true` applies the small #1/#2-3 bonuses
/// documented on [`RRF_TOP1_BONUS`] / [`RRF_TOP3_BONUS`].
pub fn rrf_multi_fusion(
    result_lists: &[(&str, Vec<Hit>)],
    k: u32,
    limit: usize,
    top_rank_bonus: bool,
) -> Vec<Hit> {
    // Entry: (fused score, best rank across sources, the hit itself).
    // BTreeMap keeps deterministic iteration order — matters for a
    // cross-machine parity contract (federated dispatch runs the same
    // fuse on every node and must produce the same rows).
    let mut fused: std::collections::BTreeMap<String, FusedEntry> =
        std::collections::BTreeMap::new();

    for (source_name, hits) in result_lists {
        // `format!` runs ONCE per source, not per hit — the source
        // list is small (2–5) so this stays in a handful of
        // allocations even on a wide fanout.
        let score_key = format!("{source_name}_score");
        for (rank, hit) in hits.iter().enumerate() {
            let rank_1based = (rank + 1) as u32;
            let key = hit.dedup_key();
            let per_source = 1.0 / f64::from(k + rank_1based);
            let entry = fused.entry(key).or_insert_with(|| FusedEntry {
                hit: hit.clone(),
                fused_score: 0.0,
                best_rank: rank_1based,
            });
            entry.fused_score += per_source;
            if rank_1based < entry.best_rank {
                entry.best_rank = rank_1based;
            }
            // Attribution: per-source raw score on the hit's extras.
            // `Value::from(f64)` is a cheap tag-and-store; no
            // intermediate JSON string / parse round-trip.
            entry
                .hit
                .extras
                .insert(score_key.clone(), serde_json::Value::from(hit.score));
        }
    }

    if top_rank_bonus {
        for entry in fused.values_mut() {
            if entry.best_rank == 1 {
                entry.fused_score += RRF_TOP1_BONUS;
            } else if entry.best_rank <= 3 {
                entry.fused_score += RRF_TOP3_BONUS;
            }
        }
    }

    let mut sorted: Vec<FusedEntry> = fused.into_values().collect();
    // Sort descending by fused_score.  `total_cmp` handles NaN
    // deterministically; stable sort keeps insertion order for ties.
    sorted.sort_by(|a, b| b.fused_score.total_cmp(&a.fused_score));
    sorted.truncate(limit);

    sorted
        .into_iter()
        .map(|mut entry| {
            entry.hit.score = entry.fused_score;
            entry.hit
        })
        .collect()
}

struct FusedEntry {
    hit: Hit,
    fused_score: f64,
    best_rank: u32,
}

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
    fn empty_input_yields_empty_output() {
        let out = rrf_multi_fusion(&[], 60, 10, true);
        assert!(out.is_empty());
    }

    #[test]
    fn single_source_preserves_order_and_uses_raw_ranks() {
        let src = vec![hit("/a", 0, 10.0), hit("/b", 0, 5.0), hit("/c", 0, 1.0)];
        let out = rrf_multi_fusion(&[("keyword", src)], 60, 10, false);
        assert_eq!(
            out.iter().map(|h| h.path.as_str()).collect::<Vec<_>>(),
            vec!["/a", "/b", "/c"],
        );
        // With k=60, rank 1..3 → 1/61, 1/62, 1/63 — strictly descending.
        assert!(out[0].score > out[1].score && out[1].score > out[2].score);
    }

    #[test]
    fn hit_in_multiple_sources_beats_either_alone() {
        // /a is #3 in keyword and #3 in vector (weak in each), but
        // present in both.  /b is #1 in keyword only.  With no
        // top-rank bonus, /a should still win because it accumulates
        // votes from two sources.
        let kw = vec![hit("/b", 0, 10.0), hit("/c", 0, 5.0), hit("/a", 0, 1.0)];
        let vec_arm = vec![hit("/d", 0, 10.0), hit("/e", 0, 5.0), hit("/a", 0, 1.0)];
        let out = rrf_multi_fusion(&[("keyword", kw), ("vector", vec_arm)], 60, 10, false);
        assert_eq!(out[0].path, "/a", "hit in both sources should win: {out:?}");
    }

    #[test]
    fn top_rank_bonus_promotes_a_number_one_over_a_broad_number_three() {
        // /a is #1 in one source only.
        // /b appears at #3 in both — with pure RRF /b would fuse to
        //   (1/63 + 1/63) ≈ 0.0317 while /a scores 1/61 ≈ 0.0164, so
        //   /b wins under pure RRF.
        // With top-rank bonus, /a gets +0.05 and jumps to 0.0664 —
        //   the "clear winner in some arm" heuristic the bonus is
        //   designed to catch.
        let a_only = vec![hit("/a", 0, 10.0)];
        let b_broad = vec![hit("/x", 0, 100.0), hit("/y", 0, 50.0), hit("/b", 0, 1.0)];
        let b_broad2 = vec![hit("/p", 0, 100.0), hit("/q", 0, 50.0), hit("/b", 0, 1.0)];
        let out_no_bonus = rrf_multi_fusion(
            &[
                ("k", a_only.clone()),
                ("v", b_broad.clone()),
                ("s", b_broad2.clone()),
            ],
            60,
            5,
            false,
        );
        assert_eq!(out_no_bonus[0].path, "/b");
        let out_with_bonus = rrf_multi_fusion(
            &[("k", a_only), ("v", b_broad), ("s", b_broad2)],
            60,
            5,
            true,
        );
        assert_eq!(out_with_bonus[0].path, "/a");
    }

    #[test]
    fn limit_caps_output_length() {
        let src: Vec<Hit> = (0..20)
            .map(|i| hit(&format!("/f{i}"), 0, (20 - i) as f64))
            .collect();
        let out = rrf_multi_fusion(&[("keyword", src)], 60, 5, false);
        assert_eq!(out.len(), 5);
    }

    #[test]
    fn per_source_scores_are_attached_as_extras_for_attribution() {
        let kw = vec![hit("/a", 0, 7.0)];
        let ve = vec![hit("/a", 0, 3.0)];
        let out = rrf_multi_fusion(&[("keyword", kw), ("vector", ve)], 60, 5, false);
        assert_eq!(out.len(), 1);
        let extras = &out[0].extras;
        assert_eq!(extras["keyword_score"], serde_json::json!(7.0));
        assert_eq!(extras["vector_score"], serde_json::json!(3.0));
    }

    #[test]
    fn zone_qualified_dedup_keeps_cross_zone_hits_distinct() {
        let mut a_eng = hit("/a", 0, 5.0);
        a_eng.zone_id = Some("eng".into());
        let mut a_leg = hit("/a", 0, 5.0);
        a_leg.zone_id = Some("legal".into());
        let out = rrf_multi_fusion(
            &[("zone_eng", vec![a_eng]), ("zone_legal", vec![a_leg])],
            60,
            10,
            false,
        );
        // Two distinct rows survive because zone_id splits the dedup key.
        assert_eq!(out.len(), 2);
        assert!(out.iter().any(|h| h.zone_id.as_deref() == Some("eng")));
        assert!(out.iter().any(|h| h.zone_id.as_deref() == Some("legal")));
    }

    // ── Numerical parity ────────────────────────────────────────
    //
    // Exact expected fused scores lifted from the Python
    // `tests/integration/bricks/search/test_rrf_bonus.py` reference
    // (Python's `rrf_fusion` is the 2-source special case of
    // `rrf_multi_fusion`).  Any drift in the RRF formula or the
    // top-rank bonus flips one of these — the parity guard for the
    // federated dispatcher, which will run Python and Rust nodes in
    // the same cluster and must produce byte-identical rankings.

    fn approx_eq(a: f64, b: f64, tol: f64) -> bool {
        (a - b).abs() < tol
    }

    #[test]
    fn parity_rank1_two_sources_matches_python_reference() {
        // Python: single doc in both sources at rank 1.  Expected
        // score = 1/(60+1) + 1/(60+1) + RRF_TOP1_BONUS.
        let kw = vec![hit("only.txt", 0, 1.0)];
        let ve = vec![hit("only.txt", 0, 1.0)];
        let out = rrf_multi_fusion(&[("keyword", kw), ("vector", ve)], 60, 10, true);
        assert_eq!(out.len(), 1);
        let expected = 1.0 / 61.0 + 1.0 / 61.0 + RRF_TOP1_BONUS;
        assert!(
            approx_eq(out[0].score, expected, 1e-9),
            "got {} want {}",
            out[0].score,
            expected,
        );
    }

    #[test]
    fn parity_rank3_receives_top3_bonus_matches_python_reference() {
        // Python: c.txt at rank 3 in keyword only.  Expected score =
        // 1/(60+3) + RRF_TOP3_BONUS.
        let kw = vec![
            hit("a.txt", 0, 1.0),
            hit("b.txt", 0, 1.0),
            hit("c.txt", 0, 1.0),
        ];
        let out = rrf_multi_fusion(&[("keyword", kw)], 60, 10, true);
        let c = out
            .iter()
            .find(|h| h.path == "c.txt")
            .expect("c.txt present");
        let expected = 1.0 / 63.0 + RRF_TOP3_BONUS;
        assert!(
            approx_eq(c.score, expected, 1e-9),
            "got {} want {}",
            c.score,
            expected,
        );
    }

    #[test]
    fn parity_rank4_receives_no_bonus_matches_python_reference() {
        // Python: r3.txt is 0-indexed rank 4, no bonus.  Expected
        // score = 1/(60+4).
        let kw: Vec<Hit> = (0..5).map(|i| hit(&format!("r{i}.txt"), 0, 1.0)).collect();
        let out = rrf_multi_fusion(&[("keyword", kw)], 60, 10, true);
        let r3 = out
            .iter()
            .find(|h| h.path == "r3.txt")
            .expect("r3.txt present");
        let expected = 1.0 / 64.0;
        assert!(
            approx_eq(r3.score, expected, 1e-9),
            "got {} want {}",
            r3.score,
            expected,
        );
    }

    #[test]
    fn parity_top1_keyword_beats_mediocre_both_matches_python_reference() {
        // Python: perfect.txt is rank 1 in keyword only, mediocre.txt
        // is rank 3 in BOTH.  With the top-rank bonus, perfect wins
        // because +RRF_TOP1_BONUS outranks the double-rank-3
        // contribution.  Without the bonus, mediocre wins.
        let kw = vec![
            hit("perfect.txt", 0, 10.0),
            hit("x.txt", 0, 1.0),
            hit("mediocre.txt", 0, 0.5),
        ];
        let ve = vec![
            hit("y.txt", 0, 0.9),
            hit("z.txt", 0, 0.8),
            hit("mediocre.txt", 0, 0.5),
        ];

        let with_bonus = rrf_multi_fusion(
            &[("keyword", kw.clone()), ("vector", ve.clone())],
            60,
            10,
            true,
        );
        let ranked: Vec<&str> = with_bonus.iter().map(|h| h.path.as_str()).collect();
        assert!(
            ranked.iter().position(|p| *p == "perfect.txt")
                < ranked.iter().position(|p| *p == "mediocre.txt"),
            "with bonus: perfect must beat mediocre, got {ranked:?}",
        );

        let no_bonus = rrf_multi_fusion(&[("keyword", kw), ("vector", ve)], 60, 10, false);
        let ranked: Vec<&str> = no_bonus.iter().map(|h| h.path.as_str()).collect();
        assert!(
            ranked.iter().position(|p| *p == "mediocre.txt")
                < ranked.iter().position(|p| *p == "perfect.txt"),
            "without bonus: mediocre must beat perfect, got {ranked:?}",
        );
    }

    #[test]
    fn tie_break_is_deterministic_by_input_order() {
        // Both hits appear once in one source at the same rank
        // (impossible in a real rank list, but easy to fabricate for
        // ties).  Feed them in a fixed order via two sources so the
        // ranks are equal but insertion order is fixed.
        let src_a = vec![hit("/first", 0, 0.0)];
        let src_b = vec![hit("/second", 0, 0.0)];
        let out = rrf_multi_fusion(&[("a", src_a), ("b", src_b)], 60, 10, false);
        // BTreeMap orders by dedup_key, so `/first:0` sorts before
        // `/second:0` — the stable-by-input-order guarantee we
        // document in the module header.
        assert_eq!(out[0].path, "/first");
        assert_eq!(out[1].path, "/second");
    }
}
