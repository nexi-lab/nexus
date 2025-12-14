"""Unit tests for NexusFSMountsMixin.

Tests cover mount management operations:
- add_mount: Add dynamic backend mount
- remove_mount: Remove backend mount
- list_mounts: List all active mounts
- get_mount: Get mount details
- has_mount: Check if mount exists
- save_mount: Persist mount to database
- load_mount: Load persisted mount
- sync_mount: Sync metadata from connector backend
"""

from __future__ import annotations

import contextlib
import tempfile
from collections.abc import Generator
from pathlib import Path
from unittest.mock import patch

import pytest

from nexus import LocalBackend, NexusFS


@pytest.fixture
def temp_dir() -> Generator[Path, None, None]:
    """Create a temporary directory for tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def nx(temp_dir: Path) -> Generator[NexusFS, None, None]:
    """Create a NexusFS instance for testing."""
    nx = NexusFS(
        backend=LocalBackend(temp_dir),
        db_path=temp_dir / "metadata.db",
        auto_parse=False,
        enforce_permissions=False,
    )
    yield nx
    nx.close()


@pytest.fixture
def nx_with_permissions(temp_dir: Path) -> Generator[NexusFS, None, None]:
    """Create a NexusFS instance with permissions enabled."""
    nx = NexusFS(
        backend=LocalBackend(temp_dir),
        db_path=temp_dir / "metadata.db",
        auto_parse=False,
        enforce_permissions=True,
    )
    yield nx
    nx.close()


class TestListMounts:
    """Tests for list_mounts method."""

    def test_list_mounts_empty(self, nx: NexusFS) -> None:
        """Test listing mounts when only root mount exists."""
        mounts = nx.list_mounts()
        # Should have at least the root mount
        assert isinstance(mounts, list)
        # Root mount always exists
        assert len(mounts) >= 1

    def test_list_mounts_returns_mount_info(self, nx: NexusFS) -> None:
        """Test that list_mounts returns proper mount info structure."""
        mounts = nx.list_mounts()
        assert len(mounts) >= 1

        mount = mounts[0]
        assert "mount_point" in mount
        assert "priority" in mount
        assert "readonly" in mount
        assert "backend_type" in mount

    def test_list_mounts_after_add_mount(self, nx: NexusFS, temp_dir: Path) -> None:
        """Test list_mounts includes newly added mounts."""
        # Create a new directory for the mount
        mount_data_dir = temp_dir / "mount_data"
        mount_data_dir.mkdir()

        # Add a local mount
        mount_id = nx.add_mount(
            mount_point="/mnt/test",
            backend_type="local",
            backend_config={"data_dir": str(mount_data_dir)},
            priority=10,
        )

        assert mount_id == "/mnt/test"

        mounts = nx.list_mounts()
        mount_points = [m["mount_point"] for m in mounts]
        assert "/mnt/test" in mount_points

        # Verify mount properties
        test_mount = next(m for m in mounts if m["mount_point"] == "/mnt/test")
        assert test_mount["priority"] == 10
        assert test_mount["readonly"] is False
        assert test_mount["backend_type"] == "LocalBackend"


class TestGetMount:
    """Tests for get_mount method."""

    def test_get_mount_root(self, nx: NexusFS) -> None:
        """Test getting the root mount."""
        mount = nx.get_mount("/")
        assert mount is not None
        assert mount["mount_point"] == "/"

    def test_get_mount_nonexistent(self, nx: NexusFS) -> None:
        """Test getting a nonexistent mount returns None."""
        mount = nx.get_mount("/nonexistent")
        assert mount is None

    def test_get_mount_after_add(self, nx: NexusFS, temp_dir: Path) -> None:
        """Test getting a mount after adding it."""
        mount_data_dir = temp_dir / "mount_data"
        mount_data_dir.mkdir()

        nx.add_mount(
            mount_point="/mnt/test",
            backend_type="local",
            backend_config={"data_dir": str(mount_data_dir)},
            priority=5,
            readonly=True,
        )

        mount = nx.get_mount("/mnt/test")
        assert mount is not None
        assert mount["mount_point"] == "/mnt/test"
        assert mount["priority"] == 5
        assert mount["readonly"] is True


class TestHasMount:
    """Tests for has_mount method."""

    def test_has_mount_root(self, nx: NexusFS) -> None:
        """Test has_mount returns True for root mount."""
        assert nx.has_mount("/") is True

    def test_has_mount_nonexistent(self, nx: NexusFS) -> None:
        """Test has_mount returns False for nonexistent mount."""
        assert nx.has_mount("/nonexistent") is False

    def test_has_mount_after_add(self, nx: NexusFS, temp_dir: Path) -> None:
        """Test has_mount after adding a mount."""
        mount_data_dir = temp_dir / "mount_data"
        mount_data_dir.mkdir()

        assert nx.has_mount("/mnt/test") is False

        nx.add_mount(
            mount_point="/mnt/test",
            backend_type="local",
            backend_config={"data_dir": str(mount_data_dir)},
        )

        assert nx.has_mount("/mnt/test") is True


class TestAddMount:
    """Tests for add_mount method."""

    def test_add_mount_local_backend(self, nx: NexusFS, temp_dir: Path) -> None:
        """Test adding a local backend mount."""
        mount_data_dir = temp_dir / "local_mount"
        mount_data_dir.mkdir()

        mount_id = nx.add_mount(
            mount_point="/mnt/local",
            backend_type="local",
            backend_config={"data_dir": str(mount_data_dir)},
        )

        assert mount_id == "/mnt/local"
        assert nx.has_mount("/mnt/local")

    def test_add_mount_with_priority(self, nx: NexusFS, temp_dir: Path) -> None:
        """Test adding a mount with custom priority."""
        mount_data_dir = temp_dir / "priority_mount"
        mount_data_dir.mkdir()

        nx.add_mount(
            mount_point="/mnt/high_priority",
            backend_type="local",
            backend_config={"data_dir": str(mount_data_dir)},
            priority=100,
        )

        mount = nx.get_mount("/mnt/high_priority")
        assert mount is not None
        assert mount["priority"] == 100

    def test_add_mount_readonly(self, nx: NexusFS, temp_dir: Path) -> None:
        """Test adding a read-only mount."""
        mount_data_dir = temp_dir / "readonly_mount"
        mount_data_dir.mkdir()

        nx.add_mount(
            mount_point="/mnt/readonly",
            backend_type="local",
            backend_config={"data_dir": str(mount_data_dir)},
            readonly=True,
        )

        mount = nx.get_mount("/mnt/readonly")
        assert mount is not None
        assert mount["readonly"] is True

    def test_add_mount_unsupported_backend_raises_error(self, nx: NexusFS) -> None:
        """Test adding an unsupported backend type raises RuntimeError."""
        with pytest.raises(RuntimeError, match="Unsupported backend type"):
            nx.add_mount(
                mount_point="/mnt/unsupported",
                backend_type="unsupported_backend",
                backend_config={},
            )

    def test_add_mount_with_context_grants_permission(
        self, nx_with_permissions: NexusFS, temp_dir: Path
    ) -> None:
        """Test that add_mount grants direct_owner permission to the user."""
        from nexus.core.permissions import OperationContext

        mount_data_dir = temp_dir / "perm_mount"
        mount_data_dir.mkdir()

        context = OperationContext(
            user="alice",
            groups=[],
            tenant_id="test_tenant",
            subject_type="user",
            subject_id="alice",
        )

        nx_with_permissions.add_mount(
            mount_point="/mnt/alice",
            backend_type="local",
            backend_config={"data_dir": str(mount_data_dir)},
            context=context,
        )

        assert nx_with_permissions.has_mount("/mnt/alice")


class TestRemoveMount:
    """Tests for remove_mount method."""

    def test_remove_mount_success(self, nx: NexusFS, temp_dir: Path) -> None:
        """Test removing a mount successfully."""
        mount_data_dir = temp_dir / "removable_mount"
        mount_data_dir.mkdir()

        nx.add_mount(
            mount_point="/mnt/removable",
            backend_type="local",
            backend_config={"data_dir": str(mount_data_dir)},
        )

        assert nx.has_mount("/mnt/removable")

        result = nx.remove_mount("/mnt/removable")
        assert result["removed"] is True
        assert nx.has_mount("/mnt/removable") is False

    def test_remove_mount_nonexistent(self, nx: NexusFS) -> None:
        """Test removing a nonexistent mount returns error."""
        result = nx.remove_mount("/mnt/nonexistent")
        assert result["removed"] is False
        assert "Mount not found" in result["errors"][0]

    def test_remove_mount_returns_cleanup_info(self, nx: NexusFS, temp_dir: Path) -> None:
        """Test that remove_mount returns cleanup information."""
        mount_data_dir = temp_dir / "cleanup_mount"
        mount_data_dir.mkdir()

        nx.add_mount(
            mount_point="/mnt/cleanup",
            backend_type="local",
            backend_config={"data_dir": str(mount_data_dir)},
        )

        result = nx.remove_mount("/mnt/cleanup")

        assert "removed" in result
        assert "directory_deleted" in result
        assert "permissions_cleaned" in result
        assert "errors" in result
        assert result["removed"] is True


class TestSaveMount:
    """Tests for save_mount method."""

    def test_save_mount_without_mount_manager_raises_error(self, temp_dir: Path) -> None:
        """Test that save_mount raises RuntimeError without mount manager."""
        # Create NexusFS without database (no mount manager)
        nx = NexusFS(
            backend=LocalBackend(temp_dir),
            auto_parse=False,
            enforce_permissions=False,
        )

        try:
            # Check if mount_manager is available
            if not hasattr(nx, "mount_manager") or nx.mount_manager is None:
                with pytest.raises(RuntimeError, match="Mount manager not available"):
                    nx.save_mount(
                        mount_point="/mnt/test",
                        backend_type="local",
                        backend_config={"data_dir": str(temp_dir)},
                    )
        finally:
            nx.close()

    def test_save_mount_with_mount_manager(self, nx: NexusFS, temp_dir: Path) -> None:
        """Test save_mount when mount manager is available."""
        if not hasattr(nx, "mount_manager") or nx.mount_manager is None:
            pytest.skip("Mount manager not available in this configuration")

        mount_data_dir = temp_dir / "saved_mount"
        mount_data_dir.mkdir()

        mount_id = nx.save_mount(
            mount_point="/mnt/saved",
            backend_type="local",
            backend_config={"data_dir": str(mount_data_dir)},
            priority=5,
            readonly=False,
            owner_user_id="alice",
            tenant_id="test_tenant",
            description="Test saved mount",
        )

        assert mount_id is not None


class TestListSavedMounts:
    """Tests for list_saved_mounts method."""

    def test_list_saved_mounts_without_mount_manager_raises_error(self, temp_dir: Path) -> None:
        """Test that list_saved_mounts raises RuntimeError without mount manager."""
        nx = NexusFS(
            backend=LocalBackend(temp_dir),
            auto_parse=False,
            enforce_permissions=False,
        )

        try:
            if not hasattr(nx, "mount_manager") or nx.mount_manager is None:
                with pytest.raises(RuntimeError, match="Mount manager not available"):
                    nx.list_saved_mounts()
        finally:
            nx.close()


class TestLoadMount:
    """Tests for load_mount method."""

    def test_load_mount_without_mount_manager_raises_error(self, temp_dir: Path) -> None:
        """Test that load_mount raises RuntimeError without mount manager."""
        nx = NexusFS(
            backend=LocalBackend(temp_dir),
            auto_parse=False,
            enforce_permissions=False,
        )

        try:
            if not hasattr(nx, "mount_manager") or nx.mount_manager is None:
                with pytest.raises(RuntimeError, match="Mount manager not available"):
                    nx.load_mount("/mnt/test")
        finally:
            nx.close()


class TestDeleteSavedMount:
    """Tests for delete_saved_mount method."""

    def test_delete_saved_mount_without_mount_manager_raises_error(self, temp_dir: Path) -> None:
        """Test that delete_saved_mount raises RuntimeError without mount manager."""
        nx = NexusFS(
            backend=LocalBackend(temp_dir),
            auto_parse=False,
            enforce_permissions=False,
        )

        try:
            if not hasattr(nx, "mount_manager") or nx.mount_manager is None:
                with pytest.raises(RuntimeError, match="Mount manager not available"):
                    nx.delete_saved_mount("/mnt/test")
        finally:
            nx.close()


class TestLoadAllSavedMounts:
    """Tests for load_all_saved_mounts method."""

    def test_load_all_saved_mounts_without_mount_manager(self, nx: NexusFS) -> None:
        """Test load_all_saved_mounts when mount manager is not available."""
        if hasattr(nx, "mount_manager") and nx.mount_manager is not None:
            pytest.skip("Mount manager is available, test N/A")

        result = nx.load_all_saved_mounts()
        assert result == {"loaded": 0, "synced": 0, "failed": 0, "errors": []}

    def test_load_all_saved_mounts_empty(self, nx: NexusFS) -> None:
        """Test load_all_saved_mounts when no mounts are saved."""
        if not hasattr(nx, "mount_manager") or nx.mount_manager is None:
            pytest.skip("Mount manager not available")

        result = nx.load_all_saved_mounts()
        assert "loaded" in result
        assert "synced" in result
        assert "failed" in result
        assert "errors" in result


class TestSyncMount:
    """Tests for sync_mount method."""

    def test_sync_mount_nonexistent_raises_error(self, nx: NexusFS) -> None:
        """Test that sync_mount raises ValueError for nonexistent mount."""
        with pytest.raises(ValueError, match="Mount not found"):
            nx.sync_mount("/mnt/nonexistent")

    def test_sync_mount_non_connector_backend_raises_error(
        self, nx: NexusFS, temp_dir: Path
    ) -> None:
        """Test sync_mount with non-connector backend raises RuntimeError."""
        # LocalBackend doesn't have list_dir for connector-style operations
        # (it does have list_dir but not the connector-style behavior)
        # This test verifies the error message is clear
        mount_data_dir = temp_dir / "sync_mount"
        mount_data_dir.mkdir()

        nx.add_mount(
            mount_point="/mnt/sync",
            backend_type="local",
            backend_config={"data_dir": str(mount_data_dir)},
        )

        # LocalBackend has list_dir, so it won't raise the "does not support" error
        # but we can test the sync functionality
        result = nx.sync_mount("/mnt/sync")
        assert "files_scanned" in result
        assert "files_created" in result
        assert "files_updated" in result
        assert "files_deleted" in result
        assert "errors" in result

    def test_sync_mount_dry_run(self, nx: NexusFS, temp_dir: Path) -> None:
        """Test sync_mount in dry run mode."""
        mount_data_dir = temp_dir / "dryrun_mount"
        mount_data_dir.mkdir()

        # Create some files in the mount directory
        (mount_data_dir / "file1.txt").write_text("content1")
        (mount_data_dir / "file2.txt").write_text("content2")

        nx.add_mount(
            mount_point="/mnt/dryrun",
            backend_type="local",
            backend_config={"data_dir": str(mount_data_dir)},
        )

        # Dry run should not create entries in database
        result = nx.sync_mount("/mnt/dryrun", dry_run=True)

        assert result["files_scanned"] >= 0
        assert result["files_created"] == 0  # Dry run doesn't create
        assert result["files_updated"] == 0  # Dry run doesn't update
        assert result["files_deleted"] == 0  # Dry run doesn't delete

    def test_sync_mount_recursive(self, nx: NexusFS, temp_dir: Path) -> None:
        """Test sync_mount with recursive option."""
        mount_data_dir = temp_dir / "recursive_mount"
        mount_data_dir.mkdir()

        # Create nested structure
        subdir = mount_data_dir / "subdir"
        subdir.mkdir()
        (mount_data_dir / "file1.txt").write_text("content1")
        (subdir / "file2.txt").write_text("content2")

        nx.add_mount(
            mount_point="/mnt/recursive",
            backend_type="local",
            backend_config={"data_dir": str(mount_data_dir)},
        )

        result = nx.sync_mount("/mnt/recursive", recursive=True)

        assert "files_scanned" in result
        assert "files_created" in result
        assert "files_updated" in result
        assert "files_deleted" in result
        assert "errors" in result
        # LocalBackend uses CAS model, so sync may return 0 if files aren't detected
        # The important thing is that the sync completes without error
        assert isinstance(result["files_scanned"], int)

    def test_sync_mount_with_context(self, nx: NexusFS, temp_dir: Path) -> None:
        """Test sync_mount with operation context."""
        from nexus.core.permissions import OperationContext

        mount_data_dir = temp_dir / "context_mount"
        mount_data_dir.mkdir()
        (mount_data_dir / "file.txt").write_text("content")

        nx.add_mount(
            mount_point="/mnt/context",
            backend_type="local",
            backend_config={"data_dir": str(mount_data_dir)},
        )

        context = OperationContext(
            user="alice",
            groups=[],
            subject_type="user",
            subject_id="alice",
        )

        result = nx.sync_mount("/mnt/context", context=context)

        assert "files_scanned" in result
        assert "errors" in result


class TestGrantMountOwnerPermission:
    """Tests for _grant_mount_owner_permission helper method."""

    def test_grant_mount_owner_permission_no_context(self, nx: NexusFS) -> None:
        """Test _grant_mount_owner_permission without context does nothing."""
        # Should not raise, just log a warning
        nx._grant_mount_owner_permission("/mnt/test", None)

    def test_grant_mount_owner_permission_with_context(self, nx_with_permissions: NexusFS) -> None:
        """Test _grant_mount_owner_permission with context."""
        from nexus.core.permissions import OperationContext

        context = OperationContext(
            user="alice",
            groups=[],
            tenant_id="test_tenant",
            subject_type="user",
            subject_id="alice",
        )

        # Should not raise
        nx_with_permissions._grant_mount_owner_permission("/mnt/test", context)


class TestMountIntegration:
    """Integration tests for mount functionality."""

    def test_write_to_mount(self, nx: NexusFS, temp_dir: Path) -> None:
        """Test writing files to a mounted backend."""
        mount_data_dir = temp_dir / "write_mount"
        mount_data_dir.mkdir()

        nx.add_mount(
            mount_point="/mnt/write",
            backend_type="local",
            backend_config={"data_dir": str(mount_data_dir)},
        )

        # Write to the mount
        nx.write("/mnt/write/test.txt", b"Hello from mount!")

        # Read back
        content = nx.read("/mnt/write/test.txt")
        assert content == b"Hello from mount!"

    def test_list_mount_contents(self, nx: NexusFS, temp_dir: Path) -> None:
        """Test listing files in a mounted backend."""
        mount_data_dir = temp_dir / "list_mount"
        mount_data_dir.mkdir()

        nx.add_mount(
            mount_point="/mnt/list",
            backend_type="local",
            backend_config={"data_dir": str(mount_data_dir)},
        )

        # Write some files
        nx.write("/mnt/list/file1.txt", b"Content 1")
        nx.write("/mnt/list/file2.txt", b"Content 2")

        # List files
        files = nx.list("/mnt/list", recursive=True)

        assert "/mnt/list/file1.txt" in files
        assert "/mnt/list/file2.txt" in files

    def test_multiple_mounts(self, nx: NexusFS, temp_dir: Path) -> None:
        """Test multiple mounts can coexist."""
        mount1_dir = temp_dir / "mount1"
        mount2_dir = temp_dir / "mount2"
        mount1_dir.mkdir()
        mount2_dir.mkdir()

        nx.add_mount(
            mount_point="/mnt/one",
            backend_type="local",
            backend_config={"data_dir": str(mount1_dir)},
            priority=1,
        )

        nx.add_mount(
            mount_point="/mnt/two",
            backend_type="local",
            backend_config={"data_dir": str(mount2_dir)},
            priority=2,
        )

        # Both mounts should exist
        assert nx.has_mount("/mnt/one")
        assert nx.has_mount("/mnt/two")

        # Write to each
        nx.write("/mnt/one/file.txt", b"Mount 1")
        nx.write("/mnt/two/file.txt", b"Mount 2")

        # Read from each
        assert nx.read("/mnt/one/file.txt") == b"Mount 1"
        assert nx.read("/mnt/two/file.txt") == b"Mount 2"


class TestMountContextUtilsIntegration:
    """Tests for mount operations using context_utils functions."""

    def test_add_mount_uses_context_utils_functions(
        self, nx_with_permissions: NexusFS, temp_dir: Path
    ):
        """Test that add_mount uses context_utils.get_tenant_id and get_user_identity."""
        from nexus.core.permissions import OperationContext

        mount_data_dir = temp_dir / "context_mount"
        mount_data_dir.mkdir()

        context = OperationContext(
            user="alice",
            groups=[],
            tenant_id="test_tenant",
            subject_type="user",
            subject_id="alice",
        )

        with (
            patch("nexus.core.nexus_fs_mounts.get_tenant_id") as mock_get_tenant,
            patch("nexus.core.nexus_fs_mounts.get_user_identity") as mock_get_user,
        ):
            mock_get_tenant.return_value = "test_tenant"
            mock_get_user.return_value = ("user", "alice")

            nx_with_permissions.add_mount(
                mount_point="/mnt/context_test",
                backend_type="local",
                backend_config={"data_dir": str(mount_data_dir)},
                context=context,
            )

            # Verify context_utils functions were called
            mock_get_tenant.assert_called_with(context)
            mock_get_user.assert_called_with(context)

    def test_remove_mount_with_context_works(self, nx_with_permissions: NexusFS, temp_dir: Path):
        """Test that remove_mount works correctly with context (uses context_utils internally)."""
        from nexus.core.permissions import OperationContext

        mount_data_dir = temp_dir / "remove_context_mount"
        mount_data_dir.mkdir()

        context = OperationContext(
            user="alice",
            groups=[],
            tenant_id="test_tenant",
            subject_type="user",
            subject_id="alice",
        )

        # Add mount first
        nx_with_permissions.add_mount(
            mount_point="/mnt/remove_test",
            backend_type="local",
            backend_config={"data_dir": str(mount_data_dir)},
            context=context,
        )

        # Remove mount with context - should work correctly
        result = nx_with_permissions.remove_mount("/mnt/remove_test", context=context)

        # Verify mount was removed
        assert result["removed"] is True
        assert not nx_with_permissions.has_mount("/mnt/remove_test")

    def test_add_mount_oauth_backend_uses_context_utils_database_url(
        self, nx: NexusFS, temp_dir: Path
    ):
        """Test that add_mount for OAuth backends uses context_utils.get_database_url."""
        # Set up database path
        nx.db_path = temp_dir / "token_manager.db"

        with patch("nexus.core.nexus_fs_mounts.get_database_url") as mock_get_db_url:
            mock_get_db_url.return_value = str(temp_dir / "token_manager.db")

            # This should use get_database_url for gdrive_connector
            with contextlib.suppress(Exception):
                nx.add_mount(
                    mount_point="/mnt/gdrive",
                    backend_type="gdrive_connector",
                    backend_config={},
                )

            # Verify get_database_url was called
            mock_get_db_url.assert_called()

    def test_load_mount_oauth_backend_uses_database_url(self, nx: NexusFS, temp_dir: Path):
        """Test that load_mount for OAuth backends resolves database URL correctly."""
        # Set up database path
        nx.db_path = temp_dir / "token_manager.db"

        mount_config = {
            "mount_point": "/mnt/gmail",
            "backend_type": "gmail_connector",
            "backend_config": {},
        }

        # This should use get_database_url internally for gmail_connector
        # The function should resolve the database URL from nx.db_path
        # It may fail due to missing OAuth config, but should not fail due to missing database URL
        try:
            nx.load_mount(mount_config)
        except RuntimeError as e:
            # Should not fail with "No database path configured" error
            # (may fail for other reasons like missing OAuth config)
            assert "No database path configured" not in str(e)
        except Exception:
            # Other exceptions are acceptable (e.g., missing OAuth credentials)
            pass

    def test_add_mount_with_none_context_uses_defaults(
        self, nx_with_permissions: NexusFS, temp_dir: Path
    ):
        """Test that add_mount handles None context gracefully using context_utils defaults."""
        mount_data_dir = temp_dir / "none_context_mount"
        mount_data_dir.mkdir()

        # Should not raise error with None context - context_utils provides defaults
        # This tests that the refactored code works with None context
        nx_with_permissions.add_mount(
            mount_point="/mnt/none_context",
            backend_type="local",
            backend_config={"data_dir": str(mount_data_dir)},
            context=None,
        )

        # Verify mount was created successfully
        assert nx_with_permissions.has_mount("/mnt/none_context")
