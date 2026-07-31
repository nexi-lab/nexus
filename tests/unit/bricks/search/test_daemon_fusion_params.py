"""Fusion request params must reach the backend hybrid path (Issue #4541).

`GET /api/v2/search/query` validates `alpha` / `fusion` (and now `rrf_k`),
but the primary indexed path `_search_via_backends` historically fused with
a hardcoded two-stage RRF (k=60), silently ignoring the knobs. These tests
pin the full chain: `search()` threads the params, `_search_via_backends`
dispatches on them, and the default request stays byte-identical to the
legacy hardcoded fusion.
"""

from __future__ import annotations

from types import MethodType
from typing import Any

import pytest


def _make_daemon() -> Any:
    """Bare SearchDaemon with fake (non-PG) fts + vector backends.

    Fixture corpus is built so keyword and dense orderings disagree:
      keyword (BM25):  /a.md (10.0) > /b.md (9.0) > /c.md (8.0)
      dense  (cosine): /d.md (0.99) > /c.md (0.9) > /a.md (0.5)
    so any change of fusion method / alpha / k is visible in the ordering.
    """
    from nexus.bricks.search.daemon import DaemonConfig, SearchDaemon, SearchResult

    def _kw(path: str, score: float) -> SearchResult:
        return SearchResult(
            path=path, chunk_text=path, score=score, chunk_index=0, search_type="keyword"
        )

    def _dense(path: str, score: float) -> SearchResult:
        return SearchResult(
            path=path, chunk_text=path, score=score, chunk_index=0, search_type="semantic"
        )

    class FakeFtsBackend:
        async def keyword_search(
            self,
            query: str,
            path: str,
            limit: int,
            zone_id: str,
            *,
            timing: dict[str, float] | None = None,
        ) -> list[Any]:
            return [_kw("/a.md", 10.0), _kw("/b.md", 9.0), _kw("/c.md", 8.0)]

    class FakeVectorBackend:
        async def semantic_search(
            self, qvec: list[float], path: str, limit: int, zone_id: str
        ) -> list[Any]:
            return [_dense("/d.md", 0.99), _dense("/c.md", 0.9), _dense("/a.md", 0.5)]

    async def _embed_query(self: Any, query: str) -> list[float]:
        return [0.1, 0.2]

    daemon: Any = SearchDaemon.__new__(SearchDaemon)
    daemon.last_search_timing = {}
    daemon._fts_backend = FakeFtsBackend()
    daemon._vector_backend = FakeVectorBackend()
    daemon._embed_query = MethodType(_embed_query, daemon)
    # Disable #4542 final-list page pooling: these tests assert chunk-grain
    # fusion ordering, which pooling would re-group.
    daemon.config = DaemonConfig(page_aggregation=False)
    return daemon


async def _hybrid(daemon: Any, **kwargs: Any) -> list[Any]:
    return await daemon._search_via_backends(
        "nexus core",
        search_type="hybrid",
        limit=4,
        path_filter=None,
        zone_id="root",
        **kwargs,
    )


@pytest.mark.asyncio
async def test_default_fusion_matches_legacy_two_stage_rrf() -> None:
    """Regression pin: default params stay byte-identical to the historical
    hardcoded fusion — plain RRF, k=60, sub-fusion then dense stage."""
    from nexus.bricks.search.fusion import rrf_fusion

    daemon = _make_daemon()
    results = await _hybrid(daemon)

    chunk_kw = await daemon._fts_backend.keyword_search("nexus core", "/", 8, "root")
    dense = await daemon._vector_backend.semantic_search([0.1, 0.2], "/", 8, "root")
    kw_fused = rrf_fusion(chunk_kw, [], k=60, limit=8, id_key=None)
    expected = rrf_fusion(kw_fused, dense, k=60, limit=4, id_key=None)

    assert [r.path for r in results] == [e["path"] for e in expected]
    assert [r.path for r in results] == ["/a.md", "/d.md", "/c.md", "/b.md"]
    for got, exp in zip(results, expected, strict=True):
        assert got.score == pytest.approx(exp["score"])


@pytest.mark.asyncio
async def test_weighted_fusion_alpha_changes_ordering() -> None:
    """fusion=weighted&alpha=0.9 must produce a different ordering than the
    default RRF (dense-favoured under high alpha)."""
    daemon = _make_daemon()
    results = await _hybrid(daemon, alpha=0.9, fusion_method="weighted")

    assert [r.path for r in results] == ["/d.md", "/c.md", "/a.md", "/b.md"]


@pytest.mark.asyncio
async def test_rrf_weighted_alpha_zero_ranks_keyword_only() -> None:
    """fusion=rrf_weighted&alpha=0.0 ranks by the keyword legs only; the
    dense-only hit contributes zero score."""
    daemon = _make_daemon()
    results = await _hybrid(daemon, alpha=0.0, fusion_method="rrf_weighted")

    assert [r.path for r in results[:3]] == ["/a.md", "/b.md", "/c.md"]
    dense_only = [r for r in results if r.path == "/d.md"]
    assert all(r.score == 0.0 for r in dense_only)


@pytest.mark.asyncio
async def test_rrf_k_reaches_fusion() -> None:
    """A non-default rrf_k must change the fused ordering (with this fixture,
    k=5 amplifies rank differences enough to swap /c.md above /d.md)."""
    daemon = _make_daemon()
    default = await _hybrid(daemon)
    small_k = await _hybrid(daemon, rrf_k=5)

    assert [r.path for r in default] == ["/a.md", "/d.md", "/c.md", "/b.md"]
    assert [r.path for r in small_k] == ["/a.md", "/c.md", "/d.md", "/b.md"]


@pytest.mark.asyncio
async def test_search_threads_fusion_params_to_backends() -> None:
    """`SearchDaemon.search()` must forward alpha / fusion_method / rrf_k to
    `_search_via_backends` (previously dropped on the floor)."""
    from nexus.bricks.search.daemon import SearchDaemon, SearchResult

    seen: dict[str, Any] = {}

    daemon: Any = SearchDaemon.__new__(SearchDaemon)
    daemon._initialized = True
    daemon._fts_backend = object()
    daemon._vector_backend = object()
    daemon._permission_enforcer = None
    daemon.last_search_timing = {}

    def _track_latency(self: Any, latency_ms: float) -> None:
        self._last_latency_ms = latency_ms

    async def _attach_path_contexts(
        self: Any, results: Any, *, zone_id: str, pinned_snapshots: Any = None
    ) -> None:
        self._last_context_zone = zone_id

    async def _search_via_backends(self: Any, *args: Any, **kwargs: Any) -> list[SearchResult]:
        seen.update(kwargs)
        return [
            SearchResult(
                path="/backend.md",
                chunk_text="backend result",
                score=1.0,
                chunk_index=0,
                search_type="hybrid",
            )
        ]

    daemon._track_latency = MethodType(_track_latency, daemon)
    daemon._attach_path_contexts = MethodType(_attach_path_contexts, daemon)
    daemon._search_via_backends = MethodType(_search_via_backends, daemon)

    await daemon.search(
        "nexus core",
        search_type="hybrid",
        limit=1,
        alpha=0.9,
        fusion_method="weighted",
        rrf_k=30,
    )

    assert seen["alpha"] == 0.9
    assert seen["fusion_method"] == "weighted"
    assert seen["rrf_k"] == 30
