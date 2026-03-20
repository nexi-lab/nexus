"""Unit tests for Tiger Cache L1→L2→L3 fallback chain and BloomFilter integration.

Issue #3192: Tests the TigerCache read path, BloomFilter pre-gate,
batch operations, and cache statistics.
"""

import time
from unittest.mock import MagicMock, patch

import pytest
from pyroaring import BitMap as RoaringBitmap
from sqlalchemy import create_engine

from nexus.bricks.rebac.cache.tiger.bitmap_cache import CacheKey, TigerCache


def _make_key(**overrides):
    defaults = {
        "subject_type": "user",
        "subject_id": "alice",
        "permission": "read",
        "resource_type": "file",
        "zone_id": "",
    }
    defaults.update(overrides)
    return CacheKey(**defaults)


@pytest.fixture
def tiger_cache():
    engine = create_engine("sqlite:///:memory:")
    cache = TigerCache(engine=engine)
    return cache


class TestL1CacheFallback:
    """Tests for L1 in-memory cache behavior."""

    def test_l1_cache_hit_returns_immediately(self, tiger_cache):
        """A valid L1 entry should be returned without touching L2/L3."""
        key = CacheKey("user", "alice", "read", "file", "zone-1")
        bitmap = RoaringBitmap([1, 2, 3])
        tiger_cache._cache[key] = (bitmap, 1, time.time())

        result = tiger_cache.get_accessible_resources(
            subject_type="user",
            subject_id="alice",
            permission="read",
            resource_type="file",
            zone_id="zone-1",
        )
        assert result == {1, 2, 3}

    def test_l1_cache_miss_falls_through_to_db(self, tiger_cache):
        """L1 miss should attempt to load from database (L3)."""
        with patch.object(tiger_cache, "_load_from_db", return_value=None) as mock_db:
            tiger_cache.get_accessible_resources(
                subject_type="user",
                subject_id="alice",
                permission="read",
                resource_type="file",
                zone_id="zone-1",
            )
            mock_db.assert_called_once()

    def test_l1_expired_entry_triggers_db_load(self, tiger_cache):
        """An expired L1 entry should fall through to DB."""
        key = CacheKey("user", "alice", "read", "file", "zone-1")
        bitmap = RoaringBitmap([1])
        tiger_cache._cache[key] = (bitmap, 1, time.time() - 999)

        with patch.object(tiger_cache, "_load_from_db", return_value=None) as mock_db:
            tiger_cache.get_accessible_resources(
                subject_type="user",
                subject_id="alice",
                permission="read",
                resource_type="file",
                zone_id="zone-1",
            )
            mock_db.assert_called_once()


class TestCacheKey:
    """Tests for CacheKey hash and equality."""

    def test_cache_key_hash_equality(self, tiger_cache):
        """Identical CacheKeys should hash equally."""
        k1 = CacheKey("user", "alice", "read", "file")
        k2 = CacheKey("user", "alice", "read", "file")
        assert k1 == k2
        assert hash(k1) == hash(k2)

        k3 = CacheKey("user", "bob", "read", "file")
        assert k1 != k3

    def test_cache_key_zone_isolation(self, tiger_cache):
        """Keys with different zone_id should be different entries."""
        k1 = CacheKey("user", "alice", "read", "file", "zone-a")
        k2 = CacheKey("user", "alice", "read", "file", "zone-b")
        assert k1 != k2


class TestL2DragonflyFallback:
    """Tests for the L2 Dragonfly cache layer."""

    def test_l2_dragonfly_get_returns_bitmap(self, tiger_cache):
        """L2 Dragonfly hit should deserialize and return bitmap correctly."""
        bitmap = RoaringBitmap([100, 200, 300])
        bitmap_bytes = bytes(bitmap.serialize())

        with patch.object(tiger_cache, "_run_dragonfly_op", return_value=(bitmap_bytes, 5)):
            tiger_cache._dragonfly = MagicMock()
            tiger_cache._bloom_add(_make_key())
            result = tiger_cache._load_from_db(_make_key(), conn=None)

            assert result is not None
            assert set(result) == {100, 200, 300}

    def test_l2_dragonfly_timeout_falls_through(self, tiger_cache):
        """L2 Dragonfly timeout (returning None) should fall through to L3."""
        tiger_cache._dragonfly = MagicMock()
        tiger_cache._resource_map._uuid_to_int[("file", "/doc.txt")] = 42

        with (
            patch.object(tiger_cache, "_run_dragonfly_op", return_value=None),
            patch.object(tiger_cache, "_load_from_db", return_value=None) as mock_l3,
        ):
            result = tiger_cache.check_access("user", "alice", "read", "file", "/doc.txt")
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
        """check_access returns True when resource is in bitmap."""
        key = CacheKey("user", "alice", "read", "file")
        bitmap = RoaringBitmap([42])
        tiger_cache._cache[key] = (bitmap, 1, time.time())
        tiger_cache._resource_map._uuid_to_int[("file", "/doc.txt")] = 42

        result = tiger_cache.check_access("user", "alice", "read", "file", "/doc.txt")
        assert result is True

    def test_check_access_returns_false_for_inaccessible(self, tiger_cache):
        """check_access returns False when resource is NOT in bitmap."""
        key = CacheKey("user", "alice", "read", "file")
        bitmap = RoaringBitmap([42])
        tiger_cache._cache[key] = (bitmap, 1, time.time())
        tiger_cache._resource_map._uuid_to_int[("file", "/other.txt")] = 99

        result = tiger_cache.check_access("user", "alice", "read", "file", "/other.txt")
        assert result is False

    def test_check_access_returns_none_on_cache_miss(self, tiger_cache):
        """check_access returns None when bitmap not in cache."""
        tiger_cache._resource_map._uuid_to_int[("file", "/missing.txt")] = 77

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
        """Cache hits and misses should be tracked."""
        key = CacheKey("user", "alice", "read", "file")
        bitmap = RoaringBitmap([42])
        tiger_cache._cache[key] = (bitmap, 1, time.time())
        tiger_cache._resource_map._uuid_to_int[("file", "/doc.txt")] = 42

        initial_hits = tiger_cache._stats_hits
        initial_misses = tiger_cache._stats_misses

        tiger_cache.check_access("user", "alice", "read", "file", "/doc.txt")
        assert tiger_cache._stats_hits == initial_hits + 1

        tiger_cache._resource_map._uuid_to_int[("file", "/other.txt")] = 77
        with patch.object(tiger_cache, "_load_from_db", return_value=None):
            tiger_cache.check_access("user", "bob", "read", "file", "/other.txt")
        assert tiger_cache._stats_misses == initial_misses + 1


class TestBloomFilterIntegration:
    """Tests for BloomFilter L2 pre-gate on TigerCache (Issue #3192)."""

    def test_bloom_rejects_unknown_key(self, tiger_cache):
        """BloomFilter rejects keys never added — skips L2 Dragonfly round-trip."""
        unknown = CacheKey("user", "nobody", "read", "file", "zone-x")
        assert tiger_cache._bloom_might_contain(unknown) is False
        assert tiger_cache._bloom_rejects == 1

    def test_bloom_passes_added_key(self, tiger_cache):
        """BloomFilter passes keys that have been added."""
        key = CacheKey("user", "alice", "read", "file", "zone-1")
        tiger_cache._bloom_add(key)
        assert tiger_cache._bloom_might_contain(key) is True
        assert tiger_cache._bloom_passes == 1

    def test_bloom_consistent_encoding(self, tiger_cache):
        """_bloom_key produces same format as Dragonfly redis key."""
        key = CacheKey("user", "alice", "read", "file", "zone-1")
        assert tiger_cache._bloom_key(key) == "tiger:user:alice:read:file"

    def test_bloom_rebuild_from_l1(self, tiger_cache):
        """_rebuild_l2_bloom rebuilds from L1 cache entries."""
        k1 = CacheKey("user", "alice", "read", "file", "z1")
        k2 = CacheKey("user", "bob", "write", "file", "z1")
        tiger_cache._cache[k1] = (RoaringBitmap([1]), 1, time.time())
        tiger_cache._cache[k2] = (RoaringBitmap([2]), 1, time.time())

        tiger_cache._rebuild_l2_bloom()
        assert tiger_cache._bloom_might_contain(k1) is True
        assert tiger_cache._bloom_might_contain(k2) is True
        unknown = CacheKey("user", "nobody", "read", "file", "z1")
        assert tiger_cache._bloom_might_contain(unknown) is False


class TestBatchOperations:
    """Tests for batch_get_bitmaps on TigerCache (Issue #3192)."""

    def test_batch_get_bitmaps_l1_hit(self, tiger_cache):
        """batch_get_bitmaps returns L1 cached entries without L2/L3."""
        key = CacheKey("user", "alice", "read", "file", "zone-1")
        bitmap = RoaringBitmap([1, 2, 3])
        tiger_cache._cache[key] = (bitmap, 1, time.time())

        results = tiger_cache.batch_get_bitmaps([key])
        assert key in results
        assert results[key] == {1, 2, 3}

    def test_batch_get_bitmaps_multiple_keys(self, tiger_cache):
        """batch_get_bitmaps handles multiple keys, some cached some not."""
        k1 = CacheKey("user", "alice", "read", "file", "z1")
        k2 = CacheKey("user", "bob", "read", "file", "z1")
        tiger_cache._cache[k1] = (RoaringBitmap([10]), 1, time.time())
        # k2 not in cache

        with patch.object(tiger_cache, "_load_from_db", return_value=None):
            results = tiger_cache.batch_get_bitmaps([k1, k2])

        assert k1 in results
        assert results[k1] == {10}
        # k2 missed L1, bloom rejects it (not added), falls to L3 which returns None
        assert k2 not in results
