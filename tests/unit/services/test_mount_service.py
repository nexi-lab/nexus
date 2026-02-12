"""Unit tests for MountService.

Tests mount management operations: add, remove, list, permission checks.
All async service methods are tested via asyncio.run() to avoid
pytest-asyncio dependency.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from nexus.core.permissions import OperationContext
from nexus.services.mount_service import MountService

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_router():
    """Create a mock PathRouter."""
    router = MagicMock()
    router.add_mount.return_value = None
    router.remove_mount.return_value = True
    router.has_mount.return_value = False
    router.get_mount.return_value = None
    router.list_mounts.return_value = []
    return router


@pytest.fixture
def mock_mount_manager():
    """Create a mock MountManager."""
    manager = MagicMock()
    manager.save_mount.return_value = "mount-uuid-1234"
    manager.get_mount.return_value = None
    manager.list_mounts.return_value = []
    manager.remove_mount.return_value = True
    return manager


@pytest.fixture
def mock_nexus_fs():
    """Create a mock NexusFilesystem."""
    fs = MagicMock()
    fs.mkdir = MagicMock()
    fs.write = MagicMock()
    fs.metadata = MagicMock()
    fs.metadata.delete = MagicMock()
    fs.rebac_add_tuple = MagicMock()
    fs.rebac_check = MagicMock(return_value=True)
    fs.rebac_delete_object_tuples = MagicMock(return_value=0)
    fs.hierarchy_manager = MagicMock()
    fs.hierarchy_manager.remove_parent_tuples = MagicMock(return_value=0)
    fs.SessionLocal = None
    return fs


@pytest.fixture
def mount_service(mock_router, mock_mount_manager, mock_nexus_fs):
    """Create a MountService with all mock dependencies."""
    return MountService(
        router=mock_router,
        mount_manager=mock_mount_manager,
        nexus_fs=mock_nexus_fs,
    )


@pytest.fixture
def operation_context():
    """Standard operation context for tests."""
    return OperationContext(
        user="test_user",
        groups=["test_group"],
        zone_id="test_zone",
        is_system=False,
        is_admin=False,
    )


# =============================================================================
# MountService initialization
# =============================================================================


class TestMountServiceInit:
    """Tests for MountService construction."""

    def test_init_stores_dependencies(self, mock_router, mock_mount_manager, mock_nexus_fs):
        """MountService stores all injected dependencies."""
        service = MountService(
            router=mock_router,
            mount_manager=mock_mount_manager,
            nexus_fs=mock_nexus_fs,
        )
        assert service.router is mock_router
        assert service.mount_manager is mock_mount_manager
        assert service.nexus_fs is mock_nexus_fs

    def test_init_with_minimal_dependencies(self, mock_router):
        """MountService can be created with only a router."""
        service = MountService(router=mock_router)
        assert service.router is mock_router
        assert service.mount_manager is None
        assert service.nexus_fs is None


# =============================================================================
# list_mounts tests
# =============================================================================


class TestListMounts:
    """Tests for the list_mounts method."""

    def test_list_mounts_returns_empty_list(self, mount_service, mock_router):
        """list_mounts returns empty list when no mounts exist."""
        mock_router.list_mounts.return_value = []
        result = asyncio.run(mount_service.list_mounts())
        assert result == []

    def test_list_mounts_returns_all_without_context(self, mount_service, mock_router):
        """Without context, all mounts are returned (backward compat)."""
        mount_info = MagicMock()
        mount_info.mount_point = "/mnt/test"
        mount_info.priority = 10
        mount_info.readonly = False
        mount_info.backend = MagicMock()
        type(mount_info.backend).__name__ = "GCSConnectorBackend"

        mock_router.list_mounts.return_value = [mount_info]

        result = asyncio.run(mount_service.list_mounts())

        assert len(result) == 1
        assert result[0]["mount_point"] == "/mnt/test"
        assert result[0]["priority"] == 10
        assert result[0]["readonly"] is False
        assert result[0]["backend_type"] == "GCSConnectorBackend"

    def test_list_mounts_filters_by_permission(
        self, mount_service, mock_router, mock_nexus_fs, operation_context
    ):
        """Mounts without read permission are excluded."""
        mount_a = MagicMock()
        mount_a.mount_point = "/mnt/allowed"
        mount_a.priority = 0
        mount_a.readonly = False
        mount_a.backend = MagicMock()
        type(mount_a.backend).__name__ = "TestBackend"

        mount_b = MagicMock()
        mount_b.mount_point = "/mnt/denied"
        mount_b.priority = 0
        mount_b.readonly = False
        mount_b.backend = MagicMock()
        type(mount_b.backend).__name__ = "TestBackend"

        mock_router.list_mounts.return_value = [mount_a, mount_b]

        # Only allow /mnt/allowed
        def check_permission(subject, permission, object, zone_id=None):
            return object[1] == "/mnt/allowed"

        mock_nexus_fs.rebac_check.side_effect = check_permission

        result = asyncio.run(mount_service.list_mounts(_context=operation_context))

        assert len(result) == 1
        assert result[0]["mount_point"] == "/mnt/allowed"

    def test_list_mounts_admin_sees_all(self, mount_service, mock_router, mock_nexus_fs):
        """Admin users see all mounts regardless of permissions."""
        mount_info = MagicMock()
        mount_info.mount_point = "/mnt/restricted"
        mount_info.priority = 0
        mount_info.readonly = False
        mount_info.backend = MagicMock()
        type(mount_info.backend).__name__ = "TestBackend"

        mock_router.list_mounts.return_value = [mount_info]
        mock_nexus_fs.rebac_check.return_value = False

        admin_ctx = OperationContext(
            user="admin_user",
            groups=[],
            zone_id="test_zone",
            is_admin=True,
        )

        result = asyncio.run(mount_service.list_mounts(_context=admin_ctx))

        assert len(result) == 1
        assert result[0]["mount_point"] == "/mnt/restricted"


# =============================================================================
# remove_mount tests
# =============================================================================


class TestRemoveMount:
    """Tests for the remove_mount method."""

    def test_remove_mount_success(self, mount_service, mock_router, mock_nexus_fs):
        """Removing an existing mount returns removed=True."""
        mock_router.remove_mount.return_value = True

        result = asyncio.run(mount_service.remove_mount("/mnt/test"))

        assert result["removed"] is True
        mock_router.remove_mount.assert_called_once_with("/mnt/test")

    def test_remove_mount_not_found(self, mount_service, mock_router):
        """Removing a non-existent mount returns errors."""
        mock_router.remove_mount.return_value = False

        result = asyncio.run(mount_service.remove_mount("/mnt/nonexistent"))

        assert result["removed"] is False
        assert "Mount not found" in result["errors"][0]

    def test_remove_mount_cleans_up_directory(self, mount_service, mock_router, mock_nexus_fs):
        """Removing a mount deletes the directory entry."""
        mock_router.remove_mount.return_value = True

        result = asyncio.run(mount_service.remove_mount("/mnt/test"))

        assert result["removed"] is True
        mock_nexus_fs.metadata.delete.assert_called_once_with("/mnt/test")
        assert result["directory_deleted"] is True

    def test_remove_mount_handles_cleanup_errors(self, mount_service, mock_router, mock_nexus_fs):
        """Errors during cleanup are reported but don't fail the removal."""
        mock_router.remove_mount.return_value = True
        mock_nexus_fs.metadata.delete.side_effect = RuntimeError("DB error")

        result = asyncio.run(mount_service.remove_mount("/mnt/test"))

        assert result["removed"] is True
        assert result["directory_deleted"] is False
        assert len(result["errors"]) > 0


# =============================================================================
# get_mount tests
# =============================================================================


class TestGetMount:
    """Tests for the get_mount method."""

    def test_get_mount_found(self, mount_service, mock_router):
        """Getting an existing mount returns its details."""
        mount_info = MagicMock()
        mount_info.mount_point = "/mnt/test"
        mount_info.priority = 5
        mount_info.readonly = True
        mount_info.backend = MagicMock()
        type(mount_info.backend).__name__ = "LocalBackend"

        mock_router.get_mount.return_value = mount_info

        result = asyncio.run(mount_service.get_mount("/mnt/test"))

        assert result is not None
        assert result["mount_point"] == "/mnt/test"
        assert result["priority"] == 5
        assert result["readonly"] is True
        assert result["backend_type"] == "LocalBackend"

    def test_get_mount_not_found(self, mount_service, mock_router):
        """Getting a non-existent mount returns None."""
        mock_router.get_mount.return_value = None

        result = asyncio.run(mount_service.get_mount("/mnt/nonexistent"))
        assert result is None


# =============================================================================
# has_mount tests
# =============================================================================


class TestHasMount:
    """Tests for the has_mount method."""

    def test_has_mount_true(self, mount_service, mock_router):
        """has_mount returns True for existing mount."""
        mock_router.has_mount.return_value = True
        assert asyncio.run(mount_service.has_mount("/mnt/test")) is True

    def test_has_mount_false(self, mount_service, mock_router):
        """has_mount returns False for non-existent mount."""
        mock_router.has_mount.return_value = False
        assert asyncio.run(mount_service.has_mount("/mnt/nonexistent")) is False


# =============================================================================
# save_mount / delete_saved_mount tests
# =============================================================================


class TestSavedMounts:
    """Tests for saved mount configuration operations."""

    def test_save_mount_success(self, mount_service, mock_mount_manager, mock_nexus_fs):
        """save_mount persists configuration and returns mount ID."""
        result = asyncio.run(
            mount_service.save_mount(
                mount_point="/mnt/test",
                backend_type="gcs_connector",
                backend_config={"bucket": "test-bucket"},
            )
        )

        assert result == "mount-uuid-1234"
        mock_mount_manager.save_mount.assert_called_once()

    def test_save_mount_requires_mount_manager(self, mock_router):
        """save_mount raises RuntimeError without mount_manager."""
        service = MountService(router=mock_router, mount_manager=None)

        with pytest.raises(RuntimeError, match="Mount manager not available"):
            asyncio.run(
                service.save_mount(
                    mount_point="/mnt/test",
                    backend_type="local",
                    backend_config={"data_dir": "/tmp"},
                )
            )

    def test_delete_saved_mount_success(self, mount_service, mock_mount_manager):
        """delete_saved_mount removes config from database."""
        mock_mount_manager.remove_mount.return_value = True

        result = asyncio.run(mount_service.delete_saved_mount("/mnt/test"))
        assert result is True

    def test_delete_saved_mount_not_found(self, mount_service, mock_mount_manager):
        """delete_saved_mount returns False if mount not in database."""
        mock_mount_manager.remove_mount.return_value = False

        result = asyncio.run(mount_service.delete_saved_mount("/mnt/nonexistent"))
        assert result is False

    def test_delete_saved_mount_requires_mount_manager(self, mock_router):
        """delete_saved_mount raises RuntimeError without mount_manager."""
        service = MountService(router=mock_router, mount_manager=None)

        with pytest.raises(RuntimeError, match="Mount manager not available"):
            asyncio.run(service.delete_saved_mount("/mnt/test"))


# =============================================================================
# sync_mount delegation tests
# =============================================================================


class TestSyncMountDelegation:
    """Tests for sync_mount delegation to NexusFS."""

    def test_sync_mount_delegates_to_nexus_fs(self, mount_service, mock_nexus_fs):
        """sync_mount delegates to nexus_fs.sync_mount."""
        mock_nexus_fs.sync_mount.return_value = {"files_synced": 10}

        result = asyncio.run(
            mount_service.sync_mount(
                mount_point="/mnt/test",
                recursive=True,
            )
        )

        assert result == {"files_synced": 10}
        mock_nexus_fs.sync_mount.assert_called_once()

    def test_sync_mount_requires_nexus_fs(self, mock_router):
        """sync_mount raises RuntimeError without nexus_fs."""
        service = MountService(router=mock_router, nexus_fs=None)

        with pytest.raises(RuntimeError, match="requires NexusFS integration"):
            asyncio.run(service.sync_mount(mount_point="/mnt/test"))


# =============================================================================
# _grant_mount_owner_permission tests
# =============================================================================


class TestGrantMountOwnerPermission:
    """Tests for the _grant_mount_owner_permission helper."""

    def test_grants_permission_with_context(self, mount_service, mock_nexus_fs, operation_context):
        """Owner permission is granted when context has a user."""
        mount_service._grant_mount_owner_permission("/mnt/test", operation_context)

        mock_nexus_fs.rebac_add_tuple.assert_called_once()
        call_kwargs = mock_nexus_fs.rebac_add_tuple.call_args
        assert call_kwargs.kwargs["relation"] == "direct_owner"

    def test_skips_permission_without_context(self, mount_service, mock_nexus_fs):
        """No permission grant when context is None."""
        mount_service._grant_mount_owner_permission("/mnt/test", None)
        mock_nexus_fs.rebac_add_tuple.assert_not_called()

    def test_creates_directory_entry(self, mount_service, mock_nexus_fs, operation_context):
        """Mount point directory is created."""
        mount_service._grant_mount_owner_permission("/mnt/test", operation_context)
        mock_nexus_fs.mkdir.assert_called_once_with("/mnt/test", parents=True, exist_ok=True)

    def test_handles_mkdir_error(self, mount_service, mock_nexus_fs, operation_context):
        """Errors creating directory do not prevent permission grant."""
        mock_nexus_fs.mkdir.side_effect = RuntimeError("mkdir failed")

        # Should not raise
        mount_service._grant_mount_owner_permission("/mnt/test", operation_context)

        # Permission grant should still be attempted
        mock_nexus_fs.rebac_add_tuple.assert_called_once()


# =============================================================================
# _generate_connector_skill tests
# =============================================================================


class TestGenerateConnectorSkill:
    """Tests for the _generate_connector_skill helper."""

    def test_generates_skill_for_connector(self, mount_service, mock_nexus_fs):
        """SKILL.md is generated for connector mounts."""
        result = mount_service._generate_connector_skill("/mnt/gcs", "gcs_connector", None)
        assert result is True
        mock_nexus_fs.write.assert_called_once()
        # Verify skill content was written to the correct path
        call_args = mock_nexus_fs.write.call_args
        assert call_args[0][0] == "/mnt/gcs/SKILL.md"

    def test_returns_false_without_nexus_fs(self, mock_router):
        """Returns False when nexus_fs is not available."""
        service = MountService(router=mock_router, nexus_fs=None)
        result = service._generate_connector_skill("/mnt/gcs", "gcs_connector", None)
        assert result is False

    def test_handles_write_error(self, mount_service, mock_nexus_fs):
        """Write errors are handled gracefully."""
        mock_nexus_fs.write.side_effect = OSError("Write failed")
        result = mount_service._generate_connector_skill("/mnt/gcs", "gcs_connector", None)
        assert result is False
