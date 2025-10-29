"""Tests for WorkspaceManager ReBAC permission checks.

Tests the security fix for P0 issue: workspace snapshots now require
proper ReBAC permissions to prevent cross-tenant access.
"""

from unittest.mock import MagicMock

import pytest

from nexus.core.exceptions import NexusPermissionError
from nexus.core.workspace_manager import WorkspaceManager
from nexus.storage.models import WorkspaceSnapshotModel


@pytest.fixture
def mock_metadata():
    """Mock metadata store."""
    metadata = MagicMock()
    metadata.SessionLocal = MagicMock()
    return metadata


@pytest.fixture
def mock_backend():
    """Mock backend."""
    return MagicMock()


@pytest.fixture
def mock_rebac_manager():
    """Mock ReBAC manager."""
    return MagicMock()


@pytest.fixture
def workspace_manager(mock_metadata, mock_backend, mock_rebac_manager):
    """Create WorkspaceManager with mocked dependencies."""
    return WorkspaceManager(
        metadata=mock_metadata,
        backend=mock_backend,
        rebac_manager=mock_rebac_manager,
        tenant_id="tenant1",
        agent_id="agent1",
    )


class TestWorkspaceManagerPermissions:
    """Test ReBAC permission enforcement in WorkspaceManager."""

    def test_create_snapshot_permission_granted(
        self, workspace_manager, mock_rebac_manager, mock_metadata, mock_backend
    ):
        """Test that create_snapshot succeeds when permission is granted."""
        # Setup
        mock_rebac_manager.rebac_check.return_value = True
        mock_backend.write_content.return_value = "manifest_hash_123"

        # Mock database session
        mock_session = MagicMock()
        mock_metadata.SessionLocal.return_value.__enter__.return_value = mock_session
        mock_metadata.list.return_value = []  # Empty workspace
        mock_session.execute.return_value.scalar.return_value = None  # No previous snapshots

        # Execute
        result = workspace_manager.create_snapshot(
            workspace_path="/test-workspace",
            description="Test snapshot",
        )

        # Verify permission was checked
        # Note: snapshot:create maps to "write" on "file" object (workspaces are directories)
        mock_rebac_manager.rebac_check.assert_called_once_with(
            subject=("agent", "agent1"),
            permission="write",
            object=("file", "/test-workspace"),
            tenant_id="tenant1",
        )

        # Verify snapshot was created
        assert result["snapshot_number"] == 1

    def test_create_snapshot_permission_denied(self, workspace_manager, mock_rebac_manager):
        """Test that create_snapshot raises NexusPermissionError when permission denied."""
        # Setup: Deny permission
        mock_rebac_manager.rebac_check.return_value = False

        # Execute & Verify
        with pytest.raises(NexusPermissionError, match="snapshot:create"):
            workspace_manager.create_snapshot(
                workspace_path="/test-workspace",
                description="Test snapshot",
            )

    def test_create_snapshot_no_agent_id(self, mock_metadata, mock_backend, mock_rebac_manager):
        """Test that create_snapshot denies access when no agent_id provided."""
        # Setup: WorkspaceManager without agent_id
        manager = WorkspaceManager(
            metadata=mock_metadata,
            backend=mock_backend,
            rebac_manager=mock_rebac_manager,
            tenant_id="tenant1",
            agent_id=None,  # No agent ID
        )

        # Execute & Verify
        with pytest.raises(NexusPermissionError, match="no user_id or agent_id provided"):
            manager.create_snapshot(
                workspace_path="/test-workspace",
                description="Test snapshot",
            )

    def test_restore_snapshot_permission_granted(
        self, workspace_manager, mock_rebac_manager, mock_metadata, mock_backend
    ):
        """Test that restore_snapshot succeeds when permission is granted."""
        # Setup
        mock_rebac_manager.rebac_check.return_value = True

        # Mock snapshot lookup
        mock_session = MagicMock()
        mock_metadata.SessionLocal.return_value.__enter__.return_value = mock_session

        mock_snapshot = WorkspaceSnapshotModel(
            snapshot_id="snap123",
            workspace_path="/test-workspace",
            snapshot_number=1,
            manifest_hash="hash123",
            file_count=5,
            total_size_bytes=1000,
        )
        mock_session.get.return_value = mock_snapshot
        mock_metadata.list.return_value = []

        # Mock manifest read
        mock_backend.read_content.return_value = b"{}"

        # Execute
        result = workspace_manager.restore_snapshot(snapshot_id="snap123")

        # Verify permission was checked
        # Note: snapshot:restore maps to "write" on "file" object
        mock_rebac_manager.rebac_check.assert_called_once_with(
            subject=("agent", "agent1"),
            permission="write",
            object=("file", "/test-workspace"),
            tenant_id="tenant1",
        )

        assert result["snapshot_info"]["snapshot_id"] == "snap123"

    def test_restore_snapshot_permission_denied(
        self, workspace_manager, mock_rebac_manager, mock_metadata
    ):
        """Test that restore_snapshot raises NexusPermissionError when permission denied."""
        # Setup
        mock_session = MagicMock()
        mock_metadata.SessionLocal.return_value.__enter__.return_value = mock_session

        mock_snapshot = WorkspaceSnapshotModel(
            snapshot_id="snap123",
            workspace_path="/test-workspace",
            snapshot_number=1,
            manifest_hash="hash123",
            file_count=5,
            total_size_bytes=1000,
        )
        mock_session.get.return_value = mock_snapshot

        # Deny permission
        mock_rebac_manager.rebac_check.return_value = False

        # Execute & Verify
        with pytest.raises(NexusPermissionError, match="snapshot:restore"):
            workspace_manager.restore_snapshot(snapshot_id="snap123")

    def test_list_snapshots_permission_granted(
        self, workspace_manager, mock_rebac_manager, mock_metadata
    ):
        """Test that list_snapshots succeeds when permission is granted."""
        # Setup
        mock_rebac_manager.rebac_check.return_value = True

        mock_session = MagicMock()
        mock_metadata.SessionLocal.return_value.__enter__.return_value = mock_session
        mock_session.execute.return_value.scalars.return_value.all.return_value = []

        # Execute
        result = workspace_manager.list_snapshots(workspace_path="/test-workspace")

        # Verify permission was checked
        # Note: snapshot:list maps to "read" on "file" object
        mock_rebac_manager.rebac_check.assert_called_once_with(
            subject=("agent", "agent1"),
            permission="read",
            object=("file", "/test-workspace"),
            tenant_id="tenant1",
        )

        assert isinstance(result, list)

    def test_list_snapshots_permission_denied(self, workspace_manager, mock_rebac_manager):
        """Test that list_snapshots raises NexusPermissionError when permission denied."""
        # Setup: Deny permission
        mock_rebac_manager.rebac_check.return_value = False

        # Execute & Verify
        with pytest.raises(NexusPermissionError, match="snapshot:list"):
            workspace_manager.list_snapshots(workspace_path="/test-workspace")

    def test_diff_snapshots_permission_granted(
        self, workspace_manager, mock_rebac_manager, mock_metadata, mock_backend
    ):
        """Test that diff_snapshots succeeds when permission is granted."""
        # Setup
        mock_rebac_manager.rebac_check.return_value = True

        mock_session = MagicMock()
        mock_metadata.SessionLocal.return_value.__enter__.return_value = mock_session

        # Mock two snapshots from same workspace
        mock_snap1 = WorkspaceSnapshotModel(
            snapshot_id="snap1",
            workspace_path="/test-workspace",
            snapshot_number=1,
            manifest_hash="hash1",
            file_count=3,
            total_size_bytes=500,
        )
        mock_snap2 = WorkspaceSnapshotModel(
            snapshot_id="snap2",
            workspace_path="/test-workspace",
            snapshot_number=2,
            manifest_hash="hash2",
            file_count=5,
            total_size_bytes=1000,
        )
        mock_session.get.side_effect = [mock_snap1, mock_snap2]

        # Mock manifest reads
        mock_backend.read_content.side_effect = [b"{}", b"{}"]

        # Execute
        result = workspace_manager.diff_snapshots("snap1", "snap2")

        # Verify permission was checked (only once since same workspace)
        # Note: snapshot:diff maps to "read" on "file" object
        mock_rebac_manager.rebac_check.assert_called_once_with(
            subject=("agent", "agent1"),
            permission="read",
            object=("file", "/test-workspace"),
            tenant_id="tenant1",
        )

        assert "added" in result
        assert "removed" in result
        assert "modified" in result

    def test_diff_snapshots_different_workspaces_both_granted(
        self, workspace_manager, mock_rebac_manager, mock_metadata, mock_backend
    ):
        """Test that diff_snapshots checks permissions for both workspaces."""
        # Setup
        mock_rebac_manager.rebac_check.return_value = True

        mock_session = MagicMock()
        mock_metadata.SessionLocal.return_value.__enter__.return_value = mock_session

        # Mock two snapshots from DIFFERENT workspaces
        mock_snap1 = WorkspaceSnapshotModel(
            snapshot_id="snap1",
            workspace_path="/workspace-a",
            snapshot_number=1,
            manifest_hash="hash1",
            file_count=3,
            total_size_bytes=500,
        )
        mock_snap2 = WorkspaceSnapshotModel(
            snapshot_id="snap2",
            workspace_path="/workspace-b",
            snapshot_number=1,
            manifest_hash="hash2",
            file_count=5,
            total_size_bytes=1000,
        )
        mock_session.get.side_effect = [mock_snap1, mock_snap2]

        # Mock manifest reads
        mock_backend.read_content.side_effect = [b"{}", b"{}"]

        # Execute
        workspace_manager.diff_snapshots("snap1", "snap2")

        # Verify permission was checked for BOTH workspaces
        # Note: Checks use "file" object type (workspaces are directories)
        assert mock_rebac_manager.rebac_check.call_count == 2
        calls = mock_rebac_manager.rebac_check.call_args_list

        assert calls[0][1]["object"] == ("file", "/workspace-a")
        assert calls[1][1]["object"] == ("file", "/workspace-b")

    def test_diff_snapshots_permission_denied_first_workspace(
        self, workspace_manager, mock_rebac_manager, mock_metadata
    ):
        """Test that diff_snapshots denies access if first workspace permission denied."""
        # Setup
        mock_session = MagicMock()
        mock_metadata.SessionLocal.return_value.__enter__.return_value = mock_session

        mock_snap1 = WorkspaceSnapshotModel(
            snapshot_id="snap1",
            workspace_path="/workspace-a",
            snapshot_number=1,
            manifest_hash="hash1",
            file_count=3,
            total_size_bytes=500,
        )
        mock_snap2 = WorkspaceSnapshotModel(
            snapshot_id="snap2",
            workspace_path="/workspace-b",
            snapshot_number=1,
            manifest_hash="hash2",
            file_count=5,
            total_size_bytes=1000,
        )
        mock_session.get.side_effect = [mock_snap1, mock_snap2]

        # Deny permission for first workspace
        mock_rebac_manager.rebac_check.return_value = False

        # Execute & Verify
        with pytest.raises(NexusPermissionError, match="snapshot:diff"):
            workspace_manager.diff_snapshots("snap1", "snap2")

    def test_no_rebac_manager_allows_operations(self, mock_metadata, mock_backend):
        """Test that operations are allowed when no ReBAC manager is configured (backward compatibility)."""
        # Setup: WorkspaceManager without ReBAC
        manager = WorkspaceManager(
            metadata=mock_metadata,
            backend=mock_backend,
            rebac_manager=None,  # No ReBAC manager
            tenant_id="tenant1",
            agent_id="agent1",
        )

        mock_backend.write_content.return_value = "manifest_hash_123"
        mock_session = MagicMock()
        mock_metadata.SessionLocal.return_value.__enter__.return_value = mock_session
        mock_metadata.list.return_value = []
        mock_session.execute.return_value.scalar.return_value = None

        # Execute - should succeed without permission check
        result = manager.create_snapshot(
            workspace_path="/test-workspace",
            description="Test snapshot",
        )

        # Verify no exception was raised
        assert result["snapshot_number"] == 1

    def test_cross_tenant_protection(self, mock_metadata, mock_backend, mock_rebac_manager):
        """Test that cross-tenant snapshot access is prevented."""
        # Setup: Agent from tenant1 trying to access tenant2's workspace
        manager = WorkspaceManager(
            metadata=mock_metadata,
            backend=mock_backend,
            rebac_manager=mock_rebac_manager,
            tenant_id="tenant1",
            agent_id="agent1",
        )

        # Deny cross-tenant access
        mock_rebac_manager.rebac_check.return_value = False

        # Execute & Verify
        with pytest.raises(NexusPermissionError):
            manager.create_snapshot(
                workspace_path="/tenant2-workspace",
                description="Attempting cross-tenant access",
            )

        # Verify the permission check included tenant_id
        mock_rebac_manager.rebac_check.assert_called_once()
        call_args = mock_rebac_manager.rebac_check.call_args
        assert call_args[1]["tenant_id"] == "tenant1"
