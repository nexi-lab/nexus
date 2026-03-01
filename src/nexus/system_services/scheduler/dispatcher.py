"""Background task dispatcher with LISTEN/NOTIFY.

Runs as a background task in the FastAPI lifespan, dispatching
queued tasks to executors. Uses PostgreSQL LISTEN/NOTIFY for
low-latency notification with a fallback poll.

Uses asyncio.TaskGroup for structured concurrency (Issue #1274).

Related: Issue #1212, #1274
"""

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any

from nexus.system_services.scheduler.constants import (
    AGING_INTERVAL_SECONDS,
    STARVATION_PROMOTION_THRESHOLD_SECS,
)

if TYPE_CHECKING:
    from nexus.system_services.scheduler.service import SchedulerService

logger = logging.getLogger(__name__)

# Starvation promotion runs every 5 minutes
_STARVATION_CHECK_INTERVAL = 300


class TaskDispatcher:
    """Background task dispatcher.

    Listens for new task notifications and dispatches them.
    Also runs periodic aging sweeps and starvation promotion.
    Uses asyncio.TaskGroup for structured lifecycle management.
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
        self._notification_event = asyncio.Event()

    async def start(self) -> None:
        """Start the dispatcher loops."""
        if self._running:
            return

        self._running = True
        logger.info("Starting task dispatcher")

        self._task_group_task = asyncio.create_task(self._run_all())

    async def stop(self) -> None:
        """Gracefully stop the dispatcher."""
        if not self._running:
            return

        logger.info("Stopping task dispatcher")
        self._running = False
        self._notification_event.set()

        if self._task_group_task:
            self._task_group_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task_group_task

        logger.info("Task dispatcher stopped")

    async def _run_all(self) -> None:
        """Run all background loops in a TaskGroup."""
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self._dispatch_loop())
                tg.create_task(self._aging_loop())
                tg.create_task(self._starvation_loop())
                if self._record_store is not None:
                    tg.create_task(self._listen_loop())
        except* asyncio.CancelledError:
            logger.info("Dispatcher TaskGroup cancelled")
        except* Exception as eg:
            for exc in eg.exceptions:
                logger.exception("Dispatcher loop failed: %s", exc)

    async def _dispatch_loop(self) -> None:
        """Main dispatch loop: dequeue and process tasks."""
        while self._running:
            try:
                task = await self._scheduler.dequeue_next()
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
                    continue  # Try to dequeue another immediately

                # No tasks available, wait for notification or timeout
                self._notification_event.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._notification_event.wait(),
                        timeout=self._poll_interval,
                    )

            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error in dispatch loop")
                await asyncio.sleep(1)

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

    async def _listen_loop(self) -> None:
        """LISTEN for task_enqueued notifications via RecordStoreABC (Issue #608).

        Uses the async engine from RecordStoreABC to obtain a dedicated raw
        connection for PostgreSQL LISTEN/NOTIFY. A long-lived connection is
        required because LISTEN subscriptions are per-connection.
        """
        if self._record_store is None:
            return

        try:
            engine = self._record_store._async_engine
            if engine is None:
                # Trigger lazy async engine creation
                _ = self._record_store.async_session_factory
                engine = self._record_store._async_engine
            if engine is None:
                logger.warning("No async engine available, using poll-only mode")
                return

            # Acquire a raw connection from the async engine for LISTEN
            async with engine.connect() as sa_conn:
                raw = await sa_conn.get_raw_connection()
                driver_conn = raw.driver_connection
                await driver_conn.add_listener("task_enqueued", self._on_notification)
                logger.info("Listening for task_enqueued notifications")

                while self._running:
                    await asyncio.sleep(1)
        except Exception:
            logger.exception("LISTEN connection failed, falling back to polling")

    def _on_notification(
        self,
        _connection: Any,
        _pid: int,
        _channel: str,
        _payload: str,
    ) -> None:
        """Handle NOTIFY callback."""
        self._notification_event.set()
