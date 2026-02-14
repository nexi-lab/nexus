"""Shared async bridge utility (Issue #1287, Phase 0.5).

Consolidates three divergent async-to-sync implementations:
- NexusFS._run_async (nexus_fs.py:6107)
- NexusFSMCPMixin._run_async_mcp_operation (nexus_fs_mcp.py:25)
- RPCRequestHandler._run_async_safe (rpc_server.py:259)

Provides a single, well-tested bridge for running async coroutines
from synchronous contexts, handling the "event loop already running"
edge case safely.

References:
    - Issue #1287: Extract NexusFS domain services from god object
    - Decision 5: Shared utilities
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Shared thread pool for async bridge operations (lazy-initialized)
_executor: concurrent.futures.ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()
_POOL_SIZE = 16


def _get_executor() -> concurrent.futures.ThreadPoolExecutor:
    """Get or create the shared thread pool executor.

    Uses double-checked locking for thread-safe lazy initialization.
    """
    global _executor
    if _executor is None:
        with _executor_lock:
            if _executor is None:
                _executor = concurrent.futures.ThreadPoolExecutor(
                    max_workers=_POOL_SIZE,
                    thread_name_prefix="nexus-async-bridge",
                )
    return _executor


def run_async(coro: Any) -> Any:
    """Run an async coroutine from a synchronous context.

    Handles two scenarios:
    1. No running event loop: Creates a fresh loop via asyncio.run()
    2. Already in an event loop: Submits to a shared ThreadPoolExecutor
       to avoid "loop is already running" errors.

    This is the preferred replacement for:
    - NexusFS._run_async()
    - NexusFSMCPMixin._run_async_mcp_operation()

    Args:
        coro: Coroutine to run.

    Returns:
        Result of the coroutine.

    Raises:
        Any exception raised by the coroutine.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # No running loop — safe to use asyncio.run directly
        return asyncio.run(coro)

    # Already in a running loop — offload to thread pool
    logger.debug("async_bridge: running loop detected, offloading to thread pool")
    executor = _get_executor()
    future = executor.submit(asyncio.run, coro)
    return future.result()


def run_async_safe(coro: Any) -> Any:
    """Run an async coroutine safely in a threaded server context.

    Always uses the shared ThreadPoolExecutor with asyncio.run() per call.
    This is the preferred replacement for RPCRequestHandler._run_async_safe().

    Use this variant when you know you're in a threaded context (e.g.,
    ThreadingHTTPServer request handlers) where each request thread needs
    its own event loop.

    Args:
        coro: Coroutine to run.

    Returns:
        Result of the coroutine.

    Raises:
        Any exception raised by the coroutine.
    """
    executor = _get_executor()
    future = executor.submit(asyncio.run, coro)
    return future.result()


def shutdown_executor() -> None:
    """Shut down the shared thread pool executor.

    Call during application shutdown for clean resource cleanup.
    """
    global _executor
    if _executor is not None:
        _executor.shutdown(wait=False)
        _executor = None
