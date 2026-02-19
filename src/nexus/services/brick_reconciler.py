"""Brick reconciler — drift detection and self-healing (Issue #2060).

Kubernetes-inspired reconciliation loop that continuously converges
actual brick state toward desired state (BrickSpec).

Architecture decisions:
    - Hybrid event+periodic detection (3C): periodic sweep every 30s,
      event-triggered on state transitions
    - FAILED→REGISTERED reset (7A) via lifecycle manager
    - Per-brick 2s timeout on health_check (13A)
    - Fixed 30s reconcile interval (14C)
    - Reuse per-brick lock from lifecycle manager (15A)
    - max_retries=3 before giving up (16A)

References:
    - Issue #2060: Brick Spec/Status Separation for Drift Detection
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

from nexus.services.protocols.brick_lifecycle import (
    BrickLifecycleProtocol,
    BrickSpec,
    BrickState,
    DriftReport,
    ReconcileResult,
)

logger = logging.getLogger(__name__)

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

        _tracer = get_tracer("nexus.brick_reconciler")
    except Exception:
        _tracer = None
    return _tracer


class BrickReconciler:
    """Drift detection and self-healing for bricks (Issue #2060).

    Kubernetes-inspired reconciliation loop:
    - Periodic sweep every ``reconcile_interval`` seconds
    - Event-triggered: immediate reconcile on ``notify_state_change()``
    - Self-healing: reset and remount FAILED bricks (max retries)
    - Health checks: ACTIVE bricks verified with per-brick timeout
    """

    def __init__(
        self,
        lifecycle_manager: Any,
        *,
        reconcile_interval: float = 30.0,
        health_check_timeout: float = 2.0,
        max_retries: int = 3,
    ) -> None:
        self._manager = lifecycle_manager
        self._reconcile_interval = reconcile_interval
        self._health_check_timeout = health_check_timeout
        self._max_retries = max_retries
        self._task: asyncio.Task[None] | None = None
        self._event = asyncio.Event()
        self._reconcile_count: int = 0
        self._stopped = False

    # ------------------------------------------------------------------
    # Loop lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the periodic reconciliation loop."""
        if self._task is not None and not self._task.done():
            return  # Already running
        self._stopped = False
        self._task = asyncio.create_task(self._loop(), name="brick-reconciler")
        logger.info(
            "[RECONCILER] Started (interval=%.0fs, max_retries=%d)",
            self._reconcile_interval,
            self._max_retries,
        )

    async def stop(self) -> None:
        """Stop the reconciliation loop."""
        self._stopped = True
        if self._task is None:
            return
        self._event.set()  # Wake up if sleeping
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        logger.info("[RECONCILER] Stopped")

    def notify_state_change(self, brick_name: str) -> None:
        """Event trigger: schedule immediate reconciliation."""
        logger.debug("[RECONCILER] State change notification for %r", brick_name)
        self._event.set()

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def _loop(self) -> None:
        """Periodic reconciliation loop with event-triggered wake."""
        while not self._stopped:
            try:
                # Wait for interval OR event trigger
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._event.wait(),
                        timeout=self._reconcile_interval,
                    )

                if self._stopped:
                    break

                self._event.clear()

                try:
                    result = await self.reconcile()
                    self._reconcile_count += 1
                    logger.info(
                        "[RECONCILER] Reconcile pass: %d bricks, %d drifted, %d actions, %d errors",
                        result.total_bricks,
                        result.drifted,
                        result.actions_taken,
                        result.errors,
                    )
                except Exception as exc:
                    self._reconcile_count += 1
                    logger.warning("[RECONCILER] Reconcile error: %s", exc)

            except asyncio.CancelledError:
                break

    # ------------------------------------------------------------------
    # Read-only drift detection (no side effects)
    # ------------------------------------------------------------------

    def detect_drift(self) -> ReconcileResult:
        """Detect drift between specs and statuses without taking actions.

        Safe for GET endpoints — no state transitions, no health checks,
        no mount/unmount/reset. Just compares spec vs current state.
        """
        bricks = self._manager._bricks
        drifts: list[DriftReport] = []

        for _name, entry in list(bricks.items()):
            drift = self._detect_drift(entry.spec, entry.state, entry)
            if drift is not None:
                drifts.append(drift)

        return ReconcileResult(
            total_bricks=len(bricks),
            drifted=len(drifts),
            actions_taken=0,
            errors=0,
            drifts=tuple(drifts),
        )

    # ------------------------------------------------------------------
    # Drift detection (internal)
    # ------------------------------------------------------------------

    def _detect_drift(
        self,
        spec: BrickSpec,
        state: BrickState,
        entry: Any,
    ) -> DriftReport | None:
        """Compare spec vs status, return drift report or None if converged."""
        if spec.enabled:
            if state == BrickState.FAILED:
                if entry.retry_count >= self._max_retries:
                    return DriftReport(
                        brick_name=spec.name,
                        spec_state="enabled",
                        actual_state=state,
                        action="skip",
                        detail=f"Max retries ({self._max_retries}) exceeded",
                    )
                return DriftReport(
                    brick_name=spec.name,
                    spec_state="enabled",
                    actual_state=state,
                    action="reset",
                    detail="Brick FAILED, will reset and remount",
                )
            if state == BrickState.REGISTERED:
                # Check if dependencies are met
                for dep_name in spec.depends_on:
                    dep_entry = self._manager._bricks.get(dep_name)
                    if dep_entry is None or dep_entry.state != BrickState.ACTIVE:
                        return DriftReport(
                            brick_name=spec.name,
                            spec_state="enabled",
                            actual_state=state,
                            action="skip",
                            detail=f"Dependency {dep_name!r} not ACTIVE",
                        )
                return DriftReport(
                    brick_name=spec.name,
                    spec_state="enabled",
                    actual_state=state,
                    action="mount",
                    detail="Brick REGISTERED but should be ACTIVE",
                )
            if state == BrickState.ACTIVE:
                # Will be checked for health in _take_action
                return None  # Handled separately below
        else:
            # spec.enabled is False
            if state == BrickState.ACTIVE:
                return DriftReport(
                    brick_name=spec.name,
                    spec_state="disabled",
                    actual_state=state,
                    action="unmount",
                    detail="Brick ACTIVE but spec says disabled",
                )

        return None

    # ------------------------------------------------------------------
    # Health checks (for ACTIVE bricks)
    # ------------------------------------------------------------------

    async def _health_check_brick(self, entry: Any) -> bool | None:
        """Call health_check() with per-brick timeout.

        Returns True if healthy, False if unhealthy, None if no health_check.
        Timeout = unhealthy.
        """
        if not isinstance(entry.instance, BrickLifecycleProtocol):
            return None  # Stateless brick — no health check

        try:
            result = await asyncio.wait_for(
                entry.instance.health_check(),
                timeout=self._health_check_timeout,
            )
            return bool(result)
        except TimeoutError:
            logger.warning(
                "[RECONCILER] Health check timeout for %r (%.1fs)",
                entry.name,
                self._health_check_timeout,
            )
            return False
        except Exception as exc:
            logger.warning("[RECONCILER] Health check error for %r: %s", entry.name, exc)
            return False

    # ------------------------------------------------------------------
    # Corrective actions
    # ------------------------------------------------------------------

    async def _take_action(
        self,
        name: str,
        entry: Any,
        drift: DriftReport,
    ) -> bool:
        """Execute the action prescribed by a DriftReport.

        Returns True if an action was taken.
        """
        action = drift.action

        if action == "skip":
            return False

        if action == "reset":
            return await self._action_reset_and_mount(name, entry)

        if action == "mount":
            return await self._action_mount(name)

        if action == "unmount":
            return await self._action_unmount(name)

        return False

    async def _action_reset_and_mount(self, name: str, entry: Any) -> bool:
        """Reset a FAILED brick and attempt to remount it."""
        try:
            retry = entry.retry_count + 1
            self._manager.reset(name)  # Clears retry_count to 0
            entry.retry_count = retry  # Restore incremented count
            await self._manager.mount(name)
            # Success — clear retry counter
            if entry.state == BrickState.ACTIVE:
                entry.retry_count = 0
                logger.info("[RECONCILER] Self-healed brick %r", name)
            return True
        except Exception as exc:
            logger.warning(
                "[RECONCILER] Failed to reset+mount %r (retry %d/%d): %s",
                name,
                entry.retry_count,
                self._max_retries,
                exc,
            )
            return True  # Action was attempted

    async def _action_mount(self, name: str) -> bool:
        """Mount a REGISTERED brick."""
        try:
            await self._manager.mount(name)
            logger.info("[RECONCILER] Mounted drifted brick %r", name)
            return True
        except Exception as exc:
            logger.warning("[RECONCILER] Failed to mount %r: %s", name, exc)
            return True

    async def _action_unmount(self, name: str) -> bool:
        """Unmount a disabled brick."""
        try:
            await self._manager.unmount(name)
            logger.info("[RECONCILER] Unmounted disabled brick %r", name)
            return True
        except Exception as exc:
            logger.warning("[RECONCILER] Failed to unmount %r: %s", name, exc)
            return True

    # ------------------------------------------------------------------
    # Single reconciliation pass
    # ------------------------------------------------------------------

    async def reconcile(self) -> ReconcileResult:
        """Single reconciliation pass: health checks + drift detection + actions.

        For each brick:
        1. If spec.enabled and status.state == ACTIVE:
           → call health_check() with timeout; if unhealthy → transition to FAILED
        2. If spec.enabled and status.state == FAILED and retry_count < max_retries:
           → reset + mount (self-healing)
        3. If spec.enabled and status.state == REGISTERED:
           → mount (brick should be active but isn't)
        4. If not spec.enabled and status.state == ACTIVE:
           → unmount (spec says disabled but brick is running)
        """
        tracer = _get_tracer()
        span = None
        if tracer is not None:
            span = tracer.start_span("reconciler.reconcile")

        t0 = time.monotonic()
        drifts: list[DriftReport] = []
        actions_taken = 0
        errors = 0

        bricks = self._manager._bricks

        # Track bricks that just failed health check — don't auto-heal same pass
        health_failed: set[str] = set()

        # Phase 1: Health check ACTIVE bricks
        for name, entry in list(bricks.items()):
            if entry.spec.enabled and entry.state == BrickState.ACTIVE:
                healthy = await self._health_check_brick(entry)
                if healthy is False:
                    # Transition to FAILED via public API
                    try:
                        self._manager.fail_brick(name, "Health check failed")
                        health_failed.add(name)
                        drifts.append(
                            DriftReport(
                                brick_name=name,
                                spec_state="enabled",
                                actual_state=BrickState.FAILED,
                                action="health_check_failed",
                                detail="Brick failed health check",
                            )
                        )
                        actions_taken += 1
                    except Exception as exc:
                        errors += 1
                        logger.warning(
                            "[RECONCILER] Failed to transition %r to FAILED: %s",
                            name,
                            exc,
                        )

        # Phase 2: Detect drift and take corrective actions
        for name, entry in list(bricks.items()):
            # Skip bricks that just failed health check — let next pass handle retry
            if name in health_failed:
                continue
            spec = entry.spec
            state = entry.state

            drift = self._detect_drift(spec, state, entry)
            if drift is None:
                continue

            drifts.append(drift)

            try:
                acted = await self._take_action(name, entry, drift)
                if acted:
                    actions_taken += 1
            except Exception as exc:
                errors += 1
                logger.warning("[RECONCILER] Action failed for %r: %s", name, exc)

        elapsed = time.monotonic() - t0
        if span is not None:
            span.set_attribute("reconciler.total_bricks", len(bricks))
            span.set_attribute("reconciler.drifted", len(drifts))
            span.set_attribute("reconciler.elapsed_ms", elapsed * 1000)
            span.end()

        return ReconcileResult(
            total_bricks=len(bricks),
            drifted=len(drifts),
            actions_taken=actions_taken,
            errors=errors,
            drifts=tuple(drifts),
        )
