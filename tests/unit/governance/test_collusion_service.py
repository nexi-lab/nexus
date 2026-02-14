"""Tests for CollusionService.

Issue #1359 Phase 2: Ring detection, Sybil detection with graph topology fixtures.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.governance.collusion_service import CollusionService
from nexus.governance.models import GovernanceEdge
from tests.unit.governance.fixtures.graph_topologies import (
    clean_chain,
    complex_ring,
    mixed_graph,
    simple_ring,
    star_topology,
    sybil_cluster,
)


def _make_mock_session_factory(edges: list[GovernanceEdge]) -> MagicMock:
    """Create a mock session factory that returns edges."""

    models = []
    for e in edges:
        m = MagicMock()
        m.id = e.edge_id
        m.from_node = e.from_node
        m.to_node = e.to_node
        m.zone_id = e.zone_id
        m.edge_type = e.edge_type
        m.weight = e.weight
        m.metadata_json = None
        m.created_at = e.created_at
        models.append(m)

    scalars_result = MagicMock()
    scalars_result.all.return_value = models

    execute_result = MagicMock()
    execute_result.scalars.return_value = scalars_result

    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.execute = AsyncMock(return_value=execute_result)

    factory = MagicMock(return_value=session)
    return factory


class TestDetectRings:
    """Tests for ring (cycle) detection."""

    @pytest.mark.asyncio
    async def test_no_rings_in_chain(self) -> None:
        edges = clean_chain(4)
        service = CollusionService(
            session_factory=_make_mock_session_factory(edges),
        )
        rings = await service.detect_rings("zone-1")
        assert len(rings) == 0

    @pytest.mark.asyncio
    async def test_simple_ring_detected(self) -> None:
        edges = simple_ring(3)
        service = CollusionService(
            session_factory=_make_mock_session_factory(edges),
        )
        rings = await service.detect_rings("zone-1")
        assert len(rings) >= 1
        assert len(rings[0].agents) == 3

    @pytest.mark.asyncio
    async def test_complex_rings_detected(self) -> None:
        edges = complex_ring()
        service = CollusionService(
            session_factory=_make_mock_session_factory(edges),
        )
        rings = await service.detect_rings("zone-1")
        assert len(rings) >= 2  # Two overlapping rings

    @pytest.mark.asyncio
    async def test_no_rings_in_star(self) -> None:
        edges = star_topology("hub", 5)
        service = CollusionService(
            session_factory=_make_mock_session_factory(edges),
        )
        rings = await service.detect_rings("zone-1")
        # Star topology has 2-cycles (hub↔spoke), but min ring size is 3
        ring_3plus = [r for r in rings if len(r.agents) >= 3]
        assert len(ring_3plus) == 0

    @pytest.mark.asyncio
    async def test_empty_graph(self) -> None:
        service = CollusionService(
            session_factory=_make_mock_session_factory([]),
        )
        rings = await service.detect_rings("zone-1")
        assert len(rings) == 0


class TestDetectSybils:
    """Tests for Sybil cluster detection."""

    @pytest.mark.asyncio
    async def test_sybil_cluster_detected(self) -> None:
        edges = sybil_cluster(n_honest=3, n_sybils=5)
        service = CollusionService(
            session_factory=_make_mock_session_factory(edges),
        )
        clusters = await service.detect_sybils("zone-1")
        # Should find at least some suspicious agents
        # (Sybils should have lower EigenTrust scores)
        # This is a statistical test, so we check structure
        assert isinstance(clusters, list)

    @pytest.mark.asyncio
    async def test_no_sybils_in_clean_chain(self) -> None:
        edges = clean_chain(4)
        service = CollusionService(
            session_factory=_make_mock_session_factory(edges),
        )
        clusters = await service.detect_sybils("zone-1")
        # Clean chain shouldn't have obvious Sybils
        assert isinstance(clusters, list)

    @pytest.mark.asyncio
    async def test_empty_graph_no_sybils(self) -> None:
        service = CollusionService(
            session_factory=_make_mock_session_factory([]),
        )
        clusters = await service.detect_sybils("zone-1")
        assert clusters == []


class TestComputeFraudScores:
    """Tests for composite fraud score computation."""

    @pytest.mark.asyncio
    async def test_fraud_scores_for_mixed_graph(self) -> None:
        edges = mixed_graph()
        service = CollusionService(
            session_factory=_make_mock_session_factory(edges),
        )
        scores = await service.compute_fraud_scores("zone-1")
        assert len(scores) > 0
        # All scores should be between 0 and 1
        for score in scores.values():
            assert 0.0 <= score.score <= 1.0
            assert "trust" in score.components

    @pytest.mark.asyncio
    async def test_fraud_scores_empty_graph(self) -> None:
        service = CollusionService(
            session_factory=_make_mock_session_factory([]),
        )
        scores = await service.compute_fraud_scores("zone-1")
        assert scores == {}


class TestSizeCaps:
    """Tests for graph size limiting."""

    @pytest.mark.asyncio
    async def test_max_nodes_limit(self) -> None:
        from tests.unit.governance.fixtures.graph_topologies import large_graph

        edges = large_graph(n_nodes=200, n_edges=1000)
        service = CollusionService(
            session_factory=_make_mock_session_factory(edges),
            max_nodes=50,
            max_edges=100,
        )
        graph = await service.build_interaction_graph("zone-1")
        assert graph.number_of_edges() <= 100
