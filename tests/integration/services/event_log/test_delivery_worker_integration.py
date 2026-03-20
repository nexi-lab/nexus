"""Integration tests for EventDeliveryWorker with real SQLite.

Tests the full poll -> dispatch -> mark cycle with a real database,
in-memory event bus mock, and ExporterRegistry.

Issue #1241, #1138, #3193.
"""

import asyncio
import tempfile
import uuid
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, PropertyMock

import pytest

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.storage.models import DeadLetterModel, OperationLogModel
from nexus.storage.record_store import SQLAlchemyRecordStore
from nexus.system_services.event_log.delivery import EventDeliveryWorker
from nexus.system_services.event_log.exporter_registry import ExporterRegistry


@pytest.fixture
def temp_dir() -> Generator[Path, None, None]:
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def record_store(temp_dir: Path) -> Generator[SQLAlchemyRecordStore, None, None]:
    rs = SQLAlchemyRecordStore(db_path=temp_dir / "integration_test.db")
    yield rs
    rs.close()


def _insert_undelivered(
    session_factory,
    path: str = "/test.txt",
    operation_type: str = "write",
    zone_id: str = ROOT_ZONE_ID,
    sequence_number: int | None = None,
) -> str:
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


class TestFullPollDispatchMarkCycle:
    """Integration test: poll -> dispatch -> mark delivered."""

    @pytest.mark.asyncio
    async def test_full_cycle_with_event_bus(self, record_store: SQLAlchemyRecordStore) -> None:
        op_id = _insert_undelivered(record_store.session_factory)

        mock_bus = MagicMock()
        mock_bus.publish = AsyncMock()

        worker = EventDeliveryWorker(record_store, event_bus=mock_bus)
        count = await worker._poll_and_dispatch()

        assert count == 1
        mock_bus.publish.assert_called_once()

        # Verify delivered
        with record_store.session_factory() as session:
            record = session.get(OperationLogModel, op_id)
            assert record.delivered is True

    @pytest.mark.asyncio
    async def test_full_cycle_with_exporter_registry(
        self, record_store: SQLAlchemyRecordStore
    ) -> None:
        _insert_undelivered(record_store.session_factory)

        mock_exporter = MagicMock()
        type(mock_exporter).name = PropertyMock(return_value="test-exporter")
        mock_exporter.publish_batch = AsyncMock(return_value=[])
        mock_exporter.close = AsyncMock()
        mock_exporter.health_check = AsyncMock(return_value=True)

        registry = ExporterRegistry()
        registry.register(mock_exporter)

        worker = EventDeliveryWorker(
            record_store,
            exporter_registry=registry,
        )
        count = await worker._poll_and_dispatch()

        assert count == 1
        mock_exporter.publish_batch.assert_called_once()

    @pytest.mark.asyncio
    async def test_concurrent_worker_safety_sqlite(
        self, record_store: SQLAlchemyRecordStore
    ) -> None:
        """Two workers should not process the same event (SQLite: no SKIP LOCKED)."""
        for i in range(10):
            _insert_undelivered(
                record_store.session_factory,
                path=f"/file{i}.txt",
                sequence_number=i + 1,
            )

        mock_bus = MagicMock()
        mock_bus.publish = AsyncMock()

        worker1 = EventDeliveryWorker(record_store, event_bus=mock_bus, batch_size=5)
        worker2 = EventDeliveryWorker(record_store, event_bus=mock_bus, batch_size=5)

        # Worker 1 takes first 5
        count1 = await worker1._poll_and_dispatch()
        # Worker 2 takes remaining 5
        count2 = await worker2._poll_and_dispatch()

        assert count1 + count2 == 10


class TestDLQIntegration:
    """Integration test: DLQ routing with real database."""

    @pytest.mark.asyncio
    async def test_dlq_entries_persisted(self, record_store: SQLAlchemyRecordStore) -> None:
        _insert_undelivered(record_store.session_factory)

        mock_bus = MagicMock()
        mock_bus.publish = AsyncMock(side_effect=ConnectionError("down"))

        worker = EventDeliveryWorker(
            record_store,
            event_bus=mock_bus,
            max_retries=1,
        )

        # First poll triggers DLQ after 1 retry
        await worker._poll_and_dispatch()

        # Verify DLQ entry exists
        with record_store.session_factory() as session:
            from sqlalchemy import select

            dlq_entries = list(session.execute(select(DeadLetterModel)).scalars())
            assert len(dlq_entries) == 1
            assert dlq_entries[0].exporter_name == "internal"
            assert dlq_entries[0].failure_type == "transient"  # ConnectionError
            assert dlq_entries[0].resolved_at is None


class TestWorkerLifecycleIntegration:
    """Integration test: start/stop with actual event processing (asyncio tasks)."""

    @pytest.mark.asyncio
    async def test_worker_processes_events_during_run(
        self, record_store: SQLAlchemyRecordStore
    ) -> None:
        op_id = _insert_undelivered(record_store.session_factory)

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

        # Signal the worker to wake up
        signal.set()

        # Wait for event to be processed
        delivered = False
        for _ in range(50):
            with record_store.session_factory() as session:
                record = session.get(OperationLogModel, op_id)
                if record and record.delivered:
                    delivered = True
                    break
            await asyncio.sleep(0.1)

        await worker.stop()
        assert delivered, "Worker did not deliver event within timeout"
