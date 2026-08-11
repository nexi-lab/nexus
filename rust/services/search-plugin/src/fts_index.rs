//! Per-zone tantivy full-text index (Phase 1 of the Python-parity
//! roadmap; see `PARITY_ROADMAP.md` D1).
//!
//! # Storage layout
//!
//! Each zone has its own tantivy directory rooted at
//! `~/.nexus/plugins/search/<zone_id>/fts-v2/`.  The directory is
//! plugin-owned, per-node, and NOT replicated — the SSOT for
//! "what belongs in the index" is the kernel VFS itself, and the
//! MetaStore `search.indexed_gen` sidecar (added in Phase 5)
//! records the last-indexed generation so any node can drop-and-
//! rebuild without losing correctness anchors.  Dropping the
//! directory is always safe — the next Index or Query call
//! transparently recreates it.
//!
//! # Schema (P1)
//!
//! ```text
//! path         STRING | STORED   exact match + retrievable
//! chunk_index  I64    | STORED   integer, 0 in P1 (one chunk per file)
//! chunk_text   TEXT(en_stem) | STORED   BM25-analysed, English-stemmed
//! mtime_ms     I64    | STORED   millisecond-resolution mtime
//! ```
//!
//! `chunk_text` runs through tantivy's built-in `en_stem` analyzer
//! (simple tokenizer + lowercase + English Porter stemmer) so
//! morphological query variants match — `authorization` finds
//! "authorized", matching the pre-P12 Postgres FTS behaviour that
//! stemmed both sides (#4618).  The query parser resolves the same
//! analyzer from the field config, so index and query time agree.
//! This is an index-schema property: indices built with the
//! pre-stemmer schema live under the old `fts/` directory and the
//! manager opens `fts-v2/` instead — rebuild to populate.
//!
//! `mtime_ms` is STORED only in P1 (no INDEXED).  P6 flips it to
//! `STORED | INDEXED` when recency queries land; INDEXED today would
//! build a range index nothing queries.  P4 grows `chunk_index` to
//! a real value once the chunker lands; P2 adds a `vec_id` field
//! pointing at the sibling HNSW index.
//!
//! # Idempotency
//!
//! [`add_document`](FtsIndex::add_document) first deletes any prior
//! document with the same `path` (P1 = one chunk per file, so path
//! IS the primary key).  Re-indexing the same file replaces its
//! document instead of duplicating it — Index calls are safe to
//! retry.  P4 grows the key to `(path, chunk_index)` when the
//! chunker emits multiple chunks per file.
//!
//! # Concurrency
//!
//! `tantivy::IndexWriter` allows one writer per index at a time.  We
//! wrap it in a `parking_lot::Mutex`; add_document + commit are
//! millisecond-scale and the mutex is not held across await points,
//! so contention is negligible even under concurrent Index calls
//! for different paths in the same zone.
//!
//! Readers are cheap — tantivy's `IndexReader` snapshots the last
//! committed generation and is `Send + Sync`.  The `search` path
//! takes no locks.

use std::path::PathBuf;
use std::sync::Arc;

use parking_lot::Mutex;
use tantivy::collector::TopDocs;
use tantivy::directory::MmapDirectory;
use tantivy::query::QueryParser;
use tantivy::query::TermQuery;
use tantivy::schema::{
    Field, IndexRecordOption, Schema, TextFieldIndexing, TextOptions, Value, STORED, STRING,
};
use tantivy::{doc, Index, IndexReader, IndexWriter, ReloadPolicy, TantivyDocument, Term};

/// tantivy writer heap — 50 MiB is the conventional per-index budget
/// and lets a batch of ~1 000 chunks commit without forcing a merge.
/// The plugin holds at most one writer per zone in memory.
const WRITER_HEAP_BYTES: usize = 50 * 1024 * 1024;

/// Result row returned by [`FtsIndex::search`].
#[derive(Debug, Clone)]
pub struct FtsHit {
    pub path: String,
    pub chunk_index: u32,
    pub chunk_text: String,
    pub score: f32,
    pub mtime_ms: Option<i64>,
}

/// P1 schema field handles.  Grouped so callers construct one
/// [`FtsIndex`] and get all field accessors without re-resolving by
/// name (which would allocate + hash per call).
#[derive(Debug, Clone)]
pub struct Fields {
    pub path: Field,
    pub chunk_index: Field,
    pub chunk_text: Field,
    pub mtime_ms: Field,
}

impl Fields {
    fn from_schema(schema: &Schema) -> Self {
        // Names are stable; if they drift a rebuild is the recovery
        // path (see module doc).  `get_field` returns `TantivyError`
        // rather than `Option`, so `expect` here would only fire on a
        // programmer bug in `build_schema` below.
        Self {
            path: schema.get_field("path").expect("schema field: path"),
            chunk_index: schema
                .get_field("chunk_index")
                .expect("schema field: chunk_index"),
            chunk_text: schema
                .get_field("chunk_text")
                .expect("schema field: chunk_text"),
            mtime_ms: schema
                .get_field("mtime_ms")
                .expect("schema field: mtime_ms"),
        }
    }
}

/// Build the P1 schema.  Broken out so tests can construct a schema
/// without opening a directory.
pub fn build_schema() -> Schema {
    let mut sb = Schema::builder();
    // path — exact-match (STRING), retrievable.  Filtered on prefix
    // by the query layer, not by tantivy itself in P1 (a term-prefix
    // query would work but adds tokenisation surprises for paths).
    sb.add_text_field("path", STRING | STORED);
    // chunk_index — STORED only in P1 (always 0, no query hits it).
    // P4's chunker flips to `STORED | INDEXED` when composite-key
    // dedupe by (path, chunk_index) lands.
    sb.add_i64_field("chunk_index", STORED);
    // chunk_text — BM25-analysed through `en_stem` (simple tokenizer
    // + lowercase + English Porter stemmer; pre-registered in
    // tantivy's default TokenizerManager).  The default TEXT chain
    // has no stemmer, which cost the keyword lane every
    // morphological-variant query vs the pre-P12 Postgres stack
    // (#4618: `authorization` vs "authorized" → 0 hits).
    let chunk_text_opts = TextOptions::default()
        .set_indexing_options(
            TextFieldIndexing::default()
                .set_tokenizer("en_stem")
                .set_index_option(IndexRecordOption::WithFreqsAndPositions),
        )
        .set_stored();
    sb.add_text_field("chunk_text", chunk_text_opts);
    // mtime_ms — STORED only in P1.  P6 lifts to `STORED | INDEXED`
    // when recency range queries land; indexing today would build a
    // range structure nothing queries.
    sb.add_i64_field("mtime_ms", STORED);
    sb.build()
}

/// One per-zone tantivy index.  Cheap to clone (`Arc` inside).
pub struct FtsIndex {
    index: Index,
    fields: Fields,
    writer: Mutex<IndexWriter>,
    reader: IndexReader,
}

impl FtsIndex {
    /// Open the index at `dir`, creating it (with the P1 schema) if
    /// nothing lives there yet.  Directory is created recursively —
    /// callers pass a leaf path, we make it.
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
            // OnCommitWithDelay = the reader auto-refreshes shortly
            // after the writer commits; searches after add_doc see
            // fresh state without the caller having to plumb a
            // manual reload.  Tests that need synchronous visibility
            // call `reader.reload()` on the returned handle.
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

    /// Add a chunk document.  P4 semantics: one call = one chunk;
    /// callers own the (path, chunk_index) key discipline.  Adding
    /// the same key twice writes TWO tantivy docs — the caller MUST
    /// [`delete_all_chunks`](Self::delete_all_chunks) first when
    /// re-indexing a file, otherwise BM25 double-counts.
    ///
    /// The writer buffers in RAM until [`commit`](Self::commit) is
    /// called — callers batch adds and commit at end-of-request so
    /// tantivy can flush a single segment (much cheaper than one
    /// commit per doc).
    ///
    /// `mtime_ms = None` is stored as a distinguishable sentinel
    /// (`i64::MIN`) so it round-trips cleanly through [`search`](Self::search).
    /// Using an out-of-band value keeps the field non-`optional` in the
    /// schema which sidesteps a tantivy `Option<i64>` retrieval quirk.
    pub fn add_document(
        &self,
        path: &str,
        chunk_index: u32,
        chunk_text: &str,
        mtime_ms: Option<i64>,
    ) -> Result<(), IndexError> {
        let writer = self.writer.lock();
        writer
            .add_document(doc!(
                self.fields.path => path,
                self.fields.chunk_index => i64::from(chunk_index),
                self.fields.chunk_text => chunk_text,
                self.fields.mtime_ms => mtime_ms.unwrap_or(i64::MIN),
            ))
            .map_err(|e| IndexError::AddDoc(path.to_string(), e.to_string()))?;
        Ok(())
    }

    /// Drop every chunk indexed under `path`.  Used by the reindex
    /// pattern in `do_index`: a file whose chunk count shrinks (was
    /// 3 chunks, now 2) leaves orphaned chunk 2/3 rows unless the
    /// caller explicitly drops the file's whole set first.
    ///
    /// Both the delete and any subsequent adds queue on the SAME
    /// writer transaction, so `commit()` lands them atomically — a
    /// reader never sees a partially-reindexed file (zero chunks
    /// visible, then N chunks visible; no in-between).  Idempotent:
    /// calling on a path with no live chunks is a no-op.
    pub fn delete_all_chunks(&self, path: &str) {
        let writer = self.writer.lock();
        writer.delete_term(Term::from_field_text(self.fields.path, path));
    }

    /// Flush buffered adds to disk and make them visible to searchers.
    /// Cheap for small batches; a couple of ms per commit + a
    /// background merge that happens on tantivy's own thread.
    ///
    /// The reader is force-reloaded synchronously so a subsequent
    /// [`search`](Self::search) sees the commit — tantivy's
    /// `OnCommitWithDelay` policy would otherwise let the reader lag
    /// long enough for tests + tight write-then-read loops to see
    /// stale state.
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

    /// Ranked keyword search.  Returns up to `limit` hits ordered by
    /// BM25 score descending.  `path_prefix` filters on
    /// `stored_path.starts_with(prefix)` after the tantivy result
    /// pass (P1); a proper indexed path-prefix filter comes with P3's
    /// query composer.
    pub fn search(
        &self,
        query: &str,
        limit: usize,
        path_prefix: Option<&str>,
    ) -> Result<Vec<FtsHit>, IndexError> {
        let searcher = self.reader.searcher();

        let parser = QueryParser::for_index(&self.index, vec![self.fields.chunk_text]);
        let parsed = parser
            .parse_query(query)
            .map_err(|e| IndexError::ParseQuery(query.to_string(), e.to_string()))?;

        // Over-fetch when a path prefix is set: the prefix filter is
        // applied post-scoring in P1, so we grab a wider window to
        // avoid returning fewer than `limit` hits when the top of the
        // BM25 list is largely off-prefix.  Bounded so a pathological
        // filter doesn't scan the whole index.
        let fetch = if path_prefix.is_some() {
            (limit * 4).min(1_000)
        } else {
            limit
        };

        let top = searcher
            .search(&parsed, &TopDocs::with_limit(fetch))
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

    /// Exact-path lookup — resolves a single doc by its `path`
    /// field.  Uses a `TermQuery` against the STRING-indexed `path`
    /// field so it doesn't depend on the BM25 tokenisation matching
    /// the query text (which the older
    /// `.search(basename, limit, Some(path))` shim did).  Returns
    /// `None` when no doc has that exact path — no-op for callers
    /// materialising ANN hits into the full QueryResult shape.
    pub fn get_by_path(&self, path: &str) -> Result<Option<FtsHit>, IndexError> {
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

    /// All chunks stored under `path`, sorted by `chunk_index`
    /// ascending.  Used by the `expand=macro` code path in
    /// `service.rs` to build the previous + current + next
    /// context around each hit without a per-hit search round trip.
    /// Caps at 512 chunks to bound a pathologically-large file's
    /// footprint here — a file with more than 512 chunks in P4 is
    /// beyond the intended budget anyway (chunks_per_page-pooled
    /// results wouldn't surface that many either).
    pub fn get_chunks_by_path(&self, path: &str) -> Result<Vec<FtsHit>, IndexError> {
        const CAP: usize = 512;
        let searcher = self.reader.searcher();
        let term = Term::from_field_text(self.fields.path, path);
        let query = TermQuery::new(term, IndexRecordOption::Basic);
        let top = searcher
            .search(&query, &TopDocs::with_limit(CAP))
            .map_err(|e| IndexError::Search(e.to_string()))?;
        let mut hits: Vec<FtsHit> = Vec::with_capacity(top.len());
        for (score, addr) in top {
            let stored: TantivyDocument = searcher
                .doc(addr)
                .map_err(|e| IndexError::Search(e.to_string()))?;
            hits.push(self.decode(stored, score));
        }
        hits.sort_by_key(|h| h.chunk_index);
        Ok(hits)
    }

    /// Current searcher generation — bumps on every commit + reader
    /// reload.  The per-zone skeleton cache (#4628) keys on this so
    /// index mutations invalidate the skeleton automatically, with
    /// no invalidation call sites to keep in sync.
    pub fn generation_id(&self) -> u64 {
        self.reader.searcher().generation().generation_id()
    }

    /// Visit every alive stored chunk as `(path, chunk_index,
    /// chunk_text)`.  Full stored-doc scan — the skeleton build
    /// (#4628) is the only caller, and it runs at most once per
    /// index generation per zone, off the async runtime on the
    /// blocking pool.  All chunks are visited (not just chunk 0)
    /// because the chunker seals frontmatter/preamble into its own
    /// leading chunk — a doc's first heading can live in chunk 1 or
    /// 2, so the caller reassembles the doc head from ordered
    /// leading chunks.  Returns the generation of the searcher the
    /// scan used, so the caller can tag derived data with a
    /// scan-consistent version.
    pub fn for_each_chunk<F: FnMut(&str, u32, &str)>(&self, mut f: F) -> Result<u64, IndexError> {
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
                f(&hit.path, hit.chunk_index, &hit.chunk_text);
            }
        }
        Ok(gen)
    }

    fn decode(&self, stored: TantivyDocument, score: f32) -> FtsHit {
        // Access stored fields by handle.  If a field is missing (a
        // schema-drift bug), we surface a blank rather than panicking
        // — search results with an empty path are visibly broken and
        // easy to trace, whereas a panic would take down the whole
        // plugin thread.
        let path = stored
            .get_first(self.fields.path)
            .and_then(|v| v.as_str())
            .unwrap_or_default()
            .to_string();
        let chunk_index = stored
            .get_first(self.fields.chunk_index)
            .and_then(|v| v.as_i64())
            .and_then(|v| u32::try_from(v).ok())
            .unwrap_or(0);
        let chunk_text = stored
            .get_first(self.fields.chunk_text)
            .and_then(|v| v.as_str())
            .unwrap_or_default()
            .to_string();
        let mtime_raw = stored
            .get_first(self.fields.mtime_ms)
            .and_then(|v| v.as_i64());
        let mtime_ms = match mtime_raw {
            Some(v) if v == i64::MIN => None,
            Some(v) => Some(v),
            None => None,
        };
        FtsHit {
            path,
            chunk_index,
            chunk_text,
            score,
            mtime_ms,
        }
    }
}

// ── Errors ────────────────────────────────────────────────────────

#[derive(Debug, thiserror::Error)]
pub enum IndexError {
    #[error("create index dir {0}: {1}")]
    CreateDir(String, String),
    #[error("open index at {0}: {1}")]
    Open(String, String),
    #[error("init tantivy writer: {0}")]
    WriterInit(String),
    #[error("init tantivy reader: {0}")]
    ReaderInit(String),
    #[error("add doc {0}: {1}")]
    AddDoc(String, String),
    #[error("commit: {0}")]
    Commit(String),
    #[error("parse query {0:?}: {1}")]
    ParseQuery(String, String),
    #[error("search: {0}")]
    Search(String),
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tempdir() -> PathBuf {
        let dir = tempfile::tempdir().expect("tempdir");
        // Leak so `dir` lives past the return; test-local, no cleanup
        // required.
        dir.keep()
    }

    #[test]
    fn open_creates_empty_index() {
        let dir = tempdir().join("fts");
        let idx = FtsIndex::open_or_create(dir.clone()).expect("open");
        assert!(dir.exists());
        // Empty index: any query returns zero hits.
        let hits = idx.search("hello", 10, None).expect("search");
        assert!(hits.is_empty());
    }

    #[test]
    fn add_then_search_finds_document() {
        let dir = tempdir().join("fts");
        let idx = FtsIndex::open_or_create(dir).expect("open");

        idx.add_document("/notes/hello.md", 0, "hello world", Some(1_700_000_000_000))
            .expect("add");
        idx.add_document(
            "/notes/other.md",
            0,
            "goodbye world",
            Some(1_700_000_000_001),
        )
        .expect("add");
        idx.commit().expect("commit");

        let hits = idx.search("hello", 10, None).expect("search");
        assert_eq!(hits.len(), 1, "expected the hello doc only, got {hits:?}");
        assert_eq!(hits[0].path, "/notes/hello.md");
        assert_eq!(hits[0].chunk_index, 0);
        assert_eq!(hits[0].mtime_ms, Some(1_700_000_000_000));
        assert!(hits[0].score > 0.0);
    }

    #[test]
    fn morphological_query_variants_match_stemmed_corpus_terms() {
        // #4618: the pre-P12 Postgres FTS stack stems both sides
        // (`authorization` → `authoriz` → matches "authorized"); the
        // tantivy default tokenizer does not, so natural-language
        // morphological variants of corpus terms returned 0 hits.
        // `chunk_text` uses the `en_stem` analyzer — same lowercase +
        // simple-tokenize chain plus an English stemmer, applied at
        // index AND query time via the field's tokenizer config.
        let dir = tempdir().join("fts");
        let idx = FtsIndex::open_or_create(dir).expect("open");
        idx.add_document("/d09.md", 0, "access was authorized by the admin", Some(1))
            .expect("add");
        idx.add_document("/d05.md", 0, "the engineering team shipped it", Some(2))
            .expect("add");
        idx.add_document("/d13.md", 0, "swap the battery pack first", Some(3))
            .expect("add");
        idx.commit().expect("commit");

        for (query, want_path) in [
            ("authorization", "/d09.md"),
            ("engineers", "/d05.md"),
            ("batteries", "/d13.md"),
            // Exact-form controls — must keep matching post-stemmer.
            ("authorized", "/d09.md"),
            ("engineering", "/d05.md"),
        ] {
            let hits = idx.search(query, 10, None).expect("search");
            assert_eq!(
                hits.len(),
                1,
                "query {query:?}: expected 1 hit, got {hits:?}"
            );
            assert_eq!(hits[0].path, want_path, "query {query:?}");
        }
    }

    #[test]
    fn missing_mtime_round_trips_as_none() {
        let dir = tempdir().join("fts");
        let idx = FtsIndex::open_or_create(dir).expect("open");
        idx.add_document("/x.md", 0, "widget alpha", None)
            .expect("add");
        idx.commit().expect("commit");

        let hits = idx.search("widget", 10, None).expect("search");
        assert_eq!(hits.len(), 1);
        assert_eq!(
            hits[0].mtime_ms, None,
            "sentinel must decode back to None, got {:?}",
            hits[0].mtime_ms,
        );
    }

    #[test]
    fn path_prefix_filters_results() {
        let dir = tempdir().join("fts");
        let idx = FtsIndex::open_or_create(dir).expect("open");
        idx.add_document("/notes/a.md", 0, "shared word", Some(1))
            .expect("add");
        idx.add_document("/logs/b.md", 0, "shared word", Some(2))
            .expect("add");
        idx.commit().expect("commit");

        let hits = idx.search("shared", 10, Some("/notes/")).expect("search");
        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0].path, "/notes/a.md");
    }

    #[test]
    fn distinct_chunks_of_same_path_coexist() {
        // P4 primary key = (path, chunk_index).  Adding chunk 0 and
        // chunk 1 under the same path leaves BOTH docs live.  A BM25
        // query that would only match one chunk (via its distinct
        // text) returns exactly one hit.
        let dir = tempdir().join("fts");
        let idx = FtsIndex::open_or_create(dir).expect("open");
        idx.add_document("/doc.md", 0, "alpha payload", Some(1))
            .expect("add-0");
        idx.add_document("/doc.md", 1, "beta payload", Some(1))
            .expect("add-1");
        idx.commit().expect("commit");

        let hits = idx.search("alpha", 10, None).expect("search");
        assert_eq!(hits.len(), 1, "only chunk 0 has 'alpha': {hits:?}");
        assert_eq!(hits[0].chunk_index, 0);

        // Both chunks share the "payload" token.
        let hits = idx.search("payload", 10, None).expect("search");
        assert_eq!(hits.len(), 2);
    }

    #[test]
    fn delete_all_chunks_then_readd_replaces_cleanly() {
        // Reindex-with-different-chunk-count contract: a file that
        // shrinks from 3 chunks to 2 must not leave the third as an
        // orphan.  Caller uses the delete_all_chunks + per-chunk
        // add_document pattern; both queue on the same writer
        // transaction so the commit lands atomically.
        let dir = tempdir().join("fts");
        let idx = FtsIndex::open_or_create(dir).expect("open");
        for i in 0..3u32 {
            idx.add_document("/doc.md", i, &format!("widget chunk {i}"), Some(1))
                .expect("add");
        }
        idx.commit().expect("commit-1");
        assert_eq!(
            idx.search("widget", 10, None).expect("search").len(),
            3,
            "3 chunks after first index",
        );

        // Reindex with only 2 chunks — first drop the whole file.
        idx.delete_all_chunks("/doc.md");
        for i in 0..2u32 {
            idx.add_document("/doc.md", i, &format!("widget rebuilt {i}"), Some(2))
                .expect("add");
        }
        idx.commit().expect("commit-2");

        let hits = idx.search("widget", 10, None).expect("search");
        assert_eq!(
            hits.len(),
            2,
            "expected 2 chunks after reindex, got {hits:?}"
        );
        // Only the rebuilt text is left.
        assert!(
            hits.iter().all(|h| h.chunk_text.contains("rebuilt")),
            "old chunks leaked: {hits:?}",
        );
    }

    #[test]
    fn delete_all_chunks_on_missing_path_is_noop() {
        let dir = tempdir().join("fts");
        let idx = FtsIndex::open_or_create(dir).expect("open");
        idx.add_document("/live.md", 0, "widget alpha", Some(1))
            .expect("add");
        idx.commit().expect("commit-1");
        idx.delete_all_chunks("/does-not-exist");
        idx.commit().expect("commit-2");
        // Live doc still there.
        let hits = idx.search("widget", 10, None).expect("search");
        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0].path, "/live.md");
    }

    #[test]
    fn top_docs_bounded_by_limit() {
        let dir = tempdir().join("fts");
        let idx = FtsIndex::open_or_create(dir).expect("open");
        for i in 0..20 {
            idx.add_document(&format!("/doc-{i}.md"), 0, "word", Some(i))
                .expect("add");
        }
        idx.commit().expect("commit");

        let hits = idx.search("word", 5, None).expect("search");
        assert_eq!(hits.len(), 5);
    }

    #[test]
    fn for_each_chunk_scans_all_chunks_and_generation_bumps() {
        let dir = tempdir().join("fts");
        let idx = FtsIndex::open_or_create(dir).expect("open");
        idx.add_document("/a.md", 0, "# Alpha\nbody a", Some(1))
            .expect("add");
        idx.add_document("/a.md", 1, "body a continued", Some(1))
            .expect("add");
        idx.add_document("/b.md", 0, "# Beta\nbody b", Some(2))
            .expect("add");
        idx.commit().expect("commit");
        let gen_before = idx.generation_id();

        let mut seen: Vec<(String, u32, String)> = Vec::new();
        let scan_gen = idx
            .for_each_chunk(|path, idx_, text| {
                seen.push((path.to_string(), idx_, text.to_string()))
            })
            .expect("scan");
        seen.sort();
        assert_eq!(
            scan_gen, gen_before,
            "scan reports the generation it observed"
        );
        assert_eq!(
            seen,
            vec![
                ("/a.md".to_string(), 0, "# Alpha\nbody a".to_string()),
                ("/a.md".to_string(), 1, "body a continued".to_string()),
                ("/b.md".to_string(), 0, "# Beta\nbody b".to_string()),
            ],
            "every chunk must appear with its index"
        );

        // A commit (even after a delete+re-add) bumps the generation —
        // this is the skeleton cache's staleness signal.
        idx.delete_all_chunks("/b.md");
        idx.add_document("/b.md", 0, "# Beta2\nbody", Some(3))
            .expect("add");
        idx.commit().expect("commit");
        assert_ne!(
            idx.generation_id(),
            gen_before,
            "commit must change the generation"
        );
    }
}
