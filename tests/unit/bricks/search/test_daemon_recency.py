"""Recency boost wiring through SearchDaemon.search() (Issue #4543).

Mirrors the #4541 fake-backend harness (test_daemon_fusion_params.py):
keyword and dense orderings disagree so any score change is visible.
The hydration seam ``_fetch_recency_mtimes`` is monkeypatched — no DB.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import MethodType
from typing import Any

import pytest

# The daemon boosts against real datetime.now(UTC) (no injectable clock on the
# request path), so fixtures must be built relative to the real now — a fixed
# constant would silently age and change the expected multipliers.


def _make_daemon(mtimes: dict[str, datetime] | None = None, **config_kwargs: Any) -> Any:
    """Bare SearchDaemon: fake fts/vector backends + fake mtime hydration.

    Corpus: keyword /a.md > /b.md > /c.md; dense /d.md > /c.md > /a.md
    (same fixture as test_daemon_fusion_params.py).
    """
    from nexus.bricks.search.daemon import (
        DaemonConfig,
        DaemonStats,
        SearchDaemon,
        SearchResult,
    )

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

    async def _attach_path_contexts(
        self: Any,
        results: Any,
        *,
        zone_id: str,
        pinned_snapshots: Any = None,
        apply_weights: bool = True,
    ) -> None:
        return None

    def _track_latency(self: Any, latency_ms: float) -> None:
        return None

    fetch_calls: list[tuple[tuple[str, ...], str]] = []

    async def _fetch_recency_mtimes(self: Any, paths: Any, *, zone_id: str) -> dict[str, datetime]:
        fetch_calls.append((tuple(paths), zone_id))
        return dict(mtimes or {})

    daemon: Any = SearchDaemon.__new__(SearchDaemon)
    daemon._initialized = True
    daemon.last_search_timing = {}
    daemon._fts_backend = FakeFtsBackend()
    daemon._vector_backend = FakeVectorBackend()
    daemon._embed_query = MethodType(_embed_query, daemon)
    daemon._attach_path_contexts = MethodType(_attach_path_contexts, daemon)
    daemon._track_latency = MethodType(_track_latency, daemon)
    daemon._fetch_recency_mtimes = MethodType(_fetch_recency_mtimes, daemon)
    daemon.config = DaemonConfig(page_aggregation=False, **config_kwargs)
    daemon.stats = DaemonStats()
    daemon._recency_fetch_calls = fetch_calls  # test-side spy
    return daemon


def _old_new_mtimes() -> dict[str, datetime]:
    """/b.md is fresh, everything else is years old — with default fusion
    ordering /a.md > /d.md > /c.md > /b.md, a strong boost lifts /b.md.

    RRF-score arithmetic for this fixture (k=60, top-rank bonuses 0.05/0.02):
    a≈.0823, d≈.0664, c≈.0520, b≈.0361. At weight=0.3 the fresh ×1.3 on b
    (.0470) still trails c's stale .0523 — so ordering tests use weight=1.0
    (b → .0722, ancient ×≈1.02 elsewhere), giving a > b > d > c."""
    now = datetime.now(UTC)
    old = now - timedelta(days=1500)
    return {"/a.md": old, "/b.md": now, "/c.md": old, "/d.md": old}


@pytest.mark.asyncio
async def test_default_request_untouched_and_no_hydration() -> None:
    """Byte-identity for neutral queries: under the default config (auto) a
    query with no recency-intent word matches #4541's pinned ordering and
    runs NO hydration query."""
    daemon = _make_daemon(_old_new_mtimes())
    results = await daemon.search("nexus core", search_type="hybrid", limit=4)

    assert [r.path for r in results] == ["/a.md", "/d.md", "/c.md", "/b.md"]
    assert all(r.recency_boost is None for r in results)
    assert daemon._recency_fetch_calls == []


@pytest.mark.asyncio
async def test_zero_config_default_is_auto(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deploy-and-forget default: with NO env and NO request params, a
    recency-intent query gets the boost (recency_mode defaults to auto)."""
    monkeypatch.delenv("NEXUS_SEARCH_RECENCY", raising=False)
    daemon = _make_daemon(_old_new_mtimes())
    results = await daemon.search("latest nexus core", search_type="hybrid", limit=4)

    assert len(daemon._recency_fetch_calls) == 1
    assert any(r.recency_boost is not None for r in results)


@pytest.mark.asyncio
async def test_recency_on_boosts_and_reorders() -> None:
    """recency='on' (weight=1.0) hydrates once and lifts the fresh /b.md
    from last place to second; results carry recency_boost attribution.
    See _old_new_mtimes docstring for the RRF arithmetic."""
    daemon = _make_daemon(_old_new_mtimes())
    results = await daemon.search(
        "nexus core", search_type="hybrid", limit=4, recency="on", recency_weight=1.0
    )

    assert len(daemon._recency_fetch_calls) == 1
    assert [r.path for r in results] == ["/a.md", "/b.md", "/d.md", "/c.md"]
    b = results[1]
    assert b.recency_boost == pytest.approx(2.0, rel=0.01)  # fresh: 1 + 1.0*H/(H+~0)
    assert all(r.recency_boost is not None for r in results)  # every path hydrated
    scores = [r.score for r in results]
    assert scores == sorted(scores, reverse=True)


@pytest.mark.asyncio
async def test_recency_auto_fires_only_on_intent_queries() -> None:
    daemon = _make_daemon(_old_new_mtimes(), recency_mode="auto")

    neutral = await daemon.search("nexus core", search_type="hybrid", limit=4)
    assert daemon._recency_fetch_calls == []
    assert all(r.recency_boost is None for r in neutral)

    boosted = await daemon.search("latest nexus core", search_type="hybrid", limit=4)
    assert len(daemon._recency_fetch_calls) == 1
    assert any(r.recency_boost is not None for r in boosted)


@pytest.mark.asyncio
async def test_request_params_override_config() -> None:
    """Explicit request knobs beat config: recency='off' suppresses a
    config-on daemon; explicit weight reaches the boost math."""
    daemon = _make_daemon(_old_new_mtimes(), recency_mode="on")
    off = await daemon.search("nexus core", search_type="hybrid", limit=4, recency="off")
    assert all(r.recency_boost is None for r in off)
    assert daemon._recency_fetch_calls == []

    daemon2 = _make_daemon(_old_new_mtimes())
    on = await daemon2.search(
        "nexus core", search_type="hybrid", limit=4, recency="on", recency_weight=1.0
    )
    b = next(r for r in on if r.path == "/b.md")
    assert b.recency_boost == pytest.approx(2.0, rel=0.01)  # 1 + 1.0 * H/(H+~0)


@pytest.mark.asyncio
async def test_hydration_failure_is_fail_soft() -> None:
    """A hydration exception must not 500 the search: results come back
    unboosted and the stats counter increments."""
    daemon = _make_daemon()

    async def _boom(self: Any, paths: Any, *, zone_id: str) -> dict[str, datetime]:
        raise RuntimeError("db down")

    daemon._fetch_recency_mtimes = MethodType(_boom, daemon)
    results = await daemon.search("nexus core", search_type="hybrid", limit=4, recency="on")

    assert [r.path for r in results] == ["/a.md", "/d.md", "/c.md", "/b.md"]
    assert all(r.recency_boost is None for r in results)
    assert daemon.stats.recency_attach_failures == 1


@pytest.mark.asyncio
async def test_mtime_hydration_routes_through_owner_loop() -> None:
    """Zoned searches run the search body on the daemon owner loop, and
    asyncpg pools are loop-affine — so the chokepoint must route the mtime
    hydration (and ONLY the hydration) through _run_on_owner_loop: exactly
    one call when recency is active, zero on the default (off) path."""
    daemon = _make_daemon(_old_new_mtimes())
    calls: list[int] = []

    async def _spy(self: Any, work: Any) -> Any:
        calls.append(1)
        return await work()

    daemon._run_on_owner_loop = MethodType(_spy, daemon)

    await daemon.search("nexus core", search_type="hybrid", limit=4)
    assert calls == []

    boosted = await daemon.search(
        "nexus core", search_type="hybrid", limit=4, recency="on", recency_weight=1.0
    )
    assert len(calls) == 1
    assert len(daemon._recency_fetch_calls) == 1
    assert any(r.recency_boost is not None for r in boosted)


@pytest.mark.asyncio
async def test_keyword_and_semantic_paths_also_boost() -> None:
    """The chokepoint lives in search(), so non-hybrid types get the boost
    too (hydration-approach coverage win over per-SELECT carrying)."""
    daemon = _make_daemon(_old_new_mtimes())
    kw = await daemon.search("nexus core", search_type="keyword", limit=3, recency="on")
    assert any(r.recency_boost is not None for r in kw)

    daemon2 = _make_daemon(_old_new_mtimes())
    sem = await daemon2.search("nexus core", search_type="semantic", limit=3, recency="on")
    assert any(r.recency_boost is not None for r in sem)
