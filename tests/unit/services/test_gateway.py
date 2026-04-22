"""Unit tests for NexusFSGateway.

Tests delegation to NexusFS for file ops, metadata, ReBAC,
hierarchy, routing, and session access.
"""

from unittest.mock import MagicMock

import pytest

from nexus.contracts.types import OperationContext
from nexus.services.gateway import NexusFSGateway

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_fs():
    """Create a mock NexusFS instance."""
    fs = MagicMock()
    fs.mkdir = MagicMock()
    fs.sys_write = MagicMock(
        return_value={"path": "/test/file.txt", "bytes_written": 7, "created": True}
    )
    fs.sys_read = MagicMock(return_value=b"file content")
    fs.sys_readdir = MagicMock(return_value=["file1.txt", "file2.txt"])
    fs.access = MagicMock(return_value=True)
    fs.metadata = MagicMock()
    fs.metadata.get = MagicMock(return_value=MagicMock(path="/test"))
    fs.metadata.put = MagicMock()
    fs.metadata.list = MagicMock(return_value=[])
    fs.metadata.delete = MagicMock()
    fs.metadata.delete_batch = MagicMock()
    fs.metadata.delete_directory_entries_recursive = MagicMock(return_value=5)
    mock_rebac_svc = MagicMock()
    mock_rebac_svc.rebac_create_sync = MagicMock(return_value={"tuple_id": "t1"})
    mock_rebac_svc.rebac_check_sync = MagicMock(return_value=True)
    mock_rebac_svc.rebac_list_tuples_sync = MagicMock(
        return_value=[
            {"tuple_id": "t1"},
            {"tuple_id": "t2"},
            {"tuple_id": "t3"},
        ]
    )
    mock_rebac_svc.rebac_delete_sync = MagicMock(return_value=True)
    mock_descendant_checker = MagicMock()
    mock_descendant_checker.has_access = MagicMock(return_value=True)

    # Wire up fs.service() for ServiceRegistry lookups and
    # fs._descendant_checker for kernel DI (Issue #1504).
    _service_map = {
        "rebac": mock_rebac_svc,
    }
    fs.service = MagicMock(side_effect=lambda name: _service_map.get(name, MagicMock()))
    fs._descendant_checker = mock_descendant_checker

    fs._rebac_manager = MagicMock()
    fs._rebac_manager.rebac_delete = MagicMock()
    # hierarchy_manager is accessed via rebac_manager.hierarchy_manager (not fs._hierarchy_manager)
    fs._rebac_manager.hierarchy_manager = MagicMock()
    fs._rebac_manager.hierarchy_manager.enable_inheritance = True
    fs._rebac_manager.hierarchy_manager.ensure_parent_tuples_batch = MagicMock(return_value=2)
    fs._rebac_manager.hierarchy_manager.remove_parent_tuples = MagicMock(return_value=1)
    fs.router = MagicMock()
    fs.SessionLocal = MagicMock()
    fs.read = MagicMock(return_value={"content": b"data", "path": "/test/file.txt"})
    fs.read_bulk = MagicMock(return_value={"/a": b"data"})
    fs._get_context_identity = MagicMock(return_value=("zone1", "agent1", False))
    fs.backend = MagicMock()
    return fs


@pytest.fixture
def gateway(mock_fs):
    """Create a NexusFSGateway with mock NexusFS."""
    return NexusFSGateway(mock_fs)


@pytest.fixture
def context():
    """Standard operation context."""
    return OperationContext(
        user_id="test_user",
        groups=["test_group"],
        zone_id="test_zone",
        is_system=False,
        is_admin=False,
    )


# =============================================================================
# Initialization
# =============================================================================


class TestGatewayInit:
    """Tests for NexusFSGateway construction."""

    def test_init_stores_fs(self, mock_fs):
        """Gateway stores NexusFS reference."""
        gw = NexusFSGateway(mock_fs)
        assert gw._fs is mock_fs


# =============================================================================
# File Operations
# =============================================================================


class TestFileOperations:
    """Tests for file operation delegation."""

    def test_mkdir_delegates(self, gateway, mock_fs, context):
        """mkdir delegates to NexusFS.mkdir."""
        gateway.mkdir("/test/dir", parents=True, exist_ok=True, context=context)
        mock_fs.mkdir.assert_called_once_with(
            "/test/dir", parents=True, exist_ok=True, context=context
        )

    def test_write_delegates_bytes(self, gateway, mock_fs, context):
        """sys_write delegates bytes to NexusFS.sys_write and returns dict."""
        result = gateway.sys_write("/test/file.txt", b"content", context=context)
        mock_fs.sys_write.assert_called_once_with("/test/file.txt", b"content", context=context)
        assert result["bytes_written"] == 7

    def test_write_delegates_str(self, gateway, mock_fs, context):
        """sys_write passes str through to NexusFS (kernel handles encoding)."""
        gateway.sys_write("/test/file.txt", "text content", context=context)
        mock_fs.sys_write.assert_called_once_with("/test/file.txt", "text content", context=context)

    def test_read_delegates(self, gateway, mock_fs, context):
        """sys_read delegates to NexusFS.sys_read."""
        result = gateway.sys_read("/test/file.txt", context=context)
        assert result == b"file content"
        mock_fs.sys_read.assert_called_once()

    def test_read_returns_bytes(self, gateway, mock_fs, context):
        """sys_read always returns bytes (POSIX pread semantics)."""
        mock_fs.sys_read.return_value = b"raw bytes"
        result = gateway.sys_read("/test/file.txt", context=context)
        assert result == b"raw bytes"

    def test_list_delegates(self, gateway, mock_fs, context):
        """sys_readdir delegates to NexusFS.sys_readdir."""
        result = gateway.sys_readdir("/test", context=context)
        assert result == ["file1.txt", "file2.txt"]

    def test_list_handles_paginated_result(self, gateway, mock_fs, context):
        """sys_readdir handles PaginatedResult objects."""
        paginated = MagicMock()
        paginated.items = ["a.txt", "b.txt"]
        mock_fs.sys_readdir.return_value = paginated
        result = gateway.sys_readdir("/test", context=context)
        assert result == ["a.txt", "b.txt"]

    def test_exists_delegates(self, gateway, mock_fs, context):
        """access delegates to NexusFS.access."""
        assert gateway.access("/test/file.txt", context=context) is True
        mock_fs.access.assert_called_once()


# =============================================================================
# Metadata Operations
# =============================================================================


class TestMetadataOperations:
    """Tests for metadata operation delegation."""

    def test_metadata_get(self, gateway, mock_fs):
        """metadata_get delegates to fs.metadata.get."""
        result = gateway.metadata_get("/test")
        assert result is not None
        mock_fs.metadata.get.assert_called_once_with("/test")

    def test_metadata_put(self, gateway, mock_fs):
        """metadata_put delegates to fs.metadata.put."""
        meta = MagicMock()
        gateway.metadata_put(meta)
        mock_fs.metadata.put.assert_called_once_with(meta)

    def test_metadata_list(self, gateway, mock_fs):
        """metadata_list delegates to fs.metadata.list."""
        gateway.metadata_list("/prefix", recursive=True)
        mock_fs.metadata.list.assert_called_once_with(prefix="/prefix", recursive=True)

    def test_metadata_delete(self, gateway, mock_fs):
        """metadata_delete delegates to fs.metadata.delete."""
        gateway.metadata_delete("/test")
        mock_fs.metadata.delete.assert_called_once_with("/test")

    def test_metadata_delete_batch(self, gateway, mock_fs):
        """metadata_delete_batch delegates to fs.metadata.delete_batch."""
        gateway.metadata_delete_batch(["/a", "/b"])
        mock_fs.metadata.delete_batch.assert_called_once_with(["/a", "/b"])

    def test_delete_directory_entries_recursive(self, gateway, mock_fs):
        """delete_directory_entries_recursive delegates to metadata."""
        result = gateway.delete_directory_entries_recursive("/test/dir", zone_id="z1")
        assert result == 5

    def test_metadata_get_returns_none_without_get(self, mock_fs):
        """Returns None when metadata has no get method."""
        del mock_fs.metadata.get
        gw = NexusFSGateway(mock_fs)
        assert gw.metadata_get("/test") is None

    def test_metadata_list_returns_empty_without_list(self, mock_fs):
        """Returns empty list when metadata has no list method."""
        del mock_fs.metadata.list
        gw = NexusFSGateway(mock_fs)
        assert gw.metadata_list("/test") == []


# =============================================================================
# ReBAC Operations
# =============================================================================


class TestReBACOperations:
    """Tests for ReBAC permission delegation via ReBACService."""

    def test_rebac_create(self, gateway, mock_fs):
        """rebac_create delegates to rebac_service."""
        result = gateway.rebac_create(
            subject=("user", "alice"),
            relation="viewer",
            object=("file", "/test"),
            zone_id="z1",
        )
        assert result == {"tuple_id": "t1"}
        mock_fs.service("rebac").rebac_create_sync.assert_called_once_with(
            subject=("user", "alice"),
            relation="viewer",
            object=("file", "/test"),
            zone_id="z1",
            context=None,
        )

    def test_rebac_check(self, gateway, mock_fs):
        """rebac_check delegates to rebac_service."""
        result = gateway.rebac_check(
            subject=("user", "alice"),
            permission="read",
            object=("file", "/test"),
            zone_id="z1",
        )
        assert result is True
        mock_fs.service("rebac").rebac_check_sync.assert_called_once()

    def test_rebac_delete_object_tuples(self, gateway, mock_fs):
        """rebac_delete_object_tuples lists then deletes each tuple."""
        result = gateway.rebac_delete_object_tuples(object=("file", "/test"), zone_id="z1")
        assert result == 3
        mock_fs.service("rebac").rebac_list_tuples_sync.assert_called_once()
        assert mock_fs.service("rebac").rebac_delete_sync.call_count == 3

    def test_rebac_list_tuples(self, gateway, mock_fs):
        """rebac_list_tuples delegates to rebac_service."""
        gateway.rebac_list_tuples(subject=("user", "alice"), relation="viewer")
        mock_fs.service("rebac").rebac_list_tuples_sync.assert_called()

    def test_rebac_delete(self, gateway, mock_fs):
        """rebac_delete delegates to rebac_service."""
        result = gateway.rebac_delete("tuple-123")
        assert result is True
        mock_fs.service("rebac").rebac_delete_sync.assert_called_with("tuple-123")

    def test_rebac_delete_no_manager(self, mock_fs):
        """rebac_delete delegates to rebac_service even without rebac_manager."""
        mock_fs._rebac_manager = None
        gw = NexusFSGateway(mock_fs)
        result = gw.rebac_delete("tuple-123")
        assert result is True

    def test_rebac_manager_property(self, gateway, mock_fs):
        """rebac_manager property returns the manager."""
        assert gateway.rebac_manager is mock_fs._rebac_manager


# =============================================================================
# Hierarchy Operations
# =============================================================================


class TestHierarchyOperations:
    """Tests for hierarchy operation delegation."""

    def test_hierarchy_enabled(self, gateway):
        """hierarchy_enabled returns True when hierarchy is enabled."""
        assert gateway.hierarchy_enabled is True

    def test_hierarchy_disabled(self, mock_fs):
        """hierarchy_enabled returns False when rebac_manager is None."""
        mock_fs._rebac_manager = None
        gw = NexusFSGateway(mock_fs)
        assert gw.hierarchy_enabled is False

    def test_ensure_parent_tuples_batch(self, gateway, mock_fs):
        """ensure_parent_tuples_batch delegates to hierarchy manager."""
        result = gateway.ensure_parent_tuples_batch(["/a", "/b"], zone_id="z1")
        assert result == 2

    def test_remove_parent_tuples(self, gateway, mock_fs):
        """remove_parent_tuples delegates to hierarchy manager."""
        result = gateway.remove_parent_tuples("/test", zone_id="z1")
        assert result == 1

    def test_ensure_parent_tuples_no_manager(self, mock_fs):
        """Returns 0 when rebac_manager is None."""
        mock_fs._rebac_manager = None
        gw = NexusFSGateway(mock_fs)
        assert gw.ensure_parent_tuples_batch(["/a"]) == 0

    def test_remove_parent_tuples_no_manager(self, mock_fs):
        """Returns 0 when rebac_manager is None."""
        mock_fs._rebac_manager = None
        gw = NexusFSGateway(mock_fs)
        assert gw.remove_parent_tuples("/test") == 0


# =============================================================================
# Router / Session / Backend Properties
# =============================================================================


class TestProperties:
    """Tests for property access."""

    def test_router_property(self, gateway, mock_fs):
        """router returns NexusFS router."""
        assert gateway.router is mock_fs.router

    def test_session_factory_property(self, gateway, mock_fs):
        """session_factory returns SessionLocal."""
        assert gateway.session_factory is mock_fs.SessionLocal

    def test_session_factory_none(self, mock_fs):
        """session_factory returns None when not available."""
        del mock_fs.SessionLocal
        gw = NexusFSGateway(mock_fs)
        assert gw.session_factory is None

    def test_backend_property(self, gateway, mock_fs):
        """backend returns NexusFS backend."""
        assert gateway.backend is mock_fs.backend


# =============================================================================
# Search Operations
# =============================================================================


class TestSearchOperations:
    """Tests for search-related delegation."""

    def test_read_file(self, gateway, mock_fs, context):
        """read_file delegates to NexusFS read() (Tier 2 convenience)."""
        gateway.read_file("/test/file.txt", context=context, return_metadata=True)
        mock_fs.read.assert_called_once_with(
            "/test/file.txt", context=context, return_metadata=True
        )

    def test_read_bulk(self, gateway, mock_fs, context):
        """read_bulk delegates to NexusFS read_bulk."""
        result = gateway.read_bulk(["/a"], context=context)
        assert result == {"/a": b"data"}

    def test_get_routing_params(self, gateway, mock_fs, context):
        """get_routing_params delegates to NexusFS."""
        zone, agent, admin = gateway.get_routing_params(context)
        assert zone == "zone1"
        assert agent == "agent1"
        assert admin is False

    def test_has_descendant_access(self, gateway, mock_fs, context):
        """has_descendant_access delegates to NexusFS."""
        from nexus.contracts.types import Permission

        result = gateway.has_descendant_access("/test", Permission.READ, context)
        assert result is True


# =============================================================================
# Mount Operations
# =============================================================================


class TestMountOperations:
    """Tests for mount listing and path resolution."""

    def test_list_mounts(self, gateway, mock_fs):
        """list_mounts returns formatted mount list."""
        mount_info = MagicMock()
        mount_info.mount_point = "/mnt/test"
        mount_info.backend = MagicMock()
        type(mount_info.backend).__name__ = "CASLocalBackend"
        mount_info.conflict_strategy = "latest"
        mock_fs.router.list_mounts.return_value = [mount_info]

        result = gateway.list_mounts()
        assert len(result) == 1
        assert result[0]["mount_point"] == "/mnt/test"
        assert result[0]["backend_type"] == "CASLocalBackend"

    def test_get_mount_for_path_found(self, gateway, mock_fs):
        """get_mount_for_path returns mount info."""
        mount_info = MagicMock()
        mount_info.mount_point = "/mnt"
        mount_info.backend = MagicMock()
        mount_info.backend.name = "my_backend"
        mount_info.conflict_strategy = "latest"
        mock_fs.router.list_mounts.return_value = [mount_info]

        result = gateway.get_mount_for_path("/mnt/subdir/file.txt")
        assert result is not None
        assert result["mount_point"] == "/mnt"
        assert result["backend_path"] == "subdir/file.txt"

    def test_get_mount_for_path_root(self, gateway, mock_fs):
        """get_mount_for_path handles root mount."""
        mount_info = MagicMock()
        mount_info.mount_point = "/"
        mount_info.backend = MagicMock()
        mount_info.backend.name = "root_backend"
        mount_info.conflict_strategy = "latest"
        mock_fs.router.list_mounts.return_value = [mount_info]

        result = gateway.get_mount_for_path("/any/path")
        assert result is not None
        assert result["mount_point"] == "/"

    def test_get_mount_for_path_not_found(self, gateway, mock_fs):
        """get_mount_for_path returns None when no mount matches."""
        mock_fs.router.list_mounts.return_value = []
        result = gateway.get_mount_for_path("/orphan/path")
        assert result is None
