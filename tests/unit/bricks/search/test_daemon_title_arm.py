"""Skeleton title arm in hybrid fusion (Issue #4545).

locate() — BM25-lite over path tokens + title — joins the keyword
sub-fusion as a third arm. These tests pin attribution plumbing,
hydration, the sub-fusion swap, and non-title-query parity.
"""

from __future__ import annotations

from types import MethodType
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


def _make_daemon(
    *,
    title_arm: bool = True,
    skeleton: dict[str, dict[str, Any]] | None = None,
    embed_none: bool = False,
):
    """Bare SearchDaemon with fake backends + skeleton docs.

    Keyword (BM25): /a.md 10 > /b.md 9 > /c.md 8; dense: /d.md .99 > /c.md .9
    > /a.md .5 (mirrors test_daemon_fusion_params). Skeleton adds a doc whose
    title matches "atlas design doc" but which no body leg returns.
    """
    from nexus.bricks.search.daemon import DaemonConfig, SearchDaemon, SearchResult
    from nexus.bricks.search.macro_chunk import ChunkRow

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

        async def fetch_ranges(self, spans: Any, zone_id: Any) -> list[Any]:
            return [
                ChunkRow(
                    path=p,
                    chunk_index=0,
                    text=f"body of {p}",
                    tokens=3,
                    line_start=1,
                    line_end=2,
                    heading_prefix=None,
                )
                for p, _lo, _hi in spans
            ]

    async def _embed_query(self: Any, query: str) -> list[float] | None:
        return None if embed_none else [0.1, 0.2]

    daemon: Any = SearchDaemon.__new__(SearchDaemon)
    daemon.last_search_timing = {}
    daemon._fts_backend = FakeFtsBackend()
    daemon._vector_backend = FakeVectorBackend()
    daemon._embed_query = MethodType(_embed_query, daemon)
    daemon._skeleton_docs = dict(skeleton or {})
    daemon._skeleton_token_index = {}
    daemon.config = DaemonConfig(page_aggregation=False, title_arm=title_arm)
    return daemon


_ATLAS_SKELETON = {
    "/designs/atlas.md": {
        "path_id": "p-atlas",
        "zone_id": "root",
        "title": "Atlas Design Doc",
        "path_tokens": "designs atlas md",
    }
}


async def _hybrid(daemon: Any, query: str, **kwargs: Any) -> list[Any]:
    return await daemon._search_via_backends(
        query, search_type="hybrid", limit=4, path_filter=None, zone_id="root", **kwargs
    )


@pytest.mark.asyncio
async def test_title_match_doc_enters_hybrid_top_n() -> None:
    """Acceptance (#4545): a doc whose title matches the query but whose body
    chunks are weak (absent from every leg) enters the hybrid top-N with
    title_score set. Without the arm it does not surface."""
    on = _make_daemon(title_arm=True, skeleton=_ATLAS_SKELETON)
    results = await _hybrid(on, "atlas design doc")
    paths = [r.path for r in results]
    assert "/designs/atlas.md" in paths
    atlas = next(r for r in results if r.path == "/designs/atlas.md")
    # locate score: 3 title-token overlaps * 2.0 + 1 path-token overlap ("atlas")
    assert atlas.title_score == pytest.approx(7.0)
    assert atlas.chunk_text == "body of /designs/atlas.md"  # hydrated chunk 0
    assert on.last_search_timing["title_ms"] >= 0.0

    off = _make_daemon(title_arm=False, skeleton=_ATLAS_SKELETON)
    assert "/designs/atlas.md" not in [r.path for r in await _hybrid(off, "atlas design doc")]


def test_merge_backend_timing_preserves_title_ms() -> None:
    """title_ms must survive the public search() timing filter: search() sets
    last_search_timing via _merge_backend_timing, which drops any key missing
    from _BACKEND_LEG_TIMING_KEYS (#4545)."""
    from nexus.bricks.search.daemon import _merge_backend_timing

    assert _merge_backend_timing(1.0, {"title_ms": 2.0})["title_ms"] == 2.0


@pytest.mark.asyncio
async def test_non_title_query_parity_arm_on_vs_off() -> None:
    """Non-title queries are byte-identical with the arm on and off: locate
    has no hits, so the title arm contributes nothing."""
    on = _make_daemon(title_arm=True, skeleton=_ATLAS_SKELETON)
    off = _make_daemon(title_arm=False, skeleton=_ATLAS_SKELETON)
    r_on = await _hybrid(on, "nexus core")
    r_off = await _hybrid(off, "nexus core")
    assert [r.path for r in r_on] == [r.path for r in r_off]
    assert [r.score for r in r_on] == pytest.approx([r.score for r in r_off])
    assert all(r.title_score is None for r in r_on)


@pytest.mark.asyncio
async def test_flag_off_never_calls_locate() -> None:
    daemon = _make_daemon(title_arm=False, skeleton=_ATLAS_SKELETON)
    called: list[str] = []

    async def _locate(self: Any, q: str, **kwargs: Any) -> list[dict[str, Any]]:
        called.append(q)
        return []

    daemon.locate = MethodType(_locate, daemon)
    await _hybrid(daemon, "atlas design doc")
    assert called == []


def test_rrf_multi_two_arms_matches_rrf_fusion() -> None:
    """Guard for the sub-fusion swap: 3-arm rrf_multi_fusion with an empty
    title arm is rank- and score-identical to the old 2-way rrf_fusion."""
    from nexus.bricks.search.fusion import rrf_fusion, rrf_multi_fusion

    kw = [
        {"path": "/a.md", "chunk_index": 0, "score": 10.0},
        {"path": "/b.md", "chunk_index": 0, "score": 9.0},
        {"path": "/c.md", "chunk_index": 0, "score": 8.0},
    ]
    page = [
        {"path": "/b.md", "chunk_index": 2, "score": 5.0},
        {"path": "/e.md", "chunk_index": 1, "score": 4.0},
    ]
    two = rrf_fusion(kw, page, k=60, limit=8, id_key=None)
    multi = rrf_multi_fusion(
        [("chunk", kw), ("page", page), ("title", [])], k=60, limit=8, id_key=None
    )
    assert [r["path"] for r in multi] == [r["path"] for r in two]
    assert [r["score"] for r in multi] == pytest.approx([r["score"] for r in two])


@pytest.mark.asyncio
async def test_no_embedding_fallback_still_runs_title_arm() -> None:
    """BM25-only / embedding-failure hybrid keeps the title arm (#4545
    review): a title-only doc must surface even when _embed_query yields
    None, with title_score attribution intact."""
    daemon = _make_daemon(title_arm=True, skeleton=_ATLAS_SKELETON, embed_none=True)
    results = await _hybrid(daemon, "atlas design doc")
    paths = [r.path for r in results]
    assert "/designs/atlas.md" in paths
    atlas = next(r for r in results if r.path == "/designs/atlas.md")
    assert atlas.title_score == pytest.approx(7.0)
    assert atlas.search_type == "hybrid"
    assert daemon.last_search_timing.get("title_ms", 0.0) >= 0.0


@pytest.mark.asyncio
async def test_no_embedding_fallback_parity_when_no_title_hits() -> None:
    """No-embedding fallback with no locate hits is byte-identical to the
    arm-off keyword fallback: same paths, same untouched BM25 scores."""
    on = _make_daemon(title_arm=True, skeleton=_ATLAS_SKELETON, embed_none=True)
    off = _make_daemon(title_arm=False, skeleton=_ATLAS_SKELETON, embed_none=True)
    r_on = await _hybrid(on, "nexus core")
    r_off = await _hybrid(off, "nexus core")
    assert [r.path for r in r_on] == [r.path for r in r_off] == ["/a.md", "/b.md", "/c.md"]
    assert [r.score for r in r_on] == [10.0, 9.0, 8.0]
    assert all(r.title_score is None for r in r_on)


def _bare_locate_daemon() -> Any:
    from nexus.bricks.search.daemon import SearchDaemon

    daemon: Any = SearchDaemon.__new__(SearchDaemon)
    daemon._skeleton_docs = {}
    daemon._skeleton_token_index = {}
    return daemon


@pytest.mark.asyncio
async def test_locate_index_upsert_delete_rename() -> None:
    """The inverted postings index stays consistent through upsert, title
    change, and delete (rename = delete + upsert)."""
    daemon = _bare_locate_daemon()
    daemon.upsert_skeleton_doc(
        path_id="p1", virtual_path="/src/old_login.py", title="Old Login", zone_id="root"
    )
    assert [h["path"] for h in await daemon.locate("login", zone_id="root")] == [
        "/src/old_login.py"
    ]

    # Title change must drop stale title postings.
    daemon.upsert_skeleton_doc(
        path_id="p1", virtual_path="/src/old_login.py", title="Auth Portal", zone_id="root"
    )
    hits = await daemon.locate("portal", zone_id="root")
    assert [h["path"] for h in hits] == ["/src/old_login.py"]

    # Rename: delete old path, upsert new one.
    daemon.delete_skeleton_doc(virtual_path="/src/old_login.py", zone_id="root")
    daemon.upsert_skeleton_doc(
        path_id="p1", virtual_path="/src/new_login.py", title="Auth Portal", zone_id="root"
    )
    hits = await daemon.locate("login", zone_id="root")
    assert [h["path"] for h in hits] == ["/src/new_login.py"]
    assert await daemon.locate("nonexistentterm", zone_id="root") == []


@pytest.mark.asyncio
async def test_locate_lazy_rebuild_from_direct_dict_fixture() -> None:
    """Docs inserted directly into _skeleton_docs (old fixture shape, no
    postings) are found via the lazy index rebuild."""
    daemon = _bare_locate_daemon()
    daemon._skeleton_docs = dict(_ATLAS_SKELETON)
    hits = await daemon.locate("atlas design doc", zone_id="root")
    assert [h["path"] for h in hits] == ["/designs/atlas.md"]
    assert hits[0]["score"] == pytest.approx(7.0)
