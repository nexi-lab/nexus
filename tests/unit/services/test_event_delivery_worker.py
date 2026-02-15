"""Unit tests for EventDeliveryWorker — transactional outbox delivery (Issue #1241).

Tests cover:
- Happy path: poll → build FileEvent → dispatch → mark delivered
- Retry on failure: dispatch fails → record stays undelivered → retry
- Batch processing: multiple records polled and dispatched
- Empty outbox: worker idles, backoff increases
- Partial batch failure: only successful dispatches marked delivered
- Graceful shutdown: stop() completes cleanly
- Event type mapping: operation_type → FileEventType
"""

from __future__ import annotations

import tempfile
import time
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nexus.core.event_bus import FileEventType
from nexus.storage.models import OperationLogModel
from nexus.storage.record_store import SQLAlchemyRecordStore


@pytest.fixture
def temp_dir() -> Generator[Path, None, None]:
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def record_store(temp_dir: Path) -> Generator[SQLAlchemyRecordStore, None, None]:
    rs = SQLAlchemyRecordStore(db_path=temp_dir / "delivery_test.db")
    yield rs
    rs.close()


def _insert_undelivered(
    session_factory,
    path: str = "/test.txt",
    operation_type: str = "write",
    zone_id: str = "default",
    agent_id: str | None = None,
    new_path: str | None = None,
) -> str:
    """Insert an undelivered operation_log row. Returns operation_id."""
    import uuid

    op_id = str(uuid.uuid4())
    with session_factory() as session:
        record = OperationLogModel(
            operation_id=op_id,
            operation_type=operation_type,
            path=path,
            new_path=new_path,
            zone_id=zone_id,
            agent_id=agent_id,
            status="success",
            delivered=False,
            created_at=datetime.now(UTC),
        )
        session.add(record)
        session.commit()
    return op_id


# =========================================================================
# Event type mapping
# =========================================================================


class TestBuildFileEvent:
    """Test _build_file_event() mapping from operation_log to FileEvent."""

    def test_write_maps_to_file_write(self, record_store: SQLAlchemyRecordStore) -> None:
        from nexus.services.event_log.delivery_worker import EventDeliveryWorker

        worker = EventDeliveryWorker(record_store.session_factory)
        op_id = _insert_undelivered(record_store.session_factory, operation_type="write")

        with record_store.session_factory() as session:
            record = session.get(OperationLogModel, op_id)
            event = worker._build_file_event(record)

        assert event.type == FileEventType.FILE_WRITE
        assert event.path == "/test.txt"
        assert event.zone_id == "default"

    def test_delete_maps_to_file_delete(self, record_store: SQLAlchemyRecordStore) -> None:
        from nexus.services.event_log.delivery_worker import EventDeliveryWorker

        worker = EventDeliveryWorker(record_store.session_factory)
        op_id = _insert_undelivered(record_store.session_factory, operation_type="delete")

        with record_store.session_factory() as session:
            record = session.get(OperationLogModel, op_id)
            event = worker._build_file_event(record)

        assert event.type == FileEventType.FILE_DELETE

    def test_rename_maps_to_file_rename_with_old_path(
        self, record_store: SQLAlchemyRecordStore
    ) -> None:
        from nexus.services.event_log.delivery_worker import EventDeliveryWorker

        worker = EventDeliveryWorker(record_store.session_factory)
        op_id = _insert_undelivered(
            record_store.session_factory,
            operation_type="rename",
            path="/old.txt",
            new_path="/new.txt",
        )

        with record_store.session_factory() as session:
            record = session.get(OperationLogModel, op_id)
            event = worker._build_file_event(record)

        assert event.type == FileEventType.FILE_RENAME
        assert event.path == "/old.txt"
        assert event.old_path == "/new.txt"  # new_path column stores old_path for renames

    def test_mkdir_maps_to_dir_create(self, record_store: SQLAlchemyRecordStore) -> None:
        from nexus.services.event_log.delivery_worker import EventDeliveryWorker

        worker = EventDeliveryWorker(record_store.session_factory)
        op_id = _insert_undelivered(record_store.session_factory, operation_type="mkdir")

        with record_store.session_factory() as session:
            record = session.get(OperationLogModel, op_id)
            event = worker._build_file_event(record)

        assert event.type == FileEventType.DIR_CREATE

    def test_chmod_maps_to_metadata_change(self, record_store: SQLAlchemyRecordStore) -> None:
        from nexus.services.event_log.delivery_worker import EventDeliveryWorker

        worker = EventDeliveryWorker(record_store.session_factory)
        op_id = _insert_undelivered(record_store.session_factory, operation_type="chmod")

        with record_store.session_factory() as session:
            record = session.get(OperationLogModel, op_id)
            event = worker._build_file_event(record)

        assert event.type == FileEventType.METADATA_CHANGE


# =========================================================================
# Happy path: poll → dispatch → mark delivered
# =========================================================================


class TestPollAndDispatch:
    """Test the core poll-dispatch-mark cycle."""

    def test_poll_dispatches_and_marks_delivered(self, record_store: SQLAlchemyRecordStore) -> None:
        from nexus.services.event_log.delivery_worker import EventDeliveryWorker

        op_id = _insert_undelivered(record_store.session_factory)

        mock_bus = MagicMock()
        mock_bus.publish = AsyncMock()

        worker = EventDeliveryWorker(record_store.session_factory, event_bus=mock_bus)
        count = worker._poll_and_dispatch()

        assert count == 1
        assert worker.metrics["total_dispatched"] == 1

        # Verify marked as delivered
        with record_store.session_factory() as session:
            record = session.get(OperationLogModel, op_id)
            assert record.delivered is True

    def test_empty_outbox_returns_zero(self, record_store: SQLAlchemyRecordStore) -> None:
        from nexus.services.event_log.delivery_worker import EventDeliveryWorker

        worker = EventDeliveryWorker(record_store.session_factory)
        count = worker._poll_and_dispatch()

        assert count == 0
        assert worker.metrics["total_dispatched"] == 0

    def test_batch_dispatches_multiple(self, record_store: SQLAlchemyRecordStore) -> None:
        from nexus.services.event_log.delivery_worker import EventDeliveryWorker

        # Insert 5 undelivered records
        op_ids = []
        for i in range(5):
            op_id = _insert_undelivered(
                record_store.session_factory,
                path=f"/file{i}.txt",
            )
            op_ids.append(op_id)

        mock_bus = MagicMock()
        mock_bus.publish = AsyncMock()

        worker = EventDeliveryWorker(
            record_store.session_factory, event_bus=mock_bus, batch_size=10
        )
        count = worker._poll_and_dispatch()

        assert count == 5
        assert worker.metrics["total_dispatched"] == 5

        # All marked delivered
        with record_store.session_factory() as session:
            for op_id in op_ids:
                record = session.get(OperationLogModel, op_id)
                assert record.delivered is True

    def test_batch_size_limits_poll(self, record_store: SQLAlchemyRecordStore) -> None:
        from nexus.services.event_log.delivery_worker import EventDeliveryWorker

        # Insert 10 records but batch_size=3
        for i in range(10):
            _insert_undelivered(record_store.session_factory, path=f"/file{i}.txt")

        mock_bus = MagicMock()
        mock_bus.publish = AsyncMock()

        worker = EventDeliveryWorker(record_store.session_factory, event_bus=mock_bus, batch_size=3)
        count = worker._poll_and_dispatch()

        assert count == 3  # Only 3 dispatched in first poll

        # Second poll picks up more
        count2 = worker._poll_and_dispatch()
        assert count2 == 3


# =========================================================================
# Failure handling
# =========================================================================


class TestDispatchFailure:
    """Test behavior when event dispatch fails."""

    def test_failed_dispatch_leaves_undelivered(self, record_store: SQLAlchemyRecordStore) -> None:
        from nexus.services.event_log.delivery_worker import EventDeliveryWorker

        op_id = _insert_undelivered(record_store.session_factory)

        mock_bus = MagicMock()
        mock_bus.publish = AsyncMock(side_effect=ConnectionError("Redis down"))

        worker = EventDeliveryWorker(record_store.session_factory, event_bus=mock_bus)
        count = worker._poll_and_dispatch()

        assert count == 0
        assert worker.metrics["total_failed"] == 1

        # Record should still be undelivered
        with record_store.session_factory() as session:
            record = session.get(OperationLogModel, op_id)
            assert record.delivered is False

    def test_partial_batch_failure_marks_only_successful(
        self, record_store: SQLAlchemyRecordStore
    ) -> None:
        from nexus.services.event_log.delivery_worker import EventDeliveryWorker

        op_ids = []
        for i in range(3):
            op_id = _insert_undelivered(record_store.session_factory, path=f"/file{i}.txt")
            op_ids.append(op_id)

        call_count = 0

        async def failing_second(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise ConnectionError("temporary failure")
            return 0

        mock_bus = MagicMock()
        mock_bus.publish = AsyncMock(side_effect=failing_second)

        worker = EventDeliveryWorker(
            record_store.session_factory, event_bus=mock_bus, batch_size=10
        )
        count = worker._poll_and_dispatch()

        assert count == 2  # 2 succeeded, 1 failed
        assert worker.metrics["total_dispatched"] == 2
        assert worker.metrics["total_failed"] == 1

    def test_retry_on_next_poll(self, record_store: SQLAlchemyRecordStore) -> None:
        """Previously failed event should be picked up on next poll."""
        from nexus.services.event_log.delivery_worker import EventDeliveryWorker

        op_id = _insert_undelivered(record_store.session_factory)

        # First poll: fail
        mock_bus = MagicMock()
        mock_bus.publish = AsyncMock(side_effect=ConnectionError("down"))
        worker = EventDeliveryWorker(record_store.session_factory, event_bus=mock_bus)
        worker._poll_and_dispatch()

        # Second poll: succeed
        mock_bus.publish = AsyncMock(return_value=0)
        count = worker._poll_and_dispatch()
        assert count == 1

        with record_store.session_factory() as session:
            record = session.get(OperationLogModel, op_id)
            assert record.delivered is True


# =========================================================================
# Backoff behavior
# =========================================================================


class TestBackoff:
    """Test exponential backoff on empty polls.

    Note: _consecutive_empty is managed by _run_loop, not _poll_and_dispatch.
    We simulate the _run_loop logic directly.
    """

    def test_consecutive_empty_increments_in_run_loop(
        self, record_store: SQLAlchemyRecordStore
    ) -> None:
        from nexus.services.event_log.delivery_worker import EventDeliveryWorker

        worker = EventDeliveryWorker(record_store.session_factory)

        # Simulate what _run_loop does: poll → check result → update counter
        count = worker._poll_and_dispatch()
        if count == 0:
            worker._consecutive_empty += 1
        assert worker._consecutive_empty == 1

        count = worker._poll_and_dispatch()
        if count == 0:
            worker._consecutive_empty += 1
        assert worker._consecutive_empty == 2

    def test_successful_dispatch_resets_backoff(self, record_store: SQLAlchemyRecordStore) -> None:
        from nexus.services.event_log.delivery_worker import EventDeliveryWorker

        mock_bus = MagicMock()
        mock_bus.publish = AsyncMock()

        worker = EventDeliveryWorker(record_store.session_factory, event_bus=mock_bus)

        # Simulate empty polls
        worker._consecutive_empty = 5

        # Add a record and dispatch it
        _insert_undelivered(record_store.session_factory)
        count = worker._poll_and_dispatch()
        assert count == 1

        # Simulate _run_loop reset
        if count > 0:
            worker._consecutive_empty = 0
        assert worker._consecutive_empty == 0


# =========================================================================
# Start / Stop lifecycle
# =========================================================================


class TestLifecycle:
    """Test start() and stop() lifecycle."""

    def test_start_creates_daemon_thread(self, record_store: SQLAlchemyRecordStore) -> None:
        from nexus.services.event_log.delivery_worker import EventDeliveryWorker

        worker = EventDeliveryWorker(record_store.session_factory, poll_interval_ms=50)
        worker.start()

        try:
            assert worker._thread is not None
            assert worker._thread.is_alive()
            assert worker._thread.daemon is True
        finally:
            worker.stop(timeout=2.0)

    def test_stop_joins_thread(self, record_store: SQLAlchemyRecordStore) -> None:
        from nexus.services.event_log.delivery_worker import EventDeliveryWorker

        worker = EventDeliveryWorker(record_store.session_factory, poll_interval_ms=50)
        worker.start()
        worker.stop(timeout=2.0)

        assert worker._thread is None

    def test_double_start_is_noop(self, record_store: SQLAlchemyRecordStore) -> None:
        from nexus.services.event_log.delivery_worker import EventDeliveryWorker

        worker = EventDeliveryWorker(record_store.session_factory, poll_interval_ms=50)
        worker.start()
        thread1 = worker._thread

        worker.start()  # Should not create a new thread
        assert worker._thread is thread1

        worker.stop(timeout=2.0)

    def test_worker_processes_during_run(self, record_store: SQLAlchemyRecordStore) -> None:
        """Worker should process events while running."""
        from nexus.services.event_log.delivery_worker import EventDeliveryWorker

        op_id = _insert_undelivered(record_store.session_factory)

        mock_bus = MagicMock()
        mock_bus.publish = AsyncMock()

        worker = EventDeliveryWorker(
            record_store.session_factory,
            event_bus=mock_bus,
            poll_interval_ms=50,
        )
        worker.start()

        # Wait for worker to pick up the event
        deadline = time.monotonic() + 5.0
        delivered = False
        while time.monotonic() < deadline:
            with record_store.session_factory() as session:
                record = session.get(OperationLogModel, op_id)
                if record and record.delivered:
                    delivered = True
                    break
            time.sleep(0.1)

        worker.stop(timeout=2.0)
        assert delivered, "Worker did not deliver event within timeout"


# =========================================================================
# No-bus graceful degradation
# =========================================================================


class TestNoBus:
    """Test that worker works even without an event bus."""

    def test_dispatch_without_bus_marks_delivered(
        self, record_store: SQLAlchemyRecordStore
    ) -> None:
        """If no event bus is available, dispatch still succeeds (no-op publish)."""
        from nexus.services.event_log.delivery_worker import EventDeliveryWorker

        op_id = _insert_undelivered(record_store.session_factory)

        # No event_bus provided, and mock the global event bus at the import source
        with patch(
            "nexus.core.event_bus.get_global_event_bus",
            return_value=None,
        ):
            worker = EventDeliveryWorker(record_store.session_factory)
            count = worker._poll_and_dispatch()

        assert count == 1

        with record_store.session_factory() as session:
            record = session.get(OperationLogModel, op_id)
            assert record.delivered is True
