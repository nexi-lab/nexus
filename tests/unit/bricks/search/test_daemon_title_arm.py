"""Skeleton title arm in hybrid fusion (Issue #4545).

locate() — BM25-lite over path tokens + title — joins the keyword
sub-fusion as a third arm. These tests pin attribution plumbing,
hydration, the sub-fusion swap, and non-title-query parity.
"""

from __future__ import annotations

from typing import Any

import pytest


def test_coerce_preserves_title_score_from_dict() -> None:
    from nexus.bricks.search.daemon import SearchDaemon

    res = SearchDaemon._coerce_to_search_result(
        {"path": "/a.md", "chunk_text": "x", "score": 1.0, "title_score": 2.5},
        search_type="hybrid",
    )
    assert res.title_score == 2.5


def test_coerce_preserves_title_score_from_dataclass() -> None:
    from nexus.bricks.search.daemon import SearchDaemon
    from nexus.bricks.search.results import BaseSearchResult

    base = BaseSearchResult(path="/a.md", chunk_text="x", score=1.0, title_score=2.5)
    res = SearchDaemon._coerce_to_search_result(base, search_type="hybrid")
    assert res.title_score == 2.5


def test_daemon_config_title_arm_defaults_on() -> None:
    from nexus.bricks.search.daemon import DaemonConfig

    assert DaemonConfig().title_arm is True
    assert DaemonConfig(title_arm=False).title_arm is False


def _hydration_daemon(fetch_rows: list[Any] | None = None, fetch_raises: bool = False):
    """Bare daemon with only what _hydrate_title_hits touches."""
    from nexus.bricks.search.daemon import SearchDaemon

    calls: list[Any] = []

    class FakeVec:
        async def fetch_ranges(self, spans: Any, zone_id: Any) -> list[Any]:
            calls.append((list(spans), zone_id))
            if fetch_raises:
                raise RuntimeError("boom")
            return list(fetch_rows or [])

    daemon: Any = SearchDaemon.__new__(SearchDaemon)
    daemon._vector_backend = FakeVec()
    return daemon, calls


def _leg(path: str, score: float, chunk_index: int, text: str) -> Any:
    from nexus.bricks.search.results import BaseSearchResult

    return BaseSearchResult(
        path=path,
        chunk_text=text,
        score=score,
        chunk_index=chunk_index,
        line_start=1,
        line_end=2,
    )


@pytest.mark.asyncio
async def test_hydrate_borrows_best_leg_chunk_no_fetch() -> None:
    """Covered path: borrow the best-scored chunk-leg row; fetch_ranges not called."""
    daemon, calls = _hydration_daemon()
    chunk_kw = [_leg("/t.md", 3.0, 4, "weak"), _leg("/t.md", 9.0, 7, "strong")]
    hits = await daemon._hydrate_title_hits(
        [{"path": "/t.md", "score": 6.0, "title": "T"}],
        chunk_kw=chunk_kw,
        page_kw=[],
        zone_id="root",
    )
    assert calls == []
    assert hits[0].chunk_index == 7
    assert hits[0].chunk_text == "strong"
    assert hits[0].score == 6.0
    assert hits[0].zone_id == "root"


@pytest.mark.asyncio
async def test_hydrate_prefers_page_leg_over_chunk_leg() -> None:
    """Page leg is already best-of-page — wins over the chunk pick."""
    daemon, _ = _hydration_daemon()
    hits = await daemon._hydrate_title_hits(
        [{"path": "/t.md", "score": 6.0, "title": "T"}],
        chunk_kw=[_leg("/t.md", 9.0, 7, "chunk-pick")],
        page_kw=[_leg("/t.md", 8.0, 2, "page-pick")],
        zone_id="root",
    )
    assert hits[0].chunk_index == 2
    assert hits[0].chunk_text == "page-pick"


@pytest.mark.asyncio
async def test_hydrate_uncovered_path_one_batched_fetch() -> None:
    """Uncovered paths hydrate via exactly one batched fetch_ranges call."""
    from nexus.bricks.search.macro_chunk import ChunkRow

    row = ChunkRow(
        path="/u.md",
        chunk_index=0,
        text="chunk zero",
        tokens=2,
        line_start=1,
        line_end=3,
        heading_prefix=None,
    )
    daemon, calls = _hydration_daemon(fetch_rows=[row])
    hits = await daemon._hydrate_title_hits(
        [
            {"path": "/u.md", "score": 6.0, "title": "U"},
            {"path": "/v.md", "score": 4.0, "title": "V"},
        ],
        chunk_kw=[],
        page_kw=[],
        zone_id="root",
    )
    assert len(calls) == 1
    assert calls[0] == ([("/u.md", 0, 0), ("/v.md", 0, 0)], "root")
    assert hits[0].chunk_text == "chunk zero"
    assert hits[0].line_start == 1
    # /v.md had no chunk rows (chunkless doc) — still retrievable, empty text.
    assert hits[1].path == "/v.md"
    assert hits[1].chunk_text == ""
    assert hits[1].chunk_index == 0


@pytest.mark.asyncio
async def test_hydrate_fetch_failure_degrades_not_fails() -> None:
    """A hydration fetch failure must never fail the search."""
    daemon, _ = _hydration_daemon(fetch_raises=True)
    hits = await daemon._hydrate_title_hits(
        [{"path": "/u.md", "score": 6.0, "title": "U"}],
        chunk_kw=[],
        page_kw=[],
        zone_id="root",
    )
    assert hits[0].chunk_text == ""
    assert hits[0].score == 6.0


@pytest.mark.asyncio
async def test_hydrate_no_fetch_ranges_backend() -> None:
    """Vector backend without fetch_ranges (protocol-minimal) degrades gracefully."""
    from nexus.bricks.search.daemon import SearchDaemon

    daemon: Any = SearchDaemon.__new__(SearchDaemon)
    daemon._vector_backend = object()
    hits = await daemon._hydrate_title_hits(
        [{"path": "/u.md", "score": 6.0, "title": "U"}],
        chunk_kw=[],
        page_kw=[],
        zone_id="root",
    )
    assert hits[0].chunk_text == ""
