"""PgVectorBackend conformance + cosine-ordering tests over halfvec(1536).

Uses a live Postgres engine (``NEXUS_TEST_DATABASE_URL`` env var) with
pgvector loaded. Each DB test is skipped when:
  (a) No Postgres URL is configured, OR
  (b) pgvector extension is not installed in that Postgres.

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

from nexus.bricks.search.pg_vector_backend import PgVectorBackend
from nexus.bricks.search.protocols import SearchBackend

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _get_pg_url() -> str | None:
    """Return a Postgres URL from the environment, or None if not configured.

    Reads NEXUS_TEST_DATABASE_URL (dedicated test DB — preferred) or
    POSTGRES_URL (fallback). Returns None when neither is set so tests can
    be skipped cleanly.
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
    """Async engine pointed at a clean Postgres test schema with pgvector.

    Skips if no Postgres URL is configured or pgvector is not installed.
    Creates the ``file_paths`` and ``document_chunks`` tables (with the
    halfvec embedding column) if absent, then truncates them so each test
    starts with an empty state.
    """
    url = _get_pg_url()
    if not url:
        pytest.skip(
            "No Postgres URL configured. Set NEXUS_TEST_DATABASE_URL to run PgVectorBackend tests."
        )

    engine = create_async_engine(url, echo=False)
    try:
        async with engine.begin() as conn:
            # Verify pgvector extension is available.
            result = await conn.execute(
                text("SELECT 1 FROM pg_extension WHERE extname = 'vector' LIMIT 1")
            )
            if result.fetchone() is None:
                await engine.dispose()
                pytest.skip(
                    "pgvector extension is not installed in this Postgres. "
                    "Install with: CREATE EXTENSION vector;"
                )

            # Ensure required tables exist (minimal DDL for test isolation).
            await conn.execute(
                text("""
                CREATE TABLE IF NOT EXISTS file_paths (
                    path_id      TEXT PRIMARY KEY,
                    zone_id      TEXT NOT NULL,
                    virtual_path TEXT NOT NULL,
                    deleted_at   TIMESTAMPTZ,
                    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)
            )
            await conn.execute(
                text("""
                CREATE TABLE IF NOT EXISTS document_chunks (
                    chunk_id     TEXT PRIMARY KEY,
                    path_id      TEXT NOT NULL REFERENCES file_paths(path_id) ON DELETE CASCADE,
                    chunk_index  INTEGER NOT NULL,
                    chunk_text   TEXT NOT NULL,
                    chunk_tokens INTEGER NOT NULL,
                    embedding    halfvec(1536),
                    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)
            )
            # Create HNSW index if not present (cosine distance, matches production).
            await conn.execute(
                text("""
                CREATE INDEX IF NOT EXISTS idx_chunks_embedding_hnsw_test
                ON document_chunks USING hnsw (embedding halfvec_cosine_ops)
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
    """PgVectorBackend wired to the clean Postgres engine."""
    return PgVectorBackend(engine=postgres_engine_clean)


# ---------------------------------------------------------------------------
# Seed helper
# ---------------------------------------------------------------------------


async def _seed_with_embeddings(engine: AsyncEngine, rows: list[dict]) -> None:
    """Insert file_paths + document_chunks rows with halfvec embeddings."""
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
                    "(chunk_id, path_id, chunk_index, chunk_text, chunk_tokens, "
                    " embedding, created_at) "
                    "VALUES (:cid, :pid, :idx, :txt, :tok, "
                    "        CAST(:emb AS halfvec), now())"
                ),
                {
                    "cid": r["chunk_id"],
                    "pid": r["path_id"],
                    "idx": r["chunk_index"],
                    "txt": r["text"],
                    "tok": len(r["text"].split()),
                    "emb": str(list(r["emb"])),
                },
            )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_satisfies_protocol():
    """PgVectorBackend satisfies SearchBackend without a database connection."""
    # Build with a mock engine — no real DB needed for isinstance() check.
    from unittest.mock import MagicMock

    mock_engine = MagicMock(spec=AsyncEngine)
    b = PgVectorBackend(engine=mock_engine)
    assert isinstance(b, SearchBackend)


@pytest.mark.asyncio
async def test_semantic_search_orders_by_cosine(
    backend: PgVectorBackend, postgres_engine_clean: AsyncEngine
):
    """Cosine KNN returns results ordered nearest-first (highest cosine sim first)."""
    qvec = [1.0] + [0.0] * 1535
    near = [0.99] + [0.01] * 1535
    far = [0.0, 1.0] + [0.0] * 1534

    await _seed_with_embeddings(
        postgres_engine_clean,
        [
            {
                "chunk_id": "near",
                "path_id": "p1",
                "zone_id": "z",
                "path": "/z/a.txt",
                "chunk_index": 0,
                "text": "near",
                "emb": near,
            },
            {
                "chunk_id": "far",
                "path_id": "p2",
                "zone_id": "z",
                "path": "/z/b.txt",
                "chunk_index": 0,
                "text": "far",
                "emb": far,
            },
        ],
    )
    hits = await backend.semantic_search(qvec, "/z/", k=10, zone_id="z")
    assert [h.path for h in hits] == ["/z/a.txt", "/z/b.txt"]


@pytest.mark.asyncio
async def test_null_embedding_skipped(backend: PgVectorBackend, postgres_engine_clean: AsyncEngine):
    """Chunks with a NULL embedding must not appear in semantic_search results."""
    async with postgres_engine_clean.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO file_paths (path_id, zone_id, virtual_path, deleted_at) "
                "VALUES ('p1', 'z', '/z/a.txt', NULL)"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO document_chunks "
                "(chunk_id, path_id, chunk_index, chunk_text, chunk_tokens, created_at) "
                "VALUES ('c1', 'p1', 0, 'x', 1, now())"  # embedding intentionally NULL
            )
        )
    hits = await backend.semantic_search([0.0] * 1536, "/z/", k=10, zone_id="z")
    assert hits == []


@pytest.mark.asyncio
async def test_zone_isolation(backend: PgVectorBackend, postgres_engine_clean: AsyncEngine):
    """semantic_search does not leak results across zone boundaries."""
    await _seed_with_embeddings(
        postgres_engine_clean,
        [
            {
                "chunk_id": "c1",
                "path_id": "p1",
                "zone_id": "z1",
                "path": "/z1/a.txt",
                "chunk_index": 0,
                "text": "x",
                "emb": [1.0] + [0.0] * 1535,
            },
            {
                "chunk_id": "c2",
                "path_id": "p2",
                "zone_id": "z2",
                "path": "/z2/a.txt",
                "chunk_index": 0,
                "text": "x",
                "emb": [1.0] + [0.0] * 1535,
            },
        ],
    )
    hits = await backend.semantic_search([1.0] + [0.0] * 1535, "/", k=10, zone_id="z1")
    assert {h.path for h in hits} == {"/z1/a.txt"}
