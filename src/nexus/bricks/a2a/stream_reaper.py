"""Periodic cleanup of abandoned A2A SSE streams.

Runs as a background asyncio.Task, checking for idle streams every
``check_interval`` seconds and closing any that have been idle longer
than ``max_idle_seconds``.

Issue #2811: A2A abandoned stream cleanup.
"""

import asyncio
import logging
from contextlib import suppress as _suppress

from nexus.bricks.a2a.stream_registry import StreamRegistry

logger = logging.getLogger(__name__)

# Defaults
_DEFAULT_CHECK_INTERVAL = 60.0  # seconds
_DEFAULT_MAX_IDLE = 300.0  # 5 minutes


class StreamReaper:
    """Periodic reaper for idle A2A SSE streams.

    Parameters
    ----------
    registry:
        The StreamRegistry to monitor.
    max_idle_seconds:
        Streams idle longer than this are reaped (default: 300s).
    check_interval:
        How often to check for idle streams (default: 60s).
    """

    def __init__(
        self,
        registry: StreamRegistry,
        max_idle_seconds: float = _DEFAULT_MAX_IDLE,
        check_interval: float = _DEFAULT_CHECK_INTERVAL,
    ) -> None:
        self._registry = registry
        self._max_idle_seconds = max_idle_seconds
        self._check_interval = check_interval
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start the reaper background task."""
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name="stream-reaper")
        logger.info(
            "StreamReaper started (interval=%.0fs, max_idle=%.0fs)",
            self._check_interval,
            self._max_idle_seconds,
        )

    async def stop(self) -> None:
        """Stop the reaper and wait for clean shutdown."""
        if self._task is None:
            return
        self._task.cancel()
        with _suppress(asyncio.CancelledError):
            await self._task
        self._task = None
        logger.info("StreamReaper stopped")

    async def _run(self) -> None:
        """Main reaper loop."""
        try:
            while True:
                await asyncio.sleep(self._check_interval)
                self._reap()
        except asyncio.CancelledError:
            raise  # Let cancellation propagate

    def _reap(self) -> None:
        """Check for and close idle streams."""
        idle_tasks = self._registry.get_idle_tasks(self._max_idle_seconds)
        if not idle_tasks:
            return

        total_closed = 0
        for task_id in idle_tasks:
            closed = self._registry.close_task_streams(task_id, reason="idle_timeout")
            total_closed += closed

        if total_closed > 0:
            logger.info(
                "Reaped %d idle stream(s) across %d task(s)",
                total_closed,
                len(idle_tasks),
            )

    @property
    def is_running(self) -> bool:
        """Whether the reaper task is active."""
        return self._task is not None and not self._task.done()
