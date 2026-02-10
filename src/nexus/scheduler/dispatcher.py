"""Background task dispatcher with LISTEN/NOTIFY.

Runs as a background task in the FastAPI lifespan, dispatching
queued tasks to executors. Uses PostgreSQL LISTEN/NOTIFY for
low-latency notification with a fallback poll.

Related: Issue #1212
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any

from nexus.scheduler.constants import AGING_INTERVAL_SECONDS

if TYPE_CHECKING:
    from nexus.scheduler.service import SchedulerService

logger = logging.getLogger(__name__)


class TaskDispatcher:
    """Background task dispatcher.

    Listens for new task notifications and dispatches them.
    Also runs periodic aging sweeps.
    """

    def __init__(
        self,
        scheduler_service: SchedulerService,
        db_dsn: str = "",
        poll_interval: int = 30,
    ) -> None:
        self._scheduler = scheduler_service
        self._dsn = db_dsn
        self._poll_interval = poll_interval
        self._running = False
        self._dispatch_task: asyncio.Task[None] | None = None
        self._aging_task: asyncio.Task[None] | None = None
        self._notification_event = asyncio.Event()

    async def start(self) -> None:
        """Start the dispatcher loops."""
        if self._running:
            return

        self._running = True
        logger.info("Starting task dispatcher")

        self._dispatch_task = asyncio.create_task(self._dispatch_loop())
        self._aging_task = asyncio.create_task(self._aging_loop())

        # Try to set up LISTEN/NOTIFY if DSN is available
        if self._dsn:
            asyncio.create_task(self._listen_loop())

    async def stop(self) -> None:
        """Gracefully stop the dispatcher."""
        if not self._running:
            return

        logger.info("Stopping task dispatcher")
        self._running = False
        self._notification_event.set()  # Wake up any waiting

        if self._dispatch_task:
            self._dispatch_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._dispatch_task

        if self._aging_task:
            self._aging_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._aging_task

        logger.info("Task dispatcher stopped")

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
                        },
                    )
                    # Task execution is handled by the executor
                    # For now, just log. Actual execution integration
                    # depends on the executor framework.
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
                await asyncio.sleep(1)  # Brief pause before retry

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

    async def _listen_loop(self) -> None:
        """LISTEN for task_enqueued notifications."""
        try:
            import asyncpg  # noqa: F811

            conn = await asyncpg.connect(self._dsn)
            try:
                await conn.add_listener("task_enqueued", self._on_notification)
                logger.info("Listening for task_enqueued notifications")

                while self._running:
                    await asyncio.sleep(1)
            finally:
                await conn.close()
        except ImportError:
            logger.warning("asyncpg not available, using poll-only mode")
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
