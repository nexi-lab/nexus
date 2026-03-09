"""Tests for StreamReaper -- periodic cleanup of idle A2A streams."""

import asyncio
import time

import pytest

from nexus.bricks.a2a.stream_reaper import StreamReaper
from nexus.bricks.a2a.stream_registry import StreamRegistry


@pytest.fixture
def anyio_backend():
    """Restrict anyio tests to asyncio (trio is not installed)."""
    return "asyncio"


class TestStreamReaperInit:
    def test_default_config(self) -> None:
        registry = StreamRegistry()
        reaper = StreamReaper(registry)
        assert reaper._max_idle_seconds == 300.0
        assert reaper._check_interval == 60.0
        assert not reaper.is_running

    def test_custom_config(self) -> None:
        registry = StreamRegistry()
        reaper = StreamReaper(registry, max_idle_seconds=60.0, check_interval=10.0)
        assert reaper._max_idle_seconds == 60.0
        assert reaper._check_interval == 10.0


class TestStreamReaperReap:
    def test_reap_no_idle_tasks(self) -> None:
        registry = StreamRegistry()
        registry.register("task-1")
        reaper = StreamReaper(registry, max_idle_seconds=300.0)
        reaper._reap()
        # Nothing should be reaped -- task was just registered
        assert registry.task_count == 1

    def test_reap_idle_task(self) -> None:
        registry = StreamRegistry()
        queue = registry.register("task-1")

        # Simulate idle time by backdating last_activity
        registry._last_activity["task-1"] = time.monotonic() - 600  # 10 min ago

        reaper = StreamReaper(registry, max_idle_seconds=300.0)
        reaper._reap()

        # Task should be reaped
        assert registry.task_count == 0
        # Queue should receive sentinel
        assert queue.get_nowait() is None

    def test_reap_multiple_idle_tasks(self) -> None:
        registry = StreamRegistry()
        q1 = registry.register("task-1")
        q2 = registry.register("task-2")
        registry.register("task-3")  # Will remain active

        now = time.monotonic()
        registry._last_activity["task-1"] = now - 600
        registry._last_activity["task-2"] = now - 400
        # task-3 keeps its recent timestamp

        reaper = StreamReaper(registry, max_idle_seconds=300.0)
        reaper._reap()

        assert registry.task_count == 1  # Only task-3 remains
        assert q1.get_nowait() is None
        assert q2.get_nowait() is None

    def test_reap_preserves_active_tasks(self) -> None:
        registry = StreamRegistry()
        registry.register("active-task")
        reaper = StreamReaper(registry, max_idle_seconds=300.0)
        reaper._reap()
        assert registry.task_count == 1


class TestStreamReaperLifecycle:
    @pytest.mark.anyio
    async def test_start_creates_task(self) -> None:
        registry = StreamRegistry()
        reaper = StreamReaper(registry, check_interval=0.1)
        await reaper.start()
        assert reaper.is_running
        await reaper.stop()
        assert not reaper.is_running

    @pytest.mark.anyio
    async def test_start_idempotent(self) -> None:
        registry = StreamRegistry()
        reaper = StreamReaper(registry, check_interval=0.1)
        await reaper.start()
        task1 = reaper._task
        await reaper.start()  # Second start should be no-op
        assert reaper._task is task1
        await reaper.stop()

    @pytest.mark.anyio
    async def test_stop_without_start(self) -> None:
        registry = StreamRegistry()
        reaper = StreamReaper(registry)
        await reaper.stop()  # Should not raise

    @pytest.mark.anyio
    async def test_reaper_runs_periodically(self) -> None:
        registry = StreamRegistry()
        registry.register("task-1")
        registry._last_activity["task-1"] = time.monotonic() - 600

        reaper = StreamReaper(registry, max_idle_seconds=300.0, check_interval=0.05)
        await reaper.start()
        await asyncio.sleep(0.15)  # Wait for at least one reap cycle
        await reaper.stop()

        # Task should have been reaped
        assert registry.task_count == 0

    @pytest.mark.anyio
    async def test_stop_cancels_cleanly(self) -> None:
        registry = StreamRegistry()
        reaper = StreamReaper(registry, check_interval=10.0)
        await reaper.start()
        assert reaper.is_running
        await reaper.stop()
        assert not reaper.is_running
