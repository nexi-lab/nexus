"""Unit tests for background tasks (Issue #2170, 9A).

Tests cover:
- agent_eviction_task calls eviction_manager.run_cycle()

Note: heartbeat_flush_task and stale_agent_detection_task were removed
(Issue #1692). AgentRegistry writes heartbeats directly to metastore.
"""

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.server.background_tasks import agent_eviction_task


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
        manager.urgent_event = asyncio.Event()
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
        manager.urgent_event = asyncio.Event()
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
        manager.urgent_event = asyncio.Event()
        manager.run_cycle = exception_then_success

        with caplog.at_level(logging.ERROR), pytest.raises(asyncio.CancelledError):
            await agent_eviction_task(manager, interval_seconds=0)

        # Verify loop continued past the exception (reached call 3)
        assert call_count >= 3
        assert "Eviction cycle failed" in caplog.text
