"""Tests for delta sync with change tracking (Issue #1127).

Tests the incremental sync functionality that skips unchanged files
based on size, mtime, or backend_version comparison.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from nexus.services.sync_service import (
    ChangeLogEntry,
    SyncContext,
    SyncResult,
    SyncService,
)

# =============================================================================
# Test Fixtures
# =============================================================================


@dataclass
class MockFileInfo:
    """Mock FileInfo for testing."""

    size: int
    mtime: datetime | None = None
    backend_version: str | None = None
    content_hash: str | None = None


@pytest.fixture
def mock_gateway():
    """Create a mock NexusFSGateway."""
    gw = MagicMock()
    gw._metadata_store = MagicMock()
    gw._metadata_store.SessionLocal = MagicMock(return_value=MagicMock())
    gw.hierarchy_enabled = False
    return gw


@pytest.fixture
def sync_service(mock_gateway):
    """Create a SyncService with mock gateway."""
    return SyncService(mock_gateway)


# =============================================================================
# Test ChangeLogEntry
# =============================================================================


class TestChangeLogEntry:
    """Tests for ChangeLogEntry dataclass."""

    def test_create_entry(self):
        """Test creating a change log entry."""
        now = datetime.now(UTC)
        entry = ChangeLogEntry(
            path="/test/file.txt",
            backend_name="s3_connector",
            size_bytes=1024,
            mtime=now,
            backend_version="v123",
            content_hash="abc123",
            synced_at=now,
        )

        assert entry.path == "/test/file.txt"
        assert entry.backend_name == "s3_connector"
        assert entry.size_bytes == 1024
        assert entry.mtime == now
        assert entry.backend_version == "v123"
        assert entry.content_hash == "abc123"

    def test_create_entry_minimal(self):
        """Test creating a minimal change log entry."""
        entry = ChangeLogEntry(
            path="/test/file.txt",
            backend_name="local_connector",
        )

        assert entry.path == "/test/file.txt"
        assert entry.backend_name == "local_connector"
        assert entry.size_bytes is None
        assert entry.mtime is None
        assert entry.backend_version is None


# =============================================================================
# Test _file_unchanged
# =============================================================================


class TestFileUnchanged:
    """Tests for _file_unchanged method (delta sync comparison logic)."""

    def test_unchanged_by_backend_version(self, sync_service):
        """Test detection of unchanged file by backend version match."""
        file_info = MockFileInfo(
            size=1024,
            mtime=datetime.now(UTC),
            backend_version="gen:12345",
        )
        cached = ChangeLogEntry(
            path="/test/file.txt",
            backend_name="gcs_connector",
            size_bytes=1024,
            mtime=datetime.now(UTC) - timedelta(hours=1),  # Different mtime
            backend_version="gen:12345",  # Same version
        )

        # Version match takes precedence - file is unchanged
        assert sync_service._file_unchanged(file_info, cached) is True

    def test_changed_by_backend_version(self, sync_service):
        """Test detection of changed file by backend version mismatch."""
        file_info = MockFileInfo(
            size=1024,
            mtime=datetime.now(UTC),
            backend_version="gen:12346",  # Different version
        )
        cached = ChangeLogEntry(
            path="/test/file.txt",
            backend_name="gcs_connector",
            size_bytes=1024,
            mtime=datetime.now(UTC),  # Same mtime
            backend_version="gen:12345",  # Different version
        )

        # Version mismatch - file has changed
        assert sync_service._file_unchanged(file_info, cached) is False

    def test_unchanged_by_size_and_mtime(self, sync_service):
        """Test detection of unchanged file by size + mtime match (rsync style)."""
        now = datetime.now(UTC)
        file_info = MockFileInfo(
            size=2048,
            mtime=now,
            backend_version=None,  # No version available
        )
        cached = ChangeLogEntry(
            path="/test/file.txt",
            backend_name="local_connector",
            size_bytes=2048,  # Same size
            mtime=now,  # Same mtime
            backend_version=None,
        )

        # Size + mtime match - file is unchanged
        assert sync_service._file_unchanged(file_info, cached) is True

    def test_changed_by_size(self, sync_service):
        """Test detection of changed file by size difference."""
        now = datetime.now(UTC)
        file_info = MockFileInfo(
            size=4096,  # Different size
            mtime=now,
            backend_version=None,
        )
        cached = ChangeLogEntry(
            path="/test/file.txt",
            backend_name="local_connector",
            size_bytes=2048,
            mtime=now,
            backend_version=None,
        )

        # Size mismatch - file has changed
        assert sync_service._file_unchanged(file_info, cached) is False

    def test_changed_by_mtime(self, sync_service):
        """Test detection of changed file by mtime difference."""
        now = datetime.now(UTC)
        file_info = MockFileInfo(
            size=2048,
            mtime=now,  # Current time
            backend_version=None,
        )
        cached = ChangeLogEntry(
            path="/test/file.txt",
            backend_name="local_connector",
            size_bytes=2048,  # Same size
            mtime=now - timedelta(hours=1),  # 1 hour ago
            backend_version=None,
        )

        # mtime difference > 1 second - file has changed
        assert sync_service._file_unchanged(file_info, cached) is False

    def test_unchanged_by_mtime_within_tolerance(self, sync_service):
        """Test mtime comparison with 1-second tolerance."""
        now = datetime.now(UTC)
        file_info = MockFileInfo(
            size=2048,
            mtime=now,
            backend_version=None,
        )
        cached = ChangeLogEntry(
            path="/test/file.txt",
            backend_name="local_connector",
            size_bytes=2048,
            mtime=now + timedelta(milliseconds=500),  # Within 1s tolerance
            backend_version=None,
        )

        # mtime within tolerance - file is unchanged
        assert sync_service._file_unchanged(file_info, cached) is True

    def test_unchanged_by_content_hash(self, sync_service):
        """Test detection of unchanged file by content hash fallback."""
        file_info = MockFileInfo(
            size=None,  # Size unavailable
            mtime=None,  # mtime unavailable
            backend_version=None,
            content_hash="sha256:abc123def456",
        )
        cached = ChangeLogEntry(
            path="/test/file.txt",
            backend_name="remote_connector",
            size_bytes=None,
            mtime=None,
            backend_version=None,
            content_hash="sha256:abc123def456",  # Same hash
        )

        # Content hash match - file is unchanged
        assert sync_service._file_unchanged(file_info, cached) is True

    def test_changed_by_content_hash(self, sync_service):
        """Test detection of changed file by content hash mismatch."""
        file_info = MockFileInfo(
            size=None,
            mtime=None,
            backend_version=None,
            content_hash="sha256:newcontent123",
        )
        cached = ChangeLogEntry(
            path="/test/file.txt",
            backend_name="remote_connector",
            size_bytes=None,
            mtime=None,
            backend_version=None,
            content_hash="sha256:oldcontent456",
        )

        # Content hash mismatch - file has changed
        assert sync_service._file_unchanged(file_info, cached) is False

    def test_unknown_when_no_comparison_possible(self, sync_service):
        """Test returns False (changed) when no comparison is possible."""
        file_info = MockFileInfo(
            size=None,
            mtime=None,
            backend_version=None,
            content_hash=None,
        )
        cached = ChangeLogEntry(
            path="/test/file.txt",
            backend_name="unknown_connector",
            size_bytes=None,
            mtime=None,
            backend_version=None,
            content_hash=None,
        )

        # Cannot determine - assume changed to be safe
        assert sync_service._file_unchanged(file_info, cached) is False


# =============================================================================
# Test SyncContext full_sync flag
# =============================================================================


class TestSyncContextFullSync:
    """Tests for SyncContext full_sync flag."""

    def test_full_sync_default_false(self):
        """Test that full_sync defaults to False."""
        ctx = SyncContext(mount_point="/mnt/test")
        assert ctx.full_sync is False

    def test_full_sync_can_be_enabled(self):
        """Test that full_sync can be set to True."""
        ctx = SyncContext(mount_point="/mnt/test", full_sync=True)
        assert ctx.full_sync is True


# =============================================================================
# Test SyncResult files_skipped
# =============================================================================


class TestSyncResultFilesSkipped:
    """Tests for SyncResult files_skipped metric."""

    def test_files_skipped_default_zero(self):
        """Test that files_skipped defaults to 0."""
        result = SyncResult()
        assert result.files_skipped == 0

    def test_files_skipped_in_dict(self):
        """Test that files_skipped is included in to_dict()."""
        result = SyncResult(
            files_scanned=100,
            files_created=5,
            files_skipped=95,
        )
        d = result.to_dict()
        assert d["files_skipped"] == 95
        assert d["files_scanned"] == 100
        assert d["files_created"] == 5


# =============================================================================
# Test Integration: _sync_file with delta sync
# =============================================================================


class TestSyncFileDeltaSync:
    """Integration tests for _sync_file with delta sync logic."""

    def test_sync_file_skips_unchanged_file(self, sync_service, mock_gateway):
        """Test that _sync_file skips unchanged files."""
        # Setup mock backend with get_file_info
        mock_backend = MagicMock()
        mock_backend.name = "test_connector"
        mock_backend.get_file_info = MagicMock(
            return_value=MagicMock(
                success=True,
                data=MockFileInfo(
                    size=1024,
                    backend_version="gen:12345",
                ),
            )
        )

        # Setup change log to return cached entry with same version
        sync_service._change_log.get_change_log = MagicMock(
            return_value=ChangeLogEntry(
                path="/mnt/test/file.txt",
                backend_name="test_connector",
                size_bytes=1024,
                backend_version="gen:12345",
            )
        )

        # Setup context
        ctx = SyncContext(mount_point="/mnt/test", full_sync=False)
        ctx.context = MagicMock()
        ctx.context.zone_id = "test-zone"

        result = SyncResult()
        files_found: set[str] = set()

        # Call _sync_file
        sync_service._sync_file(
            ctx=ctx,
            backend=mock_backend,
            virtual_path="/mnt/test/file.txt",
            backend_path="file.txt",
            created_by=None,
            result=result,
            files_found=files_found,
            paths_needing_tuples=[],
        )

        # Verify file was skipped
        assert result.files_skipped == 1
        assert result.files_created == 0

    def test_sync_file_syncs_changed_file(self, sync_service, mock_gateway):
        """Test that _sync_file syncs changed files."""
        # Setup mock backend with get_file_info
        mock_backend = MagicMock()
        mock_backend.name = "test_connector"
        mock_backend.get_file_info = MagicMock(
            return_value=MagicMock(
                success=True,
                data=MockFileInfo(
                    size=2048,  # Different size
                    backend_version="gen:12346",  # Different version
                ),
            )
        )

        # Setup change log to return cached entry with different version
        sync_service._change_log.get_change_log = MagicMock(
            return_value=ChangeLogEntry(
                path="/mnt/test/file.txt",
                backend_name="test_connector",
                size_bytes=1024,
                backend_version="gen:12345",
            )
        )

        # Setup gateway mocks
        mock_gateway.metadata_get = MagicMock(return_value=None)
        mock_gateway.metadata_put = MagicMock()
        sync_service._change_log.upsert_change_log = MagicMock(return_value=True)

        # Setup context
        ctx = SyncContext(mount_point="/mnt/test", full_sync=False)
        ctx.context = MagicMock()
        ctx.context.zone_id = "test-zone"

        result = SyncResult()
        files_found: set[str] = set()

        # Call _sync_file
        sync_service._sync_file(
            ctx=ctx,
            backend=mock_backend,
            virtual_path="/mnt/test/file.txt",
            backend_path="file.txt",
            created_by=None,
            result=result,
            files_found=files_found,
            paths_needing_tuples=[],
        )

        # Verify file was synced (not skipped)
        assert result.files_skipped == 0
        assert result.files_created == 1

    def test_sync_file_full_sync_bypasses_delta_check(self, sync_service, mock_gateway):
        """Test that full_sync=True bypasses delta checking."""
        # Setup mock backend with get_file_info
        mock_backend = MagicMock()
        mock_backend.name = "test_connector"
        mock_backend.get_file_info = MagicMock()

        # Setup gateway mocks
        mock_gateway.metadata_get = MagicMock(return_value=None)
        mock_gateway.metadata_put = MagicMock()

        # Setup context with full_sync=True
        ctx = SyncContext(mount_point="/mnt/test", full_sync=True)
        ctx.context = MagicMock()
        ctx.context.zone_id = "test-zone"

        result = SyncResult()
        files_found: set[str] = set()

        # Call _sync_file
        sync_service._sync_file(
            ctx=ctx,
            backend=mock_backend,
            virtual_path="/mnt/test/file.txt",
            backend_path="file.txt",
            created_by=None,
            result=result,
            files_found=files_found,
            paths_needing_tuples=[],
        )

        # Verify get_file_info was NOT called (delta check bypassed)
        mock_backend.get_file_info.assert_not_called()
        # File should be created since full_sync bypasses delta
        assert result.files_created == 1
