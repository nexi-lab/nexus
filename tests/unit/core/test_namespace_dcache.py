"""Unit tests for NamespaceManager dcache layer (Issue #1244 — dcache pattern, Phase 0).

Tests cover:
- dcache lookup: miss → bisect → cached, positive hit, negative hit, revision roll
- Negative entries: shorter TTL, invalidation on revision roll, grant-added flow
- Revision quantization: key-based bucket, same/different buckets, fetch failure
- filter_visible() batch method: empty, all-visible, all-invisible, mixed, order preservation
- Thread safety: concurrent dcache reads, concurrent with mount rebuild
- Pre-computed mount_paths: stored in cache, used by is_visible
"""

from __future__ import annotations

import threading
import time
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine

from nexus.rebac.namespace_manager import NamespaceManager
from nexus.storage.models import Base

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def engine():
    """Create in-memory SQLite database for testing."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def enhanced_rebac_manager(engine):
    """Create an EnhancedReBACManager for testing."""
    from nexus.rebac.manager import EnhancedReBACManager

    manager = EnhancedReBACManager(
        engine=engine,
        cache_ttl_seconds=300,
        max_depth=10,
    )
    yield manager
    manager.close()


@pytest.fixture
def namespace_manager(enhanced_rebac_manager):
    """Create a NamespaceManager with dcache enabled and short TTLs for test speed."""
    return NamespaceManager(
        rebac_manager=enhanced_rebac_manager,
        cache_maxsize=100,
        cache_ttl=60,
        revision_window=2,  # Small window so revision changes are easy to trigger
        dcache_maxsize=1000,
        dcache_positive_ttl=300,
        dcache_negative_ttl=60,
    )


def _grant_file(rebac_manager, subject, path, zone_id="test_zone"):
    """Helper to grant read access to a file via ReBAC."""
    rebac_manager.rebac_write(
        subject=subject,
        relation="direct_viewer",
        object=("file", path),
        zone_id=zone_id,
    )


# ---------------------------------------------------------------------------
# TestDCacheLookup — basic cache behavior
# ---------------------------------------------------------------------------


class TestDCacheLookup:
    """Tests for dcache miss/hit/eviction behavior."""

    def test_dcache_miss_falls_through_to_bisect(self, enhanced_rebac_manager, namespace_manager):
        """On dcache miss, falls through to mount table bisect, then caches result."""
        _grant_file(enhanced_rebac_manager, ("user", "alice"), "/workspace/proj/a.txt")
        alice = ("user", "alice")

        # First call: dcache miss → bisect → True
        assert namespace_manager.is_visible(alice, "/workspace/proj/a.txt", "test_zone") is True
        assert namespace_manager.metrics["dcache_misses"] == 1

        # Second call: dcache hit
        assert namespace_manager.is_visible(alice, "/workspace/proj/a.txt", "test_zone") is True
        assert namespace_manager.metrics["dcache_hits"] == 1

    def test_dcache_hit_returns_cached_positive(self, enhanced_rebac_manager, namespace_manager):
        """Positive dcache entry serves O(1) result without mount table lookup."""
        _grant_file(enhanced_rebac_manager, ("user", "alice"), "/workspace/proj/a.txt")
        alice = ("user", "alice")

        # Prime the dcache
        namespace_manager.is_visible(alice, "/workspace/proj/a.txt", "test_zone")

        # Verify dcache hit — mock _get_mount_data to confirm it's NOT called
        with patch.object(
            namespace_manager, "_get_mount_data", wraps=namespace_manager._get_mount_data
        ) as mock:
            result = namespace_manager.is_visible(alice, "/workspace/proj/a.txt", "test_zone")
            assert result is True
            mock.assert_not_called()

    def test_dcache_hit_returns_cached_negative(self, enhanced_rebac_manager, namespace_manager):
        """Negative dcache entry serves O(1) result without mount table lookup."""
        _grant_file(enhanced_rebac_manager, ("user", "alice"), "/workspace/proj/a.txt")
        alice = ("user", "alice")

        # Prime the dcache with a negative entry (invisible path)
        assert namespace_manager.is_visible(alice, "/secret/data.txt", "test_zone") is False

        # Verify dcache negative hit
        with patch.object(
            namespace_manager, "_get_mount_data", wraps=namespace_manager._get_mount_data
        ) as mock:
            result = namespace_manager.is_visible(alice, "/secret/data.txt", "test_zone")
            assert result is False
            mock.assert_not_called()

    def test_dcache_miss_after_revision_roll(self, enhanced_rebac_manager, namespace_manager):
        """When revision bucket changes, dcache key changes → miss → re-bisect."""
        _grant_file(enhanced_rebac_manager, ("user", "alice"), "/workspace/proj/a.txt")
        alice = ("user", "alice")
        zone = "test_zone"

        # Prime dcache with revision bucket = 0
        with patch.object(namespace_manager, "_get_current_revision_bucket", return_value=0):
            assert namespace_manager.is_visible(alice, "/workspace/proj/a.txt", zone) is True
        assert namespace_manager.metrics["dcache_misses"] == 1

        # Simulate revision bucket roll to 1
        with patch.object(namespace_manager, "_get_current_revision_bucket", return_value=1):
            assert namespace_manager.is_visible(alice, "/workspace/proj/a.txt", zone) is True
        # New bucket → different key → dcache miss
        assert namespace_manager.metrics["dcache_misses"] == 2

    def test_dcache_metrics_tracking(self, enhanced_rebac_manager, namespace_manager):
        """Metrics track dcache_hits, dcache_misses, dcache_negative_hits."""
        _grant_file(enhanced_rebac_manager, ("user", "alice"), "/workspace/proj/a.txt")
        alice = ("user", "alice")
        zone = "test_zone"

        # Miss (positive)
        namespace_manager.is_visible(alice, "/workspace/proj/a.txt", zone)
        # Miss (negative)
        namespace_manager.is_visible(alice, "/secret/data.txt", zone)
        # Hit (positive)
        namespace_manager.is_visible(alice, "/workspace/proj/a.txt", zone)
        # Hit (negative)
        namespace_manager.is_visible(alice, "/secret/data.txt", zone)

        m = namespace_manager.metrics
        assert m["dcache_misses"] == 2
        assert m["dcache_hits"] == 2
        assert m["dcache_negative_hits"] == 1
        assert m["dcache_positive_size"] == 1
        assert m["dcache_negative_size"] == 1

    def test_dcache_eviction_under_maxsize(self, enhanced_rebac_manager):
        """LRU eviction removes cold entries when dcache reaches maxsize."""
        ns = NamespaceManager(
            rebac_manager=enhanced_rebac_manager,
            dcache_maxsize=5,  # Very small for testing eviction
            dcache_positive_ttl=300,
            dcache_negative_ttl=60,
        )
        alice = ("user", "alice")
        zone = "test_zone"

        # Grant a lot of files
        for i in range(10):
            _grant_file(enhanced_rebac_manager, alice, f"/workspace/proj/file{i}.txt")

        # Check visibility for 10 different paths — exceeds maxsize of 5
        for i in range(10):
            ns.is_visible(alice, f"/workspace/proj/file{i}.txt", zone)

        # Positive cache should be capped at maxsize
        assert ns.metrics["dcache_positive_size"] <= 5


# ---------------------------------------------------------------------------
# TestDCacheNegativeEntries — security-critical negative caching
# ---------------------------------------------------------------------------


class TestDCacheNegativeEntries:
    """Tests for negative (invisible path) caching behavior."""

    def test_negative_entry_stored_on_invisible_path(
        self, enhanced_rebac_manager, namespace_manager
    ):
        """is_visible() returning False stores a negative cache entry."""
        _grant_file(enhanced_rebac_manager, ("user", "alice"), "/workspace/proj/a.txt")
        alice = ("user", "alice")

        result = namespace_manager.is_visible(alice, "/secret/nope.txt", "test_zone")
        assert result is False
        assert namespace_manager.metrics["dcache_negative_size"] == 1

    def test_negative_entry_expires_with_shorter_ttl(self, enhanced_rebac_manager):
        """Negative entries use shorter TTL than positive entries."""
        ns = NamespaceManager(
            rebac_manager=enhanced_rebac_manager,
            dcache_positive_ttl=300,
            dcache_negative_ttl=1,  # 1 second for test speed
        )
        alice = ("user", "alice")

        # Create a negative entry
        ns.is_visible(alice, "/secret/nope.txt", "test_zone")
        assert ns.metrics["dcache_negative_size"] == 1

        # Wait for negative TTL to expire
        time.sleep(1.1)

        # Negative entry should be expired — TTLCache removes on next access
        ns.is_visible(alice, "/secret/nope.txt", "test_zone")
        # This was a dcache miss (expired), not a hit
        assert ns.metrics["dcache_misses"] == 2

    def test_negative_entry_invalidated_on_revision_roll(
        self, enhanced_rebac_manager, namespace_manager
    ):
        """Revision bucket change makes negative entries stale (key mismatch)."""
        alice = ("user", "alice")
        zone = "test_zone"

        # No grants — everything is invisible. Prime with bucket=0.
        with patch.object(namespace_manager, "_get_current_revision_bucket", return_value=0):
            assert namespace_manager.is_visible(alice, "/workspace/proj/a.txt", zone) is False
        assert namespace_manager.metrics["dcache_negative_size"] == 1

        # Simulate revision bucket roll to 1
        with patch.object(namespace_manager, "_get_current_revision_bucket", return_value=1):
            namespace_manager.is_visible(alice, "/workspace/proj/a.txt", zone)
        # New bucket → different key → dcache miss (old negative entry has bucket=0)
        assert namespace_manager.metrics["dcache_misses"] == 2

    def test_grant_added_makes_negative_stale(self, enhanced_rebac_manager, namespace_manager):
        """Full flow: path invisible → grant added → revision bumps → path now visible."""
        alice = ("user", "alice")
        zone = "test_zone"

        # Initially invisible
        assert namespace_manager.is_visible(alice, "/workspace/proj/a.txt", zone) is False

        # Grant access (this bumps revision)
        _grant_file(enhanced_rebac_manager, alice, "/workspace/proj/a.txt", zone)
        # Need enough writes to roll the revision bucket (window=2)
        _grant_file(enhanced_rebac_manager, alice, "/workspace/proj/b.txt", zone)

        # Invalidate mount table to pick up new grants
        namespace_manager.invalidate(alice)

        # Now visible (dcache miss due to revision roll + mount table rebuild)
        assert namespace_manager.is_visible(alice, "/workspace/proj/a.txt", zone) is True

    def test_concurrent_negative_positive_different_subjects(
        self, enhanced_rebac_manager, namespace_manager
    ):
        """Different subjects can have positive and negative entries for the same path."""
        zone = "test_zone"
        _grant_file(enhanced_rebac_manager, ("user", "alice"), "/workspace/proj/a.txt")

        alice = ("user", "alice")
        bob = ("user", "bob")

        # Alice sees it, Bob doesn't
        assert namespace_manager.is_visible(alice, "/workspace/proj/a.txt", zone) is True
        assert namespace_manager.is_visible(bob, "/workspace/proj/a.txt", zone) is False

        # Both entries are cached — they don't interfere
        assert namespace_manager.metrics["dcache_positive_size"] == 1
        assert namespace_manager.metrics["dcache_negative_size"] == 1

    def test_negative_does_not_block_other_subjects(
        self, enhanced_rebac_manager, namespace_manager
    ):
        """Subject A's negative entry doesn't affect Subject B's lookup."""
        zone = "test_zone"
        _grant_file(enhanced_rebac_manager, ("user", "alice"), "/workspace/proj/a.txt")

        bob = ("user", "bob")
        alice = ("user", "alice")

        # Bob's lookup creates a negative entry
        assert namespace_manager.is_visible(bob, "/workspace/proj/a.txt", zone) is False

        # Alice should still see it (different subject → different dcache key)
        assert namespace_manager.is_visible(alice, "/workspace/proj/a.txt", zone) is True

    def test_all_invisible_paths_cached_negative(self, enhanced_rebac_manager, namespace_manager):
        """filter_visible with all-invisible input caches negative entries for all paths."""
        zone = "test_zone"
        alice = ("user", "alice")
        # Alice has no grants

        paths = [f"/secret/file{i}.txt" for i in range(5)]
        result = namespace_manager.filter_visible(alice, paths, zone)

        assert result == []
        assert namespace_manager.metrics["dcache_negative_size"] == 5

    def test_negative_cache_fail_closed(self, enhanced_rebac_manager, namespace_manager):
        """On revision fetch error, dcache key uses bucket 0 → still works, just less caching."""
        alice = ("user", "alice")
        zone = "test_zone"

        with patch.object(
            enhanced_rebac_manager, "_get_zone_revision", side_effect=RuntimeError("DB down")
        ):
            # Should not crash — returns bucket 0 as fallback
            result = namespace_manager.is_visible(alice, "/workspace/proj/a.txt", zone)
            assert result is False  # No grants → invisible (fail-closed)


# ---------------------------------------------------------------------------
# TestDCacheRevisionQuantization — key-based invalidation
# ---------------------------------------------------------------------------


class TestDCacheRevisionQuantization:
    """Tests for revision-bucket-based dcache key invalidation."""

    def test_same_revision_bucket_returns_cached(self, enhanced_rebac_manager, namespace_manager):
        """Within the same revision bucket, dcache entries are hit."""
        _grant_file(enhanced_rebac_manager, ("user", "alice"), "/workspace/proj/a.txt")
        alice = ("user", "alice")
        zone = "test_zone"

        # Prime dcache
        namespace_manager.is_visible(alice, "/workspace/proj/a.txt", zone)

        # One write stays within revision_window=2
        enhanced_rebac_manager.rebac_write(
            subject=("user", "bob"),
            relation="direct_viewer",
            object=("file", "/workspace/other.txt"),
            zone_id=zone,
        )

        # Should still be a dcache hit (same bucket)
        namespace_manager.is_visible(alice, "/workspace/proj/a.txt", zone)
        assert namespace_manager.metrics["dcache_hits"] >= 1

    def test_different_revision_bucket_misses(self, enhanced_rebac_manager, namespace_manager):
        """When revision crosses bucket boundary, dcache misses."""
        _grant_file(enhanced_rebac_manager, ("user", "alice"), "/workspace/proj/a.txt")
        alice = ("user", "alice")
        zone = "test_zone"

        # Prime dcache with bucket=5
        with patch.object(namespace_manager, "_get_current_revision_bucket", return_value=5):
            namespace_manager.is_visible(alice, "/workspace/proj/a.txt", zone)
        initial_misses = namespace_manager.metrics["dcache_misses"]

        # Simulate revision bucket crossing to 6
        with patch.object(namespace_manager, "_get_current_revision_bucket", return_value=6):
            namespace_manager.is_visible(alice, "/workspace/proj/a.txt", zone)
        # Different bucket → different key → dcache miss
        assert namespace_manager.metrics["dcache_misses"] > initial_misses

    def test_revision_bucket_in_cache_key(self, enhanced_rebac_manager, namespace_manager):
        """Verify the dcache key includes the revision bucket."""
        alice = ("user", "alice")
        key = namespace_manager._dcache_key(alice, "/workspace/proj/a.txt", "test_zone")

        # Key should be 5-tuple: (subject_type, subject_id, path, zone_id, revision_bucket)
        assert len(key) == 5
        assert key[0] == "user"
        assert key[1] == "alice"
        assert key[2] == "/workspace/proj/a.txt"
        assert key[3] == "test_zone"
        assert isinstance(key[4], int)

    def test_revision_fetch_failure_returns_bucket_zero(
        self, enhanced_rebac_manager, namespace_manager
    ):
        """On revision fetch error, _get_current_revision_bucket returns 0."""
        with patch.object(
            enhanced_rebac_manager, "_get_zone_revision", side_effect=RuntimeError("DB down")
        ):
            bucket = namespace_manager._get_current_revision_bucket("test_zone")
            assert bucket == 0


# ---------------------------------------------------------------------------
# TestFilterVisible — batch method
# ---------------------------------------------------------------------------


class TestFilterVisible:
    """Tests for filter_visible() batch method."""

    def test_empty_paths_returns_empty(self, namespace_manager):
        """Empty path list returns empty result."""
        result = namespace_manager.filter_visible(("user", "alice"), [], "test_zone")
        assert result == []

    def test_all_visible_returns_all(self, enhanced_rebac_manager, namespace_manager):
        """When all paths are visible, all are returned."""
        zone = "test_zone"
        alice = ("user", "alice")
        for i in range(5):
            _grant_file(enhanced_rebac_manager, alice, f"/workspace/proj/file{i}.txt", zone)

        paths = [f"/workspace/proj/file{i}.txt" for i in range(5)]
        result = namespace_manager.filter_visible(alice, paths, zone)

        assert len(result) == 5
        assert set(result) == set(paths)

    def test_all_invisible_returns_empty(self, namespace_manager):
        """When all paths are invisible, empty list returned."""
        alice = ("user", "alice")
        paths = ["/secret/a.txt", "/secret/b.txt", "/secret/c.txt"]
        result = namespace_manager.filter_visible(alice, paths, "test_zone")

        assert result == []

    def test_mixed_visibility_correct_filtering(self, enhanced_rebac_manager, namespace_manager):
        """Mixed visible/invisible paths are correctly filtered."""
        zone = "test_zone"
        alice = ("user", "alice")
        _grant_file(enhanced_rebac_manager, alice, "/workspace/proj/a.txt", zone)
        _grant_file(enhanced_rebac_manager, alice, "/workspace/proj/c.txt", zone)

        paths = [
            "/workspace/proj/a.txt",  # visible
            "/secret/b.txt",  # invisible
            "/workspace/proj/c.txt",  # visible
            "/other/d.txt",  # invisible
        ]
        result = namespace_manager.filter_visible(alice, paths, zone)

        assert "/workspace/proj/a.txt" in result
        assert "/workspace/proj/c.txt" in result
        assert "/secret/b.txt" not in result
        assert "/other/d.txt" not in result
        assert len(result) == 2

    def test_preserves_input_order(self, enhanced_rebac_manager, namespace_manager):
        """filter_visible preserves the order of visible paths from input."""
        zone = "test_zone"
        alice = ("user", "alice")
        _grant_file(enhanced_rebac_manager, alice, "/workspace/proj/z.txt", zone)
        _grant_file(enhanced_rebac_manager, alice, "/workspace/proj/a.txt", zone)
        _grant_file(enhanced_rebac_manager, alice, "/workspace/proj/m.txt", zone)

        paths = [
            "/workspace/proj/z.txt",
            "/secret/x.txt",
            "/workspace/proj/a.txt",
            "/secret/y.txt",
            "/workspace/proj/m.txt",
        ]
        result = namespace_manager.filter_visible(alice, paths, zone)

        assert result == ["/workspace/proj/z.txt", "/workspace/proj/a.txt", "/workspace/proj/m.txt"]

    def test_populates_dcache_on_miss(self, enhanced_rebac_manager, namespace_manager):
        """filter_visible populates dcache so second call is all-hits."""
        zone = "test_zone"
        alice = ("user", "alice")
        _grant_file(enhanced_rebac_manager, alice, "/workspace/proj/a.txt", zone)

        paths = ["/workspace/proj/a.txt", "/secret/b.txt"]

        # First call: all misses
        namespace_manager.filter_visible(alice, paths, zone)
        misses_after_first = namespace_manager.metrics["dcache_misses"]
        assert misses_after_first == 2

        # Second call: all hits
        namespace_manager.filter_visible(alice, paths, zone)
        assert namespace_manager.metrics["dcache_hits"] == 2
        # No new misses
        assert namespace_manager.metrics["dcache_misses"] == misses_after_first

    def test_second_call_all_hits(self, enhanced_rebac_manager, namespace_manager):
        """Repeated filter_visible with same paths gets 100% dcache hit rate."""
        zone = "test_zone"
        alice = ("user", "alice")
        _grant_file(enhanced_rebac_manager, alice, "/workspace/proj/a.txt", zone)

        paths = [f"/workspace/proj/file{i}.txt" for i in range(10)]
        paths.append("/workspace/proj/a.txt")

        # First call populates dcache
        namespace_manager.filter_visible(alice, paths, zone)
        first_misses = namespace_manager.metrics["dcache_misses"]
        assert first_misses == len(paths)

        # Second call — all hits
        result = namespace_manager.filter_visible(alice, paths, zone)
        assert namespace_manager.metrics["dcache_misses"] == first_misses
        assert namespace_manager.metrics["dcache_hits"] == len(paths)
        # Result should be consistent
        assert "/workspace/proj/a.txt" in result

    def test_large_path_list_performance(self, enhanced_rebac_manager, namespace_manager):
        """10K paths processed without error or excessive time."""
        zone = "test_zone"
        alice = ("user", "alice")
        # Grant a few files
        for i in range(10):
            _grant_file(enhanced_rebac_manager, alice, f"/workspace/proj/file{i}.txt", zone)

        paths = [f"/workspace/proj/file{i}.txt" for i in range(10000)]

        start = time.perf_counter()
        result = namespace_manager.filter_visible(alice, paths, zone)
        elapsed_ms = (time.perf_counter() - start) * 1000

        # Should complete in reasonable time (< 1 second)
        assert elapsed_ms < 1000
        # First 10 paths should be visible (they're in the mounted directory)
        assert len(result) >= 10

    def test_metrics_after_filter_visible(self, enhanced_rebac_manager, namespace_manager):
        """Metrics correctly reflect batch operations."""
        zone = "test_zone"
        alice = ("user", "alice")
        _grant_file(enhanced_rebac_manager, alice, "/workspace/proj/a.txt", zone)

        visible_paths = ["/workspace/proj/a.txt", "/workspace/proj/b.txt"]
        invisible_paths = ["/secret/c.txt", "/other/d.txt"]
        all_paths = visible_paths + invisible_paths

        namespace_manager.filter_visible(alice, all_paths, zone)

        m = namespace_manager.metrics
        assert m["dcache_misses"] == 4  # All 4 were misses on first call
        assert m["dcache_positive_size"] >= 1
        assert m["dcache_negative_size"] >= 1


# ---------------------------------------------------------------------------
# TestDCacheThreadSafety — concurrent access
# ---------------------------------------------------------------------------


class TestDCacheThreadSafety:
    """Tests for thread-safe dcache operations."""

    def test_concurrent_dcache_reads(self, enhanced_rebac_manager, namespace_manager):
        """10 threads reading dcache concurrently — no corruption."""
        zone = "test_zone"
        _grant_file(enhanced_rebac_manager, ("user", "alice"), "/workspace/proj/a.txt")
        alice = ("user", "alice")

        # Prime dcache
        namespace_manager.is_visible(alice, "/workspace/proj/a.txt", zone)

        errors: list[str] = []

        def reader():
            try:
                for _ in range(100):
                    result = namespace_manager.is_visible(alice, "/workspace/proj/a.txt", zone)
                    if result is not True:
                        errors.append(f"Expected True, got {result}")
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=reader) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert errors == [], f"Thread errors: {errors}"

    def test_concurrent_dcache_reads_plus_mount_rebuild(
        self, enhanced_rebac_manager, namespace_manager
    ):
        """Concurrent dcache reads while mount table rebuilds — no deadlock."""
        zone = "test_zone"
        _grant_file(enhanced_rebac_manager, ("user", "alice"), "/workspace/proj/a.txt")
        alice = ("user", "alice")

        errors: list[str] = []

        def reader():
            try:
                for _ in range(50):
                    namespace_manager.is_visible(alice, "/workspace/proj/a.txt", zone)
            except Exception as e:
                errors.append(f"reader: {e}")

        def invalidator():
            try:
                for _ in range(50):
                    namespace_manager.invalidate(alice)
            except Exception as e:
                errors.append(f"invalidator: {e}")

        threads = [threading.Thread(target=reader) for _ in range(5)]
        threads.append(threading.Thread(target=invalidator))

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert errors == [], f"Thread errors: {errors}"

    def test_concurrent_filter_visible_calls(self, enhanced_rebac_manager, namespace_manager):
        """Parallel filter_visible calls from multiple threads — no errors."""
        zone = "test_zone"
        alice = ("user", "alice")
        for i in range(5):
            _grant_file(enhanced_rebac_manager, alice, f"/workspace/proj/file{i}.txt", zone)

        paths = [f"/workspace/proj/file{i}.txt" for i in range(20)]
        errors: list[str] = []

        def batch_reader():
            try:
                for _ in range(20):
                    result = namespace_manager.filter_visible(alice, paths, zone)
                    if not isinstance(result, list):
                        errors.append(f"Expected list, got {type(result)}")
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=batch_reader) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert errors == [], f"Thread errors: {errors}"


# ---------------------------------------------------------------------------
# TestPreComputedMountPaths — mount_paths optimization (Issue #1244)
# ---------------------------------------------------------------------------


class TestPreComputedMountPaths:
    """Tests for pre-computed mount_paths stored in cache."""

    def test_mount_paths_precomputed_in_cache(self, enhanced_rebac_manager, namespace_manager):
        """Cache entry stores pre-computed mount_paths as 5-tuple."""
        _grant_file(enhanced_rebac_manager, ("user", "alice"), "/workspace/proj/a.txt")
        alice = ("user", "alice")

        # Trigger a mount table build
        namespace_manager.get_mount_table(alice, "test_zone")

        # Inspect the raw cache entry
        with namespace_manager._lock:
            cached = namespace_manager._cache.get(alice)

        assert cached is not None
        assert len(cached) == 5  # (mount_entries, mount_paths, revision, zone_id, grants_hash)

        mount_entries, mount_paths, _revision, _zone, _hash = cached
        assert isinstance(mount_paths, list)
        assert len(mount_paths) == len(mount_entries)
        # mount_paths should match virtual_paths from entries
        assert mount_paths == [m.virtual_path for m in mount_entries]

    def test_is_visible_uses_precomputed_paths(self, enhanced_rebac_manager, namespace_manager):
        """is_visible uses pre-computed mount_paths, not a fresh list comprehension."""
        _grant_file(enhanced_rebac_manager, ("user", "alice"), "/workspace/proj/a.txt")
        alice = ("user", "alice")

        # Prime the mount table cache
        namespace_manager.get_mount_table(alice, "test_zone")

        # Clear dcache to force mount table path
        namespace_manager.invalidate_dcache(alice)

        # _get_mount_data should return (entries, mount_paths) from cache
        entries, mount_paths = namespace_manager._get_mount_data(alice, "test_zone")
        assert len(mount_paths) > 0
        assert mount_paths == [m.virtual_path for m in entries]

    def test_mount_paths_sorted(self, enhanced_rebac_manager, namespace_manager):
        """Pre-computed mount_paths maintains sort invariant for bisect."""
        zone = "test_zone"
        alice = ("user", "alice")
        # Grant files in different directories
        _grant_file(enhanced_rebac_manager, alice, "/workspace/z-proj/a.txt", zone)
        _grant_file(enhanced_rebac_manager, alice, "/workspace/a-proj/b.txt", zone)
        _grant_file(enhanced_rebac_manager, alice, "/workspace/m-proj/c.txt", zone)

        entries, mount_paths = namespace_manager._get_mount_data(alice, zone)

        # Verify sorted
        assert mount_paths == sorted(mount_paths)
        assert len(mount_paths) == len(entries)


# ---------------------------------------------------------------------------
# TestInvalidateDcache — dcache invalidation
# ---------------------------------------------------------------------------


class TestInvalidateDcache:
    """Tests for invalidate_dcache() safety valve."""

    def test_invalidate_dcache_all(self, enhanced_rebac_manager, namespace_manager):
        """invalidate_dcache() with no subject clears entire dcache."""
        zone = "test_zone"
        _grant_file(enhanced_rebac_manager, ("user", "alice"), "/workspace/proj/a.txt")
        alice = ("user", "alice")
        bob = ("user", "bob")

        # Populate dcache entries for both subjects
        namespace_manager.is_visible(alice, "/workspace/proj/a.txt", zone)
        namespace_manager.is_visible(bob, "/workspace/proj/a.txt", zone)

        assert namespace_manager.metrics["dcache_positive_size"] >= 1
        assert namespace_manager.metrics["dcache_negative_size"] >= 1

        namespace_manager.invalidate_dcache()

        assert namespace_manager.metrics["dcache_positive_size"] == 0
        assert namespace_manager.metrics["dcache_negative_size"] == 0

    def test_invalidate_dcache_subject(self, enhanced_rebac_manager, namespace_manager):
        """invalidate_dcache(subject) clears only that subject's entries."""
        zone = "test_zone"
        _grant_file(enhanced_rebac_manager, ("user", "alice"), "/workspace/proj/a.txt")
        alice = ("user", "alice")
        bob = ("user", "bob")

        # Alice: positive + negative entries
        namespace_manager.is_visible(alice, "/workspace/proj/a.txt", zone)
        namespace_manager.is_visible(alice, "/secret/nope.txt", zone)
        # Bob: negative entry
        namespace_manager.is_visible(bob, "/workspace/proj/a.txt", zone)

        total_before = (
            namespace_manager.metrics["dcache_positive_size"]
            + namespace_manager.metrics["dcache_negative_size"]
        )
        assert total_before >= 3

        # Invalidate only Alice's entries
        namespace_manager.invalidate_dcache(alice)

        # Bob's entry should remain
        assert namespace_manager.metrics["dcache_negative_size"] >= 1

    def test_invalidate_clears_both_mount_and_dcache(
        self, enhanced_rebac_manager, namespace_manager
    ):
        """invalidate(subject) clears both mount table cache and dcache entries."""
        zone = "test_zone"
        _grant_file(enhanced_rebac_manager, ("user", "alice"), "/workspace/proj/a.txt")
        alice = ("user", "alice")

        # Populate both caches
        namespace_manager.is_visible(alice, "/workspace/proj/a.txt", zone)

        # Invalidate
        namespace_manager.invalidate(alice)

        # Mount table cache should be empty for this subject
        with namespace_manager._lock:
            assert namespace_manager._cache.get(alice) is None
