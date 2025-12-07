"""Tests for AsyncReBACBridge.

These tests verify the async-to-sync bridge functionality for ReBAC operations.
"""

import pytest

from nexus.core.async_bridge import (
    AsyncReBACBridge,
    get_async_rebac_bridge,
    shutdown_async_rebac_bridge,
)


class TestAsyncReBACBridge:
    """Test AsyncReBACBridge functionality."""

    def test_init(self) -> None:
        """Test bridge initialization without starting."""
        bridge = AsyncReBACBridge("sqlite:///test.db")
        assert bridge.database_url == "sqlite:///test.db"
        assert not bridge._started
        assert bridge._loop is None
        assert bridge._thread is None
        assert bridge._manager is None

    def test_init_with_custom_params(self) -> None:
        """Test bridge initialization with custom parameters."""
        bridge = AsyncReBACBridge(
            database_url="postgresql://test:test@localhost/test",
            cache_ttl_seconds=600,
            max_depth=100,
            enable_l1_cache=False,
            l1_cache_size=5000,
            l1_cache_ttl=120,
        )
        assert bridge.cache_ttl_seconds == 600
        assert bridge.max_depth == 100
        assert bridge.enable_l1_cache is False
        assert bridge.l1_cache_size == 5000
        assert bridge.l1_cache_ttl == 120

    def test_stop_without_start(self) -> None:
        """Test that stopping without starting is safe."""
        bridge = AsyncReBACBridge("sqlite:///test.db")
        # Should not raise
        bridge.stop()
        assert not bridge._started

    def test_run_coro_without_start_raises(self) -> None:
        """Test that running coroutine without starting raises."""

        bridge = AsyncReBACBridge("sqlite:///test.db")

        async def dummy_coro() -> None:
            pass

        with pytest.raises(RuntimeError, match="not started"):
            bridge._run_coro(dummy_coro())

    def test_rebac_check_without_start_raises(self) -> None:
        """Test that rebac_check without starting raises."""
        bridge = AsyncReBACBridge("sqlite:///test.db")

        with pytest.raises(RuntimeError, match="not started"):
            bridge.rebac_check(
                subject=("user", "alice"),
                permission="read",
                object=("file", "/test.txt"),
            )

    def test_rebac_check_bulk_without_start_raises(self) -> None:
        """Test that rebac_check_bulk without starting raises."""
        bridge = AsyncReBACBridge("sqlite:///test.db")

        with pytest.raises(RuntimeError, match="not started"):
            bridge.rebac_check_bulk(
                checks=[(("user", "alice"), "read", ("file", "/test.txt"))],
                tenant_id="default",
            )

    def test_write_tuple_without_start_raises(self) -> None:
        """Test that write_tuple without starting raises."""
        bridge = AsyncReBACBridge("sqlite:///test.db")

        with pytest.raises(RuntimeError, match="not started"):
            bridge.write_tuple(
                subject=("user", "alice"),
                relation="owner",
                object=("file", "/test.txt"),
            )

    def test_delete_tuple_without_start_raises(self) -> None:
        """Test that delete_tuple without starting raises."""
        bridge = AsyncReBACBridge("sqlite:///test.db")

        with pytest.raises(RuntimeError, match="not started"):
            bridge.delete_tuple(
                subject=("user", "alice"),
                relation="owner",
                object=("file", "/test.txt"),
            )

    def test_get_cache_stats_without_start(self) -> None:
        """Test that get_cache_stats returns empty dict without manager."""
        bridge = AsyncReBACBridge("sqlite:///test.db")
        stats = bridge.get_cache_stats()
        assert stats == {}


class TestGlobalBridgeFunctions:
    """Test global bridge helper functions."""

    def test_get_bridge_requires_url_on_first_call(self) -> None:
        """Test that get_async_rebac_bridge requires URL on first call."""
        # Ensure no global bridge exists
        shutdown_async_rebac_bridge()

        with pytest.raises(ValueError, match="database_url required"):
            get_async_rebac_bridge()

    def test_shutdown_without_bridge(self) -> None:
        """Test that shutdown without bridge is safe."""
        # Ensure no global bridge exists
        shutdown_async_rebac_bridge()
        # Should not raise
        shutdown_async_rebac_bridge()
