"""Tests for brick auto-discovery — Issue #2180."""

from __future__ import annotations

from nexus.factory._bricks import _discover_brick_factories


class TestBrickAutoDiscovery:
    """Tests for _discover_brick_factories()."""

    def test_discovers_delegation_brick(self) -> None:
        factories = _discover_brick_factories("independent")
        keys = {f.result_key for f in factories}
        assert "delegation_service" in keys

    def test_discovers_reputation_brick(self) -> None:
        factories = _discover_brick_factories("independent")
        keys = {f.result_key for f in factories}
        assert "reputation_service" in keys

    def test_discovers_snapshot_brick(self) -> None:
        factories = _discover_brick_factories("independent")
        keys = {f.result_key for f in factories}
        assert "snapshot_service" in keys

    def test_descriptor_fields(self) -> None:
        factories = _discover_brick_factories("independent")
        delegation = next(f for f in factories if f.result_key == "delegation_service")
        assert delegation.name is None  # No profile gate
        assert callable(delegation.create_fn)
