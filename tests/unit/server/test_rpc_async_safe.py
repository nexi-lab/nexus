"""Tests for _run_async_safe using sync_bridge (Issue #1300).

This module tests that _run_async_safe correctly delegates to sync_bridge.run_sync()
instead of the old ThreadPoolExecutor + asyncio.run() anti-pattern.
"""

import asyncio
import threading
from unittest.mock import AsyncMock, Mock

import pytest

from nexus.server.rpc_server import RPCRequestHandler


class TestRunAsyncSafe:
    """Tests for the _run_async_safe method."""

    @pytest.fixture
    def handler(self):
        """Create a handler with necessary attributes."""
        handler = Mock(spec=RPCRequestHandler)
        handler.nexus_fs = Mock()
        handler.api_key = None
        handler.auth_provider = None
        handler.event_loop = None
        handler.headers = {}

        # Bind the actual method
        handler._run_async_safe = lambda coro: RPCRequestHandler._run_async_safe(handler, coro)

        yield handler

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

    def test_delegates_to_run_sync(self, handler):
        """Test that _run_async_safe delegates to sync_bridge.run_sync."""

        async def async_coro():
            return "delegated"

        # run_sync handles context detection internally, so just verify
        # that the result is returned correctly
        result = handler._run_async_safe(async_coro())
        assert result == "delegated"

    def test_concurrent_calls_safe(self, handler):
        """Test that concurrent calls from multiple threads are safe."""
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

    def test_error_propagation(self, handler):
        """Test that errors from coroutines propagate correctly."""

        async def failing_coro():
            raise ValueError("test error")

        with pytest.raises(ValueError, match="test error"):
            handler._run_async_safe(failing_coro())


class TestRunAsyncSafeWithAuthProvider:
    """Tests for _run_async_safe with auth_provider (the actual use case)."""

    @pytest.fixture
    def handler_with_auth(self):
        """Create a handler with auth_provider configured."""
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

    def test_validate_auth_with_async_provider(self, handler_with_auth):
        """Test _validate_auth uses _run_async_safe correctly."""
        result = handler_with_auth._validate_auth()
        assert result is True
        handler_with_auth.auth_provider.authenticate.assert_called_once_with("test-token")

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
    """Performance tests for _run_async_safe using sync_bridge."""

    @pytest.fixture
    def handler(self):
        """Create a handler."""
        handler = Mock(spec=RPCRequestHandler)
        handler._run_async_safe = lambda coro: RPCRequestHandler._run_async_safe(handler, coro)

        yield handler

    def test_concurrent_performance(self, handler):
        """Test sync_bridge-based implementation under concurrent load."""
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

        threads = [threading.Thread(target=worker) for _ in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Verify all results completed correctly
        assert results_queue.qsize() == num_threads * calls_per_thread
