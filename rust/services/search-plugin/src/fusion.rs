//! Hybrid-fusion primitives combining keyword (BM25) + semantic
//! (vector cosine) result lists into one ranked output (Phase 3 of
//! the Python-parity roadmap; see `PARITY_ROADMAP.md`).
//!
//! The three fusion algorithms mirror Python `SearchDaemon`'s
//! `#4541` fusion-params delta — semantics and default tunings match
//! so cross-cutover results stay comparable.
//!
//! # Algorithms
//!
//! **RRF (reciprocal-rank fusion)** — the default.
//! ```text
//! score(doc) = Σ_{source s} 1 / (k + rank_s(doc))
//! ```
//! where `rank_s(doc)` is 1-indexed within source `s`.  Rank-based, so
//! no cross-source score-normalisation is needed.  Robust to sources
//! with wildly different score distributions (BM25 unbounded vs
//! cosine in [-1, 1]).  Standard `k = 60` per the original paper.
//!
//! **WEIGHTED** — linear blend on min-max-normalised scores.
//! ```text
//! score(doc) = (1 - alpha) * kw_norm(doc) + alpha * sem_norm(doc)
//! ```
//! where `kw_norm` / `sem_norm` are per-source min-max scaled to
//! [0, 1].  Requires normalisation because BM25 and cosine live in
//! different ranges.
//!
//! **RRF_WEIGHTED** — RRF with per-source weights.  Same shape as
//! RRF but each source's contribution is scaled by `(1 - alpha) /
//! alpha`.  Rarely materially different from RRF in practice; kept
//! for callers coming from Python parity.
//!
//! # Pooling
//!
//! [`pool_by_document`] enforces the per-doc chunk cap (#4542) —
//! prevents one long file from monopolising the top-N when its
//! per-chunk BM25 or cosine scores dominate multiple result slots.
//! P1-P3 emit one chunk per file so pooling is a no-op; the wire
//! and code path land here so P4's real chunker just needs to
//! bump `chunk_index` values.

use std::collections::HashMap;

use crate::search_proto::QueryResult;

/// Default RRF rank constant — canonical value from the original
/// paper, and what Python `SearchDaemon` defaults to.  Callers who
/// pass `rrf_k = 0` on the wire get this instead.
pub const DEFAULT_RRF_K: u32 = 60;

/// Default weighted-fusion alpha — 0.5 (balanced keyword + semantic).
/// Callers who pass `alpha = 0.0` on the wire get this instead so a
/// caller that forgets the field lands on the balanced blend rather
/// than pure-keyword; matches Python.  Callers who genuinely want
/// pure-keyword pass KEYWORD as `query_type` instead.
pub const DEFAULT_ALPHA: f32 = 0.5;

// ── Public entry points ──────────────────────────────────────────

pub fn rrf(keyword: &[QueryResult], semantic: &[QueryResult], k: u32) -> Vec<QueryResult> {
    let mut merged: HashMap<DocKey, MergedHit> = HashMap::new();
    let k_f = k.max(1) as f32;
    let contrib = |idx: usize, _: &QueryResult| 1.0 / (k_f + (idx + 1) as f32);
    accumulate(&mut merged, keyword, contrib);
    accumulate(&mut merged, semantic, contrib);
    finalise(merged)
}

pub fn rrf_weighted(
    keyword: &[QueryResult],
    semantic: &[QueryResult],
    k: u32,
    alpha: f32,
) -> Vec<QueryResult> {
    let alpha = alpha.clamp(0.0, 1.0);
    let k_f = k.max(1) as f32;
    let mut merged: HashMap<DocKey, MergedHit> = HashMap::new();
    accumulate(&mut merged, keyword, |idx, _| {
        (1.0 - alpha) / (k_f + (idx + 1) as f32)
    });
    accumulate(&mut merged, semantic, |idx, _| {
        alpha / (k_f + (idx + 1) as f32)
    });
    finalise(merged)
}

pub fn weighted(keyword: &[QueryResult], semantic: &[QueryResult], alpha: f32) -> Vec<QueryResult> {
    let alpha = alpha.clamp(0.0, 1.0);
    let kw_norm = min_max_normalise(keyword);
    let sem_norm = min_max_normalise(semantic);

    let mut merged: HashMap<DocKey, MergedHit> = HashMap::new();
    accumulate(&mut merged, keyword, |idx, _| (1.0 - alpha) * kw_norm[idx]);
    accumulate(&mut merged, semantic, |idx, _| alpha * sem_norm[idx]);
    finalise(merged)
}

/// Per-document chunk cap on the final ranked list (#4542).  Keeps
/// at most `chunks_per_page` hits per source path; extras (already
/// sorted lower by the fusion score) fall off the tail.
///
/// `chunks_per_page = 0` ⇒ no pooling — returns the input unchanged.
///
/// Preserves the input order (fusion score descending) among the
/// kept hits, so the caller can take the first `limit` and get
/// pooled + top-N in one pass.
pub fn pool_by_document(hits: Vec<QueryResult>, chunks_per_page: u32) -> Vec<QueryResult> {
    if chunks_per_page == 0 {
        return hits;
    }
    let cap = chunks_per_page as usize;
    let mut counts: HashMap<String, usize> = HashMap::new();
    let mut out = Vec::with_capacity(hits.len());
    for hit in hits {
        let seen = counts.entry(hit.path.clone()).or_insert(0);
        if *seen >= cap {
            continue;
        }
        *seen += 1;
        out.push(hit);
    }
    out
}

// ── Internals ────────────────────────────────────────────────────

/// Identity of a doc across sources.  `(path, chunk_index)` — same
/// pair identifies the same physical row across FTS + ANN because
/// [`crate::service`] stamps zero as the chunk_index for the P1-P3
/// one-chunk-per-file shape.  P4's chunker emits real chunk indices
/// and this key stays correct.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
struct DocKey {
    path: String,
    chunk_index: u32,
}

impl DocKey {
    fn of(r: &QueryResult) -> Self {
        Self {
            path: r.path.clone(),
            chunk_index: r.chunk_index,
        }
    }
}

/// Accumulator row — carries the fused score AND a `template` copy
/// of the first-seen result so we don't lose chunk_text / mtime_ms
/// when a doc appears in both sources.
struct MergedHit {
    score: f32,
    template: QueryResult,
}

/// Fold one source into the merged accumulator using a caller-
/// supplied per-hit contribution function.  Same shape for both RRF
/// (`weight / (k + rank)`) and WEIGHTED (`weight * normalised_score`)
/// — the difference is purely the scoring formula, so one merge loop
/// covers both.  Fixed at &self on the source so it can be handed
/// arbitrary slice reprs without allocating.
fn accumulate<F: Fn(usize, &QueryResult) -> f32>(
    merged: &mut HashMap<DocKey, MergedHit>,
    source: &[QueryResult],
    contribution: F,
) {
    for (idx, r) in source.iter().enumerate() {
        let c = contribution(idx, r);
        merged
            .entry(DocKey::of(r))
            .and_modify(|m| m.score += c)
            .or_insert_with(|| MergedHit {
                score: c,
                template: r.clone(),
            });
    }
}

fn finalise(merged: HashMap<DocKey, MergedHit>) -> Vec<QueryResult> {
    let mut out: Vec<QueryResult> = merged
        .into_values()
        .map(|m| QueryResult {
            score: m.score,
            ..m.template
        })
        .collect();
    // Sort descending by score.  Deterministic ordering when scores
    // tie: path lexicographic — callers can rely on a fixed order
    // for snapshot testing.
    out.sort_by(|a, b| {
        b.score
            .partial_cmp(&a.score)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| a.path.cmp(&b.path))
            .then_with(|| a.chunk_index.cmp(&b.chunk_index))
    });
    out
}

/// Min-max normalise a source's raw scores to [0, 1].  All-equal
/// input maps to 1.0 (so a single-hit source contributes fully to
/// the weighted blend).  Empty input returns empty.
fn min_max_normalise(source: &[QueryResult]) -> Vec<f32> {
    if source.is_empty() {
        return Vec::new();
    }
    let mut min = f32::INFINITY;
    let mut max = f32::NEG_INFINITY;
    for r in source {
        if r.score < min {
            min = r.score;
        }
        if r.score > max {
            max = r.score;
        }
    }
    let range = max - min;
    if range == 0.0 {
        return vec![1.0; source.len()];
    }
    source.iter().map(|r| (r.score - min) / range).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn hit(path: &str, chunk_index: u32, score: f32) -> QueryResult {
        QueryResult {
            path: path.to_string(),
            chunk_index,
            chunk_text: format!("text-{path}-{chunk_index}"),
            score,
            zone_id: "root".to_string(),
            mtime_ms: Some(1),
            expanded_context: String::new(),
        }
    }

    #[test]
    fn rrf_shared_doc_scores_higher_than_singleton() {
        let kw = vec![hit("/a", 0, 10.0), hit("/b", 0, 5.0)];
        let sem = vec![hit("/a", 0, 0.9), hit("/c", 0, 0.5)];
        let fused = rrf(&kw, &sem, DEFAULT_RRF_K);
        // /a appears in BOTH sources → higher fused score than /b or /c.
        assert_eq!(fused[0].path, "/a");
        assert!(
            fused[0].score > fused[1].score,
            "shared doc must lead: {fused:?}"
        );
    }

    #[test]
    fn rrf_matches_reference_formula() {
        // Two sources, k=60. /a at rank 1 in kw, rank 2 in sem.
        //   contribution = 1/(60+1) + 1/(60+2) = 1/61 + 1/62 ≈ 0.032917
        let kw = vec![hit("/a", 0, 1.0), hit("/b", 0, 0.5)];
        let sem = vec![hit("/z", 0, 1.0), hit("/a", 0, 0.5)];
        let fused = rrf(&kw, &sem, 60);
        let a = fused.iter().find(|r| r.path == "/a").expect("/a missing");
        let expected = 1.0 / 61.0 + 1.0 / 62.0;
        assert!(
            (a.score - expected).abs() < 1e-5,
            "expected {expected}, got {}",
            a.score,
        );
    }

    #[test]
    fn weighted_alpha_zero_is_pure_keyword() {
        let kw = vec![hit("/a", 0, 10.0), hit("/b", 0, 1.0)];
        let sem = vec![hit("/z", 0, 0.9)];
        let fused = weighted(&kw, &sem, 0.0);
        // alpha=0 → semantic contribution zeroed → only keyword
        // ranking survives.  /a normalised = 1.0, /b = 0.0, /z = 0.0
        // → all-tied at bottom.  Order: /a first, /b + /z tied at 0.
        assert_eq!(fused[0].path, "/a");
        assert!(fused[0].score > fused[1].score, "kw leader must lead");
    }

    #[test]
    fn weighted_alpha_one_is_pure_semantic() {
        let kw = vec![hit("/a", 0, 10.0)];
        let sem = vec![hit("/z", 0, 0.9), hit("/a", 0, 0.1)];
        let fused = weighted(&kw, &sem, 1.0);
        // alpha=1 → keyword contribution zeroed.  Semantic order: /z
        // normalised to 1.0, /a to 0.0.  So /z leads.
        assert_eq!(fused[0].path, "/z");
    }

    #[test]
    fn weighted_dedups_across_sources() {
        // Same doc in both sources appears ONCE with combined score.
        let kw = vec![hit("/a", 0, 10.0)];
        let sem = vec![hit("/a", 0, 0.9)];
        let fused = weighted(&kw, &sem, 0.5);
        assert_eq!(fused.len(), 1, "shared doc must dedupe: {fused:?}");
        assert_eq!(fused[0].path, "/a");
    }

    #[test]
    fn rrf_weighted_alpha_shifts_shared_doc_rank() {
        // /a is shared; /b is keyword-only; /c is semantic-only.
        // alpha=1.0 → keyword contribution 0 → /a (semantic rank 1) +
        // /c (semantic rank 2) > /b (keyword-only weighted 0).
        let kw = vec![hit("/a", 0, 10.0), hit("/b", 0, 5.0)];
        let sem = vec![hit("/a", 0, 0.9), hit("/c", 0, 0.5)];
        let fused_kw = rrf_weighted(&kw, &sem, 60, 0.0);
        let fused_sem = rrf_weighted(&kw, &sem, 60, 1.0);
        let b_kw = fused_kw.iter().find(|r| r.path == "/b").unwrap().score;
        let b_sem = fused_sem.iter().find(|r| r.path == "/b").unwrap().score;
        assert!(b_kw > b_sem, "kw-only /b weighted lower under alpha=1");
    }

    #[test]
    fn empty_sources_return_empty() {
        assert!(rrf(&[], &[], 60).is_empty());
        assert!(weighted(&[], &[], 0.5).is_empty());
    }

    #[test]
    fn one_empty_source_returns_the_other() {
        let kw = vec![hit("/a", 0, 10.0)];
        let fused = rrf(&kw, &[], 60);
        assert_eq!(fused.len(), 1);
        assert_eq!(fused[0].path, "/a");
    }

    #[test]
    fn preserves_chunk_text_and_mtime_from_first_seen_source() {
        // When a doc appears in both sources the accumulator keeps
        // the FIRST source's chunk_text — semantic's ANN enrichment
        // typically has richer chunk_text via the FTS sibling, so
        // callers pass semantic first.  The template preservation
        // pins the "first seen wins" contract.
        let kw = vec![QueryResult {
            path: "/a".into(),
            chunk_index: 0,
            chunk_text: "keyword-text".into(),
            score: 1.0,
            zone_id: "root".into(),
            mtime_ms: Some(100),
            expanded_context: String::new(),
        }];
        let sem = vec![QueryResult {
            path: "/a".into(),
            chunk_index: 0,
            chunk_text: "semantic-text".into(),
            score: 0.9,
            zone_id: "root".into(),
            mtime_ms: Some(200),
            expanded_context: String::new(),
        }];
        let fused = rrf(&kw, &sem, 60);
        assert_eq!(fused.len(), 1);
        assert_eq!(fused[0].chunk_text, "keyword-text");
        assert_eq!(fused[0].mtime_ms, Some(100));
    }

    #[test]
    fn pool_by_document_zero_is_noop() {
        let hits = vec![hit("/a", 0, 5.0), hit("/a", 1, 4.0), hit("/b", 0, 3.0)];
        let out = pool_by_document(hits.clone(), 0);
        assert_eq!(out.len(), 3, "chunks_per_page=0 must be no-op");
    }

    #[test]
    fn pool_by_document_enforces_cap() {
        // Three hits from /a, cap = 2 → only first two /a hits survive.
        // Fusion-score ordering preserved: input in [5, 4, 3, 2, 1]
        // → output keeps [5, 4] for /a plus /b.
        let hits = vec![
            hit("/a", 0, 5.0),
            hit("/a", 1, 4.0),
            hit("/b", 0, 3.5),
            hit("/a", 2, 3.0),
            hit("/c", 0, 2.5),
        ];
        let out = pool_by_document(hits, 2);
        let paths: Vec<&str> = out.iter().map(|r| r.path.as_str()).collect();
        assert_eq!(paths, ["/a", "/a", "/b", "/c"]);
    }

    #[test]
    fn pool_by_document_preserves_input_order_among_kept_hits() {
        // Regression: a naive impl that groups-then-emits would break
        // the fusion-score order.  Cap = 1 → keep the top hit per
        // path, and the emit-order must follow the input.
        let hits = vec![
            hit("/a", 0, 5.0),
            hit("/b", 0, 4.9),
            hit("/a", 1, 4.8), // capped away
            hit("/c", 0, 4.7),
        ];
        let out = pool_by_document(hits, 1);
        let paths: Vec<&str> = out.iter().map(|r| r.path.as_str()).collect();
        assert_eq!(paths, ["/a", "/b", "/c"]);
    }

    #[test]
    fn finalise_ties_break_deterministically() {
        // Two docs at the same fused score — path lexicographic
        // then chunk_index.  Snapshot tests depend on this.
        let kw = vec![
            QueryResult {
                path: "/b".into(),
                chunk_index: 0,
                chunk_text: String::new(),
                score: 1.0,
                zone_id: "root".into(),
                mtime_ms: None,
                expanded_context: String::new(),
            },
            QueryResult {
                path: "/a".into(),
                chunk_index: 0,
                chunk_text: String::new(),
                score: 1.0,
                zone_id: "root".into(),
                mtime_ms: None,
                expanded_context: String::new(),
            },
        ];
        // rrf here: rank-1 and rank-2 have different scores; equalise
        // by using WEIGHTED with alpha=0 and identical raw scores so
        // normalised scores tie at 1.0.
        let fused = weighted(&kw, &[], 0.0);
        assert_eq!(fused[0].path, "/a", "lex-sorted on tie: {fused:?}");
        assert_eq!(fused[1].path, "/b");
    }
}
