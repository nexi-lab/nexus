"""Unit tests for WriteBackService (Issue #1129, #1130).

Tests event handling, backlog processing, conflict resolution
with all 6 strategies, per-mount config, and rate limiting.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.core.event_bus import FileEvent, FileEventType
from nexus.services.conflict_resolution import ConflictStrategy
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
        "conflict_strategy": None,
    }
    gw.list_mounts.return_value = [
        {
            "mount_point": "/mnt/gcs",
            "readonly": False,
            "backend_type": "GCSConnector",
            "backend": MagicMock(name="gcs"),
            "conflict_strategy": None,
        }
    ]
    gw.metadata_get.return_value = MagicMock(mtime=datetime.now(UTC), content_hash="abc", size=1024)
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
def mock_conflict_log_store():
    """Mock conflict log store."""
    store = MagicMock()
    store.log_conflict.return_value = "conflict-1"
    return store


@pytest.fixture
def service(mock_gateway, mock_event_bus, mock_backlog_store, mock_change_log_store):
    """Create WriteBackService with all mocks."""
    return WriteBackService(
        gateway=mock_gateway,
        event_bus=mock_event_bus,
        backlog_store=mock_backlog_store,
        change_log_store=mock_change_log_store,
        default_strategy=ConflictStrategy.KEEP_NEWER,
        max_concurrent_per_backend=2,
        poll_interval_seconds=0.1,
    )


def _setup_conflict(
    mock_gateway,
    mock_change_log_store,
    *,
    nexus_newer: bool = True,
    nexus_size: int = 1024,
    backend_size: int = 2048,
):
    """Set up a conflict scenario for testing."""
    from nexus.services.change_log_store import ChangeLogEntry

    old_time = datetime.now(UTC) - timedelta(hours=2)
    mock_change_log_store.get_change_log.return_value = ChangeLogEntry(
        path="/mnt/gcs/file.txt",
        backend_name="gcs",
        content_hash="old_hash",
        mtime=old_time,
        backend_version="v1",
    )

    backend = mock_gateway.get_mount_for_path.return_value["backend"]
    if nexus_newer:
        backend_mtime = datetime.now(UTC) - timedelta(hours=1)
        nexus_mtime = datetime.now(UTC)
    else:
        backend_mtime = datetime.now(UTC)
        nexus_mtime = datetime.now(UTC) - timedelta(hours=1)

    backend.get_file_info.return_value = FakeFileInfo(
        backend_version="v2",
        mtime=backend_mtime,
        size=backend_size,
    )
    mock_gateway.metadata_get.return_value = MagicMock(
        mtime=nexus_mtime,
        content_hash="new_nexus_hash",
        size=nexus_size,
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
            "conflict_strategy": None,
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
    async def test_write_back_conflict_keep_newer_nexus_wins(
        self, service, mock_gateway, mock_change_log_store, mock_event_bus
    ):
        """KEEP_NEWER: Nexus newer -> write-back proceeds."""
        _setup_conflict(mock_gateway, mock_change_log_store, nexus_newer=True)
        entry = _make_entry()
        sem = asyncio.Semaphore(1)

        await service._process_entry(entry, sem)

        backend = mock_gateway.get_mount_for_path.return_value["backend"]
        backend.write_content.assert_called_once()

    @pytest.mark.asyncio
    async def test_write_back_conflict_keep_newer_backend_wins(
        self, service, mock_gateway, mock_change_log_store, mock_backlog_store
    ):
        """KEEP_NEWER: Backend newer -> write-back skipped."""
        _setup_conflict(mock_gateway, mock_change_log_store, nexus_newer=False)
        entry = _make_entry()
        sem = asyncio.Semaphore(1)

        await service._process_entry(entry, sem)

        backend = mock_gateway.get_mount_for_path.return_value["backend"]
        backend.write_content.assert_not_called()


# =============================================================================
# Per-Mount Strategy Resolution Tests
# =============================================================================


class TestPerMountStrategy:
    """Tests for per-mount conflict strategy resolution chain."""

    def test_mount_explicit_strategy(
        self,
        mock_gateway,
        mock_event_bus,
        mock_backlog_store,
        mock_change_log_store,
    ):
        """Mount has explicit strategy -> use it."""
        mock_gateway.get_mount_for_path.return_value["conflict_strategy"] = "keep_remote"

        svc = WriteBackService(
            gateway=mock_gateway,
            event_bus=mock_event_bus,
            backlog_store=mock_backlog_store,
            change_log_store=mock_change_log_store,
            default_strategy=ConflictStrategy.KEEP_NEWER,
        )

        mount_info = mock_gateway.get_mount_for_path.return_value
        result = svc._resolve_strategy(mount_info)
        assert result == ConflictStrategy.KEEP_REMOTE

    def test_mount_none_falls_to_global(
        self,
        mock_gateway,
        mock_event_bus,
        mock_backlog_store,
        mock_change_log_store,
    ):
        """Mount has no strategy -> fall back to global default."""
        mock_gateway.get_mount_for_path.return_value["conflict_strategy"] = None

        svc = WriteBackService(
            gateway=mock_gateway,
            event_bus=mock_event_bus,
            backlog_store=mock_backlog_store,
            change_log_store=mock_change_log_store,
            default_strategy=ConflictStrategy.KEEP_LARGER,
        )

        mount_info = mock_gateway.get_mount_for_path.return_value
        result = svc._resolve_strategy(mount_info)
        assert result == ConflictStrategy.KEEP_LARGER

    def test_invalid_mount_strategy_falls_to_global(
        self,
        mock_gateway,
        mock_event_bus,
        mock_backlog_store,
        mock_change_log_store,
    ):
        """Mount has invalid strategy string -> fall back to global default."""
        mock_gateway.get_mount_for_path.return_value["conflict_strategy"] = "invalid_strategy"

        svc = WriteBackService(
            gateway=mock_gateway,
            event_bus=mock_event_bus,
            backlog_store=mock_backlog_store,
            change_log_store=mock_change_log_store,
            default_strategy=ConflictStrategy.KEEP_NEWER,
        )

        mount_info = mock_gateway.get_mount_for_path.return_value
        result = svc._resolve_strategy(mount_info)
        assert result == ConflictStrategy.KEEP_NEWER

    def test_default_strategy_is_keep_newer(
        self,
        mock_gateway,
        mock_event_bus,
        mock_backlog_store,
        mock_change_log_store,
    ):
        """Default constructor uses KEEP_NEWER."""
        svc = WriteBackService(
            gateway=mock_gateway,
            event_bus=mock_event_bus,
            backlog_store=mock_backlog_store,
            change_log_store=mock_change_log_store,
        )
        assert svc._default_strategy == ConflictStrategy.KEEP_NEWER


# =============================================================================
# Strategy-Specific Conflict Tests
# =============================================================================


class TestStrategyConflicts:
    """Tests for specific conflict resolution strategies in write-back."""

    @pytest.mark.asyncio
    async def test_abort_strategy_raises_and_marks_failed(
        self,
        mock_gateway,
        mock_event_bus,
        mock_backlog_store,
        mock_change_log_store,
    ):
        """ABORT strategy raises ConflictAbortError -> marks failed."""
        _setup_conflict(mock_gateway, mock_change_log_store, nexus_newer=True)
        mock_gateway.get_mount_for_path.return_value["conflict_strategy"] = "abort"

        svc = WriteBackService(
            gateway=mock_gateway,
            event_bus=mock_event_bus,
            backlog_store=mock_backlog_store,
            change_log_store=mock_change_log_store,
            default_strategy=ConflictStrategy.ABORT,
        )

        entry = _make_entry()
        sem = asyncio.Semaphore(1)

        await svc._process_entry(entry, sem)

        mock_backlog_store.mark_failed.assert_called_once()
        backend = mock_gateway.get_mount_for_path.return_value["backend"]
        backend.write_content.assert_not_called()

    @pytest.mark.asyncio
    async def test_keep_remote_always_skips_write(
        self,
        mock_gateway,
        mock_event_bus,
        mock_backlog_store,
        mock_change_log_store,
    ):
        """KEEP_REMOTE -> backend wins, write-back skipped."""
        _setup_conflict(mock_gateway, mock_change_log_store, nexus_newer=True)
        mock_gateway.get_mount_for_path.return_value["conflict_strategy"] = "keep_remote"

        svc = WriteBackService(
            gateway=mock_gateway,
            event_bus=mock_event_bus,
            backlog_store=mock_backlog_store,
            change_log_store=mock_change_log_store,
        )

        entry = _make_entry()
        sem = asyncio.Semaphore(1)

        await svc._process_entry(entry, sem)

        backend = mock_gateway.get_mount_for_path.return_value["backend"]
        backend.write_content.assert_not_called()

    @pytest.mark.asyncio
    async def test_keep_local_always_proceeds(
        self,
        mock_gateway,
        mock_event_bus,
        mock_backlog_store,
        mock_change_log_store,
    ):
        """KEEP_LOCAL -> nexus wins, write-back proceeds."""
        _setup_conflict(mock_gateway, mock_change_log_store, nexus_newer=False)
        mock_gateway.get_mount_for_path.return_value["conflict_strategy"] = "keep_local"

        svc = WriteBackService(
            gateway=mock_gateway,
            event_bus=mock_event_bus,
            backlog_store=mock_backlog_store,
            change_log_store=mock_change_log_store,
        )

        entry = _make_entry()
        sem = asyncio.Semaphore(1)

        await svc._process_entry(entry, sem)

        backend = mock_gateway.get_mount_for_path.return_value["backend"]
        backend.write_content.assert_called_once()

    @pytest.mark.asyncio
    async def test_keep_larger_nexus_larger_proceeds(
        self,
        mock_gateway,
        mock_event_bus,
        mock_backlog_store,
        mock_change_log_store,
    ):
        """KEEP_LARGER: Nexus larger -> proceeds with write-back."""
        _setup_conflict(
            mock_gateway,
            mock_change_log_store,
            nexus_newer=False,
            nexus_size=5000,
            backend_size=1000,
        )
        mock_gateway.get_mount_for_path.return_value["conflict_strategy"] = "keep_larger"

        svc = WriteBackService(
            gateway=mock_gateway,
            event_bus=mock_event_bus,
            backlog_store=mock_backlog_store,
            change_log_store=mock_change_log_store,
        )

        entry = _make_entry()
        sem = asyncio.Semaphore(1)

        await svc._process_entry(entry, sem)

        backend = mock_gateway.get_mount_for_path.return_value["backend"]
        backend.write_content.assert_called_once()

    @pytest.mark.asyncio
    async def test_keep_larger_backend_larger_skips(
        self,
        mock_gateway,
        mock_event_bus,
        mock_backlog_store,
        mock_change_log_store,
    ):
        """KEEP_LARGER: Backend larger -> skips write-back."""
        _setup_conflict(
            mock_gateway,
            mock_change_log_store,
            nexus_newer=True,
            nexus_size=500,
            backend_size=5000,
        )
        mock_gateway.get_mount_for_path.return_value["conflict_strategy"] = "keep_larger"

        svc = WriteBackService(
            gateway=mock_gateway,
            event_bus=mock_event_bus,
            backlog_store=mock_backlog_store,
            change_log_store=mock_change_log_store,
        )

        entry = _make_entry()
        sem = asyncio.Semaphore(1)

        await svc._process_entry(entry, sem)

        backend = mock_gateway.get_mount_for_path.return_value["backend"]
        backend.write_content.assert_not_called()

    @pytest.mark.asyncio
    async def test_rename_conflict_creates_copy_and_proceeds(
        self,
        mock_gateway,
        mock_event_bus,
        mock_backlog_store,
        mock_change_log_store,
    ):
        """RENAME_CONFLICT: creates conflict copy and proceeds with write-back."""
        _setup_conflict(mock_gateway, mock_change_log_store, nexus_newer=True)
        mock_gateway.get_mount_for_path.return_value["conflict_strategy"] = "rename_conflict"

        svc = WriteBackService(
            gateway=mock_gateway,
            event_bus=mock_event_bus,
            backlog_store=mock_backlog_store,
            change_log_store=mock_change_log_store,
        )

        entry = _make_entry()
        sem = asyncio.Semaphore(1)

        await svc._process_entry(entry, sem)

        # Should create conflict copy via gateway.write
        mock_gateway.write.assert_called_once()
        conflict_path = mock_gateway.write.call_args[0][0]
        assert ".sync-conflict-" in conflict_path

        # Should still proceed with write-back
        backend = mock_gateway.get_mount_for_path.return_value["backend"]
        backend.write_content.assert_called_once()


# =============================================================================
# Conflict Logging Tests
# =============================================================================


class TestConflictLogging:
    """Tests for conflict audit log integration."""

    @pytest.mark.asyncio
    async def test_conflict_logged_to_store(
        self,
        mock_gateway,
        mock_event_bus,
        mock_backlog_store,
        mock_change_log_store,
        mock_conflict_log_store,
    ):
        """Conflict events are logged to ConflictLogStore."""
        _setup_conflict(mock_gateway, mock_change_log_store, nexus_newer=True)

        svc = WriteBackService(
            gateway=mock_gateway,
            event_bus=mock_event_bus,
            backlog_store=mock_backlog_store,
            change_log_store=mock_change_log_store,
            conflict_log_store=mock_conflict_log_store,
        )

        entry = _make_entry()
        sem = asyncio.Semaphore(1)

        await svc._process_entry(entry, sem)

        mock_conflict_log_store.log_conflict.assert_called_once()

    @pytest.mark.asyncio
    async def test_conflict_log_failure_does_not_block_write(
        self,
        mock_gateway,
        mock_event_bus,
        mock_backlog_store,
        mock_change_log_store,
        mock_conflict_log_store,
    ):
        """If conflict logging fails, write-back still proceeds."""
        _setup_conflict(mock_gateway, mock_change_log_store, nexus_newer=True)
        mock_conflict_log_store.log_conflict.side_effect = RuntimeError("DB error")

        svc = WriteBackService(
            gateway=mock_gateway,
            event_bus=mock_event_bus,
            backlog_store=mock_backlog_store,
            change_log_store=mock_change_log_store,
            conflict_log_store=mock_conflict_log_store,
        )

        entry = _make_entry()
        sem = asyncio.Semaphore(1)

        await svc._process_entry(entry, sem)

        # Write should still proceed
        backend = mock_gateway.get_mount_for_path.return_value["backend"]
        backend.write_content.assert_called_once()


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
        """Stats include running state and strategy info."""
        stats = service.get_stats()
        assert stats["running"] is False
        assert stats["default_strategy"] == "keep_newer"
        assert "backlog_stats" in stats


# =============================================================================
# Conflict Copy Path Generation Tests
# =============================================================================


class TestConflictCopyPath:
    """Tests for _generate_conflict_copy_path."""

    def test_generates_expected_format(self):
        path = WriteBackService._generate_conflict_copy_path(
            "/mnt/gcs/docs/report.pdf", "gcs_backend"
        )
        assert "/mnt/gcs/docs/" in path
        assert ".sync-conflict-" in path
        assert "-gcs_backend" in path
        assert path.endswith(".pdf")

    def test_handles_no_extension(self):
        path = WriteBackService._generate_conflict_copy_path("/mnt/gcs/Makefile", "s3")
        assert ".sync-conflict-" in path
        assert "-s3" in path
        # No extension — should end with backend name
        assert not path.endswith(".")

    def test_handles_hidden_files(self):
        path = WriteBackService._generate_conflict_copy_path("/mnt/gcs/.gitignore", "local")
        assert ".sync-conflict-" in path
        assert "-local" in path
