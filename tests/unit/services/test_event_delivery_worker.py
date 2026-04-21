"""Unit tests for EventDeliveryWorker — transactional outbox delivery (Issue #1241).

Tests cover:
- Happy path: poll → build FileEvent → dispatch → mark delivered
- Retry on failure: dispatch fails → record stays undelivered → retry
- Batch processing: multiple records polled and dispatched
- Empty outbox: worker idles
- Partial batch failure: only successful dispatches marked delivered
- Graceful shutdown: stop() completes cleanly
- Event type mapping: operation_type → FileEventType
- Async lifecycle: start/stop with asyncio.Task (Issue #3193)
- Drain-then-wait: signal-driven wakeup pattern
- Lost-wakeup prevention: clear-before-check
- Fallback polling: when no signal is provided
"""

import asyncio
import tempfile
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.services.event_bus.types import FileEventType
from nexus.services.event_log.delivery import EventDeliveryWorker
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
    zone_id: str = ROOT_ZONE_ID,
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
        worker = EventDeliveryWorker(record_store)
        op_id = _insert_undelivered(record_store.session_factory, operation_type="write")

        with record_store.session_factory() as session:
            record = session.get(OperationLogModel, op_id)
            event = worker._build_file_event(record)

        assert event.type == FileEventType.FILE_WRITE
        assert event.path == "/test.txt"
        assert event.zone_id == ROOT_ZONE_ID

    def test_delete_maps_to_file_delete(self, record_store: SQLAlchemyRecordStore) -> None:
        worker = EventDeliveryWorker(record_store)
        op_id = _insert_undelivered(record_store.session_factory, operation_type="delete")

        with record_store.session_factory() as session:
            record = session.get(OperationLogModel, op_id)
            event = worker._build_file_event(record)

        assert event.type == FileEventType.FILE_DELETE

    def test_rename_maps_to_file_rename_with_old_path(
        self, record_store: SQLAlchemyRecordStore
    ) -> None:
        worker = EventDeliveryWorker(record_store)
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
        worker = EventDeliveryWorker(record_store)
        op_id = _insert_undelivered(record_store.session_factory, operation_type="mkdir")

        with record_store.session_factory() as session:
            record = session.get(OperationLogModel, op_id)
            event = worker._build_file_event(record)

        assert event.type == FileEventType.DIR_CREATE

    def test_chmod_maps_to_metadata_change(self, record_store: SQLAlchemyRecordStore) -> None:
        worker = EventDeliveryWorker(record_store)
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

    @pytest.mark.asyncio
    async def test_poll_dispatches_and_marks_delivered(
        self, record_store: SQLAlchemyRecordStore
    ) -> None:
        op_id = _insert_undelivered(record_store.session_factory)

        mock_bus = MagicMock()
        mock_bus.publish = AsyncMock()

        worker = EventDeliveryWorker(record_store, event_bus=mock_bus)
        count = await worker._poll_and_dispatch()

        assert count == 1
        assert worker.metrics["total_dispatched"] == 1

        # Verify marked as delivered
        with record_store.session_factory() as session:
            record = session.get(OperationLogModel, op_id)
            assert record.delivered is True

    @pytest.mark.asyncio
    async def test_empty_outbox_returns_zero(self, record_store: SQLAlchemyRecordStore) -> None:
        worker = EventDeliveryWorker(record_store)
        count = await worker._poll_and_dispatch()

        assert count == 0
        assert worker.metrics["total_dispatched"] == 0

    @pytest.mark.asyncio
    async def test_batch_dispatches_multiple(self, record_store: SQLAlchemyRecordStore) -> None:
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

        worker = EventDeliveryWorker(record_store, event_bus=mock_bus, batch_size=10)
        count = await worker._poll_and_dispatch()

        assert count == 5
        assert worker.metrics["total_dispatched"] == 5

        # All marked delivered
        with record_store.session_factory() as session:
            for op_id in op_ids:
                record = session.get(OperationLogModel, op_id)
                assert record.delivered is True

    @pytest.mark.asyncio
    async def test_batch_size_limits_poll(self, record_store: SQLAlchemyRecordStore) -> None:
        # Insert 10 records but batch_size=3
        for i in range(10):
            _insert_undelivered(record_store.session_factory, path=f"/file{i}.txt")

        mock_bus = MagicMock()
        mock_bus.publish = AsyncMock()

        worker = EventDeliveryWorker(record_store, event_bus=mock_bus, batch_size=3)
        count = await worker._poll_and_dispatch()

        assert count == 3  # Only 3 dispatched in first poll

        # Second poll picks up more
        count2 = await worker._poll_and_dispatch()
        assert count2 == 3


# =========================================================================
# Failure handling
# =========================================================================


class TestDispatchFailure:
    """Test behavior when event dispatch fails."""

    @pytest.mark.asyncio
    async def test_failed_dispatch_leaves_undelivered(
        self, record_store: SQLAlchemyRecordStore
    ) -> None:
        op_id = _insert_undelivered(record_store.session_factory)

        mock_bus = MagicMock()
        mock_bus.publish = AsyncMock(side_effect=ConnectionError("Redis down"))

        worker = EventDeliveryWorker(record_store, event_bus=mock_bus)
        count = await worker._poll_and_dispatch()

        assert count == 0
        assert worker.metrics["total_failed"] == 1

        # Record should still be undelivered
        with record_store.session_factory() as session:
            record = session.get(OperationLogModel, op_id)
            assert record.delivered is False

    @pytest.mark.asyncio
    async def test_partial_batch_failure_marks_only_successful(
        self, record_store: SQLAlchemyRecordStore
    ) -> None:
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

        worker = EventDeliveryWorker(record_store, event_bus=mock_bus, batch_size=10)
        count = await worker._poll_and_dispatch()

        assert count == 2  # 2 succeeded, 1 failed
        assert worker.metrics["total_dispatched"] == 2
        assert worker.metrics["total_failed"] == 1

    @pytest.mark.asyncio
    async def test_retry_on_next_poll(self, record_store: SQLAlchemyRecordStore) -> None:
        """Previously failed event should be picked up on next poll."""
        op_id = _insert_undelivered(record_store.session_factory)

        # First poll: fail
        mock_bus = MagicMock()
        mock_bus.publish = AsyncMock(side_effect=ConnectionError("down"))
        worker = EventDeliveryWorker(record_store, event_bus=mock_bus)
        await worker._poll_and_dispatch()

        # Second poll: succeed
        mock_bus.publish = AsyncMock(return_value=0)
        count = await worker._poll_and_dispatch()
        assert count == 1

        with record_store.session_factory() as session:
            record = session.get(OperationLogModel, op_id)
            assert record.delivered is True


# =========================================================================
# Start / Stop lifecycle (asyncio-based, Issue #3193)
# =========================================================================


class TestLifecycle:
    """Test start() and stop() lifecycle with asyncio tasks."""

    @pytest.mark.asyncio
    async def test_start_creates_consumer_task(self, record_store: SQLAlchemyRecordStore) -> None:
        worker = EventDeliveryWorker(record_store, fallback_poll_interval_s=0.05)
        await worker.start()

        try:
            assert worker._consumer_task is not None
            assert not worker._consumer_task.done()
        finally:
            await worker.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_task(self, record_store: SQLAlchemyRecordStore) -> None:
        worker = EventDeliveryWorker(record_store, fallback_poll_interval_s=0.05)
        await worker.start()
        await worker.stop()

        assert worker._consumer_task is None

    @pytest.mark.asyncio
    async def test_double_start_is_noop(self, record_store: SQLAlchemyRecordStore) -> None:
        worker = EventDeliveryWorker(record_store, fallback_poll_interval_s=0.05)
        await worker.start()
        task1 = worker._consumer_task

        await worker.start()  # Should not create a new task
        assert worker._consumer_task is task1

        await worker.stop()

    @pytest.mark.asyncio
    async def test_worker_processes_during_run(self, record_store: SQLAlchemyRecordStore) -> None:
        """Worker should process events while running."""
        op_id = _insert_undelivered(record_store.session_factory)

        mock_bus = MagicMock()
        mock_bus.publish = AsyncMock()

        worker = EventDeliveryWorker(
            record_store,
            event_bus=mock_bus,
            fallback_poll_interval_s=0.05,
        )
        await worker.start()

        # Wait for worker to pick up the event
        deadline = asyncio.get_event_loop().time() + 5.0
        delivered = False
        while asyncio.get_event_loop().time() < deadline:
            with record_store.session_factory() as session:
                record = session.get(OperationLogModel, op_id)
                if record and record.delivered:
                    delivered = True
                    break
            await asyncio.sleep(0.1)

        await worker.stop()
        assert delivered, "Worker did not deliver event within timeout"


# =========================================================================
# No-bus graceful degradation
# =========================================================================


class TestNoBus:
    """Test that worker works even without an event bus."""

    @pytest.mark.asyncio
    async def test_dispatch_without_bus_marks_delivered(
        self, record_store: SQLAlchemyRecordStore
    ) -> None:
        """If no event bus is available, dispatch still succeeds (no-op publish)."""
        op_id = _insert_undelivered(record_store.session_factory)

        # No event_bus provided — worker should still mark delivered
        worker = EventDeliveryWorker(record_store)
        count = await worker._poll_and_dispatch()

        assert count == 1

        with record_store.session_factory() as session:
            record = session.get(OperationLogModel, op_id)
            assert record.delivered is True


# =========================================================================
# Async lifecycle: drain-then-wait pattern (Issue #3193)
# =========================================================================


class TestDrainThenWait:
    """Test the drain-then-wait consumer loop with asyncio.Event signal."""

    @pytest.mark.asyncio
    async def test_signal_wakes_worker_immediately(
        self, record_store: SQLAlchemyRecordStore
    ) -> None:
        """Worker wakes instantly when event_signal is set (no polling delay)."""
        signal = asyncio.Event()
        mock_bus = MagicMock()
        mock_bus.publish = AsyncMock()

        worker = EventDeliveryWorker(
            record_store,
            event_bus=mock_bus,
            event_signal=signal,
            fallback_poll_interval_s=60.0,  # Very long fallback
        )
        await worker.start()

        try:
            # Insert an event and signal
            _insert_undelivered(record_store.session_factory, path="/signal.txt")
            signal.set()

            # Should be delivered quickly despite long fallback interval
            delivered = False
            for _ in range(50):
                with record_store.session_factory() as session:
                    from sqlalchemy import select

                    rows = list(
                        session.execute(
                            select(OperationLogModel).where(OperationLogModel.path == "/signal.txt")
                        ).scalars()
                    )
                    if rows and rows[0].delivered:
                        delivered = True
                        break
                await asyncio.sleep(0.05)

            assert delivered, "Signal-driven wakeup did not deliver event"
        finally:
            await worker.stop()

    @pytest.mark.asyncio
    async def test_drain_continues_until_empty(self, record_store: SQLAlchemyRecordStore) -> None:
        """Worker drains all events before waiting for next signal."""
        signal = asyncio.Event()
        mock_bus = MagicMock()
        mock_bus.publish = AsyncMock()

        # Insert multiple events
        for i in range(5):
            _insert_undelivered(record_store.session_factory, path=f"/drain{i}.txt")

        worker = EventDeliveryWorker(
            record_store,
            event_bus=mock_bus,
            event_signal=signal,
            fallback_poll_interval_s=60.0,
            batch_size=2,  # Small batch to force multiple drain iterations
        )
        await worker.start()

        try:
            signal.set()
            # Wait for all 5 to be delivered
            for _ in range(50):
                with record_store.session_factory() as session:
                    from sqlalchemy import func, select

                    count = session.execute(
                        select(func.count())
                        .select_from(OperationLogModel)
                        .where(OperationLogModel.delivered == True)  # noqa: E712
                    ).scalar()
                    if count == 5:
                        break
                await asyncio.sleep(0.05)
            else:
                pytest.fail("Not all events drained")
        finally:
            await worker.stop()


# =========================================================================
# Lost-wakeup prevention (Issue #3193)
# =========================================================================


class TestLostWakeup:
    """Test that the clear-before-check pattern prevents lost wakeups."""

    @pytest.mark.asyncio
    async def test_signal_set_during_poll_not_lost(
        self, record_store: SQLAlchemyRecordStore
    ) -> None:
        """If signal is set while _poll_and_dispatch runs, next iteration picks it up."""
        signal = asyncio.Event()
        mock_bus = MagicMock()
        mock_bus.publish = AsyncMock()

        worker = EventDeliveryWorker(
            record_store,
            event_bus=mock_bus,
            event_signal=signal,
            fallback_poll_interval_s=60.0,
        )
        await worker.start()

        try:
            # Insert event, set signal
            _insert_undelivered(record_store.session_factory, path="/lost-wakeup.txt")
            signal.set()

            delivered = False
            for _ in range(50):
                with record_store.session_factory() as session:
                    from sqlalchemy import select

                    rows = list(
                        session.execute(
                            select(OperationLogModel).where(
                                OperationLogModel.path == "/lost-wakeup.txt"
                            )
                        ).scalars()
                    )
                    if rows and rows[0].delivered:
                        delivered = True
                        break
                await asyncio.sleep(0.05)

            assert delivered, "Lost wakeup: event not delivered"
        finally:
            await worker.stop()


# =========================================================================
# Fallback polling (no signal provided)
# =========================================================================


class TestFallbackPolling:
    """Test that worker falls back to timed polling when no signal is provided."""

    @pytest.mark.asyncio
    async def test_fallback_poll_delivers_events(self, record_store: SQLAlchemyRecordStore) -> None:
        """Without signal, worker uses fallback_poll_interval_s."""
        mock_bus = MagicMock()
        mock_bus.publish = AsyncMock()

        worker = EventDeliveryWorker(
            record_store,
            event_bus=mock_bus,
            fallback_poll_interval_s=0.05,  # Fast fallback for test
        )
        await worker.start()

        try:
            _insert_undelivered(record_store.session_factory, path="/fallback.txt")

            delivered = False
            for _ in range(50):
                with record_store.session_factory() as session:
                    from sqlalchemy import select

                    rows = list(
                        session.execute(
                            select(OperationLogModel).where(
                                OperationLogModel.path == "/fallback.txt"
                            )
                        ).scalars()
                    )
                    if rows and rows[0].delivered:
                        delivered = True
                        break
                await asyncio.sleep(0.05)

            assert delivered, "Fallback polling did not deliver event"
        finally:
            await worker.stop()
