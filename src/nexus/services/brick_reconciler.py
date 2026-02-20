"""Brick reconciler — drift detection and self-healing (Issue #2060).

Kubernetes-inspired reconciliation loop that continuously converges
actual brick state toward desired state (BrickSpec).

Architecture decisions:
    - Hybrid event+periodic detection (3C): periodic sweep every 30s,
      event-triggered on state transitions
    - FAILED→REGISTERED reset (7A) via lifecycle manager
    - Per-brick 2s timeout on health_check (13A)
    - Fixed 30s reconcile interval with jitter (14C)
    - Reuse per-brick lock from lifecycle manager (15A)
    - max_retries=3 with exponential backoff before giving up (16A)

References:
    - Issue #2060: Brick Spec/Status Separation for Drift Detection
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from typing import Any

from nexus.services._tracing import lazy_tracer
from nexus.services.protocols.brick_lifecycle import (
    BrickLifecycleProtocol,
    BrickSpec,
    BrickState,
    DriftAction,
    DriftReport,
    ReconcileResult,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# OTel tracing — zero-overhead when telemetry is not enabled
# Shared implementation in nexus.services._tracing
# ---------------------------------------------------------------------------

_get_tracer, _lifecycle_span = lazy_tracer("nexus.brick_reconciler")

# ---------------------------------------------------------------------------
# Backoff constants
# ---------------------------------------------------------------------------

_BACKOFF_BASE: float = 30.0  # Base retry interval in seconds
_BACKOFF_MAX: float = 300.0  # Max retry interval cap
_JITTER_MAX: float = 5.0  # Max jitter added to loop interval


class BrickReconciler:
    """Drift detection and self-healing for bricks (Issue #2060).

    Kubernetes-inspired reconciliation loop:
    - Periodic sweep every ``reconcile_interval`` seconds (+ jitter)
    - Event-triggered: immediate reconcile on ``notify_state_change()``
    - Self-healing: reset and remount FAILED bricks (exponential backoff)
    - Health checks: ACTIVE bricks verified concurrently with per-brick timeout
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
        # Cached result for read-only drift endpoint (#14A)
        self._last_result: ReconcileResult | None = None
        self._last_reconcile_at: float | None = None
        # Per-brick next-eligible-retry timestamps (#3A)
        self._next_retry_after: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Public read-only accessors
    # ------------------------------------------------------------------

    @property
    def last_reconcile_at(self) -> float | None:
        """Monotonic timestamp of the last reconcile pass start."""
        return self._last_reconcile_at

    @property
    def reconcile_count(self) -> int:
        """Total number of reconcile passes completed."""
        return self._reconcile_count

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
        self._task = None
        logger.info("[RECONCILER] Stopped")

    def notify_state_change(self, brick_name: str) -> None:
        """Event trigger: schedule immediate reconciliation."""
        logger.debug("[RECONCILER] State change notification for %r", brick_name)
        self._event.set()

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def _loop(self) -> None:
        """Periodic reconciliation loop with event-triggered wake and jitter."""
        while not self._stopped:
            try:
                # Wait for interval OR event trigger (with jitter)
                jitter = random.uniform(0, _JITTER_MAX)  # noqa: S311
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._event.wait(),
                        timeout=self._reconcile_interval + jitter,
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
    # Read-only drift detection (cached, for GET endpoints)
    # ------------------------------------------------------------------

    def detect_drift(self) -> ReconcileResult:
        """Return cached drift report from last reconcile pass.

        Returns the cached result from the most recent ``reconcile()`` call.
        If no reconciliation has occurred yet, performs a fresh read-only scan.

        Safe for GET endpoints — no state transitions, no side effects.
        """
        if self._last_result is not None:
            return self._last_result
        # Fallback: fresh scan (only before first reconcile)
        return self._detect_drift_fresh()

    def _detect_drift_fresh(self) -> ReconcileResult:
        """Perform a fresh read-only drift scan via public API."""
        brick_list = self._manager.iter_bricks()
        drifts: list[DriftReport] = []

        for _name, spec, state, retry_count, _instance in brick_list:
            drift = self._compute_drift(spec, state, retry_count)
            if drift is not None:
                drifts.append(drift)

        return ReconcileResult(
            total_bricks=len(brick_list),
            drifted=len(drifts),
            actions_taken=0,
            errors=0,
            drifts=tuple(drifts),
        )

    # ------------------------------------------------------------------
    # Drift detection (internal)
    # ------------------------------------------------------------------

    def _compute_drift(
        self,
        spec: BrickSpec,
        state: BrickState,
        retry_count: int,
    ) -> DriftReport | None:
        """Compare spec vs status, return drift report or None if converged.

        Uses only immutable data — no access to internal manager structures.
        """
        if spec.enabled:
            if state == BrickState.FAILED:
                if retry_count >= self._max_retries:
                    return DriftReport(
                        brick_name=spec.name,
                        spec_state="enabled",
                        actual_state=state,
                        action=DriftAction.SKIP,
                        detail=f"Max retries ({self._max_retries}) exceeded",
                    )
                return DriftReport(
                    brick_name=spec.name,
                    spec_state="enabled",
                    actual_state=state,
                    action=DriftAction.RESET,
                    detail="Brick FAILED, will reset and remount",
                )
            if state == BrickState.REGISTERED:
                # Check if dependencies are met via public API
                for dep_name in spec.depends_on:
                    dep_status = self._manager.get_status(dep_name)
                    if dep_status is None or dep_status.state != BrickState.ACTIVE:
                        return DriftReport(
                            brick_name=spec.name,
                            spec_state="enabled",
                            actual_state=state,
                            action=DriftAction.SKIP,
                            detail=f"Dependency {dep_name!r} not ACTIVE",
                        )
                return DriftReport(
                    brick_name=spec.name,
                    spec_state="enabled",
                    actual_state=state,
                    action=DriftAction.MOUNT,
                    detail="Brick REGISTERED but should be ACTIVE",
                )
            if state == BrickState.ACTIVE:
                return None  # Handled by health check phase
        else:
            # spec.enabled is False
            if state == BrickState.ACTIVE:
                return DriftReport(
                    brick_name=spec.name,
                    spec_state="disabled",
                    actual_state=state,
                    action=DriftAction.UNMOUNT,
                    detail="Brick ACTIVE but spec says disabled",
                )

        return None

    # ------------------------------------------------------------------
    # Health checks (for ACTIVE bricks) — runs concurrently (#13A)
    # ------------------------------------------------------------------

    async def _health_check_brick(self, name: str, instance: Any) -> tuple[str, bool | None]:
        """Call health_check() with per-brick timeout.

        Returns (name, healthy) where healthy is True/False/None.
        """
        if not isinstance(instance, BrickLifecycleProtocol):
            return (name, None)  # Stateless brick — no health check

        try:
            result = await asyncio.wait_for(
                instance.health_check(),
                timeout=self._health_check_timeout,
            )
            return (name, bool(result))
        except TimeoutError:
            logger.warning(
                "[RECONCILER] Health check timeout for %r (%.1fs)",
                name,
                self._health_check_timeout,
            )
            return (name, False)
        except Exception as exc:
            logger.warning("[RECONCILER] Health check error for %r: %s", name, exc)
            return (name, False)

    # ------------------------------------------------------------------
    # Backoff calculation (#3A)
    # ------------------------------------------------------------------

    def _should_retry(self, name: str) -> bool:
        """Check if a brick is eligible for retry based on backoff schedule."""
        deadline = self._next_retry_after.get(name, 0.0)
        return time.monotonic() >= deadline

    def _set_backoff(self, name: str, retry_count: int) -> None:
        """Set exponential backoff deadline for a brick's next retry."""
        delay = min(_BACKOFF_BASE * (2 ** (retry_count - 1)), _BACKOFF_MAX)
        self._next_retry_after[name] = time.monotonic() + delay
        logger.debug(
            "[RECONCILER] Backoff for %r: %.0fs (retry %d)",
            name,
            delay,
            retry_count,
        )

    def _clear_backoff(self, name: str) -> None:
        """Clear backoff for a brick (on success or manual reset)."""
        self._next_retry_after.pop(name, None)

    # ------------------------------------------------------------------
    # Corrective actions
    # ------------------------------------------------------------------

    async def _take_action(
        self,
        name: str,
        drift: DriftReport,
    ) -> bool:
        """Execute the action prescribed by a DriftReport.

        Returns True if an action was taken.
        """
        action = drift.action

        if action is DriftAction.SKIP:
            return False

        if action is DriftAction.RESET:
            return await self._action_reset_and_mount(name)

        if action is DriftAction.MOUNT:
            return await self._action_mount(name)

        if action is DriftAction.UNMOUNT:
            return await self._action_unmount(name)

        return False

    async def _action_reset_and_mount(self, name: str) -> bool:
        """Reset a FAILED brick and attempt to remount it."""
        try:
            # Check backoff schedule
            if not self._should_retry(name):
                return False

            new_retry = self._manager.reset_for_retry(name)
            await self._manager.mount(name)

            # Check if mount succeeded
            status = self._manager.get_status(name)
            if status is not None and status.state == BrickState.ACTIVE:
                self._manager.clear_retry_count(name)
                self._clear_backoff(name)
                logger.info("[RECONCILER] Self-healed brick %r", name)
            else:
                self._set_backoff(name, new_retry)
            return True
        except Exception as exc:
            retry_count = self._manager.get_retry_count(name)
            self._set_backoff(name, retry_count)
            logger.warning(
                "[RECONCILER] Failed to reset+mount %r (retry %d/%d): %s",
                name,
                retry_count,
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
           → reset + mount (self-healing with exponential backoff)
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

        try:
            # Snapshot via public API
            brick_list = self._manager.iter_bricks()

            # Track bricks that just failed health check — don't auto-heal same pass
            health_failed: set[str] = set()

            # Phase 1: Health check ACTIVE bricks concurrently (#13A)
            active_bricks = [
                (name, instance)
                for name, spec, state, _retry, instance in brick_list
                if spec.enabled and state == BrickState.ACTIVE
            ]

            if active_bricks:
                check_results = await asyncio.gather(
                    *(self._health_check_brick(name, inst) for name, inst in active_bricks),
                    return_exceptions=True,
                )
                for check_result in check_results:
                    if isinstance(check_result, BaseException):
                        errors += 1
                        continue
                    name, healthy = check_result
                    if healthy is False:
                        try:
                            self._manager.fail_brick(name, "Health check failed")
                            health_failed.add(name)
                            drifts.append(
                                DriftReport(
                                    brick_name=name,
                                    spec_state="enabled",
                                    actual_state=BrickState.FAILED,
                                    action=DriftAction.HEALTH_CHECK_FAILED,
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
            # Re-snapshot to pick up state changes from Phase 1
            brick_list = self._manager.iter_bricks()
            for name, spec, state, retry_count, _instance in brick_list:
                if name in health_failed:
                    continue

                drift = self._compute_drift(spec, state, retry_count)
                if drift is None:
                    continue

                drifts.append(drift)

                try:
                    acted = await self._take_action(name, drift)
                    if acted:
                        actions_taken += 1
                except Exception as exc:
                    errors += 1
                    logger.warning("[RECONCILER] Action failed for %r: %s", name, exc)
        finally:
            elapsed = time.monotonic() - t0

            if span is not None:
                span.set_attribute("reconciler.total_bricks", len(drifts))
                span.set_attribute("reconciler.elapsed_ms", elapsed * 1000)
                span.end()

        result = ReconcileResult(
            total_bricks=len(brick_list),
            drifted=len(drifts),
            actions_taken=actions_taken,
            errors=errors,
            drifts=tuple(drifts),
        )

        # Cache for read-only drift endpoint (#14A)
        self._last_result = result
        self._last_reconcile_at = t0

        return result
