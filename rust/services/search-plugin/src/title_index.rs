//! Per-zone skeleton title index — the Rust mirror of Python's
//! deleted `SearchDaemon.locate()` BM25-lite (#4628, mirrors #4552 /
//! #4545 / #3725).
//!
//! The skeleton is DERIVED data: path tokens come from the FTS
//! `path` field, titles from the first ATX heading of each doc's
//! chunk-0 text — both already indexed, so no schema change and no
//! reindex (issue #4628 non-goal).  [`ZoneSkeleton`] builds the
//! in-memory index from a tantivy stored-doc scan and
//! [`crate::index_manager::IndexManager`] caches it per zone keyed
//! by the FTS searcher generation.
//!
//! Scoring matches Python exactly: title-token overlap × 2 +
//! path-token overlap × 1, DF-capped candidate selection, aggregate
//! budgets, deterministic (-score, path) ordering.

/// Bytes of doc-head text scanned for a title — Python
/// `SKELETON_HEAD_BYTES` parity (titles live at the top of a doc;
/// scanning further finds section headings, not titles).
pub const SKELETON_HEAD_BYTES: usize = 2048;

/// How many leading chunks per doc the skeleton build keeps while
/// reassembling the head window.  The chunker seals frontmatter and
/// preamble into their own chunks, so the first heading typically
/// lives in chunk 0–2; anything past this cap is beyond the 2 KiB
/// head window for any realistic chunk size, and the cap bounds the
/// build's transient memory.
const HEAD_CHUNKS_PER_DOC: usize = 8;

/// Tokenize a path or title into lowercase word tokens — port of
/// Python `text_utils.tokenize_path`.  Splits on `/ _ - .` and
/// whitespace, then on camelCase boundaries within each segment
/// (`parseUserAuth` → `parse user auth`, `HTMLParser` → `html
/// parser`).  Index-time and query-time both use this, so the two
/// sides always agree.
pub fn tokenize(text: &str) -> Vec<String> {
    let mut tokens = Vec::new();
    for part in text.split(|c: char| matches!(c, '/' | '_' | '-' | '.') || c.is_whitespace()) {
        if part.is_empty() {
            continue;
        }
        let chars: Vec<char> = part.chars().collect();
        let mut start = 0;
        for i in 1..chars.len() {
            let prev = chars[i - 1];
            let cur = chars[i];
            // Python _CAMEL_SPLIT_RE: (?<=[a-z0-9])(?=[A-Z]) |
            // (?<=[A-Z])(?=[A-Z][a-z])
            let boundary = ((prev.is_ascii_lowercase() || prev.is_ascii_digit())
                && cur.is_ascii_uppercase())
                || (prev.is_ascii_uppercase()
                    && cur.is_ascii_uppercase()
                    && matches!(chars.get(i + 1), Some(n) if n.is_ascii_lowercase()));
            if boundary {
                tokens.push(chars[start..i].iter().collect::<String>().to_lowercase());
                start = i;
            }
        }
        tokens.push(chars[start..].iter().collect::<String>().to_lowercase());
    }
    tokens
}

/// Extract a doc title from its chunk-0 text: the first ATX heading
/// (`# ...` – `###### ...`) within the first [`SKELETON_HEAD_BYTES`].
/// A leading YAML frontmatter block (`---` ... `---`) is skipped so
/// `#`-comments inside it can't masquerade as headings.  Returns
/// `None` for docs with no heading in the head window — they still
/// join the skeleton on path tokens alone (Python parity: extractor
/// returning None kept the doc, title-less).
pub fn extract_title(chunk0_text: &str) -> Option<String> {
    let mut end = SKELETON_HEAD_BYTES.min(chunk0_text.len());
    while end > 0 && !chunk0_text.is_char_boundary(end) {
        end -= 1;
    }
    let head = &chunk0_text[..end];
    let mut in_frontmatter = false;
    for (i, line) in head.lines().enumerate() {
        let trimmed = line.trim();
        if i == 0 && trimmed == "---" {
            in_frontmatter = true;
            continue;
        }
        if in_frontmatter {
            if trimmed == "---" {
                in_frontmatter = false;
            }
            continue;
        }
        if let Some(rest) = trimmed.strip_prefix('#') {
            let title = rest.trim_start_matches('#').trim();
            if !title.is_empty() {
                return Some(title.to_string());
            }
        }
    }
    None
}

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::sync::LazyLock;

use crate::fts_index::{FtsIndex, IndexError};

// ── Python-parity budgets (pre-P12 daemon.py L146-241) ───────────
//
// TITLE_ARM_MAX_TOKEN_DF: a query token whose posting bucket exceeds
// this is non-discriminative zone-wide — skipped (prefix-scoped
// queries get a second chance via the in-prefix filter).
pub const TITLE_ARM_MAX_TOKEN_DF: usize = 1024;
// Aggregate selection budgets: per-token DF caps do not bound the
// UNION across tokens.  Buckets are consumed most-selective-first
// until either budget is exhausted.
pub const TITLE_ARM_MAX_QUERY_TOKENS: usize = 12;
pub const TITLE_ARM_MAX_CANDIDATES: usize = 4096;
// Prefix-scoped scan budget: an oversized bucket is scanned IN FULL
// or not at all (partial scans of unordered data would make recall
// nondeterministic).
pub const TITLE_ARM_MAX_PREFIX_SCAN: usize = 200_000;
// Evidence gate: one real title-token match (2.0) qualifies a hit
// for the fusion arm; a lone incidental path-token overlap (1.0)
// does not.  Applied by the service-layer caller, not locate().
pub const TITLE_ARM_MIN_SCORE: f32 = 2.0;

/// Function-word query tokens carry no title evidence — without
/// this, "how to configure authentication" scores 4.0 against an
/// unrelated "How To Guide".  Verbatim from Python
/// `_LOCATE_QUERY_STOPWORDS`.
static LOCATE_QUERY_STOPWORDS: LazyLock<std::collections::HashSet<&'static str>> =
    LazyLock::new(|| {
        [
            "a", "an", "the", "and", "or", "of", "to", "in", "on", "at", "for", "with",
            "from", "by", "as", "is", "are", "was", "were", "be", "been", "do", "does",
            "did", "can", "could", "should", "would", "will", "how", "what", "why",
            "when", "where", "which", "who", "whom", "it", "its", "this", "that",
            "these", "those", "i", "me", "my", "you", "your", "we", "our", "they",
            "their",
        ]
        .into_iter()
        .collect()
    });

/// One ranked locate() hit — the raw title-arm row before hydration.
#[derive(Debug, Clone, PartialEq)]
pub struct TitleHit {
    pub path: String,
    pub score: f32,
    pub title: Option<String>,
}

struct SkeletonDoc {
    title: Option<String>,
    title_tokens: BTreeSet<String>,
    path_tokens: BTreeSet<String>,
}

/// In-memory per-zone skeleton index.  Immutable once built — the
/// manager swaps whole snapshots keyed by FTS generation instead of
/// mutating in place (the Rust analogue of Python's build-aside +
/// swap bootstrap, minus the journal: queries between commits see
/// the previous consistent snapshot).
pub struct ZoneSkeleton {
    generation_id: u64,
    docs: BTreeMap<String, SkeletonDoc>,
    title_postings: HashMap<String, BTreeSet<String>>,
    path_postings: HashMap<String, BTreeSet<String>>,
    exact_title: HashMap<String, BTreeSet<String>>,
}

fn round4(x: f32) -> f32 {
    (x * 10_000.0).round() / 10_000.0
}

impl ZoneSkeleton {
    /// Build from a full stored-doc scan of the zone's FTS index.
    /// Runs on the blocking pool (caller's job); cost is one store
    /// pass — ms-scale for typical zones, and paid at most once per
    /// index generation (single-flighted by the manager).
    ///
    /// Title source is the DOC HEAD reassembled from ordered leading
    /// chunks, not chunk 0 alone: the chunker seals frontmatter /
    /// preamble into its own chunk, so a doc shaped
    /// `---\n…\n---\n# Title\n…` stores the frontmatter as chunk 0
    /// and the heading at the START of chunk 1 (review R1 of #4628).
    /// Reassembly is capped at [`HEAD_CHUNKS_PER_DOC`] chunks and
    /// [`SKELETON_HEAD_BYTES`] bytes — Python read the first 2 KiB
    /// of the FILE, and this reconstructs the same window.
    pub fn build(fts: &FtsIndex) -> Result<Self, IndexError> {
        let mut heads: BTreeMap<String, BTreeMap<u32, String>> = BTreeMap::new();
        let generation_id = fts.for_each_chunk(|path, chunk_index, text| {
            let entry = heads.entry(path.to_string()).or_default();
            if (chunk_index as usize) < HEAD_CHUNKS_PER_DOC {
                let mut end = SKELETON_HEAD_BYTES.min(text.len());
                while end > 0 && !text.is_char_boundary(end) {
                    end -= 1;
                }
                entry.insert(chunk_index, text[..end].to_string());
            }
        })?;
        let mut docs: BTreeMap<String, SkeletonDoc> = BTreeMap::new();
        for (path, chunks) in heads {
            let mut head = String::new();
            for text in chunks.into_values() {
                if head.len() >= SKELETON_HEAD_BYTES {
                    break;
                }
                head.push_str(&text);
            }
            let title = extract_title(&head);
            let title_tokens: BTreeSet<String> = title
                .as_deref()
                .map(|t| tokenize(t).into_iter().collect())
                .unwrap_or_default();
            let path_tokens: BTreeSet<String> = tokenize(&path).into_iter().collect();
            docs.insert(
                path,
                SkeletonDoc {
                    title,
                    title_tokens,
                    path_tokens,
                },
            );
        }
        Ok(Self::from_docs(generation_id, docs))
    }

    fn from_docs(generation_id: u64, docs: BTreeMap<String, SkeletonDoc>) -> Self {
        let mut title_postings: HashMap<String, BTreeSet<String>> = HashMap::new();
        let mut path_postings: HashMap<String, BTreeSet<String>> = HashMap::new();
        let mut exact_title: HashMap<String, BTreeSet<String>> = HashMap::new();
        for (path, doc) in &docs {
            for t in &doc.title_tokens {
                title_postings.entry(t.clone()).or_default().insert(path.clone());
            }
            for t in &doc.path_tokens {
                path_postings.entry(t.clone()).or_default().insert(path.clone());
            }
            if let Some(title) = &doc.title {
                let norm = tokenize(title).join(" ");
                if !norm.is_empty() {
                    exact_title.entry(norm).or_default().insert(path.clone());
                }
            }
        }
        Self {
            generation_id,
            docs,
            title_postings,
            path_postings,
            exact_title,
        }
    }

    /// The FTS searcher generation this snapshot was built from —
    /// the manager's cache key (#4628).
    pub fn generation_id(&self) -> u64 {
        self.generation_id
    }

    pub fn doc_count(&self) -> usize {
        self.docs.len()
    }

    /// BM25-lite path+title search — the port of Python
    /// `SearchDaemon.locate()` (pre-P12 daemon.py L2158).  Scores
    /// each candidate by token overlap: title × 2 + path × 1.
    /// Candidate selection is inverted-index driven with per-field
    /// DF caps, an oversized-bucket prefix rescue, and aggregate
    /// token/candidate budgets.  Ordering: score desc, path asc —
    /// deterministic across restarts (RRF ranks depend on it).
    pub fn locate(&self, q: &str, limit: usize, path_prefix: Option<&str>) -> Vec<TitleHit> {
        if q.trim().is_empty() || limit == 0 {
            return Vec::new();
        }
        let raw_tokens: BTreeSet<String> = tokenize(q).into_iter().collect();
        if raw_tokens.is_empty() {
            return Vec::new();
        }
        let query_tokens: BTreeSet<&str> = raw_tokens
            .iter()
            .map(String::as_str)
            .filter(|t| !LOCATE_QUERY_STOPWORDS.contains(*t))
            .collect();

        if query_tokens.is_empty() {
            // Stopword-only query ("How To") — content-token selection
            // is impossible, but an EXACT normalized title match is
            // still unambiguous evidence.
            let norm_q = tokenize(q).join(" ");
            let Some(bucket) = self.exact_title.get(&norm_q) else {
                return Vec::new();
            };
            if bucket.len() > TITLE_ARM_MAX_TOKEN_DF {
                return Vec::new();
            }
            let mut hits = Vec::new();
            for path in bucket {
                if let Some(p) = path_prefix {
                    if !path.starts_with(p) {
                        continue;
                    }
                }
                let Some(doc) = self.docs.get(path) else { continue };
                let score = 2.0 * doc.title_tokens.len() as f32;
                hits.push(TitleHit {
                    path: path.clone(),
                    score: round4(score),
                    title: doc.title.clone(),
                });
                if hits.len() >= limit {
                    break;
                }
            }
            return hits;
        }

        // Per-token, per-field posting buckets under the DF cap;
        // oversized buckets are set aside for the prefix rescue.
        // field_rank 0 = title, 1 = path (stable tie-break order).
        let mut token_buckets: BTreeMap<&str, Vec<Vec<&String>>> = BTreeMap::new();
        let mut token_min_df: BTreeMap<&str, usize> = BTreeMap::new();
        let mut oversized: Vec<(usize, &str, usize, &BTreeSet<String>)> = Vec::new();
        for token in &query_tokens {
            for (field_rank, index) in
                [&self.title_postings, &self.path_postings].into_iter().enumerate()
            {
                let Some(bucket) = index.get(*token) else { continue };
                if bucket.len() > TITLE_ARM_MAX_TOKEN_DF {
                    if path_prefix.is_some() {
                        oversized.push((bucket.len(), token, field_rank, bucket));
                    }
                    continue;
                }
                token_buckets.entry(token).or_default().push(bucket.iter().collect());
                let e = token_min_df.entry(token).or_insert(usize::MAX);
                *e = (*e).min(bucket.len());
            }
        }

        // Oversized second chance: scan whole buckets (never partial)
        // in stable (size, token, field) order under a shared budget;
        // keep the in-prefix subset when it fits the DF cap.
        if let Some(prefix) = path_prefix {
            let mut scan_budget = TITLE_ARM_MAX_PREFIX_SCAN;
            oversized.sort_by(|a, b| a.0.cmp(&b.0).then(a.1.cmp(b.1)).then(a.2.cmp(&b.2)));
            for (size, token, _field_rank, big) in oversized {
                if size > scan_budget {
                    continue;
                }
                scan_budget -= size;
                let filtered: Vec<&String> =
                    big.iter().filter(|p| p.starts_with(prefix)).collect();
                if filtered.is_empty() || filtered.len() > TITLE_ARM_MAX_TOKEN_DF {
                    continue;
                }
                let e = token_min_df.entry(token).or_insert(usize::MAX);
                *e = (*e).min(filtered.len());
                token_buckets.entry(token).or_default().push(filtered);
            }
        }

        // Most-selective-first token expansion under aggregate budgets.
        let mut selected: Vec<&str> = token_buckets.keys().copied().collect();
        selected.sort_by_key(|t| (token_min_df[t], *t));
        selected.truncate(TITLE_ARM_MAX_QUERY_TOKENS);

        let mut candidates: BTreeSet<&String> = BTreeSet::new();
        'tokens: for token in &selected {
            for bucket in &token_buckets[*token] {
                let remaining = TITLE_ARM_MAX_CANDIDATES.saturating_sub(candidates.len());
                if remaining == 0 {
                    break 'tokens;
                }
                // Buckets are sorted (BTreeSet-derived) — a capped
                // take is deterministic.
                candidates.extend(bucket.iter().take(remaining).copied());
            }
            if candidates.len() >= TITLE_ARM_MAX_CANDIDATES {
                break;
            }
        }

        let mut scored: Vec<(f32, &String, &Option<String>)> = Vec::new();
        for path in candidates {
            if let Some(p) = path_prefix {
                if !path.starts_with(p) {
                    continue;
                }
            }
            let Some(doc) = self.docs.get(path) else { continue };
            let title_overlap = doc
                .title_tokens
                .iter()
                .filter(|t| query_tokens.contains(t.as_str()))
                .count();
            let path_overlap = doc
                .path_tokens
                .iter()
                .filter(|t| query_tokens.contains(t.as_str()))
                .count();
            let score = title_overlap as f32 * 2.0 + path_overlap as f32;
            if score > 0.0 {
                scored.push((score, path, &doc.title));
            }
        }
        scored.sort_by(|a, b| {
            b.0.partial_cmp(&a.0)
                .unwrap_or(std::cmp::Ordering::Equal)
                .then_with(|| a.1.cmp(b.1))
        });
        scored.truncate(limit);
        scored
            .into_iter()
            .map(|(score, path, title)| TitleHit {
                path: path.clone(),
                score: round4(score),
                title: title.clone(),
            })
            .collect()
    }
}

#[cfg(test)]
impl ZoneSkeleton {
    /// Test-only: empty skeleton for synthetic DF-cap fixtures.
    pub fn empty_for_test() -> Self {
        Self::from_docs(0, BTreeMap::new())
    }

    /// Test-only: insert one doc and rebuild postings.  O(n) per
    /// call — fine for fixtures, not exposed to production code.
    pub fn insert_doc_for_test(&mut self, path: &str, title: Option<&str>) {
        let title = title.map(str::to_string);
        let title_tokens: BTreeSet<String> = title
            .as_deref()
            .map(|t| tokenize(t).into_iter().collect())
            .unwrap_or_default();
        let path_tokens: BTreeSet<String> = tokenize(path).into_iter().collect();
        let mut docs = std::mem::take(&mut self.docs);
        docs.insert(
            path.to_string(),
            SkeletonDoc {
                title,
                title_tokens,
                path_tokens,
            },
        );
        *self = Self::from_docs(self.generation_id, docs);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tokenize_matches_python_docstring_examples() {
        assert_eq!(
            tokenize("/workspace/src/auth/parseUserLogin.py"),
            ["workspace", "src", "auth", "parse", "user", "login", "py"]
        );
        assert_eq!(tokenize("/docs/README_API.md"), ["docs", "readme", "api", "md"]);
    }

    #[test]
    fn tokenize_splits_acronym_camel_boundary() {
        // (?<=[A-Z])(?=[A-Z][a-z]): HTMLParser → html parser
        assert_eq!(tokenize("HTMLParser"), ["html", "parser"]);
        // digit→upper boundary: v2Beta → v2 beta
        assert_eq!(tokenize("v2Beta"), ["v2", "beta"]);
    }

    #[test]
    fn tokenize_title_text_with_spaces() {
        assert_eq!(tokenize("Atlas Design Doc"), ["atlas", "design", "doc"]);
    }

    #[test]
    fn tokenize_empty_and_separator_only() {
        assert!(tokenize("").is_empty());
        assert!(tokenize("///--..").is_empty());
    }

    #[test]
    fn extract_title_first_atx_heading() {
        assert_eq!(
            extract_title("# Atlas Design Doc\n\nbody text"),
            Some("Atlas Design Doc".to_string())
        );
        // Deeper heading levels count too; hashes stripped.
        assert_eq!(extract_title("### Deep Title\nbody"), Some("Deep Title".to_string()));
    }

    #[test]
    fn extract_title_skips_yaml_frontmatter() {
        let text = "---\ntitle: raw\n# not a heading, a YAML comment\n---\n# Real Title\nbody";
        assert_eq!(extract_title(text), Some("Real Title".to_string()));
    }

    #[test]
    fn extract_title_none_when_no_heading() {
        assert_eq!(extract_title("plain prose with no heading"), None);
        assert_eq!(extract_title(""), None);
    }

    #[test]
    fn extract_title_respects_head_cap() {
        // Heading past the 2 KiB window is not a title.
        let text = format!("{}\n# Late Heading\n", "x".repeat(SKELETON_HEAD_BYTES));
        assert_eq!(extract_title(&text), None);
    }

    // ── ZoneSkeleton build + locate ────────────────────────────

    use crate::fts_index::FtsIndex;

    fn fts_with(docs: &[(&str, u32, &str)]) -> std::sync::Arc<FtsIndex> {
        let dir = tempfile::tempdir().expect("tempdir").keep().join("fts");
        let idx = FtsIndex::open_or_create(dir).expect("open");
        for (path, chunk_index, text) in docs {
            idx.add_document(path, *chunk_index, text, Some(1)).expect("add");
        }
        idx.commit().expect("commit");
        idx
    }

    fn atlas_skeleton() -> ZoneSkeleton {
        // The acceptance workload shape from the Python plan doc:
        // /designs/atlas.md titled "Atlas Design Doc", plus decoys.
        let fts = fts_with(&[
            ("/designs/atlas.md", 0, "# Atlas Design Doc\nbody of atlas"),
            ("/designs/atlas.md", 1, "more atlas body"),
            ("/notes/other.md", 0, "# Meeting Notes\nunrelated"),
            ("/src/parseUserLogin.py", 0, "def parse():\n    pass"),
        ]);
        ZoneSkeleton::build(&fts).expect("build")
    }

    #[test]
    fn build_indexes_titles_and_paths_from_chunk0() {
        let sk = atlas_skeleton();
        assert_eq!(sk.doc_count(), 3, "one skeleton doc per path, chunk 1 ignored");
    }

    #[test]
    fn locate_scores_title_2x_plus_path_1x() {
        let sk = atlas_skeleton();
        let hits = sk.locate("atlas design doc", 10, None);
        assert_eq!(hits[0].path, "/designs/atlas.md");
        // 3 title-token overlaps × 2.0 + 1 path-token overlap ("atlas")
        // — same 7.0 the Python acceptance test pinned.
        assert!((hits[0].score - 7.0).abs() < 1e-4, "got {}", hits[0].score);
        assert_eq!(hits[0].title.as_deref(), Some("Atlas Design Doc"));
    }

    #[test]
    fn locate_path_only_match_scores_1_per_token() {
        let sk = atlas_skeleton();
        // "login" hits only path tokens of parseUserLogin.py.
        let hits = sk.locate("login", 10, None);
        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0].path, "/src/parseUserLogin.py");
        assert!((hits[0].score - 1.0).abs() < 1e-4);
    }

    #[test]
    fn locate_stopwords_stripped_from_query() {
        let sk = atlas_skeleton();
        // "the atlas design doc" — "the" contributes nothing.
        let with = sk.locate("the atlas design doc", 10, None);
        let without = sk.locate("atlas design doc", 10, None);
        assert_eq!(with, without);
    }

    #[test]
    fn locate_stopword_only_query_uses_exact_title_match() {
        let fts = fts_with(&[
            ("/docs/howto.md", 0, "# How To\nguide body"),
            ("/docs/other.md", 0, "# Setup Guide\nbody"),
        ]);
        let sk = ZoneSkeleton::build(&fts).expect("build");
        // Every query token is a stopword → only an exact normalized
        // title match returns; score = 2 × title-token-count.
        let hits = sk.locate("How To", 10, None);
        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0].path, "/docs/howto.md");
        assert!((hits[0].score - 4.0).abs() < 1e-4);
        // Non-matching stopword-only query → nothing.
        assert!(sk.locate("the who", 10, None).is_empty());
    }

    #[test]
    fn locate_path_prefix_filters() {
        let sk = atlas_skeleton();
        assert!(sk.locate("atlas design doc", 10, Some("/notes/")).is_empty());
        assert_eq!(
            sk.locate("atlas design doc", 10, Some("/designs/"))[0].path,
            "/designs/atlas.md"
        );
    }

    #[test]
    fn locate_ordering_is_deterministic_on_ties() {
        let fts = fts_with(&[
            ("/b/report.md", 0, "# Q3 Report\nbody"),
            ("/a/report.md", 0, "# Q3 Report\nbody"),
        ]);
        let sk = ZoneSkeleton::build(&fts).expect("build");
        let hits = sk.locate("q3 report", 10, None);
        assert_eq!(hits.len(), 2);
        // Equal scores → ascending path.
        assert_eq!(hits[0].path, "/a/report.md");
        assert_eq!(hits[1].path, "/b/report.md");
    }

    #[test]
    fn locate_empty_query_and_no_hit_query() {
        let sk = atlas_skeleton();
        assert!(sk.locate("", 10, None).is_empty());
        assert!(sk.locate("   ", 10, None).is_empty());
        assert!(sk.locate("zzz qqq nomatch", 10, None).is_empty());
    }

    #[test]
    fn locate_respects_limit() {
        let fts = fts_with(&[
            ("/a.md", 0, "# widget alpha\nx"),
            ("/b.md", 0, "# widget beta\nx"),
            ("/c.md", 0, "# widget gamma\nx"),
        ]);
        let sk = ZoneSkeleton::build(&fts).expect("build");
        assert_eq!(sk.locate("widget", 2, None).len(), 2);
    }

    #[test]
    fn build_finds_heading_sealed_past_chunk0_by_real_chunker() {
        // Review R1 (#4628): the production chunker seals frontmatter
        // (and any preamble) into its own leading chunk, so the first
        // heading lands at the START of chunk 1 — a chunk-0-only scan
        // silently loses the title.  Index through the REAL chunker
        // and assert the reassembled head still finds it.
        let text = "---\ntitle: raw\nauthor: someone\n---\n# Atlas Design Doc\n\nbody paragraph.\n";
        let chunks = crate::chunker::chunk_document(text);
        assert!(
            chunks.len() >= 2 && !chunks[0].text.contains("# Atlas"),
            "fixture must reproduce the frontmatter-sealed shape: {chunks:?}"
        );
        let dir = tempfile::tempdir().expect("tempdir").keep().join("fts");
        let idx = FtsIndex::open_or_create(dir).expect("open");
        for c in &chunks {
            idx.add_document("/designs/atlas.md", c.chunk_index, &c.text, Some(1))
                .expect("add");
        }
        idx.commit().expect("commit");
        let sk = ZoneSkeleton::build(&idx).expect("build");
        let hits = sk.locate("atlas design doc", 10, None);
        assert_eq!(hits.first().map(|h| h.path.as_str()), Some("/designs/atlas.md"));
        assert_eq!(hits[0].title.as_deref(), Some("Atlas Design Doc"));
    }

    #[test]
    fn locate_df_cap_drops_flooded_tokens_but_prefix_rescues() {
        // A token flooding > TITLE_ARM_MAX_TOKEN_DF paths is dropped
        // zone-wide; with a path_prefix the oversized bucket gets a
        // second chance via the in-prefix filter.  Build synthetic
        // skeleton directly (indexing 1025 tantivy docs in a unit
        // test is slow).
        let mut sk = ZoneSkeleton::empty_for_test();
        for i in 0..(TITLE_ARM_MAX_TOKEN_DF + 1) {
            sk.insert_doc_for_test(&format!("/flood/doc{i}"), Some("Common Term"));
        }
        sk.insert_doc_for_test("/special/doc", Some("Common Term"));
        // Zone-wide: "common term" postings exceed the DF cap → no hits.
        assert!(sk.locate("common term", 10, None).is_empty());
        // Prefix-scoped: the oversized bucket is filtered to the
        // prefix (1 doc ≤ cap) and the hit comes back.
        let hits = sk.locate("common term", 10, Some("/special/"));
        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0].path, "/special/doc");
    }
}
