"""Multi-cursor task dispatcher with per-executor dequeue isolation.

Each connected executor gets its own dispatch cursor (asyncio.Task)
that dequeues only tasks assigned to that executor_id. This eliminates
head-of-line blocking when one executor is slow.

Architecture (Issue #2748):
- Hybrid TaskGroup + manual cursor registry
- TaskGroup manages fixed loops (aging, starvation, LISTEN, reconcile)
- Manual dict[str, asyncio.Task] manages dynamic per-executor cursors
- NOTIFY demux: shared LISTEN channel routes JSON payload to per-executor Events
- Adaptive polling: 5s active → 60s idle (reduces DB pressure)
- Exponential backoff: 1s → 2s → 4s → ... → 60s on repeated errors
- Reconcile sweep: periodic safety net spawns missing cursors

Uses asyncio.TaskGroup for structured concurrency on fixed loops,
and manual task registry for dynamic executor cursors.

Related: Issue #1212, #1274, #2748
"""

import asyncio
import contextlib
import json
import logging
from typing import TYPE_CHECKING, Any

from nexus.system_services.scheduler.constants import (
    AGING_INTERVAL_SECONDS,
    STARVATION_PROMOTION_THRESHOLD_SECS,
)

if TYPE_CHECKING:
    from nexus.system_services.scheduler.events import AgentStateEvent
    from nexus.system_services.scheduler.service import SchedulerService

logger = logging.getLogger(__name__)

# Starvation promotion runs every 5 minutes
_STARVATION_CHECK_INTERVAL = 300

# Adaptive polling bounds
_POLL_ACTIVE_SECS = 5.0
_POLL_IDLE_SECS = 60.0
_IDLE_THRESHOLD = 3  # consecutive empty dequeues before switching to idle interval

# Backoff bounds
_BACKOFF_BASE_SECS = 1.0
_BACKOFF_MAX_SECS = 60.0
_MAX_CONSECUTIVE_ERRORS = 20

# Reconcile sweep interval
_RECONCILE_INTERVAL_SECS = 120.0


class TaskDispatcher:
    """Multi-cursor background task dispatcher.

    Each executor gets its own dequeue cursor. Fixed infrastructure
    loops (aging, starvation, LISTEN, reconcile) run in a TaskGroup.
    Dynamic executor cursors are managed via a manual task registry.
    """

    def __init__(
        self,
        scheduler_service: "SchedulerService",
        poll_interval: int = 30,
        *,
        record_store: Any | None = None,
    ) -> None:
        self._scheduler = scheduler_service
        self._record_store = record_store
        self._poll_interval = poll_interval
        self._running = False
        self._task_group_task: asyncio.Task[None] | None = None

        # Per-executor cursor registry: executor_id → asyncio.Task
        self._cursors: dict[str, asyncio.Task[None]] = {}

        # Per-executor notification events for NOTIFY demux
        self._executor_events: dict[str, asyncio.Event] = {}

        # Global fallback event (for tasks without executor routing)
        self._global_event = asyncio.Event()

    # =========================================================================
    # Lifecycle
    # =========================================================================

    async def start(self) -> None:
        """Start the dispatcher infrastructure and register for state events."""
        if self._running:
            return

        self._running = True
        logger.info("Starting multi-cursor task dispatcher")

        # Register for agent state events to auto-spawn/cancel cursors
        if self._scheduler._state_emitter is not None:
            self._scheduler._state_emitter.add_handler(self._on_agent_state_change)

        self._task_group_task = asyncio.create_task(self._run_all())

    async def stop(self) -> None:
        """Gracefully stop all cursors and infrastructure loops."""
        if not self._running:
            return

        logger.info("Stopping multi-cursor task dispatcher")
        self._running = False

        # Unregister state event handler
        if self._scheduler._state_emitter is not None:
            self._scheduler._state_emitter.remove_handler(self._on_agent_state_change)

        # Cancel all executor cursors
        for executor_id, task in list(self._cursors.items()):
            task.cancel()
            logger.info("Cancelled cursor for executor %s", executor_id)
        # Wait for cursors to finish
        if self._cursors:
            await asyncio.gather(
                *self._cursors.values(),
                return_exceptions=True,
            )
        self._cursors.clear()
        self._executor_events.clear()

        # Wake up any sleeping loops
        self._global_event.set()

        # Cancel the infrastructure TaskGroup
        if self._task_group_task:
            self._task_group_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task_group_task

        logger.info("Multi-cursor task dispatcher stopped")

    # =========================================================================
    # Infrastructure loops (fixed, run in TaskGroup)
    # =========================================================================

    async def _run_all(self) -> None:
        """Run fixed infrastructure loops in a TaskGroup."""
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self._aging_loop())
                tg.create_task(self._starvation_loop())
                tg.create_task(self._reconcile_loop())
                if self._record_store is not None:
                    tg.create_task(self._listen_loop())
        except* asyncio.CancelledError:
            logger.info("Dispatcher TaskGroup cancelled")
        except* Exception as eg:
            for exc in eg.exceptions:
                logger.exception("Dispatcher infrastructure loop failed: %s", exc)

    async def _aging_loop(self) -> None:
        """Periodic aging sweep loop."""
        while self._running:
            try:
                count = await self._scheduler.run_aging_sweep()
                if count > 0:
                    logger.info("Aging sweep updated %d tasks", count)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error in aging sweep")

            await asyncio.sleep(AGING_INTERVAL_SECONDS)

    async def _starvation_loop(self) -> None:
        """Periodic starvation promotion loop (Issue #1274)."""
        while self._running:
            try:
                count = await self._scheduler.run_starvation_promotion(
                    STARVATION_PROMOTION_THRESHOLD_SECS,
                )
                if count > 0:
                    logger.info("Starvation promotion: %d tasks promoted", count)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error in starvation promotion")

            await asyncio.sleep(_STARVATION_CHECK_INTERVAL)

    async def _reconcile_loop(self) -> None:
        """Periodic reconcile sweep: discover executors with queued tasks but no cursor.

        Safety net to catch executors that connected before the dispatcher started,
        or whose cursor was lost due to an unexpected error exceeding max retries.
        """
        while self._running:
            await asyncio.sleep(_RECONCILE_INTERVAL_SECS)
            try:
                await self._reconcile_cursors()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error in reconcile sweep")

    async def _reconcile_cursors(self) -> None:
        """Query DB for distinct executor_ids with queued tasks, spawn missing cursors."""
        async with self._scheduler.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT DISTINCT executor_id FROM scheduled_tasks WHERE status = 'queued'"
            )
        for row in rows:
            executor_id = row["executor_id"]
            if executor_id not in self._cursors or self._cursors[executor_id].done():
                logger.info("Reconcile: spawning cursor for executor %s", executor_id)
                self._spawn_cursor(executor_id)

    # =========================================================================
    # LISTEN/NOTIFY demux
    # =========================================================================

    async def _listen_loop(self) -> None:
        """LISTEN for task_enqueued notifications and demux to per-executor events.

        Uses the async engine from RecordStoreABC for a dedicated raw connection.
        """
        if self._record_store is None:
            return

        try:
            engine = self._record_store._async_engine
            if engine is None:
                _ = self._record_store.async_session_factory
                engine = self._record_store._async_engine
            if engine is None:
                logger.warning("No async engine available, using poll-only mode")
                return

            async with engine.connect() as sa_conn:
                raw = await sa_conn.get_raw_connection()
                driver_conn = raw.driver_connection
                await driver_conn.add_listener("task_enqueued", self._on_notification)
                logger.info("Listening for task_enqueued notifications (multi-cursor)")

                while self._running:
                    await asyncio.sleep(1)
        except Exception:
            logger.exception("LISTEN connection failed, falling back to polling")

    def _on_notification(
        self,
        _connection: Any,
        _pid: int,
        _channel: str,
        payload: str,
    ) -> None:
        """Handle NOTIFY callback — route to per-executor event or global fallback."""
        try:
            data = json.loads(payload)
            executor_id = data.get("executor_id")
        except (json.JSONDecodeError, TypeError):
            executor_id = None

        if executor_id and executor_id in self._executor_events:
            self._executor_events[executor_id].set()
        else:
            # Wake all cursors as fallback
            self._global_event.set()
            for event in self._executor_events.values():
                event.set()

    # =========================================================================
    # Per-executor cursor management
    # =========================================================================

    def _spawn_cursor(self, executor_id: str) -> None:
        """Spawn a dequeue cursor for an executor (idempotent)."""
        if executor_id in self._cursors and not self._cursors[executor_id].done():
            return  # Already running

        event = asyncio.Event()
        self._executor_events[executor_id] = event
        task = asyncio.create_task(
            self._executor_dispatch_loop(executor_id, event),
            name=f"cursor-{executor_id}",
        )
        self._cursors[executor_id] = task
        logger.info("Spawned cursor for executor %s (total: %d)", executor_id, len(self._cursors))

        # Pool sizing warning
        pool = getattr(self._scheduler, "_pool", None)
        if pool is not None:
            pool_size = getattr(pool, "get_size", lambda: 0)()
            cursor_count = len(self._cursors)
            # Reserve 2 connections for infrastructure (LISTEN, aging, etc.)
            if cursor_count > max(pool_size - 2, 1):
                logger.warning(
                    "Cursor count (%d) exceeds pool size (%d) - 2. Consider increasing pool size.",
                    cursor_count,
                    pool_size,
                )

    def _cancel_cursor(self, executor_id: str) -> None:
        """Cancel a dequeue cursor for an executor."""
        task = self._cursors.pop(executor_id, None)
        self._executor_events.pop(executor_id, None)
        if task is not None and not task.done():
            task.cancel()
            logger.info(
                "Cancelled cursor for executor %s (remaining: %d)",
                executor_id,
                len(self._cursors),
            )

    async def _executor_dispatch_loop(
        self,
        executor_id: str,
        event: asyncio.Event,
    ) -> None:
        """Per-executor dequeue loop with adaptive polling and exponential backoff."""
        consecutive_empty = 0
        consecutive_errors = 0
        backoff = _BACKOFF_BASE_SECS

        while self._running:
            try:
                task = await self._scheduler.dequeue_next(executor_id=executor_id)
                if task:
                    logger.info(
                        "Dispatching task",
                        extra={
                            "task_id": task.id,
                            "task_type": task.task_type,
                            "executor": task.executor_id,
                            "effective_tier": task.effective_tier,
                            "priority_class": task.priority_class,
                        },
                    )
                    consecutive_empty = 0
                    consecutive_errors = 0
                    backoff = _BACKOFF_BASE_SECS
                    continue  # Try next immediately

                # No task available — adaptive polling
                consecutive_empty += 1
                consecutive_errors = 0
                backoff = _BACKOFF_BASE_SECS

                poll_interval = (
                    _POLL_IDLE_SECS if consecutive_empty >= _IDLE_THRESHOLD else _POLL_ACTIVE_SECS
                )

                event.clear()
                self._global_event.clear()

                # Wait for either per-executor or global event, or timeout
                done, _ = await asyncio.wait(
                    [
                        asyncio.create_task(event.wait()),
                        asyncio.create_task(self._global_event.wait()),
                    ],
                    timeout=poll_interval,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                # Cancel the remaining waiter tasks
                for pending_task in _:
                    pending_task.cancel()

            except asyncio.CancelledError:
                break
            except Exception:
                consecutive_errors += 1
                logger.exception(
                    "Error in dispatch loop for executor %s (attempt %d/%d)",
                    executor_id,
                    consecutive_errors,
                    _MAX_CONSECUTIVE_ERRORS,
                )

                if consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
                    logger.error(
                        "Executor %s cursor exceeded max consecutive errors (%d), stopping cursor. "
                        "Reconcile sweep will restart it.",
                        executor_id,
                        _MAX_CONSECUTIVE_ERRORS,
                    )
                    break

                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _BACKOFF_MAX_SECS)

    # =========================================================================
    # Agent state event handler (cursor lifecycle)
    # =========================================================================

    async def _on_agent_state_change(self, event: "AgentStateEvent") -> None:
        """Spawn or cancel cursor based on agent state transitions."""
        executor_id = event.agent_id

        if event.new_state in ("CONNECTED", "IDLE"):
            if self._running:
                self._spawn_cursor(executor_id)
        elif event.new_state == "SUSPENDED":
            self._cancel_cursor(executor_id)

    # =========================================================================
    # Introspection (for metrics / observability)
    # =========================================================================

    @property
    def cursor_count(self) -> int:
        """Number of active executor cursors."""
        return sum(1 for t in self._cursors.values() if not t.done())

    @property
    def active_executors(self) -> list[str]:
        """List of executor_ids with active cursors."""
        return [eid for eid, t in self._cursors.items() if not t.done()]
