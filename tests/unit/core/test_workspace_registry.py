"""Tests for WorkspaceRegistry.

These tests verify workspace and memory registration functionality.
"""

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from nexus.core.workspace_registry import (
    MemoryConfig,
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


class TestMemoryConfig:
    """Test MemoryConfig dataclass."""

    def test_init_minimal(self) -> None:
        """Test minimal initialization."""
        config = MemoryConfig(path="/my-memory")
        assert config.path == "/my-memory"
        assert config.name is None
        assert config.description == ""

    def test_init_full(self) -> None:
        """Test full initialization."""
        now = datetime.now()
        config = MemoryConfig(
            path="/memory",
            name="Knowledge Base",
            description="Test memory",
            created_at=now,
            created_by="alice",
            metadata={"type": "kb"},
        )
        assert config.path == "/memory"
        assert config.name == "Knowledge Base"
        assert config.description == "Test memory"
        assert config.created_at == now
        assert config.created_by == "alice"
        assert config.metadata == {"type": "kb"}

    def test_to_dict(self) -> None:
        """Test to_dict conversion."""
        now = datetime.now()
        config = MemoryConfig(
            path="/memory",
            name="KB",
            created_at=now,
        )
        result = config.to_dict()
        assert result["path"] == "/memory"
        assert result["name"] == "KB"
        assert result["created_at"] == now.isoformat()


class TestWorkspaceRegistry:
    """Test WorkspaceRegistry functionality."""

    @pytest.fixture
    def mock_metadata(self) -> MagicMock:
        """Create mock metadata store."""
        mock = MagicMock()
        mock_session = MagicMock()
        mock_session.query.return_value.all.return_value = []
        mock_session.__enter__ = lambda self: mock_session
        mock_session.__exit__ = lambda self, *args: None
        mock.SessionLocal.return_value = mock_session
        return mock

    @pytest.fixture
    def registry(self, mock_metadata: MagicMock) -> WorkspaceRegistry:
        """Create registry instance with mocked metadata."""
        with patch("nexus.core.workspace_registry.WorkspaceRegistry._load_from_db"):
            reg = WorkspaceRegistry(mock_metadata)
            reg._workspaces = {}
            reg._memories = {}
            return reg

    def test_init(self, mock_metadata: MagicMock) -> None:
        """Test registry initialization."""
        with patch("nexus.core.workspace_registry.WorkspaceRegistry._load_from_db"):
            registry = WorkspaceRegistry(mock_metadata)
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

    def test_register_memory(self, registry: WorkspaceRegistry) -> None:
        """Test memory registration."""
        with patch.object(registry, "_save_memory_to_db"):
            config = registry.register_memory(
                path="/my-memory",
                name="Test Memory",
                description="A test memory",
            )
            assert config.path == "/my-memory"
            assert config.name == "Test Memory"
            assert config.description == "A test memory"
            assert "/my-memory" in registry._memories

    def test_register_memory_duplicate_raises(self, registry: WorkspaceRegistry) -> None:
        """Test that registering duplicate memory raises."""
        with patch.object(registry, "_save_memory_to_db"):
            registry.register_memory("/memory")

        with pytest.raises(ValueError, match="already registered"):
            registry.register_memory("/memory")

    def test_unregister_memory(self, registry: WorkspaceRegistry) -> None:
        """Test memory unregistration."""
        with patch.object(registry, "_save_memory_to_db"):
            registry.register_memory("/memory")

        with patch.object(registry, "_delete_memory_from_db"):
            result = registry.unregister_memory("/memory")
            assert result is True
            assert "/memory" not in registry._memories

    def test_unregister_memory_not_found(self, registry: WorkspaceRegistry) -> None:
        """Test unregistering non-existent memory."""
        result = registry.unregister_memory("/nonexistent")
        assert result is False

    def test_get_memory(self, registry: WorkspaceRegistry) -> None:
        """Test getting memory by path."""
        with patch.object(registry, "_save_memory_to_db"):
            registry.register_memory("/memory", name="KB")

        config = registry.get_memory("/memory")
        assert config is not None
        assert config.name == "KB"

    def test_get_memory_not_found(self, registry: WorkspaceRegistry) -> None:
        """Test getting non-existent memory."""
        config = registry.get_memory("/nonexistent")
        assert config is None

    def test_find_memory_for_path_exact(self, registry: WorkspaceRegistry) -> None:
        """Test finding memory for exact path."""
        with patch.object(registry, "_save_memory_to_db"):
            registry.register_memory("/my-memory")

        config = registry.find_memory_for_path("/my-memory")
        assert config is not None
        assert config.path == "/my-memory"

    def test_find_memory_for_path_nested(self, registry: WorkspaceRegistry) -> None:
        """Test finding memory for nested path."""
        with patch.object(registry, "_save_memory_to_db"):
            registry.register_memory("/my-memory")

        config = registry.find_memory_for_path("/my-memory/docs/file.md")
        assert config is not None
        assert config.path == "/my-memory"

    def test_find_memory_for_path_not_found(self, registry: WorkspaceRegistry) -> None:
        """Test finding memory for unregistered path."""
        config = registry.find_memory_for_path("/random/path")
        assert config is None

    def test_list_memories(self, registry: WorkspaceRegistry) -> None:
        """Test listing memories."""
        with patch.object(registry, "_save_memory_to_db"):
            registry.register_memory("/mem1", name="M1")
            registry.register_memory("/mem2", name="M2")

        memories = registry.list_memories()
        assert len(memories) == 2
        paths = [m.path for m in memories]
        assert "/mem1" in paths
        assert "/mem2" in paths

    def test_register_workspace_with_context_dict(self, registry: WorkspaceRegistry) -> None:
        """Test workspace registration with context as dict."""
        with patch.object(registry, "_save_workspace_to_db"):
            context = {"user_id": "alice", "tenant_id": "default"}
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

    def test_register_memory_with_context_dict(self, registry: WorkspaceRegistry) -> None:
        """Test memory registration with context as dict."""
        with patch.object(registry, "_save_memory_to_db"):
            context = {"user_id": "bob", "tenant_id": "team1"}
            config = registry.register_memory(
                path="/memory",
                context=context,
            )
            assert config.created_by == "bob"
