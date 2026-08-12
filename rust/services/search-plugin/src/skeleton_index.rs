//! Per-zone tantivy skeleton index — BM25 over path tokens + title,
//! with title weighted 2× at query time (Rust mirror of the Python
//! `_skeleton_docs` + `SearchDaemon.locate()` pair — #4552).
//!
//! # Why a separate index
//!
//! Titles are not searchable text in [`crate::fts_index`] because the
//! FTS schema stores CHUNK text, not document metadata.  A query
//! naming a document by its title only ("Q3 design doc",
//! "<project> spec") under-ranks its target when the title text does
//! not recur in body chunks.  The skeleton index gives titles their
//! own BM25 field so the hybrid fusion pipeline can enter a third
//! keyword-side arm ranking title matches on their own merits, then
//! RRF-fuse that with the chunk-BM25 and semantic-vector arms.
//!
//! # Storage layout
//!
//! Same pattern as [`crate::fts_index`]:
//! `~/.nexus/plugins/search/<zone_id>/skeleton-v1/`.  Plugin-owned,
//! per-node, NOT replicated — the SSOT is the kernel VFS and the
//! sibling FtsIndex/AnnIndex which hold the raw doc content.
//! Dropping the directory is always safe: the next Index or Refresh
//! rebuilds it from the same doc bytes the FTS index sees.
//!
//! # Schema
//!
//! ```text
//! path         STRING | STORED       exact match + retrievable (dedup key)
//! path_tokens  TEXT(en_stem)         BM25-analysed, English-stemmed
//! title        TEXT(en_stem) | STORED  BM25-analysed, English-stemmed
//! ```
//!
//! Both text fields use the same `en_stem` chain as FtsIndex so
//! morphological query variants match consistently across the three
//! keyword arms (chunk / page / title) — a query "authorization"
//! matches a title "authorized guide" the same way it matches a
//! body chunk containing "authorized".  Title-2× is applied at
//! QUERY time (per-field boost), keeping the schema symmetric with
//! FtsIndex's single-analyser convention.
//!
//! # Idempotency
//!
//! One document per `path`.  [`upsert`](SkeletonIndex::upsert) deletes
//! any prior doc under the same path before adding the new one, so
//! the writer's uncommitted transaction is (drop-all, add-new) —
//! commit lands atomically, readers never see a partial state.
//!
//! # Concurrency
//!
//! Same shape as FtsIndex — one writer per index behind a
//! `parking_lot::Mutex`; the reader is `Send + Sync` and searches
//! take no locks.  The mutex is not held across `.await`.

use std::path::PathBuf;
use std::sync::Arc;

use parking_lot::Mutex;
use tantivy::collector::TopDocs;
use tantivy::directory::MmapDirectory;
use tantivy::query::{BooleanQuery, Occur, Query, QueryParser};
use tantivy::query::{BoostQuery, TermQuery};
use tantivy::schema::{
    Field, IndexRecordOption, Schema, TextFieldIndexing, TextOptions, Value, STORED, STRING,
};
use tantivy::{doc, Index, IndexReader, IndexWriter, ReloadPolicy, TantivyDocument, Term};

use crate::fts_index::IndexError;

/// Same 50 MiB budget FtsIndex uses.  The skeleton index has far
/// fewer documents (one per file, not per chunk), so this is
/// generous — kept identical to make sizing predictable.
const WRITER_HEAP_BYTES: usize = 50 * 1024 * 1024;

/// Weight applied to the title field at query time — Python's
/// `SearchDaemon.locate()` weights title 2× vs path tokens; mirror
/// exactly so cross-cutover ranking stays comparable.
const TITLE_BOOST: f32 = 2.0;

/// Result row returned by [`SkeletonIndex::search`].
#[derive(Debug, Clone)]
pub struct SkeletonHit {
    pub path: String,
    pub title: String,
    pub score: f32,
}

/// Skeleton schema field handles.
#[derive(Debug, Clone)]
struct Fields {
    path: Field,
    path_tokens: Field,
    title: Field,
}

impl Fields {
    fn from_schema(schema: &Schema) -> Self {
        Self {
            path: schema.get_field("path").expect("schema field: path"),
            path_tokens: schema
                .get_field("path_tokens")
                .expect("schema field: path_tokens"),
            title: schema.get_field("title").expect("schema field: title"),
        }
    }
}

/// Build the skeleton schema.  Broken out so tests can construct a
/// schema without opening a directory.
pub fn build_schema() -> Schema {
    let mut sb = Schema::builder();
    // path — exact-match STRING, primary dedupe key.  Also stored so
    // hits can round-trip the retrievable path without a sibling
    // lookup.
    sb.add_text_field("path", STRING | STORED);
    // path_tokens — path split into whitespace-separated segments and
    // BM25-analysed through `en_stem`.  Callers pass the tokenized
    // form; the schema keeps the transform explicit at insert time so
    // storage cost is minimal.
    let text_opts = TextOptions::default().set_indexing_options(
        TextFieldIndexing::default()
            .set_tokenizer("en_stem")
            .set_index_option(IndexRecordOption::WithFreqsAndPositions),
    );
    sb.add_text_field("path_tokens", text_opts.clone());
    // title — BM25-analysed AND stored so hits carry the display
    // string.  Applies the query-time boost via [`TITLE_BOOST`].
    let title_opts = TextOptions::default()
        .set_indexing_options(
            TextFieldIndexing::default()
                .set_tokenizer("en_stem")
                .set_index_option(IndexRecordOption::WithFreqsAndPositions),
        )
        .set_stored();
    sb.add_text_field("title", title_opts);
    sb.build()
}

/// One per-zone skeleton index.  Cheap to clone (`Arc` inside).
pub struct SkeletonIndex {
    index: Index,
    fields: Fields,
    writer: Mutex<IndexWriter>,
    reader: IndexReader,
}

impl SkeletonIndex {
    /// Open the index at `dir`, creating it (with the skeleton schema)
    /// if nothing lives there yet.  Same open-or-create contract as
    /// [`crate::fts_index::FtsIndex::open_or_create`].
    pub fn open_or_create(dir: PathBuf) -> Result<Arc<Self>, IndexError> {
        std::fs::create_dir_all(&dir)
            .map_err(|e| IndexError::CreateDir(dir.display().to_string(), e.to_string()))?;

        let mmap = MmapDirectory::open(&dir)
            .map_err(|e| IndexError::Open(dir.display().to_string(), e.to_string()))?;

        let schema = build_schema();
        let index = Index::open_or_create(mmap, schema.clone())
            .map_err(|e| IndexError::Open(dir.display().to_string(), e.to_string()))?;

        let fields = Fields::from_schema(&schema);

        let writer = index
            .writer::<TantivyDocument>(WRITER_HEAP_BYTES)
            .map_err(|e| IndexError::WriterInit(e.to_string()))?;

        let reader = index
            .reader_builder()
            .reload_policy(ReloadPolicy::OnCommitWithDelay)
            .try_into()
            .map_err(|e| IndexError::ReaderInit(e.to_string()))?;

        Ok(Arc::new(Self {
            index,
            fields,
            writer: Mutex::new(writer),
            reader,
        }))
    }

    /// Idempotent write: drops any prior doc under `path`, then adds
    /// the new (path, title) pair.  Both operations queue on the same
    /// writer transaction so [`commit`](Self::commit) lands them
    /// atomically — a reader never sees the file present under the
    /// wrong title.
    ///
    /// `title` may be empty when a doc has no heading and no
    /// meaningful basename; skeleton entries with empty title still
    /// match on `path_tokens`, keeping the "path-search" leg working.
    pub fn upsert(&self, path: &str, title: &str) -> Result<(), IndexError> {
        let path_tokens = tokenize_path(path);
        let writer = self.writer.lock();
        writer.delete_term(Term::from_field_text(self.fields.path, path));
        writer
            .add_document(doc!(
                self.fields.path => path,
                self.fields.path_tokens => path_tokens,
                self.fields.title => title,
            ))
            .map_err(|e| IndexError::AddDoc(path.to_string(), e.to_string()))?;
        Ok(())
    }

    /// Drop the skeleton entry for `path`.  No-op if none exists.
    /// The caller commits with the next [`commit`](Self::commit).
    pub fn delete(&self, path: &str) {
        let writer = self.writer.lock();
        writer.delete_term(Term::from_field_text(self.fields.path, path));
    }

    /// Flush buffered writes to disk and force-reload the reader so a
    /// subsequent [`search`](Self::search) sees the commit — mirrors
    /// FtsIndex's commit contract.
    pub fn commit(&self) -> Result<(), IndexError> {
        let mut writer = self.writer.lock();
        writer
            .commit()
            .map_err(|e| IndexError::Commit(e.to_string()))?;
        self.reader
            .reload()
            .map_err(|e| IndexError::Commit(e.to_string()))?;
        Ok(())
    }

    /// Title-2× ranked search over path tokens + title.  Returns up to
    /// `limit` hits ordered by BM25 score descending.  `path_prefix`
    /// filters on `stored_path.starts_with(prefix)` after scoring
    /// (same pattern FtsIndex uses).
    pub fn search(
        &self,
        query: &str,
        limit: usize,
        path_prefix: Option<&str>,
    ) -> Result<Vec<SkeletonHit>, IndexError> {
        if query.trim().is_empty() {
            return Ok(Vec::new());
        }
        let searcher = self.reader.searcher();

        // Two per-field queries; the title arm is boosted 2×.  Wrapped
        // in a SHOULD boolean so a doc matching either field scores,
        // and a doc matching both accumulates.  Both branches use the
        // same tokenized query text because the two fields share the
        // `en_stem` analyser.
        let path_parser = QueryParser::for_index(&self.index, vec![self.fields.path_tokens]);
        let path_q = path_parser
            .parse_query(query)
            .map_err(|e| IndexError::ParseQuery(query.to_string(), e.to_string()))?;

        let title_parser = QueryParser::for_index(&self.index, vec![self.fields.title]);
        let title_q = title_parser
            .parse_query(query)
            .map_err(|e| IndexError::ParseQuery(query.to_string(), e.to_string()))?;

        let boosted_title: Box<dyn Query> = Box::new(BoostQuery::new(title_q, TITLE_BOOST));
        let combined = BooleanQuery::new(vec![
            (Occur::Should, path_q),
            (Occur::Should, boosted_title),
        ]);

        let fetch = if path_prefix.is_some() {
            (limit * 4).min(1_000)
        } else {
            limit
        };

        let top = searcher
            .search(&combined, &TopDocs::with_limit(fetch))
            .map_err(|e| IndexError::Search(e.to_string()))?;

        let mut hits = Vec::with_capacity(limit);
        for (score, addr) in top {
            let stored: TantivyDocument = searcher
                .doc(addr)
                .map_err(|e| IndexError::Search(e.to_string()))?;
            let hit = self.decode(stored, score);
            if let Some(prefix) = path_prefix {
                if !hit.path.starts_with(prefix) {
                    continue;
                }
            }
            hits.push(hit);
            if hits.len() >= limit {
                break;
            }
        }
        Ok(hits)
    }

    /// Exact-path lookup — resolves a single skeleton row by its
    /// `path` field.  Used by tests and by the hydration path in
    /// `do_hybrid_query` to fetch a doc's title after fusion.
    pub fn get_by_path(&self, path: &str) -> Result<Option<SkeletonHit>, IndexError> {
        let searcher = self.reader.searcher();
        let term = Term::from_field_text(self.fields.path, path);
        let query = TermQuery::new(term, IndexRecordOption::Basic);
        let top = searcher
            .search(&query, &TopDocs::with_limit(1))
            .map_err(|e| IndexError::Search(e.to_string()))?;
        let Some((score, addr)) = top.into_iter().next() else {
            return Ok(None);
        };
        let stored: TantivyDocument = searcher
            .doc(addr)
            .map_err(|e| IndexError::Search(e.to_string()))?;
        Ok(Some(self.decode(stored, score)))
    }

    fn decode(&self, stored: TantivyDocument, score: f32) -> SkeletonHit {
        let path = stored
            .get_first(self.fields.path)
            .and_then(|v| v.as_str())
            .unwrap_or_default()
            .to_string();
        let title = stored
            .get_first(self.fields.title)
            .and_then(|v| v.as_str())
            .unwrap_or_default()
            .to_string();
        SkeletonHit { path, title, score }
    }
}

/// Split a path into whitespace-separated segments the BM25 analyser
/// can tokenize normally.  Non-alphanumeric separators (`/`, `-`,
/// `_`, `.`) all become spaces so `/notes/q3-plan.md` becomes
/// `notes q3 plan md` and matches a query "q3 plan".
///
/// Deliberately preserves `md` / `txt` / etc. as tokens — the query
/// stemmer treats them like any other word, and callers who want to
/// exclude them can filter at the caller.  Not a public API; the
/// caller passes a `path`, not tokens.
fn tokenize_path(path: &str) -> String {
    let mut out = String::with_capacity(path.len());
    for ch in path.chars() {
        if ch.is_alphanumeric() {
            out.push(ch);
        } else {
            out.push(' ');
        }
    }
    // Collapse runs of whitespace so the token stream stays clean.
    out.split_whitespace().collect::<Vec<_>>().join(" ")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tempdir() -> PathBuf {
        tempfile::tempdir().expect("tempdir").keep()
    }

    #[test]
    fn open_creates_empty_index() {
        let dir = tempdir().join("skeleton");
        let idx = SkeletonIndex::open_or_create(dir.clone()).expect("open");
        assert!(dir.exists());
        let hits = idx.search("anything", 10, None).expect("search");
        assert!(hits.is_empty());
    }

    #[test]
    fn upsert_then_search_finds_by_path_token() {
        let dir = tempdir().join("skeleton");
        let idx = SkeletonIndex::open_or_create(dir).expect("open");
        idx.upsert("/notes/q3-plan.md", "Q3 Roadmap").expect("upsert");
        idx.commit().expect("commit");

        // Query matches on the path tokenization.
        let hits = idx.search("q3 plan", 10, None).expect("search");
        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0].path, "/notes/q3-plan.md");
        assert!(hits[0].score > 0.0);
    }

    #[test]
    fn title_matches_score_higher_than_path_only_matches() {
        // Two docs, one has the query word ONLY in path tokens, the
        // other has it ONLY in the title.  Title 2× must rank the
        // title-match ahead of the path-match.
        let dir = tempdir().join("skeleton");
        let idx = SkeletonIndex::open_or_create(dir).expect("open");
        idx.upsert("/reports/rocket-launch.md", "Weekly Digest")
            .expect("upsert-path");
        idx.upsert("/misc/other.md", "Rocket Launch Retrospective")
            .expect("upsert-title");
        idx.commit().expect("commit");

        let hits = idx.search("rocket", 10, None).expect("search");
        assert_eq!(hits.len(), 2, "both docs match on 'rocket': {hits:?}");
        assert_eq!(
            hits[0].path, "/misc/other.md",
            "title-match must lead path-only match: {hits:?}"
        );
        assert!(
            hits[0].score > hits[1].score,
            "title 2× boost should widen the score gap: {hits:?}"
        );
    }

    #[test]
    fn upsert_is_idempotent_and_replaces_title() {
        let dir = tempdir().join("skeleton");
        let idx = SkeletonIndex::open_or_create(dir).expect("open");
        idx.upsert("/doc.md", "Old Title").expect("first");
        idx.commit().expect("commit-1");
        idx.upsert("/doc.md", "New Title").expect("second");
        idx.commit().expect("commit-2");

        // Only ONE hit — no duplicate row.
        let hits = idx.search("title", 10, None).expect("search");
        assert_eq!(hits.len(), 1, "upsert must dedupe: {hits:?}");
        assert_eq!(hits[0].title, "New Title");
        // Old title no longer scores.
        let old = idx.search("old", 10, None).expect("search-old");
        assert!(old.is_empty(), "old title text must be gone: {old:?}");
    }

    #[test]
    fn delete_removes_the_entry() {
        let dir = tempdir().join("skeleton");
        let idx = SkeletonIndex::open_or_create(dir).expect("open");
        idx.upsert("/gone.md", "Doomed").expect("upsert");
        idx.commit().expect("commit-1");
        idx.delete("/gone.md");
        idx.commit().expect("commit-2");
        let hits = idx.search("doomed", 10, None).expect("search");
        assert!(hits.is_empty(), "delete must remove the doc: {hits:?}");
    }

    #[test]
    fn empty_query_returns_empty() {
        let dir = tempdir().join("skeleton");
        let idx = SkeletonIndex::open_or_create(dir).expect("open");
        idx.upsert("/x.md", "Widget").expect("upsert");
        idx.commit().expect("commit");
        assert!(idx.search("", 10, None).expect("search").is_empty());
        assert!(idx.search("   ", 10, None).expect("search").is_empty());
    }

    #[test]
    fn morphological_variants_match_stemmed_titles() {
        // Same en_stem chain FtsIndex uses — title "authorized"
        // must match query "authorization".
        let dir = tempdir().join("skeleton");
        let idx = SkeletonIndex::open_or_create(dir).expect("open");
        idx.upsert("/a.md", "authorized access guide")
            .expect("upsert");
        idx.commit().expect("commit");
        let hits = idx.search("authorization", 10, None).expect("search");
        assert_eq!(hits.len(), 1, "stemmer must bridge the variants");
    }

    #[test]
    fn path_prefix_filters_out_off_prefix_hits() {
        let dir = tempdir().join("skeleton");
        let idx = SkeletonIndex::open_or_create(dir).expect("open");
        idx.upsert("/notes/hello.md", "Hello Notes")
            .expect("upsert-a");
        idx.upsert("/logs/hello.md", "Hello Log").expect("upsert-b");
        idx.commit().expect("commit");
        let hits = idx.search("hello", 10, Some("/notes/")).expect("search");
        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0].path, "/notes/hello.md");
    }

    #[test]
    fn get_by_path_returns_the_stored_row() {
        let dir = tempdir().join("skeleton");
        let idx = SkeletonIndex::open_or_create(dir).expect("open");
        idx.upsert("/z.md", "Zeta Doc").expect("upsert");
        idx.commit().expect("commit");
        let hit = idx
            .get_by_path("/z.md")
            .expect("get")
            .expect("must be present");
        assert_eq!(hit.title, "Zeta Doc");
    }

    #[test]
    fn get_by_path_missing_is_none() {
        let dir = tempdir().join("skeleton");
        let idx = SkeletonIndex::open_or_create(dir).expect("open");
        assert!(idx.get_by_path("/nope.md").expect("get").is_none());
    }

    #[test]
    fn empty_title_still_matches_on_path_tokens() {
        // Chunkless / heading-less files still enter the index so the
        // path-search leg keeps working.  Zero-body files stay
        // retrievable per the design doc.
        let dir = tempdir().join("skeleton");
        let idx = SkeletonIndex::open_or_create(dir).expect("open");
        idx.upsert("/notes/rocket.md", "").expect("upsert");
        idx.commit().expect("commit");
        let hits = idx.search("rocket", 10, None).expect("search");
        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0].path, "/notes/rocket.md");
        assert_eq!(hits[0].title, "");
    }

    #[test]
    fn tokenize_path_splits_on_non_alphanumeric() {
        assert_eq!(tokenize_path("/notes/q3-plan.md"), "notes q3 plan md");
        assert_eq!(
            tokenize_path("/a/b_c/d.e.f"),
            "a b c d e f",
            "underscores and dots split too"
        );
        assert_eq!(tokenize_path("/"), "");
    }
}
