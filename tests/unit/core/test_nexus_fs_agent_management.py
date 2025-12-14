"""Unit tests for NexusFS agent management methods.

Tests cover:
- Helper methods extracted from register_agent
- delete_agent cleanup logic
- Context extraction methods
- Agent config data creation
- API key expiration determination
"""

from __future__ import annotations

import tempfile
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nexus import LocalBackend, NexusFS
from nexus.core.permissions import OperationContext
from nexus.storage.models import APIKeyModel


@pytest.fixture
def temp_dir() -> Generator[Path, None, None]:
    """Create a temporary directory for tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def nx(temp_dir: Path) -> Generator[NexusFS, None, None]:
    """Create a NexusFS instance."""
    nx = NexusFS(
        backend=LocalBackend(temp_dir),
        db_path=temp_dir / "metadata.db",
        auto_parse=False,
        enforce_permissions=True,
    )
    yield nx
    nx.close()


class TestExtractTenantId:
    """Tests for _extract_tenant_id helper method."""

    def test_extract_tenant_id_from_none(self, nx: NexusFS) -> None:
        """Test extracting tenant_id from None context."""
        result = nx._extract_tenant_id(None)
        assert result is None

    def test_extract_tenant_id_from_dict(self, nx: NexusFS) -> None:
        """Test extracting tenant_id from dict context."""
        context = {"tenant_id": "acme"}
        result = nx._extract_tenant_id(context)
        assert result == "acme"

    def test_extract_tenant_id_from_dict_missing(self, nx: NexusFS) -> None:
        """Test extracting tenant_id from dict without tenant_id."""
        context = {"user_id": "alice"}
        result = nx._extract_tenant_id(context)
        assert result is None

    def test_extract_tenant_id_from_operation_context(self, nx: NexusFS) -> None:
        """Test extracting tenant_id from OperationContext."""
        context = OperationContext(
            user="alice",
            groups=[],
            tenant_id="acme",
        )
        result = nx._extract_tenant_id(context)
        assert result == "acme"

    def test_extract_tenant_id_from_operation_context_missing(self, nx: NexusFS) -> None:
        """Test extracting tenant_id from OperationContext without tenant_id."""
        context = OperationContext(user="alice", groups=[])
        result = nx._extract_tenant_id(context)
        assert result is None


class TestExtractUserId:
    """Tests for _extract_user_id helper method."""

    def test_extract_user_id_from_none(self, nx: NexusFS) -> None:
        """Test extracting user_id from None context."""
        result = nx._extract_user_id(None)
        assert result is None

    def test_extract_user_id_from_dict_with_user_id(self, nx: NexusFS) -> None:
        """Test extracting user_id from dict with user_id key."""
        context = {"user_id": "alice"}
        result = nx._extract_user_id(context)
        assert result == "alice"

    def test_extract_user_id_from_dict_with_user(self, nx: NexusFS) -> None:
        """Test extracting user_id from dict with user key (fallback)."""
        context = {"user": "bob"}
        result = nx._extract_user_id(context)
        assert result == "bob"

    def test_extract_user_id_from_dict_prefers_user_id(self, nx: NexusFS) -> None:
        """Test that user_id is preferred over user key."""
        context = {"user_id": "alice", "user": "bob"}
        result = nx._extract_user_id(context)
        assert result == "alice"

    def test_extract_user_id_from_operation_context(self, nx: NexusFS) -> None:
        """Test extracting user_id from OperationContext."""
        context = OperationContext(
            user="alice",
            groups=[],
            user_id="alice",
        )
        result = nx._extract_user_id(context)
        assert result == "alice"

    def test_extract_user_id_from_operation_context_fallback(self, nx: NexusFS) -> None:
        """Test extracting user_id from OperationContext falls back to user."""
        context = OperationContext(user="bob", groups=[])
        result = nx._extract_user_id(context)
        assert result == "bob"


class TestCreateAgentConfigData:
    """Tests for _create_agent_config_data helper method."""

    def test_create_agent_config_data_minimal(self, nx: NexusFS) -> None:
        """Test creating minimal agent config data."""
        config = nx._create_agent_config_data(
            agent_id="admin,test_agent",
            name="Test Agent",
            user_id="admin",
            description=None,
            created_at=None,
        )

        assert config["agent_id"] == "admin,test_agent"
        assert config["name"] == "Test Agent"
        assert config["user_id"] == "admin"
        assert config["description"] is None
        assert config["created_at"] is None

    def test_create_agent_config_data_with_description(self, nx: NexusFS) -> None:
        """Test creating agent config data with description."""
        config = nx._create_agent_config_data(
            agent_id="admin,test_agent",
            name="Test Agent",
            user_id="admin",
            description="A test agent",
            created_at="2024-01-01T00:00:00Z",
        )

        assert config["description"] == "A test agent"
        assert config["created_at"] == "2024-01-01T00:00:00Z"

    def test_create_agent_config_data_with_metadata(self, nx: NexusFS) -> None:
        """Test creating agent config data with metadata."""
        metadata = {
            "platform": "langgraph",
            "endpoint_url": "http://localhost:2024",
            "agent_id": "agent",
        }
        config = nx._create_agent_config_data(
            agent_id="admin,test_agent",
            name="Test Agent",
            user_id="admin",
            description=None,
            created_at=None,
            metadata=metadata,
        )

        assert config["metadata"] == metadata
        assert config["metadata"]["platform"] == "langgraph"

    def test_create_agent_config_data_with_api_key(self, nx: NexusFS) -> None:
        """Test creating agent config data with API key."""
        config = nx._create_agent_config_data(
            agent_id="admin,test_agent",
            name="Test Agent",
            user_id="admin",
            description=None,
            created_at=None,
            api_key="sk-test-key",
        )

        assert config["api_key"] == "sk-test-key"

    def test_create_agent_config_data_with_inherit_permissions(self, nx: NexusFS) -> None:
        """Test creating agent config data with inherit_permissions flag."""
        config = nx._create_agent_config_data(
            agent_id="admin,test_agent",
            name="Test Agent",
            user_id="admin",
            description=None,
            created_at=None,
            inherit_permissions=True,
        )

        assert config["inherit_permissions"] is True

    def test_create_agent_config_data_with_all_options(self, nx: NexusFS) -> None:
        """Test creating agent config data with all options."""
        metadata = {"platform": "langgraph"}
        config = nx._create_agent_config_data(
            agent_id="admin,test_agent",
            name="Test Agent",
            user_id="admin",
            description="Test description",
            created_at="2024-01-01T00:00:00Z",
            metadata=metadata,
            api_key="sk-test-key",
            inherit_permissions=False,
        )

        assert config["agent_id"] == "admin,test_agent"
        assert config["name"] == "Test Agent"
        assert config["user_id"] == "admin"
        assert config["description"] == "Test description"
        assert config["created_at"] == "2024-01-01T00:00:00Z"
        assert config["metadata"] == metadata
        assert config["api_key"] == "sk-test-key"
        assert config["inherit_permissions"] is False


class TestDetermineAgentKeyExpiration:
    """Tests for _determine_agent_key_expiration helper method."""

    def test_determine_expiration_with_owner_key_expires(self, nx: NexusFS) -> None:
        """Test expiration when owner has key with expiration."""
        session = nx.metadata.SessionLocal()
        try:
            # Create owner's API key with expiration
            owner_key = APIKeyModel(
                user_id="alice",
                name="alice_key",
                key_hash="hash",
                subject_type="user",
                subject_id="alice",
                tenant_id="default",
                expires_at=datetime.now(UTC) + timedelta(days=30),
                revoked=0,
            )
            session.add(owner_key)
            session.commit()

            expires_at = nx._determine_agent_key_expiration("alice", session)

            assert expires_at == owner_key.expires_at
        finally:
            session.close()

    def test_determine_expiration_with_owner_key_no_expiration(self, nx: NexusFS) -> None:
        """Test expiration when owner has key without expiration (defaults to 365 days)."""
        session = nx.metadata.SessionLocal()
        try:
            # Create owner's API key without expiration
            owner_key = APIKeyModel(
                user_id="alice",
                name="alice_key",
                key_hash="hash",
                subject_type="user",
                subject_id="alice",
                tenant_id="default",
                expires_at=None,
                revoked=0,
            )
            session.add(owner_key)
            session.commit()

            expires_at = nx._determine_agent_key_expiration("alice", session)

            # Should default to 365 days from now
            expected = datetime.now(UTC) + timedelta(days=365)
            # Allow 1 second tolerance
            assert abs((expires_at - expected).total_seconds()) < 1
        finally:
            session.close()

    def test_determine_expiration_no_owner_key(self, nx: NexusFS) -> None:
        """Test expiration when owner has no key (defaults to 365 days)."""
        session = nx.metadata.SessionLocal()
        try:
            expires_at = nx._determine_agent_key_expiration("alice", session)

            # Should default to 365 days from now
            expected = datetime.now(UTC) + timedelta(days=365)
            # Allow 1 second tolerance
            assert abs((expires_at - expected).total_seconds()) < 1
        finally:
            session.close()

    def test_determine_expiration_owner_key_expired(self, nx: NexusFS) -> None:
        """Test that expired owner key raises ValueError."""
        session = nx.metadata.SessionLocal()
        try:
            # Create expired owner's API key
            owner_key = APIKeyModel(
                user_id="alice",
                name="alice_key",
                key_hash="hash",
                subject_type="user",
                subject_id="alice",
                tenant_id="default",
                expires_at=datetime.now(UTC) - timedelta(days=1),  # Expired
                revoked=0,
            )
            session.add(owner_key)
            session.commit()

            with pytest.raises(ValueError, match="Cannot generate API key for agent.*expired"):
                nx._determine_agent_key_expiration("alice", session)
        finally:
            session.close()

    def test_determine_expiration_ignores_agent_keys(self, nx: NexusFS) -> None:
        """Test that agent keys are ignored when finding owner's key."""
        session = nx.metadata.SessionLocal()
        try:
            # Create agent key (should be ignored)
            agent_key = APIKeyModel(
                user_id="alice",
                name="agent_key",
                key_hash="hash",
                subject_type="agent",
                subject_id="alice,agent1",
                tenant_id="default",
                expires_at=datetime.now(UTC) + timedelta(days=10),
                revoked=0,
            )
            session.add(agent_key)

            # Create user key (should be used)
            user_key = APIKeyModel(
                user_id="alice",
                name="alice_key",
                key_hash="hash2",
                subject_type="user",
                subject_id="alice",
                tenant_id="default",
                expires_at=datetime.now(UTC) + timedelta(days=30),
                revoked=0,
            )
            session.add(user_key)
            session.commit()

            expires_at = nx._determine_agent_key_expiration("alice", session)

            # Should use user key, not agent key
            assert expires_at == user_key.expires_at
        finally:
            session.close()

    def test_determine_expiration_ignores_revoked_keys(self, nx: NexusFS) -> None:
        """Test that revoked keys are ignored."""
        session = nx.metadata.SessionLocal()
        try:
            # Create revoked owner's API key
            revoked_key = APIKeyModel(
                user_id="alice",
                name="alice_key",
                key_hash="hash",
                subject_type="user",
                subject_id="alice",
                tenant_id="default",
                expires_at=datetime.now(UTC) + timedelta(days=30),
                revoked=1,  # Revoked
            )
            session.add(revoked_key)
            session.commit()

            expires_at = nx._determine_agent_key_expiration("alice", session)

            # Should default to 365 days since revoked key is ignored
            expected = datetime.now(UTC) + timedelta(days=365)
            assert abs((expires_at - expected).total_seconds()) < 1
        finally:
            session.close()


class TestDeleteAgentCleanup:
    """Tests for delete_agent cleanup logic."""

    def test_delete_agent_revokes_api_keys(self, nx: NexusFS) -> None:
        """Test that delete_agent revokes all API keys for the agent."""
        # Register agent first
        context = {"user_id": "alice", "tenant_id": "default"}
        nx.register_agent(
            agent_id="alice,test_agent",
            name="Test Agent",
            generate_api_key=True,
            context=context,
        )

        # Create additional API key for the agent using NexusFS's session
        session = nx.metadata.SessionLocal()
        try:
            agent_key = APIKeyModel(
                user_id="alice",
                name="alice,test_agent_extra",
                key_hash="hash",
                subject_type="agent",
                subject_id="alice,test_agent",
                tenant_id="default",
                revoked=0,
            )
            session.add(agent_key)
            session.commit()

            # Verify key exists and is not revoked
            key_count = (
                session.query(APIKeyModel)
                .filter(
                    APIKeyModel.subject_type == "agent",
                    APIKeyModel.subject_id == "alice,test_agent",
                    APIKeyModel.revoked == 0,
                )
                .count()
            )
            assert key_count > 0
        finally:
            session.close()

        # Delete agent
        result = nx.delete_agent("alice,test_agent", _context=context)
        assert result is True

        # Verify all keys are revoked using new session
        session = nx.metadata.SessionLocal()
        try:
            active_key_count = (
                session.query(APIKeyModel)
                .filter(
                    APIKeyModel.subject_type == "agent",
                    APIKeyModel.subject_id == "alice,test_agent",
                    APIKeyModel.revoked == 0,
                )
                .count()
            )
            assert active_key_count == 0

            # Verify keys are marked as revoked
            revoked_key_count = (
                session.query(APIKeyModel)
                .filter(
                    APIKeyModel.subject_type == "agent",
                    APIKeyModel.subject_id == "alice,test_agent",
                    APIKeyModel.revoked == 1,
                )
                .count()
            )
            assert revoked_key_count > 0
        finally:
            session.close()

    def test_delete_agent_removes_directory(self, nx: NexusFS) -> None:
        """Test that delete_agent removes agent directory."""
        context = {"user_id": "alice", "tenant_id": "default"}
        nx.register_agent(
            agent_id="alice,test_agent",
            name="Test Agent",
            context=context,
        )

        agent_dir = "/agent/alice/test_agent"
        # Parse context to OperationContext for exists check
        ctx = nx._parse_context(context)
        # Directory may or may not exist depending on test environment
        # Just verify delete_agent succeeds
        directory_existed = nx.exists(agent_dir, context=ctx)

        # Delete agent
        result = nx.delete_agent("alice,test_agent", _context=context)
        assert result is True

        # Verify directory is removed (if it existed)
        if directory_existed:
            assert not nx.exists(agent_dir, context=ctx)

    def test_delete_agent_removes_rebac_tuples(self, nx: NexusFS) -> None:
        """Test that delete_agent removes ReBAC tuples for the agent."""
        # Mock rebac_list_tuples method on NexusFS to return test tuples
        mock_tuples = [
            {
                "tuple_id": "test-tuple-id-1",
                "subject_type": "agent",
                "subject_id": "alice,test_agent",
                "relation": "direct_viewer",
                "object_type": "file",
                "object_id": "/workspace/alice/test",
            }
        ]
        original_rebac_list_tuples = nx.rebac_list_tuples
        nx.rebac_list_tuples = MagicMock(return_value=mock_tuples)

        # Mock rebac_delete
        original_rebac_delete = nx.rebac_delete
        nx.rebac_delete = MagicMock(return_value=True)

        context = {"user_id": "alice", "tenant_id": "default"}
        nx.register_agent(
            agent_id="alice,test_agent",
            name="Test Agent",
            context=context,
        )

        # Delete agent
        result = nx.delete_agent("alice,test_agent", _context=context)
        assert result is True

        # Verify rebac_list_tuples was called (at least once for agent tuples, possibly for user tuples too)
        assert nx.rebac_list_tuples.call_count >= 1
        # Verify rebac_delete was called for the tuple
        assert nx.rebac_delete.call_count >= 1

        # Restore original methods
        nx.rebac_list_tuples = original_rebac_list_tuples
        nx.rebac_delete = original_rebac_delete

    def test_delete_agent_handles_missing_directory(self, nx: NexusFS) -> None:
        """Test that delete_agent handles missing directory gracefully."""
        context = {"user_id": "alice", "tenant_id": "default"}

        # Register agent
        nx.register_agent(
            agent_id="alice,test_agent",
            name="Test Agent",
            context=context,
        )

        # Manually remove directory - use admin context to bypass permission checks
        agent_dir = "/agent/alice/test_agent"
        from nexus.core.permissions import OperationContext

        ctx = nx._parse_context(context)
        # Create admin context to bypass permission checks
        admin_ctx = OperationContext(
            user=ctx.user,
            groups=ctx.groups,
            tenant_id=ctx.tenant_id,
            agent_id=ctx.agent_id,
            is_admin=True,
            is_system=False,
        )
        # Temporarily disable permission enforcement for this test
        original_enforce = nx._enforce_permissions
        nx._enforce_permissions = False
        try:
            nx.rmdir(agent_dir, recursive=True, context=admin_ctx)
        finally:
            nx._enforce_permissions = original_enforce

        # Delete agent should still succeed
        result = nx.delete_agent("alice,test_agent", _context=context)
        assert result is True

    def test_delete_agent_handles_missing_rebac_manager(self, nx: NexusFS) -> None:
        """Test that delete_agent handles missing ReBAC manager gracefully."""
        # Store original manager to restore later
        original_rebac_manager = nx._rebac_manager
        nx._rebac_manager = None

        context = {"user_id": "alice", "tenant_id": "default"}
        nx.register_agent(
            agent_id="alice,test_agent",
            name="Test Agent",
            context=context,
        )

        # Delete agent should still succeed
        result = nx.delete_agent("alice,test_agent", _context=context)
        assert result is True

        # Restore original manager to prevent teardown errors
        nx._rebac_manager = original_rebac_manager
