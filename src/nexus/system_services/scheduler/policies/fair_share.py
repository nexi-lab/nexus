"""Per-agent fair-share admission control (Issue #1274).

In-memory counter that tracks running tasks per agent and enforces
max_concurrent limits. Syncs from DB on startup for crash recovery.

No I/O — the sync method accepts pre-fetched data.
"""

from dataclasses import dataclass

from cachetools import LRUCache


@dataclass(frozen=True, slots=True)
class FairShareSnapshot:
    """Immutable snapshot of an agent's fair-share state."""

    agent_id: str
    running_count: int
    max_concurrent: int

    @property
    def available_slots(self) -> int:
        return max(0, self.max_concurrent - self.running_count)

    @property
    def is_at_capacity(self) -> bool:
        return self.running_count >= self.max_concurrent


# Default max concurrent tasks per agent
_DEFAULT_MAX_CONCURRENT = 10


class FairShareCounter:
    """In-memory per-agent concurrency tracker.

    Thread-safety: This is designed for single-threaded asyncio use.
    For multi-process deployments, use DB-backed counters.
    """

    def __init__(
        self,
        *,
        default_max_concurrent: int = _DEFAULT_MAX_CONCURRENT,
        max_agents: int = 4096,
    ) -> None:
        self._running: LRUCache[str, int] = LRUCache(maxsize=max_agents)
        self._limits: LRUCache[str, int] = LRUCache(maxsize=max_agents)
        self._default_max = default_max_concurrent

    def _get_limit(self, agent_id: str) -> int:
        return self._limits.get(agent_id, self._default_max)

    def admit(self, agent_id: str) -> bool:
        """Check if an agent can accept another task (without incrementing).

        Args:
            agent_id: Agent to check.

        Returns:
            True if the agent has available capacity.
        """
        current = self._running.get(agent_id, 0)
        return current < self._get_limit(agent_id)

    def record_start(self, agent_id: str) -> None:
        """Record that a task started executing on this agent."""
        self._running[agent_id] = self._running.get(agent_id, 0) + 1

    def record_complete(self, agent_id: str) -> None:
        """Record that a task completed on this agent."""
        current = self._running.get(agent_id, 0)
        self._running[agent_id] = max(0, current - 1)

    def set_limit(self, agent_id: str, max_concurrent: int) -> None:
        """Set the max concurrent tasks for an agent.

        Args:
            agent_id: Agent to configure.
            max_concurrent: Maximum concurrent tasks (must be >= 1).

        Raises:
            ValueError: If max_concurrent < 1.
        """
        if max_concurrent < 1:
            raise ValueError(f"max_concurrent must be >= 1, got {max_concurrent}")
        self._limits[agent_id] = max_concurrent

    def sync_from_db(self, running_counts: dict[str, int]) -> None:
        """Bulk-load running counts from database on startup.

        Replaces all current counters with the provided values.

        Args:
            running_counts: Mapping of agent_id → running task count.
        """
        self._running.clear()
        self._running.update(running_counts)

    def snapshot(self, agent_id: str) -> FairShareSnapshot:
        """Get a snapshot of an agent's fair-share state."""
        return FairShareSnapshot(
            agent_id=agent_id,
            running_count=self._running.get(agent_id, 0),
            max_concurrent=self._get_limit(agent_id),
        )

    def all_snapshots(self) -> dict[str, FairShareSnapshot]:
        """Get snapshots for all known agents."""
        all_agents = set(self._running.keys()) | set(self._limits.keys())
        return {agent_id: self.snapshot(agent_id) for agent_id in sorted(all_agents)}
