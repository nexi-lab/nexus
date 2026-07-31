"""Conditional over-fetch for tier weights (Issue #4544).

When the effective zone has a path_contexts weight != 1.0 the daemon must
widen its backend fetch (limit × tier_boost_overfetch_factor), apply the
weights, re-sort, and trim back to the requested limit — so a boost can
promote a hit that the un-widened fusion would have cut. Zones with no
weights must hit the byte-identical legacy path (no widened fetch).
"""

from __future__ import annotations

from types import MethodType
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from nexus.bricks.search.path_context import PathContextCache, PathContextStore

CREATE_TABLE_SQL = """
CREATE TABLE path_contexts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    zone_id TEXT NOT NULL DEFAULT 'root',
    path_prefix TEXT NOT NULL,
    description TEXT NOT NULL,
    weight FLOAT,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(zone_id, path_prefix)
)
"""


@pytest_asyncio.fixture
async def cache():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.exec_driver_sql(CREATE_TABLE_SQL)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    store = PathContextStore(async_session_factory=factory, db_type="sqlite")
    yield PathContextCache(store=store)
    await engine.dispose()


def _make_daemon(cache: PathContextCache) -> Any:
    """Keyword-only daemon: N corpus docs, keyword_search honours ``limit``
    and records the limits it was asked for."""
    from nexus.bricks.search.daemon import DaemonConfig, SearchDaemon, SearchResult

    corpus = [
        SearchResult(path="chat/a.md", chunk_text="", score=10.0, search_type="keyword"),
        SearchResult(path="chat/b.md", chunk_text="", score=9.0, search_type="keyword"),
        SearchResult(path="docs/x.md", chunk_text="", score=8.0, search_type="keyword"),
    ]

    class FakeFtsBackend:
        def __init__(self) -> None:
            self.requested_limits: list[int] = []

        async def keyword_search(
            self,
            query: str,
            path: str,
            limit: int,
            zone_id: str,
            *,
            timing: dict[str, float] | None = None,
        ) -> list[Any]:
            self.requested_limits.append(limit)
            # Fresh copies: score mutation must not leak between searches.
            return [
                SearchResult(
                    path=r.path,
                    chunk_text=r.chunk_text,
                    score=r.score,
                    search_type=r.search_type,
                )
                for r in corpus[:limit]
            ]

    class FakeVectorBackend:
        async def semantic_search(
            self, qvec: list[float], path: str, limit: int, zone_id: str
        ) -> list[Any]:
            return []

    class _Stats:
        path_context_attach_failures = 0
        path_context_resolve_failures = 0

    daemon: Any = SearchDaemon.__new__(SearchDaemon)
    daemon.last_search_timing = {}
    daemon._initialized = True
    daemon._fts_backend = FakeFtsBackend()
    daemon._vector_backend = FakeVectorBackend()
    daemon._path_context_cache = cache
    daemon._path_context_cache_by_loop = {}
    daemon._path_context_engines_by_loop = {}
    daemon.stats = _Stats()
    daemon._track_latency = MethodType(lambda self, ms: None, daemon)
    daemon.config = DaemonConfig(page_aggregation=False)
    return daemon


class TestOverfetchAndTrim:
    @pytest.mark.asyncio
    async def test_demotion_promotes_below_cutoff_hit_into_returned_set(self, cache) -> None:
        # limit=2 without weights returns the two chat docs. Demoting chat
        # must let docs/x.md (rank 3, beyond the un-widened fetch) into the
        # top 2 — this is exactly the promotion the over-fetch exists for.
        await cache._store.upsert("root", "chat", "Chat transcripts", weight=0.5)
        daemon = _make_daemon(cache)
        results = await daemon._search_on_current_loop(
            "q", search_type="keyword", limit=2, zone_id="root"
        )
        assert len(results) == 2  # trimmed back to the requested limit
        assert daemon._fts_backend.requested_limits == [2 * 3]  # widened fetch
        paths = [r.path for r in results]
        assert "docs/x.md" in paths
        assert results[0].path == "docs/x.md"  # 8.0 beats 10.0*0.5

    @pytest.mark.asyncio
    async def test_no_weights_keeps_legacy_fetch_size(self, cache) -> None:
        await cache._store.upsert("root", "chat", "Chat transcripts")  # no weight
        daemon = _make_daemon(cache)
        results = await daemon._search_on_current_loop(
            "q", search_type="keyword", limit=2, zone_id="root"
        )
        assert daemon._fts_backend.requested_limits == [2]  # byte-identical path
        assert [r.path for r in results] == ["chat/a.md", "chat/b.md"]
        assert all(r.tier_boost is None for r in results)

    @pytest.mark.asyncio
    async def test_weight_one_rows_do_not_trigger_overfetch(self, cache) -> None:
        await cache._store.upsert("root", "chat", "Chat transcripts", weight=1.0)
        daemon = _make_daemon(cache)
        await daemon._search_on_current_loop("q", search_type="keyword", limit=2, zone_id="root")
        assert daemon._fts_backend.requested_limits == [2]

    @pytest.mark.asyncio
    async def test_probe_failure_fails_soft(self, cache) -> None:
        daemon = _make_daemon(cache)

        async def _boom(self: Any) -> Any:
            raise RuntimeError("cache resolution exploded")

        daemon._resolve_path_context_cache = MethodType(_boom, daemon)
        results = await daemon._search_on_current_loop(
            "q", search_type="keyword", limit=2, zone_id="root"
        )
        assert [r.path for r in results] == ["chat/a.md", "chat/b.md"]

    @pytest.mark.asyncio
    async def test_zero_overfetch_factor_clamped_to_one(self, cache) -> None:
        await cache._store.upsert("root", "chat", "Chat transcripts", weight=0.5)
        daemon = _make_daemon(cache)
        daemon.config.tier_boost_overfetch_factor = 0
        results = await daemon._search_on_current_loop(
            "q", search_type="keyword", limit=2, zone_id="root"
        )
        assert daemon._fts_backend.requested_limits == [2]  # clamped, not zeroed
        assert len(results) == 2
