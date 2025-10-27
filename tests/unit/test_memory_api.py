"""Tests for Memory API (Phase 4) and Backward Compatibility (Phase 5)."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from nexus.backends.local import LocalBackend
from nexus.core.entity_registry import EntityRegistry
from nexus.core.memory_api import Memory
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
def backend(tmp_path):
    """Create local backend for content storage."""
    return LocalBackend(root_path=tmp_path)


@pytest.fixture
def entity_registry(session):
    """Create and populate entity registry."""
    registry = EntityRegistry(session)
    # Register test entities
    registry.register_entity("tenant", "acme")
    registry.register_entity("user", "alice", parent_type="tenant", parent_id="acme")
    registry.register_entity("agent", "agent1", parent_type="user", parent_id="alice")
    registry.register_entity("agent", "agent2", parent_type="user", parent_id="alice")
    return registry


@pytest.fixture
def memory_api(session, backend, entity_registry):
    """Create Memory API instance."""
    return Memory(
        session=session,
        backend=backend,
        tenant_id="acme",
        user_id="alice",
        agent_id="agent1",
        entity_registry=entity_registry,
    )


class TestPhase4MemoryAPI:
    """Test Phase 4: Memory API implementation."""

    def test_store_memory(self, memory_api):
        """Test storing a memory."""
        memory_id = memory_api.store(
            content="User prefers Python over JavaScript",
            scope="user",
            memory_type="preference",
            importance=0.9,
        )

        assert memory_id is not None
        assert len(memory_id) > 0

    def test_query_memory(self, memory_api):
        """Test querying memories."""
        # Store some memories
        memory_api.store("Fact 1", scope="user", memory_type="fact")
        memory_api.store("Preference 1", scope="user", memory_type="preference")
        memory_api.store("Experience 1", scope="agent", memory_type="experience")

        # Query all
        results = memory_api.query()
        assert len(results) >= 3

        # Query by type
        preferences = memory_api.query(memory_type="preference")
        assert len(preferences) == 1
        assert preferences[0]["content"] == "Preference 1"

        # Query by scope
        user_memories = memory_api.query(scope="user")
        assert len(user_memories) == 2

    def test_search_memory(self, memory_api):
        """Test semantic search over memories."""
        # Store test data
        memory_api.store("Python is a great language", scope="user")
        memory_api.store("JavaScript has async/await", scope="user")
        memory_api.store("User likes coffee", scope="user")

        # Search for Python
        results = memory_api.search("Python programming")
        assert len(results) > 0
        assert any("Python" in r["content"] for r in results)

    def test_get_memory(self, memory_api):
        """Test getting a specific memory."""
        # Store memory
        memory_id = memory_api.store("Test content", scope="user")

        # Retrieve it
        result = memory_api.get(memory_id)
        assert result is not None
        assert result["memory_id"] == memory_id
        assert result["content"] == "Test content"
        assert result["scope"] == "user"

    def test_list_memories(self, memory_api):
        """Test listing memories."""
        # Store some memories
        memory_api.store("Memory 1", scope="user")
        memory_api.store("Memory 2", scope="agent")
        memory_api.store("Memory 3", scope="user", memory_type="preference")

        # List all
        results = memory_api.list()
        assert len(results) >= 3

        # List by scope
        user_memories = memory_api.list(scope="user")
        assert len(user_memories) == 2

        # List by type
        preferences = memory_api.list(memory_type="preference")
        assert len(preferences) == 1

    def test_delete_memory(self, memory_api):
        """Test deleting a memory."""
        # Store memory
        memory_id = memory_api.store("To be deleted", scope="user")

        # Delete it
        deleted = memory_api.delete(memory_id)
        assert deleted is True

        # Verify it's gone
        result = memory_api.get(memory_id)
        assert result is None

    def test_memory_with_importance(self, memory_api):
        """Test storing memory with importance score."""
        memory_id = memory_api.store(
            content="Critical information",
            scope="user",
            importance=1.0,
        )

        result = memory_api.get(memory_id)
        assert result["importance"] == 1.0

    def test_memory_with_metadata(self, memory_api):
        """Test storing memory with full metadata."""
        memory_id = memory_api.store(
            content="User birthday: January 1, 2000",
            scope="user",
            memory_type="fact",
            importance=0.8,
        )

        result = memory_api.get(memory_id)
        assert result["memory_type"] == "fact"
        assert result["importance"] == 0.8


class TestPhase5BackwardCompatibility:
    """Test Phase 5: Backward compatibility."""

    def test_user_id_fallback_to_agent_id(self, session, backend, entity_registry):
        """Test that user_id falls back to agent_id if not provided."""
        # Create Memory API without user_id (old behavior)
        memory_api = Memory(
            session=session,
            backend=backend,
            tenant_id="acme",
            user_id=None,  # Not provided
            agent_id="agent1",
            entity_registry=entity_registry,
        )

        # Store memory
        memory_id = memory_api.store("Test content", scope="user")

        # Check that user_id was set to agent_id
        result = memory_api.get(memory_id)
        assert result["user_id"] == "agent1"  # Fallback worked
        assert result["agent_id"] == "agent1"

    def test_memory_sharing_across_agents(self, session, backend, entity_registry):
        """Test that memories can be shared across agents of same user."""
        # agent1 stores a user-scoped memory
        memory_api1 = Memory(
            session=session,
            backend=backend,
            tenant_id="acme",
            user_id="alice",
            agent_id="agent1",
            entity_registry=entity_registry,
        )

        memory_id = memory_api1.store(
            "Shared preference",
            scope="user",
        )

        # agent2 (same user) should be able to access it
        memory_api2 = Memory(
            session=session,
            backend=backend,
            tenant_id="acme",
            user_id="alice",
            agent_id="agent2",
            entity_registry=entity_registry,
        )

        result = memory_api2.get(memory_id)
        assert result is not None
        assert result["content"] == "Shared preference"

    def test_agent_scoped_memory_isolation(self, session, backend, entity_registry):
        """Test that agent-scoped memories with restrictive permissions are isolated."""
        from nexus.core.memory_router import MemoryViewRouter

        _memory_api1 = Memory(
            session=session,
            backend=backend,
            tenant_id="acme",
            user_id="alice",
            agent_id="agent1",
            entity_registry=entity_registry,
        )

        # Store with restrictive permissions (owner only - 0o600)
        memory_router = MemoryViewRouter(session, entity_registry)
        content_hash = backend.write_content(b"Private to agent1")
        memory = memory_router.create_memory(
            content_hash=content_hash,
            tenant_id="acme",
            user_id="alice",
            agent_id="agent1",
            scope="agent",
            # v0.5.0: mode removed - use ReBAC for permissions
        )

        # agent2 should not see agent1's agent-scoped memory with restrictive permissions
        memory_api2 = Memory(
            session=session,
            backend=backend,
            tenant_id="acme",
            user_id="alice",
            agent_id="agent2",
            entity_registry=entity_registry,
        )

        # agent2 should not be able to get agent1's memory (no permission)
        result = memory_api2.get(memory.memory_id)
        assert result is None  # No permission due to restrictive UNIX permissions

    def test_migration_creates_tables(self, session):
        """Test that migration creates necessary tables."""
        from nexus.migrations.migrate_identity_memory_v04 import IdentityMemoryMigration

        migration = IdentityMemoryMigration(session)

        # Tables should already exist from fixture setup
        assert not migration.needs_migration()

    def test_binary_content_storage(self, memory_api):
        """Test storing binary content."""
        binary_data = b"\x00\x01\x02\x03\xff"

        memory_id = memory_api.store(binary_data, scope="user")
        result = memory_api.get(memory_id)

        assert result is not None
        # Binary content should be hex-encoded
        assert result["content"] == binary_data.hex()

    def test_large_content_storage(self, memory_api):
        """Test storing large content."""
        large_content = "A" * 10000  # 10KB

        memory_id = memory_api.store(large_content, scope="user")
        result = memory_api.get(memory_id)

        assert result is not None
        assert len(result["content"]) == 10000

    def test_query_with_limit(self, memory_api):
        """Test querying with limit."""
        # Store multiple memories
        for i in range(10):
            memory_api.store(f"Memory {i}", scope="user")

        # Query with limit
        results = memory_api.query(limit=5)
        assert len(results) == 5

    def test_search_no_results(self, memory_api):
        """Test search with no matching results."""
        memory_api.store("Python programming", scope="user")

        results = memory_api.search("Rust programming")
        # Should return empty or low-scored results (score <= 0.5 is low relevance)
        assert len(results) == 0 or results[0]["score"] <= 0.5
