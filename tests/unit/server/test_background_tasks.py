"""Unit tests for background tasks (Issue #2170, 9A).

Tests cover:
- heartbeat_flush_task calls registry.flush_heartbeats()
- stale_agent_detection_task passes threshold_seconds through
- stale_agent_detection_task logs warning for stale agents
- agent_eviction_task calls eviction_manager.run_cycle()
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.server.background_tasks import (
    agent_eviction_task,
    heartbeat_flush_task,
    stale_agent_detection_task,
)


@pytest.fixture
def mock_registry():
    """Create a mock AgentRegistry."""
    registry = MagicMock()
    registry.flush_heartbeats.return_value = 3
    registry.detect_stale.return_value = []
    return registry


class TestHeartbeatFlushTask:
    """Tests for heartbeat_flush_task."""

    @pytest.mark.asyncio
    async def test_heartbeat_flush_calls_registry(self, mock_registry):
        """heartbeat_flush_task calls flush_heartbeats() after sleeping."""
        call_count = 0

        original_flush = mock_registry.flush_heartbeats

        def counting_flush():
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                raise asyncio.CancelledError
            return original_flush()

        mock_registry.flush_heartbeats = counting_flush

        with pytest.raises(asyncio.CancelledError):
            await heartbeat_flush_task(mock_registry, interval_seconds=0)

        assert call_count >= 1


class TestStaleAgentDetectionTask:
    """Tests for stale_agent_detection_task."""

    @pytest.mark.asyncio
    async def test_stale_detection_calls_detect_stale(self, mock_registry):
        """stale_agent_detection_task calls detect_stale with threshold."""
        call_count = 0

        original_detect = mock_registry.detect_stale

        def counting_detect(**kwargs):
            nonlocal call_count
            call_count += 1
            assert kwargs.get("threshold_seconds") == 42
            if call_count >= 1:
                raise asyncio.CancelledError
            return original_detect(**kwargs)

        mock_registry.detect_stale = counting_detect

        with pytest.raises(asyncio.CancelledError):
            await stale_agent_detection_task(
                mock_registry, interval_seconds=0, threshold_seconds=42
            )

        assert call_count >= 1

    @pytest.mark.asyncio
    async def test_stale_detection_logs_warning(self, mock_registry, caplog):
        """stale_agent_detection_task logs warning for stale agents."""
        stale_agent = MagicMock()
        stale_agent.agent_id = "stale-agent-1"

        call_count = 0

        def detect_with_stale(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                raise asyncio.CancelledError
            return [stale_agent]

        mock_registry.detect_stale = detect_with_stale

        with caplog.at_level(logging.WARNING), pytest.raises(asyncio.CancelledError):
            await stale_agent_detection_task(
                mock_registry, interval_seconds=0, threshold_seconds=300
            )

        assert "stale agents detected" in caplog.text


class TestAgentEvictionTask:
    """Tests for agent_eviction_task."""

    @pytest.mark.asyncio
    async def test_eviction_task_calls_run_cycle(self):
        """agent_eviction_task calls eviction_manager.run_cycle()."""
        result = MagicMock()
        result.evicted = 0
        result.reason = "normal_pressure"
        result.post_pressure = "normal"

        manager = AsyncMock()
        call_count = 0

        async def counting_cycle():
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                raise asyncio.CancelledError
            return result

        manager.run_cycle = counting_cycle

        with pytest.raises(asyncio.CancelledError):
            await agent_eviction_task(manager, interval_seconds=0)

        assert call_count >= 1

    @pytest.mark.asyncio
    async def test_eviction_task_logs_on_eviction(self, caplog):
        """agent_eviction_task logs when agents are evicted."""
        result = MagicMock()
        result.evicted = 5
        result.reason = "pressure_critical"
        result.post_pressure = "warning"
        result.skipped = 0

        call_count = 0

        async def counting_cycle():
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                raise asyncio.CancelledError
            return result

        manager = MagicMock()
        manager.run_cycle = counting_cycle

        with caplog.at_level(logging.INFO), pytest.raises(asyncio.CancelledError):
            await agent_eviction_task(manager, interval_seconds=0)

        assert "Evicted 5 agents" in caplog.text

    @pytest.mark.asyncio
    async def test_eviction_task_continues_after_exception(self, caplog):
        """agent_eviction_task continues running after run_cycle() raises."""
        call_count = 0

        async def exception_then_success():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("DB connection lost")
            if call_count >= 3:
                raise asyncio.CancelledError
            result = MagicMock()
            result.evicted = 0
            result.reason = "normal_pressure"
            result.post_pressure = "normal"
            result.skipped = 0
            return result

        manager = MagicMock()
        manager.run_cycle = exception_then_success

        with caplog.at_level(logging.ERROR), pytest.raises(asyncio.CancelledError):
            await agent_eviction_task(manager, interval_seconds=0)

        # Verify loop continued past the exception (reached call 3)
        assert call_count >= 3
        assert "Eviction cycle failed" in caplog.text
