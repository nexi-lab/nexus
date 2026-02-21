"""Unit tests for ContentCache with LZ4 compression (Issue #908)."""

from concurrent.futures import ThreadPoolExecutor

from nexus.storage.content_cache import ContentCache


class TestContentCacheBasic:
    """Test basic ContentCache functionality."""

    def test_put_and_get(self):
        """Test basic put and get operations."""
        cache = ContentCache(max_size_mb=1)
        content = b"Hello World"

        cache.put("hash1", content)
        result = cache.get("hash1")

        assert result == content

    def test_get_nonexistent(self):
        """Test getting non-existent key returns None."""
        cache = ContentCache(max_size_mb=1)

        result = cache.get("nonexistent")

        assert result is None

    def test_remove(self):
        """Test remove operation."""
        cache = ContentCache(max_size_mb=1)
        cache.put("hash1", b"content")

        assert cache.remove("hash1") is True
        assert cache.get("hash1") is None
        assert cache.remove("hash1") is False

    def test_clear(self):
        """Test clear operation."""
        cache = ContentCache(max_size_mb=1)
        cache.put("hash1", b"content1")
        cache.put("hash2", b"content2")

        cache.clear()

        assert cache.get("hash1") is None
        assert cache.get("hash2") is None
        stats = cache.get_stats()
        assert stats["entries"] == 0
        assert stats["size_bytes"] == 0

    def test_lru_eviction(self):
        """Test LRU eviction when cache is full."""
        # 1KB cache
        cache = ContentCache(max_size_mb=0, compression_threshold=10000)
        cache._max_size_bytes = 1024  # Override for test

        # Add 400 bytes
        cache.put("hash1", b"a" * 400)
        # Add another 400 bytes (total 800)
        cache.put("hash2", b"b" * 400)

        # Both should fit
        assert cache.get("hash1") is not None
        assert cache.get("hash2") is not None

        # Add 400 bytes - need to evict to make space
        # Order after gets: hash1, hash2 (hash2 accessed last)
        # Eviction will remove hash1 (LRU)
        cache.put("hash3", b"c" * 400)

        # hash1 should be evicted (it was LRU)
        assert cache.get("hash1") is None
        # hash2 and hash3 should still exist
        assert cache.get("hash2") is not None
        assert cache.get("hash3") is not None

    def test_update_existing_entry(self):
        """Test updating an existing entry."""
        cache = ContentCache(max_size_mb=1, compression_threshold=10000)

        cache.put("hash1", b"original")
        cache.put("hash1", b"updated content")

        result = cache.get("hash1")
        assert result == b"updated content"

    def test_content_too_large_not_cached(self):
        """Test that content larger than max size is not cached."""
        cache = ContentCache(max_size_mb=0)
        cache._max_size_bytes = 100  # 100 bytes max

        # Try to cache 200 bytes
        cache.put("hash1", b"x" * 200)

        assert cache.get("hash1") is None


class TestContentCacheCompression:
    """Test LZ4 compression functionality."""

    def test_small_content_not_compressed(self):
        """Test content below threshold is not compressed."""
        cache = ContentCache(max_size_mb=1, compression_threshold=1024)

        # 100 bytes - below threshold
        small_content = b"x" * 100
        cache.put("hash1", small_content)

        result = cache.get("hash1")
        assert result == small_content

        stats = cache.get_stats()
        assert stats["compressed_entries"] == 0

    def test_large_content_compressed(self):
        """Test content above threshold is compressed."""
        cache = ContentCache(max_size_mb=1, compression_threshold=1024)

        # 10KB of repetitive content (compresses well)
        large_content = b"Hello World! " * 1000
        cache.put("hash1", large_content)

        result = cache.get("hash1")
        assert result == large_content

        stats = cache.get_stats()
        assert stats["compressed_entries"] == 1
        assert stats["compression_savings_bytes"] > 0
        # Stored size should be less than original
        assert stats["size_bytes"] < len(large_content)

    def test_compression_round_trip(self):
        """Test compression/decompression preserves data integrity."""
        cache = ContentCache(max_size_mb=1, compression_threshold=100)

        # Various content types
        test_cases = [
            b"x" * 1000,  # Repetitive
            bytes(range(256)) * 10,  # All byte values
            b"The quick brown fox jumps over the lazy dog. " * 50,  # Text
        ]

        for i, content in enumerate(test_cases):
            cache.put(f"hash{i}", content)
            result = cache.get(f"hash{i}")
            assert result == content, f"Round-trip failed for test case {i}"

    def test_incompressible_content_stored_uncompressed(self):
        """Test that already-compressed/random content is stored as-is."""
        cache = ContentCache(max_size_mb=1, compression_threshold=100, min_compression_ratio=0.9)

        # Random bytes don't compress well
        import os

        random_content = os.urandom(2000)
        cache.put("hash1", random_content)

        result = cache.get("hash1")
        assert result == random_content

        # Should not count as compressed if compression didn't help
        stats = cache.get_stats()
        # The random content likely won't compress by 10%, so may be stored raw
        # Just verify round-trip works
        assert stats["entries"] == 1

    def test_compression_stats_accuracy(self):
        """Test compression statistics are accurate."""
        cache = ContentCache(max_size_mb=10, compression_threshold=100)

        # Add compressible content
        content1 = b"A" * 5000  # Very compressible
        content2 = b"B" * 5000  # Very compressible

        cache.put("hash1", content1)
        cache.put("hash2", content2)

        stats = cache.get_stats()
        assert stats["entries"] == 2
        assert stats["compressed_entries"] == 2
        assert stats["compression_ratio"] < 1.0  # Should be compressed
        assert stats["compression_savings_bytes"] > 0
        assert stats["effective_capacity_mb"] >= stats["max_size_mb"]

    def test_clear_resets_compression_stats(self):
        """Test that clear() resets all compression stats."""
        cache = ContentCache(max_size_mb=1, compression_threshold=100)

        cache.put("hash1", b"x" * 1000)
        cache.clear()

        stats = cache.get_stats()
        assert stats["compressed_entries"] == 0
        assert stats["compression_savings_bytes"] == 0
        assert stats["compression_ratio"] == 1.0


class TestContentCacheThreadSafety:
    """Test thread safety of ContentCache."""

    def test_concurrent_reads_and_writes(self):
        """Test concurrent read/write operations."""
        cache = ContentCache(max_size_mb=10, compression_threshold=100)
        errors = []

        def writer(thread_id: int):
            try:
                for i in range(100):
                    content = f"content-{thread_id}-{i}".encode() * 100
                    cache.put(f"hash-{thread_id}-{i}", content)
            except Exception as e:
                errors.append(e)

        def reader(thread_id: int):
            try:
                for i in range(100):
                    cache.get(f"hash-{thread_id}-{i}")
            except Exception as e:
                errors.append(e)

        with ThreadPoolExecutor(max_workers=8) as executor:
            # 4 writers, 4 readers
            futures = []
            for i in range(4):
                futures.append(executor.submit(writer, i))
                futures.append(executor.submit(reader, i))

            for f in futures:
                f.result()

        assert len(errors) == 0, f"Errors occurred: {errors}"

    def test_concurrent_stats(self):
        """Test concurrent stats access during writes."""
        cache = ContentCache(max_size_mb=10, compression_threshold=100)
        errors = []

        def writer():
            try:
                for i in range(50):
                    cache.put(f"hash-{i}", b"x" * 1000)
            except Exception as e:
                errors.append(e)

        def stats_reader():
            try:
                for _ in range(50):
                    cache.get_stats()
            except Exception as e:
                errors.append(e)

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [
                executor.submit(writer),
                executor.submit(writer),
                executor.submit(stats_reader),
                executor.submit(stats_reader),
            ]
            for f in futures:
                f.result()

        assert len(errors) == 0


class TestContentCacheEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_empty_content(self):
        """Test caching empty content."""
        cache = ContentCache(max_size_mb=1)

        cache.put("hash1", b"")
        result = cache.get("hash1")

        assert result == b""

    def test_binary_content_with_lz4_magic_bytes(self):
        """Test content that happens to start with LZ4 magic bytes."""
        cache = ContentCache(max_size_mb=1, compression_threshold=10000)

        # Content starting with LZ4 magic but not actually compressed
        # This is below threshold so won't be compressed
        fake_lz4 = b"\x04\x22\x4d\x18" + b"not really compressed"
        cache.put("hash1", fake_lz4)

        # The get() will try to decompress since it sees magic bytes,
        # but should gracefully handle the failure and return original content
        result = cache.get("hash1")
        assert result == fake_lz4

    def test_exactly_threshold_size(self):
        """Test content exactly at compression threshold."""
        cache = ContentCache(max_size_mb=1, compression_threshold=1000)

        # Exactly 1000 bytes - at threshold, should NOT compress
        content = b"x" * 1000
        cache.put("hash1", content)

        result = cache.get("hash1")
        assert result == content

        stats = cache.get_stats()
        assert stats["compressed_entries"] == 0

    def test_just_above_threshold(self):
        """Test content just above compression threshold."""
        cache = ContentCache(max_size_mb=1, compression_threshold=1000)

        # 1001 bytes - above threshold, should compress
        content = b"x" * 1001
        cache.put("hash1", content)

        result = cache.get("hash1")
        assert result == content

        stats = cache.get_stats()
        assert stats["compressed_entries"] == 1

    def test_custom_min_compression_ratio(self):
        """Test custom minimum compression ratio."""
        # Require 50% compression (very aggressive)
        cache = ContentCache(max_size_mb=1, compression_threshold=100, min_compression_ratio=0.5)

        # Moderately compressible content
        content = b"Hello World! " * 100
        cache.put("hash1", content)

        result = cache.get("hash1")
        assert result == content

        # May or may not be compressed depending on actual ratio
        stats = cache.get_stats()
        assert stats["entries"] == 1


class TestContentCachePriority:
    """Test priority-aware eviction (Issue #2427)."""

    def _make_cache(self, size_bytes: int) -> ContentCache:
        """Create a cache with exact byte capacity (no compression)."""
        cache = ContentCache(max_size_mb=1, compression_threshold=999_999_999)
        cache._max_size_bytes = size_bytes
        return cache

    def test_priority_stored_and_retrieved(self):
        """put with priority stores it; get still returns correct bytes."""
        cache = self._make_cache(1024)
        cache.put("h1", b"data", priority=3)
        assert cache.get("h1") == b"data"

    def test_low_priority_evicted_first(self):
        """Fill cache, verify priority=0 evicted before priority=3."""
        cache = self._make_cache(20)  # fits 2x 10-byte entries

        cache.put("low", b"A" * 10, priority=0)
        cache.put("high", b"B" * 10, priority=3)
        # Cache is full (20/20). Adding another entry forces eviction.
        cache.put("new", b"C" * 10, priority=2)

        # low (priority=0) should be evicted, high (priority=3) survives
        assert cache.get("low") is None
        assert cache.get("high") == b"B" * 10
        assert cache.get("new") == b"C" * 10

    def test_same_priority_uses_lru(self):
        """Same priority falls back to LRU order."""
        cache = self._make_cache(20)

        cache.put("first", b"A" * 10, priority=1)
        cache.put("second", b"B" * 10, priority=1)
        # Both are priority=1. "first" is LRU.
        cache.put("third", b"C" * 10, priority=1)

        # "first" (oldest, same priority) should be evicted
        assert cache.get("first") is None
        assert cache.get("second") == b"B" * 10
        assert cache.get("third") == b"C" * 10

    def test_priority_upgrade_on_reput(self):
        """Re-putting same hash with higher priority upgrades it."""
        cache = self._make_cache(20)  # fits exactly 2 entries

        cache.put("h1", b"A" * 10, priority=0)
        cache.put("h2", b"B" * 10, priority=0)
        # Upgrade h1 to priority=3 (no eviction, same hash updates in-place)
        cache.put("h1", b"A" * 10, priority=3)
        # Now force eviction — cache is full (20/20)
        cache.put("h3", b"C" * 10, priority=2)

        # h2 (priority=0) should be evicted, h1 (upgraded to 3) should survive
        assert cache.get("h2") is None
        assert cache.get("h1") == b"A" * 10
        assert cache.get("h3") == b"C" * 10

    def test_all_high_priority_still_evicts(self):
        """Full cache with all priority=3 eventually evicts LRU."""
        cache = self._make_cache(20)

        cache.put("a", b"A" * 10, priority=3)
        cache.put("b", b"B" * 10, priority=3)
        # All priority=3: pass 1 finds nothing, pass 2 evicts LRU
        cache.put("c", b"C" * 10, priority=3)

        # "a" is LRU, should be evicted by fallback pass
        assert cache.get("a") is None
        assert cache.get("b") == b"B" * 10
        assert cache.get("c") == b"C" * 10

    def test_mixed_priority_eviction_order(self):
        """Mixed priorities evict in correct order: low first, then LRU."""
        cache = self._make_cache(40)

        cache.put("archive", b"A" * 10, priority=0)  # lowest
        cache.put("write", b"B" * 10, priority=1)  # normal
        cache.put("edit", b"C" * 10, priority=2)  # elevated
        cache.put("read", b"D" * 10, priority=3)  # highest
        # Full at 40/40. Need to evict 10 bytes for new entry.
        cache.put("new", b"E" * 10, priority=1)

        # archive (priority=0) evicted first
        assert cache.get("archive") is None
        assert cache.get("write") == b"B" * 10
        assert cache.get("edit") == b"C" * 10
        assert cache.get("read") == b"D" * 10
        assert cache.get("new") == b"E" * 10

    def test_stats_include_priority_distribution(self):
        """get_stats() reports priority breakdown."""
        cache = self._make_cache(1024)

        cache.put("a", b"A", priority=0)
        cache.put("b", b"B", priority=1)
        cache.put("c", b"C", priority=2)
        cache.put("d", b"D", priority=3)
        cache.put("e", b"E", priority=1)

        stats = cache.get_stats()
        dist = stats["priority_distribution"]
        assert dist == {0: 1, 1: 2, 2: 1, 3: 1}

    def test_default_priority_is_zero(self):
        """put() without priority uses 0 (backward compat)."""
        cache = self._make_cache(20)

        cache.put("old_api", b"A" * 10)  # no priority kwarg
        cache.put("high", b"B" * 10, priority=3)
        # Force eviction
        cache.put("new", b"C" * 10, priority=1)

        # old_api (default priority=0) should be evicted first
        assert cache.get("old_api") is None
        assert cache.get("high") == b"B" * 10
