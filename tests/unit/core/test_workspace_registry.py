"""Tests for WorkspaceRegistry.

These tests verify workspace registration functionality.
"""

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from nexus.bricks.workspace.workspace_registry import (
    WorkspaceConfig,
    WorkspaceRegistry,
)


class TestWorkspaceConfig:
    """Test WorkspaceConfig dataclass."""

    def test_init_minimal(self) -> None:
        """Test minimal initialization."""
        config = WorkspaceConfig(path="/my-workspace")
        assert config.path == "/my-workspace"
        assert config.name is None
        assert config.description == ""
        assert config.metadata == {}

    def test_init_full(self) -> None:
        """Test full initialization."""
        now = datetime.now()
        config = WorkspaceConfig(
            path="/workspace",
            name="Main Workspace",
            description="Test workspace",
            created_at=now,
            created_by="alice",
            metadata={"key": "value"},
        )
        assert config.path == "/workspace"
        assert config.name == "Main Workspace"
        assert config.description == "Test workspace"
        assert config.created_at == now
        assert config.created_by == "alice"
        assert config.metadata == {"key": "value"}

    def test_to_dict(self) -> None:
        """Test to_dict conversion."""
        now = datetime.now()
        config = WorkspaceConfig(
            path="/workspace",
            name="Test",
            created_at=now,
            created_by="bob",
        )
        result = config.to_dict()
        assert result["path"] == "/workspace"
        assert result["name"] == "Test"
        assert result["created_at"] == now.isoformat()
        assert result["created_by"] == "bob"

    def test_to_dict_no_created_at(self) -> None:
        """Test to_dict with no created_at."""
        config = WorkspaceConfig(path="/workspace")
        result = config.to_dict()
        assert result["created_at"] is None


class TestWorkspaceRegistry:
    """Test WorkspaceRegistry functionality."""

    @pytest.fixture
    def mock_record_store(self) -> MagicMock:
        """Create mock record_store with session_factory to avoid real DB initialization."""
        mock_session = MagicMock()
        mock_session.query.return_value.all.return_value = []
        mock_session.__enter__ = lambda self: mock_session
        mock_session.__exit__ = lambda self, *args: None
        factory = MagicMock(return_value=mock_session)
        mock_rs = MagicMock()
        mock_rs.session_factory = factory
        return mock_rs

    @pytest.fixture
    def mock_metadata(self) -> MagicMock:
        """Create mock metadata store."""
        return MagicMock()

    @pytest.fixture
    def registry(self, mock_metadata: MagicMock, mock_record_store: MagicMock) -> WorkspaceRegistry:
        """Create registry instance with mocked metadata."""
        with patch("nexus.bricks.workspace.workspace_registry.WorkspaceRegistry._load_from_db"):
            reg = WorkspaceRegistry(mock_metadata, record_store=mock_record_store)
            reg._workspaces = {}
            return reg

    def test_init(self, mock_metadata: MagicMock, mock_record_store: MagicMock) -> None:
        """Test registry initialization."""
        with patch("nexus.bricks.workspace.workspace_registry.WorkspaceRegistry._load_from_db"):
            registry = WorkspaceRegistry(mock_metadata, record_store=mock_record_store)
            assert registry.metadata == mock_metadata
            assert registry.rebac_manager is None

    def test_register_workspace(self, registry: WorkspaceRegistry) -> None:
        """Test workspace registration."""
        with patch.object(registry, "_save_workspace_to_db"):
            config = registry.register_workspace(
                path="/my-workspace",
                name="Test Workspace",
                description="A test workspace",
            )
            assert config.path == "/my-workspace"
            assert config.name == "Test Workspace"
            assert config.description == "A test workspace"
            assert "/my-workspace" in registry._workspaces

    def test_register_workspace_duplicate_raises(self, registry: WorkspaceRegistry) -> None:
        """Test that registering duplicate workspace raises."""
        with patch.object(registry, "_save_workspace_to_db"):
            registry.register_workspace("/workspace")

        with pytest.raises(ValueError, match="already registered"):
            registry.register_workspace("/workspace")

    def test_unregister_workspace(self, registry: WorkspaceRegistry) -> None:
        """Test workspace unregistration."""
        with patch.object(registry, "_save_workspace_to_db"):
            registry.register_workspace("/workspace")

        with patch.object(registry, "_delete_workspace_from_db"):
            result = registry.unregister_workspace("/workspace")
            assert result is True
            assert "/workspace" not in registry._workspaces

    def test_unregister_workspace_not_found(self, registry: WorkspaceRegistry) -> None:
        """Test unregistering non-existent workspace."""
        result = registry.unregister_workspace("/nonexistent")
        assert result is False

    def test_get_workspace(self, registry: WorkspaceRegistry) -> None:
        """Test getting workspace by path."""
        with patch.object(registry, "_save_workspace_to_db"):
            registry.register_workspace("/workspace", name="Test")

        config = registry.get_workspace("/workspace")
        assert config is not None
        assert config.name == "Test"

    def test_get_workspace_not_found(self, registry: WorkspaceRegistry) -> None:
        """Test getting non-existent workspace."""
        config = registry.get_workspace("/nonexistent")
        assert config is None

    def test_find_workspace_for_path_exact(self, registry: WorkspaceRegistry) -> None:
        """Test finding workspace for exact path."""
        with patch.object(registry, "_save_workspace_to_db"):
            registry.register_workspace("/my-workspace")

        config = registry.find_workspace_for_path("/my-workspace")
        assert config is not None
        assert config.path == "/my-workspace"

    def test_find_workspace_for_path_nested(self, registry: WorkspaceRegistry) -> None:
        """Test finding workspace for nested path."""
        with patch.object(registry, "_save_workspace_to_db"):
            registry.register_workspace("/my-workspace")

        config = registry.find_workspace_for_path("/my-workspace/subdir/file.txt")
        assert config is not None
        assert config.path == "/my-workspace"

    def test_find_workspace_for_path_not_found(self, registry: WorkspaceRegistry) -> None:
        """Test finding workspace for unregistered path."""
        config = registry.find_workspace_for_path("/random/path")
        assert config is None

    def test_list_workspaces(self, registry: WorkspaceRegistry) -> None:
        """Test listing workspaces."""
        with patch.object(registry, "_save_workspace_to_db"):
            registry.register_workspace("/ws1", name="WS1")
            registry.register_workspace("/ws2", name="WS2")

        workspaces = registry.list_workspaces()
        assert len(workspaces) == 2
        paths = [ws.path for ws in workspaces]
        assert "/ws1" in paths
        assert "/ws2" in paths

    def test_register_workspace_with_context_dict(self, registry: WorkspaceRegistry) -> None:
        """Test workspace registration with context as dict."""
        with patch.object(registry, "_save_workspace_to_db"):
            context = {"user_id": "alice", "zone_id": "root"}
            config = registry.register_workspace(
                path="/workspace",
                context=context,
            )
            assert config.created_by == "alice"

    def test_register_workspace_with_metadata(self, registry: WorkspaceRegistry) -> None:
        """Test workspace registration with metadata."""
        with patch.object(registry, "_save_workspace_to_db"):
            config = registry.register_workspace(
                path="/workspace",
                metadata={"key": "value", "count": 42},
            )
            assert config.metadata == {"key": "value", "count": 42}
