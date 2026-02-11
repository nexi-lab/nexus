"""Integration test for WriteBackService write -> notify pipeline (Issue #1129).

Tests the full flow: Nexus write event -> backlog enqueue -> backend write-back
-> change log update -> completion event, using an in-memory SQLite database
and mock backend.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from nexus.core.event_bus import FileEvent, FileEventType
from nexus.services.change_log_store import ChangeLogStore
from nexus.services.sync_backlog_store import SyncBacklogStore
from nexus.services.write_back_service import WriteBackService
from nexus.storage.models import Base

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def db_session_factory():
    """In-memory SQLite with all tables."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.fixture
def mock_gateway(db_session_factory):
    """Gateway with real session factory and mock file ops."""
    gw = MagicMock()
    gw.session_factory = db_session_factory

    # Mock backend that records write_content calls
    mock_backend = MagicMock()
    mock_backend.name = "test_gcs"
    write_response = MagicMock()
    write_response.success = True
    write_response.data = "new_content_hash"
    mock_backend.write_content.return_value = write_response

    gw.get_mount_for_path.return_value = {
        "mount_point": "/mnt/gcs",
        "backend": mock_backend,
        "backend_path": "project/file.txt",
        "readonly": False,
        "backend_name": "test_gcs",
    }
    gw.list_mounts.return_value = [
        {
            "mount_point": "/mnt/gcs",
            "readonly": False,
            "backend_type": "GCSConnector",
            "backend": mock_backend,
        }
    ]
    gw.metadata_get.return_value = MagicMock(mtime=datetime.now(UTC), content_hash="abc")
    gw.read.return_value = b"hello world"
    return gw


@pytest.fixture
def mock_event_bus():
    """In-memory event bus that tracks published events."""
    bus = AsyncMock()
    published: list[FileEvent] = []

    async def capture_publish(event: FileEvent) -> int:
        published.append(event)
        return 1

    bus.publish = AsyncMock(side_effect=capture_publish)
    bus.subscribe = MagicMock(return_value=_empty_async_iter())
    bus._published = published
    return bus


async def _empty_async_iter():
    return
    yield  # pragma: no cover


# =============================================================================
# Integration Test
# =============================================================================


class TestWriteBackIntegration:
    """Full pipeline integration test."""

    @pytest.mark.asyncio
    async def test_nexus_write_triggers_backend_write_back(
        self, mock_gateway, mock_event_bus, db_session_factory
    ):
        """Full flow: event -> enqueue -> process -> backend write -> change log update."""
        # Setup stores with real SQLite
        backlog_store = SyncBacklogStore(mock_gateway)
        change_log_store = ChangeLogStore(mock_gateway)

        service = WriteBackService(
            gateway=mock_gateway,
            event_bus=mock_event_bus,
            backlog_store=backlog_store,
            change_log_store=change_log_store,
            conflict_policy="lww",
            max_concurrent_per_backend=5,
            poll_interval_seconds=60,  # Won't actually poll in this test
        )

        # Step 1: Simulate a FILE_WRITE event
        event = FileEvent(
            type=FileEventType.FILE_WRITE,
            path="/mnt/gcs/project/file.txt",
            zone_id="default",
            etag="abc123",
        )
        await service._on_file_event(event)

        # Step 2: Verify backlog entry was created
        entries = backlog_store.fetch_pending("test_gcs", "default")
        assert len(entries) == 1
        assert entries[0].path == "/mnt/gcs/project/file.txt"
        assert entries[0].operation_type == "write"

        # Step 3: Process pending entries (simulates poll loop)
        await service._process_all_backends()

        # Step 4: Verify backend.write_content was called
        backend = mock_gateway.get_mount_for_path.return_value["backend"]
        backend.write_content.assert_called_once_with(b"hello world")

        # Step 5: Verify change log was updated
        change_log = change_log_store.get_change_log(
            "/mnt/gcs/project/file.txt", "test_gcs", "default"
        )
        assert change_log is not None
        assert change_log.content_hash == "new_content_hash"

        # Step 6: Verify SYNC_TO_BACKEND_COMPLETED event was published
        completed_events = [
            e
            for e in mock_event_bus._published
            if e.type == FileEventType.SYNC_TO_BACKEND_COMPLETED
        ]
        assert len(completed_events) == 1
        assert completed_events[0].path == "/mnt/gcs/project/file.txt"

        # Step 7: Verify backlog entry is now completed (no more pending)
        remaining = backlog_store.fetch_pending("test_gcs", "default")
        assert len(remaining) == 0

    @pytest.mark.asyncio
    async def test_multiple_writes_coalesce_in_backlog(self, mock_gateway, mock_event_bus):
        """Multiple writes to same path coalesce into single backlog entry."""
        backlog_store = SyncBacklogStore(mock_gateway)
        change_log_store = ChangeLogStore(mock_gateway)

        service = WriteBackService(
            gateway=mock_gateway,
            event_bus=mock_event_bus,
            backlog_store=backlog_store,
            change_log_store=change_log_store,
        )

        # Three rapid writes to same path
        for etag in ("v1", "v2", "v3"):
            event = FileEvent(
                type=FileEventType.FILE_WRITE,
                path="/mnt/gcs/project/file.txt",
                zone_id="default",
                etag=etag,
            )
            await service._on_file_event(event)

        # Only 1 pending entry (coalesced)
        entries = backlog_store.fetch_pending("test_gcs", "default")
        assert len(entries) == 1
        assert entries[0].content_hash == "v3"  # Latest wins
