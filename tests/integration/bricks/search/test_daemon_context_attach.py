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

        async def _attach(results):
            zones = {(r.zone_id or "root") for r in results}
            for zone in zones:
                await cache.refresh_if_stale(zone)
            for r in results:
                r.context = cache.lookup_cached(r.zone_id, r.path)

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
