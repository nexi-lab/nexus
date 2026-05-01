"""Background worker that drains the activity queue into sinks."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Sequence

from nexus.services.activity.events import ActivityEvent
from nexus.services.activity.sinks.protocol import SinkProtocol

logger = logging.getLogger(__name__)


class ActivityWorker:
    """Drain queue into sinks with batching/timeout.

    Lifecycle:
    - start() creates the consumer asyncio.Task.
    - stop(timeout) signals shutdown, drains pending events, awaits exit.
    """

    def __init__(
        self,
        *,
        queue: asyncio.Queue[ActivityEvent],
        sinks: Sequence[SinkProtocol],
        batch_size: int = 200,
        batch_timeout_s: float = 0.5,
    ) -> None:
        self._queue = queue
        self._sinks = list(sinks)
        self._batch_size = batch_size
        self._batch_timeout_s = batch_timeout_s
        self._stopping = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._consume())
        self._task.add_done_callback(self._on_task_done)

    def _on_task_done(self, task: asyncio.Task[None]) -> None:
        """Surface unexpected worker death via the sink-errors counter.

        ``stop()`` sets ``_stopping`` before letting the task exit, so a
        completion with the flag clear is a crash. The component-level
        health flag and /metrics need an alertable signal so operators
        notice that activity persistence stopped happening.
        """
        if self._stopping.is_set():
            return
        if task.cancelled():
            logger.warning("activity worker cancelled unexpectedly")
        else:
            exc = task.exception()
            if exc is not None:
                logger.error("activity worker died unexpectedly", exc_info=exc)
            else:
                logger.warning("activity worker exited without stop signal")
        try:
            from nexus.services.activity.metrics import ACTIVITY_SINK_ERRORS

            ACTIVITY_SINK_ERRORS.labels(sink="ActivityWorker").inc()
        except Exception:
            pass

    def is_healthy(self) -> bool:
        return self._task is not None and not self._task.done()

    async def stop(self, *, timeout: float = 10.0) -> None:
        """Drain the queue and close sinks.

        Cancellation cannot stop the SQLite executor thread mid-write, so
        cancelling the consumer task while it is awaiting ``write_batch``
        would let the thread keep using the connection while ``close()``
        runs against it. Instead we set the stopping flag and wait for the
        consumer to exit naturally — it polls the flag between batches and
        terminates after the current flush completes. ``timeout`` is a
        soft budget: when it elapses we log a warning and keep waiting,
        because closing under a wedged write is the worse failure mode.
        """
        self._stopping.set()
        if self._task is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(self._task), timeout=timeout)
        except TimeoutError:
            logger.warning(
                "activity worker drain exceeded %ss; waiting for in-flight write to finish",
                timeout,
            )
            with contextlib.suppress(Exception):
                await self._task
        self._task = None
        for sink in self._sinks:
            with contextlib.suppress(Exception):
                await sink.close()

    async def _consume(self) -> None:
        while not self._stopping.is_set():
            batch = await self._collect_batch()
            if batch:
                await self._flush(batch)
        # Drain remaining events once stopping is set.
        remainder: list[ActivityEvent] = []
        while not self._queue.empty():
            try:
                remainder.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
            if len(remainder) >= self._batch_size:
                await self._flush(remainder)
                remainder = []
        if remainder:
            await self._flush(remainder)

    async def _get_with_stop(self, timeout: float) -> ActivityEvent | None:
        """Get one event, returning None on timeout or when stopping is set.

        Wakes early if the stopping event fires; cancels the queue get without
        losing items that landed in flight.
        """
        getter = asyncio.create_task(self._queue.get())
        stopper = asyncio.create_task(self._stopping.wait())
        try:
            await asyncio.wait(
                {getter, stopper},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            if not stopper.done():
                stopper.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await stopper
        if getter.done():
            return getter.result()
        # Timeout or stop fired before queue produced — cancel the getter.
        getter.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await getter
        return None

    async def _collect_batch(self) -> list[ActivityEvent]:
        first = await self._get_with_stop(self._batch_timeout_s)
        if first is None:
            return []
        batch = [first]
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._batch_timeout_s
        while len(batch) < self._batch_size:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            nxt = await self._get_with_stop(remaining)
            if nxt is None:
                break
            batch.append(nxt)
        return batch

    async def _flush(self, batch: list[ActivityEvent]) -> None:
        for sink in self._sinks:
            try:
                await sink.write_batch(batch)
            except Exception:
                try:
                    from nexus.services.activity.metrics import ACTIVITY_SINK_ERRORS

                    ACTIVITY_SINK_ERRORS.labels(sink=type(sink).__name__).inc()
                except Exception:
                    pass
                logger.warning(
                    "activity sink %s failed batch write", type(sink).__name__, exc_info=True
                )
