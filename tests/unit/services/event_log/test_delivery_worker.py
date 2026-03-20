"""Unit tests for EventDeliveryWorker — async + notification-driven (Issue #3193).

Tests cover:
- ExporterRegistry integration (parallel dispatch)
- DLQ routing after max_retries
- Error classification and retry tracking
- Async dispatch (no _run_async bridge)
"""

import tempfile
import uuid
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.services.event_subsystem.types import FileEvent
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
    sequence_number: int | None = None,
) -> str:
    """Insert an undelivered operation_log row. Returns operation_id."""
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
            sequence_number=sequence_number,
        )
        session.add(record)
        session.commit()
    return op_id


# =========================================================================
# ExporterRegistry integration
# =========================================================================


class TestExporterRegistryIntegration:
    """Test EventDeliveryWorker with ExporterRegistry wired in."""

    @pytest.mark.asyncio
    async def test_dispatch_calls_exporter_registry(
        self, record_store: SQLAlchemyRecordStore
    ) -> None:
        from nexus.services.event_subsystem.log.delivery import EventDeliveryWorker
        from nexus.services.event_subsystem.log.exporter_registry import ExporterRegistry

        _insert_undelivered(record_store.session_factory)

        mock_registry = MagicMock(spec=ExporterRegistry)
        mock_registry.exporter_names = ["mock-exporter"]
        mock_registry.dispatch_batch = AsyncMock(return_value={})

        worker = EventDeliveryWorker(
            record_store,
            exporter_registry=mock_registry,
        )
        count = await worker._poll_and_dispatch()

        assert count == 1
        mock_registry.dispatch_batch.assert_called_once()
        # Verify the batch contains one FileEvent
        call_args = mock_registry.dispatch_batch.call_args
        events = call_args[0][0]
        assert len(events) == 1
        assert isinstance(events[0], FileEvent)

    @pytest.mark.asyncio
    async def test_exporter_failure_routes_to_dlq(
        self, record_store: SQLAlchemyRecordStore
    ) -> None:
        from nexus.services.event_subsystem.log.delivery import EventDeliveryWorker
        from nexus.services.event_subsystem.log.exporter_registry import ExporterRegistry

        _insert_undelivered(record_store.session_factory)

        # Mock registry returns failures for the exporter
        mock_registry = MagicMock(spec=ExporterRegistry)
        mock_registry.exporter_names = ["kafka"]

        async def mock_dispatch(events):
            return {"kafka": [e.event_id for e in events]}

        mock_registry.dispatch_batch = AsyncMock(side_effect=mock_dispatch)

        worker = EventDeliveryWorker(
            record_store,
            exporter_registry=mock_registry,
        )
        await worker._poll_and_dispatch()

        # DLQ entries should have been created
        assert worker.metrics["total_dlq"] == 1

    @pytest.mark.asyncio
    async def test_no_exporter_registry_skips_export(
        self, record_store: SQLAlchemyRecordStore
    ) -> None:
        from nexus.services.event_subsystem.log.delivery import EventDeliveryWorker

        _insert_undelivered(record_store.session_factory)

        worker = EventDeliveryWorker(record_store)
        count = await worker._poll_and_dispatch()

        # Should still work without registry
        assert count == 1
        assert worker.metrics["total_dispatched"] == 1


# =========================================================================
# DLQ routing after max_retries
# =========================================================================


class TestDLQRouting:
    """Test DLQ routing after exhausting retries."""

    @pytest.mark.asyncio
    async def test_routes_to_dlq_after_max_retries(
        self, record_store: SQLAlchemyRecordStore
    ) -> None:
        from nexus.services.event_subsystem.log.delivery import EventDeliveryWorker

        _insert_undelivered(record_store.session_factory)

        mock_bus = MagicMock()
        mock_bus.publish = AsyncMock(side_effect=ConnectionError("down"))

        worker = EventDeliveryWorker(
            record_store,
            event_bus=mock_bus,
            max_retries=2,
        )

        # First attempt: retry 1
        await worker._poll_and_dispatch()
        assert worker.metrics["total_dlq"] == 0

        # Second attempt: retry 2 -> DLQ
        await worker._poll_and_dispatch()
        assert worker.metrics["total_dlq"] == 1

    @pytest.mark.asyncio
    async def test_retry_count_clears_on_success(self, record_store: SQLAlchemyRecordStore) -> None:
        from nexus.services.event_subsystem.log.delivery import EventDeliveryWorker

        _insert_undelivered(record_store.session_factory)

        call_count = 0

        async def sometimes_fail(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("temporary")
            return 0

        mock_bus = MagicMock()
        mock_bus.publish = AsyncMock(side_effect=sometimes_fail)

        worker = EventDeliveryWorker(
            record_store,
            event_bus=mock_bus,
            max_retries=3,
        )

        # First poll: fail
        await worker._poll_and_dispatch()
        assert worker.metrics["total_failed"] == 1

        # Second poll: succeed -> retry count should be cleared
        await worker._poll_and_dispatch()
        assert worker.metrics["total_dispatched"] == 1
        # Internal retry counts should be empty
        assert len(worker._retry_counts) == 0
