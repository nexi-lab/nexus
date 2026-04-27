"""Conflict detection for edge split-brain reconciliation.

Compares edge vs cloud state using vector clocks and etags to determine
whether operations conflict and how to resolve them using LWW (last-writer-wins).

Issue #1707: Edge split-brain resilience.
"""

import logging
from dataclasses import dataclass
from enum import Enum

from nexus.proxy.vector_clock import CausalOrder, VectorClock

logger = logging.getLogger(__name__)


class ConflictOutcome(Enum):
    """Result of conflict resolution."""

    NO_CONFLICT = "no_conflict"
    EDGE_WINS = "edge_wins"
    CLOUD_WINS = "cloud_wins"
    TRUE_CONFLICT = "true_conflict"


@dataclass(frozen=True, slots=True)
class OperationState:
    """Snapshot of an operation's state for conflict comparison."""

    vector_clock: VectorClock
    content_id: str | None = None
    timestamp: float = 0.0


@dataclass(frozen=True, slots=True)
class ConflictResult:
    """Outcome of comparing edge vs cloud state."""

    outcome: ConflictOutcome
    edge_state: OperationState
    cloud_state: OperationState
    reason: str


class ConflictDetector:
    """Detects and resolves conflicts between edge and cloud operations.

    Uses vector clocks for causal ordering. When clocks are concurrent,
    falls back to LWW (last-writer-wins) using timestamps.

    Parameters
    ----------
    node_id:
        Identifier for this edge node (used in log messages).
    """

    def __init__(self, node_id: str = "edge") -> None:
        self._node_id = node_id

    def detect(
        self,
        edge: OperationState,
        cloud: OperationState,
    ) -> ConflictResult:
        """Compare edge and cloud states to detect conflicts.

        Resolution strategy:
        1. If vector clocks show causal ordering → no conflict
        2. If content_ids match → no conflict (same content)
        3. If clocks are concurrent → LWW by timestamp
        4. If timestamps are equal → true conflict (manual resolution needed)
        """
        order = edge.vector_clock.compare(cloud.vector_clock)

        if order is CausalOrder.EQUAL:
            return ConflictResult(
                outcome=ConflictOutcome.NO_CONFLICT,
                edge_state=edge,
                cloud_state=cloud,
                reason="identical vector clocks",
            )

        if order is CausalOrder.BEFORE:
            return ConflictResult(
                outcome=ConflictOutcome.CLOUD_WINS,
                edge_state=edge,
                cloud_state=cloud,
                reason="edge happened-before cloud",
            )

        if order is CausalOrder.AFTER:
            return ConflictResult(
                outcome=ConflictOutcome.EDGE_WINS,
                edge_state=edge,
                cloud_state=cloud,
                reason="edge happened-after cloud",
            )

        # CONCURRENT — check content_ids first
        if (
            edge.content_id is not None
            and cloud.content_id is not None
            and edge.content_id == cloud.content_id
        ):
            return ConflictResult(
                outcome=ConflictOutcome.NO_CONFLICT,
                edge_state=edge,
                cloud_state=cloud,
                reason="concurrent clocks but identical content_ids",
            )

        # CONCURRENT — fall back to LWW by timestamp
        if edge.timestamp > cloud.timestamp:
            logger.info(
                "LWW resolution for node %s: edge wins (%.3f > %.3f)",
                self._node_id,
                edge.timestamp,
                cloud.timestamp,
            )
            return ConflictResult(
                outcome=ConflictOutcome.EDGE_WINS,
                edge_state=edge,
                cloud_state=cloud,
                reason="concurrent clocks, edge has later timestamp (LWW)",
            )

        if cloud.timestamp > edge.timestamp:
            logger.info(
                "LWW resolution for node %s: cloud wins (%.3f > %.3f)",
                self._node_id,
                cloud.timestamp,
                edge.timestamp,
            )
            return ConflictResult(
                outcome=ConflictOutcome.CLOUD_WINS,
                edge_state=edge,
                cloud_state=cloud,
                reason="concurrent clocks, cloud has later timestamp (LWW)",
            )

        # Timestamps equal + concurrent clocks → true conflict
        logger.warning(
            "True conflict detected for node %s: concurrent clocks with equal timestamps",
            self._node_id,
        )
        return ConflictResult(
            outcome=ConflictOutcome.TRUE_CONFLICT,
            edge_state=edge,
            cloud_state=cloud,
            reason="concurrent clocks with equal timestamps",
        )
