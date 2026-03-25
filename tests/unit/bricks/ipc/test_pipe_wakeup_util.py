"""Tests for the shared wait_for_signal() DT_PIPE utility (Issue #3194, #11A).

Tests the generic drain-and-process utility extracted from PipeWakeupListener.
Uses a mock PipeManager to test in isolation.
"""

import asyncio

import pytest

from nexus.bricks.ipc.wakeup import wait_for_signal


class _FakePipeManager:
    """Minimal PipeManager mock with controllable signal delivery."""

    def __init__(self) -> None:
        self._signals: list[bytes] = []
        self._read_event = asyncio.Event()
        self._closed = False

    def enqueue_signal(self, data: bytes = b"\x01") -> None:
        """Add a signal to the buffer."""
        self._signals.append(data)
        self._read_event.set()

    async def pipe_read(self, path: str, blocking: bool = True) -> bytes:
        if self._closed:
            raise RuntimeError("PipeClosedError")
        if self._signals:
            self._read_event.clear() if len(self._signals) <= 1 else None
            return self._signals.pop(0)
        if not blocking:
            raise RuntimeError("PipeEmptyError")
        # Block until signal arrives or pipe is closed
        while not self._signals and not self._closed:
            self._read_event.clear()
            await self._read_event.wait()
        if self._closed:
            raise RuntimeError("PipeClosedError")
        return self._signals.pop(0)

    def signal_close(self, path: str) -> None:
        self._closed = True
        self._read_event.set()


class TestWaitForSignal:
    """Tests for wait_for_signal() utility."""

    @pytest.mark.asyncio
    async def test_signal_arrives_returns_immediately(self):
        """Signal already in buffer -> returns True without waiting."""
        pm = _FakePipeManager()
        pm.enqueue_signal()

        result = await wait_for_signal(pm, "/test/pipe", timeout=5.0)
        assert result is True

    @pytest.mark.asyncio
    async def test_no_signal_times_out(self):
        """No signal -> times out and returns False."""
        pm = _FakePipeManager()

        result = await wait_for_signal(pm, "/test/pipe", timeout=0.05)
        assert result is False

    @pytest.mark.asyncio
    async def test_multiple_signals_coalesce(self):
        """Multiple signals in buffer -> all drained, returns True once."""
        pm = _FakePipeManager()
        pm.enqueue_signal(b"\x01")
        pm.enqueue_signal(b"\x01")
        pm.enqueue_signal(b"\x01")

        result = await wait_for_signal(pm, "/test/pipe", timeout=5.0)
        assert result is True
        # All signals should be drained
        assert len(pm._signals) == 0

    @pytest.mark.asyncio
    async def test_pipe_closed_raises(self):
        """Closed pipe -> raises exception (caller handles)."""
        pm = _FakePipeManager()
        pm._closed = True

        with pytest.raises(RuntimeError, match="PipeClosedError"):
            await wait_for_signal(pm, "/test/pipe", timeout=5.0)

    @pytest.mark.asyncio
    async def test_no_timeout_blocks_until_signal(self):
        """timeout=None -> blocks indefinitely until signal arrives."""
        pm = _FakePipeManager()

        async def deliver_later():
            await asyncio.sleep(0.05)
            pm.enqueue_signal()

        task = asyncio.create_task(deliver_later())
        result = await wait_for_signal(pm, "/test/pipe", timeout=None)
        assert result is True
        await task

    @pytest.mark.asyncio
    async def test_signal_during_wait(self):
        """Signal arrives while waiting -> returns True promptly."""
        pm = _FakePipeManager()

        async def deliver_later():
            await asyncio.sleep(0.02)
            pm.enqueue_signal()

        task = asyncio.create_task(deliver_later())
        result = await wait_for_signal(pm, "/test/pipe", timeout=5.0)
        assert result is True
        await task
