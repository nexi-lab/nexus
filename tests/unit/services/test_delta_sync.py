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

    def test_size_match_no_mtime(self, sync_service):
        """Test same size but no mtime available — cannot confirm unchanged."""
        file_info = MockFileInfo(
            size=1024,
            mtime=None,
            backend_version=None,
            content_hash=None,
        )
        cached = ChangeLogEntry(
            path="/test/file.txt",
            backend_name="local_connector",
            size_bytes=1024,
            mtime=None,
            backend_version=None,
            content_hash=None,
        )

        # Same size but no mtime — can't confirm unchanged, assume changed
        assert sync_service._file_unchanged(file_info, cached) is False

    def test_one_has_mtime_other_doesnt(self, sync_service):
        """Test file_info has mtime but cached doesn't — falls through to hash."""
        now = datetime.now(UTC)
        file_info = MockFileInfo(
            size=1024,
            mtime=now,
            backend_version=None,
            content_hash=None,
        )
        cached = ChangeLogEntry(
            path="/test/file.txt",
            backend_name="local_connector",
            size_bytes=1024,
            mtime=None,  # No mtime in cached
            backend_version=None,
            content_hash=None,
        )

        # mtime comparison skipped (one is None), no hash — assume changed
        assert sync_service._file_unchanged(file_info, cached) is False

    def test_zero_size_files(self, sync_service):
        """Test both size=0 with matching mtime — unchanged."""
        now = datetime.now(UTC)
        file_info = MockFileInfo(
            size=0,
            mtime=now,
            backend_version=None,
        )
        cached = ChangeLogEntry(
            path="/test/empty.txt",
            backend_name="local_connector",
            size_bytes=0,
            mtime=now,
            backend_version=None,
        )

        # size=0 is a valid match, mtime matches — unchanged
        assert sync_service._file_unchanged(file_info, cached) is True

    def test_version_takes_precedence_over_size_change(self, sync_service):
        """Test same version but different size — version wins, file unchanged."""
        file_info = MockFileInfo(
            size=2048,  # Different size
            backend_version="gen:12345",
        )
        cached = ChangeLogEntry(
            path="/test/file.txt",
            backend_name="gcs_connector",
            size_bytes=1024,  # Different size
            backend_version="gen:12345",  # Same version
        )

        # Version match takes precedence over size difference
        assert sync_service._file_unchanged(file_info, cached) is True


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


# =============================================================================
# Test Bootstrap: First Sync Populates Change Log, Second Sync Skips
# =============================================================================


class TestDeltaSyncBootstrap:
    """Tests for the delta sync bootstrap flow.

    Verifies that the first sync populates the change log, enabling
    subsequent syncs to skip unchanged files via delta checking.
    """

    def test_first_sync_populates_change_log(self, sync_service, mock_gateway):
        """Test that first sync creates file AND populates change log."""
        # Setup mock backend with get_file_info
        mock_backend = MagicMock()
        mock_backend.name = "test_connector"
        mock_backend.get_file_info = MagicMock(
            return_value=MagicMock(
                success=True,
                data=MockFileInfo(
                    size=1024,
                    mtime=datetime.now(UTC),
                    backend_version="gen:12345",
                ),
            )
        )

        # No cached entry (first sync)
        sync_service._change_log.get_change_log = MagicMock(return_value=None)
        sync_service._change_log.upsert_change_log = MagicMock(return_value=True)

        # No existing metadata (new file)
        mock_gateway.metadata_get = MagicMock(return_value=None)
        mock_gateway.metadata_put = MagicMock()

        ctx = SyncContext(mount_point="/mnt/test", full_sync=False)
        ctx.context = MagicMock()
        ctx.context.zone_id = "test-zone"

        result = SyncResult()
        files_found: set[str] = set()

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

        # File should be created
        assert result.files_created == 1
        assert result.files_skipped == 0

        # get_file_info SHOULD be called (to populate change log)
        mock_backend.get_file_info.assert_called_once()

        # Change log SHOULD be populated
        sync_service._change_log.upsert_change_log.assert_called_once_with(
            path="/mnt/test/file.txt",
            backend_name="test_connector",
            zone_id="test-zone",
            size_bytes=1024,
            mtime=mock_backend.get_file_info.return_value.data.mtime,
            backend_version="gen:12345",
            content_hash=None,
        )

    def test_second_sync_skips_unchanged_after_bootstrap(self, sync_service, mock_gateway):
        """Test that second sync skips file when change log was populated by first sync."""
        now = datetime.now(UTC)

        # Setup mock backend with same file_info as "first sync"
        mock_backend = MagicMock()
        mock_backend.name = "test_connector"
        mock_backend.get_file_info = MagicMock(
            return_value=MagicMock(
                success=True,
                data=MockFileInfo(
                    size=1024,
                    mtime=now,
                    backend_version="gen:12345",
                ),
            )
        )

        # Cached entry exists from first sync (same version)
        sync_service._change_log.get_change_log = MagicMock(
            return_value=ChangeLogEntry(
                path="/mnt/test/file.txt",
                backend_name="test_connector",
                size_bytes=1024,
                mtime=now,
                backend_version="gen:12345",
                synced_at=now,
            )
        )

        ctx = SyncContext(mount_point="/mnt/test", full_sync=False)
        ctx.context = MagicMock()
        ctx.context.zone_id = "test-zone"

        result = SyncResult()
        files_found: set[str] = set()

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

        # File should be SKIPPED (unchanged)
        assert result.files_skipped == 1
        assert result.files_created == 0

        # metadata_get should NOT be called (skipped before metadata check)
        mock_gateway.metadata_get.assert_not_called()

    def test_first_sync_populates_for_existing_files(self, sync_service, mock_gateway):
        """Test change log is populated for pre-existing files on first delta sync."""
        existing_meta = MagicMock()
        existing_meta.path = "/mnt/test/file.txt"

        mock_backend = MagicMock()
        mock_backend.name = "test_connector"
        mock_backend.get_file_info = MagicMock(
            return_value=MagicMock(
                success=True,
                data=MockFileInfo(
                    size=2048,
                    mtime=datetime.now(UTC),
                    backend_version="gen:99999",
                ),
            )
        )

        # No cached entry (first delta sync for this file)
        sync_service._change_log.get_change_log = MagicMock(return_value=None)
        sync_service._change_log.upsert_change_log = MagicMock(return_value=True)

        # File already exists in metadata (pre-existing)
        mock_gateway.metadata_get = MagicMock(return_value=existing_meta)

        ctx = SyncContext(mount_point="/mnt/test", full_sync=False)
        ctx.context = MagicMock()
        ctx.context.zone_id = "test-zone"

        result = SyncResult()
        files_found: set[str] = set()

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

        # File should NOT be created (already exists)
        assert result.files_created == 0
        assert result.files_skipped == 0

        # Change log SHOULD still be populated for the existing file
        sync_service._change_log.upsert_change_log.assert_called_once()


# =============================================================================
# Test Deletion Cleans Up Change Log
# =============================================================================


class TestDeletionChangeLogCleanup:
    """Tests for change log cleanup during file deletion."""

    def test_sync_deletions_cleans_up_change_log(self, sync_service, mock_gateway):
        """Test that _sync_deletions removes stale change log entries."""
        # Setup: file exists in metadata but NOT in backend (was deleted)
        existing_meta = MagicMock()
        existing_meta.path = "/mnt/test/deleted_file.txt"

        mock_gateway.metadata_list = MagicMock(return_value=[existing_meta])
        mock_gateway.metadata_get = MagicMock(return_value=existing_meta)
        mock_gateway.metadata_delete = MagicMock()
        mock_gateway.list_mounts = MagicMock(return_value=[])

        sync_service._change_log.delete_change_log = MagicMock(return_value=True)

        mock_backend = MagicMock()
        mock_backend.name = "test_connector"

        ctx = SyncContext(mount_point="/mnt/test")
        ctx.context = MagicMock()
        ctx.context.zone_id = "test-zone"

        result = SyncResult()
        files_found: set[str] = set()  # Empty = file not found in backend

        sync_service._sync_deletions(ctx, mock_backend, files_found, result)

        # File should be deleted
        assert result.files_deleted == 1
        mock_gateway.metadata_delete.assert_called_once_with("/mnt/test/deleted_file.txt")

        # Change log should be cleaned up
        sync_service._change_log.delete_change_log.assert_called_once_with(
            "/mnt/test/deleted_file.txt", "test_connector", "test-zone"
        )


# =============================================================================
# Test _sync_single_file delegation
# =============================================================================


class TestSyncSingleFileDelegation:
    """Tests that _sync_single_file delegates correctly to _sync_file."""

    def test_sync_single_file_delegates_to_sync_file(self, sync_service, mock_gateway):
        """Test that _sync_single_file produces same result as _sync_file."""
        mock_backend = MagicMock()
        mock_backend.name = "test_connector"
        mock_backend.get_file_info = MagicMock(
            return_value=MagicMock(
                success=True,
                data=MockFileInfo(
                    size=1024,
                    backend_version="gen:100",
                ),
            )
        )

        sync_service._change_log.get_change_log = MagicMock(return_value=None)
        sync_service._change_log.upsert_change_log = MagicMock(return_value=True)

        mock_gateway.metadata_get = MagicMock(return_value=None)
        mock_gateway.metadata_put = MagicMock()

        ctx = SyncContext(mount_point="/mnt/test", full_sync=False)
        ctx.context = MagicMock()
        ctx.context.zone_id = "test-zone"

        result = SyncResult()
        files_found: set[str] = set()
        paths_needing_tuples: list[str] = []
        flush_fn = MagicMock()

        returned = sync_service._sync_single_file(
            ctx=ctx,
            backend=mock_backend,
            virtual_path="/mnt/test/file.txt",
            backend_path="file.txt",
            created_by=None,
            result=result,
            files_found=files_found,
            paths_needing_tuples=paths_needing_tuples,
            flush_fn=flush_fn,
        )

        # File should be created via delegation
        assert result.files_created == 1
        assert result.files_scanned == 1
        assert "/mnt/test/file.txt" in files_found
        assert returned is files_found

        # Change log should be populated (bootstrap via delegation)
        sync_service._change_log.upsert_change_log.assert_called_once()

    def test_sync_single_file_skips_unchanged_via_delegation(self, sync_service, mock_gateway):
        """Test that _sync_single_file gets delta checking via delegation."""
        now = datetime.now(UTC)

        mock_backend = MagicMock()
        mock_backend.name = "test_connector"
        mock_backend.get_file_info = MagicMock(
            return_value=MagicMock(
                success=True,
                data=MockFileInfo(size=1024, backend_version="gen:100"),
            )
        )

        # Cached entry with same version (file unchanged)
        sync_service._change_log.get_change_log = MagicMock(
            return_value=ChangeLogEntry(
                path="/mnt/test/file.txt",
                backend_name="test_connector",
                size_bytes=1024,
                backend_version="gen:100",
                synced_at=now,
            )
        )

        ctx = SyncContext(mount_point="/mnt/test", full_sync=False)
        ctx.context = MagicMock()
        ctx.context.zone_id = "test-zone"

        result = SyncResult()
        files_found: set[str] = set()

        sync_service._sync_single_file(
            ctx=ctx,
            backend=mock_backend,
            virtual_path="/mnt/test/file.txt",
            backend_path="file.txt",
            created_by=None,
            result=result,
            files_found=files_found,
            paths_needing_tuples=[],
            flush_fn=MagicMock(),
        )

        # File should be SKIPPED (delta check via delegation)
        assert result.files_skipped == 1
        assert result.files_created == 0


# =============================================================================
# Test backend without get_file_info (graceful degradation)
# =============================================================================


class TestBackendWithoutGetFileInfo:
    """Tests for backends that don't support get_file_info."""

    def test_sync_file_works_without_get_file_info(self, sync_service, mock_gateway):
        """Test that sync works when backend lacks get_file_info."""
        mock_backend = MagicMock(spec=["name", "get_content_size"])
        mock_backend.name = "basic_connector"
        # No get_file_info attribute (spec limits available attributes)

        mock_gateway.metadata_get = MagicMock(return_value=None)
        mock_gateway.metadata_put = MagicMock()

        ctx = SyncContext(mount_point="/mnt/test", full_sync=False)
        ctx.context = MagicMock()
        ctx.context.zone_id = "test-zone"

        result = SyncResult()
        files_found: set[str] = set()

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

        # File should be created (no delta check, falls through to creation)
        assert result.files_created == 1
        assert result.files_skipped == 0
