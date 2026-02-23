"""Unit tests for EventDeliveryWorker — extended for Issue #1138/#1139.

Tests cover the new features added to the delivery worker:
- ExporterRegistry integration (parallel dispatch)
- DLQ routing after max_retries
- _run_async helper (fire-and-forget fix)
- Error classification and retry tracking
"""

import asyncio
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
# _run_async helper
# =========================================================================


class TestRunAsync:
    """Test the sync->async bridge helper."""

    def test_run_async_without_loop(self) -> None:
        """_run_async should create a temporary loop when none is running."""
        from nexus.services.event_subsystem.log.delivery import _run_async

        async def simple_coro():
            return 42

        result = _run_async(simple_coro())
        assert result == 42

    def test_run_async_with_loop(self) -> None:
        """_run_async should use run_coroutine_threadsafe with an existing loop."""
        from nexus.services.event_subsystem.log.delivery import _run_async

        loop = asyncio.new_event_loop()

        import threading

        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()

        try:

            async def simple_coro():
                return 99

            result = _run_async(simple_coro(), loop)
            assert result == 99
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=2.0)
            loop.close()


# =========================================================================
# ExporterRegistry integration
# =========================================================================


class TestExporterRegistryIntegration:
    """Test EventDeliveryWorker with ExporterRegistry wired in."""

    def test_dispatch_calls_exporter_registry(self, record_store: SQLAlchemyRecordStore) -> None:
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
        count = worker._poll_and_dispatch()

        assert count == 1
        mock_registry.dispatch_batch.assert_called_once()
        # Verify the batch contains one FileEvent
        call_args = mock_registry.dispatch_batch.call_args
        events = call_args[0][0]
        assert len(events) == 1
        assert isinstance(events[0], FileEvent)

    def test_exporter_failure_routes_to_dlq(self, record_store: SQLAlchemyRecordStore) -> None:
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
        worker._poll_and_dispatch()

        # DLQ entries should have been created
        assert worker.metrics["total_dlq"] == 1

    def test_no_exporter_registry_skips_export(self, record_store: SQLAlchemyRecordStore) -> None:
        from nexus.services.event_subsystem.log.delivery import EventDeliveryWorker

        _insert_undelivered(record_store.session_factory)

        worker = EventDeliveryWorker(record_store)
        count = worker._poll_and_dispatch()

        # Should still work without registry
        assert count == 1
        assert worker.metrics["total_dispatched"] == 1


# =========================================================================
# DLQ routing after max_retries
# =========================================================================


class TestDLQRouting:
    """Test DLQ routing after exhausting retries."""

    def test_routes_to_dlq_after_max_retries(self, record_store: SQLAlchemyRecordStore) -> None:
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
        worker._poll_and_dispatch()
        assert worker.metrics["total_dlq"] == 0

        # Second attempt: retry 2 -> DLQ
        worker._poll_and_dispatch()
        assert worker.metrics["total_dlq"] == 1

    def test_retry_count_clears_on_success(self, record_store: SQLAlchemyRecordStore) -> None:
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
        worker._poll_and_dispatch()
        assert worker.metrics["total_failed"] == 1

        # Second poll: succeed -> retry count should be cleared
        worker._poll_and_dispatch()
        assert worker.metrics["total_dispatched"] == 1
        # Internal retry counts should be empty
        assert len(worker._retry_counts) == 0
