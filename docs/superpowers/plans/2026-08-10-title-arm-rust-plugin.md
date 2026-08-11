# Title-Arm Hybrid Fusion in the Rust Search Plugin (#4628) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Mirror Python #4552's title arm into the Rust `nexus-search-plugin`: a per-zone in-memory skeleton index (path tokens + first-heading title, derived from already-indexed FTS data) joins the hybrid keyword lane as a second RRF arm, so title-shaped queries ("atlas design doc") surface their target even when body chunks are weak.

**Architecture:** A new `title_index.rs` module ports the deleted Python `SearchDaemon.locate()` scoring (title tokens weighted 2×, path tokens 1×, DF caps, candidate budgets, deterministic ordering) over a `ZoneSkeleton` built lazily from a tantivy stored-doc scan and cached per zone, keyed by the FTS searcher generation (auto-invalidates on every commit — no invalidation call sites to maintain). In the `Query` RPC's Hybrid branch, a third parallel blocking task runs locate; its hits hydrate to `QueryResult` shape (borrowing chunk rows from the fetched legs, FTS chunk-0 fallback) and fuse with the chunk-FTS arm via a new N-way `fusion::rrf_multi` (with Python's top-rank bonus + `title_score` stamping) BEFORE the existing `fuse_hybrid` alpha/fusion stage, which is untouched. Empty title arm ⇒ the keyword lane passes through byte-identical to today (guarantees non-title-query parity under ALL fusion methods, including WEIGHTED).

**Tech Stack:** Rust (tantivy 0.22, tonic/prost, parking_lot), proto3 (`rust/services/proto/nexus/search/v1/search.proto`), Python shim passthrough (`src/nexus/bricks/search/daemon.py` + regenerated `grpc_tools` stubs).

**Design SSOT:** `docs/superpowers/specs/2026-07-31-title-arm-hybrid-fusion-design.md` (the approved Python #4545 design — this plan is its Rust-plugin mirror per issue #4628). Python reference implementation: `git show 469d64de4^:src/nexus/bricks/search/daemon.py` (locate: L2158, hydration: L2336, gather: L2432, fusion swap: L3475).

## Global Constraints

- Branch: `feat/4628-title-arm-rust-plugin` off `develop`. Linear history repo — rebase, never merge (project memory).
- Work entirely inside this worktree: `/Users/tafeng/nexus/.claude/worktrees/streamed-popping-pie`.
- Rust commands run from the worktree root: `cargo test -p nexus-search-plugin`, `cargo clippy -p nexus-search-plugin --all-targets -- -D warnings`, `cargo fmt -p nexus-search-plugin`.
- Python tests (worktree gotcha — no local venv, `uv run` fails on native deps): `PYTHONPATH=$PWD/src /Users/tafeng/nexus/.venv/bin/python -m pytest <path> -v`. If a bare `.venv` dir exists in the worktree, `rm -rf .venv` first.
- Python-parity constants are copied verbatim from the pre-P12 daemon: `TITLE_ARM_MAX_TOKEN_DF = 1024`, `TITLE_ARM_MAX_HYDRATION_FETCH = 32`, `TITLE_ARM_MAX_QUERY_TOKENS = 12`, `TITLE_ARM_MAX_CANDIDATES = 4096`, `TITLE_ARM_MAX_PREFIX_SCAN = 200_000`, `TITLE_ARM_MIN_SCORE = 2.0`, `SKELETON_HEAD_BYTES = 2048`, `RRF_TOP1_BONUS = 0.05`, `RRF_TOP3_BONUS = 0.02`.
- Env knob: `NEXUS_SEARCH_TITLE_ARM` — default ON; `"false" | "0" | "no"` (trimmed, case-insensitive) disables. Same parse rule as Python's lifespan parser.
- Non-goals (from issue #4628): no reindexing (skeleton derives from existing indexed data), no cross-zone federation changes, no schema change to the tantivy index, no `Locate` RPC change (that RPC is a path-existence check, unrelated to Python's ranked `locate()`).
- Scope note — arm shape: Python's kw sub-fusion had a page-BM25 arm on the Postgres branch only; the SQLite branch passed an empty page arm (a no-op, per the design SSOT). The Rust plugin has a chunk-level tantivy index only — the mirror is the SQLite shape: `rrf_multi([chunk, title])`, with the N-way signature ready for a future page arm. Do NOT derive a fake page arm by pooling chunk hits — that double-votes chunk evidence and breaks the non-title-query parity acceptance criterion.
- Scope note — hybrid only: the title arm runs in `QueryType::Hybrid` only, matching the design SSOT. (Python also ran it on its no-embedding keyword fallback; the Rust hybrid path has no such fallback — it errors "hybrid unavailable" — so there is nothing to mirror there.)

---

### Task 1: Proto `title_score` field + Rust struct-literal catch-up

**Files:**
- Modify: `rust/services/proto/nexus/search/v1/search.proto` (QueryResult message, after `expanded_context = 7`)
- Modify: every Rust `QueryResult { ... }` struct literal in `rust/services/search-plugin/src/` and `rust/services/search-plugin/tests/` (prost adds a field; literals must name it)

**Interfaces:**
- Produces: `QueryResult.title_score: Option<f32>` (prost-generated from `optional float`), used by Tasks 2, 7, 8, 9.

- [ ] **Step 1: Add the proto field**

In `rust/services/proto/nexus/search/v1/search.proto`, inside `message QueryResult`, after the `expanded_context` field (line ~390):

```proto
  // Title-arm attribution (#4628, mirrors Python #4552/#4545).  Set
  // only on hybrid results the title arm voted for — carries the
  // locate() score (title-token overlaps × 2 + path-token overlaps)
  // so callers can see WHY a weak-body doc surfaced.  Absent for
  // keyword / semantic modes, for hybrid hits with no title match,
  // and when the arm is disabled (NEXUS_SEARCH_TITLE_ARM=false).
  optional float title_score = 8;
```

- [ ] **Step 2: Build to surface every literal that needs the new field**

Run: `cargo build -p nexus-search-plugin 2>&1 | head -50`
Expected: E0063 "missing field `title_score`" errors listing every `QueryResult { ... }` literal (service.rs `fts_hit_to_result` + `enrich_ann_hit`, fusion.rs test helper `hit()` + two full literals in tests, and any e2e test literals).

- [ ] **Step 3: Add `title_score: None` to every flagged literal**

For each error site add `title_score: None,` (production sites `fts_hit_to_result` / `enrich_ann_hit` are keyword/semantic-lane constructors — always `None` there by design). `fusion::finalise` uses `..m.template` so it needs no change.

- [ ] **Step 4: Verify the crate compiles and existing tests pass**

Run: `cargo test -p nexus-search-plugin 2>&1 | tail -15`
Expected: all existing tests PASS (behaviour unchanged — the field is inert until Task 7 wires it).

- [ ] **Step 5: Commit**

```bash
git add rust/services/proto/nexus/search/v1/search.proto rust/services/search-plugin/
git commit -m "feat(search-plugin): add QueryResult.title_score wire field (#4628)"
```

---

### Task 2: `fusion::rrf_multi` — N-way RRF with top-rank bonus + title_score stamping

**Files:**
- Modify: `rust/services/search-plugin/src/fusion.rs`

**Interfaces:**
- Consumes: `QueryResult.title_score` from Task 1; private `DocKey` / `MergedHit` / `finalise` already in fusion.rs.
- Produces: `pub enum ArmKind { Chunk, Title }` and `pub fn rrf_multi(arms: &[(ArmKind, &[QueryResult])], k: u32) -> Vec<QueryResult>`; `pub const RRF_TOP1_BONUS: f32 = 0.05;` `pub const RRF_TOP3_BONUS: f32 = 0.02;`. Task 7 calls `fusion::rrf_multi(&[(ArmKind::Chunk, &keyword), (ArmKind::Title, &hydrated)], opts.rrf_k)`.

- [ ] **Step 1: Write the failing tests** (append to `mod tests` in fusion.rs)

```rust
    #[test]
    fn rrf_multi_accumulates_across_arms_and_stamps_title_score() {
        // /a is in both arms → RRF sum + title_score stamped even on
        // the chunk arm's first-seen template.  /b chunk-only → no
        // title_score.  /c title-only → title_score set.
        let chunk = vec![hit("/a", 0, 10.0), hit("/b", 0, 5.0)];
        let title = vec![hit("/a", 0, 7.0), hit("/c", 0, 4.0)];
        let fused = rrf_multi(
            &[(ArmKind::Chunk, &chunk), (ArmKind::Title, &title)],
            DEFAULT_RRF_K,
        );
        let a = fused.iter().find(|r| r.path == "/a").expect("/a");
        let b = fused.iter().find(|r| r.path == "/b").expect("/b");
        let c = fused.iter().find(|r| r.path == "/c").expect("/c");
        assert_eq!(a.title_score, Some(7.0), "merged doc carries the arm's own score");
        assert_eq!(b.title_score, None);
        assert_eq!(c.title_score, Some(4.0));
        // Shared doc leads: two votes + top-1 bonus.
        assert_eq!(fused[0].path, "/a");
    }

    #[test]
    fn rrf_multi_matches_reference_formula_with_top_rank_bonus() {
        // /a: rank 1 in chunk, rank 2 in title, best rank 1
        //   → 1/61 + 1/62 + RRF_TOP1_BONUS
        // /z: rank 1 in title, best rank 1 → 1/61 + RRF_TOP1_BONUS
        // /b: rank 2 in chunk, best rank 2 → 1/62 + RRF_TOP3_BONUS
        let chunk = vec![hit("/a", 0, 1.0), hit("/b", 0, 0.5)];
        let title = vec![hit("/z", 0, 9.0), hit("/a", 0, 8.0)];
        let fused = rrf_multi(&[(ArmKind::Chunk, &chunk), (ArmKind::Title, &title)], 60);
        let score_of = |p: &str| fused.iter().find(|r| r.path == p).unwrap().score;
        let close = |a: f32, b: f32| (a - b).abs() < 1e-5;
        assert!(close(score_of("/a"), 1.0 / 61.0 + 1.0 / 62.0 + RRF_TOP1_BONUS));
        assert!(close(score_of("/z"), 1.0 / 61.0 + RRF_TOP1_BONUS));
        assert!(close(score_of("/b"), 1.0 / 62.0 + RRF_TOP3_BONUS));
    }

    #[test]
    fn rrf_multi_single_arm_preserves_input_order() {
        // Guards the Task 7 pass-through contract: even when callers
        // DO sub-fuse a lone chunk arm, ranks must not move.
        let chunk = vec![hit("/x", 0, 9.0), hit("/y", 0, 5.0), hit("/z", 0, 1.0)];
        let fused = rrf_multi(&[(ArmKind::Chunk, &chunk)], DEFAULT_RRF_K);
        let paths: Vec<&str> = fused.iter().map(|r| r.path.as_str()).collect();
        assert_eq!(paths, ["/x", "/y", "/z"]);
        assert!(fused.iter().all(|r| r.title_score.is_none()));
    }

    #[test]
    fn rrf_multi_first_seen_template_wins_but_title_score_lands() {
        // Chunk arm listed first → its chunk_text/mtime win the base
        // copy (mirrors Python "title arm listed last" rule); the
        // title vote still stamps title_score on that template.
        let mut chunk_row = hit("/a", 3, 1.0);
        chunk_row.chunk_text = "leg-text".into();
        chunk_row.mtime_ms = Some(100);
        let mut title_row = hit("/a", 3, 6.0);
        title_row.chunk_text = "hydrated-text".into();
        title_row.mtime_ms = Some(200);
        let fused = rrf_multi(
            &[(ArmKind::Chunk, &[chunk_row]), (ArmKind::Title, &[title_row])],
            60,
        );
        assert_eq!(fused.len(), 1);
        assert_eq!(fused[0].chunk_text, "leg-text");
        assert_eq!(fused[0].mtime_ms, Some(100));
        assert_eq!(fused[0].title_score, Some(6.0));
    }

    #[test]
    fn rrf_multi_key_alignment_merges_only_same_chunk_index() {
        // (path, chunk_index) is the fusion identity — a title hit
        // hydrated to chunk 0 does NOT merge with a chunk-arm hit at
        // chunk 2 (that's why hydration borrows the leg's index).
        let chunk = vec![hit("/a", 2, 1.0)];
        let title = vec![hit("/a", 0, 6.0)];
        let fused = rrf_multi(&[(ArmKind::Chunk, &chunk), (ArmKind::Title, &title)], 60);
        assert_eq!(fused.len(), 2, "different chunk_index must not merge");
    }
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cargo test -p nexus-search-plugin --lib fusion 2>&1 | tail -5`
Expected: FAIL to compile — `rrf_multi` / `ArmKind` / bonus constants not defined.

- [ ] **Step 3: Implement `rrf_multi`** (in fusion.rs, after `weighted`, before `pool_by_document`)

```rust
/// Top-rank bonus constants (#3773 via Python `rrf_multi_fusion`):
/// a doc whose BEST rank across arms is 1 gets +0.05; ranks 2-3 get
/// +0.02.  Rewards strong evidence in ANY single arm over uniform
/// mediocrity.  Applied only by [`rrf_multi`] — the 2-way [`rrf`]
/// stays bonus-free for backward compatibility with existing hybrid
/// rankings.
pub const RRF_TOP1_BONUS: f32 = 0.05;
pub const RRF_TOP3_BONUS: f32 = 0.02;

/// Which retrieval arm a source list is.  `rrf_multi` uses this for
/// per-arm attribution: a `Title` arm's votes stamp
/// `QueryResult.title_score` with the arm's own score so callers see
/// why a weak-body doc surfaced (#4628).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ArmKind {
    Chunk,
    Title,
}

/// N-way reciprocal-rank fusion — the keyword-lane sub-fusion for
/// the title arm (#4628; mirrors Python `rrf_multi_fusion`, #4552).
/// Same `(path, chunk_index)` identity and first-seen-template rules
/// as [`rrf`]; adds the top-rank bonus and per-arm attribution.
/// Returns the FULL fused union (callers pool + truncate downstream,
/// matching the Python "fuse complete union, cap later" shape).
pub fn rrf_multi(arms: &[(ArmKind, &[QueryResult])], k: u32) -> Vec<QueryResult> {
    let k_f = k.max(1) as f32;
    let mut merged: HashMap<DocKey, MergedHit> = HashMap::new();
    let mut best_rank: HashMap<DocKey, usize> = HashMap::new();
    for (kind, source) in arms {
        for (idx, r) in source.iter().enumerate() {
            let rank = idx + 1;
            let c = 1.0 / (k_f + rank as f32);
            let key = DocKey::of(r);
            let entry = merged.entry(key.clone()).or_insert_with(|| MergedHit {
                score: 0.0,
                template: r.clone(),
            });
            entry.score += c;
            if matches!(kind, ArmKind::Title) {
                entry.template.title_score = Some(r.score);
            }
            best_rank
                .entry(key)
                .and_modify(|b| *b = (*b).min(rank))
                .or_insert(rank);
        }
    }
    for (key, m) in merged.iter_mut() {
        match best_rank.get(key) {
            Some(1) => m.score += RRF_TOP1_BONUS,
            Some(2) | Some(3) => m.score += RRF_TOP3_BONUS,
            _ => {}
        }
    }
    finalise(merged)
}
```

Note the `or_insert_with` + `entry.score += c` restructure vs `rrf`'s `and_modify` — needed because the title stamp must apply on BOTH the insert and the merge path.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cargo test -p nexus-search-plugin --lib fusion 2>&1 | tail -5`
Expected: all fusion tests PASS (new + pre-existing).

- [ ] **Step 5: Commit**

```bash
git add rust/services/search-plugin/src/fusion.rs
git commit -m "feat(search-plugin): N-way rrf_multi with top-rank bonus + title attribution (#4628)"
```

---

### Task 3: `title_index.rs` — tokenizer + first-heading title extraction

**Files:**
- Create: `rust/services/search-plugin/src/title_index.rs`
- Modify: `rust/services/search-plugin/src/lib.rs` (add `pub mod title_index;` alongside the existing module list)

**Interfaces:**
- Produces: `pub fn tokenize(text: &str) -> Vec<String>` (port of Python `text_utils.tokenize_path`: split on `[/_-.\s]+`, camelCase boundaries, lowercase) and `pub fn extract_title(chunk0_text: &str) -> Option<String>` (first ATX heading in the first 2 KiB, YAML frontmatter skipped). Task 5 consumes both.

- [ ] **Step 1: Create the module with failing tests**

Create `rust/services/search-plugin/src/title_index.rs`:

```rust
//! Per-zone skeleton title index — the Rust mirror of Python's
//! deleted `SearchDaemon.locate()` BM25-lite (#4628, mirrors #4552 /
//! #4545 / #3725).
//!
//! The skeleton is DERIVED data: path tokens come from the FTS
//! `path` field, titles from the first ATX heading of each doc's
//! chunk-0 text — both already indexed, so no schema change and no
//! reindex (issue #4628 non-goal).  [`ZoneSkeleton`] (Task 5) builds
//! the in-memory index from a tantivy stored-doc scan and
//! [`crate::index_manager::IndexManager`] caches it per zone keyed
//! by the FTS searcher generation.
//!
//! Scoring matches Python exactly: title-token overlap × 2 +
//! path-token overlap × 1, DF-capped candidate selection, aggregate
//! budgets, deterministic (-score, path) ordering.

/// Bytes of chunk-0 text scanned for a title — Python
/// `SKELETON_HEAD_BYTES` parity (titles live at the top of a doc;
/// scanning further finds section headings, not titles).
pub const SKELETON_HEAD_BYTES: usize = 2048;

/// Tokenize a path or title into lowercase word tokens — port of
/// Python `text_utils.tokenize_path`.  Splits on `/ _ - .` and
/// whitespace, then on camelCase boundaries within each segment
/// (`parseUserAuth` → `parse user auth`, `HTMLParser` → `html
/// parser`).  Index-time and query-time both use this, so the two
/// sides always agree.
pub fn tokenize(text: &str) -> Vec<String> {
    let mut tokens = Vec::new();
    for part in
        text.split(|c: char| matches!(c, '/' | '_' | '-' | '.') || c.is_whitespace())
    {
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
}
```

- [ ] **Step 2: Register the module**

In `rust/services/search-plugin/src/lib.rs`, add `pub mod title_index;` in the module list (alphabetical placement with the existing `pub mod` block).

- [ ] **Step 3: Run tests to verify they pass**

Run: `cargo test -p nexus-search-plugin --lib title_index 2>&1 | tail -5`
Expected: PASS (this task's functions are written directly against the tests; run them together).

- [ ] **Step 4: Commit**

```bash
git add rust/services/search-plugin/src/title_index.rs rust/services/search-plugin/src/lib.rs
git commit -m "feat(search-plugin): skeleton tokenizer + first-heading title extraction (#4628)"
```

---

### Task 4: `FtsIndex` scan support — `generation_id` + `for_each_chunk0`

**Files:**
- Modify: `rust/services/search-plugin/src/fts_index.rs`

**Interfaces:**
- Produces: `pub fn generation_id(&self) -> u64` (tantivy searcher generation — bumps on every commit+reload, the skeleton cache key) and `pub fn for_each_chunk0<F: FnMut(&str, &str)>(&self, f: F) -> Result<u64, IndexError>` (visits every alive chunk-0 stored doc as `(path, chunk_text)`, returns the generation the scan observed). Tasks 5–6 consume both.

- [ ] **Step 1: Write the failing test** (append to `mod tests` in fts_index.rs)

```rust
    #[test]
    fn for_each_chunk0_scans_only_chunk_zero_and_generation_bumps() {
        let dir = tempdir().join("fts");
        let idx = FtsIndex::open_or_create(dir).expect("open");
        idx.add_document("/a.md", 0, "# Alpha\nbody a", Some(1)).expect("add");
        idx.add_document("/a.md", 1, "body a continued", Some(1)).expect("add");
        idx.add_document("/b.md", 0, "# Beta\nbody b", Some(2)).expect("add");
        idx.commit().expect("commit");
        let gen_before = idx.generation_id();

        let mut seen: Vec<(String, String)> = Vec::new();
        let scan_gen = idx
            .for_each_chunk0(|path, text| seen.push((path.to_string(), text.to_string())))
            .expect("scan");
        seen.sort();
        assert_eq!(scan_gen, gen_before, "scan reports the generation it observed");
        assert_eq!(
            seen,
            vec![
                ("/a.md".to_string(), "# Alpha\nbody a".to_string()),
                ("/b.md".to_string(), "# Beta\nbody b".to_string()),
            ],
            "chunk 1 must not appear"
        );

        // A commit (even after a delete+re-add) bumps the generation —
        // this is the skeleton cache's staleness signal.
        idx.delete_all_chunks("/b.md");
        idx.add_document("/b.md", 0, "# Beta2\nbody", Some(3)).expect("add");
        idx.commit().expect("commit");
        assert_ne!(idx.generation_id(), gen_before, "commit must change the generation");
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cargo test -p nexus-search-plugin --lib fts_index::tests::for_each_chunk0 2>&1 | tail -5`
Expected: FAIL to compile — methods not defined.

- [ ] **Step 3: Implement** (in `impl FtsIndex`, after `get_chunks_by_path`)

```rust
    /// Current searcher generation — bumps on every commit + reader
    /// reload.  The per-zone skeleton cache (#4628) keys on this so
    /// index mutations invalidate the skeleton automatically, with
    /// no invalidation call sites to keep in sync.
    pub fn generation_id(&self) -> u64 {
        self.reader.searcher().generation().generation_id()
    }

    /// Visit every alive chunk-0 stored doc as `(path, chunk_text)`.
    /// Full stored-doc scan — the skeleton build (#4628) is the only
    /// caller, and it runs at most once per index generation per
    /// zone, off the async runtime on the blocking pool.  Returns
    /// the generation of the searcher the scan used, so the caller
    /// can tag derived data with a scan-consistent version.
    pub fn for_each_chunk0<F: FnMut(&str, &str)>(&self, mut f: F) -> Result<u64, IndexError> {
        let searcher = self.reader.searcher();
        let gen = searcher.generation().generation_id();
        for seg in searcher.segment_readers() {
            let store = seg
                .get_store_reader(1)
                .map_err(|e| IndexError::Search(e.to_string()))?;
            for doc_id in seg.doc_ids_alive() {
                let stored: TantivyDocument = store
                    .get(doc_id)
                    .map_err(|e| IndexError::Search(e.to_string()))?;
                let hit = self.decode(stored, 0.0);
                if hit.chunk_index == 0 {
                    f(&hit.path, &hit.chunk_text);
                }
            }
        }
        Ok(gen)
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cargo test -p nexus-search-plugin --lib fts_index 2>&1 | tail -5`
Expected: PASS (new + pre-existing fts tests).

- [ ] **Step 5: Commit**

```bash
git add rust/services/search-plugin/src/fts_index.rs
git commit -m "feat(search-plugin): FTS generation id + chunk-0 stored-doc scan (#4628)"
```

---

### Task 5: `ZoneSkeleton` — build + `locate()` (the Python algorithm port)

**Files:**
- Modify: `rust/services/search-plugin/src/title_index.rs`

**Interfaces:**
- Consumes: `tokenize` / `extract_title` (Task 3), `FtsIndex::for_each_chunk0` (Task 4).
- Produces: `pub struct TitleHit { pub path: String, pub score: f32, pub title: Option<String> }`; `pub struct ZoneSkeleton` with `pub fn build(fts: &FtsIndex) -> Result<ZoneSkeleton, IndexError>`, `pub fn generation_id(&self) -> u64`, `pub fn doc_count(&self) -> usize`, `pub fn locate(&self, q: &str, limit: usize, path_prefix: Option<&str>) -> Vec<TitleHit>`; `pub const TITLE_ARM_MIN_SCORE: f32 = 2.0;` and the budget constants. Tasks 6–7 consume.

- [ ] **Step 1: Write the failing tests** (append to `mod tests` in title_index.rs)

```rust
    use crate::fts_index::FtsIndex;
    use std::path::PathBuf;

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
    fn locate_df_cap_drops_flooded_tokens_but_prefix_rescues() {
        // A token flooding > TITLE_ARM_MAX_TOKEN_DF paths is dropped
        // zone-wide; with a path_prefix the oversized bucket gets a
        // second chance via the in-prefix filter.  Build synthetic
        // skeleton directly (indexing 1025 tantivy docs in a unit
        // test is slow) — `insert_doc_for_test` is the test-only
        // constructor below.
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cargo test -p nexus-search-plugin --lib title_index 2>&1 | tail -5`
Expected: FAIL to compile — `ZoneSkeleton` / `TitleHit` / constants not defined.

- [ ] **Step 3: Implement `ZoneSkeleton`** (in title_index.rs, below `extract_title`)

```rust
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
    /// Build from a full chunk-0 stored-doc scan of the zone's FTS
    /// index.  Runs on the blocking pool (caller's job); cost is one
    /// store pass — ms-scale for typical zones, and paid at most
    /// once per index generation.
    pub fn build(fts: &FtsIndex) -> Result<Self, IndexError> {
        let mut docs: BTreeMap<String, SkeletonDoc> = BTreeMap::new();
        let generation_id = fts.for_each_chunk0(|path, chunk0| {
            let title = extract_title(chunk0);
            let title_tokens: BTreeSet<String> = title
                .as_deref()
                .map(|t| tokenize(t).into_iter().collect())
                .unwrap_or_default();
            let path_tokens: BTreeSet<String> = tokenize(path).into_iter().collect();
            docs.insert(
                path.to_string(),
                SkeletonDoc {
                    title,
                    title_tokens,
                    path_tokens,
                },
            );
        })?;
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

    pub fn generation_id(&self) -> u64 {
        self.generation_id
    }

    pub fn doc_count(&self) -> usize {
        self.docs.len()
    }

    /// BM25-lite path+title search — the verbatim port of Python
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
            SkeletonDoc { title, title_tokens, path_tokens },
        );
        *self = Self::from_docs(self.generation_id, docs);
    }
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cargo test -p nexus-search-plugin --lib title_index 2>&1 | tail -5`
Expected: all title_index tests PASS.

- [ ] **Step 5: Commit**

```bash
git add rust/services/search-plugin/src/title_index.rs
git commit -m "feat(search-plugin): ZoneSkeleton build + locate — Python locate() port (#4628)"
```

---

### Task 6: `IndexManager` generation-keyed skeleton cache

**Files:**
- Modify: `rust/services/search-plugin/src/index_manager.rs`

**Interfaces:**
- Consumes: `ZoneSkeleton::build` / `generation_id` (Task 5), `FtsIndex::generation_id` (Task 4).
- Produces: `pub fn get_or_build_skeleton(&self, zone_id: &str) -> Result<Arc<ZoneSkeleton>, IndexError>` on `IndexManager`. Task 7 consumes.

- [ ] **Step 1: Write the failing test** (append to index_manager.rs `mod tests`; follow the file's existing test-fixture style for constructing a manager with a temp root)

```rust
    #[test]
    fn skeleton_cache_rebuilds_on_generation_change() {
        let root = tempfile::tempdir().expect("tempdir").keep();
        let mgr = IndexManager::with_root(root);
        let fts = mgr.get_or_open("zoneA").expect("open");
        fts.add_document("/a.md", 0, "# Alpha\nbody", Some(1)).expect("add");
        fts.commit().expect("commit");

        let sk1 = mgr.get_or_build_skeleton("zoneA").expect("build");
        assert_eq!(sk1.doc_count(), 1);
        // Same generation → the SAME Arc comes back (cache hit).
        let sk1b = mgr.get_or_build_skeleton("zoneA").expect("cached");
        assert!(std::sync::Arc::ptr_eq(&sk1, &sk1b));

        // Mutate + commit → generation bump → rebuild with fresh docs.
        fts.add_document("/b.md", 0, "# Beta\nbody", Some(2)).expect("add");
        fts.commit().expect("commit");
        let sk2 = mgr.get_or_build_skeleton("zoneA").expect("rebuild");
        assert!(!std::sync::Arc::ptr_eq(&sk1, &sk2));
        assert_eq!(sk2.doc_count(), 2);

        // Zone isolation: an unrelated zone builds its own skeleton.
        let sk_other = mgr.get_or_build_skeleton("zoneB").expect("other");
        assert_eq!(sk_other.doc_count(), 0);
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cargo test -p nexus-search-plugin --lib index_manager 2>&1 | tail -5`
Expected: FAIL to compile — `get_or_build_skeleton` not defined.

- [ ] **Step 3: Implement**

Add the field to `IndexManager` (with the existing `zones` / `ann_zones` fields):

```rust
    /// Per-zone skeleton snapshots for the title arm (#4628), keyed
    /// implicitly by each snapshot's FTS generation — see
    /// [`Self::get_or_build_skeleton`].
    skeletons: Mutex<HashMap<String, Arc<crate::title_index::ZoneSkeleton>>>,
```

Initialise `skeletons: Mutex::new(HashMap::new()),` in `with_root` (and `new` if it constructs fields directly rather than delegating).

Add the method to `impl IndexManager` (near `get_or_open`):

```rust
    /// Get the zone's skeleton snapshot, rebuilding when the FTS
    /// index has committed since the cached build.  Generation-keyed
    /// (not call-site invalidated): every Index / Refresh /
    /// IndexDocuments / NotifyFileChange mutation ends in an FTS
    /// commit, which bumps the searcher generation — so staleness
    /// detection is structural and new mutation paths can't forget
    /// to invalidate.  The build runs OUTSIDE the cache lock; two
    /// racing builders duplicate work and last-write-wins, which is
    /// harmless (both snapshots are internally consistent).
    pub fn get_or_build_skeleton(
        &self,
        zone_id: &str,
    ) -> Result<Arc<crate::title_index::ZoneSkeleton>, IndexError> {
        let fts = self.get_or_open(zone_id)?;
        let current_gen = fts.generation_id();
        {
            let cache = self.skeletons.lock();
            if let Some(sk) = cache.get(zone_id) {
                if sk.generation_id() == current_gen {
                    return Ok(Arc::clone(sk));
                }
            }
        }
        let built = Arc::new(crate::title_index::ZoneSkeleton::build(&fts)?);
        self.skeletons
            .lock()
            .insert(zone_id.to_string(), Arc::clone(&built));
        Ok(built)
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cargo test -p nexus-search-plugin --lib index_manager 2>&1 | tail -5`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add rust/services/search-plugin/src/index_manager.rs
git commit -m "feat(search-plugin): generation-keyed per-zone skeleton cache (#4628)"
```

---

### Task 7: Service wiring — knob, parallel title task, hydration, kw-lane sub-fusion

**Files:**
- Modify: `rust/services/search-plugin/src/service.rs`

**Interfaces:**
- Consumes: `rrf_multi` / `ArmKind` (Task 2), `TitleHit` / `TITLE_ARM_MIN_SCORE` (Task 5), `get_or_build_skeleton` (Task 6). Defines the service-local `TITLE_ARM_MAX_HYDRATION_FETCH = 32` const.
- Produces: `SearchServiceBuilder::title_arm(bool)` (test override; env `NEXUS_SEARCH_TITLE_ARM` is the operator knob); hybrid results carry `title_score` when the arm voted. Consumed by Task 8's e2e and Task 9's shim.

- [ ] **Step 1: Add the knob plumbing**

Near `BATCH_QUERY_CONCURRENCY_ENV` (service.rs top):

```rust
/// Kill-switch for the hybrid title arm (#4628; mirrors Python's
/// NEXUS_SEARCH_TITLE_ARM, default ON).  Read per-query so an
/// operator can flip it without a restart; "false" / "0" / "no"
/// (trimmed, case-insensitive) disable.
const TITLE_ARM_ENV: &str = "NEXUS_SEARCH_TITLE_ARM";

fn title_arm_env_enabled() -> bool {
    match std::env::var(TITLE_ARM_ENV) {
        Ok(v) => !matches!(v.trim().to_ascii_lowercase().as_str(), "false" | "0" | "no"),
        Err(_) => true,
    }
}
```

Add to `SearchServiceImpl` struct: `title_arm: Option<bool>,` with doc comment `/// Builder override for the title arm — None ⇒ read TITLE_ARM_ENV per query (production); Some(_) pins it (tests).`

Add to `impl SearchServiceImpl`:

```rust
    fn title_arm_enabled(&self) -> bool {
        self.title_arm.unwrap_or_else(title_arm_env_enabled)
    }
```

Add to `SearchServiceBuilder`: field `title_arm: Option<bool>,` (init `None` in `builder()`), setter

```rust
    /// Pin the title arm on/off, bypassing NEXUS_SEARCH_TITLE_ARM —
    /// for tests that must not race the process environment.
    pub fn title_arm(mut self, enabled: bool) -> Self {
        self.title_arm = Some(enabled);
        self
    }
```

and thread it through `build()` into the struct literal.

- [ ] **Step 2: Add the locate + hydration helpers** (near `fuse_hybrid`)

```rust
/// Cap on per-query representative-chunk fetches for uncovered
/// title hits (Python TITLE_ARM_MAX_HYDRATION_FETCH).  Hits beyond
/// it degrade to chunk_text="" instead of unbounded FTS lookups.
const TITLE_ARM_MAX_HYDRATION_FETCH: usize = 32;

/// Run the title arm's locate: skeleton lookup (building it on
/// first use per index generation) + evidence gate.  Fail-soft —
/// a skeleton build error degrades to an empty arm with a debug
/// log; the search itself must never fail on the arm's account.
fn do_title_locate(
    manager: &IndexManager,
    q: &str,
    zone_id: &str,
    limit: usize,
    path_filter: &str,
) -> Vec<crate::title_index::TitleHit> {
    match manager.get_or_build_skeleton(zone_id) {
        Ok(skeleton) => {
            let prefix = (!path_filter.is_empty()).then_some(path_filter);
            let mut hits = skeleton.locate(q, limit, prefix);
            // Evidence gate: a lone incidental path-token overlap
            // (score 1.0) must not earn rank-based RRF votes;
            // require at least one real title-token match (2.0).
            hits.retain(|h| h.score >= crate::title_index::TITLE_ARM_MIN_SCORE);
            hits
        }
        Err(e) => {
            tracing::debug!(err = %e, zone = %zone_id, "title arm: skeleton unavailable — degrading");
            Vec::new()
        }
    }
}

/// Hydrate locate() hits to the QueryResult shape for fusion.  The
/// fusion identity is (path, chunk_index), so each hit needs a
/// representative chunk: borrow the best-scored keyword-leg row for
/// the path (aligns the key so RRF votes accumulate on one fused
/// entry instead of splitting), dense rows as the lowest-priority
/// borrow source, then a capped FTS chunk-0 fetch for still-
/// uncovered paths.  A doc with no FTS row (drift) stays
/// retrievable with empty text.  `score` = the locate score —
/// rrf_multi turns it into rank votes and stamps it as
/// title_score attribution.
fn hydrate_title_hits(
    fts: Option<&crate::fts_index::FtsIndex>,
    title_hits: &[crate::title_index::TitleHit],
    keyword: &[QueryResult],
    semantic: &[QueryResult],
    zone_id: &str,
) -> Vec<QueryResult> {
    let mut best_by_path: std::collections::HashMap<&str, &QueryResult> =
        std::collections::HashMap::new();
    for r in keyword {
        match best_by_path.get(r.path.as_str()) {
            Some(cur) if cur.score >= r.score => {}
            _ => {
                best_by_path.insert(&r.path, r);
            }
        }
    }
    let mut best_dense: std::collections::HashMap<&str, &QueryResult> =
        std::collections::HashMap::new();
    for r in semantic {
        match best_dense.get(r.path.as_str()) {
            Some(cur) if cur.score >= r.score => {}
            _ => {
                best_dense.insert(&r.path, r);
            }
        }
    }
    for (path, r) in best_dense {
        best_by_path.entry(path).or_insert(r);
    }

    let mut fetched: std::collections::HashMap<&str, crate::fts_index::FtsHit> =
        std::collections::HashMap::new();
    if let Some(fts) = fts {
        let uncovered: Vec<&str> = title_hits
            .iter()
            .map(|h| h.path.as_str())
            .filter(|p| !best_by_path.contains_key(*p))
            .take(TITLE_ARM_MAX_HYDRATION_FETCH)
            .collect();
        for path in uncovered {
            match fts.get_chunks_by_path(path) {
                // Sorted by chunk_index — first entry is chunk 0.
                Ok(chunks) => {
                    if let Some(c0) = chunks.into_iter().next() {
                        fetched.insert(path, c0);
                    }
                }
                Err(e) => tracing::debug!(
                    err = %e, path = %path,
                    "title arm: representative-chunk fetch failed — empty text",
                ),
            }
        }
    }

    title_hits
        .iter()
        .map(|h| {
            let (chunk_index, chunk_text, mtime_ms) =
                if let Some(leg) = best_by_path.get(h.path.as_str()) {
                    (leg.chunk_index, leg.chunk_text.clone(), leg.mtime_ms)
                } else if let Some(row) = fetched.get(h.path.as_str()) {
                    (row.chunk_index, row.chunk_text.clone(), row.mtime_ms)
                } else {
                    (0, String::new(), None)
                };
            QueryResult {
                path: h.path.clone(),
                chunk_index,
                chunk_text,
                score: h.score,
                zone_id: zone_id.to_string(),
                mtime_ms,
                expanded_context: String::new(),
                // Stamped by rrf_multi per arm vote, not here — so
                // merged chunk-arm entries get it too.
                title_score: None,
            }
        })
        .collect()
}
```

- [ ] **Step 3: Wire the Hybrid branch**

In `async fn query`, `QueryType::Hybrid` arm — replace the two-task block and the `(kw, sem)` match with a three-task version. Current code spawns `(kw_task, sem_task)` and matches `(Ok(keyword), Ok(semantic)) => Ok(fuse_hybrid(keyword, semantic, fetch_limit, fusion_opts))`. New code:

```rust
                // Title arm (#4628): a third parallel leg over the
                // in-memory skeleton.  Independent of kw/sem, so it
                // joins the same spawn_blocking fan-out.  Runs only
                // when enabled — a disabled arm costs nothing (no
                // skeleton is ever built).
                let title_arm_on = self.title_arm_enabled();
                let (kw_task, sem_task, title_task) = {
                    let mgr_kw = Arc::clone(&manager);
                    let mgr_sem = Arc::clone(&manager);
                    let mgr_title = Arc::clone(&manager);
                    let embedder = Arc::clone(&embedder);
                    let embed_cache = Arc::clone(&self.embed_cache);
                    let q_kw = q.clone();
                    let q_title = q.clone();
                    let q_sem = q;
                    let zone_kw = zone_id.clone();
                    let zone_title = zone_id.clone();
                    let zone_sem = zone_id;
                    let path_kw = path_filter.clone();
                    let path_title = path_filter.clone();
                    let path_sem = path_filter;
                    (
                        tokio::task::spawn_blocking(move || {
                            do_keyword_query(&mgr_kw, &q_kw, &zone_kw, over_fetch, &path_kw)
                        }),
                        tokio::task::spawn_blocking(move || {
                            do_semantic_query(
                                &mgr_sem,
                                &embedder,
                                &embed_cache,
                                &q_sem,
                                &zone_sem,
                                over_fetch,
                                &path_sem,
                            )
                        }),
                        tokio::task::spawn_blocking(move || {
                            if !title_arm_on {
                                return Vec::new();
                            }
                            // limit * 2 mirrors Python's locate
                            // over-fetch (fusion headroom).
                            do_title_locate(
                                &mgr_title,
                                &q_title,
                                &zone_title,
                                limit.saturating_mul(2),
                                &path_title,
                            )
                        }),
                    )
                };
                let (kw_join, sem_join, title_join) = tokio::join!(kw_task, sem_task, title_task);
                let kw = kw_join
                    .map_err(|e| Status::internal(format!("spawn_blocking joined error: {e}")))?;
                let sem = sem_join
                    .map_err(|e| Status::internal(format!("spawn_blocking joined error: {e}")))?;
                // A panicked title task degrades to an empty arm —
                // the arm is additive evidence, never a failure
                // source (fail-soft parity with Python).
                let title_hits = title_join.unwrap_or_default();
                match (kw, sem) {
                    (Ok(keyword), Ok(semantic)) => {
                        // Keyword-lane sub-fusion (#4628).  Empty
                        // title arm ⇒ the lane passes through
                        // UNCHANGED — non-title queries stay byte-
                        // identical to the pre-title-arm plugin
                        // under every fusion method (the WEIGHTED
                        // method normalises raw scores, so even a
                        // score-preserving no-op re-fusion would
                        // shift its blend).
                        let kw_lane = if title_hits.is_empty() {
                            keyword
                        } else {
                            let fts = manager_for_expand.get_or_open(&zone_for_expand).ok();
                            let hydrated = hydrate_title_hits(
                                fts.as_deref(),
                                &title_hits,
                                &keyword,
                                &semantic,
                                &zone_for_expand,
                            );
                            fusion::rrf_multi(
                                &[
                                    (fusion::ArmKind::Chunk, keyword.as_slice()),
                                    (fusion::ArmKind::Title, hydrated.as_slice()),
                                ],
                                fusion_opts.rrf_k,
                            )
                        };
                        Ok(fuse_hybrid(kw_lane, semantic, fetch_limit, fusion_opts))
                    }
                    // A source-side error on either leg — surface it
                    // as the response's `error` field rather than a
                    // gRPC Status, matching keyword / semantic
                    // behaviour.  If both fail we surface the
                    // keyword error (arbitrary but stable).
                    (Err(e), _) | (Ok(_), Err(e)) => Err(e),
                }
```

Notes for the implementer:
- `manager_for_expand` / `zone_for_expand` already exist in scope (cloned before the match for the expand path) and are NOT moved until the post-outcome expand block — reusing them here is safe because this arm runs before that block. If the borrow checker objects (they're used again later), add dedicated `manager_for_titles` / `zone_for_titles` clones next to them instead.
- `limit` (the caller-visible limit) is `Copy` (`usize`) — usable inside the closure without cloning.

- [ ] **Step 4: Compile + run the full crate test suite**

Run: `cargo test -p nexus-search-plugin 2>&1 | tail -15`
Expected: everything PASSES — existing hybrid tests are unaffected because their fixtures have no headings/title matches (empty arm ⇒ pass-through) or run keyword/semantic modes. Any hybrid e2e that indexes markdown WITH headings matching its query may now see `title_score` set — if one fails on an exact-equality assert, inspect: the ranking must be unchanged or better; update only assertions about the new field, never weaken ranking asserts without understanding why.

- [ ] **Step 5: Commit**

```bash
git add rust/services/search-plugin/src/service.rs
git commit -m "feat(search-plugin): wire title arm into hybrid keyword lane (#4628)"
```

---

### Task 8: E2E — `title_arm_e2e.rs`

**Files:**
- Create: `rust/services/search-plugin/tests/title_arm_e2e.rs` (harness cloned from `tests/semantic_query_e2e.rs` — same MockKernel + MockEmbedder scaffold; the file-level comment there explicitly blesses duplication over a shared `tests/common`)

**Interfaces:**
- Consumes: `SearchServiceBuilder::title_arm(bool)` (Task 7), `MockEmbedder`, Query RPC with `QueryType::Hybrid`.

- [ ] **Step 1: Create the test file**

Copy the MockKernel scaffold (everything from the `FileEntry` struct through `handle_for`) from `tests/semantic_query_e2e.rs` verbatim, then add:

```rust
//! End-to-end tests for the hybrid title arm (#4628).
//!
//! The acceptance workload mirrors the Python #4552 plan doc: a doc
//! whose TITLE matches the query but whose body is weak must enter
//! the hybrid top-N with title_score attribution; with the arm off
//! (builder pin) it must not benefit; a query with no title match
//! must return byte-identical results with the arm on and off.

// ... MockKernel scaffold here (copied) ...

struct Harness {
    _dir: TempDir,
    mock: *mut MockKernel,
    svc: SearchServiceImpl,
}

impl Harness {
    fn start(title_arm: bool) -> Self {
        let dir = TempDir::new().expect("tempdir");
        let mock = Box::into_raw(Box::new(MockKernel::new()));
        let handle = handle_for(mock);
        let manager = Arc::new(IndexManager::with_root(dir.path().to_path_buf()));
        let embedder = Arc::new(MockEmbedder::with_dim(16));
        let svc = SearchServiceImpl::builder(Arc::new(handle))
            .manager(manager)
            .embedder(embedder)
            .title_arm(title_arm)
            .build();
        Self { _dir: dir, mock, svc }
    }

    fn mock_mut(&self) -> &mut MockKernel {
        unsafe { &mut *self.mock }
    }
}

impl Drop for Harness {
    fn drop(&mut self) {
        unsafe { drop(Box::from_raw(self.mock)) }
    }
}

/// The acceptance corpus: /designs/atlas.md is titled "Atlas Design
/// Doc" but its body never mentions "design" or "doc"; the decoys
/// repeat the query words in their bodies so chunk-BM25 favours
/// them.  Pre-title-arm, "atlas design doc" buried the target.
fn seed_corpus(mock: &mut MockKernel) {
    mock.add_dir("/");
    mock.add_dir("/designs");
    mock.add_dir("/notes");
    mock.add_file(
        "/designs/atlas.md",
        b"# Atlas Design Doc\n\nservice topology overview and rollout phases.",
        1,
    );
    mock.add_file(
        "/notes/scratch1.md",
        b"# Scratch\n\ndesign doc design doc design doc atlas mention here.",
        2,
    );
    mock.add_file(
        "/notes/scratch2.md",
        b"# Scratch Two\n\nanother design doc draft, atlas atlas design notes.",
        3,
    );
}

async fn index_root(svc: &SearchServiceImpl) {
    let resp = svc
        .index(Request::new(IndexRequest {
            root_path: "/".into(),
            zone_id: "root".into(),
            recursive: true,
            max_docs: 0,
            auth_token: String::new(),
        }))
        .await
        .expect("index")
        .into_inner();
    assert!(resp.error.is_none(), "index failed: {:?}", resp.error);
}

async fn hybrid_query(
    svc: &SearchServiceImpl,
    q: &str,
) -> Vec<nexus_search_plugin::search_proto::QueryResult> {
    let resp = svc
        .query(Request::new(QueryRequest {
            q: q.into(),
            zone_id: "root".into(),
            limit: 10,
            query_type: QueryType::Hybrid as i32,
            ..Default::default()
        }))
        .await
        .expect("query")
        .into_inner();
    assert!(resp.error.is_none(), "query failed: {:?}", resp.error);
    resp.results
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn title_shaped_query_surfaces_weak_body_doc_with_attribution() {
    let h = Harness::start(true);
    seed_corpus(h.mock_mut());
    index_root(&h.svc).await;

    let results = hybrid_query(&h.svc, "atlas design doc").await;
    let atlas = results
        .iter()
        .find(|r| r.path == "/designs/atlas.md")
        .expect("title-matched doc must enter the hybrid results");
    // locate score: 3 title-token overlaps × 2.0 + 1 path-token
    // overlap ("atlas") = 7.0 — the same pin the Python acceptance
    // test used.
    assert_eq!(atlas.title_score, Some(7.0));
    // Hydration: the doc has an FTS chunk-0 row, so text is real.
    assert!(!atlas.chunk_text.is_empty(), "hydrated chunk text expected");
    // The decoys must not carry title attribution ("Scratch" /
    // "Scratch Two" share no title token with the query).
    for r in results.iter().filter(|r| r.path != "/designs/atlas.md") {
        assert_eq!(r.title_score, None, "unexpected attribution on {}", r.path);
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn arm_off_no_attribution_and_no_title_benefit() {
    let on = Harness::start(true);
    seed_corpus(on.mock_mut());
    index_root(&on.svc).await;
    let off = Harness::start(false);
    seed_corpus(off.mock_mut());
    index_root(&off.svc).await;

    let res_on = hybrid_query(&on.svc, "atlas design doc").await;
    let res_off = hybrid_query(&off.svc, "atlas design doc").await;

    assert!(res_off.iter().all(|r| r.title_score.is_none()));
    let rank = |rs: &[nexus_search_plugin::search_proto::QueryResult]| {
        rs.iter().position(|r| r.path == "/designs/atlas.md")
    };
    let rank_on = rank(&res_on).expect("arm on: target present");
    // MockEmbedder is deterministic, so this comparison is stable:
    // with the arm off the target either drops out of the list or
    // ranks strictly worse.
    match rank(&res_off) {
        None => {}
        Some(rank_off) => assert!(
            rank_on < rank_off,
            "title arm must strictly improve the target's rank (on={rank_on}, off={rank_off})"
        ),
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn non_title_query_is_byte_identical_with_arm_on_and_off() {
    // Locate has no hits for this query (no title/path token overlap
    // ≥ MIN_SCORE) → the keyword lane passes through untouched and
    // the responses must match EXACTLY — the pass-through parity
    // guarantee that keeps the arm invisible off its target.
    let on = Harness::start(true);
    seed_corpus(on.mock_mut());
    index_root(&on.svc).await;
    let off = Harness::start(false);
    seed_corpus(off.mock_mut());
    index_root(&off.svc).await;

    let res_on = hybrid_query(&on.svc, "rollout topology overview").await;
    let res_off = hybrid_query(&off.svc, "rollout topology overview").await;
    assert_eq!(res_on, res_off);
}
```

Import list mirrors `semantic_query_e2e.rs` (`IndexRequest`, `QueryRequest`, `QueryType`, `SearchService` trait, `SearchServiceImpl`, `IndexManager`, `MockEmbedder`, `TempDir`, `tonic::Request`).

Fixture caveat for the parity test: "rollout topology overview" must not reach `TITLE_ARM_MIN_SCORE` against any doc — "overview" appears in atlas's BODY (not title/path) and "rollout"/"topology" likewise; no title or path token matches ⇒ locate yields nothing ≥ 2.0. If the assert fails because locate DOES fire, pick body-only words that appear in no title and no path.

- [ ] **Step 2: Run the e2e suite**

Run: `cargo test -p nexus-search-plugin --test title_arm_e2e 2>&1 | tail -10`
Expected: 3 tests PASS.

- [ ] **Step 3: Commit**

```bash
git add rust/services/search-plugin/tests/title_arm_e2e.rs
git commit -m "test(search-plugin): title-arm e2e — acceptance, kill-switch, parity (#4628)"
```

---

### Task 9: Python shim passthrough + stub regeneration

**Files:**
- Modify: `src/nexus/bricks/search/daemon.py` (`_result_to_base`, ~line 393)
- Regenerate: `src/nexus/grpc/search/v1/search_pb2.py`, `search_pb2.pyi`, `search_pb2_grpc.py`
- Test: `tests/unit/bricks/search/test_daemon_transport.py`

The downstream Python surfaces (BaseSearchResult.title_score, HTTP serializer, federated passthrough, search_service omit-when-None) all survived P12 — the ONLY missing link is the proto→BaseSearchResult copy in the shim.

- [ ] **Step 1: Write the failing test** (append to `test_daemon_transport.py`, following its existing style for constructing `search_pb2.QueryResult` fixtures — reuse its import of `search_pb2` and `_result_to_base` or import them the same way the module's existing tests do)

```python
def test_result_to_base_copies_title_score_when_set() -> None:
    """#4628: title-arm attribution must survive the proto→Base hop —
    the HTTP/batch/federated serializers all read
    BaseSearchResult.title_score and omit-when-None."""
    from nexus.bricks.search.daemon import _result_to_base
    from nexus.grpc.search.v1 import search_pb2

    pb = search_pb2.QueryResult(
        path="/designs/atlas.md",
        chunk_index=0,
        chunk_text="body",
        score=0.9,
        zone_id="root",
        title_score=7.0,
    )
    base = _result_to_base(pb)
    assert base.title_score == pytest.approx(7.0)


def test_result_to_base_title_score_none_when_unset() -> None:
    from nexus.bricks.search.daemon import _result_to_base
    from nexus.grpc.search.v1 import search_pb2

    pb = search_pb2.QueryResult(path="/a.md", chunk_index=0, chunk_text="t", score=0.5)
    base = _result_to_base(pb)
    assert base.title_score is None
```

- [ ] **Step 2: Run to verify failure mode**

Run: `PYTHONPATH=$PWD/src /Users/tafeng/nexus/.venv/bin/python -m pytest tests/unit/bricks/search/test_daemon_transport.py -v -k title_score 2>&1 | tail -5`
Expected: FAIL — `ValueError: Protocol message QueryResult has no "title_score" field` (stubs not yet regenerated).

- [ ] **Step 3: Regenerate the Python stubs**

```bash
TMP=$(mktemp -d)
/Users/tafeng/nexus/.venv/bin/python -m grpc_tools.protoc \
  -I rust/services/proto \
  --python_out="$TMP" --pyi_out="$TMP" --grpc_python_out="$TMP" \
  nexus/search/v1/search.proto
cp "$TMP"/nexus/search/v1/search_pb2.py \
   "$TMP"/nexus/search/v1/search_pb2.pyi \
   "$TMP"/nexus/search/v1/search_pb2_grpc.py \
   src/nexus/grpc/search/v1/
rm -rf "$TMP"
```

Then restore the relative import in `src/nexus/grpc/search/v1/search_pb2_grpc.py`: the generated line `import nexus.search.v1.search_pb2 as nexus_dot_search_dot_v1_dot_search__pb2` (or `from nexus.search.v1 import ...`) must read `from . import search_pb2 as nexus_dot_search_dot_v1_dot_search__pb2` — match the current file's line 6 exactly. Verify with `git diff src/nexus/grpc/search/v1/search_pb2_grpc.py` that ONLY generated-content changes and the import line stays relative.

- [ ] **Step 4: Add the shim passthrough**

In `_result_to_base` (daemon.py ~L398), extend the constructor call:

```python
    return BaseSearchResult(
        path=pb.path,
        chunk_text=pb.chunk_text,
        score=float(pb.score),
        chunk_index=int(pb.chunk_index),
        zone_id=pb.zone_id or None,
        macro_text=pb.expanded_context or None,
        # #4628: title-arm attribution — optional proto field, so
        # presence (not zero-ness) decides None.
        title_score=float(pb.title_score) if pb.HasField("title_score") else None,
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `PYTHONPATH=$PWD/src /Users/tafeng/nexus/.venv/bin/python -m pytest tests/unit/bricks/search/test_daemon_transport.py -v 2>&1 | tail -5`
Expected: PASS (new + all pre-existing transport tests — the regenerated stubs must not break request mapping).

- [ ] **Step 6: Commit**

```bash
git add src/nexus/bricks/search/daemon.py src/nexus/grpc/search/v1/
git commit -m "feat(search): pass title_score through the Rust-daemon shim (#4628)"
```

---

### Task 10: Full verification + PR

- [ ] **Step 1: Rust suite, clippy, fmt**

```bash
cargo test -p nexus-search-plugin 2>&1 | tail -5
cargo clippy -p nexus-search-plugin --all-targets -- -D warnings 2>&1 | tail -5
cargo fmt -p nexus-search-plugin && git diff --stat
```
Expected: all tests pass; clippy clean (fix any new-code lints; PRE-EXISTING lints in untouched code are out of scope — don't chase them); fmt produces no diff (or commit the formatting).

- [ ] **Step 2: Python search unit suites that touch the changed surfaces**

```bash
PYTHONPATH=$PWD/src /Users/tafeng/nexus/.venv/bin/python -m pytest \
  tests/unit/bricks/search/test_daemon_transport.py \
  tests/unit/bricks/search/test_search_result_serialize.py -v 2>&1 | tail -5
```
Expected: PASS.

- [ ] **Step 3: Push and open the PR**

```bash
git push -u origin feat/4628-title-arm-rust-plugin
gh pr create --repo nexi-lab/nexus --base develop \
  --title "feat(search-plugin): mirror title-arm hybrid fusion into the Rust plugin (#4628)" \
  --body "$(cat <<'EOF'
Closes #4628 — the last unmirrored windoliver search feature (Python #4552).

## What
- Per-zone in-memory skeleton (path tokens + first-heading title) derived from already-indexed FTS data — no schema change, no reindex.
- `ZoneSkeleton::locate()`: verbatim port of the deleted Python `SearchDaemon.locate()` (title×2 + path×1 scoring, DF caps, candidate budgets, deterministic ordering, stopword + exact-title handling).
- Generation-keyed skeleton cache on `IndexManager` — FTS commits bump the tantivy searcher generation, so staleness detection is structural (no invalidation call sites).
- Hybrid keyword lane: `fusion::rrf_multi([chunk, title])` sub-fusion with Python's top-rank bonus, BEFORE the untouched alpha/fusion final stage. Empty title arm ⇒ byte-identical pass-through (pinned by e2e).
- `QueryResult.title_score` attribution (optional proto field) + Python shim passthrough; HTTP/batch/federated serializers already handle it.
- Kill-switch: `NEXUS_SEARCH_TITLE_ARM` (default on), builder pin for tests.

## Testing
- Unit: tokenizer/extract-title, locate scoring + budgets, rrf_multi formula + attribution, skeleton cache generation semantics.
- E2E (`title_arm_e2e.rs`): title-shaped query surfaces a weak-body doc with `title_score=7.0`; arm-off shows no attribution and strictly worse target rank; non-title query byte-identical on/off.
EOF
)"
```

---

## Self-Review Notes (issue → task mapping)

| Issue #4628 ask | Covered by |
|---|---|
| Title-arm run over per-zone skeleton (path segments + first-heading) | Tasks 3–6 |
| Ranked list from existing indexed data, no reindex | Task 4 scan + Task 5 build (derived only) |
| Extend keyword-lane sub-fusion via rrf_multi before final alpha/fusion stage | Tasks 2 + 7 (`fuse_hybrid` untouched) |
| Gate on env knob mirroring `NEXUS_SEARCH_TITLE_ARM` default-on | Task 7 (env + builder pin) |
| Per-hit `title_score` attribution | Tasks 1, 2, 7, 9 |
| E2E: title-shaped query surfaces intended file with weak body | Task 8 (workload shape from the Python plan doc, score pin 7.0) |
| Non-goal: reindexing / cross-zone | honored — no schema change; single-zone skeleton |

Known deltas vs Python, accepted and documented in code:
1. No page arm (Rust mirrors the SQLite-branch shape; `rrf_multi` is N-way ready). Rationale in Global Constraints.
2. Title extraction = ATX-heading-only from chunk-0 head (Python had a per-extension extractor registry living in the catalog brick; the plugin's corpus is text/markdown-chunked). Path tokens still index every doc.
3. Skeleton snapshot semantics: rebuilt per FTS generation instead of Python's journaled live mutation — queries between commits see the previous consistent snapshot, matching the plugin's existing query-cache staleness posture.
4. Sub-fusion is skipped entirely when the title arm is empty (Python always sub-fused chunk+page). Python's kw lane was already RRF-shaped pre-#4552, so an empty arm changed nothing there; in Rust the lane is raw BM25, and unconditional re-fusion would perturb WEIGHTED-method blends. Pass-through preserves exact parity — pinned by the Task 8 parity e2e.
