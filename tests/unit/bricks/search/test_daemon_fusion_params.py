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

from nexus.contracts.search_types import SearchRequest


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
    # Title arm (#4545) is on by default; bare __new__ skips __init__, so
    # give locate() an empty skeleton index (no hits → fusion unchanged).
    daemon._skeleton_docs = {}
    daemon._skeleton_title_index = {}
    daemon._skeleton_path_index = {}
    daemon._skeleton_exact_title_index = {}
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
    """`SearchDaemon.search(SearchRequest(query=))` must forward alpha / fusion_method / rrf_k to
    `_search_via_backends` (previously dropped on the floor)."""
    from nexus.bricks.search.daemon import DaemonConfig, SearchDaemon, SearchResult

    seen: dict[str, Any] = {}

    daemon: Any = SearchDaemon.__new__(SearchDaemon)
    daemon._initialized = True
    daemon._fts_backend = object()
    daemon._vector_backend = object()
    daemon._permission_enforcer = None
    daemon.last_search_timing = {}
    daemon.config = DaemonConfig(page_aggregation=False)

    def _track_latency(self: Any, latency_ms: float) -> None:
        self._last_latency_ms = latency_ms

    async def _attach_path_contexts(
        self: Any, results: Any, *, zone_id: str, **_attach_kwargs: Any
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
        SearchRequest(
            query="nexus core",
            search_type="hybrid",
            limit=1,
            alpha=0.9,
            fusion_method="weighted",
            rrf_k=30,
        )
    )

    assert seen["alpha"] == 0.9
    assert seen["fusion_method"] == "weighted"
    assert seen["rrf_k"] == 30


# =============================================================================
# Legacy fallback path (#4541 review): _hybrid_search must honour the same
# fusion knobs as the primary indexed path, with byte-identical defaults.
# =============================================================================


def _make_fallback_daemon() -> Any:
    """Bare daemon exercising the legacy `_hybrid_search` fallback.

    Same divergent corpus as `_make_daemon` but served through the legacy
    `_keyword_search` / `_semantic_search` stack (no indexed backends).
    """
    from nexus.bricks.search.daemon import SearchDaemon, SearchResult

    def _res(path: str, score: float, st: str) -> SearchResult:
        # Legs carry distinguishable per-modality fields so tests can pin
        # the legacy first-seen field-preservation contract.
        return SearchResult(
            path=path,
            chunk_text=path,
            score=score,
            chunk_index=0,
            search_type=st,
            keyword_score=score if st == "keyword" else None,
            vector_score=score if st == "semantic" else None,
        )

    async def _keyword_search(
        self: Any, query: str, limit: int, path_filter: Any, *, zone_id: Any = None
    ) -> list[Any]:
        return [
            _res("/a.md", 10.0, "keyword"),
            _res("/b.md", 9.0, "keyword"),
            _res("/c.md", 8.0, "keyword"),
        ]

    async def _semantic_search(
        self: Any,
        query: str,
        limit: int,
        path_filter: Any,
        *,
        zone_id: Any = None,
        query_vector: list[float] | None = None,
        propagate_failures: bool = False,
        embedding_unavailable: bool = False,
    ) -> list[Any]:
        return [
            _res("/d.md", 0.99, "semantic"),
            _res("/c.md", 0.9, "semantic"),
            _res("/a.md", 0.5, "semantic"),
        ]

    daemon: Any = SearchDaemon.__new__(SearchDaemon)
    daemon.last_search_timing = {}
    daemon._keyword_search = MethodType(_keyword_search, daemon)
    daemon._semantic_search = MethodType(_semantic_search, daemon)
    return daemon


@pytest.mark.asyncio
async def test_fallback_default_matches_legacy_inline_rrf() -> None:
    """Regression pin: default fallback fusion stays byte-identical to the
    historical inline loop — plain reciprocal-rank sums, k=60, no top-rank
    bonus."""
    daemon = _make_fallback_daemon()
    results = await daemon._hybrid_search("nexus core", 4, None, 0.5, "rrf")

    kw = [("/a.md", 1), ("/b.md", 2), ("/c.md", 3)]
    sem = [("/d.md", 1), ("/c.md", 2), ("/a.md", 3)]
    expected: dict[str, float] = {}
    for path, rank in kw + sem:
        expected[path] = expected.get(path, 0.0) + 1.0 / (60 + rank)
    expected_order = sorted(expected, key=lambda p: expected[p], reverse=True)

    assert [r.path for r in results] == expected_order == ["/a.md", "/c.md", "/d.md", "/b.md"]
    for r in results:
        assert r.score == pytest.approx(expected[r.path])

    # Byte-identical includes serialized field values, not just ordering:
    # the legacy loop copied every field from the FIRST-seen leg result
    # (keyword leg wins) and never rewrote per-leg modality scores.
    by_path = {r.path: r for r in results}
    assert by_path["/a.md"].keyword_score == 10.0
    assert by_path["/a.md"].vector_score is None  # kw-first, despite dense hit
    assert by_path["/c.md"].keyword_score == 8.0
    assert by_path["/c.md"].vector_score is None
    assert by_path["/d.md"].keyword_score is None  # dense-only
    assert by_path["/d.md"].vector_score == 0.99
    assert all(r.search_type == "hybrid" for r in results)


@pytest.mark.asyncio
async def test_fallback_weighted_alpha_changes_ordering() -> None:
    daemon = _make_fallback_daemon()
    low = await daemon._hybrid_search("nexus core", 4, None, 0.05, "weighted")
    high = await daemon._hybrid_search("nexus core", 4, None, 0.95, "weighted")

    assert [r.path for r in low] != [r.path for r in high]
    assert [r.path for r in low][0] == "/a.md"
    assert [r.path for r in high][0] == "/d.md"


@pytest.mark.asyncio
async def test_fallback_rrf_weighted_alpha_zero_ranks_keyword_only() -> None:
    daemon = _make_fallback_daemon()
    results = await daemon._hybrid_search("nexus core", 4, None, 0.0, "rrf_weighted")

    assert [r.path for r in results[:3]] == ["/a.md", "/b.md", "/c.md"]


@pytest.mark.asyncio
async def test_fallback_rrf_k_reaches_fusion() -> None:
    daemon = _make_fallback_daemon()
    default = await daemon._hybrid_search("nexus core", 4, None, 0.5, "rrf")
    small_k = await daemon._hybrid_search("nexus core", 4, None, 0.5, "rrf", 1)

    assert [r.score for r in small_k] != [r.score for r in default]


@pytest.mark.asyncio
async def test_search_threads_fusion_params_to_fallback() -> None:
    """With no indexed backends, `search()` must forward the fusion knobs to
    the legacy `_hybrid_search` fallback."""
    from nexus.bricks.search.daemon import DaemonConfig, SearchDaemon, SearchResult

    seen: dict[str, Any] = {}

    daemon: Any = SearchDaemon.__new__(SearchDaemon)
    daemon._initialized = True
    daemon._fts_backend = None
    daemon._vector_backend = None
    daemon._permission_enforcer = None
    daemon.last_search_timing = {}
    # #4543: search() resolves recency knobs from config at the chokepoint.
    daemon.config = DaemonConfig(page_aggregation=False)

    def _track_latency(self: Any, latency_ms: float) -> None:
        self._last_latency_ms = latency_ms

    async def _attach_path_contexts(
        self: Any, results: Any, *, zone_id: str, **kwargs: Any
    ) -> None:
        self._last_context_zone = zone_id

    async def _keyword_search(self: Any, *args: Any, **kwargs: Any) -> list[Any]:
        return []

    async def _hybrid_search(
        self: Any,
        query: str,
        limit: int,
        path_filter: Any,
        alpha: float,
        fusion_method: str = "rrf",
        rrf_k: int = 60,
        *,
        zone_id: Any = None,
        query_vector: list[float] | None = None,
        embedding_unavailable: bool = False,
    ) -> list[SearchResult]:
        seen.update({"alpha": alpha, "fusion_method": fusion_method, "rrf_k": rrf_k})
        return []

    daemon._track_latency = MethodType(_track_latency, daemon)
    daemon._attach_path_contexts = MethodType(_attach_path_contexts, daemon)
    daemon._keyword_search = MethodType(_keyword_search, daemon)
    daemon._hybrid_search = MethodType(_hybrid_search, daemon)

    await daemon.search(
        SearchRequest(
            query="nexus core",
            search_type="hybrid",
            limit=1,
            alpha=0.9,
            fusion_method="weighted",
            rrf_k=30,
        )
    )

    assert seen == {"alpha": 0.9, "fusion_method": "weighted", "rrf_k": 30}


@pytest.mark.asyncio
async def test_search_positional_signature_unchanged() -> None:
    """#4541 review round 3: rrf_k sits at the signature TAIL so pre-existing
    positional callers keep binding zone_id in position 7."""
    from nexus.bricks.search.daemon import DaemonConfig, SearchDaemon, SearchResult

    seen: dict[str, Any] = {}

    daemon: Any = SearchDaemon.__new__(SearchDaemon)
    daemon._initialized = True
    daemon._fts_backend = object()
    daemon._vector_backend = object()
    daemon._permission_enforcer = None
    daemon.last_search_timing = {}
    # #4543: search() resolves recency knobs from config at the chokepoint.
    daemon.config = DaemonConfig(page_aggregation=False)

    def _track_latency(self: Any, latency_ms: float) -> None:
        self._last_latency_ms = latency_ms

    async def _attach_path_contexts(
        self: Any, results: Any, *, zone_id: str, **kwargs: Any
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
                search_type="keyword",
            )
        ]

    daemon._track_latency = MethodType(_track_latency, daemon)
    daemon._attach_path_contexts = MethodType(_attach_path_contexts, daemon)
    daemon._search_via_backends = MethodType(_search_via_backends, daemon)

    # SearchRequest bundles all knobs — zone_id is a plain field now.
    await daemon.search(
        SearchRequest(
            query="nexus core",
            search_type="keyword",
            limit=10,
            path_filter=None,
            alpha=0.5,
            fusion_method="rrf",
            zone_id="tenant-a",
        )
    )

    assert seen["zone_id"] == "tenant-a"


# =============================================================================
# Embedding-unavailable hybrid branch (#4541 review round 4): the keyword-only
# degradation must still honour non-default fusion knobs.
# =============================================================================


def _make_no_embed_daemon() -> Any:
    daemon = _make_daemon()

    async def _embed_query(self: Any, query: str) -> None:
        return None

    daemon._embed_query = MethodType(_embed_query, daemon)
    return daemon


@pytest.mark.asyncio
async def test_no_embedding_default_keeps_raw_keyword_shortcut() -> None:
    """Byte-identical default: embedding loss + default knobs returns the raw
    keyword results exactly as before (BM25 scores untouched)."""
    daemon = _make_no_embed_daemon()
    results = await _hybrid(daemon)

    assert [r.path for r in results] == ["/a.md", "/b.md", "/c.md"]
    assert [r.score for r in results] == [10.0, 9.0, 8.0]


@pytest.mark.asyncio
async def test_no_embedding_honours_rrf_k() -> None:
    """Non-default rrf_k re-scores the keyword leg through the fusion module
    instead of silently returning raw BM25 scores."""
    daemon = _make_no_embed_daemon()
    results = await _hybrid(daemon, rrf_k=5)

    assert [r.path for r in results] == ["/a.md", "/b.md", "/c.md"]
    assert [r.score for r in results] != [10.0, 9.0, 8.0]
    assert results[0].score == pytest.approx(1.0 / 6 + 0.05)  # rank-1 RRF + top bonus


@pytest.mark.asyncio
async def test_no_embedding_honours_weighted_alpha() -> None:
    """weighted&alpha=1.0 assigns zero weight to the only (keyword) leg —
    scores must reflect the requested fusion, not raw BM25."""
    daemon = _make_no_embed_daemon()
    results = await _hybrid(daemon, alpha=1.0, fusion_method="weighted")

    assert all(r.score == 0.0 for r in results)


@pytest.mark.asyncio
async def test_no_embedding_non_default_results_stamped_degraded() -> None:
    """#4541 review round 6: the dense leg is missing, so non-default fusion
    results carry the #3778 semantic_degraded marker."""
    daemon = _make_no_embed_daemon()
    results = await _hybrid(daemon, alpha=1.0, fusion_method="weighted")

    assert results and all(r.semantic_degraded is True for r in results)

    # The byte-identical default shortcut stays unstamped.
    default = await _hybrid(daemon)
    assert all(not r.semantic_degraded for r in default)


@pytest.mark.asyncio
async def test_fallback_non_default_keeps_fusion_provenance() -> None:
    """#4541 review round 6: non-default fallback fusion reports the leg
    scores that actually produced the fused score (shared hits carry BOTH
    keyword_score and vector_score), unlike the legacy default shape."""
    daemon = _make_fallback_daemon()
    results = await daemon._hybrid_search("nexus core", 4, None, 0.5, "rrf_weighted")

    by_path = {r.path: r for r in results}
    # /a.md is in both legs: fusion provenance keeps both modality scores.
    assert by_path["/a.md"].keyword_score == 10.0
    assert by_path["/a.md"].vector_score == 0.5
    # /d.md is dense-only.
    assert by_path["/d.md"].keyword_score is None
    assert by_path["/d.md"].vector_score == 0.99


@pytest.mark.asyncio
async def test_empty_dense_leg_stamps_non_default_results_degraded() -> None:
    """#4541 review round 7: embedding OK but vector index empty — non-default
    fusion is ranking on keyword legs alone and must carry the marker."""
    daemon = _make_daemon()

    class EmptyVectorBackend:
        async def semantic_search(
            self, qvec: list[float], path: str, limit: int, zone_id: str
        ) -> list[Any]:
            return []

    daemon._vector_backend = EmptyVectorBackend()

    stamped = await _hybrid(daemon, alpha=1.0, fusion_method="weighted")
    assert stamped and all(r.semantic_degraded is True for r in stamped)

    default = await _hybrid(daemon)
    assert all(not r.semantic_degraded for r in default)


@pytest.mark.asyncio
async def test_fallback_empty_semantic_leg_stamps_non_default_degraded() -> None:
    daemon = _make_fallback_daemon()

    async def _semantic_search(
        self: Any,
        query: str,
        limit: int,
        path_filter: Any,
        *,
        zone_id: Any = None,
        query_vector: list[float] | None = None,
        propagate_failures: bool = False,
        embedding_unavailable: bool = False,
    ) -> list[Any]:
        return []

    daemon._semantic_search = MethodType(_semantic_search, daemon)

    stamped = await daemon._hybrid_search("nexus core", 4, None, 0.0, "rrf_weighted")
    assert stamped and all(r.semantic_degraded is True for r in stamped)

    default = await daemon._hybrid_search("nexus core", 4, None, 0.5, "rrf")
    assert all(not r.semantic_degraded for r in default)


@pytest.mark.asyncio
async def test_dense_leg_exception_fails_soft() -> None:
    """#4541 review round 8: a vector-backend exception degrades hybrid to
    keyword-only instead of aborting the whole query."""
    daemon = _make_daemon()

    class RaisingVectorBackend:
        async def semantic_search(
            self, qvec: list[float], path: str, limit: int, zone_id: str
        ) -> list[Any]:
            raise RuntimeError("pgvector connection lost")

    daemon._vector_backend = RaisingVectorBackend()

    default = await _hybrid(daemon)
    assert [r.path for r in default] == ["/a.md", "/b.md", "/c.md"]
    assert all(not r.semantic_degraded for r in default)

    stamped = await _hybrid(daemon, alpha=1.0, fusion_method="weighted")
    assert stamped and all(r.semantic_degraded is True for r in stamped)


@pytest.mark.asyncio
async def test_empty_degraded_response_keeps_list_level_flag() -> None:
    """#4541 review round 8: when the keyword leg is ALSO empty, the degraded
    response is empty — the list-level flag is the only surviving signal."""
    daemon = _make_no_embed_daemon()

    class EmptyFtsBackend:
        async def keyword_search(
            self,
            query: str,
            path: str,
            limit: int,
            zone_id: str,
            *,
            timing: dict[str, float] | None = None,
        ) -> list[Any]:
            return []

    daemon._fts_backend = EmptyFtsBackend()

    results = await _hybrid(daemon, alpha=1.0, fusion_method="weighted")
    assert list(results) == []
    assert getattr(results, "semantic_degraded", False) is True


@pytest.mark.asyncio
async def test_keyword_failure_propagates_promptly_despite_hung_dense_leg() -> None:
    """#4541 review round 9: a keyword-leg failure must not wait on (or be
    masked by) a hung vector backend — the dense task is cancelled and the
    keyword error re-raised promptly."""
    import asyncio

    daemon = _make_daemon()
    dense_cancelled = asyncio.Event()

    class FailingFtsBackend:
        async def keyword_search(
            self,
            query: str,
            path: str,
            limit: int,
            zone_id: str,
            *,
            timing: dict[str, float] | None = None,
        ) -> list[Any]:
            raise RuntimeError("fts down")

    class HangingVectorBackend:
        async def semantic_search(
            self, qvec: list[float], path: str, limit: int, zone_id: str
        ) -> list[Any]:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                dense_cancelled.set()
                raise
            return []

    daemon._fts_backend = FailingFtsBackend()
    daemon._vector_backend = HangingVectorBackend()

    with pytest.raises(RuntimeError, match="fts down"):
        await asyncio.wait_for(_hybrid(daemon), timeout=5)
    assert dense_cancelled.is_set()


@pytest.mark.asyncio
async def test_fallback_keyword_failure_propagates() -> None:
    """#4541 review round 10: the legacy fallback holds the same asymmetric
    contract as the primary path — keyword failures propagate (no dense-only
    'hybrid' response), only the semantic leg is fail-soft."""
    daemon = _make_fallback_daemon()

    async def _keyword_search(
        self: Any, query: str, limit: int, path_filter: Any, *, zone_id: Any = None
    ) -> list[Any]:
        raise RuntimeError("keyword stack down")

    daemon._keyword_search = MethodType(_keyword_search, daemon)

    with pytest.raises(RuntimeError, match="keyword stack down"):
        await daemon._hybrid_search("nexus core", 4, None, 0.5, "rrf")
