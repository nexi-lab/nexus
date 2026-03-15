"""Tests for WorkspaceRegistry.

These tests verify workspace registration functionality.
"""

from datetime import datetime, timedelta
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


_has_dry_helpers = hasattr(WorkspaceRegistry, "_extract_context")


@pytest.mark.skipif(not _has_dry_helpers, reason="requires Issue #2987 refactor")
class TestUpdateWorkspace:
    """Tests for update_workspace() method (DB-first)."""

    @pytest.fixture
    def registry(self) -> WorkspaceRegistry:
        mock_rs = MagicMock()
        mock_session = MagicMock()
        mock_session.__enter__ = lambda self: mock_session
        mock_session.__exit__ = lambda self, *args: None
        mock_rs.session_factory = MagicMock(return_value=mock_session)
        with patch.object(WorkspaceRegistry, "_load_from_db"):
            reg = WorkspaceRegistry(MagicMock(), record_store=mock_rs)
            reg._workspaces = {}
            return reg

    def test_update_name(self, registry: WorkspaceRegistry) -> None:
        with patch.object(registry, "_save_workspace_to_db"):
            registry.register_workspace("/ws", name="Old")
        config = registry.update_workspace("/ws", name="New")
        assert config.name == "New"

    def test_update_preserves_unchanged_fields(self, registry: WorkspaceRegistry) -> None:
        with patch.object(registry, "_save_workspace_to_db"):
            registry.register_workspace("/ws", name="Keep", description="Also keep")
        config = registry.update_workspace("/ws", name="Changed")
        assert config.name == "Changed"
        assert config.description == "Also keep"

    def test_update_nonexistent_raises(self, registry: WorkspaceRegistry) -> None:
        with pytest.raises(ValueError, match="Workspace not found"):
            registry.update_workspace("/nonexistent", name="X")


@pytest.mark.skipif(not _has_dry_helpers, reason="requires Issue #2987 refactor")
class TestDRYHelpers:
    """Tests for extracted DRY helper methods (Issue #2987)."""

    @pytest.fixture
    def registry(self) -> WorkspaceRegistry:
        mock_rs = MagicMock()
        mock_session = MagicMock()
        mock_session.__enter__ = lambda self: mock_session
        mock_session.__exit__ = lambda self, *args: None
        mock_rs.session_factory = MagicMock(return_value=mock_session)
        with patch.object(WorkspaceRegistry, "_load_from_db"):
            reg = WorkspaceRegistry(MagicMock(), record_store=mock_rs)
            reg._workspaces = {}
            return reg

    def test_extract_context_none(self, registry: WorkspaceRegistry) -> None:
        assert registry._extract_context(None) == (None, None, None)

    def test_extract_context_dict(self, registry: WorkspaceRegistry) -> None:
        ctx = {"user_id": "alice", "agent_id": "bot-1", "zone_id": "z1"}
        assert registry._extract_context(ctx) == ("alice", "bot-1", "z1")

    def test_extract_context_object(self, registry: WorkspaceRegistry) -> None:
        ctx = MagicMock()
        ctx.user_id = "bob"
        ctx.agent_id = "agent-2"
        ctx.zone_id = "z2"
        assert registry._extract_context(ctx) == ("bob", "agent-2", "z2")

    def test_validate_agent_ownership_no_agent(self, registry: WorkspaceRegistry) -> None:
        registry._validate_agent_ownership(None, "alice")  # should not raise

    def test_validate_agent_ownership_invalid(self, registry: WorkspaceRegistry) -> None:
        mock_agent_reg = MagicMock()
        mock_agent_reg.validate_ownership.return_value = False
        registry._agent_registry = mock_agent_reg
        with pytest.raises(PermissionError, match="not owned"):
            registry._validate_agent_ownership("agent-1", "alice")

    def test_compute_expiry_none(self, registry: WorkspaceRegistry) -> None:
        assert registry._compute_expiry(None) is None

    def test_compute_expiry_timedelta(self, registry: WorkspaceRegistry) -> None:
        result = registry._compute_expiry(timedelta(hours=1))
        assert result is not None
        assert isinstance(result, datetime)

    def test_auto_grant_no_rebac(self, registry: WorkspaceRegistry) -> None:
        registry.rebac_manager = None
        registry._auto_grant_ownership("/ws", "alice", "z1")  # should not raise

    def test_auto_grant_success(self, registry: WorkspaceRegistry) -> None:
        registry.rebac_manager = MagicMock()
        registry._auto_grant_ownership("/ws", "alice", "z1")
        registry.rebac_manager.rebac_write.assert_called_once()

    def test_auto_grant_error_is_nonfatal(self, registry: WorkspaceRegistry) -> None:
        registry.rebac_manager = MagicMock()
        registry.rebac_manager.rebac_write.side_effect = RuntimeError("DB error")
        registry._auto_grant_ownership("/ws", "alice", "z1")  # should not raise
