"""Unit tests for ReBAC iterator cache implementation."""

import time

import pytest

from nexus.services.permissions.rebac_iterator_cache import (
    CachedResult,
    CursorExpiredError,
    IteratorCache,
)


class TestIteratorCache:
    """Test suite for iterator caching for paginated list operations."""

    def test_cache_basic_operations(self):
        """Test basic get_or_create and get_page operations."""
        cache = IteratorCache(max_size=100, ttl_seconds=60)

        # First call should compute results
        cursor_id, results, total = cache.get_or_create(
            query_hash="test:query",
            zone_id="default",
            compute_fn=lambda: [{"id": i} for i in range(10)],
        )

        assert cursor_id is not None
        assert len(results) == 10
        assert total == 10

        # Second call with same query should return cached results
        cursor_id2, results2, total2 = cache.get_or_create(
            query_hash="test:query",
            zone_id="default",
            compute_fn=lambda: [{"id": i} for i in range(20)],  # Different results
        )

        assert cursor_id2 == cursor_id  # Same cursor
        assert len(results2) == 10  # Original results
        assert total2 == 10

    def test_cache_pagination(self):
        """Test pagination through cached results."""
        cache = IteratorCache(max_size=100, ttl_seconds=60)

        # Create cached results
        cursor_id, results, total = cache.get_or_create(
            query_hash="test:query",
            zone_id="default",
            compute_fn=lambda: [{"id": i} for i in range(100)],
        )

        # Get first page
        items, next_cursor, page_total = cache.get_page(cursor_id, offset=0, limit=10)
        assert len(items) == 10
        assert items[0]["id"] == 0
        assert items[9]["id"] == 9
        assert next_cursor == cursor_id
        assert page_total == 100

        # Get second page
        items, next_cursor, page_total = cache.get_page(cursor_id, offset=10, limit=10)
        assert len(items) == 10
        assert items[0]["id"] == 10
        assert items[9]["id"] == 19
        assert next_cursor == cursor_id

        # Get last page
        items, next_cursor, page_total = cache.get_page(cursor_id, offset=90, limit=10)
        assert len(items) == 10
        assert items[0]["id"] == 90
        assert items[9]["id"] == 99
        assert next_cursor is None  # No more pages

    def test_cache_ttl_expiration(self):
        """Test that cache entries expire after TTL."""
        cache = IteratorCache(max_size=100, ttl_seconds=1)  # 1 second TTL

        cursor_id, results, total = cache.get_or_create(
            query_hash="test:query",
            zone_id="default",
            compute_fn=lambda: [{"id": i} for i in range(10)],
        )

        # Should work immediately
        items, _, _ = cache.get_page(cursor_id, offset=0, limit=5)
        assert len(items) == 5

        # Wait for expiration
        time.sleep(1.5)

        # Should raise CursorExpiredError
        with pytest.raises(CursorExpiredError):
            cache.get_page(cursor_id, offset=0, limit=5)

    def test_cache_invalidate_zone(self):
        """Test invalidating all entries for a zone."""
        cache = IteratorCache(max_size=100, ttl_seconds=60)

        # Create entries for different zones
        cursor1, _, _ = cache.get_or_create(
            query_hash="query1",
            zone_id="zone1",
            compute_fn=lambda: [{"id": 1}],
        )
        cursor2, _, _ = cache.get_or_create(
            query_hash="query2",
            zone_id="zone1",
            compute_fn=lambda: [{"id": 2}],
        )
        cursor3, _, _ = cache.get_or_create(
            query_hash="query3",
            zone_id="zone2",
            compute_fn=lambda: [{"id": 3}],
        )

        # Invalidate zone1
        count = cache.invalidate_zone("zone1")
        assert count == 2

        # zone1 cursors should be expired
        with pytest.raises(CursorExpiredError):
            cache.get_page(cursor1, offset=0, limit=10)
        with pytest.raises(CursorExpiredError):
            cache.get_page(cursor2, offset=0, limit=10)

        # zone2 cursor should still work
        items, _, _ = cache.get_page(cursor3, offset=0, limit=10)
        assert len(items) == 1

    def test_cache_invalidate_cursor(self):
        """Test invalidating a specific cursor."""
        cache = IteratorCache(max_size=100, ttl_seconds=60)

        cursor_id, _, _ = cache.get_or_create(
            query_hash="test:query",
            zone_id="default",
            compute_fn=lambda: [{"id": i} for i in range(10)],
        )

        # Verify it works
        items, _, _ = cache.get_page(cursor_id, offset=0, limit=5)
        assert len(items) == 5

        # Invalidate
        result = cache.invalidate_cursor(cursor_id)
        assert result is True

        # Should be expired now
        with pytest.raises(CursorExpiredError):
            cache.get_page(cursor_id, offset=0, limit=5)

        # Invalidating again should return False
        result = cache.invalidate_cursor(cursor_id)
        assert result is False

    def test_cache_metrics(self):
        """Test cache metrics tracking."""
        cache = IteratorCache(max_size=100, ttl_seconds=60, enable_metrics=True)

        # First call - miss
        cursor_id, _, _ = cache.get_or_create(
            query_hash="test:query",
            zone_id="default",
            compute_fn=lambda: [{"id": i} for i in range(10)],
        )

        # Second call with same query - hit
        cache.get_or_create(
            query_hash="test:query",
            zone_id="default",
            compute_fn=lambda: [],
        )

        # Get page - hit
        cache.get_page(cursor_id, offset=0, limit=5)

        stats = cache.get_stats()
        assert stats["hits"] == 2
        assert stats["misses"] == 1
        assert stats["current_size"] == 1
        assert stats["active_queries"] == 1
        assert stats["hit_rate_percent"] == pytest.approx(66.67, rel=0.01)

    def test_cache_query_deduplication(self):
        """Test that same query returns same cursor."""
        cache = IteratorCache(max_size=100, ttl_seconds=60)

        call_count = 0

        def compute():
            nonlocal call_count
            call_count += 1
            return [{"id": i} for i in range(10)]

        # First call
        cursor1, _, _ = cache.get_or_create(
            query_hash="same:query",
            zone_id="default",
            compute_fn=compute,
        )

        # Second call with same query
        cursor2, _, _ = cache.get_or_create(
            query_hash="same:query",
            zone_id="default",
            compute_fn=compute,
        )

        # Should be same cursor, compute_fn called only once
        assert cursor1 == cursor2
        assert call_count == 1

    def test_cache_different_queries(self):
        """Test that different queries create different cursors."""
        cache = IteratorCache(max_size=100, ttl_seconds=60)

        cursor1, _, _ = cache.get_or_create(
            query_hash="query1",
            zone_id="default",
            compute_fn=lambda: [{"id": 1}],
        )
        cursor2, _, _ = cache.get_or_create(
            query_hash="query2",
            zone_id="default",
            compute_fn=lambda: [{"id": 2}],
        )

        assert cursor1 != cursor2

        stats = cache.get_stats()
        assert stats["current_size"] == 2
        assert stats["active_queries"] == 2

    def test_cache_clear(self):
        """Test clearing all cache entries."""
        cache = IteratorCache(max_size=100, ttl_seconds=60)

        cursor1, _, _ = cache.get_or_create(
            query_hash="query1",
            zone_id="default",
            compute_fn=lambda: [{"id": 1}],
        )
        cursor2, _, _ = cache.get_or_create(
            query_hash="query2",
            zone_id="default",
            compute_fn=lambda: [{"id": 2}],
        )

        stats = cache.get_stats()
        assert stats["current_size"] == 2

        cache.clear()

        stats = cache.get_stats()
        assert stats["current_size"] == 0
        assert stats["active_queries"] == 0

        # Both cursors should be expired
        with pytest.raises(CursorExpiredError):
            cache.get_page(cursor1, offset=0, limit=10)
        with pytest.raises(CursorExpiredError):
            cache.get_page(cursor2, offset=0, limit=10)

    def test_cache_reset_stats(self):
        """Test resetting cache statistics."""
        cache = IteratorCache(max_size=100, ttl_seconds=60, enable_metrics=True)

        # Generate some metrics
        cursor_id, _, _ = cache.get_or_create(
            query_hash="test:query",
            zone_id="default",
            compute_fn=lambda: [{"id": i} for i in range(10)],
        )
        cache.get_page(cursor_id, offset=0, limit=5)
        cache.get_page(cursor_id, offset=5, limit=5)

        # Reset stats
        cache.reset_stats()

        stats = cache.get_stats()
        assert stats["hits"] == 0
        assert stats["misses"] == 0
        assert stats["expired_cursors"] == 0

        # Cache entries should still exist
        items, _, _ = cache.get_page(cursor_id, offset=0, limit=5)
        assert len(items) == 5

    def test_cache_empty_results(self):
        """Test caching empty result sets."""
        cache = IteratorCache(max_size=100, ttl_seconds=60)

        cursor_id, results, total = cache.get_or_create(
            query_hash="empty:query",
            zone_id="default",
            compute_fn=lambda: [],
        )

        assert results == []
        assert total == 0

        items, next_cursor, page_total = cache.get_page(cursor_id, offset=0, limit=10)
        assert items == []
        assert next_cursor is None
        assert page_total == 0

    def test_cache_partial_page(self):
        """Test requesting a page larger than remaining results."""
        cache = IteratorCache(max_size=100, ttl_seconds=60)

        cursor_id, _, _ = cache.get_or_create(
            query_hash="test:query",
            zone_id="default",
            compute_fn=lambda: [{"id": i} for i in range(15)],
        )

        # Request page of 10 starting at offset 10 (only 5 items left)
        items, next_cursor, page_total = cache.get_page(cursor_id, offset=10, limit=10)
        assert len(items) == 5
        assert items[0]["id"] == 10
        assert items[4]["id"] == 14
        assert next_cursor is None  # No more pages
        assert page_total == 15

    def test_cached_result_dataclass(self):
        """Test CachedResult dataclass properties."""
        cached = CachedResult(
            cursor_id="test-cursor",
            query_hash="test:hash",
            results=[1, 2, 3],
            total_count=3,
            created_at=time.time(),
            zone_id="default",
        )

        assert cached.cursor_id == "test-cursor"
        assert cached.query_hash == "test:hash"
        assert cached.results == [1, 2, 3]
        assert cached.total_count == 3
        assert cached.zone_id == "default"
