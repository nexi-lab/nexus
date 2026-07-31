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
        tier_boost_probe_failures = 0
        tier_boost_suppressed_searches = 0

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
        # Codex review R3: an un-widened pool must stay UNWEIGHTED — a
        # demoted top-N hit could never be displaced by rank N+1, so the
        # weight is suppressed (and counted) rather than applied.
        assert all(r.tier_boost is None for r in results)
        assert results[0].score == 10.0
        assert daemon.stats.tier_boost_suppressed_searches == 1

    @pytest.mark.asyncio
    async def test_factor_one_suppresses_weights_and_counts(self, cache) -> None:
        from nexus.bricks.search.daemon import SearchResult  # noqa: F401

        await cache._store.upsert("root", "chat", "Chat transcripts", weight=0.5)
        daemon = _make_daemon(cache)
        daemon.config.tier_boost_overfetch_factor = 1
        results = await daemon._search_on_current_loop(
            "q", search_type="keyword", limit=2, zone_id="root"
        )
        assert daemon._fts_backend.requested_limits == [2]
        assert all(r.tier_boost is None for r in results)
        assert daemon.stats.tier_boost_suppressed_searches == 1

    @pytest.mark.asyncio
    async def test_factor_one_counts_even_when_weighted_hit_beyond_cutoff(self, cache) -> None:
        # Codex review R4: the uplifted prefix's first candidate sits at
        # rank N+1 (docs/x.md, rank 3, limit 2). With factor=1 nothing
        # weighted is ever RETURNED, so attach alone could never observe
        # the bypass — the decision-point counter must fire regardless.
        await cache._store.upsert("root", "docs", "Curated docs", weight=2.0)
        daemon = _make_daemon(cache)
        daemon.config.tier_boost_overfetch_factor = 1
        results = await daemon._search_on_current_loop(
            "q", search_type="keyword", limit=2, zone_id="root"
        )
        assert [r.path for r in results] == ["chat/a.md", "chat/b.md"]
        assert all(r.tier_boost is None for r in results)
        assert daemon.stats.tier_boost_suppressed_searches == 1


class TestProbeFailureSuppressesWeights:
    """Codex review R2: a failed probe means the pool was NOT widened, so
    attach must not apply ranking weights for that request — otherwise a
    recovered cache would weight against a pool already cut to limit."""

    @pytest.mark.asyncio
    async def test_attach_apply_weights_false_keeps_scores_but_attaches_context(
        self, cache
    ) -> None:
        from nexus.bricks.search.daemon import SearchResult

        await cache._store.upsert("root", "chat", "Chat transcripts", weight=0.5)
        daemon = _make_daemon(cache)
        r = SearchResult(path="chat/a.md", chunk_text="", score=1.0)
        await daemon._attach_path_contexts([r], zone_id="root", apply_weights=False)
        assert r.score == 1.0 and r.tier_boost is None  # ranking untouched
        assert r.context == "Chat transcripts"  # context still attached

    @pytest.mark.asyncio
    async def test_probe_failure_search_never_weights(self, cache) -> None:
        # Weighted zone + probe that raises: results must come back with
        # legacy fetch size, unweighted scores, and no tier_boost stamps —
        # even though attach's own refresh CAN see the weight row.
        from types import MethodType

        await cache._store.upsert("root", "chat", "Chat transcripts", weight=0.5)
        daemon = _make_daemon(cache)
        real_resolve = daemon._resolve_path_context_cache
        calls = {"n": 0}

        async def _flaky_resolve(self):
            calls["n"] += 1
            if calls["n"] == 1:  # probe's call fails, attach's succeeds
                raise RuntimeError("transient")
            return await real_resolve()

        daemon._resolve_path_context_cache = MethodType(_flaky_resolve, daemon)
        results = await daemon._search_on_current_loop(
            "q", search_type="keyword", limit=2, zone_id="root"
        )
        assert daemon._fts_backend.requested_limits == [2]  # not widened
        assert all(r.tier_boost is None for r in results)  # not weighted
        assert results[0].score == 10.0  # pristine score
        # Codex review R3: the bypass must be observable.
        assert daemon.stats.tier_boost_probe_failures == 1
        assert daemon.stats.tier_boost_suppressed_searches == 1


class TestGraphPathOverfetch:
    """Codex review R2: graph search must widen, weight, and trim like the
    daemon paths — a demoted top-N hit must be displaceable by rank N+1."""

    @pytest.mark.asyncio
    async def test_graph_demotion_promotes_next_candidate(self, cache) -> None:
        from nexus.bricks.search.daemon import SearchResult
        from nexus.bricks.search.graph_search_service import graph_enhanced_search

        await cache._store.upsert("root", "chat", "Chat transcripts", weight=0.5)
        daemon = _make_daemon(cache)

        corpus = [
            SearchResult(path="chat/a.md", chunk_text="", score=10.0),
            SearchResult(path="chat/b.md", chunk_text="", score=9.0),
            SearchResult(path="docs/x.md", chunk_text="", score=8.0),
        ]
        seen_limits: list[int] = []

        class _Backend:
            async def graph_search(self, query, *, zone_id, limit, path_filter=None):
                seen_limits.append(limit)
                return [
                    SearchResult(path=r.path, chunk_text=r.chunk_text, score=r.score)
                    for r in corpus[:limit]
                ]

        daemon._backend = _Backend()
        results = await graph_enhanced_search(
            "q",
            "hybrid",
            2,
            None,
            0.5,
            "low",
            record_store=None,
            async_session_factory=None,
            search_daemon=daemon,
            zone_id="root",
        )
        assert seen_limits == [2 * 3]  # widened graph fetch
        assert len(results) == 2  # trimmed back
        assert results[0].path == "docs/x.md"  # 8.0 beats 10.0*0.5
        assert results[0].tier_boost is None
        assert results[1].tier_boost == 0.5
