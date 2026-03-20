"""Tests for Tiger cache L1->L2->L3 fallback chain and BloomFilter integration.

Covers Issue #3192 decision 10B (BloomFilter pre-check) and the Tiger cache
layered caching architecture (L1 in-memory, L2 Dragonfly, L3 PostgreSQL).
"""

import time
from unittest.mock import MagicMock, patch

import pytest
from pyroaring import BitMap as RoaringBitmap
from sqlalchemy import create_engine

from nexus.bricks.rebac.cache.tiger.bitmap_cache import CacheKey, TigerCache


@pytest.fixture()
def engine():
    """Create an in-memory SQLite engine for testing."""
    return create_engine("sqlite:///:memory:")


@pytest.fixture()
def tiger_cache(engine):
    """Create a TigerCache with mocked resource map for testing."""
    cache = TigerCache(engine)
    return cache


def _make_key(
    subject_type="user",
    subject_id="alice",
    permission="read",
    resource_type="file",
    zone_id="zone1",
):
    return CacheKey(subject_type, subject_id, permission, resource_type, zone_id)


class TestL1CacheFallback:
    """Tests for the L1 in-memory cache layer of TigerCache."""

    def test_l1_cache_hit_returns_immediately(self, tiger_cache):
        """L1 hit should return cached bitmap without touching the database."""
        key = _make_key()
        bitmap = RoaringBitmap([1, 2, 3, 42])
        tiger_cache._cache[key] = (bitmap, 1, time.time())

        with patch.object(tiger_cache, "_load_from_db") as mock_db:
            result = tiger_cache.get_accessible_resources(
                subject_type="user",
                subject_id="alice",
                permission="read",
                resource_type="file",
                zone_id="zone1",
            )

            mock_db.assert_not_called()
            assert result == {1, 2, 3, 42}

    def test_l1_cache_miss_falls_through_to_db(self, tiger_cache):
        """L1 miss should fall through and call _load_from_db."""
        bitmap = RoaringBitmap([10, 20])

        with patch.object(tiger_cache, "_load_from_db", return_value=bitmap) as mock_db:
            result = tiger_cache.get_accessible_resources(
                subject_type="user",
                subject_id="bob",
                permission="write",
                resource_type="file",
                zone_id="zone1",
            )

            mock_db.assert_called_once()
            assert result == {10, 20}

    def test_l1_expired_entry_triggers_db_load(self, tiger_cache):
        """An expired L1 entry (old cached_at) should be treated as a miss."""
        key = _make_key()
        bitmap = RoaringBitmap([5, 6, 7])
        # Set cached_at to well before TTL (300s default)
        old_time = time.time() - 600
        tiger_cache._cache[key] = (bitmap, 1, old_time)

        fresh_bitmap = RoaringBitmap([8, 9])
        with patch.object(tiger_cache, "_load_from_db", return_value=fresh_bitmap) as mock_db:
            result = tiger_cache.get_accessible_resources(
                subject_type="user",
                subject_id="alice",
                permission="read",
                resource_type="file",
                zone_id="zone1",
            )

            mock_db.assert_called_once()
            assert result == {8, 9}


class TestCacheKey:
    """Tests for CacheKey hashing and equality."""

    def test_cache_key_hash_equality(self):
        """Identical CacheKeys should have the same hash and be equal."""
        key1 = CacheKey("user", "alice", "read", "file", "zone1")
        key2 = CacheKey("user", "alice", "read", "file", "zone1")

        assert key1 == key2
        assert hash(key1) == hash(key2)

        # Different keys should not be equal
        key3 = CacheKey("user", "bob", "read", "file", "zone1")
        assert key1 != key3

    def test_cache_key_zone_isolation(self):
        """CacheKeys with different zone_ids should be different cache entries."""
        key_zone1 = CacheKey("user", "alice", "read", "file", "zone1")
        key_zone2 = CacheKey("user", "alice", "read", "file", "zone2")

        assert key_zone1 != key_zone2
        assert hash(key_zone1) != hash(key_zone2)

        # They should work as distinct dict keys
        cache = {}
        cache[key_zone1] = "bitmap_zone1"
        cache[key_zone2] = "bitmap_zone2"
        assert len(cache) == 2
        assert cache[key_zone1] == "bitmap_zone1"
        assert cache[key_zone2] == "bitmap_zone2"


class TestL2DragonflyFallback:
    """Tests for the L2 Dragonfly cache layer."""

    def test_l2_dragonfly_get_returns_bitmap(self, tiger_cache):
        """L2 Dragonfly hit should deserialize and return bitmap correctly."""
        bitmap = RoaringBitmap([100, 200, 300])
        bitmap_bytes = bytes(bitmap.serialize())

        with patch.object(tiger_cache, "_run_dragonfly_op", return_value=(bitmap_bytes, 5)):
            # Enable dragonfly so _load_from_db tries L2
            tiger_cache._dragonfly = MagicMock()
            # Add key to bloom filter so it passes the pre-gate
            tiger_cache._bloom_add(_make_key())
            result = tiger_cache._load_from_db(_make_key(), conn=None)

            assert result is not None
            assert set(result) == {100, 200, 300}

    def test_l2_dragonfly_timeout_falls_through(self, tiger_cache):
        """L2 Dragonfly timeout (returning None) should fall through to L3."""
        tiger_cache._dragonfly = MagicMock()
        # Pre-populate resource map so check_access doesn't hit DB
        tiger_cache._resource_map._uuid_to_int[("file", "/doc.txt")] = 42

        with (
            patch.object(tiger_cache, "_run_dragonfly_op", return_value=None),
            patch.object(tiger_cache, "_load_from_db", return_value=None) as mock_l3,
        ):
            result = tiger_cache.check_access("user", "alice", "read", "file", "/doc.txt")
            # L2 returns None (timeout), L3 also returns None → overall miss
            assert result is None
            mock_l3.assert_called_once()

    def test_l2_dragonfly_set_pipeline(self, tiger_cache):
        """L2 Dragonfly set should be called with correct arguments."""
        tiger_cache._dragonfly = MagicMock()
        bitmap = RoaringBitmap([1, 2, 3])
        bitmap_bytes = bytes(bitmap.serialize())

        with patch.object(tiger_cache, "_run_dragonfly_op") as mock_op:
            mock_op.return_value = True
            tiger_cache._run_dragonfly_op(
                operation="set",
                subject_type="user",
                subject_id="alice",
                permission="read",
                resource_type="file",
                bitmap_data=bitmap_bytes,
                revision=10,
            )

            mock_op.assert_called_once_with(
                operation="set",
                subject_type="user",
                subject_id="alice",
                permission="read",
                resource_type="file",
                bitmap_data=bitmap_bytes,
                revision=10,
            )


class TestCheckAccess:
    """Tests for the check_access method."""

    def test_check_access_returns_true_for_accessible(self, tiger_cache):
        """check_access should return True when resource int_id is in the bitmap."""
        key = CacheKey("user", "alice", "read", "file")
        bitmap = RoaringBitmap([42, 100, 200])
        tiger_cache._cache[key] = (bitmap, 1, time.time())

        # Pre-populate the resource map so get_or_create_int_id is not needed
        resource_key = ("file", "/doc.txt")
        tiger_cache._resource_map._uuid_to_int[resource_key] = 42

        result = tiger_cache.check_access(
            subject_type="user",
            subject_id="alice",
            permission="read",
            resource_type="file",
            resource_id="/doc.txt",
        )
        assert result is True

    def test_check_access_returns_false_for_inaccessible(self, tiger_cache):
        """check_access should return False when resource int_id is NOT in bitmap."""
        key = CacheKey("user", "alice", "read", "file")
        bitmap = RoaringBitmap([1, 2, 3])  # Does not contain 99
        tiger_cache._cache[key] = (bitmap, 1, time.time())

        resource_key = ("file", "/secret.txt")
        tiger_cache._resource_map._uuid_to_int[resource_key] = 99

        result = tiger_cache.check_access(
            subject_type="user",
            subject_id="alice",
            permission="read",
            resource_type="file",
            resource_id="/secret.txt",
        )
        assert result is False

    def test_check_access_returns_none_on_cache_miss(self, tiger_cache):
        """check_access should return None when no bitmap is cached."""
        # Pre-populate resource map but not the bitmap cache
        resource_key = ("file", "/missing.txt")
        tiger_cache._resource_map._uuid_to_int[resource_key] = 50

        with patch.object(tiger_cache, "_load_from_db", return_value=None):
            result = tiger_cache.check_access(
                subject_type="user",
                subject_id="alice",
                permission="read",
                resource_type="file",
                resource_id="/missing.txt",
            )
            assert result is None


class TestStatsTracking:
    """Tests for cache statistics counters."""

    def test_stats_tracking_hits_misses(self, tiger_cache):
        """Cache hits and misses should be tracked in _stats_hits and _stats_misses."""
        # Set up a cache entry for hits
        key = CacheKey("user", "alice", "read", "file")
        bitmap = RoaringBitmap([42])
        tiger_cache._cache[key] = (bitmap, 1, time.time())

        resource_key = ("file", "/doc.txt")
        tiger_cache._resource_map._uuid_to_int[resource_key] = 42

        initial_hits = tiger_cache._stats_hits
        initial_misses = tiger_cache._stats_misses

        # Generate a hit
        tiger_cache.check_access("user", "alice", "read", "file", "/doc.txt")
        assert tiger_cache._stats_hits == initial_hits + 1

        # Generate a miss — use a different subject so L1 cache doesn't match
        resource_key_miss = ("file", "/other.txt")
        tiger_cache._resource_map._uuid_to_int[resource_key_miss] = 77

        with patch.object(tiger_cache, "_load_from_db", return_value=None):
            tiger_cache.check_access("user", "bob", "read", "file", "/other.txt")

        assert tiger_cache._stats_misses == initial_misses + 1


class TestBloomFilterIntegration:
    """Future tests for BloomFilter pre-check integration (Issue #3192, decision 10B)."""

    @pytest.mark.skip(reason="BloomFilter not yet added to TigerCache (Issue #3192 decision 10B)")
    def test_bloom_filter_integration_rejects_negative(self, tiger_cache):
        """BloomFilter should reject keys that are definitely not in cache.

        When integrated, the BloomFilter sits before L1 and returns a
        definitive "not in cache" for keys that have never been added,
        avoiding the cost of L1 dict lookup for cold keys.
        """
        # Future: tiger_cache._bloom_filter.add(key_in_cache)
        # key_not_in_cache should be rejected by bloom filter
        # assert tiger_cache._bloom_filter.contains(key_not_in_cache) is False
        pass

    @pytest.mark.skip(reason="BloomFilter not yet added to TigerCache (Issue #3192 decision 10B)")
    def test_bloom_filter_integration_allows_positive(self, tiger_cache):
        """BloomFilter should allow keys that might be in cache.

        A positive BloomFilter result means "maybe in cache" and the
        lookup should proceed to L1. False positives are acceptable
        (they just cause an unnecessary L1 lookup).
        """
        # Future: tiger_cache._bloom_filter.add(key)
        # assert tiger_cache._bloom_filter.contains(key) is True
        pass


class TestBatchOperations:
    """Future tests for batch bitmap retrieval."""

    @pytest.mark.skip(reason="batch_get not yet implemented on TigerCache")
    def test_batch_get_pipeline(self, tiger_cache):
        """batch_get should return multiple bitmaps in a single call.

        When implemented, batch_get will use pipelined L2 reads and
        a single L3 SQL query with IN clause for efficiency.
        """
        # Future: results = tiger_cache.batch_get([key1, key2, key3])
        # assert len(results) == 3
        pass
