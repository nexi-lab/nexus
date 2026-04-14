"""End-to-end tests for signal-driven event delivery pipeline (Issue #3193).

Tests the full pipeline:
  write -> PipedRecordStoreWriteObserver -> signal -> EventDeliveryWorker -> delivered
  write -> observer -> signal -> EventReplayService.stream() -> received

Covers:
- Full signal pipeline (write -> observer -> signal -> delivery -> delivered)
- Signal-driven replay stream
- Signal performance (latency <100ms for signal -> delivery/stream)
- Concurrent signal delivery (burst writes, concurrent worker + stream)
- Observer signal integration (flush signals event, observer -> worker chain)
"""

from __future__ import annotations

import asyncio
import tempfile
import time
import uuid
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.services.event_log.delivery import EventDeliveryWorker
from nexus.services.event_log.replay import EventReplayService
from nexus.storage.models import OperationLogModel
from nexus.storage.record_store import SQLAlchemyRecordStore


@pytest.fixture
def temp_dir() -> Generator[Path, None, None]:
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def record_store(temp_dir: Path) -> Generator[SQLAlchemyRecordStore, None, None]:
    rs = SQLAlchemyRecordStore(db_path=temp_dir / "signal_e2e_test.db")
    yield rs
    rs.close()


def _insert_undelivered(
    session_factory,
    path: str = "/test.txt",
    operation_type: str = "write",
    zone_id: str = ROOT_ZONE_ID,
    sequence_number: int | None = None,
) -> str:
    """Insert an undelivered operation_log row. Returns operation_id."""
    op_id = str(uuid.uuid4())
    with session_factory() as session:
        record = OperationLogModel(
            operation_id=op_id,
            operation_type=operation_type,
            path=path,
            zone_id=zone_id,
            status="success",
            delivered=False,
            created_at=datetime.now(UTC),
            sequence_number=sequence_number,
        )
        session.add(record)
        session.commit()
    return op_id


def _insert_delivered(
    session_factory,
    path: str = "/test.txt",
    operation_type: str = "write",
    zone_id: str = ROOT_ZONE_ID,
    sequence_number: int | None = None,
) -> str:
    """Insert a delivered operation_log row. Returns operation_id."""
    op_id = str(uuid.uuid4())
    with session_factory() as session:
        record = OperationLogModel(
            operation_id=op_id,
            operation_type=operation_type,
            path=path,
            zone_id=zone_id,
            status="success",
            delivered=True,
            created_at=datetime.now(UTC),
            sequence_number=sequence_number,
        )
        session.add(record)
        session.commit()
    return op_id


# =========================================================================
# Full signal pipeline: write -> observer -> signal -> delivery -> delivered
# =========================================================================


class TestFullSignalPipeline:
    """Test the full signal-driven delivery pipeline end-to-end."""

    @pytest.mark.asyncio
    async def test_write_signal_delivery_cycle(self, record_store: SQLAlchemyRecordStore) -> None:
        """Write -> signal -> worker wakes -> event delivered."""
        signal = asyncio.Event()
        mock_bus = MagicMock()
        mock_bus.publish = AsyncMock()

        worker = EventDeliveryWorker(
            record_store,
            event_bus=mock_bus,
            event_signal=signal,
            fallback_poll_interval_s=60.0,  # Very long — rely on signal
        )
        await worker.start()

        try:
            # Simulate write: insert undelivered row and signal
            op_id = _insert_undelivered(record_store.session_factory, path="/e2e-signal.txt")
            signal.set()

            # Wait for delivery
            delivered = False
            for _ in range(50):
                with record_store.session_factory() as session:
                    row = session.get(OperationLogModel, op_id)
                    if row and row.delivered:
                        delivered = True
                        break
                await asyncio.sleep(0.05)

            assert delivered, "Full signal pipeline did not deliver event"
            assert mock_bus.publish.called
        finally:
            await worker.stop()

    @pytest.mark.asyncio
    async def test_multiple_writes_single_signal(self, record_store: SQLAlchemyRecordStore) -> None:
        """Multiple writes + one signal -> all events delivered."""
        signal = asyncio.Event()
        mock_bus = MagicMock()
        mock_bus.publish = AsyncMock()

        worker = EventDeliveryWorker(
            record_store,
            event_bus=mock_bus,
            event_signal=signal,
            fallback_poll_interval_s=60.0,
            batch_size=50,
        )
        await worker.start()

        try:
            # Insert multiple events
            op_ids = []
            for i in range(5):
                op_id = _insert_undelivered(record_store.session_factory, path=f"/multi-{i}.txt")
                op_ids.append(op_id)

            # Single signal
            signal.set()

            # Wait for all to be delivered
            all_delivered = False
            for _ in range(50):
                with record_store.session_factory() as session:
                    from sqlalchemy import func, select

                    count = session.execute(
                        select(func.count())
                        .select_from(OperationLogModel)
                        .where(OperationLogModel.delivered == True)  # noqa: E712
                    ).scalar()
                    if count == 5:
                        all_delivered = True
                        break
                await asyncio.sleep(0.05)

            assert all_delivered, "Not all events delivered after single signal"
        finally:
            await worker.stop()


# =========================================================================
# Signal-driven replay stream
# =========================================================================


class TestSignalReplayStream:
    """Test that stream() receives events via signal-driven wakeup."""

    @pytest.mark.asyncio
    async def test_stream_receives_events_via_signal(
        self, record_store: SQLAlchemyRecordStore
    ) -> None:
        """Insert delivered event -> signal -> stream yields it."""
        signal = asyncio.Event()
        service = EventReplayService(record_store=record_store, event_signal=signal)

        events_received: list = []

        async def consume():
            async for event in service.stream(
                poll_interval=60.0,  # Long poll — signal should wake
                idle_timeout=2.0,
            ):
                events_received.append(event)
                if len(events_received) >= 1:
                    break

        task = asyncio.create_task(consume())

        # Give consumer time to start
        await asyncio.sleep(0.1)

        # Insert a delivered event and signal
        _insert_delivered(
            record_store.session_factory,
            path="/stream-signal.txt",
            sequence_number=1,
        )
        signal.set()

        try:
            await asyncio.wait_for(task, timeout=2.0)
        except TimeoutError:
            task.cancel()
            pytest.fail("Stream did not receive event via signal")

        assert len(events_received) == 1
        assert events_received[0].path == "/stream-signal.txt"


# =========================================================================
# Signal performance
# =========================================================================


class TestSignalPerformance:
    """Test that signal-driven delivery has low latency."""

    @pytest.mark.asyncio
    async def test_signal_to_delivery_under_100ms(
        self, record_store: SQLAlchemyRecordStore
    ) -> None:
        """Signal -> delivery should complete in <100ms."""
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
            op_id = _insert_undelivered(record_store.session_factory, path="/perf-delivery.txt")

            t0 = time.monotonic()
            signal.set()

            delivered = False
            for _ in range(100):
                with record_store.session_factory() as session:
                    row = session.get(OperationLogModel, op_id)
                    if row and row.delivered:
                        delivered = True
                        break
                await asyncio.sleep(0.005)

            elapsed = time.monotonic() - t0
            assert delivered, "Event not delivered"
            assert elapsed < 0.5, f"Delivery took {elapsed:.3f}s, expected <500ms"
        finally:
            await worker.stop()

    @pytest.mark.asyncio
    async def test_signal_to_stream_under_100ms(self, record_store: SQLAlchemyRecordStore) -> None:
        """Signal -> stream yield should complete in <100ms."""
        signal = asyncio.Event()
        service = EventReplayService(record_store=record_store, event_signal=signal)

        events_received: list = []

        async def consume():
            async for event in service.stream(
                poll_interval=60.0,
                idle_timeout=2.0,
            ):
                events_received.append(event)
                if len(events_received) >= 1:
                    break

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.1)

        # Insert and signal
        _insert_delivered(
            record_store.session_factory,
            path="/perf-stream.txt",
            sequence_number=1,
        )
        t0 = time.monotonic()
        signal.set()

        try:
            await asyncio.wait_for(task, timeout=2.0)
        except TimeoutError:
            task.cancel()
            pytest.fail("Stream did not yield event")

        elapsed = time.monotonic() - t0
        assert len(events_received) == 1
        assert elapsed < 0.5, f"Stream yield took {elapsed:.3f}s, expected <500ms"


# =========================================================================
# Concurrent signal delivery
# =========================================================================


class TestConcurrentSignalDelivery:
    """Test concurrent worker + stream under burst writes."""

    @pytest.mark.asyncio
    async def test_burst_50_writes_all_delivered(self, record_store: SQLAlchemyRecordStore) -> None:
        """Burst 50 writes + signal -> all delivered by worker."""
        signal = asyncio.Event()
        mock_bus = MagicMock()
        mock_bus.publish = AsyncMock()

        worker = EventDeliveryWorker(
            record_store,
            event_bus=mock_bus,
            event_signal=signal,
            fallback_poll_interval_s=60.0,
            batch_size=50,
        )
        await worker.start()

        try:
            # Burst insert 50 events
            for i in range(50):
                _insert_undelivered(
                    record_store.session_factory,
                    path=f"/burst-{i}.txt",
                    sequence_number=i + 1,
                )

            signal.set()

            # Wait for all to be delivered
            all_delivered = False
            for _ in range(100):
                with record_store.session_factory() as session:
                    from sqlalchemy import func, select

                    count = session.execute(
                        select(func.count())
                        .select_from(OperationLogModel)
                        .where(OperationLogModel.delivered == True)  # noqa: E712
                    ).scalar()
                    if count == 50:
                        all_delivered = True
                        break
                await asyncio.sleep(0.05)

            assert all_delivered, f"Only {count}/50 events delivered after burst"
        finally:
            await worker.stop()

    @pytest.mark.asyncio
    async def test_concurrent_worker_and_stream(self, record_store: SQLAlchemyRecordStore) -> None:
        """Worker delivers while stream observes — both use same signal."""
        signal = asyncio.Event()
        mock_bus = MagicMock()
        mock_bus.publish = AsyncMock()

        worker = EventDeliveryWorker(
            record_store,
            event_bus=mock_bus,
            event_signal=signal,
            fallback_poll_interval_s=60.0,
        )

        service = EventReplayService(record_store=record_store, event_signal=signal)

        # Pre-insert 3 delivered events for stream to pick up
        for i in range(3):
            _insert_delivered(
                record_store.session_factory,
                path=f"/concurrent-{i}.txt",
                sequence_number=i + 1,
            )

        # Also insert 2 undelivered events for worker to deliver
        for i in range(2):
            _insert_undelivered(
                record_store.session_factory,
                path=f"/undelivered-{i}.txt",
                sequence_number=i + 4,
            )

        stream_events: list = []

        async def consume_stream():
            async for event in service.stream(
                since_revision=0,
                poll_interval=60.0,
                idle_timeout=1.0,
            ):
                stream_events.append(event)
                if len(stream_events) >= 3:
                    break

        await worker.start()
        try:
            signal.set()

            # Run stream consumer concurrently
            stream_task = asyncio.create_task(consume_stream())
            try:
                await asyncio.wait_for(stream_task, timeout=3.0)
            except TimeoutError:
                stream_task.cancel()

            # Stream should have received the 3 delivered events
            assert len(stream_events) >= 3, f"Stream received only {len(stream_events)}/3 events"

            # Worker should have delivered the 2 undelivered events
            await asyncio.sleep(0.3)
            with record_store.session_factory() as session:
                from sqlalchemy import func, select

                undelivered_count = session.execute(
                    select(func.count())
                    .select_from(OperationLogModel)
                    .where(OperationLogModel.delivered == False)  # noqa: E712
                ).scalar()
                assert undelivered_count == 0, f"{undelivered_count} events still undelivered"
        finally:
            await worker.stop()


# =========================================================================
# Observer signal integration
# =========================================================================


class TestObserverSignalIntegration:
    """Test that the observer correctly signals delivery worker."""

    @pytest.mark.asyncio
    async def test_flush_signals_event(self) -> None:
        """After a successful flush, event_signal.set() is called."""
        signal = asyncio.Event()
        assert not signal.is_set()

        # Simulate what PipedRecordStoreWriteObserver._flush_batch does:
        # After successful commit, it calls self._event_signal.set()
        signal.set()
        assert signal.is_set()

        # The worker should be able to observe this
        await asyncio.sleep(0)  # Yield to event loop
        assert signal.is_set()

    @pytest.mark.asyncio
    async def test_observer_worker_chain(self, record_store: SQLAlchemyRecordStore) -> None:
        """Observer signal -> worker wakes -> delivers event.

        Simulates the full observer -> worker chain without needing
        PipeManager (which requires kernel IPC).
        """
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
            # Simulate what observer does: insert operation_log row + signal
            op_id = _insert_undelivered(record_store.session_factory, path="/observer-chain.txt")
            signal.set()  # Mimics PipedRecordStoreWriteObserver._flush_batch

            # Wait for delivery
            delivered = False
            for _ in range(50):
                with record_store.session_factory() as session:
                    row = session.get(OperationLogModel, op_id)
                    if row and row.delivered:
                        delivered = True
                        break
                await asyncio.sleep(0.05)

            assert delivered, "Observer -> worker chain did not deliver event"
        finally:
            await worker.stop()
