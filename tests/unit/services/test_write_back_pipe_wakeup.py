"""Tests for DT_PIPE wakeup integration in WriteBackService (Issue #3194, #9A).

Tests the pipe-driven poll loop wakeup and on_enqueue callback wiring.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nexus.system_services.sync.write_back_service import (
    _BACKLOG_PIPE_CAPACITY,
    _BACKLOG_WAKEUP_PIPE,
    WriteBackService,
)


def _make_service(
    *,
    pipe_manager: MagicMock | None = None,
    mock_gateway: MagicMock | None = None,
    mock_event_bus: AsyncMock | None = None,
) -> WriteBackService:
    """Create a WriteBackService with mocked dependencies."""
    gw = mock_gateway or MagicMock()
    bus = mock_event_bus or AsyncMock()
    bus.subscribe = MagicMock(return_value=_empty_async_iter())
    bus.publish = AsyncMock(return_value=1)

    backlog = MagicMock()
    backlog.fetch_distinct_backend_zones.return_value = []
    backlog.enqueue.return_value = True

    change_log = MagicMock()

    return WriteBackService(
        gateway=gw,
        event_bus=bus,
        backlog_store=backlog,
        change_log_store=change_log,
        pipe_manager=pipe_manager,
        poll_interval_seconds=0.1,  # Short for tests
    )


async def _empty_async_iter():
    return
    yield  # pragma: no cover


class TestPipeWakeupStart:
    """Test pipe creation during start()."""

    @pytest.mark.asyncio
    async def test_start_creates_pipe(self):
        """start() calls pipe_manager.ensure() with correct path and capacity."""
        pm = MagicMock()
        service = _make_service(pipe_manager=pm)

        await service.start()
        try:
            pm.ensure.assert_called_once_with(_BACKLOG_WAKEUP_PIPE, capacity=_BACKLOG_PIPE_CAPACITY)
        finally:
            await service.stop()

    @pytest.mark.asyncio
    async def test_start_without_pipe_manager(self):
        """start() works without pipe_manager (polling-only fallback)."""
        service = _make_service(pipe_manager=None)

        await service.start()
        try:
            assert service._running is True
            assert service._pipe_manager is None
        finally:
            await service.stop()

    @pytest.mark.asyncio
    async def test_start_pipe_failure_falls_back(self):
        """If pipe creation fails, pipe_manager is disabled (falls back to timer)."""
        pm = MagicMock()
        pm.ensure.side_effect = RuntimeError("pipe creation failed")
        service = _make_service(pipe_manager=pm)

        await service.start()
        try:
            assert service._running is True
            assert service._pipe_manager is None  # Disabled on failure
        finally:
            await service.stop()


class TestPipeWakeupStop:
    """Test pipe cleanup during stop()."""

    @pytest.mark.asyncio
    async def test_stop_closes_pipe(self):
        """stop() calls signal_close() on the wakeup pipe."""
        pm = MagicMock()
        service = _make_service(pipe_manager=pm)

        await service.start()
        await service.stop()

        pm.signal_close.assert_called_once_with(_BACKLOG_WAKEUP_PIPE)

    @pytest.mark.asyncio
    async def test_stop_without_pipe_manager(self):
        """stop() works without pipe_manager (no error)."""
        service = _make_service(pipe_manager=None)

        await service.start()
        await service.stop()
        assert service._running is False


class TestPollLoopWakeup:
    """Test poll loop wakeup behavior."""

    @pytest.mark.asyncio
    async def test_poll_loop_uses_timer_without_pipe(self):
        """Without pipe, poll loop sleeps for poll_interval."""
        service = _make_service(pipe_manager=None)
        service._poll_interval = 0.05

        await service.start()
        # Let the poll loop run a couple iterations
        await asyncio.sleep(0.15)
        await service.stop()

        # Should have called _process_all_backends at least once
        service._backlog_store.fetch_distinct_backend_zones.assert_called()

    @pytest.mark.asyncio
    async def test_poll_loop_uses_pipe_when_available(self):
        """With pipe, poll loop calls wait_for_signal."""
        pm = MagicMock()
        service = _make_service(pipe_manager=pm)

        # The mock must actually suspend (yield to the event loop) to avoid a
        # CPU-burning tight loop that starves cancellation and exhausts memory.
        async def _fake_wait(*args, **kwargs):
            await asyncio.sleep(0.01)
            return True

        with patch(
            "nexus.lib.pipe_wakeup.wait_for_signal",
            side_effect=_fake_wait,
        ) as mock_wait:
            await service.start()
            await asyncio.sleep(0.05)
            await service.stop()

            mock_wait.assert_called()
