"""Minimal sync-to-async bridge for Memory brick.

Brick-local copy of the essential run_sync() function (Issue #2177).
Avoids importing nexus.core.sync_bridge which is a kernel internal.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Coroutine
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Background event loop for running coroutines from sync code
_loop: asyncio.AbstractEventLoop | None = None
_loop_thread: threading.Thread | None = None
_lock = threading.Lock()


def _get_or_create_loop() -> asyncio.AbstractEventLoop:
    """Get or create the background event loop."""
    global _loop, _loop_thread  # noqa: PLW0603

    if _loop is not None and _loop.is_running():
        return _loop

    with _lock:
        if _loop is not None and _loop.is_running():
            return _loop

        _loop = asyncio.new_event_loop()

        def _run_loop() -> None:
            assert _loop is not None
            asyncio.set_event_loop(_loop)
            _loop.run_forever()

        _loop_thread = threading.Thread(target=_run_loop, daemon=True, name="memory-sync-bridge")
        _loop_thread.start()
        return _loop


def run_sync(coro: Coroutine[Any, Any, T], timeout: float = 30.0) -> T:
    """Run an async coroutine from synchronous code.

    Detects whether we're inside an event loop and dispatches accordingly:
    - No running loop: uses asyncio.run()
    - Inside a loop: submits to background thread loop
    """
    try:
        asyncio.get_running_loop()
        # We're inside an event loop — use background thread
        loop = _get_or_create_loop()
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result(timeout=timeout)
    except RuntimeError:
        # No running event loop — safe to use asyncio.run()
        return asyncio.run(coro)
