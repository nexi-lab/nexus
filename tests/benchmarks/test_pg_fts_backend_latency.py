"""Opt-in PgFtsBackend corpus-growth latency benchmark for issue #4244."""

from __future__ import annotations

import os
import statistics
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from nexus.bricks.search.pg_fts_backend import PgFtsBackend

BENCH_SCHEMA = "nexus_pg_fts_bench_4244"
BENCH_ZONE_ID = "bench"
CORPUS_SIZES = (1_000, 5_000, 25_000)
SAMPLE_COUNT = 7
QUERY = "needle4244 latency regression"


_BM25_DDL_BY_EXTENSION = {
    "pg_search": """
        CREATE INDEX idx_chunks_bench_4244_bm25
        ON document_chunks USING bm25 (chunk_id, chunk_text)
        WITH (key_field='chunk_id', text_fields='{"chunk_text": {}}')
    """,
    "pg_textsearch": """
        CREATE INDEX idx_chunks_bench_4244_bm25
        ON document_chunks USING bm25(chunk_text)
        WITH (text_config='english')
    """,
}


def _get_pg_url() -> str | None:
    url = (
        os.environ.get("NEXUS_TEST_DATABASE_URL")
        or os.environ.get("NEXUS_DATABASE_URL")
        or os.environ.get("POSTGRES_URL")
    )
    if not url:
        return None
    if url.startswith("postgresql+asyncpg://"):
        return url
    if url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url[len("postgresql://") :]
    if url.startswith("postgres://"):
        return "postgresql+asyncpg://" + url[len("postgres://") :]
    return url


async def _drop_schema(url: str) -> None:
    engine = create_async_engine(url, echo=False)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f"DROP SCHEMA IF EXISTS {BENCH_SCHEMA} CASCADE"))
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def pg_fts_bench_engine() -> AsyncIterator[AsyncEngine]:
    url = _get_pg_url()
    if not url:
        pytest.skip(
            "No Postgres URL configured. Set NEXUS_TEST_DATABASE_URL, "
            "NEXUS_DATABASE_URL, or POSTGRES_URL to run PgFts latency benchmarks."
        )

    bootstrap = create_async_engine(url, echo=False)
    try:
        async with bootstrap.begin() as conn:
            await conn.execute(text(f"DROP SCHEMA IF EXISTS {BENCH_SCHEMA} CASCADE"))
            await conn.execute(text(f"CREATE SCHEMA {BENCH_SCHEMA}"))
    except Exception as exc:
        await bootstrap.dispose()
        pytest.skip(f"Postgres benchmark database is not reachable: {exc}")
    finally:
        await bootstrap.dispose()

    engine = create_async_engine(
        url,
        echo=False,
        connect_args={"server_settings": {"search_path": BENCH_SCHEMA}},
    )
    try:
        await _ensure_schema(engine)
        yield engine
    finally:
        await engine.dispose()
        await _drop_schema(url)


async def _ensure_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text("""
            CREATE TABLE file_paths (
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
            CREATE TABLE document_chunks (
                chunk_id     TEXT PRIMARY KEY,
                path_id      TEXT NOT NULL REFERENCES file_paths(path_id) ON DELETE CASCADE,
                chunk_index  INTEGER NOT NULL,
                chunk_text   TEXT NOT NULL,
                chunk_tokens INTEGER NOT NULL,
                created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """)
        )
        await conn.execute(
            text("""
            CREATE INDEX idx_file_paths_bench_4244_zone_path
            ON file_paths (zone_id, virtual_path)
            """)
        )
        await conn.execute(
            text("""
            CREATE INDEX idx_chunks_bench_4244_native_fts
            ON document_chunks USING GIN (to_tsvector('english', chunk_text))
            """)
        )
        await _create_optional_bm25_index(conn)


async def _create_optional_bm25_index(conn: Any) -> None:
    has_bm25 = (
        await conn.execute(text("SELECT 1 FROM pg_am WHERE amname = 'bm25' LIMIT 1"))
    ).scalar_one_or_none()
    if not has_bm25:
        return

    rows = (
        await conn.execute(
            text("SELECT extname FROM pg_extension WHERE extname IN ('pg_search', 'pg_textsearch')")
        )
    ).fetchall()
    installed = {row[0] for row in rows}
    ddl_candidates = [ddl for ext, ddl in _BM25_DDL_BY_EXTENSION.items() if ext in installed]
    ddl_candidates.extend(
        ddl for ext, ddl in _BM25_DDL_BY_EXTENSION.items() if ext not in installed
    )

    for index, ddl in enumerate(ddl_candidates):
        savepoint = f"try_bench_4244_bm25_{index}"
        try:
            await conn.execute(text(f"SAVEPOINT {savepoint}"))
            await conn.execute(text(ddl))
            await conn.execute(text(f"RELEASE SAVEPOINT {savepoint}"))
            return
        except Exception:
            await conn.execute(text(f"ROLLBACK TO SAVEPOINT {savepoint}"))
            await conn.execute(text(f"RELEASE SAVEPOINT {savepoint}"))


def _doc_text(index: int) -> str:
    if index % 10 == 0:
        marker = "needle4244 latency regression pgfts backend timing"
    else:
        marker = "background corpus search indexing permission routing"
    return (
        f"{marker} document {index} issue 4244 edge sha256 df14670 "
        "postgres full text search benchmark synthetic corpus growth."
    )


async def _seed_corpus(engine: AsyncEngine, corpus_size: int) -> None:
    file_rows = [
        {
            "path_id": f"bench-path-{i}",
            "zone_id": BENCH_ZONE_ID,
            "virtual_path": f"/bench/doc-{i:05d}.md",
        }
        for i in range(corpus_size)
    ]
    chunk_rows = [
        {
            "chunk_id": f"bench-chunk-{i}",
            "path_id": f"bench-path-{i}",
            "chunk_index": 0,
            "chunk_text": _doc_text(i),
            "chunk_tokens": max(1, len(_doc_text(i).split())),
        }
        for i in range(corpus_size)
    ]
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE document_chunks, file_paths"))
        await conn.execute(
            text("""
            INSERT INTO file_paths (path_id, zone_id, virtual_path, deleted_at)
            VALUES (:path_id, :zone_id, :virtual_path, NULL)
            """),
            file_rows,
        )
        await conn.execute(
            text("""
            INSERT INTO document_chunks
                (chunk_id, path_id, chunk_index, chunk_text, chunk_tokens, created_at)
            VALUES
                (:chunk_id, :path_id, :chunk_index, :chunk_text, :chunk_tokens, now())
            """),
            chunk_rows,
        )


async def _measure_samples(
    call: Callable[[], Awaitable[list[Any]]],
    *,
    samples: int = SAMPLE_COUNT,
) -> tuple[list[float], int]:
    samples_ms: list[float] = []
    max_result_count = 0
    for _ in range(samples):
        start = time.perf_counter()
        results = await call()
        samples_ms.append((time.perf_counter() - start) * 1000)
        max_result_count = max(max_result_count, len(results))
    return samples_ms, max_result_count


def _summary(samples_ms: list[float]) -> dict[str, float]:
    ordered = sorted(samples_ms)
    return {
        "min_ms": min(samples_ms),
        "median_ms": statistics.median(samples_ms),
        "p95_ms": ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))],
        "max_ms": max(samples_ms),
    }


def _format_summary(stats: dict[str, float]) -> str:
    return ", ".join(f"{key}={value:.2f}" for key, value in stats.items())


@pytest.mark.asyncio
@pytest.mark.benchmark
@pytest.mark.parametrize("corpus_size", CORPUS_SIZES)
async def test_pg_fts_backend_latency_scales_with_corpus(
    pg_fts_bench_engine: AsyncEngine,
    corpus_size: int,
    record_property: Callable[[str, object], None],
) -> None:
    await _seed_corpus(pg_fts_bench_engine, corpus_size)
    backend = PgFtsBackend(engine=pg_fts_bench_engine)
    await backend.startup()

    await backend.keyword_search(QUERY, "/bench/", 10, BENCH_ZONE_ID)
    await backend.keyword_search_pages(QUERY, "/bench/", 10, BENCH_ZONE_ID)

    chunk_samples, chunk_result_count = await _measure_samples(
        lambda: backend.keyword_search(QUERY, "/bench/", 10, BENCH_ZONE_ID)
    )
    page_samples, page_result_count = await _measure_samples(
        lambda: backend.keyword_search_pages(QUERY, "/bench/", 10, BENCH_ZONE_ID)
    )

    chunk_summary = _summary(chunk_samples)
    page_summary = _summary(page_samples)
    record_property(f"pg_fts_{corpus_size}_chunk", _format_summary(chunk_summary))
    record_property(f"pg_fts_{corpus_size}_page", _format_summary(page_summary))

    assert chunk_result_count > 0
    assert page_result_count > 0
