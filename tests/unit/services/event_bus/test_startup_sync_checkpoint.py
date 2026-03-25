"""Tests for startup_sync checkpoint safety (Issue #2752).

Verifies that:
- Checkpoint only advances to the last successfully processed event
- Failed events are retried on next startup (checkpoint not advanced past them)
- Truncated batches (hit max_sync_events) log a warning
- All-succeed case still works correctly
"""

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from nexus.services.event_bus.base import EventBusBase


def _make_operation(op_id: str, created_at: datetime, op_type: str = "write") -> Mock:
    """Create a mock OperationLogModel row."""
    op = Mock()
    op.operation_id = op_id
    op.operation_type = op_type
    op.path = f"/test/{op_id}.txt"
    op.new_path = None
    op.zone_id = "zone-1"
    op.status = "success"
    op.created_at = created_at
    return op


class ConcreteEventBus(EventBusBase):
    """Minimal concrete subclass for testing the ABC's startup_sync."""

    async def _do_start(self) -> None:
        pass

    async def _do_stop(self) -> None:
        pass

    async def publish(self, event):
        return 0

    async def wait_for_event(self, zone_id, path_pattern, timeout=30.0, since_version=None):
        return None

    async def health_check(self) -> bool:
        return True

    def subscribe(self, zone_id):
        pass

    def subscribe_durable(self, zone_id, consumer_name, deliver_policy="all"):
        pass


def _make_bus(
    operations: list[Mock],
    max_sync_events: int = 10_000,
) -> ConcreteEventBus:
    """Create a bus with mocked DB that returns the given operations."""
    mock_record_store = MagicMock()
    mock_session = MagicMock()

    # Mock the query chain
    mock_scalars = MagicMock()
    mock_scalars.all.return_value = operations
    mock_execute = MagicMock()
    mock_execute.scalars.return_value = mock_scalars
    mock_session.execute.return_value = mock_execute
    mock_session.__enter__ = Mock(return_value=mock_session)
    mock_session.__exit__ = Mock(return_value=False)
    mock_record_store.session_factory.return_value = mock_session

    bus = ConcreteEventBus(
        record_store=mock_record_store,
        node_id="test-node",
        max_sync_events=max_sync_events,
    )
    return bus


class TestStartupSyncCheckpointSafety:
    """Test that checkpoint advances correctly based on success/failure."""

    def test_all_succeed_advances_to_last(self):
        """When all events succeed, checkpoint advances to last event's timestamp."""
        t1 = datetime(2026, 1, 1, 0, 0, 1)
        t2 = datetime(2026, 1, 1, 0, 0, 2)
        t3 = datetime(2026, 1, 1, 0, 0, 3)
        ops = [
            _make_operation("op-1", t1),
            _make_operation("op-2", t2),
            _make_operation("op-3", t3),
        ]

        bus = _make_bus(ops)
        handler = AsyncMock()
        checkpoint_updates: list[datetime] = []

        async def run():
            bus._update_checkpoint = AsyncMock(side_effect=lambda ts: checkpoint_updates.append(ts))
            bus._get_checkpoint = AsyncMock(return_value=datetime(2026, 1, 1, 0, 0, 0))
            result = await bus.startup_sync(event_handler=handler)
            return result

        result = asyncio.run(run())

        assert result == 3
        assert handler.call_count == 3
        assert len(checkpoint_updates) == 1
        assert checkpoint_updates[0] == t3

    def test_partial_failure_stops_at_last_success(self):
        """When event #2 fails, checkpoint advances to event #1 only."""
        t1 = datetime(2026, 1, 1, 0, 0, 1)
        t2 = datetime(2026, 1, 1, 0, 0, 2)
        t3 = datetime(2026, 1, 1, 0, 0, 3)
        ops = [
            _make_operation("op-1", t1),
            _make_operation("op-2", t2),
            _make_operation("op-3", t3),
        ]

        bus = _make_bus(ops)
        call_count = 0

        async def handler(event):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise ConnectionError("down")

        checkpoint_updates: list[datetime] = []

        async def run():
            bus._update_checkpoint = AsyncMock(side_effect=lambda ts: checkpoint_updates.append(ts))
            bus._get_checkpoint = AsyncMock(return_value=datetime(2026, 1, 1, 0, 0, 0))
            result = await bus.startup_sync(event_handler=handler)
            return result

        result = asyncio.run(run())

        assert result == 1  # only first event succeeded
        assert call_count == 2  # tried 2, stopped after failure
        assert len(checkpoint_updates) == 1
        assert checkpoint_updates[0] == t1  # checkpoint at first event, NOT t3

    def test_first_event_fails_no_checkpoint_update(self):
        """When the very first event fails, checkpoint is NOT updated at all."""
        t1 = datetime(2026, 1, 1, 0, 0, 1)
        t2 = datetime(2026, 1, 1, 0, 0, 2)
        ops = [
            _make_operation("op-1", t1),
            _make_operation("op-2", t2),
        ]

        bus = _make_bus(ops)
        handler = AsyncMock(side_effect=ConnectionError("down"))
        checkpoint_updates: list[datetime] = []

        async def run():
            bus._update_checkpoint = AsyncMock(side_effect=lambda ts: checkpoint_updates.append(ts))
            bus._get_checkpoint = AsyncMock(return_value=datetime(2026, 1, 1, 0, 0, 0))
            result = await bus.startup_sync(event_handler=handler)
            return result

        result = asyncio.run(run())

        assert result == 0
        assert handler.call_count == 1  # tried first, failed, stopped
        assert len(checkpoint_updates) == 0  # NO checkpoint update

    def test_truncated_batch_logs_warning(self):
        """When batch hits max_sync_events, a warning is logged."""
        ops = [_make_operation(f"op-{i}", datetime(2026, 1, 1, 0, 0, i)) for i in range(1, 6)]

        bus = _make_bus(ops, max_sync_events=5)
        handler = AsyncMock()

        async def run():
            bus._update_checkpoint = AsyncMock()
            bus._get_checkpoint = AsyncMock(return_value=datetime(2026, 1, 1, 0, 0, 0))
            with patch("nexus.services.event_bus.base.logger") as mock_logger:
                await bus.startup_sync(event_handler=handler)
                # Check that warning about truncation was logged
                warning_calls = [
                    c for c in mock_logger.warning.call_args_list if "max_sync_events" in str(c)
                ]
                assert len(warning_calls) == 1

        asyncio.run(run())

    def test_no_handler_still_advances_checkpoint(self):
        """When no event_handler is provided, all events count as synced."""
        t1 = datetime(2026, 1, 1, 0, 0, 1)
        t2 = datetime(2026, 1, 1, 0, 0, 2)
        ops = [
            _make_operation("op-1", t1),
            _make_operation("op-2", t2),
        ]

        bus = _make_bus(ops)
        checkpoint_updates: list[datetime] = []

        async def run():
            bus._update_checkpoint = AsyncMock(side_effect=lambda ts: checkpoint_updates.append(ts))
            bus._get_checkpoint = AsyncMock(return_value=datetime(2026, 1, 1, 0, 0, 0))
            result = await bus.startup_sync(event_handler=None)
            return result

        result = asyncio.run(run())

        assert result == 2
        assert len(checkpoint_updates) == 1
        assert checkpoint_updates[0] == t2

    def test_failed_events_retried_on_next_startup(self):
        """Simulate two startups: first has a failure, second retries from checkpoint."""
        t1 = datetime(2026, 1, 1, 0, 0, 1)
        t2 = datetime(2026, 1, 1, 0, 0, 2)
        t3 = datetime(2026, 1, 1, 0, 0, 3)

        # First startup: event 2 fails
        ops_run1 = [
            _make_operation("op-1", t1),
            _make_operation("op-2", t2),
            _make_operation("op-3", t3),
        ]
        bus1 = _make_bus(ops_run1)
        saved_checkpoint = [None]

        call_count = 0

        async def handler_run1(event):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise ConnectionError("down")

        async def run1():
            bus1._get_checkpoint = AsyncMock(return_value=datetime(2026, 1, 1, 0, 0, 0))
            bus1._update_checkpoint = AsyncMock(
                side_effect=lambda ts: saved_checkpoint.__setitem__(0, ts)
            )
            return await bus1.startup_sync(event_handler=handler_run1)

        result1 = asyncio.run(run1())
        assert result1 == 1
        assert saved_checkpoint[0] == t1  # checkpoint at event 1

        # Second startup: all events succeed (simulating events after checkpoint)
        ops_run2 = [
            _make_operation("op-2", t2),
            _make_operation("op-3", t3),
        ]
        bus2 = _make_bus(ops_run2)
        handler_run2 = AsyncMock()

        async def run2():
            bus2._get_checkpoint = AsyncMock(return_value=saved_checkpoint[0])
            bus2._update_checkpoint = AsyncMock(
                side_effect=lambda ts: saved_checkpoint.__setitem__(0, ts)
            )
            return await bus2.startup_sync(event_handler=handler_run2)

        result2 = asyncio.run(run2())
        assert result2 == 2
        assert handler_run2.call_count == 2
        assert saved_checkpoint[0] == t3  # now advanced to last event
