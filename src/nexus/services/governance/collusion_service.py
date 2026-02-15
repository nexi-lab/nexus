"""Collusion detection service.

Issue #1359 Phase 2: Agent interaction graph, ring detection,
Sybil detection, and fraud scoring.

All graph operations are zone-scoped with configurable size caps.
Runs as background job (not in request path).
"""

from __future__ import annotations

import contextlib
import json
import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from nexus.services.governance.models import (
    EdgeType,
    FraudRing,
    FraudScore,
    GovernanceEdge,
    RingType,
)
from nexus.services.governance.trust_math import (
    build_local_trust_matrix,
    detect_sybil_cluster,
    eigentrust,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    import networkx as nx
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


class CollusionService:
    """Detects collusion patterns in agent transaction graphs.

    Responsibilities:
        - Build interaction graphs from DB edges
        - Detect transaction rings (cycles)
        - Detect Sybil clusters via EigenTrust
        - Compute composite fraud scores
    """

    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        max_nodes: int = 10_000,
        max_edges: int = 50_000,
        max_cycle_length: int = 8,
    ) -> None:
        self._session_factory = session_factory
        self._max_nodes = max_nodes
        self._max_edges = max_edges
        self._max_cycle_length = max_cycle_length

    async def build_interaction_graph(
        self,
        zone_id: str,
        since: datetime | None = None,
    ) -> nx.DiGraph:
        """Build a NetworkX DiGraph from stored governance edges.

        Zone-scoped with size limits to prevent OOM.
        """
        import networkx as nx

        edges = await self._load_edges(zone_id, since)

        g = nx.DiGraph()
        for edge in edges[: self._max_edges]:
            g.add_edge(
                edge.from_node,
                edge.to_node,
                weight=edge.weight,
                edge_type=edge.edge_type,
                edge_id=edge.edge_id,
            )
            if g.number_of_nodes() > self._max_nodes:
                if logger.isEnabledFor(logging.WARNING):
                    logger.warning(
                        "Graph for zone=%s hit node cap (%d), truncating",
                        zone_id,
                        self._max_nodes,
                    )
                break

        return g

    async def detect_rings(self, zone_id: str) -> list[FraudRing]:
        """Detect transaction rings (cycles) in the interaction graph.

        Uses Johnson's algorithm (nx.simple_cycles) to find all simple cycles
        with length between 3 and max_cycle_length.
        """
        import networkx as nx

        graph = await self.build_interaction_graph(zone_id)

        if graph.number_of_nodes() == 0:
            return []

        rings: list[FraudRing] = []
        now = datetime.now(UTC)

        for cycle in nx.simple_cycles(graph, length_bound=self._max_cycle_length):
            if len(cycle) < 3:
                continue

            # Compute confidence based on edge weights in the cycle
            total_weight = 0.0
            for i in range(len(cycle)):
                src = cycle[i]
                dst = cycle[(i + 1) % len(cycle)]
                edge_data = graph.get_edge_data(src, dst, default={})
                total_weight += edge_data.get("weight", 1.0)

            avg_weight = total_weight / len(cycle)
            confidence = min(avg_weight / 10.0, 1.0)  # Normalize

            ring_type = RingType.SIMPLE_CYCLE if len(cycle) <= 4 else RingType.COMPLEX_CYCLE

            rings.append(
                FraudRing(
                    ring_id=str(uuid.uuid4()),
                    zone_id=zone_id,
                    agents=list(cycle),
                    ring_type=ring_type,
                    confidence=round(confidence, 4),
                    total_volume=total_weight,
                    detected_at=now,
                )
            )

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "Detected %d rings in zone=%s",
                len(rings),
                zone_id,
            )

        return rings

    async def detect_sybils(self, zone_id: str) -> list[set[str]]:
        """Detect Sybil clusters using EigenTrust scores.

        Low-trust agents are clustered together as potential Sybils.
        """
        edges = await self._load_edges(zone_id)

        if not edges:
            return []

        # Collect unique node IDs
        node_set: set[str] = set()
        for e in edges:
            node_set.add(e.from_node)
            node_set.add(e.to_node)
        node_ids = sorted(node_set)

        if len(node_ids) < 2:
            return []

        # Build trust matrix and compute EigenTrust
        matrix = build_local_trust_matrix(edges, node_ids)
        trust_vector = eigentrust(matrix)

        trust_scores = dict(zip(node_ids, trust_vector.tolist(), strict=True))
        return detect_sybil_cluster(trust_scores)

    async def compute_fraud_scores(self, zone_id: str) -> dict[str, FraudScore]:
        """Compute composite fraud scores for all agents in a zone.

        Combines:
            - Ring membership count
            - EigenTrust score (inverted: low trust = high fraud)
            - Weighted composite
        """
        now = datetime.now(UTC)

        # Get rings and trust scores
        rings = await self.detect_rings(zone_id)
        edges = await self._load_edges(zone_id)

        if not edges:
            return {}

        # Build trust scores
        node_set: set[str] = set()
        for e in edges:
            node_set.add(e.from_node)
            node_set.add(e.to_node)
        node_ids = sorted(node_set)

        matrix = build_local_trust_matrix(edges, node_ids)
        trust_vector = eigentrust(matrix)
        trust_scores = dict(zip(node_ids, trust_vector.tolist(), strict=True))

        # Count ring memberships per agent
        ring_counts: dict[str, int] = {}
        for ring in rings:
            for agent_id in ring.agents:
                ring_counts[agent_id] = ring_counts.get(agent_id, 0) + 1

        # Composite score per agent
        scores: dict[str, FraudScore] = {}
        for agent_id in node_ids:
            trust = trust_scores.get(agent_id, 0.5)
            ring_count = ring_counts.get(agent_id, 0)

            # Fraud score: 0.0 (clean) to 1.0 (fraudulent)
            # Invert trust (low trust = high fraud), add ring penalty
            trust_component = max(0.0, 1.0 - trust * len(node_ids))
            ring_component = min(ring_count * 0.2, 1.0)

            # Weighted composite
            score = min(0.6 * trust_component + 0.4 * ring_component, 1.0)

            scores[agent_id] = FraudScore(
                agent_id=agent_id,
                zone_id=zone_id,
                score=round(score, 4),
                components={
                    "trust": round(trust, 4),
                    "trust_component": round(trust_component, 4),
                    "ring_count": ring_count,
                    "ring_component": round(ring_component, 4),
                },
                computed_at=now,
            )

        return scores

    async def get_fraud_score(
        self,
        agent_id: str,
        zone_id: str,
    ) -> FraudScore | None:
        """Get cached fraud score for an agent."""
        from sqlalchemy import select

        from nexus.services.governance.db_models import FraudScoreModel

        async with self._session_factory() as session:
            stmt = select(FraudScoreModel).where(
                FraudScoreModel.agent_id == agent_id,
                FraudScoreModel.zone_id == zone_id,
            )
            result = await session.execute(stmt)
            model = result.scalar_one_or_none()

        if model is None:
            return None

        components: dict[str, float] = {}
        if model.components:
            with contextlib.suppress(json.JSONDecodeError, TypeError):
                components = json.loads(model.components)

        return FraudScore(
            agent_id=model.agent_id,
            zone_id=model.zone_id,
            score=model.score,
            components=components,
            computed_at=model.computed_at,
        )

    async def _load_edges(
        self,
        zone_id: str,
        since: datetime | None = None,
    ) -> list[GovernanceEdge]:
        """Load governance edges from DB."""
        from sqlalchemy import select

        from nexus.services.governance.db_models import GovernanceEdgeModel

        async with self._session_factory() as session:
            stmt = (
                select(GovernanceEdgeModel)
                .where(GovernanceEdgeModel.zone_id == zone_id)
                .limit(self._max_edges)
            )

            if since is not None:
                stmt = stmt.where(GovernanceEdgeModel.created_at >= since)

            result = await session.execute(stmt)
            models = result.scalars().all()

            edges: list[GovernanceEdge] = []
            for m in models:
                metadata: dict[str, object] = {}
                if m.metadata_json:
                    with contextlib.suppress(json.JSONDecodeError, TypeError):
                        metadata = json.loads(m.metadata_json)
                edges.append(
                    GovernanceEdge(
                        edge_id=m.id,
                        from_node=m.from_node,
                        to_node=m.to_node,
                        zone_id=m.zone_id,
                        edge_type=EdgeType(m.edge_type),
                        weight=m.weight,
                        metadata=metadata,
                        created_at=m.created_at,
                    )
                )

            return edges
