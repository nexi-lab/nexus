# Drop txtai in favor of direct pgvector + pgtext SQL

**Issue**: [#3699](https://github.com/nexi-lab/nexus/issues/3699)
**Date**: 2026-05-03
**Status**: Draft (awaiting plan)

## Problem

`txtai_backend.py` (2169 lines) is a thin abstraction over pgvector + pgtext we've
spent ~500 lines wrapping with workarounds. It pulls torch + transformers
(~500 MB) for an embedding path we drive ourselves via litellm. BM25 already
moved off txtai (#3997). Time to remove the dependency.

## Goal

Replace `TxtaiBackend` with profile-appropriate direct backends behind one
`SearchBackend` protocol. Retire `bm25s_search.py` simultaneously since SQLite
FTS5 has native `bm25()` ranking. Hard cutover, single PR.

## Non-Goals

- Cross-encoder reranking (drop; can re-add later without txtai)
- Graph-mode search (drop; can re-add later directly on pgvector)
- Multi-lingual tokenizer support (matches current txtai-era limitation)
- Multi-vector / late-interaction (out of scope)

---

## Architecture

### Backend selection by profile

In `daemon.py`, at startup:

```
database_url contains "postgresql"  → PgFtsBackend + PgVectorBackend
otherwise (SQLite sandbox profile)  → SqliteFtsBackend + sqlite_vec_backend
```

Profile alignment is explicit: full profile = Postgres + Pg* backends; sandbox
profile = SQLite + sqlite_* backends.

### Single protocol

`src/nexus/bricks/search/protocols.py` adds (already landed in T1, commit
`ba7121bdb`):

```python
class SearchBackend(Protocol):
    async def add(self, docs: Sequence[dict[str, Any]], *, zone_id: str) -> int
    async def upsert(self, docs: Sequence[dict[str, Any]], *, zone_id: str) -> int
    async def delete(self, ids: Sequence[str], *, zone_id: str) -> int
    async def keyword_search(
        self, query: str, path: str, k: int, zone_id: str
    ) -> list[BaseSearchResult]
    async def semantic_search(
        self, query_vector: Sequence[float], path: str, k: int, zone_id: str
    ) -> list[BaseSearchResult]
    async def startup(self) -> None
    async def shutdown(self) -> None
```

`PgFtsBackend` adds a non-protocol method `keyword_search_pages(...)` for
the page-BM25 leg (`#3980` parity); see Data Flow §Read — keyword (page).
The protocol stays minimal because page-BM25 is a Postgres-only
optimization — `SqliteFtsBackend` may not implement it.

Hits use `BaseSearchResult` (`results.py`); writes accept dict shape
matching `chunk_store.ChunkRecord`. No `hybrid_search` method on the
protocol — hybrid mode is the daemon's concern.

### Files

**New**:

- `src/nexus/bricks/search/pg_fts_backend.py` (~150 LOC)
- `src/nexus/bricks/search/pg_vector_backend.py` (~200 LOC)
- `src/nexus/bricks/search/sqlite_fts_backend.py` (~150 LOC)
- `src/nexus/bricks/search/embedding_client.py` (~80 LOC)

**Modified**:

- `src/nexus/bricks/search/daemon.py` — backend selection by profile, lazy
  re-embed task spawn, drop txtai imports
- `src/nexus/bricks/search/__init__.py` — export new backends, drop
  `TxtaiBackend`, `bm25s_*`
- `src/nexus/bricks/search/protocols.py` — add `SearchBackend`
- `src/nexus/bricks/search/manifest.py` — drop txtai entry
- `src/nexus/bricks/search/scope_ops.py` — drop txtai-bootstrap doc references
- `pyproject.toml` — drop `txtai`, `faiss-cpu` from `semantic-search` and `all`
  extras; matryoshka 1024 stays default
- `alembic/versions/<new>_drop_txtai_add_search_columns.py` — schema migration

**Deleted**:

- `src/nexus/bricks/search/txtai_backend.py` (2169 LOC)
- `src/nexus/bricks/search/bm25s_search.py` (883 LOC)

**Extracted before delete**: `_aggregate_chunks_to_pages` from `txtai_backend.py`
moves to `result_builders.py` (page aggregation is not txtai-specific).

**Net delta**: −3052 LOC / ~+500 LOC ≈ **~−2550 LOC** (revised down from
+580 after the schema audit revealed half of PgVectorBackend already lives
in `daemon.py:2010-2021`).

### Why no Postgres alembic schema work

The earlier draft of this spec called for adding `tsv` + `embedding`
columns + GIN/HNSW indexes. Audit of the live model (`DocumentChunkModel`
and prior migrations) showed all of these already exist:

- `embedding halfvec(1536)` + `idx_chunks_embedding_hnsw` were added
  before this issue was filed.
- `idx_chunks_bm25 ON document_chunks USING bm25(chunk_text)` was added
  by `add_pg_textsearch_bm25_index.py` and is currently dead code.
- `chunk_text` is the body column already; we don't need a redundant
  `tsv` GIN given true BM25 is already provisioned.

So the new alembic migration only adds the SQLite FTS5 vtable + sync
triggers; the Postgres branch is `pass`. This is a significant scope
reduction relative to the first draft.

---

## Schema & Migration

### Postgres branch — no schema change

After auditing the live model (`DocumentChunkModel` in
`src/nexus/storage/models/filesystem.py`), Postgres already has everything
the new backends need:

- `document_chunks.embedding halfvec(1536)` (added pre-Issue #3699; not declared
  in the ORM, written via raw SQL by `chunk_store.py`)
- `idx_chunks_embedding_hnsw` HNSW index on `embedding` (halfvec_cosine_ops,
  m=24, ef_construction=128)
- `idx_chunks_bm25 ON document_chunks USING bm25(chunk_text)` from
  `add_pg_textsearch_bm25_index.py` — already provisioned via
  `pg_textsearch` (Tiger BM25, true k1+b ranking). **Currently dead code** —
  no Python references it. PgFtsBackend wraps it.
- `chunk_text` is the body column; `chunk_id` is PK.
- Zone + path live on `file_paths`. Every search query JOINs
  `document_chunks → file_paths` via `path_id`. The covering index
  `idx_file_paths_zone_path_covering(zone_id, virtual_path)` makes this
  cheap.

**The Postgres branch of the new alembic migration is empty.** No new
columns, no new indexes. The migration file exists only for the SQLite
branch (below) so Nexus deployments that share one alembic chain can run
`alembic upgrade head` regardless of dialect.

Embedding dimension is **1536** (`halfvec(1536)`) — matches commit
`340e39410`'s actual default. `text-embedding-3-large` truncates via the
OpenAI `dimensions` parameter (matryoshka) to fit the column; `3-small`
is 1536 native. The earlier draft of this spec said 1024; that was a
misread of the matryoshka commit.

### SQLite branch

Reuses existing `sqlite_vec_backend.py` machinery for vectors. Adds an FTS5
virtual table mirroring `document_chunks.chunk_text`:

```sql
CREATE VIRTUAL TABLE document_chunks_fts USING fts5(
  chunk_text,
  content='document_chunks',
  content_rowid='rowid',
  tokenize='porter unicode61'
);
-- triggers: AFTER INSERT/UPDATE/DELETE on document_chunks → sync to fts table
```

`document_chunks.chunk_id` is a TEXT UUID; the FTS5 vtable's
`content_rowid` points at SQLite's implicit integer rowid. Triggers use
`NEW.rowid` / `OLD.rowid` to bind. We never query by rowid from
Python — search results JOIN back through `chunk_id` (text). Triggers
keep `document_chunks_fts` in sync. FTS5 `bm25()` provides ranking
(replaces `bm25s_search.py`).

### Hard cutover

Single PR:

1. Alembic migration — SQLite branch only (FTS5 vtable + triggers); Postgres
   branch is a no-op. Runs in seconds.
2. Daemon backend init swaps `TxtaiBackend` → profile-appropriate pair.
3. Same PR deletes `txtai_backend.py`, `bm25s_search.py`, txtai-specific tests.
4. `pyproject.toml` drops `txtai`, `faiss-cpu`. `torch` / `transformers` /
   `sympy` / `huggingface_hub` fall out as transitive consequence.

**No lazy backfill task** — `chunk_store.replace_document_chunks()` already
writes `embedding` inline alongside `chunk_text`. There is no NULL-embedding
backlog to drain on cutover; old rows that were embedded by txtai still
have their vectors in `document_chunks.embedding` (txtai wrote them via
its `pgvector` backend, which used the same column).

**Rollback**: forward-only by intent. Reverting requires restoring deleted
files from git + dropping the SQLite FTS5 vtable. Postgres schema is
unchanged so no DDL rollback needed there.

---

## Components & Data Flow

### Write path — owned by `chunk_store`, not by backends

```
mutation_resolver
  → indexing_service.index_document(path)
    → indexing_pipeline._chunk_document() / _embed_*()
    → chunk_store.replace_document_chunks(path_id, records)
      # writes chunk_text + embedding (CAST AS halfvec) in one INSERT.
      # Per-directory scope (#3698) gates whether `records[i].embedding`
      # is populated; chunk_text is always indexed (BM25 always works).
```

**Backend `add` / `upsert` / `delete` are pass-throughs** that delegate to
`chunk_store` — they exist only to satisfy the `SearchBackend` protocol.
There is no second write path. This avoids the txtai-era split where
`chunk_store` wrote chunks AND `txtai.add()` wrote a parallel index.

The `idx_chunks_bm25` index updates automatically on `chunk_text` insert
(pg_textsearch maintains it). HNSW on `embedding` updates automatically
on `embedding` insert.

### Read — keyword mode (chunk-level BM25)

```
daemon.search(q, path, k, search_mode="keyword")
  → backend.keyword_search(q, path, k, zone_id)
    PgFts:
      SELECT c.chunk_id, fp.virtual_path AS path, c.chunk_text,
             c.chunk_index, paradedb.score(c.chunk_id) AS score
      FROM document_chunks c
      JOIN file_paths fp ON c.path_id = fp.path_id
      WHERE c.chunk_text @@@ :q
        AND fp.zone_id = :zone_id
        AND fp.virtual_path LIKE :prefix || '%'
        AND fp.deleted_at IS NULL
      ORDER BY score DESC LIMIT :k
    SqliteFts:
      SELECT dc.chunk_id, fp.virtual_path AS path, dc.chunk_text,
             dc.chunk_index, bm25(document_chunks_fts) AS score
      FROM document_chunks_fts
        JOIN document_chunks dc ON dc.chunk_id_rowid = document_chunks_fts.rowid
        JOIN file_paths fp ON dc.path_id = fp.path_id
      WHERE document_chunks_fts MATCH :q
        AND fp.zone_id = :zone_id
        AND fp.virtual_path LIKE :prefix || '%'
        AND fp.deleted_at IS NULL
      ORDER BY score LIMIT :k
```

(`chunk_text @@@ :q` is the `pg_textsearch` BM25 match operator;
`paradedb.score(...)` returns the BM25 score. If the actual operator/score
function differs in the installed version, adapt at impl time — see
`add_pg_textsearch_bm25_index.py` for the index DDL the operator binds to.)

### Read — keyword mode (page-level BM25, `#3980` parity)

Issue #3980 added a page-level BM25 leg to recover rare-phrase signal that
chunk-level BM25 dilutes. txtai today runs **two** BM25 legs (chunk +
page); we replicate the second leg in PgFtsBackend:

```
backend.keyword_search_pages(q, path, k, zone_id)
  PgFts:
    -- aggregate chunks → pages, then BM25 on the assembled text
    WITH pages AS (
      SELECT fp.path_id, fp.virtual_path, fp.zone_id,
             string_agg(c.chunk_text, ' ' ORDER BY c.chunk_index) AS page_text
      FROM document_chunks c
      JOIN file_paths fp ON c.path_id = fp.path_id
      WHERE fp.zone_id = :zone_id
        AND fp.virtual_path LIKE :prefix || '%'
        AND fp.deleted_at IS NULL
      GROUP BY fp.path_id, fp.virtual_path, fp.zone_id
    )
    SELECT path_id, virtual_path AS path, page_text, score FROM (
      SELECT *, paradedb.score(path_id) AS score
      FROM pages
      WHERE page_text @@@ :q
    ) ranked
    ORDER BY score DESC LIMIT :k
```

(Implementation may instead reuse the existing
`_aggregate_chunks_to_pages` helper from `result_builders.py` to do
aggregation client-side over the chunk-BM25 result set, matching today's
txtai pattern. Choose at impl time based on which is faster on the
profile DB; correctness is the same either way.)

### Read — semantic mode

```
daemon.search(q, path, k, search_mode="semantic")
  → embedding_client.embed_query(q) → qvec
  → backend.semantic_search(qvec, path, k, zone_id)
    PgVector:
      SELECT c.chunk_id, fp.virtual_path AS path, c.chunk_text,
             c.chunk_index, 1 - (c.embedding <=> CAST(:qvec AS halfvec)) AS score
      FROM document_chunks c
      JOIN file_paths fp ON c.path_id = fp.path_id
      WHERE c.embedding IS NOT NULL
        AND fp.zone_id = :zone_id
        AND fp.virtual_path LIKE :prefix || '%'
        AND fp.deleted_at IS NULL
      ORDER BY c.embedding <=> CAST(:qvec AS halfvec) LIMIT :k
```

This SQL is lifted near-verbatim from `daemon.py:2010-2021` — half of
PgVectorBackend already exists in the codebase, just unwrapped.

### Read — hybrid mode (3-way RRF)

```
daemon.search(q, path, k, search_mode="hybrid")
  → asyncio.gather(
      backend.keyword_search(q, path, k * 2, zone_id),         # chunk-BM25
      backend.keyword_search_pages(q, path, k * 2, zone_id),   # page-BM25 (#3980)
      backend.semantic_search(qvec, path, k * 2, zone_id),     # dense
    )
  → fusion.rrf_fusion_3way(chunk_hits, page_hits, dense_hits, k=60)[:k]
```

Three legs to match today's txtai behavior: chunk-BM25, page-BM25
(`#3980`), and dense. RRF fuses all three via `fusion.py`. If
`fusion.py` only has 2-way `rrf_fusion`, add a 3-way variant or call it
twice (chunk+page → mid → mid+dense).

Over-fetch `k * 2` per leg for fusion headroom.

### Embedding client

`embedding_client.py`:

- `EmbeddingClient(model: str, cache: EmbeddingCache | None)`
- `async embed_query(text) -> list[float]`
- `async embed_batch(texts: list[str]) -> list[list[float]]`
  - Batches up to `NEXUS_EMBED_BATCH` (default 100)
  - Retries 429/5xx with exp backoff (3 attempts, base 1s)
  - Honors embedding cache (existing brick)
- Direct `litellm.aembedding(...)` calls. No monkey-patching `litellm.vectors.dense`.

Replaces `_configure_litellm` from `txtai_backend.py:383-420`.

### Backfill

**Not needed.** `chunk_store.replace_document_chunks()` already writes
`embedding` inline alongside `chunk_text`. txtai's pgvector backend wrote
to the same `document_chunks.embedding halfvec(1536)` column, so the
existing corpus already has its vectors. No NULL-embedding backlog at
cutover.

If, in some operational scenario, embeddings need to be regenerated
(model change, dim change, corruption), use the existing
`reindex` command rather than building a new background task in the
daemon.

---

## Error Handling & Concurrency

### Carried-over txtai pain (resolved)

| txtai pain                            | Resolution                                                |
|---------------------------------------|-----------------------------------------------------------|
| `_escape_sql_string` (Lark quirks)    | Parameterized SQL. Gone.                                  |
| `LIKE ... ESCAPE` removed             | Parameterized `path LIKE :p || '%'`. Gone.                |
| `similar()+path LIKE` filter-order    | WHERE before ORDER. Gone.                                 |
| Semicolon-in-query crash              | Parameterized. Gone.                                      |
| `PendingRollbackError` cascades       | Per-call session via `async with session_factory()`.      |
| `_save()` lock contention             | Rows persist on UPSERT. No save/load. Gone.               |
| `config.json` drift                   | No txtai config file. Gone.                               |
| `_rollback_db_sessions()` workaround  | Per-call session scope. Gone.                             |

### Still must handle

1. **Embedding API failures (429/5xx)** — retry exp backoff in `EmbeddingClient`.
   After exhaustion, log + skip (leaves row's `embedding` NULL → next backfill
   pass picks it up). Never crash daemon.

2. **HNSW index rebuild during heavy writes** — pgvector handles concurrent
   writes natively. SQLite-vec's per-loop asyncio locks (#3976) preserved.

3. **Backfill task crashes/restarts** — task is idempotent (predicate
   `embedding IS NULL`). Resumes on next daemon restart. No checkpoint file.

4. **Migration mid-backfill failure** — backfill runs **post**-migration, not
   in it. Migration only does schema (fast). Backfill failure → daemon serves
   keyword search at full quality + partial semantic.

5. **Connection pool exhaustion under concurrent search** — backends use
   existing `async_session_factory`. No new pools. Honors existing
   `db_pool_size` config.

### Concurrency model

- **Reads**: stateless. Each search method opens its own session. No
  backend-level locks.
- **Writes**: per-zone `asyncio.Lock` to serialize bulk upserts (matches
  `sqlite_vec_backend.py` from #3976). One lock per backend instance.
- **Backfill**: single bg coroutine per daemon. No locks during embedding API
  call. Acquires zone lock only for UPDATE. Yields between batches.

---

## Testing

### Layers

1. **Protocol conformance** — `tests/unit/bricks/search/test_search_backend_protocol.py`.
   Parameterized over all 4 backends. Verifies each satisfies `SearchBackend`.
   Single test file, same suite per backend.

2. **Per-backend unit tests** (new):
   - `test_pg_fts_backend.py` — Postgres testcontainer. Add/upsert/delete,
     path-prefix filter, tsv ranking, parameterized injection-safe (semicolon /
     smart quotes pass unchanged).
   - `test_pg_vector_backend.py` — Postgres + pgvector. Cosine ordering,
     NULL-embedding skipped, HNSW recall sanity (top-1 of identical vector).
   - `test_sqlite_fts_backend.py` — in-memory SQLite. FTS5 `bm25()` ranking,
     trigger-sync on UPDATE/DELETE.
   - `test_sqlite_vec_backend.py` — existing, unchanged.

3. **Integration** (`tests/integration/bricks/search/`):
   - `test_daemon_search_pg.py` — end-to-end Postgres. All three modes,
     per-directory scope, lazy backfill triggers + completes.
   - `test_daemon_search_sqlite.py` — same, SQLite profile.
   - `test_migration_cutover.py` — load fixture with old schema, run alembic
     upgrade, verify tsv populates + embedding NULL + indexes built.

4. **Fusion** (existing) — `fusion.py` tests already cover RRF. Re-verify
   daemon call signature matches.

5. **Regression delete** — remove `test_txtai_backend_*.py`,
   `test_txtai_reranker.py`, `test_bootstrap_filter_shape.py`,
   `test_page_aggregation.py` (port internal-fn imports to extracted helper in
   `result_builders.py`).

### Benchmark gate

`tests/benchmarks/gbrain_eval.py` (new, manual):

- Pulls [gbrain-evals](https://github.com/garrytan/gbrain-evals) corpus.
- Runs new daemon end-to-end. Compares `recall@5` / `NDCG@5` against issue
  baseline (0.95 / 0.90).
- **Pre-merge gate**: must run, results posted in PR. Not CI (API cost).
- `make bench-search` Makefile target.

### TDD cycle per backend

Contract test red → impl one method → green → next method. Backend done when
full conformance suite passes against real DB (testcontainer).

---

## Risks & Unknowns

- **Lazy backfill window** — semantic recall degrades for un-embedded paths
  until backfill completes. Mitigation: BM25 path always at full quality;
  hybrid mode falls back gracefully (RRF with one empty leg = keyword-only).

- **PG FTS tokenizer English-only** — same limitation txtai inherited. Document
  in release notes; plan multi-lingual via `simple` config or ICU as separate
  issue if needed.

- **`ts_rank_cd` ≠ true BM25** — pgtext uses TF-IDF variant. Already in prod
  today (issue notes); no behavioral change.

- **HNSW build time on large corpora** — index built post-migration during
  first backfill. Acceptable: search keeps working without it (sequential
  scan), HNSW kicks in after first ANALYZE.

- **gbrain-evals API cost** — manual gate keeps cost bounded. Budget per run
  documented in PR.

## Related

- #3698 (closed) — per-directory semantic index scope. Already integrates with
  this design (write-path gate).
- #3997 — BM25-only fast-path. New design preserves this codepath; daemon
  keeps the fast-path branch for keyword-only mode.
- #3980 — page-level BM25 + RRF. Page aggregation extracted to
  `result_builders.py` before txtai delete; behavior preserved.
- #2913, #2916, #2973 (closed) — historical txtai bugs that motivated this
  rewrite.
