"""Tests for GovernanceGraphService.

Issue #1359 Phase 3: CRUD, cache invalidation, constraint check hot path.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.services.governance.governance_graph_service import GovernanceGraphService
from nexus.services.governance.models import ConstraintCheckResult, ConstraintType, EdgeType


def _make_mock_session_factory() -> MagicMock:
    """Create a mock async session factory."""
    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.execute = AsyncMock()

    begin_ctx = AsyncMock()
    begin_ctx.__aenter__ = AsyncMock()
    begin_ctx.__aexit__ = AsyncMock()
    session.begin = MagicMock(return_value=begin_ctx)

    factory = MagicMock(return_value=session)
    return factory


class TestAddConstraint:
    """Tests for adding constraints."""

    @pytest.mark.asyncio
    async def test_add_block_constraint(self) -> None:
        factory = _make_mock_session_factory()
        service = GovernanceGraphService(session_factory=factory)

        edge = await service.add_constraint(
            from_agent="agent-a",
            to_agent="agent-b",
            zone_id="zone-1",
            constraint_type=ConstraintType.BLOCK,
            reason="Suspicious activity",
        )

        assert edge.from_node == "agent-a"
        assert edge.to_node == "agent-b"
        assert edge.zone_id == "zone-1"
        assert edge.edge_type == EdgeType.CONSTRAINT

    @pytest.mark.asyncio
    async def test_add_constraint_invalidates_cache(self) -> None:
        factory = _make_mock_session_factory()
        service = GovernanceGraphService(session_factory=factory)

        # Populate cache
        service._cache[("zone-1", "agent-a", "agent-b")] = (
            ConstraintCheckResult(allowed=True),
            float("inf"),
        )

        await service.add_constraint(
            from_agent="agent-a",
            to_agent="agent-b",
            zone_id="zone-1",
            constraint_type=ConstraintType.BLOCK,
        )

        # Cache should be invalidated
        assert ("zone-1", "agent-a", "agent-b") not in service._cache


class TestCheckConstraint:
    """Tests for constraint checking (hot path)."""

    @pytest.mark.asyncio
    async def test_no_constraint_allows(self) -> None:
        factory = _make_mock_session_factory()
        session = factory()
        scalars = MagicMock()
        scalars.first.return_value = None
        execute_result = MagicMock()
        execute_result.scalars.return_value = scalars
        session.execute = AsyncMock(return_value=execute_result)

        service = GovernanceGraphService(session_factory=factory)
        result = await service.check_constraint("agent-a", "agent-b", "zone-1")

        assert result.allowed is True
        assert result.constraint_type is None

    @pytest.mark.asyncio
    async def test_cached_result_returned(self) -> None:
        factory = _make_mock_session_factory()
        service = GovernanceGraphService(session_factory=factory)

        # Pre-populate cache
        cached = ConstraintCheckResult(
            allowed=False,
            constraint_type=ConstraintType.BLOCK,
            reason="cached",
        )
        service._cache[("zone-1", "agent-a", "agent-b")] = (cached, float("inf"))

        result = await service.check_constraint("agent-a", "agent-b", "zone-1")
        assert result.allowed is False
        assert result.reason == "cached"

    @pytest.mark.asyncio
    async def test_block_constraint_found(self) -> None:
        factory = _make_mock_session_factory()
        session = factory()

        model = MagicMock()
        model.id = "edge-1"
        model.metadata_json = json.dumps(
            {
                "constraint_type": "block",
                "reason": "Blocked agent",
            }
        )

        scalars = MagicMock()
        scalars.first.return_value = model
        execute_result = MagicMock()
        execute_result.scalars.return_value = scalars
        session.execute = AsyncMock(return_value=execute_result)

        service = GovernanceGraphService(session_factory=factory)
        result = await service.check_constraint("agent-a", "agent-b", "zone-1")

        assert result.allowed is False
        assert result.constraint_type == ConstraintType.BLOCK
        assert result.reason == "Blocked agent"
        assert result.edge_id == "edge-1"


class TestCacheManagement:
    """Tests for TTL cache behavior."""

    def test_clear_cache(self) -> None:
        factory = _make_mock_session_factory()
        service = GovernanceGraphService(session_factory=factory)

        service._cache[("z", "a", "b")] = (ConstraintCheckResult(allowed=True), float("inf"))
        assert len(service._cache) == 1

        service.clear_cache()
        assert len(service._cache) == 0

    def test_cache_max_size(self) -> None:
        factory = _make_mock_session_factory()
        service = GovernanceGraphService(session_factory=factory)
        service._CACHE_MAX_SIZE = 2

        service._cache[("z", "a", "b")] = (ConstraintCheckResult(allowed=True), float("inf"))
        service._cache[("z", "c", "d")] = (ConstraintCheckResult(allowed=True), float("inf"))

        # Cache is full, new entry should respect limit
        assert len(service._cache) <= 2
