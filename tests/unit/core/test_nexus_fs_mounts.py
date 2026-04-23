"""Unit tests for mount management via direct service access.

Tests cover mount management operations via mount_service,
_mount_persist_service, and _sync_service (replacing old __getattr__ routing):
- add_mount_sync: Add dynamic backend mount (MountService)
- remove_mount_sync: Remove backend mount (MountService)
- list_mounts_sync: List all active mounts (MountService)
- get_mount_sync: Get mount details (MountService)
- has_mount_sync: Check if mount exists (MountService)
- save_mount: Persist mount to database (MountPersistService)
- load_mount: Load persisted mount (MountPersistService)
- sync_mount: Sync metadata from connector backend (SyncService)
"""

from __future__ import annotations

import asyncio
import contextlib
import tempfile
from collections.abc import Generator
from pathlib import Path
from unittest.mock import patch

import pytest

from nexus import CASLocalBackend, NexusFS
from nexus.core.config import ParseConfig, PermissionConfig
from nexus.factory import create_nexus_fs
from nexus.storage.raft_metadata_store import RaftMetadataStore
from nexus.storage.record_store import SQLAlchemyRecordStore


@pytest.fixture
def temp_dir() -> Generator[Path, None, None]:
    """Create a temporary directory for tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def nx(temp_dir: Path) -> Generator[NexusFS, None, None]:
    """Create a NexusFS instance for testing."""
    nx = asyncio.run(
        create_nexus_fs(
            backend=CASLocalBackend(temp_dir),
            metadata_store=RaftMetadataStore.embedded(str(temp_dir / "raft-metadata")),
            record_store=SQLAlchemyRecordStore(db_path=temp_dir / "metadata.db"),
            parsing=ParseConfig(auto_parse=False),
            permissions=PermissionConfig(enforce=False),
        )
    )
    yield nx
    nx.close()


@pytest.fixture
def nx_with_permissions(temp_dir: Path) -> Generator[NexusFS, None, None]:
    """Create a NexusFS instance with permissions enabled."""
    nx = asyncio.run(
        create_nexus_fs(
            backend=CASLocalBackend(temp_dir),
            metadata_store=RaftMetadataStore.embedded(str(temp_dir / "raft-metadata-perms")),
            record_store=SQLAlchemyRecordStore(db_path=temp_dir / "metadata.db"),
            parsing=ParseConfig(auto_parse=False),
            permissions=PermissionConfig(enforce=True),
        )
    )
    yield nx
    nx.close()


class TestListMounts:
    """Tests for list_mounts method."""

    def test_list_mounts_empty(self, nx: NexusFS) -> None:
        """Test listing mounts when only root mount exists."""
        mounts = nx.service("mount").list_mounts_sync()
        # Should have at least the root mount
        assert isinstance(mounts, list)
        # Root mount always exists
        assert len(mounts) >= 1

    def test_list_mounts_returns_mount_info(self, nx: NexusFS) -> None:
        """Test that list_mounts returns proper mount info structure."""
        mounts = nx.service("mount").list_mounts_sync()
        assert len(mounts) >= 1

        mount = mounts[0]
        assert "mount_point" in mount

    async def test_list_mounts_after_add_mount(self, nx: NexusFS, temp_dir: Path) -> None:
        """Test list_mounts includes newly added mounts."""
        # Create a new directory for the mount
        mount_data_dir = temp_dir / "mount_data"
        mount_data_dir.mkdir()

        # Add a local mount
        mount_id = nx.service("mount").add_mount_sync(
            mount_point="/mnt/test",
            backend_type="cas_local",
            backend_config={"data_dir": str(mount_data_dir)},
        )

        assert mount_id == "/mnt/test"

        mounts = nx.service("mount").list_mounts_sync()
        mount_points = [m["mount_point"] for m in mounts]
        assert "/mnt/test" in mount_points


class TestGetMount:
    """Tests for get_mount method."""

    def test_get_mount_root(self, nx: NexusFS) -> None:
        """Test getting the root mount."""
        mount = nx.service("mount").get_mount_sync("/")
        assert mount is not None
        assert mount["mount_point"] == "/"

    def test_get_mount_nonexistent(self, nx: NexusFS) -> None:
        """Test getting a nonexistent mount returns None."""
        mount = nx.service("mount").get_mount_sync("/nonexistent")
        assert mount is None

    async def test_get_mount_after_add(self, nx: NexusFS, temp_dir: Path) -> None:
        """Test getting a mount after adding it."""
        mount_data_dir = temp_dir / "mount_data"
        mount_data_dir.mkdir()

        nx.service("mount").add_mount_sync(
            mount_point="/mnt/test",
            backend_type="cas_local",
            backend_config={"data_dir": str(mount_data_dir)},
        )

        mount = nx.service("mount").get_mount_sync("/mnt/test")
        assert mount is not None
        assert mount["mount_point"] == "/mnt/test"


class TestHasMount:
    """Tests for has_mount method."""

    def test_has_mount_root(self, nx: NexusFS) -> None:
        """Test has_mount returns True for root mount."""
        assert nx.service("mount").has_mount_sync("/") is True

    def test_has_mount_nonexistent(self, nx: NexusFS) -> None:
        """Test has_mount returns False for nonexistent mount."""
        assert nx.service("mount").has_mount_sync("/nonexistent") is False

    async def test_has_mount_after_add(self, nx: NexusFS, temp_dir: Path) -> None:
        """Test has_mount after adding a mount."""
        mount_data_dir = temp_dir / "mount_data"
        mount_data_dir.mkdir()

        assert nx.service("mount").has_mount_sync("/mnt/test") is False

        nx.service("mount").add_mount_sync(
            mount_point="/mnt/test",
            backend_type="cas_local",
            backend_config={"data_dir": str(mount_data_dir)},
        )

        assert nx.service("mount").has_mount_sync("/mnt/test") is True


class TestAddMount:
    """Tests for add_mount method."""

    async def test_add_mount_local_backend(self, nx: NexusFS, temp_dir: Path) -> None:
        """Test adding a local backend mount."""
        mount_data_dir = temp_dir / "local_mount"
        mount_data_dir.mkdir()

        mount_id = nx.service("mount").add_mount_sync(
            mount_point="/mnt/local",
            backend_type="cas_local",
            backend_config={"data_dir": str(mount_data_dir)},
        )

        assert mount_id == "/mnt/local"
        assert nx.service("mount").has_mount_sync("/mnt/local")

    async def test_add_mount_unsupported_backend_raises_error(self, nx: NexusFS) -> None:
        """Test adding an unsupported backend type raises RuntimeError."""
        with pytest.raises(RuntimeError, match="Unsupported backend type"):
            nx.service("mount").add_mount_sync(
                mount_point="/mnt/unsupported",
                backend_type="unsupported_backend",
                backend_config={},
            )

    async def test_add_mount_with_context_grants_permission(
        self, nx_with_permissions: NexusFS, temp_dir: Path
    ) -> None:
        """Test that add_mount grants direct_owner permission to the user."""
        from nexus.contracts.types import OperationContext

        mount_data_dir = temp_dir / "perm_mount"
        mount_data_dir.mkdir()

        # Use admin context to bypass permission check (testing permission grant, not check)
        context = OperationContext(
            user_id="alice",
            groups=[],
            zone_id="test_zone",
            subject_type="user",
            subject_id="alice",
            is_admin=True,
        )

        nx_with_permissions.service("mount").add_mount_sync(
            mount_point="/mnt/alice",
            backend_type="cas_local",
            backend_config={"data_dir": str(mount_data_dir)},
            context=context,
        )

        assert nx_with_permissions.service("mount").has_mount_sync("/mnt/alice")


class TestRemoveMount:
    """Tests for remove_mount method."""

    async def test_remove_mount_success(self, nx: NexusFS, temp_dir: Path) -> None:
        """Test removing a mount successfully."""
        mount_data_dir = temp_dir / "removable_mount"
        mount_data_dir.mkdir()

        nx.service("mount").add_mount_sync(
            mount_point="/mnt/removable",
            backend_type="cas_local",
            backend_config={"data_dir": str(mount_data_dir)},
        )

        assert nx.service("mount").has_mount_sync("/mnt/removable")

        result = nx.service("mount").remove_mount_sync("/mnt/removable")
        assert result["removed"] is True
        assert nx.service("mount").has_mount_sync("/mnt/removable") is False

    def test_remove_mount_nonexistent(self, nx: NexusFS) -> None:
        """Test removing a nonexistent mount returns error."""
        result = nx.service("mount").remove_mount_sync("/mnt/nonexistent")
        assert result["removed"] is False
        assert "Mount not found" in result["errors"][0]

    async def test_remove_mount_returns_cleanup_info(self, nx: NexusFS, temp_dir: Path) -> None:
        """Test that remove_mount returns cleanup information."""
        mount_data_dir = temp_dir / "cleanup_mount"
        mount_data_dir.mkdir()

        nx.service("mount").add_mount_sync(
            mount_point="/mnt/cleanup",
            backend_type="cas_local",
            backend_config={"data_dir": str(mount_data_dir)},
        )

        result = nx.service("mount").remove_mount_sync("/mnt/cleanup")

        assert "removed" in result
        assert "directory_deleted" in result
        assert "permissions_cleaned" in result
        assert "errors" in result
        assert result["removed"] is True


class TestSaveMount:
    """Tests for save_mount method."""

    @pytest.mark.asyncio
    async def test_save_mount_without_mount_manager_raises_error(self, temp_dir: Path) -> None:
        """Test that save_mount raises RuntimeError without mount manager."""
        # Create NexusFS without database (no mount manager)
        nx = create_nexus_fs(
            backend=CASLocalBackend(temp_dir),
            metadata_store=RaftMetadataStore.embedded(str(temp_dir / "raft-test-save-mount")),
            record_store=SQLAlchemyRecordStore(db_path=temp_dir / "test_save_mount.db"),
            parsing=ParseConfig(auto_parse=False),
            permissions=PermissionConfig(enforce=False),
        )

        try:
            # Check if mount_manager is available via service registry
            mount_svc = nx.service("mount")
            if mount_svc is None or mount_svc.mount_manager is None:
                with pytest.raises(RuntimeError, match="Mount manager not available"):
                    nx.service("mount_persist").save_mount(
                        mount_point="/mnt/test",
                        backend_type="cas_local",
                        backend_config={"data_dir": str(temp_dir)},
                    )
        finally:
            nx.close()

    async def test_save_mount_with_mount_manager(self, nx: NexusFS, temp_dir: Path) -> None:
        """Test save_mount when mount manager is available."""
        mount_svc = nx.service("mount")
        if mount_svc is None or mount_svc.mount_manager is None:
            pytest.skip("Mount manager not available in this configuration")

        mount_data_dir = temp_dir / "saved_mount"
        mount_data_dir.mkdir()

        mount_id = await nx.service("mount_persist").save_mount(
            mount_point="/mnt/saved",
            backend_type="cas_local",
            backend_config={"data_dir": str(mount_data_dir)},
            owner_user_id="alice",
            zone_id="test_zone",
            description="Test saved mount",
        )

        assert mount_id is not None


class TestListSavedMounts:
    """Tests for list_saved_mounts method."""

    @pytest.mark.asyncio
    async def test_list_saved_mounts_without_mount_manager_raises_error(
        self, temp_dir: Path
    ) -> None:
        """Test that list_saved_mounts raises RuntimeError without mount manager."""
        nx = create_nexus_fs(
            backend=CASLocalBackend(temp_dir),
            metadata_store=RaftMetadataStore.embedded(
                str(temp_dir / "raft-test-list-saved-mounts")
            ),
            record_store=SQLAlchemyRecordStore(db_path=temp_dir / "test_list_saved_mounts.db"),
            parsing=ParseConfig(auto_parse=False),
            permissions=PermissionConfig(enforce=False),
        )

        try:
            mount_svc = nx.service("mount")
            if mount_svc is None or mount_svc.mount_manager is None:
                with pytest.raises(RuntimeError, match="Mount manager not available"):
                    nx.service("mount_persist").list_saved_mounts()
        finally:
            nx.close()


class TestLoadMount:
    """Tests for load_mount method."""

    @pytest.mark.asyncio
    async def test_load_mount_without_mount_manager_raises_error(self, temp_dir: Path) -> None:
        """Test that load_mount raises RuntimeError without mount manager."""
        nx = create_nexus_fs(
            backend=CASLocalBackend(temp_dir),
            metadata_store=RaftMetadataStore.embedded(str(temp_dir / "raft-test-load-mount")),
            record_store=SQLAlchemyRecordStore(db_path=temp_dir / "test_load_mount.db"),
            parsing=ParseConfig(auto_parse=False),
            permissions=PermissionConfig(enforce=False),
        )

        try:
            mount_svc = nx.service("mount")
            if mount_svc is None or mount_svc.mount_manager is None:
                with pytest.raises(RuntimeError, match="Mount manager not available"):
                    nx.service("mount_persist").load_mount("/mnt/test")
        finally:
            nx.close()


class TestDeleteSavedMount:
    """Tests for delete_saved_mount method."""

    @pytest.mark.asyncio
    async def test_delete_saved_mount_without_mount_manager_raises_error(
        self, temp_dir: Path
    ) -> None:
        """Test that delete_saved_mount raises RuntimeError without mount manager."""
        nx = create_nexus_fs(
            backend=CASLocalBackend(temp_dir),
            metadata_store=RaftMetadataStore.embedded(
                str(temp_dir / "raft-test-delete-saved-mount")
            ),
            record_store=SQLAlchemyRecordStore(db_path=temp_dir / "test_delete_saved_mount.db"),
            parsing=ParseConfig(auto_parse=False),
            permissions=PermissionConfig(enforce=False),
        )

        try:
            mount_svc = nx.service("mount")
            if mount_svc is None or mount_svc.mount_manager is None:
                with pytest.raises(RuntimeError, match="Mount manager not available"):
                    nx.service("mount_persist").delete_saved_mount("/mnt/test")
        finally:
            nx.close()


class TestLoadAllSavedMounts:
    """Tests for load_all_saved_mounts method."""

    async def test_load_all_saved_mounts_without_mount_manager(self, nx: NexusFS) -> None:
        """Test load_all_saved_mounts when mount manager is not available."""
        mount_svc = nx.service("mount")
        if mount_svc is not None and mount_svc.mount_manager is not None:
            pytest.skip("Mount manager is available, test N/A")

        result = await nx.service("mount_persist").load_all_mounts()
        assert result == {"loaded": 0, "failed": 0, "errors": []}

    async def test_load_all_saved_mounts_empty(self, nx: NexusFS) -> None:
        """Test load_all_saved_mounts when no mounts are saved."""
        mount_svc = nx.service("mount")
        if mount_svc is None or mount_svc.mount_manager is None:
            pytest.skip("Mount manager not available")

        result = await nx.service("mount_persist").load_all_mounts()
        assert "loaded" in result
        assert "failed" in result
        assert "errors" in result


class TestMountPermissionEnforcement:
    """Tests for mount operation permission enforcement."""

    async def test_add_mount_requires_write_permission_on_parent(
        self, nx_with_permissions: NexusFS, temp_dir: Path
    ) -> None:
        """Test that add_mount fails without write permission on parent path."""
        from nexus.contracts.types import OperationContext

        mount_data_dir = temp_dir / "perm_test_mount"
        mount_data_dir.mkdir()

        # Non-admin user without write permission on /mnt
        context = OperationContext(
            user_id="bob",
            groups=[],
            zone_id="test_zone",
            subject_type="user",
            subject_id="bob",
            is_admin=False,
        )

        with pytest.raises(PermissionError, match="no write permission"):
            nx_with_permissions.service("mount").add_mount_sync(
                mount_point="/mnt/bob_mount",
                backend_type="cas_local",
                backend_config={"data_dir": str(mount_data_dir)},
                context=context,
            )

    async def test_remove_mount_requires_write_permission(
        self, nx_with_permissions: NexusFS, temp_dir: Path
    ) -> None:
        """Test that remove_mount fails without write permission on mount."""
        from nexus.contracts.types import OperationContext

        mount_data_dir = temp_dir / "remove_perm_mount"
        mount_data_dir.mkdir()

        # First create mount as admin
        admin_context = OperationContext(
            user_id="admin",
            groups=[],
            zone_id="test_zone",
            subject_type="user",
            subject_id="admin",
            is_admin=True,
        )
        nx_with_permissions.service("mount").add_mount_sync(
            mount_point="/mnt/admin_mount",
            backend_type="cas_local",
            backend_config={"data_dir": str(mount_data_dir)},
            context=admin_context,
        )

        # Non-admin user tries to remove without permission
        user_context = OperationContext(
            user_id="bob",
            groups=[],
            zone_id="test_zone",
            subject_type="user",
            subject_id="bob",
            is_admin=False,
        )

        with pytest.raises(PermissionError, match="no write permission"):
            nx_with_permissions.service("mount").remove_mount_sync(
                "/mnt/admin_mount", context=user_context
            )

    async def test_get_mount_returns_none_without_read_permission(
        self, nx_with_permissions: NexusFS, temp_dir: Path
    ) -> None:
        """Test that get_mount returns None without read permission."""
        from nexus.contracts.types import OperationContext

        mount_data_dir = temp_dir / "get_perm_mount"
        mount_data_dir.mkdir()

        # First create mount as admin
        admin_context = OperationContext(
            user_id="admin",
            groups=[],
            zone_id="test_zone",
            subject_type="user",
            subject_id="admin",
            is_admin=True,
        )
        nx_with_permissions.service("mount").add_mount_sync(
            mount_point="/mnt/get_test",
            backend_type="cas_local",
            backend_config={"data_dir": str(mount_data_dir)},
            context=admin_context,
        )

        # Non-admin user tries to get mount without permission
        user_context = OperationContext(
            user_id="bob",
            groups=[],
            zone_id="test_zone",
            subject_type="user",
            subject_id="bob",
            is_admin=False,
        )

        result = nx_with_permissions.service("mount").get_mount_sync(
            "/mnt/get_test", context=user_context
        )
        assert result is None

    async def test_no_context_allows_operations_for_backward_compatibility(
        self, nx: NexusFS, temp_dir: Path
    ) -> None:
        """Test that operations without context succeed (backward compatibility)."""
        mount_data_dir = temp_dir / "no_context_mount"
        mount_data_dir.mkdir()

        # Should succeed without context
        mount_id = nx.service("mount").add_mount_sync(
            mount_point="/mnt/no_ctx",
            backend_type="cas_local",
            backend_config={"data_dir": str(mount_data_dir)},
            context=None,
        )
        assert mount_id == "/mnt/no_ctx"

        # get_mount should also work
        mount = nx.service("mount").get_mount_sync("/mnt/no_ctx", context=None)
        assert mount is not None

        # remove_mount should work
        result = nx.service("mount").remove_mount_sync("/mnt/no_ctx", context=None)
        assert result["removed"] is True


class TestMountIntegration:
    """Integration tests for mount functionality."""

    @pytest.mark.asyncio
    async def test_write_to_mount(self, nx: NexusFS, temp_dir: Path) -> None:
        """Test writing files to a mounted backend."""
        mount_data_dir = temp_dir / "write_mount"
        mount_data_dir.mkdir()

        nx.service("mount").add_mount_sync(
            mount_point="/mnt/write",
            backend_type="cas_local",
            backend_config={"data_dir": str(mount_data_dir)},
        )

        # Write to the mount
        nx.write("/mnt/write/test.txt", b"Hello from mount!")

        # Read back via route.backend to avoid pool collision
        # (all CASLocalBackend instances have name="local", so the pool
        # can only hold one; resolve_backend may return a different instance)
        meta = nx.metadata.get("/mnt/write/test.txt")
        assert meta is not None
        route = nx.router.route("/mnt/write/test.txt", zone_id=nx._zone_id)
        content = route.backend.read_content(meta.etag)
        assert content == b"Hello from mount!"

    @pytest.mark.asyncio
    async def test_list_mount_contents(self, nx: NexusFS, temp_dir: Path) -> None:
        """Test listing files in a mounted backend."""
        mount_data_dir = temp_dir / "list_mount"
        mount_data_dir.mkdir()

        nx.service("mount").add_mount_sync(
            mount_point="/mnt/list",
            backend_type="cas_local",
            backend_config={"data_dir": str(mount_data_dir)},
        )

        # Write some files
        nx.write("/mnt/list/file1.txt", b"Content 1")
        nx.write("/mnt/list/file2.txt", b"Content 2")

        # List files
        files = nx.sys_readdir("/mnt/list", recursive=True)

        assert "/mnt/list/file1.txt" in files
        assert "/mnt/list/file2.txt" in files

    @pytest.mark.asyncio
    async def test_multiple_mounts(self, nx: NexusFS, temp_dir: Path) -> None:
        """Test multiple mounts can coexist."""
        mount1_dir = temp_dir / "mount1"
        mount2_dir = temp_dir / "mount2"
        mount1_dir.mkdir()
        mount2_dir.mkdir()

        nx.service("mount").add_mount_sync(
            mount_point="/mnt/one",
            backend_type="cas_local",
            backend_config={"data_dir": str(mount1_dir)},
        )

        nx.service("mount").add_mount_sync(
            mount_point="/mnt/two",
            backend_type="cas_local",
            backend_config={"data_dir": str(mount2_dir)},
        )

        # Both mounts should exist
        assert nx.service("mount").has_mount_sync("/mnt/one")
        assert nx.service("mount").has_mount_sync("/mnt/two")

        # Write to each
        nx.write("/mnt/one/file.txt", b"Mount 1")
        nx.write("/mnt/two/file.txt", b"Mount 2")

        # Read back via route.backend to avoid pool collision.
        # All CASLocalBackend instances share name="local", so the
        # coordinator's backend_pool can only hold one at a time;
        # resolve_backend("local") returns whichever was last registered.
        # Verify content via route.backend (correctly resolved by LPM).
        for mount_path, expected in [
            ("/mnt/one/file.txt", b"Mount 1"),
            ("/mnt/two/file.txt", b"Mount 2"),
        ]:
            meta = nx.metadata.get(mount_path)
            assert meta is not None, f"metadata missing for {mount_path}"
            route = nx.router.route(mount_path, zone_id=nx._zone_id)
            content = route.backend.read_content(meta.etag)
            assert content == expected, f"content mismatch for {mount_path}"


class TestMountContextUtilsIntegration:
    """Tests for mount operations using context_utils functions."""

    async def test_add_mount_uses_context_utils_functions(
        self, nx_with_permissions: NexusFS, temp_dir: Path
    ):
        """Test that add_mount uses context_utils.get_zone_id and get_user_identity."""
        from nexus.contracts.types import OperationContext

        mount_data_dir = temp_dir / "context_mount"
        mount_data_dir.mkdir()

        # Use admin context to bypass permission check (testing context_utils usage)
        context = OperationContext(
            user_id="alice",
            groups=[],
            zone_id="test_zone",
            subject_type="user",
            subject_id="alice",
            is_admin=True,
        )

        # Patch in mount_service where the functions are actually imported
        with (
            patch("nexus.bricks.mount.mount_service.get_zone_id") as mock_get_zone,
            patch("nexus.bricks.mount.mount_service.get_user_identity") as mock_get_user,
        ):
            mock_get_zone.return_value = "test_zone"
            mock_get_user.return_value = ("user", "alice")

            nx_with_permissions.service("mount").add_mount_sync(
                mount_point="/mnt/context_test",
                backend_type="cas_local",
                backend_config={"data_dir": str(mount_data_dir)},
                context=context,
            )

            # Verify context_utils functions were called
            mock_get_zone.assert_called()
            mock_get_user.assert_called()

    async def test_remove_mount_with_context_works(
        self, nx_with_permissions: NexusFS, temp_dir: Path
    ):
        """Test that remove_mount works correctly with context (uses context_utils internally)."""
        from nexus.contracts.types import OperationContext

        mount_data_dir = temp_dir / "remove_context_mount"
        mount_data_dir.mkdir()

        # Use admin context to bypass permission check (testing remove functionality)
        context = OperationContext(
            user_id="alice",
            groups=[],
            zone_id="test_zone",
            subject_type="user",
            subject_id="alice",
            is_admin=True,
        )

        # Add mount first
        nx_with_permissions.service("mount").add_mount_sync(
            mount_point="/mnt/remove_test",
            backend_type="cas_local",
            backend_config={"data_dir": str(mount_data_dir)},
            context=context,
        )

        # Remove mount with context - should work correctly
        result = nx_with_permissions.service("mount").remove_mount_sync(
            "/mnt/remove_test", context=context
        )

        # Verify mount was removed
        assert result["removed"] is True
        assert not nx_with_permissions.service("mount").has_mount_sync("/mnt/remove_test")

    async def test_add_mount_oauth_backend_uses_context_utils_database_url(
        self, nx: NexusFS, temp_dir: Path
    ):
        """Test that add_mount for OAuth backends uses context_utils.get_database_url."""
        # Set up database path
        nx.db_path = temp_dir / "token_manager.db"

        # Patch _needs_token_manager_db to return True (gdrive_connector not in registry)
        # and patch get_database_url at the source module
        with (
            patch(
                "nexus.bricks.mount.mount_service._needs_token_manager_db",
                return_value=True,
            ),
            patch("nexus.lib.context_utils.get_database_url") as mock_get_db_url,
        ):
            mock_get_db_url.return_value = str(temp_dir / "token_manager.db")

            # This should use get_database_url for gdrive_connector
            with contextlib.suppress(Exception):
                nx.service("mount").add_mount_sync(
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
            nx.service("mount_persist").load_mount(mount_config["mount_point"])
        except RuntimeError as e:
            # Should not fail with "No database path configured" error
            # (may fail for other reasons like missing OAuth config)
            assert "No database path configured" not in str(e)
        except Exception:
            # Other exceptions are acceptable (e.g., missing OAuth credentials)
            pass

    async def test_add_mount_with_none_context_uses_defaults(
        self, nx_with_permissions: NexusFS, temp_dir: Path
    ):
        """Test that add_mount handles None context gracefully using context_utils defaults."""
        mount_data_dir = temp_dir / "none_context_mount"
        mount_data_dir.mkdir()

        # Should not raise error with None context - context_utils provides defaults
        # This tests that the refactored code works with None context
        nx_with_permissions.service("mount").add_mount_sync(
            mount_point="/mnt/none_context",
            backend_type="cas_local",
            backend_config={"data_dir": str(mount_data_dir)},
            context=None,
        )

        # Verify mount was created successfully
        assert nx_with_permissions.service("mount").has_mount_sync("/mnt/none_context")
