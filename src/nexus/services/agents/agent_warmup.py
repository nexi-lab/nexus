"""Agent warmup service — structured initialization before accepting work (Issue #2172).

Server-orchestrated warmup that runs a sequence of steps for an agent,
gating the UNKNOWN → CONNECTED transition on required step success.

Design decisions (from review):
    - 1A: Server-orchestrated (server runs all steps)
    - 2C: UNKNOWN→CONNECTED gate (no new AgentState)
    - 3A: Step registry + callables (ParserRegistry pattern)
    - 13A: Sequential execution with per-step timeouts
    - 14A: Session-per-operation (existing DB pattern)
    - 16A: New warmup_and_connect() method (transition() unchanged)

Edge cases handled:
    - Re-warmup on already-CONNECTED agent → skip (idempotent)
    - Required step timeout → WarmupResult with failed_step
    - Required step exception → WarmupResult with error
    - All optional steps fail → still transitions CONNECTED
    - Agent unregistered during warmup → clean failure
    - Concurrent warmup → optimistic locking via expected_generation
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from nexus.contracts.agent_warmup_types import (
    STANDARD_WARMUP,
    WarmupContext,
    WarmupResult,
    WarmupStep,
)
from nexus.contracts.process_types import (
    AgentError,
    AgentSignal,
    AgentState,
    InvalidTransitionError,
)

if TYPE_CHECKING:
    from nexus.services.agents.agent_registry import AgentRegistry

logger = logging.getLogger(__name__)

# Type alias for warmup step functions.
WarmupStepFn = Callable[[WarmupContext], Awaitable[bool]]


class AgentWarmupService:
    """Server-orchestrated agent warmup with step registry.

    Follows the ParserRegistry pattern: step names map to async callables.
    New steps are registered via ``register_step()``, and the executor
    iterates the step list calling the registry.

    Args:
        agent_registry: AgentRegistry for state queries and transitions.
        namespace_manager: Optional NamespaceManager for mount resolution.
        enabled_bricks: Set of brick names enabled in this deployment.
        cache_store: Optional CacheStoreABC for cache warming.
        mcp_config: Optional MCP server configuration.
    """

    def __init__(
        self,
        agent_registry: "AgentRegistry",
        namespace_manager: Any | None = None,
        enabled_bricks: frozenset[str] | None = None,
        cache_store: Any | None = None,
        mcp_config: dict[str, Any] | None = None,
    ) -> None:
        self._agent_registry = agent_registry
        self._namespace_manager = namespace_manager
        self._enabled_bricks = enabled_bricks or frozenset()
        self._cache_store = cache_store
        self._mcp_config = mcp_config
        self._step_registry: dict[str, WarmupStepFn] = {}

    def register_step(self, name: str, fn: WarmupStepFn) -> None:
        """Register a warmup step function.

        Args:
            name: Step name (must match WarmupStep.name).
            fn: Async callable that receives WarmupContext and returns bool.

        Raises:
            ValueError: If a step with this name is already registered.
        """
        if name in self._step_registry:
            raise ValueError(f"Warmup step '{name}' is already registered")
        self._step_registry[name] = fn

    def get_step(self, name: str) -> WarmupStepFn | None:
        """Look up a registered step function by name.

        Args:
            name: Step name.

        Returns:
            The step function, or None if not registered.
        """
        return self._step_registry.get(name)

    async def warmup(
        self,
        agent_id: str,
        steps: list[WarmupStep] | None = None,
    ) -> WarmupResult:
        """Execute warmup steps for an agent.

        Runs each step sequentially with per-step timeouts. Required steps
        that fail abort warmup. Optional steps that fail are logged and
        skipped. On success, transitions agent from UNKNOWN → CONNECTED.

        Args:
            agent_id: The agent to warm up.
            steps: Warmup steps to execute. If None, uses STANDARD_WARMUP.

        Returns:
            WarmupResult with success status and step-level detail.
        """
        if steps is None:
            steps = list(STANDARD_WARMUP)

        start = time.monotonic()

        # Edge case 1: Verify agent exists
        record = self._agent_registry.get(agent_id)
        if record is None:
            return WarmupResult(
                success=False,
                agent_id=agent_id,
                error=f"Agent '{agent_id}' not found",
                duration_ms=_elapsed_ms(start),
            )

        # Edge case 2: Already CONNECTED → skip (idempotent)
        if record.state is AgentState.BUSY:
            logger.info("[WARMUP] Agent %s already BUSY, skipping warmup", agent_id)
            return WarmupResult(
                success=True,
                agent_id=agent_id,
                duration_ms=_elapsed_ms(start),
            )

        if record.state is AgentState.REGISTERED:
            try:
                record = self._agent_registry._transition(record, AgentState.WARMING_UP)
            except (InvalidTransitionError, AgentError) as exc:
                logger.warning(
                    "[WARMUP] Failed to transition agent %s into WARMING_UP: %s",
                    agent_id,
                    exc,
                )
                return WarmupResult(
                    success=False,
                    agent_id=agent_id,
                    error=str(exc),
                    duration_ms=_elapsed_ms(start),
                )

        # Edge case 5: Empty step list → immediate transition
        if not steps:
            return await self._transition_connected(agent_id, record.generation, start, (), ())

        # Build context for step functions
        ctx = WarmupContext(
            agent_id=agent_id,
            agent_record=record,
            agent_registry=self._agent_registry,
            namespace_manager=self._namespace_manager,
            enabled_bricks=self._enabled_bricks,
            cache_store=self._cache_store,
            mcp_config=self._mcp_config,
        )

        completed: list[str] = []
        skipped: list[str] = []

        for step in steps:
            step_fn = self._step_registry.get(step.name)
            if step_fn is None:
                # Unregistered step: skip if optional, fail if required
                if step.required:
                    logger.error(
                        "[WARMUP] Required step '%s' not registered for agent %s",
                        step.name,
                        agent_id,
                    )
                    return WarmupResult(
                        success=False,
                        agent_id=agent_id,
                        steps_completed=tuple(completed),
                        steps_skipped=tuple(skipped),
                        failed_step=step.name,
                        error=f"Required step '{step.name}' is not registered",
                        duration_ms=_elapsed_ms(start),
                    )
                logger.debug("[WARMUP] Optional step '%s' not registered, skipping", step.name)
                skipped.append(step.name)
                continue

            # Execute step with timeout
            success = await self._execute_step(step, step_fn, ctx)

            if success:
                completed.append(step.name)
            elif step.required:
                # Required step failed → abort
                return WarmupResult(
                    success=False,
                    agent_id=agent_id,
                    steps_completed=tuple(completed),
                    steps_skipped=tuple(skipped),
                    failed_step=step.name,
                    error=f"Required step '{step.name}' failed",
                    duration_ms=_elapsed_ms(start),
                )
            else:
                # Optional step failed → skip and continue
                skipped.append(step.name)

        # All required steps passed → transition to CONNECTED
        return await self._transition_connected(
            agent_id, record.generation, start, tuple(completed), tuple(skipped)
        )

    async def _execute_step(
        self,
        step: WarmupStep,
        step_fn: WarmupStepFn,
        ctx: WarmupContext,
    ) -> bool:
        """Execute a single warmup step with timeout.

        Returns True on success, False on failure (timeout or exception).
        """
        timeout_secs = step.timeout.total_seconds()
        try:
            result = await asyncio.wait_for(step_fn(ctx), timeout=timeout_secs)
            if result:
                logger.debug("[WARMUP] Step '%s' completed for agent %s", step.name, ctx.agent_id)
            else:
                logger.warning(
                    "[WARMUP] Step '%s' returned False for agent %s", step.name, ctx.agent_id
                )
            return bool(result)
        except TimeoutError:
            logger.warning(
                "[WARMUP] Step '%s' timed out after %.1fs for agent %s",
                step.name,
                timeout_secs,
                ctx.agent_id,
            )
            return False
        except Exception:
            logger.exception(
                "[WARMUP] Step '%s' raised exception for agent %s", step.name, ctx.agent_id
            )
            return False

    async def _transition_connected(
        self,
        agent_id: str,
        expected_generation: int,
        start: float,
        completed: tuple[str, ...],
        skipped: tuple[str, ...],
    ) -> WarmupResult:
        """Transition agent to CONNECTED after successful warmup.

        Handles edge cases:
        - Agent unregistered during warmup (ValueError)
        - Concurrent warmup (StaleAgentError / InvalidTransitionError)
        """
        try:
            # CAS check: verify generation hasn't changed
            current = self._agent_registry.get(agent_id)
            if current is None:
                raise ValueError(f"Agent '{agent_id}' not found")
            if current.generation != expected_generation:
                raise InvalidTransitionError(
                    f"stale generation for {agent_id}: expected {expected_generation}, got {current.generation}"
                )
            self._agent_registry.signal(agent_id, AgentSignal.SIGCONT)
        except (ValueError, InvalidTransitionError, AgentError) as exc:
            logger.warning("[WARMUP] Failed to transition agent %s to CONNECTED: %s", agent_id, exc)
            return WarmupResult(
                success=False,
                agent_id=agent_id,
                steps_completed=completed,
                steps_skipped=skipped,
                error=str(exc),
                duration_ms=_elapsed_ms(start),
            )

        elapsed = _elapsed_ms(start)
        logger.info(
            "[WARMUP] Agent %s warmed up in %.1fms (%d completed, %d skipped)",
            agent_id,
            elapsed,
            len(completed),
            len(skipped),
        )
        return WarmupResult(
            success=True,
            agent_id=agent_id,
            steps_completed=completed,
            steps_skipped=skipped,
            duration_ms=elapsed,
        )


def _elapsed_ms(start: float) -> float:
    """Compute elapsed milliseconds since start (monotonic)."""
    return (time.monotonic() - start) * 1000.0
