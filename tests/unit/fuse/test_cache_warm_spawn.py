"""Unit tests for NexusFUSEOperations._spawn_cache_warm — production hydration trigger (Issue #4055)."""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

from nexus.fuse.operations import NexusFUSEOperations


def _wait_for(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class TestSpawnCacheWarm:
    def test_spawns_daemon_thread_that_calls_cache_warm(self) -> None:
        ops = NexusFUSEOperations.__new__(NexusFUSEOperations)
        rust_client = MagicMock()
        rust_client.cache_warm.return_value = {"admitted_count": 0}

        ops._spawn_cache_warm(rust_client)

        assert _wait_for(lambda: rust_client.cache_warm.call_count >= 1), (
            "cache_warm was never called"
        )
        rust_client.cache_warm.assert_called_with("/")

    def test_returns_immediately_without_blocking(self) -> None:
        ops = NexusFUSEOperations.__new__(NexusFUSEOperations)
        rust_client = MagicMock()
        block = threading.Event()
        rust_client.cache_warm.side_effect = lambda *_a, **_kw: block.wait(timeout=5)

        start = time.monotonic()
        ops._spawn_cache_warm(rust_client)
        elapsed = time.monotonic() - start

        assert elapsed < 0.5, f"_spawn_cache_warm took {elapsed:.3f}s — appears synchronous"
        block.set()

    def test_swallows_exceptions_silently(self) -> None:
        ops = NexusFUSEOperations.__new__(NexusFUSEOperations)
        rust_client = MagicMock()
        rust_client.cache_warm.side_effect = RuntimeError("daemon dead")

        ops._spawn_cache_warm(rust_client)

        # If the exception escaped the thread, the test runner would see it.
        assert _wait_for(lambda: rust_client.cache_warm.call_count >= 1), (
            "cache_warm was never called"
        )
        # Still no crash here — that's the point.
