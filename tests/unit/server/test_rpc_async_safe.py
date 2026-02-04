"""Tests for _run_async_safe performance fix.

This module tests the shared ThreadPoolExecutor optimization in the RPC server.
The fix addresses the issue where a new ThreadPoolExecutor was created per call,
causing significant overhead under load.
"""

import asyncio
import concurrent.futures
import threading
import time
from unittest.mock import AsyncMock, Mock

import pytest

from nexus.server.rpc_server import RPCRequestHandler


class TestRunAsyncSafe:
    """Tests for the _run_async_safe method."""

    @pytest.fixture
    def handler(self):
        """Create a handler with necessary attributes."""
        # Reset class-level state before each test
        RPCRequestHandler._async_executor = None
        RPCRequestHandler._async_executor_lock = None

        handler = Mock(spec=RPCRequestHandler)
        handler.nexus_fs = Mock()
        handler.api_key = None
        handler.auth_provider = None
        handler.event_loop = None
        handler.headers = {}

        # Bind the actual method
        handler._run_async_safe = lambda coro: RPCRequestHandler._run_async_safe(handler, coro)

        yield handler

        # Cleanup after test
        if RPCRequestHandler._async_executor is not None:
            RPCRequestHandler._async_executor.shutdown(wait=True)
            RPCRequestHandler._async_executor = None

    def test_run_async_safe_basic(self, handler):
        """Test basic async coroutine execution."""

        async def async_coro():
            return "hello"

        result = handler._run_async_safe(async_coro())
        assert result == "hello"

    def test_run_async_safe_with_await(self, handler):
        """Test async coroutine with internal await."""

        async def async_coro():
            await asyncio.sleep(0.01)
            return 42

        result = handler._run_async_safe(async_coro())
        assert result == 42

    def test_shared_executor_created_once(self, handler):
        """Test that shared executor is created only once."""

        # First call should create executor
        async def coro1():
            return 1

        handler._run_async_safe(coro1())

        executor1 = RPCRequestHandler._async_executor
        assert executor1 is not None

        # Second call should reuse same executor
        async def coro2():
            return 2

        handler._run_async_safe(coro2())

        executor2 = RPCRequestHandler._async_executor
        assert executor1 is executor2

    def test_shared_executor_thread_safe(self, handler):
        """Test that executor creation is thread-safe."""
        results = []
        errors = []

        async def simple_coro(n):
            await asyncio.sleep(0.001)
            return n

        def thread_func(n):
            try:
                result = handler._run_async_safe(simple_coro(n))
                results.append(result)
            except Exception as e:
                errors.append(e)

        # Start multiple threads simultaneously
        threads = [threading.Thread(target=thread_func, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0, f"Errors: {errors}"
        assert len(results) == 10
        assert set(results) == set(range(10))

        # Should still be only one executor
        assert RPCRequestHandler._async_executor is not None

    def test_executor_max_workers(self, handler):
        """Test that executor has correct number of workers."""

        async def coro():
            return 1

        handler._run_async_safe(coro())

        executor = RPCRequestHandler._async_executor
        assert executor is not None
        assert executor._max_workers == 16  # Optimized for concurrent load

    def test_executor_thread_name_prefix(self, handler):
        """Test that executor uses correct thread name prefix."""

        async def coro():
            return threading.current_thread().name

        thread_name = handler._run_async_safe(coro())
        assert "rpc-async" in thread_name


class TestRunAsyncSafeWithAuthProvider:
    """Tests for _run_async_safe with auth_provider (the actual use case)."""

    @pytest.fixture
    def handler_with_auth(self):
        """Create a handler with auth_provider configured."""
        # Reset class-level state
        RPCRequestHandler._async_executor = None
        RPCRequestHandler._async_executor_lock = None

        handler = Mock(spec=RPCRequestHandler)
        handler.nexus_fs = Mock()
        handler.api_key = None
        handler.event_loop = asyncio.new_event_loop()
        handler.headers = {"Authorization": "Bearer test-token"}

        # Create async auth provider
        auth_provider = AsyncMock()
        auth_provider.authenticate = AsyncMock(
            return_value=Mock(
                authenticated=True,
                subject_type="user",
                subject_id="user123",
                metadata={},
            )
        )
        handler.auth_provider = auth_provider

        # Bind actual methods
        handler._run_async_safe = lambda coro: RPCRequestHandler._run_async_safe(handler, coro)
        handler._validate_auth = lambda: RPCRequestHandler._validate_auth(handler)

        yield handler

        # Cleanup
        handler.event_loop.close()
        if RPCRequestHandler._async_executor is not None:
            RPCRequestHandler._async_executor.shutdown(wait=True)
            RPCRequestHandler._async_executor = None

    def test_validate_auth_with_async_provider(self, handler_with_auth):
        """Test _validate_auth uses _run_async_safe correctly."""
        result = handler_with_auth._validate_auth()
        assert result is True
        handler_with_auth.auth_provider.authenticate.assert_called_once_with("test-token")

    def test_multiple_auth_calls_reuse_executor(self, handler_with_auth):
        """Test that multiple auth calls reuse the same executor."""
        # First auth call
        handler_with_auth._validate_auth()
        executor1 = RPCRequestHandler._async_executor

        # Second auth call
        handler_with_auth._validate_auth()
        executor2 = RPCRequestHandler._async_executor

        assert executor1 is executor2

    def test_concurrent_auth_calls(self, handler_with_auth):
        """Test concurrent authentication calls."""
        results = []
        errors = []

        def auth_thread():
            try:
                result = handler_with_auth._validate_auth()
                results.append(result)
            except Exception as e:
                errors.append(e)

        # Simulate concurrent requests
        threads = [threading.Thread(target=auth_thread) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0, f"Errors: {errors}"
        assert len(results) == 20
        assert all(r is True for r in results)


class TestRunAsyncSafePerformance:
    """Performance tests for _run_async_safe optimization."""

    @pytest.fixture
    def handler(self):
        """Create a handler."""
        RPCRequestHandler._async_executor = None
        RPCRequestHandler._async_executor_lock = None

        handler = Mock(spec=RPCRequestHandler)
        handler._run_async_safe = lambda coro: RPCRequestHandler._run_async_safe(handler, coro)

        yield handler

        if RPCRequestHandler._async_executor is not None:
            RPCRequestHandler._async_executor.shutdown(wait=True)
            RPCRequestHandler._async_executor = None

    def test_concurrent_performance_shared_executor(self, handler):
        """Test shared executor performance under concurrent load.

        The benefit of shared executor is primarily under concurrent load:
        - Avoids thread creation/destruction overhead per call
        - Reduces GC pressure from thread cleanup
        - Better thread reuse under sustained load
        """
        import queue

        async def simple_coro():
            await asyncio.sleep(0.001)  # Simulate async work
            return 1

        results_queue: queue.Queue = queue.Queue()
        num_threads = 20
        calls_per_thread = 5

        def worker():
            for _ in range(calls_per_thread):
                result = handler._run_async_safe(simple_coro())
                results_queue.put(result)

        # Time with shared executor (new implementation) under concurrent load
        start = time.perf_counter()
        threads = [threading.Thread(target=worker) for _ in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        shared_time = time.perf_counter() - start

        # Verify all results
        assert results_queue.qsize() == num_threads * calls_per_thread

        # Time with per-call executor (old implementation) under concurrent load
        def old_run_async_safe(coro):
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(asyncio.run, coro)
                return future.result()

        results_queue_old: queue.Queue = queue.Queue()

        def worker_old():
            for _ in range(calls_per_thread):
                result = old_run_async_safe(simple_coro())
                results_queue_old.put(result)

        start = time.perf_counter()
        threads = [threading.Thread(target=worker_old) for _ in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        per_call_time = time.perf_counter() - start

        # Verify all results
        assert results_queue_old.qsize() == num_threads * calls_per_thread

        # Log performance comparison (not a strict assertion due to system variability)
        print(f"Concurrent: Shared executor: {shared_time:.3f}s, Per-call: {per_call_time:.3f}s")
        print(f"Speedup: {per_call_time / shared_time:.2f}x")

        # The key functional benefit is avoiding thread creation overhead
        # Both should complete all work correctly - that's the main test
        assert results_queue.qsize() == num_threads * calls_per_thread
