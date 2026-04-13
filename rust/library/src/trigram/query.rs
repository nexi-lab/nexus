//! Trigram query construction from patterns and regex.
//!
//! Extracts trigrams from literal patterns and uses `regex_syntax` to
//! extract literal substrings from regex patterns for trigram lookup.

use super::extract::extract_trigrams_for_query;
use crate::search::literal::is_literal_pattern;

/// A trigram query representing the set of trigrams needed to match a pattern.
#[derive(Debug, Clone)]
pub enum TrigramQuery {
    /// All trigrams must be present (conjunction).
    And(Vec<[u8; 3]>),
    /// At least one sub-query must match (disjunction).
    Or(Vec<TrigramQuery>),
    /// Pattern is too short or complex — all files are candidates.
    All,
}

impl TrigramQuery {
    /// Returns true if this query matches all files (no filtering).
    pub fn is_all(&self) -> bool {
        matches!(self, TrigramQuery::All)
    }

    /// Returns true if this query has no useful trigrams.
    pub fn is_empty(&self) -> bool {
        match self {
            TrigramQuery::All => true,
            TrigramQuery::And(trigrams) => trigrams.is_empty(),
            TrigramQuery::Or(queries) => queries.is_empty() || queries.iter().all(|q| q.is_empty()),
        }
    }
}

/// Build a `TrigramQuery` from a search pattern.
///
/// For literal patterns, extracts trigrams directly.
/// For regex patterns, uses `regex_syntax` to extract literal substrings.
pub fn build_trigram_query(pattern: &str) -> TrigramQuery {
    if pattern.len() < 3 {
        return TrigramQuery::All;
    }

    if is_literal_pattern(pattern) {
        let trigrams = extract_trigrams_for_query(pattern);
        if trigrams.is_empty() {
            return TrigramQuery::All;
        }
        return TrigramQuery::And(trigrams);
    }

    // For regex patterns, parse with regex_syntax and extract literals.
    build_trigram_query_from_regex(pattern)
}

/// Extract trigrams from a regex pattern using `regex_syntax::Hir`.
fn build_trigram_query_from_regex(pattern: &str) -> TrigramQuery {
    let hir = match regex_syntax::parse(pattern) {
        Ok(h) => h,
        Err(_) => return TrigramQuery::All,
    };

    extract_from_hir(&hir)
}

/// Recursively extract trigram queries from an HIR node.
fn extract_from_hir(hir: &regex_syntax::hir::Hir) -> TrigramQuery {
    use regex_syntax::hir::HirKind;

    match hir.kind() {
        HirKind::Literal(lit) => {
            let bytes = &lit.0;
            if bytes.len() < 3 {
                return TrigramQuery::All;
            }
            let trigrams = extract_trigrams_for_query_bytes(bytes);
            if trigrams.is_empty() {
                TrigramQuery::All
            } else {
                TrigramQuery::And(trigrams)
            }
        }
        HirKind::Concat(subs) => {
            // Concatenation: collect all literal bytes, extract trigrams.
            let mut all_bytes = Vec::new();
            let mut _has_non_literal = false;

            for sub in subs {
                if let HirKind::Literal(lit) = sub.kind() {
                    all_bytes.extend_from_slice(&lit.0);
                } else {
                    // If we accumulated enough bytes, extract trigrams from them.
                    if all_bytes.len() >= 3 {
                        let trigrams = extract_trigrams_for_query_bytes(&all_bytes);
                        if !trigrams.is_empty() {
                            // We found useful trigrams from the literal prefix/part.
                            // Continue to collect more, but mark non-literal seen.
                        }
                    }
                    _has_non_literal = true;
                    // Try to extract from sub-expression too.
                    all_bytes.clear();
                }
            }

            // Collect trigrams from accumulated literal bytes.
            let mut all_trigrams = Vec::new();

            // Re-walk to get trigrams from each contiguous literal run.
            let mut run_bytes = Vec::new();
            for sub in subs {
                if let HirKind::Literal(lit) = sub.kind() {
                    run_bytes.extend_from_slice(&lit.0);
                } else {
                    if run_bytes.len() >= 3 {
                        all_trigrams.extend(extract_trigrams_for_query_bytes(&run_bytes));
                    }
                    run_bytes.clear();

                    // Recurse into non-literal sub-expression.
                    let sub_query = extract_from_hir(sub);
                    if let TrigramQuery::And(trigrams) = sub_query {
                        all_trigrams.extend(trigrams);
                    }
                }
            }
            if run_bytes.len() >= 3 {
                all_trigrams.extend(extract_trigrams_for_query_bytes(&run_bytes));
            }

            if all_trigrams.is_empty() {
                TrigramQuery::All
            } else {
                // Deduplicate.
                all_trigrams.sort();
                all_trigrams.dedup();
                TrigramQuery::And(all_trigrams)
            }
        }
        HirKind::Alternation(alts) => {
            // OR: each alternative must have trigrams for effective filtering.
            let sub_queries: Vec<TrigramQuery> = alts.iter().map(extract_from_hir).collect();

            // If any alternative is All, the whole OR is All (can't filter).
            if sub_queries.iter().any(|q| q.is_all()) {
                return TrigramQuery::All;
            }

            TrigramQuery::Or(sub_queries)
        }
        HirKind::Repetition(rep) => {
            // For repetitions, we can extract from the sub-pattern.
            // But only if min >= 1 (pattern must appear at least once).
            if rep.min >= 1 {
                extract_from_hir(&rep.sub)
            } else {
                TrigramQuery::All
            }
        }
        HirKind::Capture(cap) => extract_from_hir(&cap.sub),
        // Class, Look, Empty — no useful trigrams.
        _ => TrigramQuery::All,
    }
}

/// Extract trigrams from raw bytes (for regex literal extraction).
fn extract_trigrams_for_query_bytes(bytes: &[u8]) -> Vec<[u8; 3]> {
    if bytes.len() < 3 {
        return Vec::new();
    }

    let mut seen = ahash::AHashSet::new();
    for window in bytes.windows(3) {
        seen.insert([window[0], window[1], window[2]]);
    }
    seen.into_iter().collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_literal_to_trigram_query() {
        let query = build_trigram_query("hello");
        match query {
            TrigramQuery::And(trigrams) => {
                assert_eq!(trigrams.len(), 3);
                assert!(trigrams.contains(&[b'h', b'e', b'l']));
                assert!(trigrams.contains(&[b'e', b'l', b'l']));
                assert!(trigrams.contains(&[b'l', b'l', b'o']));
            }
            _ => panic!("Expected And query"),
        }
    }

    #[test]
    fn test_regex_literal_extraction() {
        // "foo.*bar" should extract trigrams from "foo" and "bar"
        let query = build_trigram_query("foo.*bar");
        match query {
            TrigramQuery::And(trigrams) => {
                assert!(trigrams.contains(&[b'f', b'o', b'o']));
                assert!(trigrams.contains(&[b'b', b'a', b'r']));
            }
            _ => panic!("Expected And query, got {:?}", query),
        }
    }

    #[test]
    fn test_short_pattern_returns_all() {
        let query = build_trigram_query("ab");
        assert!(query.is_all());
    }

    #[test]
    fn test_alternation() {
        // "foo|bar" → Or([And(["foo"]), And(["bar"])])
        let query = build_trigram_query("foo|bar");
        match query {
            TrigramQuery::Or(subs) => {
                assert_eq!(subs.len(), 2);
                for sub in &subs {
                    match sub {
                        TrigramQuery::And(trigrams) => {
                            assert_eq!(trigrams.len(), 1);
                        }
                        _ => panic!("Expected And sub-query"),
                    }
                }
            }
            _ => panic!("Expected Or query, got {:?}", query),
        }
    }

    #[test]
    fn test_complex_regex() {
        // Character class — can't extract useful trigrams.
        let query = build_trigram_query("[abc]+");
        assert!(query.is_all());
    }

    #[test]
    fn test_empty_pattern() {
        let query = build_trigram_query("");
        assert!(query.is_all());
    }

    #[test]
    fn test_dot_star_only() {
        let query = build_trigram_query(".*");
        assert!(query.is_all());
    }
}
