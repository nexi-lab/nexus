"""Collusion detection service.

Issue #1359 Phase 2: Agent interaction graph, ring detection,
Sybil detection, and fraud scoring.

All graph operations are zone-scoped with configurable size caps.
Runs as background job (not in request path).
"""

import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from nexus.bricks.governance.converters import edge_model_to_domain
from nexus.bricks.governance.json_utils import parse_json_metadata
from nexus.bricks.governance.models import (
    FraudRing,
    FraudScore,
    GovernanceEdge,
    RingType,
)
from nexus.bricks.governance.snapshot import GovernanceSnapshot
from nexus.bricks.governance.trust_math import (
    build_local_trust_matrix,
    detect_sybil_cluster,
    eigentrust,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    import networkx as nx
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


def _require_networkx() -> Any:
    """Import networkx or raise a clear governance-extra install hint."""
    try:
        import networkx as nx
    except ImportError:
        raise RuntimeError(
            "Governance graph dependency 'networkx' is not installed. "
            "Install with: pip install 'nexus-ai-fs[governance]'"
        ) from None
    return nx


class CollusionService:
    """Detects collusion patterns in agent transaction graphs.

    Responsibilities:
        - Build interaction graphs from DB edges
        - Detect transaction rings (cycles)
        - Detect Sybil clusters via EigenTrust
        - Compute composite fraud scores

    Memory budget:
        Graph size is capped by ``max_nodes`` (default 10,000) and
        ``max_edges`` (default 50,000).  At worst case this is roughly
        ~50 MB for the NetworkX DiGraph plus the trust matrix.
        Increase caps only if the host has headroom.
    """

    def __init__(
        self,
        session_factory: "Callable[[], AsyncSession]",
        max_nodes: int = 10_000,
        max_edges: int = 50_000,
        max_cycle_length: int = 8,
    ) -> None:
        self._session_factory = session_factory
        self._max_nodes = max_nodes
        self._max_edges = max_edges
        self._max_cycle_length = max_cycle_length

    async def load_snapshot(
        self,
        zone_id: str,
        since: datetime | None = None,
    ) -> GovernanceSnapshot:
        """Load edges and derive node IDs for batch operations.

        Returns a frozen snapshot that can be passed to multiple methods
        to avoid duplicate DB loads (Issue #2129 §13B).
        """
        edges = await self._load_edges(zone_id, since)
        node_set: set[str] = set()
        for e in edges:
            node_set.add(e.from_node)
            node_set.add(e.to_node)
        return GovernanceSnapshot(edges=edges, node_ids=sorted(node_set))

    async def build_interaction_graph(
        self,
        zone_id: str,
        since: datetime | None = None,
        *,
        _edges: list[GovernanceEdge] | None = None,
    ) -> "nx.DiGraph":
        """Build a NetworkX DiGraph from stored governance edges.

        Zone-scoped with size limits to prevent OOM.

        Args:
            zone_id: Zone to build graph for
            since: Optional time filter for edges
            _edges: Pre-loaded edges (avoids duplicate DB query)
        """
        nx = _require_networkx()

        edges = _edges if _edges is not None else await self._load_edges(zone_id, since)

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

    async def detect_rings(
        self,
        zone_id: str,
        *,
        _graph: "nx.DiGraph | None" = None,
    ) -> list[FraudRing]:
        """Detect transaction rings (cycles) in the interaction graph.

        Uses Johnson's algorithm (nx.simple_cycles) to find all simple cycles
        with length between 3 and max_cycle_length.

        Args:
            zone_id: Zone to detect rings in
            _graph: Pre-built graph (avoids duplicate edge load)
        """
        nx = _require_networkx()

        graph = _graph if _graph is not None else await self.build_interaction_graph(zone_id)

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

    async def detect_sybils(
        self,
        zone_id: str,
        *,
        _snapshot: GovernanceSnapshot | None = None,
    ) -> list[set[str]]:
        """Detect Sybil clusters using EigenTrust scores.

        Low-trust agents are clustered together as potential Sybils.

        Args:
            zone_id: Zone to analyze.
            _snapshot: Pre-loaded snapshot (avoids duplicate DB query).
        """
        if _snapshot is not None:
            edges = _snapshot.edges
            node_ids = _snapshot.node_ids
        else:
            edges = await self._load_edges(zone_id)
            if not edges:
                return []
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

    async def list_fraud_scores(self, zone_id: str) -> list[FraudScore]:
        """List stored fraud scores for a zone (read path).

        Returns pre-computed scores from the database without
        triggering recomputation.
        """
        from sqlalchemy import select

        from nexus.bricks.governance.db_models import FraudScoreModel

        async with self._session_factory() as session:
            stmt = select(FraudScoreModel).where(FraudScoreModel.zone_id == zone_id)
            result = await session.execute(stmt)
            models = result.scalars().all()

        scores: list[FraudScore] = []
        for model in models:
            components: dict[str, float] = {}
            raw = parse_json_metadata(getattr(model, "components", None))
            for k, v in raw.items():
                if isinstance(v, int | float):
                    components[k] = float(v)
            scores.append(
                FraudScore(
                    agent_id=model.agent_id,
                    zone_id=model.zone_id,
                    score=model.score,
                    components=components,
                    computed_at=model.computed_at,
                )
            )
        return scores

    async def compute_and_persist_fraud_scores(self, zone_id: str) -> dict[str, FraudScore]:
        """Compute fraud scores and persist to DB (write path).

        Call this from a background job or explicit POST endpoint,
        not from GET requests.
        """
        import json

        from nexus.bricks.governance.db_models import FraudScoreModel

        scores = await self.compute_fraud_scores(zone_id)
        now = datetime.now(UTC)

        async with self._session_factory() as session, session.begin():
            for score in scores.values():
                from sqlalchemy import select

                computed = score.computed_at or now

                stmt = select(FraudScoreModel).where(
                    FraudScoreModel.agent_id == score.agent_id,
                    FraudScoreModel.zone_id == score.zone_id,
                )
                result = await session.execute(stmt)
                existing = result.scalar_one_or_none()

                if existing is not None:
                    existing.score = score.score
                    existing.components = json.dumps(score.components)
                    existing.computed_at = computed
                else:
                    session.add(
                        FraudScoreModel(
                            agent_id=score.agent_id,
                            zone_id=score.zone_id,
                            score=score.score,
                            components=json.dumps(score.components),
                            computed_at=computed,
                        )
                    )

        return scores

    async def compute_fraud_scores(self, zone_id: str) -> dict[str, FraudScore]:
        """Compute composite fraud scores for all agents in a zone.

        Combines:
            - Ring membership count
            - EigenTrust score (inverted: low trust = high fraud)
            - Weighted composite
        """
        now = datetime.now(UTC)

        # Load snapshot once and reuse for all sub-operations (Issue #2129 §13B)
        snapshot = await self.load_snapshot(zone_id)

        if not snapshot.edges:
            return {}

        # Build graph and detect rings from pre-loaded edges
        graph = await self.build_interaction_graph(zone_id, _edges=snapshot.edges)
        rings = await self.detect_rings(zone_id, _graph=graph)

        node_ids = snapshot.node_ids
        matrix = build_local_trust_matrix(snapshot.edges, node_ids)
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

        from nexus.bricks.governance.db_models import FraudScoreModel

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
        raw = parse_json_metadata(getattr(model, "components", None))
        for k, v in raw.items():
            if isinstance(v, int | float):
                components[k] = float(v)

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

        from nexus.bricks.governance.db_models import GovernanceEdgeModel

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

            return [edge_model_to_domain(m) for m in models]
