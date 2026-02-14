"""Tests for Memory API (Phase 4) and Backward Compatibility (Phase 5)."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from nexus.backends.local import LocalBackend
from nexus.core.memory_api import Memory
from nexus.services.permissions.entity_registry import EntityRegistry
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
    registry.register_entity("zone", "acme")
    registry.register_entity("user", "alice", parent_type="zone", parent_id="acme")
    registry.register_entity("agent", "agent1", parent_type="user", parent_id="alice")
    registry.register_entity("agent", "agent2", parent_type="user", parent_id="alice")
    return registry


@pytest.fixture
def memory_api(session, backend, entity_registry):
    """Create Memory API instance."""
    return Memory(
        session=session,
        backend=backend,
        zone_id="acme",
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
            zone_id="acme",
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
        """Test that agents don't automatically share memory (v0.5.1 - inherit_permissions removed)."""
        # agent1 stores a user-scoped memory
        memory_api1 = Memory(
            session=session,
            backend=backend,
            zone_id="acme",
            user_id="alice",
            agent_id="agent1",
            entity_registry=entity_registry,
        )

        memory_id = memory_api1.store(
            "Shared preference",
            scope="user",
        )

        # agent2 (same user) should NOT be able to access it without explicit ReBAC grant
        # v0.5.1: inherit_permissions feature removed - agents have zero permissions by default
        memory_api2 = Memory(
            session=session,
            backend=backend,
            zone_id="acme",
            user_id="alice",
            agent_id="agent2",
            entity_registry=entity_registry,
        )

        result = memory_api2.get(memory_id)
        assert result is None  # v0.5.1: No automatic sharing

    def test_agent_scoped_memory_isolation(self, session, backend, entity_registry):
        """Test that agent-scoped memories with restrictive permissions are isolated."""
        from nexus.core.memory_router import MemoryViewRouter

        _memory_api1 = Memory(
            session=session,
            backend=backend,
            zone_id="acme",
            user_id="alice",
            agent_id="agent1",
            entity_registry=entity_registry,
        )

        # Store with restrictive permissions (owner only - 0o600)
        memory_router = MemoryViewRouter(session, entity_registry)
        content_hash = backend.write_content(b"Private to agent1").unwrap()
        memory = memory_router.create_memory(
            content_hash=content_hash,
            zone_id="acme",
            user_id="alice",
            agent_id="agent1",
            scope="agent",
            # v0.5.0: mode removed - use ReBAC for permissions
        )

        # agent2 should not see agent1's agent-scoped memory with restrictive permissions
        memory_api2 = Memory(
            session=session,
            backend=backend,
            zone_id="acme",
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


class TestBitemporalMemory:
    """Test bi-temporal memory features (#1183)."""

    def test_store_with_valid_at(self, memory_api):
        """Test storing memory with explicit valid_at."""
        from datetime import UTC, datetime

        past_date = datetime(2025, 1, 1, tzinfo=UTC)
        memory_id = memory_api.store(
            content="Historical fact",
            scope="user",
            valid_at=past_date,
        )

        result = memory_api.get(memory_id)
        assert result is not None
        # Compare date portion (timezone handling varies by database)
        assert "2025-01-01" in result["valid_at"]

    def test_store_with_valid_at_string(self, memory_api):
        """Test storing memory with valid_at as ISO string."""
        memory_id = memory_api.store(
            content="Another historical fact",
            scope="user",
            valid_at="2025-06-15T10:00:00Z",
        )

        result = memory_api.get(memory_id)
        assert result is not None
        assert "2025-06-15" in result["valid_at"]

    def test_invalidate_memory(self, memory_api):
        """Test invalidating a memory."""
        memory_id = memory_api.store(content="Temporary fact", scope="user")

        # Verify it's current initially
        result = memory_api.get(memory_id)
        assert result["is_current"] is True
        assert result["invalid_at"] is None

        # Invalidate it
        success = memory_api.invalidate(memory_id)
        assert success is True

        # Verify it's now invalidated
        result = memory_api.get(memory_id)
        assert result["invalid_at"] is not None
        assert result["is_current"] is False

    def test_invalidate_with_specific_date(self, memory_api):
        """Test invalidating with a specific date."""
        from datetime import UTC, datetime

        memory_id = memory_api.store(content="Dated fact", scope="user")
        invalid_date = datetime(2026, 1, 15, tzinfo=UTC)

        success = memory_api.invalidate(memory_id, invalid_at=invalid_date)
        assert success is True

        result = memory_api.get(memory_id)
        # Compare date portion (timezone handling varies by database)
        assert "2026-01-15" in result["invalid_at"]

    def test_invalidate_with_string_date(self, memory_api):
        """Test invalidating with string date."""
        memory_id = memory_api.store(content="String date fact", scope="user")

        success = memory_api.invalidate(memory_id, invalid_at="2026-03-01T00:00:00Z")
        assert success is True

        result = memory_api.get(memory_id)
        assert "2026-03-01" in result["invalid_at"]

    def test_query_excludes_invalid_by_default(self, memory_api):
        """Test that default query excludes invalidated memories."""
        valid_id = memory_api.store(content="Valid fact", scope="user")
        invalid_id = memory_api.store(content="Invalid fact", scope="user")
        memory_api.invalidate(invalid_id)

        results = memory_api.query()
        memory_ids = [r["memory_id"] for r in results]

        assert valid_id in memory_ids
        assert invalid_id not in memory_ids

    def test_query_include_invalid(self, memory_api):
        """Test query with include_invalid=True."""
        valid_id = memory_api.store(content="Valid fact 2", scope="user")
        invalid_id = memory_api.store(content="Invalid fact 2", scope="user")
        memory_api.invalidate(invalid_id)

        # With include_invalid=True, should include both
        results = memory_api.query(include_invalid=True)
        memory_ids = [r["memory_id"] for r in results]

        assert valid_id in memory_ids
        assert invalid_id in memory_ids

    def test_point_in_time_query(self, memory_api):
        """Test as_of point-in-time query."""
        from datetime import UTC, datetime

        # Create memory valid from Jan 1
        jan_1 = datetime(2026, 1, 1, tzinfo=UTC)
        memory_id = memory_api.store(
            content="January fact",
            scope="user",
            valid_at=jan_1,
        )

        # Invalidate it on Jan 15
        jan_15 = datetime(2026, 1, 15, tzinfo=UTC)
        memory_api.invalidate(memory_id, invalid_at=jan_15)

        # Query as of Jan 10 - should include the memory
        jan_10 = datetime(2026, 1, 10, tzinfo=UTC)
        results = memory_api.query(as_of=jan_10, include_invalid=True)
        memory_ids = [r["memory_id"] for r in results]
        assert memory_id in memory_ids

        # Query as of Jan 20 - should exclude the memory
        jan_20 = datetime(2026, 1, 20, tzinfo=UTC)
        results = memory_api.query(as_of=jan_20, include_invalid=True)
        memory_ids = [r["memory_id"] for r in results]
        assert memory_id not in memory_ids

    def test_invalidate_batch(self, memory_api):
        """Test batch invalidation."""
        ids = [memory_api.store(content=f"Batch fact {i}", scope="user") for i in range(3)]

        result = memory_api.invalidate_batch(ids)
        assert result["invalidated"] == 3
        assert result["failed"] == 0
        assert len(result["invalidated_ids"]) == 3

    def test_revalidate_memory(self, memory_api):
        """Test revalidating a memory."""
        memory_id = memory_api.store(content="Revalidatable fact", scope="user")

        # Invalidate it
        memory_api.invalidate(memory_id)
        result = memory_api.get(memory_id)
        assert result["is_current"] is False

        # Revalidate it
        success = memory_api.revalidate(memory_id)
        assert success is True

        result = memory_api.get(memory_id)
        assert result["is_current"] is True
        assert result["invalid_at"] is None

    def test_invalidate_nonexistent_memory(self, memory_api):
        """Test invalidating a non-existent memory."""
        success = memory_api.invalidate("nonexistent-id")
        assert success is False

    def test_default_valid_at_is_none(self, memory_api):
        """Test that valid_at defaults to None when not provided."""
        memory_id = memory_api.store(content="Default validity", scope="user")

        result = memory_api.get(memory_id)
        assert result["valid_at"] is None  # NULL = use created_at semantically


class TestMemoryStabilityClassification:
    """Test memory auto-classification (#1191)."""

    def test_store_auto_classifies_static_memory(self, memory_api):
        """Test that storing a static-sounding memory gets classified as static."""
        memory_id = memory_api.store(
            content="Paris is the capital of France",
            scope="user",
        )

        result = memory_api.get(memory_id)
        assert result is not None
        assert result["temporal_stability"] == "static"
        assert result["stability_confidence"] is not None
        assert result["stability_confidence"] >= 0.5
        assert result["estimated_ttl_days"] is None  # Static = infinite

    def test_store_auto_classifies_dynamic_memory(self, memory_api):
        """Test that storing a dynamic-sounding memory gets classified as dynamic."""
        memory_id = memory_api.store(
            content="John is currently working on the Q4 report",
            scope="user",
        )

        result = memory_api.get(memory_id)
        assert result is not None
        assert result["temporal_stability"] == "dynamic"
        assert result["stability_confidence"] is not None
        assert result["estimated_ttl_days"] is not None

    def test_store_auto_classifies_semi_dynamic_memory(self, memory_api):
        """Test that storing a semi-dynamic memory gets classified correctly."""
        memory_id = memory_api.store(
            content="Sarah works at Microsoft as a senior engineer",
            scope="user",
        )

        result = memory_api.get(memory_id)
        assert result is not None
        assert result["temporal_stability"] == "semi_dynamic"
        assert result["stability_confidence"] is not None

    def test_store_classification_disabled(self, memory_api):
        """Test that classify_stability=False skips classification."""
        memory_id = memory_api.store(
            content="Paris is the capital of France",
            scope="user",
            classify_stability=False,
        )

        result = memory_api.get(memory_id)
        assert result is not None
        assert result["temporal_stability"] is None
        assert result["stability_confidence"] is None
        assert result["estimated_ttl_days"] is None

    def test_store_classification_failure_non_fatal(self, memory_api):
        """Test that classification failure doesn't break store()."""
        from unittest.mock import patch

        with patch(
            "nexus.services.memory.stability_classifier.TemporalStabilityClassifier.classify",
            side_effect=RuntimeError("Classification engine exploded"),
        ):
            # Should still store successfully despite classification failure
            memory_id = memory_api.store(
                content="Some content that would normally be classified",
                scope="user",
            )

            result = memory_api.get(memory_id)
            assert result is not None
            assert result["memory_id"] == memory_id
            # Classification fields should be None due to failure
            assert result["temporal_stability"] is None

    def test_query_filter_by_temporal_stability(self, memory_api):
        """Test querying memories filtered by temporal_stability."""
        # Store memories with different stability levels
        memory_api.store(content="Paris is the capital of France", scope="user")
        memory_api.store(content="John is currently at the office", scope="user")

        # Query only static memories
        static_results = memory_api.query(temporal_stability="static")
        for r in static_results:
            assert r["temporal_stability"] == "static"

        # Query only dynamic memories
        dynamic_results = memory_api.query(temporal_stability="dynamic")
        for r in dynamic_results:
            assert r["temporal_stability"] == "dynamic"

    def test_classification_fields_in_response(self, memory_api):
        """Test that classification fields appear in get() response."""
        memory_id = memory_api.store(
            content="The Pythagorean theorem states a^2 + b^2 = c^2",
            scope="user",
        )

        result = memory_api.get(memory_id)
        assert "temporal_stability" in result
        assert "stability_confidence" in result
        assert "estimated_ttl_days" in result
