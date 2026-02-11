"""Async task runner for the Nexus durable task queue.

Provides an asyncio-native worker pool that claims and executes tasks
from the Rust-backed TaskEngine. Workers use exponential backoff polling
(100ms -> 2s) and reset on successful claim.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProgressReporter:
    """Allows task executors to report progress back to the engine."""

    _engine: Any  # TaskEngine
    _task_id: int

    def update(self, pct: int = 0, message: str = "") -> bool:
        """Report progress. Returns False if the task was cancelled."""
        result: bool = self._engine.heartbeat(self._task_id, pct, message)
        return result


# Type alias for executor callables
Executor = Callable[[bytes, ProgressReporter], Coroutine[Any, Any, bytes | None]]


@dataclass
class AsyncTaskRunner:
    """Async worker pool that claims and executes tasks from TaskEngine.

    Usage:
        engine = TaskEngine("/tmp/tasks-db")
        runner = AsyncTaskRunner(engine, max_workers=4)

        @runner.register("sync.full")
        async def handle_sync(params: bytes, progress: ProgressReporter):
            # ... do work ...
            await progress.update(50, "halfway")
            return b"result"

        await runner.run()  # blocks until shutdown
    """

    engine: Any  # TaskEngine (Rust)
    max_workers: int = 4
    lease_secs: int = 300
    requeue_interval: float = 60.0
    _executors: dict[str, Executor] = field(default_factory=dict, init=False)
    _shutdown: bool = field(default=False, init=False)

    def register(self, task_type: str) -> Callable[[Executor], Executor]:
        """Decorator to register a task executor for a given task type."""

        def decorator(fn: Executor) -> Executor:
            self._executors[task_type] = fn
            return fn

        return decorator

    async def run(self) -> None:
        """Start the worker pool and requeue loop. Blocks until shutdown."""
        logger.info(
            "Starting AsyncTaskRunner with %d workers, lease=%ds",
            self.max_workers,
            self.lease_secs,
        )
        workers = [
            asyncio.create_task(self._worker(i), name=f"task-worker-{i}")
            for i in range(self.max_workers)
        ]
        reaper = asyncio.create_task(self._requeue_loop(), name="task-requeue-loop")

        try:
            # Wait for shutdown signal
            while not self._shutdown:
                await asyncio.sleep(1.0)
        finally:
            # Cancel workers and reaper
            for w in workers:
                w.cancel()
            reaper.cancel()
            # Wait for graceful completion
            await asyncio.gather(*workers, reaper, return_exceptions=True)
            logger.info("AsyncTaskRunner shut down")

    async def _worker(self, worker_id: int) -> None:
        """Claim -> execute -> complete/fail loop with exponential backoff."""
        backoff = 0.1
        wid = f"w-{worker_id}"

        while not self._shutdown:
            try:
                task = self.engine.claim_next(wid, self.lease_secs)
            except Exception:
                logger.exception("Error claiming task (worker %s)", wid)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 2.0)
                continue

            if task is None:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 2.0)
                continue

            # Reset backoff on successful claim
            backoff = 0.1

            executor = self._executors.get(task.task_type)
            if executor is None:
                logger.warning(
                    "No executor for task type '%s' (task_id=%d)",
                    task.task_type,
                    task.task_id,
                )
                try:
                    self.engine.fail(
                        task.task_id,
                        f"No executor registered for task type: {task.task_type}",
                    )
                except Exception:
                    logger.exception("Error failing task %d", task.task_id)
                continue

            progress = ProgressReporter(_engine=self.engine, _task_id=task.task_id)

            try:
                result = await executor(task.params, progress)
                self.engine.complete(task.task_id, result or b"")
                logger.debug("Task %d (%s) completed", task.task_id, task.task_type)
            except asyncio.CancelledError:
                # Graceful shutdown — don't fail the task, let it be requeued via lease expiry
                logger.info(
                    "Worker %s cancelled during task %d, will be requeued",
                    wid,
                    task.task_id,
                )
                raise
            except Exception as exc:
                logger.exception("Task %d (%s) failed", task.task_id, task.task_type)
                try:
                    self.engine.fail(task.task_id, str(exc))
                except Exception:
                    logger.exception("Error reporting failure for task %d", task.task_id)

    async def _requeue_loop(self) -> None:
        """Periodically reclaim tasks with expired leases."""
        while not self._shutdown:
            try:
                count = self.engine.requeue_abandoned()
                if count > 0:
                    logger.info("Requeued %d abandoned tasks", count)
            except Exception:
                logger.exception("Error in requeue loop")
            await asyncio.sleep(self.requeue_interval)

    async def shutdown(self) -> None:
        """Signal graceful shutdown. Workers will finish current tasks."""
        logger.info("Shutdown requested")
        self._shutdown = True
