"""Final-list per-document pooling for hybrid search (Issue #4542).

``DaemonConfig.page_aggregation`` / ``chunks_per_page`` existed (Issue #3980)
but were never applied to the final fused hybrid list — one long document
could occupy every result slot. These tests pin:

- daemon: the fused hybrid list emits at most ``chunks_per_page`` chunks per
  document, letting other documents enter the top-N;
- ``page_aggregation=False`` restores chunk-grain behavior exactly;
- federated: the raw-score flat merge applies the same per-zone pooling so
  remote-zone (chunk-grain) results are not chunk-diluted, while results for
  the same path in different zones stay separate.
"""

from __future__ import annotations

from types import MethodType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from nexus.bricks.search.daemon import DaemonConfig, SearchDaemon, SearchResult
from nexus.bricks.search.federated_search import (
    FederatedSearchDispatcher,
    _merge_by_raw_score,
)

# ---------------------------------------------------------------------------
# Daemon: hybrid final-list pooling
# ---------------------------------------------------------------------------

_LONG_DOC_ROWS = [
    SearchResult(path="/long.md", chunk_text="c0", score=10.0, chunk_index=0, search_type="k"),
    SearchResult(path="/long.md", chunk_text="c1", score=9.0, chunk_index=1, search_type="k"),
    SearchResult(path="/long.md", chunk_text="c2", score=8.0, chunk_index=2, search_type="k"),
    SearchResult(path="/long.md", chunk_text="c3", score=7.0, chunk_index=3, search_type="k"),
    SearchResult(path="/other-a.md", chunk_text="a", score=6.0, chunk_index=0, search_type="k"),
    SearchResult(path="/other-b.md", chunk_text="b", score=5.0, chunk_index=0, search_type="k"),
]


class _FakeFtsBackend:
    """Plain (non-PG) FTS backend → exercises the 2-way hybrid branch."""

    async def keyword_search(
        self,
        query: str,
        path: str,
        limit: int,
        zone_id: str,
        *,
        timing: dict[str, float] | None = None,
    ) -> list[SearchResult]:
        return list(_LONG_DOC_ROWS)


class _FakeVectorBackend:
    async def semantic_search(
        self, qvec: list[float], path: str, limit: int, zone_id: str
    ) -> list[SearchResult]:
        return list(_LONG_DOC_ROWS)


def _hybrid_daemon(config: DaemonConfig) -> Any:
    daemon: Any = SearchDaemon.__new__(SearchDaemon)
    daemon.last_search_timing = {}
    daemon.config = config
    daemon._fts_backend = _FakeFtsBackend()
    daemon._vector_backend = _FakeVectorBackend()

    async def _embed_query(self: Any, query: str) -> list[float]:
        return [0.1, 0.2]

    daemon._embed_query = MethodType(_embed_query, daemon)
    return daemon


@pytest.mark.asyncio
async def test_hybrid_final_list_pools_dominant_doc() -> None:
    """One long doc matching many chunks is capped at ``chunks_per_page``
    in the fused hybrid list, so other docs enter the top-N."""
    daemon = _hybrid_daemon(DaemonConfig(page_aggregation=True, chunks_per_page=2))

    results = await daemon._search_via_backends(
        "query",
        search_type="hybrid",
        limit=6,
        path_filter=None,
        zone_id="root",
    )

    paths = [r.path for r in results]
    assert paths == ["/long.md", "/long.md", "/other-a.md", "/other-b.md"]
    # Best two chunks of the dominant doc survive, in fused-rank order.
    assert [r.chunk_index for r in results[:2]] == [0, 1]


@pytest.mark.asyncio
async def test_page_aggregation_false_restores_chunk_grain_behavior() -> None:
    """NEXUS_SEARCH_PAGE_AGGREGATION=false parity: no pooling at all."""
    daemon = _hybrid_daemon(DaemonConfig(page_aggregation=False, chunks_per_page=2))

    results = await daemon._search_via_backends(
        "query",
        search_type="hybrid",
        limit=6,
        path_filter=None,
        zone_id="root",
    )

    paths = [r.path for r in results]
    assert paths == ["/long.md"] * 4 + ["/other-a.md", "/other-b.md"]


@pytest.mark.asyncio
async def test_chunks_per_page_cap_honored() -> None:
    daemon = _hybrid_daemon(DaemonConfig(page_aggregation=True, chunks_per_page=1))

    results = await daemon._search_via_backends(
        "query",
        search_type="hybrid",
        limit=6,
        path_filter=None,
        zone_id="root",
    )

    paths = [r.path for r in results]
    assert paths == ["/long.md", "/other-a.md", "/other-b.md"]


# ---------------------------------------------------------------------------
# Federated: raw-score flat merge pooling
# ---------------------------------------------------------------------------


def _remote_chunk(zone: str, path: str, idx: int, score: float) -> dict[str, Any]:
    """Remote-zone style result: plain dict, chunk-grain, no
    ``zone_qualified_path`` (so the merge dedup key stays chunk-grain)."""
    return {
        "path": path,
        "chunk_index": idx,
        "chunk_text": f"{path}#{idx}",
        "score": score,
        "zone_id": zone,
    }


def test_merge_by_raw_score_pools_remote_style_chunks() -> None:
    zone_lists = [
        (
            "zone-a",
            [
                _remote_chunk("zone-a", "/doc.md", 0, 0.9),
                _remote_chunk("zone-a", "/doc.md", 1, 0.8),
                _remote_chunk("zone-a", "/doc.md", 2, 0.7),
                _remote_chunk("zone-a", "/doc.md", 3, 0.6),
                _remote_chunk("zone-a", "/b.md", 0, 0.5),
            ],
        ),
    ]

    merged = _merge_by_raw_score(zone_lists, limit=10, chunks_per_page=2)

    paths = [r["path"] for r in merged]
    assert paths == ["/doc.md", "/doc.md", "/b.md"]


def test_merge_by_raw_score_default_keeps_chunk_grain() -> None:
    """Without a pooling cap the merge behaves exactly as before."""
    zone_lists = [
        (
            "zone-a",
            [
                _remote_chunk("zone-a", "/doc.md", 0, 0.9),
                _remote_chunk("zone-a", "/doc.md", 1, 0.8),
                _remote_chunk("zone-a", "/doc.md", 2, 0.7),
                _remote_chunk("zone-a", "/b.md", 0, 0.5),
            ],
        ),
    ]

    merged = _merge_by_raw_score(zone_lists, limit=10)

    assert len(merged) == 4


def test_merge_pooling_is_zone_scoped() -> None:
    """Same path in two zones = two distinct documents; pooling must not
    collapse them across zones."""
    zone_lists = [
        ("zone-a", [_remote_chunk("zone-a", "/doc.md", 0, 0.9)]),
        ("zone-b", [_remote_chunk("zone-b", "/doc.md", 0, 0.8)]),
    ]

    merged = _merge_by_raw_score(zone_lists, limit=10, chunks_per_page=1)

    assert [(r["zone_id"], r["path"]) for r in merged] == [
        ("zone-a", "/doc.md"),
        ("zone-b", "/doc.md"),
    ]


def test_dispatcher_reads_pooling_cap_from_daemon_config() -> None:
    daemon = SimpleNamespace(config=DaemonConfig(page_aggregation=True, chunks_per_page=3))
    dispatcher = FederatedSearchDispatcher(daemon=daemon, rebac=AsyncMock())

    assert dispatcher._pooling_chunks_per_page() == 3


def test_dispatcher_pooling_disabled_by_config() -> None:
    daemon = SimpleNamespace(config=DaemonConfig(page_aggregation=False, chunks_per_page=2))
    dispatcher = FederatedSearchDispatcher(daemon=daemon, rebac=AsyncMock())

    assert dispatcher._pooling_chunks_per_page() is None


def test_dispatcher_pooling_off_for_configless_daemon() -> None:
    """Mock daemons (tests) and daemons without a real DaemonConfig must not
    accidentally enable pooling via Mock auto-attributes."""
    dispatcher = FederatedSearchDispatcher(daemon=AsyncMock(), rebac=AsyncMock())

    assert dispatcher._pooling_chunks_per_page() is None


# ---------------------------------------------------------------------------
# Review hardening (#4550 follow-up): backfill, ordering, real federated shapes
# ---------------------------------------------------------------------------


class _RowsFtsBackend:
    def __init__(self, rows: list[SearchResult]) -> None:
        self._rows = rows

    async def keyword_search(
        self,
        query: str,
        path: str,
        limit: int,
        zone_id: str,
        *,
        timing: dict[str, float] | None = None,
    ) -> list[SearchResult]:
        return list(self._rows)


class _RowsVectorBackend:
    def __init__(self, rows: list[SearchResult]) -> None:
        self._rows = rows

    async def semantic_search(
        self, qvec: list[float], path: str, limit: int, zone_id: str
    ) -> list[SearchResult]:
        return list(self._rows)


def _daemon_with_rows(config: DaemonConfig, rows: list[SearchResult]) -> Any:
    daemon: Any = SearchDaemon.__new__(SearchDaemon)
    daemon.last_search_timing = {}
    daemon.config = config
    daemon._fts_backend = _RowsFtsBackend(rows)
    daemon._vector_backend = _RowsVectorBackend(rows)

    async def _embed_query(self: Any, query: str) -> list[float]:
        return [0.1, 0.2]

    daemon._embed_query = MethodType(_embed_query, daemon)
    return daemon


def _chunk(path: str, idx: int, score: float) -> SearchResult:
    return SearchResult(
        path=path, chunk_text=f"{path}#{idx}", score=score, chunk_index=idx, search_type="k"
    )


@pytest.mark.asyncio
async def test_pooling_backfills_below_the_requested_limit_cutoff() -> None:
    """When one doc's chunks fill the entire requested-limit window, the cap
    must backfill other docs from below the cutoff instead of returning an
    underfilled page (review finding: fuse-then-trim-then-cap starved the
    response)."""
    rows = [_chunk("/long.md", i, 10.0 - i) for i in range(8)] + [
        _chunk("/other-a.md", 0, 1.5),
        _chunk("/other-b.md", 0, 1.0),
    ]
    daemon = _daemon_with_rows(DaemonConfig(page_aggregation=True, chunks_per_page=2), rows)

    results = await daemon._search_via_backends(
        "query", search_type="hybrid", limit=6, path_filter=None, zone_id="root"
    )

    paths = [r.path for r in results]
    assert paths == ["/long.md", "/long.md", "/other-a.md", "/other-b.md"]


@pytest.mark.asyncio
async def test_pooling_preserves_fused_score_ordering() -> None:
    """The cap must be a stable filter over the fused ranking, not a
    page-grouped re-emission: a doc's second chunk may not leapfrog a
    higher-scored doc (review finding: A=0.9, A=0.1, B=0.8 ordering)."""
    rows = [
        _chunk("/a.md", 0, 10.0),
        _chunk("/b.md", 0, 9.0),
        _chunk("/a.md", 1, 8.0),
        _chunk("/c.md", 0, 7.0),
    ]
    daemon = _daemon_with_rows(DaemonConfig(page_aggregation=True, chunks_per_page=2), rows)

    results = await daemon._search_via_backends(
        "query", search_type="hybrid", limit=4, path_filter=None, zone_id="root"
    )

    assert [r.path for r in results] == ["/a.md", "/b.md", "/a.md", "/c.md"]
    scores = [r.score for r in results]
    assert scores == sorted(scores, reverse=True)


def _local_result(zone: str, path: str, idx: int, score: float) -> SearchResult:
    """Production local-zone shape: dataclass tagged with zone_id, whose
    ``zone_qualified_path`` property is PAGE-grain (``zone:path``)."""
    r = _chunk(path, idx, score)
    r.zone_id = zone
    return r


def _remote_result(zone: str, path: str, idx: int, score: float) -> dict[str, Any]:
    """Production remote-zone shape: dict with an explicit page-grain
    ``zone_qualified_path`` (see _search_remote_zone)."""
    return {
        "path": path,
        "chunk_index": idx,
        "chunk_text": f"{path}#{idx}",
        "score": score,
        "zone_id": zone,
        "zone_qualified_path": f"{zone}:{path}",
    }


def test_merge_cap_honored_on_local_dataclass_shape() -> None:
    """Review finding: the page-grain zone_qualified_path dedup collapsed
    every doc back to ONE chunk, silently overriding chunks_per_page > 1 on
    the shapes production actually emits."""
    zone_lists = [
        (
            "zone-a",
            [
                _local_result("zone-a", "/doc.md", 0, 0.9),
                _local_result("zone-a", "/doc.md", 1, 0.8),
                _local_result("zone-a", "/doc.md", 2, 0.7),
                _local_result("zone-a", "/b.md", 0, 0.5),
            ],
        ),
    ]

    merged = _merge_by_raw_score(zone_lists, limit=10, chunks_per_page=2)

    assert [r["path"] for r in merged] == ["/doc.md", "/doc.md", "/b.md"]


def test_merge_cap_honored_on_remote_dict_shape() -> None:
    zone_lists = [
        (
            "zone-a",
            [
                _remote_result("zone-a", "/doc.md", 0, 0.9),
                _remote_result("zone-a", "/doc.md", 1, 0.8),
                _remote_result("zone-a", "/doc.md", 2, 0.7),
                _remote_result("zone-a", "/b.md", 0, 0.5),
            ],
        ),
    ]

    merged = _merge_by_raw_score(zone_lists, limit=10, chunks_per_page=2)

    assert [r["path"] for r in merged] == ["/doc.md", "/doc.md", "/b.md"]


def test_merge_flag_off_keeps_historical_page_grain_dedup() -> None:
    """With pooling disabled the merge must keep the pre-#4542 behavior on
    production shapes: page-grain dedup, one (highest-scoring) chunk per doc
    per zone."""
    zone_lists = [
        (
            "zone-a",
            [
                _local_result("zone-a", "/doc.md", 0, 0.9),
                _local_result("zone-a", "/doc.md", 1, 0.8),
                _local_result("zone-a", "/b.md", 0, 0.5),
            ],
        ),
    ]

    merged = _merge_by_raw_score(zone_lists, limit=10)

    assert [(r["path"], r["chunk_index"]) for r in merged] == [("/doc.md", 0), ("/b.md", 0)]


@pytest.mark.asyncio
async def test_backfill_survives_asymmetric_legs_saturated_by_one_doc() -> None:
    """Round-2 review scenario: keyword leg is ENTIRELY one doc (12 chunks)
    and the dense leg is half that doc — a fixed limit×2 fusion window kept
    mostly long-doc chunks and returned an underfilled page. Fusing the full
    candidate union must fill all requested slots."""
    long_kw = [_chunk("/long.md", i, 12.0 - i) for i in range(12)]
    dense = [_chunk("/long.md", i, 0.99 - i * 0.01) for i in range(6)] + [
        _chunk(f"/other-{j}.md", 0, 0.5 - j * 0.01) for j in range(6)
    ]

    daemon: Any = SearchDaemon.__new__(SearchDaemon)
    daemon.last_search_timing = {}
    daemon.config = DaemonConfig(page_aggregation=True, chunks_per_page=2)
    daemon._fts_backend = _RowsFtsBackend(long_kw)
    daemon._vector_backend = _RowsVectorBackend(dense)

    async def _embed_query(self: Any, query: str) -> list[float]:
        return [0.1, 0.2]

    daemon._embed_query = MethodType(_embed_query, daemon)

    results = await daemon._search_via_backends(
        "query", search_type="hybrid", limit=6, path_filter=None, zone_id="root"
    )

    paths = [r.path for r in results]
    assert len(paths) == 6
    assert paths.count("/long.md") == 2
    assert len({p for p in paths if p != "/long.md"}) == 4
