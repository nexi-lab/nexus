"""Brick reconciler — drift detection and self-healing (Issue #2060, #2059).

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
    - Per-brick ReconcilerProtocol callout with 5s timeout (Issue #2059)

References:
    - Issue #2060: Brick Spec/Status Separation for Drift Detection
    - Issue #2059: Self-Healing Brick Reconciler Protocol
"""

import asyncio
import contextlib
import logging
import random
import time
from typing import Any

from nexus.contracts.protocols.brick_lifecycle import (
    BrickLifecycleProtocol,
    BrickReconcileOutcome,
    BrickSpec,
    BrickState,
    BrickStatus,
    DriftAction,
    DriftReport,
    LifecycleManagerProtocol,
    ReconcileContext,
    ReconcileResult,
    ReconcilerProtocol,
)
from nexus.services._tracing import lazy_tracer
from nexus.system_services.lifecycle.expectations import Expectations

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# OTel tracing — zero-overhead when telemetry is not enabled
# Shared implementation in nexus.services._tracing
# ---------------------------------------------------------------------------

_get_tracer, _lifecycle_span = lazy_tracer("nexus.brick_reconciler")

# ---------------------------------------------------------------------------
# Backoff constants (Issue #2059: split per-brick vs central restart)
# ---------------------------------------------------------------------------

_BACKOFF_BASE_RECONCILE: float = 1.0  # Per-brick reconcile (lightweight)
_BACKOFF_BASE_RESTART: float = 30.0  # Central restart (heavy, original)
_BACKOFF_MAX: float = 300.0  # Shared cap
_JITTER_MAX: float = 5.0  # Max jitter added to loop interval

# Type alias for brick list snapshot
_BrickList = list[tuple[str, BrickSpec, BrickState, int, Any]]


class BrickReconciler:
    """Drift detection and self-healing for bricks (Issue #2060, #2059).

    Kubernetes-inspired reconciliation loop:
    - Periodic sweep every ``reconcile_interval`` seconds (+ jitter)
    - Event-triggered: immediate reconcile on ``notify_state_change()``
    - Self-healing: reset and remount FAILED bricks (exponential backoff)
    - Health checks: ACTIVE bricks verified concurrently with per-brick timeout
    - Per-brick reconcile: bricks implementing ``ReconcilerProtocol`` get
      custom self-healing via ``reconcile()`` callout (Issue #2059)
    """

    def __init__(
        self,
        lifecycle_manager: LifecycleManagerProtocol,
        *,
        reconcile_interval: float = 30.0,
        health_check_timeout: float = 2.0,
        max_retries: int = 3,
        reconcile_timeout: float = 5.0,
    ) -> None:
        self._manager = lifecycle_manager
        self._reconcile_interval = reconcile_interval
        self._health_check_timeout = health_check_timeout
        self._max_retries = max_retries
        self._reconcile_timeout = reconcile_timeout
        self._task: asyncio.Task[None] | None = None
        self._event = asyncio.Event()
        self._reconcile_count: int = 0
        self._stopped = False
        # Cached result for read-only drift endpoint (#14A)
        self._last_result: ReconcileResult | None = None
        self._last_reconcile_at: float | None = None
        # Per-brick next-eligible-retry timestamps (#3A)
        self._next_retry_after: dict[str, float] = {}
        # Zone-aware filtering (Issue #2061)
        self._terminating_zone_bricks: dict[str, set[str]] = {}  # zone_id → brick_names
        # Expectations tracker — prevents duplicate actions (Issue #2067)
        self._expectations = Expectations()
        # Per-brick reconcile outcomes — exposed via REST API (Issue #2059)
        self._last_reconcile_outcomes: list[BrickReconcileOutcome] = []
        self._last_reconcile_outcome_names: list[str] = []

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

    @property
    def last_reconcile_outcomes(self) -> list[tuple[str, BrickReconcileOutcome]]:
        """Per-brick reconcile outcomes from the most recent pass."""
        return list(
            zip(self._last_reconcile_outcome_names, self._last_reconcile_outcomes, strict=True)
        )

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
    # Zone awareness (Issue #2061)
    # ------------------------------------------------------------------

    def mark_zone_terminating(self, zone_id: str, *, brick_names: set[str]) -> None:
        """Mark bricks as belonging to a terminating zone.

        The reconciler will skip health checks and self-healing for these bricks.
        """
        self._terminating_zone_bricks[zone_id] = brick_names
        logger.info(
            "[RECONCILER] Zone %r marked terminating (%d bricks)",
            zone_id,
            len(brick_names),
        )

    def mark_zone_destroyed(self, zone_id: str) -> None:
        """Remove zone terminating marker — normal reconciliation resumes."""
        self._terminating_zone_bricks.pop(zone_id, None)
        logger.info("[RECONCILER] Zone %r marked destroyed", zone_id)

    def _is_brick_in_terminating_zone(self, brick_name: str) -> bool:
        """Check if a brick belongs to any terminating zone."""
        return any(brick_name in bricks for bricks in self._terminating_zone_bricks.values())

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
        # Skip bricks in terminating zones (Issue #2061)
        if self._is_brick_in_terminating_zone(spec.name):
            return None

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
            if state in (BrickState.REGISTERED, BrickState.UNMOUNTED):
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
                detail = (
                    "Brick UNMOUNTED but should be ACTIVE"
                    if state == BrickState.UNMOUNTED
                    else "Brick REGISTERED but should be ACTIVE"
                )
                return DriftReport(
                    brick_name=spec.name,
                    spec_state="enabled",
                    actual_state=state,
                    action=DriftAction.MOUNT,
                    detail=detail,
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
    # Backoff calculation (#3A, split per Issue #2059)
    # ------------------------------------------------------------------

    def _should_retry(self, name: str) -> bool:
        """Check if a brick is eligible for retry based on backoff schedule."""
        deadline = self._next_retry_after.get(name, 0.0)
        return time.monotonic() >= deadline

    def _set_backoff(self, name: str, retry_count: int) -> None:
        """Set exponential backoff deadline for central restart (30s base)."""
        delay = min(_BACKOFF_BASE_RESTART * (2 ** (retry_count - 1)), _BACKOFF_MAX)
        self._next_retry_after[name] = time.monotonic() + delay
        logger.debug(
            "[RECONCILER] Backoff for %r: %.0fs (retry %d)",
            name,
            delay,
            retry_count,
        )

    def _set_reconcile_backoff(
        self, name: str, retry_count: int, *, explicit_delay: float | None = None
    ) -> None:
        """Set exponential backoff for per-brick reconcile (1s base).

        If ``explicit_delay`` is provided (from ``BrickReconcileOutcome.requeue_after``),
        it overrides the computed delay but is still capped at ``_BACKOFF_MAX``.
        """
        if explicit_delay is not None:
            delay = min(explicit_delay, _BACKOFF_MAX)
        else:
            delay = min(_BACKOFF_BASE_RECONCILE * (2 ** max(0, retry_count - 1)), _BACKOFF_MAX)
        self._next_retry_after[name] = time.monotonic() + delay
        logger.debug(
            "[RECONCILER] Reconcile backoff for %r: %.1fs (retry %d)",
            name,
            delay,
            retry_count,
        )

    def _clear_backoff(self, name: str) -> None:
        """Clear backoff for a brick (on success or manual reset)."""
        self._next_retry_after.pop(name, None)

    # ------------------------------------------------------------------
    # Expectations auto-observation (Issue #2067)
    # ------------------------------------------------------------------

    def _observe_completed_operations(self, brick_list: _BrickList) -> None:
        """Auto-observe completed operations by diffing expectations against snapshot.

        O(k) scan of pending expectations only — not full brick list.
        """
        pending = self._expectations.pending_keys
        if not pending:
            return

        # Build lookup for current brick states (only for pending keys)
        state_map: dict[str, BrickState] = {}
        for name, _spec, state, _retry, _inst in brick_list:
            if name in pending:
                state_map[name] = state

        for key in pending:
            brick_state = state_map.get(key)
            if brick_state is None:
                continue
            if brick_state == BrickState.ACTIVE:
                self._expectations.mount_observed(key)
                logger.debug("[RECONCILER] Auto-observed mount for %r", key)
            elif brick_state in (BrickState.UNREGISTERED, BrickState.STOPPING):
                self._expectations.unmount_observed(key)
                logger.debug("[RECONCILER] Auto-observed unmount for %r", key)

    # ------------------------------------------------------------------
    # DRY helper: expectation-wrapped actions (Issue #2059)
    # ------------------------------------------------------------------

    async def _with_expectation(
        self,
        name: str,
        kind: str,
        action_coro: Any,
    ) -> bool:
        """Execute an action wrapped in expect/observe bookkeeping.

        Args:
            name: Brick name.
            kind: ``"mount"`` or ``"unmount"``.
            action_coro: Awaitable coroutine to execute.

        Returns True if the action was attempted.
        """
        expect_fn = (
            self._expectations.expect_mount
            if kind == "mount"
            else self._expectations.expect_unmount
        )
        observe_fn = (
            self._expectations.mount_observed
            if kind == "mount"
            else self._expectations.unmount_observed
        )
        try:
            expect_fn(name)
            await action_coro
            return True
        except Exception as exc:
            observe_fn(name)
            logger.warning("[RECONCILER] %s failed for %r: %s", kind, name, exc)
            return True

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

            self._expectations.expect_mount(name)
            new_retry = self._manager.reset_for_retry(name)
            await self._manager.mount(name)

            # Check if mount succeeded
            status = self._manager.get_status(name)
            if status is not None and status.state == BrickState.ACTIVE:
                self._manager.clear_retry_count(name)
                self._clear_backoff(name)
                logger.info("[RECONCILER] Self-healed brick %r", name)
            else:
                # Mount failed — clear expectation so next pass can retry
                self._expectations.mount_observed(name)
                self._set_backoff(name, new_retry)
            return True
        except Exception as exc:
            # Clear expectation on error so brick isn't permanently gated
            self._expectations.mount_observed(name)
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
        return await self._with_expectation(
            name,
            "mount",
            self._manager.mount(name),
        )

    async def _action_unmount(self, name: str) -> bool:
        """Unmount a disabled brick."""
        return await self._with_expectation(
            name,
            "unmount",
            self._manager.unmount(name),
        )

    # ------------------------------------------------------------------
    # Phase methods — decomposed from reconcile() (Issue #2059)
    # ------------------------------------------------------------------

    async def _phase_health_checks(
        self, brick_list: _BrickList
    ) -> tuple[set[str], list[DriftReport], int, int]:
        """Phase 1: Health check ACTIVE bricks concurrently.

        Returns:
            (health_failed, drifts, actions_taken, errors)
        """
        health_failed: set[str] = set()
        drifts: list[DriftReport] = []
        actions_taken = 0
        errors = 0

        active_bricks = [
            (name, instance)
            for name, spec, state, _retry, instance in brick_list
            if spec.enabled
            and state == BrickState.ACTIVE
            and not self._is_brick_in_terminating_zone(name)
        ]

        if not active_bricks:
            return health_failed, drifts, actions_taken, errors

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

        return health_failed, drifts, actions_taken, errors

    async def _phase_drift_and_actions(
        self, brick_list: _BrickList, health_failed: set[str]
    ) -> tuple[list[DriftReport], int, int]:
        """Phase 2: Detect drift and take corrective actions.

        Returns:
            (drifts, actions_taken, errors)
        """
        drifts: list[DriftReport] = []
        actions_taken = 0
        errors = 0

        for name, spec, state, retry_count, _instance in brick_list:
            if name in health_failed:
                continue

            # Gate: skip bricks with unsatisfied expectations (Issue #2067)
            if not self._expectations.satisfied(name):
                logger.debug("[RECONCILER] Expectations pending for %r, skipping", name)
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

        return drifts, actions_taken, errors

    async def _phase_per_brick_reconcile(
        self, brick_list: _BrickList
    ) -> list[tuple[str, BrickReconcileOutcome]]:
        """Phase 3: Per-brick reconcile for bricks implementing ReconcilerProtocol.

        Calls ``instance.reconcile(ctx)`` sequentially with timeout.
        Handles outcomes: error → fail_brick, requeue → set backoff,
        healthy → clear backoff.

        Returns list of (brick_name, outcome) pairs.
        """
        outcomes: list[tuple[str, BrickReconcileOutcome]] = []

        for name, spec, state, retry_count, instance in brick_list:
            if not isinstance(instance, ReconcilerProtocol):
                continue

            # Skip bricks in terminating zones
            if self._is_brick_in_terminating_zone(name):
                continue

            # Skip bricks with unsatisfied expectations
            if not self._expectations.satisfied(name):
                continue

            # Build context
            status: BrickStatus | None = self._manager.get_status(name)
            last_error = status.error if status else None
            last_healthy_at = (
                status.started_at if status and status.state == BrickState.ACTIVE else None
            )

            ctx = ReconcileContext(
                brick_name=name,
                current_state=state,
                desired_enabled=spec.enabled,
                retry_count=retry_count,
                last_error=last_error,
                last_healthy_at=last_healthy_at,
            )

            try:
                outcome = await asyncio.wait_for(
                    instance.reconcile(ctx),
                    timeout=self._reconcile_timeout,
                )
            except TimeoutError:
                logger.warning(
                    "[RECONCILER] Per-brick reconcile timeout for %r (%.1fs)",
                    name,
                    self._reconcile_timeout,
                )
                outcome = BrickReconcileOutcome(requeue=True)
            except Exception as exc:
                logger.warning("[RECONCILER] Per-brick reconcile error for %r: %s", name, exc)
                outcome = BrickReconcileOutcome(requeue=True)

            outcomes.append((name, outcome))

            # Handle outcome
            if outcome.error is not None:
                self._manager.fail_brick(name, outcome.error)
            elif outcome.requeue:
                explicit_delay = (
                    outcome.requeue_after.total_seconds()
                    if outcome.requeue_after is not None
                    else None
                )
                self._set_reconcile_backoff(name, retry_count, explicit_delay=explicit_delay)
            else:
                # Healthy — clear any per-brick backoff
                self._clear_backoff(name)

        return outcomes

    # ------------------------------------------------------------------
    # Single reconciliation pass
    # ------------------------------------------------------------------

    async def reconcile(self) -> ReconcileResult:
        """Single reconciliation pass: health checks + drift + per-brick reconcile.

        Three phases:
        1. Health check ACTIVE bricks concurrently; transition unhealthy → FAILED
        2. Detect drift and take corrective actions (reset, mount, unmount)
        3. Per-brick reconcile for bricks implementing ReconcilerProtocol
        """
        tracer = _get_tracer()
        span = None
        if tracer is not None:
            span = tracer.start_span("reconciler.reconcile")

        t0 = time.monotonic()
        all_drifts: list[DriftReport] = []
        actions_taken = 0
        errors = 0

        try:
            # Snapshot via public API
            brick_list = self._manager.iter_bricks()

            # Auto-observe completed operations (Issue #2067)
            self._observe_completed_operations(brick_list)

            # Phase 1: Health checks
            (
                health_failed,
                health_drifts,
                health_actions,
                health_errors,
            ) = await self._phase_health_checks(brick_list)
            all_drifts.extend(health_drifts)
            actions_taken += health_actions
            errors += health_errors

            # Phase 2: Drift detection and corrective actions
            # Re-snapshot to pick up state changes from Phase 1
            brick_list = self._manager.iter_bricks()
            drift_reports, drift_actions, drift_errors = await self._phase_drift_and_actions(
                brick_list, health_failed
            )
            all_drifts.extend(drift_reports)
            actions_taken += drift_actions
            errors += drift_errors

            # Phase 3: Per-brick reconcile (reuses Phase 2 snapshot)
            outcomes = await self._phase_per_brick_reconcile(brick_list)
            self._last_reconcile_outcomes = [o for _, o in outcomes]
            self._last_reconcile_outcome_names = [n for n, _ in outcomes]

        finally:
            elapsed = time.monotonic() - t0

            if span is not None:
                span.set_attribute("reconciler.total_bricks", len(all_drifts))
                span.set_attribute("reconciler.elapsed_ms", elapsed * 1000)
                span.end()

        result = ReconcileResult(
            total_bricks=len(brick_list),
            drifted=len(all_drifts),
            actions_taken=actions_taken,
            errors=errors,
            drifts=tuple(all_drifts),
        )

        # Cache for read-only drift endpoint (#14A)
        self._last_result = result
        self._last_reconcile_at = t0

        return result
