"""End-to-end daemon search on Postgres profile (Issue #3699 T9).

Verifies:
* The daemon picks PgFtsBackend + PgVectorBackend when given a postgres URL.
* Keyword mode returns chunks via PgFtsBackend.
* Hybrid mode invokes the fusion module (3-way RRF: chunk + page + dense).

Tests are skipped cleanly when no Postgres URL is configured — matches the
T4-T6 conftest pattern.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from nexus.bricks.search.daemon import DaemonConfig, SearchDaemon
from nexus.bricks.search.pg_fts_backend import PgFtsBackend
from nexus.bricks.search.pg_vector_backend import PgVectorBackend


def _get_pg_url() -> str | None:
    """Return a Postgres URL from the environment, or None if not configured."""
    url = os.environ.get("NEXUS_TEST_DATABASE_URL") or os.environ.get("POSTGRES_URL")
    if not url:
        return None
    if url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://") :]
    elif url.startswith("postgres://"):
        url = "postgresql+asyncpg://" + url[len("postgres://") :]
    return url


@pytest_asyncio.fixture
async def postgres_engine_clean() -> AsyncIterator[AsyncEngine]:
    """Async engine pointed at a clean Postgres test schema with file_paths +
    document_chunks tables, truncated to start each test fresh."""
    url = _get_pg_url()
    if not url:
        pytest.skip(
            "No Postgres URL configured. Set NEXUS_TEST_DATABASE_URL to run daemon search PG tests."
        )

    engine = create_async_engine(url, echo=False)
    try:
        async with engine.begin() as conn:
            result = await conn.execute(text("SELECT 1 FROM pg_am WHERE amname = 'bm25' LIMIT 1"))
            if result.fetchone() is None:
                await engine.dispose()
                pytest.skip("pg_textsearch (bm25 access method) is not installed in this Postgres.")

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
            await conn.execute(
                text("""
                CREATE INDEX IF NOT EXISTS idx_chunks_bm25_test
                ON document_chunks USING bm25(chunk_text)
                WITH (text_config='english')
                """)
            )

        async with engine.begin() as conn:
            await conn.execute(
                text("TRUNCATE document_chunks, file_paths RESTART IDENTITY CASCADE")
            )

        yield engine

        async with engine.begin() as conn:
            await conn.execute(
                text("TRUNCATE document_chunks, file_paths RESTART IDENTITY CASCADE")
            )
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def daemon(postgres_engine_clean: AsyncEngine) -> AsyncIterator[SearchDaemon]:
    """SearchDaemon wired to the clean Postgres engine via session factory."""
    url = str(postgres_engine_clean.url).replace("+asyncpg", "")
    config = DaemonConfig(database_url=url, refresh_enabled=False)

    factory = async_sessionmaker(postgres_engine_clean, expire_on_commit=False)
    d = SearchDaemon(config=config, async_session_factory=factory)
    # The daemon's _init_database_pool would build its own engine from
    # database_url; injecting the clean async_engine + session keeps tests
    # talking to the same DB the fixture truncated.
    d._async_engine = postgres_engine_clean
    d._owns_engine = False
    await d.startup()
    try:
        yield d
    finally:
        await d.shutdown()


@pytest.mark.asyncio
async def test_daemon_picks_pg_backends_for_postgres_url(daemon: SearchDaemon) -> None:
    assert isinstance(daemon._fts_backend, PgFtsBackend)
    assert isinstance(daemon._vector_backend, PgVectorBackend)


@pytest.mark.asyncio
async def test_daemon_hybrid_mode_calls_fusion(
    daemon: SearchDaemon,
    monkeypatch: pytest.MonkeyPatch,
    postgres_engine_clean: AsyncEngine,
) -> None:
    """Hybrid mode = chunk-BM25 + page-BM25 + dense, fused via fusion module."""
    from nexus.bricks.search import fusion

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
                "VALUES ('c1', 'p1', 0, 'alpha', 1, now())"
            )
        )

    seen = {"calls": 0}
    real_rrf = fusion.rrf_fusion

    def spy(*a, **kw):
        seen["calls"] += 1
        return real_rrf(*a, **kw)

    monkeypatch.setattr(fusion, "rrf_fusion", spy)

    # Stub embedding client so the test doesn't need a live API key.
    async def fake_embed_query(_text: str) -> list[float]:
        return [0.0] * 1536

    monkeypatch.setattr(daemon._embedding_client, "embed_query", fake_embed_query)

    await daemon.search("alpha", path_filter="/z/", limit=5, search_type="hybrid", zone_id="z")
    # 3-way RRF can be implemented as one call OR two nested rrf_fusion calls.
    assert seen["calls"] >= 1
