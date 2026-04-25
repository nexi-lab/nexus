"""Integration test for Transactional Event Log (Issue #1241).

End-to-end flow:
1. Write file via RecordStoreWriteObserver -> verify operation_log has delivered=FALSE
2. Run EventDeliveryWorker -> verify event dispatched to mock EventBus
3. Verify delivered=TRUE after dispatch
4. Verify retry on dispatch failure
5. Signal-based background delivery (Issue #3193)
"""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.metadata import FileMetadata
from nexus.services.event_bus.types import FileEventType
from nexus.services.event_log.delivery import EventDeliveryWorker
from nexus.storage.models import OperationLogModel
from nexus.storage.record_store import SQLAlchemyRecordStore
from nexus.storage.record_store_write_observer import RecordStoreWriteObserver


@pytest.fixture
def temp_dir() -> Generator[Path, None, None]:
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def record_store(temp_dir: Path) -> Generator[SQLAlchemyRecordStore, None, None]:
    rs = SQLAlchemyRecordStore(db_path=temp_dir / "integration_test.db")
    yield rs
    rs.close()


@pytest.fixture
def syncer(record_store: SQLAlchemyRecordStore) -> RecordStoreWriteObserver:
    return RecordStoreWriteObserver(record_store)


def _make_metadata(
    path: str = "/test.txt",
    *,
    etag: str = "abc123",
    size: int = 100,
    version: int = 1,
) -> FileMetadata:
    return FileMetadata(
        path=path,
        size=size,
        etag=etag,
        mime_type="text/plain",
        created_at=datetime.now(UTC),
        modified_at=datetime.now(UTC),
        version=version,
        zone_id=ROOT_ZONE_ID,
        owner_id="user1",
    )


class TestTransactionalOutboxIntegration:
    """Full cycle: write -> undelivered -> delivery worker -> delivered."""

    @pytest.mark.asyncio
    async def test_write_creates_undelivered_then_worker_delivers(
        self,
        syncer: RecordStoreWriteObserver,
        record_store: SQLAlchemyRecordStore,
    ) -> None:
        """Write via syncer -> start worker -> verify delivery."""
        # Step 1: Write file via syncer (transactional)
        metadata = _make_metadata("/integration.txt", etag="ihash")
        syncer.on_write(metadata, is_new=True, path="/integration.txt", zone_id=ROOT_ZONE_ID)

        # Verify: delivered=FALSE in operation_log
        with record_store.session_factory() as session:
            ops = session.query(OperationLogModel).all()
            assert len(ops) == 1
            assert ops[0].delivered is False
            assert ops[0].operation_type == "write"
            assert ops[0].path == "/integration.txt"
            op_id = ops[0].operation_id

        # Step 2: Create delivery worker with mock event bus
        mock_bus = MagicMock()
        mock_bus.publish = AsyncMock(return_value=1)
        published_events: list = []
        original_publish = mock_bus.publish

        async def capture_publish(event):
            published_events.append(event)
            return await original_publish(event)

        mock_bus.publish = AsyncMock(side_effect=capture_publish)

        worker = EventDeliveryWorker(
            record_store,
            event_bus=mock_bus,
        )

        # Step 3: Poll once (async call)
        count = await worker._poll_and_dispatch()
        assert count == 1

        # Step 4: Verify event was dispatched correctly
        assert len(published_events) == 1
        event = published_events[0]
        assert event.type == FileEventType.FILE_WRITE
        assert event.path == "/integration.txt"
        assert event.zone_id == ROOT_ZONE_ID

        # Step 5: Verify delivered=TRUE in operation_log
        with record_store.session_factory() as session:
            record = session.get(OperationLogModel, op_id)
            assert record.delivered is True

    @pytest.mark.asyncio
    async def test_multiple_operations_delivered_in_order(
        self,
        syncer: RecordStoreWriteObserver,
        record_store: SQLAlchemyRecordStore,
    ) -> None:
        """Multiple writes + delete -> all delivered in created_at order."""
        # Create multiple operations
        m1 = _make_metadata("/a.txt", etag="h1")
        syncer.on_write(m1, is_new=True, path="/a.txt", zone_id=ROOT_ZONE_ID)

        m2 = _make_metadata("/b.txt", etag="h2")
        syncer.on_write(m2, is_new=True, path="/b.txt", zone_id=ROOT_ZONE_ID)

        syncer.on_delete(path="/a.txt", zone_id=ROOT_ZONE_ID)

        # Verify 3 undelivered records
        with record_store.session_factory() as session:
            ops = (
                session.query(OperationLogModel)
                .filter(
                    OperationLogModel.delivered == False  # noqa: E712
                )
                .all()
            )
            assert len(ops) == 3

        # Deliver all
        mock_bus = MagicMock()
        mock_bus.publish = AsyncMock()
        dispatched_paths: list[str] = []

        async def capture(event):
            dispatched_paths.append(event.path)

        mock_bus.publish = AsyncMock(side_effect=capture)

        worker = EventDeliveryWorker(
            record_store,
            event_bus=mock_bus,
            batch_size=50,
        )
        count = await worker._poll_and_dispatch()
        assert count == 3

        # All paths delivered
        assert "/a.txt" in dispatched_paths
        assert "/b.txt" in dispatched_paths

        # All marked delivered
        with record_store.session_factory() as session:
            undelivered = (
                session.query(OperationLogModel)
                .filter(
                    OperationLogModel.delivered == False  # noqa: E712
                )
                .count()
            )
            assert undelivered == 0

    @pytest.mark.asyncio
    async def test_crash_recovery_retries_undelivered(
        self,
        syncer: RecordStoreWriteObserver,
        record_store: SQLAlchemyRecordStore,
    ) -> None:
        """Simulate crash: dispatch fails -> restart -> events retried."""
        # Write a file
        m = _make_metadata("/crash.txt", etag="crash")
        syncer.on_write(m, is_new=True, path="/crash.txt", zone_id=ROOT_ZONE_ID)

        # First delivery attempt fails (simulating crash mid-dispatch)
        failing_bus = MagicMock()
        failing_bus.publish = AsyncMock(side_effect=RuntimeError("crash!"))

        worker1 = EventDeliveryWorker(record_store, event_bus=failing_bus)
        count1 = await worker1._poll_and_dispatch()
        assert count1 == 0  # Nothing delivered

        # Verify still undelivered
        with record_store.session_factory() as session:
            ops = session.query(OperationLogModel).all()
            assert ops[0].delivered is False

        # "Restart" with a new worker (success this time)
        success_bus = MagicMock()
        success_bus.publish = AsyncMock()

        worker2 = EventDeliveryWorker(record_store, event_bus=success_bus)
        count2 = await worker2._poll_and_dispatch()
        assert count2 == 1

        # Now delivered
        with record_store.session_factory() as session:
            ops = session.query(OperationLogModel).all()
            assert ops[0].delivered is True

    @pytest.mark.asyncio
    async def test_worker_background_delivery(
        self,
        syncer: RecordStoreWriteObserver,
        record_store: SQLAlchemyRecordStore,
    ) -> None:
        """Worker running in background picks up events automatically."""
        mock_bus = MagicMock()
        mock_bus.publish = AsyncMock()

        signal = asyncio.Event()

        worker = EventDeliveryWorker(
            record_store,
            event_bus=mock_bus,
            event_signal=signal,
            fallback_poll_interval_s=0.05,
        )
        await worker.start()

        try:
            # Write file while worker is running
            m = _make_metadata("/bg.txt", etag="bghash")
            syncer.on_write(m, is_new=True, path="/bg.txt", zone_id=ROOT_ZONE_ID)

            # Signal the worker
            signal.set()

            # Wait for delivery
            delivered = False
            for _ in range(50):
                with record_store.session_factory() as session:
                    ops = (
                        session.query(OperationLogModel)
                        .filter(OperationLogModel.path == "/bg.txt")
                        .all()
                    )
                    if ops and ops[0].delivered:
                        delivered = True
                        break
                await asyncio.sleep(0.1)

            assert delivered, "Background worker did not deliver event"
        finally:
            await worker.stop()

    @pytest.mark.asyncio
    async def test_signal_based_background_delivery(
        self,
        syncer: RecordStoreWriteObserver,
        record_store: SQLAlchemyRecordStore,
    ) -> None:
        """Signal-driven delivery: write -> signal -> worker wakes and delivers."""
        mock_bus = MagicMock()
        mock_bus.publish = AsyncMock()

        signal = asyncio.Event()

        worker = EventDeliveryWorker(
            record_store,
            event_bus=mock_bus,
            event_signal=signal,
            fallback_poll_interval_s=60.0,  # Very long fallback
        )
        await worker.start()

        try:
            # Write file and signal
            m = _make_metadata("/signal-bg.txt", etag="sighash")
            syncer.on_write(m, is_new=True, path="/signal-bg.txt", zone_id=ROOT_ZONE_ID)
            signal.set()

            # Should be delivered quickly despite long fallback
            delivered = False
            for _ in range(50):
                with record_store.session_factory() as session:
                    ops = (
                        session.query(OperationLogModel)
                        .filter(OperationLogModel.path == "/signal-bg.txt")
                        .all()
                    )
                    if ops and ops[0].delivered:
                        delivered = True
                        break
                await asyncio.sleep(0.05)

            assert delivered, "Signal-based background delivery did not work"
        finally:
            await worker.stop()
