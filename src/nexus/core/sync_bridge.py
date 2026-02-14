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

    from nexus.core.sync_bridge import run_sync

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
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import threading
from collections.abc import Coroutine
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

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


def run_sync(
    coro: Coroutine[Any, Any, T],
    *,
    timeout: float | None = 30.0,
) -> T:
    """Run an async coroutine from synchronous code.

    Automatically detects whether the caller is inside an async event
    loop and chooses the correct dispatch strategy:

    * No running loop → ``asyncio.run()``
    * Running loop (e.g. FastAPI worker thread) →
      ``run_coroutine_threadsafe()`` to the background event loop.

    Args:
        coro: The coroutine to execute.
        timeout: Max seconds to wait for the result.  ``None`` means
            wait indefinitely.  Default 30 s.

    Returns:
        The coroutine's return value.

    Raises:
        RuntimeError: If the background loop fails to start.
        TimeoutError: If *timeout* is exceeded.
        Exception: Any exception raised by the coroutine is re-raised.
    """
    try:
        asyncio.get_running_loop()
        # We are inside an async context (e.g. a sync function called
        # from a FastAPI threadpool worker while the main loop runs).
        # Cannot use asyncio.run() — submit to the background loop.
        bg_loop = _ensure_background_loop()
        future = asyncio.run_coroutine_threadsafe(coro, bg_loop)
        return future.result(timeout=timeout)
    except RuntimeError:
        # No running event loop → safe to use asyncio.run().
        return asyncio.run(coro)


def fire_and_forget(coro: Coroutine[Any, Any, Any]) -> None:
    """Schedule an async coroutine without waiting for the result.

    Errors are logged but not raised.  Ideal for event dispatch, webhook
    broadcasts, and other non-critical background work.

    If a running event loop is available in the current thread, the
    coroutine is scheduled as a task on that loop.  Otherwise it is
    submitted to the background event loop.

    Args:
        coro: The coroutine to schedule.
    """
    try:
        loop = asyncio.get_running_loop()
        # We are inside the event loop — schedule directly.
        task = loop.create_task(coro)
        task.add_done_callback(_log_task_exception)
    except RuntimeError:
        # No running loop — submit to the background loop.
        bg_loop = _ensure_background_loop()
        future = asyncio.run_coroutine_threadsafe(coro, bg_loop)
        future.add_done_callback(_log_future_exception)


def _log_task_exception(task: asyncio.Task[Any]) -> None:
    """Callback to log unhandled exceptions from fire-and-forget tasks."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning("fire_and_forget task failed: %s", exc, exc_info=exc)


def _log_future_exception(future: Any) -> None:
    """Callback to log unhandled exceptions from threadsafe futures."""
    if future.cancelled():
        return
    exc = future.exception()
    if exc is not None:
        logger.warning("fire_and_forget future failed: %s", exc, exc_info=exc)


def shutdown_sync_bridge() -> None:
    """Shut down the background event loop.

    Safe to call multiple times.  Blocks until the background thread
    exits (up to 5 s).
    """
    global _bg_loop, _bg_thread

    with _bg_lock:
        loop = _bg_loop
        thread = _bg_thread
        _bg_loop = None
        _bg_thread = None

    if loop is not None and loop.is_running():
        loop.call_soon_threadsafe(loop.stop)

    if thread is not None:
        thread.join(timeout=5.0)

    logger.debug("Background sync-bridge event loop stopped")


# Auto-shutdown on interpreter exit to avoid ResourceWarnings.
atexit.register(shutdown_sync_bridge)
