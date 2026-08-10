//! Post-fusion score adjustments — recency decay + path-prefix
//! boost (Phase 6 of the Python-parity roadmap; see
//! `PARITY_ROADMAP.md`).
//!
//! # What lives here
//!
//! Two orthogonal multipliers applied to every hit's `score` after
//! fusion but before pooling.  Both are pure math over
//! `QueryResult` slices; no I/O, no side effects.
//!
//! - [`apply_recency`] — bumps recently-modified files' scores per
//!   `score *= 1 + weight * H / (H + age_days)`.  In "auto" mode
//!   only fires if the query text hints at recency
//!   (`latest`, `recent`, ...).  Matches Python `#4543` decay.
//!
//! - [`apply_prefix_boost`] — multiplies each hit's score by the
//!   longest-prefix-match multiplier from the caller's map.
//!   Matches Python `#4544` `path_contexts` weighting.
//!
//! # Why post-fusion
//!
//! Recency + prefix boost must NOT influence the raw retrieval
//! ranks that RRF fuses on; they'd double-count if applied to the
//! per-source lists AND the fused one.  Applying once at the end
//! keeps the semantics predictable and matches Python's shape.
//!
//! # Pool interaction
//!
//! Pooling (`chunks_per_page`) runs AFTER these adjustments so a
//! recency-boosted or prefix-boosted chunk can rise above another
//! chunk of the same file before pooling caps them.

use std::collections::HashMap;

use crate::search_proto::QueryResult;

/// Server-side default weight when the caller sends 0.0.  Matches
/// Python `SearchDaemon.recency_weight` default.
pub const DEFAULT_RECENCY_WEIGHT: f32 = 0.5;

/// Server-side default half-life in days.  Matches Python.
pub const DEFAULT_RECENCY_HALF_LIFE_DAYS: f32 = 30.0;

/// Clamp on weight to bound worst-case ranking skew.  A caller
/// sending weight = 1000 shouldn't be able to make an old-but-
/// popular doc leapfrog a fresh-but-irrelevant one by six orders
/// of magnitude; 5.0 = ceiling.
pub const RECENCY_WEIGHT_MAX: f32 = 5.0;

/// Recency modes the wire accepts.  Matches Python's three-value
/// string convention.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RecencyMode {
    /// No decay (default).
    Off,
    /// Always apply decay.
    On,
    /// Apply only if the query text hints at recency (see
    /// [`query_wants_recency`]).
    Auto,
}

impl RecencyMode {
    /// Parse the wire string.  Empty / unknown / "off" all map to
    /// Off for forward compat.  Named `parse_wire` (not
    /// `from_str`) to avoid the clippy `should_implement_trait`
    /// lint on the stdlib `FromStr::from_str` shape — we return
    /// an infallible `Self`, not a `Result`, so implementing
    /// `FromStr` outright would be wrong.
    pub fn parse_wire(s: &str) -> Self {
        match s {
            "on" => Self::On,
            "auto" => Self::Auto,
            _ => Self::Off,
        }
    }
}

/// Set of query words that trigger `RecencyMode::Auto`.  Sourced
/// from Python `RECENCY_WORDS` (`nexus/contracts/search_types.py`)
/// so recall matches after the cutover.  Case-insensitive: the
/// scorer lowercases the query before checking.
const RECENCY_TRIGGER_WORDS: &[&str] = &[
    "latest",
    "newest",
    "recent",
    "recently",
    "current",
    "currently",
    "today",
    "yesterday",
    "now",
    "new",
];

/// True if `query` contains any of the recency trigger words.
/// Whitespace-split so `"newmarket"` doesn't trip on "new".  Fast
/// path for empty query — returns false without allocating.
pub fn query_wants_recency(query: &str) -> bool {
    if query.is_empty() {
        return false;
    }
    for token in query.split_whitespace() {
        let normalised: String = token
            .chars()
            .filter(|c| c.is_alphanumeric())
            .flat_map(|c| c.to_lowercase())
            .collect();
        if RECENCY_TRIGGER_WORDS.iter().any(|w| *w == normalised) {
            return true;
        }
    }
    false
}

/// Recency decay applied in-place.  Each hit's `score` is
/// multiplied by `1 + weight * H / (H + age_days)`.  Hits with no
/// mtime (kernel didn't stamp them) get no boost — their score
/// stays untouched so a query that promoted them earlier isn't
/// silently penalised for lack of an mtime.
///
/// `now_ms` is the current wall-clock time in ms.  Injected so
/// tests are deterministic; production callers pass
/// `chrono::Utc::now().timestamp_millis()` or equivalent.
pub fn apply_recency(
    hits: &mut [QueryResult],
    mode: RecencyMode,
    weight: f32,
    half_life_days: f32,
    now_ms: i64,
    query: &str,
) {
    let should_apply = match mode {
        RecencyMode::Off => false,
        RecencyMode::On => true,
        RecencyMode::Auto => query_wants_recency(query),
    };
    if !should_apply {
        return;
    }
    let w = weight.clamp(0.0, RECENCY_WEIGHT_MAX);
    let h = half_life_days.max(0.001); // avoid divide-by-zero
    let ms_per_day = 86_400_000.0f32;
    for hit in hits.iter_mut() {
        let Some(mtime_ms) = hit.mtime_ms else {
            continue;
        };
        let age_ms = (now_ms - mtime_ms).max(0) as f32;
        let age_days = age_ms / ms_per_day;
        let multiplier = 1.0 + w * h / (h + age_days);
        hit.score *= multiplier;
    }
}

/// Path-prefix score boost applied in-place.  For each hit, walk
/// `boosts` for the LONGEST prefix that matches the hit's path;
/// multiply the score by that entry's multiplier.  No matching
/// prefix ⇒ score unchanged (multiplier 1.0).
///
/// A key ending in `/` also matches the path that EQUALS the key
/// minus that trailing slash (#4620): the server wraps stored
/// path-context prefixes as `/{p}/` for slash-boundary safety, and a
/// row whose prefix names an exact file (`/notes/hello.md/` after
/// wrapping) must still boost that file — the pre-P12 stack matched
/// it via `path == prefix` equality.  Plain `starts_with` keys keep
/// their raw semantics.
///
/// Longest-match semantics matter when boosts overlap
/// (`/docs/` = 1.5, `/docs/api/` = 2.0) — the more specific
/// prefix wins.  Ties (same length) break arbitrarily but stably
/// (BTreeMap iteration order).
pub fn apply_prefix_boost(hits: &mut [QueryResult], boosts: &HashMap<String, f32>) {
    if boosts.is_empty() {
        return;
    }
    for hit in hits.iter_mut() {
        let best = boosts
            .iter()
            .filter(|(prefix, _)| {
                hit.path.starts_with(prefix.as_str())
                    || (prefix.ends_with('/') && hit.path == prefix[..prefix.len() - 1])
            })
            .max_by_key(|(prefix, _)| prefix.len());
        if let Some((_, &multiplier)) = best {
            hit.score *= multiplier;
        }
    }
}

/// Convenience wrapper — apply both adjustments in the canonical
/// order (recency first, then prefix boost).  Order matters because
/// the two multipliers stack; the test suite pins expected values
/// for both orderings but callers should use this helper unless
/// they have a reason to reorder.
pub fn apply_all(
    hits: &mut [QueryResult],
    recency_mode: RecencyMode,
    recency_weight: f32,
    recency_half_life_days: f32,
    now_ms: i64,
    query: &str,
    prefix_boosts: &HashMap<String, f32>,
) {
    apply_recency(
        hits,
        recency_mode,
        recency_weight,
        recency_half_life_days,
        now_ms,
        query,
    );
    apply_prefix_boost(hits, prefix_boosts);
}

#[cfg(test)]
mod tests {
    use super::*;

    fn hit(path: &str, score: f32, mtime_ms: Option<i64>) -> QueryResult {
        QueryResult {
            path: path.to_string(),
            chunk_index: 0,
            chunk_text: String::new(),
            score,
            zone_id: "root".to_string(),
            mtime_ms,
            expanded_context: String::new(),
        }
    }

    // ── query_wants_recency ────────────────────────────────────

    #[test]
    fn recency_trigger_words_hit() {
        for q in [
            "latest news",
            "what's new?",
            "recent activity",
            "today",
            "NEWEST",
        ] {
            assert!(query_wants_recency(q), "should trigger: {q:?}");
        }
    }

    #[test]
    fn non_trigger_query_does_not_hit() {
        for q in ["cat dog fish", "widget alpha", "python code", "newmarket"] {
            assert!(!query_wants_recency(q), "should not trigger: {q:?}");
        }
    }

    #[test]
    fn empty_query_short_circuits() {
        assert!(!query_wants_recency(""));
    }

    // ── apply_recency ──────────────────────────────────────────

    #[test]
    fn recency_off_leaves_scores_untouched() {
        let mut hits = vec![hit("/a", 1.0, Some(0))];
        apply_recency(
            &mut hits,
            RecencyMode::Off,
            DEFAULT_RECENCY_WEIGHT,
            DEFAULT_RECENCY_HALF_LIFE_DAYS,
            1_700_000_000_000,
            "latest",
        );
        assert_eq!(hits[0].score, 1.0);
    }

    #[test]
    fn recency_on_boosts_fresh_more_than_old() {
        // Two docs, same score.  Fresh (mtime = now) should end up
        // with a higher score than old (mtime = 60 days back).
        let now = 1_700_000_000_000;
        let day = 86_400_000i64;
        let mut hits = vec![
            hit("/fresh", 1.0, Some(now)),
            hit("/old", 1.0, Some(now - 60 * day)),
        ];
        apply_recency(&mut hits, RecencyMode::On, 1.0, 30.0, now, "any");
        assert!(
            hits[0].score > hits[1].score,
            "fresh should beat old: {hits:?}"
        );
    }

    #[test]
    fn recency_multiplier_matches_formula() {
        // For weight=1, H=30, age=0: multiplier = 1 + 1 * 30 / (30 + 0) = 2.
        let now = 1_700_000_000_000;
        let mut hits = vec![hit("/x", 2.5, Some(now))];
        apply_recency(&mut hits, RecencyMode::On, 1.0, 30.0, now, "any");
        assert!(
            (hits[0].score - 5.0).abs() < 0.001,
            "expected 2.5 * 2 = 5.0, got {}",
            hits[0].score,
        );
    }

    #[test]
    fn recency_hit_with_no_mtime_untouched() {
        let mut hits = vec![hit("/x", 1.5, None)];
        apply_recency(
            &mut hits,
            RecencyMode::On,
            1.0,
            30.0,
            1_700_000_000_000,
            "any",
        );
        assert_eq!(
            hits[0].score, 1.5,
            "no-mtime hits must not be silently penalised"
        );
    }

    #[test]
    fn recency_auto_only_fires_when_query_triggers() {
        let now = 1_700_000_000_000;
        // Same query, same corpus; auto-off with a non-trigger
        // query is a no-op, auto-on with "latest" applies the boost.
        let baseline = hit("/x", 1.0, Some(now));
        let mut off = vec![baseline.clone()];
        apply_recency(&mut off, RecencyMode::Auto, 1.0, 30.0, now, "widget");
        assert_eq!(off[0].score, 1.0, "auto-mode + non-trigger query = off");

        let mut on = vec![baseline];
        apply_recency(&mut on, RecencyMode::Auto, 1.0, 30.0, now, "latest widget");
        assert!(on[0].score > 1.0, "auto-mode + trigger query = on");
    }

    #[test]
    fn recency_weight_clamped_to_max() {
        // A caller passing weight = 1000 gets the SAME multiplier
        // as weight = RECENCY_WEIGHT_MAX (5.0) — bounded skew.
        let now = 1_700_000_000_000;
        let mut runaway = vec![hit("/x", 1.0, Some(now))];
        let mut capped = vec![hit("/x", 1.0, Some(now))];
        apply_recency(&mut runaway, RecencyMode::On, 1000.0, 30.0, now, "any");
        apply_recency(
            &mut capped,
            RecencyMode::On,
            RECENCY_WEIGHT_MAX,
            30.0,
            now,
            "any",
        );
        assert!(
            (runaway[0].score - capped[0].score).abs() < 0.001,
            "weight clamp not enforced: {} vs {}",
            runaway[0].score,
            capped[0].score,
        );
    }

    // ── apply_prefix_boost ─────────────────────────────────────

    #[test]
    fn prefix_boost_trailing_slash_key_matches_exact_file_path() {
        // #4620 parity: the server wraps stored path_contexts prefixes
        // as "/{p}/" for slash-boundary safety.  A row whose prefix
        // names an exact FILE ("/notes/hello.md/" after wrapping) must
        // still boost that file — pre-P12 matched it via
        // `path == prefix` equality.
        let mut hits = vec![hit("/notes/hello.md", 1.0, None)];
        let mut boosts = HashMap::new();
        boosts.insert("/notes/hello.md/".to_string(), 3.0);
        apply_prefix_boost(&mut hits, &boosts);
        assert!(
            (hits[0].score - 3.0).abs() < 1e-6,
            "exact-file trailing-slash key must boost, got {}",
            hits[0].score
        );
    }

    #[test]
    fn prefix_boost_trailing_slash_key_does_not_leak_to_siblings() {
        // The boundary guarantee that motivated the "/{p}/" wrapping:
        // "/docs/" must not touch "/docs-v2/…" or "/docsfile.md".
        let mut hits = vec![
            hit("/docs-v2/x.md", 1.0, None),
            hit("/docsfile.md", 1.0, None),
        ];
        let mut boosts = HashMap::new();
        boosts.insert("/docs/".to_string(), 5.0);
        apply_prefix_boost(&mut hits, &boosts);
        assert!((hits[0].score - 1.0).abs() < 1e-6, "sibling dir leaked");
        assert!((hits[1].score - 1.0).abs() < 1e-6, "sibling file leaked");
    }

    #[test]
    fn prefix_boost_empty_map_is_noop() {
        let mut hits = vec![hit("/a", 1.0, None)];
        apply_prefix_boost(&mut hits, &HashMap::new());
        assert_eq!(hits[0].score, 1.0);
    }

    #[test]
    fn prefix_boost_multiplies_matching_hit() {
        let mut hits = vec![
            hit("/docs/api.md", 1.0, None),
            hit("/tmp/scratch.md", 1.0, None),
            hit("/unrelated.md", 1.0, None),
        ];
        let mut boosts = HashMap::new();
        boosts.insert("/docs/".to_string(), 2.0);
        boosts.insert("/tmp/".to_string(), 0.5);
        apply_prefix_boost(&mut hits, &boosts);
        assert_eq!(hits[0].score, 2.0);
        assert_eq!(hits[1].score, 0.5);
        assert_eq!(hits[2].score, 1.0, "no matching prefix = untouched");
    }

    #[test]
    fn prefix_boost_longest_match_wins() {
        // Overlapping prefixes — the more specific one wins.
        let mut hits = vec![hit("/docs/api/spec.md", 1.0, None)];
        let mut boosts = HashMap::new();
        boosts.insert("/docs/".to_string(), 1.5);
        boosts.insert("/docs/api/".to_string(), 3.0);
        apply_prefix_boost(&mut hits, &boosts);
        assert_eq!(hits[0].score, 3.0, "longer prefix must win");
    }

    #[test]
    fn prefix_boost_at_zero_zeros_the_score() {
        // A caller genuinely wants to blacklist a path — 0.0
        // multiplier collapses the score to 0 without special-
        // casing.  Documented as intentional; if a caller wants
        // "exclude" semantics they can filter upstream.
        let mut hits = vec![hit("/blocked/foo.md", 5.0, None)];
        let mut boosts = HashMap::new();
        boosts.insert("/blocked/".to_string(), 0.0);
        apply_prefix_boost(&mut hits, &boosts);
        assert_eq!(hits[0].score, 0.0);
    }

    // ── apply_all ordering ─────────────────────────────────────

    #[test]
    fn apply_all_composes_recency_and_prefix() {
        // Doc /docs/x.md with mtime = now, score 1.0, weight 1,
        // H=30.  Recency multiplier = 2.  Prefix boost = 1.5.
        // Combined = 1.0 * 2 * 1.5 = 3.0.
        let now = 1_700_000_000_000;
        let mut hits = vec![hit("/docs/x.md", 1.0, Some(now))];
        let mut boosts = HashMap::new();
        boosts.insert("/docs/".to_string(), 1.5);
        apply_all(&mut hits, RecencyMode::On, 1.0, 30.0, now, "any", &boosts);
        assert!(
            (hits[0].score - 3.0).abs() < 0.001,
            "expected 3.0, got {}",
            hits[0].score,
        );
    }
}
