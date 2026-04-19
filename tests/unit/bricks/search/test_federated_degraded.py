"""Tests for federation-unreachable detection (Issue #3778)."""

import logging
from unittest.mock import MagicMock

import pytest

from nexus.bricks.search.federated_search import (
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
