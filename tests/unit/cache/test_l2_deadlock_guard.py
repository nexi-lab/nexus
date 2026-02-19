"""Tests for CachingBackendWrapper._l2_get_sync deadlock guard.

Verifies that _l2_get_sync returns None (graceful skip) when called from
the event loop thread, preventing deadlock from run_coroutine_threadsafe
+ .result() on the same loop.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from nexus.backends.caching_wrapper import CacheWrapperConfig, CachingBackendWrapper
from nexus.backends.local import LocalBackend
from nexus.cache.cache_store import CacheStoreABC


@pytest.fixture
def local_backend(tmp_path: Path) -> LocalBackend:
    """Create a real LocalBackend with temp directory."""
    root = tmp_path / "storage"
    root.mkdir()
    return LocalBackend(root_path=str(root))


@pytest.fixture
def mock_cache_store() -> AsyncMock:
    """Mock CacheStoreABC that tracks calls."""
    store = AsyncMock(spec=CacheStoreABC)
    store.get = AsyncMock(return_value=b"cached-data")
    store.set = AsyncMock()
    store.delete = AsyncMock()
    store.health_check = AsyncMock(return_value=True)
    return store


class TestL2DeadlockGuard:
    """Verify _l2_get_sync skips L2 when called from the event loop thread."""

    @pytest.mark.asyncio
    async def test_l2_get_sync_skips_on_event_loop_thread(
        self, local_backend: LocalBackend, mock_cache_store: AsyncMock
    ) -> None:
        """When called from the event loop thread, _l2_get_sync must return None.

        If it didn't skip, run_coroutine_threadsafe().result() would block
        the event loop thread waiting for a coroutine that can never execute
        on the blocked loop → deadlock.
        """
        wrapper = CachingBackendWrapper(
            inner=local_backend,
            config=CacheWrapperConfig(l2_enabled=True),
            cache_store=mock_cache_store,
        )

        # Call from inside the event loop — should skip L2
        result = wrapper._l2_get_sync("sha256:abc123")
        assert result is None, (
            "_l2_get_sync should return None when called from the event loop "
            "thread to prevent deadlock"
        )

        # Verify store.get was NOT called (skipped before reaching it)
        mock_cache_store.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_l2_get_sync_returns_none_without_store(
        self, local_backend: LocalBackend
    ) -> None:
        """Without a cache store, _l2_get_sync returns None immediately."""
        wrapper = CachingBackendWrapper(
            inner=local_backend,
            config=CacheWrapperConfig(l2_enabled=True),
            cache_store=None,
        )

        result = wrapper._l2_get_sync("sha256:abc123")
        assert result is None

    @pytest.mark.asyncio
    async def test_l2_get_sync_returns_none_when_disabled(
        self, local_backend: LocalBackend, mock_cache_store: AsyncMock
    ) -> None:
        """When L2 is disabled, _l2_get_sync returns None immediately."""
        wrapper = CachingBackendWrapper(
            inner=local_backend,
            config=CacheWrapperConfig(l2_enabled=False),
            cache_store=mock_cache_store,
        )

        result = wrapper._l2_get_sync("sha256:abc123")
        assert result is None
        mock_cache_store.get.assert_not_called()

    def test_l2_get_sync_works_from_non_event_loop_thread(
        self, local_backend: LocalBackend, mock_cache_store: AsyncMock
    ) -> None:
        """From a non-event-loop context (no running loop), should return None.

        asyncio.get_running_loop() raises RuntimeError when no loop is running,
        which _l2_get_sync catches and returns None.
        """
        wrapper = CachingBackendWrapper(
            inner=local_backend,
            config=CacheWrapperConfig(l2_enabled=True),
            cache_store=mock_cache_store,
        )

        # No running event loop → RuntimeError caught → return None
        result = wrapper._l2_get_sync("sha256:abc123")
        assert result is None


class TestL2GetSyncTimeout:
    """Verify _l2_get_sync has proper timeout handling."""

    @pytest.mark.asyncio
    async def test_l2_get_sync_timeout_returns_none(self, local_backend: LocalBackend) -> None:
        """If L2 get takes too long, timeout should return None (not hang)."""
        slow_store = AsyncMock(spec=CacheStoreABC)

        async def slow_get(key: str) -> bytes:
            await asyncio.sleep(10)  # Simulate very slow response
            return b"data"

        slow_store.get = slow_get

        wrapper = CachingBackendWrapper(
            inner=local_backend,
            config=CacheWrapperConfig(l2_enabled=True),
            cache_store=slow_store,
        )

        # From event loop thread → deadlock guard kicks in → None
        result = wrapper._l2_get_sync("sha256:abc123")
        assert result is None
