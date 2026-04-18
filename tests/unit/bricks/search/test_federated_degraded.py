"""Tests for federation-unreachable detection (Issue #3778)."""

from nexus.bricks.search.federated_search import (
    FederatedSearchResponse,
    FederationUnreachableError,
    ZoneFailure,
    is_all_peers_failed,
)


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
