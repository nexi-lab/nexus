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
//!
//! # Writer liveness (#4725)
//!
//! tantivy's `commit()` joins and respawns every indexing worker
//! thread.  When the OS refuses a thread mid-respawn (`pids.max`
//! exhausted ⇒ EAGAIN) the writer can be left with NO workers, and its
//! next `commit()` quietly recreates a live document channel: from
//! then on `add_document` buffers into a channel nobody drains, every
//! commit discards the buffer and still succeeds, and the plugin
//! acknowledges writes that never become searchable.  That was the
//! 26-hour silent-loss window in #4725.  Two guards close it:
//!
//! - **Commit probe.**  Each transaction remembers the last path it
//!   added; after the commit + reload that path must resolve via the
//!   exact-match `path` term.  If it does not, the commit is reported
//!   as [`IndexError::LostWrite`] — the caller never records state or
//!   returns a success count for it.
//! - **Rebuild on fault.**  A failed commit or a failed probe drops
//!   the writer and reopens it.  If the reopen fails too (still under
//!   thread pressure) the slot stays empty and every mutation fails
//!   loudly until a later reopen succeeds.
//!
//! [`FtsIndex::writer_status`] exposes the fault for the Health RPC so
//! a poller cannot read `healthy` while the writer is dead.

use std::path::PathBuf;
use std::sync::Arc;
use std::time::Instant;

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

/// Indexing worker threads per writer (#4725).  tantivy's default is
/// `min(num_cpus, 8)`, trimmed to 3 by the 50 MiB heap budget — and
/// `commit()` JOINS + RESPAWNS every worker, so each commit cost three
/// `pthread_create`s.  The plugin commits per mutating RPC (and every
/// few docs inside an explicit batch), which under a cgroup `pids.max`
/// shared with the Python server was the steady thread churn behind
/// the EAGAIN in #4725.  One worker: indexing is not the bottleneck
/// (embedding is), and one segment per commit merges cheaper than
/// three.
const WRITER_INDEXING_THREADS: usize = 1;

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
    writer: Mutex<WriterSlot>,
    /// Writer liveness for the Health RPC — a side mutex `commit()`
    /// updates, so a poll never queues behind an in-flight commit.
    status: Mutex<WriterStatus>,
    reader: IndexReader,
}

/// The tantivy writer plus the per-transaction bookkeeping the
/// commit-time liveness check needs (#4725).  Guarded by one
/// `parking_lot::Mutex`; see the module doc's "Writer liveness".
struct WriterSlot {
    /// `None` after a fault whose rebuild failed — every mutation then
    /// fails loudly until [`FtsIndex::ensure_writer`] reopens it.
    writer: Option<IndexWriter>,
    /// Path of the most recent `add_document` in the current
    /// transaction whose LAST op is still an add (a later
    /// `delete_all_chunks` on the same path clears it).  `commit()`
    /// looks it up after the reload: a committed add that is not
    /// searchable means the writer accepted ops it never indexed.
    probe: Option<String>,
    /// Mutations this transaction could not queue because the writer
    /// was unavailable.  Non-zero fails the commit — the caller must
    /// not acknowledge a transaction that only partly reached tantivy.
    lost_ops: u32,
    #[cfg(test)]
    fault_injection: FaultInjection,
}

impl WriterSlot {
    fn new(writer: IndexWriter) -> Self {
        Self {
            writer: Some(writer),
            probe: None,
            lost_ops: 0,
            #[cfg(test)]
            fault_injection: FaultInjection::default(),
        }
    }
}

/// Test seams for the fault paths — the real trigger (the OS refusing
/// a thread) cannot be reproduced deterministically in a unit test.
#[cfg(test)]
#[derive(Default)]
struct FaultInjection {
    /// Accept `add_document` calls without handing them to tantivy —
    /// the zero-worker writer shape from #4725.
    drop_adds: bool,
    /// Fail the next `commit()` before tantivy runs.
    fail_next_commit: bool,
}

/// Writer liveness surfaced on the Health RPC (#4725).
#[derive(Debug, Clone)]
pub struct WriterStatus {
    /// False when the last fault's writer rebuild failed: every write
    /// to this zone errors until a later call manages to reopen it.
    pub available: bool,
    /// Most recent fault not yet followed by a probe-verified commit.
    /// `None` = the writer has proven it indexes what it accepts.
    pub last_fault: Option<WriterFault>,
    /// When the writer last proved a write searchable (probe-verified
    /// commit).  `None` until the first one in this process.  Health
    /// reports its age so a poller can alert on a writer that stopped
    /// landing writes.
    pub last_verified_commit: Option<Instant>,
}

/// One recorded writer fault.
#[derive(Debug, Clone)]
pub struct WriterFault {
    pub at: Instant,
    pub detail: String,
}

fn open_writer(index: &Index) -> Result<IndexWriter, IndexError> {
    index
        .writer_with_num_threads::<TantivyDocument>(WRITER_INDEXING_THREADS, WRITER_HEAP_BYTES)
        .map_err(|e| IndexError::WriterInit(e.to_string()))
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

        let writer = open_writer(&index)?;

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
            writer: Mutex::new(WriterSlot::new(writer)),
            status: Mutex::new(WriterStatus {
                available: true,
                last_fault: None,
                last_verified_commit: None,
            }),
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
        let mut slot = self.writer.lock();
        #[cfg(test)]
        if slot.fault_injection.drop_adds {
            slot.probe = Some(path.to_string());
            return Ok(());
        }
        self.ensure_writer(&mut slot)?
            .add_document(doc!(
                self.fields.path => path,
                self.fields.chunk_index => i64::from(chunk_index),
                self.fields.chunk_text => chunk_text,
                self.fields.mtime_ms => mtime_ms.unwrap_or(i64::MIN),
            ))
            .map_err(|e| IndexError::AddDoc(path.to_string(), e.to_string()))?;
        // Commit-time liveness probe (#4725): this path's final op is
        // an add, so it must be searchable once committed.
        slot.probe = Some(path.to_string());
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
        let mut slot = self.writer.lock();
        // The path's final op this transaction is now a delete — an
        // empty post-commit lookup would be correct, not a lost write.
        if slot.probe.as_deref() == Some(path) {
            slot.probe = None;
        }
        let queued = match self.ensure_writer(&mut slot) {
            Ok(writer) => {
                writer.delete_term(Term::from_field_text(self.fields.path, path));
                true
            }
            Err(e) => {
                tracing::warn!(
                    path = %path,
                    err = %e,
                    "fts delete could not be queued — the commit will fail",
                );
                false
            }
        };
        if !queued {
            // Infallible signature — the loss is accounted for here and
            // fails the commit instead (#4725).
            slot.lost_ops += 1;
        }
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
    ///
    /// Liveness (#4725): after the reload, the transaction's probe
    /// path (its last add) must be searchable.  A commit tantivy
    /// reported successful whose adds are not searchable is a LOST
    /// WRITE — reported as [`IndexError::LostWrite`] after the writer
    /// is rebuilt, so the caller neither records state nor
    /// acknowledges the batch.  Mutations that could not even be
    /// queued (writer unavailable) fail the commit the same way.  Only
    /// a probe-verified commit clears a recorded fault.
    pub fn commit(&self) -> Result<(), IndexError> {
        let mut slot = self.writer.lock();
        let probe = slot.probe.take();
        let lost_ops = std::mem::take(&mut slot.lost_ops);
        if lost_ops > 0 {
            let detail = format!(
                "{lost_ops} mutation(s) never reached tantivy — the writer was unavailable"
            );
            self.fault(&mut slot, &detail);
            return Err(IndexError::LostWrite(detail));
        }
        if let Err(e) = self.commit_writer(&mut slot) {
            self.fault(&mut slot, &format!("commit failed: {e}"));
            return Err(e);
        }
        self.reader
            .reload()
            .map_err(|e| IndexError::Commit(e.to_string()))?;
        let Some(path) = probe else {
            // Nothing added this transaction (delete-only / empty):
            // nothing to verify, and nothing that proves a faulted
            // writer healthy again.
            return Ok(());
        };
        if self.get_chunks_by_path(&path)?.is_empty() {
            let detail = format!(
                "commit succeeded but {path:?} is not searchable — the writer accepted \
                 mutations it never indexed"
            );
            self.fault(&mut slot, &detail);
            return Err(IndexError::LostWrite(detail));
        }
        {
            let mut status = self.status.lock();
            status.last_fault = None;
            status.last_verified_commit = Some(Instant::now());
        }
        Ok(())
    }

    /// The tantivy commit proper, isolated so tests can inject a
    /// failure at exactly this point.
    fn commit_writer(&self, slot: &mut WriterSlot) -> Result<(), IndexError> {
        #[cfg(test)]
        if std::mem::take(&mut slot.fault_injection.fail_next_commit) {
            return Err(IndexError::Commit("injected commit failure".to_string()));
        }
        self.ensure_writer(slot)?
            .commit()
            .map(|_opstamp| ())
            .map_err(|e| IndexError::Commit(e.to_string()))
    }

    /// Hand back the live writer, reopening it if a previous fault left
    /// the slot empty.  A failed reopen keeps the slot empty and the
    /// zone `available: false` on Health — every mutation keeps failing
    /// loudly rather than being buffered nowhere (#4725).
    fn ensure_writer<'s>(
        &self,
        slot: &'s mut WriterSlot,
    ) -> Result<&'s mut IndexWriter, IndexError> {
        if slot.writer.is_none() {
            match open_writer(&self.index) {
                Ok(fresh) => {
                    slot.writer = Some(fresh);
                    self.status.lock().available = true;
                    tracing::warn!(
                        "fts writer reopened after an earlier fault — unverified until the \
                         next successful write"
                    );
                }
                Err(e) => {
                    let mut status = self.status.lock();
                    status.available = false;
                    if status.last_fault.is_none() {
                        status.last_fault = Some(WriterFault {
                            at: Instant::now(),
                            detail: format!("writer reopen failed: {e}"),
                        });
                    }
                    return Err(IndexError::WriterUnavailable(e.to_string()));
                }
            }
        }
        slot.writer
            .as_mut()
            .ok_or_else(|| IndexError::WriterUnavailable("writer slot empty".to_string()))
    }

    /// Record a writer fault and replace the writer (#4725).  The
    /// faulted writer is dropped FIRST: tantivy holds an exclusive
    /// per-directory writer lock, so a replacement cannot open while it
    /// lives.  Dropping joins its remaining indexing threads and
    /// releases the lock; the uncommitted ops it held belong to the
    /// transaction failing right now, so nothing acknowledged is
    /// discarded.  When the reopen fails too the slot stays empty and
    /// Health reports the zone unavailable; every later mutation
    /// retries the reopen.
    fn fault(&self, slot: &mut WriterSlot, detail: &str) {
        slot.writer = None;
        slot.probe = None;
        slot.lost_ops = 0;
        let (available, detail) = match open_writer(&self.index) {
            Ok(fresh) => {
                slot.writer = Some(fresh);
                (
                    true,
                    format!(
                        "{detail}; writer rebuilt — unverified until the next successful write"
                    ),
                )
            }
            Err(e) => (
                false,
                format!(
                    "{detail}; writer rebuild failed ({e}) — writes fail until a reopen succeeds"
                ),
            ),
        };
        tracing::error!(available, detail = %detail, "fts writer fault");
        let mut status = self.status.lock();
        status.available = available;
        status.last_fault = Some(WriterFault {
            at: Instant::now(),
            detail,
        });
    }

    /// Snapshot of the writer's liveness for the Health RPC (#4725).
    /// Reads the side mutex `commit()` maintains — never the writer
    /// lock — so a poll cannot queue behind an in-flight commit.
    pub fn writer_status(&self) -> WriterStatus {
        self.status.lock().clone()
    }

    /// Test seam (#4725): make `add_document` accept documents without
    /// handing them to tantivy — the zero-worker writer shape.
    #[cfg(test)]
    fn inject_drop_adds(&self, on: bool) {
        self.writer.lock().fault_injection.drop_adds = on;
    }

    /// Test seam (#4725): fail the next `commit()` before tantivy runs.
    #[cfg(test)]
    fn inject_fail_next_commit(&self) {
        self.writer.lock().fault_injection.fail_next_commit = true;
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
        // Natural-language queries routinely contain tantivy syntax by
        // accident ("confirm - what did ...", an unbalanced quote, a
        // stray ':' or '[').  The strict parser rejects the whole query,
        // and because the hybrid path joins this leg with the semantic
        // leg, one dash used to fail the entire hybrid RPC with
        // "parse query ...: Syntax Error" even though the ANN leg had
        // the answer (LongMemEval 58470ed2).  Try strict first so
        // deliberate operator syntax keeps its exact semantics; on a
        // syntax error fall back to tantivy's lenient parser, which
        // drops the offending tokens and keeps the rest of the terms.
        let parsed = match parser.parse_query(query) {
            Ok(parsed) => parsed,
            Err(strict_err) => {
                let (lenient, lenient_errors) = parser.parse_query_lenient(query);
                tracing::debug!(
                    query,
                    strict_error = %strict_err,
                    lenient_errors = lenient_errors.len(),
                    "fts query did not parse strictly; using lenient parse"
                );
                lenient
            }
        };

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
    /// The writer faulted and could not be reopened (#4725); the
    /// mutation was NOT queued.  Callers treat it as transient.
    #[error("fts writer unavailable: {0}")]
    WriterUnavailable(String),
    /// tantivy acknowledged the transaction but the probe document is
    /// not searchable — or some mutation never reached the writer.
    /// The writer has been rebuilt; the caller must NOT acknowledge
    /// the batch (#4725).
    #[error("fts writer lost the write: {0}")]
    LostWrite(String),
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
    fn natural_language_syntax_falls_back_to_lenient_parse() {
        // A dash surrounded by spaces, an unbalanced quote, a stray
        // colon: all strict-parser syntax errors that a user question
        // carries by accident.  They must not fail the query (and, via
        // the hybrid join, the whole hybrid RPC) — the remaining terms
        // still search.  Deliberate, well-formed syntax keeps working.
        let dir = tempdir().join("fts");
        let idx = FtsIndex::open_or_create(dir).expect("open");
        idx.add_document(
            "/lme/borges.md",
            0,
            "Borges wrote that the Library is a sphere whose exact center is any hexagon",
            Some(1),
        )
        .expect("add");
        idx.add_document(
            "/lme/other.md",
            0,
            "an unrelated note about gardening",
            Some(2),
        )
        .expect("add");
        idx.commit().expect("commit");

        for query in [
            "I wanted to confirm - what did Borges say about the center of the Library?",
            "what did Borges say about the \"center of the Library",
            "Borges: center of the Library [sphere]",
        ] {
            let hits = idx
                .search(query, 10, None)
                .expect("lenient search must not fail");
            assert!(
                hits.iter().any(|h| h.path == "/lme/borges.md"),
                "query {query:?}: expected the Borges doc, got {hits:?}"
            );
        }
        // Well-formed operator syntax is still honoured strictly.
        let hits = idx
            .search("Borges -gardening", 10, None)
            .expect("strict search");
        assert_eq!(hits.len(), 1, "{hits:?}");
        assert_eq!(hits[0].path, "/lme/borges.md");
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

    // ── Writer liveness (#4725) ─────────────────────────────────

    #[test]
    fn lost_write_is_reported_and_writer_rebuilt() {
        let dir = tempdir().join("fts");
        let idx = FtsIndex::open_or_create(dir).expect("open");
        assert!(idx.writer_status().available);
        assert!(idx.writer_status().last_fault.is_none());

        // A writer that acks adds it never indexes — the zero-worker
        // shape a failed worker respawn leaves behind.
        idx.inject_drop_adds(true);
        idx.add_document("/ghost.md", 0, "ghost text", Some(1))
            .expect("the add is accepted, as it was in #4725");
        let err = idx
            .commit()
            .expect_err("commit must not acknowledge a write that is not searchable");
        assert!(matches!(err, IndexError::LostWrite(_)), "{err}");
        let status = idx.writer_status();
        assert!(status.available, "rebuild succeeds on a healthy host");
        let fault = status.last_fault.expect("fault recorded for Health");
        assert!(fault.detail.contains("/ghost.md"), "{}", fault.detail);

        // Real writer again: the next transaction lands and clears the
        // fault; the ghost never became searchable.
        idx.inject_drop_adds(false);
        idx.add_document("/real.md", 0, "real text", Some(2))
            .expect("add after rebuild");
        idx.commit().expect("commit after rebuild");
        assert_eq!(idx.search("real", 10, None).expect("search").len(), 1);
        assert!(
            idx.writer_status().last_fault.is_none(),
            "a probe-verified commit clears the fault"
        );
        assert!(
            idx.writer_status().last_verified_commit.is_some(),
            "a probe-verified commit is timestamped for Health"
        );
        assert!(idx
            .get_chunks_by_path("/ghost.md")
            .expect("lookup")
            .is_empty());
    }

    #[test]
    fn commit_failure_rebuilds_writer_and_next_write_lands() {
        let dir = tempdir().join("fts");
        let idx = FtsIndex::open_or_create(dir).expect("open");
        idx.add_document("/a.md", 0, "alpha", Some(1)).expect("add");
        idx.inject_fail_next_commit();
        let err = idx.commit().expect_err("injected failure");
        assert!(matches!(err, IndexError::Commit(_)), "{err}");
        let status = idx.writer_status();
        assert!(status.available);
        assert!(status.last_fault.is_some());

        // The failed transaction's ops went down with the faulted
        // writer — the caller retries, exactly as the error told it.
        idx.add_document("/a.md", 0, "alpha", Some(1))
            .expect("add after rebuild");
        idx.commit().expect("commit after rebuild");
        assert_eq!(idx.search("alpha", 10, None).expect("search").len(), 1);
        assert!(idx.writer_status().last_fault.is_none());
    }

    #[test]
    fn add_then_delete_same_path_in_one_transaction_is_not_a_lost_write() {
        let dir = tempdir().join("fts");
        let idx = FtsIndex::open_or_create(dir).expect("open");
        idx.add_document("/tmp.md", 0, "temporary", Some(1))
            .expect("add");
        idx.delete_all_chunks("/tmp.md");
        idx.commit()
            .expect("a path whose final op is a delete is legitimately absent");
        assert!(idx.writer_status().last_fault.is_none());
        assert!(idx
            .get_chunks_by_path("/tmp.md")
            .expect("lookup")
            .is_empty());
    }

    #[test]
    fn delete_only_commit_does_not_clear_an_unverified_fault() {
        let dir = tempdir().join("fts");
        let idx = FtsIndex::open_or_create(dir).expect("open");
        idx.inject_drop_adds(true);
        idx.add_document("/x.md", 0, "x", Some(1)).expect("add");
        idx.commit().expect_err("lost write");
        idx.inject_drop_adds(false);
        idx.delete_all_chunks("/x.md");
        idx.commit().expect("delete-only commit");
        assert!(
            idx.writer_status().last_fault.is_some(),
            "only an add that becomes searchable proves the writer indexes again"
        );
    }
}
