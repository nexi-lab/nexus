"""Tests for Identity-Based Memory System (v0.4.0).

Tests all three phases:
- Phase 1: Entity Registry
- Phase 2: Memory View Router
- Phase 3: Memory Permission Enforcer
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from nexus.core.entity_registry import EntityRegistry
from nexus.core.memory_permission_enforcer import MemoryPermissionEnforcer
from nexus.core.memory_router import MemoryViewRouter
from nexus.core.permissions import OperationContext, Permission
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
    session.close()


@pytest.fixture
def entity_registry(session):
    """Create entity registry."""
    return EntityRegistry(session)


@pytest.fixture
def memory_router(session, entity_registry):
    """Create memory router."""
    return MemoryViewRouter(session, entity_registry)


@pytest.fixture
def setup_entities(entity_registry):
    """Set up test entities."""
    # Create tenant
    entity_registry.register_entity("tenant", "acme")

    # Create user under tenant
    entity_registry.register_entity("user", "alice", parent_type="tenant", parent_id="acme")

    # Create agents under user
    entity_registry.register_entity("agent", "agent1", parent_type="user", parent_id="alice")
    entity_registry.register_entity("agent", "agent2", parent_type="user", parent_id="alice")

    # Create another user with agent
    entity_registry.register_entity("user", "bob", parent_type="tenant", parent_id="acme")
    entity_registry.register_entity("agent", "agent3", parent_type="user", parent_id="bob")


class TestPhase1EntityRegistry:
    """Test Phase 1: Entity Registry."""

    def test_register_entity(self, entity_registry):
        """Test registering entities."""
        entity = entity_registry.register_entity("tenant", "test-tenant")
        assert entity.entity_type == "tenant"
        assert entity.entity_id == "test-tenant"
        assert entity.parent_type is None
        assert entity.parent_id is None

    def test_register_entity_with_parent(self, entity_registry):
        """Test registering entity with parent."""
        entity_registry.register_entity("tenant", "acme")
        entity = entity_registry.register_entity(
            "user", "alice", parent_type="tenant", parent_id="acme"
        )

        assert entity.entity_type == "user"
        assert entity.entity_id == "alice"
        assert entity.parent_type == "tenant"
        assert entity.parent_id == "acme"

    def test_get_entity(self, entity_registry):
        """Test retrieving entity."""
        entity_registry.register_entity("user", "alice")
        entity = entity_registry.get_entity("user", "alice")

        assert entity is not None
        assert entity.entity_id == "alice"

    def test_lookup_entity_by_id(self, entity_registry, setup_entities):
        """Test looking up entity by ID."""
        entities = entity_registry.lookup_entity_by_id("alice")

        assert len(entities) == 1
        assert entities[0].entity_type == "user"
        assert entities[0].entity_id == "alice"

    def test_get_children(self, entity_registry, setup_entities):
        """Test getting child entities."""
        children = entity_registry.get_children("user", "alice")

        assert len(children) == 2
        agent_ids = {child.entity_id for child in children}
        assert agent_ids == {"agent1", "agent2"}

    def test_auto_register_from_config(self, entity_registry):
        """Test auto-registering from config."""
        config = {
            "tenant_id": "acme",
            "user_id": "alice",
            "agent_id": "agent1",
        }

        entity_registry.auto_register_from_config(config)

        # Check all entities were registered
        tenant = entity_registry.get_entity("tenant", "acme")
        user = entity_registry.get_entity("user", "alice")
        agent = entity_registry.get_entity("agent", "agent1")

        assert tenant is not None
        assert user is not None
        assert user.parent_id == "acme"
        assert agent is not None
        assert agent.parent_id == "alice"

    def test_extract_ids_from_path_parts(self, entity_registry, setup_entities):
        """Test extracting IDs from path parts."""
        parts = ["workspace", "alice", "agent1", "memory"]
        ids = entity_registry.extract_ids_from_path_parts(parts)

        assert ids == {"user_id": "alice", "agent_id": "agent1"}

    def test_extract_ids_order_neutral(self, entity_registry, setup_entities):
        """Test order-neutral ID extraction."""
        # Different order should give same result
        parts1 = ["workspace", "alice", "agent1"]
        parts2 = ["workspace", "agent1", "alice"]

        ids1 = entity_registry.extract_ids_from_path_parts(parts1)
        ids2 = entity_registry.extract_ids_from_path_parts(parts2)

        assert ids1 == ids2
        assert ids1 == {"user_id": "alice", "agent_id": "agent1"}


class TestPhase2MemoryRouter:
    """Test Phase 2: Memory View Router."""

    def test_create_memory(self, memory_router):
        """Test creating a memory."""
        memory = memory_router.create_memory(
            content_hash="abc123",
            tenant_id="acme",
            user_id="alice",
            agent_id="agent1",
            scope="user",
        )

        assert memory.memory_id is not None
        assert memory.content_hash == "abc123"
        assert memory.user_id == "alice"
        assert memory.agent_id == "agent1"
        assert memory.scope == "user"

    def test_get_memory_by_id(self, memory_router):
        """Test retrieving memory by ID."""
        created = memory_router.create_memory(
            content_hash="abc123",
            user_id="alice",
        )

        retrieved = memory_router.get_memory_by_id(created.memory_id)
        assert retrieved is not None
        assert retrieved.memory_id == created.memory_id

    def test_query_memories(self, memory_router):
        """Test querying memories by relationships."""
        # Create memories
        memory_router.create_memory(content_hash="abc1", user_id="alice", agent_id="agent1")
        memory_router.create_memory(content_hash="abc2", user_id="alice", agent_id="agent2")
        memory_router.create_memory(content_hash="abc3", user_id="bob", agent_id="agent3")

        # Query by user
        alice_memories = memory_router.query_memories(user_id="alice")
        assert len(alice_memories) == 2

        # Query by agent
        agent1_memories = memory_router.query_memories(agent_id="agent1")
        assert len(agent1_memories) == 1

    def test_resolve_canonical_path(self, memory_router):
        """Test resolving canonical path."""
        created = memory_router.create_memory(
            content_hash="abc123",
            user_id="alice",
        )

        # Resolve canonical path
        resolved = memory_router.resolve(f"/objs/memory/{created.memory_id}")
        assert resolved is not None
        assert resolved.memory_id == created.memory_id

    def test_resolve_order_neutral_path(self, memory_router, setup_entities):
        """Test order-neutral path resolution."""
        # Create memory
        memory = memory_router.create_memory(
            content_hash="abc123",
            user_id="alice",
            agent_id="agent1",
        )

        # Different path orders should resolve to same memory
        path1 = "/workspace/alice/agent1/memory/test.json"
        path2 = "/workspace/agent1/alice/memory/test.json"

        resolved1 = memory_router.resolve(path1)
        resolved2 = memory_router.resolve(path2)

        assert resolved1 is not None
        assert resolved2 is not None
        assert resolved1.memory_id == resolved2.memory_id
        assert resolved1.memory_id == memory.memory_id

    def test_get_virtual_paths(self, memory_router):
        """Test generating virtual paths for a memory."""
        memory = memory_router.create_memory(
            content_hash="abc123",
            tenant_id="acme",
            user_id="alice",
            agent_id="agent1",
        )

        paths = memory_router.get_virtual_paths(memory)

        # Should include canonical path
        assert f"/objs/memory/{memory.memory_id}" in paths

        # Should include by-user path
        assert any("by-user/alice" in p for p in paths)

        # Should include by-agent path
        assert any("by-agent/agent1" in p for p in paths)

    def test_update_memory(self, memory_router):
        """Test updating memory."""
        memory = memory_router.create_memory(content_hash="abc123", user_id="alice")

        updated = memory_router.update_memory(memory.memory_id, importance=0.9, memory_type="fact")

        assert updated is not None
        assert updated.importance == 0.9
        assert updated.memory_type == "fact"

    def test_delete_memory(self, memory_router):
        """Test deleting memory."""
        memory = memory_router.create_memory(content_hash="abc123", user_id="alice")

        deleted = memory_router.delete_memory(memory.memory_id)
        assert deleted is True

        # Should not be found after deletion
        retrieved = memory_router.get_memory_by_id(memory.memory_id)
        assert retrieved is None


class TestPhase3MemoryPermissions:
    """Test Phase 3: Memory Permission Enforcer."""

    @pytest.fixture
    def permission_enforcer(self, memory_router, entity_registry):
        """Create memory permission enforcer."""
        return MemoryPermissionEnforcer(
            memory_router=memory_router,
            entity_registry=entity_registry,
        )

    def test_direct_creator_access(self, memory_router, permission_enforcer, setup_entities):
        """Test direct creator can access memory."""
        memory = memory_router.create_memory(
            content_hash="abc123",
            user_id="alice",
            agent_id="agent1",
            scope="agent",
        )

        ctx = OperationContext(user="agent1", groups=[])
        can_read = permission_enforcer.check_memory(memory, Permission.READ, ctx)

        assert can_read is True

    def test_user_ownership_inheritance(self, memory_router, permission_enforcer, setup_entities):
        """Test user ownership inheritance - agent2 can access agent1's user-scoped memory."""
        # agent1 creates user-scoped memory
        memory = memory_router.create_memory(
            content_hash="abc123",
            user_id="alice",
            agent_id="agent1",
            scope="user",
        )

        # agent2 (also owned by alice) should have access
        ctx = OperationContext(user="agent2", groups=[])
        can_read = permission_enforcer.check_memory(memory, Permission.READ, ctx)

        assert can_read is True

    def test_agent_scoped_memory_isolation(
        self, memory_router, permission_enforcer, setup_entities
    ):
        """Test agent-scoped memory is isolated to creator."""
        # agent1 creates agent-scoped memory with restrictive permissions
        memory = memory_router.create_memory(
            content_hash="abc123",
            user_id="alice",
            agent_id="agent1",
            scope="agent",  # Agent-scoped
            # v0.5.0: mode removed - use ReBAC for permissions
        )

        # agent2 (same user) should NOT have access to agent-scoped memory
        ctx = OperationContext(user="agent2", groups=[])
        can_read = permission_enforcer.check_memory(memory, Permission.READ, ctx)

        # This should be False because scope is 'agent', not 'user'
        # agent2 is not the creator and mode is 0o600 (owner only)
        assert can_read is False  # agent2 is not the creator

    def test_tenant_scoped_sharing(self, memory_router, permission_enforcer, setup_entities):
        """Test tenant-scoped memory sharing."""
        # alice's agent1 creates tenant-scoped memory
        memory = memory_router.create_memory(
            content_hash="abc123",
            tenant_id="acme",
            user_id="alice",
            agent_id="agent1",
            scope="tenant",
        )

        # bob's agent3 (same tenant) should have access
        ctx = OperationContext(user="agent3", groups=[])
        can_read = permission_enforcer.check_memory(memory, Permission.READ, ctx)

        assert can_read is True

    def test_unix_permissions_user_ownership(
        self, memory_router, permission_enforcer, setup_entities
    ):
        """Test UNIX permissions use user_id as owner for user-scoped memories."""
        # Create user-scoped memory with restrictive permissions (owner only)
        memory = memory_router.create_memory(
            content_hash="abc123",
            user_id="alice",
            agent_id="agent1",
            scope="user",  # User-scoped, not agent-scoped
            # v0.5.0: mode removed - use ReBAC for permissions
        )

        # Direct user access should work
        ctx = OperationContext(user="alice", groups=[])
        can_read = permission_enforcer.check_memory(memory, Permission.READ, ctx)

        assert can_read is True

        # Agent owned by alice should also work (resolves to alice)
        ctx2 = OperationContext(user="agent1", groups=[])
        can_read2 = permission_enforcer.check_memory(memory, Permission.READ, ctx2)

        assert can_read2 is True

        # Different user should not have access
        ctx3 = OperationContext(user="bob", groups=[])
        can_read3 = permission_enforcer.check_memory(memory, Permission.READ, ctx3)

        assert can_read3 is False

    def test_admin_bypass(self, memory_router, permission_enforcer):
        """Test admin can access any memory."""
        memory = memory_router.create_memory(
            content_hash="abc123",
            user_id="alice",
            # v0.5.0: mode removed - use ReBAC for permissions
        )

        ctx = OperationContext(user="admin", groups=[], is_admin=True)
        can_read = permission_enforcer.check_memory(memory, Permission.READ, ctx)

        assert can_read is True

    def test_check_memory_by_path(self, memory_router, permission_enforcer, setup_entities):
        """Test checking memory permission by virtual path."""
        memory = memory_router.create_memory(
            content_hash="abc123",
            user_id="alice",
            agent_id="agent1",
        )

        # Check via canonical path
        ctx = OperationContext(user="agent1", groups=[])
        can_read = permission_enforcer.check_memory_by_path(
            f"/objs/memory/{memory.memory_id}",
            Permission.READ,
            ctx,
        )

        assert can_read is True


class TestIntegration:
    """Integration tests for all phases."""

    @pytest.fixture
    def full_setup(self, session, entity_registry, memory_router):
        """Set up complete test environment."""
        # Register entities
        config = {
            "tenant_id": "acme",
            "user_id": "alice",
            "agent_id": "agent1",
        }
        entity_registry.auto_register_from_config(config)

        # Register second agent for alice
        entity_registry.register_entity("agent", "agent2", parent_type="user", parent_id="alice")

        # Create permission enforcer
        enforcer = MemoryPermissionEnforcer(
            memory_router=memory_router,
            entity_registry=entity_registry,
        )

        return {
            "session": session,
            "entity_registry": entity_registry,
            "memory_router": memory_router,
            "enforcer": enforcer,
        }

    def test_multi_agent_memory_sharing(self, full_setup):
        """Test complete multi-agent memory sharing workflow."""
        router = full_setup["memory_router"]
        enforcer = full_setup["enforcer"]

        # agent1 creates user-scoped memory
        memory = router.create_memory(
            content_hash="python_preferences",
            tenant_id="acme",
            user_id="alice",
            agent_id="agent1",
            scope="user",
            memory_type="preference",
        )

        # agent2 (same user) can access it
        ctx = OperationContext(user="agent2", groups=[])
        can_read = enforcer.check_memory(memory, Permission.READ, ctx)

        assert can_read is True

        # Different order of same IDs should resolve to same memory
        path1 = "/workspace/alice/agent1/memory/"
        path2 = "/workspace/agent1/alice/memory/"  # Same IDs, different order

        resolved1 = router.resolve(path1)
        resolved2 = router.resolve(path2)

        # They should resolve to the same memory_id
        assert resolved1 is not None
        assert resolved2 is not None
        assert resolved1.memory_id == resolved2.memory_id

    def test_order_neutral_path_with_permissions(self, full_setup):
        """Test order-neutral paths work correctly with permissions."""
        router = full_setup["memory_router"]
        enforcer = full_setup["enforcer"]

        # Create memory
        memory = router.create_memory(
            content_hash="test_content",
            tenant_id="acme",
            user_id="alice",
            agent_id="agent1",
        )

        # Multiple path formats should work
        paths = [
            f"/objs/memory/{memory.memory_id}",
            "/workspace/alice/agent1/memory/",
            "/workspace/agent1/alice/memory/",
        ]

        ctx = OperationContext(user="agent1", groups=[])

        for path in paths:
            can_read = enforcer.check_memory_by_path(path, Permission.READ, ctx)
            assert can_read is True
