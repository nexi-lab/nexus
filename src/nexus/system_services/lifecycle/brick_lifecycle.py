"""Brick lifecycle manager — mount/unmount/hook orchestration (Issue #1704).

Orchestrates the full brick lifecycle:
``REGISTER → MOUNT → USE → HOOK → LOG → UNMOUNT → UNREGISTER``

Lives at the System Service tier (not kernel) per Liedtke's test:
lifecycle orchestration CAN run outside the kernel.

Architecture decisions:
    - 7-state machine: REGISTERED→STARTING→ACTIVE→STOPPING→UNMOUNTED→UNREGISTERED + FAILED
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

import asyncio
import logging
import time
from collections import deque
from dataclasses import replace
from graphlib import CycleError, TopologicalSorter
from typing import Any

from nexus.contracts.protocols.brick_lifecycle import (
    EVENT_FAILED,
    EVENT_MOUNT,
    EVENT_RESET,
    EVENT_STARTED,
    EVENT_STOPPED,
    EVENT_UNMOUNT,
    EVENT_UNREGISTER,
    BrickHealthReport,
    BrickLifecycleProtocol,
    BrickSpec,
    BrickState,
    BrickStatus,
    ZoneDeprovisionReport,
    ZoneState,
)
from nexus.lib.tracing import lazy_tracer, record_span_result

logger = logging.getLogger(__name__)

# Default timeout for brick.start() in seconds
DEFAULT_START_TIMEOUT: float = 5.0

# Maximum number of state transitions to retain per brick
MAX_TRANSITION_HISTORY: int = 50

# ---------------------------------------------------------------------------
# OTel tracing — zero-overhead when telemetry is not enabled
# Shared implementation in nexus.lib.tracing
# ---------------------------------------------------------------------------

_get_tracer, _lifecycle_span = lazy_tracer("nexus.brick_lifecycle")
_record_span_result = record_span_result

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
    (BrickState.REGISTERED, EVENT_MOUNT): BrickState.STARTING,
    (BrickState.REGISTERED, EVENT_FAILED): BrickState.FAILED,  # Issue #2060: 5B
    (BrickState.STARTING, EVENT_STARTED): BrickState.ACTIVE,
    (BrickState.STARTING, EVENT_FAILED): BrickState.FAILED,
    (BrickState.ACTIVE, EVENT_UNMOUNT): BrickState.STOPPING,
    (BrickState.ACTIVE, EVENT_FAILED): BrickState.FAILED,
    (BrickState.STOPPING, EVENT_STOPPED): BrickState.UNMOUNTED,  # Issue #2363: was UNREGISTERED
    (BrickState.STOPPING, EVENT_FAILED): BrickState.FAILED,
    (BrickState.UNMOUNTED, EVENT_MOUNT): BrickState.STARTING,  # Issue #2363: re-mount
    (BrickState.UNMOUNTED, EVENT_UNREGISTER): BrickState.UNREGISTERED,  # Issue #2363: full removal
    (BrickState.UNMOUNTED, EVENT_FAILED): BrickState.FAILED,  # Issue #2363: error during unregister
    (BrickState.FAILED, EVENT_RESET): BrickState.REGISTERED,  # Issue #2060: 7A
}

# ---------------------------------------------------------------------------
# Internal mutable brick entry (not exposed outside this module)
# ---------------------------------------------------------------------------


class _BrickEntry:
    """Mutable internal tracking for a managed brick.

    External API only exposes frozen ``BrickStatus`` snapshots.
    The ``spec`` field is the frozen desired-state declaration (Issue #2060).
    """

    __slots__ = (
        "spec",
        "instance",
        "state",
        "error",
        "started_at",
        "stopped_at",
        "unmounted_at",
        "retry_count",
        "lock",
        "transitions",
    )

    def __init__(
        self,
        spec: BrickSpec,
        instance: Any,
    ) -> None:
        self.spec = spec
        self.instance = instance
        self.state = BrickState.REGISTERED
        self.error: str | None = None
        self.started_at: float | None = None
        self.stopped_at: float | None = None
        self.unmounted_at: float | None = None
        self.retry_count: int = 0
        self.lock = asyncio.Lock()
        self.transitions: deque[tuple[float, str, str, str]] = deque(maxlen=MAX_TRANSITION_HISTORY)

    # Convenience accessors (delegate to spec)
    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def protocol_name(self) -> str:
        return self.spec.protocol_name

    @property
    def depends_on(self) -> tuple[str, ...]:
        return self.spec.depends_on

    def to_status(self) -> BrickStatus:
        """Create an immutable snapshot of current state."""
        return BrickStatus(
            name=self.spec.name,
            state=self.state,
            protocol_name=self.spec.protocol_name,
            error=self.error,
            started_at=self.started_at,
            stopped_at=self.stopped_at,
            unmounted_at=self.unmounted_at,
        )

    def to_spec(self) -> BrickSpec:
        """Return the frozen BrickSpec."""
        return self.spec


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

    def __init__(self) -> None:
        self._bricks: dict[str, _BrickEntry] = {}
        self._zone_states: dict[str, ZoneState] = {}

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
        spec = BrickSpec(
            name=name,
            protocol_name=protocol_name,
            depends_on=tuple(depends_on),
        )
        self._bricks[name] = _BrickEntry(spec=spec, instance=instance)
        logger.info("[LIFECYCLE] Registered brick %r (protocol=%s)", name, protocol_name)

    async def unregister(self, name: str) -> None:
        """Unregister a brick: UNMOUNTED → UNREGISTERED (terminal).

        Args:
            name: Brick name to unregister.

        Raises:
            KeyError: If brick is not found.
            InvalidTransitionError: If brick is not in UNMOUNTED state.
        """
        entry = self._bricks.get(name)
        if entry is None:
            raise KeyError(f"Brick {name!r} not found")

        async with entry.lock:
            await self._do_unregister(entry)

    def _force_unregister(self, name: str) -> None:
        """Force-remove a brick from the registry (testing only, no guards/hooks)."""
        if name not in self._bricks:
            raise KeyError(f"Brick {name!r} not found")
        del self._bricks[name]
        logger.info("[LIFECYCLE] Force-unregistered brick %r", name)

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
        entry.transitions.append((time.monotonic(), event, old_state.name, new_state.name))
        logger.debug("[LIFECYCLE] %s: %s + %s → %s", name, old_state.name, event, new_state.name)
        return new_state

    def _force_state(self, name: str, state: BrickState) -> None:
        """Force a brick to a specific state (for testing only)."""
        entry = self._bricks.get(name)
        if entry is None:
            raise KeyError(f"Brick {name!r} not found")
        old_state = entry.state
        entry.state = state
        entry.transitions.append((time.monotonic(), "force", old_state.name, state.name))

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

    # ------------------------------------------------------------------
    # Reset (Issue #2060: 7A)
    # ------------------------------------------------------------------

    def reset(self, name: str) -> None:
        """Reset a FAILED brick to REGISTERED for retry.

        Clears error, timestamps, and retry counter so the reconciler
        (or a manual mount) can attempt to bring the brick back.

        Raises:
            KeyError: If brick not found.
            InvalidTransitionError: If brick is not in FAILED state.
        """
        entry = self._bricks.get(name)
        if entry is None:
            raise KeyError(f"Brick {name!r} not found")
        self._transition(name, EVENT_RESET)
        entry.error = None
        entry.started_at = None
        entry.stopped_at = None
        entry.unmounted_at = None
        entry.retry_count = 0

    # ------------------------------------------------------------------
    # Spec accessors (Issue #2060: 6C)
    # ------------------------------------------------------------------

    def get_spec(self, name: str) -> BrickSpec | None:
        """Return the frozen BrickSpec for a brick, or None if not found."""
        entry = self._bricks.get(name)
        return entry.spec if entry else None

    def all_specs(self) -> dict[str, BrickSpec]:
        """Return all brick specs keyed by name."""
        return {name: entry.spec for name, entry in self._bricks.items()}

    def iter_bricks(self) -> list[tuple[str, BrickSpec, BrickState, int, Any]]:
        """Iterate all bricks as (name, spec, state, retry_count, instance) tuples.

        Public API for the reconciler — avoids direct ``_bricks`` dict access.
        Returns a snapshot list (safe to iterate while mutations happen).
        """
        return [
            (name, entry.spec, entry.state, entry.retry_count, entry.instance)
            for name, entry in list(self._bricks.items())
        ]

    def get_retry_count(self, name: str) -> int:
        """Return the current retry count for a brick."""
        entry = self._bricks.get(name)
        if entry is None:
            raise KeyError(f"Brick {name!r} not found")
        return entry.retry_count

    def get_transitions(self, name: str) -> list[tuple[float, str, str, str]]:
        """Return transition history for a brick.

        Each tuple is (timestamp, event, from_state, to_state).

        Raises:
            KeyError: If brick not found.
        """
        entry = self._bricks.get(name)
        if entry is None:
            raise KeyError(f"Brick {name!r} not found")
        return list(entry.transitions)

    def update_spec(self, name: str, *, enabled: bool | None = None) -> BrickSpec:
        """Update a brick's spec fields. Returns the new spec.

        Only ``enabled`` is currently updatable. Uses ``dataclasses.replace()``
        for immutability — creates a new spec, doesn't mutate.

        Raises:
            KeyError: If brick not found.
        """
        entry = self._bricks.get(name)
        if entry is None:
            raise KeyError(f"Brick {name!r} not found")
        kwargs: dict[str, Any] = {}
        if enabled is not None:
            kwargs["enabled"] = enabled
        if kwargs:
            entry.spec = replace(entry.spec, **kwargs)
        return entry.spec

    def reset_for_retry(self, name: str) -> int:
        """Reset a FAILED brick for retry, preserving and incrementing retry_count.

        Unlike ``reset()`` (which clears retry_count to 0 for manual resets),
        this method increments the counter so the reconciler can track attempts.

        Returns the new retry_count after increment.

        Raises:
            KeyError: If brick not found.
            InvalidTransitionError: If brick is not in FAILED state.
        """
        entry = self._bricks.get(name)
        if entry is None:
            raise KeyError(f"Brick {name!r} not found")
        new_retry = entry.retry_count + 1
        self._transition(name, EVENT_RESET)
        entry.error = None
        entry.started_at = None
        entry.stopped_at = None
        entry.unmounted_at = None
        entry.retry_count = new_retry
        return new_retry

    def clear_retry_count(self, name: str) -> None:
        """Clear retry counter for a brick (called on successful mount)."""
        entry = self._bricks.get(name)
        if entry is None:
            raise KeyError(f"Brick {name!r} not found")
        entry.retry_count = 0

    def fail_brick(self, name: str, error: str) -> None:
        """Transition a brick to FAILED state with an error message.

        Used by the reconciler for health check failures. Only transitions
        if the brick is in a state that allows the 'failed' event.

        Raises:
            KeyError: If brick not found.
            InvalidTransitionError: If transition is not allowed.
        """
        entry = self._bricks.get(name)
        if entry is None:
            raise KeyError(f"Brick {name!r} not found")
        self._transition(name, EVENT_FAILED)
        entry.error = error

    # ------------------------------------------------------------------
    # Zone state tracking (Issue #2061)
    # ------------------------------------------------------------------

    def get_zone_state(self, zone_id: str) -> ZoneState:
        """Return the current state of a zone. Defaults to ACTIVE if unknown."""
        return self._zone_states.get(zone_id, ZoneState.ACTIVE)

    # ------------------------------------------------------------------
    # Zone deprovision (Issue #2061)
    # ------------------------------------------------------------------

    async def deprovision_zone(
        self,
        zone_id: str,
        *,
        grace_period: float = 30.0,
        max_concurrent_drain: int = 5,
    ) -> ZoneDeprovisionReport:
        """Orchestrate zone teardown: TERMINATING → drain → finalize → DESTROYED.

        Two-phase shutdown:
        1. **Drain**: Call ``drain(zone_id)`` on all zone-aware bricks in parallel,
           bounded by ``max_concurrent_drain`` semaphore.
        2. **Finalize**: Call ``finalize(zone_id)`` on all zone-aware bricks in
           reverse-DAG order (dependents before dependencies).

        On timeout at any phase: log warning, set ``forced=True``, proceed.
        Idempotent: returns a no-op report if the zone is already DESTROYED.

        Args:
            zone_id: Identifier of the zone to deprovision.
            grace_period: Max seconds for the entire operation.
            max_concurrent_drain: Max concurrent drain() calls.

        Returns:
            ZoneDeprovisionReport summarizing the operation.
        """
        # Idempotency guard
        current = self._zone_states.get(zone_id, ZoneState.ACTIVE)
        if current == ZoneState.DESTROYED:
            return ZoneDeprovisionReport(
                zone_id=zone_id,
                zone_state=ZoneState.DESTROYED,
                bricks_drained=0,
                bricks_finalized=0,
                drain_errors=0,
                finalize_errors=0,
                elapsed_seconds=0.0,
                forced=False,
            )

        t0 = time.monotonic()
        self._zone_states[zone_id] = ZoneState.TERMINATING
        forced = False

        # Find all zone-aware bricks (O(N) scan — optimize later)
        drainable: list[tuple[str, _BrickEntry]] = []
        finalizable: list[tuple[str, _BrickEntry]] = []
        for name, entry in self._bricks.items():
            if hasattr(entry.instance, "drain") and callable(entry.instance.drain):
                drainable.append((name, entry))
            if hasattr(entry.instance, "finalize") and callable(entry.instance.finalize):
                finalizable.append((name, entry))

        # Phase 1: Drain (parallel, semaphore-bounded)
        bricks_drained = 0
        drain_errors = 0
        half_grace = grace_period / 2

        if drainable:
            sem = asyncio.Semaphore(max_concurrent_drain)

            async def _drain_one(name: str, entry: _BrickEntry) -> bool:
                async with sem:
                    try:
                        await entry.instance.drain(zone_id)
                        return True
                    except Exception as exc:
                        logger.warning(
                            "[LIFECYCLE] drain(%r) failed for brick %r: %s",
                            zone_id,
                            name,
                            exc,
                        )
                        return False

            # Use asyncio.wait to preserve partial results on timeout
            tasks = {asyncio.create_task(_drain_one(n, e), name=f"drain-{n}") for n, e in drainable}
            done, pending = await asyncio.wait(tasks, timeout=half_grace)

            if pending:
                logger.warning(
                    "[LIFECYCLE] Drain phase timed out for zone %r after %.1fs (%d/%d completed)",
                    zone_id,
                    half_grace,
                    len(done),
                    len(tasks),
                )
                forced = True
                for t in pending:
                    t.cancel()

            for t in done:
                exc = t.exception()
                if exc is not None:
                    drain_errors += 1
                elif t.result() is True:
                    bricks_drained += 1
                else:
                    drain_errors += 1

        # Phase 2: Finalize (reverse-DAG order)
        bricks_finalized = 0
        finalize_errors = 0
        finalizable_names = {n for n, _ in finalizable}

        if finalizable:
            shutdown_levels = self.compute_shutdown_order()

            async def _finalize_one(name: str) -> bool:
                entry = self._bricks.get(name)
                if entry is None:
                    logger.warning(
                        "[LIFECYCLE] Brick %r disappeared during zone %r finalize",
                        name,
                        zone_id,
                    )
                    return False
                try:
                    await entry.instance.finalize(zone_id)
                    return True
                except Exception as exc:
                    logger.warning(
                        "[LIFECYCLE] finalize(%r) failed for brick %r: %s",
                        zone_id,
                        name,
                        exc,
                    )
                    return False

            remaining_grace = max(0.0, grace_period - (time.monotonic() - t0))

            async def _run_finalize_levels() -> tuple[int, int]:
                ok, err = 0, 0
                for level in shutdown_levels:
                    to_finalize = [n for n in level if n in finalizable_names]
                    if not to_finalize:
                        continue
                    results = await asyncio.gather(
                        *(_finalize_one(n) for n in to_finalize),
                        return_exceptions=True,
                    )
                    for r in results:
                        if isinstance(r, BaseException):
                            err += 1
                        elif r is True:
                            ok += 1
                        else:
                            err += 1
                return ok, err

            try:
                ok, err = await asyncio.wait_for(
                    _run_finalize_levels(),
                    timeout=remaining_grace,
                )
                bricks_finalized = ok
                finalize_errors = err
            except TimeoutError:
                logger.warning(
                    "[LIFECYCLE] Finalize phase timed out for zone %r",
                    zone_id,
                )
                forced = True
            except Exception as exc:
                logger.warning(
                    "[LIFECYCLE] Finalize phase error for zone %r: %s",
                    zone_id,
                    exc,
                )
                forced = True

        # Mark zone as DESTROYED
        self._zone_states[zone_id] = ZoneState.DESTROYED
        elapsed = time.monotonic() - t0

        report = ZoneDeprovisionReport(
            zone_id=zone_id,
            zone_state=ZoneState.DESTROYED,
            bricks_drained=bricks_drained,
            bricks_finalized=bricks_finalized,
            drain_errors=drain_errors,
            finalize_errors=finalize_errors,
            elapsed_seconds=elapsed,
            forced=forced,
        )

        logger.info(
            "[LIFECYCLE] Zone %r deprovisioned: drained=%d, finalized=%d, "
            "drain_errors=%d, finalize_errors=%d, forced=%s (%.3fs)",
            zone_id,
            bricks_drained,
            bricks_finalized,
            drain_errors,
            finalize_errors,
            forced,
            elapsed,
        )

        return report

    # ------------------------------------------------------------------
    # DAG ordering
    # ------------------------------------------------------------------

    def _deps_satisfied(self, name: str) -> bool:
        """Check if all dependencies of a brick are ACTIVE."""
        entry = self._bricks.get(name)
        if entry is None:
            return False
        for dep_name in entry.depends_on:
            dep = self._bricks.get(dep_name)
            if dep is None or dep.state != BrickState.ACTIVE:
                return False
        return True

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
    ) -> None:
        """Mount a single brick: REGISTERED/UNMOUNTED → STARTING → ACTIVE.

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
            InvalidTransitionError: If brick is not in REGISTERED or UNMOUNTED state.
        """
        entry = self._bricks.get(name)
        if entry is None:
            raise KeyError(f"Brick {name!r} not found")

        async with entry.lock:
            await self._do_mount(entry, timeout=timeout)

    async def _do_mount(
        self,
        entry: _BrickEntry,
        *,
        timeout: float,
    ) -> None:
        """Internal mount logic (must be called under entry.lock).

        Flow: transition → start().
        If start() fails, brick transitions to FAILED.
        """
        with _lifecycle_span(
            "mount", entry.name, protocol=entry.protocol_name, timeout=str(timeout)
        ) as span:
            # Transition: REGISTERED/UNMOUNTED → STARTING
            self._transition(entry.name, EVENT_MOUNT)

            is_lifecycle = isinstance(entry.instance, BrickLifecycleProtocol)

            if is_lifecycle:
                try:
                    await asyncio.wait_for(entry.instance.start(), timeout=timeout)
                    self._transition(entry.name, EVENT_STARTED)
                    entry.started_at = time.monotonic()
                    logger.info("[LIFECYCLE] Brick %r mounted (ACTIVE)", entry.name)
                except TimeoutError:
                    entry.error = f"Timeout after {timeout}s during start()"
                    self._transition(entry.name, EVENT_FAILED)
                    logger.warning("[LIFECYCLE] Brick %r FAILED: %s", entry.name, entry.error)
                    _record_span_result(span, state="FAILED", error=entry.error)
                    return  # Don't fire POST_MOUNT on failure
                except Exception as exc:
                    entry.error = str(exc)
                    self._transition(entry.name, EVENT_FAILED)
                    logger.warning("[LIFECYCLE] Brick %r FAILED: %s", entry.name, entry.error)
                    _record_span_result(span, state="FAILED", error=entry.error)
                    return  # Don't fire POST_MOUNT on failure
            else:
                # Stateless brick — skip start(), go directly to ACTIVE
                self._transition(entry.name, EVENT_STARTED)
                entry.started_at = time.monotonic()
                logger.info("[LIFECYCLE] Brick %r mounted (ACTIVE, stateless)", entry.name)

            _record_span_result(span, state=entry.state.value)

    async def remount(
        self,
        name: str,
        *,
        timeout: float = DEFAULT_START_TIMEOUT,
    ) -> None:
        """Re-mount an UNMOUNTED brick: UNMOUNTED → STARTING → ACTIVE.

        Convenience method that delegates to ``mount()``.

        Args:
            name: Brick name to remount.
            timeout: Maximum seconds to wait for ``start()`` to complete.

        Raises:
            KeyError: If brick not found.
            InvalidTransitionError: If brick is not in UNMOUNTED state.
        """
        entry = self._bricks.get(name)
        if entry is None:
            raise KeyError(f"Brick {name!r} not found")
        if entry.state != BrickState.UNMOUNTED:
            raise InvalidTransitionError(name, entry.state, EVENT_MOUNT)
        await self.mount(name, timeout=timeout)

    async def unmount(self, name: str) -> None:
        """Unmount a single brick: ACTIVE → STOPPING → UNMOUNTED.

        For lifecycle-aware bricks, calls ``stop()``. Stateless bricks
        transition directly to UNMOUNTED.

        The brick remains in the registry and can be re-mounted via
        ``mount()`` or ``remount()``.

        Args:
            name: Brick name to unmount.

        Raises:
            KeyError: If brick not found.
            InvalidTransitionError: If brick is not in ACTIVE state.
        """
        entry = self._bricks.get(name)
        if entry is None:
            raise KeyError(f"Brick {name!r} not found")

        async with entry.lock:
            await self._do_unmount(entry)

    async def _do_unmount(self, entry: _BrickEntry) -> None:
        """Internal unmount logic (must be called under entry.lock).

        Flow: transition → stop().
        If stop() fails, brick transitions to FAILED.
        Terminal state is UNMOUNTED (not UNREGISTERED — Issue #2363).
        """
        with _lifecycle_span("unmount", entry.name, protocol=entry.protocol_name) as span:
            # Transition: ACTIVE → STOPPING
            self._transition(entry.name, EVENT_UNMOUNT)

            is_lifecycle = isinstance(entry.instance, BrickLifecycleProtocol)

            if is_lifecycle:
                try:
                    await entry.instance.stop()
                    self._transition(entry.name, EVENT_STOPPED)
                    entry.stopped_at = time.monotonic()
                    entry.unmounted_at = time.monotonic()
                    logger.info("[LIFECYCLE] Brick %r unmounted (UNMOUNTED)", entry.name)
                except Exception as exc:
                    entry.error = str(exc)
                    self._transition(entry.name, EVENT_FAILED)
                    logger.warning("[LIFECYCLE] Brick %r FAILED during stop: %s", entry.name, exc)
                    _record_span_result(span, state="FAILED", error=entry.error)
                    return  # Don't fire POST_UNMOUNT on failure
            else:
                # Stateless brick — skip stop()
                self._transition(entry.name, EVENT_STOPPED)
                entry.stopped_at = time.monotonic()
                entry.unmounted_at = time.monotonic()
                logger.info("[LIFECYCLE] Brick %r unmounted (UNMOUNTED, stateless)", entry.name)

            _record_span_result(span, state=entry.state.value)

    async def _do_unregister(self, entry: _BrickEntry) -> None:
        """Internal unregister logic (must be called under entry.lock).

        Flow: transition → remove from registry.
        """
        with _lifecycle_span("unregister", entry.name, protocol=entry.protocol_name) as span:
            # Transition: UNMOUNTED → UNREGISTERED
            self._transition(entry.name, EVENT_UNREGISTER)

            # Remove from registry
            brick_name = entry.name
            del self._bricks[brick_name]
            logger.info("[LIFECYCLE] Brick %r unregistered (removed from registry)", brick_name)

            _record_span_result(span, state="UNREGISTERED")

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
            # Filter to only REGISTERED bricks whose dependencies are all ACTIVE.
            # This prevents mounting a brick whose dependency failed earlier.
            to_mount = [
                name
                for name in level
                if name in self._bricks
                and self._bricks[name].state in (BrickState.REGISTERED, BrickState.UNMOUNTED)
                and self._deps_satisfied(name)
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

    async def _safe_lifecycle_op(
        self,
        brick_name: str,
        op: str,
        coro: Any,
    ) -> None:
        """Execute a lifecycle coroutine, catching exceptions (fail-forward).

        On failure, transitions the brick to FAILED if it isn't already.
        """
        try:
            await coro
        except Exception as exc:
            logger.warning("[LIFECYCLE] Brick %r failed during %s: %s", brick_name, op, exc)
            entry = self._bricks.get(brick_name)
            if entry is not None and entry.state not in (
                BrickState.FAILED,
                BrickState.UNREGISTERED,
                BrickState.UNMOUNTED,
            ):
                self._transition(brick_name, EVENT_FAILED)
                entry.error = str(exc)

    async def _safe_mount(self, name: str, *, timeout: float) -> None:
        """Mount a brick, catching all exceptions (fail-forward)."""
        await self._safe_lifecycle_op(name, "mount", self.mount(name, timeout=timeout))

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
        await self._safe_lifecycle_op(name, "unmount", self.unmount(name))

    async def unregister_all(self) -> None:
        """Unregister all UNMOUNTED bricks (remove from registry).

        Iterates a snapshot of brick names to avoid dict-size-changed errors.
        """
        names = [
            name
            for name, entry in list(self._bricks.items())
            if entry.state == BrickState.UNMOUNTED
        ]
        for name in names:
            try:
                await self.unregister(name)
            except Exception as exc:
                logger.warning("[LIFECYCLE] Failed to unregister %r: %s", name, exc)

    async def shutdown_all(self) -> BrickHealthReport:
        """Full shutdown: unmount all ACTIVE bricks, then unregister all UNMOUNTED.

        Returns the health report after all operations complete.
        """
        report = await self.unmount_all()
        await self.unregister_all()
        return report
