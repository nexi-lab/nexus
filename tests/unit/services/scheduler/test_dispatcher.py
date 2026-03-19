"""Tests for TaskDispatcher timer gate (Issue #2747).

Unit tests for:
- Timer arm/re-arm logic
- pg_notify JSON payload parsing with deadline
- Deadline event signaling
- Cold-start seeding from DB
- Per-executor dispatch loop wake on deadline event
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nexus.system_services.scheduler.dispatcher import TaskDispatcher


@pytest.fixture()
def mock_scheduler() -> MagicMock:
    """Minimal mock of SchedulerService for dispatcher tests."""
    scheduler = MagicMock()
    scheduler.dequeue_next = AsyncMock(return_value=None)
    scheduler.run_aging_sweep = AsyncMock(return_value=0)
    scheduler.run_starvation_promotion = AsyncMock(return_value=0)
    scheduler.pool = MagicMock()
    scheduler._queue = MagicMock()
    scheduler._queue.nearest_deadline = AsyncMock(return_value=None)
    scheduler._state_emitter = None
    return scheduler


@pytest.fixture()
def dispatcher(mock_scheduler: MagicMock) -> TaskDispatcher:
    return TaskDispatcher(mock_scheduler, poll_interval=30)


# ---------------------------------------------------------------------------
# Timer arm/re-arm logic
# ---------------------------------------------------------------------------


class TestTimerArm:
    """Test _arm_timer and _on_deadline_reached."""

    def test_arm_timer_sets_next_deadline(self, dispatcher: TaskDispatcher) -> None:
        """Arming the timer records the deadline."""
        deadline = datetime.now(UTC) + timedelta(seconds=10)
        with patch("asyncio.get_running_loop") as mock_loop:
            mock_loop.return_value = MagicMock()
            mock_loop.return_value.call_later.return_value = MagicMock()
            dispatcher._arm_timer(deadline)

        assert dispatcher._next_deadline == deadline
        assert dispatcher._deadline_timer is not None

    def test_arm_timer_earlier_deadline_replaces(self, dispatcher: TaskDispatcher) -> None:
        """A new earlier deadline replaces the current one."""
        later = datetime.now(UTC) + timedelta(seconds=60)
        earlier = datetime.now(UTC) + timedelta(seconds=5)

        with patch("asyncio.get_running_loop") as mock_loop:
            mock_handle = MagicMock()
            mock_loop.return_value = MagicMock()
            mock_loop.return_value.call_later.return_value = mock_handle
            dispatcher._arm_timer(later)

            first_timer = mock_handle
            dispatcher._arm_timer(earlier)

        assert dispatcher._next_deadline == earlier
        # The first timer should have been cancelled
        first_timer.cancel.assert_called_once()

    def test_arm_timer_later_deadline_ignored(self, dispatcher: TaskDispatcher) -> None:
        """A later deadline does not replace an earlier one."""
        earlier = datetime.now(UTC) + timedelta(seconds=5)
        later = datetime.now(UTC) + timedelta(seconds=60)

        with patch("asyncio.get_running_loop") as mock_loop:
            mock_loop.return_value = MagicMock()
            mock_loop.return_value.call_later.return_value = MagicMock()
            dispatcher._arm_timer(earlier)
            dispatcher._arm_timer(later)

        assert dispatcher._next_deadline == earlier

    def test_arm_timer_past_deadline_zero_delay(self, dispatcher: TaskDispatcher) -> None:
        """A past deadline results in delay=0 (fires immediately)."""
        past = datetime.now(UTC) - timedelta(seconds=10)

        with patch("asyncio.get_running_loop") as mock_loop:
            mock_loop.return_value = MagicMock()
            mock_loop.return_value.call_later.return_value = MagicMock()
            dispatcher._arm_timer(past)

        call_args = mock_loop.return_value.call_later.call_args
        delay = call_args[0][0]
        assert delay == 0.0

    def test_on_deadline_reached_sets_event(self, dispatcher: TaskDispatcher) -> None:
        """Deadline callback sets the deadline event and clears state."""
        dispatcher._next_deadline = datetime.now(UTC)
        dispatcher._deadline_timer = MagicMock()

        dispatcher._on_deadline_reached()

        assert dispatcher._deadline_event.is_set()
        assert dispatcher._next_deadline is None
        assert dispatcher._deadline_timer is None


# ---------------------------------------------------------------------------
# pg_notify JSON payload parsing
# ---------------------------------------------------------------------------


class TestNotificationParsing:
    """Test _on_notification with JSON deadline payload format."""

    def test_immediate_task_no_deadline(self, dispatcher: TaskDispatcher) -> None:
        """JSON payload without deadline wakes global event, no timer."""
        payload = json.dumps({"task_id": "task-1", "executor_id": "e1"})
        dispatcher._on_notification(None, 0, "task_enqueued", payload)

        assert dispatcher._global_event.is_set()
        assert dispatcher._next_deadline is None

    def test_immediate_task_routes_to_executor(self, dispatcher: TaskDispatcher) -> None:
        """JSON payload with known executor routes to per-executor event."""
        # Register an executor event
        event = asyncio.Event()
        dispatcher._executor_events["e1"] = event

        payload = json.dumps({"task_id": "task-1", "executor_id": "e1"})
        dispatcher._on_notification(None, 0, "task_enqueued", payload)

        assert event.is_set()
        assert not dispatcher._global_event.is_set()

    def test_deadlined_task_arms_timer(self, dispatcher: TaskDispatcher) -> None:
        """JSON payload with deadline field arms timer."""
        deadline = datetime.now(UTC) + timedelta(seconds=30)
        payload = json.dumps(
            {
                "task_id": "task-1",
                "executor_id": "e1",
                "deadline": deadline.isoformat(),
            }
        )

        with patch("asyncio.get_running_loop") as mock_loop:
            mock_loop.return_value = MagicMock()
            mock_loop.return_value.call_later.return_value = MagicMock()
            dispatcher._on_notification(None, 0, "task_enqueued", payload)

        assert dispatcher._global_event.is_set()
        assert dispatcher._next_deadline == deadline

    def test_malformed_deadline_gracefully_handled(self, dispatcher: TaskDispatcher) -> None:
        """Malformed deadline in JSON payload doesn't crash, just warns."""
        payload = json.dumps(
            {
                "task_id": "task-1",
                "executor_id": "e1",
                "deadline": "not-a-date",
            }
        )

        # Should not raise
        dispatcher._on_notification(None, 0, "task_enqueued", payload)

        assert dispatcher._global_event.is_set()
        assert dispatcher._next_deadline is None

    def test_invalid_json_gracefully_handled(self, dispatcher: TaskDispatcher) -> None:
        """Non-JSON payload doesn't crash, wakes all cursors."""
        dispatcher._on_notification(None, 0, "task_enqueued", "not-json")

        assert dispatcher._global_event.is_set()
        assert dispatcher._next_deadline is None

    def test_naive_deadline_normalized_to_utc(self, dispatcher: TaskDispatcher) -> None:
        """Timezone-naive deadline in JSON payload is assumed UTC."""
        naive_deadline = datetime(2026, 6, 15, 12, 0, 0)
        payload = json.dumps(
            {
                "task_id": "task-1",
                "executor_id": "e1",
                "deadline": naive_deadline.isoformat(),
            }
        )

        with patch("asyncio.get_running_loop") as mock_loop:
            mock_loop.return_value = MagicMock()
            mock_loop.return_value.call_later.return_value = MagicMock()
            dispatcher._on_notification(None, 0, "task_enqueued", payload)

        assert dispatcher._next_deadline is not None
        assert dispatcher._next_deadline.tzinfo is not None  # Should be UTC


# ---------------------------------------------------------------------------
# Cold-start seeding
# ---------------------------------------------------------------------------


class TestColdStartSeeding:
    """Test _seed_timer_from_db for startup timer initialization."""

    async def test_seeds_from_db_when_deadline_exists(
        self, dispatcher: TaskDispatcher, mock_scheduler: MagicMock
    ) -> None:
        """Startup query finds a future deadline and seeds the timer."""
        future_deadline = datetime.now(UTC) + timedelta(seconds=120)
        mock_conn = AsyncMock()
        mock_scheduler.pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_scheduler.pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_scheduler._queue.nearest_deadline = AsyncMock(return_value=future_deadline)

        with patch("asyncio.get_running_loop") as mock_loop:
            mock_loop.return_value = MagicMock()
            mock_loop.return_value.call_later.return_value = MagicMock()
            await dispatcher._seed_timer_from_db()

        assert dispatcher._next_deadline == future_deadline

    async def test_no_seed_when_no_deadlines(
        self, dispatcher: TaskDispatcher, mock_scheduler: MagicMock
    ) -> None:
        """Startup query finds no deadlines — timer stays unset."""
        mock_conn = AsyncMock()
        mock_scheduler.pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_scheduler.pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_scheduler._queue.nearest_deadline = AsyncMock(return_value=None)

        await dispatcher._seed_timer_from_db()

        assert dispatcher._next_deadline is None

    async def test_db_failure_gracefully_handled(
        self, dispatcher: TaskDispatcher, mock_scheduler: MagicMock
    ) -> None:
        """DB error during seeding doesn't crash, falls back to poll."""
        mock_scheduler.pool.acquire.side_effect = RuntimeError("DB unavailable")

        # Should not raise
        await dispatcher._seed_timer_from_db()

        assert dispatcher._next_deadline is None


# ---------------------------------------------------------------------------
# Per-executor dispatch loop wake integration
# ---------------------------------------------------------------------------


class TestDispatchLoopWake:
    """Test that the per-executor dispatch loop wakes on deadline event."""

    async def test_deadline_event_wakes_loop(
        self, dispatcher: TaskDispatcher, mock_scheduler: MagicMock
    ) -> None:
        """Setting the deadline event should unblock the executor dispatch loop."""
        dispatcher._running = True
        executor_event = asyncio.Event()

        call_count = 0

        def stop_after_two(**_: object) -> None:
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                dispatcher._running = False
            return None

        mock_scheduler.dequeue_next.side_effect = stop_after_two

        # Fire deadline event after a short delay
        async def fire_deadline() -> None:
            await asyncio.sleep(0.05)
            dispatcher._deadline_event.set()

        task = asyncio.create_task(fire_deadline())

        await asyncio.wait_for(
            dispatcher._executor_dispatch_loop("test-executor", executor_event),
            timeout=2.0,
        )
        await task

        assert call_count >= 2

    async def test_deadline_rearm_after_fire(
        self, dispatcher: TaskDispatcher, mock_scheduler: MagicMock
    ) -> None:
        """After deadline fires, dispatch loop re-seeds timer from DB for next deadline."""
        dispatcher._running = True
        executor_event = asyncio.Event()

        later_deadline = datetime.now(UTC) + timedelta(seconds=60)
        call_count = 0

        def stop_after_two(**_: object) -> None:
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                dispatcher._running = False
            return None

        mock_scheduler.dequeue_next.side_effect = stop_after_two

        mock_conn = AsyncMock()
        mock_scheduler.pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_scheduler.pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_scheduler._queue.nearest_deadline = AsyncMock(return_value=later_deadline)

        async def fire_deadline() -> None:
            await asyncio.sleep(0.05)
            dispatcher._deadline_event.set()

        task = asyncio.create_task(fire_deadline())

        with patch("asyncio.get_running_loop") as mock_loop:
            mock_loop.return_value = MagicMock()
            mock_loop.return_value.call_later.return_value = MagicMock()
            await asyncio.wait_for(
                dispatcher._executor_dispatch_loop("test-executor", executor_event),
                timeout=2.0,
            )

        await task

        mock_scheduler._queue.nearest_deadline.assert_called()
        assert dispatcher._next_deadline == later_deadline

    async def test_executor_event_still_works(
        self, dispatcher: TaskDispatcher, mock_scheduler: MagicMock
    ) -> None:
        """Setting the per-executor event (immediate task) still wakes the loop."""
        dispatcher._running = True
        executor_event = asyncio.Event()

        call_count = 0

        def stop_after_two(**_: object) -> None:
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                dispatcher._running = False
            return None

        mock_scheduler.dequeue_next.side_effect = stop_after_two

        async def fire_notification() -> None:
            await asyncio.sleep(0.05)
            executor_event.set()

        task = asyncio.create_task(fire_notification())

        await asyncio.wait_for(
            dispatcher._executor_dispatch_loop("test-executor", executor_event),
            timeout=2.0,
        )
        await task

        assert call_count >= 2


# ---------------------------------------------------------------------------
# Stop / cleanup
# ---------------------------------------------------------------------------


class TestStopCleanup:
    """Test that stop() properly cleans up timer state."""

    async def test_stop_cancels_timer(self, dispatcher: TaskDispatcher) -> None:
        """stop() cancels any pending deadline timer."""
        mock_handle = MagicMock()
        dispatcher._deadline_timer = mock_handle
        dispatcher._next_deadline = datetime.now(UTC) + timedelta(seconds=60)
        dispatcher._running = True

        await dispatcher.stop()

        mock_handle.cancel.assert_called_once()
        assert dispatcher._deadline_timer is None

    async def test_stop_sets_deadline_event(self, dispatcher: TaskDispatcher) -> None:
        """stop() sets the deadline event to unblock any waiting loop."""
        dispatcher._running = True

        await dispatcher.stop()

        assert dispatcher._deadline_event.is_set()
