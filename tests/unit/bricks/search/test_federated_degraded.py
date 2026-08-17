"""Tests for federation-unreachable detection (Issue #3778)."""

import logging
from unittest.mock import MagicMock

import pytest

from nexus.bricks.search.search_degraded import (
    FederatedSearchResponse,
    FederationUnreachableError,
    ZoneFailure,
    is_all_peers_failed,
)
from nexus.bricks.search.results import BaseSearchResult
from nexus.bricks.search.search_service import SearchService


class TestFederationUnreachableDetection:
    def test_error_class_exists(self) -> None:
        err = FederationUnreachableError("all peers down")
        assert isinstance(err, Exception)
        assert str(err) == "all peers down"

    def test_response_with_all_failures_is_unreachable(self) -> None:
        resp = FederatedSearchResponse(
            results=[],
            zones_searched=["a", "b"],
            zones_failed=[
                ZoneFailure(zone_id="a", error="timeout"),
                ZoneFailure(zone_id="b", error="connection refused"),
            ],
        )
        assert is_all_peers_failed(resp) is True

    def test_response_with_partial_failure_is_not_unreachable(self) -> None:
        resp = FederatedSearchResponse(
            results=[{"path": "/x", "score": 1.0}],
            zones_searched=["a", "b"],
            zones_failed=[ZoneFailure(zone_id="b", error="timeout")],
        )
        assert is_all_peers_failed(resp) is False

    def test_response_with_zero_peers_is_unreachable(self) -> None:
        resp = FederatedSearchResponse(
            results=[],
            zones_searched=[],
            zones_failed=[],
        )
        assert is_all_peers_failed(resp) is True


def _make_sandbox_service() -> SearchService:
    """Build a minimal SearchService in SANDBOX profile for fallback tests."""
    metadata = MagicMock()
    return SearchService(
        metadata_store=metadata,
        enforce_permissions=False,
        deployment_profile="sandbox",
    )


def _make_full_service() -> SearchService:
    metadata = MagicMock()
    return SearchService(
        metadata_store=metadata,
        enforce_permissions=False,
        deployment_profile="full",
    )


def _bm25_result(path: str, score: float) -> BaseSearchResult:
    return BaseSearchResult(
        path=path,
        chunk_text=f"hit for {path}",
        score=score,
    )


class TestSearchServiceSandboxFallback:
    """Issue #3778 — SANDBOX profile BM25S fallback when federation is unreachable."""

    @pytest.mark.asyncio
    async def test_all_peers_fail_triggers_bm25_degraded(self) -> None:
        """All peers failed → fall back to BM25S + stamp semantic_degraded=True."""
        svc = _make_sandbox_service()
        bm25_called = {"count": 0}

        async def fed_call() -> FederatedSearchResponse:
            return FederatedSearchResponse(
                results=[],
                zones_searched=["a", "b"],
                zones_failed=[
                    ZoneFailure(zone_id="a", error="timeout"),
                    ZoneFailure(zone_id="b", error="conn refused"),
                ],
            )

        async def bm25_call() -> list[BaseSearchResult]:
            bm25_called["count"] += 1
            return [_bm25_result("/x/a.py", 0.9), _bm25_result("/x/b.py", 0.7)]

        results = await svc._semantic_with_sandbox_fallback(fed_call, bm25_call)

        assert bm25_called["count"] == 1
        assert len(results) == 2
        assert all(isinstance(r, BaseSearchResult) for r in results)
        assert all(r.semantic_degraded is True for r in results)

    @pytest.mark.asyncio
    async def test_partial_peer_success_no_degraded_flag(self) -> None:
        """Partial success → return federation results unchanged, no BM25S call."""
        svc = _make_sandbox_service()
        bm25_called = {"count": 0}

        async def fed_call() -> FederatedSearchResponse:
            return FederatedSearchResponse(
                results=[{"path": "/hit", "score": 1.0}],
                zones_searched=["a", "b"],
                zones_failed=[ZoneFailure(zone_id="b", error="timeout")],
            )

        async def bm25_call() -> list[BaseSearchResult]:
            bm25_called["count"] += 1
            return []

        results = await svc._semantic_with_sandbox_fallback(fed_call, bm25_call)

        assert bm25_called["count"] == 0
        assert results == [{"path": "/hit", "score": 1.0}]
        # Dict entries have no semantic_degraded attribute — confirm raw passthrough.
        assert "semantic_degraded" not in results[0]

    @pytest.mark.asyncio
    async def test_warn_only_once_per_session(self, caplog: pytest.LogCaptureFixture) -> None:
        """Three back-to-back all-peers-failed calls → exactly one WARNING."""
        svc = _make_sandbox_service()

        async def fed_call() -> FederatedSearchResponse:
            return FederatedSearchResponse(
                results=[],
                zones_searched=["a"],
                zones_failed=[ZoneFailure(zone_id="a", error="down")],
            )

        async def bm25_call() -> list[BaseSearchResult]:
            return [_bm25_result("/p", 0.5)]

        caplog.set_level(logging.DEBUG, logger="nexus.bricks.search.search_service")

        for _ in range(3):
            out = await svc._semantic_with_sandbox_fallback(fed_call, bm25_call)
            assert len(out) == 1
            assert out[0].semantic_degraded is True

        warn_records = [
            rec
            for rec in caplog.records
            if rec.levelno == logging.WARNING
            and rec.name == "nexus.bricks.search.search_service"
            and "SANDBOX" in rec.getMessage()
        ]
        assert len(warn_records) == 1, (
            f"expected exactly 1 WARNING, got {len(warn_records)}: "
            f"{[r.getMessage() for r in warn_records]}"
        )
        assert svc._sandbox_fallback_warned is True

    @pytest.mark.asyncio
    async def test_non_sandbox_profile_skips_fallback(self) -> None:
        """When profile != sandbox, federation results pass through untouched."""
        svc = _make_full_service()
        bm25_called = {"count": 0}

        async def fed_call() -> FederatedSearchResponse:
            # Even "all peers failed" should not trigger fallback for FULL.
            return FederatedSearchResponse(
                results=[],
                zones_searched=["a"],
                zones_failed=[ZoneFailure(zone_id="a", error="down")],
            )

        async def bm25_call() -> list[BaseSearchResult]:
            bm25_called["count"] += 1
            return []

        results = await svc._semantic_with_sandbox_fallback(fed_call, bm25_call)

        assert bm25_called["count"] == 0
        assert results == []
        assert svc._sandbox_fallback_warned is False

    def test_warn_flag_is_instance_scoped(self) -> None:
        """Flag lives on the instance, not on the class/module."""
        a = _make_sandbox_service()
        b = _make_sandbox_service()
        a._sandbox_fallback_warned = True
        assert b._sandbox_fallback_warned is False


class TestSandboxInnerFederationZoneScope:
    """Issue #4542 round-7 review: the SANDBOX semantic fallback re-enters the
    federation dispatcher — it must carry the context's zone allow-list, or a
    scoped token whose allowed zone is down could receive results from any
    zone its subject can reach."""

    def _svc_with_dispatcher(self, captured: dict) -> SearchService:
        svc = _make_sandbox_service()

        async def disp_search(**kwargs):
            captured.update(kwargs)
            return FederatedSearchResponse(
                results=[
                    {
                        "path": "/doc.md",
                        "zone_id": "eng",
                        "score": 0.9,
                        "chunk_text": "x",
                        "chunk_index": 0,
                    }
                ],
                zones_searched=["eng"],
                zones_failed=[],
            )

        dispatcher = MagicMock()
        dispatcher.search = disp_search
        svc._federation_dispatcher = dispatcher
        return svc

    @pytest.mark.asyncio
    async def test_scoped_context_forwards_zone_filter(self) -> None:
        from nexus.contracts.types import OperationContext

        captured: dict = {}
        svc = self._svc_with_dispatcher(captured)
        ctx = OperationContext(user_id="alice", groups=[], zone_id="eng")

        await svc._semantic_search_sandbox(
            query="q", path="/", limit=5, context=ctx, search_mode="semantic"
        )

        assert captured["zone_filter"] == frozenset({"eng"})

    @pytest.mark.asyncio
    async def test_admin_context_keeps_unbounded_federation(self) -> None:
        from nexus.contracts.types import OperationContext

        captured: dict = {}
        svc = self._svc_with_dispatcher(captured)
        ctx = OperationContext(user_id="root", groups=[], zone_id="eng", is_admin=True)

        await svc._semantic_search_sandbox(
            query="q", path="/", limit=5, context=ctx, search_mode="semantic"
        )

        assert captured["zone_filter"] is None


class TestReadableZoneFilter:
    """Issue #4542 round-8 review: write-only zone grants are not searchable."""

    def test_write_only_grant_fails_closed(self) -> None:
        from nexus.bricks.search.search_auth import readable_zone_filter

        assert readable_zone_filter(("eng",), (("eng", "w"),)) == frozenset()

    def test_mixed_grants_keep_readable_only(self) -> None:
        from nexus.bricks.search.search_auth import readable_zone_filter

        out = readable_zone_filter(
            ("eng", "legal", "ops"),
            (("eng", "r"), ("legal", "w"), ("ops", "rwx")),
        )
        assert out == frozenset({"eng", "ops"})

    def test_zone_set_without_perms_kept_whole(self) -> None:
        from nexus.bricks.search.search_auth import readable_zone_filter

        assert readable_zone_filter(["eng", "legal"], None) == frozenset({"eng", "legal"})

    def test_no_grants_means_unbounded(self) -> None:
        from nexus.bricks.search.search_auth import readable_zone_filter

        assert readable_zone_filter((), ()) is None

    @pytest.mark.asyncio
    async def test_write_only_context_fails_closed_in_sandbox_dispatch(self) -> None:
        """Round-9 strengthened round-8: a write-only context now fails
        closed at SANDBOX entry — the dispatcher is never reached."""
        from nexus.contracts.types import OperationContext

        captured: dict = {}
        svc = TestSandboxInnerFederationZoneScope()._svc_with_dispatcher(captured)
        ctx = OperationContext(
            user_id="alice", groups=[], zone_id="eng", zone_perms=(("eng", "w"),)
        )

        out = await svc._semantic_search_sandbox(
            query="q", path="/", limit=5, context=ctx, search_mode="semantic"
        )

        assert out == []
        assert "zone_filter" not in captured  # dispatch never happened


class TestWriteOnlyContextFailsClosedEverywhere:
    """Issue #4542 round-9 review: an empty readable scope is an
    authorization outcome — SANDBOX local/vector/BM25S paths must not run."""

    @pytest.mark.asyncio
    async def test_write_only_context_gets_no_results_and_no_fallback(self) -> None:
        from nexus.contracts.types import OperationContext

        svc = _make_sandbox_service()
        # Any retrieval work reaching these would be a leak.
        svc._hybrid_search_sandbox = None  # type: ignore[assignment]
        svc._try_sqlite_vec_sandbox = None  # type: ignore[assignment]

        dispatched = {"called": False}

        async def disp_search(**kwargs):
            dispatched["called"] = True
            return FederatedSearchResponse(
                results=[{"path": "/secret.md", "score": 0.9}],
                zones_searched=["eng"],
                zones_failed=[],
            )

        dispatcher = MagicMock()
        dispatcher.search = disp_search
        svc._federation_dispatcher = dispatcher

        ctx = OperationContext(
            user_id="alice", groups=[], zone_id="eng", zone_perms=(("eng", "w"),)
        )

        out = await svc._semantic_search_sandbox(
            query="q", path="/", limit=5, context=ctx, search_mode="semantic"
        )

        assert out == []
        assert dispatched["called"] is False
