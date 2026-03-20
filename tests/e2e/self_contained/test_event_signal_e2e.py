"""End-to-end test for event notification pipeline (Issue #3193).

Exercises the full signal path:
1. File write via WriteObserver → operation_log INSERT
2. PipedRecordStoreWriteObserver._flush_batch() → asyncio.Event signaled
3. EventDeliveryWorker wakes on signal → dispatches → marks delivered
4. EventReplayService.stream() wakes on signal → yields new events
5. Concurrent writes + reads with signal notification
6. Performance: verify µs wakeup vs previous 200ms polling

No external services required (SQLite, in-process).
"""

from __future__ import annotations

import asyncio
import tempfile
import time
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.contracts.metadata import FileMetadata
from nexus.services.event_subsystem.log.delivery import EventDeliveryWorker
from nexus.services.event_subsystem.log.replay import EventReplayService
from nexus.storage.models import OperationLogModel
from nexus.storage.piped_record_store_write_observer import PipedRecordStoreWriteObserver
from nexus.storage.record_store import SQLAlchemyRecordStore
from nexus.storage.record_store_write_observer import RecordStoreWriteObserver


@pytest.fixture
def temp_dir() -> Generator[Path, None, None]:
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def record_store(temp_dir: Path) -> Generator[SQLAlchemyRecordStore, None, None]:
    rs = SQLAlchemyRecordStore(db_path=temp_dir / "e2e_signal_test.db")
    yield rs
    rs.close()


def _make_metadata(
    path: str = "/test.txt",
    *,
    etag: str = "abc123",
    size: int = 100,
    version: int = 1,
) -> FileMetadata:
    return FileMetadata(
        path=path,
        backend_name="local",
        physical_path=etag,
        size=size,
        etag=etag,
        mime_type="text/plain",
        created_at=datetime.now(UTC),
        modified_at=datetime.now(UTC),
        version=version,
        zone_id="root",
        created_by="test_user",
        owner_id="user1",
    )


# =============================================================================
# Full pipeline: write → observer → signal → delivery worker → delivered
# =============================================================================


class TestFullSignalPipeline:
    """End-to-end: write → observer → asyncio.Event → delivery worker → delivered."""

    @pytest.mark.asyncio
    async def test_write_triggers_signal_and_delivery(
        self, record_store: SQLAlchemyRecordStore
    ) -> None:
        """Complete pipeline: sync write observer → DB → signal → async worker → dispatch."""
        signal = asyncio.Event()

        # 1. Set up sync write observer (writes directly to DB, not piped)
        syncer = RecordStoreWriteObserver(record_store)

        # 2. Set up delivery worker with signal
        mock_bus = MagicMock()
        mock_bus.publish = AsyncMock()
        delivered_events: list = []

        async def capture(event):
            delivered_events.append(event)

        mock_bus.publish = AsyncMock(side_effect=capture)

        worker = EventDeliveryWorker(
            record_store,
            event_bus=mock_bus,
            event_signal=signal,
        )
        await worker.start()

        try:
            # 3. Write file via observer (this creates undelivered row in operation_log)
            metadata = _make_metadata("/signal_test.txt", etag="sig1")
            syncer.on_write(metadata, is_new=True, path="/signal_test.txt", zone_id="root")

            # 4. Signal the worker (simulating what PipedRecordStoreWriteObserver does)
            signal.set()

            # 5. Wait for delivery
            deadline = asyncio.get_event_loop().time() + 5.0
            while asyncio.get_event_loop().time() < deadline:
                if delivered_events:
                    break
                await asyncio.sleep(0.02)

            # 6. Verify delivery
            assert len(delivered_events) == 1
            assert delivered_events[0].path == "/signal_test.txt"

            # 7. Verify marked as delivered in DB
            with record_store.session_factory() as session:
                ops = (
                    session.query(OperationLogModel)
                    .filter(OperationLogModel.path == "/signal_test.txt")
                    .all()
                )
                assert len(ops) == 1
                assert ops[0].delivered is True
        finally:
            await worker.stop()

    @pytest.mark.asyncio
    async def test_multiple_writes_all_delivered_via_signal(
        self, record_store: SQLAlchemyRecordStore
    ) -> None:
        """Multiple writes, one signal → all delivered in drain-then-wait."""
        signal = asyncio.Event()
        mock_bus = MagicMock()
        mock_bus.publish = AsyncMock()

        syncer = RecordStoreWriteObserver(record_store)
        worker = EventDeliveryWorker(
            record_store,
            event_bus=mock_bus,
            event_signal=signal,
            batch_size=10,
        )
        await worker.start()

        try:
            # Write 5 files
            for i in range(5):
                m = _make_metadata(f"/multi{i}.txt", etag=f"m{i}")
                syncer.on_write(m, is_new=True, path=f"/multi{i}.txt", zone_id="root")

            # Single signal
            signal.set()

            # Wait for all deliveries
            deadline = asyncio.get_event_loop().time() + 5.0
            while asyncio.get_event_loop().time() < deadline:
                if worker.metrics["total_dispatched"] >= 5:
                    break
                await asyncio.sleep(0.02)

            assert worker.metrics["total_dispatched"] == 5

            # All marked delivered
            with record_store.session_factory() as session:
                from sqlalchemy import func, select

                undelivered = session.execute(
                    select(func.count())
                    .select_from(OperationLogModel)
                    .where(OperationLogModel.delivered == False)  # noqa: E712
                ).scalar()
                assert undelivered == 0
        finally:
            await worker.stop()


# =============================================================================
# Full pipeline: write → observer → signal → replay stream → SSE yield
# =============================================================================


class TestSignalReplayStream:
    """End-to-end: write → signal → EventReplayService.stream() yields event."""

    @pytest.mark.asyncio
    async def test_stream_receives_events_via_signal(
        self, record_store: SQLAlchemyRecordStore
    ) -> None:
        """stream() yields new events immediately when signaled."""
        signal = asyncio.Event()
        syncer = RecordStoreWriteObserver(record_store)
        service = EventReplayService(record_store=record_store, event_signal=signal)

        received_events = []

        async def consume():
            async for event in service.stream(idle_timeout=3.0, poll_interval=10.0):
                received_events.append(event)
                if len(received_events) >= 2:
                    return

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.1)

        # Write first file + signal
        m1 = _make_metadata("/stream1.txt", etag="s1")
        syncer.on_write(m1, is_new=True, path="/stream1.txt", zone_id="root")
        signal.set()
        await asyncio.sleep(0.2)

        # Write second file + signal
        m2 = _make_metadata("/stream2.txt", etag="s2")
        syncer.on_write(m2, is_new=True, path="/stream2.txt", zone_id="root")
        signal.set()

        try:
            await asyncio.wait_for(task, timeout=5.0)
        except TimeoutError:
            task.cancel()
            pytest.fail(f"stream() only received {len(received_events)} events, expected 2")

        assert len(received_events) == 2
        paths = [e.path for e in received_events]
        assert "/stream1.txt" in paths
        assert "/stream2.txt" in paths


# =============================================================================
# Performance: µs wakeup latency
# =============================================================================


class TestSignalPerformance:
    """Verify notification-driven delivery is significantly faster than polling."""

    @pytest.mark.asyncio
    async def test_signal_wakeup_under_100ms(self, record_store: SQLAlchemyRecordStore) -> None:
        """Signal → delivery should complete in <100ms (vs 200ms+ polling baseline)."""
        signal = asyncio.Event()
        mock_bus = MagicMock()
        mock_bus.publish = AsyncMock()

        syncer = RecordStoreWriteObserver(record_store)
        worker = EventDeliveryWorker(
            record_store,
            event_bus=mock_bus,
            event_signal=signal,
        )
        await worker.start()

        try:
            # Write event
            m = _make_metadata("/perf.txt", etag="perf1")
            syncer.on_write(m, is_new=True, path="/perf.txt", zone_id="root")

            # Time from signal to delivery
            t0 = time.monotonic()
            signal.set()

            # Wait for delivery
            deadline = asyncio.get_event_loop().time() + 5.0
            while asyncio.get_event_loop().time() < deadline:
                if worker.metrics["total_dispatched"] >= 1:
                    break
                await asyncio.sleep(0.001)  # 1ms poll

            elapsed_ms = (time.monotonic() - t0) * 1000
            assert worker.metrics["total_dispatched"] == 1
            assert elapsed_ms < 100, f"Signal→delivery took {elapsed_ms:.1f}ms, expected <100ms"
        finally:
            await worker.stop()

    @pytest.mark.asyncio
    async def test_stream_signal_latency_under_100ms(
        self, record_store: SQLAlchemyRecordStore
    ) -> None:
        """stream() signal → yield should complete in <100ms."""
        signal = asyncio.Event()
        syncer = RecordStoreWriteObserver(record_store)
        service = EventReplayService(record_store=record_store, event_signal=signal)

        received_at: float | None = None

        async def consume():
            nonlocal received_at
            async for _ in service.stream(idle_timeout=5.0, poll_interval=10.0):
                received_at = time.monotonic()
                return

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.1)  # Let stream start

        # Write + signal
        m = _make_metadata("/stream_perf.txt", etag="sp1")
        syncer.on_write(m, is_new=True, path="/stream_perf.txt", zone_id="root")

        t0 = time.monotonic()
        signal.set()

        try:
            await asyncio.wait_for(task, timeout=5.0)
        except TimeoutError:
            task.cancel()
            pytest.fail("stream() did not yield event")

        assert received_at is not None
        elapsed_ms = (received_at - t0) * 1000
        assert elapsed_ms < 100, f"Signal→yield took {elapsed_ms:.1f}ms, expected <100ms"


# =============================================================================
# Concurrent writes + delivery (stress test)
# =============================================================================


class TestConcurrentSignalDelivery:
    """Stress test: concurrent writes with signal notification."""

    @pytest.mark.asyncio
    async def test_burst_writes_all_delivered(self, record_store: SQLAlchemyRecordStore) -> None:
        """50 rapid writes → all delivered via signal within timeout."""
        signal = asyncio.Event()
        mock_bus = MagicMock()
        mock_bus.publish = AsyncMock()

        syncer = RecordStoreWriteObserver(record_store)
        worker = EventDeliveryWorker(
            record_store,
            event_bus=mock_bus,
            event_signal=signal,
            batch_size=25,
        )
        await worker.start()

        total = 50
        try:
            for i in range(total):
                m = _make_metadata(f"/burst{i}.txt", etag=f"b{i}")
                syncer.on_write(m, is_new=True, path=f"/burst{i}.txt", zone_id="root")
                if i % 10 == 0:
                    signal.set()

            signal.set()  # Final signal

            # Wait for all deliveries
            deadline = asyncio.get_event_loop().time() + 10.0
            while asyncio.get_event_loop().time() < deadline:
                if worker.metrics["total_dispatched"] >= total:
                    break
                signal.set()
                await asyncio.sleep(0.05)

            assert worker.metrics["total_dispatched"] == total, (
                f"Only {worker.metrics['total_dispatched']}/{total} events delivered"
            )
        finally:
            await worker.stop()

    @pytest.mark.asyncio
    async def test_delivery_and_stream_concurrent(
        self, record_store: SQLAlchemyRecordStore
    ) -> None:
        """Delivery worker and stream() consume from same signal concurrently."""
        signal = asyncio.Event()
        mock_bus = MagicMock()
        mock_bus.publish = AsyncMock()

        syncer = RecordStoreWriteObserver(record_store)

        # Both consumers share the same signal
        worker = EventDeliveryWorker(
            record_store,
            event_bus=mock_bus,
            event_signal=signal,
        )
        service = EventReplayService(record_store=record_store, event_signal=signal)

        stream_events = []

        async def stream_consumer():
            async for event in service.stream(idle_timeout=3.0, poll_interval=10.0):
                stream_events.append(event)
                if len(stream_events) >= 3:
                    return

        await worker.start()
        stream_task = asyncio.create_task(stream_consumer())
        await asyncio.sleep(0.1)

        try:
            # Write 3 files + signal
            for i in range(3):
                m = _make_metadata(f"/concurrent{i}.txt", etag=f"c{i}")
                syncer.on_write(m, is_new=True, path=f"/concurrent{i}.txt", zone_id="root")
                signal.set()
                await asyncio.sleep(0.1)

            # Wait for stream to complete
            try:
                await asyncio.wait_for(stream_task, timeout=5.0)
            except TimeoutError:
                stream_task.cancel()

            # Both consumers should have received events
            assert worker.metrics["total_dispatched"] == 3
            assert len(stream_events) == 3
        finally:
            await worker.stop()


# =============================================================================
# Observer event_signal integration (piped path)
# =============================================================================


class TestObserverSignalIntegration:
    """Test that PipedRecordStoreWriteObserver signals after _flush_batch."""

    @pytest.mark.asyncio
    async def test_flush_batch_signals_event(self, record_store: SQLAlchemyRecordStore) -> None:
        """Observer._flush_batch() should signal after commit, not before."""
        signal = asyncio.Event()
        observer = PipedRecordStoreWriteObserver(record_store, event_signal=signal)

        # Flush a write event
        events = [
            {
                "op": "write",
                "path": "/observed.txt",
                "is_new": True,
                "zone_id": "root",
                "agent_id": None,
                "snapshot_hash": None,
                "metadata_snapshot": None,
                "metadata": {
                    "path": "/observed.txt",
                    "backend_name": "local",
                    "physical_path": "obs1",
                    "size": 50,
                    "etag": "obs1",
                    "mime_type": "text/plain",
                    "created_at": datetime.now(UTC).isoformat(),
                    "modified_at": datetime.now(UTC).isoformat(),
                    "version": 1,
                    "zone_id": "root",
                    "created_by": "test",
                    "owner_id": "test",
                },
            }
        ]

        assert not signal.is_set()
        await observer._flush_batch(events)
        assert signal.is_set()

        # Verify DB write happened too
        with record_store.session_factory() as session:
            ops = (
                session.query(OperationLogModel)
                .filter(OperationLogModel.path == "/observed.txt")
                .all()
            )
            assert len(ops) == 1
            assert ops[0].operation_type == "write"

    @pytest.mark.asyncio
    async def test_observer_to_worker_full_chain(self, record_store: SQLAlchemyRecordStore) -> None:
        """Full chain: observer._flush_batch() → signal → worker delivers."""
        signal = asyncio.Event()
        mock_bus = MagicMock()
        mock_bus.publish = AsyncMock()

        observer = PipedRecordStoreWriteObserver(record_store, event_signal=signal)
        worker = EventDeliveryWorker(
            record_store,
            event_bus=mock_bus,
            event_signal=signal,
        )
        await worker.start()

        try:
            # Flush via observer (this writes to DB AND signals)
            events = [
                {
                    "op": "write",
                    "path": "/chain.txt",
                    "is_new": True,
                    "zone_id": "root",
                    "agent_id": None,
                    "snapshot_hash": None,
                    "metadata_snapshot": None,
                    "metadata": {
                        "path": "/chain.txt",
                        "backend_name": "local",
                        "physical_path": "ch1",
                        "size": 25,
                        "etag": "ch1",
                        "mime_type": "text/plain",
                        "created_at": datetime.now(UTC).isoformat(),
                        "modified_at": datetime.now(UTC).isoformat(),
                        "version": 1,
                        "zone_id": "root",
                        "created_by": "test",
                        "owner_id": "test",
                    },
                }
            ]
            await observer._flush_batch(events)

            # Worker should wake and deliver
            deadline = asyncio.get_event_loop().time() + 5.0
            while asyncio.get_event_loop().time() < deadline:
                if worker.metrics["total_dispatched"] >= 1:
                    break
                await asyncio.sleep(0.02)

            assert worker.metrics["total_dispatched"] == 1

            # Verify delivered in DB
            with record_store.session_factory() as session:
                op = (
                    session.query(OperationLogModel)
                    .filter(OperationLogModel.path == "/chain.txt")
                    .first()
                )
                assert op is not None
                assert op.delivered is True
        finally:
            await worker.stop()
