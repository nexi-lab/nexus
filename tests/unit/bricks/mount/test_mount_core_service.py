"""Unit tests for MountService sync core logic (Issue #2754).

Tests atomic add_mount_sync rollback, _grant_owner_permission propagation,
and remove_mount_sync error collection.

Formerly tested MountCoreService; now tests the unified MountService.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.bricks.mount.mount_service import MountService
from nexus.contracts.types import OperationContext

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_nexus_fs(*, permission_ok: bool = True) -> MagicMock:
    """Create a mock NexusFS with metadata, rebac service, etc."""
    nx = MagicMock()
    nx.mkdir = AsyncMock(return_value=None)
    # rebac service mock
    rebac_svc = MagicMock()
    rebac_svc.rebac_create_sync.return_value = "tuple-1"
    rebac_svc.rebac_check_sync.return_value = permission_ok
    rebac_svc.rebac_delete_object_tuples_sync.return_value = 0
    nx.service.return_value = rebac_svc
    # metadata mock
    nx.metadata.list.return_value = []
    nx.metadata.get.return_value = None
    nx.metadata.delete_batch.return_value = None
    nx.metadata.delete_directory_entries_recursive.return_value = 0
    nx._record_store = None
    return nx


def _build_service(
    *,
    nexus_fs: MagicMock | None = None,
    permission_ok: bool = True,
) -> tuple[MountService, MagicMock]:
    """Build a MountService with mocked nexus_fs."""
    if nexus_fs is None:
        nexus_fs = _mock_nexus_fs(permission_ok=permission_ok)
    mock_dlc = MagicMock()
    service = MountService(dlc=mock_dlc, nexus_fs=nexus_fs)
    # DriverLifecycleCoordinator is kernel-owned; mock it for unit tests.
    service._driver_coordinator = MagicMock()
    return service, nexus_fs


def _op_context(
    user_id: str = "alice",
    zone_id: str = "test-zone",
) -> OperationContext:
    return OperationContext(
        user_id=user_id,
        groups=["users"],
        zone_id=zone_id,
        is_system=False,
        is_admin=False,
    )


# ---------------------------------------------------------------------------
# add_mount_sync rollback tests
# ---------------------------------------------------------------------------


class TestAddMountRollback:
    """Tests that add_mount_sync rolls back router registration on setup failure."""

    async def test_add_mount_success_does_not_rollback(self) -> None:
        """On success, mount stays in router (no rollback)."""
        service, gw = _build_service()
        result = service.add_mount_sync(
            mount_point="/mnt/test",
            backend_type="cas_local",
            backend_config={"data_dir": "/tmp"},
            context=_op_context(),
        )
        assert result == "/mnt/test"
        # R7c: kernel-backed mount path goes through ``nexus_fs.sys_setattr``
        # (entry_type=DT_MOUNT) instead of the legacy ``DLC.mount()`` facade.
        service.nexus_fs.sys_setattr.assert_called_once()
        # unmount should NOT be called on success
        service._driver_coordinator.unmount.assert_not_called()

    async def test_add_mount_rolls_back_on_permission_failure(self) -> None:
        """If _grant_owner_permission fails, mount is removed from router."""
        service, nx = _build_service()
        nx.service("rebac").rebac_create_sync.side_effect = RuntimeError(
            "ReBAC service unavailable"
        )

        with pytest.raises(RuntimeError, match="ReBAC service unavailable"):
            service.add_mount_sync(
                mount_point="/mnt/test",
                backend_type="cas_local",
                backend_config={"data_dir": "/tmp"},
                context=_op_context(),
            )

        # Kernel mount call via sys_setattr fires first, then rollback via
        # DLC.unmount() runs in the except branch of add_mount_sync.
        service.nexus_fs.sys_setattr.assert_called_once()
        service._driver_coordinator.unmount.assert_called_once_with("/mnt/test")

    def test_mkdir_failure_is_best_effort_no_rollback(self) -> None:
        """metadata.put failure is non-critical -- mount stays active (best effort)."""
        service, nx = _build_service()
        nx.metadata.put.side_effect = RuntimeError("Metastore down")

        # metadata.put fails but is caught in _setup_mount_point -- mount succeeds
        result = service.add_mount_sync(
            mount_point="/mnt/test",
            backend_type="cas_local",
            backend_config={"data_dir": "/tmp"},
            context=_op_context(),
        )
        assert result == "/mnt/test"
        # Permission grant still ran, so no rollback
        nx.service("rebac").rebac_create_sync.assert_called_once()
        service._driver_coordinator.unmount.assert_not_called()

    def test_add_mount_no_context_skips_permissions_no_rollback(self) -> None:
        """Without context, permission grant is skipped -- no failure, no rollback."""
        service, nx = _build_service()
        result = service.add_mount_sync(
            mount_point="/mnt/test",
            backend_type="cas_local",
            backend_config={"data_dir": "/tmp"},
            context=None,
        )
        assert result == "/mnt/test"
        service._driver_coordinator.unmount.assert_not_called()
        # rebac_create_sync should not be called without context
        nx.service("rebac").rebac_create_sync.assert_not_called()


# ---------------------------------------------------------------------------
# _grant_owner_permission propagation tests
# ---------------------------------------------------------------------------


class TestGrantOwnerPermission:
    """Tests that _grant_owner_permission lets failures propagate."""

    def test_runtime_error_propagates(self) -> None:
        """Non-'not available' RuntimeError propagates from _grant_owner_permission."""
        service, nx = _build_service()
        nx.service("rebac").rebac_create_sync.side_effect = RuntimeError("unexpected failure")

        with pytest.raises(RuntimeError, match="unexpected failure"):
            service._grant_owner_permission("/mnt/test", _op_context())

    def test_non_runtime_error_is_logged_not_raised(self) -> None:
        """Non-RuntimeError exceptions are caught and logged (best-effort)."""
        service, nx = _build_service()
        nx.service("rebac").rebac_create_sync.side_effect = ConnectionError("DB timeout")

        # Should NOT raise — ConnectionError is caught in the generic except
        service._grant_owner_permission("/mnt/test", _op_context())

    def test_permission_success(self) -> None:
        """On success, rebac_create_sync is called with correct args."""
        service, nx = _build_service()
        service._grant_owner_permission("/mnt/test", _op_context())
        nx.service("rebac").rebac_create_sync.assert_called_once()
        call_kwargs = nx.service("rebac").rebac_create_sync.call_args.kwargs
        assert call_kwargs["relation"] == "direct_owner"
        assert call_kwargs["object"] == ("file", "/mnt/test")

    def test_no_context_skips_silently(self) -> None:
        """Without context, no error and no rebac call."""
        service, nx = _build_service()
        service._grant_owner_permission("/mnt/test", None)
        nx.service("rebac").rebac_create_sync.assert_not_called()

    def test_rebac_not_available_skips_gracefully(self) -> None:
        """ReBAC not configured (no record_store) is non-fatal."""
        service, nx = _build_service()
        nx.service("rebac").rebac_create_sync.side_effect = RuntimeError(
            "ReBAC manager not available (record_store not configured)"
        )

        # Should NOT raise -- just logs a warning and returns
        service._grant_owner_permission("/mnt/test", _op_context())
        nx.service("rebac").rebac_create_sync.assert_called_once()

    def test_rebac_not_available_no_rollback(self) -> None:
        """ReBAC not available during add_mount_sync does not trigger rollback."""
        service, nx = _build_service()
        nx.service("rebac").rebac_create_sync.side_effect = RuntimeError(
            "ReBAC manager not available (record_store not configured)"
        )

        result = service.add_mount_sync(
            mount_point="/mnt/test",
            backend_type="cas_local",
            backend_config={"data_dir": "/tmp"},
            context=_op_context(),
        )
        assert result == "/mnt/test"
        service._driver_coordinator.unmount.assert_not_called()


# ---------------------------------------------------------------------------
# remove_mount_sync error collection tests
# ---------------------------------------------------------------------------


class TestRemoveMountErrorCollection:
    """Tests that remove_mount_sync collects all cleanup errors."""

    def test_metadata_failure_does_not_block_permission_cleanup(self) -> None:
        """Even if metadata delete fails, permission cleanup still runs."""
        service, nx = _build_service()
        nx.metadata.list.side_effect = RuntimeError("metadata DB error")

        result = service.remove_mount_sync("/mnt/test")

        assert result["removed"] is True
        # Error from metadata should be collected
        assert any("metadata" in e.lower() or "db error" in e.lower() for e in result["errors"])

    def test_all_cleanup_errors_collected(self) -> None:
        """Multiple cleanup failures are all reported in result["errors"]."""
        service, nx = _build_service()
        # Metadata list failure
        nx.metadata.list.side_effect = RuntimeError("metadata failure")
        # Directory index cleanup failure
        nx.metadata.delete_directory_entries_recursive.side_effect = RuntimeError(
            "dir index failure"
        )
        # ReBAC cleanup failure
        nx.service("rebac").rebac_list_tuples_sync.side_effect = RuntimeError("rebac failure")
        # Hierarchy manager failure
        nx._rebac_manager.hierarchy_manager.remove_parent_tuples.side_effect = RuntimeError(
            "parent tuple failure"
        )

        result = service.remove_mount_sync("/mnt/test")

        assert result["removed"] is True
        assert len(result["errors"]) == 4

    def test_successful_remove_has_no_errors(self) -> None:
        """Clean removal returns zero errors."""
        service, _nx = _build_service()
        result = service.remove_mount_sync("/mnt/test")
        assert result["removed"] is True
        assert result["errors"] == []

    def test_nonexistent_mount_returns_error(self) -> None:
        """Removing a mount that doesn't exist in router returns error."""
        service, nx = _build_service()  # noqa: F841
        service._driver_coordinator.unmount.return_value = False

        result = service.remove_mount_sync("/mnt/nonexistent")

        assert result["removed"] is False
        assert "Mount not found" in result["errors"][0]
