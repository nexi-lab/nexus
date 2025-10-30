"""Tests for EntityRegistry cascade deletion (v0.5.0)."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from nexus.core.entity_registry import EntityRegistry
from nexus.storage.models import Base


@pytest.fixture
def engine():
    """Create in-memory SQLite database."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def session(engine):
    """Create database session."""
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


@pytest.fixture
def registry(session):
    """Create EntityRegistry instance."""
    return EntityRegistry(session)


def test_delete_entity_basic(registry):
    """Test basic entity deletion without children."""
    # Register a tenant
    registry.register_entity("tenant", "acme")

    # Verify it exists
    entity = registry.get_entity("tenant", "acme")
    assert entity is not None
    assert entity.entity_id == "acme"

    # Delete it
    result = registry.delete_entity("tenant", "acme")
    assert result is True

    # Verify it's gone
    entity = registry.get_entity("tenant", "acme")
    assert entity is None


def test_delete_entity_not_found(registry):
    """Test deleting non-existent entity."""
    result = registry.delete_entity("user", "nonexistent")
    assert result is False


def test_cascade_delete_user_with_agents(registry):
    """Test cascade deletion: deleting user deletes their agents."""
    # Register hierarchy: tenant → user → agents
    registry.register_entity("tenant", "acme")
    registry.register_entity("user", "alice", parent_type="tenant", parent_id="acme")
    registry.register_entity("agent", "agent_1", parent_type="user", parent_id="alice")
    registry.register_entity("agent", "agent_2", parent_type="user", parent_id="alice")

    # Verify all exist
    assert registry.get_entity("tenant", "acme") is not None
    assert registry.get_entity("user", "alice") is not None
    assert registry.get_entity("agent", "agent_1") is not None
    assert registry.get_entity("agent", "agent_2") is not None

    # Delete user (cascade=True by default)
    result = registry.delete_entity("user", "alice", cascade=True)
    assert result is True

    # Verify user and agents are gone
    assert registry.get_entity("user", "alice") is None
    assert registry.get_entity("agent", "agent_1") is None
    assert registry.get_entity("agent", "agent_2") is None

    # Tenant should still exist
    assert registry.get_entity("tenant", "acme") is not None


def test_cascade_delete_tenant_with_hierarchy(registry):
    """Test cascade deletion: deleting tenant deletes users and agents."""
    # Register hierarchy: tenant → user → agent
    registry.register_entity("tenant", "acme")
    registry.register_entity("user", "alice", parent_type="tenant", parent_id="acme")
    registry.register_entity("user", "bob", parent_type="tenant", parent_id="acme")
    registry.register_entity("agent", "alice_agent", parent_type="user", parent_id="alice")
    registry.register_entity("agent", "bob_agent", parent_type="user", parent_id="bob")

    # Verify all exist
    assert registry.get_entity("tenant", "acme") is not None
    assert registry.get_entity("user", "alice") is not None
    assert registry.get_entity("user", "bob") is not None
    assert registry.get_entity("agent", "alice_agent") is not None
    assert registry.get_entity("agent", "bob_agent") is not None

    # Delete tenant (cascade=True by default)
    result = registry.delete_entity("tenant", "acme", cascade=True)
    assert result is True

    # Verify everything is gone
    assert registry.get_entity("tenant", "acme") is None
    assert registry.get_entity("user", "alice") is None
    assert registry.get_entity("user", "bob") is None
    assert registry.get_entity("agent", "alice_agent") is None
    assert registry.get_entity("agent", "bob_agent") is None


def test_no_cascade_delete_leaves_orphans(registry):
    """Test non-cascade deletion leaves children orphaned."""
    # Register hierarchy: user → agent
    registry.register_entity("user", "alice")
    registry.register_entity("agent", "agent_1", parent_type="user", parent_id="alice")

    # Delete user without cascade
    result = registry.delete_entity("user", "alice", cascade=False)
    assert result is True

    # User is gone
    assert registry.get_entity("user", "alice") is None

    # Agent still exists (orphaned)
    agent = registry.get_entity("agent", "agent_1")
    assert agent is not None
    assert agent.parent_id == "alice"  # Still references deleted parent


def test_cascade_delete_default_behavior(registry):
    """Test that cascade=True is the default behavior."""
    # Register hierarchy: user → agent
    registry.register_entity("user", "alice")
    registry.register_entity("agent", "agent_1", parent_type="user", parent_id="alice")

    # Delete user (no cascade parameter = should default to True)
    result = registry.delete_entity("user", "alice")
    assert result is True

    # Both should be gone
    assert registry.get_entity("user", "alice") is None
    assert registry.get_entity("agent", "agent_1") is None


def test_cascade_delete_multiple_levels(registry):
    """Test cascade deletion works through multiple levels."""
    # In theory: tenant → user → agent → sub-agent
    # But current schema only supports 3 levels (agent can't own agents)
    # This test verifies the recursive logic works

    # Register: tenant → user1 → agent1, user2 → agent2
    registry.register_entity("tenant", "acme")
    registry.register_entity("user", "user1", parent_type="tenant", parent_id="acme")
    registry.register_entity("user", "user2", parent_type="tenant", parent_id="acme")
    registry.register_entity("agent", "agent1", parent_type="user", parent_id="user1")
    registry.register_entity("agent", "agent2", parent_type="user", parent_id="user2")

    # Delete tenant
    result = registry.delete_entity("tenant", "acme", cascade=True)
    assert result is True

    # Verify entire hierarchy is gone
    assert registry.get_entity("tenant", "acme") is None
    assert registry.get_entity("user", "user1") is None
    assert registry.get_entity("user", "user2") is None
    assert registry.get_entity("agent", "agent1") is None
    assert registry.get_entity("agent", "agent2") is None


def test_get_children_returns_direct_children_only(registry):
    """Test that get_children only returns direct children, not descendants."""
    # Register: tenant → user → agent
    registry.register_entity("tenant", "acme")
    registry.register_entity("user", "alice", parent_type="tenant", parent_id="acme")
    registry.register_entity("agent", "agent_1", parent_type="user", parent_id="alice")

    # Get children of tenant (should only return user, not agent)
    tenant_children = registry.get_children("tenant", "acme")
    assert len(tenant_children) == 1
    assert tenant_children[0].entity_type == "user"
    assert tenant_children[0].entity_id == "alice"

    # Get children of user (should return agent)
    user_children = registry.get_children("user", "alice")
    assert len(user_children) == 1
    assert user_children[0].entity_type == "agent"
    assert user_children[0].entity_id == "agent_1"

    # Agent has no children
    agent_children = registry.get_children("agent", "agent_1")
    assert len(agent_children) == 0
