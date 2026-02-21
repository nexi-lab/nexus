"""Eviction policy protocol and implementations (Issues #2170, #2171).

Defines the pluggable policy interface for selecting which agents to evict
under resource pressure. Includes:
- LRUEvictionPolicy: Simple least-recently-used (default fallback).
- QoSEvictionPolicy: QoS-aware ordering (spot first, premium last) with
  preemption filtering for agent-level preemption scenarios.
"""

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from nexus.contracts.qos import EVICTION_ORDER, EvictionContext

if TYPE_CHECKING:
    from nexus.contracts.agent_types import AgentRecord


@runtime_checkable
class EvictionPolicy(Protocol):
    """Protocol for agent eviction candidate selection.

    Implementations decide which agents from a pre-sorted candidate list
    should actually be evicted. Optionally accepts an EvictionContext for
    QoS-aware decisions.
    """

    def select_candidates(
        self,
        agents: "list[AgentRecord]",
        batch_size: int,
        context: EvictionContext | None = None,
    ) -> "list[AgentRecord]":
        """Select which agents to evict from the candidates list.

        Args:
            agents: Candidate agents (may be pre-sorted by DB query).
            batch_size: Maximum number of agents to select.
            context: Optional eviction context for QoS-aware decisions.

        Returns:
            List of agents to evict (up to batch_size).
        """
        ...


class LRUEvictionPolicy:
    """Evict agents with oldest last_heartbeat first (fallback policy).

    The candidates are already sorted by the DB query (eviction_priority ASC,
    last_heartbeat ASC NULLS FIRST), so this policy simply slices the first
    batch_size agents. Context is accepted but ignored.
    """

    def select_candidates(
        self,
        agents: "list[AgentRecord]",
        batch_size: int,
        context: EvictionContext | None = None,  # noqa: ARG002
    ) -> "list[AgentRecord]":
        """Select least-recently-used agents for eviction.

        Args:
            agents: Pre-sorted candidate agents.
            batch_size: Maximum number of agents to select.
            context: Ignored by LRU policy.

        Returns:
            List of agents to evict (up to batch_size).
        """
        return agents[:batch_size]


class QoSEvictionPolicy:
    """QoS-aware eviction: spot first, standard next, premium last (Issue #2171).

    Sorts candidates by (eviction_class ascending, last_heartbeat ascending)
    so lower-QoS agents are evicted before higher-QoS ones.

    For preemption scenarios (context.requesting_agent_qos is set), only
    selects agents whose eviction_class is strictly lower than the
    requesting agent's QoS class.
    """

    def select_candidates(
        self,
        agents: "list[AgentRecord]",
        batch_size: int,
        context: EvictionContext | None = None,
    ) -> "list[AgentRecord]":
        """Select agents for eviction with QoS-aware ordering.

        Args:
            agents: Candidate agents (typically pre-sorted by DB query,
                but re-sorted here for safety).
            batch_size: Maximum number of agents to select.
            context: Optional context. When requesting_agent_qos is set,
                only agents with lower eviction priority are eligible.

        Returns:
            List of agents to evict (up to batch_size).
        """
        # Filter for preemption: only evict agents below the requester's QoS
        if context is not None and context.requesting_agent_qos is not None:
            requester_priority = EVICTION_ORDER.get(context.requesting_agent_qos, 1)
            candidates = [
                a
                for a in agents
                if EVICTION_ORDER.get(a.qos.eviction_class, 1) < requester_priority
            ]
        else:
            # Input is typically pre-sorted by DB query, but re-sort for safety
            # when called from non-DB contexts (e.g., unit tests with unsorted input).
            candidates = agents  # no copy needed — input is a fresh list from the registry

        # Sort by (eviction_priority ASC, last_heartbeat ASC with None first)
        candidates.sort(
            key=lambda a: (
                EVICTION_ORDER.get(a.qos.eviction_class, 1),
                (0 if a.last_heartbeat is None else 1, a.last_heartbeat),
            )
        )

        return candidates[:batch_size]
