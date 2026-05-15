# Issue #3699 — Drop txtai for direct pgvector + pgtext SQL

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `TxtaiBackend` with profile-appropriate direct backends (Postgres pgvector + pgtext, SQLite FTS5 + sqlite-vec) behind one `SearchBackend` protocol; retire `bm25s_search.py` simultaneously. Single PR, hard cutover.

**Architecture:** One `SearchBackend` protocol; daemon picks `(PgFtsBackend, PgVectorBackend)` for Postgres or `(SqliteFtsBackend, SqliteVecBackend)` for SQLite by inspecting `database_url`. Schema reuses `document_chunks` (adds generated `tsv` column + `vector(1024)` `embedding` column). Lazy background re-embed task fills `embedding` post-migration. Hybrid mode = daemon `asyncio.gather` over keyword + semantic legs, fused via existing `fusion.rrf_fusion`.

**Tech Stack:** Python 3.13 / asyncpg / SQLAlchemy / pgvector / sqlite-vec / FTS5 / litellm / Alembic / pytest + testcontainers.

**Spec:** `docs/superpowers/specs/2026-05-03-drop-txtai-design.md`

---

## File Structure

**New** (`src/nexus/bricks/search/`):
- `pg_fts_backend.py` — Postgres BM25-style ranking via `tsvector` + `ts_rank_cd`. ~150 LOC.
- `pg_vector_backend.py` — Postgres dense via pgvector HNSW + cosine. ~200 LOC.
- `sqlite_fts_backend.py` — SQLite FTS5 virtual table + `bm25()` ranking. ~150 LOC.
- `embedding_client.py` — direct `litellm.aembedding` wrapper with batching, retries, cache hookup. ~80 LOC.

**Modified** (`src/nexus/bricks/search/`):
- `protocols.py` — add `SearchBackend` protocol.
- `result_builders.py` — receive `_aggregate_chunks_to_pages` extracted from txtai_backend.
- `daemon.py` — backend selection by profile (replaces `TxtaiBackend(...)` block at L653-687); spawn `_lazy_reembed_task`; drop txtai imports.
- `__init__.py` — drop `TxtaiBackend`, `bm25s_*` exports; add new backends.
- `manifest.py` — drop `nexus.bricks.search.txtai_backend` entry.
- `scope_ops.py` — strip `txtai`-specific docstring references.

**Created (DB)**:
- `alembic/versions/<hash>_add_search_columns_drop_txtai.py` — adds `tsv` + `embedding` columns + indexes; SQLite branch creates FTS5 virtual table + sync triggers.

**Modified (project)**:
- `pyproject.toml` — drop `txtai[database,graph]`, `faiss-cpu` from `semantic-search` and `all` extras.
- `Makefile` — add `bench-search` target.

**New (tests)**:
- `tests/unit/bricks/search/test_search_backend_protocol.py`
- `tests/unit/bricks/search/test_pg_fts_backend.py`
- `tests/unit/bricks/search/test_pg_vector_backend.py`
- `tests/unit/bricks/search/test_sqlite_fts_backend.py`
- `tests/unit/bricks/search/test_embedding_client.py`
- `tests/integration/bricks/search/test_daemon_search_pg.py`
- `tests/integration/bricks/search/test_daemon_search_sqlite.py`
- `tests/integration/bricks/search/test_migration_cutover.py`
- `tests/benchmarks/gbrain_eval.py`

**Deleted**:
- `src/nexus/bricks/search/txtai_backend.py` (2169 LOC)
- `src/nexus/bricks/search/bm25s_search.py` (883 LOC)
- `tests/unit/bricks/search/test_txtai_backend_*.py`
- `tests/integration/bricks/search/test_txtai_reranker.py`
- `tests/unit/bricks/search/test_bootstrap_filter_shape.py`
- `tests/unit/bricks/search/test_page_aggregation.py` (replaced by tests on extracted helper in `result_builders.py`)

---

## Task 1: Add `SearchBackend` protocol

**Files:**
- Modify: `src/nexus/bricks/search/protocols.py`
- Test: `tests/unit/bricks/search/test_search_backend_protocol.py` (new)

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/bricks/search/test_search_backend_protocol.py
"""Conformance test: every backend satisfies SearchBackend.

Parameterized so the same suite runs against pg/sqlite backends as they
land in later tasks. Initially only the protocol shape is checked.
"""

from typing import Protocol, runtime_checkable

import pytest

from nexus.bricks.search.protocols import SearchBackend


def test_search_backend_protocol_is_runtime_checkable():
    assert isinstance(SearchBackend, type)
    # runtime_checkable enables isinstance() checks
    assert hasattr(SearchBackend, "_is_runtime_protocol")


def test_search_backend_protocol_has_required_methods():
    required = {
        "add",
        "upsert",
        "delete",
        "keyword_search",
        "semantic_search",
        "startup",
        "shutdown",
    }
    actual = {m for m in dir(SearchBackend) if not m.startswith("_")}
    missing = required - actual
    assert not missing, f"SearchBackend missing methods: {missing}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/bricks/search/test_search_backend_protocol.py -v`
Expected: FAIL with `ImportError: cannot import name 'SearchBackend'`.

- [ ] **Step 3: Add the protocol**

Append to `src/nexus/bricks/search/protocols.py`:

```python
from collections.abc import Sequence


@runtime_checkable
class SearchBackend(Protocol):
    """Unified backend contract for keyword + semantic search.

    Hybrid fusion is the daemon's responsibility (see fusion.rrf_fusion);
    backends only expose the two single-mode primitives.
    """

    async def add(self, docs: Sequence[dict[str, Any]], *, zone_id: str) -> int: ...

    async def upsert(self, docs: Sequence[dict[str, Any]], *, zone_id: str) -> int: ...

    async def delete(self, ids: Sequence[str], *, zone_id: str) -> int: ...

    async def keyword_search(
        self,
        query: str,
        path: str,
        k: int,
        zone_id: str,
    ) -> list[BaseSearchResult]: ...

    async def semantic_search(
        self,
        query_vector: Sequence[float],
        path: str,
        k: int,
        zone_id: str,
    ) -> list[BaseSearchResult]: ...

    async def startup(self) -> None: ...

    async def shutdown(self) -> None: ...
```

Update `__all__`:

```python
__all__ = ["FileReaderProtocol", "SearchableProtocol", "SearchBackend"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/bricks/search/test_search_backend_protocol.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/bricks/search/protocols.py tests/unit/bricks/search/test_search_backend_protocol.py
git commit -m "feat(#3699): add SearchBackend protocol for unified keyword + semantic contract"
```

---

## Task 2: Extract `_aggregate_chunks_to_pages` to `result_builders.py`

Move-only refactor. Required before deleting `txtai_backend.py`.

**Files:**
- Modify: `src/nexus/bricks/search/result_builders.py`
- Modify: `src/nexus/bricks/search/txtai_backend.py` (re-export shim during migration)
- Modify: `tests/unit/bricks/search/test_page_aggregation.py` (update import path)

- [ ] **Step 1: Locate the function and copy verbatim**

Run: `grep -n "_aggregate_chunks_to_pages" src/nexus/bricks/search/txtai_backend.py`
Read the function (and any helpers it calls) at the located line numbers.

- [ ] **Step 2: Paste into `result_builders.py`**

Append the function (and any private helpers it depends on) to `src/nexus/bricks/search/result_builders.py`. Keep the function name and signature identical.

- [ ] **Step 3: Replace original with re-export**

In `src/nexus/bricks/search/txtai_backend.py`, replace the function body with:

```python
from nexus.bricks.search.result_builders import _aggregate_chunks_to_pages  # noqa: F401
```

(This keeps existing imports inside `txtai_backend.py` working until Task 11 deletes the file.)

- [ ] **Step 4: Update the test import**

In `tests/unit/bricks/search/test_page_aggregation.py`, change:

```python
from nexus.bricks.search.txtai_backend import _aggregate_chunks_to_pages
```

to:

```python
from nexus.bricks.search.result_builders import _aggregate_chunks_to_pages
```

Leave the rest of the test file untouched (the `TxtaiBackend(...)` instantiations remain; those tests die in Task 11 with the file).

- [ ] **Step 5: Run extracted-helper tests**

Run: `uv run pytest tests/unit/bricks/search/test_page_aggregation.py -v -k "aggregate"`
Expected: PASS for any pure-function tests; the `TxtaiBackend` tests in this file may still pass (re-export keeps txtai working).

- [ ] **Step 6: Commit**

```bash
git add src/nexus/bricks/search/result_builders.py src/nexus/bricks/search/txtai_backend.py tests/unit/bricks/search/test_page_aggregation.py
git commit -m "refactor(#3699): move _aggregate_chunks_to_pages to result_builders.py"
```

---

## Task 3: Build `EmbeddingClient`

Direct `litellm.aembedding` wrapper. Replaces `_configure_litellm` monkey-patch.

**Files:**
- Create: `src/nexus/bricks/search/embedding_client.py`
- Test: `tests/unit/bricks/search/test_embedding_client.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/bricks/search/test_embedding_client.py
import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from nexus.bricks.search.embedding_client import EmbeddingClient


@pytest.mark.asyncio
async def test_embed_query_returns_vector():
    fake = AsyncMock(return_value={"data": [{"embedding": [0.1] * 1024}]})
    with patch("litellm.aembedding", fake):
        client = EmbeddingClient(model="text-embedding-3-small")
        vec = await client.embed_query("hello")
    assert len(vec) == 1024
    assert vec[0] == pytest.approx(0.1)


@pytest.mark.asyncio
async def test_embed_batch_chunks_at_max_batch():
    fake = AsyncMock(side_effect=[
        {"data": [{"embedding": [float(i)] * 4} for i in range(2)]},
        {"data": [{"embedding": [float(i + 2)] * 4} for i in range(1)]},
    ])
    with patch("litellm.aembedding", fake):
        client = EmbeddingClient(model="m", max_batch=2, dim=4)
        vecs = await client.embed_batch(["a", "b", "c"])
    assert len(vecs) == 3
    assert fake.await_count == 2  # two batches: [a,b], [c]


@pytest.mark.asyncio
async def test_embed_batch_retries_on_rate_limit():
    err = Exception("RateLimitError: 429")
    success = {"data": [{"embedding": [0.5] * 4}]}
    fake = AsyncMock(side_effect=[err, err, success])
    with patch("litellm.aembedding", fake), patch("asyncio.sleep", AsyncMock()):
        client = EmbeddingClient(model="m", max_batch=10, dim=4, max_retries=3)
        vecs = await client.embed_batch(["a"])
    assert len(vecs) == 1
    assert fake.await_count == 3
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/bricks/search/test_embedding_client.py -v`
Expected: FAIL `ImportError: cannot import name 'EmbeddingClient'`.

- [ ] **Step 3: Implement the client**

Create `src/nexus/bricks/search/embedding_client.py`:

```python
"""Direct litellm.aembedding wrapper (Issue #3699).

Replaces txtai_backend._configure_litellm monkey-patch. Owns batching,
retries, and embedding-cache integration. Backend code calls this; the
backends themselves stay storage-only.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Sequence
from typing import Any

import litellm

logger = logging.getLogger(__name__)


class EmbeddingClient:
    def __init__(
        self,
        model: str,
        *,
        dim: int = 1024,
        max_batch: int | None = None,
        max_retries: int = 3,
        backoff_base: float = 1.0,
        cache: Any | None = None,
    ) -> None:
        self.model = model
        self.dim = dim
        self.max_batch = max_batch or int(os.getenv("NEXUS_EMBED_BATCH", "100"))
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.cache = cache

    async def embed_query(self, text: str) -> list[float]:
        vecs = await self.embed_batch([text])
        return vecs[0]

    async def embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for start in range(0, len(texts), self.max_batch):
            batch = list(texts[start : start + self.max_batch])
            out.extend(await self._call_with_retry(batch))
        return out

    async def _call_with_retry(self, batch: list[str]) -> list[list[float]]:
        attempt = 0
        while True:
            try:
                resp = await litellm.aembedding(
                    model=self.model,
                    input=batch,
                    dimensions=self.dim,
                )
                return [d["embedding"] for d in resp["data"]]
            except Exception as exc:  # noqa: BLE001
                attempt += 1
                if attempt >= self.max_retries:
                    logger.error("embedding failed after %d retries: %s", attempt, exc)
                    raise
                wait = self.backoff_base * (2 ** (attempt - 1))
                logger.warning("embedding retry %d/%d after %.1fs: %s",
                               attempt, self.max_retries, wait, exc)
                await asyncio.sleep(wait)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/bricks/search/test_embedding_client.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/bricks/search/embedding_client.py tests/unit/bricks/search/test_embedding_client.py
git commit -m "feat(#3699): add EmbeddingClient — direct litellm.aembedding wrapper with batching + retries"
```

---

## Task 4: Alembic migration — SQLite FTS5 vtable + triggers

**Postgres branch is empty** (everything we need exists already on
`document_chunks`: `embedding halfvec(1536)` + `idx_chunks_embedding_hnsw`,
plus `idx_chunks_bm25` from `add_pg_textsearch_bm25_index.py`).

**Files:**
- Create: `alembic/versions/<hash>_add_sqlite_search_fts5.py`
- Test: `tests/integration/bricks/search/test_migration_cutover.py`

- [ ] **Step 1: Generate migration skeleton**

Run: `uv run alembic revision -m "add_sqlite_search_fts5"`
Note the generated filename. Open it.

- [ ] **Step 2: Write the failing migration test**

```python
# tests/integration/bricks/search/test_migration_cutover.py
"""Verify the cutover migration adds the SQLite FTS5 vtable + triggers
on SQLite, and is a no-op on Postgres (everything we need already exists).
"""

import pytest
import sqlalchemy as sa


@pytest.mark.asyncio
async def test_postgres_branch_is_noop(postgres_engine):
    """Postgres already has embedding halfvec(1536) + idx_chunks_bm25.
    The migration must not try to add or drop them.
    """
    async with postgres_engine.connect() as conn:
        cols = await conn.run_sync(
            lambda sync: {c["name"] for c in sa.inspect(sync).get_columns("document_chunks")}
        )
        idx = await conn.run_sync(
            lambda sync: {i["name"] for i in sa.inspect(sync).get_indexes("document_chunks")}
        )
    assert "embedding" in cols
    assert "chunk_text" in cols
    assert "idx_chunks_embedding_hnsw" in idx
    assert "idx_chunks_bm25" in idx


def test_sqlite_branch_creates_fts_vtable(sqlite_engine_after_upgrade):
    """SQLite needs a brand new FTS5 vtable + triggers to mirror chunk_text."""
    with sqlite_engine_after_upgrade.connect() as conn:
        tables = {row[0] for row in conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
        )}
        triggers = {row[0] for row in conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        )}
    assert "document_chunks_fts" in tables
    assert {"document_chunks_fts_ai", "document_chunks_fts_ad",
            "document_chunks_fts_au"} <= triggers


def test_sqlite_trigger_syncs_on_insert(sqlite_engine_after_upgrade):
    """Insert a row into document_chunks; expect the FTS vtable to mirror it."""
    with sqlite_engine_after_upgrade.begin() as conn:
        # path_id has FK to file_paths; insert one there too.
        conn.exec_driver_sql(
            "INSERT INTO file_paths (path_id, zone_id, virtual_path, deleted_at) "
            "VALUES ('p1', 'z', '/z/a.txt', NULL)"
        )
        conn.exec_driver_sql(
            "INSERT INTO document_chunks "
            "(chunk_id, path_id, chunk_index, chunk_text, chunk_tokens, created_at) "
            "VALUES ('c1', 'p1', 0, 'the quick brown fox', 4, datetime('now'))"
        )
    with sqlite_engine_after_upgrade.connect() as conn:
        rows = conn.exec_driver_sql(
            "SELECT chunk_text FROM document_chunks_fts WHERE chunk_text MATCH 'quick'"
        ).fetchall()
    assert len(rows) == 1
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/integration/bricks/search/test_migration_cutover.py -v`
Expected: FAIL — vtable + triggers missing on SQLite.

- [ ] **Step 4: Implement the migration**

Edit `alembic/versions/<hash>_add_sqlite_search_fts5.py`:

```python
"""add_sqlite_search_fts5

Revision ID: <hash>
Revises: <prev_hash>
Create Date: 2026-05-03

SQLite branch: create document_chunks_fts FTS5 vtable + sync triggers
mirroring document_chunks.chunk_text. Postgres branch: no-op (HNSW on
embedding halfvec(1536) and pg_textsearch BM25 on chunk_text already
provisioned by prior migrations).

document_chunks.chunk_id is a TEXT UUID, so the FTS5 vtable's
content_rowid points at SQLite's implicit integer rowid. We do not add
a synthetic chunk_id_rowid column because:
  * SQLite assigns rowid automatically to the row that holds the UUID
  * The trigger uses NEW.rowid to bind the FTS row to the source row
  * We never query by rowid from Python — we always JOIN back through
    chunk_id (text), so the rowid stays internal to the FTS sync
"""

from alembic import op
from sqlalchemy import text

revision = "<hash>"
down_revision = "<prev_hash>"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        return  # Postgres has everything already — see add_pg_textsearch_bm25_index.py

    op.execute(text("""
        CREATE VIRTUAL TABLE IF NOT EXISTS document_chunks_fts USING fts5(
          chunk_text,
          content='document_chunks',
          content_rowid='rowid',
          tokenize='porter unicode61'
        )
    """))
    op.execute(text("""
        CREATE TRIGGER IF NOT EXISTS document_chunks_fts_ai
        AFTER INSERT ON document_chunks BEGIN
          INSERT INTO document_chunks_fts(rowid, chunk_text)
          VALUES (NEW.rowid, NEW.chunk_text);
        END
    """))
    op.execute(text("""
        CREATE TRIGGER IF NOT EXISTS document_chunks_fts_ad
        AFTER DELETE ON document_chunks BEGIN
          INSERT INTO document_chunks_fts(document_chunks_fts, rowid, chunk_text)
          VALUES ('delete', OLD.rowid, OLD.chunk_text);
        END
    """))
    op.execute(text("""
        CREATE TRIGGER IF NOT EXISTS document_chunks_fts_au
        AFTER UPDATE OF chunk_text ON document_chunks BEGIN
          INSERT INTO document_chunks_fts(document_chunks_fts, rowid, chunk_text)
          VALUES ('delete', OLD.rowid, OLD.chunk_text);
          INSERT INTO document_chunks_fts(rowid, chunk_text)
          VALUES (NEW.rowid, NEW.chunk_text);
        END
    """))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        return
    op.execute(text("DROP TRIGGER IF EXISTS document_chunks_fts_au"))
    op.execute(text("DROP TRIGGER IF EXISTS document_chunks_fts_ad"))
    op.execute(text("DROP TRIGGER IF EXISTS document_chunks_fts_ai"))
    op.execute(text("DROP TABLE IF EXISTS document_chunks_fts"))
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run alembic upgrade head && uv run pytest tests/integration/bricks/search/test_migration_cutover.py -v`
Expected: 3 passed.

- [ ] **Step 6: Commit**

```bash
git add alembic/versions/ tests/integration/bricks/search/test_migration_cutover.py
git commit -m "feat(#3699): alembic — SQLite FTS5 vtable + triggers (Postgres is no-op)"
```

---

## Task 5: `PgFtsBackend`

Wraps the existing `idx_chunks_bm25` (pg_textsearch / Tiger BM25) for both
chunk-level and page-level (`#3980`) BM25. JOINs `file_paths` for path +
zone. **Writes are pass-through to `chunk_store`** — backend does not own
the write path.

**Files:**
- Create: `src/nexus/bricks/search/pg_fts_backend.py`
- Test: `tests/unit/bricks/search/test_pg_fts_backend.py`

- [ ] **Step 1: Audit the pg_textsearch operator + score function name**

`pg_textsearch` BM25 in `add_pg_textsearch_bm25_index.py` was added with
`USING bm25(chunk_text)`. The query syntax for that index family varies
by version (`<@>` operator, `@@@` operator, or `paradedb.score()` /
`bm25.score()` function). Run before coding:

```bash
psql "$NEXUS_DATABASE_URL" -c "\dx pg_textsearch"
psql "$NEXUS_DATABASE_URL" -c "SELECT proname FROM pg_proc WHERE proname LIKE '%score%' AND pronamespace IN (SELECT oid FROM pg_namespace WHERE nspname IN ('paradedb','pg_textsearch','public'));"
```

If no such extension is installed in your dev environment, install it
following the README at `alembic/README_DATABASES.md`. Use whichever
operator/function the installed version provides; the backend doesn't
care, as long as the test fixture loads the same extension.

- [ ] **Step 2: Write the failing tests**

```python
# tests/unit/bricks/search/test_pg_fts_backend.py
"""PgFtsBackend conformance + correctness tests.

Uses Postgres testcontainer fixture (existing) with pg_textsearch loaded.
Each test gets a clean document_chunks + file_paths state.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from nexus.bricks.search.pg_fts_backend import PgFtsBackend
from nexus.bricks.search.protocols import SearchBackend
from nexus.testing.fixtures import postgres_engine_clean  # existing


@pytest.fixture
async def backend(postgres_engine_clean: AsyncEngine):
    return PgFtsBackend(engine=postgres_engine_clean)


async def _seed(engine, rows: list[dict]) -> None:
    """Helper: insert file_paths + document_chunks rows."""
    async with engine.begin() as conn:
        for r in rows:
            await conn.execute(text(
                "INSERT INTO file_paths (path_id, zone_id, virtual_path, deleted_at) "
                "VALUES (:pid, :zid, :path, NULL) ON CONFLICT DO NOTHING"
            ), {"pid": r["path_id"], "zid": r["zone_id"], "path": r["path"]})
            await conn.execute(text(
                "INSERT INTO document_chunks "
                "(chunk_id, path_id, chunk_index, chunk_text, chunk_tokens, created_at) "
                "VALUES (:cid, :pid, :idx, :txt, :tok, now())"
            ), {"cid": r["chunk_id"], "pid": r["path_id"], "idx": r["chunk_index"],
                "txt": r["text"], "tok": len(r["text"].split())})


def test_satisfies_protocol(backend):
    assert isinstance(backend, SearchBackend)


@pytest.mark.asyncio
async def test_keyword_search_chunk_level(backend, postgres_engine_clean):
    await _seed(postgres_engine_clean, [
        {"chunk_id": "c1", "path_id": "p1", "zone_id": "z", "path": "/z/a.txt",
         "chunk_index": 0, "text": "the quick brown fox"},
        {"chunk_id": "c2", "path_id": "p2", "zone_id": "z", "path": "/z/b.txt",
         "chunk_index": 0, "text": "lazy dogs sleep"},
    ])
    hits = await backend.keyword_search("quick", "/z/", k=10, zone_id="z")
    assert [h.path for h in hits] == ["/z/a.txt"]


@pytest.mark.asyncio
async def test_path_prefix_filter(backend, postgres_engine_clean):
    await _seed(postgres_engine_clean, [
        {"chunk_id": "c1", "path_id": "p1", "zone_id": "z", "path": "/z/sub/a.txt",
         "chunk_index": 0, "text": "alpha"},
        {"chunk_id": "c2", "path_id": "p2", "zone_id": "z", "path": "/z/other/b.txt",
         "chunk_index": 0, "text": "alpha"},
    ])
    hits = await backend.keyword_search("alpha", "/z/sub/", k=10, zone_id="z")
    assert {h.path for h in hits} == {"/z/sub/a.txt"}


@pytest.mark.asyncio
async def test_zone_isolation(backend, postgres_engine_clean):
    await _seed(postgres_engine_clean, [
        {"chunk_id": "c1", "path_id": "p1", "zone_id": "z1", "path": "/z1/a.txt",
         "chunk_index": 0, "text": "alpha"},
        {"chunk_id": "c2", "path_id": "p2", "zone_id": "z2", "path": "/z2/a.txt",
         "chunk_index": 0, "text": "alpha"},
    ])
    hits = await backend.keyword_search("alpha", "/", k=10, zone_id="z1")
    assert {h.path for h in hits} == {"/z1/a.txt"}


@pytest.mark.asyncio
async def test_keyword_search_pages_aggregates_chunks(
    backend, postgres_engine_clean
):
    """#3980 page-BM25 leg — assemble chunks per path, BM25 over the page text."""
    await _seed(postgres_engine_clean, [
        {"chunk_id": "c1", "path_id": "p1", "zone_id": "z", "path": "/z/a.txt",
         "chunk_index": 0, "text": "common preamble"},
        {"chunk_id": "c2", "path_id": "p1", "zone_id": "z", "path": "/z/a.txt",
         "chunk_index": 1, "text": "rare phrase XYZQQ deep in body"},
    ])
    hits = await backend.keyword_search_pages("XYZQQ", "/z/", k=10, zone_id="z")
    assert [h.path for h in hits] == ["/z/a.txt"]
    # The page-BM25 leg returns a single result PER PATH, not per chunk.
    assert len({h.path for h in hits}) == len(hits)
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/unit/bricks/search/test_pg_fts_backend.py -v`
Expected: FAIL `ImportError`.

- [ ] **Step 4: Implement the backend**

Create `src/nexus/bricks/search/pg_fts_backend.py`:

```python
"""Postgres BM25 backend (Issue #3699).

Wraps the existing idx_chunks_bm25 pg_textsearch index (true k1+b BM25)
on document_chunks.chunk_text. Replaces the BM25 leg of txtai_backend
+ bm25s_search.py for Postgres deployments.

Two BM25 modes:
  * keyword_search()         — chunk-level (one row per chunk)
  * keyword_search_pages()   — page-level (#3980; one row per path,
                                aggregating all chunks for that path)

Writes are NOT owned by this backend. ChunkStore.replace_document_chunks
already writes chunk_text + embedding atomically; pg_textsearch maintains
the BM25 index automatically. The add/upsert/delete methods on this
class delegate to the existing chunk_store for protocol conformance.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from nexus.bricks.search.results import BaseSearchResult


# pg_textsearch operator/function names — adapt at install time. The
# default below uses the Tiger Data convention; if your build uses a
# different operator name, change here.
_BM25_MATCH_OP = "@@@"
_BM25_SCORE_FN = "paradedb.score"


class PgFtsBackend:
    def __init__(self, engine: AsyncEngine, chunk_store: Any | None = None) -> None:
        self._engine = engine
        self._chunk_store = chunk_store  # for add/upsert/delete pass-through

    async def startup(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    # ---- Pass-through writes (chunk_store owns the actual SQL) ------------

    async def add(self, docs: Sequence[dict[str, Any]], *, zone_id: str) -> int:
        if self._chunk_store is None or not docs:
            return 0
        return await self._chunk_store.add_chunks(docs, zone_id=zone_id)

    async def upsert(self, docs: Sequence[dict[str, Any]], *, zone_id: str) -> int:
        if self._chunk_store is None or not docs:
            return 0
        return await self._chunk_store.upsert_chunks(docs, zone_id=zone_id)

    async def delete(self, ids: Sequence[str], *, zone_id: str) -> int:
        if self._chunk_store is None or not ids:
            return 0
        return await self._chunk_store.delete_chunks(list(ids), zone_id=zone_id)

    # ---- Keyword search (chunk-level) ------------------------------------

    async def keyword_search(
        self, query: str, path: str, k: int, zone_id: str,
    ) -> list[BaseSearchResult]:
        sql = text(f"""
            SELECT c.chunk_id, fp.virtual_path AS path, c.chunk_text,
                   c.chunk_index,
                   {_BM25_SCORE_FN}(c.chunk_id) AS score
            FROM document_chunks c
            JOIN file_paths fp ON c.path_id = fp.path_id
            WHERE c.chunk_text {_BM25_MATCH_OP} :q
              AND fp.zone_id = :zone_id
              AND fp.virtual_path LIKE :prefix || '%'
              AND fp.deleted_at IS NULL
            ORDER BY score DESC
            LIMIT :k
        """)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(sql, {
                "q": query, "prefix": path, "zone_id": zone_id, "k": k,
            })).mappings().all()
        return [
            BaseSearchResult(
                path=r["path"],
                chunk_text=r["chunk_text"],
                score=float(r["score"]),
                chunk_index=int(r["chunk_index"]),
                keyword_score=float(r["score"]),
                zone_id=zone_id,
            )
            for r in rows
        ]

    # ---- Keyword search (page-level, #3980 parity) ------------------------

    async def keyword_search_pages(
        self, query: str, path: str, k: int, zone_id: str,
    ) -> list[BaseSearchResult]:
        """Aggregate chunks → pages, BM25 on the page text. One row per path."""
        sql = text(f"""
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
            SELECT path_id, virtual_path AS path, page_text,
                   {_BM25_SCORE_FN}(path_id) AS score
            FROM pages
            WHERE page_text {_BM25_MATCH_OP} :q
            ORDER BY score DESC
            LIMIT :k
        """)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(sql, {
                "q": query, "prefix": path, "zone_id": zone_id, "k": k,
            })).mappings().all()
        return [
            BaseSearchResult(
                path=r["path"],
                chunk_text=r["page_text"],
                score=float(r["score"]),
                chunk_index=0,
                keyword_score=float(r["score"]),
                zone_id=zone_id,
            )
            for r in rows
        ]

    # ---- Semantic (lives in PgVectorBackend; no-op here) -----------------

    async def semantic_search(
        self, query_vector: Sequence[float], path: str, k: int, zone_id: str,
    ) -> list[BaseSearchResult]:
        return []
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/unit/bricks/search/test_pg_fts_backend.py -v`
Expected: 5 passed.

If `paradedb.score` / `@@@` doesn't exist in your installed pg_textsearch
version, adapt `_BM25_SCORE_FN` and `_BM25_MATCH_OP` at the top of
`pg_fts_backend.py` to whatever the installed version provides. The rest
of the file is unaffected.

- [ ] **Step 6: Commit**

```bash
git add src/nexus/bricks/search/pg_fts_backend.py tests/unit/bricks/search/test_pg_fts_backend.py
git commit -m "feat(#3699): PgFtsBackend — pg_textsearch BM25 (chunk + page legs, #3980 parity)"
```

---

## Task 6: `PgVectorBackend`

Wraps the existing `idx_chunks_embedding_hnsw` (halfvec(1536) cosine) on
`document_chunks.embedding`. Half of this code already lives at
`daemon.py:2010-2021` — extract it.

**Files:**
- Create: `src/nexus/bricks/search/pg_vector_backend.py`
- Test: `tests/unit/bricks/search/test_pg_vector_backend.py`

- [ ] **Step 1: Read the donor SQL**

Read `src/nexus/bricks/search/daemon.py:2010-2021` (the direct-SQL semantic
search block that today bypasses txtai). That SQL is the model for
`PgVectorBackend.semantic_search`.

- [ ] **Step 2: Write the failing tests**

```python
# tests/unit/bricks/search/test_pg_vector_backend.py
"""PgVectorBackend conformance + cosine-ordering tests over halfvec(1536)."""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from nexus.bricks.search.pg_vector_backend import PgVectorBackend
from nexus.bricks.search.protocols import SearchBackend
from nexus.testing.fixtures import postgres_engine_clean


@pytest.fixture
async def backend(postgres_engine_clean: AsyncEngine):
    return PgVectorBackend(engine=postgres_engine_clean)


async def _seed_with_embeddings(engine, rows: list[dict]) -> None:
    async with engine.begin() as conn:
        for r in rows:
            await conn.execute(text(
                "INSERT INTO file_paths (path_id, zone_id, virtual_path, deleted_at) "
                "VALUES (:pid, :zid, :path, NULL) ON CONFLICT DO NOTHING"
            ), {"pid": r["path_id"], "zid": r["zone_id"], "path": r["path"]})
            await conn.execute(text(
                "INSERT INTO document_chunks "
                "(chunk_id, path_id, chunk_index, chunk_text, chunk_tokens, "
                " embedding, created_at) "
                "VALUES (:cid, :pid, :idx, :txt, :tok, "
                "        CAST(:emb AS halfvec), now())"
            ), {"cid": r["chunk_id"], "pid": r["path_id"], "idx": r["chunk_index"],
                "txt": r["text"], "tok": len(r["text"].split()),
                "emb": str(list(r["emb"]))})


def test_satisfies_protocol(backend):
    assert isinstance(backend, SearchBackend)


@pytest.mark.asyncio
async def test_semantic_search_orders_by_cosine(backend, postgres_engine_clean):
    qvec = [1.0] + [0.0] * 1535
    near = [0.99] + [0.01] * 1535
    far = [0.0, 1.0] + [0.0] * 1534
    await _seed_with_embeddings(postgres_engine_clean, [
        {"chunk_id": "near", "path_id": "p1", "zone_id": "z", "path": "/z/a.txt",
         "chunk_index": 0, "text": "near", "emb": near},
        {"chunk_id": "far", "path_id": "p2", "zone_id": "z", "path": "/z/b.txt",
         "chunk_index": 0, "text": "far", "emb": far},
    ])
    hits = await backend.semantic_search(qvec, "/z/", k=10, zone_id="z")
    assert [h.path for h in hits] == ["/z/a.txt", "/z/b.txt"]


@pytest.mark.asyncio
async def test_null_embedding_skipped(backend, postgres_engine_clean):
    async with postgres_engine_clean.begin() as conn:
        await conn.execute(text(
            "INSERT INTO file_paths (path_id, zone_id, virtual_path, deleted_at) "
            "VALUES ('p1', 'z', '/z/a.txt', NULL)"
        ))
        await conn.execute(text(
            "INSERT INTO document_chunks "
            "(chunk_id, path_id, chunk_index, chunk_text, chunk_tokens, created_at) "
            "VALUES ('c1', 'p1', 0, 'x', 1, now())"  # embedding NULL
        ))
    hits = await backend.semantic_search([0.0] * 1536, "/z/", k=10, zone_id="z")
    assert hits == []


@pytest.mark.asyncio
async def test_zone_isolation(backend, postgres_engine_clean):
    await _seed_with_embeddings(postgres_engine_clean, [
        {"chunk_id": "c1", "path_id": "p1", "zone_id": "z1", "path": "/z1/a.txt",
         "chunk_index": 0, "text": "x", "emb": [1.0] + [0.0] * 1535},
        {"chunk_id": "c2", "path_id": "p2", "zone_id": "z2", "path": "/z2/a.txt",
         "chunk_index": 0, "text": "x", "emb": [1.0] + [0.0] * 1535},
    ])
    hits = await backend.semantic_search([1.0] + [0.0] * 1535, "/", k=10, zone_id="z1")
    assert {h.path for h in hits} == {"/z1/a.txt"}
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/unit/bricks/search/test_pg_vector_backend.py -v`
Expected: FAIL `ImportError`.

- [ ] **Step 4: Implement the backend**

Create `src/nexus/bricks/search/pg_vector_backend.py`:

```python
"""Postgres pgvector backend (Issue #3699).

Dense semantic search via cosine distance over document_chunks.embedding
(halfvec(1536), HNSW-indexed). Mirrors the direct-SQL path that today
lives at daemon.py:2010-2021; once T9 lands, that inline block is
removed and the daemon calls this backend.

Writes are NOT owned by this backend — chunk_store.replace_document_chunks
writes the embedding column atomically. add/upsert/delete are
pass-throughs for protocol conformance.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from nexus.bricks.search.results import BaseSearchResult


class PgVectorBackend:
    def __init__(self, engine: AsyncEngine, chunk_store: Any | None = None) -> None:
        self._engine = engine
        self._chunk_store = chunk_store

    async def startup(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    async def add(self, docs: Sequence[dict[str, Any]], *, zone_id: str) -> int:
        if self._chunk_store is None or not docs:
            return 0
        return await self._chunk_store.add_chunks(docs, zone_id=zone_id)

    async def upsert(self, docs: Sequence[dict[str, Any]], *, zone_id: str) -> int:
        if self._chunk_store is None or not docs:
            return 0
        return await self._chunk_store.upsert_chunks(docs, zone_id=zone_id)

    async def delete(self, ids: Sequence[str], *, zone_id: str) -> int:
        if self._chunk_store is None or not ids:
            return 0
        return await self._chunk_store.delete_chunks(list(ids), zone_id=zone_id)

    async def keyword_search(
        self, query: str, path: str, k: int, zone_id: str,
    ) -> list[BaseSearchResult]:
        return []  # Lives in PgFtsBackend.

    async def semantic_search(
        self, query_vector: Sequence[float], path: str, k: int, zone_id: str,
    ) -> list[BaseSearchResult]:
        sql = text("""
            SELECT c.chunk_id, fp.virtual_path AS path, c.chunk_text,
                   c.chunk_index,
                   1 - (c.embedding <=> CAST(:qvec AS halfvec)) AS score
            FROM document_chunks c
            JOIN file_paths fp ON c.path_id = fp.path_id
            WHERE c.embedding IS NOT NULL
              AND fp.zone_id = :zone_id
              AND fp.virtual_path LIKE :prefix || '%'
              AND fp.deleted_at IS NULL
            ORDER BY c.embedding <=> CAST(:qvec AS halfvec)
            LIMIT :k
        """)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(sql, {
                "qvec": str(list(query_vector)),
                "prefix": path,
                "zone_id": zone_id,
                "k": k,
            })).mappings().all()
        return [
            BaseSearchResult(
                path=r["path"],
                chunk_text=r["chunk_text"],
                score=float(r["score"]),
                chunk_index=int(r["chunk_index"]),
                vector_score=float(r["score"]),
                zone_id=zone_id,
            )
            for r in rows
        ]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/unit/bricks/search/test_pg_vector_backend.py -v`
Expected: 4 passed.

- [ ] **Step 6: Commit**

```bash
git add src/nexus/bricks/search/pg_vector_backend.py tests/unit/bricks/search/test_pg_vector_backend.py
git commit -m "feat(#3699): PgVectorBackend — halfvec(1536) HNSW cosine, JOIN file_paths for zone+path"
```

---

## Task 7: `SqliteFtsBackend`

SQLite FTS5 over `document_chunks.chunk_text`, JOIN `file_paths` for path
+ zone. Replaces `bm25s_search.py` for the SANDBOX profile. **Writes are
pass-through to `chunk_store`** (the FTS5 vtable + triggers from T4 keep
themselves in sync).

**Files:**
- Create: `src/nexus/bricks/search/sqlite_fts_backend.py`
- Test: `tests/unit/bricks/search/test_sqlite_fts_backend.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/bricks/search/test_sqlite_fts_backend.py
"""SqliteFtsBackend conformance + FTS5 bm25() ranking + JOIN tests."""

import sqlite3
from pathlib import Path

import pytest

from nexus.bricks.search.protocols import SearchBackend
from nexus.bricks.search.sqlite_fts_backend import SqliteFtsBackend


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    p = tmp_path / "test.db"
    conn = sqlite3.connect(str(p))
    # Minimal schema mirroring the production tables + triggers from T4.
    conn.executescript("""
        CREATE TABLE file_paths (
            path_id      TEXT PRIMARY KEY,
            zone_id      TEXT NOT NULL,
            virtual_path TEXT NOT NULL,
            deleted_at   TIMESTAMP NULL
        );
        CREATE TABLE document_chunks (
            chunk_id     TEXT PRIMARY KEY,
            path_id      TEXT NOT NULL REFERENCES file_paths(path_id),
            chunk_index  INTEGER NOT NULL,
            chunk_text   TEXT NOT NULL,
            chunk_tokens INTEGER NOT NULL,
            created_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE VIRTUAL TABLE document_chunks_fts USING fts5(
            chunk_text, content='document_chunks', content_rowid='rowid',
            tokenize='porter unicode61'
        );
        CREATE TRIGGER document_chunks_fts_ai AFTER INSERT ON document_chunks BEGIN
            INSERT INTO document_chunks_fts(rowid, chunk_text)
            VALUES (NEW.rowid, NEW.chunk_text);
        END;
        CREATE TRIGGER document_chunks_fts_ad AFTER DELETE ON document_chunks BEGIN
            INSERT INTO document_chunks_fts(document_chunks_fts, rowid, chunk_text)
            VALUES ('delete', OLD.rowid, OLD.chunk_text);
        END;
    """)
    conn.close()
    return str(p)


def _seed(db_path: str, rows: list[dict]) -> None:
    conn = sqlite3.connect(db_path)
    for r in rows:
        conn.execute(
            "INSERT OR IGNORE INTO file_paths (path_id, zone_id, virtual_path) "
            "VALUES (?, ?, ?)",
            (r["path_id"], r["zone_id"], r["path"]),
        )
        conn.execute(
            "INSERT INTO document_chunks "
            "(chunk_id, path_id, chunk_index, chunk_text, chunk_tokens) "
            "VALUES (?, ?, ?, ?, ?)",
            (r["chunk_id"], r["path_id"], r["chunk_index"], r["text"],
             len(r["text"].split())),
        )
    conn.commit()
    conn.close()


@pytest.fixture
def backend(db_path: str) -> SqliteFtsBackend:
    return SqliteFtsBackend(db_path=db_path)


def test_satisfies_protocol(backend):
    assert isinstance(backend, SearchBackend)


@pytest.mark.asyncio
async def test_keyword_search_chunk_level(backend, db_path):
    _seed(db_path, [
        {"chunk_id": "c1", "path_id": "p1", "zone_id": "z", "path": "/z/a.txt",
         "chunk_index": 0, "text": "the quick brown fox"},
        {"chunk_id": "c2", "path_id": "p2", "zone_id": "z", "path": "/z/b.txt",
         "chunk_index": 0, "text": "lazy dogs sleep"},
    ])
    hits = await backend.keyword_search("quick", "/z/", k=10, zone_id="z")
    assert [h.path for h in hits] == ["/z/a.txt"]


@pytest.mark.asyncio
async def test_path_prefix_filter(backend, db_path):
    _seed(db_path, [
        {"chunk_id": "c1", "path_id": "p1", "zone_id": "z", "path": "/z/sub/a.txt",
         "chunk_index": 0, "text": "alpha"},
        {"chunk_id": "c2", "path_id": "p2", "zone_id": "z", "path": "/z/other/b.txt",
         "chunk_index": 0, "text": "alpha"},
    ])
    hits = await backend.keyword_search("alpha", "/z/sub/", k=10, zone_id="z")
    assert [h.path for h in hits] == ["/z/sub/a.txt"]


@pytest.mark.asyncio
async def test_zone_isolation(backend, db_path):
    _seed(db_path, [
        {"chunk_id": "c1", "path_id": "p1", "zone_id": "z1", "path": "/z1/a.txt",
         "chunk_index": 0, "text": "alpha"},
        {"chunk_id": "c2", "path_id": "p2", "zone_id": "z2", "path": "/z2/a.txt",
         "chunk_index": 0, "text": "alpha"},
    ])
    hits = await backend.keyword_search("alpha", "/", k=10, zone_id="z1")
    assert {h.path for h in hits} == {"/z1/a.txt"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/bricks/search/test_sqlite_fts_backend.py -v`
Expected: FAIL `ImportError`.

- [ ] **Step 3: Implement the backend**

Create `src/nexus/bricks/search/sqlite_fts_backend.py`:

```python
"""SQLite FTS5 backend (Issue #3699).

Replaces bm25s_search.py for the SANDBOX profile. Uses FTS5 native bm25()
ranking — no in-memory index, no rebuild on query. JOINs file_paths for
zone + path filtering (mirrors PgFtsBackend's shape).

sqlite3 is sync; calls are wrapped in asyncio.to_thread to keep the event
loop responsive (matches sqlite_vec_backend.py pattern from #3778).

Writes are NOT owned here — chunk_store.replace_document_chunks writes
chunk_text, and the FTS5 triggers from the T4 migration keep
document_chunks_fts in sync. add/upsert/delete pass through to chunk_store.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Sequence
from typing import Any

from nexus.bricks.search.results import BaseSearchResult


class SqliteFtsBackend:
    def __init__(self, db_path: str, chunk_store: Any | None = None) -> None:
        self._db_path = db_path
        self._chunk_store = chunk_store

    async def startup(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    async def add(self, docs: Sequence[dict[str, Any]], *, zone_id: str) -> int:
        if self._chunk_store is None or not docs:
            return 0
        return await self._chunk_store.add_chunks(docs, zone_id=zone_id)

    async def upsert(self, docs: Sequence[dict[str, Any]], *, zone_id: str) -> int:
        if self._chunk_store is None or not docs:
            return 0
        return await self._chunk_store.upsert_chunks(docs, zone_id=zone_id)

    async def delete(self, ids: Sequence[str], *, zone_id: str) -> int:
        if self._chunk_store is None or not ids:
            return 0
        return await self._chunk_store.delete_chunks(list(ids), zone_id=zone_id)

    async def keyword_search(
        self, query: str, path: str, k: int, zone_id: str,
    ) -> list[BaseSearchResult]:
        def _search() -> list[BaseSearchResult]:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT dc.chunk_id, fp.virtual_path AS path, dc.chunk_text, "
                    "       dc.chunk_index, "
                    "       bm25(document_chunks_fts) AS score "
                    "FROM document_chunks_fts "
                    "JOIN document_chunks dc ON dc.rowid = document_chunks_fts.rowid "
                    "JOIN file_paths fp ON dc.path_id = fp.path_id "
                    "WHERE document_chunks_fts MATCH ? "
                    "  AND fp.zone_id = ? "
                    "  AND fp.virtual_path LIKE ? || '%' "
                    "  AND fp.deleted_at IS NULL "
                    "ORDER BY score "  # bm25() returns negative; ASC = best first
                    "LIMIT ?",
                    [query, zone_id, path, k],
                ).fetchall()
            return [
                BaseSearchResult(
                    path=r["path"],
                    chunk_text=r["chunk_text"],
                    score=-float(r["score"]),  # flip sign so higher = better
                    chunk_index=int(r["chunk_index"]),
                    keyword_score=-float(r["score"]),
                    zone_id=zone_id,
                )
                for r in rows
            ]

        return await asyncio.to_thread(_search)

    async def semantic_search(
        self, query_vector: Sequence[float], path: str, k: int, zone_id: str,
    ) -> list[BaseSearchResult]:
        return []  # Semantic lives in sqlite_vec_backend.
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/bricks/search/test_sqlite_fts_backend.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/bricks/search/sqlite_fts_backend.py tests/unit/bricks/search/test_sqlite_fts_backend.py
git commit -m "feat(#3699): SqliteFtsBackend — FTS5 bm25() over document_chunks + file_paths JOIN"
```

---

## Task 8: Verify `SqliteVecBackend` satisfies `SearchBackend` protocol

`sqlite_vec_backend.py` exists. Confirm its method signatures match the new
protocol; rename / add stubs as needed. The current API likely uses
`search()`; the protocol expects `semantic_search()` plus a no-op
`keyword_search()` returning `[]`.

**Files:**
- Modify (if needed): `src/nexus/bricks/search/sqlite_vec_backend.py`
- Test: `tests/unit/bricks/search/test_sqlite_vec_backend.py` (existing; add one assertion)

- [ ] **Step 1: Audit existing methods**

Run: `grep -n "async def \|def " src/nexus/bricks/search/sqlite_vec_backend.py | grep -v "^\s*#"`

Required protocol methods: `add`, `upsert`, `delete`, `keyword_search`,
`semantic_search`, `startup`, `shutdown`. Identify which exist (under
current names) and which are missing.

- [ ] **Step 2: Add the conformance assertion to the existing test file**

Append to `tests/unit/bricks/search/test_sqlite_vec_backend.py` (or its
conftest if separate):

```python
def test_satisfies_search_backend_protocol():
    from nexus.bricks.search.protocols import SearchBackend
    from nexus.bricks.search.sqlite_vec_backend import SqliteVecBackend

    backend = SqliteVecBackend(db_path=":memory:")
    assert isinstance(backend, SearchBackend)
```

- [ ] **Step 3: Run the test to see what is missing**

Run: `uv run pytest tests/unit/bricks/search/test_sqlite_vec_backend.py::test_satisfies_search_backend_protocol -v`

The failure message names the missing method. Add minimal stubs:
- For methods missing entirely (e.g., `keyword_search`):

  ```python
  async def keyword_search(
      self, query: str, path: str, k: int, zone_id: str,
  ) -> list[BaseSearchResult]:
      return []  # Semantic-only backend; SqliteFtsBackend handles keyword.
  ```
- For methods that exist under a different name (e.g., `search` → `semantic_search`),
  rename in-place; if external callers reference the old name (grep first), keep
  a thin wrapper.

- [ ] **Step 4: Re-run the full file**

Run: `uv run pytest tests/unit/bricks/search/test_sqlite_vec_backend.py -v`
Expected: all pass including the new conformance test.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/bricks/search/sqlite_vec_backend.py tests/unit/bricks/search/test_sqlite_vec_backend.py
git commit -m "refactor(#3699): SqliteVecBackend conforms to SearchBackend protocol"
```

---

## Task 9: Daemon — backend selection by profile + 3-way RRF hybrid

Replace the `TxtaiBackend(...)` instantiation block (`src/nexus/bricks/search/daemon.py:653-687`) with profile-based selection of `(fts_backend, vector_backend)`. Wire 3-way RRF in `daemon.search()`. **No lazy backfill task** — `chunk_store` writes embeddings inline.

**Files:**
- Modify: `src/nexus/bricks/search/daemon.py`
- Test: `tests/integration/bricks/search/test_daemon_search_pg.py` + `test_daemon_search_sqlite.py`

- [ ] **Step 1: Write the failing integration tests**

```python
# tests/integration/bricks/search/test_daemon_search_pg.py
"""End-to-end daemon search on Postgres profile."""

import pytest

from nexus.bricks.search.daemon import SearchDaemon
from nexus.bricks.search.pg_fts_backend import PgFtsBackend
from nexus.bricks.search.pg_vector_backend import PgVectorBackend
from nexus.testing.fixtures import postgres_daemon_config  # existing helper


@pytest.fixture
async def daemon(postgres_daemon_config):
    d = SearchDaemon(config=postgres_daemon_config)
    await d.startup()
    yield d
    await d.shutdown()


@pytest.mark.asyncio
async def test_daemon_picks_pg_backends_for_postgres_url(daemon: SearchDaemon):
    assert isinstance(daemon._fts_backend, PgFtsBackend)
    assert isinstance(daemon._vector_backend, PgVectorBackend)


@pytest.mark.asyncio
async def test_daemon_keyword_mode(daemon: SearchDaemon, postgres_engine_clean):
    from sqlalchemy import text
    async with postgres_engine_clean.begin() as conn:
        await conn.execute(text(
            "INSERT INTO file_paths (path_id, zone_id, virtual_path, deleted_at) "
            "VALUES ('p1', 'z', '/z/a.txt', NULL)"
        ))
        await conn.execute(text(
            "INSERT INTO document_chunks "
            "(chunk_id, path_id, chunk_index, chunk_text, chunk_tokens, created_at) "
            "VALUES ('c1', 'p1', 0, 'the quick fox', 3, now())"
        ))
    hits = await daemon.search("quick", path="/z/", limit=10, search_mode="keyword")
    assert [h.path for h in hits] == ["/z/a.txt"]


@pytest.mark.asyncio
async def test_daemon_hybrid_mode_calls_3way_fusion(
    daemon: SearchDaemon, monkeypatch, postgres_engine_clean,
):
    """Hybrid mode = chunk-BM25 + page-BM25 + dense, fused via fusion module."""
    from nexus.bricks.search import fusion
    from sqlalchemy import text

    async with postgres_engine_clean.begin() as conn:
        await conn.execute(text(
            "INSERT INTO file_paths (path_id, zone_id, virtual_path, deleted_at) "
            "VALUES ('p1', 'z', '/z/a.txt', NULL)"
        ))
        await conn.execute(text(
            "INSERT INTO document_chunks "
            "(chunk_id, path_id, chunk_index, chunk_text, chunk_tokens, created_at) "
            "VALUES ('c1', 'p1', 0, 'alpha', 1, now())"
        ))

    seen = {"calls": 0}
    real_rrf = fusion.rrf_fusion

    def spy(*a, **kw):
        seen["calls"] += 1
        return real_rrf(*a, **kw)

    monkeypatch.setattr(fusion, "rrf_fusion", spy)
    await daemon.search("alpha", path="/z/", limit=5, search_mode="hybrid")
    # 3-way RRF can be implemented as one rrf_fusion_3way call OR two
    # nested rrf_fusion calls. Either is acceptable.
    assert seen["calls"] >= 1
```

```python
# tests/integration/bricks/search/test_daemon_search_sqlite.py
import pytest

from nexus.bricks.search.daemon import SearchDaemon
from nexus.bricks.search.sqlite_fts_backend import SqliteFtsBackend
from nexus.bricks.search.sqlite_vec_backend import SqliteVecBackend
from nexus.testing.fixtures import sqlite_daemon_config


@pytest.fixture
async def daemon(sqlite_daemon_config):
    d = SearchDaemon(config=sqlite_daemon_config)
    await d.startup()
    yield d
    await d.shutdown()


@pytest.mark.asyncio
async def test_daemon_picks_sqlite_backends_for_sqlite_url(daemon: SearchDaemon):
    assert isinstance(daemon._fts_backend, SqliteFtsBackend)
    assert isinstance(daemon._vector_backend, SqliteVecBackend)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/integration/bricks/search/test_daemon_search_pg.py tests/integration/bricks/search/test_daemon_search_sqlite.py -v`
Expected: FAIL — `daemon._fts_backend` / `daemon._vector_backend` do not exist.

- [ ] **Step 3: Replace the txtai-init block in `daemon.py`**

Locate the existing `try: from nexus.bricks.search.txtai_backend import TxtaiBackend ...` block (around lines 653-687 in current `daemon.py` — confirm by grep). Replace with:

```python
# Initialize search backends by profile (Issue #3699)
url = self.config.database_url or ""
self._fts_backend, self._vector_backend = self._build_backends(url)
await self._fts_backend.startup()
await self._vector_backend.startup()
```

Add `_build_backends`:

```python
def _build_backends(self, database_url: str):
    if "postgresql" in database_url:
        from nexus.bricks.search.pg_fts_backend import PgFtsBackend
        from nexus.bricks.search.pg_vector_backend import PgVectorBackend
        return (
            PgFtsBackend(engine=self._async_engine, chunk_store=self._chunk_store),
            PgVectorBackend(engine=self._async_engine, chunk_store=self._chunk_store),
        )
    from nexus.bricks.search.sqlite_fts_backend import SqliteFtsBackend
    from nexus.bricks.search.sqlite_vec_backend import SqliteVecBackend
    return (
        SqliteFtsBackend(db_path=self._sqlite_path, chunk_store=self._chunk_store),
        SqliteVecBackend(db_path=self._sqlite_path),
    )
```

Strip:
- All txtai imports from `daemon.py`.
- The `_bootstrap_txtai_backend` method.
- The `self._txtai_bootstrap_task` attribute and its `asyncio.create_task(...)` call.
- Any `_lazy_reembed_task` references inherited from the prior plan draft (this revised plan has no backfill).
- The inline direct-SQL semantic search at `daemon.py:2010-2021` (now in PgVectorBackend).

Add hybrid wiring in `daemon.search()` (search for the existing `search_mode` dispatch):

```python
async def search(
    self,
    query: str,
    path: str = "/",
    limit: int = 10,
    search_mode: str = "hybrid",
    **_: Any,
) -> list[BaseSearchResult]:
    zone_id = self._zone_for_path(path)

    if search_mode == "keyword":
        return await self._fts_backend.keyword_search(query, path, limit, zone_id)

    if search_mode == "semantic":
        qvec = await self._embedding_client.embed_query(query)
        return await self._vector_backend.semantic_search(qvec, path, limit, zone_id)

    # hybrid: 3-way RRF (chunk-BM25 + page-BM25 + dense)
    qvec = await self._embedding_client.embed_query(query)
    is_pg = isinstance(self._fts_backend, PgFtsBackend)
    if is_pg:
        chunk_kw, page_kw, dense = await asyncio.gather(
            self._fts_backend.keyword_search(query, path, limit * 2, zone_id),
            self._fts_backend.keyword_search_pages(query, path, limit * 2, zone_id),
            self._vector_backend.semantic_search(qvec, path, limit * 2, zone_id),
        )
    else:
        # SQLite path: no page-BM25 leg.
        chunk_kw, dense = await asyncio.gather(
            self._fts_backend.keyword_search(query, path, limit * 2, zone_id),
            self._vector_backend.semantic_search(qvec, path, limit * 2, zone_id),
        )
        page_kw = []

    from nexus.bricks.search.fusion import rrf_fusion
    # 3-way: fuse chunk+page first, then with dense.
    kw_fused = rrf_fusion(chunk_kw, page_kw, k=60, limit=limit * 2, id_key=None)
    fused = rrf_fusion(kw_fused, dense, k=60, limit=limit, id_key=None)
    return [BaseSearchResult(**f) if isinstance(f, dict) else f for f in fused]
```

Instantiate `EmbeddingClient` somewhere in `startup()` (use `self.config.embedding_model` and the matryoshka-aware default of 1536):

```python
from nexus.bricks.search.embedding_client import EmbeddingClient
self._embedding_client = EmbeddingClient(
    model=self.config.embedding_model,
    dim=getattr(self.config, "embedding_dimensions", 1536) or 1536,
)
```

- [ ] **Step 4: Run integration tests**

Run: `uv run pytest tests/integration/bricks/search/test_daemon_search_pg.py tests/integration/bricks/search/test_daemon_search_sqlite.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/bricks/search/daemon.py tests/integration/bricks/search/test_daemon_search_pg.py tests/integration/bricks/search/test_daemon_search_sqlite.py
git commit -m "feat(#3699): daemon — profile-based backend selection + 3-way RRF (chunk + page + dense)"
```
---

## Task 10: Drop `TxtaiBackend` and `bm25s_search.py` exports

**Files:**
- Modify: `src/nexus/bricks/search/__init__.py`
- Modify: `src/nexus/bricks/search/manifest.py`
- Modify: `src/nexus/bricks/search/scope_ops.py`

- [ ] **Step 1: Edit `__init__.py`**

Read `src/nexus/bricks/search/__init__.py`. Remove:

```python
from nexus.bricks.search.txtai_backend import (
    ...
    TxtaiBackend,
    ...
)
```

and from `__all__`:

```python
"TxtaiBackend",
```

Add:

```python
from nexus.bricks.search.pg_fts_backend import PgFtsBackend
from nexus.bricks.search.pg_vector_backend import PgVectorBackend
from nexus.bricks.search.sqlite_fts_backend import SqliteFtsBackend
```

and to `__all__`:

```python
"PgFtsBackend",
"PgVectorBackend",
"SqliteFtsBackend",
```

Also remove any `bm25s_search` exports.

- [ ] **Step 2: Edit `manifest.py`**

Open `src/nexus/bricks/search/manifest.py`. Remove the line `"nexus.bricks.search.txtai_backend",` from the modules list.

- [ ] **Step 3: Strip txtai-era docstrings in `scope_ops.py`**

Open `src/nexus/bricks/search/scope_ops.py`:
- Line 486: rewrite `"backfill_zone_from_chunks: txtai upsert failed for zone %s"` → `"backfill_zone_from_chunks: vector upsert failed for zone %s"`.
- Line 697 docstring: drop the `# ``TxtaiBackend.delete`` raises if persistence to pgvector` comment or rewrite to reference `PgVectorBackend`.
- Line 611 docstring: rewrite to reference `_run_lazy_reembed` instead of `_bootstrap_txtai_backend`.

- [ ] **Step 4: Sanity-run the full unit suite for search**

Run: `uv run pytest tests/unit/bricks/search/ -v --ignore=tests/unit/bricks/search/test_txtai_backend_bm25_only.py --ignore=tests/unit/bricks/search/test_page_aggregation.py --ignore=tests/unit/bricks/search/test_bootstrap_filter_shape.py`
Expected: all pass (txtai-specific files excluded; deleted in next task).

- [ ] **Step 5: Commit**

```bash
git add src/nexus/bricks/search/__init__.py src/nexus/bricks/search/manifest.py src/nexus/bricks/search/scope_ops.py
git commit -m "refactor(#3699): drop TxtaiBackend / bm25s_search exports + manifest entries"
```

---

## Task 11: Delete `txtai_backend.py`, `bm25s_search.py`, and their tests

**Files:**
- Delete: `src/nexus/bricks/search/txtai_backend.py`
- Delete: `src/nexus/bricks/search/bm25s_search.py`
- Delete: `tests/unit/bricks/search/test_txtai_backend_bm25_only.py`
- Delete: `tests/unit/bricks/search/test_bootstrap_filter_shape.py`
- Delete: `tests/integration/bricks/search/test_txtai_reranker.py`
- Delete: any `tests/unit/bricks/search/test_bm25s_*` files
- Modify: `tests/unit/bricks/search/test_page_aggregation.py` — keep only the pure-helper assertions; drop the `TxtaiBackend(...)` cases.

- [ ] **Step 1: Identify all files to delete**

Run: `find tests -name 'test_txtai*' -o -name 'test_bm25s*' -o -name 'test_bootstrap_filter_shape*'`
Note the list.

- [ ] **Step 2: Delete the source files**

Run: `git rm src/nexus/bricks/search/txtai_backend.py src/nexus/bricks/search/bm25s_search.py`

- [ ] **Step 3: Delete the test files**

Run: `git rm` against each path from Step 1 except `test_page_aggregation.py`.

- [ ] **Step 4: Trim `test_page_aggregation.py`**

Read the file. Delete every test that imports `TxtaiBackend` or instantiates it. Keep only tests that import from `result_builders`. If no tests remain, delete the file too (`git rm`).

- [ ] **Step 5: Run the full search test suite**

Run: `uv run pytest tests/unit/bricks/search/ tests/integration/bricks/search/ -v`
Expected: all pass.

- [ ] **Step 6: Run the brick-import lint**

Run: `pre-commit run check-brick-imports --all-files`
Expected: pass (no stale txtai imports).

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "feat(#3699): delete txtai_backend.py + bm25s_search.py + their tests"
```

---

## Task 12: Drop `txtai` and `faiss-cpu` from `pyproject.toml`

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Edit the `semantic-search` extra**

Open `pyproject.toml` at line ~314. Remove:

```toml
"txtai[database,graph]>=9.0; sys_platform != 'darwin' or platform_machine != 'x86_64'",
"faiss-cpu>=1.11.0; platform_machine=='x86_64' or platform_machine=='aarch64' or platform_machine=='arm64'",
```

Replace the comment block with:

```toml
# Direct pgvector + pgtext SQL — no txtai (Issue #3699).
# torch / transformers / faiss-cpu are no longer pulled in.
```

- [ ] **Step 2: Edit the `all` extra**

At line ~375, remove the same two lines from the `all` list.

- [ ] **Step 3: Re-lock and verify**

Run: `uv sync --all-extras`
Expected: lock file regenerates without txtai / torch / transformers / sympy / faiss-cpu / huggingface_hub.

Run: `uv pip list | grep -iE "txtai|torch|transformers|faiss|huggingface"`
Expected: empty output.

- [ ] **Step 4: Run the full suite once more**

Run: `uv run pytest tests/unit/bricks/search/ tests/integration/bricks/search/ -v`
Expected: all pass with the slim install.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "build(#3699): drop txtai + faiss-cpu — direct pgvector path no longer needs them"
```

---

## Task 13: Add `gbrain-evals` benchmark gate

**Files:**
- Create: `tests/benchmarks/gbrain_eval.py`
- Modify: `Makefile`

- [ ] **Step 1: Write the benchmark harness**

Create `tests/benchmarks/gbrain_eval.py`:

```python
"""Manual benchmark gate for #3699 cutover.

Runs the new search daemon against the gbrain-evals corpus
(https://github.com/garrytan/gbrain-evals). Prints recall@5 and NDCG@5.

Pre-merge gate: numbers must match or beat the issue baseline:
    recall@5  = 0.9489
    NDCG@5    = 0.9028

Usage:
    make bench-search

Env:
    GBRAIN_EVALS_DIR   path to a checkout of gbrain-evals
    NEXUS_DATABASE_URL must point at a fresh Postgres instance
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from nexus.bricks.search.daemon import SearchDaemon
from nexus.bricks.search.config import SearchConfig


async def main() -> int:
    eval_dir = Path(os.environ["GBRAIN_EVALS_DIR"])
    db_url = os.environ["NEXUS_DATABASE_URL"]

    daemon = SearchDaemon(config=SearchConfig(database_url=db_url))
    await daemon.startup()
    try:
        # Index corpus
        for doc in (eval_dir / "corpus.jsonl").read_text().splitlines():
            d = json.loads(doc)
            await daemon._fts_backend.upsert(
                [{"id": d["id"], "path": d["path"], "text": d["text"]}],
                zone_id="bench",
            )

        # Wait for backfill
        if daemon._lazy_reembed_task:
            await daemon._lazy_reembed_task

        # Score
        recalls, ndcgs = [], []
        for line in (eval_dir / "queries.jsonl").read_text().splitlines():
            q = json.loads(line)
            hits = await daemon.search(
                q["query"], path="/", limit=10, search_mode="hybrid",
            )
            top5 = [h.path for h in hits[:5]]
            relevant = set(q["relevant"])
            recalls.append(len(set(top5) & relevant) / max(1, len(relevant)))
            ndcgs.append(_ndcg(top5, relevant))

        r5 = sum(recalls) / len(recalls)
        n5 = sum(ndcgs) / len(ndcgs)
        print(f"recall@5 = {r5:.4f}")
        print(f"NDCG@5   = {n5:.4f}")

        baseline_r5, baseline_n5 = 0.9489, 0.9028
        if r5 < baseline_r5 - 0.01 or n5 < baseline_n5 - 0.01:
            print("FAIL: regression vs issue baseline", file=sys.stderr)
            return 1
        return 0
    finally:
        await daemon.shutdown()


def _ndcg(predicted: list[str], relevant: set[str]) -> float:
    import math
    dcg = sum(
        1.0 / math.log2(i + 2) for i, p in enumerate(predicted) if p in relevant
    )
    ideal = sum(1.0 / math.log2(i + 2) for i in range(min(len(predicted), len(relevant))))
    return dcg / ideal if ideal > 0 else 0.0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
```

- [ ] **Step 2: Add the Makefile target**

Append to `Makefile`:

```makefile
.PHONY: bench-search
bench-search:
	@test -n "$$GBRAIN_EVALS_DIR" || (echo "set GBRAIN_EVALS_DIR" && exit 1)
	@test -n "$$NEXUS_DATABASE_URL" || (echo "set NEXUS_DATABASE_URL" && exit 1)
	uv run python tests/benchmarks/gbrain_eval.py
```

- [ ] **Step 3: Smoke-run with a tiny fixture (don't gate on real numbers)**

Create `tests/benchmarks/_tiny_fixture/corpus.jsonl` and `queries.jsonl` (5 docs / 2 queries) and run:

```bash
GBRAIN_EVALS_DIR=tests/benchmarks/_tiny_fixture \
NEXUS_DATABASE_URL=postgresql+asyncpg://localhost/nexus_bench \
uv run python tests/benchmarks/gbrain_eval.py
```

Expected: prints recall + ndcg numbers, exits 0 (or 1 if tiny fixture trips the threshold; that's fine — we're checking the harness, not the score).

- [ ] **Step 4: Commit**

```bash
git add tests/benchmarks/gbrain_eval.py tests/benchmarks/_tiny_fixture/ Makefile
git commit -m "feat(#3699): bench-search Makefile target + gbrain-evals harness for cutover gate"
```

---

## Final verification

- [ ] **Run the entire search test suite**

Run: `uv run pytest tests/unit/bricks/search/ tests/integration/bricks/search/ -v`
Expected: all pass.

- [ ] **Run the daemon-level integration tests for non-search consumers**

Run: `uv run pytest tests/integration/server/ -v -k "search or daemon" --timeout=60`
Expected: all pass — confirms no consumer broke.

- [ ] **Verify deps**

Run: `uv pip list | grep -iE "txtai|torch|transformers|faiss"`
Expected: empty.

- [ ] **Run gbrain-evals against the real corpus** (manual gate, blocking PR merge)

Document numbers in the PR body. Merge only if `recall@5 ≥ 0.94` and `NDCG@5 ≥ 0.89`.

- [ ] **Open PR**

Title: `feat(#3699): drop txtai for direct pgvector + pgtext SQL`
Body must include:
- Link to spec: `docs/superpowers/specs/2026-05-03-drop-txtai-design.md`
- Link to issue #3699
- gbrain-evals benchmark numbers (manual run output)
- Migration notes for downstream: hard cutover, lazy backfill window where semantic recall is degraded until the background task drains
