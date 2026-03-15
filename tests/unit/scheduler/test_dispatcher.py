"""Tests for multi-cursor task dispatcher (Issue #2748).

Tests cursor lifecycle, NOTIFY demux, adaptive polling, exponential backoff,
reconcile sweep, and agent state event integration.
"""

import asyncio
import contextlib
import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nexus.system_services.scheduler.constants import PriorityTier
from nexus.system_services.scheduler.dispatcher import (
    _BACKOFF_BASE_SECS,
    TaskDispatcher,
)
from nexus.system_services.scheduler.events import AgentStateEmitter, AgentStateEvent
from nexus.system_services.scheduler.models import ScheduledTask


def _make_task(
    *,
    task_id: str = "task-1",
    executor_id: str = "exec-a",
    task_type: str = "default",
    priority_class: str = "batch",
    effective_tier: int = 3,
) -> ScheduledTask:
    """Create a minimal ScheduledTask for testing."""
    return ScheduledTask(
        id=task_id,
        agent_id="agent-1",
        executor_id=executor_id,
        task_type=task_type,
        payload={},
        priority_tier=PriorityTier.NORMAL,
        effective_tier=effective_tier,
        enqueued_at=datetime.now(UTC),
        status="running",
        priority_class=priority_class,
    )


def _make_scheduler_service(
    *,
    dequeue_results: list[ScheduledTask | None] | None = None,
    state_emitter: AgentStateEmitter | None = None,
) -> MagicMock:
    """Create a mock SchedulerService."""
    svc = MagicMock()
    svc._state_emitter = state_emitter

    # Mock pool
    pool = MagicMock()
    pool.get_size.return_value = 10
    svc._pool = pool

    # pool property
    type(svc).pool = property(lambda self: self._pool)

    # Async mocks for methods used by dispatcher
    svc.run_aging_sweep = AsyncMock(return_value=0)
    svc.run_starvation_promotion = AsyncMock(return_value=0)

    if dequeue_results is not None:
        svc.dequeue_next = AsyncMock(side_effect=dequeue_results)
    else:
        svc.dequeue_next = AsyncMock(return_value=None)

    # For reconcile: pool.acquire -> conn -> conn.fetch
    conn_mock = AsyncMock()
    conn_mock.fetch = AsyncMock(return_value=[])

    # Create an async context manager for pool.acquire()
    acquire_cm = AsyncMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn_mock)
    acquire_cm.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=acquire_cm)

    return svc


class TestDispatcherLifecycle:
    """Test start/stop and basic lifecycle."""

    @pytest.mark.asyncio
    async def test_start_sets_running(self):
        svc = _make_scheduler_service()
        dispatcher = TaskDispatcher(svc)
        await dispatcher.start()
        assert dispatcher._running is True
        await dispatcher.stop()
        assert dispatcher._running is False

    @pytest.mark.asyncio
    async def test_double_start_is_noop(self):
        svc = _make_scheduler_service()
        dispatcher = TaskDispatcher(svc)
        await dispatcher.start()
        task1 = dispatcher._task_group_task
        await dispatcher.start()  # Should not create a second task
        assert dispatcher._task_group_task is task1
        await dispatcher.stop()

    @pytest.mark.asyncio
    async def test_stop_before_start_is_noop(self):
        svc = _make_scheduler_service()
        dispatcher = TaskDispatcher(svc)
        await dispatcher.stop()  # Should not raise

    @pytest.mark.asyncio
    async def test_stop_cancels_cursors(self):
        svc = _make_scheduler_service()
        dispatcher = TaskDispatcher(svc)
        await dispatcher.start()

        # Spawn a cursor
        dispatcher._spawn_cursor("exec-a")
        assert "exec-a" in dispatcher._cursors

        await dispatcher.stop()
        assert len(dispatcher._cursors) == 0
        assert len(dispatcher._executor_events) == 0


class TestCursorManagement:
    """Test per-executor cursor spawn/cancel."""

    @pytest.mark.asyncio
    async def test_spawn_cursor_creates_task(self):
        svc = _make_scheduler_service()
        dispatcher = TaskDispatcher(svc)
        dispatcher._running = True

        dispatcher._spawn_cursor("exec-a")
        assert "exec-a" in dispatcher._cursors
        assert "exec-a" in dispatcher._executor_events
        assert not dispatcher._cursors["exec-a"].done()

        # Cleanup
        dispatcher._running = False
        dispatcher._cursors["exec-a"].cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await dispatcher._cursors["exec-a"]

    @pytest.mark.asyncio
    async def test_spawn_cursor_idempotent(self):
        svc = _make_scheduler_service()
        dispatcher = TaskDispatcher(svc)
        dispatcher._running = True

        dispatcher._spawn_cursor("exec-a")
        task1 = dispatcher._cursors["exec-a"]
        dispatcher._spawn_cursor("exec-a")  # Should not replace
        assert dispatcher._cursors["exec-a"] is task1

        # Cleanup
        dispatcher._running = False
        task1.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task1

    @pytest.mark.asyncio
    async def test_cancel_cursor_removes_entry(self):
        svc = _make_scheduler_service()
        dispatcher = TaskDispatcher(svc)
        dispatcher._running = True

        dispatcher._spawn_cursor("exec-a")
        dispatcher._cancel_cursor("exec-a")
        assert "exec-a" not in dispatcher._cursors
        assert "exec-a" not in dispatcher._executor_events

    @pytest.mark.asyncio
    async def test_cancel_nonexistent_cursor_is_noop(self):
        svc = _make_scheduler_service()
        dispatcher = TaskDispatcher(svc)
        dispatcher._cancel_cursor("nonexistent")  # Should not raise

    @pytest.mark.asyncio
    async def test_cursor_count_property(self):
        svc = _make_scheduler_service()
        dispatcher = TaskDispatcher(svc)
        dispatcher._running = True

        assert dispatcher.cursor_count == 0
        dispatcher._spawn_cursor("exec-a")
        dispatcher._spawn_cursor("exec-b")
        assert dispatcher.cursor_count == 2

        dispatcher._cancel_cursor("exec-a")
        # Give a moment for cancellation to propagate
        await asyncio.sleep(0.01)
        assert dispatcher.cursor_count == 1

        # Cleanup
        dispatcher._running = False
        for t in dispatcher._cursors.values():
            t.cancel()
        await asyncio.gather(*dispatcher._cursors.values(), return_exceptions=True)

    @pytest.mark.asyncio
    async def test_active_executors_property(self):
        svc = _make_scheduler_service()
        dispatcher = TaskDispatcher(svc)
        dispatcher._running = True

        dispatcher._spawn_cursor("exec-a")
        dispatcher._spawn_cursor("exec-b")
        assert set(dispatcher.active_executors) == {"exec-a", "exec-b"}

        # Cleanup
        dispatcher._running = False
        for t in dispatcher._cursors.values():
            t.cancel()
        await asyncio.gather(*dispatcher._cursors.values(), return_exceptions=True)


class TestNotifyDemux:
    """Test NOTIFY payload routing to per-executor events."""

    def test_routes_to_correct_executor(self):
        svc = _make_scheduler_service()
        dispatcher = TaskDispatcher(svc)

        event_a = asyncio.Event()
        event_b = asyncio.Event()
        dispatcher._executor_events["exec-a"] = event_a
        dispatcher._executor_events["exec-b"] = event_b

        payload = json.dumps({"task_id": "t1", "executor_id": "exec-a"})
        dispatcher._on_notification(None, 0, "task_enqueued", payload)

        assert event_a.is_set()
        assert not event_b.is_set()

    def test_unknown_executor_wakes_all(self):
        svc = _make_scheduler_service()
        dispatcher = TaskDispatcher(svc)

        event_a = asyncio.Event()
        event_b = asyncio.Event()
        dispatcher._executor_events["exec-a"] = event_a
        dispatcher._executor_events["exec-b"] = event_b

        payload = json.dumps({"task_id": "t1", "executor_id": "exec-unknown"})
        dispatcher._on_notification(None, 0, "task_enqueued", payload)

        assert event_a.is_set()
        assert event_b.is_set()

    def test_invalid_json_wakes_all(self):
        svc = _make_scheduler_service()
        dispatcher = TaskDispatcher(svc)

        event_a = asyncio.Event()
        dispatcher._executor_events["exec-a"] = event_a

        dispatcher._on_notification(None, 0, "task_enqueued", "not-json")

        assert event_a.is_set()

    def test_missing_executor_id_wakes_all(self):
        svc = _make_scheduler_service()
        dispatcher = TaskDispatcher(svc)

        event_a = asyncio.Event()
        dispatcher._executor_events["exec-a"] = event_a

        payload = json.dumps({"task_id": "t1"})
        dispatcher._on_notification(None, 0, "task_enqueued", payload)

        assert event_a.is_set()


class TestAgentStateIntegration:
    """Test cursor spawn/cancel via agent state events."""

    @pytest.mark.asyncio
    async def test_connected_spawns_cursor(self):
        svc = _make_scheduler_service()
        dispatcher = TaskDispatcher(svc)
        dispatcher._running = True

        event = AgentStateEvent(
            agent_id="exec-a",
            previous_state="SUSPENDED",
            new_state="CONNECTED",
        )
        await dispatcher._on_agent_state_change(event)
        assert "exec-a" in dispatcher._cursors

        # Cleanup
        dispatcher._running = False
        for t in dispatcher._cursors.values():
            t.cancel()
        await asyncio.gather(*dispatcher._cursors.values(), return_exceptions=True)

    @pytest.mark.asyncio
    async def test_idle_spawns_cursor(self):
        svc = _make_scheduler_service()
        dispatcher = TaskDispatcher(svc)
        dispatcher._running = True

        event = AgentStateEvent(
            agent_id="exec-b",
            previous_state="CONNECTED",
            new_state="IDLE",
        )
        await dispatcher._on_agent_state_change(event)
        assert "exec-b" in dispatcher._cursors

        # Cleanup
        dispatcher._running = False
        for t in dispatcher._cursors.values():
            t.cancel()
        await asyncio.gather(*dispatcher._cursors.values(), return_exceptions=True)

    @pytest.mark.asyncio
    async def test_suspended_cancels_cursor(self):
        svc = _make_scheduler_service()
        dispatcher = TaskDispatcher(svc)
        dispatcher._running = True

        # First spawn a cursor
        dispatcher._spawn_cursor("exec-a")
        assert "exec-a" in dispatcher._cursors

        # Then suspend it
        event = AgentStateEvent(
            agent_id="exec-a",
            previous_state="CONNECTED",
            new_state="SUSPENDED",
        )
        await dispatcher._on_agent_state_change(event)
        assert "exec-a" not in dispatcher._cursors

    @pytest.mark.asyncio
    async def test_state_event_when_not_running_is_noop(self):
        svc = _make_scheduler_service()
        dispatcher = TaskDispatcher(svc)
        dispatcher._running = False

        event = AgentStateEvent(
            agent_id="exec-a",
            previous_state="SUSPENDED",
            new_state="CONNECTED",
        )
        await dispatcher._on_agent_state_change(event)
        assert "exec-a" not in dispatcher._cursors

    @pytest.mark.asyncio
    async def test_start_registers_state_handler(self):
        emitter = AgentStateEmitter()
        svc = _make_scheduler_service(state_emitter=emitter)
        dispatcher = TaskDispatcher(svc)

        assert emitter.handler_count == 0
        await dispatcher.start()
        # SchedulerService.__init__ registers one handler, start() registers another
        # But our mock doesn't call __init__, so just check our handler is registered
        assert emitter.handler_count >= 1
        await dispatcher.stop()
        # Handler should be unregistered on stop
        # (SchedulerService's own handler remains if it was registered)

    @pytest.mark.asyncio
    async def test_stop_unregisters_state_handler(self):
        emitter = AgentStateEmitter()
        svc = _make_scheduler_service(state_emitter=emitter)
        dispatcher = TaskDispatcher(svc)

        await dispatcher.start()
        handler_count_after_start = emitter.handler_count
        await dispatcher.stop()
        assert emitter.handler_count == handler_count_after_start - 1


class TestExecutorDispatchLoop:
    """Test the per-executor dispatch loop behavior."""

    @pytest.mark.asyncio
    async def test_dequeues_task_for_executor(self):
        task = _make_task(executor_id="exec-a")
        svc = _make_scheduler_service(dequeue_results=[task, None])
        dispatcher = TaskDispatcher(svc)
        dispatcher._running = True

        event = asyncio.Event()
        dispatcher._executor_events["exec-a"] = event

        # Run the loop briefly — it will dequeue the task, then get None and wait
        loop_task = asyncio.create_task(dispatcher._executor_dispatch_loop("exec-a", event))
        await asyncio.sleep(0.05)
        dispatcher._running = False
        event.set()  # Wake to exit
        dispatcher._global_event.set()

        with contextlib.suppress(asyncio.CancelledError):
            loop_task.cancel()
            await loop_task

        # Verify dequeue was called with executor_id
        svc.dequeue_next.assert_called_with(executor_id="exec-a")

    @pytest.mark.asyncio
    async def test_exponential_backoff_on_errors(self):
        svc = _make_scheduler_service()
        svc.dequeue_next = AsyncMock(side_effect=RuntimeError("db error"))
        dispatcher = TaskDispatcher(svc)
        dispatcher._running = True

        event = asyncio.Event()

        # Let it run for a bit — it should back off
        with patch(
            "nexus.system_services.scheduler.dispatcher.asyncio.sleep", new_callable=AsyncMock
        ) as mock_sleep:
            loop_task = asyncio.create_task(dispatcher._executor_dispatch_loop("exec-a", event))
            # Let 3 errors happen
            await asyncio.sleep(0.05)
            dispatcher._running = False
            loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await loop_task

            # Check that sleep was called with increasing backoff
            sleep_calls = [c.args[0] for c in mock_sleep.call_args_list if c.args]
            if len(sleep_calls) >= 2:
                assert sleep_calls[0] == _BACKOFF_BASE_SECS
                assert sleep_calls[1] == _BACKOFF_BASE_SECS * 2

    @pytest.mark.asyncio
    async def test_max_errors_stops_cursor(self):
        svc = _make_scheduler_service()
        svc.dequeue_next = AsyncMock(side_effect=RuntimeError("db error"))
        dispatcher = TaskDispatcher(svc)
        dispatcher._running = True

        event = asyncio.Event()

        # Patch sleep to not actually wait, and reduce max errors to 3
        with (
            patch(
                "nexus.system_services.scheduler.dispatcher.asyncio.sleep", new_callable=AsyncMock
            ),
            patch("nexus.system_services.scheduler.dispatcher._MAX_CONSECUTIVE_ERRORS", 3),
        ):
            await dispatcher._executor_dispatch_loop("exec-a", event)

        # Loop should have exited after max errors
        assert svc.dequeue_next.call_count == 3


class TestPoolSizingWarning:
    """Test pool sizing warning in cursor spawn."""

    @pytest.mark.asyncio
    async def test_warns_when_cursors_exceed_pool(self, caplog):
        svc = _make_scheduler_service()
        svc._pool.get_size.return_value = 3  # Small pool

        dispatcher = TaskDispatcher(svc)
        dispatcher._running = True

        # Spawn 2 cursors (3 - 2 = 1, so 2 > 1 triggers warning)
        dispatcher._spawn_cursor("exec-a")
        dispatcher._spawn_cursor("exec-b")

        assert any("exceeds pool size" in record.message for record in caplog.records)

        # Cleanup
        dispatcher._running = False
        for t in dispatcher._cursors.values():
            t.cancel()
        await asyncio.gather(*dispatcher._cursors.values(), return_exceptions=True)


class TestReconcile:
    """Test reconcile sweep functionality."""

    @pytest.mark.asyncio
    async def test_reconcile_spawns_missing_cursors(self):
        svc = _make_scheduler_service()
        dispatcher = TaskDispatcher(svc)
        dispatcher._running = True

        # Mock the DB query to return executors with queued tasks
        conn_mock = AsyncMock()
        conn_mock.fetch = AsyncMock(
            return_value=[{"executor_id": "exec-a"}, {"executor_id": "exec-b"}]
        )
        acquire_cm = AsyncMock()
        acquire_cm.__aenter__ = AsyncMock(return_value=conn_mock)
        acquire_cm.__aexit__ = AsyncMock(return_value=False)
        svc._pool.acquire = MagicMock(return_value=acquire_cm)

        await dispatcher._reconcile_cursors()

        assert "exec-a" in dispatcher._cursors
        assert "exec-b" in dispatcher._cursors

        # Cleanup
        dispatcher._running = False
        for t in dispatcher._cursors.values():
            t.cancel()
        await asyncio.gather(*dispatcher._cursors.values(), return_exceptions=True)

    @pytest.mark.asyncio
    async def test_reconcile_skips_existing_cursors(self):
        svc = _make_scheduler_service()
        dispatcher = TaskDispatcher(svc)
        dispatcher._running = True

        # Pre-spawn a cursor
        dispatcher._spawn_cursor("exec-a")
        original_task = dispatcher._cursors["exec-a"]

        # Mock DB to return exec-a
        conn_mock = AsyncMock()
        conn_mock.fetch = AsyncMock(return_value=[{"executor_id": "exec-a"}])
        acquire_cm = AsyncMock()
        acquire_cm.__aenter__ = AsyncMock(return_value=conn_mock)
        acquire_cm.__aexit__ = AsyncMock(return_value=False)
        svc._pool.acquire = MagicMock(return_value=acquire_cm)

        await dispatcher._reconcile_cursors()

        # Should not have replaced the existing cursor
        assert dispatcher._cursors["exec-a"] is original_task

        # Cleanup
        dispatcher._running = False
        for t in dispatcher._cursors.values():
            t.cancel()
        await asyncio.gather(*dispatcher._cursors.values(), return_exceptions=True)


class TestDRYStatusDict:
    """Test _task_to_status_dict helper (Issue #2748 DRY fix)."""

    def test_produces_correct_dict(self):
        from nexus.system_services.scheduler.service import _task_to_status_dict

        task = _make_task(
            task_id="t-123",
            executor_id="exec-a",
            priority_class="interactive",
            effective_tier=1,
        )
        result = _task_to_status_dict(task)

        assert result["id"] == "t-123"
        assert result["executor_id"] == "exec-a"
        assert result["priority_class"] == "interactive"
        assert result["effective_tier"] == 1
        assert result["status"] == "running"
        assert isinstance(result["enqueued_at"], str)
        assert result["boost_amount"] == "0"


class TestTaskColumnsConstant:
    """Test _TASK_COLUMNS DRY constant (Issue #2748)."""

    def test_task_columns_has_all_fields(self):
        from nexus.system_services.scheduler.queue import _TASK_COLUMNS

        required = [
            "id::text",
            "agent_id",
            "executor_id",
            "task_type",
            "payload::text",
            "priority_tier",
            "effective_tier",
            "enqueued_at",
            "status",
            "deadline",
            "boost_amount",
            "boost_tiers",
            "boost_reservation_id",
            "started_at",
            "completed_at",
            "error_message",
            "zone_id",
            "idempotency_key",
            "request_state",
            "priority_class",
            "executor_state",
            "estimated_service_time",
        ]
        for field in required:
            assert field in _TASK_COLUMNS, f"Missing field: {field}"


class TestNotifyPayload:
    """Test JSON NOTIFY payload generation."""

    @pytest.mark.asyncio
    async def test_enqueue_sends_json_notify(self):
        from nexus.system_services.scheduler.queue import TaskQueue

        queue = TaskQueue()
        conn = AsyncMock()
        conn.fetchval = AsyncMock(return_value="task-123")
        conn.execute = AsyncMock()

        await queue.enqueue(
            conn,
            agent_id="agent-1",
            executor_id="exec-a",
            task_type="test",
            payload={"key": "val"},
            priority_tier=3,
            effective_tier=3,
        )

        # Second call is the NOTIFY
        notify_call = conn.execute.call_args_list[0]
        payload_str = notify_call.args[1]
        payload = json.loads(payload_str)
        assert payload["task_id"] == "task-123"
        assert payload["executor_id"] == "exec-a"
