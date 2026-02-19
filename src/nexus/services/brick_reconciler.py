"""Self-healing brick reconciler with requeue and exponential backoff (Issue #2059).

Detects failed or degraded bricks and automatically attempts recovery
using decorrelated jitter backoff.  Runs as a System Service alongside
``BrickLifecycleManager``.

Architecture:
    - Two background loops: reconcile (work queue) + health poll
    - In-memory asyncio.Queue — brick lifecycle state is ephemeral
    - Per-brick deduplication via ``_queued`` set + ``_in_progress`` set
    - Decorrelated jitter backoff per AWS recommendation
    - Dead-letter after ``max_attempts`` (stop retrying, log warning)

Trigger mechanisms (hybrid):
    1. State-change callback: ``on_state_change`` fires when any brick
       transitions to FAILED → immediately enqueued for recovery.
    2. Periodic health polling: every ``health_check_interval`` seconds,
       calls ``check_health()`` on all ACTIVE bricks concurrently.

References:
    - docs/design/NEXUS-LEGO-ARCHITECTURE.md §2.4 (System Services)
    - Kubernetes controller-runtime reconciler pattern
    - AWS Builders Library: Timeouts, Retries, and Backoff with Jitter
    - Issue #2059: Self-healing brick reconciler
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

from nexus.services.brick_lifecycle import BrickLifecycleManager
from nexus.services.protocols.brick_lifecycle import BrickState
from nexus.services.protocols.brick_reconciler import (
    BackoffState,
    BrickReconcilerConfig,
    compute_next_backoff,
)

logger = logging.getLogger(__name__)

# Max concurrent health checks to avoid thundering herd on poll
_HEALTH_CHECK_CONCURRENCY = 50


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
    """Self-healing brick reconciler with requeue and exponential backoff.

    Detects failed/degraded bricks via:
    1. State-change callback (instant, on FAILED transition)
    2. Periodic health polling (configurable interval)

    Recovers bricks via: reset → mount with decorrelated jitter backoff.
    Dead-letters bricks after ``max_attempts``.

    Usage::

        reconciler = BrickReconciler(lifecycle_manager, config=config)
        await reconciler.start()    # launch background loops
        ...
        await reconciler.stop()     # graceful shutdown
    """

    def __init__(
        self,
        lifecycle_manager: BrickLifecycleManager,
        config: BrickReconcilerConfig = BrickReconcilerConfig(),
    ) -> None:
        self._manager = lifecycle_manager
        self._config = config
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=1024)
        self._queued: set[str] = set()  # queue-level dedup
        self._backoff: dict[str, BackoffState] = {}
        self._in_progress: set[str] = set()
        self._reconcile_task: asyncio.Task[None] | None = None
        self._health_poll_task: asyncio.Task[None] | None = None

        # Register state-change callback (Issue #2059)
        if lifecycle_manager.on_state_change is not None:
            logger.warning("[RECONCILER] Overwriting existing on_state_change callback")
        lifecycle_manager.on_state_change = self._on_state_change

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def enqueue(self, brick_name: str) -> None:
        """Enqueue a brick for reconciliation (deduplicates)."""
        if brick_name in self._queued:
            return
        try:
            self._queue.put_nowait(brick_name)
            self._queued.add(brick_name)
        except asyncio.QueueFull:
            logger.warning("[RECONCILER] Queue full, dropping brick %r", brick_name)

    async def start(self) -> None:
        """Start the reconciler background loops."""
        logger.info(
            "[RECONCILER] Starting (interval=%.1fs, max_attempts=%d, max_delay=%.1fs)",
            self._config.health_check_interval,
            self._config.max_attempts,
            self._config.max_delay,
        )
        self._reconcile_task = asyncio.create_task(self._reconcile_loop(), name="brick_reconciler")
        self._health_poll_task = asyncio.create_task(
            self._health_poll_loop(), name="brick_health_poll"
        )

    async def stop(self) -> None:
        """Stop the reconciler and cancel background tasks."""
        for task in (self._reconcile_task, self._health_poll_task):
            if task is not None and not task.done():
                task.cancel()

        # Await cancellation
        for task in (self._reconcile_task, self._health_poll_task):
            if task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        self._reconcile_task = None
        self._health_poll_task = None
        logger.info("[RECONCILER] Stopped")

    # ------------------------------------------------------------------
    # State-change callback (trigger #1)
    # ------------------------------------------------------------------

    def _on_state_change(
        self, brick_name: str, old_state: BrickState, new_state: BrickState
    ) -> None:
        """Callback from BrickLifecycleManager — enqueue FAILED bricks.

        Skips enqueue if the brick is currently being reconciled (in_progress)
        to avoid re-enqueue loops during recovery attempts.

        Note: _in_progress and _queued are both accessed on the same event
        loop thread (single-threaded asyncio) — no lock needed.
        """
        if new_state == BrickState.FAILED and brick_name not in self._in_progress:
            logger.info(
                "[RECONCILER] Brick %r transitioned to FAILED (from %s), enqueuing",
                brick_name,
                old_state.name,
            )
            self.enqueue(brick_name)

    # ------------------------------------------------------------------
    # Health polling loop (trigger #2)
    # ------------------------------------------------------------------

    async def _health_poll_loop(self) -> None:
        """Periodically check health of all ACTIVE bricks."""
        try:
            while True:
                await asyncio.sleep(self._config.health_check_interval)
                await self._poll_health()
        except asyncio.CancelledError:
            logger.debug("[RECONCILER] Health poll loop cancelled")

    async def _poll_health(self) -> None:
        """Run health checks on all ACTIVE bricks with bounded concurrency."""
        active_bricks = self._manager.get_active_brick_names()

        if not active_bricks:
            return

        # Bounded concurrency to avoid thundering herd
        sem = asyncio.Semaphore(_HEALTH_CHECK_CONCURRENCY)

        async def _bounded_check(name: str) -> bool:
            async with sem:
                return await self._manager.check_health(
                    name, timeout=self._config.health_check_timeout
                )

        results = await asyncio.gather(
            *(_bounded_check(name) for name in active_bricks),
            return_exceptions=True,
        )

        healthy = sum(1 for r in results if r is True)

        logger.debug(
            "[RECONCILER] Health poll: %d/%d healthy",
            healthy,
            len(active_bricks),
        )

    # ------------------------------------------------------------------
    # Reconcile loop (work queue consumer)
    # ------------------------------------------------------------------

    async def _reconcile_loop(self) -> None:
        """Pull bricks from the queue and dispatch recovery as tasks.

        Each brick is processed as an independent asyncio task so that
        one brick's backoff sleep does not block recovery of others.
        """
        try:
            while True:
                brick_name = await self._queue.get()
                self._queued.discard(brick_name)
                try:
                    # Spawn recovery as independent task to avoid
                    # head-of-line blocking from backoff sleep
                    asyncio.create_task(
                        self._process_item(brick_name),
                        name=f"reconcile_{brick_name}",
                    )
                except Exception as exc:
                    logger.error(
                        "[RECONCILER] Unexpected error dispatching %r: %s",
                        brick_name,
                        exc,
                    )
                finally:
                    self._queue.task_done()
        except asyncio.CancelledError:
            logger.debug("[RECONCILER] Reconcile loop cancelled")

    async def _process_item(self, brick_name: str) -> None:
        """Process a single queued brick: check dedup, wait for backoff, attempt recovery."""
        # Deduplication: skip if already being reconciled
        if brick_name in self._in_progress:
            logger.debug("[RECONCILER] Skipping %r (already in progress)", brick_name)
            return

        # Check if brick still needs recovery
        status = self._manager.get_status(brick_name)
        if status is None:
            logger.debug("[RECONCILER] Brick %r not found, skipping", brick_name)
            return
        if status.state != BrickState.FAILED:
            logger.debug(
                "[RECONCILER] Brick %r is %s (not FAILED), skipping",
                brick_name,
                status.state.name,
            )
            return

        # Check dead-letter threshold
        backoff = self._backoff.get(brick_name, BackoffState())
        if backoff.attempt >= self._config.max_attempts:
            logger.warning(
                "[RECONCILER] Brick %r dead-lettered after %d attempts",
                brick_name,
                backoff.attempt,
            )
            # Clean up backoff state to avoid memory leak
            self._backoff.pop(brick_name, None)
            return

        # Wait for backoff delay (non-blocking to other bricks since each
        # _process_item runs as its own task)
        now = time.monotonic()
        if backoff.next_retry_at > now:
            delay = backoff.next_retry_at - now
            logger.debug("[RECONCILER] Brick %r: waiting %.2fs for backoff", brick_name, delay)
            await asyncio.sleep(delay)

        # Attempt recovery (_attempt_recovery manages _in_progress internally)
        await self._attempt_recovery(brick_name)

    async def _attempt_recovery(self, brick_name: str) -> None:
        """Attempt to recover a FAILED brick: reset → mount.

        On success: clear backoff state.
        On failure: compute next backoff, requeue if under max_attempts.

        Adds brick_name to ``_in_progress`` to suppress callback re-enqueue.
        """
        self._in_progress.add(brick_name)
        try:
            await self._do_recovery(brick_name)
        finally:
            self._in_progress.discard(brick_name)

    async def _do_recovery(self, brick_name: str) -> None:
        """Internal recovery logic (called under _in_progress guard)."""
        tracer = _get_tracer()
        span = None
        if tracer is not None:
            span = tracer.start_span(f"brick.reconcile.{brick_name}")

        backoff = self._backoff.get(brick_name, BackoffState())

        try:
            try:
                # Reset: FAILED → REGISTERED
                self._manager.reset(brick_name)
                # Mount: REGISTERED → STARTING → ACTIVE
                await self._manager.mount(brick_name)
            except Exception as exc:
                # Recovery failed — compute backoff and potentially requeue
                new_backoff = compute_next_backoff(backoff, self._config)
                self._backoff[brick_name] = new_backoff

                logger.warning(
                    "[RECONCILER] Recovery failed for %r (attempt %d/%d): %s. Next retry in %.2fs",
                    brick_name,
                    new_backoff.attempt,
                    self._config.max_attempts,
                    exc,
                    new_backoff.last_delay,
                )

                if span is not None:
                    span.set_attribute("brick.error", str(exc))

                # Requeue if under max attempts
                if new_backoff.attempt < self._config.max_attempts:
                    self.enqueue(brick_name)
                else:
                    logger.warning(
                        "[RECONCILER] Brick %r dead-lettered after %d attempts",
                        brick_name,
                        new_backoff.attempt,
                    )
                    self._backoff.pop(brick_name, None)
                return

            # Check if mount actually succeeded (brick could still be FAILED)
            status = self._manager.get_status(brick_name)
            if status is not None and status.state == BrickState.ACTIVE:
                # Success! Clear backoff state
                self._backoff.pop(brick_name, None)
                logger.info(
                    "[RECONCILER] Brick %r recovered successfully (after %d attempt(s))",
                    brick_name,
                    backoff.attempt + 1,
                )
                if span is not None:
                    span.set_attribute("brick.recovered", True)
            else:
                # Mount completed but brick is not ACTIVE (e.g. FAILED during start)
                new_backoff = compute_next_backoff(backoff, self._config)
                self._backoff[brick_name] = new_backoff
                logger.warning(
                    "[RECONCILER] Brick %r still not ACTIVE after mount (state=%s, attempt %d/%d)",
                    brick_name,
                    status.state.name if status else "unknown",
                    new_backoff.attempt,
                    self._config.max_attempts,
                )
                if new_backoff.attempt < self._config.max_attempts:
                    self.enqueue(brick_name)
                else:
                    self._backoff.pop(brick_name, None)
        finally:
            if span is not None:
                span.end()
