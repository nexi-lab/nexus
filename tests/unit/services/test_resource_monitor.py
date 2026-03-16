"""Unit tests for ResourceMonitor (Issue #2170).

Tests cover:
- Normal pressure at low memory usage
- Warning pressure at medium memory usage
- Critical pressure at high memory usage
- Graceful fallback when psutil is unavailable
- Async executor usage for non-blocking operation
"""

from unittest.mock import MagicMock, patch

import pytest

from nexus.lib.performance_tuning import EvictionTuning
from nexus.system_services.agents.resource_monitor import PressureLevel, ResourceMonitor


@pytest.fixture
def tuning():
    """Create EvictionTuning for testing."""
    return EvictionTuning(
        memory_high_watermark_pct=85,
        memory_low_watermark_pct=75,
        max_active_agents=100,
        eviction_batch_size=10,
        checkpoint_timeout_seconds=5.0,
        eviction_cooldown_seconds=60,
    )


@pytest.fixture
def monitor(tuning):
    """Create a ResourceMonitor for testing."""
    return ResourceMonitor(tuning=tuning)


class TestResourceMonitor:
    """Tests for ResourceMonitor."""

    @pytest.mark.asyncio
    async def test_normal_pressure(self, monitor):
        """Memory at 50% returns NORMAL."""
        mem = MagicMock()
        mem.percent = 50.0

        with (
            patch("nexus.system_services.agents.resource_monitor.psutil") as mock_psutil,
            patch("nexus.system_services.agents.resource_monitor._HAS_PSUTIL", True),
        ):
            mock_psutil.virtual_memory.return_value = mem
            result = await monitor.check_pressure()

        assert result is PressureLevel.NORMAL

    @pytest.mark.asyncio
    async def test_warning_pressure(self, monitor):
        """Memory at 78% returns WARNING (between low and high watermarks)."""
        mem = MagicMock()
        mem.percent = 78.0

        with (
            patch("nexus.system_services.agents.resource_monitor.psutil") as mock_psutil,
            patch("nexus.system_services.agents.resource_monitor._HAS_PSUTIL", True),
        ):
            mock_psutil.virtual_memory.return_value = mem
            result = await monitor.check_pressure()

        assert result is PressureLevel.WARNING

    @pytest.mark.asyncio
    async def test_critical_pressure(self, monitor):
        """Memory at 90% returns CRITICAL (above high watermark)."""
        mem = MagicMock()
        mem.percent = 90.0

        with (
            patch("nexus.system_services.agents.resource_monitor.psutil") as mock_psutil,
            patch("nexus.system_services.agents.resource_monitor._HAS_PSUTIL", True),
        ):
            mock_psutil.virtual_memory.return_value = mem
            result = await monitor.check_pressure()

        assert result is PressureLevel.CRITICAL

    @pytest.mark.asyncio
    async def test_psutil_missing_fallback(self, monitor):
        """When psutil is unavailable, always returns NORMAL."""
        with patch("nexus.system_services.agents.resource_monitor._HAS_PSUTIL", False):
            result = await monitor.check_pressure()

        assert result is PressureLevel.NORMAL

    @pytest.mark.asyncio
    async def test_get_memory_percent_returns_value(self, monitor):
        """get_memory_percent returns the raw percentage from psutil."""
        mem = MagicMock()
        mem.percent = 67.5

        with (
            patch("nexus.system_services.agents.resource_monitor.psutil") as mock_psutil,
            patch("nexus.system_services.agents.resource_monitor._HAS_PSUTIL", True),
        ):
            mock_psutil.virtual_memory.return_value = mem
            result = await monitor.get_memory_percent()

        assert result == 67.5

    @pytest.mark.asyncio
    async def test_get_memory_percent_no_psutil(self, monitor):
        """get_memory_percent returns -1.0 when psutil is unavailable."""
        with patch("nexus.system_services.agents.resource_monitor._HAS_PSUTIL", False):
            result = await monitor.get_memory_percent()

        assert result == -1.0

    @pytest.mark.asyncio
    async def test_exact_high_watermark_is_critical(self, monitor):
        """Memory exactly at high_watermark (85%) is CRITICAL."""
        mem = MagicMock()
        mem.percent = 85.0

        with (
            patch("nexus.system_services.agents.resource_monitor.psutil") as mock_psutil,
            patch("nexus.system_services.agents.resource_monitor._HAS_PSUTIL", True),
        ):
            mock_psutil.virtual_memory.return_value = mem
            result = await monitor.check_pressure()

        assert result is PressureLevel.CRITICAL

    @pytest.mark.asyncio
    async def test_exact_low_watermark_is_warning(self, monitor):
        """Memory exactly at low_watermark (75%) is WARNING."""
        mem = MagicMock()
        mem.percent = 75.0

        with (
            patch("nexus.system_services.agents.resource_monitor.psutil") as mock_psutil,
            patch("nexus.system_services.agents.resource_monitor._HAS_PSUTIL", True),
        ):
            mock_psutil.virtual_memory.return_value = mem
            result = await monitor.check_pressure()

        assert result is PressureLevel.WARNING

    @pytest.mark.asyncio
    async def test_psutil_exception_returns_normal(self, monitor):
        """When psutil.virtual_memory() raises, falls back to NORMAL."""
        with (
            patch("nexus.system_services.agents.resource_monitor.psutil") as mock_psutil,
            patch("nexus.system_services.agents.resource_monitor._HAS_PSUTIL", True),
        ):
            mock_psutil.virtual_memory.side_effect = OSError("cgroups v2 not supported")
            result = await monitor.check_pressure()

        assert result is PressureLevel.NORMAL

    @pytest.mark.asyncio
    async def test_psutil_exception_get_memory_returns_negative(self, monitor):
        """When psutil raises, get_memory_percent returns -1.0."""
        with (
            patch("nexus.system_services.agents.resource_monitor.psutil") as mock_psutil,
            patch("nexus.system_services.agents.resource_monitor._HAS_PSUTIL", True),
        ):
            mock_psutil.virtual_memory.side_effect = PermissionError("no access")
            result = await monitor.get_memory_percent()

        assert result == -1.0
