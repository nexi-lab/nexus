//! Read-side macro-chunk (neighbour-context) expansion for hybrid
//! search — Rust port of the Python `nexus.bricks.search.macro_chunk`
//! heuristic (issue #4130 review R5).
//!
//! # Why upgrade
//!
//! The pre-R5 [`apply_expand`] hard-coded a ±1 window: for every hit
//! at (path, N), it stitched chunks N-1, N, N+1.  That works for a
//! narrow prev/next glance, but Python's `macro_chunk.py` had already
//! taken the same feature (#4398) further:
//!
//!   * configurable window (default ±8, not ±1),
//!   * token budget (default 1024 tokens, not "however big three
//!     chunks are"),
//!   * bidirectional expansion — walk back AND forward until the
//!     budget is spent,
//!   * code-file forward-bias — for `.py` / `.rs` / `.ts` / …
//!     files, expand *forward* first (subsequent code lines usually
//!     matter more than the definitions above),
//!   * section awareness — bound the walk by same-heading contiguity.
//!
//! The Python surface is now dead (post-P12), but its callers in
//! nexus-server expected the smarter expand shape.  Ship parity.
//!
//! # Known gap
//!
//! Section awareness needs a `heading_prefix` field on chunks — the
//! FTS schema doesn't have one yet, so this port treats "same
//! section" as "same file" (section bounds = full chunk set).  Same
//! for `line_start` / `line_end`: no schema fields, no attribution.
//! Both are a schema-bump follow-up when the indexer starts emitting
//! them.  The window + budget + forward-bias upgrades stand on their
//! own and give the +7 chunk headroom callers were configured for.
//!
//! # Config
//!
//! * `NEXUS_SEARCH_MACRO_EXPAND_WINDOW`         (default 8)
//! * `NEXUS_SEARCH_MACRO_EXPAND_TOKEN_BUDGET`   (default 1024)
//! * `NEXUS_SEARCH_MACRO_EXPAND_CODE_FORWARD_BIAS` (default true)
//!
//! All defaults keep the feature on with the Python-parity shape.
//! Env parse errors log a warning and use the default; a hosed env
//! must not break search.

use std::collections::BTreeMap;

use crate::fts_index::FtsHit;
use crate::index_manager::IndexManager;
use crate::search_proto::QueryResult;

/// Extension list that flips the code-forward-bias branch.  Mirrors
/// Python `macro_chunk._CODE_EXTENSIONS`.
const CODE_EXTENSIONS: &[&str] = &[
    ".c", ".cc", ".cpp", ".h", ".hpp", ".py", ".rs", ".go", ".java", ".ts", ".tsx", ".js", ".jsx",
    ".v", ".vh", ".sv", ".svh", ".scala", ".rb", ".swift", ".kt",
];

/// Rough tokens-per-character ratio.  A real tokenizer would give
/// exact counts but drag in `tiktoken`/`tokenizers` for one budget
/// check; 4 chars/token matches the OpenAI rule of thumb and the
/// error is bounded to a small over- or under-count (single-digit
/// chunks at the boundary).  If we ever want exact budgeting, swap
/// this for a token-count field stamped at index time.
const CHARS_PER_TOKEN: usize = 4;

/// Read-side context expansion mode (P4, #4398).  Sourced from
/// `QueryRequest.expand`; unknown values fall back to `None` so a
/// forgetful caller gets the pre-P4 shape.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ExpandMode {
    /// No enrichment; `QueryResult.expanded_context` stays empty.
    None,
    /// Fill `expanded_context` with a section-aware window of chunks
    /// under the same path.
    Macro,
}

impl ExpandMode {
    /// Parse a wire-string mode ("macro" / "" / anything else).
    /// Not `FromStr` because parse errors don't exist here — an
    /// unknown value degrades to [`Self::None`] rather than
    /// returning `Err`, so the trait shape doesn't fit.
    pub fn parse(s: &str) -> Self {
        match s {
            "macro" => Self::Macro,
            _ => Self::None,
        }
    }
}

/// Tuneable knobs for [`window_for_anchor`].  Values populated from
/// environment; kept behind a struct so tests can pin exact values
/// without touching the process env.
#[derive(Debug, Clone, Copy)]
pub struct MacroExpandConfig {
    pub token_budget: usize,
    pub window: u32,
    pub code_forward_bias: bool,
}

impl Default for MacroExpandConfig {
    fn default() -> Self {
        Self {
            token_budget: 1024,
            window: 8,
            code_forward_bias: true,
        }
    }
}

impl MacroExpandConfig {
    /// Read the three env vars.  Unparseable values log a warning
    /// and use the default — a bad env must not break the query
    /// path.
    pub fn from_env() -> Self {
        let default = Self::default();
        let token_budget = parse_or_warn_usize(
            "NEXUS_SEARCH_MACRO_EXPAND_TOKEN_BUDGET",
            default.token_budget,
        );
        let window = parse_or_warn_u32("NEXUS_SEARCH_MACRO_EXPAND_WINDOW", default.window);
        let code_forward_bias = parse_or_warn_bool(
            "NEXUS_SEARCH_MACRO_EXPAND_CODE_FORWARD_BIAS",
            default.code_forward_bias,
        );
        Self {
            token_budget,
            window,
            code_forward_bias,
        }
    }
}

fn parse_or_warn_usize(key: &str, default: usize) -> usize {
    match std::env::var(key) {
        Ok(v) => v.parse().unwrap_or_else(|_| {
            tracing::warn!(
                env = key,
                value = %v,
                fallback = default,
                "search-plugin macro-expand: env parse failed — using default",
            );
            default
        }),
        Err(_) => default,
    }
}

fn parse_or_warn_u32(key: &str, default: u32) -> u32 {
    match std::env::var(key) {
        Ok(v) => v.parse().unwrap_or_else(|_| {
            tracing::warn!(
                env = key,
                value = %v,
                fallback = default,
                "search-plugin macro-expand: env parse failed — using default",
            );
            default
        }),
        Err(_) => default,
    }
}

fn parse_or_warn_bool(key: &str, default: bool) -> bool {
    match std::env::var(key) {
        Ok(v) => match v.to_ascii_lowercase().as_str() {
            "1" | "true" | "yes" | "on" => true,
            "0" | "false" | "no" | "off" => false,
            _ => {
                tracing::warn!(
                    env = key,
                    value = %v,
                    fallback = default,
                    "search-plugin macro-expand: env parse failed — using default",
                );
                default
            }
        },
        Err(_) => default,
    }
}

fn approx_tokens(text: &str) -> usize {
    text.len().div_ceil(CHARS_PER_TOKEN)
}

fn is_code_path(path: &str) -> bool {
    let lower = path.to_ascii_lowercase();
    CODE_EXTENSIONS.iter().any(|ext| lower.ends_with(ext))
}

/// Pick the widest (lo, hi) chunk window around `anchor_idx` that
/// stays within `cfg.token_budget`.  Bounded by `[s_lo, s_hi]` (the
/// section bounds — here == the full chunk set, since the schema
/// lacks `heading_prefix`; see module doc).  Direct port of Python
/// `macro_chunk._window_for_anchor` with the two branches:
///
///   * `is_code && code_forward_bias`  → walk forward first, then
///     backwards until the budget runs out.  Matches the
///     "next lines matter more" heuristic for source files.
///   * otherwise                       → alternate back/forward one
///     step at a time; when one direction hits the section boundary,
///     keep spending the budget on the other.  Matches Python.
///
/// Contract: `anchor_idx` MUST be in `chunks_by_index` (a hit whose
/// chunk isn't in the fetched set is a schema/consistency bug —
/// the caller filters those out before calling).
fn window_for_anchor(
    chunks_by_index: &BTreeMap<u32, &FtsHit>,
    anchor_idx: u32,
    cfg: &MacroExpandConfig,
    is_code: bool,
) -> (u32, u32) {
    // Section = full file for now (schema lacks heading_prefix).
    let s_lo = chunks_by_index.keys().next().copied().unwrap_or(anchor_idx);
    let s_hi = chunks_by_index
        .keys()
        .next_back()
        .copied()
        .unwrap_or(anchor_idx);

    // Cap the section by `cfg.window` on each side of anchor so a
    // 500-chunk file doesn't blow past the token budget just because
    // section == file.  Under budget the walk stops on its own; over
    // budget the window keeps us near the anchor.
    let s_lo = s_lo.max(anchor_idx.saturating_sub(cfg.window));
    let s_hi = s_hi.min(anchor_idx.saturating_add(cfg.window));

    let section_tokens: usize = (s_lo..=s_hi)
        .filter_map(|i| chunks_by_index.get(&i))
        .map(|c| approx_tokens(&c.chunk_text))
        .sum();
    if section_tokens <= cfg.token_budget {
        return (s_lo, s_hi);
    }

    let anchor_tokens = chunks_by_index
        .get(&anchor_idx)
        .map(|c| approx_tokens(&c.chunk_text))
        .unwrap_or(0);
    let mut used = anchor_tokens;
    let mut lo = anchor_idx;
    let mut hi = anchor_idx;

    let cost = |i: u32| -> usize {
        chunks_by_index
            .get(&i)
            .map(|c| approx_tokens(&c.chunk_text))
            .unwrap_or(0)
    };

    if is_code && cfg.code_forward_bias {
        while hi < s_hi {
            let next = hi + 1;
            let c = cost(next);
            if used + c > cfg.token_budget {
                break;
            }
            hi = next;
            used += c;
        }
        while lo > s_lo {
            let prev = lo - 1;
            let c = cost(prev);
            if used + c > cfg.token_budget {
                break;
            }
            lo = prev;
            used += c;
        }
        return (lo, hi);
    }

    // Alternating back/forth, matching Python's ping-pong loop.
    let mut back = true;
    loop {
        let mut moved = false;
        let primary_ok = if back {
            lo > s_lo && used + cost(lo - 1) <= cfg.token_budget
        } else {
            hi < s_hi && used + cost(hi + 1) <= cfg.token_budget
        };
        if primary_ok {
            if back {
                lo -= 1;
                used += cost(lo);
            } else {
                hi += 1;
                used += cost(hi);
            }
            moved = true;
        } else {
            // Primary direction blocked — spend the budget on the
            // opposite side if it still has room.  Same shape as
            // Python's fallback branch inside the same iteration.
            let fallback_ok = if back {
                hi < s_hi && used + cost(hi + 1) <= cfg.token_budget
            } else {
                lo > s_lo && used + cost(lo - 1) <= cfg.token_budget
            };
            if fallback_ok {
                if back {
                    hi += 1;
                    used += cost(hi);
                } else {
                    lo -= 1;
                    used += cost(lo);
                }
                moved = true;
            }
        }
        if !moved {
            break;
        }
        back = !back;
    }
    (lo, hi)
}

/// Stitch chunks `[lo, hi]` into a single text blob.  Separator is
/// `\n` to match Python `_stitch`; the old ±1 Rust code used `\n\n`
/// (harmless but callers rendering the section see a doubled break).
fn stitch(chunks_by_index: &BTreeMap<u32, &FtsHit>, lo: u32, hi: u32) -> String {
    let mut out = String::new();
    let mut wrote_any = false;
    for i in lo..=hi {
        if let Some(c) = chunks_by_index.get(&i) {
            if wrote_any {
                out.push('\n');
            }
            out.push_str(&c.chunk_text);
            wrote_any = true;
        }
    }
    out
}

/// Fill `expanded_context` on each hit when the caller asked for
/// `expand=macro`.  Fetches the file's chunk set once per path
/// (cache within one Query call), picks a section-aware window via
/// [`window_for_anchor`], stitches the text.
///
/// Failure posture: any error path (zone open, chunk fetch, missing
/// anchor) silently leaves `expanded_context` empty.  Best-effort —
/// callers should render the hit without context rather than fail
/// the whole query.
pub fn apply_expand(
    manager: &IndexManager,
    zone_id: &str,
    mode: ExpandMode,
    hits: &mut [QueryResult],
) {
    if matches!(mode, ExpandMode::None) {
        return;
    }
    let Ok(fts) = manager.get_or_open(zone_id) else {
        return;
    };
    let cfg = MacroExpandConfig::from_env();
    let mut cache: std::collections::HashMap<String, Vec<FtsHit>> =
        std::collections::HashMap::new();
    for hit in hits.iter_mut() {
        let chunks = cache
            .entry(hit.path.clone())
            .or_insert_with(|| fts.get_chunks_by_path(&hit.path).unwrap_or_default());
        if chunks.len() <= 1 {
            continue;
        }
        let is_code = is_code_path(&hit.path);
        let by_index: BTreeMap<u32, &FtsHit> = chunks.iter().map(|c| (c.chunk_index, c)).collect();
        if !by_index.contains_key(&hit.chunk_index) {
            // Anchor chunk not in fetched set — cache is stale or
            // schema/consistency drift.  Skip rather than assert.
            continue;
        }
        let (w_lo, w_hi) = window_for_anchor(&by_index, hit.chunk_index, &cfg, is_code);
        let stitched = stitch(&by_index, w_lo, w_hi);
        if !stitched.is_empty() {
            hit.expanded_context = stitched;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn hit(chunk_index: u32, text: &str) -> FtsHit {
        FtsHit {
            path: "/f".to_string(),
            chunk_index,
            chunk_text: text.to_string(),
            score: 0.0,
            mtime_ms: None,
        }
    }

    fn cfg(token_budget: usize, window: u32, code_forward_bias: bool) -> MacroExpandConfig {
        MacroExpandConfig {
            token_budget,
            window,
            code_forward_bias,
        }
    }

    fn make_by_index(chunks: &[FtsHit]) -> BTreeMap<u32, &FtsHit> {
        chunks.iter().map(|c| (c.chunk_index, c)).collect()
    }

    #[test]
    fn is_code_path_matches_python_extensions() {
        assert!(is_code_path("/x/foo.py"));
        assert!(is_code_path("/x/Foo.RS"));
        assert!(is_code_path("/x/bar.tsx"));
        assert!(is_code_path("/x/svh_case.svh"));
        assert!(!is_code_path("/x/README.md"));
        assert!(!is_code_path("/x/no_ext"));
    }

    #[test]
    fn window_returns_full_section_when_under_budget() {
        // 3 chunks × 4 bytes = 3 tokens, fits in 1024.
        let chunks = vec![hit(0, "aaaa"), hit(1, "bbbb"), hit(2, "cccc")];
        let by = make_by_index(&chunks);
        let (lo, hi) = window_for_anchor(&by, 1, &cfg(1024, 8, false), false);
        assert_eq!((lo, hi), (0, 2));
    }

    #[test]
    fn window_capped_by_cfg_window_even_when_budget_allows() {
        // 20 tiny chunks — budget could hold them all, but
        // cfg.window=1 caps to anchor±1.
        let chunks: Vec<FtsHit> = (0..20).map(|i| hit(i, "x")).collect();
        let by = make_by_index(&chunks);
        let (lo, hi) = window_for_anchor(&by, 10, &cfg(1024, 1, false), false);
        assert_eq!((lo, hi), (9, 11));
    }

    #[test]
    fn window_over_budget_alternates_back_and_forth() {
        // Anchor + 4 neighbours; each chunk = 4 bytes (1 token).
        // Budget = 3 tokens ⇒ anchor + 2 more.  Ping-pong picks
        // back first, then forward.
        let chunks: Vec<FtsHit> = (0..5).map(|i| hit(i, "aaaa")).collect();
        let by = make_by_index(&chunks);
        let (lo, hi) = window_for_anchor(&by, 2, &cfg(3, 8, false), false);
        // 3-token budget = 3 chunks total; back-first ping-pong picks
        // chunks 1 then 3 around anchor 2.
        assert_eq!((lo, hi), (1, 3));
    }

    #[test]
    fn window_code_forward_bias_expands_forward_first() {
        // 5 chunks (each 1 token); budget 3 = 3 chunks total.
        // Anchor at 2, code path, forward bias ⇒ (2,4) not (1,3).
        let chunks: Vec<FtsHit> = (0..5).map(|i| hit(i, "aaaa")).collect();
        let by = make_by_index(&chunks);
        let (lo, hi) = window_for_anchor(&by, 2, &cfg(3, 8, true), true);
        assert_eq!((lo, hi), (2, 4));
    }

    #[test]
    fn window_code_forward_bias_backfills_when_forward_exhausted() {
        // Anchor at 4 (last position); forward hits the section
        // boundary immediately, so the whole budget goes backwards.
        let chunks: Vec<FtsHit> = (0..5).map(|i| hit(i, "aaaa")).collect();
        let by = make_by_index(&chunks);
        let (lo, hi) = window_for_anchor(&by, 4, &cfg(3, 8, true), true);
        assert_eq!((lo, hi), (2, 4));
    }

    #[test]
    fn window_alternating_falls_back_when_one_side_boundary() {
        // Anchor at 0 → back is impossible; the fallback branch on
        // each iteration must move forward instead.  Budget 4 tokens
        // ⇒ chunks 0..3 (anchor + 3 forward).
        let chunks: Vec<FtsHit> = (0..6).map(|i| hit(i, "aaaa")).collect();
        let by = make_by_index(&chunks);
        let (lo, hi) = window_for_anchor(&by, 0, &cfg(4, 8, false), false);
        assert_eq!((lo, hi), (0, 3));
    }

    #[test]
    fn window_zero_budget_returns_anchor_only() {
        // Anchor already costs 1 token; budget=0 rejects any grow.
        let chunks: Vec<FtsHit> = (0..3).map(|i| hit(i, "aaaa")).collect();
        let by = make_by_index(&chunks);
        let (lo, hi) = window_for_anchor(&by, 1, &cfg(0, 8, false), false);
        assert_eq!((lo, hi), (1, 1));
    }

    #[test]
    fn stitch_joins_present_chunks_with_single_newline() {
        let chunks = vec![hit(0, "alpha"), hit(1, "beta"), hit(2, "gamma")];
        let by = make_by_index(&chunks);
        let out = stitch(&by, 0, 2);
        assert_eq!(out, "alpha\nbeta\ngamma");
    }

    #[test]
    fn stitch_skips_gaps() {
        // chunk 1 missing — the [0, 2] range still stitches 0 and 2
        // without emitting an empty line for the gap.
        let chunks = vec![hit(0, "alpha"), hit(2, "gamma")];
        let by = make_by_index(&chunks);
        let out = stitch(&by, 0, 2);
        assert_eq!(out, "alpha\ngamma");
    }

    #[test]
    fn approx_tokens_rounds_up() {
        // 4 chars = 1 token; 5 chars = 2 tokens (ceiling division).
        // Ceiling avoids "sub-token" chunks going free against the
        // budget.
        assert_eq!(approx_tokens(""), 0);
        assert_eq!(approx_tokens("abcd"), 1);
        assert_eq!(approx_tokens("abcde"), 2);
    }

    #[test]
    fn expand_mode_parse_recognises_macro_only() {
        assert_eq!(ExpandMode::parse("macro"), ExpandMode::Macro);
        assert_eq!(ExpandMode::parse(""), ExpandMode::None);
        assert_eq!(ExpandMode::parse("none"), ExpandMode::None);
        assert_eq!(ExpandMode::parse("bogus"), ExpandMode::None);
    }
}
