"""Integration test for WriteBackService write -> notify pipeline (Issue #1129, #1130).

Tests the full flow: Nexus write event -> backlog enqueue -> backend write-back
-> change log update -> completion event, using an in-memory SQLite database
and mock backend. Includes conflict resolution integration tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from nexus.core.event_bus import FileEvent, FileEventType
from nexus.services.change_log_store import ChangeLogStore
from nexus.services.conflict_log_store import ConflictLogStore
from nexus.services.conflict_resolution import ConflictStrategy
from nexus.services.sync_backlog_store import SyncBacklogStore
from nexus.services.write_back_service import WriteBackService
from nexus.storage.models import Base

# =============================================================================
# Fixtures
# =============================================================================


@dataclass
class FakeFileInfo:
    """Minimal FileInfo for integration tests."""

    size: int = 1024
    mtime: datetime | None = None
    backend_version: str | None = None
    content_hash: str | None = None


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
        "conflict_strategy": None,
    }
    gw.list_mounts.return_value = [
        {
            "mount_point": "/mnt/gcs",
            "readonly": False,
            "backend_type": "GCSConnector",
            "backend": mock_backend,
            "conflict_strategy": None,
        }
    ]
    gw.metadata_get.return_value = MagicMock(mtime=datetime.now(UTC), content_hash="abc", size=1024)
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
            default_strategy=ConflictStrategy.KEEP_NEWER,
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


# =============================================================================
# Conflict Integration Tests (Issue #1130)
# =============================================================================


class TestConflictIntegration:
    """Conflict resolution integration tests with real SQLite stores."""

    @pytest.mark.asyncio
    async def test_keep_newer_nexus_wins_with_real_stores(self, mock_gateway, mock_event_bus):
        """KEEP_NEWER: Nexus newer -> write proceeds, conflict logged."""
        backlog_store = SyncBacklogStore(mock_gateway)
        change_log_store = ChangeLogStore(mock_gateway)
        conflict_log_store = ConflictLogStore(mock_gateway)

        service = WriteBackService(
            gateway=mock_gateway,
            event_bus=mock_event_bus,
            backlog_store=backlog_store,
            change_log_store=change_log_store,
            conflict_log_store=conflict_log_store,
            default_strategy=ConflictStrategy.KEEP_NEWER,
        )

        # Pre-populate change log (last synced state)
        change_log_store.upsert_change_log(
            path="/mnt/gcs/project/file.txt",
            backend_name="test_gcs",
            zone_id="default",
            content_hash="old_hash",
        )

        # Set up conflict: backend has a newer version
        backend = mock_gateway.get_mount_for_path.return_value["backend"]
        backend.get_file_info.return_value = FakeFileInfo(
            backend_version="v2",
            mtime=datetime.now(UTC) - timedelta(hours=1),
            size=500,
        )

        # Nexus is the newest
        mock_gateway.metadata_get.return_value = MagicMock(
            mtime=datetime.now(UTC),
            content_hash="nexus_new_hash",
            size=1024,
        )

        # Enqueue and process
        await service._on_file_event(
            FileEvent(
                type=FileEventType.FILE_WRITE,
                path="/mnt/gcs/project/file.txt",
                zone_id="default",
                etag="nexus_new_hash",
            )
        )
        await service._process_all_backends()

        # Backend write should happen (Nexus wins)
        backend.write_content.assert_called_once()

        # Conflict should be logged
        conflicts = conflict_log_store.list_conflicts()
        assert len(conflicts) == 1
        assert conflicts[0].outcome.value == "nexus_wins"

    @pytest.mark.asyncio
    async def test_keep_newer_backend_wins_with_real_stores(self, mock_gateway, mock_event_bus):
        """KEEP_NEWER: Backend newer -> write skipped, conflict logged."""
        backlog_store = SyncBacklogStore(mock_gateway)
        change_log_store = ChangeLogStore(mock_gateway)
        conflict_log_store = ConflictLogStore(mock_gateway)

        service = WriteBackService(
            gateway=mock_gateway,
            event_bus=mock_event_bus,
            backlog_store=backlog_store,
            change_log_store=change_log_store,
            conflict_log_store=conflict_log_store,
            default_strategy=ConflictStrategy.KEEP_NEWER,
        )

        # Pre-populate change log
        change_log_store.upsert_change_log(
            path="/mnt/gcs/project/file.txt",
            backend_name="test_gcs",
            zone_id="default",
            content_hash="old_hash",
        )

        # Set up conflict: backend is the newest
        backend = mock_gateway.get_mount_for_path.return_value["backend"]
        backend.get_file_info.return_value = FakeFileInfo(
            backend_version="v2",
            mtime=datetime.now(UTC),
            size=2048,
        )

        # Nexus is older
        mock_gateway.metadata_get.return_value = MagicMock(
            mtime=datetime.now(UTC) - timedelta(hours=1),
            content_hash="nexus_old_hash",
            size=1024,
        )

        # Enqueue and process
        await service._on_file_event(
            FileEvent(
                type=FileEventType.FILE_WRITE,
                path="/mnt/gcs/project/file.txt",
                zone_id="default",
                etag="nexus_old_hash",
            )
        )
        await service._process_all_backends()

        # Backend write should NOT happen (backend wins)
        backend.write_content.assert_not_called()

        # Conflict should be logged with backend_wins
        conflicts = conflict_log_store.list_conflicts()
        assert len(conflicts) == 1
        assert conflicts[0].outcome.value == "backend_wins"

    @pytest.mark.asyncio
    async def test_rename_conflict_creates_copy_with_real_stores(
        self, mock_gateway, mock_event_bus
    ):
        """RENAME_CONFLICT: creates copy and proceeds with real stores."""
        backlog_store = SyncBacklogStore(mock_gateway)
        change_log_store = ChangeLogStore(mock_gateway)
        conflict_log_store = ConflictLogStore(mock_gateway)

        service = WriteBackService(
            gateway=mock_gateway,
            event_bus=mock_event_bus,
            backlog_store=backlog_store,
            change_log_store=change_log_store,
            conflict_log_store=conflict_log_store,
            default_strategy=ConflictStrategy.RENAME_CONFLICT,
        )

        # Pre-populate change log
        change_log_store.upsert_change_log(
            path="/mnt/gcs/project/file.txt",
            backend_name="test_gcs",
            zone_id="default",
            content_hash="old_hash",
        )

        # Set up conflict
        backend = mock_gateway.get_mount_for_path.return_value["backend"]
        backend.get_file_info.return_value = FakeFileInfo(
            backend_version="v2",
            mtime=datetime.now(UTC) - timedelta(hours=1),
            size=500,
        )
        mock_gateway.metadata_get.return_value = MagicMock(
            mtime=datetime.now(UTC),
            content_hash="nexus_new_hash",
            size=1024,
        )

        # Enqueue and process
        await service._on_file_event(
            FileEvent(
                type=FileEventType.FILE_WRITE,
                path="/mnt/gcs/project/file.txt",
                zone_id="default",
                etag="nexus_new_hash",
            )
        )
        await service._process_all_backends()

        # Both conflict copy AND backend write should happen
        mock_gateway.write.assert_called_once()
        conflict_path = mock_gateway.write.call_args[0][0]
        assert ".sync-conflict-" in conflict_path

        backend.write_content.assert_called_once()

        # Conflict should be logged with rename_conflict outcome
        conflicts = conflict_log_store.list_conflicts()
        assert len(conflicts) == 1
        assert conflicts[0].outcome.value == "rename_conflict"
        assert conflicts[0].conflict_copy_path is not None

    @pytest.mark.asyncio
    async def test_conflict_lifecycle_detect_resolve_log_retrieve(
        self, mock_gateway, mock_event_bus
    ):
        """Full conflict lifecycle: enqueue → detect → auto-resolve → log → query → manual resolve."""
        from nexus.services.conflict_resolution import ConflictStatus, ResolutionOutcome

        backlog_store = SyncBacklogStore(mock_gateway)
        change_log_store = ChangeLogStore(mock_gateway)
        conflict_log_store = ConflictLogStore(mock_gateway)

        service = WriteBackService(
            gateway=mock_gateway,
            event_bus=mock_event_bus,
            backlog_store=backlog_store,
            change_log_store=change_log_store,
            conflict_log_store=conflict_log_store,
            default_strategy=ConflictStrategy.KEEP_NEWER,
        )

        # Pre-populate change log (creates a "last synced" state)
        change_log_store.upsert_change_log(
            path="/mnt/gcs/project/file.txt",
            backend_name="test_gcs",
            zone_id="default",
            content_hash="old_hash",
        )

        # Set up conflict: backend changed since last sync
        backend = mock_gateway.get_mount_for_path.return_value["backend"]
        backend.get_file_info.return_value = FakeFileInfo(
            backend_version="v2",
            mtime=datetime.now(UTC) - timedelta(hours=1),
            size=500,
        )
        mock_gateway.metadata_get.return_value = MagicMock(
            mtime=datetime.now(UTC),
            content_hash="nexus_newer",
            size=1024,
        )

        # Phase 1: Enqueue + process → conflict auto-resolved
        await service._on_file_event(
            FileEvent(
                type=FileEventType.FILE_WRITE,
                path="/mnt/gcs/project/file.txt",
                zone_id="default",
                etag="nexus_newer",
            )
        )
        await service._process_all_backends()

        # Phase 2: Verify conflict was logged with auto_resolved status
        conflicts = conflict_log_store.list_conflicts()
        assert len(conflicts) == 1
        record = conflicts[0]
        assert record.status == ConflictStatus.AUTO_RESOLVED
        assert record.outcome == ResolutionOutcome.NEXUS_WINS

        # Phase 3: Query by ID works
        fetched = conflict_log_store.get_conflict(record.id)
        assert fetched is not None
        assert fetched.id == record.id

        # Phase 4: Count matches
        total = conflict_log_store.count_conflicts()
        assert total == 1

        # Phase 5: Stats reflect the conflict
        stats = conflict_log_store.get_stats()
        assert stats["total"] >= 1


# =============================================================================
# Multi-Zone Integration Tests (#9A)
# =============================================================================


class TestMultiZoneIntegration:
    """Multi-zone write-back integration tests."""

    @pytest.mark.asyncio
    async def test_events_from_different_zones_process_independently(
        self, mock_gateway, mock_event_bus
    ):
        """Events from different zones create separate backlog entries."""
        backlog_store = SyncBacklogStore(mock_gateway)
        change_log_store = ChangeLogStore(mock_gateway)

        service = WriteBackService(
            gateway=mock_gateway,
            event_bus=mock_event_bus,
            backlog_store=backlog_store,
            change_log_store=change_log_store,
        )

        # Write events in two different zones
        for zone in ("us-east", "eu-west"):
            await service._on_file_event(
                FileEvent(
                    type=FileEventType.FILE_WRITE,
                    path="/mnt/gcs/project/file.txt",
                    zone_id=zone,
                    etag=f"hash-{zone}",
                )
            )

        # Each zone gets its own backlog entry
        us_entries = backlog_store.fetch_pending("test_gcs", "us-east")
        eu_entries = backlog_store.fetch_pending("test_gcs", "eu-west")
        assert len(us_entries) == 1
        assert len(eu_entries) == 1
        assert us_entries[0].content_hash == "hash-us-east"
        assert eu_entries[0].content_hash == "hash-eu-west"

        # _process_all_backends picks up both zones
        await service._process_all_backends()

        backend = mock_gateway.get_mount_for_path.return_value["backend"]
        assert backend.write_content.call_count == 2
