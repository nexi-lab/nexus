"""Brick lifecycle manager — mount/unmount/hook orchestration (Issue #1704).

Orchestrates the full brick lifecycle:
``REGISTER → MOUNT → USE → HOOK → LOG → UNMOUNT → UNREGISTER``

Lives at the System Service tier (not kernel) per Liedtke's test:
lifecycle orchestration CAN run outside the kernel.

Architecture decisions:
    - 5-state machine: REGISTERED→STARTING→ACTIVE→STOPPING→UNREGISTERED + FAILED
    - Composes existing BrickRegistry + BrickContainer (no replacement)
    - Explicit DAG via ``graphlib.TopologicalSorter`` for startup/shutdown order
    - Fail-forward: one brick failure doesn't block others
    - Per-brick ``asyncio.Lock`` for safe runtime hot-swap
    - Per-brick timeout (default 5s) on ``start()``

References:
    - docs/design/NEXUS-LEGO-ARCHITECTURE.md §3.2 (Brick Lifecycle)
    - docs/design/NEXUS-LEGO-ARCHITECTURE.md §7 (Linux Kernel Lessons)
    - Issue #1704: Brick lifecycle manager
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from graphlib import CycleError, TopologicalSorter
from typing import Any

from nexus.services.protocols.brick_lifecycle import (
    POST_MOUNT,
    POST_UNMOUNT,
    PRE_MOUNT,
    PRE_UNMOUNT,
    BrickHealthReport,
    BrickLifecycleProtocol,
    BrickState,
    BrickStatus,
)
from nexus.services.protocols.hook_engine import (
    HookContext,
    HookEngineProtocol,
)

logger = logging.getLogger(__name__)

# Default timeout for brick.start() in seconds
DEFAULT_START_TIMEOUT: float = 5.0


# ---------------------------------------------------------------------------
# OTel tracing — zero-overhead when telemetry is not enabled
# ---------------------------------------------------------------------------

_tracer: Any = None
_tracer_resolved: bool = False


def _get_tracer() -> Any:
    """Lazy-resolve the OTel tracer (returns None if unavailable)."""
    global _tracer, _tracer_resolved  # noqa: PLW0603
    if _tracer_resolved:
        return _tracer
    _tracer_resolved = True
    try:
        from nexus.server.telemetry import get_tracer

        _tracer = get_tracer("nexus.brick_lifecycle")
    except Exception:
        _tracer = None
    return _tracer


@contextmanager
def _lifecycle_span(operation: str, brick_name: str, **attrs: Any) -> Generator[Any, None, None]:
    """Context manager for a brick lifecycle OTel span.

    Zero-overhead: if no tracer, yields None immediately.
    """
    tracer = _get_tracer()
    if tracer is None:
        yield None
        return
    with tracer.start_as_current_span(f"brick.{operation}") as span:
        span.set_attribute("brick.name", brick_name)
        for k, v in attrs.items():
            span.set_attribute(f"brick.{k}", v)
        yield span


def _record_span_result(span: Any, *, state: str, error: str | None = None) -> None:
    """Record final state and optional error on a span."""
    if span is None:
        return
    span.set_attribute("brick.state", state)
    if error:
        span.set_attribute("brick.error", error)
        try:
            from opentelemetry.trace import StatusCode

            span.set_status(StatusCode.ERROR, error)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class InvalidTransitionError(Exception):
    """Raised when a state transition is not allowed."""

    def __init__(self, brick_name: str, current_state: BrickState, event: str) -> None:
        self.brick_name = brick_name
        self.current_state = current_state
        self.event = event
        super().__init__(
            f"Invalid transition for brick {brick_name!r}: "
            f"{current_state.name} + {event!r} is not allowed"
        )


class CyclicDependencyError(Exception):
    """Raised when brick dependencies form a cycle."""

    def __init__(self, message: str = "Cyclic dependency detected in brick DAG") -> None:
        super().__init__(message)


# ---------------------------------------------------------------------------
# Transition table — the state machine definition
# ---------------------------------------------------------------------------

# Maps (current_state, event) → next_state
_TRANSITIONS: dict[tuple[BrickState, str], BrickState] = {
    (BrickState.REGISTERED, "mount"): BrickState.STARTING,
    (BrickState.STARTING, "started"): BrickState.ACTIVE,
    (BrickState.STARTING, "failed"): BrickState.FAILED,
    (BrickState.ACTIVE, "unmount"): BrickState.STOPPING,
    (BrickState.ACTIVE, "failed"): BrickState.FAILED,
    (BrickState.STOPPING, "stopped"): BrickState.UNREGISTERED,
    (BrickState.STOPPING, "failed"): BrickState.FAILED,
    # Recovery path: reconciler resets a failed brick for re-mount (Issue #2059)
    (BrickState.FAILED, "reset"): BrickState.REGISTERED,
}


# ---------------------------------------------------------------------------
# Internal mutable brick entry (not exposed outside this module)
# ---------------------------------------------------------------------------


class _BrickEntry:
    """Mutable internal tracking for a managed brick.

    External API only exposes frozen ``BrickStatus`` snapshots.
    """

    __slots__ = (
        "name",
        "instance",
        "protocol_name",
        "state",
        "error",
        "started_at",
        "stopped_at",
        "depends_on",
        "lock",
    )

    def __init__(
        self,
        name: str,
        instance: Any,
        protocol_name: str,
        depends_on: tuple[str, ...] = (),
    ) -> None:
        self.name = name
        self.instance = instance
        self.protocol_name = protocol_name
        self.state = BrickState.REGISTERED
        self.error: str | None = None
        self.started_at: float | None = None
        self.stopped_at: float | None = None
        self.depends_on = depends_on
        self.lock = asyncio.Lock()

    def to_status(self) -> BrickStatus:
        """Create an immutable snapshot of current state."""
        return BrickStatus(
            name=self.name,
            state=self.state,
            protocol_name=self.protocol_name,
            error=self.error,
            started_at=self.started_at,
            stopped_at=self.stopped_at,
        )


# ---------------------------------------------------------------------------
# BrickLifecycleManager
# ---------------------------------------------------------------------------


class BrickLifecycleManager:
    """Orchestrates brick lifecycle: register, mount, use, unmount, unregister.

    Thread-safe for reads after initialization. Runtime mount/unmount uses
    per-brick ``asyncio.Lock`` to prevent concurrent transitions on the
    same brick.

    Usage::

        manager = BrickLifecycleManager()
        manager.register("search", search_brick, protocol_name="SearchProtocol")
        manager.register("rag", rag_brick, protocol_name="RAGProtocol",
                         depends_on=("search", "llm"))
        await manager.mount_all()   # DAG-ordered concurrent start
        ...
        await manager.unmount_all() # Reverse-DAG-ordered shutdown
    """

    # Type alias for the state-change callback (Issue #2059).
    # Signature: (brick_name, old_state, new_state) → None
    StateChangeCallback = Callable[[str, BrickState, BrickState], None]

    def __init__(
        self,
        *,
        hook_engine: HookEngineProtocol | None = None,
        on_state_change: StateChangeCallback | None = None,
    ) -> None:
        self._bricks: dict[str, _BrickEntry] = {}
        self._hook_engine = hook_engine
        self.on_state_change = on_state_change

    # ------------------------------------------------------------------
    # Registration (synchronous, boot-time)
    # ------------------------------------------------------------------

    def register(
        self,
        name: str,
        instance: Any,
        *,
        protocol_name: str,
        depends_on: tuple[str, ...] | list[str] = (),
    ) -> None:
        """Register a brick for lifecycle management.

        Args:
            name: Unique brick name.
            instance: Brick instance (may or may not satisfy BrickLifecycleProtocol).
            protocol_name: Human-readable protocol name for reporting.
            depends_on: Names of bricks this brick depends on.

        Raises:
            ValueError: If a brick with this name is already registered.
        """
        if name in self._bricks:
            raise ValueError(f"Brick {name!r} already registered")
        self._bricks[name] = _BrickEntry(
            name=name,
            instance=instance,
            protocol_name=protocol_name,
            depends_on=tuple(depends_on),
        )
        logger.info("[LIFECYCLE] Registered brick %r (protocol=%s)", name, protocol_name)

    def unregister(self, name: str) -> None:
        """Remove a brick from lifecycle management.

        Raises:
            KeyError: If brick is not found.
        """
        if name not in self._bricks:
            raise KeyError(f"Brick {name!r} not found")
        del self._bricks[name]
        logger.info("[LIFECYCLE] Unregistered brick %r", name)

    # ------------------------------------------------------------------
    # State machine
    # ------------------------------------------------------------------

    def _transition(self, name: str, event: str) -> BrickState:
        """Apply a state transition event to a brick.

        Returns the new state.

        Raises:
            InvalidTransitionError: If the transition is not allowed.
            KeyError: If brick not found.
        """
        entry = self._bricks.get(name)
        if entry is None:
            raise KeyError(f"Brick {name!r} not found")

        key = (entry.state, event)
        new_state = _TRANSITIONS.get(key)
        if new_state is None:
            raise InvalidTransitionError(name, entry.state, event)

        old_state = entry.state
        entry.state = new_state

        # Clear error on reset (recovery path)
        if event == "reset":
            entry.error = None

        logger.debug("[LIFECYCLE] %s: %s + %s → %s", name, old_state.name, event, new_state.name)

        # Fire state-change callback for reconciler (Issue #2059)
        if self.on_state_change is not None:
            try:
                self.on_state_change(name, old_state, new_state)
            except Exception:
                logger.warning("[LIFECYCLE] on_state_change callback failed for %s", name)

        return new_state

    def _force_state(self, name: str, state: BrickState) -> None:
        """Force a brick to a specific state (for testing only)."""
        entry = self._bricks.get(name)
        if entry is None:
            raise KeyError(f"Brick {name!r} not found")
        entry.state = state

    # ------------------------------------------------------------------
    # Hook integration
    # ------------------------------------------------------------------

    async def _fire_hook(
        self,
        phase: str,
        entry: _BrickEntry,
        *,
        veto_keeps_current: bool = False,
        agent_id: str | None = None,
    ) -> bool:
        """Fire a lifecycle hook via the HookEngine.

        Returns True if the operation should proceed, False if vetoed.
        When no hook engine is configured, always returns True.

        Args:
            phase: The lifecycle phase (PRE_MOUNT, POST_MOUNT, etc.)
            entry: The brick entry to include in hook context.
            veto_keeps_current: If True, a veto does NOT mark the brick as FAILED
                (used for PRE_UNMOUNT where the brick should stay ACTIVE).
            agent_id: Agent requesting the operation (explicit propagation).
        """
        if self._hook_engine is None:
            return True

        context = HookContext(
            phase=phase,
            path=None,
            zone_id=None,
            agent_id=agent_id,
            payload={
                "brick_name": entry.name,
                "protocol_name": entry.protocol_name,
                "state": entry.state.value,
            },
        )

        try:
            result = await self._hook_engine.fire(phase, context)
        except Exception as exc:
            logger.warning(
                "[LIFECYCLE] Hook fire failed for %s on brick %r: %s",
                phase,
                entry.name,
                exc,
            )
            # Hook failure doesn't block the operation
            return True

        if not result.proceed:
            error_msg = result.error or f"Vetoed by {phase} hook"
            logger.warning("[LIFECYCLE] Brick %r vetoed by %s: %s", entry.name, phase, error_msg)
            if not veto_keeps_current:
                entry.state = BrickState.FAILED
                entry.error = error_msg
            return False

        return True

    # ------------------------------------------------------------------
    # Status & health
    # ------------------------------------------------------------------

    def get_status(self, name: str) -> BrickStatus | None:
        """Return an immutable snapshot of a brick's lifecycle state."""
        entry = self._bricks.get(name)
        if entry is None:
            return None
        return entry.to_status()

    def health(self) -> BrickHealthReport:
        """Generate an aggregated health report for all managed bricks."""
        statuses = tuple(entry.to_status() for entry in self._bricks.values())
        active = sum(1 for s in statuses if s.state == BrickState.ACTIVE)
        failed = sum(1 for s in statuses if s.state == BrickState.FAILED)
        return BrickHealthReport(
            total=len(statuses),
            active=active,
            failed=failed,
            bricks=statuses,
        )

    def get_active_brick_names(self) -> list[str]:
        """Return names of all ACTIVE bricks (lightweight, no snapshot allocation)."""
        return [name for name, entry in self._bricks.items() if entry.state == BrickState.ACTIVE]

    # ------------------------------------------------------------------
    # Health checking & recovery (Issue #2059)
    # ------------------------------------------------------------------

    async def check_health(self, name: str, *, timeout: float = 5.0) -> bool:
        """Invoke ``health_check()`` on a single ACTIVE brick.

        If health_check returns False or raises, transitions the brick to
        FAILED and returns False.  Stateless bricks (no health_check method)
        are always considered healthy.

        Returns True if the brick is healthy, False otherwise.
        Silently returns True for bricks not in ACTIVE state (no-op).
        """
        entry = self._bricks.get(name)
        if entry is None:
            return True

        is_lifecycle = isinstance(entry.instance, BrickLifecycleProtocol)
        if not is_lifecycle:
            return True  # Stateless bricks are always healthy

        async with entry.lock:
            # Re-check state inside lock to avoid TOCTOU race (CRITICAL-2)
            if entry.state != BrickState.ACTIVE:
                return True

            try:
                healthy = await asyncio.wait_for(entry.instance.health_check(), timeout=timeout)
            except Exception as exc:
                entry.error = f"health_check raised: {exc}"
                self._transition(name, "failed")
                logger.warning("[LIFECYCLE] Brick %r FAILED health check: %s", name, entry.error)
                return False

            if not healthy:
                entry.error = "health_check returned False"
                self._transition(name, "failed")
                logger.warning("[LIFECYCLE] Brick %r FAILED health check: unhealthy", name)
                return False

        return True

    def reset(self, name: str) -> None:
        """Reset a FAILED brick to REGISTERED state for re-mount.

        This is the recovery path used by the reconciler.

        Raises:
            KeyError: If brick not found.
            InvalidTransitionError: If brick is not in FAILED state.
        """
        self._transition(name, "reset")
        logger.info("[LIFECYCLE] Brick %r reset (FAILED → REGISTERED)", name)

    # ------------------------------------------------------------------
    # DAG ordering
    # ------------------------------------------------------------------

    def compute_startup_order(self) -> list[list[str]]:
        """Compute DAG-ordered startup levels.

        Returns a list of levels, where each level is a list of brick names
        that can be started concurrently. Level N depends on level N-1.

        Raises:
            CyclicDependencyError: If dependencies form a cycle.
            KeyError: If a dependency references an unregistered brick.
        """
        # Validate all dependencies exist
        for entry in self._bricks.values():
            for dep in entry.depends_on:
                if dep not in self._bricks:
                    raise KeyError(
                        f"Brick {entry.name!r} depends on {dep!r} which is not registered"
                    )

        # Build the graph for TopologicalSorter
        graph: dict[str, set[str]] = {}
        for name, entry in self._bricks.items():
            graph[name] = set(entry.depends_on)

        try:
            sorter = TopologicalSorter(graph)
            sorter.prepare()
        except CycleError as exc:
            raise CyclicDependencyError(str(exc)) from exc

        levels: list[list[str]] = []
        while sorter.is_active():
            level = sorted(sorter.get_ready())  # sorted for deterministic ordering
            levels.append(level)
            for name in level:
                sorter.done(name)

        return levels

    def compute_shutdown_order(self) -> list[list[str]]:
        """Compute DAG-ordered shutdown levels (reverse of startup)."""
        startup = self.compute_startup_order()
        return list(reversed(startup))

    # ------------------------------------------------------------------
    # Mount / unmount single brick
    # ------------------------------------------------------------------

    async def mount(
        self,
        name: str,
        *,
        timeout: float = DEFAULT_START_TIMEOUT,
        agent_id: str | None = None,
    ) -> None:
        """Mount a single brick: REGISTERED → STARTING → ACTIVE.

        For lifecycle-aware bricks (implementing BrickLifecycleProtocol),
        calls ``start()`` with the configured timeout. Stateless bricks
        transition directly to ACTIVE.

        On failure, the brick transitions to FAILED with the error recorded.

        Args:
            name: Brick name to mount.
            timeout: Maximum seconds to wait for ``start()`` to complete.
            agent_id: Agent requesting the mount (for scoped hooks).

        Raises:
            KeyError: If brick not found.
            InvalidTransitionError: If brick is not in REGISTERED state.
        """
        entry = self._bricks.get(name)
        if entry is None:
            raise KeyError(f"Brick {name!r} not found")

        async with entry.lock:
            await self._do_mount(entry, timeout=timeout, agent_id=agent_id)

    async def _do_mount(
        self,
        entry: _BrickEntry,
        *,
        timeout: float,
        agent_id: str | None = None,
    ) -> None:
        """Internal mount logic (must be called under entry.lock).

        Flow: PRE_MOUNT hook → transition → start() → POST_MOUNT hook.
        If PRE_MOUNT vetoes, brick transitions to FAILED.
        If start() fails, POST_MOUNT is NOT fired.
        """
        with _lifecycle_span(
            "mount", entry.name, protocol=entry.protocol_name, timeout=str(timeout)
        ) as span:
            # Fire PRE_MOUNT hook — may veto
            if not await self._fire_hook(PRE_MOUNT, entry, agent_id=agent_id):
                _record_span_result(span, state="FAILED", error="Vetoed by PRE_MOUNT hook")
                return  # Vetoed — entry already marked FAILED

            # Transition: REGISTERED → STARTING
            self._transition(entry.name, "mount")

            is_lifecycle = isinstance(entry.instance, BrickLifecycleProtocol)

            if is_lifecycle:
                try:
                    await asyncio.wait_for(entry.instance.start(), timeout=timeout)
                    self._transition(entry.name, "started")
                    entry.started_at = time.monotonic()
                    logger.info("[LIFECYCLE] Brick %r mounted (ACTIVE)", entry.name)
                except TimeoutError:
                    entry.error = f"Timeout after {timeout}s during start()"
                    self._transition(entry.name, "failed")
                    logger.warning("[LIFECYCLE] Brick %r FAILED: %s", entry.name, entry.error)
                    _record_span_result(span, state="FAILED", error=entry.error)
                    return  # Don't fire POST_MOUNT on failure
                except Exception as exc:
                    entry.error = str(exc)
                    self._transition(entry.name, "failed")
                    logger.warning("[LIFECYCLE] Brick %r FAILED: %s", entry.name, entry.error)
                    _record_span_result(span, state="FAILED", error=entry.error)
                    return  # Don't fire POST_MOUNT on failure
            else:
                # Stateless brick — skip start(), go directly to ACTIVE
                self._transition(entry.name, "started")
                entry.started_at = time.monotonic()
                logger.info("[LIFECYCLE] Brick %r mounted (ACTIVE, stateless)", entry.name)

            # Fire POST_MOUNT hook (informational — no veto check)
            await self._fire_hook(POST_MOUNT, entry, agent_id=agent_id)
            _record_span_result(span, state=entry.state.value)

    async def unmount(self, name: str, *, agent_id: str | None = None) -> None:
        """Unmount a single brick: ACTIVE → STOPPING → UNREGISTERED.

        For lifecycle-aware bricks, calls ``stop()``. Stateless bricks
        transition directly to UNREGISTERED.

        Args:
            name: Brick name to unmount.
            agent_id: Agent requesting the unmount (for hook scoping).

        Raises:
            KeyError: If brick not found.
            InvalidTransitionError: If brick is not in ACTIVE state.
        """
        entry = self._bricks.get(name)
        if entry is None:
            raise KeyError(f"Brick {name!r} not found")

        async with entry.lock:
            await self._do_unmount(entry, agent_id=agent_id)

    async def _do_unmount(self, entry: _BrickEntry, *, agent_id: str | None = None) -> None:
        """Internal unmount logic (must be called under entry.lock).

        Flow: PRE_UNMOUNT hook → transition → stop() → POST_UNMOUNT hook.
        If PRE_UNMOUNT vetoes, brick stays ACTIVE.
        If stop() fails, POST_UNMOUNT is NOT fired.

        Args:
            agent_id: Agent requesting the unmount (for hook scoping).
        """
        with _lifecycle_span("unmount", entry.name, protocol=entry.protocol_name) as span:
            # Fire PRE_UNMOUNT hook — may veto (brick stays ACTIVE)
            if not await self._fire_hook(
                PRE_UNMOUNT, entry, veto_keeps_current=True, agent_id=agent_id
            ):
                _record_span_result(span, state="ACTIVE", error="Vetoed by PRE_UNMOUNT hook")
                return  # Vetoed — brick stays in current state

            # Transition: ACTIVE → STOPPING
            self._transition(entry.name, "unmount")

            is_lifecycle = isinstance(entry.instance, BrickLifecycleProtocol)

            if is_lifecycle:
                try:
                    await entry.instance.stop()
                    self._transition(entry.name, "stopped")
                    entry.stopped_at = time.monotonic()
                    logger.info("[LIFECYCLE] Brick %r unmounted (UNREGISTERED)", entry.name)
                except Exception as exc:
                    entry.error = str(exc)
                    self._transition(entry.name, "failed")
                    logger.warning("[LIFECYCLE] Brick %r FAILED during stop: %s", entry.name, exc)
                    _record_span_result(span, state="FAILED", error=entry.error)
                    return  # Don't fire POST_UNMOUNT on failure
            else:
                # Stateless brick — skip stop()
                self._transition(entry.name, "stopped")
                entry.stopped_at = time.monotonic()
                logger.info("[LIFECYCLE] Brick %r unmounted (UNREGISTERED, stateless)", entry.name)

            # Fire POST_UNMOUNT hook (informational)
            await self._fire_hook(POST_UNMOUNT, entry, agent_id=agent_id)
            _record_span_result(span, state=entry.state.value)

    # ------------------------------------------------------------------
    # Mount all / unmount all (DAG-ordered, concurrent per level)
    # ------------------------------------------------------------------

    async def mount_all(self, *, timeout: float = DEFAULT_START_TIMEOUT) -> BrickHealthReport:
        """Mount all registered bricks in DAG-ordered levels.

        Bricks at the same level start concurrently via ``asyncio.TaskGroup``.
        One brick's failure does not prevent others from starting (fail-forward).

        Returns the health report after all mounts complete.
        """
        t0 = time.monotonic()
        levels = self.compute_startup_order()
        for level in levels:
            # Filter to only REGISTERED bricks (skip already-mounted or failed)
            to_mount = [
                name
                for name in level
                if name in self._bricks and self._bricks[name].state == BrickState.REGISTERED
            ]
            if not to_mount:
                continue

            # Concurrent mount within this level
            async with asyncio.TaskGroup() as tg:
                for name in to_mount:
                    tg.create_task(self._safe_mount(name, timeout=timeout))

        report = self.health()
        elapsed = time.monotonic() - t0
        logger.info(
            "[LIFECYCLE] mount_all: %d/%d active, %d failed (%.3fs)",
            report.active,
            report.total,
            report.failed,
            elapsed,
        )
        return report

    async def _safe_mount(self, name: str, *, timeout: float) -> None:
        """Mount a brick, catching all exceptions (fail-forward)."""
        try:
            await self.mount(name, timeout=timeout)
        except Exception as exc:
            logger.warning("[LIFECYCLE] Brick %r failed during mount_all: %s", name, exc)
            # Ensure the brick is in FAILED state (use _transition for callback)
            entry = self._bricks.get(name)
            if entry is not None and entry.state not in (
                BrickState.FAILED,
                BrickState.UNREGISTERED,
            ):
                entry.error = str(exc)
                try:
                    self._transition(name, "failed")
                except InvalidTransitionError:
                    # Fallback: force state and fire callback manually
                    old_state = entry.state
                    entry.state = BrickState.FAILED
                    if self.on_state_change is not None:
                        with contextlib.suppress(Exception):
                            self.on_state_change(name, old_state, BrickState.FAILED)

    async def unmount_all(self) -> BrickHealthReport:
        """Unmount all ACTIVE bricks in reverse-DAG order.

        Returns the health report after all unmounts complete.
        """
        levels = self.compute_shutdown_order()
        for level in levels:
            to_unmount = [
                name
                for name in level
                if name in self._bricks and self._bricks[name].state == BrickState.ACTIVE
            ]
            if not to_unmount:
                continue

            async with asyncio.TaskGroup() as tg:
                for name in to_unmount:
                    tg.create_task(self._safe_unmount(name))

        return self.health()

    async def _safe_unmount(self, name: str) -> None:
        """Unmount a brick, catching all exceptions (fail-forward)."""
        try:
            await self.unmount(name)
        except Exception as exc:
            logger.warning("[LIFECYCLE] Brick %r failed during unmount_all: %s", name, exc)
            # Ensure the brick is in FAILED state (use _transition for callback)
            entry = self._bricks.get(name)
            if entry is not None and entry.state not in (
                BrickState.FAILED,
                BrickState.UNREGISTERED,
            ):
                entry.error = str(exc)
                try:
                    self._transition(name, "failed")
                except InvalidTransitionError:
                    # Fallback: force state and fire callback manually
                    old_state = entry.state
                    entry.state = BrickState.FAILED
                    if self.on_state_change is not None:
                        with contextlib.suppress(Exception):
                            self.on_state_change(name, old_state, BrickState.FAILED)
