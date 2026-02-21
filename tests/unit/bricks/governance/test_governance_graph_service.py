"""Unit tests for GovernanceGraphService.

Tests constraint cache behavior, cache invalidation logic,
and the edge_model_to_domain converter.
"""

import time
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.bricks.governance.converters import edge_model_to_domain
from nexus.bricks.governance.governance_graph_service import GovernanceGraphService
from nexus.bricks.governance.models import (
    ConstraintCheckResult,
    ConstraintType,
    EdgeType,
)


@pytest.fixture()
def session_factory() -> AsyncMock:
    """Mock async session factory."""
    return AsyncMock()


@pytest.fixture()
def service(session_factory: AsyncMock) -> GovernanceGraphService:
    return GovernanceGraphService(session_factory=session_factory, cache_ttl=60.0)


# ---------------------------------------------------------------------------
# Cache behavior
# ---------------------------------------------------------------------------


class TestCache:
    """Tests for constraint cache logic (dict with TTL tuples)."""

    def test_cache_starts_empty(self, service: GovernanceGraphService) -> None:
        assert len(service._cache) == 0

    def test_clear_cache(self, service: GovernanceGraphService) -> None:
        result = ConstraintCheckResult(allowed=True)
        service._cache[("z1", "a", "b")] = (result, time.monotonic() + 60)
        assert len(service._cache) == 1

        service.clear_cache()
        assert len(service._cache) == 0

    def test_invalidate_removes_both_directions(self, service: GovernanceGraphService) -> None:
        result = ConstraintCheckResult(allowed=True)
        expires = time.monotonic() + 60
        service._cache[("z1", "a", "b")] = (result, expires)
        service._cache[("z1", "b", "a")] = (result, expires)
        assert len(service._cache) == 2

        service._invalidate("z1", "a", "b")
        assert len(service._cache) == 0

    def test_invalidate_only_affects_matching_pair(self, service: GovernanceGraphService) -> None:
        result = ConstraintCheckResult(allowed=True)
        expires = time.monotonic() + 60
        service._cache[("z1", "a", "b")] = (result, expires)
        service._cache[("z1", "c", "d")] = (result, expires)

        service._invalidate("z1", "a", "b")
        assert ("z1", "c", "d") in service._cache
        assert ("z1", "a", "b") not in service._cache

    def test_invalidate_nonexistent_key_is_noop(self, service: GovernanceGraphService) -> None:
        # Should not raise
        service._invalidate("z1", "x", "y")
        assert len(service._cache) == 0


# ---------------------------------------------------------------------------
# check_constraint with cache
# ---------------------------------------------------------------------------


class TestCheckConstraint:
    """Tests for check_constraint cache behavior."""

    @pytest.mark.asyncio
    async def test_cache_hit_returns_cached_result(self, service: GovernanceGraphService) -> None:
        cached_result = ConstraintCheckResult(
            allowed=False,
            constraint_type=ConstraintType.BLOCK,
            reason="Cached block",
        )
        # Cache stores (result, expires_at_monotonic)
        service._cache[("z1", "a", "b")] = (cached_result, time.monotonic() + 60)

        result = await service.check_constraint("a", "b", "z1")
        assert result.allowed is False
        assert result.reason == "Cached block"

    @pytest.mark.asyncio
    async def test_cache_miss_triggers_lookup(self, service: GovernanceGraphService) -> None:
        # No cache entry -> should call _lookup_constraint
        fresh_result = ConstraintCheckResult(allowed=True)
        service._lookup_constraint = AsyncMock(return_value=fresh_result)

        result = await service.check_constraint("a", "b", "z1")
        assert result.allowed is True
        service._lookup_constraint.assert_called_once_with("a", "b", "z1")


# ---------------------------------------------------------------------------
# edge_model_to_domain converter
# ---------------------------------------------------------------------------


class TestEdgeModelToDomain:
    """Tests for the edge_model_to_domain converter (was _model_to_edge)."""

    def test_basic_conversion(self) -> None:
        model = MagicMock()
        model.id = "e1"
        model.from_node = "agent-a"
        model.to_node = "agent-b"
        model.zone_id = "zone-1"
        model.edge_type = EdgeType.CONSTRAINT
        model.weight = 0.0
        model.metadata_json = '{"constraint_type": "block", "reason": "test"}'
        model.created_at = datetime(2025, 1, 1, tzinfo=UTC)

        edge = edge_model_to_domain(model)
        assert edge.edge_id == "e1"
        assert edge.from_node == "agent-a"
        assert edge.to_node == "agent-b"
        assert edge.zone_id == "zone-1"
        assert edge.edge_type == EdgeType.CONSTRAINT
        assert edge.metadata["constraint_type"] == "block"
        assert edge.metadata["reason"] == "test"

    def test_null_metadata_json(self) -> None:
        model = MagicMock()
        model.id = "e2"
        model.from_node = "a"
        model.to_node = "b"
        model.zone_id = "z1"
        model.edge_type = EdgeType.TRANSACTION
        model.weight = 1.0
        model.metadata_json = None
        model.created_at = None

        edge = edge_model_to_domain(model)
        assert edge.metadata == {}

    def test_invalid_json_metadata(self) -> None:
        model = MagicMock()
        model.id = "e3"
        model.from_node = "a"
        model.to_node = "b"
        model.zone_id = "z1"
        model.edge_type = EdgeType.DELEGATION
        model.weight = 2.0
        model.metadata_json = "not valid json{{"
        model.created_at = None

        edge = edge_model_to_domain(model)
        # Invalid JSON should result in empty metadata
        assert edge.metadata == {}


# ---------------------------------------------------------------------------
# Response service integration (auto_throttle logic)
# ---------------------------------------------------------------------------


class _FakeAsyncSession:
    """Fake async session for response service tests."""

    async def __aenter__(self) -> "_FakeAsyncSession":
        return self

    async def __aexit__(self, *args: object) -> None:
        pass

    def begin(self) -> "_FakeAsyncSession":
        return self

    def add(self, model: object) -> None:
        pass

    async def flush(self) -> None:
        pass


class TestResponseServiceAutoThrottle:
    """Tests for ResponseService.auto_throttle thresholds.

    These test the threshold logic without database interaction.
    """

    @pytest.mark.asyncio
    async def test_below_throttle_threshold_returns_none(self) -> None:
        from nexus.bricks.governance.models import FraudScore
        from nexus.bricks.governance.response_service import ResponseService

        svc = ResponseService(session_factory=lambda: _FakeAsyncSession())
        score = FraudScore(agent_id="a1", zone_id="z1", score=0.3)
        result = await svc.auto_throttle("a1", "z1", score)
        assert result is None

    @pytest.mark.asyncio
    async def test_above_block_threshold_blocks(self) -> None:
        from nexus.bricks.governance.models import FraudScore
        from nexus.bricks.governance.response_service import ResponseService

        graph_service = AsyncMock()
        svc = ResponseService(
            session_factory=lambda: _FakeAsyncSession(),
            graph_service=graph_service,
        )
        score = FraudScore(agent_id="a1", zone_id="z1", score=0.9)
        result = await svc.auto_throttle("a1", "z1", score)
        # Score >= 0.8 triggers block, returns None (blocked, not throttled)
        assert result is None
        graph_service.add_constraint.assert_called_once()

    @pytest.mark.asyncio
    async def test_throttle_range_returns_config(self) -> None:
        from nexus.bricks.governance.models import FraudScore
        from nexus.bricks.governance.response_service import ResponseService

        svc = ResponseService(session_factory=lambda: _FakeAsyncSession())
        score = FraudScore(agent_id="a1", zone_id="z1", score=0.6)
        result = await svc.auto_throttle("a1", "z1", score)
        assert result is not None
        assert result.agent_id == "a1"
        assert result.max_tx_per_hour >= 1
        assert result.max_amount_per_day >= 1.0
