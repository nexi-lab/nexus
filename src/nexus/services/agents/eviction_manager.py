"""Eviction manager orchestrating resource-pressure agent eviction (Issue #2170).

Composes ResourceMonitor + EvictionPolicy + AgentRegistry to implement the
eviction pipeline: check pressure -> select candidates -> checkpoint -> evict.

Follows Orleans watermark-based eviction pattern:
- Start evicting above high_watermark
- Stop evicting below low_watermark
- Cooldown between cycles to prevent thrashing
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from nexus.services.agents.resource_monitor import PressureLevel

if TYPE_CHECKING:
    from nexus.contracts.agent_types import AgentRecord
    from nexus.core.performance_tuning import EvictionTuning
    from nexus.services.agents.agent_registry import AgentRegistry
    from nexus.services.agents.eviction_policy import EvictionPolicy
    from nexus.services.agents.resource_monitor import ResourceMonitor

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EvictionResult:
    """Result of a single eviction cycle."""

    evicted: int
    reason: str
    post_pressure: str = "unknown"
    skipped: int = 0


class EvictionManager:
    """Orchestrate agent eviction under resource pressure.

    Composes:
    - ResourceMonitor: checks memory pressure
    - EvictionPolicy: selects which agents to evict
    - AgentRegistry: manages agent state transitions and checkpoints

    Args:
        registry: AgentRegistry for state transitions and checkpoints.
        monitor: ResourceMonitor for pressure detection.
        policy: EvictionPolicy for candidate selection.
        tuning: EvictionTuning with thresholds and batch sizes.
    """

    def __init__(
        self,
        registry: AgentRegistry,
        monitor: ResourceMonitor,
        policy: EvictionPolicy,
        tuning: EvictionTuning,
    ) -> None:
        self._registry = registry
        self._monitor = monitor
        self._policy = policy
        self._tuning = tuning
        self._last_eviction: float = 0.0

    async def run_cycle(self) -> EvictionResult:
        """Execute one eviction cycle.

        Pipeline:
        1. Check resource pressure (+ agent cap as secondary trigger)
        2. Check cooldown
        3. Get candidates from registry (LRU order)
        4. Apply policy filter
        5. Batch checkpoint state (with timeout enforcement)
        6. Transition concurrently to SUSPENDED (with CAS safety)
        7. Re-check pressure after eviction

        Returns:
            EvictionResult with eviction counts and reason.
        """
        from nexus.contracts.agent_types import AgentState
        from nexus.services.agents.agent_registry import (
            InvalidTransitionError,
            StaleAgentError,
        )

        # 1. Check resource pressure
        pressure = await self._monitor.check_pressure()

        # 1b. Check max_active_agents cap (secondary trigger, lightweight COUNT)
        over_cap = False
        if pressure is PressureLevel.NORMAL:
            connected_count = await asyncio.to_thread(
                self._registry.count_connected_agents,
            )
            if connected_count > self._tuning.max_active_agents:
                over_cap = True
                logger.info(
                    "[EVICTION] Agent count %d exceeds cap %d, triggering eviction",
                    connected_count,
                    self._tuning.max_active_agents,
                )
            else:
                return EvictionResult(evicted=0, reason="normal_pressure")

        # 2. Check cooldown
        if self._in_cooldown():
            return EvictionResult(evicted=0, reason="cooldown")

        # 3. Get candidates via to_thread (sync registry)
        candidates = await asyncio.to_thread(
            self._registry.list_eviction_candidates,
            batch_size=self._tuning.eviction_batch_size,
        )
        if not candidates:
            return EvictionResult(evicted=0, reason="no_candidates")

        # 4. Apply policy (may filter/reorder)
        selected = self._policy.select_candidates(candidates, self._tuning.eviction_batch_size)
        if not selected:
            return EvictionResult(evicted=0, reason="no_candidates")

        # 5. Batch checkpoint with timeout enforcement
        checkpoint_data = {a.agent_id: self._build_checkpoint(a) for a in selected}
        try:
            await asyncio.wait_for(
                asyncio.to_thread(self._registry.batch_checkpoint, checkpoint_data),
                timeout=self._tuning.checkpoint_timeout_seconds,
            )
        except TimeoutError:
            logger.warning(
                "[EVICTION] Checkpoint timed out after %.1fs for %d agents",
                self._tuning.checkpoint_timeout_seconds,
                len(selected),
            )
            return EvictionResult(evicted=0, reason="checkpoint_timeout")

        # 6. Transition concurrently to SUSPENDED (individual for CAS safety)
        async def _transition_one(agent: AgentRecord) -> bool:
            try:
                await asyncio.to_thread(
                    self._registry.transition,
                    agent.agent_id,
                    AgentState.SUSPENDED,
                    expected_generation=agent.generation,
                )
                return True
            except (InvalidTransitionError, StaleAgentError):
                return False

        results = await asyncio.gather(*[_transition_one(a) for a in selected])
        evicted = sum(1 for ok in results if ok)
        skipped = len(results) - evicted

        # 7. Update cooldown
        self._last_eviction = time.monotonic()

        # 8. Re-check pressure after eviction
        post_pressure = await self._monitor.check_pressure()

        reason = "over_agent_cap" if over_cap else f"pressure_{pressure.value}"
        return EvictionResult(
            evicted=evicted,
            reason=reason,
            post_pressure=post_pressure.value,
            skipped=skipped,
        )

    def _in_cooldown(self) -> bool:
        """Check if we're within the cooldown period."""
        if self._last_eviction == 0.0:
            return False
        elapsed = time.monotonic() - self._last_eviction
        return elapsed < self._tuning.eviction_cooldown_seconds

    @staticmethod
    def _build_checkpoint(agent: AgentRecord) -> dict[str, Any]:
        """Build checkpoint data for an agent before eviction.

        Captures the essential state needed to restore the agent on
        reconnection.

        Args:
            agent: Agent record to checkpoint.

        Returns:
            Dict of checkpoint data.
        """
        return {
            "state": agent.state.value,
            "generation": agent.generation,
            "last_heartbeat": (agent.last_heartbeat.isoformat() if agent.last_heartbeat else None),
            "evicted_at": time.time(),
        }
