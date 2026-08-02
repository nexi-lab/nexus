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
from nexus.contracts.search_types import SearchRequest

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


# ---------------------------------------------------------------------------
# Round-3 review: dispatcher-level cap behavior on both federated control paths
# ---------------------------------------------------------------------------


def _capture_daemon(
    zone_results: dict[str, list[Any]],
    config: DaemonConfig,
    seen_limits: dict[str, int],
) -> Any:
    """Mock daemon that honors the requested limit (slices its fixture) and
    records the limit each zone was asked for."""
    daemon = SimpleNamespace(config=config, is_initialized=True)

    async def search(request: SearchRequest) -> list[Any]:
        seen_limits[request.zone_id or ""] = request.limit
        return list(zone_results.get(request.zone_id or "", []))[: request.limit]

    daemon.search = search
    return daemon


def _rebac_for(zones: list[str]) -> AsyncMock:
    rebac = AsyncMock()
    rebac.list_accessible_zones = AsyncMock(return_value=zones)
    return rebac


@pytest.mark.asyncio
async def test_single_zone_federated_applies_cap_with_overfetch() -> None:
    """Round-3 review: the single-zone fast path bypassed pooling entirely —
    the cap must not depend on how many zones are accessible."""
    seen: dict[str, int] = {}
    daemon = _capture_daemon(
        {
            "z1": [
                _local_result("z1", "/doc.md", 0, 0.9),
                _local_result("z1", "/doc.md", 1, 0.8),
                _local_result("z1", "/doc.md", 2, 0.7),
                _local_result("z1", "/b.md", 0, 0.5),
            ]
        },
        DaemonConfig(page_aggregation=True, chunks_per_page=1),
        seen,
    )
    dispatcher = FederatedSearchDispatcher(daemon=daemon, rebac=_rebac_for(["z1"]))

    resp = await dispatcher.search("q", subject=("user", "u1"), search_type="semantic", limit=2)

    assert [r["path"] for r in resp.results] == ["/doc.md", "/b.md"]
    # Over-fetched so the cap can backfill instead of shrinking the page.
    assert seen["z1"] > 2


@pytest.mark.asyncio
async def test_single_zone_federated_flag_off_keeps_fast_path() -> None:
    seen: dict[str, int] = {}
    daemon = _capture_daemon(
        {
            "z1": [
                _local_result("z1", "/doc.md", 0, 0.9),
                _local_result("z1", "/doc.md", 1, 0.8),
                _local_result("z1", "/b.md", 0, 0.5),
            ]
        },
        DaemonConfig(page_aggregation=False, chunks_per_page=2),
        seen,
    )
    dispatcher = FederatedSearchDispatcher(daemon=daemon, rebac=_rebac_for(["z1"]))

    resp = await dispatcher.search("q", subject=("user", "u1"), limit=2)

    # Historical behavior: exact-limit fetch, no cap, no dedup on this path.
    assert seen["z1"] == 2
    assert [r["path"] for r in resp.results] == ["/doc.md", "/doc.md"]


@pytest.mark.asyncio
async def test_multi_zone_saturated_window_backfills() -> None:
    """Round-3 review repro: limit=3, cap=1, each zone's leading window is one
    long doc with the next doc ranked just below it. The cap-aware wider
    window must let other docs backfill instead of underfilling the page."""
    config = DaemonConfig(page_aggregation=True, chunks_per_page=1)
    seen: dict[str, int] = {}

    def zone_rows(zone: str) -> list[Any]:
        rows = [_local_result(zone, f"/{zone}-long.md", i, 1.0 - i * 0.01) for i in range(6)]
        rows.append(_local_result(zone, f"/{zone}-next.md", 0, 0.9))
        return rows

    daemon = _capture_daemon({"za": zone_rows("za"), "zb": zone_rows("zb")}, config, seen)
    dispatcher = FederatedSearchDispatcher(daemon=daemon, rebac=_rebac_for(["za", "zb"]))

    resp = await dispatcher.search("q", subject=("user", "u1"), search_type="semantic", limit=3)

    paths = [r["path"] for r in resp.results]
    assert len(paths) == 3
    # One chunk per doc (cap=1) and the below-window docs backfilled.
    assert len(set(paths)) == 3
    assert any("next" in p for p in paths)


# ---------------------------------------------------------------------------
# Round-4 review: degraded/alternate paths and cache zone scoping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hybrid_embed_failure_fallback_still_caps() -> None:
    """Round-4 review: an embedding-provider outage sent hybrid down the
    keyword-only fallback which bypassed the cap and fetched exact-limit
    (no backfill headroom)."""

    class _SlicingFtsBackend:
        def __init__(self, rows: list[SearchResult]) -> None:
            self._rows = rows
            self.seen_limits: list[int] = []

        async def keyword_search(
            self,
            query: str,
            path: str,
            limit: int,
            zone_id: str,
            *,
            timing: dict[str, float] | None = None,
        ) -> list[SearchResult]:
            self.seen_limits.append(limit)
            return list(self._rows)[:limit]

    rows = [_chunk("/long.md", i, 10.0 - i) for i in range(4)] + [
        _chunk("/other-a.md", 0, 5.0),
        _chunk("/other-b.md", 0, 4.0),
    ]
    daemon: Any = SearchDaemon.__new__(SearchDaemon)
    daemon.last_search_timing = {}
    daemon.config = DaemonConfig(page_aggregation=True, chunks_per_page=1)
    daemon._fts_backend = _SlicingFtsBackend(rows)
    daemon._vector_backend = _RowsVectorBackend([])

    async def _embed_query(self: Any, query: str) -> None:
        return None

    daemon._embed_query = MethodType(_embed_query, daemon)

    results = await daemon._search_via_backends(
        "query", search_type="hybrid", limit=4, path_filter=None, zone_id="root"
    )

    paths = [r.path for r in results]
    assert paths == ["/long.md", "/other-a.md", "/other-b.md"]
    # Over-fetched beyond the requested limit so the cap could backfill.
    assert daemon._fts_backend.seen_limits[0] > 4


@pytest.mark.asyncio
async def test_survivor_branch_applies_cap_when_other_zone_empty() -> None:
    """Round-4 review: with one zone empty, the one-surviving-list branch
    sliced without capping — zone failures must not change flag semantics."""
    seen: dict[str, int] = {}
    daemon = _capture_daemon(
        {
            "za": [
                _local_result("za", "/doc.md", 0, 0.9),
                _local_result("za", "/doc.md", 1, 0.8),
                _local_result("za", "/b.md", 0, 0.5),
            ],
            "zb": [],
        },
        DaemonConfig(page_aggregation=True, chunks_per_page=1),
        seen,
    )
    dispatcher = FederatedSearchDispatcher(daemon=daemon, rebac=_rebac_for(["za", "zb"]))

    resp = await dispatcher.search("q", subject=("user", "u1"), limit=2)

    assert [r["path"] for r in resp.results] == ["/doc.md", "/b.md"]


@pytest.mark.asyncio
async def test_rrf_strategy_honors_cap_at_chunk_grain() -> None:
    """Round-4 review: RRF fusion deduped at page grain, returning one chunk
    per doc regardless of chunks_per_page and underfilling the request."""
    from nexus.bricks.search.federated_search import FederatedSearchConfig

    seen: dict[str, int] = {}
    daemon = _capture_daemon(
        {
            "za": [
                _local_result("za", "/doc.md", 0, 0.9),
                _local_result("za", "/doc.md", 1, 0.8),
                _local_result("za", "/doc.md", 2, 0.7),
                _local_result("za", "/b.md", 0, 0.5),
            ],
            "zb": [_local_result("zb", "/c.md", 0, 0.6)],
        },
        DaemonConfig(page_aggregation=True, chunks_per_page=2),
        seen,
    )
    dispatcher = FederatedSearchDispatcher(
        daemon=daemon,
        rebac=_rebac_for(["za", "zb"]),
        config=FederatedSearchConfig(fusion_strategy="rrf"),
    )

    resp = await dispatcher.search("q", subject=("user", "u1"), limit=4)

    paths = [r["path"] for r in resp.results]
    assert paths.count("/doc.md") == 2
    assert set(paths) == {"/doc.md", "/b.md", "/c.md"}
    assert all("_zq_chunk" not in r for r in resp.results)


@pytest.mark.asyncio
async def test_result_cache_scoped_by_zone_filter() -> None:
    """Round-4 review (tenant isolation): a broad-scope and a narrow-scope
    token for the same subject must not share a cache entry."""
    from nexus.bricks.search.federated_search import FederatedSearchConfig

    seen: dict[str, int] = {}
    daemon = _capture_daemon(
        {
            "za": [_local_result("za", "/a.md", 0, 0.9)],
            "zb": [_local_result("zb", "/b.md", 0, 0.8)],
        },
        DaemonConfig(page_aggregation=True, chunks_per_page=2),
        seen,
    )
    dispatcher = FederatedSearchDispatcher(
        daemon=daemon,
        rebac=_rebac_for(["za", "zb"]),
        config=FederatedSearchConfig(result_cache_enabled=True),
    )

    broad = await dispatcher.search("q", subject=("user", "u1"), limit=10)
    narrow = await dispatcher.search(
        "q", subject=("user", "u1"), limit=10, zone_filter=frozenset({"za"})
    )

    assert {r["zone_id"] for r in broad.results} == {"za", "zb"}
    assert not narrow.cached
    assert {r["zone_id"] for r in narrow.results} == {"za"}


# ---------------------------------------------------------------------------
# Round-5 review: cache-key collisions, legacy hybrid, window saturation
# ---------------------------------------------------------------------------


def test_cache_key_has_no_cross_field_collisions() -> None:
    """Round-5 review: delimiter concatenation let subject_id 'alice|foo' +
    query 'bar' collide with subject 'alice' + query 'foo|bar' — and cache
    lookup precedes ReBAC, so a collision leaks another subject's results."""
    dispatcher = FederatedSearchDispatcher(daemon=AsyncMock(), rebac=AsyncMock())

    k1 = dispatcher._make_cache_key("bar", ("user", "alice|foo"), "hybrid", 10, None)
    k2 = dispatcher._make_cache_key("foo|bar", ("user", "alice"), "hybrid", 10, None)
    k3 = dispatcher._make_cache_key("bar", ("user:x", "alice"), "hybrid", 10, None)
    k4 = dispatcher._make_cache_key("bar", ("user", "x:alice"), "hybrid", 10, None)

    assert len({k1, k2, k3, k4}) == 4


@pytest.mark.asyncio
async def test_legacy_hybrid_fallback_caps_at_shared_boundary() -> None:
    """Round-5 review: when the new backends are absent, hybrid routes to the
    legacy ``_hybrid_search`` stack which bypassed the cap entirely."""
    daemon: Any = SearchDaemon.__new__(SearchDaemon)
    daemon._initialized = True
    daemon._fts_backend = None
    daemon._vector_backend = None
    daemon._permission_enforcer = None
    daemon.last_search_timing = {}
    daemon.config = DaemonConfig(page_aggregation=True, chunks_per_page=1)

    def _track_latency(self: Any, latency_ms: float) -> None:
        self._last_latency_ms = latency_ms

    async def _attach_path_contexts(
        self: Any, results: Any, *, zone_id: str, **_attach_kwargs: Any
    ) -> None:
        return None

    legacy_limits: list[int] = []

    async def _hybrid_search(
        self: Any,
        query: str,
        limit: int,
        path_filter: str | None,
        alpha: float,
        fusion_method: str,
        rrf_k: int = 60,
        *,
        zone_id: str | None = None,
    ) -> list[SearchResult]:
        legacy_limits.append(limit)
        rows = [_chunk("/long.md", i, 10.0 - i) for i in range(4)] + [
            _chunk("/other-a.md", 0, 5.0),
            _chunk("/other-b.md", 0, 4.0),
        ]
        return rows[:limit]

    async def _keyword_search(self: Any, *args: Any, **kwargs: Any) -> list[SearchResult]:
        return []

    daemon._track_latency = MethodType(_track_latency, daemon)
    daemon._attach_path_contexts = MethodType(_attach_path_contexts, daemon)
    daemon._hybrid_search = MethodType(_hybrid_search, daemon)
    daemon._keyword_search = MethodType(_keyword_search, daemon)

    results = await daemon.search(
        SearchRequest(query="q", search_type="hybrid", limit=3, zone_id="root")
    )

    paths = [r.path for r in results]
    assert paths == ["/long.md", "/other-a.md", "/other-b.md"]
    # Legacy stack was asked for a wider window so the cap could backfill.
    assert legacy_limits[0] > 3


@pytest.mark.asyncio
async def test_leg_windows_widen_so_backfill_survives_saturated_prefix() -> None:
    """Round-5 review: with limit=4 and cap=1, eight leading one-doc chunks
    saturated the old limit*2 leg windows; alternatives at ranks 9-10 were
    never fetched and the response underfilled."""

    class _SlicingBackend:
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
            return list(self._rows)[:limit]

        async def semantic_search(
            self, qvec: list[float], path: str, limit: int, zone_id: str
        ) -> list[SearchResult]:
            return list(self._rows)[:limit]

    rows = [_chunk("/long.md", i, 10.0 - i * 0.1) for i in range(8)] + [
        _chunk("/other-a.md", 0, 5.0),
        _chunk("/other-b.md", 0, 4.0),
    ]
    backend = _SlicingBackend(rows)
    daemon: Any = SearchDaemon.__new__(SearchDaemon)
    daemon.last_search_timing = {}
    daemon.config = DaemonConfig(page_aggregation=True, chunks_per_page=1)
    daemon._fts_backend = backend
    daemon._vector_backend = backend

    async def _embed_query(self: Any, query: str) -> list[float]:
        return [0.1, 0.2]

    daemon._embed_query = MethodType(_embed_query, daemon)

    results = await daemon._search_via_backends(
        "query", search_type="hybrid", limit=4, path_filter=None, zone_id="root"
    )

    paths = [r.path for r in results]
    assert paths == ["/long.md", "/other-a.md", "/other-b.md"]


# ---------------------------------------------------------------------------
# Round-6 review: fetch-window compounding
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_local_hybrid_zones_keep_historical_fetch_window() -> None:
    """Round-6 review: the dispatcher's cap-aware widening must NOT stack on
    top of the local hybrid daemon's own cap-aware legs — the multipliers
    compounded into pathological retrieval windows. Local hybrid zones keep
    the historical limit x over_fetch_factor window; semantic zones (where
    dispatcher-side capping is the only protection) get the wider one."""
    seen: dict[str, int] = {}
    rows = {
        "za": [_local_result("za", "/a.md", 0, 0.9)],
        "zb": [_local_result("zb", "/b.md", 0, 0.8)],
    }
    config = DaemonConfig(page_aggregation=True, chunks_per_page=2)

    daemon = _capture_daemon(rows, config, seen)
    dispatcher = FederatedSearchDispatcher(daemon=daemon, rebac=_rebac_for(["za", "zb"]))
    await dispatcher.search("q", subject=("user", "u1"), search_type="hybrid", limit=5)
    # over_fetch_factor default = 2 → historical window 10, no (cap+1) stacking.
    assert seen == {"za": 10, "zb": 10}

    seen.clear()
    daemon = _capture_daemon(rows, config, seen)
    dispatcher = FederatedSearchDispatcher(daemon=daemon, rebac=_rebac_for(["za", "zb"]))
    await dispatcher.search("q", subject=("user", "u1"), search_type="semantic", limit=5)
    # Semantic zones are only capped dispatcher-side → widened by (cap+1).
    assert seen == {"za": 30, "zb": 30}


@pytest.mark.asyncio
async def test_retrieval_widening_is_bounded_for_huge_caps() -> None:
    """Round-10 review: chunks_per_page is an unbounded emission cap — the
    retrieval-window widening it drives must saturate, or cap=1000 turns a
    limit=100 request into ~600k-row leg scans."""

    class _LimitCaptureBackend:
        def __init__(self) -> None:
            self.limits: list[int] = []

        async def keyword_search(
            self,
            query: str,
            path: str,
            limit: int,
            zone_id: str,
            *,
            timing: dict[str, float] | None = None,
        ) -> list[SearchResult]:
            self.limits.append(limit)
            return []

        async def semantic_search(
            self, qvec: list[float], path: str, limit: int, zone_id: str
        ) -> list[SearchResult]:
            self.limits.append(limit)
            return []

    backend = _LimitCaptureBackend()
    daemon: Any = SearchDaemon.__new__(SearchDaemon)
    daemon.last_search_timing = {}
    daemon.config = DaemonConfig(page_aggregation=True, chunks_per_page=1000)
    daemon._fts_backend = backend
    daemon._vector_backend = backend

    async def _embed_query(self: Any, query: str) -> list[float]:
        return [0.1, 0.2]

    daemon._embed_query = MethodType(_embed_query, daemon)

    await daemon._search_via_backends(
        "query", search_type="hybrid", limit=100, path_filter=None, zone_id="root"
    )

    # Widening saturates: limit * 2 * (min(cap, 4) + 1) = 1000, not 200_200.
    assert backend.limits
    assert max(backend.limits) <= 1000
