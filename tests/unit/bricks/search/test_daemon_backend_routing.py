"""SearchDaemon routing regressions for post-#3699 backends."""

from __future__ import annotations

import asyncio
from types import MethodType, SimpleNamespace
from typing import Any, cast

import pytest

_BACKEND_TIMING_KEYS = {
    "backend_ms",
    "embed_ms",
    "keyword_ms",
    "page_keyword_ms",
    "vector_ms",
    "fusion_ms",
    "rerank_ms",
}


def _daemon_with_backend_result(search_type_seen: list[str]) -> Any:
    from nexus.bricks.search.daemon import SearchDaemon, SearchResult

    daemon: Any = SearchDaemon.__new__(SearchDaemon)
    daemon._initialized = True
    daemon._fts_backend = object()
    daemon._vector_backend = object()
    daemon._permission_enforcer = None
    daemon.last_search_timing = {}

    def _track_latency(self: Any, latency_ms: float) -> None:
        self._last_latency_ms = latency_ms

    async def _attach_path_contexts(
        self: Any, results: list[SearchResult], *, zone_id: str
    ) -> None:
        self._last_context_zone = zone_id

    async def _search_via_backends(self: Any, *args: Any, **kwargs: Any) -> list[SearchResult]:
        search_type_seen.append(kwargs["search_type"])
        self.last_search_timing = {
            "backend_ms": 8.0,
            "keyword_ms": 3.0,
            "rerank_ms": 0.0,
        }
        return [
            SearchResult(
                path="/backend.md",
                chunk_text="backend result",
                score=10.0,
                chunk_index=0,
                search_type=kwargs["search_type"],
            )
        ]

    async def _keyword_search(self: Any, *args: Any, **kwargs: Any) -> list[SearchResult]:
        raise AssertionError("legacy keyword path should not run before new backends")

    daemon._track_latency = MethodType(_track_latency, daemon)
    daemon._attach_path_contexts = MethodType(_attach_path_contexts, daemon)
    daemon._search_via_backends = MethodType(_search_via_backends, daemon)
    daemon._keyword_search = MethodType(_keyword_search, daemon)
    return daemon


def test_engine_dialect_name_prefers_async_engine_dialect() -> None:
    from nexus.bricks.search.daemon import SearchDaemon

    engine = SimpleNamespace(
        dialect=SimpleNamespace(name="sqlite"),
        sync_engine=SimpleNamespace(dialect=SimpleNamespace(name="postgresql")),
    )

    assert SearchDaemon._engine_dialect_name(engine) == "sqlite"


def test_engine_dialect_name_falls_back_to_sync_engine_dialect() -> None:
    from nexus.bricks.search.daemon import SearchDaemon

    engine = SimpleNamespace(
        sync_engine=SimpleNamespace(dialect=SimpleNamespace(name="PostgreSQL"))
    )

    assert SearchDaemon._engine_dialect_name(engine) == "postgresql"


def test_engine_dialect_name_handles_missing_engine() -> None:
    from nexus.bricks.search.daemon import SearchDaemon

    assert SearchDaemon._engine_dialect_name(None) == ""


@pytest.mark.asyncio
async def test_keyword_search_prefers_new_fts_backend_before_legacy_keyword_stack() -> None:
    seen: list[str] = []
    daemon = _daemon_with_backend_result(seen)

    results = await daemon.search("Nexus Core", search_type="keyword", limit=1, zone_id="root")

    assert seen == ["keyword"]
    assert [result.path for result in results] == ["/backend.md"]
    assert daemon.last_search_timing["backend_ms"] >= 0.0
    assert daemon.last_search_timing["keyword_ms"] == 3.0
    assert daemon.last_search_timing["rerank_ms"] == 0.0


@pytest.mark.asyncio
async def test_hybrid_search_does_not_prefetch_legacy_keyword_when_backends_exist() -> None:
    seen: list[str] = []
    daemon = _daemon_with_backend_result(seen)

    results = await daemon.search("Nexus Core", search_type="hybrid", limit=1, zone_id="root")

    assert seen == ["hybrid"]
    assert [result.path for result in results] == ["/backend.md"]
    assert daemon.last_search_timing["keyword_ms"] == 3.0


@pytest.mark.asyncio
async def test_concurrent_searches_return_request_local_timing_snapshots() -> None:
    from nexus.bricks.search.daemon import SearchDaemon, SearchResult

    daemon: Any = SearchDaemon.__new__(SearchDaemon)
    daemon._initialized = True
    daemon._fts_backend = object()
    daemon._vector_backend = object()
    daemon._permission_enforcer = None
    daemon.last_search_timing = {}

    first_recorded = asyncio.Event()
    second_recorded = asyncio.Event()

    def _track_latency(self: Any, latency_ms: float) -> None:
        self._last_latency_ms = latency_ms

    async def _attach_path_contexts(
        self: Any, results: list[SearchResult], *, zone_id: str
    ) -> None:
        self._last_context_zone = zone_id

    async def _search_via_backends(
        self: Any, query: str, *args: Any, **kwargs: Any
    ) -> list[SearchResult]:
        if query == "first":
            self.last_search_timing = {
                "backend_ms": 1.0,
                "keyword_ms": 1.0,
                "rerank_ms": 0.0,
            }
            first_recorded.set()
            await second_recorded.wait()
        else:
            await first_recorded.wait()
            self.last_search_timing = {
                "backend_ms": 2.0,
                "keyword_ms": 2.0,
                "rerank_ms": 0.0,
            }
            second_recorded.set()
        return [
            SearchResult(
                path=f"/{query}.md",
                chunk_text=query,
                score=10.0,
                chunk_index=0,
                search_type=kwargs["search_type"],
            )
        ]

    async def _keyword_search(self: Any, *args: Any, **kwargs: Any) -> list[SearchResult]:
        raise AssertionError("legacy keyword path should not run before new backends")

    daemon._track_latency = MethodType(_track_latency, daemon)
    daemon._attach_path_contexts = MethodType(_attach_path_contexts, daemon)
    daemon._search_via_backends = MethodType(_search_via_backends, daemon)
    daemon._keyword_search = MethodType(_keyword_search, daemon)

    first_results, second_results = await asyncio.gather(
        daemon.search("first", search_type="keyword", limit=1, zone_id="root"),
        daemon.search("second", search_type="keyword", limit=1, zone_id="root"),
    )

    assert [result.path for result in first_results] == ["/first.md"]
    assert [result.path for result in second_results] == ["/second.md"]
    assert cast(Any, first_results).search_timing["keyword_ms"] == 1.0
    assert cast(Any, second_results).search_timing["keyword_ms"] == 2.0


@pytest.mark.asyncio
async def test_keyword_backend_timing_records_keyword_and_total() -> None:
    from nexus.bricks.search.daemon import SearchDaemon, SearchResult

    class FakeFtsBackend:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, int, str]] = []

        async def keyword_search(
            self, query: str, path: str, limit: int, zone_id: str
        ) -> list[SearchResult]:
            self.calls.append((query, path, limit, zone_id))
            return [
                SearchResult(
                    path="/backend.md",
                    chunk_text="backend result",
                    score=10.0,
                    chunk_index=0,
                    search_type="keyword",
                )
            ]

    daemon: Any = SearchDaemon.__new__(SearchDaemon)
    daemon.last_search_timing = {}
    daemon._fts_backend = FakeFtsBackend()

    results = await daemon._search_via_backends(
        "Nexus Core",
        search_type="keyword",
        limit=1,
        path_filter=None,
        zone_id="root",
    )

    assert [result.path for result in results] == ["/backend.md"]
    assert daemon._fts_backend.calls == [("Nexus Core", "/", 1, "root")]
    assert daemon.last_search_timing.keys() >= _BACKEND_TIMING_KEYS
    assert daemon.last_search_timing["backend_ms"] >= 0.0
    assert daemon.last_search_timing["keyword_ms"] >= 0.0
    assert daemon.last_search_timing["embed_ms"] == 0.0
    assert daemon.last_search_timing["rerank_ms"] == 0.0


@pytest.mark.asyncio
async def test_pg_hybrid_backend_timing_records_each_leg() -> None:
    from nexus.bricks.search.daemon import SearchDaemon, SearchResult
    from nexus.bricks.search.pg_fts_backend import PgFtsBackend
    from nexus.bricks.search.results import BaseSearchResult

    class FakePgFtsBackend(PgFtsBackend):
        def __init__(self) -> None:
            self.keyword_limits: list[int] = []

        async def keyword_search(
            self, query: str, path: str, limit: int, zone_id: str
        ) -> list[BaseSearchResult]:
            self.keyword_limits.append(limit)
            return [
                SearchResult(
                    path="/chunk.md",
                    chunk_text="chunk result",
                    score=9.0,
                    chunk_index=0,
                    search_type="keyword",
                ),
                SearchResult(
                    path="/page.md",
                    chunk_text="page result",
                    score=8.0,
                    chunk_index=3,
                    search_type="keyword",
                ),
            ]

        async def keyword_search_pages(
            self, query: str, path: str, limit: int, zone_id: str
        ) -> list[BaseSearchResult]:
            raise AssertionError("PG hybrid should derive page results from indexed chunk matches")

    class FakeVectorBackend:
        async def semantic_search(
            self, qvec: list[float], path: str, limit: int, zone_id: str
        ) -> list[SearchResult]:
            return [
                SearchResult(
                    path="/dense.md",
                    chunk_text="dense result",
                    score=7.0,
                    chunk_index=0,
                    search_type="semantic",
                )
            ]

    async def _embed_query(self: Any, query: str) -> list[float]:
        return [0.1, 0.2]

    daemon: Any = SearchDaemon.__new__(SearchDaemon)
    daemon.last_search_timing = {}
    daemon._fts_backend = FakePgFtsBackend()
    daemon._vector_backend = FakeVectorBackend()
    daemon._embed_query = MethodType(_embed_query, daemon)

    results = await daemon._search_via_backends(
        "Nexus Core",
        search_type="hybrid",
        limit=3,
        path_filter="/docs",
        zone_id="root",
    )

    assert {result.path for result in results} == {"/chunk.md", "/page.md", "/dense.md"}
    assert daemon._fts_backend.keyword_limits == [64]
    assert daemon.last_search_timing.keys() >= _BACKEND_TIMING_KEYS
    assert daemon.last_search_timing["backend_ms"] >= 0.0
    assert daemon.last_search_timing["embed_ms"] >= 0.0
    assert daemon.last_search_timing["keyword_ms"] >= 0.0
    assert daemon.last_search_timing["page_keyword_ms"] >= 0.0
    assert daemon.last_search_timing["vector_ms"] >= 0.0
    assert daemon.last_search_timing["fusion_ms"] >= 0.0
    assert daemon.last_search_timing["rerank_ms"] == 0.0
