"""Unit tests for StreamRegistry.

TDD-first tests for the extracted SSE stream management component.
"""

import asyncio
import logging

import pytest

from nexus.bricks.a2a.stream_registry import StreamRegistry


class TestRegister:
    def test_register_returns_queue(self) -> None:
        registry = StreamRegistry()
        queue = registry.register("task-1")
        assert isinstance(queue, asyncio.Queue)

    def test_register_returns_bounded_queue(self) -> None:
        registry = StreamRegistry(maxsize=10)
        queue = registry.register("task-1")
        assert queue.maxsize == 10

    def test_register_default_maxsize(self) -> None:
        registry = StreamRegistry()
        queue = registry.register("task-1")
        assert queue.maxsize == 100

    def test_register_multiple_subscribers(self) -> None:
        registry = StreamRegistry()
        q1 = registry.register("task-1")
        q2 = registry.register("task-1")
        assert q1 is not q2


class TestUnregister:
    def test_unregister_removes_queue(self) -> None:
        registry = StreamRegistry()
        queue = registry.register("task-1")
        registry.unregister("task-1", queue)
        # Pushing after unregister should be a no-op (no subscribers)
        registry.push_event("task-1", {"test": True})
        assert queue.empty()

    def test_unregister_unknown_task_is_noop(self) -> None:
        registry = StreamRegistry()
        queue: asyncio.Queue[dict | None] = asyncio.Queue()
        # Should not raise
        registry.unregister("nonexistent", queue)

    def test_unregister_unknown_queue_is_noop(self) -> None:
        registry = StreamRegistry()
        registry.register("task-1")
        other_queue: asyncio.Queue[dict | None] = asyncio.Queue()
        # Should not raise
        registry.unregister("task-1", other_queue)

    def test_cleanup_removes_empty_task_entry(self) -> None:
        registry = StreamRegistry()
        queue = registry.register("task-1")
        registry.unregister("task-1", queue)
        # Internal dict should be cleaned up
        assert "task-1" not in registry._active_streams


class TestPushEvent:
    def test_push_event_to_subscriber(self) -> None:
        registry = StreamRegistry()
        queue = registry.register("task-1")
        event = {"statusUpdate": {"state": "working"}}
        registry.push_event("task-1", event)
        assert queue.get_nowait() == event

    def test_push_event_to_multiple_subscribers(self) -> None:
        registry = StreamRegistry()
        q1 = registry.register("task-1")
        q2 = registry.register("task-1")
        event = {"statusUpdate": {"state": "completed"}}
        registry.push_event("task-1", event)
        assert q1.get_nowait() == event
        assert q2.get_nowait() == event

    def test_push_to_empty_is_noop(self) -> None:
        registry = StreamRegistry()
        # Should not raise when no subscribers exist
        registry.push_event("nonexistent", {"test": True})

    def test_bounded_queue_logs_warning_on_full(self, caplog: pytest.LogCaptureFixture) -> None:
        registry = StreamRegistry(maxsize=1)
        queue = registry.register("task-1")
        # Fill the queue
        registry.push_event("task-1", {"event": 1})
        # This should log a warning, not raise
        with caplog.at_level(logging.WARNING):
            registry.push_event("task-1", {"event": 2})
        assert "queue full" in caplog.text.lower()
        # Only the first event should be in the queue
        assert queue.get_nowait() == {"event": 1}
        assert queue.empty()


class TestStreamRegistryActivity:
    """Tests for last_activity tracking (Issue #2811)."""

    def test_register_sets_last_activity(self) -> None:
        reg = StreamRegistry()
        reg.register("task-1")
        assert "task-1" in reg._last_activity

    def test_push_event_updates_last_activity(self) -> None:
        reg = StreamRegistry()
        reg.register("task-1")
        old_activity = reg._last_activity["task-1"]
        import time

        time.sleep(0.01)
        reg.push_event("task-1", {"type": "test"})
        assert reg._last_activity["task-1"] >= old_activity

    def test_unregister_cleans_last_activity(self) -> None:
        reg = StreamRegistry()
        queue = reg.register("task-1")
        reg.unregister("task-1", queue)
        assert "task-1" not in reg._last_activity

    def test_get_idle_tasks(self) -> None:
        import time

        reg = StreamRegistry()
        reg.register("task-1")
        reg.register("task-2")
        reg._last_activity["task-1"] = time.monotonic() - 600
        idle = reg.get_idle_tasks(300.0)
        assert "task-1" in idle
        assert "task-2" not in idle

    def test_close_task_streams_sends_sentinel(self) -> None:
        reg = StreamRegistry()
        q1 = reg.register("task-1")
        q2 = reg.register("task-1")
        closed = reg.close_task_streams("task-1")
        assert closed == 2
        assert q1.get_nowait() is None
        assert q2.get_nowait() is None
        assert reg.task_count == 0

    def test_active_stream_count(self) -> None:
        reg = StreamRegistry()
        reg.register("task-1")
        reg.register("task-1")
        reg.register("task-2")
        assert reg.active_stream_count == 3
        assert reg.task_count == 2
