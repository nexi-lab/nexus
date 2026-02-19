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

from nexus.contracts.agent_types import EvictionReason
from nexus.services.agents.resource_monitor import PressureLevel

if TYPE_CHECKING:
    from nexus.contracts.agent_types import AgentRecord
    from nexus.core.performance_tuning import EvictionTuning
    from nexus.services.agents.agent_registry import AgentRegistry
    from nexus.services.agents.eviction_policy import EvictionPolicy
    from nexus.services.agents.resource_monitor import ResourceMonitor

logger = logging.getLogger(__name__)

_PRESSURE_TO_REASON: dict[PressureLevel, EvictionReason] = {
    PressureLevel.WARNING: EvictionReason.PRESSURE_WARNING,
    PressureLevel.CRITICAL: EvictionReason.PRESSURE_CRITICAL,
}


@dataclass(frozen=True)
class EvictionResult:
    """Result of a single eviction cycle."""

    evicted: int
    reason: EvictionReason
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
        self._transition_semaphore = asyncio.Semaphore(tuning.max_concurrent_transitions)

    async def run_cycle(self) -> EvictionResult:
        """Execute one eviction cycle.

        Pipeline:
        1. Check resource pressure (+ agent cap as secondary trigger)
        2. Check cooldown
        3. Get candidates from registry (LRU order)
        4. Apply policy filter
        5. Batch checkpoint state (with timeout enforcement)
        6. Transition concurrently to SUSPENDED (with CAS safety + semaphore)
        7. Re-check pressure after eviction (skipped for over_cap)

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
                return EvictionResult(evicted=0, reason=EvictionReason.NORMAL_PRESSURE)

        # 2. Check cooldown
        if self._in_cooldown():
            return EvictionResult(evicted=0, reason=EvictionReason.COOLDOWN)

        # 3. Get candidates via to_thread (sync registry)
        candidates = await asyncio.to_thread(
            self._registry.list_eviction_candidates,
            batch_size=self._tuning.eviction_batch_size,
        )
        if not candidates:
            return EvictionResult(evicted=0, reason=EvictionReason.NO_CANDIDATES)

        # 4. Apply policy (may filter/reorder)
        selected = self._policy.select_candidates(candidates, self._tuning.eviction_batch_size)
        if not selected:
            return EvictionResult(evicted=0, reason=EvictionReason.NO_CANDIDATES)

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
            return EvictionResult(evicted=0, reason=EvictionReason.CHECKPOINT_TIMEOUT)

        # 6. Transition concurrently to SUSPENDED (semaphore-bounded, CAS-safe)
        async def _transition_one(agent: AgentRecord) -> bool:
            async with self._transition_semaphore:
                try:
                    await asyncio.to_thread(
                        self._registry.transition,
                        agent.agent_id,
                        AgentState.SUSPENDED,
                        expected_generation=agent.generation,
                    )
                    logger.debug(
                        "[EVICTION] Transitioned agent %s to SUSPENDED (gen=%d)",
                        agent.agent_id,
                        agent.generation,
                    )
                    return True
                except (InvalidTransitionError, StaleAgentError) as exc:
                    logger.info(
                        "[EVICTION] Skipping agent %s: %s",
                        agent.agent_id,
                        exc,
                    )
                    return False
                except Exception:
                    logger.exception(
                        "[EVICTION] Unexpected error transitioning agent %s",
                        agent.agent_id,
                    )
                    return False

        results = await asyncio.gather(*[_transition_one(a) for a in selected])
        evicted = sum(1 for ok in results if ok)
        skipped = len(results) - evicted

        # 7. Update cooldown
        self._last_eviction = time.monotonic()

        # 8. Re-check pressure after eviction (skip for over_cap — pressure is NORMAL)
        if over_cap:
            post_pressure_value = "normal"
        else:
            post_pressure = await self._monitor.check_pressure()
            post_pressure_value = post_pressure.value

        reason = (
            EvictionReason.OVER_AGENT_CAP
            if over_cap
            else _PRESSURE_TO_REASON.get(pressure, EvictionReason.PRESSURE_WARNING)
        )
        return EvictionResult(
            evicted=evicted,
            reason=reason,
            post_pressure=post_pressure_value,
            skipped=skipped,
        )

    async def evict_agent(self, agent_id: str) -> EvictionResult:
        """Manually evict a single agent (for REST API endpoint).

        Bypasses pressure checks and cooldown. Still checkpoints and
        uses CAS transition for safety.

        Args:
            agent_id: Agent to evict.

        Returns:
            EvictionResult with eviction outcome.
        """
        from nexus.contracts.agent_types import AgentState
        from nexus.services.agents.agent_registry import (
            InvalidTransitionError,
            StaleAgentError,
        )

        record = await asyncio.to_thread(self._registry.get, agent_id)
        if record is None:
            raise ValueError(f"Agent '{agent_id}' not found")
        if record.state is not AgentState.CONNECTED:
            raise ValueError(f"Agent '{agent_id}' is {record.state.value}, not CONNECTED")

        # Checkpoint
        checkpoint_data = self._build_checkpoint(record)
        await asyncio.to_thread(self._registry.checkpoint, agent_id, checkpoint_data)

        # Transition
        try:
            await asyncio.to_thread(
                self._registry.transition,
                agent_id,
                AgentState.SUSPENDED,
                expected_generation=record.generation,
            )
        except (InvalidTransitionError, StaleAgentError) as exc:
            logger.info("[EVICTION] Manual eviction skipped for %s: %s", agent_id, exc)
            return EvictionResult(evicted=0, reason=EvictionReason.MANUAL, skipped=1)

        logger.info("[EVICTION] Manually evicted agent %s", agent_id)
        return EvictionResult(evicted=1, reason=EvictionReason.MANUAL)

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
