"""Eviction policy protocol and LRU implementation (Issue #2170).

Defines the pluggable policy interface for selecting which agents to evict
under resource pressure, plus the default LRU (least-recently-used) policy.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from nexus.contracts.agent_types import AgentRecord


@runtime_checkable
class EvictionPolicy(Protocol):
    """Protocol for agent eviction candidate selection.

    Implementations decide which agents from a pre-sorted candidate list
    should actually be evicted. The default LRU policy simply takes the
    first N agents (already sorted by staleness from the DB query).
    """

    def select_candidates(self, agents: list[AgentRecord], batch_size: int) -> list[AgentRecord]:
        """Select which agents to evict from the candidates list.

        Args:
            agents: Pre-sorted candidate agents (oldest heartbeat first).
            batch_size: Maximum number of agents to select.

        Returns:
            List of agents to evict (up to batch_size).
        """
        ...


class LRUEvictionPolicy:
    """Evict agents with oldest last_heartbeat first (default policy).

    The candidates are already sorted by the DB query (last_heartbeat ASC
    NULLS FIRST), so this policy simply slices the first batch_size agents.

    TODO(#2170): Add QoS-based eviction ordering. Agents should declare a
    priority tier (e.g. via metadata["qos"] = "best_effort" | "standard" |
    "critical"), and eviction should prefer lower-priority agents first.
    Requires AgentRecord.qos property + policy sorting by (qos ASC, heartbeat ASC).
    """

    def select_candidates(self, agents: list[AgentRecord], batch_size: int) -> list[AgentRecord]:
        """Select least-recently-used agents for eviction.

        Args:
            agents: Pre-sorted candidate agents (oldest heartbeat first).
            batch_size: Maximum number of agents to select.

        Returns:
            List of agents to evict (up to batch_size).
        """
        return agents[:batch_size]
