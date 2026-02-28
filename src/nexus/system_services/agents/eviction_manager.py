"""Eviction manager orchestrating resource-pressure agent eviction (Issues #2170, #2171).

Composes ResourceMonitor + EvictionPolicy + AgentRegistry to implement the
eviction pipeline: check pressure -> select candidates -> checkpoint -> evict.

Follows Orleans watermark-based eviction pattern:
- Start evicting above high_watermark
- Stop evicting below low_watermark
- Cooldown between cycles to prevent thrashing

Issue #2171 additions:
- Async signal (_urgent_event) for immediate preemption cycles.
- EvictionContext propagation to QoS-aware policies.
- trigger_immediate_cycle() for agent-level preemption.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from nexus.contracts.agent_types import EvictionReason
from nexus.contracts.qos import EVICTION_ORDER, EvictionContext, PressureLevel, QoSClass

if TYPE_CHECKING:
    from nexus.contracts.agent_types import AgentRecord
    from nexus.lib.performance_tuning import EvictionTuning
    from nexus.system_services.agents.agent_registry import AgentRegistry
    from nexus.system_services.agents.eviction_policy import EvictionPolicy
    from nexus.system_services.agents.resource_monitor import ResourceMonitor

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

    Issue #2171: Adds an asyncio.Event for immediate preemption cycles
    triggered by premium agent registration when at capacity.

    Args:
        registry: AgentRegistry for state transitions and checkpoints.
        monitor: ResourceMonitor for pressure detection.
        policy: EvictionPolicy for candidate selection.
        tuning: EvictionTuning with thresholds and batch sizes.
    """

    def __init__(
        self,
        registry: "AgentRegistry",
        monitor: "ResourceMonitor",
        policy: "EvictionPolicy",
        tuning: "EvictionTuning",
    ) -> None:
        self._registry = registry
        self._monitor = monitor
        self._policy = policy
        self._tuning = tuning
        self._last_eviction: float = 0.0
        self._transition_semaphore = asyncio.Semaphore(tuning.max_concurrent_transitions)
        # Async signal for immediate preemption (Issue #2171).
        # Uses Event + Queue: Event for wake-up, Queue for thread-safe QoS delivery.
        self._urgent_event = asyncio.Event()
        self._urgent_queue: asyncio.Queue[QoSClass | None] = asyncio.Queue()

    @property
    def urgent_event(self) -> asyncio.Event:
        """Expose the urgent event for the background task loop."""
        return self._urgent_event

    def trigger_immediate_cycle(self, requesting_qos: QoSClass | None = None) -> None:
        """Signal an immediate eviction cycle for preemption (Issue #2171).

        Coroutine-safe (single event loop). Uses asyncio.Queue to deliver the
        requesting QoS class, avoiding race conditions between concurrent callers.
        NOT thread-safe — call from the event loop only.

        Args:
            requesting_qos: QoS class of the agent requesting resources.
                Used to filter candidates (only evict lower-priority agents).
        """
        self._urgent_queue.put_nowait(requesting_qos)
        self._urgent_event.set()
        logger.info(
            "[EVICTION] Immediate cycle triggered (requesting_qos=%s)",
            requesting_qos,
        )

    async def run_cycle(self, context: EvictionContext | None = None) -> EvictionResult:
        """Execute one eviction cycle.

        Pipeline:
        1. Check resource pressure (+ agent cap as secondary trigger)
        2. Check cooldown
        3. Get candidates from registry (QoS-aware order)
        4. Apply policy filter (with EvictionContext)
        5. Batch checkpoint state (with timeout enforcement)
        6. Transition concurrently to SUSPENDED (with CAS safety + semaphore)
        7. Re-check pressure after eviction (skipped for over_cap)

        Args:
            context: Optional EvictionContext for QoS-aware decisions.
                If None, context is built from current pressure state.

        Returns:
            EvictionResult with eviction counts and reason.
        """
        from nexus.contracts.agent_types import AgentState
        from nexus.system_services.agents.agent_registry import (
            InvalidTransitionError,
            StaleAgentError,
        )

        # Consume urgent event if set, drain queue for highest-priority requester
        requesting_qos: QoSClass | None = None
        if self._urgent_event.is_set():
            self._urgent_event.clear()
            # Drain all queued requests; pick the highest-priority requester
            while not self._urgent_queue.empty():
                try:
                    qos = self._urgent_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if qos is not None and (
                    requesting_qos is None
                    or EVICTION_ORDER.get(qos, 1) > EVICTION_ORDER.get(requesting_qos, 1)
                ):
                    requesting_qos = qos

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
            elif requesting_qos is not None:
                # Preemption trigger: at cap, premium needs slot
                over_cap = True
                logger.info(
                    "[EVICTION] Preemption triggered for %s agent at cap %d",
                    requesting_qos,
                    self._tuning.max_active_agents,
                )
            else:
                return EvictionResult(evicted=0, reason=EvictionReason.NORMAL_PRESSURE)

        # 2. Check cooldown (skip for preemption triggers)
        if requesting_qos is None and self._in_cooldown():
            return EvictionResult(evicted=0, reason=EvictionReason.COOLDOWN)

        # Build EvictionContext if not provided
        if context is None:
            reason = (
                EvictionReason.OVER_AGENT_CAP
                if over_cap
                else _PRESSURE_TO_REASON.get(pressure, EvictionReason.PRESSURE_WARNING)
            )
            context = EvictionContext(
                pressure=pressure,
                trigger=reason,
                requesting_agent_qos=requesting_qos,
            )

        # 3. Get candidates via to_thread (sync registry)
        candidates = await asyncio.to_thread(
            self._registry.list_eviction_candidates,
            batch_size=self._tuning.eviction_batch_size,
        )
        if not candidates:
            return EvictionResult(evicted=0, reason=EvictionReason.NO_CANDIDATES)

        # 4. Apply policy (may filter/reorder based on QoS context)
        selected = self._policy.select_candidates(
            candidates, self._tuning.eviction_batch_size, context=context
        )
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
        async def _transition_one(agent: "AgentRecord") -> bool:
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

        assert context is not None  # Built above if not provided
        return EvictionResult(
            evicted=evicted,
            reason=context.trigger,
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
        from nexus.system_services.agents.agent_registry import (
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
    def _build_checkpoint(agent: "AgentRecord") -> dict[str, Any]:
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
