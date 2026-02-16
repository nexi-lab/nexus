"""E2E test for write-back with real LocalConnectorBackend (Issue #1129).

Tests the full round-trip:
1. Create NexusFS with SQLite
2. Mount a LocalConnectorBackend pointing to a temp dir
3. Write a file through NexusFS -> triggers event -> enqueues to backlog
4. Process pending backlog (simulating push)
5. Verify the file was written to the temp dir (real filesystem I/O)
6. Verify metrics
7. Test conflict scenario

Uses: in-process WriteBackService, real SQLite, real LocalConnectorBackend.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# Attempt SQLAlchemy import for in-memory DB
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from nexus.backends.local_connector import LocalConnectorBackend
from nexus.core.event_bus import FileEvent, FileEventType
from nexus.services.change_log_store import ChangeLogStore
from nexus.services.conflict_log_store import ConflictLogStore
from nexus.services.conflict_resolution import ConflictStrategy
from nexus.services.sync_backlog_store import SyncBacklogEntry, SyncBacklogStore
from nexus.services.write_back_service import WriteBackService
from nexus.storage.models import Base

# =============================================================================
# Helpers
# =============================================================================


def _make_entry(
    *,
    entry_id: str = "entry-1",
    path: str = "/mnt/local/test.txt",
    backend_name: str = "local_conn",
    zone_id: str = "default",
    operation_type: str = "write",
    content_hash: str | None = "abc123",
) -> SyncBacklogEntry:
    now = datetime.now(UTC)
    return SyncBacklogEntry(
        id=entry_id,
        path=path,
        backend_name=backend_name,
        zone_id=zone_id,
        operation_type=operation_type,
        content_hash=content_hash,
        new_path=None,
        status="pending",
        retry_count=0,
        max_retries=5,
        created_at=now,
        updated_at=now,
        last_attempted_at=None,
        error_message=None,
    )


def _async_iter_empty():
    """Return an async iterator that yields nothing."""

    async def _gen():
        return
        yield  # noqa: RET504 — makes this an async generator

    return _gen()


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
def backend_dir(tmp_path: Path) -> Path:
    """Dedicated temp dir for the LocalConnectorBackend."""
    d = tmp_path / "backend_root"
    d.mkdir()
    return d


@pytest.fixture
def local_backend(backend_dir: Path) -> LocalConnectorBackend:
    """Real LocalConnectorBackend pointing to backend_dir."""
    return LocalConnectorBackend(local_path=str(backend_dir))


@pytest.fixture
def mock_event_bus():
    """Async event bus mock that records published events."""
    bus = AsyncMock()
    published: list[FileEvent] = []

    async def capture_publish(event: FileEvent) -> int:
        published.append(event)
        return 1

    bus.publish = AsyncMock(side_effect=capture_publish)
    bus.subscribe = MagicMock(return_value=_async_iter_empty())
    bus._published = published
    return bus


@pytest.fixture
def gateway_with_local(local_backend):
    """Mock gateway wired to real LocalConnectorBackend."""
    gw = MagicMock()
    gw.get_mount_for_path.return_value = {
        "mount_point": "/mnt/local",
        "backend": local_backend,
        "backend_path": "test.txt",
        "readonly": False,
        "backend_name": "local_conn",
        "conflict_strategy": None,
        "zone_id": "default",
    }
    gw.list_mounts.return_value = [
        {
            "mount_point": "/mnt/local",
            "readonly": False,
            "backend_type": "local_connector",
            "backend": local_backend,
            "conflict_strategy": None,
        }
    ]
    gw.metadata_get.return_value = MagicMock(
        mtime=datetime.now(UTC), content_hash="abc123", size=13
    )
    gw.read.return_value = b"hello from nx"
    return gw


@pytest.fixture
def write_back_service(gateway_with_local, mock_event_bus, db_session_factory) -> WriteBackService:
    """WriteBackService wired to real local backend."""
    # Stores expect a gateway-like object with session_factory attribute
    gateway_with_local.session_factory = db_session_factory
    backlog = SyncBacklogStore(gateway_with_local)
    change_log = ChangeLogStore(gateway_with_local)
    conflict_log = ConflictLogStore(gateway_with_local)

    return WriteBackService(
        gateway=gateway_with_local,
        event_bus=mock_event_bus,
        backlog_store=backlog,
        change_log_store=change_log,
        conflict_log_store=conflict_log,
        default_strategy=ConflictStrategy.KEEP_NEWER,
    )


# =============================================================================
# Tests
# =============================================================================


class TestWriteBackE2ERoundTrip:
    """Full round-trip: NexusFS write -> backlog -> backend push -> verify file."""

    @pytest.mark.asyncio
    async def test_write_round_trip(self, write_back_service, backend_dir: Path) -> None:
        """Write through NexusFS, process pending, verify file on disk."""
        service = write_back_service

        # Enqueue a write entry
        entry = _make_entry(path="/mnt/local/test.txt", backend_name="local_conn")
        service._backlog_store.enqueue(
            path=entry.path,
            backend_name=entry.backend_name,
            zone_id=entry.zone_id,
            operation_type=entry.operation_type,
            content_hash=entry.content_hash,
        )

        # Process pending entries
        await service._process_pending("local_conn", "default")

        # Verify the file was written to the backend dir
        written_file = backend_dir / "test.txt"
        assert written_file.exists(), f"File not found at {written_file}"
        assert written_file.read_bytes() == b"hello from nx"

        # Verify metrics
        stats = service.get_stats()
        assert stats["metrics"]["changes_pushed"] >= 1
        assert stats["metrics"]["changes_failed"] == 0

    @pytest.mark.asyncio
    async def test_write_completion_event_published(
        self, write_back_service, mock_event_bus
    ) -> None:
        """Verify SYNC_TO_BACKEND_COMPLETED event is published on success."""
        service = write_back_service

        service._backlog_store.enqueue(
            path="/mnt/local/test.txt",
            backend_name="local_conn",
            zone_id="default",
            operation_type="write",
            content_hash="abc123",
        )
        await service._process_pending("local_conn", "default")

        event_types = [e.type for e in mock_event_bus._published]
        assert FileEventType.SYNC_TO_BACKEND_COMPLETED in event_types

    @pytest.mark.asyncio
    async def test_delete_round_trip(self, write_back_service, backend_dir: Path) -> None:
        """Write a file, then delete through write-back, verify removal."""
        service = write_back_service

        # Pre-create a file in the backend dir
        target = backend_dir / "test.txt"
        target.write_bytes(b"existing content")
        assert target.exists()

        # Enqueue a delete entry
        service._backlog_store.enqueue(
            path="/mnt/local/test.txt",
            backend_name="local_conn",
            zone_id="default",
            operation_type="delete",
            content_hash=None,
        )
        await service._process_pending("local_conn", "default")

        # Verify file was removed
        assert not target.exists(), "File should have been deleted"

    @pytest.mark.asyncio
    async def test_mkdir_round_trip(
        self, write_back_service, gateway_with_local, backend_dir: Path
    ) -> None:
        """Create directory through write-back."""
        service = write_back_service

        # Update gateway to point to new subdir
        gateway_with_local.get_mount_for_path.return_value = {
            "mount_point": "/mnt/local",
            "backend": gateway_with_local.get_mount_for_path.return_value["backend"],
            "backend_path": "subdir",
            "readonly": False,
            "backend_name": "local_conn",
            "conflict_strategy": None,
            "zone_id": "default",
        }

        service._backlog_store.enqueue(
            path="/mnt/local/subdir",
            backend_name="local_conn",
            zone_id="default",
            operation_type="mkdir",
            content_hash=None,
        )
        await service._process_pending("local_conn", "default")

        assert (backend_dir / "subdir").is_dir()

    @pytest.mark.asyncio
    async def test_metrics_record_failure(self, write_back_service, gateway_with_local) -> None:
        """Failed backend writes should increment failure counter."""
        service = write_back_service

        # Make gateway return None content to trigger error
        gateway_with_local.read.return_value = None
        gateway_with_local.metadata_get.return_value = MagicMock(
            mtime=datetime.now(UTC), content_hash=None, size=0
        )

        service._backlog_store.enqueue(
            path="/mnt/local/bad.txt",
            backend_name="local_conn",
            zone_id="default",
            operation_type="write",
            content_hash="hash",
        )
        await service._process_pending("local_conn", "default")

        stats = service.get_stats()
        assert stats["metrics"]["changes_failed"] >= 1


class TestWriteBackE2EConflict:
    """Conflict scenario: backend file modified externally."""

    @pytest.mark.asyncio
    async def test_conflict_detected_and_resolved(
        self, write_back_service, gateway_with_local, backend_dir: Path
    ) -> None:
        """Modify file in temp dir, write different content in Nexus, push.

        With KEEP_NEWER strategy and nexus mtime > backend mtime,
        Nexus wins and file is overwritten.
        """
        service = write_back_service

        # Write a file to the backend directory externally
        target = backend_dir / "test.txt"
        target.write_bytes(b"old backend content")

        # Set backend_file_info to simulate an existing file
        # (the backend has get_file_info via local_connector)
        # Gateway mtime is newer than backend, so nexus should win
        gateway_with_local.metadata_get.return_value = MagicMock(
            mtime=datetime.now(UTC),
            content_hash="nexus_hash",
            size=13,
        )
        gateway_with_local.read.return_value = b"nexus content"

        # Enqueue write entry
        service._backlog_store.enqueue(
            path="/mnt/local/test.txt",
            backend_name="local_conn",
            zone_id="default",
            operation_type="write",
            content_hash="nexus_hash",
        )

        # Create a change log entry to enable conflict detection
        service._change_log_store.upsert_change_log(
            path="/mnt/local/test.txt",
            backend_name="local_conn",
            zone_id="default",
            content_hash="old_hash",
        )

        await service._process_pending("local_conn", "default")

        # With KEEP_NEWER and nexus mtime > backend, nexus wins
        assert target.read_bytes() == b"nexus content"

        # Verify conflict was recorded in metrics
        stats = service.get_stats()
        assert stats["metrics"]["conflicts_detected"] >= 1
