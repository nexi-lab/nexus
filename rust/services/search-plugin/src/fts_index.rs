//! Per-zone tantivy full-text index (Phase 1 of the Python-parity
//! roadmap; see `PARITY_ROADMAP.md` D1).
//!
//! # Storage layout
//!
//! Each zone has its own tantivy directory rooted at
//! `~/.nexus/plugins/search/<zone_id>/fts/`.  The directory is
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
//! path         STRING | STORED             exact match + retrievable
//! chunk_index  I64    | INDEXED | STORED   integer, 0 in P1 (one chunk per file)
//! chunk_text   TEXT   | STORED             BM25-analysed
//! mtime_ms     I64    | INDEXED | STORED   millisecond-resolution mtime
//! ```
//!
//! P4 grows `chunk_index` to a real value once the chunker lands;
//! P2 adds a `vec_id` field pointing at the sibling HNSW index.
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
use tantivy::schema::{Field, Schema, INDEXED, STORED, STRING, TEXT};
use tantivy::{doc, Index, IndexReader, IndexWriter, ReloadPolicy, TantivyDocument};

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
            mtime_ms: schema.get_field("mtime_ms").expect("schema field: mtime_ms"),
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
    // chunk_index — small integer, INDEXED so future range queries
    // (dedupe by (path, chunk_index)) don't require re-scan.
    sb.add_i64_field("chunk_index", INDEXED | STORED);
    // chunk_text — BM25-analysed.  Default TEXT = lowercase + simple
    // tokeniser; P4 replaces this with a language-aware chain.
    sb.add_text_field("chunk_text", TEXT | STORED);
    // mtime_ms — INDEXED for P6's recency range queries.
    sb.add_i64_field("mtime_ms", INDEXED | STORED);
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
            // OnCommit = the reader auto-refreshes when the writer
            // commits; searches after add_doc see fresh state without
            // the caller having to plumb a manual reload.
            .reload_policy(ReloadPolicy::OnCommit)
            .try_into()
            .map_err(|e| IndexError::ReaderInit(e.to_string()))?;

        Ok(Arc::new(Self {
            index,
            fields,
            writer: Mutex::new(writer),
            reader,
        }))
    }

    /// Add a document.  P1 semantics: one call = one document = one
    /// chunk.  The writer buffers in RAM until [`commit`](Self::commit)
    /// is called — callers batch adds and commit at end-of-request so
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

    /// Flush buffered adds to disk and make them visible to searchers.
    /// Cheap for small batches; a couple of ms per commit + a
    /// background merge that happens on tantivy's own thread.
    pub fn commit(&self) -> Result<(), IndexError> {
        let mut writer = self.writer.lock();
        writer
            .commit()
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
        idx.add_document("/notes/other.md", 0, "goodbye world", Some(1_700_000_000_001))
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
}
