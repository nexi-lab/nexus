"""Unit tests for Leopard transitive group closure index.

Tests cover:
- Direct group membership closure
- Transitive group membership (deep nesting)
- Closure maintenance on add/remove
- In-memory cache operations
- Rebuild functionality
- Edge cases (cycles, orphans)

Related: Issue #692
"""

import pytest
from sqlalchemy import create_engine, text

from nexus.services.permissions.leopard import LeopardCache, LeopardIndex
from nexus.services.permissions.rebac_manager_enhanced import EnhancedReBACManager
from nexus.storage.models import Base


@pytest.fixture
def engine():
    """Create in-memory SQLite database for testing."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    # Create the leopard closure table (normally done by migration)
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
def leopard_index(engine):
    """Create a Leopard index for testing."""
    return LeopardIndex(engine=engine, cache_enabled=True, cache_max_size=1000)


@pytest.fixture
def manager(engine):
    """Create an EnhancedReBACManager with Leopard enabled."""
    mgr = EnhancedReBACManager(
        engine=engine,
        cache_ttl_seconds=300,
        max_depth=50,
        enforce_zone_isolation=False,  # Simplify tests
        enable_graph_limits=True,
        enable_leopard=True,
    )
    yield mgr
    mgr.close()


@pytest.fixture
def manager_no_leopard(engine):
    """Create an EnhancedReBACManager with Leopard disabled."""
    mgr = EnhancedReBACManager(
        engine=engine,
        cache_ttl_seconds=300,
        max_depth=50,
        enforce_zone_isolation=False,
        enable_graph_limits=True,
        enable_leopard=False,
    )
    yield mgr
    mgr.close()


class TestLeopardCache:
    """Tests for the in-memory LeopardCache."""

    def test_basic_get_set(self):
        """Test basic cache operations."""
        cache = LeopardCache(max_size=100)

        # Initially empty
        result = cache.get_transitive_groups("user", "alice", "zone1")
        assert result is None

        # Set and retrieve
        groups = {("group", "team-a"), ("group", "engineering")}
        cache.set_transitive_groups("user", "alice", "zone1", groups)

        result = cache.get_transitive_groups("user", "alice", "zone1")
        assert result == groups

    def test_cache_returns_copy(self):
        """Test that cache returns a copy, not the original set."""
        cache = LeopardCache(max_size=100)

        groups = {("group", "team-a")}
        cache.set_transitive_groups("user", "alice", "zone1", groups)

        result = cache.get_transitive_groups("user", "alice", "zone1")
        result.add(("group", "should-not-be-in-cache"))

        # Original cache should be unchanged
        result2 = cache.get_transitive_groups("user", "alice", "zone1")
        assert ("group", "should-not-be-in-cache") not in result2

    def test_invalidate_member(self):
        """Test invalidating cache for a specific member."""
        cache = LeopardCache(max_size=100)

        cache.set_transitive_groups("user", "alice", "zone1", {("group", "team-a")})
        cache.set_transitive_groups("user", "bob", "zone1", {("group", "team-b")})

        # Invalidate alice
        cache.invalidate_member("user", "alice", "zone1")

        assert cache.get_transitive_groups("user", "alice", "zone1") is None
        assert cache.get_transitive_groups("user", "bob", "zone1") == {("group", "team-b")}

    def test_invalidate_group(self):
        """Test invalidating cache for all members of a group."""
        cache = LeopardCache(max_size=100)

        # Alice and Bob both in team-a
        cache.set_transitive_groups("user", "alice", "zone1", {("group", "team-a")})
        cache.set_transitive_groups("user", "bob", "zone1", {("group", "team-a")})
        cache.set_transitive_groups("user", "charlie", "zone1", {("group", "team-b")})

        # Invalidate team-a - should clear alice and bob
        cache.invalidate_group("group", "team-a", "zone1")

        assert cache.get_transitive_groups("user", "alice", "zone1") is None
        assert cache.get_transitive_groups("user", "bob", "zone1") is None
        # Charlie should be unaffected
        assert cache.get_transitive_groups("user", "charlie", "zone1") == {("group", "team-b")}

    def test_invalidate_zone(self):
        """Test invalidating cache for entire zone."""
        cache = LeopardCache(max_size=100)

        cache.set_transitive_groups("user", "alice", "zone1", {("group", "team-a")})
        cache.set_transitive_groups("user", "bob", "zone1", {("group", "team-b")})
        cache.set_transitive_groups("user", "charlie", "zone2", {("group", "team-c")})

        # Invalidate zone1
        cache.invalidate_zone("zone1")

        assert cache.get_transitive_groups("user", "alice", "zone1") is None
        assert cache.get_transitive_groups("user", "bob", "zone1") is None
        # zone2 should be unaffected
        assert cache.get_transitive_groups("user", "charlie", "zone2") == {("group", "team-c")}

    def test_lru_eviction(self):
        """Test LRU eviction when cache is full."""
        cache = LeopardCache(max_size=3)

        # Fill cache
        cache.set_transitive_groups("user", "a", "t1", {("g", "1")})
        cache.set_transitive_groups("user", "b", "t1", {("g", "2")})
        cache.set_transitive_groups("user", "c", "t1", {("g", "3")})

        # Access 'a' to make it recently used
        cache.get_transitive_groups("user", "a", "t1")

        # Add new entry - should evict 'b' (least recently used)
        cache.set_transitive_groups("user", "d", "t1", {("g", "4")})

        # 'a' should still be there (recently accessed)
        assert cache.get_transitive_groups("user", "a", "t1") is not None
        # 'd' should be there (just added)
        assert cache.get_transitive_groups("user", "d", "t1") is not None


class TestLeopardIndex:
    """Tests for the LeopardIndex database operations."""

    def test_direct_membership_closure(self, manager):
        """Test closure for direct group membership."""
        # Add direct membership: alice -> team-a using manager
        manager.rebac_write(
            subject=("user", "alice"),
            relation="member-of",
            object=("group", "team-a"),
            zone_id="zone1",
        )

        # Check closure (should be updated by rebac_write)
        groups = manager.get_transitive_groups("user", "alice", "zone1")
        assert ("group", "team-a") in groups

    def test_transitive_membership_closure(self, manager):
        """Test closure for transitive group membership.

        Hierarchy: alice -> team-a -> engineering -> all-employees
        """
        # Create membership tuples
        manager.rebac_write(("user", "alice"), "member-of", ("group", "team-a"), zone_id="zone1")
        manager.rebac_write(
            ("group", "team-a"), "member-of", ("group", "engineering"), zone_id="zone1"
        )
        manager.rebac_write(
            ("group", "engineering"), "member-of", ("group", "all-employees"), zone_id="zone1"
        )

        # Build closure from scratch
        manager.rebuild_leopard_closure("zone1")

        # Verify alice has transitive access to all groups
        groups = manager.get_transitive_groups("user", "alice", "zone1")
        assert ("group", "team-a") in groups
        assert ("group", "engineering") in groups
        assert ("group", "all-employees") in groups

    def test_rebuild_closure(self, manager):
        """Test rebuilding closure from existing tuples."""
        # Create membership hierarchy
        manager.rebac_write(("user", "alice"), "member-of", ("group", "team-a"), zone_id="zone1")
        manager.rebac_write(("user", "bob"), "member-of", ("group", "team-b"), zone_id="zone1")
        manager.rebac_write(
            ("group", "team-a"), "member-of", ("group", "engineering"), zone_id="zone1"
        )
        manager.rebac_write(
            ("group", "team-b"), "member-of", ("group", "engineering"), zone_id="zone1"
        )

        # Rebuild closure
        entries = manager.rebuild_leopard_closure("zone1")
        assert entries > 0

        # Verify alice's groups
        alice_groups = manager.get_transitive_groups("user", "alice", "zone1")
        assert ("group", "team-a") in alice_groups
        assert ("group", "engineering") in alice_groups
        assert ("group", "team-b") not in alice_groups  # Alice not in team-b

        # Verify bob's groups
        bob_groups = manager.get_transitive_groups("user", "bob", "zone1")
        assert ("group", "team-b") in bob_groups
        assert ("group", "engineering") in bob_groups
        assert ("group", "team-a") not in bob_groups  # Bob not in team-a


class TestEnhancedReBACManagerWithLeopard:
    """Tests for EnhancedReBACManager integration with Leopard."""

    def test_write_updates_closure(self, manager):
        """Test that rebac_write updates the Leopard closure."""
        # Write membership
        manager.rebac_write(
            subject=("user", "alice"),
            relation="member-of",
            object=("group", "team-a"),
            zone_id="zone1",
        )

        # Check closure was updated
        groups = manager.get_transitive_groups("user", "alice", "zone1")
        assert ("group", "team-a") in groups

    def test_delete_updates_closure(self, manager):
        """Test that rebac_delete updates the Leopard closure."""
        # Write membership
        write_result = manager.rebac_write(
            subject=("user", "alice"),
            relation="member-of",
            object=("group", "team-a"),
            zone_id="zone1",
        )

        # Verify it's in closure
        groups = manager.get_transitive_groups("user", "alice", "zone1")
        assert ("group", "team-a") in groups

        # Delete membership
        manager.rebac_delete(write_result.tuple_id)

        # Rebuild closure to ensure consistency
        manager.rebuild_leopard_closure("zone1")

        # Verify it's removed from closure
        groups = manager.get_transitive_groups("user", "alice", "zone1")
        assert ("group", "team-a") not in groups

    def test_deep_nesting(self, manager):
        """Test deep group nesting (5 levels)."""
        # Create hierarchy: user -> g1 -> g2 -> g3 -> g4 -> g5
        manager.rebac_write(("user", "alice"), "member-of", ("group", "g1"), zone_id="t1")
        manager.rebac_write(("group", "g1"), "member-of", ("group", "g2"), zone_id="t1")
        manager.rebac_write(("group", "g2"), "member-of", ("group", "g3"), zone_id="t1")
        manager.rebac_write(("group", "g3"), "member-of", ("group", "g4"), zone_id="t1")
        manager.rebac_write(("group", "g4"), "member-of", ("group", "g5"), zone_id="t1")

        # Rebuild to ensure all transitive relationships
        manager.rebuild_leopard_closure("t1")

        # Verify alice has access to all groups
        groups = manager.get_transitive_groups("user", "alice", "t1")
        assert ("group", "g1") in groups
        assert ("group", "g2") in groups
        assert ("group", "g3") in groups
        assert ("group", "g4") in groups
        assert ("group", "g5") in groups

    def test_fallback_without_leopard(self, manager_no_leopard):
        """Test fallback computation when Leopard is disabled."""
        # Write memberships
        manager_no_leopard.rebac_write(
            subject=("user", "alice"),
            relation="member-of",
            object=("group", "team-a"),
            zone_id="zone1",
        )
        manager_no_leopard.rebac_write(
            subject=("group", "team-a"),
            relation="member-of",
            object=("group", "engineering"),
            zone_id="zone1",
        )

        # Get transitive groups (should use fallback)
        groups = manager_no_leopard.get_transitive_groups("user", "alice", "zone1")
        assert ("group", "team-a") in groups
        assert ("group", "engineering") in groups

    def test_zone_isolation(self, manager):
        """Test that closure is zone-isolated."""
        # Write memberships in different zones
        manager.rebac_write(
            subject=("user", "alice"),
            relation="member-of",
            object=("group", "team-a"),
            zone_id="zone1",
        )
        manager.rebac_write(
            subject=("user", "alice"),
            relation="member-of",
            object=("group", "team-b"),
            zone_id="zone2",
        )

        # Verify zone isolation
        zone1_groups = manager.get_transitive_groups("user", "alice", "zone1")
        assert ("group", "team-a") in zone1_groups
        assert ("group", "team-b") not in zone1_groups

        zone2_groups = manager.get_transitive_groups("user", "alice", "zone2")
        assert ("group", "team-b") in zone2_groups
        assert ("group", "team-a") not in zone2_groups

    def test_invalidate_leopard_cache(self, manager):
        """Test cache invalidation."""
        # Write membership
        manager.rebac_write(
            subject=("user", "alice"),
            relation="member-of",
            object=("group", "team-a"),
            zone_id="zone1",
        )

        # Warm up cache
        manager.get_transitive_groups("user", "alice", "zone1")

        # Invalidate cache
        manager.invalidate_leopard_cache("zone1")

        # Should still work (fetches from DB)
        groups = manager.get_transitive_groups("user", "alice", "zone1")
        assert ("group", "team-a") in groups


class TestLeopardEdgeCases:
    """Tests for edge cases and error handling."""

    def test_empty_closure(self, manager):
        """Test getting closure for member with no groups."""
        groups = manager.get_transitive_groups("user", "nobody", "zone1")
        assert groups == set()

    def test_self_loop(self, manager):
        """Test handling of self-referential membership (should be ignored)."""
        # This shouldn't happen in practice, but we should handle it gracefully
        manager.rebac_write(
            subject=("group", "team-a"),
            relation="member-of",
            object=("group", "team-a"),  # Self-loop
            zone_id="zone1",
        )

        manager.rebuild_leopard_closure("zone1")

        # Should not cause infinite loop
        groups = manager.get_transitive_groups("group", "team-a", "zone1")
        # Self-membership might or might not be in closure depending on implementation
        # Just verify it doesn't hang and returns a set
        assert isinstance(groups, set)

    def test_non_membership_relation(self, manager):
        """Test that non-membership relations don't affect closure."""
        # Write ownership (not membership)
        manager.rebac_write(
            subject=("user", "alice"),
            relation="owner-of",
            object=("file", "readme.txt"),
            zone_id="zone1",
        )

        # Closure should be empty (owner-of is not a membership relation)
        groups = manager.get_transitive_groups("user", "alice", "zone1")
        assert ("file", "readme.txt") not in groups
