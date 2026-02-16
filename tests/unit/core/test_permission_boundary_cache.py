"""Tests for PermissionBoundaryCache (Issue #922).

Tests the permission boundary cache that provides O(1) inheritance checks
by caching the nearest ancestor with an explicit permission grant.
"""

from nexus.services.permissions.permission_boundary_cache import PermissionBoundaryCache


class TestPermissionBoundaryCache:
    """Tests for PermissionBoundaryCache class."""

    def test_init_default_values(self):
        """Test initialization with default values."""
        cache = PermissionBoundaryCache()
        stats = cache.get_stats()

        assert stats["max_size"] == 50_000
        assert stats["ttl_seconds"] == 300
        assert stats["enable_metrics"] is True
        assert stats["current_subjects"] == 0
        assert stats["total_mappings"] == 0

    def test_init_custom_values(self):
        """Test initialization with custom values."""
        cache = PermissionBoundaryCache(
            max_size=1000,
            ttl_seconds=60,
            enable_metrics=False,
        )
        stats = cache.get_stats()

        assert stats["max_size"] == 1000
        assert stats["ttl_seconds"] == 60
        assert stats["enable_metrics"] is False

    def test_set_and_get_boundary(self):
        """Test setting and retrieving a boundary."""
        cache = PermissionBoundaryCache()

        # Set boundary: /workspace/project/src/file.py → /workspace
        # Note: paths are normalized (trailing slashes removed)
        cache.set_boundary(
            zone_id="zone1",
            subject_type="user",
            subject_id="alice",
            permission="read",
            path="/workspace/project/src/file.py",
            boundary_path="/workspace",
        )

        # Get boundary should return the cached boundary
        boundary = cache.get_boundary(
            zone_id="zone1",
            subject_type="user",
            subject_id="alice",
            permission="read",
            path="/workspace/project/src/file.py",
        )

        assert boundary == "/workspace"

    def test_get_boundary_ancestor_lookup(self):
        """Test that ancestor boundaries are found for descendant paths.

        The boundary cache has two lookup modes:
        1. Exact path match - returns cached boundary for that path
        2. Ancestor lookup - walks up the path tree to find a cached ancestor

        For efficient caching, when we cache a boundary for a directory (e.g., /workspace/project),
        all descendant paths should resolve via ancestor lookup.
        """
        cache = PermissionBoundaryCache()

        # Cache boundary for a directory path
        # Note: trailing slashes are normalized away
        cache.set_boundary(
            zone_id="zone1",
            subject_type="user",
            subject_id="alice",
            permission="read",
            path="/workspace/project",  # Directory path (no trailing slash)
            boundary_path="/workspace",
        )

        # Files under that directory should find the boundary via ancestor lookup
        boundary = cache.get_boundary(
            zone_id="zone1",
            subject_type="user",
            subject_id="alice",
            permission="read",
            path="/workspace/project/file.py",
        )

        assert boundary == "/workspace"

        # Deep nested paths should also resolve
        boundary = cache.get_boundary(
            zone_id="zone1",
            subject_type="user",
            subject_id="alice",
            permission="read",
            path="/workspace/project/deep/nested/file.py",
        )

        assert boundary == "/workspace"

        # Paths outside the cached directory should not resolve
        boundary = cache.get_boundary(
            zone_id="zone1",
            subject_type="user",
            subject_id="alice",
            permission="read",
            path="/other/path/file.py",
        )

        assert boundary is None

    def test_get_boundary_miss(self):
        """Test cache miss returns None."""
        cache = PermissionBoundaryCache()

        boundary = cache.get_boundary(
            zone_id="zone1",
            subject_type="user",
            subject_id="alice",
            permission="read",
            path="/workspace/file.py",
        )

        assert boundary is None

    def test_zone_isolation(self):
        """Test that boundaries are isolated by zone."""
        cache = PermissionBoundaryCache()

        # Set boundary for zone1
        cache.set_boundary(
            zone_id="zone1",
            subject_type="user",
            subject_id="alice",
            permission="read",
            path="/workspace/file.py",
            boundary_path="/workspace/",
        )

        # Should NOT find boundary for zone2
        boundary = cache.get_boundary(
            zone_id="zone2",
            subject_type="user",
            subject_id="alice",
            permission="read",
            path="/workspace/file.py",
        )

        assert boundary is None

    def test_subject_isolation(self):
        """Test that boundaries are isolated by subject."""
        cache = PermissionBoundaryCache()

        # Set boundary for alice
        cache.set_boundary(
            zone_id="zone1",
            subject_type="user",
            subject_id="alice",
            permission="read",
            path="/workspace/file.py",
            boundary_path="/workspace/",
        )

        # Should NOT find boundary for bob
        boundary = cache.get_boundary(
            zone_id="zone1",
            subject_type="user",
            subject_id="bob",
            permission="read",
            path="/workspace/file.py",
        )

        assert boundary is None

    def test_permission_isolation(self):
        """Test that boundaries are isolated by permission type."""
        cache = PermissionBoundaryCache()

        # Set boundary for read
        cache.set_boundary(
            zone_id="zone1",
            subject_type="user",
            subject_id="alice",
            permission="read",
            path="/workspace/file.py",
            boundary_path="/workspace/",
        )

        # Should NOT find boundary for write
        boundary = cache.get_boundary(
            zone_id="zone1",
            subject_type="user",
            subject_id="alice",
            permission="write",
            path="/workspace/file.py",
        )

        assert boundary is None

    def test_invalidate_subject(self):
        """Test invalidating all boundaries for a subject."""
        cache = PermissionBoundaryCache()

        # Set multiple boundaries for alice
        cache.set_boundary("zone1", "user", "alice", "read", "/a.py", "/")
        cache.set_boundary("zone1", "user", "alice", "write", "/b.py", "/")
        cache.set_boundary("zone1", "user", "bob", "read", "/c.py", "/")

        # Invalidate alice's boundaries
        count = cache.invalidate_subject("zone1", "user", "alice")

        assert count == 2  # alice had 2 boundaries

        # Alice's boundaries should be gone
        assert cache.get_boundary("zone1", "user", "alice", "read", "/a.py") is None
        assert cache.get_boundary("zone1", "user", "alice", "write", "/b.py") is None

        # Bob's boundary should still exist
        assert cache.get_boundary("zone1", "user", "bob", "read", "/c.py") == "/"

    def test_invalidate_path_prefix(self):
        """Test invalidating boundaries under a path prefix."""
        cache = PermissionBoundaryCache()

        # Set boundaries at different paths (trailing slashes normalized away)
        cache.set_boundary("zone1", "user", "alice", "read", "/workspace/a.py", "/workspace")
        cache.set_boundary("zone1", "user", "alice", "read", "/other/b.py", "/other")
        cache.set_boundary("zone1", "user", "bob", "read", "/workspace/c.py", "/workspace")

        # Invalidate /workspace prefix
        count = cache.invalidate_path_prefix("zone1", "/workspace")

        assert count == 2  # Two entries under /workspace

        # /workspace boundaries should be gone
        assert cache.get_boundary("zone1", "user", "alice", "read", "/workspace/a.py") is None
        assert cache.get_boundary("zone1", "user", "bob", "read", "/workspace/c.py") is None

        # /other boundary should still exist
        assert cache.get_boundary("zone1", "user", "alice", "read", "/other/b.py") == "/other"

    def test_invalidate_permission_change(self):
        """Test precise invalidation for a specific permission change."""
        cache = PermissionBoundaryCache()

        # Set boundaries (trailing slashes normalized away)
        cache.set_boundary("zone1", "user", "alice", "read", "/workspace/a.py", "/workspace")
        cache.set_boundary("zone1", "user", "alice", "read", "/workspace/b.py", "/workspace")
        cache.set_boundary("zone1", "user", "alice", "write", "/workspace/c.py", "/workspace")

        # Invalidate alice's read permission on /workspace
        count = cache.invalidate_permission_change("zone1", "user", "alice", "read", "/workspace")

        assert count == 2  # Two read entries pointing to /workspace

        # Read boundaries should be gone
        assert cache.get_boundary("zone1", "user", "alice", "read", "/workspace/a.py") is None
        assert cache.get_boundary("zone1", "user", "alice", "read", "/workspace/b.py") is None

        # Write boundary should still exist
        assert (
            cache.get_boundary("zone1", "user", "alice", "write", "/workspace/c.py") == "/workspace"
        )

    def test_metrics_tracking(self):
        """Test that metrics are tracked correctly."""
        cache = PermissionBoundaryCache(enable_metrics=True)

        # Miss
        cache.get_boundary("t", "u", "a", "r", "/file.py")

        stats = cache.get_stats()
        assert stats["misses"] == 1
        assert stats["hits"] == 0

        # Set and hit
        cache.set_boundary("t", "u", "a", "r", "/file.py", "/")
        cache.get_boundary("t", "u", "a", "r", "/file.py")

        stats = cache.get_stats()
        assert stats["sets"] == 1
        assert stats["hits"] == 1
        assert stats["misses"] == 1
        assert stats["hit_rate_percent"] == 50.0

    def test_reset_stats(self):
        """Test resetting statistics."""
        cache = PermissionBoundaryCache()

        cache.set_boundary("t", "u", "a", "r", "/file.py", "/")
        cache.get_boundary("t", "u", "a", "r", "/file.py")

        cache.reset_stats()

        stats = cache.get_stats()
        assert stats["hits"] == 0
        assert stats["misses"] == 0
        assert stats["sets"] == 0

    def test_clear(self):
        """Test clearing the cache."""
        cache = PermissionBoundaryCache()

        cache.set_boundary("t", "u", "a", "r", "/file.py", "/")
        assert cache.get_boundary("t", "u", "a", "r", "/file.py") == "/"

        cache.clear()

        assert cache.get_boundary("t", "u", "a", "r", "/file.py") is None

        stats = cache.get_stats()
        assert stats["current_subjects"] == 0
        assert stats["total_mappings"] == 0

    def test_default_zone_id(self):
        """Test that None zone_id is treated as 'default'."""
        cache = PermissionBoundaryCache()

        # Set with None zone
        cache.set_boundary(None, "user", "alice", "read", "/file.py", "/")  # type: ignore

        # Should find with "default" zone
        boundary = cache.get_boundary("default", "user", "alice", "read", "/file.py")
        assert boundary == "/"

    def test_root_boundary(self):
        """Test caching boundary at root."""
        cache = PermissionBoundaryCache()

        cache.set_boundary("t", "u", "a", "r", "/file.py", "/")

        boundary = cache.get_boundary("t", "u", "a", "r", "/file.py")
        assert boundary == "/"

    def test_concurrent_access_thread_safety(self):
        """Test thread safety with concurrent access."""
        import threading

        cache = PermissionBoundaryCache()
        errors = []

        def writer():
            try:
                for i in range(100):
                    cache.set_boundary("t", "u", f"user{i}", "r", f"/file{i}.py", "/")
            except Exception as e:
                errors.append(e)

        def reader():
            try:
                for i in range(100):
                    cache.get_boundary("t", "u", f"user{i}", "r", f"/file{i}.py")
            except Exception as e:
                errors.append(e)

        def invalidator():
            try:
                for i in range(10):
                    cache.invalidate_subject("t", "u", f"user{i * 10}")
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=writer),
            threading.Thread(target=reader),
            threading.Thread(target=invalidator),
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0, f"Thread safety errors: {errors}"


class TestPermissionBoundaryCacheIntegration:
    """Integration tests with PermissionEnforcer."""

    def test_enforcer_creates_boundary_cache_by_default(self):
        """Test that PermissionEnforcer creates boundary cache by default."""
        from nexus.services.permissions.enforcer import PermissionEnforcer

        enforcer = PermissionEnforcer()

        assert enforcer._boundary_cache is not None
        assert isinstance(enforcer._boundary_cache, PermissionBoundaryCache)

    def test_enforcer_boundary_cache_disabled(self):
        """Test disabling boundary cache in PermissionEnforcer."""
        from nexus.services.permissions.enforcer import PermissionEnforcer

        enforcer = PermissionEnforcer(enable_boundary_cache=False)

        assert enforcer._boundary_cache is None

    def test_enforcer_custom_boundary_cache(self):
        """Test providing custom boundary cache to PermissionEnforcer."""
        from nexus.services.permissions.enforcer import PermissionEnforcer

        custom_cache = PermissionBoundaryCache(max_size=100, ttl_seconds=10)
        enforcer = PermissionEnforcer(boundary_cache=custom_cache)

        assert enforcer._boundary_cache is custom_cache
        stats = enforcer.get_boundary_cache_stats()
        assert stats is not None
        assert stats["max_size"] == 100
        assert stats["ttl_seconds"] == 10

    def test_enforcer_get_boundary_cache_stats(self):
        """Test getting boundary cache stats from enforcer."""
        from nexus.services.permissions.enforcer import PermissionEnforcer

        enforcer = PermissionEnforcer()
        stats = enforcer.get_boundary_cache_stats()

        assert stats is not None
        assert "hits" in stats
        assert "misses" in stats
        assert "hit_rate_percent" in stats

    def test_enforcer_get_boundary_cache_stats_when_disabled(self):
        """Test getting stats when boundary cache is disabled returns None."""
        from nexus.services.permissions.enforcer import PermissionEnforcer

        enforcer = PermissionEnforcer(enable_boundary_cache=False)
        stats = enforcer.get_boundary_cache_stats()

        assert stats is None

    def test_enforcer_reset_boundary_cache_stats(self):
        """Test resetting boundary cache stats through enforcer."""
        from nexus.services.permissions.enforcer import PermissionEnforcer

        enforcer = PermissionEnforcer()

        # Generate some stats
        enforcer._boundary_cache.set_boundary("t", "u", "a", "r", "/f.py", "/")
        enforcer._boundary_cache.get_boundary("t", "u", "a", "r", "/f.py")

        stats = enforcer.get_boundary_cache_stats()
        assert stats["hits"] > 0

        enforcer.reset_boundary_cache_stats()

        stats = enforcer.get_boundary_cache_stats()
        assert stats["hits"] == 0

    def test_enforcer_clear_boundary_cache(self):
        """Test clearing boundary cache through enforcer."""
        from nexus.services.permissions.enforcer import PermissionEnforcer

        enforcer = PermissionEnforcer()

        enforcer._boundary_cache.set_boundary("t", "u", "a", "r", "/f.py", "/")
        assert enforcer._boundary_cache.get_boundary("t", "u", "a", "r", "/f.py") == "/"

        enforcer.clear_boundary_cache()

        assert enforcer._boundary_cache.get_boundary("t", "u", "a", "r", "/f.py") is None
