"""Unit tests for SyncService.

Tests metadata and content synchronization from connector backends.
"""

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from nexus.contracts.types import OperationContext
from nexus.services.sync.change_log_store import ChangeLogEntry
from nexus.services.sync.sync_service import (
    SyncContext,
    SyncResult,
    SyncService,
    _belongs_to_other_mount,
)

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_gateway():
    """Create a mock NexusFSGateway with standard configuration."""
    gw = MagicMock()
    gw.hierarchy_enabled = False
    gw.metadata_get.return_value = None
    gw.metadata_put.return_value = None
    gw.metadata_delete.return_value = None
    gw.metadata_list.return_value = []
    gw.rebac_check.return_value = True
    gw.session_factory = None
    gw.list_mounts.return_value = []

    # record_store and is_postgresql for ChangeLogStore init
    gw.record_store = MagicMock()
    gw.record_store.session_factory = None
    gw.is_postgresql = False

    # Router mock — source uses has_mount() + route(), not get_mount()
    mount_info = MagicMock()
    mount_info.backend = MagicMock()
    mount_info.backend.name = "test_backend"
    mount_info.backend.list_dir = MagicMock(return_value=[])
    gw.router = MagicMock()
    gw.router.has_mount.return_value = True
    gw.router.route.return_value = mount_info
    # Keep get_mount for backward compat (used in _sync_all_mounts path)
    gw.router.get_mount.return_value = mount_info

    return gw


@pytest.fixture
def sync_service(mock_gateway):
    """Create a SyncService with mocked gateway."""
    return SyncService(gateway=mock_gateway)


@pytest.fixture
def operation_context():
    """Standard operation context for tests."""
    return OperationContext(
        user_id="test_user",
        groups=["test_group"],
        zone_id="test_zone",
        is_system=False,
        is_admin=False,
    )


@pytest.fixture
def sync_context(operation_context):
    """Standard sync context for tests."""
    return SyncContext(
        mount_point="/mnt/gcs",
        recursive=True,
        context=operation_context,
    )


# =============================================================================
# _belongs_to_other_mount helper tests
# =============================================================================


class TestBelongsToOtherMount:
    """Tests for the _belongs_to_other_mount helper function."""

    def test_path_matches_exact_mount(self):
        """Path that exactly matches a mount point belongs to that mount."""
        sorted_mounts = ["/mnt/a", "/mnt/b", "/mnt/c"]
        assert _belongs_to_other_mount("/mnt/b", sorted_mounts) is True

    def test_path_is_child_of_mount(self):
        """Path that is a child of a mount point belongs to that mount."""
        sorted_mounts = ["/mnt/a", "/mnt/b", "/mnt/c"]
        assert _belongs_to_other_mount("/mnt/b/file.txt", sorted_mounts) is True

    def test_path_does_not_belong_to_any_mount(self):
        """Path not under any mount returns False."""
        sorted_mounts = ["/mnt/a", "/mnt/b", "/mnt/c"]
        assert _belongs_to_other_mount("/other/file.txt", sorted_mounts) is False

    def test_empty_mount_list(self):
        """Empty mount list means nothing belongs to another mount."""
        assert _belongs_to_other_mount("/any/path", []) is False

    def test_path_with_similar_prefix_does_not_match(self):
        """Path that shares a prefix but is not a child should not match."""
        sorted_mounts = ["/mnt/abc"]
        # "/mnt/abcdef" starts with "/mnt/abc" but not "/mnt/abc/"
        assert _belongs_to_other_mount("/mnt/abcdef", sorted_mounts) is False

    def test_deeply_nested_child(self):
        """Deeply nested path correctly identified as belonging to mount."""
        sorted_mounts = ["/mnt/deep"]
        assert _belongs_to_other_mount("/mnt/deep/a/b/c/d.txt", sorted_mounts) is True


# =============================================================================
# SyncService initialization tests
# =============================================================================


class TestSyncServiceInit:
    """Tests for SyncService construction."""

    def test_init_stores_gateway(self, mock_gateway):
        """SyncService stores gateway reference."""
        service = SyncService(gateway=mock_gateway)
        assert service._gw is mock_gateway

    def test_init_creates_change_log_store(self, mock_gateway):
        """SyncService initializes a ChangeLogStore."""
        service = SyncService(gateway=mock_gateway)
        assert service._change_log is not None
        assert service._change_log._session_factory is mock_gateway.record_store.session_factory


# =============================================================================
# sync_mount tests
# =============================================================================


class TestSyncMount:
    """Tests for the sync_mount method."""

    def test_sync_mount_returns_sync_result(self, sync_service, sync_context):
        """sync_mount returns a SyncResult instance."""
        result = sync_service.sync_mount(sync_context)
        assert isinstance(result, SyncResult)

    def test_sync_mount_checks_permission(self, sync_service, sync_context, mock_gateway):
        """sync_mount checks read permission before syncing."""
        mock_gateway.rebac_check.return_value = False

        with pytest.raises(PermissionError, match="no read permission"):
            sync_service.sync_mount(sync_context)

    def test_sync_mount_no_context_allows_access(self, sync_service, mock_gateway):
        """sync_mount allows access when no context is provided (backward compat)."""
        ctx = SyncContext(mount_point="/mnt/gcs", recursive=True, context=None)
        result = sync_service.sync_mount(ctx)
        assert isinstance(result, SyncResult)

    def test_sync_mount_admin_bypasses_permission(self, sync_service, mock_gateway):
        """Admin users bypass permission checks."""
        mock_gateway.rebac_check.return_value = False
        admin_ctx = OperationContext(
            user_id="admin",
            groups=[],
            zone_id="test_zone",
            is_admin=True,
        )
        ctx = SyncContext(mount_point="/mnt/gcs", recursive=True, context=admin_ctx)
        result = sync_service.sync_mount(ctx)
        assert isinstance(result, SyncResult)

    def test_sync_mount_validates_mount_exists(self, sync_service, mock_gateway):
        """sync_mount raises ValueError if mount not found."""
        mock_gateway.router.has_mount.return_value = False

        ctx = SyncContext(mount_point="/mnt/nonexistent", recursive=True)
        with pytest.raises(ValueError, match="Mount not found"):
            sync_service.sync_mount(ctx)

    def test_sync_mount_validates_backend_supports_listing(self, sync_service, mock_gateway):
        """sync_mount raises RuntimeError if backend has no list_dir."""
        mount = MagicMock()
        mount.backend = MagicMock(spec=[])  # spec=[] means no attributes
        mock_gateway.router.route.return_value = mount

        ctx = SyncContext(mount_point="/mnt/test", recursive=True)
        with pytest.raises(RuntimeError, match="does not support metadata sync"):
            sync_service.sync_mount(ctx)

    def test_sync_mount_with_files_from_backend(self, sync_service, sync_context, mock_gateway):
        """sync_mount creates metadata for files found in backend."""
        mount = mock_gateway.router.route.return_value
        backend = mount.backend
        backend.list_dir.return_value = ["file1.txt", "file2.txt"]

        # Mock _get_file_size to avoid internal import issues
        sync_service._get_file_size = MagicMock(return_value=1024)

        result = sync_service.sync_mount(sync_context)

        assert result.files_scanned == 2
        assert result.files_created == 2
        assert mock_gateway.metadata_put.call_count == 2

    def test_sync_mount_skips_existing_files(self, sync_service, sync_context, mock_gateway):
        """sync_mount does not recreate metadata for existing files."""
        mount = mock_gateway.router.route.return_value
        backend = mount.backend
        backend.list_dir.return_value = ["existing.txt"]

        # Return existing metadata for the file
        mock_gateway.metadata_get.return_value = MagicMock(path="/mnt/gcs/existing.txt")

        result = sync_service.sync_mount(sync_context)

        assert result.files_scanned == 1
        assert result.files_created == 0

    def test_sync_mount_handles_directories(self, sync_service, sync_context, mock_gateway):
        """sync_mount recurses into directories when recursive=True."""
        mount = mock_gateway.router.route.return_value
        backend = mount.backend

        # First call returns a directory, second call returns files in that directory
        backend.list_dir.side_effect = [
            ["subdir/"],  # Root listing
            ["nested.txt"],  # subdir listing
        ]

        # Mock _get_file_size to avoid internal import issues
        sync_service._get_file_size = MagicMock(return_value=512)

        result = sync_service.sync_mount(sync_context)

        # subdir entry + nested.txt entry
        assert result.files_scanned == 1  # Only files are counted as scanned
        assert mock_gateway.metadata_put.call_count == 2  # dir + file

    def test_sync_mount_non_recursive(self, sync_service, mock_gateway):
        """sync_mount with recursive=False does not traverse subdirectories."""
        mount = mock_gateway.router.route.return_value
        backend = mount.backend
        backend.list_dir.return_value = ["subdir/", "file.txt"]

        ctx = SyncContext(
            mount_point="/mnt/gcs",
            recursive=False,
            context=OperationContext(user_id="test_user", groups=[], zone_id="test_zone"),
        )

        sync_service.sync_mount(ctx)

        # Should only call list_dir once (root), not recurse into subdir
        assert backend.list_dir.call_count == 1


# =============================================================================
# dry_run tests
# =============================================================================


class TestSyncMountDryRun:
    """Tests for dry_run mode."""

    def test_dry_run_does_not_create_metadata(self, sync_service, mock_gateway):
        """Dry run scans but does not write metadata."""
        mount = mock_gateway.router.route.return_value
        backend = mount.backend
        backend.list_dir.return_value = ["file1.txt", "file2.txt"]

        ctx = SyncContext(
            mount_point="/mnt/gcs",
            recursive=True,
            dry_run=True,
            context=OperationContext(user_id="test_user", groups=[], zone_id="test_zone"),
        )

        result = sync_service.sync_mount(ctx)

        assert result.files_scanned == 2
        assert result.files_created == 0
        mock_gateway.metadata_put.assert_not_called()

    def test_dry_run_does_not_delete_metadata(self, sync_service, mock_gateway):
        """Dry run does not delete files no longer in backend."""
        mount = mock_gateway.router.route.return_value
        backend = mount.backend
        backend.list_dir.return_value = []

        # Existing metadata that would be deleted in non-dry_run
        existing_meta = MagicMock()
        existing_meta.path = "/mnt/gcs/old_file.txt"
        mock_gateway.metadata_list.return_value = [existing_meta]

        ctx = SyncContext(
            mount_point="/mnt/gcs",
            recursive=True,
            dry_run=True,
            context=OperationContext(user_id="test_user", groups=[], zone_id="test_zone"),
        )

        result = sync_service.sync_mount(ctx)
        assert result.files_deleted == 0
        mock_gateway.metadata_delete.assert_not_called()

    def test_dry_run_does_not_sync_content(self, sync_service, mock_gateway):
        """Dry run does not sync content even if sync_content=True."""
        mount = mock_gateway.router.route.return_value
        backend = mount.backend
        backend.list_dir.return_value = []
        backend.sync_content_to_cache = MagicMock()

        ctx = SyncContext(
            mount_point="/mnt/gcs",
            recursive=True,
            dry_run=True,
            sync_content=True,
            context=OperationContext(user_id="test_user", groups=[], zone_id="test_zone"),
        )

        sync_service.sync_mount(ctx)
        backend.sync_content_to_cache.assert_not_called()


# =============================================================================
# _sync_deletions tests
# =============================================================================


class TestSyncDeletions:
    """Tests for _sync_deletions method."""

    def test_deletes_files_not_in_backend(self, sync_service, mock_gateway):
        """Files in metadata but not in backend are deleted."""
        existing_meta = MagicMock()
        existing_meta.path = "/mnt/gcs/old_file.txt"
        mock_gateway.metadata_list.return_value = [existing_meta]

        backend = MagicMock()
        backend.name = "test_backend"

        ctx = SyncContext(
            mount_point="/mnt/gcs",
            recursive=True,
            context=OperationContext(user_id="test_user", groups=[], zone_id="test_zone"),
        )

        result = SyncResult()
        files_found: set[str] = set()  # No files found in backend

        sync_service._sync_deletions(ctx, backend, files_found, result)

        assert result.files_deleted == 1
        mock_gateway.metadata_delete.assert_called_once_with("/mnt/gcs/old_file.txt")

    def test_skips_files_found_in_backend(self, sync_service, mock_gateway):
        """Files that exist in both metadata and backend are not deleted."""
        existing_meta = MagicMock()
        existing_meta.path = "/mnt/gcs/active.txt"
        mock_gateway.metadata_list.return_value = [existing_meta]

        backend = MagicMock()
        backend.name = "test_backend"

        ctx = SyncContext(
            mount_point="/mnt/gcs",
            recursive=True,
            context=OperationContext(user_id="test_user", groups=[], zone_id="test_zone"),
        )

        result = SyncResult()
        files_found = {"/mnt/gcs/active.txt"}

        sync_service._sync_deletions(ctx, backend, files_found, result)

        assert result.files_deleted == 0
        mock_gateway.metadata_delete.assert_not_called()

    def test_skips_mount_point_itself(self, sync_service, mock_gateway):
        """The mount point directory itself is never deleted."""
        existing_meta = MagicMock()
        existing_meta.path = "/mnt/gcs"
        mock_gateway.metadata_list.return_value = [existing_meta]

        backend = MagicMock()
        backend.name = "test_backend"

        ctx = SyncContext(
            mount_point="/mnt/gcs",
            recursive=True,
            context=OperationContext(user_id="test_user", groups=[], zone_id="test_zone"),
        )

        result = SyncResult()
        files_found: set[str] = set()

        sync_service._sync_deletions(ctx, backend, files_found, result)

        assert result.files_deleted == 0

    def test_skips_paths_belonging_to_other_mounts(self, sync_service, mock_gateway):
        """Paths under other mounts are not deleted."""
        existing_meta = MagicMock()
        existing_meta.path = "/mnt/gcs/sub_mount/file.txt"
        mock_gateway.metadata_list.return_value = [existing_meta]

        # Report another mount at /mnt/gcs/sub_mount
        mock_gateway.list_mounts.return_value = [
            {"mount_point": "/mnt/gcs/sub_mount", "backend": MagicMock()},
        ]

        backend = MagicMock()
        backend.name = "test_backend"

        ctx = SyncContext(
            mount_point="/mnt/gcs",
            recursive=True,
            context=OperationContext(user_id="test_user", groups=[], zone_id="test_zone"),
        )

        result = SyncResult()
        files_found: set[str] = set()

        sync_service._sync_deletions(ctx, backend, files_found, result)

        assert result.files_deleted == 0

    def test_skips_when_path_is_specified(self, sync_service, mock_gateway):
        """Deletion check is skipped when syncing a specific path (not root)."""
        backend = MagicMock()
        backend.name = "test_backend"

        ctx = SyncContext(
            mount_point="/mnt/gcs",
            path="/mnt/gcs/subdir",
            recursive=True,
            context=OperationContext(user_id="test_user", groups=[], zone_id="test_zone"),
        )

        result = SyncResult()
        sync_service._sync_deletions(ctx, backend, set(), result)

        # Should not attempt to list metadata at all
        mock_gateway.metadata_list.assert_not_called()

    def test_handles_metadata_list_error_gracefully(self, sync_service, mock_gateway):
        """Errors during deletion check are captured in result.errors."""
        mock_gateway.metadata_list.side_effect = RuntimeError("DB error")

        backend = MagicMock()
        backend.name = "test_backend"

        ctx = SyncContext(
            mount_point="/mnt/gcs",
            recursive=True,
            context=OperationContext(user_id="test_user", groups=[], zone_id="test_zone"),
        )

        result = SyncResult()
        sync_service._sync_deletions(ctx, backend, set(), result)

        assert len(result.errors) == 1
        assert "Failed to check for deletions" in result.errors[0]


# =============================================================================
# Error handling tests
# =============================================================================


class TestSyncErrorHandling:
    """Tests for error handling during sync."""

    def test_backend_list_dir_failure_records_error(self, sync_service, mock_gateway):
        """Errors from backend.list_dir are captured in result.errors."""
        mount = mock_gateway.router.route.return_value
        backend = mount.backend
        backend.list_dir.side_effect = OSError("Connection failed")

        ctx = SyncContext(
            mount_point="/mnt/gcs",
            recursive=True,
            context=OperationContext(user_id="test_user", groups=[], zone_id="test_zone"),
        )

        result = sync_service.sync_mount(ctx)

        assert len(result.errors) > 0
        assert "Failed to scan" in result.errors[0]

    def test_metadata_put_failure_records_error(self, sync_service, mock_gateway):
        """Errors from metadata_put are captured in result.errors."""
        mount = mock_gateway.router.route.return_value
        backend = mount.backend
        backend.list_dir.return_value = ["file.txt"]
        mock_gateway.metadata_put.side_effect = RuntimeError("DB write failed")

        # Mock _get_file_size to avoid internal import issues
        sync_service._get_file_size = MagicMock(return_value=100)

        ctx = SyncContext(
            mount_point="/mnt/gcs",
            recursive=True,
            context=OperationContext(user_id="test_user", groups=[], zone_id="test_zone"),
        )

        result = sync_service.sync_mount(ctx)

        assert len(result.errors) > 0
        assert "Failed to add" in result.errors[0]


# =============================================================================
# _sync_all_mounts tests
# =============================================================================


class TestSyncAllMounts:
    """Tests for syncing all mounts when mount_point is None."""

    def test_sync_all_mounts_syncs_connectors(self, sync_service, mock_gateway):
        """When mount_point is None, all connector mounts are synced."""
        connector_backend = MagicMock()
        connector_backend.name = "connector"
        connector_backend.list_dir = MagicMock(return_value=[])

        non_connector_backend = MagicMock(spec=[])  # No list_dir

        mock_gateway.list_mounts.return_value = [
            {
                "mount_point": "/mnt/connector",
                "backend": connector_backend,
                "backend_type": "path_gcs",
            },
            {
                "mount_point": "/mnt/local",
                "backend": non_connector_backend,
                "backend_type": "local",
            },
        ]

        # Ensure route() works for the connector mount
        mount_info = MagicMock()
        mount_info.backend = connector_backend
        mock_gateway.router.route.return_value = mount_info

        ctx = SyncContext(mount_point=None, recursive=True)
        result = sync_service.sync_mount(ctx)

        assert result.mounts_synced == 1
        assert result.mounts_skipped == 1

    def test_sync_all_mounts_handles_errors(self, sync_service, mock_gateway):
        """Errors syncing one mount do not prevent syncing others."""
        connector_backend = MagicMock()
        connector_backend.name = "connector"
        connector_backend.list_dir = MagicMock(side_effect=RuntimeError("Fail"))

        mock_gateway.list_mounts.return_value = [
            {
                "mount_point": "/mnt/fail",
                "backend": connector_backend,
                "backend_type": "path_gcs",
            },
        ]

        mount_info = MagicMock()
        mount_info.backend = connector_backend
        mock_gateway.router.route.return_value = mount_info

        ctx = SyncContext(mount_point=None, recursive=True)
        result = sync_service.sync_mount(ctx)

        # The mount failed, but the overall operation did not crash
        assert len(result.errors) > 0


# =============================================================================
# _file_unchanged (delta sync) tests
# =============================================================================


class TestFileUnchanged:
    """Tests for delta sync change detection."""

    def test_matching_backend_version_is_unchanged(self, sync_service):
        """Files with the same backend version are considered unchanged."""
        file_info = MagicMock()
        file_info.backend_version = "gen123"
        file_info.size = 100
        file_info.mtime = None
        file_info.content_hash = None

        cached = ChangeLogEntry(
            path="/test",
            backend_name="gcs",
            backend_version="gen123",
        )

        assert sync_service._file_unchanged(file_info, cached) is True

    def test_different_backend_version_is_changed(self, sync_service):
        """Files with different backend versions are considered changed."""
        file_info = MagicMock()
        file_info.backend_version = "gen456"
        file_info.size = 100
        file_info.mtime = None
        file_info.content_hash = None

        cached = ChangeLogEntry(
            path="/test",
            backend_name="gcs",
            backend_version="gen123",
        )

        assert sync_service._file_unchanged(file_info, cached) is False

    def test_different_size_is_changed(self, sync_service):
        """Files with different sizes are considered changed."""
        file_info = MagicMock()
        file_info.backend_version = None
        file_info.size = 200
        file_info.mtime = datetime(2024, 1, 1, tzinfo=UTC)
        file_info.content_hash = None

        cached = ChangeLogEntry(
            path="/test",
            backend_name="gcs",
            size_bytes=100,
            mtime=datetime(2024, 1, 1, tzinfo=UTC),
        )

        assert sync_service._file_unchanged(file_info, cached) is False

    def test_matching_size_and_mtime_is_unchanged(self, sync_service):
        """Files with matching size and mtime are considered unchanged."""
        ts = datetime(2024, 1, 1, tzinfo=UTC)

        file_info = MagicMock()
        file_info.backend_version = None
        file_info.size = 100
        file_info.mtime = ts
        file_info.content_hash = None

        cached = ChangeLogEntry(
            path="/test",
            backend_name="gcs",
            size_bytes=100,
            mtime=ts,
        )

        assert sync_service._file_unchanged(file_info, cached) is True

    def test_no_comparison_data_assumes_changed(self, sync_service):
        """When no comparison data exists, file is assumed changed."""
        file_info = MagicMock()
        file_info.backend_version = None
        file_info.size = None
        file_info.mtime = None
        file_info.content_hash = None

        cached = ChangeLogEntry(path="/test", backend_name="gcs")

        assert sync_service._file_unchanged(file_info, cached) is False


# =============================================================================
# SyncResult dataclass tests
# =============================================================================


class TestSyncResult:
    """Tests for SyncResult dataclass."""

    def test_default_values(self):
        """SyncResult has sensible defaults."""
        result = SyncResult()
        assert result.files_scanned == 0
        assert result.files_created == 0
        assert result.files_updated == 0
        assert result.files_deleted == 0
        assert result.files_skipped == 0
        assert result.errors == []

    def test_to_dict(self):
        """SyncResult.to_dict produces a complete dictionary."""
        result = SyncResult(files_scanned=10, files_created=5)
        d = result.to_dict()
        assert d["files_scanned"] == 10
        assert d["files_created"] == 5
        assert "errors" in d


# =============================================================================
# Pattern matching tests
# =============================================================================


class TestMatchesPatterns:
    """Tests for include/exclude pattern filtering."""

    def test_no_patterns_matches_everything(self, sync_service):
        """Without patterns, all files match."""
        ctx = SyncContext(mount_point="/mnt/test")
        assert sync_service._matches_patterns("/mnt/test/file.txt", ctx) is True

    def test_include_pattern_filters_files(self, sync_service):
        """Include patterns restrict which files are synced."""
        ctx = SyncContext(mount_point="/mnt/test", include_patterns=["*.py"])
        assert sync_service._matches_patterns("/mnt/test/script.py", ctx) is True
        assert sync_service._matches_patterns("/mnt/test/readme.md", ctx) is False

    def test_exclude_pattern_filters_files(self, sync_service):
        """Exclude patterns prevent certain files from being synced."""
        ctx = SyncContext(mount_point="/mnt/test", exclude_patterns=["*.pyc"])
        assert sync_service._matches_patterns("/mnt/test/module.pyc", ctx) is False
        assert sync_service._matches_patterns("/mnt/test/module.py", ctx) is True


# =============================================================================
# SyncContext tests
# =============================================================================


class TestSyncContext:
    """Tests for SyncContext defaults."""

    def test_default_values(self):
        """SyncContext has correct defaults."""
        ctx = SyncContext(mount_point="/mnt/test")
        assert ctx.recursive is True
        assert ctx.dry_run is False
        assert ctx.sync_content is True
        assert ctx.include_patterns is None
        assert ctx.exclude_patterns is None
        assert ctx.full_sync is False


# =============================================================================
# list_dir_metadata protocol tests (Issue #3266)
# =============================================================================


class TestListDirMetadataProtocol:
    """Tests for the generic list_dir_metadata protocol in the sync service."""

    def test_sync_calls_list_dir_metadata_when_available(
        self, sync_service, sync_context, mock_gateway
    ):
        """Sync service calls list_dir_metadata when the backend implements it."""
        mount = mock_gateway.router.route.return_value
        backend = mount.backend
        backend.list_dir.return_value = ["msg1.yaml", "msg2.yaml"]
        backend.list_dir_metadata = MagicMock(
            return_value={
                "msg1.yaml": {"subject": "Hello", "date": "2026-03-20"},
                "msg2.yaml": {"subject": "World", "date": "2026-03-21"},
            }
        )
        # Avoid get_sync_provider trying to use the generic provider
        backend.get_sync_provider = MagicMock(return_value=None)

        sync_service._get_file_size = MagicMock(return_value=1024)
        sync_service.sync_mount(sync_context)

        backend.list_dir_metadata.assert_called_once()

    def test_sync_works_without_list_dir_metadata(self, sync_service, sync_context, mock_gateway):
        """Sync service works normally when backend has no list_dir_metadata."""
        mount = mock_gateway.router.route.return_value
        backend = mount.backend
        backend.list_dir.return_value = ["file.txt"]
        # Ensure no list_dir_metadata attribute
        if hasattr(backend, "list_dir_metadata"):
            delattr(backend, "list_dir_metadata")

        sync_service._get_file_size = MagicMock(return_value=100)
        result = sync_service.sync_mount(sync_context)

        assert result.files_scanned == 1
        assert result.files_created == 1

    def test_sync_handles_list_dir_metadata_returning_none(
        self, sync_service, sync_context, mock_gateway
    ):
        """When list_dir_metadata returns None, sync continues via slow path."""
        mount = mock_gateway.router.route.return_value
        backend = mount.backend
        backend.list_dir.return_value = ["file.txt"]
        backend.list_dir_metadata = MagicMock(return_value=None)

        sync_service._get_file_size = MagicMock(return_value=100)
        result = sync_service.sync_mount(sync_context)

        assert result.files_scanned == 1
        assert result.files_created == 1

    def test_sync_handles_list_dir_metadata_exception(
        self, sync_service, sync_context, mock_gateway
    ):
        """When list_dir_metadata raises, sync falls back gracefully."""
        mount = mock_gateway.router.route.return_value
        backend = mount.backend
        backend.list_dir.return_value = ["file.txt"]
        backend.list_dir_metadata = MagicMock(side_effect=RuntimeError("API error"))

        sync_service._get_file_size = MagicMock(return_value=100)
        result = sync_service.sync_mount(sync_context)

        assert result.files_scanned == 1
        assert result.files_created == 1

    def test_dir_metadata_passed_to_apply_display_path(
        self, sync_service, sync_context, mock_gateway
    ):
        """dir_metadata from list_dir_metadata is forwarded to _apply_display_path_for_sync."""
        # Test via _apply_display_path_for_sync directly to avoid mock class issues.
        backend = MagicMock()
        backend.display_path.return_value = "2026-03/2026-03-25_Test.yaml"

        ctx = MagicMock()
        ctx.mount_point = "/mnt/test"

        dir_metadata = {
            "msg1.yaml": {"subject": "Test", "date": "2026-03-25"},
        }

        result = sync_service._apply_display_path_for_sync(
            backend,
            "/mnt/test/msg1.yaml",
            "msg1.yaml",
            ctx,
            dir_metadata=dir_metadata,
        )

        # display_path should have been called with the batch metadata
        backend.display_path.assert_called_once_with("msg1", dir_metadata["msg1.yaml"])
        assert result == "/mnt/test/2026-03/2026-03-25_Test.yaml"


# =============================================================================
# _apply_display_path_for_sync with dir_metadata tests
# =============================================================================


class TestApplyDisplayPathWithDirMetadata:
    """Tests for _apply_display_path_for_sync using dir_metadata fast path."""

    def test_fast_path_uses_dir_metadata(self, sync_service):
        """Fast path: dir_metadata lookup avoids read_content call."""
        backend = MagicMock()
        backend.display_path.return_value = "INBOX/PRIMARY/2026-03-20_Meeting.yaml"

        ctx = MagicMock()
        ctx.mount_point = "/mnt/gmail"

        dir_metadata = {
            "thread1-msg1.yaml": {
                "subject": "Meeting",
                "date": "2026-03-20",
                "labels": ["INBOX"],
            }
        }

        result = sync_service._apply_display_path_for_sync(
            backend,
            "/mnt/gmail/INBOX/PRIMARY/thread1-msg1.yaml",
            "INBOX/PRIMARY/thread1-msg1.yaml",
            ctx,
            dir_metadata=dir_metadata,
        )

        assert result == "/mnt/gmail/INBOX/PRIMARY/2026-03-20_Meeting.yaml"
        backend.display_path.assert_called_once()
        backend.read_content.assert_not_called()

    def test_fast_path_extracts_gmail_msg_id(self, sync_service):
        """For Gmail threadId-msgId format, passes just msg_id to display_path."""
        backend = MagicMock()
        backend.display_path.return_value = "INBOX/PRIMARY/2026-03-20_Hello.yaml"

        ctx = MagicMock()
        ctx.mount_point = "/mnt/gmail"

        dir_metadata = {
            "tid123-mid456.yaml": {"subject": "Hello", "date": "2026-03-20"},
        }

        sync_service._apply_display_path_for_sync(
            backend,
            "/mnt/gmail/INBOX/tid123-mid456.yaml",
            "INBOX/tid123-mid456.yaml",
            ctx,
            dir_metadata=dir_metadata,
        )

        # Should be called with just the msg_id portion
        backend.display_path.assert_called_once_with("mid456", dir_metadata["tid123-mid456.yaml"])

    def test_slow_path_when_dir_metadata_none(self, sync_service):
        """Slow path: when dir_metadata is None, falls back to read_content."""
        backend = MagicMock()
        backend.display_path.return_value = "primary/2026-03/event.yaml"
        backend.read_content.return_value = (
            b"summary: event\ncalendarId: primary\nstart:\n  dateTime: '2026-03-21T10:00:00Z'\n"
        )

        ctx = MagicMock()
        ctx.mount_point = "/mnt/cal"
        ctx.context = None

        result = sync_service._apply_display_path_for_sync(
            backend,
            "/mnt/cal/primary/2026-03/evt123.yaml",
            "primary/2026-03/evt123.yaml",
            ctx,
            dir_metadata=None,
        )

        assert result == "/mnt/cal/primary/2026-03/event.yaml"
        backend.read_content.assert_called_once()

    def test_slow_path_when_file_not_in_dir_metadata(self, sync_service):
        """Slow path: file not found in dir_metadata, falls back to read_content."""
        backend = MagicMock()
        backend.display_path.return_value = "folder/file.yaml"
        backend.read_content.return_value = b"summary: file\n"

        ctx = MagicMock()
        ctx.mount_point = "/mnt/test"
        ctx.context = None

        dir_metadata = {
            "other-file.yaml": {"subject": "Other"},
        }

        result = sync_service._apply_display_path_for_sync(
            backend,
            "/mnt/test/missing.yaml",
            "missing.yaml",
            ctx,
            dir_metadata=dir_metadata,
        )

        assert result == "/mnt/test/folder/file.yaml"
        backend.read_content.assert_called_once()

    def test_default_display_path_no_rewrite(self, sync_service):
        """When display_path returns default, virtual_path is unchanged."""
        backend = MagicMock()
        backend.display_path.return_value = "msg1.yaml"

        ctx = MagicMock()
        ctx.mount_point = "/mnt/test"

        dir_metadata = {
            "msg1.yaml": {"subject": ""},
        }

        result = sync_service._apply_display_path_for_sync(
            backend,
            "/mnt/test/msg1.yaml",
            "msg1.yaml",
            ctx,
            dir_metadata=dir_metadata,
        )

        assert result == "/mnt/test/msg1.yaml"
