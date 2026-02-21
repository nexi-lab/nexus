"""Unit tests for AuthCacheManager — edge offline grace period handling.

Issue #1707: Edge split-brain resilience.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from nexus.proxy.auth_cache_manager import AuthCacheManager


def _make_auth_cache() -> MagicMock:
    """Create a mock AuthCache."""
    cache = MagicMock()
    cache.get.return_value = {"user": "test", "role": "admin"}
    cache.invalidate.return_value = None
    cache.clear.return_value = None
    return cache


class TestAuthCacheManagerOfflineMode:
    """Enter/exit offline mode lifecycle."""

    def test_starts_online(self) -> None:
        mgr = AuthCacheManager(_make_auth_cache())
        assert not mgr.is_offline
        assert not mgr.needs_refresh

    def test_enter_offline(self) -> None:
        mgr = AuthCacheManager(_make_auth_cache())
        mgr.enter_offline_mode()
        assert mgr.is_offline

    def test_exit_offline_sets_needs_refresh(self) -> None:
        mgr = AuthCacheManager(_make_auth_cache())
        mgr.enter_offline_mode()
        mgr.exit_offline_mode()
        assert not mgr.is_offline
        assert mgr.needs_refresh

    def test_double_enter_is_noop(self) -> None:
        mgr = AuthCacheManager(_make_auth_cache())
        mgr.enter_offline_mode()
        # Second enter should not reset the offline_since timestamp
        mgr.enter_offline_mode()
        assert mgr.is_offline


class TestAuthCacheManagerGracePeriod:
    """Grace period validation during offline mode."""

    def test_grace_period_valid_when_online(self) -> None:
        mgr = AuthCacheManager(_make_auth_cache(), grace_period_seconds=3600)
        assert mgr.is_grace_period_valid()

    def test_grace_period_valid_within_window(self) -> None:
        mgr = AuthCacheManager(_make_auth_cache(), grace_period_seconds=3600)
        mgr.enter_offline_mode()
        assert mgr.is_grace_period_valid()

    def test_grace_period_expired(self) -> None:
        mgr = AuthCacheManager(_make_auth_cache(), grace_period_seconds=1.0)
        mgr.enter_offline_mode()
        # Simulate time passing beyond grace period
        with patch("nexus.proxy.auth_cache_manager.time") as mock_time:
            mock_time.monotonic.return_value = time.monotonic() + 2.0
            # Need to also set the offline_since to a consistent time
            mgr._offline_since = mock_time.monotonic() - 2.0  # noqa: SLF001
            assert not mgr.is_grace_period_valid()


class TestAuthCacheManagerGetCachedAuth:
    """get_cached_auth() respects offline grace period."""

    def test_returns_cached_when_online(self) -> None:
        cache = _make_auth_cache()
        mgr = AuthCacheManager(cache)
        result = mgr.get_cached_auth("token123")
        assert result == {"user": "test", "role": "admin"}
        cache.get.assert_called_once_with("token123")

    def test_returns_cached_during_grace_period(self) -> None:
        cache = _make_auth_cache()
        mgr = AuthCacheManager(cache, grace_period_seconds=3600)
        mgr.enter_offline_mode()
        result = mgr.get_cached_auth("token123")
        assert result is not None

    def test_returns_none_after_grace_expires(self) -> None:
        cache = _make_auth_cache()
        mgr = AuthCacheManager(cache, grace_period_seconds=0.001)
        mgr.enter_offline_mode()
        # Wait a tiny bit for grace period to expire
        import time as t

        t.sleep(0.01)
        result = mgr.get_cached_auth("token123")
        assert result is None


class TestAuthCacheManagerForceRefresh:
    """force_refresh() invalidates cache and clears needs_refresh flag."""

    @pytest.mark.asyncio
    async def test_force_refresh_invalidates(self) -> None:
        cache = _make_auth_cache()
        mgr = AuthCacheManager(cache)
        mgr.enter_offline_mode()
        mgr.exit_offline_mode()
        assert mgr.needs_refresh

        await mgr.force_refresh("token123")
        assert not mgr.needs_refresh
        cache.invalidate.assert_called_once_with("token123")


class TestAuthCacheManagerClear:
    """clear() resets all state."""

    def test_clear_resets_everything(self) -> None:
        cache = _make_auth_cache()
        mgr = AuthCacheManager(cache)
        mgr.enter_offline_mode()
        mgr.clear()
        assert not mgr.is_offline
        assert not mgr.needs_refresh
        cache.clear.assert_called_once()
