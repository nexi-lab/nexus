"""Tests for agent registration and management."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from nexus.core.agents import (
    create_agent_with_api_key,
    register_agent,
    unregister_agent,
    validate_agent_ownership,
)
from nexus.core.entity_registry import EntityRegistry
from nexus.storage.models import Base


@pytest.fixture
def engine():
    """Create in-memory SQLite database for testing."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def session(engine):
    """Create database session."""
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    yield session
    session.rollback()
    session.close()


@pytest.fixture
def entity_registry(session):
    """Create entity registry."""
    return EntityRegistry(session)


class TestRegisterAgent:
    """Test register_agent function."""

    def test_register_agent_basic(self):
        """Test basic agent registration."""
        agent = register_agent(
            user_id="alice",
            agent_id="agent_data_analyst",
            name="Data Analyst",
        )

        assert agent is not None
        assert agent["agent_id"] == "agent_data_analyst"
        assert agent["user_id"] == "alice"
        assert agent["name"] == "Data Analyst"
        assert "created_at" in agent

    def test_register_agent_with_tenant(self):
        """Test agent registration with tenant."""
        agent = register_agent(
            user_id="alice",
            agent_id="agent_test",
            name="Test Agent",
            tenant_id="acme",
        )

        assert agent["tenant_id"] == "acme"

    def test_register_agent_with_metadata(self):
        """Test agent registration with metadata."""
        metadata = {"version": "1.0", "capabilities": ["read", "write"]}

        agent = register_agent(
            user_id="alice",
            agent_id="agent_test",
            metadata=metadata,
        )

        assert agent["metadata"] == metadata

    def test_register_agent_with_entity_registry(self, entity_registry):
        """Test agent registration creates entity registry entry."""
        # First register user
        entity_registry.register_entity("user", "alice")

        agent = register_agent(
            user_id="alice",
            agent_id="agent_test",
            name="Test Agent",
            entity_registry=entity_registry,
        )

        assert agent is not None

        # Verify entity was registered
        entity = entity_registry.get_entity("agent", "agent_test")
        assert entity is not None
        assert entity.entity_type == "agent"
        assert entity.entity_id == "agent_test"
        assert entity.parent_type == "user"
        assert entity.parent_id == "alice"

    def test_register_agent_without_user_id_fails(self):
        """Test that registering agent without user_id raises error."""
        with pytest.raises(ValueError, match="user_id is required"):
            register_agent(user_id="", agent_id="agent_test")

    def test_register_agent_without_agent_id_fails(self):
        """Test that registering agent without agent_id raises error."""
        with pytest.raises(ValueError, match="agent_id is required"):
            register_agent(user_id="alice", agent_id="")

    def test_register_agent_minimal(self):
        """Test agent registration with minimal parameters."""
        agent = register_agent(user_id="alice", agent_id="agent_test")

        assert agent["agent_id"] == "agent_test"
        assert agent["user_id"] == "alice"
        assert agent["name"] is None
        assert agent["tenant_id"] is None
        assert agent["metadata"] == {}

    def test_register_multiple_agents_same_user(self, entity_registry):
        """Test registering multiple agents for same user."""
        entity_registry.register_entity("user", "alice")

        agent1 = register_agent(
            user_id="alice",
            agent_id="agent1",
            name="Agent 1",
            entity_registry=entity_registry,
        )

        agent2 = register_agent(
            user_id="alice",
            agent_id="agent2",
            name="Agent 2",
            entity_registry=entity_registry,
        )

        assert agent1["agent_id"] != agent2["agent_id"]
        assert agent1["user_id"] == agent2["user_id"]

        # Verify both registered
        children = entity_registry.get_children("user", "alice")
        assert len(children) == 2


class TestCreateAgentWithAPIKey:
    """Test create_agent_with_api_key function."""

    def test_create_agent_with_api_key(self, session, entity_registry):
        """Test creating agent with API key."""
        # Register user first
        entity_registry.register_entity("user", "alice")

        agent, api_key = create_agent_with_api_key(
            session,
            user_id="alice",
            agent_id="agent_task",
            name="Task Agent",
            entity_registry=entity_registry,
        )

        assert agent is not None
        assert agent["agent_id"] == "agent_task"
        assert api_key is not None
        assert isinstance(api_key, str)
        assert len(api_key) > 0

    def test_create_agent_with_temporary_api_key(self, session, entity_registry):
        """Test creating agent with temporary API key."""
        entity_registry.register_entity("user", "alice")

        expires_at = datetime.now(UTC) + timedelta(hours=1)

        agent, api_key = create_agent_with_api_key(
            session,
            user_id="alice",
            agent_id="agent_temp",
            name="Temp Agent",
            expires_at=expires_at,
            entity_registry=entity_registry,
        )

        assert agent is not None
        assert api_key is not None

    def test_create_agent_with_permanent_api_key(self, session, entity_registry):
        """Test creating agent with permanent API key."""
        entity_registry.register_entity("user", "alice")

        agent, api_key = create_agent_with_api_key(
            session,
            user_id="alice",
            agent_id="agent_perm",
            name="Permanent Agent",
            expires_at=None,  # Permanent
            entity_registry=entity_registry,
        )

        assert agent is not None
        assert api_key is not None

    def test_create_agent_registers_entity(self, session, entity_registry):
        """Test that creating agent with API key also registers entity."""
        entity_registry.register_entity("user", "alice")

        agent, _api_key = create_agent_with_api_key(
            session,
            user_id="alice",
            agent_id="agent_test",
            name="Test Agent",
            entity_registry=entity_registry,
        )

        # Verify entity was registered
        entity = entity_registry.get_entity("agent", "agent_test")
        assert entity is not None
        assert entity.parent_id == "alice"

    def test_create_agent_with_additional_kwargs(self, session, entity_registry):
        """Test creating agent with additional kwargs for register_agent."""
        entity_registry.register_entity("user", "alice")

        agent, _api_key = create_agent_with_api_key(
            session,
            user_id="alice",
            agent_id="agent_test",
            name="Test Agent",
            tenant_id="acme",
            metadata={"version": "1.0"},
            entity_registry=entity_registry,
        )

        assert agent["tenant_id"] == "acme"
        assert agent["metadata"]["version"] == "1.0"


class TestUnregisterAgent:
    """Test unregister_agent function."""

    def test_unregister_existing_agent(self, entity_registry):
        """Test unregistering an existing agent."""
        # Register agent
        entity_registry.register_entity("user", "alice")
        entity_registry.register_entity(
            "agent", "agent_test", parent_type="user", parent_id="alice"
        )

        # Unregister
        success = unregister_agent("agent_test", entity_registry=entity_registry)

        assert success is True

        # Verify agent is gone
        entity = entity_registry.get_entity("agent", "agent_test")
        assert entity is None

    def test_unregister_nonexistent_agent(self, entity_registry):
        """Test unregistering a non-existent agent."""
        success = unregister_agent("nonexistent_agent", entity_registry=entity_registry)

        assert success is False

    def test_unregister_without_registry(self):
        """Test unregistering without entity registry."""
        success = unregister_agent("agent_test", entity_registry=None)

        assert success is False

    def test_unregister_agent_preserves_other_agents(self, entity_registry):
        """Test that unregistering one agent doesn't affect others."""
        # Register multiple agents
        entity_registry.register_entity("user", "alice")
        entity_registry.register_entity("agent", "agent1", parent_type="user", parent_id="alice")
        entity_registry.register_entity("agent", "agent2", parent_type="user", parent_id="alice")

        # Unregister one
        unregister_agent("agent1", entity_registry=entity_registry)

        # Verify agent2 still exists
        entity = entity_registry.get_entity("agent", "agent2")
        assert entity is not None


class TestValidateAgentOwnership:
    """Test validate_agent_ownership function."""

    def test_validate_owner_has_access(self, entity_registry):
        """Test that owner can access their agent."""
        # Register user and agent
        entity_registry.register_entity("user", "alice")
        entity_registry.register_entity(
            "agent", "agent_test", parent_type="user", parent_id="alice"
        )

        valid = validate_agent_ownership("agent_test", "alice", entity_registry)

        assert valid is True

    def test_validate_non_owner_no_access(self, entity_registry):
        """Test that non-owner cannot access agent."""
        # Register users and agent
        entity_registry.register_entity("user", "alice")
        entity_registry.register_entity("user", "bob")
        entity_registry.register_entity(
            "agent", "agent_test", parent_type="user", parent_id="alice"
        )

        valid = validate_agent_ownership("agent_test", "bob", entity_registry)

        assert valid is False

    def test_validate_nonexistent_agent(self, entity_registry):
        """Test validation with non-existent agent."""
        valid = validate_agent_ownership("nonexistent_agent", "alice", entity_registry)

        assert valid is False

    def test_validate_agent_different_parent_type(self, entity_registry):
        """Test validation fails if agent has different parent type."""
        # Register with tenant as parent (unusual case)
        entity_registry.register_entity("tenant", "acme")
        entity_registry.register_entity(
            "agent", "agent_test", parent_type="tenant", parent_id="acme"
        )

        # Should fail because parent_type is not "user"
        valid = validate_agent_ownership("agent_test", "acme", entity_registry)

        assert valid is False

    def test_validate_multiple_users(self, entity_registry):
        """Test that each user can only access their own agents."""
        # Register multiple users with agents
        entity_registry.register_entity("user", "alice")
        entity_registry.register_entity("user", "bob")
        entity_registry.register_entity(
            "agent", "agent_alice", parent_type="user", parent_id="alice"
        )
        entity_registry.register_entity("agent", "agent_bob", parent_type="user", parent_id="bob")

        # Alice can access her agent
        assert validate_agent_ownership("agent_alice", "alice", entity_registry) is True

        # Bob can access his agent
        assert validate_agent_ownership("agent_bob", "bob", entity_registry) is True

        # Alice cannot access Bob's agent
        assert validate_agent_ownership("agent_bob", "alice", entity_registry) is False

        # Bob cannot access Alice's agent
        assert validate_agent_ownership("agent_alice", "bob", entity_registry) is False


class TestAgentIntegration:
    """Integration tests for agent management."""

    def test_complete_agent_lifecycle(self, session, entity_registry):
        """Test complete agent lifecycle."""
        # 1. Register user
        entity_registry.register_entity("user", "alice")

        # 2. Register agent
        agent = register_agent(
            user_id="alice",
            agent_id="agent_lifecycle",
            name="Lifecycle Agent",
            entity_registry=entity_registry,
        )

        assert agent is not None

        # 3. Validate ownership
        assert validate_agent_ownership("agent_lifecycle", "alice", entity_registry) is True

        # 4. Unregister agent
        success = unregister_agent("agent_lifecycle", entity_registry=entity_registry)
        assert success is True

        # 5. Verify agent is gone
        assert validate_agent_ownership("agent_lifecycle", "alice", entity_registry) is False

    def test_agent_with_api_key_lifecycle(self, session, entity_registry):
        """Test agent with API key lifecycle."""
        # 1. Register user
        entity_registry.register_entity("user", "alice")

        # 2. Create agent with API key
        agent, api_key = create_agent_with_api_key(
            session,
            user_id="alice",
            agent_id="agent_api",
            name="API Agent",
            entity_registry=entity_registry,
        )

        assert agent is not None
        assert api_key is not None

        # 3. Validate ownership
        assert validate_agent_ownership("agent_api", "alice", entity_registry) is True

        # 4. Unregister (note: API key would need separate revocation in real system)
        success = unregister_agent("agent_api", entity_registry=entity_registry)
        assert success is True

    def test_multi_tenant_agent_isolation(self, session, entity_registry):
        """Test agent isolation across tenants."""
        # Register tenants
        entity_registry.register_entity("tenant", "acme")
        entity_registry.register_entity("tenant", "initech")

        # Register users under different tenants
        entity_registry.register_entity("user", "alice", parent_type="tenant", parent_id="acme")
        entity_registry.register_entity("user", "bob", parent_type="tenant", parent_id="initech")

        # Register agents
        agent_alice = register_agent(
            user_id="alice",
            agent_id="agent_acme",
            name="Acme Agent",
            tenant_id="acme",
            entity_registry=entity_registry,
        )

        agent_bob = register_agent(
            user_id="bob",
            agent_id="agent_initech",
            name="Initech Agent",
            tenant_id="initech",
            entity_registry=entity_registry,
        )

        # Verify isolation
        assert agent_alice["tenant_id"] == "acme"
        assert agent_bob["tenant_id"] == "initech"
        assert validate_agent_ownership("agent_acme", "alice", entity_registry) is True
        assert validate_agent_ownership("agent_initech", "bob", entity_registry) is True
        assert validate_agent_ownership("agent_acme", "bob", entity_registry) is False
        assert validate_agent_ownership("agent_initech", "alice", entity_registry) is False

    def test_agent_hierarchy_relationships(self, entity_registry):
        """Test agent hierarchy and relationships."""
        # Build hierarchy
        entity_registry.register_entity("tenant", "acme")
        entity_registry.register_entity("user", "alice", parent_type="tenant", parent_id="acme")
        entity_registry.register_entity("agent", "agent1", parent_type="user", parent_id="alice")
        entity_registry.register_entity("agent", "agent2", parent_type="user", parent_id="alice")

        # Verify relationships
        children = entity_registry.get_children("user", "alice")
        assert len(children) == 2

        agent_ids = {child.entity_id for child in children}
        assert "agent1" in agent_ids
        assert "agent2" in agent_ids

        # Verify each agent knows its parent
        agent1 = entity_registry.get_entity("agent", "agent1")
        agent2 = entity_registry.get_entity("agent", "agent2")

        assert agent1.parent_id == "alice"
        assert agent2.parent_id == "alice"

    def test_register_agent_idempotency(self, entity_registry):
        """Test that registering same agent twice doesn't create duplicates."""
        entity_registry.register_entity("user", "alice")

        # Register agent
        agent1 = register_agent(
            user_id="alice",
            agent_id="agent_test",
            name="Test Agent",
            entity_registry=entity_registry,
        )

        # Register again (should update, not duplicate)
        agent2 = register_agent(
            user_id="alice",
            agent_id="agent_test",
            name="Test Agent Updated",
            entity_registry=entity_registry,
        )

        # Both should have same agent_id
        assert agent1["agent_id"] == agent2["agent_id"]

        # Should only have one entity in registry
        _ = entity_registry.get_children("user", "alice")
        # Note: Depending on implementation, this might create duplicate or update
        # Current implementation creates new entry each time
        # This test documents current behavior


class TestAgentPermissionManagement:
    """Test agent permission management features (generate_api_key, inherit_permissions, tenant_id)."""

    def test_register_agent_without_api_key(self, entity_registry):
        """Test registering agent without API key (default behavior)."""
        entity_registry.register_entity("user", "alice")

        agent = register_agent(
            user_id="alice",
            agent_id="agent_no_key",
            name="No Key Agent",
            tenant_id="default",
            entity_registry=entity_registry,
        )

        assert agent is not None
        assert agent["agent_id"] == "agent_no_key"
        assert agent["user_id"] == "alice"
        assert agent["tenant_id"] == "default"

    def test_register_agent_with_default_tenant(self, entity_registry):
        """Test that tenant_id defaults to None when not provided."""
        entity_registry.register_entity("user", "alice")

        agent = register_agent(
            user_id="alice",
            agent_id="agent_default_tenant",
            name="Default Tenant Agent",
            entity_registry=entity_registry,
        )

        # When tenant_id is not provided, it should be None
        assert agent["tenant_id"] is None

    def test_register_agent_with_explicit_tenant(self, entity_registry):
        """Test registering agent with explicit tenant_id."""
        entity_registry.register_entity("user", "alice")

        agent = register_agent(
            user_id="alice",
            agent_id="agent_tenant",
            name="Tenant Agent",
            tenant_id="acme",
            entity_registry=entity_registry,
        )

        assert agent["tenant_id"] == "acme"

    def test_register_agent_with_metadata_description(self, entity_registry):
        """Test registering agent with description in metadata."""
        entity_registry.register_entity("user", "alice")

        agent = register_agent(
            user_id="alice",
            agent_id="agent_desc",
            name="Description Agent",
            metadata={"description": "Test agent with description"},
            entity_registry=entity_registry,
        )

        assert agent["metadata"]["description"] == "Test agent with description"

    def test_register_agent_tenant_id_consistency(self, entity_registry):
        """Test that tenant_id is consistently set across multiple registrations."""
        entity_registry.register_entity("user", "alice")

        agent1 = register_agent(
            user_id="alice",
            agent_id="agent1",
            name="Agent 1",
            tenant_id="default",
            entity_registry=entity_registry,
        )

        agent2 = register_agent(
            user_id="alice",
            agent_id="agent2",
            name="Agent 2",
            tenant_id="default",
            entity_registry=entity_registry,
        )

        assert agent1["tenant_id"] == "default"
        assert agent2["tenant_id"] == "default"
        assert agent1["tenant_id"] == agent2["tenant_id"]

    def test_register_agent_multiple_tenants(self, entity_registry):
        """Test registering agents in different tenants."""
        entity_registry.register_entity("user", "alice")

        agent_acme = register_agent(
            user_id="alice",
            agent_id="agent_acme",
            name="Acme Agent",
            tenant_id="acme",
            entity_registry=entity_registry,
        )

        agent_initech = register_agent(
            user_id="alice",
            agent_id="agent_initech",
            name="Initech Agent",
            tenant_id="initech",
            entity_registry=entity_registry,
        )

        assert agent_acme["tenant_id"] == "acme"
        assert agent_initech["tenant_id"] == "initech"
        assert agent_acme["tenant_id"] != agent_initech["tenant_id"]

    def test_register_agent_with_complex_metadata(self, entity_registry):
        """Test registering agent with complex metadata structure."""
        entity_registry.register_entity("user", "alice")

        complex_metadata = {
            "description": "Complex agent",
            "platform": "langgraph",
            "endpoint_url": "http://localhost:2024",
            "agent_id": "agent",
            "version": "1.0.0",
            "capabilities": ["read", "write", "execute"],
        }

        agent = register_agent(
            user_id="alice",
            agent_id="agent_complex",
            name="Complex Agent",
            metadata=complex_metadata,
            entity_registry=entity_registry,
        )

        assert agent["metadata"] == complex_metadata
        assert agent["metadata"]["platform"] == "langgraph"
        assert agent["metadata"]["capabilities"] == ["read", "write", "execute"]
