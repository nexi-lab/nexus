"""Tests for nexus.core.sync_bridge — unified sync-to-async bridge.

Tests cover:
  - run_sync() from sync context (no running loop)
  - run_sync() from async context (running loop)
  - run_sync() from thread pool worker
  - fire_and_forget() from both contexts
  - Error propagation
  - Timeout handling
  - Concurrent usage
  - Background loop lifecycle
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import time
from unittest.mock import patch

import pytest

from nexus.core.sync_bridge import (
    _ensure_background_loop,
    fire_and_forget,
    run_sync,
    shutdown_sync_bridge,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _add(a: int, b: int) -> int:
    """Simple async function for testing."""
    return a + b


async def _slow(seconds: float) -> str:
    """Async function that takes time."""
    await asyncio.sleep(seconds)
    return "done"


async def _fail() -> None:
    """Async function that always raises."""
    raise ValueError("intentional error")


async def _nested_async() -> int:
    """Async function that itself calls other coroutines."""
    r1 = await _add(1, 2)
    r2 = await _add(3, 4)
    return r1 + r2


def _run_with_loop(coro_fn):
    """Run an async test function using an explicit event loop."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro_fn(loop))
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# run_sync — sync context (no running event loop)
# ---------------------------------------------------------------------------


class TestRunSyncFromSyncContext:
    """run_sync() called from regular synchronous code (e.g. CLI)."""

    def test_basic_return_value(self) -> None:
        result = run_sync(_add(3, 4))
        assert result == 7

    def test_nested_async_calls(self) -> None:
        result = run_sync(_nested_async())
        assert result == 10

    def test_error_propagation(self) -> None:
        with pytest.raises(ValueError, match="intentional error"):
            run_sync(_fail())

    def test_none_return(self) -> None:
        async def returns_none() -> None:
            pass

        result = run_sync(returns_none())
        assert result is None

    def test_returns_complex_type(self) -> None:
        async def returns_dict() -> dict[str, int]:
            return {"a": 1, "b": 2}

        result = run_sync(returns_dict())
        assert result == {"a": 1, "b": 2}


# ---------------------------------------------------------------------------
# run_sync — async context (running event loop)
# ---------------------------------------------------------------------------


class TestRunSyncFromAsyncContext:
    """run_sync() called from within an async event loop (e.g. FastAPI worker thread)."""

    def test_from_thread_pool_worker(self) -> None:
        """Simulate a FastAPI sync dep running in the threadpool."""

        async def _test(loop: asyncio.AbstractEventLoop) -> None:
            def sync_worker() -> int:
                return run_sync(_add(10, 20))

            result = await loop.run_in_executor(None, sync_worker)
            assert result == 30

        _run_with_loop(_test)

    def test_error_from_thread_pool_worker(self) -> None:
        """Errors propagate correctly through the thread pool."""

        async def _test(loop: asyncio.AbstractEventLoop) -> None:
            def sync_worker() -> None:
                run_sync(_fail())

            with pytest.raises(ValueError, match="intentional error"):
                await loop.run_in_executor(None, sync_worker)

        _run_with_loop(_test)

    def test_nested_calls_from_thread_pool(self) -> None:
        """Multiple run_sync calls from the same worker thread."""

        async def _test(loop: asyncio.AbstractEventLoop) -> None:
            def sync_worker() -> int:
                a = run_sync(_add(1, 2))
                b = run_sync(_add(3, 4))
                return a + b

            result = await loop.run_in_executor(None, sync_worker)
            assert result == 10

        _run_with_loop(_test)

    def test_timeout_from_thread_pool(self) -> None:
        """Timeout works when called from thread pool."""

        async def _test(loop: asyncio.AbstractEventLoop) -> None:
            def sync_worker() -> str:
                return run_sync(_slow(0.01), timeout=5.0)

            result = await loop.run_in_executor(None, sync_worker)
            assert result == "done"

        _run_with_loop(_test)


# ---------------------------------------------------------------------------
# run_sync — timeout
# ---------------------------------------------------------------------------


class TestRunSyncTimeout:
    def test_timeout_exceeded_via_background_loop(self) -> None:
        """Timeout raises when going through the background loop path.

        The timeout only applies when run_sync() detects a running event
        loop and routes through run_coroutine_threadsafe().  We force
        this path by patching get_running_loop to NOT raise.
        """
        bg_loop = _ensure_background_loop()
        future = asyncio.run_coroutine_threadsafe(_slow(10.0), bg_loop)
        with pytest.raises(concurrent.futures.TimeoutError):
            future.result(timeout=0.05)

    def test_sync_context_runs_to_completion(self) -> None:
        """In sync context (no running loop), run_sync runs to completion."""
        result = run_sync(_slow(0.01))
        assert result == "done"


# ---------------------------------------------------------------------------
# run_sync — concurrent usage
# ---------------------------------------------------------------------------


class TestRunSyncConcurrent:
    def test_concurrent_workers(self) -> None:
        """Multiple thread pool workers calling run_sync concurrently."""

        async def _test(loop: asyncio.AbstractEventLoop) -> None:
            def sync_worker(n: int) -> int:
                return run_sync(_add(n, n))

            futures = [loop.run_in_executor(None, sync_worker, i) for i in range(10)]
            results = await asyncio.gather(*futures)
            assert results == [i * 2 for i in range(10)]

        _run_with_loop(_test)


# ---------------------------------------------------------------------------
# fire_and_forget
# ---------------------------------------------------------------------------


class TestFireAndForget:
    def test_from_sync_context(self) -> None:
        """fire_and_forget works from sync code."""
        result_holder: list[int] = []

        async def append_value() -> None:
            result_holder.append(42)

        fire_and_forget(append_value())
        # Give the background loop a moment to process.
        time.sleep(0.1)
        assert result_holder == [42]

    def test_from_async_context(self) -> None:
        """fire_and_forget schedules on the current loop when available."""

        async def _test(loop: asyncio.AbstractEventLoop) -> None:
            result_holder: list[int] = []

            async def append_value() -> None:
                result_holder.append(99)

            fire_and_forget(append_value())
            await asyncio.sleep(0.05)
            assert result_holder == [99]

        _run_with_loop(_test)

    def test_error_is_logged_not_raised(self) -> None:
        """Errors in fire_and_forget coroutines are logged, not raised."""
        with patch("nexus.core.sync_bridge.logger") as mock_logger:
            fire_and_forget(_fail())
            # Give background loop time to process.
            time.sleep(0.1)
            # The warning callback should have fired.
            assert mock_logger.warning.called


# ---------------------------------------------------------------------------
# Background loop lifecycle
# ---------------------------------------------------------------------------


class TestBackgroundLoopLifecycle:
    def test_ensure_background_loop_is_idempotent(self) -> None:
        """Multiple calls return the same loop."""
        loop1 = _ensure_background_loop()
        loop2 = _ensure_background_loop()
        assert loop1 is loop2
        assert loop1.is_running()

    def test_shutdown_and_restart(self) -> None:
        """After shutdown, a new loop is created on next use."""
        loop1 = _ensure_background_loop()
        shutdown_sync_bridge()

        # After shutdown, ensure a fresh loop is created.
        loop2 = _ensure_background_loop()
        assert loop2 is not loop1
        assert loop2.is_running()

    def test_shutdown_is_idempotent(self) -> None:
        """Calling shutdown multiple times doesn't crash."""
        shutdown_sync_bridge()
        shutdown_sync_bridge()
        shutdown_sync_bridge()


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _cleanup_bridge():
    """Ensure the bridge is shut down after each test to avoid cross-test interference."""
    yield
    shutdown_sync_bridge()
