"""Integration test for PgVectorBackend.fetch_ranges (Issue #4398 T6).

Verifies that fetch_ranges correctly pulls contiguous chunks from Postgres and
returns them as ChunkRow objects conforming to the NeighborFetcher protocol.

Skips cleanly when NEXUS_TEST_DATABASE_URL / POSTGRES_URL is unset — matches
the established pattern across the search integration suite. CI runs this
against a real Postgres instance.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from nexus.bricks.search.pg_vector_backend import PgVectorBackend


def _get_pg_url() -> str | None:
    """Return an asyncpg-dialect Postgres URL from the environment, or None."""
    url = os.environ.get("NEXUS_TEST_DATABASE_URL") or os.environ.get("POSTGRES_URL")
    if not url:
        return None
    if url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://") :]
    elif url.startswith("postgres://"):
        url = "postgresql+asyncpg://" + url[len("postgres://") :]
    return url


@pytest_asyncio.fixture
async def pg_engine_macro() -> AsyncIterator[AsyncEngine]:
    """Async engine with file_paths + document_chunks tables seeded for fetch_ranges tests.

    Creates the extended document_chunks schema that includes line_start,
    line_end, and heading_prefix — columns required by fetch_ranges but absent
    from the minimal DDL used in the daemon-search fixture.

    Yields the engine then truncates and disposes on teardown.
    """
    url = _get_pg_url()
    if not url:
        pytest.skip(
            "No Postgres URL configured. Set NEXUS_TEST_DATABASE_URL to run macro-fetch PG tests."
        )

    engine = create_async_engine(url, echo=False)
    try:
        async with engine.begin() as conn:
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
            await conn.execute(text("DROP TABLE IF EXISTS document_chunks CASCADE"))
            await conn.execute(
                text("""
                CREATE TABLE document_chunks (
                    chunk_id       TEXT PRIMARY KEY,
                    path_id        TEXT NOT NULL REFERENCES file_paths(path_id) ON DELETE CASCADE,
                    chunk_index    INTEGER NOT NULL,
                    chunk_text     TEXT NOT NULL,
                    chunk_tokens   INTEGER,
                    line_start     INTEGER,
                    line_end       INTEGER,
                    heading_prefix TEXT,
                    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """)
            )

        # Seed one path with three chunks (indices 0, 1, 2).
        async with engine.begin() as conn:
            await conn.execute(
                text("TRUNCATE document_chunks, file_paths RESTART IDENTITY CASCADE")
            )
            await conn.execute(
                text(
                    "INSERT INTO file_paths (path_id, zone_id, virtual_path, deleted_at) "
                    "VALUES ('macro-p1', 'z1', '/ws/doc.md', NULL)"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO document_chunks "
                    "(chunk_id, path_id, chunk_index, chunk_text, chunk_tokens, "
                    " line_start, line_end, heading_prefix) VALUES "
                    "('macro-c0', 'macro-p1', 0, 'intro text', 10, 1, 5, '## Intro'), "
                    "('macro-c1', 'macro-p1', 1, 'body text',  20, 6, 12, '## Intro'), "
                    "('macro-c2', 'macro-p1', 2, 'outro text', 15, 13, 18, '## Outro')"
                )
            )

        yield engine

        async with engine.begin() as conn:
            await conn.execute(
                text("TRUNCATE document_chunks, file_paths RESTART IDENTITY CASCADE")
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_fetch_ranges_returns_all_chunks_in_span(pg_engine_macro: AsyncEngine) -> None:
    """fetch_ranges([('/ws/doc.md', 0, 2)], 'z1') returns all three seeded chunks."""
    backend = PgVectorBackend(pg_engine_macro)
    rows = await backend.fetch_ranges([("/ws/doc.md", 0, 2)], zone_id="z1")

    idxs = sorted(r.chunk_index for r in rows)
    assert idxs == [0, 1, 2]
    assert all(r.path == "/ws/doc.md" for r in rows)
    assert all(r.tokens >= 0 for r in rows)


@pytest.mark.asyncio
async def test_fetch_ranges_respects_zone_isolation(pg_engine_macro: AsyncEngine) -> None:
    """fetch_ranges with a different zone_id returns no rows."""
    backend = PgVectorBackend(pg_engine_macro)
    rows = await backend.fetch_ranges([("/ws/doc.md", 0, 2)], zone_id="other-zone")
    assert rows == []


@pytest.mark.asyncio
async def test_fetch_ranges_empty_spans(pg_engine_macro: AsyncEngine) -> None:
    """fetch_ranges with an empty spans list short-circuits and returns []."""
    backend = PgVectorBackend(pg_engine_macro)
    rows = await backend.fetch_ranges([], zone_id="z1")
    assert rows == []


@pytest.mark.asyncio
async def test_fetch_ranges_partial_span(pg_engine_macro: AsyncEngine) -> None:
    """fetch_ranges honours inclusive lo/hi bounds — (0, 1) excludes chunk 2."""
    backend = PgVectorBackend(pg_engine_macro)
    rows = await backend.fetch_ranges([("/ws/doc.md", 0, 1)], zone_id="z1")
    idxs = sorted(r.chunk_index for r in rows)
    assert idxs == [0, 1]


@pytest.mark.asyncio
async def test_fetch_ranges_chunk_row_fields(pg_engine_macro: AsyncEngine) -> None:
    """ChunkRow fields (line_start, line_end, heading_prefix) are populated."""
    backend = PgVectorBackend(pg_engine_macro)
    rows = await backend.fetch_ranges([("/ws/doc.md", 0, 0)], zone_id="z1")
    assert len(rows) == 1
    row = rows[0]
    assert row.chunk_index == 0
    assert row.line_start == 1
    assert row.line_end == 5
    assert row.heading_prefix == "## Intro"
    assert row.text == "intro text"
    assert row.tokens == 10
