"""Unit tests for WriteBackService (Issue #1129).

Tests event handling, backlog processing, conflict resolution,
and rate limiting using mocked dependencies.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.core.event_bus import FileEvent, FileEventType
from nexus.services.sync_backlog_store import SyncBacklogEntry
from nexus.services.write_back_service import WriteBackService

# =============================================================================
# Fixtures
# =============================================================================


def _make_entry(
    *,
    path: str = "/mnt/gcs/file.txt",
    backend_name: str = "gcs",
    zone_id: str = "default",
    operation_type: str = "write",
    content_hash: str | None = "abc123",
    status: str = "pending",
    retry_count: int = 0,
) -> SyncBacklogEntry:
    """Create a test SyncBacklogEntry."""
    now = datetime.now(UTC)
    return SyncBacklogEntry(
        id="entry-1",
        path=path,
        backend_name=backend_name,
        zone_id=zone_id,
        operation_type=operation_type,
        content_hash=content_hash,
        new_path=None,
        status=status,
        retry_count=retry_count,
        max_retries=5,
        created_at=now,
        updated_at=now,
        last_attempted_at=None,
        error_message=None,
    )


@dataclass
class FakeFileInfo:
    """Minimal FileInfo for tests."""

    size: int = 1024
    mtime: datetime | None = None
    backend_version: str | None = None
    content_hash: str | None = None


@pytest.fixture
def mock_gateway():
    """Mock gateway with writable mount."""
    gw = MagicMock()
    gw.get_mount_for_path.return_value = {
        "mount_point": "/mnt/gcs",
        "backend": MagicMock(),
        "backend_path": "file.txt",
        "readonly": False,
        "backend_name": "gcs",
    }
    gw.list_mounts.return_value = [
        {
            "mount_point": "/mnt/gcs",
            "readonly": False,
            "backend_type": "GCSConnector",
            "backend": MagicMock(name="gcs"),
        }
    ]
    gw.metadata_get.return_value = MagicMock(mtime=datetime.now(UTC), content_hash="abc")
    gw.read.return_value = b"test content"
    return gw


@pytest.fixture
def mock_event_bus():
    """Mock event bus."""
    bus = AsyncMock()
    bus.publish = AsyncMock(return_value=1)
    # Create an async generator that yields nothing (no events)
    bus.subscribe = MagicMock(return_value=_empty_async_iter())
    return bus


async def _empty_async_iter():
    """Empty async iterator."""
    return
    yield  # pragma: no cover


@pytest.fixture
def mock_backlog_store():
    """Mock backlog store."""
    store = MagicMock()
    store.enqueue.return_value = True
    store.fetch_pending.return_value = []
    store.mark_in_progress.return_value = True
    store.mark_completed.return_value = True
    store.mark_failed.return_value = True
    store.get_stats.return_value = {"pending": 0}
    return store


@pytest.fixture
def mock_change_log_store():
    """Mock change log store."""
    store = MagicMock()
    store.get_change_log.return_value = None
    store.upsert_change_log.return_value = True
    return store


@pytest.fixture
def service(mock_gateway, mock_event_bus, mock_backlog_store, mock_change_log_store):
    """Create WriteBackService with all mocks."""
    return WriteBackService(
        gateway=mock_gateway,
        event_bus=mock_event_bus,
        backlog_store=mock_backlog_store,
        change_log_store=mock_change_log_store,
        conflict_policy="lww",
        max_concurrent_per_backend=2,
        poll_interval_seconds=0.1,
    )


# =============================================================================
# Event Handler Tests
# =============================================================================


class TestOnFileEvent:
    """Tests for _on_file_event."""

    @pytest.mark.asyncio
    async def test_event_handler_enqueues_write_for_mounted_path(self, service, mock_backlog_store):
        """FILE_WRITE event on mounted path -> enqueue."""
        event = FileEvent(
            type=FileEventType.FILE_WRITE,
            path="/mnt/gcs/file.txt",
            zone_id="default",
            etag="abc",
        )
        await service._on_file_event(event)

        mock_backlog_store.enqueue.assert_called_once_with(
            path="/mnt/gcs/file.txt",
            backend_name="gcs",
            zone_id="default",
            operation_type="write",
            content_hash="abc",
            new_path=None,
        )

    @pytest.mark.asyncio
    async def test_event_handler_ignores_non_mounted_path(
        self, service, mock_gateway, mock_backlog_store
    ):
        """Events for unmounted paths are ignored."""
        mock_gateway.get_mount_for_path.return_value = None

        event = FileEvent(
            type=FileEventType.FILE_WRITE,
            path="/unknown/file.txt",
        )
        await service._on_file_event(event)

        mock_backlog_store.enqueue.assert_not_called()

    @pytest.mark.asyncio
    async def test_write_back_skips_readonly_mounts(
        self, service, mock_gateway, mock_backlog_store
    ):
        """Readonly mounts are skipped."""
        mock_gateway.get_mount_for_path.return_value = {
            "mount_point": "/mnt/ro",
            "backend": MagicMock(),
            "backend_path": "file.txt",
            "readonly": True,
            "backend_name": "ro_backend",
        }

        event = FileEvent(
            type=FileEventType.FILE_WRITE,
            path="/mnt/ro/file.txt",
        )
        await service._on_file_event(event)

        mock_backlog_store.enqueue.assert_not_called()

    @pytest.mark.asyncio
    async def test_event_handler_ignores_metadata_change(self, service, mock_backlog_store):
        """METADATA_CHANGE events are not write-back triggers."""
        event = FileEvent(
            type=FileEventType.METADATA_CHANGE,
            path="/mnt/gcs/file.txt",
        )
        await service._on_file_event(event)

        mock_backlog_store.enqueue.assert_not_called()


# =============================================================================
# Write-Back Processing Tests
# =============================================================================


class TestWriteBackProcessing:
    """Tests for _process_entry and _write_back_single."""

    @pytest.mark.asyncio
    async def test_write_back_success_marks_completed(
        self, service, mock_backlog_store, mock_event_bus
    ):
        """Successful write-back marks entry as completed."""
        entry = _make_entry()
        sem = asyncio.Semaphore(1)

        await service._process_entry(entry, sem)

        mock_backlog_store.mark_in_progress.assert_called_once_with("entry-1")
        mock_backlog_store.mark_completed.assert_called_once_with("entry-1")
        # Should publish SYNC_TO_BACKEND_COMPLETED
        assert mock_event_bus.publish.call_count >= 1

    @pytest.mark.asyncio
    async def test_write_back_failure_marks_failed_increments_retry(
        self, service, mock_gateway, mock_backlog_store, mock_event_bus
    ):
        """Failed write-back marks entry as failed with error message."""
        entry = _make_entry()
        sem = asyncio.Semaphore(1)

        # Make backend write fail
        backend = mock_gateway.get_mount_for_path.return_value["backend"]
        backend.write_content.side_effect = RuntimeError("Connection reset")

        await service._process_entry(entry, sem)

        mock_backlog_store.mark_failed.assert_called_once()
        # Should publish SYNC_TO_BACKEND_FAILED
        assert any(
            call.args[0].type == FileEventType.SYNC_TO_BACKEND_FAILED
            for call in mock_event_bus.publish.call_args_list
        )

    @pytest.mark.asyncio
    async def test_write_back_conflict_detected_lww_nexus_wins(
        self, service, mock_gateway, mock_change_log_store, mock_event_bus
    ):
        """When conflict detected and Nexus is newer, write-back proceeds."""
        from nexus.services.change_log_store import ChangeLogEntry

        entry = _make_entry()
        sem = asyncio.Semaphore(1)

        # Set up conflict scenario: both sides changed
        old_time = datetime.now(UTC) - timedelta(hours=2)
        mock_change_log_store.get_change_log.return_value = ChangeLogEntry(
            path="/mnt/gcs/file.txt",
            backend_name="gcs",
            content_hash="old_hash",
            mtime=old_time,
            backend_version="v1",
        )

        backend = mock_gateway.get_mount_for_path.return_value["backend"]
        backend.get_file_info.return_value = FakeFileInfo(
            backend_version="v2",
            mtime=datetime.now(UTC) - timedelta(hours=1),
        )

        # Nexus is newest (gateway.metadata_get returns newer mtime)
        mock_gateway.metadata_get.return_value = MagicMock(
            mtime=datetime.now(UTC),
            content_hash="new_nexus_hash",
        )

        await service._process_entry(entry, sem)

        # Should still complete (Nexus wins)
        backend.write_content.assert_called_once()

    @pytest.mark.asyncio
    async def test_write_back_conflict_backend_wins_skips_write(
        self, service, mock_gateway, mock_change_log_store, mock_backlog_store
    ):
        """When conflict detected and backend is newer, write-back is skipped."""
        from nexus.services.change_log_store import ChangeLogEntry

        entry = _make_entry()
        sem = asyncio.Semaphore(1)

        old_time = datetime.now(UTC) - timedelta(hours=2)
        mock_change_log_store.get_change_log.return_value = ChangeLogEntry(
            path="/mnt/gcs/file.txt",
            backend_name="gcs",
            content_hash="old_hash",
            mtime=old_time,
            backend_version="v1",
        )

        backend = mock_gateway.get_mount_for_path.return_value["backend"]
        backend.get_file_info.return_value = FakeFileInfo(
            backend_version="v2",
            mtime=datetime.now(UTC),  # Backend is newest
        )

        mock_gateway.metadata_get.return_value = MagicMock(
            mtime=datetime.now(UTC) - timedelta(hours=1),  # Nexus is older
            content_hash="nexus_hash",
        )

        await service._process_entry(entry, sem)

        # Backend write should NOT be called (backend wins)
        backend.write_content.assert_not_called()


# =============================================================================
# Rate Limiting Tests
# =============================================================================


class TestRateLimiting:
    """Tests for per-backend semaphore rate limiting."""

    @pytest.mark.asyncio
    async def test_rate_limiting_semaphore(self, service):
        """Semaphore limits concurrent operations per backend."""
        sem = service._get_semaphore("gcs")
        assert isinstance(sem, asyncio.Semaphore)
        # Same backend returns same semaphore
        assert service._get_semaphore("gcs") is sem
        # Different backend gets different semaphore
        assert service._get_semaphore("s3") is not sem


# =============================================================================
# Poll Loop Tests
# =============================================================================


class TestPollLoop:
    """Tests for poll-based processing."""

    @pytest.mark.asyncio
    async def test_poll_loop_processes_pending_entries(
        self, service, mock_backlog_store, mock_event_bus
    ):
        """Poll loop fetches and processes pending entries."""
        entry = _make_entry()
        mock_backlog_store.fetch_pending.return_value = [entry]

        await service._process_all_backends()

        mock_backlog_store.fetch_pending.assert_called()
        mock_backlog_store.mark_in_progress.assert_called()


# =============================================================================
# Stats Tests
# =============================================================================


class TestStats:
    """Tests for get_stats."""

    def test_get_stats_returns_service_info(self, service):
        """Stats include running state and backlog stats."""
        stats = service.get_stats()
        assert stats["running"] is False
        assert stats["conflict_policy"] == "lww"
        assert "backlog_stats" in stats
