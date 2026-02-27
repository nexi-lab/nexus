"""Sync-to-Async Bridge Utility.

Provides a unified ``run_sync()`` function for calling async code from
synchronous contexts.  The implementation detects the calling context
(sync thread vs. async event loop) and dispatches accordingly:

* **No running event loop** – uses ``asyncio.run()`` (optimal for CLI).
* **Inside an async event loop** – submits to a shared background event
  loop thread via ``asyncio.run_coroutine_threadsafe()`` and blocks on
  the ``Future``.  This avoids the "cannot call asyncio.run() from a
  running event loop" crash.

The background event loop is lazily created on first use and can be
cleanly shut down via ``shutdown_sync_bridge()``.

Usage::

    from nexus.lib.sync_bridge import run_sync

    # Works from ANY context (sync, async, thread pool worker)
    result = run_sync(some_async_function(arg1, arg2))

    # With timeout
    result = run_sync(some_async_function(), timeout=5.0)

    # Fire-and-forget (non-blocking, errors logged)
    fire_and_forget(some_async_function())

Design:
    Follows the AnyIO ``BlockingPortal`` pattern, adapted as a module-
    level singleton so it can be used without plumbing a portal through
    the call stack.  Compatible with uvloop.

    fire_and_forget() is bounded by a TaskRegistry (Issue #1519, 13A)
    that limits concurrent background tasks via asyncio.Semaphore and
    tracks pending tasks for graceful shutdown.
"""

import asyncio
import atexit
import logging
import threading
from collections.abc import Coroutine
from typing import Any, TypeVar, cast, overload

logger = logging.getLogger(__name__)

T = TypeVar("T")

# ---------------------------------------------------------------------------
# TaskRegistry — bounded task tracker for fire-and-forget (Issue #1519)
# ---------------------------------------------------------------------------


class TaskRegistry:
    """Bounded registry for fire-and-forget tasks.

    Prevents unbounded task spawning by gating coroutines behind a
    per-event-loop ``asyncio.Semaphore``.  Tracks all pending tasks
    for graceful shutdown via ``drain()``.

    Thread-safe: multiple threads may call ``schedule()`` concurrently.
    """

    def __init__(
        self,
        max_concurrent: int = 100,
        warn_threshold: int = 80,
    ) -> None:
        self._max_concurrent = max_concurrent
        self._warn_threshold = warn_threshold
        self._tasks: set[asyncio.Task[Any]] = set()
        self._lock = threading.Lock()
        # Per-loop semaphores (asyncio.Semaphore is bound to one loop)
        self._semaphores: dict[int, asyncio.Semaphore] = {}

    def _get_semaphore(self, loop: asyncio.AbstractEventLoop) -> asyncio.Semaphore:
        loop_id = id(loop)
        if loop_id not in self._semaphores:
            with self._lock:
                if loop_id not in self._semaphores:
                    self._semaphores[loop_id] = asyncio.Semaphore(self._max_concurrent)
        return self._semaphores[loop_id]

    @property
    def pending_count(self) -> int:
        """Number of currently tracked fire-and-forget tasks."""
        return len(self._tasks)

    def schedule_on_loop(
        self,
        coro: Coroutine[Any, Any, Any],
        loop: asyncio.AbstractEventLoop,
    ) -> asyncio.Task[Any]:
        """Schedule *coro* as a bounded task on *loop*.

        Must be called from within *loop*'s thread (i.e. the loop is
        running in the current thread).
        """
        sem = self._get_semaphore(loop)

        async def _guarded() -> None:
            async with sem:
                await coro

        task = loop.create_task(_guarded())
        self._tasks.add(task)
        task.add_done_callback(self._on_task_done)

        count = len(self._tasks)
        if count >= self._warn_threshold:
            logger.warning(
                "fire_and_forget backlog: %d pending tasks (warn_threshold=%d)",
                count,
                self._warn_threshold,
            )
        return task

    def schedule_threadsafe(
        self,
        coro: Coroutine[Any, Any, Any],
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        """Schedule *coro* from a thread that is NOT running *loop*.

        Uses ``call_soon_threadsafe`` to create the task on the correct
        loop thread.
        """
        loop.call_soon_threadsafe(self.schedule_on_loop, coro, loop)

    def _on_task_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.warning("fire_and_forget task failed: %s", exc, exc_info=exc)

    async def drain(self, timeout: float = 10.0) -> int:
        """Await all pending tasks, cancelling stragglers after *timeout*.

        Returns the number of tasks that completed successfully.
        """
        if not self._tasks:
            return 0

        tasks = list(self._tasks)
        count = len(tasks)
        logger.info("Draining %d fire_and_forget tasks (timeout=%.1fs)", count, timeout)

        done, pending = await asyncio.wait(tasks, timeout=timeout)
        if pending:
            logger.warning(
                "Drain timed out: %d/%d tasks still pending after %.1fs — cancelling",
                len(pending),
                count,
                timeout,
            )
            for t in pending:
                t.cancel()
            # Allow cancellations to propagate
            await asyncio.gather(*pending, return_exceptions=True)

        return len(done)


# Module-level singleton
_task_registry = TaskRegistry()


def get_task_registry() -> TaskRegistry:
    """Return the module-level TaskRegistry for observability."""
    return _task_registry


# ---------------------------------------------------------------------------
# Background event loop singleton
# ---------------------------------------------------------------------------
_bg_loop: asyncio.AbstractEventLoop | None = None
_bg_thread: threading.Thread | None = None
_bg_lock = threading.Lock()


def _ensure_background_loop() -> asyncio.AbstractEventLoop:
    """Lazily create the background event loop thread.

    Thread-safe, idempotent.  Returns the loop that is guaranteed to be
    running in a daemon thread.
    """
    global _bg_loop, _bg_thread

    if _bg_loop is not None and _bg_loop.is_running():
        return _bg_loop

    with _bg_lock:
        # Double-check after acquiring the lock.
        if _bg_loop is not None and _bg_loop.is_running():
            return _bg_loop

        loop = asyncio.new_event_loop()
        thread = threading.Thread(
            target=_run_bg_loop,
            args=(loop,),
            daemon=True,
            name="nexus-sync-bridge",
        )
        thread.start()

        # Wait until the loop is actually running.
        _started = threading.Event()
        loop.call_soon_threadsafe(_started.set)
        _started.wait(timeout=5.0)

        _bg_loop = loop
        _bg_thread = thread
        logger.debug("Background sync-bridge event loop started (thread=%s)", thread.ident)
        return loop


def _run_bg_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Entry point for the background thread."""
    asyncio.set_event_loop(loop)
    try:
        loop.run_forever()
    finally:
        # Drain remaining tasks before closing.
        pending = asyncio.all_tasks(loop)
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@overload
def run_sync(coro: Coroutine[Any, Any, T], *, timeout: float | None = ...) -> T: ...
@overload
def run_sync(coro: T, *, timeout: float | None = ...) -> T: ...


def run_sync(
    coro: Coroutine[Any, Any, T] | T,
    *,
    timeout: float | None = 30.0,
) -> T:
    """Run an async coroutine from synchronous code.

    Automatically detects whether the caller is inside an async event
    loop and chooses the correct dispatch strategy:

    * No running loop → ``asyncio.run()``
    * Running loop (e.g. FastAPI worker thread) →
      ``run_coroutine_threadsafe()`` to the background event loop.

    If *coro* is not actually a coroutine (e.g. ``RemoteServiceProxy``
    already resolved the RPC synchronously), the value is returned as-is.
    This lets callers use ``run_sync()`` uniformly regardless of whether
    the underlying service is local (async) or remote (sync RPC proxy).

    Args:
        coro: The coroutine to execute, or an already-resolved value.
        timeout: Max seconds to wait for the result.  ``None`` means
            wait indefinitely.  Default 30 s.

    Returns:
        The coroutine's return value, or *coro* itself if not a coroutine.

    Raises:
        RuntimeError: If the background loop fails to start.
        TimeoutError: If *timeout* is exceeded.
        Exception: Any exception raised by the coroutine is re-raised.
    """
    # If the caller passed a non-coroutine (e.g. RemoteServiceProxy already
    # resolved the RPC synchronously), return the value as-is.
    if not asyncio.iscoroutine(coro):
        return coro

    # Narrow type for mypy: after iscoroutine check, coro is a Coroutine.
    real_coro = cast(Coroutine[Any, Any, T], coro)

    try:
        asyncio.get_running_loop()
        # We are inside an async context (e.g. a sync function called
        # from a FastAPI threadpool worker while the main loop runs).
        # Cannot use asyncio.run() — submit to the background loop.
        bg_loop = _ensure_background_loop()
        future = asyncio.run_coroutine_threadsafe(real_coro, bg_loop)
        return future.result(timeout=timeout)
    except RuntimeError:
        # No running event loop → safe to use asyncio.run().
        return asyncio.run(real_coro)


def fire_and_forget(coro: Coroutine[Any, Any, Any]) -> None:
    """Schedule an async coroutine without waiting for the result.

    Errors are logged but not raised.  Ideal for event dispatch, webhook
    broadcasts, and other non-critical background work.

    Bounded by ``TaskRegistry`` (Issue #1519): concurrent tasks are limited
    by a semaphore, and all pending tasks are tracked for graceful shutdown
    via ``shutdown_sync_bridge()``.

    If a running event loop is available in the current thread, the
    coroutine is scheduled as a task on that loop.  Otherwise it is
    submitted to the background event loop.

    Args:
        coro: The coroutine to schedule.
    """
    try:
        loop = asyncio.get_running_loop()
        # We are inside the event loop — schedule directly via registry.
        _task_registry.schedule_on_loop(coro, loop)
    except RuntimeError:
        # No running loop — submit to the background loop via registry.
        bg_loop = _ensure_background_loop()
        _task_registry.schedule_threadsafe(coro, bg_loop)


def shutdown_sync_bridge() -> None:
    """Shut down the background event loop after draining pending tasks.

    Safe to call multiple times.  Drains fire-and-forget tasks (up to 10 s),
    then stops the loop and joins the thread (up to 5 s).
    """
    global _bg_loop, _bg_thread

    with _bg_lock:
        loop = _bg_loop
        thread = _bg_thread
        _bg_loop = None
        _bg_thread = None

    if loop is not None and loop.is_running():
        # Drain tracked tasks before stopping the loop.
        pending = _task_registry.pending_count
        if pending > 0:
            logger.info("Shutting down sync-bridge: draining %d pending tasks", pending)
            try:
                future = asyncio.run_coroutine_threadsafe(_task_registry.drain(timeout=10.0), loop)
                future.result(timeout=12.0)  # 10s drain + 2s margin
            except Exception:
                logger.warning(
                    "Failed to drain fire_and_forget tasks during shutdown",
                    exc_info=True,
                )

        loop.call_soon_threadsafe(loop.stop)

    if thread is not None:
        thread.join(timeout=5.0)

    logger.debug("Background sync-bridge event loop stopped")


# Auto-shutdown on interpreter exit to avoid ResourceWarnings.
atexit.register(shutdown_sync_bridge)
