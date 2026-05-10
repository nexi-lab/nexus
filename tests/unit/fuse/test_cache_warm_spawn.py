"""Unit tests for NexusFUSEOperations._spawn_cache_warm — production hydration trigger (Issue #4055)."""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

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
        ephemeral = MagicMock()
        ephemeral.cache_warm.return_value = {"admitted_count": 0}
        ephemeral.__enter__ = MagicMock(return_value=ephemeral)
        ephemeral.__exit__ = MagicMock(return_value=False)

        with patch("nexus.fuse.rust_client.RustFUSEClient", return_value=ephemeral) as ctor:
            ops._spawn_cache_warm("http://nx.test", "key", agent_id=None)
            assert _wait_for(lambda: ephemeral.cache_warm.call_count >= 1), (
                "cache_warm was never called"
            )

        ctor.assert_called_once_with(nexus_url="http://nx.test", api_key="key", agent_id=None)
        ephemeral.cache_warm.assert_called_with("/")
        ephemeral.__exit__.assert_called()  # ephemeral client closed

    def test_returns_immediately_without_blocking(self) -> None:
        ops = NexusFUSEOperations.__new__(NexusFUSEOperations)
        ephemeral = MagicMock()
        block = threading.Event()
        ephemeral.cache_warm.side_effect = lambda *_a, **_kw: block.wait(timeout=5)
        ephemeral.__enter__ = MagicMock(return_value=ephemeral)
        ephemeral.__exit__ = MagicMock(return_value=False)

        with patch("nexus.fuse.rust_client.RustFUSEClient", return_value=ephemeral):
            start = time.monotonic()
            ops._spawn_cache_warm("http://nx.test", "key")
            elapsed = time.monotonic() - start

        assert elapsed < 0.5, f"_spawn_cache_warm took {elapsed:.3f}s — appears synchronous"
        block.set()

    def test_swallows_exceptions_silently(self) -> None:
        ops = NexusFUSEOperations.__new__(NexusFUSEOperations)
        ephemeral = MagicMock()
        ephemeral.cache_warm.side_effect = RuntimeError("daemon dead")
        ephemeral.__enter__ = MagicMock(return_value=ephemeral)
        ephemeral.__exit__ = MagicMock(return_value=False)

        with patch("nexus.fuse.rust_client.RustFUSEClient", return_value=ephemeral):
            ops._spawn_cache_warm("http://nx.test", "key")
            assert _wait_for(lambda: ephemeral.cache_warm.call_count >= 1), (
                "cache_warm was never called"
            )
        # No crash here — that's the point.
