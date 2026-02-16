"""Tests for AsyncReBACManager (thin asyncio.to_thread wrapper).

These tests verify the async facade correctly delegates to the sync manager.
"""

from unittest.mock import MagicMock, patch

import pytest

from nexus.rebac.async_manager import AsyncReBACManager


class TestAsyncReBACManager:
    """Test AsyncReBACManager functionality."""

    @pytest.fixture
    def mock_sync_manager(self) -> MagicMock:
        """Create mock sync ReBACManager."""
        manager = MagicMock()
        manager.engine = MagicMock()
        manager.enforce_zone_isolation = True
        return manager

    @pytest.fixture
    def async_manager(self, mock_sync_manager: MagicMock) -> AsyncReBACManager:
        """Create AsyncReBACManager wrapping mock sync manager."""
        return AsyncReBACManager(mock_sync_manager)

    def test_init(self, async_manager: AsyncReBACManager, mock_sync_manager: MagicMock) -> None:
        """Test initialization wraps sync manager."""
        assert async_manager._sync is mock_sync_manager

    def test_engine_property(
        self, async_manager: AsyncReBACManager, mock_sync_manager: MagicMock
    ) -> None:
        """Test engine property delegates to sync manager."""
        assert async_manager.engine is mock_sync_manager.engine

    def test_enforce_zone_isolation_property(
        self, async_manager: AsyncReBACManager, mock_sync_manager: MagicMock
    ) -> None:
        """Test enforce_zone_isolation delegates to sync manager."""
        assert async_manager.enforce_zone_isolation is mock_sync_manager.enforce_zone_isolation

    @pytest.mark.asyncio
    async def test_rebac_check_delegates(
        self, async_manager: AsyncReBACManager, mock_sync_manager: MagicMock
    ) -> None:
        """Test rebac_check delegates to sync manager via to_thread."""
        mock_sync_manager.rebac_check.return_value = True
        with patch("nexus.rebac.async_manager.asyncio.to_thread", return_value=True) as mock_thread:
            result = await async_manager.rebac_check(
                ("user", "alice"), "read", ("file", "/test.txt")
            )
            assert result is True
            mock_thread.assert_called_once()

    @pytest.mark.asyncio
    async def test_rebac_delete_delegates(
        self, async_manager: AsyncReBACManager, mock_sync_manager: MagicMock
    ) -> None:
        """Test rebac_delete delegates to sync manager via to_thread."""
        mock_sync_manager.rebac_delete.return_value = True
        with patch("nexus.rebac.async_manager.asyncio.to_thread", return_value=True) as mock_thread:
            result = await async_manager.rebac_delete("tuple-123")
            assert result is True
            mock_thread.assert_called_once()

    @pytest.mark.asyncio
    async def test_close_delegates(
        self, async_manager: AsyncReBACManager, mock_sync_manager: MagicMock
    ) -> None:
        """Test close delegates to sync manager via to_thread."""
        with patch("nexus.rebac.async_manager.asyncio.to_thread", return_value=None) as mock_thread:
            await async_manager.close()
            mock_thread.assert_called_once()
