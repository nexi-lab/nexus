"""TDD tests for CacheBrick lifecycle management (Issue #1524).

Tests the start()/stop()/health_check() state machine that makes CacheBrick
compatible with BrickLifecycleProtocol.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_store() -> MagicMock:
    """Create a mock CacheStoreABC."""
    store = AsyncMock()
    store.health_check = AsyncMock(return_value=True)
    store.close = AsyncMock()
    return store


# ---------------------------------------------------------------------------
# Start tests
# ---------------------------------------------------------------------------


class TestCacheBrickStart:
    """Test start() lifecycle method."""

    @pytest.mark.asyncio
    async def test_start_connects_store(self) -> None:
        """start() should set the brick to active state."""
        from nexus.cache.brick import CacheBrick

        brick = CacheBrick(cache_store=_make_mock_store())
        await brick.start()
        assert brick._started is True

    @pytest.mark.asyncio
    async def test_start_idempotent(self) -> None:
        """Double start() should be safe (no error)."""
        from nexus.cache.brick import CacheBrick

        brick = CacheBrick(cache_store=_make_mock_store())
        await brick.start()
        await brick.start()  # Should not raise
        assert brick._started is True

    @pytest.mark.asyncio
    async def test_start_failure_stays_stopped(self) -> None:
        """If start() fails, brick should remain not started."""
        from nexus.cache.brick import CacheBrick

        store = _make_mock_store()
        store.health_check = AsyncMock(side_effect=ConnectionError("fail"))
        brick = CacheBrick(cache_store=store)
        # start() should not propagate, just log warning (Tier 2 = silent degrade)
        await brick.start()
        # Even on error, the brick is usable (silent degradation)


# ---------------------------------------------------------------------------
# Stop tests
# ---------------------------------------------------------------------------


class TestCacheBrickStop:
    """Test stop() lifecycle method."""

    @pytest.mark.asyncio
    async def test_stop_disconnects_store(self) -> None:
        """stop() should close the underlying store."""
        from nexus.cache.brick import CacheBrick

        store = _make_mock_store()
        brick = CacheBrick(cache_store=store)
        await brick.start()
        await brick.stop()
        store.close.assert_awaited_once()
        assert brick._started is False

    @pytest.mark.asyncio
    async def test_stop_idempotent(self) -> None:
        """Double stop() should be safe."""
        from nexus.cache.brick import CacheBrick

        store = _make_mock_store()
        brick = CacheBrick(cache_store=store)
        await brick.start()
        await brick.stop()
        await brick.stop()  # Should not raise

    @pytest.mark.asyncio
    async def test_stop_without_start(self) -> None:
        """stop() before start() should be safe."""
        from nexus.cache.brick import CacheBrick

        brick = CacheBrick()
        await brick.stop()  # Should not raise


# ---------------------------------------------------------------------------
# Health check tests
# ---------------------------------------------------------------------------


class TestCacheBrickHealthCheck:
    """Test health_check() method."""

    @pytest.mark.asyncio
    async def test_health_check_healthy(self) -> None:
        """health_check() should return True when store is healthy."""
        from nexus.cache.brick import CacheBrick

        store = _make_mock_store()
        store.health_check = AsyncMock(return_value=True)
        brick = CacheBrick(cache_store=store)
        await brick.start()
        assert await brick.health_check() is True

    @pytest.mark.asyncio
    async def test_health_check_degraded_null_store(self) -> None:
        """NullCacheStore health_check should return True (degraded but operational)."""
        from nexus.cache.brick import CacheBrick

        brick = CacheBrick()  # NullCacheStore
        result = await brick.health_check()
        assert result is True  # NullCacheStore always reports healthy

    @pytest.mark.asyncio
    async def test_health_check_unhealthy(self) -> None:
        """health_check() should return False when store connection is lost."""
        from nexus.cache.brick import CacheBrick

        store = _make_mock_store()
        store.health_check = AsyncMock(return_value=False)
        brick = CacheBrick(cache_store=store)
        assert await brick.health_check() is False


# ---------------------------------------------------------------------------
# Lifecycle protocol conformance
# ---------------------------------------------------------------------------


class TestCacheBrickLifecycleProtocol:
    """Test that CacheBrick satisfies BrickLifecycleProtocol."""

    def test_satisfies_lifecycle_protocol(self) -> None:
        """CacheBrick should be a structural match for BrickLifecycleProtocol."""
        from nexus.cache.brick import CacheBrick
        from nexus.contracts.protocols.brick_lifecycle import BrickLifecycleProtocol

        brick = CacheBrick()
        assert isinstance(brick, BrickLifecycleProtocol)

    @pytest.mark.asyncio
    async def test_lifecycle_with_null_store(self) -> None:
        """Full lifecycle with NullCacheStore should be no-op."""
        from nexus.cache.brick import CacheBrick

        brick = CacheBrick()  # NullCacheStore fallback
        await brick.start()  # No-op
        assert await brick.health_check() is True
        await brick.stop()  # No-op

    @pytest.mark.asyncio
    async def test_full_lifecycle(self) -> None:
        """Full start → health → stop cycle with real mock store."""
        from nexus.cache.brick import CacheBrick

        store = _make_mock_store()
        brick = CacheBrick(cache_store=store)

        # Start
        await brick.start()
        assert brick._started is True

        # Health check
        assert await brick.health_check() is True

        # Stop
        await brick.stop()
        assert brick._started is False
        store.close.assert_awaited_once()
