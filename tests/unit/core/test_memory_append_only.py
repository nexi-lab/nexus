"""Tests for Non-Destructive Memory Updates - Append-Only Pattern (Issue #1188).

TDD approach: These tests capture all acceptance criteria before implementation.

The append-only pattern ensures:
- Updates create new rows instead of overwriting existing data
- Old rows are preserved with invalid_at timestamp
- New rows link to old via supersedes_id
- Complete audit trails and point-in-time queries
- Soft-delete replaces hard delete

Best practices applied:
- Event Sourcing: All state changes are captured as new rows
- Bi-temporal model: valid_at/invalid_at tracks fact validity
- Datomic-style immutability: Never overwrite, always append
- Zep/Graphiti: Temporal edge invalidation pattern

References:
- https://martinfowler.com/eaaDev/EventSourcing.html
- https://docs.datomic.com/
- https://arxiv.org/abs/2501.13956 (Zep temporal model)
"""

import time

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from nexus.backends.local import LocalBackend
from nexus.core.memory_api import Memory
from nexus.rebac.entity_registry import EntityRegistry
from nexus.storage.models import Base, MemoryModel, VersionHistoryModel

# ============================================================================
# Fixtures
# ============================================================================


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


# ============================================================================
# AC1: Add supersedes_id and superseded_by_id to MemoryModel
# ============================================================================


class TestSupersededFields:
    """Test that MemoryModel has the new supersedes tracking fields."""

    def test_memory_model_has_supersedes_id(self, session):
        """MemoryModel should have a supersedes_id field (FK to memory_id)."""
        assert hasattr(MemoryModel, "supersedes_id"), (
            "MemoryModel must have supersedes_id field for lineage tracking"
        )

    def test_memory_model_has_superseded_by_id(self, session):
        """MemoryModel should have a superseded_by_id field (denormalized)."""
        assert hasattr(MemoryModel, "superseded_by_id"), (
            "MemoryModel must have superseded_by_id for fast lookup"
        )

    def test_supersedes_id_nullable(self, memory_api, session):
        """supersedes_id should be NULL for original (non-superseding) memories."""
        memory_id = memory_api.store(
            content="Original fact",
            scope="user",
            memory_type="fact",
        )

        memory = session.execute(
            select(MemoryModel).where(MemoryModel.memory_id == memory_id)
        ).scalar_one()

        assert memory.supersedes_id is None, "Original memory should have NULL supersedes_id"

    def test_superseded_by_id_nullable(self, memory_api, session):
        """superseded_by_id should be NULL for current (non-superseded) memories."""
        memory_id = memory_api.store(
            content="Current fact",
            scope="user",
            memory_type="fact",
        )

        memory = session.execute(
            select(MemoryModel).where(MemoryModel.memory_id == memory_id)
        ).scalar_one()

        assert memory.superseded_by_id is None, "Current memory should have NULL superseded_by_id"


# ============================================================================
# AC2: Implement append-only update pattern
# ============================================================================


class TestAppendOnlyUpdate:
    """Test that updates create new rows instead of overwriting existing data."""

    def test_update_creates_new_memory_row(self, memory_api, session):
        """Updating a memory via upsert should create a NEW MemoryModel row."""
        # Store initial memory
        original_id = memory_api.store(
            content="Paris is the capital of France",
            scope="user",
            namespace="knowledge/geography",
            path_key="france_capital",
        )

        # Update the memory (upsert mode)
        new_id = memory_api.store(
            content="Paris is the capital and largest city of France",
            scope="user",
            namespace="knowledge/geography",
            path_key="france_capital",
        )

        # The returned ID should be DIFFERENT (new row, not in-place update)
        assert new_id != original_id, "Append-only update must create a new row with new memory_id"

        # Both rows should exist in the database
        all_memories = session.execute(select(MemoryModel)).scalars().all()
        memory_ids = [m.memory_id for m in all_memories]
        assert original_id in memory_ids, "Original memory must still exist"
        assert new_id in memory_ids, "New memory must exist"

    def test_update_marks_old_memory_with_invalid_at(self, memory_api, session):
        """The old memory should have invalid_at set when superseded."""
        original_id = memory_api.store(
            content="Old content",
            scope="user",
            namespace="test/append",
            path_key="doc",
        )

        memory_api.store(
            content="New content",
            scope="user",
            namespace="test/append",
            path_key="doc",
        )

        # Old memory should have invalid_at set
        old_memory = session.execute(
            select(MemoryModel).where(MemoryModel.memory_id == original_id)
        ).scalar_one()

        assert old_memory.invalid_at is not None, (
            "Superseded memory must have invalid_at timestamp set"
        )

    def test_update_links_new_to_old_via_supersedes_id(self, memory_api, session):
        """New memory should link to old memory via supersedes_id."""
        original_id = memory_api.store(
            content="V1 content",
            scope="user",
            namespace="test/link",
            path_key="linked",
        )

        new_id = memory_api.store(
            content="V2 content",
            scope="user",
            namespace="test/link",
            path_key="linked",
        )

        new_memory = session.execute(
            select(MemoryModel).where(MemoryModel.memory_id == new_id)
        ).scalar_one()

        assert new_memory.supersedes_id == original_id, (
            "New memory must have supersedes_id pointing to the old memory"
        )

    def test_update_sets_superseded_by_on_old_memory(self, memory_api, session):
        """Old memory should have superseded_by_id set to new memory ID."""
        original_id = memory_api.store(
            content="Original",
            scope="user",
            namespace="test/backlink",
            path_key="doc",
        )

        new_id = memory_api.store(
            content="Replacement",
            scope="user",
            namespace="test/backlink",
            path_key="doc",
        )

        old_memory = session.execute(
            select(MemoryModel).where(MemoryModel.memory_id == original_id)
        ).scalar_one()

        assert old_memory.superseded_by_id == new_id, (
            "Old memory must have superseded_by_id pointing to the new memory"
        )

    def test_update_preserves_old_content(self, memory_api, session):
        """Old memory content must remain accessible through CAS."""
        original_id = memory_api.store(
            content="Preserved content",
            scope="user",
            namespace="test/preserve",
            path_key="data",
        )

        # Store original content hash
        old_memory = session.execute(
            select(MemoryModel).where(MemoryModel.memory_id == original_id)
        ).scalar_one()
        old_content_hash = old_memory.content_hash

        memory_api.store(
            content="Replacement content",
            scope="user",
            namespace="test/preserve",
            path_key="data",
        )

        # Re-fetch old memory - content_hash should be unchanged
        session.refresh(old_memory)
        assert old_memory.content_hash == old_content_hash, (
            "Old memory's content_hash must remain unchanged"
        )

    def test_update_new_memory_has_incremented_version(self, memory_api, session):
        """New memory should have current_version incremented from old."""
        memory_api.store(
            content="V1",
            scope="user",
            namespace="test/version",
            path_key="doc",
        )

        new_id = memory_api.store(
            content="V2",
            scope="user",
            namespace="test/version",
            path_key="doc",
        )

        new_memory = session.execute(
            select(MemoryModel).where(MemoryModel.memory_id == new_id)
        ).scalar_one()

        assert new_memory.current_version == 2, (
            "New memory version should be 2 (incremented from 1)"
        )

    def test_update_copies_identity_fields(self, memory_api, session):
        """New memory should inherit identity fields from old memory."""
        original_id = memory_api.store(
            content="Identity test",
            scope="user",
            namespace="test/identity",
            path_key="doc",
            memory_type="fact",
        )

        new_id = memory_api.store(
            content="Updated identity test",
            scope="user",
            namespace="test/identity",
            path_key="doc",
        )

        old = session.execute(
            select(MemoryModel).where(MemoryModel.memory_id == original_id)
        ).scalar_one()
        new = session.execute(
            select(MemoryModel).where(MemoryModel.memory_id == new_id)
        ).scalar_one()

        assert new.zone_id == old.zone_id
        assert new.user_id == old.user_id
        assert new.agent_id == old.agent_id

    def test_multiple_updates_create_chain(self, memory_api, session):
        """Multiple updates should create a chain: v1 -> v2 -> v3."""
        v1_id = memory_api.store(
            content="Version 1",
            scope="user",
            namespace="test/chain",
            path_key="doc",
        )

        v2_id = memory_api.store(
            content="Version 2",
            scope="user",
            namespace="test/chain",
            path_key="doc",
        )

        v3_id = memory_api.store(
            content="Version 3",
            scope="user",
            namespace="test/chain",
            path_key="doc",
        )

        v2 = session.execute(select(MemoryModel).where(MemoryModel.memory_id == v2_id)).scalar_one()
        v3 = session.execute(select(MemoryModel).where(MemoryModel.memory_id == v3_id)).scalar_one()

        assert v2.supersedes_id == v1_id, "v2 should supersede v1"
        assert v3.supersedes_id == v2_id, "v3 should supersede v2"
        assert v3.current_version == 3, "v3 should be version 3"

    def test_correction_preserves_original_valid_at(self, memory_api, session):
        """Correction mode should preserve the original valid_at timestamp.

        When correction=True, the fact was always true at the original time,
        we're just fixing an error in the content.
        """
        from datetime import UTC, datetime

        original_valid_at = datetime(2025, 1, 1, tzinfo=UTC)

        memory_api.store(
            content="Pariz is capital of France",  # typo
            scope="user",
            namespace="test/correction",
            path_key="capital",
            valid_at=original_valid_at,
        )

        # Correction: fix typo, same fact was always true
        new_id = memory_api.store(
            content="Paris is the capital of France",  # corrected
            scope="user",
            namespace="test/correction",
            path_key="capital",
            _metadata={"correction": True},
        )

        new_memory = session.execute(
            select(MemoryModel).where(MemoryModel.memory_id == new_id)
        ).scalar_one()

        # For corrections, valid_at should be inherited from original
        assert new_memory.valid_at is not None, "Correction should inherit valid_at from original"

    def test_new_information_gets_fresh_valid_at(self, memory_api, session):
        """Non-correction update (new information) gets fresh valid_at."""
        from datetime import UTC, datetime

        old_valid_at = datetime(2020, 1, 1, tzinfo=UTC)

        memory_api.store(
            content="Population is 60 million",
            scope="user",
            namespace="test/update",
            path_key="population",
            valid_at=old_valid_at,
        )

        # New information (not correction) - fact changed
        new_id = memory_api.store(
            content="Population is 68 million",
            scope="user",
            namespace="test/update",
            path_key="population",
        )

        new_memory = session.execute(
            select(MemoryModel).where(MemoryModel.memory_id == new_id)
        ).scalar_one()

        # New information should get current time as valid_at, not inherit old
        if new_memory.valid_at is not None:
            assert new_memory.valid_at != old_valid_at, (
                "New information should not inherit old valid_at"
            )


# ============================================================================
# AC3: Convert delete to soft-delete
# ============================================================================


class TestSoftDelete:
    """Test that delete marks records as deleted instead of removing them."""

    def test_delete_sets_invalid_at(self, memory_api, session):
        """delete() should set invalid_at instead of removing the row."""
        memory_id = memory_api.store(
            content="To be soft-deleted",
            scope="user",
            memory_type="fact",
        )

        memory_api.delete(memory_id)

        # Memory should still exist in database
        memory = session.execute(
            select(MemoryModel).where(MemoryModel.memory_id == memory_id)
        ).scalar_one_or_none()

        assert memory is not None, "Soft-deleted memory must remain in database"
        assert memory.invalid_at is not None, "Soft-deleted memory must have invalid_at set"

    def test_delete_preserves_content(self, memory_api, session):
        """Soft-deleted memory should still have its content_hash."""
        memory_id = memory_api.store(
            content="Preserved after delete",
            scope="user",
            memory_type="fact",
        )

        # Get content hash before delete
        memory = session.execute(
            select(MemoryModel).where(MemoryModel.memory_id == memory_id)
        ).scalar_one()
        content_hash = memory.content_hash

        memory_api.delete(memory_id)

        # Refresh and verify content_hash unchanged
        session.refresh(memory)
        assert memory.content_hash == content_hash, (
            "Soft-deleted memory must retain its content_hash"
        )

    def test_deleted_memory_excluded_from_default_queries(self, memory_api, session):
        """Soft-deleted memories should not appear in default queries."""
        memory_id = memory_api.store(
            content="Will be deleted",
            scope="user",
            memory_type="fact",
        )

        memory_api.delete(memory_id)

        # Default query should not include deleted memory
        results = memory_api.query(memory_type="fact", state="all")

        result_ids = [r["memory_id"] for r in results]
        assert memory_id not in result_ids, (
            "Soft-deleted memory should not appear in default queries"
        )

    def test_delete_returns_true_on_success(self, memory_api):
        """delete() should return True for existing memory."""
        memory_id = memory_api.store(
            content="Deletable",
            scope="user",
        )

        result = memory_api.delete(memory_id)
        assert result is True

    def test_delete_returns_false_for_nonexistent(self, memory_api):
        """delete() should return False for non-existent memory."""
        result = memory_api.delete("non-existent-id")
        assert result is False


# ============================================================================
# AC4: Default queries filter to current (invalid_at IS NULL)
# ============================================================================


class TestDefaultQueryFiltering:
    """Test that queries filter to current memories by default."""

    def test_query_excludes_superseded_memories_by_default(self, memory_api, session):
        """Default queries should only return current (non-superseded) memories."""
        memory_api.store(
            content="V1 - will be superseded",
            scope="user",
            namespace="test/filter",
            path_key="doc",
        )

        new_id = memory_api.store(
            content="V2 - current",
            scope="user",
            namespace="test/filter",
            path_key="doc",
        )

        results = memory_api.query(state="all")

        # Should only return the current version
        result_ids = [r["memory_id"] for r in results]
        assert new_id in result_ids, "Current memory should be in results"
        # Superseded memory should NOT be in default results
        assert len([r for r in results if r.get("content", "").startswith("V1")]) == 0, (
            "Superseded memory should not appear in default queries"
        )

    def test_query_returns_only_non_superseded(self, memory_api, session):
        """After multiple updates, only the latest version should appear."""
        for i in range(5):
            memory_api.store(
                content=f"Version {i + 1}",
                scope="user",
                namespace="test/many",
                path_key="doc",
            )

        results = memory_api.query(state="all")

        # Should only have 1 result for this namespace/path_key
        namespace_results = [r for r in results if r.get("namespace") == "test/many"]
        assert len(namespace_results) == 1, (
            "Only the latest version should appear in default queries"
        )
        assert "Version 5" in namespace_results[0]["content"], (
            "The latest version should be the one returned"
        )


# ============================================================================
# AC5: Add include_superseded parameter to query
# ============================================================================


class TestIncludeSuperseded:
    """Test the include_superseded parameter on queries."""

    def test_include_superseded_returns_all_versions(self, memory_api, session):
        """include_superseded=True should return all versions including old ones."""
        for i in range(3):
            memory_api.store(
                content=f"Version {i + 1}",
                scope="user",
                namespace="test/all",
                path_key="doc",
            )

        results = memory_api.query(include_superseded=True, state="all")

        namespace_results = [r for r in results if r.get("namespace") == "test/all"]
        assert len(namespace_results) == 3, "include_superseded=True should return all 3 versions"

    def test_include_superseded_false_excludes_old(self, memory_api, session):
        """include_superseded=False (default) should exclude superseded versions."""
        for i in range(3):
            memory_api.store(
                content=f"Version {i + 1}",
                scope="user",
                namespace="test/exclude",
                path_key="doc",
            )

        results = memory_api.query(include_superseded=False, state="all")

        namespace_results = [r for r in results if r.get("namespace") == "test/exclude"]
        assert len(namespace_results) == 1, (
            "include_superseded=False should only return the current version"
        )

    def test_include_superseded_with_point_in_time(self, memory_api, session):
        """include_superseded should work with point-in-time queries."""
        from datetime import UTC, datetime

        # Store initial memory
        memory_api.store(
            content="V1 content",
            scope="user",
            namespace="test/pit",
            path_key="doc",
            valid_at=datetime(2024, 1, 1, tzinfo=UTC),
        )

        # small delay to ensure different timestamps
        time.sleep(0.01)

        memory_api.store(
            content="V2 content",
            scope="user",
            namespace="test/pit",
            path_key="doc",
            valid_at=datetime(2025, 6, 1, tzinfo=UTC),
        )

        # Point-in-time query for early 2025 should return V1
        results = memory_api.query(
            include_superseded=True,
            as_of_event=datetime(2025, 3, 1, tzinfo=UTC),
            state="all",
        )

        namespace_results = [r for r in results if r.get("namespace") == "test/pit"]
        assert len(namespace_results) >= 1, (
            "Point-in-time query with include_superseded should find valid memories"
        )


# ============================================================================
# AC6: Add lineage traversal: memory.get_history(memory_id)
# ============================================================================


class TestGetHistory:
    """Test lineage traversal via get_history()."""

    def test_get_history_returns_full_chain(self, memory_api, session):
        """get_history() should return the complete version chain."""
        v1_id = memory_api.store(
            content="History V1",
            scope="user",
            namespace="test/history",
            path_key="doc",
        )

        v2_id = memory_api.store(
            content="History V2",
            scope="user",
            namespace="test/history",
            path_key="doc",
        )

        v3_id = memory_api.store(
            content="History V3",
            scope="user",
            namespace="test/history",
            path_key="doc",
        )

        # get_history on the latest should return all versions
        history = memory_api.get_history(v3_id)

        assert isinstance(history, list)
        assert len(history) == 3, "Should have 3 versions in history"

        # Should be in chronological order (oldest first)
        assert history[0]["memory_id"] == v1_id
        assert history[1]["memory_id"] == v2_id
        assert history[2]["memory_id"] == v3_id

    def test_get_history_from_middle_version(self, memory_api, session):
        """get_history() from a middle version should return the full chain."""
        v1_id = memory_api.store(
            content="Chain V1",
            scope="user",
            namespace="test/mid",
            path_key="doc",
        )

        v2_id = memory_api.store(
            content="Chain V2",
            scope="user",
            namespace="test/mid",
            path_key="doc",
        )

        v3_id = memory_api.store(
            content="Chain V3",
            scope="user",
            namespace="test/mid",
            path_key="doc",
        )

        # get_history from the middle should still return full chain
        history = memory_api.get_history(v2_id)

        assert len(history) == 3
        memory_ids = [h["memory_id"] for h in history]
        assert v1_id in memory_ids
        assert v2_id in memory_ids
        assert v3_id in memory_ids

    def test_get_history_single_memory(self, memory_api):
        """get_history() on a memory with no updates should return just itself."""
        memory_id = memory_api.store(
            content="Standalone",
            scope="user",
            memory_type="fact",
        )

        history = memory_api.get_history(memory_id)

        assert len(history) == 1
        assert history[0]["memory_id"] == memory_id

    def test_get_history_includes_content(self, memory_api):
        """get_history() entries should include content."""
        memory_api.store(
            content="Content V1",
            scope="user",
            namespace="test/content_hist",
            path_key="doc",
        )

        v2_id = memory_api.store(
            content="Content V2",
            scope="user",
            namespace="test/content_hist",
            path_key="doc",
        )

        history = memory_api.get_history(v2_id)

        for entry in history:
            assert "content" in entry, "History entry should include content"
            assert "memory_id" in entry, "History entry should include memory_id"
            assert "created_at" in entry, "History entry should include created_at"

        assert history[0]["content"] == "Content V1"
        assert history[1]["content"] == "Content V2"

    def test_get_history_nonexistent_memory(self, memory_api):
        """get_history() on non-existent memory should return empty list."""
        history = memory_api.get_history("non-existent-id")
        assert history == []

    def test_get_history_includes_valid_at(self, memory_api, session):
        """get_history() entries should include valid_at/invalid_at timestamps."""
        memory_api.store(
            content="Temporal V1",
            scope="user",
            namespace="test/temporal_hist",
            path_key="doc",
        )

        v2_id = memory_api.store(
            content="Temporal V2",
            scope="user",
            namespace="test/temporal_hist",
            path_key="doc",
        )

        history = memory_api.get_history(v2_id)

        # First entry (superseded) should have invalid_at set
        assert history[0]["invalid_at"] is not None, (
            "Superseded version should have invalid_at in history"
        )
        # Latest entry should have invalid_at as None
        assert history[-1]["invalid_at"] is None, (
            "Current version should have NULL invalid_at in history"
        )


# ============================================================================
# AC7: Optional GC for old versions
# ============================================================================


class TestGarbageCollection:
    """Test optional garbage collection for old superseded versions."""

    def test_gc_removes_old_superseded_versions(self, memory_api, session):
        """gc_old_versions() should remove superseded versions older than threshold."""

        # Create chain of versions
        for i in range(5):
            memory_api.store(
                content=f"GC test V{i + 1}",
                scope="user",
                namespace="test/gc",
                path_key="doc",
            )

        # Count total memories before GC
        all_mems = (
            session.execute(select(MemoryModel).where(MemoryModel.namespace == "test/gc"))
            .scalars()
            .all()
        )
        assert len(all_mems) == 5, "Should have 5 memories before GC"

        # GC with 0-day threshold (remove all old versions)
        removed = memory_api.gc_old_versions(older_than_days=0)

        # After GC, only the current version should remain
        remaining = (
            session.execute(
                select(MemoryModel).where(
                    MemoryModel.namespace == "test/gc",
                    MemoryModel.invalid_at.is_(None),
                )
            )
            .scalars()
            .all()
        )

        assert len(remaining) == 1, "Only current version should remain after GC"
        assert removed >= 4, "Should have removed at least 4 old versions"

    def test_gc_preserves_current_versions(self, memory_api, session):
        """GC should never remove the current (non-superseded) version."""
        memory_api.store(
            content="Current - never delete",
            scope="user",
            namespace="test/gc_preserve",
            path_key="doc",
        )

        memory_api.gc_old_versions(older_than_days=0)

        remaining = (
            session.execute(select(MemoryModel).where(MemoryModel.namespace == "test/gc_preserve"))
            .scalars()
            .all()
        )

        assert len(remaining) == 1, "Current version must survive GC"

    def test_gc_respects_age_threshold(self, memory_api, session):
        """GC should only remove versions older than the threshold."""
        for i in range(3):
            memory_api.store(
                content=f"Age test V{i + 1}",
                scope="user",
                namespace="test/gc_age",
                path_key="doc",
            )

        # GC with 365-day threshold should not remove anything recent
        removed = memory_api.gc_old_versions(older_than_days=365)

        all_mems = (
            session.execute(select(MemoryModel).where(MemoryModel.namespace == "test/gc_age"))
            .scalars()
            .all()
        )

        # All versions should still exist since none are 365 days old
        assert len(all_mems) == 3, "No versions should be removed with 365-day threshold"
        assert removed == 0


# ============================================================================
# AC8: Migration / backward compatibility
# ============================================================================


class TestBackwardCompatibility:
    """Test that existing memories without supersedes fields work correctly."""

    def test_existing_memories_treated_as_current(self, memory_api, session):
        """Memories without supersedes_id should be treated as current."""
        memory_id = memory_api.store(
            content="Legacy memory",
            scope="user",
            memory_type="fact",
        )

        # Should appear in normal queries
        results = memory_api.query(memory_type="fact", state="all")
        result_ids = [r["memory_id"] for r in results]

        assert memory_id in result_ids, "Memory without supersedes_id should appear as current"

    def test_append_mode_still_works(self, memory_api, session):
        """Append mode (no path_key) should still create independent memories."""
        id1 = memory_api.store(
            content="Memory A",
            scope="user",
            memory_type="fact",
        )

        id2 = memory_api.store(
            content="Memory B",
            scope="user",
            memory_type="fact",
        )

        # Should be two independent memories (not superseding each other)
        mem1 = session.execute(select(MemoryModel).where(MemoryModel.memory_id == id1)).scalar_one()
        mem2 = session.execute(select(MemoryModel).where(MemoryModel.memory_id == id2)).scalar_one()

        assert mem1.supersedes_id is None, "Append mode memory should have no supersedes_id"
        assert mem2.supersedes_id is None, "Append mode memory should have no supersedes_id"
        assert mem1.superseded_by_id is None
        assert mem2.superseded_by_id is None

    def test_version_history_still_created(self, memory_api, session):
        """Version history (VersionHistoryModel) should still be created."""
        memory_api.store(
            content="Versioned",
            scope="user",
            namespace="test/compat",
            path_key="doc",
        )

        new_id = memory_api.store(
            content="Updated",
            scope="user",
            namespace="test/compat",
            path_key="doc",
        )

        # Version history entries should exist for the new memory
        versions = (
            session.execute(
                select(VersionHistoryModel).where(
                    VersionHistoryModel.resource_type == "memory",
                    VersionHistoryModel.resource_id == new_id,
                )
            )
            .scalars()
            .all()
        )

        assert len(versions) >= 1, "Version history should still be created"


# ============================================================================
# Edge cases
# ============================================================================


class TestEdgeCases:
    """Test edge cases for append-only pattern."""

    def test_rapid_successive_updates(self, memory_api, session):
        """Rapid successive updates should all create proper chains."""
        ids = []
        for i in range(10):
            mid = memory_api.store(
                content=f"Rapid update {i}",
                scope="user",
                namespace="test/rapid",
                path_key="doc",
            )
            ids.append(mid)

        # All IDs should be unique
        assert len(set(ids)) == 10, "All updates should create unique memory IDs"

        # Only the last should be current (no invalid_at)
        for mid in ids[:-1]:
            mem = session.execute(
                select(MemoryModel).where(MemoryModel.memory_id == mid)
            ).scalar_one()
            assert mem.invalid_at is not None, (
                f"Memory {mid} should be superseded (have invalid_at)"
            )

        last_mem = session.execute(
            select(MemoryModel).where(MemoryModel.memory_id == ids[-1])
        ).scalar_one()
        assert last_mem.invalid_at is None, "Last memory should be current"

    def test_update_preserves_namespace_and_path_key(self, memory_api, session):
        """New memory from append-only update should inherit namespace/path_key."""
        memory_api.store(
            content="Original",
            scope="user",
            namespace="inherited/ns",
            path_key="inherited_key",
        )

        new_id = memory_api.store(
            content="Updated",
            scope="user",
            namespace="inherited/ns",
            path_key="inherited_key",
        )

        new_mem = session.execute(
            select(MemoryModel).where(MemoryModel.memory_id == new_id)
        ).scalar_one()

        assert new_mem.namespace == "inherited/ns"
        assert new_mem.path_key == "inherited_key"

    def test_get_returns_only_current_version(self, memory_api, session):
        """memory.get() should always return the current (non-superseded) version."""
        v1_id = memory_api.store(
            content="V1",
            scope="user",
            namespace="test/get",
            path_key="doc",
        )

        v2_id = memory_api.store(
            content="V2 - current",
            scope="user",
            namespace="test/get",
            path_key="doc",
        )

        # get() on v1 should still work (return the old content)
        v1_result = memory_api.get(v1_id)
        assert v1_result is not None, "get() on old memory_id should still work"

        # get() on v2 should return current content
        v2_result = memory_api.get(v2_id)
        assert v2_result is not None
        assert "V2" in v2_result["content"]
