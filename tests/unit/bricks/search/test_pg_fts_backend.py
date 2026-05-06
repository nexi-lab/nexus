"""PgFtsBackend conformance + correctness tests (Issue #3699).

Uses a live Postgres engine (``NEXUS_TEST_DATABASE_URL`` env var) with
pg_textsearch loaded. Each DB test is skipped when:
  (a) No Postgres URL is configured, OR
  (b) pg_textsearch extension is not installed in that Postgres.

The protocol conformance test (``test_satisfies_protocol``) runs without
any database — it only checks isinstance() against SearchBackend.

Note: The parent ``tests/unit/conftest.py`` auto-clears ``NEXUS_DATABASE_URL``
via ``isolate_test_database``. The ``postgres_engine_clean`` fixture below
reads ``NEXUS_TEST_DATABASE_URL`` which is a separate env var intentionally
NOT cleared by that fixture, so DB tests can still access a test-dedicated
Postgres instance when available.
"""

from __future__ import annotations

import os

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from nexus.bricks.search.pg_fts_backend import PgFtsBackend
from nexus.bricks.search.protocols import SearchBackend

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _get_pg_url() -> str | None:
    """Return a Postgres URL from the environment, or None if not configured.

    Reads NEXUS_TEST_DATABASE_URL (dedicated test DB — preferred) or
    NEXUS_DATABASE_URL (production DB — only if running in a safe context).
    Returns None when neither is set so tests can be skipped cleanly.
    """
    url = os.environ.get("NEXUS_TEST_DATABASE_URL") or os.environ.get("POSTGRES_URL")
    if not url:
        return None
    # Ensure asyncpg driver prefix.
    if url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://") :]
    elif url.startswith("postgres://"):
        url = "postgresql+asyncpg://" + url[len("postgres://") :]
    return url


@pytest_asyncio.fixture
async def postgres_engine_clean():
    """Async engine pointed at a clean Postgres test schema.

    Skips if no Postgres URL is configured or pg_textsearch is not installed.
    Creates the ``file_paths`` and ``document_chunks`` tables if they are
    absent, then truncates them so each test starts with an empty state.
    """
    url = _get_pg_url()
    if not url:
        pytest.skip(
            "No Postgres URL configured. Set NEXUS_TEST_DATABASE_URL to run PgFtsBackend tests."
        )

    engine = create_async_engine(url, echo=False)
    try:
        async with engine.begin() as conn:
            # Verify pg_textsearch (@@@) operator is available.
            # We do this by checking for the BM25 access method in pg_am.
            result = await conn.execute(text("SELECT 1 FROM pg_am WHERE amname = 'bm25' LIMIT 1"))
            if result.fetchone() is None:
                await engine.dispose()
                pytest.skip(
                    "pg_textsearch (bm25 access method) is not installed in this Postgres. "
                    "Requires PostgreSQL 17+ with the pg_textsearch extension."
                )

            # Ensure required tables exist (minimal DDL for test isolation).
            await conn.execute(
                text("""
                CREATE TABLE IF NOT EXISTS file_paths (
                    path_id     TEXT PRIMARY KEY,
                    zone_id     TEXT NOT NULL,
                    virtual_path TEXT NOT NULL,
                    deleted_at  TIMESTAMPTZ,
                    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)
            )
            await conn.execute(
                text("""
                CREATE TABLE IF NOT EXISTS document_chunks (
                    chunk_id    TEXT PRIMARY KEY,
                    path_id     TEXT NOT NULL REFERENCES file_paths(path_id) ON DELETE CASCADE,
                    chunk_index INTEGER NOT NULL,
                    chunk_text  TEXT NOT NULL,
                    chunk_tokens INTEGER NOT NULL,
                    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)
            )
            # Create BM25 index if not present.
            await conn.execute(
                text("""
                CREATE INDEX IF NOT EXISTS idx_chunks_bm25_test
                ON document_chunks USING bm25(chunk_text)
                WITH (text_config='english')
            """)
            )

        # Truncate for a clean slate (preserve schema).
        async with engine.begin() as conn:
            await conn.execute(
                text("TRUNCATE document_chunks, file_paths RESTART IDENTITY CASCADE")
            )

        yield engine

        # Cleanup after test.
        async with engine.begin() as conn:
            await conn.execute(
                text("TRUNCATE document_chunks, file_paths RESTART IDENTITY CASCADE")
            )
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def backend(postgres_engine_clean: AsyncEngine):
    """PgFtsBackend wired to the clean Postgres engine."""
    return PgFtsBackend(engine=postgres_engine_clean)


# ---------------------------------------------------------------------------
# Seed helper
# ---------------------------------------------------------------------------


async def _seed(engine: AsyncEngine, rows: list[dict]) -> None:
    """Insert file_paths + document_chunks rows for test setup."""
    async with engine.begin() as conn:
        for r in rows:
            await conn.execute(
                text(
                    "INSERT INTO file_paths (path_id, zone_id, virtual_path, deleted_at) "
                    "VALUES (:pid, :zid, :path, NULL) ON CONFLICT DO NOTHING"
                ),
                {"pid": r["path_id"], "zid": r["zone_id"], "path": r["path"]},
            )
            await conn.execute(
                text(
                    "INSERT INTO document_chunks "
                    "(chunk_id, path_id, chunk_index, chunk_text, chunk_tokens, created_at) "
                    "VALUES (:cid, :pid, :idx, :txt, :tok, now())"
                ),
                {
                    "cid": r["chunk_id"],
                    "pid": r["path_id"],
                    "idx": r["chunk_index"],
                    "txt": r["text"],
                    "tok": len(r["text"].split()),
                },
            )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_satisfies_protocol():
    """PgFtsBackend satisfies SearchBackend without a database connection."""
    # Build with a mock engine — no real DB needed for isinstance() check.
    from unittest.mock import MagicMock

    mock_engine = MagicMock(spec=AsyncEngine)
    b = PgFtsBackend(engine=mock_engine)
    assert isinstance(b, SearchBackend)


@pytest.mark.asyncio
async def test_keyword_search_chunk_level(
    backend: PgFtsBackend, postgres_engine_clean: AsyncEngine
):
    """Chunk-level BM25 returns matching chunks ordered by score."""
    await _seed(
        postgres_engine_clean,
        [
            {
                "chunk_id": "c1",
                "path_id": "p1",
                "zone_id": "z",
                "path": "/z/a.txt",
                "chunk_index": 0,
                "text": "the quick brown fox",
            },
            {
                "chunk_id": "c2",
                "path_id": "p2",
                "zone_id": "z",
                "path": "/z/b.txt",
                "chunk_index": 0,
                "text": "lazy dogs sleep",
            },
        ],
    )
    hits = await backend.keyword_search("quick", "/z/", k=10, zone_id="z")
    assert [h.path for h in hits] == ["/z/a.txt"]


@pytest.mark.asyncio
async def test_path_prefix_filter(backend: PgFtsBackend, postgres_engine_clean: AsyncEngine):
    """keyword_search respects the path prefix filter."""
    await _seed(
        postgres_engine_clean,
        [
            {
                "chunk_id": "c1",
                "path_id": "p1",
                "zone_id": "z",
                "path": "/z/sub/a.txt",
                "chunk_index": 0,
                "text": "alpha",
            },
            {
                "chunk_id": "c2",
                "path_id": "p2",
                "zone_id": "z",
                "path": "/z/other/b.txt",
                "chunk_index": 0,
                "text": "alpha",
            },
        ],
    )
    hits = await backend.keyword_search("alpha", "/z/sub/", k=10, zone_id="z")
    assert {h.path for h in hits} == {"/z/sub/a.txt"}


@pytest.mark.asyncio
async def test_zone_isolation(backend: PgFtsBackend, postgres_engine_clean: AsyncEngine):
    """keyword_search does not leak results across zone boundaries."""
    await _seed(
        postgres_engine_clean,
        [
            {
                "chunk_id": "c1",
                "path_id": "p1",
                "zone_id": "z1",
                "path": "/z1/a.txt",
                "chunk_index": 0,
                "text": "alpha",
            },
            {
                "chunk_id": "c2",
                "path_id": "p2",
                "zone_id": "z2",
                "path": "/z2/a.txt",
                "chunk_index": 0,
                "text": "alpha",
            },
        ],
    )
    hits = await backend.keyword_search("alpha", "/", k=10, zone_id="z1")
    assert {h.path for h in hits} == {"/z1/a.txt"}


@pytest.mark.asyncio
async def test_keyword_search_pages_aggregates_chunks(
    backend: PgFtsBackend, postgres_engine_clean: AsyncEngine
):
    """Page-BM25 leg (#3980) — assemble chunks per path, BM25 over the page text.

    Two chunks belong to the same path. A rare phrase appears only in the
    second chunk. keyword_search_pages must surface the path (assembling both
    chunks into a page) and return exactly one result per path.
    """
    await _seed(
        postgres_engine_clean,
        [
            {
                "chunk_id": "c1",
                "path_id": "p1",
                "zone_id": "z",
                "path": "/z/a.txt",
                "chunk_index": 0,
                "text": "common preamble",
            },
            {
                "chunk_id": "c2",
                "path_id": "p1",
                "zone_id": "z",
                "path": "/z/a.txt",
                "chunk_index": 1,
                "text": "rare phrase XYZQQ deep in body",
            },
        ],
    )
    hits = await backend.keyword_search_pages("XYZQQ", "/z/", k=10, zone_id="z")
    assert [h.path for h in hits] == ["/z/a.txt"]
    # Page-level results: one row per path, not per chunk.
    assert len({h.path for h in hits}) == len(hits)
