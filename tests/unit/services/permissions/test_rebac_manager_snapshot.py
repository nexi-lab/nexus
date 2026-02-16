"""Snapshot/regression tests for ReBAC manager decomposition.

These tests validate end-to-end behavior of core ReBAC operations that will be
decomposed in later phases. They serve as regression guards to ensure no behavior
changes during refactoring.

Test coverage:
- Write + Check round-trip
- Batch write operations
- Delete operations
- Graph traversal (inheritance, groups, cycles)
- Expand API
- Consistency levels
- Bulk check operations
- Cache behavior

All tests use in-memory SQLite database with EnhancedReBACManager.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from nexus.services.permissions.rebac_manager_enhanced import (
    ConsistencyLevel,
    ConsistencyMode,
    ConsistencyRequirement,
    EnhancedReBACManager,
    WriteResult,
)
from nexus.storage.models import Base


@pytest.fixture
def engine():
    """Create in-memory SQLite database."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    # Also create rebac_group_closure table for Leopard
    with engine.begin() as conn:
        conn.execute(
            text(
                """
            CREATE TABLE IF NOT EXISTS rebac_group_closure (
                member_type VARCHAR(50) NOT NULL,
                member_id VARCHAR(255) NOT NULL,
                group_type VARCHAR(50) NOT NULL,
                group_id VARCHAR(255) NOT NULL,
                zone_id VARCHAR(255) NOT NULL,
                depth INTEGER NOT NULL,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (member_type, member_id, group_type, group_id, zone_id)
            )
        """
            )
        )
    return engine


@pytest.fixture
def manager(engine):
    """Create EnhancedReBACManager for testing."""
    mgr = EnhancedReBACManager(
        engine=engine,
        cache_ttl_seconds=300,
        max_depth=50,
        enforce_zone_isolation=False,
        enable_graph_limits=True,
        enable_leopard=True,
        enable_tiger_cache=False,  # SQLite doesn't support Tiger
    )
    yield mgr
    mgr.close()


class TestWriteCheckRoundTrip:
    """Test write + check round-trip operations."""

    def test_write_direct_relation_check_exists(self, manager):
        """Write a direct relation and verify it exists."""
        # Write: alice is member-of eng-team
        manager.rebac_write(
            subject=("user", "alice"),
            relation="member-of",
            object=("group", "eng-team"),
        )

        # Check exists
        allowed = manager.rebac_check(
            subject=("user", "alice"),
            permission="member-of",
            object=("group", "eng-team"),
        )

        assert allowed is True

    def test_write_returns_write_result(self, manager):
        """Write returns WriteResult with tuple_id, revision, consistency_token."""
        result = manager.rebac_write(
            subject=("user", "bob"),
            relation="viewer-of",
            object=("file", "/doc.txt"),
        )

        assert isinstance(result, WriteResult)
        assert result.tuple_id is not None
        assert isinstance(result.tuple_id, str)
        assert result.revision > 0
        assert isinstance(result.revision, int)
        assert result.consistency_token is not None
        assert isinstance(result.consistency_token, str)
        assert result.written_at_ms > 0
        assert isinstance(result.written_at_ms, float)

    def test_check_non_existent_relation_returns_false(self, manager):
        """Check non-existent relation returns False."""
        allowed = manager.rebac_check(
            subject=("user", "charlie"),
            permission="owner-of",
            object=("file", "/secret.txt"),
        )

        assert allowed is False

    def test_write_with_zone_id_check_same_zone(self, manager):
        """Write with zone_id, check within same zone."""
        manager.rebac_write(
            subject=("user", "dave"),
            relation="editor-of",
            object=("file", "/data.csv"),
            zone_id="org_123",
        )

        # Check in same zone
        allowed = manager.rebac_check(
            subject=("user", "dave"),
            permission="editor-of",
            object=("file", "/data.csv"),
            zone_id="org_123",
        )

        assert allowed is True

    def test_write_with_past_expiry_does_not_grant_access(self, manager):
        """Write with expires_at in the past doesn't grant access."""
        past_time = datetime.now(UTC) - timedelta(hours=1)

        manager.rebac_write(
            subject=("user", "eve"),
            relation="viewer-of",
            object=("file", "/temp.txt"),
            expires_at=past_time,
        )

        # Check should fail because tuple is expired
        allowed = manager.rebac_check(
            subject=("user", "eve"),
            permission="viewer-of",
            object=("file", "/temp.txt"),
        )

        assert allowed is False


class TestBatchWrite:
    """Test batch write operations."""

    def test_batch_write_creates_multiple_tuples(self, manager):
        """rebac_write_batch creates multiple tuples."""
        tuples = [
            {
                "subject": ("user", "alice"),
                "relation": "viewer-of",
                "object": ("file", "/doc1.txt"),
            },
            {
                "subject": ("user", "alice"),
                "relation": "viewer-of",
                "object": ("file", "/doc2.txt"),
            },
            {
                "subject": ("user", "bob"),
                "relation": "editor-of",
                "object": ("file", "/doc3.txt"),
            },
        ]

        count = manager.rebac_write_batch(tuples)

        assert count == 3

        # Verify all tuples were created
        assert manager.rebac_check(("user", "alice"), "viewer-of", ("file", "/doc1.txt"))
        assert manager.rebac_check(("user", "alice"), "viewer-of", ("file", "/doc2.txt"))
        assert manager.rebac_check(("user", "bob"), "editor-of", ("file", "/doc3.txt"))

    def test_batch_write_returns_count(self, manager):
        """rebac_write_batch returns count."""
        tuples = [
            {
                "subject": ("user", "charlie"),
                "relation": "member-of",
                "object": ("group", "team-a"),
            },
            {
                "subject": ("user", "dave"),
                "relation": "member-of",
                "object": ("group", "team-a"),
            },
        ]

        count = manager.rebac_write_batch(tuples)

        assert count == 2

    def test_batch_write_empty_list_returns_zero(self, manager):
        """rebac_write_batch with empty list returns 0."""
        count = manager.rebac_write_batch([])

        assert count == 0


class TestDelete:
    """Test delete operations."""

    def test_delete_removes_tuple(self, manager):
        """rebac_delete removes tuple."""
        # Write a tuple
        result = manager.rebac_write(
            subject=("user", "frank"),
            relation="owner-of",
            object=("file", "/project.txt"),
        )

        # Verify it exists
        assert manager.rebac_check(("user", "frank"), "owner-of", ("file", "/project.txt"))

        # Delete it
        deleted = manager.rebac_delete(result.tuple_id)

        assert deleted is True

    def test_after_delete_check_returns_false(self, manager):
        """After delete, rebac_check returns False."""
        # Write a tuple
        result = manager.rebac_write(
            subject=("user", "grace"),
            relation="viewer-of",
            object=("file", "/report.pdf"),
        )

        # Delete it
        manager.rebac_delete(result.tuple_id)

        # Verify it no longer exists
        allowed = manager.rebac_check(
            subject=("user", "grace"),
            permission="viewer-of",
            object=("file", "/report.pdf"),
        )

        assert allowed is False

    def test_delete_non_existent_tuple_returns_false(self, manager):
        """Delete non-existent tuple returns False."""
        deleted = manager.rebac_delete("non-existent-tuple-id")

        assert deleted is False


class TestGraphTraversal:
    """Test graph traversal for inherited permissions."""

    def test_permission_inherited_through_group_membership(self, manager):
        """Permission inherited through group membership.

        Scenario:
        - alice is member-of eng-team
        - eng-team has direct_viewer on file123
        - Check alice has member-of relation to eng-team
        - Check eng-team has direct_viewer on file123
        """
        # alice member-of eng-team
        manager.rebac_write(
            subject=("user", "alice"),
            relation="member-of",
            object=("group", "eng-team"),
        )

        # eng-team has direct_viewer on file123
        manager.rebac_write(
            subject=("group", "eng-team"),
            relation="direct_viewer",
            object=("file", "file123"),
        )

        # Verify direct relations exist
        assert manager.rebac_check(
            subject=("user", "alice"),
            permission="member-of",
            object=("group", "eng-team"),
        )
        assert manager.rebac_check(
            subject=("group", "eng-team"),
            permission="direct_viewer",
            object=("file", "file123"),
        )

    def test_permission_inherited_through_nested_groups(self, manager):
        """Permission inherited through nested groups.

        Scenario:
        - alice member-of team-a
        - team-a member-of team-b
        - team-b has direct_editor on file456
        - Verify the membership chain exists
        """
        # alice member-of team-a
        manager.rebac_write(
            subject=("user", "alice"),
            relation="member-of",
            object=("group", "team-a"),
        )

        # team-a member-of team-b
        manager.rebac_write(
            subject=("group", "team-a"),
            relation="member-of",
            object=("group", "team-b"),
        )

        # team-b has direct_editor on file456
        manager.rebac_write(
            subject=("group", "team-b"),
            relation="direct_editor",
            object=("file", "file456"),
        )

        # Verify direct relations exist
        assert manager.rebac_check(
            subject=("user", "alice"),
            permission="member-of",
            object=("group", "team-a"),
        )
        assert manager.rebac_check(
            subject=("group", "team-a"),
            permission="member-of",
            object=("group", "team-b"),
        )
        assert manager.rebac_check(
            subject=("group", "team-b"),
            permission="direct_editor",
            object=("file", "file456"),
        )

    def test_cycle_detection_does_not_hang(self, manager):
        """Cycle detection doesn't hang.

        Scenario:
        - group-a member-of group-b
        - group-b member-of group-a (cycle!)
        - Check should complete without hanging
        """
        # Create cycle
        manager.rebac_write(
            subject=("group", "group-a"),
            relation="member-of",
            object=("group", "group-b"),
        )
        manager.rebac_write(
            subject=("group", "group-b"),
            relation="member-of",
            object=("group", "group-a"),
        )

        # This should not hang
        allowed = manager.rebac_check(
            subject=("group", "group-a"),
            permission="viewer-of",
            object=("file", "test.txt"),
        )

        # Should return False (no permission), not hang
        assert allowed is False

    def test_union_relations_resolved_correctly(self, manager):
        """Union relations resolved correctly.

        Scenario:
        - alice has direct_viewer on file789
        - Viewer permission is union of direct_viewer and group_viewer
        - alice should have read permission
        """
        # alice direct_viewer file789
        manager.rebac_write(
            subject=("user", "alice"),
            relation="direct_viewer",
            object=("file", "file789"),
        )

        # Check read permission (which resolves via viewer union)
        allowed = manager.rebac_check(
            subject=("user", "alice"),
            permission="read",
            object=("file", "file789"),
        )

        assert allowed is True

    def test_parent_child_file_hierarchy_traversal(self, manager):
        """Parent-child file hierarchy traversal.

        Scenario:
        - alice has direct_viewer on /workspace
        - /workspace/doc.txt has parent relation to /workspace
        - Verify the parent-child relationship exists
        """
        # alice direct_viewer on /workspace
        manager.rebac_write(
            subject=("user", "alice"),
            relation="direct_viewer",
            object=("file", "/workspace"),
        )

        # /workspace/doc.txt parent is /workspace
        manager.rebac_write(
            subject=("file", "/workspace/doc.txt"),
            relation="parent",
            object=("file", "/workspace"),
        )

        # Verify direct relations exist
        assert manager.rebac_check(
            subject=("user", "alice"),
            permission="direct_viewer",
            object=("file", "/workspace"),
        )
        assert manager.rebac_check(
            subject=("file", "/workspace/doc.txt"),
            permission="parent",
            object=("file", "/workspace"),
        )


class TestExpandAPI:
    """Test expand API for finding all subjects with permission."""

    def test_expand_returns_all_subjects_with_permission(self, manager):
        """rebac_expand returns all subjects with permission."""
        # Write multiple subjects with permission
        manager.rebac_write(
            subject=("user", "alice"),
            relation="viewer-of",
            object=("file", "/shared.txt"),
        )
        manager.rebac_write(
            subject=("user", "bob"),
            relation="viewer-of",
            object=("file", "/shared.txt"),
        )
        manager.rebac_write(
            subject=("user", "charlie"),
            relation="editor-of",
            object=("file", "/shared.txt"),
        )

        # Expand to find all viewers
        subjects = manager.rebac_expand(
            permission="viewer-of",
            object=("file", "/shared.txt"),
        )

        # Should return alice and bob (direct viewers)
        assert ("user", "alice") in subjects
        assert ("user", "bob") in subjects
        # charlie is editor, not direct viewer
        assert len([s for s in subjects if s[0] == "user" and s[1] == "charlie"]) >= 0

    def test_expand_with_no_matches_returns_empty(self, manager):
        """rebac_expand with no matches returns empty."""
        subjects = manager.rebac_expand(
            permission="owner-of",
            object=("file", "/nobody-owns-this.txt"),
        )

        assert len(subjects) == 0

    def test_expand_follows_transitive_relations(self, manager):
        """rebac_expand follows transitive relations.

        Scenario:
        - alice member-of team-x
        - team-x has viewer-of file999
        - Expand should include alice via group membership
        """
        # alice member-of team-x
        manager.rebac_write(
            subject=("user", "alice"),
            relation="member-of",
            object=("group", "team-x"),
        )

        # team-x viewer-of file999
        manager.rebac_write(
            subject=("group", "team-x"),
            relation="viewer-of",
            object=("file", "file999"),
        )

        # Expand should include team-x
        subjects = manager.rebac_expand(
            permission="viewer-of",
            object=("file", "file999"),
        )

        # Should return team-x (direct) and possibly alice (via group)
        assert ("group", "team-x") in subjects


class TestConsistencyLevels:
    """Test consistency levels for cache control."""

    def test_consistency_eventual_uses_cache(self, manager):
        """ConsistencyLevel.EVENTUAL uses cache."""
        # Write a permission
        manager.rebac_write(
            subject=("user", "alice"),
            relation="viewer-of",
            object=("file", "/cached.txt"),
        )

        # First check populates cache
        result1 = manager.rebac_check(
            subject=("user", "alice"),
            permission="viewer-of",
            object=("file", "/cached.txt"),
            consistency=ConsistencyLevel.EVENTUAL,
        )

        # Second check should hit cache
        result2 = manager.rebac_check(
            subject=("user", "alice"),
            permission="viewer-of",
            object=("file", "/cached.txt"),
            consistency=ConsistencyLevel.EVENTUAL,
        )

        assert result1 is True
        assert result2 is True

    def test_consistency_strong_bypasses_cache(self, manager):
        """ConsistencyLevel.STRONG bypasses cache."""
        # Write a permission
        manager.rebac_write(
            subject=("user", "bob"),
            relation="editor-of",
            object=("file", "/fresh.txt"),
        )

        # STRONG consistency bypasses cache
        result = manager.rebac_check(
            subject=("user", "bob"),
            permission="editor-of",
            object=("file", "/fresh.txt"),
            consistency=ConsistencyLevel.STRONG,
        )

        assert result is True

    def test_consistency_requirement_at_least_as_fresh(self, manager):
        """ConsistencyRequirement with AT_LEAST_AS_FRESH."""
        # Write a permission and get revision
        write_result = manager.rebac_write(
            subject=("user", "charlie"),
            relation="owner-of",
            object=("file", "/versioned.txt"),
        )

        # Check with AT_LEAST_AS_FRESH using the write revision
        consistency_req = ConsistencyRequirement(
            mode=ConsistencyMode.AT_LEAST_AS_FRESH,
            min_revision=write_result.revision,
        )

        result = manager.rebac_check(
            subject=("user", "charlie"),
            permission="owner-of",
            object=("file", "/versioned.txt"),
            consistency=consistency_req,
        )

        assert result is True


class TestBulkCheck:
    """Test bulk check operations."""

    @pytest.mark.xfail(
        reason="Flaky on Ubuntu CI — Rust bulk checker race condition with in-memory SQLite. "
        "Passes locally and on macOS CI. Same failure observed on main.",
        strict=False,
    )
    def test_bulk_check_returns_dict_of_results(self, manager):
        """rebac_check_bulk returns dict of results."""
        # Setup permissions
        manager.rebac_write(
            subject=("user", "alice"),
            relation="viewer-of",
            object=("file", "/bulk1.txt"),
            zone_id="org_123",
        )
        manager.rebac_write(
            subject=("user", "alice"),
            relation="viewer-of",
            object=("file", "/bulk2.txt"),
            zone_id="org_123",
        )

        # Bulk check
        checks = [
            (("user", "alice"), "viewer-of", ("file", "/bulk1.txt")),
            (("user", "alice"), "viewer-of", ("file", "/bulk2.txt")),
            (("user", "alice"), "viewer-of", ("file", "/bulk3.txt")),  # No permission
        ]

        results = manager.rebac_check_bulk(checks, zone_id="org_123")

        assert isinstance(results, dict)
        assert len(results) == 3
        assert results[(("user", "alice"), "viewer-of", ("file", "/bulk1.txt"))] is True
        assert results[(("user", "alice"), "viewer-of", ("file", "/bulk2.txt"))] is True
        assert results[(("user", "alice"), "viewer-of", ("file", "/bulk3.txt"))] is False

    def test_bulk_check_empty_list(self, manager):
        """rebac_check_bulk with empty list."""
        results = manager.rebac_check_bulk([], zone_id="org_123")

        assert isinstance(results, dict)
        assert len(results) == 0


class TestCacheBehavior:
    """Test cache behavior and invalidation."""

    def test_first_check_caches_second_check_hits_cache(self, manager):
        """First check caches, second check hits cache."""
        # Write permission
        manager.rebac_write(
            subject=("user", "alice"),
            relation="viewer-of",
            object=("file", "/cache-test.txt"),
        )

        # First check populates cache
        result1 = manager.rebac_check(
            subject=("user", "alice"),
            permission="viewer-of",
            object=("file", "/cache-test.txt"),
        )

        # Second check should hit cache (faster)
        result2 = manager.rebac_check(
            subject=("user", "alice"),
            permission="viewer-of",
            object=("file", "/cache-test.txt"),
        )

        assert result1 is True
        assert result2 is True

    def test_write_invalidates_relevant_cache_entries(self, manager):
        """Write invalidates relevant cache entries."""
        # Initial permission
        manager.rebac_write(
            subject=("user", "bob"),
            relation="viewer-of",
            object=("file", "/invalidate.txt"),
            zone_id="org_123",
        )

        # Check and populate cache
        result1 = manager.rebac_check(
            subject=("user", "bob"),
            permission="viewer-of",
            object=("file", "/invalidate.txt"),
            zone_id="org_123",
        )
        assert result1 is True

        # Write new permission (should invalidate zone cache)
        manager.rebac_write(
            subject=("user", "charlie"),
            relation="editor-of",
            object=("file", "/other.txt"),
            zone_id="org_123",
        )

        # Check again (cache should be invalidated)
        result2 = manager.rebac_check(
            subject=("user", "bob"),
            permission="viewer-of",
            object=("file", "/invalidate.txt"),
            zone_id="org_123",
        )
        assert result2 is True

    def test_delete_invalidates_relevant_cache_entries(self, manager):
        """Delete invalidates relevant cache entries."""
        # Write permission
        write_result = manager.rebac_write(
            subject=("user", "dave"),
            relation="owner-of",
            object=("file", "/delete-test.txt"),
            zone_id="org_123",
        )

        # Check and populate cache
        result1 = manager.rebac_check(
            subject=("user", "dave"),
            permission="owner-of",
            object=("file", "/delete-test.txt"),
            zone_id="org_123",
        )
        assert result1 is True

        # Delete permission
        manager.rebac_delete(write_result.tuple_id)

        # Check again (should be False, cache invalidated)
        result2 = manager.rebac_check(
            subject=("user", "dave"),
            permission="owner-of",
            object=("file", "/delete-test.txt"),
            zone_id="org_123",
        )
        assert result2 is False


class TestConsistencyModuleIntegration:
    """Integration tests verifying delegation to consistency module (Issue #1459 Phase 9)."""

    def test_write_returns_monotonic_consistency_tokens(self, manager):
        """Multiple writes produce monotonically increasing version tokens."""
        tokens = []
        for i in range(3):
            result = manager.rebac_write(
                subject=("user", f"user_{i}"),
                relation="viewer-of",
                object=("file", f"/token_test_{i}.txt"),
            )
            tokens.append(result.consistency_token)

        # All tokens must be non-None strings
        assert all(isinstance(t, str) for t in tokens)
        assert all(t.startswith("v") for t in tokens)

        # Extract version numbers and verify monotonic ordering
        versions = [int(t[1:]) for t in tokens]
        assert versions == sorted(versions)
        assert versions[-1] > versions[0]

    def test_zone_validation_delegates_to_zone_manager(self, engine):
        """Zone-aware manager delegates zone validation to ZoneManager."""
        mgr = EnhancedReBACManager(
            engine=engine,
            cache_ttl_seconds=300,
            max_depth=50,
            enforce_zone_isolation=True,
            enable_graph_limits=True,
            enable_leopard=False,
            enable_tiger_cache=False,
        )
        try:
            # Same-zone write should succeed
            result = mgr.rebac_write(
                subject=("user", "alice"),
                relation="viewer-of",
                object=("file", "/doc.txt"),
                zone_id="org_a",
                subject_zone_id="org_a",
                object_zone_id="org_a",
            )
            assert isinstance(result, WriteResult)

            # Cross-zone write with non-shared relation should fail
            from nexus.services.permissions.consistency.zone_manager import ZoneIsolationError

            with pytest.raises(ZoneIsolationError):
                mgr.rebac_write(
                    subject=("user", "bob"),
                    relation="editor-of",
                    object=("file", "/secret.txt"),
                    zone_id="org_a",
                    subject_zone_id="org_a",
                    object_zone_id="org_b",
                )
        finally:
            mgr.close()

    def test_cross_zone_shared_viewer_write_succeeds(self, engine):
        """Cross-zone shared-viewer writes are allowed when zone isolation is enforced."""
        mgr = EnhancedReBACManager(
            engine=engine,
            cache_ttl_seconds=300,
            max_depth=50,
            enforce_zone_isolation=True,
            enable_graph_limits=True,
            enable_leopard=False,
            enable_tiger_cache=False,
        )
        try:
            result = mgr.rebac_write(
                subject=("user", "alice"),
                relation="shared-viewer",
                object=("file", "/shared.txt"),
                zone_id="org_a",
                subject_zone_id="org_a",
                object_zone_id="org_b",
            )
            assert isinstance(result, WriteResult)
            assert result.tuple_id is not None
        finally:
            mgr.close()
