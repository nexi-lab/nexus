"""SearchDaemon routing regressions for post-#3699 backends."""

from __future__ import annotations

from types import MethodType
from typing import Any

import pytest


def _daemon_with_backend_result(search_type_seen: list[str]):
    from nexus.bricks.search.daemon import SearchDaemon, SearchResult

    daemon = SearchDaemon.__new__(SearchDaemon)
    daemon._initialized = True
    daemon._fts_backend = object()
    daemon._vector_backend = object()
    daemon._permission_enforcer = None
    daemon.last_search_timing = {}

    def _track_latency(self, latency_ms: float) -> None:
        self._last_latency_ms = latency_ms

    async def _attach_path_contexts(self, results: list[SearchResult], *, zone_id: str) -> None:
        self._last_context_zone = zone_id

    async def _search_via_backends(self, *args: Any, **kwargs: Any) -> list[SearchResult]:
        search_type_seen.append(kwargs["search_type"])
        return [
            SearchResult(
                path="/backend.md",
                chunk_text="backend result",
                score=10.0,
                chunk_index=0,
                search_type=kwargs["search_type"],
            )
        ]

    async def _keyword_search(self, *args: Any, **kwargs: Any) -> list[SearchResult]:
        raise AssertionError("legacy keyword path should not run before new backends")

    daemon._track_latency = MethodType(_track_latency, daemon)
    daemon._attach_path_contexts = MethodType(_attach_path_contexts, daemon)
    daemon._search_via_backends = MethodType(_search_via_backends, daemon)
    daemon._keyword_search = MethodType(_keyword_search, daemon)
    return daemon


@pytest.mark.asyncio
async def test_keyword_search_prefers_new_fts_backend_before_legacy_keyword_stack():
    seen: list[str] = []
    daemon = _daemon_with_backend_result(seen)

    results = await daemon.search("Nexus Core", search_type="keyword", limit=1, zone_id="root")

    assert seen == ["keyword"]
    assert [result.path for result in results] == ["/backend.md"]


@pytest.mark.asyncio
async def test_hybrid_search_does_not_prefetch_legacy_keyword_when_backends_exist():
    seen: list[str] = []
    daemon = _daemon_with_backend_result(seen)

    results = await daemon.search("Nexus Core", search_type="hybrid", limit=1, zone_id="root")

    assert seen == ["hybrid"]
    assert [result.path for result in results] == ["/backend.md"]
