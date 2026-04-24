"""Unit tests for MountService.

Tests mount management operations: add, remove, list, permission checks.
All async service methods are tested via asyncio.run() to avoid
pytest-asyncio dependency.
"""

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from nexus.bricks.mount.mount_service import MountService
from nexus.contracts.types import OperationContext

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_kernel():
    """Create a mock Rust kernel."""
    kernel = MagicMock()
    return kernel


@pytest.fixture
def mock_dlc():
    """Create a mock DriverLifecycleCoordinator."""
    dlc = MagicMock()
    dlc.get_mount_info.return_value = None
    dlc.list_mounts.return_value = []
    return dlc


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
    """Create a mock NexusFS."""
    fs = MagicMock()
    fs.mkdir = MagicMock()
    fs.sys_write = MagicMock()
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
def mock_driver_coordinator():
    """Mock DriverLifecycleCoordinator (kernel-owned, always available)."""
    coord = MagicMock()
    coord.unmount.return_value = True
    return coord


@pytest.fixture
def mount_service(
    mock_kernel, mock_dlc, mock_mount_manager, mock_nexus_fs, mock_driver_coordinator
):
    """Create a MountService with all mock dependencies."""
    svc = MountService(
        kernel=mock_kernel,
        dlc=mock_dlc,
        mount_manager=mock_mount_manager,
        nexus_fs=mock_nexus_fs,
    )
    svc._driver_coordinator = mock_driver_coordinator
    return svc


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


# =============================================================================
# MountService initialization
# =============================================================================


class TestMountServiceInit:
    """Tests for MountService construction."""

    def test_init_stores_dependencies(
        self, mock_kernel, mock_dlc, mock_mount_manager, mock_nexus_fs
    ):
        """MountService stores all injected dependencies."""
        service = MountService(
            kernel=mock_kernel,
            dlc=mock_dlc,
            mount_manager=mock_mount_manager,
            nexus_fs=mock_nexus_fs,
        )
        assert service._kernel is mock_kernel
        assert service._dlc is mock_dlc
        assert service.mount_manager is mock_mount_manager
        assert service.nexus_fs is mock_nexus_fs

    def test_init_with_minimal_dependencies(self, mock_kernel, mock_dlc):
        """MountService can be created with only kernel and dlc."""
        service = MountService(kernel=mock_kernel, dlc=mock_dlc)
        assert service._kernel is mock_kernel
        assert service._dlc is mock_dlc
        assert service.mount_manager is None
        assert service.nexus_fs is None

    def test_init_stores_auth_service(self, mock_kernel, mock_dlc) -> None:
        auth_service = MagicMock()
        service = MountService(kernel=mock_kernel, dlc=mock_dlc, auth_service=auth_service)
        assert service._auth_service is auth_service


# =============================================================================
# list_mounts tests
# =============================================================================


class TestListMounts:
    """Tests for the list_mounts method."""

    def test_list_mounts_returns_empty_list(self, mount_service, mock_dlc):
        """list_mounts returns empty list when no mounts exist."""
        mock_dlc.list_mounts.return_value = []
        result = asyncio.run(mount_service.list_mounts())
        assert result == []

    def test_list_mounts_returns_all_without_context(self, mount_service, mock_dlc):
        """Without context, all mounts are returned (backward compat)."""
        dlc_info = MagicMock()
        dlc_info.backend = MagicMock()

        mock_dlc.list_mounts.return_value = [("/root/mnt/test", dlc_info)]

        result = asyncio.run(mount_service.list_mounts())

        assert len(result) == 1
        assert result[0]["mount_point"] == "/mnt/test"

    def test_list_mounts_filters_by_permission(
        self, mount_service, mock_dlc, mock_nexus_fs, operation_context
    ):
        """Mounts without read permission are excluded."""
        dlc_info_a = MagicMock()
        dlc_info_a.backend = MagicMock()
        dlc_info_b = MagicMock()
        dlc_info_b.backend = MagicMock()

        mock_dlc.list_mounts.return_value = [
            ("/root/mnt/allowed", dlc_info_a),
            ("/root/mnt/denied", dlc_info_b),
        ]

        # Mock _check_mount_permission directly — without a gateway the
        # permissive fallback returns True for all mounts.
        mount_service._check_mount_permission = lambda mp, ctx: mp == "/mnt/allowed"

        result = asyncio.run(mount_service.list_mounts(context=operation_context))

        assert len(result) == 1
        assert result[0]["mount_point"] == "/mnt/allowed"

    def test_list_mounts_admin_sees_all(self, mount_service, mock_dlc, mock_nexus_fs):
        """Admin users see all mounts regardless of permissions."""
        dlc_info = MagicMock()
        dlc_info.backend = MagicMock()

        mock_dlc.list_mounts.return_value = [("/root/mnt/restricted", dlc_info)]
        mock_nexus_fs.rebac_check.return_value = False

        admin_ctx = OperationContext(
            user_id="admin_user",
            groups=[],
            zone_id="test_zone",
            is_admin=True,
        )

        result = asyncio.run(mount_service.list_mounts(context=admin_ctx))

        assert len(result) == 1
        assert result[0]["mount_point"] == "/mnt/restricted"


# =============================================================================
# remove_mount tests
# =============================================================================


class TestRemoveMount:
    """Tests for the remove_mount method."""

    def test_remove_mount_success(self, mount_service, mock_nexus_fs):
        """Removing an existing mount returns removed=True."""
        mount_service._driver_coordinator.unmount.return_value = True

        result = asyncio.run(mount_service.remove_mount("/mnt/test"))

        assert result["removed"] is True
        mount_service._driver_coordinator.unmount.assert_called_once_with("/mnt/test")

    def test_remove_mount_not_found(self, mount_service):
        """Removing a non-existent mount returns errors."""
        mount_service._driver_coordinator.unmount.return_value = False

        result = asyncio.run(mount_service.remove_mount("/mnt/nonexistent"))

        assert result["removed"] is False
        assert "Mount not found" in result["errors"][0]

    def test_remove_mount_cleans_up_directory(self, mount_service, mock_nexus_fs):
        """Removing a mount deletes the directory entry."""
        mount_service._driver_coordinator.unmount.return_value = True

        result = asyncio.run(mount_service.remove_mount("/mnt/test"))

        assert result["removed"] is True
        mock_nexus_fs.metadata.delete.assert_called_once_with("/mnt/test")
        assert result["directory_deleted"] is True

    def test_remove_mount_handles_cleanup_errors(self, mount_service, mock_nexus_fs):
        """Errors during cleanup are reported but don't fail the removal."""
        mount_service._driver_coordinator.unmount.return_value = True
        mock_nexus_fs.metadata.delete.side_effect = RuntimeError("DB error")

        result = asyncio.run(mount_service.remove_mount("/mnt/test"))

        assert result["removed"] is True
        assert result["directory_deleted"] is False
        assert len(result["errors"]) > 0


class TestAddMountAuthResolution:
    """Tests auth resolution during mount creation."""

    def test_add_mount_uses_auth_service_resolution(
        self, mock_kernel, mock_dlc, mock_mount_manager, mock_nexus_fs, mock_driver_coordinator
    ) -> None:
        auth_service = MagicMock()
        auth_service.resolve_backend_config.return_value = MagicMock(
            resolved_config={"bucket": "demo", "access_key_id": "AKIA", "secret_access_key": "x"},
            status=MagicMock(value="authed"),
            message=None,
        )
        service = MountService(
            kernel=mock_kernel,
            dlc=mock_dlc,
            mount_manager=mock_mount_manager,
            nexus_fs=mock_nexus_fs,
            auth_service=auth_service,
        )
        service._check_permission = MagicMock(return_value=True)
        service._create_backend = MagicMock(return_value=MagicMock())
        service._setup_mount_point = MagicMock()
        service._driver_coordinator = mock_driver_coordinator

        with patch("nexus.backends.base.registry.ConnectorRegistry") as mock_cr:
            mock_cr.get_info.return_value = MagicMock(
                auth_fields=["access_key_id", "secret_access_key"]
            )

            result = service.add_mount_sync(
                "/mnt/s3",
                "path_s3",
                {"bucket": "demo"},
            )

            assert result == "/mnt/s3"
            auth_service.resolve_backend_config.assert_called_once()
            service._create_backend.assert_called_once_with(
                "path_s3",
                {"bucket": "demo", "access_key_id": "AKIA", "secret_access_key": "x"},
            )

    def test_add_mount_raises_when_auth_missing(
        self, mock_kernel, mock_dlc, mock_mount_manager, mock_nexus_fs, mock_driver_coordinator
    ) -> None:
        auth_service = MagicMock()
        auth_service.resolve_backend_config.return_value = MagicMock(
            resolved_config={"bucket": "demo"},
            status=MagicMock(value="no_auth"),
            message="Run `nexus auth connect s3 secret`.",
        )
        service = MountService(
            kernel=mock_kernel,
            dlc=mock_dlc,
            mount_manager=mock_mount_manager,
            nexus_fs=mock_nexus_fs,
            auth_service=auth_service,
        )
        service._check_permission = MagicMock(return_value=True)
        service._driver_coordinator = mock_driver_coordinator

        with pytest.raises(RuntimeError, match="nexus auth connect s3 secret"):
            service.add_mount_sync("/mnt/s3", "path_s3", {"bucket": "demo"})


# =============================================================================
# get_mount tests
# =============================================================================


class TestGetMount:
    """Tests for the get_mount method."""

    def test_get_mount_found(self, mount_service, mock_dlc):
        """Getting an existing mount returns its details."""
        dlc_info = MagicMock()
        dlc_info.backend = MagicMock()

        mock_dlc.get_mount_info.return_value = dlc_info

        result = asyncio.run(mount_service.get_mount("/mnt/test"))

        assert result is not None
        assert result["mount_point"] == "/mnt/test"

    def test_get_mount_not_found(self, mount_service, mock_dlc):
        """Getting a non-existent mount returns None."""
        mock_dlc.get_mount_info.return_value = None

        result = asyncio.run(mount_service.get_mount("/mnt/nonexistent"))
        assert result is None


# =============================================================================
# has_mount tests
# =============================================================================


class TestHasMount:
    """Tests for the has_mount method."""

    def test_has_mount_true(self, mount_service, mock_dlc):
        """has_mount returns True for existing mount."""
        mock_dlc.get_mount_info.return_value = MagicMock()
        assert asyncio.run(mount_service.has_mount("/mnt/test")) is True

    def test_has_mount_false(self, mount_service, mock_dlc):
        """has_mount returns False for non-existent mount."""
        mock_dlc.get_mount_info.return_value = None
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
                backend_type="path_gcs",
                backend_config={"bucket": "test-bucket"},
            )
        )

        assert result == "mount-uuid-1234"
        mock_mount_manager.save_mount.assert_called_once()

    def test_save_mount_requires_mount_manager(self, mock_kernel, mock_dlc):
        """save_mount raises RuntimeError without mount_manager."""
        service = MountService(kernel=mock_kernel, dlc=mock_dlc, mount_manager=None)

        with pytest.raises(RuntimeError, match="Mount manager not available"):
            asyncio.run(
                service.save_mount(
                    mount_point="/mnt/test",
                    backend_type="cas_local",
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

    def test_delete_saved_mount_requires_mount_manager(self, mock_kernel, mock_dlc):
        """delete_saved_mount raises RuntimeError without mount_manager."""
        service = MountService(kernel=mock_kernel, dlc=mock_dlc, mount_manager=None)

        with pytest.raises(RuntimeError, match="Mount manager not available"):
            asyncio.run(service.delete_saved_mount("/mnt/test"))


# =============================================================================
# _grant_owner_permission tests
# =============================================================================


class TestGrantMountOwnerPermission:
    """Tests for the _grant_owner_permission helper."""

    def test_grants_permission_with_context(self, mount_service, mock_nexus_fs, operation_context):
        """Owner permission is granted when context has a user."""
        mount_service._grant_owner_permission("/mnt/test", operation_context)

        # Issue #2033: MountService now uses rebac_service.rebac_create_sync
        mock_nexus_fs.service("rebac").rebac_create_sync.assert_called_once()
        call_kwargs = mock_nexus_fs.service("rebac").rebac_create_sync.call_args
        assert call_kwargs.kwargs["relation"] == "direct_owner"

    def test_skips_permission_without_context(self, mount_service, mock_nexus_fs):
        """No permission grant when context is None."""
        mount_service._grant_owner_permission("/mnt/test", None)
        mock_nexus_fs.rebac_add_tuple.assert_not_called()

    def test_creates_directory_entry(self, mount_service, mock_nexus_fs, operation_context):
        """Mount point directory entries are created via _setup_mount_point."""
        from unittest.mock import MagicMock

        # Provide a gateway mock with metadata_put + metadata_get
        gw = MagicMock()
        gw.metadata_get.return_value = None  # dirs don't exist yet
        mount_service._gw = gw
        mount_service._setup_mount_point("/mnt/test", operation_context)
        # metadata_put called for /mnt and /mnt/test
        assert gw.metadata_put.call_count == 2

    def test_handles_mkdir_error(self, mount_service, mock_nexus_fs, operation_context):
        """Errors creating directory do not prevent permission grant."""
        from unittest.mock import MagicMock

        gw = MagicMock()
        gw.metadata_get.return_value = None
        gw.metadata_put.side_effect = RuntimeError("put failed")
        mount_service._gw = gw

        # Should not raise — errors in directory creation are logged but not fatal
        mount_service._setup_mount_point("/mnt/test", operation_context)

        # Permission grant should still be attempted even when mkdir fails
        gw.rebac_create.assert_called_once()
