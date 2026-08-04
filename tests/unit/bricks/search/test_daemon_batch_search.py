"""Batch search plumbing: pre-computed query_vector threads through the daemon.

SearchRequest.query_vector allows the batch endpoint to embed all unique queries
once and hand each inner search its vector, bypassing redundant per-search embeds.
Tests pin the thread and the embed-skip logic.
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


class _CountingEmbed:
    """Records embed calls made through daemon._embed_query."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __call__(self, daemon: Any, query: str) -> list[float]:
        self.calls.append(query)
        return [0.1, 0.2]


@pytest.mark.asyncio
async def test_query_vector_override_skips_embed() -> None:
    daemon = _make_daemon()
    counter = _CountingEmbed()
    daemon._embed_query = MethodType(counter.__call__, daemon)

    results = await daemon._search_via_backends(
        "hello",
        search_type="hybrid",
        limit=5,
        path_filter=None,
        zone_id="root",
        query_vector=[0.3, 0.4],
    )

    assert counter.calls == []
    assert results  # fused hybrid results still produced


@pytest.mark.asyncio
async def test_no_query_vector_still_embeds() -> None:
    daemon = _make_daemon()
    counter = _CountingEmbed()
    daemon._embed_query = MethodType(counter.__call__, daemon)

    await daemon._search_via_backends(
        "hello",
        search_type="hybrid",
        limit=5,
        path_filter=None,
        zone_id="root",
    )

    assert counter.calls == ["hello"]


def _make_batch_daemon(concurrency: int = 8) -> Any:
    """Bare daemon for _batch_search_on_current_loop: search() is stubbed
    per-test; only batch orchestration fields are real."""
    from unittest.mock import AsyncMock

    from nexus.bricks.search.daemon import DaemonConfig, SearchDaemon

    daemon: Any = SearchDaemon.__new__(SearchDaemon)
    daemon._initialized = True
    daemon._embedding_client = None
    daemon.config = DaemonConfig(batch_search_concurrency=concurrency)
    # Path-context attach is exercised elsewhere; resolve to None (= skip).
    daemon._resolve_path_context_cache = AsyncMock(return_value=None)
    return daemon


class _FakeEmbeddingClient:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[list[str]] = []
        self.fail = fail

    async def embed_batch(self, texts: Any) -> list[list[float]]:
        self.calls.append(list(texts))
        if self.fail:
            raise RuntimeError("embed down")
        return [[0.1, 0.2] for _ in texts]


@pytest.mark.asyncio
async def test_batch_pre_embeds_unique_texts_once() -> None:
    daemon = _make_batch_daemon()
    client = _FakeEmbeddingClient()
    daemon._embedding_client = client
    seen_vectors: list[Any] = []

    async def fake_search(self: Any, request: Any) -> list[Any]:
        seen_vectors.append(request.query_vector)
        return []

    daemon.search = MethodType(fake_search, daemon)
    specs = [{"query": "same text", "search_type": "hybrid", "limit": 5} for _ in range(24)]

    out = await daemon._batch_search_on_current_loop(specs, zone_id="root")

    assert client.calls == [["same text"]]  # ONE embed_batch call, unique texts only
    assert seen_vectors == [[0.1, 0.2]] * 24
    assert out == [[] for _ in range(24)]


@pytest.mark.asyncio
async def test_batch_keyword_only_never_embeds() -> None:
    daemon = _make_batch_daemon()
    client = _FakeEmbeddingClient()
    daemon._embedding_client = client

    async def fake_search(self: Any, request: Any) -> list[Any]:
        return []

    daemon.search = MethodType(fake_search, daemon)

    await daemon._batch_search_on_current_loop(
        [{"query": "kw", "search_type": "keyword"}], zone_id="root"
    )

    assert client.calls == []


@pytest.mark.asyncio
async def test_batch_embed_failure_falls_back_to_per_query() -> None:
    daemon = _make_batch_daemon()
    daemon._embedding_client = _FakeEmbeddingClient(fail=True)
    seen_vectors: list[Any] = []

    async def fake_search(self: Any, request: Any) -> list[Any]:
        seen_vectors.append(request.query_vector)
        return []

    daemon.search = MethodType(fake_search, daemon)

    out = await daemon._batch_search_on_current_loop(
        [{"query": "a", "search_type": "hybrid"}], zone_id="root"
    )

    assert out == [[]]  # fail-soft: search still ran
    assert seen_vectors == [None]  # ...and embeds for itself downstream


@pytest.mark.asyncio
async def test_batch_failure_isolated_per_query() -> None:
    from nexus.contracts.search_types import BatchQueryFailure

    daemon = _make_batch_daemon()

    async def fake_search(self: Any, request: Any) -> list[Any]:
        if request.query == "poison":
            raise RuntimeError("boom")
        return []

    daemon.search = MethodType(fake_search, daemon)

    out = await daemon._batch_search_on_current_loop(
        [{"query": "ok1"}, {"query": "poison"}, {"query": "ok2"}], zone_id="root"
    )

    assert out[0] == [] and out[2] == []
    assert isinstance(out[1], BatchQueryFailure)
    assert "boom" in out[1].error


@pytest.mark.asyncio
async def test_batch_respects_concurrency_bound() -> None:
    import asyncio

    daemon = _make_batch_daemon(concurrency=2)
    peak = 0
    active = 0

    async def fake_search(self: Any, request: Any) -> list[Any]:
        nonlocal peak, active
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return []

    daemon.search = MethodType(fake_search, daemon)

    await daemon._batch_search_on_current_loop(
        [{"query": f"q{i}"} for i in range(10)], zone_id="root"
    )

    assert peak <= 2


@pytest.mark.asyncio
async def test_batch_forwards_tuning_params() -> None:
    daemon = _make_batch_daemon()
    captured: list[Any] = []

    async def fake_search(self: Any, request: Any) -> list[Any]:
        captured.append(request)
        return []

    daemon.search = MethodType(fake_search, daemon)

    await daemon._batch_search_on_current_loop(
        [
            {
                "query": "tuned",
                "search_type": "hybrid",
                "limit": 7,
                "path_filter": "/ws/",
                "alpha": 0.3,
                "fusion_method": "weighted",
                "rrf_k": 90,
                "expand": "macro",
                "recency": "on",
                "recency_weight": 1.5,
                "recency_half_life_days": 30.0,
            }
        ],
        zone_id="root",
    )

    req = captured[0]
    assert (req.alpha, req.fusion_method, req.rrf_k) == (0.3, "weighted", 90)
    assert (req.expand, req.recency, req.recency_weight) == ("macro", "on", 1.5)
    assert req.recency_half_life_days == 30.0
    assert (req.limit, req.path_filter, req.zone_id) == (7, "/ws/", "root")


@pytest.mark.asyncio
async def test_batch_non_dict_spec_becomes_failure() -> None:
    from nexus.contracts.search_types import BatchQueryFailure

    daemon = _make_batch_daemon()

    async def fake_search(self: Any, request: Any) -> list[Any]:
        return []

    daemon.search = MethodType(fake_search, daemon)

    out = await daemon._batch_search_on_current_loop(["not a dict"], zone_id="root")

    assert isinstance(out[0], BatchQueryFailure)
