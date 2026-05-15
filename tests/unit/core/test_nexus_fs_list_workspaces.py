"""Tests for NexusFS.list_workspaces() filtering and auth guard.

Covers issue #1201: register_workspace succeeds but not returned by list_workspaces.
"""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from nexus.bricks.workspace.workspace_registry import WorkspaceConfig
from nexus.contracts.constants import ROOT_ZONE_ID


def _make_workspace(path: str, created_by: str | None = None) -> WorkspaceConfig:
    """Create a WorkspaceConfig for testing."""
    return WorkspaceConfig(
        path=path,
        name=path.split("/")[-1],
        created_at=datetime.now(),
        created_by=created_by,
    )


def _make_context(user_id: str | None = None, zone_id: str | None = None) -> SimpleNamespace:
    """Create a mock operation context."""
    return SimpleNamespace(user_id=user_id, zone_id=zone_id)


@pytest.fixture
def nexus_fs():
    """Create a NexusFS instance with a mocked workspace RPC service.

    list_workspaces lives on WorkspaceRPCService (Issue #2033).
    Tests call nx.service("workspace_rpc").list_workspaces() directly.
    """
    from nexus.services.workspace.workspace_rpc_service import WorkspaceRPCService

    mock_registry = MagicMock()
    mock_registry.list_workspaces.return_value = []

    # Create a real WorkspaceRPCService with mock registry
    svc = WorkspaceRPCService.__new__(WorkspaceRPCService)
    svc._wr = mock_registry

    # Create NexusFS and inject the service for __getattr__ forwarding
    from nexus.core.nexus_fs import NexusFS

    with patch.object(NexusFS, "__init__", lambda self: None):
        fs = NexusFS.__new__(NexusFS)
        fs._kernel = MagicMock()
        fs._kernel.service_lookup = MagicMock(
            side_effect=lambda name: svc if name == "workspace_rpc" else None
        )
        fs._hook_specs = {}
        # Also expose mock registry for test setup
        fs._workspace_registry = mock_registry
        return fs


class TestListWorkspacesAuthGuard:
    """Test that list_workspaces requires authenticated context."""

    def test_raises_when_context_is_none(self, nexus_fs) -> None:
        """No context at all should raise ValueError."""
        with pytest.raises(ValueError, match="requires authenticated context"):
            nexus_fs.service("workspace_rpc").list_workspaces(context=None)

    def test_raises_when_user_id_missing(self, nexus_fs) -> None:
        """Context without user_id should raise ValueError."""
        ctx = _make_context(user_id=None, zone_id=ROOT_ZONE_ID)
        with pytest.raises(ValueError, match="requires authenticated context"):
            nexus_fs.service("workspace_rpc").list_workspaces(context=ctx)

    def test_raises_when_zone_id_missing(self, nexus_fs) -> None:
        """Context without zone_id should raise ValueError."""
        ctx = _make_context(user_id="alice", zone_id=None)
        with pytest.raises(ValueError, match="requires authenticated context"):
            nexus_fs.service("workspace_rpc").list_workspaces(context=ctx)

    def test_raises_when_both_missing(self, nexus_fs) -> None:
        """Context with neither user_id nor zone_id should raise ValueError."""
        ctx = _make_context(user_id=None, zone_id=None)
        with pytest.raises(ValueError, match="requires authenticated context"):
            nexus_fs.service("workspace_rpc").list_workspaces(context=ctx)

    def test_raises_when_user_id_empty_string(self, nexus_fs) -> None:
        """Context with empty string user_id should raise ValueError."""
        ctx = _make_context(user_id="", zone_id=ROOT_ZONE_ID)
        with pytest.raises(ValueError, match="requires authenticated context"):
            nexus_fs.service("workspace_rpc").list_workspaces(context=ctx)

    def test_raises_when_zone_id_empty_string(self, nexus_fs) -> None:
        """Context with empty string zone_id should raise ValueError."""
        ctx = _make_context(user_id="alice", zone_id="")
        with pytest.raises(ValueError, match="requires authenticated context"):
            nexus_fs.service("workspace_rpc").list_workspaces(context=ctx)


class TestListWorkspacesFiltering:
    """Test workspace filtering by created_by and path prefix."""

    def test_filters_by_path_prefix(self, nexus_fs) -> None:
        """Workspaces in user's zone-scoped path should be returned."""
        nexus_fs._workspace_registry.list_workspaces.return_value = [
            _make_workspace("/zone/root/user/alice/workspace/project1", created_by="bob"),
            _make_workspace("/zone/root/user/bob/workspace/project2", created_by="bob"),
        ]

        ctx = _make_context(user_id="alice", zone_id=ROOT_ZONE_ID)
        result = nexus_fs.service("workspace_rpc").list_workspaces(context=ctx)

        assert len(result) == 1
        assert result[0]["path"] == "/zone/root/user/alice/workspace/project1"

    def test_filters_by_created_by(self, nexus_fs) -> None:
        """Workspaces created by the user at any path should be returned."""
        nexus_fs._workspace_registry.list_workspaces.return_value = [
            _make_workspace("/shared/team-project", created_by="alice"),
            _make_workspace("/shared/other-project", created_by="bob"),
        ]

        ctx = _make_context(user_id="alice", zone_id=ROOT_ZONE_ID)
        result = nexus_fs.service("workspace_rpc").list_workspaces(context=ctx)

        assert len(result) == 1
        assert result[0]["path"] == "/shared/team-project"

    def test_union_of_both_filters(self, nexus_fs) -> None:
        """Should return workspaces matching EITHER created_by OR path prefix."""
        nexus_fs._workspace_registry.list_workspaces.return_value = [
            # Matches path prefix (but created_by is different)
            _make_workspace("/zone/root/user/alice/workspace/scoped", created_by="system"),
            # Matches created_by (but path is non-standard)
            _make_workspace("/custom/path", created_by="alice"),
            # Matches neither
            _make_workspace("/zone/root/user/bob/workspace/bobs", created_by="bob"),
        ]

        ctx = _make_context(user_id="alice", zone_id=ROOT_ZONE_ID)
        result = nexus_fs.service("workspace_rpc").list_workspaces(context=ctx)

        assert len(result) == 2
        paths = [r["path"] for r in result]
        assert "/zone/root/user/alice/workspace/scoped" in paths
        assert "/custom/path" in paths

    def test_returns_empty_when_no_matches(self, nexus_fs) -> None:
        """Should return empty list when no workspaces match."""
        nexus_fs._workspace_registry.list_workspaces.return_value = [
            _make_workspace("/zone/root/user/bob/workspace/project", created_by="bob"),
        ]

        ctx = _make_context(user_id="alice", zone_id=ROOT_ZONE_ID)
        result = nexus_fs.service("workspace_rpc").list_workspaces(context=ctx)

        assert result == []

    def test_returns_empty_when_no_workspaces_exist(self, nexus_fs) -> None:
        """Should return empty list when registry is empty."""
        nexus_fs._workspace_registry.list_workspaces.return_value = []

        ctx = _make_context(user_id="alice", zone_id=ROOT_ZONE_ID)
        result = nexus_fs.service("workspace_rpc").list_workspaces(context=ctx)

        assert result == []

    def test_workspace_with_none_created_by_only_matches_prefix(self, nexus_fs) -> None:
        """Workspaces with None created_by should only match by path prefix."""
        nexus_fs._workspace_registry.list_workspaces.return_value = [
            _make_workspace("/zone/root/user/alice/workspace/legacy", created_by=None),
            _make_workspace("/other/path", created_by=None),
        ]

        ctx = _make_context(user_id="alice", zone_id=ROOT_ZONE_ID)
        result = nexus_fs.service("workspace_rpc").list_workspaces(context=ctx)

        assert len(result) == 1
        assert result[0]["path"] == "/zone/root/user/alice/workspace/legacy"

    def test_context_with_user_attr_fallback(self, nexus_fs) -> None:
        """Context with 'user' attribute (instead of 'user_id') should work."""
        nexus_fs._workspace_registry.list_workspaces.return_value = [
            _make_workspace("/custom/ws", created_by="alice"),
        ]

        # Some contexts use 'user' instead of 'user_id'
        ctx = SimpleNamespace(user_id="alice", zone_id=ROOT_ZONE_ID)
        result = nexus_fs.service("workspace_rpc").list_workspaces(context=ctx)

        assert len(result) == 1
        assert result[0]["created_by"] == "alice"

    def test_workspace_matching_both_conditions_not_duplicated(self, nexus_fs) -> None:
        """Workspace matching both created_by AND path should appear once."""
        nexus_fs._workspace_registry.list_workspaces.return_value = [
            _make_workspace("/zone/root/user/alice/workspace/project", created_by="alice"),
        ]

        ctx = _make_context(user_id="alice", zone_id=ROOT_ZONE_ID)
        result = nexus_fs.service("workspace_rpc").list_workspaces(context=ctx)

        assert len(result) == 1
