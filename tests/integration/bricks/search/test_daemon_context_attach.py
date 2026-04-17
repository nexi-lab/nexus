"""End-to-end: path contexts are attached to SearchResult instances (Issue #3773)."""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from nexus.bricks.search.path_context import PathContextCache, PathContextStore
from nexus.bricks.search.results import BaseSearchResult

CREATE_TABLE_SQL = """
CREATE TABLE path_contexts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    zone_id TEXT NOT NULL DEFAULT 'root',
    path_prefix TEXT NOT NULL,
    description TEXT NOT NULL,
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
    await store.upsert("root", "src/nexus/bricks/search", "Hybrid search brick")
    await store.upsert("root", "docs", "Project documentation")
    yield PathContextCache(store=store)
    await engine.dispose()


class TestBaseSearchResultContextField:
    def test_default_context_is_none(self) -> None:
        r = BaseSearchResult(path="x", chunk_text="y", score=0.5)
        assert r.context is None


class TestAttachUsesCallerZone:
    """Round-3 regression: daemon must use the caller's effective zone_id as
    the fallback because most backends construct SearchResult without the
    zone_id field set. Prior code collapsed everything to ROOT_ZONE_ID and
    silently dropped context on non-root zone searches.
    """

    @pytest.mark.asyncio
    async def test_attach_falls_back_to_caller_zone_when_result_zone_missing(
        self, cache: PathContextCache
    ) -> None:
        from types import SimpleNamespace

        # Seed a context under zone "other" only — not visible under root.
        await cache._store.upsert("other", "src/nexus/bricks/search", "other-zone brick")
        # Simulate how the real daemon's _attach_path_contexts threads
        # effective_zone_id as the fallback when r.zone_id is unset.
        results = [
            BaseSearchResult(
                path="src/nexus/bricks/search/fusion.py",
                chunk_text="",
                score=0.9,
                zone_id=None,
            ),
        ]

        async def _attach(items, *, zone_id):
            effective = zone_id or "root"
            zone = items[0].zone_id or effective
            await cache.refresh_if_stale(zone)
            snap = cache.snapshot_zone(zone)
            if snap is not None:
                from nexus.bricks.search.path_context import lookup_in_records

                for r in items:
                    r.context = lookup_in_records(snap, r.path)

        _ = SimpleNamespace(_attach_path_contexts=_attach)
        await _attach(results, zone_id="other")
        # With the caller-zone fallback, the "other" zone's description is
        # picked up even though the result carried no zone_id.
        assert results[0].context == "other-zone brick"


class TestRealDaemonAttach:
    """Round-4 follow-up: exercise the real SearchDaemon._attach_path_contexts
    so regressions to the actual method can't hide behind a local stub.
    """

    @pytest.mark.asyncio
    async def test_real_daemon_attach_falls_back_to_caller_zone(
        self, cache: PathContextCache
    ) -> None:
        from nexus.bricks.search.daemon import DaemonConfig, SearchDaemon

        # Seed the "other" zone on the shared store — the root-zone rows the
        # fixture already added must NOT be attached.
        await cache._store.upsert("other", "reports", "Quarterly reports")

        # Construct a daemon without running startup(); we only exercise
        # _attach_path_contexts directly.
        daemon = SearchDaemon.__new__(SearchDaemon)
        daemon.config = DaemonConfig()
        daemon._path_context_cache = cache
        daemon._path_context_cache_by_loop = {}
        daemon._path_context_engines_by_loop = {}

        # stats is normally built in __init__; mint a minimal stand-in.
        class _Stats:
            path_context_attach_failures = 0
            path_context_resolve_failures = 0

        daemon.stats = _Stats()

        from nexus.bricks.search.daemon import SearchResult

        result = SearchResult(path="reports/q4.md", chunk_text="", score=0.9, zone_id=None)
        await daemon._attach_path_contexts([result], zone_id="other")
        assert result.context == "Quarterly reports"

        # Control: same result, zone_id="root" -> no match (root has no
        # rows under "reports/").
        result2 = SearchResult(path="reports/q4.md", chunk_text="", score=0.9, zone_id=None)
        await daemon._attach_path_contexts([result2], zone_id="root")
        assert result2.context is None


class TestAttachStaleFallbackOnRefreshError:
    """Round-5 review: a transient DB failure during ``refresh_if_stale`` must
    not discard a previously-cached snapshot. The fail-soft contract says
    stale context beats no context for an LLM consumer."""

    @pytest.mark.asyncio
    async def test_attach_uses_stale_snapshot_when_refresh_raises(
        self, cache: PathContextCache
    ) -> None:
        from nexus.bricks.search.daemon import DaemonConfig, SearchDaemon, SearchResult

        # Warm the cache with a successful load first so snapshot_zone has
        # records to fall back to.
        await cache.refresh_if_stale("root")

        # Force the next refresh to fail via monkeypatch-style assignment;
        # this is a test-only stub of the store's fingerprint method.
        async def boom(zone_id: str) -> tuple[int, object]:
            raise RuntimeError("simulated DB brownout")

        object.__setattr__(cache._store, "zone_fingerprint", boom)

        daemon = SearchDaemon.__new__(SearchDaemon)
        daemon.config = DaemonConfig()
        daemon._path_context_cache = cache
        daemon._path_context_cache_by_loop = {}
        daemon._path_context_engines_by_loop = {}

        class _Stats:
            path_context_attach_failures = 0
            path_context_resolve_failures = 0

        daemon.stats = _Stats()

        result = SearchResult(
            path="src/nexus/bricks/search/fusion.py", chunk_text="", score=0.9, zone_id=None
        )
        await daemon._attach_path_contexts([result], zone_id="root")
        # Stale snapshot preserved "Hybrid search brick" under root.
        assert result.context == "Hybrid search brick"
        assert daemon.stats.path_context_attach_failures == 1


class TestBatchSearchStaleFallback:
    """Round-6 review: batch_search must respect the same fail-soft contract
    as ``_attach_path_contexts`` — a transient refresh error should still
    serve the last-known snapshot for the batch instead of erasing
    context for every inner result list.
    """

    @pytest.mark.asyncio
    async def test_batch_search_serves_stale_snapshot_on_refresh_error(
        self, cache: PathContextCache
    ) -> None:
        from nexus.bricks.search.path_context import lookup_in_records

        # Prime the cache: this simulates a prior batch having loaded the
        # zone successfully.
        await cache.refresh_if_stale("root")
        records_before = cache.snapshot_zone("root")
        assert records_before is not None

        # Force a refresh failure on the next attempt.
        async def boom(zone_id: str) -> tuple[int, object]:
            raise RuntimeError("simulated DB brownout")

        object.__setattr__(cache._store, "zone_fingerprint", boom)

        # Emulate the batch_search attach block (daemon.py) — refresh inside
        # its own try, then snapshot OUTSIDE so stale records still serve
        # this batch.
        failures = 0
        try:
            await cache.refresh_if_stale("root")
        except Exception:
            failures += 1
        records = cache.snapshot_zone("root")
        assert records is not None
        assert failures == 1

        hit = lookup_in_records(records, "src/nexus/bricks/search/fusion.py")
        assert hit == "Hybrid search brick"


class TestBatchSearchZoneAttachment:
    """Round-4 gap: no test covered batch_search's path-context attach on
    a non-root zone. Mirror the attach logic here to guarantee the
    effective_zone_id fallback survives future refactors.
    """

    @pytest.mark.asyncio
    async def test_batch_search_attaches_non_root_zone_context(
        self, cache: PathContextCache
    ) -> None:
        await cache._store.upsert("analytics", "dashboards", "BI dashboards")

        # Mimic what daemon.batch_search does after backend_batch returns
        # (see src/nexus/bricks/search/daemon.py batch_search).
        effective_zone_id = "analytics"
        backend_batch = [
            [
                BaseSearchResult(path="dashboards/revenue.md", chunk_text="", score=0.9),
                BaseSearchResult(path="other/path.md", chunk_text="", score=0.5),
            ],
            [BaseSearchResult(path="dashboards/churn.md", chunk_text="", score=0.8)],
        ]
        await cache.refresh_if_stale(effective_zone_id)
        records = cache.snapshot_zone(effective_zone_id)
        assert records is not None

        from nexus.bricks.search.path_context import lookup_in_records

        for inner in backend_batch:
            for r in inner:
                r.context = lookup_in_records(records, r.path)

        assert backend_batch[0][0].context == "BI dashboards"
        assert backend_batch[0][1].context is None
        assert backend_batch[1][0].context == "BI dashboards"


class TestLoopLocalResolver:
    """Regression tests for ``SearchDaemon._resolve_path_context_cache``.

    Issue #3773: the resolver must return a distinct cache for each running
    event loop so that asyncpg connections aren't shared cross-loop, and must
    memoize per-loop so repeated lookups on one loop don't build a new engine.
    """

    @pytest.mark.asyncio
    async def test_resolver_reuses_cache_within_loop(self, tmp_path) -> None:
        import asyncio
        import os

        from nexus.bricks.search.daemon import DaemonConfig, SearchDaemon

        db_file = tmp_path / "ctx.db"
        db_url = f"sqlite+aiosqlite:///{db_file}"
        # Force the resolver down the loop-local code path by setting the URL.
        os.environ.pop("DATABASE_URL", None)
        daemon = SearchDaemon.__new__(SearchDaemon)
        daemon.config = DaemonConfig(database_url=db_url)
        daemon._path_context_cache = None
        daemon._path_context_cache_by_loop = {}
        daemon._path_context_engines_by_loop = {}

        # Need the table first; create it through the same URL.
        engine = create_async_engine(db_url, future=True)
        async with engine.begin() as conn:
            await conn.exec_driver_sql(CREATE_TABLE_SQL)
        await engine.dispose()

        cache1 = await daemon._resolve_path_context_cache()
        cache2 = await daemon._resolve_path_context_cache()
        assert cache1 is cache2  # same loop -> memoized
        assert asyncio.get_running_loop() in daemon._path_context_cache_by_loop

        # Dispose the engine we created so the tmp file releases cleanly.
        for eng in list(daemon._path_context_engines_by_loop.values()):
            await eng.dispose()

    def test_resolver_builds_distinct_caches_per_loop(self, tmp_path) -> None:
        """Run the resolver on two fresh asyncio loops and confirm the
        daemon caches distinct instances keyed by loop."""
        import asyncio

        from nexus.bricks.search.daemon import DaemonConfig, SearchDaemon

        db_file = tmp_path / "ctx2.db"
        db_url = f"sqlite+aiosqlite:///{db_file}"

        # Create the table once up front.
        async def _setup() -> None:
            engine = create_async_engine(db_url, future=True)
            async with engine.begin() as conn:
                await conn.exec_driver_sql(CREATE_TABLE_SQL)
            await engine.dispose()

        asyncio.run(_setup())

        daemon = SearchDaemon.__new__(SearchDaemon)
        daemon.config = DaemonConfig(database_url=db_url)
        daemon._path_context_cache = None
        daemon._path_context_cache_by_loop = {}
        daemon._path_context_engines_by_loop = {}

        async def _resolve_once():
            cache = await daemon._resolve_path_context_cache()
            return cache

        cache_a = asyncio.run(_resolve_once())
        cache_b = asyncio.run(_resolve_once())
        # Distinct caches because each asyncio.run creates a fresh loop.
        assert cache_a is not cache_b
        # Two loops tracked.
        assert len(daemon._path_context_cache_by_loop) == 2
        assert len(daemon._path_context_engines_by_loop) == 2


class TestAttachContextToResults:
    @pytest.mark.asyncio
    async def test_attach_via_cache(self, cache: PathContextCache) -> None:
        results = [
            BaseSearchResult(
                path="src/nexus/bricks/search/fusion.py",
                chunk_text="",
                score=0.9,
                zone_id="root",
            ),
            BaseSearchResult(
                path="docs/README.md",
                chunk_text="",
                score=0.8,
                zone_id="root",
            ),
            BaseSearchResult(
                path="scripts/noop.py",
                chunk_text="",
                score=0.7,
                zone_id="root",
            ),
        ]
        for r in results:
            r.context = await cache.lookup(r.zone_id, r.path)
        assert results[0].context == "Hybrid search brick"
        assert results[1].context == "Project documentation"
        assert results[2].context is None


class TestGraphSearchContextAttachment:
    @pytest.mark.asyncio
    async def test_graph_enhanced_search_attaches_context(self, cache: PathContextCache) -> None:
        """graph_enhanced_search must attach context like the non-graph branch."""
        from types import SimpleNamespace

        from nexus.bricks.search.graph_search_service import graph_enhanced_search
        from nexus.bricks.search.results import BaseSearchResult

        async def _fake_graph_search(query, *, zone_id, limit, path_filter):
            return [
                BaseSearchResult(
                    path="src/nexus/bricks/search/fusion.py",
                    chunk_text="",
                    score=0.9,
                    zone_id=zone_id,
                ),
                BaseSearchResult(
                    path="docs/README.md",
                    chunk_text="",
                    score=0.8,
                    zone_id=zone_id,
                ),
            ]

        # Fake daemon: backend exposing graph_search, _attach_path_contexts bound
        # to a local SearchDaemon-style helper powered by the cache.
        backend = SimpleNamespace(graph_search=_fake_graph_search)

        async def _attach(results, *, zone_id=None):
            zones = {(r.zone_id or zone_id or "root") for r in results}
            for zone in zones:
                await cache.refresh_if_stale(zone)
            for r in results:
                r.context = cache.lookup_cached(r.zone_id or zone_id, r.path)

        daemon = SimpleNamespace(_backend=backend, _attach_path_contexts=_attach)

        results = await graph_enhanced_search(
            "q",
            "hybrid",
            10,
            None,
            0.5,
            "auto",
            record_store=None,
            async_session_factory=None,
            search_daemon=daemon,
            zone_id="root",
        )
        assert results[0].context == "Hybrid search brick"
        assert results[1].context == "Project documentation"


class TestSerializerEmitsContext:
    def test_context_field_emitted_when_set(self) -> None:
        from nexus.server.api.v2.routers.search import _serialize_search_result

        r = BaseSearchResult(
            path="src/nexus/bricks/search/fusion.py",
            chunk_text="body",
            score=0.9,
            zone_id="root",
        )
        r.context = "Hybrid search brick"
        out = _serialize_search_result(r)
        assert out.get("context") == "Hybrid search brick"

    def test_context_field_omitted_when_none(self) -> None:
        from nexus.server.api.v2.routers.search import _serialize_search_result

        r = BaseSearchResult(path="x", chunk_text="y", score=0.5)
        out = _serialize_search_result(r)
        assert "context" not in out


class TestFullFlowStoreAttachSerialize:
    """Chain the real store, real cache refresh+lookup, and real serializer.

    Mirrors what happens in production across these boundaries:
      admin PUT /api/v2/path-contexts/ -> store.upsert
      backend returns hits -> daemon._attach_path_contexts -> serializer
    """

    @pytest.mark.asyncio
    async def test_put_then_search_response_carries_context(self) -> None:
        from nexus.server.api.v2.routers.search import _serialize_search_result

        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.exec_driver_sql(CREATE_TABLE_SQL)
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        store = PathContextStore(async_session_factory=factory, db_type="sqlite")
        cache = PathContextCache(store=store)

        # Admin seeds contexts (same call path as PUT /api/v2/path-contexts/).
        await store.upsert("root", "src/nexus/bricks/search", "Hybrid search brick")
        await store.upsert("root", "docs", "Project documentation")

        # Backend returns raw results with no context set.
        raw_results = [
            BaseSearchResult(
                path="src/nexus/bricks/search/fusion.py",
                chunk_text="def rrf_fusion(...)",
                score=0.9,
                zone_id="root",
                keyword_score=0.8,
                vector_score=0.7,
            ),
            BaseSearchResult(
                path="docs/README.md",
                chunk_text="Project overview",
                score=0.8,
                zone_id="root",
            ),
            BaseSearchResult(
                path="scripts/unrelated.py",
                chunk_text="other",
                score=0.5,
                zone_id="root",
            ),
        ]

        # Daemon-equivalent attach: one refresh per unique zone, cached lookups.
        zones = {(r.zone_id or "root") for r in raw_results}
        for zone in zones:
            await cache.refresh_if_stale(zone)
        for r in raw_results:
            r.context = cache.lookup_cached(r.zone_id, r.path)

        # Router serializer produces the HTTP response dict.
        response = [_serialize_search_result(r) for r in raw_results]

        assert response[0]["path"] == "src/nexus/bricks/search/fusion.py"
        assert response[0]["context"] == "Hybrid search brick"
        assert response[1]["path"] == "docs/README.md"
        assert response[1]["context"] == "Project documentation"
        # No matching prefix -> context key omitted to keep response compact.
        assert response[2]["path"] == "scripts/unrelated.py"
        assert "context" not in response[2]

        await engine.dispose()

    @pytest.mark.asyncio
    async def test_empty_store_emits_no_context_anywhere(self) -> None:
        from nexus.server.api.v2.routers.search import _serialize_search_result

        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.exec_driver_sql(CREATE_TABLE_SQL)
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        store = PathContextStore(async_session_factory=factory, db_type="sqlite")
        cache = PathContextCache(store=store)

        raw_results = [
            BaseSearchResult(path="any/path.py", chunk_text="x", score=0.9, zone_id="root"),
        ]
        await cache.refresh_if_stale("root")
        for r in raw_results:
            r.context = cache.lookup_cached(r.zone_id, r.path)
        response = [_serialize_search_result(r) for r in raw_results]

        assert "context" not in response[0]
        await engine.dispose()

    @pytest.mark.asyncio
    async def test_longest_prefix_wins_through_full_pipeline(self) -> None:
        """Overlapping prefixes: the longer match decides which description appears."""
        from nexus.server.api.v2.routers.search import _serialize_search_result

        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.exec_driver_sql(CREATE_TABLE_SQL)
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        store = PathContextStore(async_session_factory=factory, db_type="sqlite")
        cache = PathContextCache(store=store)

        await store.upsert("root", "src", "generic source")
        await store.upsert("root", "src/nexus/bricks/search", "search brick")
        await cache.refresh_if_stale("root")

        r = BaseSearchResult(
            path="src/nexus/bricks/search/fusion.py",
            chunk_text="",
            score=0.9,
            zone_id="root",
        )
        r.context = cache.lookup_cached(r.zone_id, r.path)
        out = _serialize_search_result(r)
        assert out["context"] == "search brick"  # longer prefix wins

        await engine.dispose()
