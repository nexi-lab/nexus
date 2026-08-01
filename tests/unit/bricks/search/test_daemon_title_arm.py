"""Skeleton title arm in hybrid fusion (Issue #4545).

locate() — BM25-lite over path tokens + title — joins the keyword
sub-fusion as a third arm. These tests pin attribution plumbing,
hydration, the sub-fusion swap, and non-title-query parity.
"""

from __future__ import annotations

import asyncio
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
    daemon._skeleton_title_index = {}
    daemon._skeleton_path_index = {}
    daemon._skeleton_exact_title_index = {}
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
    daemon._skeleton_title_index = {}
    daemon._skeleton_path_index = {}
    daemon._skeleton_exact_title_index = {}
    daemon._skeleton_bootstrap_journal = None
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


@pytest.mark.asyncio
async def test_locate_zone_scoped_same_path_no_clobber() -> None:
    """Same virtual_path in two zones must not clobber each other (#4545
    review round 2): upsert/delete are (zone_id, virtual_path)-scoped."""
    daemon = _bare_locate_daemon()
    daemon.upsert_skeleton_doc(
        path_id="pa", virtual_path="/README.md", title="Alpha Guide", zone_id="zone-a"
    )
    daemon.upsert_skeleton_doc(
        path_id="pb", virtual_path="/README.md", title="Beta Guide", zone_id="zone-b"
    )
    assert [h["title"] for h in await daemon.locate("guide", zone_id="zone-a")] == ["Alpha Guide"]
    assert [h["title"] for h in await daemon.locate("guide", zone_id="zone-b")] == ["Beta Guide"]

    daemon.delete_skeleton_doc(virtual_path="/README.md", zone_id="zone-b")
    assert [h["title"] for h in await daemon.locate("guide", zone_id="zone-a")] == ["Alpha Guide"]
    assert await daemon.locate("guide", zone_id="zone-b") == []


@pytest.mark.asyncio
async def test_bootstrap_replays_live_mutations() -> None:
    """A live delete/update racing the bootstrap DB snapshot must win: the
    snapshot can neither resurrect a deleted doc nor roll back a newer
    title (#4545 review round 2)."""
    from nexus.bricks.search.daemon import SearchDaemon

    daemon: Any = SearchDaemon.__new__(SearchDaemon)
    daemon._skeleton_docs = {}
    daemon._skeleton_title_index = {}
    daemon._skeleton_path_index = {}
    daemon._skeleton_exact_title_index = {}
    daemon._skeleton_bootstrap_journal = None
    daemon._skeleton_bootstrapped = False

    release = asyncio.Event()

    class FakeResult:
        def fetchall(self) -> list[tuple[str, str, str, str]]:
            return [
                ("p1", "root", "Doomed Doc", "/doomed.md"),
                ("p2", "root", "Stale Title", "/updated.md"),
            ]

    class FakeSession:
        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *args: Any) -> bool:
            return False

        async def execute(self, *_args: Any, **_kwargs: Any) -> Any:
            await release.wait()
            return FakeResult()

    daemon._async_session = lambda: FakeSession()

    task = asyncio.create_task(daemon._bootstrap_skeleton())
    while daemon._skeleton_bootstrap_journal is None:
        await asyncio.sleep(0)

    # Live mutations racing the snapshot (both docs are in the DB rows):
    daemon.delete_skeleton_doc(virtual_path="/doomed.md", zone_id="root")
    daemon.upsert_skeleton_doc(
        path_id="p2", virtual_path="/updated.md", title="Fresh Title", zone_id="root"
    )
    release.set()
    await task

    assert daemon._skeleton_bootstrapped is True
    assert daemon._skeleton_bootstrap_journal is None
    assert ("root", "/doomed.md") not in daemon._skeleton_docs
    assert daemon._skeleton_docs[("root", "/updated.md")]["title"] == "Fresh Title"
    assert await daemon.locate("doomed", zone_id="root") == []
    assert [h["path"] for h in await daemon.locate("fresh title", zone_id="root")] == [
        "/updated.md"
    ]


def test_federated_strip_normalizes_title_score() -> None:
    """Federated emission matches the single-zone omit-when-None + round-4
    title_score contract (#4545 review round 2)."""
    from nexus.bricks.search.daemon import SearchResult
    from nexus.bricks.search.federated_search import _result_to_dict, _strip_none_context

    assert "title_score" not in _strip_none_context({"path": "/a", "title_score": None})
    assert _strip_none_context({"path": "/a", "title_score": 7.00004})["title_score"] == 7.0

    plain = SearchResult(path="/a", chunk_text="x", score=1.0)
    assert "title_score" not in _result_to_dict(plain)
    scored = SearchResult(path="/a", chunk_text="x", score=1.0, title_score=3.14159)
    assert _result_to_dict(scored)["title_score"] == 3.1416


def test_postings_are_zone_partitioned() -> None:
    """Postings buckets are keyed (zone_id, token) so one tenant's query
    never expands another tenant's postings (#4545 review round 3)."""
    daemon = _bare_locate_daemon()
    daemon.upsert_skeleton_doc(
        path_id="pa", virtual_path="/guide.md", title="Guide", zone_id="zone-a"
    )
    daemon.upsert_skeleton_doc(
        path_id="pb", virtual_path="/guide.md", title="Guide", zone_id="zone-b"
    )
    assert daemon._skeleton_title_index[("zone-a", "guide")] == {("zone-a", "/guide.md")}
    assert daemon._skeleton_title_index[("zone-b", "guide")] == {("zone-b", "/guide.md")}
    assert daemon._skeleton_path_index[("zone-a", "guide")] == {("zone-a", "/guide.md")}


@pytest.mark.asyncio
async def test_locate_tied_scores_deterministic_order() -> None:
    """Tied candidates order by (score desc, path asc) — stable across
    set-iteration/hash order, so RRF ranks don't drift between restarts
    (#4545 review round 3)."""
    daemon = _bare_locate_daemon()
    for name in ("zeta", "alpha", "mid"):
        daemon.upsert_skeleton_doc(
            path_id=f"p-{name}",
            virtual_path=f"/{name}/report.md",
            title="Quarterly Report",
            zone_id="root",
        )
    first = await daemon.locate("quarterly report", zone_id="root", limit=2)
    second = await daemon.locate("quarterly report", zone_id="root", limit=2)
    assert first == second
    assert [h["path"] for h in first] == ["/alpha/report.md", "/mid/report.md"]


@pytest.mark.asyncio
async def test_hydration_fetch_is_bounded() -> None:
    """Uncovered hydration spans are capped at TITLE_ARM_MAX_HYDRATION_FETCH;
    overflow hits degrade to empty text instead of growing the SQL span
    list (#4545 review round 3)."""
    from nexus.bricks.search.daemon import TITLE_ARM_MAX_HYDRATION_FETCH

    daemon, calls = _hydration_daemon(fetch_rows=[])
    hits = await daemon._hydrate_title_hits(
        [{"path": f"/n{i:03}.md", "score": 5.0, "title": "N"} for i in range(50)],
        chunk_kw=[],
        page_kw=[],
        zone_id="root",
    )
    assert len(calls) == 1
    assert len(calls[0][0]) == TITLE_ARM_MAX_HYDRATION_FETCH
    assert len(hits) == 50  # every hit still emitted, overflow unhydrated


@pytest.mark.asyncio
async def test_gather_title_hits_locate_depth_scales_with_limit() -> None:
    """Locate recall depth scales with the request (limit*2) so ReBAC
    over-fetch keeps deep permitted title hits reachable; only the
    hydration FETCH is capped (#4545 review round 4)."""
    from nexus.bricks.search.daemon import DaemonConfig, SearchDaemon

    seen: dict[str, Any] = {}

    daemon: Any = SearchDaemon.__new__(SearchDaemon)
    daemon.config = DaemonConfig()

    async def _locate(self: Any, q: str, **kwargs: Any) -> list[dict[str, Any]]:
        seen.update(kwargs)
        return []

    daemon.locate = MethodType(_locate, daemon)
    timing: dict[str, float] = {}
    await daemon._gather_title_hits(
        "q", zone_id="root", limit=300, path_filter=None, chunk_kw=[], page_kw=[], timing=timing
    )
    assert seen["limit"] == 600


@pytest.mark.asyncio
async def test_locate_skips_high_df_tokens() -> None:
    """A token whose zone bucket exceeds TITLE_ARM_MAX_TOKEN_DF is skipped
    for candidate selection (non-discriminative), but still counts toward
    overlap scoring for docs selected via selective tokens (#4545 review
    round 4)."""
    from nexus.bricks.search.daemon import TITLE_ARM_MAX_TOKEN_DF

    daemon = _bare_locate_daemon()
    # Flood the "md" bucket past the DF cap.
    for i in range(TITLE_ARM_MAX_TOKEN_DF + 1):
        daemon.upsert_skeleton_doc(
            path_id=f"p{i}", virtual_path=f"/notes/n{i:05}.md", title=None, zone_id="root"
        )
    daemon.upsert_skeleton_doc(
        path_id="px", virtual_path="/designs/atlas.md", title="Atlas Design", zone_id="root"
    )

    # Pure high-DF query: no candidates, arm contributes nothing.
    assert await daemon.locate("md", zone_id="root", limit=5) == []

    # Selective token selects the doc; the high-DF "md" token still scores.
    hits = await daemon.locate("atlas md", zone_id="root", limit=5)
    assert [h["path"] for h in hits] == ["/designs/atlas.md"]
    # atlas: title(2.0) + path(1.0); md: path(1.0) => 4.0
    assert hits[0]["score"] == pytest.approx(4.0)


@pytest.mark.asyncio
async def test_bootstrap_replay_matches_live_order_same_doc() -> None:
    """Journal replay is faithful to live-map application order: for
    same-doc delete-then-upsert during bootstrap, the swapped state equals
    the live end state (consumer-level event ordering is a pre-existing
    #3725 ingestion concern, unchanged by the journal)."""
    from nexus.bricks.search.daemon import SearchDaemon

    daemon: Any = SearchDaemon.__new__(SearchDaemon)
    daemon._skeleton_docs = {}
    daemon._skeleton_title_index = {}
    daemon._skeleton_path_index = {}
    daemon._skeleton_exact_title_index = {}
    daemon._skeleton_bootstrap_journal = None
    daemon._skeleton_bootstrapped = False

    release = asyncio.Event()

    class FakeResult:
        def fetchall(self) -> list[tuple[str, str, str, str]]:
            return [("p1", "root", "Snapshot Title", "/doc.md")]

    class FakeSession:
        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *args: Any) -> bool:
            return False

        async def execute(self, *_args: Any, **_kwargs: Any) -> Any:
            await release.wait()
            return FakeResult()

    daemon._async_session = lambda: FakeSession()
    task = asyncio.create_task(daemon._bootstrap_skeleton())
    while daemon._skeleton_bootstrap_journal is None:
        await asyncio.sleep(0)

    # Same-doc interleaving in live-application order: delete, then upsert.
    daemon.delete_skeleton_doc(virtual_path="/doc.md", zone_id="root")
    daemon.upsert_skeleton_doc(
        path_id="p1", virtual_path="/doc.md", title="Live Title", zone_id="root"
    )
    live_state = dict(daemon._skeleton_docs)
    release.set()
    await task

    assert daemon._skeleton_docs[("root", "/doc.md")]["title"] == "Live Title"
    assert daemon._skeleton_docs.keys() == live_state.keys()


@pytest.mark.asyncio
async def test_title_token_survives_path_flooding() -> None:
    """A token flooded past the DF cap in PATHS must still surface the one
    doc whose TITLE carries it — DF pruning is per-field (#4545 review
    round 5)."""
    from nexus.bricks.search.daemon import TITLE_ARM_MAX_TOKEN_DF

    daemon = _bare_locate_daemon()
    for i in range(TITLE_ARM_MAX_TOKEN_DF + 1):
        daemon.upsert_skeleton_doc(
            path_id=f"p{i}", virtual_path=f"/atlas/f{i:05}.md", title=None, zone_id="root"
        )
    daemon.upsert_skeleton_doc(
        path_id="pt", virtual_path="/docs/overview.md", title="Atlas", zone_id="root"
    )
    hits = await daemon.locate("atlas", zone_id="root", limit=5)
    assert [h["path"] for h in hits] == ["/docs/overview.md"]
    assert hits[0]["score"] == pytest.approx(2.0)  # title-only overlap


@pytest.mark.asyncio
async def test_locate_aggregate_candidate_budget(monkeypatch: Any) -> None:
    """Bucket unions are consumed most-selective-first and stop at the
    aggregate candidate budget (#4545 review round 5)."""
    import nexus.bricks.search.daemon as daemon_mod

    daemon = _bare_locate_daemon()
    # token "aaa": 2 docs; token "bbb": 3 docs; token "ccc": 4 docs.
    for i in range(2):
        daemon.upsert_skeleton_doc(
            path_id=f"a{i}", virtual_path=f"/x/aaa-{i}.md", title=None, zone_id="root"
        )
    for i in range(3):
        daemon.upsert_skeleton_doc(
            path_id=f"b{i}", virtual_path=f"/x/bbb-{i}.md", title=None, zone_id="root"
        )
    for i in range(4):
        daemon.upsert_skeleton_doc(
            path_id=f"c{i}", virtual_path=f"/x/ccc-{i}.md", title=None, zone_id="root"
        )
    monkeypatch.setattr(daemon_mod, "TITLE_ARM_MAX_CANDIDATES", 4)
    hits = await daemon.locate("aaa bbb ccc", zone_id="root", limit=20)
    # Most selective buckets first: aaa (2) fully selected, bbb (3) pushes
    # past the budget of 4, ccc (4) never selected. Every candidate scores
    # 2.0 (path overlap incl. shared /x token... aaa docs: tokens {x, aaa, i, md}).
    paths = {h["path"] for h in hits}
    assert all("aaa" in p or "bbb" in p for p in paths)
    assert not any("ccc" in p for p in paths)


@pytest.mark.asyncio
async def test_title_and_dense_votes_merge_on_dense_chunk() -> None:
    """A title-matched doc absent from keyword legs but present in dense
    borrows the dense row's chunk_index, so title + dense votes fuse into
    ONE result identity instead of duplicate rows (#4545 review round 5)."""
    from nexus.bricks.search.daemon import DaemonConfig, SearchDaemon, SearchResult

    def _kw(path: str, score: float) -> SearchResult:
        return SearchResult(
            path=path, chunk_text=path, score=score, chunk_index=0, search_type="keyword"
        )

    def _dense(path: str, score: float, chunk_index: int = 0) -> SearchResult:
        return SearchResult(
            path=path,
            chunk_text=f"{path}#{chunk_index}",
            score=score,
            chunk_index=chunk_index,
            search_type="semantic",
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
            return [_kw("/a.md", 10.0), _kw("/b.md", 9.0)]

    class FakeVectorBackend:
        async def semantic_search(
            self, qvec: list[float], path: str, limit: int, zone_id: str
        ) -> list[Any]:
            return [_dense("/designs/atlas.md", 0.95, chunk_index=7), _dense("/a.md", 0.5)]

        async def fetch_ranges(self, spans: Any, zone_id: Any) -> list[Any]:
            raise AssertionError("dense borrow should make the DB fetch unnecessary")

    async def _embed_query(self: Any, query: str) -> list[float]:
        return [0.1, 0.2]

    daemon: Any = SearchDaemon.__new__(SearchDaemon)
    daemon.last_search_timing = {}
    daemon._fts_backend = FakeFtsBackend()
    daemon._vector_backend = FakeVectorBackend()
    daemon._embed_query = MethodType(_embed_query, daemon)
    daemon._skeleton_docs = dict(_ATLAS_SKELETON)
    daemon._skeleton_title_index = {}
    daemon._skeleton_path_index = {}
    daemon._skeleton_exact_title_index = {}
    daemon._skeleton_bootstrap_journal = None
    daemon.config = DaemonConfig(page_aggregation=False, title_arm=True)

    results = await daemon._search_via_backends(
        "atlas design doc", search_type="hybrid", limit=5, path_filter=None, zone_id="root"
    )
    atlas_rows = [r for r in results if r.path == "/designs/atlas.md"]
    assert len(atlas_rows) == 1  # one fused identity, not dense+title duplicates
    assert atlas_rows[0].chunk_index == 7  # borrowed the dense representative
    assert atlas_rows[0].title_score == pytest.approx(7.0)


@pytest.mark.asyncio
async def test_stopword_query_does_not_match_stopword_title() -> None:
    """Function-word overlap is not title evidence: 'how to configure
    authentication' must not surface an unrelated 'How To Guide' title
    (#4545 review round 6)."""
    daemon = _bare_locate_daemon()
    daemon.upsert_skeleton_doc(
        path_id="pg", virtual_path="/help/misc.md", title="How To Guide", zone_id="root"
    )
    assert await daemon.locate("how to configure authentication", zone_id="root") == []
    # Content-word overlap still works.
    hits = await daemon.locate("guide", zone_id="root")
    assert [h["path"] for h in hits] == ["/help/misc.md"]


@pytest.mark.asyncio
async def test_arm_gates_out_single_path_token_overlap() -> None:
    """A lone incidental path-token overlap (locate score 1.0) earns no
    fusion votes — TITLE_ARM_MIN_SCORE requires real title evidence
    (#4545 review round 6, closes the parked rank-1-bonus follow-up)."""
    from nexus.bricks.search.daemon import DaemonConfig

    daemon = _bare_locate_daemon()
    daemon.config = DaemonConfig()
    daemon.upsert_skeleton_doc(
        path_id="pd", virtual_path="/src/daemon/runner.py", title="Runner Notes", zone_id="root"
    )
    # locate itself still reports the weak path hit (endpoint semantics)...
    locate_hits = await daemon.locate("daemon shutdown", zone_id="root")
    assert [h["score"] for h in locate_hits] == [pytest.approx(1.0)]
    # ...but the fusion arm filters it out.
    timing: dict[str, float] = {}
    hits = await daemon._gather_title_hits(
        "daemon shutdown",
        zone_id="root",
        limit=5,
        path_filter=None,
        chunk_kw=[],
        page_kw=[],
        timing=timing,
    )
    assert hits == []


@pytest.mark.asyncio
async def test_locate_distinct_token_budget() -> None:
    """At most TITLE_ARM_MAX_QUERY_TOKENS DISTINCT tokens expand, counted
    per token rather than per field bucket (#4545 review round 6)."""
    daemon = _bare_locate_daemon()
    tokens = [f"tok{i:02}" for i in range(1, 14)]  # 13 distinct tokens
    for t in tokens:
        daemon.upsert_skeleton_doc(
            path_id=f"p-{t}", virtual_path=f"/y/{t}.md", title=None, zone_id="root"
        )
    hits = await daemon.locate(" ".join(tokens), zone_id="root", limit=20)
    # 12 most-selective (lexicographically first on DF ties) tokens expand.
    assert len(hits) == 12
    assert "/y/tok13.md" not in {h["path"] for h in hits}


@pytest.mark.asyncio
async def test_stopword_only_exact_title_still_found() -> None:
    """Stopword-only titles ("How To") stay reachable via the bounded
    exact-normalized-title fallback (#4545 review round 7) — while
    non-exact stopword queries still return nothing."""
    daemon = _bare_locate_daemon()
    daemon.upsert_skeleton_doc(
        path_id="ph", virtual_path="/docs/howto.md", title="How To", zone_id="root"
    )
    hits = await daemon.locate("how to", zone_id="root")
    assert [h["path"] for h in hits] == ["/docs/howto.md"]
    assert hits[0]["score"] == pytest.approx(4.0)  # 2 title tokens x 2.0
    # Non-exact stopword-only query: no evidence, no hits.
    assert await daemon.locate("how to the", zone_id="root") == []


@pytest.mark.asyncio
async def test_kill_switch_builds_no_index_and_scans_legacy() -> None:
    """title_arm=False builds no postings/token structures (memory parity
    with pre-#4545) and locate() serves via the legacy scan
    (#4545 review round 7)."""
    from nexus.bricks.search.daemon import DaemonConfig

    daemon = _bare_locate_daemon()
    daemon.config = DaemonConfig(title_arm=False)
    daemon.upsert_skeleton_doc(
        path_id="p1", virtual_path="/src/login.py", title="Login Module", zone_id="root"
    )
    assert daemon._skeleton_title_index == {}
    assert daemon._skeleton_path_index == {}
    assert daemon._skeleton_exact_title_index == {}
    entry = daemon._skeleton_docs[("root", "/src/login.py")]
    assert "title_token_set" not in entry and "path_token_set" not in entry
    # Legacy scan still serves the endpoint.
    hits = await daemon.locate("login", zone_id="root")
    assert [h["path"] for h in hits] == ["/src/login.py"]


@pytest.mark.asyncio
async def test_sandbox_hybrid_runs_title_arm() -> None:
    """SANDBOX-profile hybrid folds the title arm into its own fusion and
    preserves title_score in the shaped dicts (#4545 review round 7)."""
    from nexus.bricks.search.daemon import DaemonConfig, SearchDaemon, SearchResult
    from nexus.bricks.search.search_service import SearchService

    daemon: Any = SearchDaemon.__new__(SearchDaemon)
    daemon._skeleton_docs = {}
    daemon._skeleton_title_index = {}
    daemon._skeleton_path_index = {}
    daemon._skeleton_exact_title_index = {}
    daemon._skeleton_exact_title_index = {}
    daemon._skeleton_bootstrap_journal = None
    daemon._vector_backend = None
    daemon.config = DaemonConfig(title_arm=True)
    daemon.upsert_skeleton_doc(
        path_id="pa", virtual_path="/designs/atlas.md", title="Atlas Design Doc", zone_id="root"
    )

    async def _kw_search(self: Any, **kwargs: Any) -> list[SearchResult]:
        return [
            SearchResult(
                path="/a.md", chunk_text="a", score=10.0, chunk_index=0, search_type="keyword"
            )
        ]

    daemon.search = MethodType(lambda self, **kw: _kw_search(self, **kw), daemon)

    class FakeVecBackend:
        async def search(self, **kwargs: Any) -> list[Any]:
            return []

    svc: Any = SearchService.__new__(SearchService)
    svc._sqlite_vec_backend = FakeVecBackend()
    svc._search_daemon = daemon
    svc._enforce_permissions = False
    svc._permission_enforcer = None
    svc._record_store = None
    svc._sandbox_hybrid_no_vec_warned = False

    fused = await svc._hybrid_search_sandbox(
        query="atlas design doc", path="/", limit=5, context=None
    )
    assert fused is not None
    atlas = [r for r in fused if r.get("path") == "/designs/atlas.md"]
    assert atlas, f"title-only doc missing from sandbox hybrid: {fused}"
    assert atlas[0].get("title_score") == pytest.approx(7.0)


@pytest.mark.asyncio
async def test_sandbox_title_only_when_both_lanes_empty() -> None:
    """A chunkless title-only doc surfaces in SANDBOX hybrid even when the
    keyword AND vector lanes are both empty (#4545 review round 8)."""
    from nexus.bricks.search.daemon import DaemonConfig, SearchDaemon
    from nexus.bricks.search.search_service import SearchService

    daemon: Any = SearchDaemon.__new__(SearchDaemon)
    daemon._skeleton_docs = {}
    daemon._skeleton_title_index = {}
    daemon._skeleton_path_index = {}
    daemon._skeleton_exact_title_index = {}
    daemon._skeleton_bootstrap_journal = None
    daemon._vector_backend = None
    daemon.config = DaemonConfig(title_arm=True)
    daemon.upsert_skeleton_doc(
        path_id="pa", virtual_path="/designs/atlas.md", title="Atlas Design Doc", zone_id="root"
    )

    async def _kw_search(self: Any, **kwargs: Any) -> list[Any]:
        return []

    daemon.search = MethodType(lambda self, **kw: _kw_search(self, **kw), daemon)

    class FakeVecBackend:
        async def search(self, **kwargs: Any) -> list[Any]:
            return []

    svc: Any = SearchService.__new__(SearchService)
    svc._sqlite_vec_backend = FakeVecBackend()
    svc._search_daemon = daemon
    svc._enforce_permissions = False
    svc._permission_enforcer = None
    svc._record_store = None
    svc._sandbox_hybrid_no_vec_warned = False

    fused = await svc._hybrid_search_sandbox(
        query="atlas design doc", path="/", limit=5, context=None
    )
    assert fused is not None
    assert [r.get("path") for r in fused] == ["/designs/atlas.md"]
    assert fused[0].get("title_score") == pytest.approx(7.0)
    assert fused[0].get("semantic_degraded") is True  # vec lane was empty


@pytest.mark.asyncio
async def test_prefix_scoped_locate_survives_zone_wide_df() -> None:
    """A title token flooded past the DF cap zone-wide must still be
    selectable under a path_prefix that isolates one in-scope hit
    (#4545 review round 8)."""
    from nexus.bricks.search.daemon import TITLE_ARM_MAX_TOKEN_DF

    daemon = _bare_locate_daemon()
    for i in range(TITLE_ARM_MAX_TOKEN_DF + 1):
        daemon.upsert_skeleton_doc(
            path_id=f"p{i}",
            virtual_path=f"/elsewhere/d{i:05}/x.md",
            title="Readme",
            zone_id="root",
        )
    daemon.upsert_skeleton_doc(
        path_id="ps", virtual_path="/scope/overview.md", title="Readme", zone_id="root"
    )

    # Unscoped: bucket over DF cap, skipped (non-discriminative) — unchanged.
    assert await daemon.locate("readme", zone_id="root", limit=5) == []
    # Prefix-scoped: in-prefix filter rescues the single in-scope hit.
    hits = await daemon.locate("readme", zone_id="root", limit=5, path_prefix="/scope/")
    assert [h["path"] for h in hits] == ["/scope/overview.md"]


@pytest.mark.asyncio
async def test_duplicate_tokens_score_distinct_overlap() -> None:
    """Duplicate tokens in titles/paths count once: exact 'Foo Bar'
    outranks 'Foo Foo Foo' for query 'foo bar' (#4545 review round 8)."""
    daemon = _bare_locate_daemon()
    daemon.upsert_skeleton_doc(
        path_id="p1", virtual_path="/a/spam.md", title="Foo Foo Foo", zone_id="root"
    )
    daemon.upsert_skeleton_doc(
        path_id="p2", virtual_path="/a/real.md", title="Foo Bar", zone_id="root"
    )
    hits = await daemon.locate("foo bar", zone_id="root", limit=5)
    assert [h["path"] for h in hits][0] == "/a/real.md"
    by_path = {h["path"]: h["score"] for h in hits}
    assert by_path["/a/real.md"] == pytest.approx(4.0)
    assert by_path["/a/spam.md"] == pytest.approx(2.0)  # one distinct match


@pytest.mark.asyncio
async def test_exact_title_fallback_is_df_capped() -> None:
    """A stopword-only exact title shared by more docs than the DF cap is
    non-discriminative and returns nothing instead of sorting a huge
    bucket on the owner loop (#4545 review round 9)."""
    import nexus.bricks.search.daemon as daemon_mod

    daemon = _bare_locate_daemon()
    cap = 8
    orig = daemon_mod.TITLE_ARM_MAX_TOKEN_DF
    daemon_mod.TITLE_ARM_MAX_TOKEN_DF = cap
    try:
        for i in range(cap + 1):
            daemon.upsert_skeleton_doc(
                path_id=f"p{i}", virtual_path=f"/n/{i:03}.md", title="How To", zone_id="root"
            )
        assert await daemon.locate("how to", zone_id="root", limit=5) == []
        # Under the cap the fallback works.
        daemon2 = _bare_locate_daemon()
        daemon2.upsert_skeleton_doc(
            path_id="p1", virtual_path="/n/one.md", title="How To", zone_id="root"
        )
        hits = await daemon2.locate("how to", zone_id="root", limit=5)
        assert [h["path"] for h in hits] == ["/n/one.md"]
    finally:
        daemon_mod.TITLE_ARM_MAX_TOKEN_DF = orig


@pytest.mark.asyncio
async def test_prefix_rescue_is_all_or_nothing(monkeypatch: Any) -> None:
    """Oversized buckets are prefix-scanned in FULL or skipped entirely —
    never partially sampled from unordered sets (#4545 review round 9)."""
    import nexus.bricks.search.daemon as daemon_mod

    daemon = _bare_locate_daemon()
    monkeypatch.setattr(daemon_mod, "TITLE_ARM_MAX_TOKEN_DF", 4)
    for i in range(5):
        daemon.upsert_skeleton_doc(
            path_id=f"p{i}", virtual_path=f"/elsewhere/{i}.md", title="Readme", zone_id="root"
        )
    daemon.upsert_skeleton_doc(
        path_id="ps", virtual_path="/scope/overview.md", title="Readme", zone_id="root"
    )
    # Bucket (6) over DF cap (4): with sufficient budget, full-scan rescue.
    hits = await daemon.locate("readme", zone_id="root", limit=5, path_prefix="/scope/")
    assert [h["path"] for h in hits] == ["/scope/overview.md"]
    # With a budget smaller than the bucket, the bucket is skipped whole.
    monkeypatch.setattr(daemon_mod, "TITLE_ARM_MAX_PREFIX_SCAN", 3)
    assert await daemon.locate("readme", zone_id="root", limit=5, path_prefix="/scope/") == []


@pytest.mark.asyncio
async def test_sandbox_no_vec_backend_still_serves_title_only() -> None:
    """SANDBOX with no vector backend (NEXUS_DISABLE_VECTOR_SEARCH) still
    surfaces a title-only doc; without title evidence the legacy None
    contract is preserved (#4545 review round 9)."""
    from nexus.bricks.search.daemon import DaemonConfig, SearchDaemon
    from nexus.bricks.search.search_service import SearchService

    daemon: Any = SearchDaemon.__new__(SearchDaemon)
    daemon._skeleton_docs = {}
    daemon._skeleton_title_index = {}
    daemon._skeleton_path_index = {}
    daemon._skeleton_exact_title_index = {}
    daemon._skeleton_bootstrap_journal = None
    daemon._vector_backend = None
    daemon.config = DaemonConfig(title_arm=True)
    daemon.upsert_skeleton_doc(
        path_id="pa", virtual_path="/designs/atlas.md", title="Atlas Design Doc", zone_id="root"
    )

    async def _kw_search(self: Any, **kwargs: Any) -> list[Any]:
        return []

    daemon.search = MethodType(lambda self, **kw: _kw_search(self, **kw), daemon)

    svc: Any = SearchService.__new__(SearchService)
    svc._sqlite_vec_backend = None
    svc._search_daemon = daemon
    svc._enforce_permissions = False
    svc._permission_enforcer = None
    svc._record_store = None
    svc._sandbox_hybrid_no_vec_warned = True

    fused = await svc._hybrid_search_sandbox(
        query="atlas design doc", path="/", limit=5, context=None
    )
    assert fused is not None
    assert [r.get("path") for r in fused] == ["/designs/atlas.md"]
    assert fused[0].get("semantic_degraded") is True

    # No title evidence -> legacy None contract preserved.
    fused2 = await svc._hybrid_search_sandbox(
        query="unrelated nonsense", path="/", limit=5, context=None
    )
    assert fused2 is None


@pytest.mark.asyncio
async def test_hydrate_borrows_from_dict_shaped_rows() -> None:
    """SANDBOX keyword-lane rows are shaped dicts — hydration borrows from
    them and aligns the title vote on the BM25 chunk identity
    (#4545 review round 10)."""
    daemon, calls = _hydration_daemon()
    kw_dicts = [
        {"path": "/t.md", "chunk_text": "body chunk", "score": 9.0, "chunk_index": 3},
    ]
    hits = await daemon._hydrate_title_hits(
        [{"path": "/t.md", "score": 6.0, "title": "T"}],
        chunk_kw=kw_dicts,
        page_kw=[],
        zone_id="root",
    )
    assert calls == []
    assert hits[0].chunk_index == 3
    assert hits[0].chunk_text == "body chunk"


@pytest.mark.asyncio
async def test_prefix_rescue_budget_stable_order(monkeypatch: Any) -> None:
    """When multiple oversized buckets compete for the scan budget, the
    SMALLEST bucket wins deterministically regardless of token hash order
    (#4545 review round 10)."""
    import nexus.bricks.search.daemon as daemon_mod

    daemon = _bare_locate_daemon()
    monkeypatch.setattr(daemon_mod, "TITLE_ARM_MAX_TOKEN_DF", 2)
    # Token "zzz" bucket: 3 docs (one in-prefix). Token "aaa" bucket: 4 docs
    # (one in-prefix). Budget fits only one bucket: the smaller ("zzz")
    # must win even though "aaa" sorts first alphabetically.
    for i in range(2):
        daemon.upsert_skeleton_doc(
            path_id=f"z{i}", virtual_path=f"/other/zzz-{i}.md", title=None, zone_id="root"
        )
    daemon.upsert_skeleton_doc(
        path_id="zs", virtual_path="/scope/zzz-target.md", title=None, zone_id="root"
    )
    for i in range(3):
        daemon.upsert_skeleton_doc(
            path_id=f"a{i}", virtual_path=f"/other/aaa-{i}.md", title=None, zone_id="root"
        )
    daemon.upsert_skeleton_doc(
        path_id="as", virtual_path="/scope/aaa-target.md", title=None, zone_id="root"
    )
    monkeypatch.setattr(daemon_mod, "TITLE_ARM_MAX_PREFIX_SCAN", 3)
    hits = await daemon.locate("aaa zzz", zone_id="root", limit=5, path_prefix="/scope/")
    assert [h["path"] for h in hits] == ["/scope/zzz-target.md"]


@pytest.mark.asyncio
async def test_sandbox_title_arm_uses_overfetch_depth() -> None:
    """With permission enforcement active, the SANDBOX title arm receives
    the per-lane over-fetch limit, not the caller limit (#4545 review
    round 10)."""
    from nexus.bricks.search.daemon import DaemonConfig, SearchDaemon
    from nexus.bricks.search.search_service import SearchService

    daemon: Any = SearchDaemon.__new__(SearchDaemon)
    daemon.config = DaemonConfig(title_arm=True)
    seen: dict[str, Any] = {}

    async def _gather(self: Any, query: str, **kwargs: Any) -> list[Any]:
        seen.update(kwargs)
        return []

    daemon._gather_title_hits = MethodType(_gather, daemon)

    async def _kw_search(self: Any, **kwargs: Any) -> list[Any]:
        return []

    daemon.search = MethodType(lambda self, **kw: _kw_search(self, **kw), daemon)

    class FakeVecBackend:
        async def search(self, **kwargs: Any) -> list[Any]:
            return []

    class FakeEnforcer:
        pass

    svc: Any = SearchService.__new__(SearchService)
    svc._sqlite_vec_backend = FakeVecBackend()
    svc._search_daemon = daemon
    svc._enforce_permissions = True
    svc._permission_enforcer = FakeEnforcer()
    svc._record_store = None
    svc._sandbox_hybrid_no_vec_warned = True

    await svc._hybrid_search_sandbox(query="anything", path="/", limit=2, context=None)
    assert seen["limit"] == 10  # limit * 5 under enforcement
