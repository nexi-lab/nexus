"""Unit tests for CollusionService.

Tests interaction graph building, ring detection, Sybil detection,
and composite fraud score computation using pre-loaded edges
to avoid database dependencies.

Requires: networkx (available on Python 3.13)
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

nx = pytest.importorskip("networkx", reason="networkx required for collusion tests")

from nexus.bricks.governance.collusion_service import CollusionService  # noqa: E402
from nexus.bricks.governance.models import (  # noqa: E402
    EdgeType,
    GovernanceEdge,
    RingType,
)


def _edge(from_n: str, to_n: str, weight: float = 1.0, edge_id: str = "") -> GovernanceEdge:
    """Helper to create a GovernanceEdge."""
    return GovernanceEdge(
        edge_id=edge_id or f"{from_n}->{to_n}",
        from_node=from_n,
        to_node=to_n,
        zone_id="z1",
        edge_type=EdgeType.TRANSACTION,
        weight=weight,
        created_at=datetime.now(UTC),
    )


@pytest.fixture()
def session_factory() -> AsyncMock:
    """Mock async session factory."""
    return AsyncMock()


@pytest.fixture()
def service(session_factory: AsyncMock) -> CollusionService:
    return CollusionService(session_factory=session_factory, max_cycle_length=8)


# ---------------------------------------------------------------------------
# build_interaction_graph
# ---------------------------------------------------------------------------


class TestBuildInteractionGraph:
    """Tests for build_interaction_graph with _edges parameter."""

    @pytest.mark.asyncio
    async def test_empty_edges(self, service: CollusionService) -> None:
        graph = await service.build_interaction_graph("z1", _edges=[])
        assert graph.number_of_nodes() == 0
        assert graph.number_of_edges() == 0

    @pytest.mark.asyncio
    async def test_single_edge(self, service: CollusionService) -> None:
        edges = [_edge("a", "b", weight=5.0)]
        graph = await service.build_interaction_graph("z1", _edges=edges)
        assert graph.number_of_nodes() == 2
        assert graph.number_of_edges() == 1
        assert graph.has_edge("a", "b")

    @pytest.mark.asyncio
    async def test_edge_attributes(self, service: CollusionService) -> None:
        edges = [_edge("a", "b", weight=3.0)]
        graph = await service.build_interaction_graph("z1", _edges=edges)
        data = graph.get_edge_data("a", "b")
        assert data["weight"] == 3.0

    @pytest.mark.asyncio
    async def test_max_edges_cap(self, session_factory: AsyncMock) -> None:
        svc = CollusionService(session_factory=session_factory, max_edges=2)
        edges = [_edge("a", "b"), _edge("b", "c"), _edge("c", "d")]
        graph = await svc.build_interaction_graph("z1", _edges=edges)
        # Only first 2 edges should be loaded
        assert graph.number_of_edges() <= 2


# ---------------------------------------------------------------------------
# detect_rings
# ---------------------------------------------------------------------------


class TestDetectRings:
    """Tests for ring detection."""

    @pytest.mark.asyncio
    async def test_no_cycle(self, service: CollusionService) -> None:
        edges = [_edge("a", "b"), _edge("b", "c")]
        graph = await service.build_interaction_graph("z1", _edges=edges)
        rings = await service.detect_rings("z1", _graph=graph)
        assert rings == []

    @pytest.mark.asyncio
    async def test_simple_triangle(self, service: CollusionService) -> None:
        edges = [_edge("a", "b"), _edge("b", "c"), _edge("c", "a")]
        graph = await service.build_interaction_graph("z1", _edges=edges)
        rings = await service.detect_rings("z1", _graph=graph)
        assert len(rings) == 1
        assert len(rings[0].agents) == 3
        assert rings[0].ring_type == RingType.SIMPLE_CYCLE

    @pytest.mark.asyncio
    async def test_empty_graph(self, service: CollusionService) -> None:
        graph = await service.build_interaction_graph("z1", _edges=[])
        rings = await service.detect_rings("z1", _graph=graph)
        assert rings == []

    @pytest.mark.asyncio
    async def test_larger_cycle_is_complex(self, service: CollusionService) -> None:
        # 5-node cycle: a->b->c->d->e->a
        edges = [
            _edge("a", "b"),
            _edge("b", "c"),
            _edge("c", "d"),
            _edge("d", "e"),
            _edge("e", "a"),
        ]
        graph = await service.build_interaction_graph("z1", _edges=edges)
        rings = await service.detect_rings("z1", _graph=graph)
        assert len(rings) >= 1
        # A cycle of length 5 should be COMPLEX_CYCLE
        long_rings = [r for r in rings if len(r.agents) > 4]
        assert all(r.ring_type == RingType.COMPLEX_CYCLE for r in long_rings)

    @pytest.mark.asyncio
    async def test_ring_has_valid_fields(self, service: CollusionService) -> None:
        edges = [_edge("a", "b", 5.0), _edge("b", "c", 3.0), _edge("c", "a", 7.0)]
        graph = await service.build_interaction_graph("z1", _edges=edges)
        rings = await service.detect_rings("z1", _graph=graph)
        assert len(rings) >= 1
        ring = rings[0]
        assert ring.ring_id
        assert ring.zone_id == "z1"
        assert 0 <= ring.confidence <= 1.0
        assert ring.total_volume > 0
        assert ring.detected_at is not None


# ---------------------------------------------------------------------------
# detect_sybils
# ---------------------------------------------------------------------------


class TestDetectSybils:
    """Tests for Sybil detection via EigenTrust."""

    @pytest.mark.asyncio
    async def test_empty_zone(self) -> None:
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_session.execute.return_value = mock_result
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        factory = MagicMock(return_value=mock_session)

        svc = CollusionService(session_factory=factory)
        clusters = await svc.detect_sybils("z1")
        assert clusters == []


# ---------------------------------------------------------------------------
# compute_fraud_scores
# ---------------------------------------------------------------------------


class TestComputeFraudScores:
    """Tests for composite fraud score computation."""

    @pytest.mark.asyncio
    async def test_empty_zone(self) -> None:
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_session.execute.return_value = mock_result
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        factory = MagicMock(return_value=mock_session)

        svc = CollusionService(session_factory=factory)
        scores = await svc.compute_fraud_scores("z1")
        assert scores == {}
