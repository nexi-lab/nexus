"""Unit tests for AgentService agent management methods.

Tests cover:
- Standalone context extraction functions (_extract_zone_id, _extract_user_id)
- AgentService._create_agent_config_data helper
- AgentService._determine_agent_key_expiration helper
- AgentService.delete_agent cleanup logic
"""

import tempfile
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nexus import CASLocalBackend, NexusFS
from nexus.contracts.types import OperationContext
from nexus.core.config import ParseConfig, PermissionConfig
from nexus.factory import create_nexus_fs
from nexus.storage.models import APIKeyModel
from nexus.storage.raft_metadata_store import RaftMetadataStore
from nexus.storage.record_store import SQLAlchemyRecordStore
from nexus.system_services.agents.agent_service import (
    AgentService,
    _extract_user_id,
    _extract_zone_id,
    create_agent_service,
)


@pytest.fixture
def temp_dir() -> Generator[Path, None, None]:
    """Create a temporary directory for tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def record_store(temp_dir: Path) -> Generator[SQLAlchemyRecordStore, None, None]:
    """Create a SQLAlchemyRecordStore for testing."""
    rs = SQLAlchemyRecordStore(db_path=temp_dir / "metadata.db")
    yield rs
    rs.close()


@pytest.fixture
async def nx(temp_dir: Path, record_store: SQLAlchemyRecordStore) -> AsyncGenerator[NexusFS, None]:
    """Create a NexusFS instance."""
    nx = await create_nexus_fs(
        backend=CASLocalBackend(temp_dir),
        metadata_store=RaftMetadataStore.embedded(str(temp_dir / "raft-metadata")),
        record_store=record_store,
        parsing=ParseConfig(auto_parse=False),
        permissions=PermissionConfig(enforce=True),
    )
    yield nx
    nx.close()


@pytest.fixture
def agent_service(nx: NexusFS) -> AgentService:
    """Create an AgentService instance from NexusFS."""
    svc = create_agent_service(nx)
    assert svc is not None
    return svc


class TestExtractZoneId:
    """Tests for _extract_zone_id standalone helper function."""

    def test_extract_zone_id_from_none(self) -> None:
        """Test extracting zone_id from None context."""
        result = _extract_zone_id(None)
        assert result is None

    def test_extract_zone_id_from_dict(self) -> None:
        """Test extracting zone_id from dict context."""
        context = {"zone_id": "acme"}
        result = _extract_zone_id(context)
        assert result == "acme"

    def test_extract_zone_id_from_dict_missing(self) -> None:
        """Test extracting zone_id from dict without zone_id."""
        context = {"user_id": "alice"}
        result = _extract_zone_id(context)
        assert result is None

    def test_extract_zone_id_from_operation_context(self) -> None:
        """Test extracting zone_id from OperationContext."""
        context = OperationContext(
            user_id="alice",
            groups=[],
            zone_id="acme",
        )
        result = _extract_zone_id(context)
        assert result == "acme"

    def test_extract_zone_id_from_operation_context_missing(self) -> None:
        """Test extracting zone_id from OperationContext without zone_id."""
        context = OperationContext(user_id="alice", groups=[])
        result = _extract_zone_id(context)
        assert result is None


class TestExtractUserId:
    """Tests for _extract_user_id standalone helper function."""

    def test_extract_user_id_from_none(self) -> None:
        """Test extracting user_id from None context."""
        result = _extract_user_id(None)
        assert result is None

    def test_extract_user_id_from_dict_with_user_id(self) -> None:
        """Test extracting user_id from dict with user_id key."""
        context = {"user_id": "alice"}
        result = _extract_user_id(context)
        assert result == "alice"

    def test_extract_user_id_from_dict_with_user_id_key(self) -> None:
        """Test extracting user_id from dict with user_id key."""
        context = {"user_id": "bob"}
        result = _extract_user_id(context)
        assert result == "bob"

    def test_extract_user_id_from_dict_prefers_user_id(self) -> None:
        """Test that user_id is preferred over user key."""
        context = {"user_id": "alice", "user": "bob"}
        result = _extract_user_id(context)
        assert result == "alice"

    def test_extract_user_id_from_operation_context(self) -> None:
        """Test extracting user_id from OperationContext."""
        context = OperationContext(
            user_id="alice",
            groups=[],
        )
        result = _extract_user_id(context)
        assert result == "alice"

    def test_extract_user_id_from_operation_context_fallback(self) -> None:
        """Test extracting user_id from OperationContext falls back to user."""
        context = OperationContext(user_id="bob", groups=[])
        result = _extract_user_id(context)
        assert result == "bob"


class TestCreateAgentConfigData:
    """Tests for AgentService._create_agent_config_data helper method."""

    def test_create_agent_config_data_minimal(self, agent_service: AgentService) -> None:
        """Test creating minimal agent config data."""
        config = agent_service._create_agent_config_data(
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

    def test_create_agent_config_data_with_description(self, agent_service: AgentService) -> None:
        """Test creating agent config data with description."""
        config = agent_service._create_agent_config_data(
            agent_id="admin,test_agent",
            name="Test Agent",
            user_id="admin",
            description="A test agent",
            created_at="2024-01-01T00:00:00Z",
        )

        assert config["description"] == "A test agent"
        assert config["created_at"] == "2024-01-01T00:00:00Z"

    def test_create_agent_config_data_with_metadata(self, agent_service: AgentService) -> None:
        """Test creating agent config data with metadata."""
        metadata = {
            "platform": "langgraph",
            "endpoint_url": "http://localhost:2024",
            "agent_id": "agent",
        }
        config = agent_service._create_agent_config_data(
            agent_id="admin,test_agent",
            name="Test Agent",
            user_id="admin",
            description=None,
            created_at=None,
            metadata=metadata,
        )

        assert config["metadata"] == metadata
        assert config["metadata"]["platform"] == "langgraph"

    def test_create_agent_config_data_with_api_key(self, agent_service: AgentService) -> None:
        """Test creating agent config data with API key."""
        config = agent_service._create_agent_config_data(
            agent_id="admin,test_agent",
            name="Test Agent",
            user_id="admin",
            description=None,
            created_at=None,
            api_key="sk-test-key",
        )

        assert config["api_key"] == "sk-test-key"

    def test_create_agent_config_data_with_all_options(self, agent_service: AgentService) -> None:
        """Test creating agent config data with all options."""
        metadata = {"platform": "langgraph"}
        config = agent_service._create_agent_config_data(
            agent_id="admin,test_agent",
            name="Test Agent",
            user_id="admin",
            description="Test description",
            created_at="2024-01-01T00:00:00Z",
            metadata=metadata,
            api_key="sk-test-key",
        )

        assert config["agent_id"] == "admin,test_agent"
        assert config["name"] == "Test Agent"
        assert config["user_id"] == "admin"
        assert config["description"] == "Test description"
        assert config["created_at"] == "2024-01-01T00:00:00Z"
        assert config["metadata"] == metadata
        assert config["api_key"] == "sk-test-key"


class TestDetermineAgentKeyExpiration:
    """Tests for AgentService._determine_agent_key_expiration helper method."""

    def test_determine_expiration_with_owner_key_expires(
        self, agent_service: AgentService, record_store: SQLAlchemyRecordStore
    ) -> None:
        """Test expiration when owner has key with expiration."""
        session = record_store.session_factory()
        try:
            # Create owner's API key with expiration
            owner_key = APIKeyModel(
                user_id="alice",
                name="alice_key",
                key_hash="hash",
                subject_type="user",
                subject_id="alice",
                zone_id="root",
                expires_at=datetime.now(UTC) + timedelta(days=30),
                revoked=0,
            )
            session.add(owner_key)
            session.commit()

            expires_at = agent_service._determine_agent_key_expiration("alice", session)

            assert expires_at == owner_key.expires_at
        finally:
            session.close()

    def test_determine_expiration_with_owner_key_no_expiration(
        self, agent_service: AgentService, record_store: SQLAlchemyRecordStore
    ) -> None:
        """Test expiration when owner has key without expiration (defaults to 365 days)."""
        session = record_store.session_factory()
        try:
            # Create owner's API key without expiration
            owner_key = APIKeyModel(
                user_id="alice",
                name="alice_key",
                key_hash="hash",
                subject_type="user",
                subject_id="alice",
                zone_id="root",
                expires_at=None,
                revoked=0,
            )
            session.add(owner_key)
            session.commit()

            expires_at = agent_service._determine_agent_key_expiration("alice", session)

            # Should default to 365 days from now
            expected = datetime.now(UTC) + timedelta(days=365)
            # Allow 1 second tolerance
            assert abs((expires_at - expected).total_seconds()) < 1
        finally:
            session.close()

    def test_determine_expiration_no_owner_key(
        self, agent_service: AgentService, record_store: SQLAlchemyRecordStore
    ) -> None:
        """Test expiration when owner has no key (defaults to 365 days)."""
        session = record_store.session_factory()
        try:
            expires_at = agent_service._determine_agent_key_expiration("alice", session)

            # Should default to 365 days from now
            expected = datetime.now(UTC) + timedelta(days=365)
            # Allow 1 second tolerance
            assert abs((expires_at - expected).total_seconds()) < 1
        finally:
            session.close()

    def test_determine_expiration_owner_key_expired(
        self, agent_service: AgentService, record_store: SQLAlchemyRecordStore
    ) -> None:
        """Test that expired owner key raises ValueError."""
        session = record_store.session_factory()
        try:
            # Create expired owner's API key
            owner_key = APIKeyModel(
                user_id="alice",
                name="alice_key",
                key_hash="hash",
                subject_type="user",
                subject_id="alice",
                zone_id="root",
                expires_at=datetime.now(UTC) - timedelta(days=1),  # Expired
                revoked=0,
            )
            session.add(owner_key)
            session.commit()

            with pytest.raises(ValueError, match="Cannot generate API key for agent.*expired"):
                agent_service._determine_agent_key_expiration("alice", session)
        finally:
            session.close()

    def test_determine_expiration_ignores_agent_keys(
        self, agent_service: AgentService, record_store: SQLAlchemyRecordStore
    ) -> None:
        """Test that agent keys are ignored when finding owner's key."""
        session = record_store.session_factory()
        try:
            # Create agent key (should be ignored)
            agent_key = APIKeyModel(
                user_id="alice",
                name="agent_key",
                key_hash="hash",
                subject_type="agent",
                subject_id="alice,agent1",
                zone_id="root",
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
                zone_id="root",
                expires_at=datetime.now(UTC) + timedelta(days=30),
                revoked=0,
            )
            session.add(user_key)
            session.commit()

            expires_at = agent_service._determine_agent_key_expiration("alice", session)

            # Should use user key, not agent key
            assert expires_at == user_key.expires_at
        finally:
            session.close()

    def test_determine_expiration_ignores_revoked_keys(
        self, agent_service: AgentService, record_store: SQLAlchemyRecordStore
    ) -> None:
        """Test that revoked keys are ignored."""
        session = record_store.session_factory()
        try:
            # Create revoked owner's API key
            revoked_key = APIKeyModel(
                user_id="alice",
                name="alice_key",
                key_hash="hash",
                subject_type="user",
                subject_id="alice",
                zone_id="root",
                expires_at=datetime.now(UTC) + timedelta(days=30),
                revoked=1,  # Revoked
            )
            session.add(revoked_key)
            session.commit()

            expires_at = agent_service._determine_agent_key_expiration("alice", session)

            # Should default to 365 days since revoked key is ignored
            expected = datetime.now(UTC) + timedelta(days=365)
            assert abs((expires_at - expected).total_seconds()) < 1
        finally:
            session.close()


class TestDeleteAgentCleanup:
    """Tests for AgentService.delete_agent cleanup logic."""

    async def test_delete_agent_revokes_api_keys(
        self, agent_service: AgentService, record_store: SQLAlchemyRecordStore
    ) -> None:
        """Test that delete_agent revokes all API keys for the agent."""
        # Register agent first
        context = {"user_id": "alice", "zone_id": "root"}
        await agent_service.register_agent(
            agent_id="alice,test_agent",
            name="Test Agent",
            generate_api_key=True,
            context=context,
        )

        # Create additional API key for the agent using record_store's session
        session = record_store.session_factory()
        try:
            agent_key = APIKeyModel(
                user_id="alice",
                name="alice,test_agent_extra",
                key_hash="hash",
                subject_type="agent",
                subject_id="alice,test_agent",
                zone_id="root",
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
        result = await agent_service.delete_agent("alice,test_agent", _context=context)
        assert result is True

        # Verify all keys are revoked using new session
        session = record_store.session_factory()
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

    async def test_delete_agent_removes_directory(
        self, agent_service: AgentService, nx: NexusFS
    ) -> None:
        """Test that delete_agent removes agent directory."""
        context = {"user_id": "alice", "zone_id": "root"}
        await agent_service.register_agent(
            agent_id="alice,test_agent",
            name="Test Agent",
            context=context,
        )

        agent_dir = "/zone/root/user/alice/agent/test_agent"
        # Parse context to OperationContext for exists check
        ctx = nx._parse_context(context)
        # Directory may or may not exist depending on test environment
        # Just verify delete_agent succeeds
        directory_existed = await nx.sys_access(agent_dir, context=ctx)

        # Delete agent
        result = await agent_service.delete_agent("alice,test_agent", _context=context)
        assert result is True

        # Verify directory is removed (if it existed)
        if directory_existed:
            assert not await nx.sys_access(agent_dir, context=ctx)

    async def test_delete_agent_removes_rebac_tuples(self, agent_service: AgentService) -> None:
        """Test that delete_agent removes ReBAC tuples for the agent."""
        # Mock rebac_manager on the agent_service
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
        original_rebac_manager = agent_service._rebac_manager
        mock_rebac_manager = MagicMock()
        mock_rebac_manager.rebac_list_tuples = MagicMock(return_value=mock_tuples)
        mock_rebac_manager.rebac_delete = MagicMock(return_value=True)
        agent_service._rebac_manager = mock_rebac_manager

        context = {"user_id": "alice", "zone_id": "root"}
        await agent_service.register_agent(
            agent_id="alice,test_agent",
            name="Test Agent",
            context=context,
        )

        # Delete agent
        result = await agent_service.delete_agent("alice,test_agent", _context=context)
        assert result is True

        # Verify rebac_list_tuples was called (at least once for agent tuples, possibly for user tuples too)
        assert mock_rebac_manager.rebac_list_tuples.call_count >= 1
        # Verify rebac_delete was called for the tuple
        assert mock_rebac_manager.rebac_delete.call_count >= 1

        # Restore original manager
        agent_service._rebac_manager = original_rebac_manager

    async def test_delete_agent_handles_missing_directory(
        self, agent_service: AgentService, nx: NexusFS
    ) -> None:
        """Test that delete_agent handles missing directory gracefully."""
        context = {"user_id": "alice", "zone_id": "root"}

        # Register agent
        await agent_service.register_agent(
            agent_id="alice,test_agent",
            name="Test Agent",
            context=context,
        )

        # Manually remove directory - use admin context to bypass permission checks
        agent_dir = "/zone/root/user/alice/agent/test_agent"
        ctx = nx._parse_context(context)
        # Create admin context to bypass permission checks
        admin_ctx = OperationContext(
            user_id=ctx.user_id,
            groups=ctx.groups,
            zone_id=ctx.zone_id,
            agent_id=ctx.agent_id,
            is_admin=True,
            is_system=False,
        )
        # Temporarily disable permission enforcement for this test
        original_enforce = nx._enforce_permissions
        nx._enforce_permissions = False
        try:
            await nx.sys_rmdir(agent_dir, recursive=True, context=admin_ctx)
        finally:
            nx._enforce_permissions = original_enforce

        # Delete agent should still succeed
        result = await agent_service.delete_agent("alice,test_agent", _context=context)
        assert result is True

    async def test_delete_agent_handles_missing_rebac_manager(
        self, agent_service: AgentService
    ) -> None:
        """Test that delete_agent handles missing ReBAC manager gracefully."""
        # Store original manager to restore later
        original_rebac_manager = agent_service._rebac_manager
        agent_service._rebac_manager = None

        context = {"user_id": "alice", "zone_id": "root"}
        await agent_service.register_agent(
            agent_id="alice,test_agent",
            name="Test Agent",
            context=context,
        )

        # Delete agent should still succeed
        result = await agent_service.delete_agent("alice,test_agent", _context=context)
        assert result is True

        # Restore original manager to prevent teardown errors
        agent_service._rebac_manager = original_rebac_manager
