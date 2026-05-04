"""Tests for SANDBOX hybrid RRF (FULL-profile parity).

Validates ``SearchService._hybrid_search_sandbox`` — the lane-fuse path
that runs the local sqlite-vec backend and the daemon's BM25S keyword
backend in parallel, then combines them via Reciprocal Rank Fusion.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest

from nexus.bricks.search.results import BaseSearchResult
from nexus.bricks.search.search_service import SearchService

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass
class _DaemonRow:
    """Mimics the SearchResult shape the daemon returns to SearchService."""

    path: str
    chunk_text: str = ""
    score: float = 0.0
    chunk_index: int = 0
    start_offset: int = 0
    end_offset: int = 0
    line_start: int = 0
    line_end: int = 0
    context: Any = None


class _FakeVecBackend:
    """Minimal stand-in for ``SqliteVecBackend`` used by the hybrid path."""

    def __init__(self, results: list[BaseSearchResult] | Exception | None = None) -> None:
        self._results = results
        self.calls: list[dict[str, Any]] = []

    async def search(
        self,
        *,
        query: str,
        limit: int,
        zone_id: str,
        search_type: str = "hybrid",
        path_filter: str | None = None,
    ) -> list[BaseSearchResult]:
        self.calls.append(
            {
                "query": query,
                "limit": limit,
                "zone_id": zone_id,
                "path_filter": path_filter,
            }
        )
        if isinstance(self._results, Exception):
            raise self._results
        return list(self._results or [])


class _FakeDaemon:
    """Mimics ``SearchDaemon.search(search_type='keyword', ...)``."""

    def __init__(self, rows: list[_DaemonRow] | Exception | None = None) -> None:
        # Mimics daemon's ``self._backend`` attribute the hybrid path
        # checks before invoking the keyword lane.
        self._backend = object() if rows is not None else None
        self._rows = rows
        self.calls: list[dict[str, Any]] = []

    async def search(
        self,
        *,
        query: str,
        search_type: str,
        limit: int,
        path_filter: str | None,
        zone_id: str | None,
    ) -> list[_DaemonRow]:
        self.calls.append(
            {
                "query": query,
                "search_type": search_type,
                "limit": limit,
                "path_filter": path_filter,
                "zone_id": zone_id,
            }
        )
        if isinstance(self._rows, Exception):
            raise self._rows
        return list(self._rows or [])


def _vec_hit(path: str, score: float = 0.9, chunk_index: int = 0) -> BaseSearchResult:
    return BaseSearchResult(
        path=path,
        chunk_text=f"vec hit {path}",
        score=score,
        chunk_index=chunk_index,
        vector_score=score,
    )


def _kw_row(path: str, score: float = 0.8, chunk_index: int = 0) -> _DaemonRow:
    return _DaemonRow(
        path=path,
        chunk_text=f"bm25 hit {path}",
        score=score,
        chunk_index=chunk_index,
    )


def _make_sandbox_service(
    *,
    vec_backend: _FakeVecBackend | None = None,
    daemon: _FakeDaemon | None = None,
) -> SearchService:
    metadata = MagicMock()
    svc = SearchService(
        metadata_store=metadata,
        enforce_permissions=False,
        deployment_profile="sandbox",
        sqlite_vec_backend=vec_backend,
    )
    if daemon is not None:
        # SearchService reads ``self._search_daemon`` via getattr; setattr
        # mirrors the production wiring without tripping mypy on a name
        # the class never declares.
        setattr(svc, "_search_daemon", daemon)  # noqa: B010
    return svc


# ---------------------------------------------------------------------------
# Hybrid RRF
# ---------------------------------------------------------------------------


class TestHybridRRF:
    @pytest.mark.asyncio
    async def test_both_lanes_run_and_fuse(self) -> None:
        """Both lanes return distinct hits → fused result contains all of them."""
        vec = _FakeVecBackend([_vec_hit("/v1.md"), _vec_hit("/shared.md", 0.7)])
        kw = _FakeDaemon([_kw_row("/k1.md"), _kw_row("/shared.md", 0.6)])
        svc = _make_sandbox_service(vec_backend=vec, daemon=kw)

        results = await svc._hybrid_search_sandbox(query="q", path="/", limit=10, context=None)

        assert results is not None
        # Both lanes were actually invoked exactly once.
        assert len(vec.calls) == 1
        assert len(kw.calls) == 1
        # Shared doc must appear once (RRF dedup) with both lane scores stamped.
        paths = [r["path"] for r in results]
        assert "/v1.md" in paths
        assert "/k1.md" in paths
        assert paths.count("/shared.md") == 1
        shared = next(r for r in results if r["path"] == "/shared.md")
        assert "keyword_score" in shared and "vector_score" in shared

    @pytest.mark.asyncio
    async def test_no_vec_backend_returns_none(self) -> None:
        """Without a vec backend the helper bows out so the caller falls
        through to the semantic-only / degraded chain."""
        svc = _make_sandbox_service(vec_backend=None, daemon=_FakeDaemon([_kw_row("/x")]))
        out = await svc._hybrid_search_sandbox(query="q", path="/", limit=5, context=None)
        assert out is None

    @pytest.mark.asyncio
    async def test_keyword_lane_absent_falls_back_to_vec_only(self) -> None:
        """No daemon wired → fusion still runs with only the vec lane."""
        vec = _FakeVecBackend([_vec_hit("/only-vec.md")])
        svc = _make_sandbox_service(vec_backend=vec, daemon=None)

        results = await svc._hybrid_search_sandbox(query="q", path="/", limit=5, context=None)

        assert results is not None
        assert [r["path"] for r in results] == ["/only-vec.md"]

    @pytest.mark.asyncio
    async def test_vec_lane_error_falls_back_to_kw_only(self) -> None:
        """Vec backend raising must not abort fusion — keyword lane carries it."""
        vec = _FakeVecBackend(RuntimeError("sqlite-vec exploded"))
        kw = _FakeDaemon([_kw_row("/kept.md")])
        svc = _make_sandbox_service(vec_backend=vec, daemon=kw)

        results = await svc._hybrid_search_sandbox(query="q", path="/", limit=5, context=None)

        assert results is not None
        assert [r["path"] for r in results] == ["/kept.md"]

    @pytest.mark.asyncio
    async def test_both_lanes_empty_returns_none(self) -> None:
        """Empty + empty → None so the caller can try the fallback chain."""
        vec = _FakeVecBackend([])
        kw = _FakeDaemon([])
        svc = _make_sandbox_service(vec_backend=vec, daemon=kw)

        results = await svc._hybrid_search_sandbox(query="q", path="/", limit=5, context=None)

        assert results is None

    @pytest.mark.asyncio
    async def test_hybrid_does_not_set_semantic_degraded(self) -> None:
        """Real RRF hybrid is a real semantic match — no degraded marker."""
        from nexus.bricks.search.search_service import LAST_SEMANTIC_DEGRADED

        vec = _FakeVecBackend([_vec_hit("/a.md")])
        kw = _FakeDaemon([_kw_row("/b.md")])
        svc = _make_sandbox_service(vec_backend=vec, daemon=kw)

        # Reset to a known truthy state so we can prove the helper does
        # not set it on the success path.
        LAST_SEMANTIC_DEGRADED.set(True)
        results = await svc._semantic_search_sandbox(
            query="q", path="/", limit=5, context=None, search_mode="hybrid"
        )
        assert results, "expected fused hits"
        for r in results:
            # BaseSearchResult dataclass has the field with default None,
            # which fusion's _to_dict surfaces — what matters is that the
            # hybrid path never marks it True.
            assert not r.get("semantic_degraded"), (
                f"hybrid path should not mark results degraded: {r['path']}"
            )

    @pytest.mark.asyncio
    async def test_hybrid_dispatches_through_semantic_search_sandbox(self) -> None:
        """``_semantic_search_sandbox(search_mode='hybrid')`` must reach the
        fused path when the vec backend is wired (rather than falling
        through to the semantic-only sqlite_vec lane)."""
        vec = _FakeVecBackend([_vec_hit("/v.md")])
        kw = _FakeDaemon([_kw_row("/k.md")])
        svc = _make_sandbox_service(vec_backend=vec, daemon=kw)

        results = await svc._semantic_search_sandbox(
            query="q", path="/", limit=10, context=None, search_mode="hybrid"
        )

        # Both lanes invoked exactly once — confirms hybrid dispatch fired.
        assert len(vec.calls) == 1
        assert len(kw.calls) == 1
        paths = {r["path"] for r in results}
        assert paths == {"/v.md", "/k.md"}


class TestSandboxHybridDefault:
    """SANDBOX upgrades the default 'semantic' mode to 'hybrid' when a
    local vector backend is wired, so users get fusion without having to
    pass the keyword."""

    @pytest.mark.asyncio
    async def test_default_semantic_upgrades_to_hybrid_when_vec_wired(self) -> None:
        vec = _FakeVecBackend([_vec_hit("/v.md")])
        kw = _FakeDaemon([_kw_row("/k.md")])
        svc = _make_sandbox_service(vec_backend=vec, daemon=kw)

        # Caller asks for the default 'semantic' mode; SANDBOX must
        # silently route to the hybrid path so both lanes fire.
        results = await svc._semantic_search_impl(
            query="q", path="/", limit=10, context=None, search_mode="semantic"
        )

        assert len(vec.calls) == 1, "vec lane must be invoked under hybrid upgrade"
        assert len(kw.calls) == 1, "keyword lane must be invoked under hybrid upgrade"
        paths = {r["path"] for r in results}
        assert paths == {"/v.md", "/k.md"}

    @pytest.mark.asyncio
    async def test_default_semantic_stays_semantic_without_vec_backend(self) -> None:
        """No vec backend wired (e.g. user opted out via
        NEXUS_DISABLE_VECTOR_SEARCH or fastembed missing) → upgrade
        does NOT fire, the keyword-only fallback chain handles it."""
        kw = _FakeDaemon([_kw_row("/k.md")])
        svc = _make_sandbox_service(vec_backend=None, daemon=kw)

        # Should still return *something* via the BM25S degraded chain,
        # but the vec lane must not be invented when no backend exists.
        await svc._semantic_search_impl(
            query="q", path="/", limit=10, context=None, search_mode="semantic"
        )
        # (No vec backend to assert calls on; the absence of an exception
        # plus the keyword lane firing is the contract.)
        assert kw.calls, "keyword lane should still fire on the fallback chain"


class TestSandboxHybridNoVecWarning:
    """Missing vec backend during a hybrid request must warn once and
    let the caller fall back to keyword-only — never raise."""

    @pytest.mark.asyncio
    async def test_warns_once_then_silent(self, caplog: pytest.LogCaptureFixture) -> None:
        # Vec absent, daemon present — caller passes hybrid explicitly.
        kw = _FakeDaemon([_kw_row("/k.md")])
        svc = _make_sandbox_service(vec_backend=None, daemon=kw)

        import logging

        caplog.set_level(logging.WARNING, logger="nexus.bricks.search.search_service")

        # First call: WARNING.
        out1 = await svc._hybrid_search_sandbox(query="q", path="/", limit=5, context=None)
        # Second call: must not warn again (DEBUG only).
        out2 = await svc._hybrid_search_sandbox(query="q", path="/", limit=5, context=None)

        assert out1 is None and out2 is None
        warns = [
            r
            for r in caplog.records
            if r.levelname == "WARNING" and "SANDBOX hybrid" in r.getMessage()
        ]
        assert len(warns) == 1, (
            f"expected exactly one WARNING about hybrid degradation, got {len(warns)}"
        )

    @pytest.mark.asyncio
    async def test_warning_fires_via_public_search_mode_hybrid(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Codex review (medium): the warn-once helper must be reachable
        through the public `_semantic_search_impl(search_mode='hybrid')`
        entry point, not just the private helper. Previously the outer
        gate (``self._sqlite_vec_backend is not None``) skipped the
        helper entirely when no backend was wired, so users on the
        public path never saw the install-hint warning."""
        import logging

        kw = _FakeDaemon([_kw_row("/k.md")])
        svc = _make_sandbox_service(vec_backend=None, daemon=kw)
        caplog.set_level(logging.WARNING, logger="nexus.bricks.search.search_service")

        await svc._semantic_search_impl(
            query="q", path="/", limit=5, context=None, search_mode="hybrid"
        )

        warns = [
            r
            for r in caplog.records
            if r.levelname == "WARNING" and "SANDBOX hybrid" in r.getMessage()
        ]
        assert len(warns) == 1, (
            "public search_mode='hybrid' with no vec backend must surface the "
            "one-shot install-hint warning"
        )


class TestHybridDegradedMarker:
    """Codex review (high): when ``_hybrid_search_sandbox`` returns
    keyword-only fused results because the vector lane errored or was
    empty, the result MUST carry ``semantic_degraded=True`` so envelope
    builders can warn callers that the answer isn't really semantic."""

    @pytest.mark.asyncio
    async def test_vec_lane_error_stamps_degraded_on_kw_only_results(self) -> None:
        from nexus.bricks.search.search_service import LAST_SEMANTIC_DEGRADED

        vec = _FakeVecBackend(RuntimeError("embedder unreachable"))
        kw = _FakeDaemon([_kw_row("/kept.md")])
        svc = _make_sandbox_service(vec_backend=vec, daemon=kw)

        LAST_SEMANTIC_DEGRADED.set(False)
        results = await svc._hybrid_search_sandbox(query="q", path="/", limit=5, context=None)

        assert results, "expected fallback fused results"
        for r in results:
            assert r.get("semantic_degraded") is True, (
                "vec lane errored → caller is on keyword-only; every result must be marked degraded"
            )
        assert LAST_SEMANTIC_DEGRADED.get() is True, (
            "LAST_SEMANTIC_DEGRADED contextvar must mirror the per-result flag"
        )

    @pytest.mark.asyncio
    async def test_vec_lane_empty_stamps_degraded_on_kw_only_results(self) -> None:
        """Vec returning [] (no embedder, dim mismatch, empty index) is
        the same effective situation as an error from the caller's
        perspective: no semantic match contributed."""
        vec = _FakeVecBackend([])
        kw = _FakeDaemon([_kw_row("/kept.md")])
        svc = _make_sandbox_service(vec_backend=vec, daemon=kw)

        results = await svc._hybrid_search_sandbox(query="q", path="/", limit=5, context=None)

        assert results, "expected fused results from kw lane"
        for r in results:
            assert r.get("semantic_degraded") is True

    @pytest.mark.asyncio
    async def test_both_lanes_succeed_does_not_stamp_degraded(self) -> None:
        """Sanity: real fused results must NOT carry the degraded marker
        — that would be a regression of the parity claim."""
        vec = _FakeVecBackend([_vec_hit("/v.md")])
        kw = _FakeDaemon([_kw_row("/k.md")])
        svc = _make_sandbox_service(vec_backend=vec, daemon=kw)

        results = await svc._hybrid_search_sandbox(query="q", path="/", limit=5, context=None)

        assert results
        for r in results:
            assert not r.get("semantic_degraded"), (
                f"real hybrid hit must not be marked degraded: {r['path']}"
            )

    @pytest.mark.asyncio
    async def test_post_permission_filter_strips_all_vec_hits_marks_degraded(self) -> None:
        """Codex review R2 (high): when the permission filter strips
        every row that originated in the vec lane (e.g. user has access
        to keyword-matched docs but not to the semantically nearest
        ones), the surviving results are effectively keyword-only and
        MUST be marked ``semantic_degraded=True``. Without the post-
        filter recompute, the contract silently degrades from "real
        semantic match" to "BM25 dressed up as semantic"."""
        from nexus.bricks.search.search_service import LAST_SEMANTIC_DEGRADED

        # Vec returns two hits; keyword returns a different doc. A
        # permission_enforcer with allowlist={"/k.md"} will strip BOTH
        # vec hits, leaving only the kw hit in fused.
        vec = _FakeVecBackend([_vec_hit("/secret-v1.md"), _vec_hit("/secret-v2.md")])
        kw = _FakeDaemon([_kw_row("/k.md")])

        class _AllowlistEnforcer:
            def __init__(self, allowed: set[str]) -> None:
                self._allowed = allowed
                self.rebac_manager = None

            def filter_list(self, paths: list[str], _ctx: object) -> list[str]:
                return [p for p in paths if p in self._allowed]

        metadata = MagicMock()
        svc = SearchService(
            metadata_store=metadata,
            permission_enforcer=_AllowlistEnforcer({"/k.md"}),
            enforce_permissions=True,
            deployment_profile="sandbox",
            sqlite_vec_backend=vec,
        )
        setattr(svc, "_search_daemon", kw)  # noqa: B010

        # A non-None context is required for the permission filter branch.
        ctx = MagicMock()
        LAST_SEMANTIC_DEGRADED.set(False)
        results = await svc._hybrid_search_sandbox(query="q", path="/", limit=5, context=ctx)

        assert results, "expected the keyword-lane survivor to come through"
        assert {r["path"] for r in results} == {"/k.md"}, (
            "vec hits must be filtered out by the allowlist enforcer"
        )
        for r in results:
            assert r.get("semantic_degraded") is True, (
                "vec hits stripped by permissions → effective keyword-only; "
                "every surviving row must be marked degraded"
            )
        assert LAST_SEMANTIC_DEGRADED.get() is True, (
            "LAST_SEMANTIC_DEGRADED contextvar must mirror the per-result flag"
        )
