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
