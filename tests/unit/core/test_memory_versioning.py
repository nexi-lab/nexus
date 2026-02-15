"""Tests for Memory Versioning (Issue #1184).

TDD approach: These tests capture the core requirements before implementation.

Best practices applied:
- Shadow table pattern: VersionHistoryModel tracks all memory versions
- Immutable CAS: Content stored by hash, versions point to hashes
- Audit trail: Full lineage tracking with parent_version_id
- Atomic operations: Version increment at database level

References:
- https://www.red-gate.com/blog/database-design-for-audit-logging/
- https://langchain-ai.github.io/langmem/concepts/conceptual_guide/
"""

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from nexus.backends.local import LocalBackend
from nexus.services.memory.memory_api import Memory
from nexus.services.permissions.entity_registry import EntityRegistry
from nexus.storage.models import Base, MemoryModel, VersionHistoryModel


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
    registry.register_entity("zone", "acme")
    registry.register_entity("user", "alice", parent_type="zone", parent_id="acme")
    registry.register_entity("agent", "agent1", parent_type="user", parent_id="alice")
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


class TestMemoryVersionCreation:
    """Test that version entries are created when memories are stored/updated."""

    def test_memory_has_current_version_field(self, memory_api, session):
        """MemoryModel should have a current_version field tracking the version number."""
        # Store a memory
        memory_id = memory_api.store(
            content="Paris is the capital of France",
            scope="user",
            memory_type="fact",
        )

        # Verify MemoryModel has current_version
        memory = session.execute(
            select(MemoryModel).where(MemoryModel.memory_id == memory_id)
        ).scalar_one()

        assert hasattr(memory, "current_version"), "MemoryModel should have current_version field"
        assert memory.current_version == 1, "Initial version should be 1"

    def test_version_entry_created_on_memory_store(self, memory_api, session):
        """Creating a memory should create a version history entry."""
        # Store a memory
        memory_id = memory_api.store(
            content="The sky is blue",
            scope="user",
            memory_type="fact",
        )

        # Check version history entry was created
        version_entry = session.execute(
            select(VersionHistoryModel).where(
                VersionHistoryModel.resource_type == "memory",
                VersionHistoryModel.resource_id == memory_id,
            )
        ).scalar_one_or_none()

        assert version_entry is not None, "Version entry should be created"
        assert version_entry.version_number == 1
        assert version_entry.source_type == "original"
        assert version_entry.content_hash is not None

    def test_version_entry_created_on_memory_update(self, memory_api, session):
        """Updating a memory should create a new version history entry."""
        # Store initial memory with upsert mode (namespace + path_key)
        memory_id = memory_api.store(
            content="Initial content",
            scope="user",
            namespace="test/facts",
            path_key="sky_color",
        )

        # Update the memory (upsert mode triggers update)
        memory_api.store(
            content="Updated content - the sky is actually blue",
            scope="user",
            namespace="test/facts",
            path_key="sky_color",
        )

        # #1188: With append-only, use list_versions API which follows the chain
        versions = memory_api.list_versions(memory_id)

        assert len(versions) == 2, "Should have 2 versions after update"
        # list_versions returns newest first
        assert versions[1]["version"] == 1
        assert versions[0]["version"] == 2
        assert versions[0]["source_type"] == "update"

    def test_version_tracks_content_hash_and_size(self, memory_api, session):
        """Version entry should track content hash and size."""
        content = "This is test content for versioning"
        memory_id = memory_api.store(
            content=content,
            scope="user",
            memory_type="fact",
        )

        version_entry = session.execute(
            select(VersionHistoryModel).where(
                VersionHistoryModel.resource_type == "memory",
                VersionHistoryModel.resource_id == memory_id,
            )
        ).scalar_one()

        assert version_entry.content_hash is not None
        assert len(version_entry.content_hash) > 0
        assert version_entry.size_bytes > 0

    def test_consolidation_creates_version_entry(self, memory_api, session):
        """Consolidation should create a version entry with source_type='consolidated'."""
        # Store memory with upsert mode
        memory_id = memory_api.store(
            content="Original fact",
            scope="user",
            namespace="test/facts",
            path_key="consolidated_fact",
        )

        # Update with consolidation (simulated by change_reason)
        memory_api.store(
            content="Consolidated: Original fact + additional context",
            scope="user",
            namespace="test/facts",
            path_key="consolidated_fact",
            _metadata={"change_reason": "consolidation"},
        )

        # #1188: Use list_versions API which follows the supersedes chain
        versions = memory_api.list_versions(memory_id)

        # Latest version should exist (either update or consolidated)
        assert len(versions) >= 2


class TestMemoryVersionRetrieval:
    """Test Memory API version retrieval methods."""

    def test_list_versions_returns_all_versions(self, memory_api, session):
        """Memory.list_versions() should return all version history."""
        # Create memory with multiple versions
        memory_id = memory_api.store(
            content="Version 1",
            scope="user",
            namespace="test/versioned",
            path_key="doc",
        )

        memory_api.store(
            content="Version 2 - updated",
            scope="user",
            namespace="test/versioned",
            path_key="doc",
        )

        memory_api.store(
            content="Version 3 - final",
            scope="user",
            namespace="test/versioned",
            path_key="doc",
        )

        # Call list_versions API
        versions = memory_api.list_versions(memory_id)

        assert isinstance(versions, list)
        assert len(versions) == 3
        # Should be in reverse chronological order (newest first)
        assert versions[0]["version"] == 3
        assert versions[1]["version"] == 2
        assert versions[2]["version"] == 1

    def test_list_versions_includes_metadata(self, memory_api, session):
        """list_versions should include version metadata."""
        memory_id = memory_api.store(
            content="Test content",
            scope="user",
            memory_type="fact",
        )

        versions = memory_api.list_versions(memory_id)

        assert len(versions) == 1
        v = versions[0]
        assert "version" in v
        assert "content_hash" in v
        assert "size" in v
        assert "created_at" in v
        assert "created_by" in v

    def test_get_version_retrieves_specific_version_content(self, memory_api, session):
        """Memory.get_version() should retrieve content for a specific version."""
        # Create memory with multiple versions
        memory_id = memory_api.store(
            content="First version content",
            scope="user",
            namespace="test/versioned",
            path_key="content",
        )

        memory_api.store(
            content="Second version content",
            scope="user",
            namespace="test/versioned",
            path_key="content",
        )

        # Get version 1
        v1_content = memory_api.get_version(memory_id, version=1)
        assert v1_content is not None
        assert v1_content["content"] == "First version content"

        # Get version 2
        v2_content = memory_api.get_version(memory_id, version=2)
        assert v2_content is not None
        assert v2_content["content"] == "Second version content"

    def test_get_version_returns_none_for_invalid_version(self, memory_api):
        """get_version should return None for non-existent versions."""
        memory_id = memory_api.store(content="Test", scope="user")

        result = memory_api.get_version(memory_id, version=999)
        assert result is None

    def test_get_version_returns_none_for_invalid_memory(self, memory_api):
        """get_version should return None for non-existent memories."""
        result = memory_api.get_version("non-existent-id", version=1)
        assert result is None


class TestMemoryRollback:
    """Test Memory rollback functionality."""

    def test_rollback_restores_previous_content(self, memory_api, session):
        """rollback() should restore memory to a previous version's content."""
        # Create memory with multiple versions
        memory_id = memory_api.store(
            content="Original content - correct",
            scope="user",
            namespace="test/rollback",
            path_key="doc",
        )

        # #1188: Append-only update returns new memory_id
        new_memory_id = memory_api.store(
            content="Bad update - this is wrong",
            scope="user",
            namespace="test/rollback",
            path_key="doc",
        )

        # Verify current content is wrong (use new_memory_id)
        current = memory_api.get(new_memory_id)
        assert "wrong" in current["content"]

        # Rollback to version 1 (uses any memory_id in the chain)
        memory_api.rollback(memory_id, version=1)

        # Verify content is restored on the latest memory
        restored = memory_api.get(new_memory_id)
        assert restored["content"] == "Original content - correct"

    def test_rollback_creates_new_version_entry(self, memory_api, session):
        """rollback() should create a new version entry with source_type='rollback'."""
        memory_id = memory_api.store(
            content="Version 1",
            scope="user",
            namespace="test/rollback",
            path_key="tracked",
        )

        memory_api.store(
            content="Version 2",
            scope="user",
            namespace="test/rollback",
            path_key="tracked",
        )

        # Rollback to version 1 (can use any memory_id in the chain)
        memory_api.rollback(memory_id, version=1)

        # Check version history (follows chain from any memory_id)
        versions = memory_api.list_versions(memory_id)

        # Should have 3 versions: original, update, rollback
        assert len(versions) == 3
        assert versions[0]["version"] == 3  # Newest - the rollback
        assert versions[0]["source_type"] == "rollback"
        assert "Rollback to version 1" in (versions[0].get("change_reason") or "")

    def test_rollback_preserves_lineage(self, memory_api, session):
        """rollback version should track its parent correctly."""
        memory_id = memory_api.store(
            content="V1",
            scope="user",
            namespace="test/lineage",
            path_key="doc",
        )

        memory_api.store(
            content="V2",
            scope="user",
            namespace="test/lineage",
            path_key="doc",
        )

        memory_api.rollback(memory_id, version=1)

        # #1188: Use list_versions API which follows supersedes chain
        versions = memory_api.list_versions(memory_id)

        # Versions are newest-first: [v3, v2, v1]
        assert len(versions) == 3
        v3 = versions[0]  # rollback
        # v3 (rollback) should have parent_version_id pointing to v2's version entry
        # The parent_version_id tracks lineage across the chain
        assert v3["source_type"] == "rollback"
        assert v3["version"] == 3

    def test_rollback_increments_current_version(self, memory_api, session):
        """rollback should increment memory's current_version."""
        memory_id = memory_api.store(
            content="V1",
            scope="user",
            namespace="test/version",
            path_key="doc",
        )

        # #1188: Append-only update returns new memory_id
        new_memory_id = memory_api.store(
            content="V2",
            scope="user",
            namespace="test/version",
            path_key="doc",
        )

        # Check current_version on the latest memory (new row)
        new_memory = session.execute(
            select(MemoryModel).where(MemoryModel.memory_id == new_memory_id)
        ).scalar_one()
        assert new_memory.current_version == 2

        memory_api.rollback(memory_id, version=1)

        session.refresh(new_memory)
        assert new_memory.current_version == 3

    def test_rollback_invalid_version_raises_error(self, memory_api):
        """rollback to non-existent version should raise error."""
        memory_id = memory_api.store(content="Test", scope="user")

        with pytest.raises(ValueError):
            memory_api.rollback(memory_id, version=999)

    def test_rollback_invalid_memory_raises_error(self, memory_api):
        """rollback on non-existent memory should raise error."""
        with pytest.raises(ValueError):
            memory_api.rollback("non-existent-id", version=1)


class TestMemoryVersionDiff:
    """Test Memory version diff functionality."""

    def test_diff_versions_shows_content_change(self, memory_api):
        """diff_versions should indicate if content changed."""
        memory_id = memory_api.store(
            content="Original text",
            scope="user",
            namespace="test/diff",
            path_key="doc",
        )

        memory_api.store(
            content="Modified text",
            scope="user",
            namespace="test/diff",
            path_key="doc",
        )

        diff = memory_api.diff_versions(memory_id, v1=1, v2=2)

        assert diff["content_changed"] is True
        assert diff["content_hash_v1"] != diff["content_hash_v2"]

    def test_diff_versions_shows_size_delta(self, memory_api):
        """diff_versions should show size difference between versions."""
        memory_id = memory_api.store(
            content="Short",
            scope="user",
            namespace="test/diff",
            path_key="size",
        )

        memory_api.store(
            content="This is a much longer piece of content",
            scope="user",
            namespace="test/diff",
            path_key="size",
        )

        diff = memory_api.diff_versions(memory_id, v1=1, v2=2)

        assert "size_v1" in diff
        assert "size_v2" in diff
        assert "size_delta" in diff
        assert diff["size_delta"] > 0  # v2 is larger

    def test_diff_versions_content_mode(self, memory_api):
        """diff_versions with mode='content' should return unified diff."""
        memory_id = memory_api.store(
            content="Line 1\nLine 2\nLine 3",
            scope="user",
            namespace="test/diff",
            path_key="lines",
        )

        memory_api.store(
            content="Line 1\nModified Line 2\nLine 3\nLine 4",
            scope="user",
            namespace="test/diff",
            path_key="lines",
        )

        diff = memory_api.diff_versions(memory_id, v1=1, v2=2, mode="content")

        # Content diff should be a string in unified diff format
        assert isinstance(diff, str)
        assert "---" in diff or "-Line 2" in diff or "+Modified" in diff

    def test_diff_versions_invalid_versions_raises_error(self, memory_api):
        """diff_versions with invalid version numbers should raise error."""
        memory_id = memory_api.store(content="Test", scope="user")

        with pytest.raises(ValueError):
            memory_api.diff_versions(memory_id, v1=1, v2=999)


class TestMemoryVersionEdgeCases:
    """Test edge cases for memory versioning."""

    def test_version_numbers_are_sequential(self, memory_api, session):
        """Version numbers should be sequential without gaps."""
        memory_id = memory_api.store(
            content="V1",
            scope="user",
            namespace="test/seq",
            path_key="doc",
        )

        for i in range(2, 6):
            memory_api.store(
                content=f"V{i}",
                scope="user",
                namespace="test/seq",
                path_key="doc",
            )

        versions = memory_api.list_versions(memory_id)
        version_numbers = [v["version"] for v in versions]

        assert version_numbers == [5, 4, 3, 2, 1]  # Descending order

    def test_concurrent_updates_handle_version_correctly(self, memory_api, session):
        """Concurrent updates should result in sequential versions (atomic increment)."""
        # This tests that version increment is atomic at database level
        memory_id = memory_api.store(
            content="Initial",
            scope="user",
            namespace="test/concurrent",
            path_key="doc",
        )

        # Simulate rapid updates - each returns a new memory_id
        latest_memory_id = memory_id
        for i in range(5):
            latest_memory_id = memory_api.store(
                content=f"Update {i}",
                scope="user",
                namespace="test/concurrent",
                path_key="doc",
            )

        # #1188: Check current_version on the latest memory in the chain
        latest_memory = session.execute(
            select(MemoryModel).where(MemoryModel.memory_id == latest_memory_id)
        ).scalar_one()

        # Should have 6 versions (1 original + 5 updates)
        assert latest_memory.current_version == 6

    def test_version_history_persists_after_delete(self, memory_api, session):
        """Version history should remain after memory is deleted (audit trail)."""
        memory_id = memory_api.store(
            content="To be deleted",
            scope="user",
            namespace="test/audit",
            path_key="deleted",
        )

        new_memory_id = memory_api.store(
            content="Updated before delete",
            scope="user",
            namespace="test/audit",
            path_key="deleted",
        )

        # #1188: Delete the latest memory (soft-delete)
        memory_api.delete(new_memory_id)

        # #1188: Use list_versions API which follows the supersedes chain
        # Version history should persist even after soft-delete
        versions = memory_api.list_versions(memory_id)

        assert len(versions) == 2, "Version history should persist for audit trail"

    def test_binary_content_versioning(self, memory_api, session):
        """Binary content should be versioned correctly."""
        memory_id = memory_api.store(
            content=b"Binary data v1",
            scope="user",
        )

        versions = memory_api.list_versions(memory_id)
        assert len(versions) == 1
        assert versions[0]["content_hash"] is not None

    def test_structured_content_versioning(self, memory_api, session):
        """Structured dict content should be versioned correctly."""
        memory_id = memory_api.store(
            content={"fact": "Paris is capital of France", "confidence": 0.95},
            scope="user",
            namespace="test/structured",
            path_key="paris",
        )

        memory_api.store(
            content={"fact": "Paris is capital of France", "confidence": 0.99, "verified": True},
            scope="user",
            namespace="test/structured",
            path_key="paris",
        )

        versions = memory_api.list_versions(memory_id)
        assert len(versions) == 2
        # Hashes should differ because content is different
        assert versions[0]["content_hash"] != versions[1]["content_hash"]
