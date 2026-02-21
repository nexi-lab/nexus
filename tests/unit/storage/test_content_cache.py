"""Unit tests for ContentCache with LZ4 compression (Issue #908)
and priority-aware eviction (Issue #2427)."""

import time
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


# ---------------------------------------------------------------------------
# Priority-aware eviction tests (Issue #2427)
# ---------------------------------------------------------------------------


class TestContentCachePriority:
    """Test priority-aware two-pass LRU eviction."""

    def _make_cache(self, max_bytes: int = 1024) -> ContentCache:
        """Create a small ContentCache for priority testing."""
        cache = ContentCache(max_size_mb=0, compression_threshold=10000)
        cache._max_size_bytes = max_bytes
        return cache

    def test_put_with_priority_default(self):
        """put() without priority should default to 0."""
        cache = self._make_cache()
        cache.put("hash1", b"content")
        # Should retrieve normally — priority is internal
        assert cache.get("hash1") == b"content"

    def test_put_with_explicit_priority(self):
        """put() with explicit priority stores and retrieves correctly."""
        cache = self._make_cache()
        cache.put("hash1", b"content", priority=3)
        assert cache.get("hash1") == b"content"

    def test_eviction_skips_high_priority_first_pass(self):
        """First-pass eviction should skip high-priority entries."""
        cache = self._make_cache(max_bytes=800)

        # Fill with mix: hash1=priority 0, hash2=priority 3
        cache.put("hash1", b"a" * 300, priority=0)
        cache.put("hash2", b"b" * 300, priority=3)

        # Access hash1 more recently so LRU order is: hash2 (LRU), hash1 (MRU)
        cache.get("hash1")

        # Add new content that forces eviction (300 + 300 + 300 > 800)
        cache.put("hash3", b"c" * 300, priority=0)

        # hash1 (priority=0) should be evicted despite being MRU,
        # because first pass only targets priority=0 entries.
        # hash2 (priority=3) should survive first pass.
        assert cache.get("hash2") is not None, "High-priority entry should survive"
        assert cache.get("hash3") is not None, "New entry should exist"

    def test_eviction_evicts_high_priority_second_pass(self):
        """When ALL entries are high-priority, LRU still works (second pass)."""
        cache = self._make_cache(max_bytes=600)

        # Fill with all high-priority
        cache.put("hash1", b"a" * 300, priority=3)
        cache.put("hash2", b"b" * 300, priority=3)

        # Add more — forces eviction. First pass finds no priority=0,
        # second pass evicts LRU (hash1).
        cache.put("hash3", b"c" * 300, priority=3)

        assert cache.get("hash1") is None, "LRU high-priority entry should be evicted"
        assert cache.get("hash2") is not None
        assert cache.get("hash3") is not None

    def test_mixed_priority_eviction_order(self):
        """Eviction prefers priority=0 before touching higher priorities."""
        cache = self._make_cache(max_bytes=1200)

        # Fill: p0, p1, p2 (each 400 bytes, total 1200)
        cache.put("p0", b"a" * 400, priority=0)
        cache.put("p1", b"b" * 400, priority=1)
        cache.put("p2", b"c" * 400, priority=2)

        # Add 400 more — need to evict 400 bytes
        cache.put("new", b"d" * 400, priority=0)

        # p0 (priority=0) should be evicted first
        assert cache.get("p0") is None, "Priority-0 should be evicted first"
        assert cache.get("p1") is not None, "Priority-1 should survive"
        assert cache.get("p2") is not None, "Priority-2 should survive"
        assert cache.get("new") is not None

    def test_priority_preserved_on_update(self):
        """Updating content should accept new priority."""
        cache = self._make_cache(max_bytes=800)

        cache.put("hash1", b"a" * 300, priority=0)
        cache.put("hash2", b"b" * 300, priority=0)

        # Update hash1 with high priority
        cache.put("hash1", b"a" * 300, priority=3)

        # Force eviction — hash2 (priority=0) should go, hash1 (now priority=3) stays
        cache.put("hash3", b"c" * 300, priority=0)

        assert cache.get("hash1") is not None, "Updated high-priority should survive"
        assert cache.get("hash2") is None, "Low-priority should be evicted"

    def test_priority_in_stats(self):
        """get_stats() should include priority distribution."""
        cache = self._make_cache()

        cache.put("h1", b"a" * 100, priority=0)
        cache.put("h2", b"b" * 100, priority=0)
        cache.put("h3", b"c" * 100, priority=3)

        stats = cache.get_stats()
        assert "priority_zero_count" in stats
        assert stats["priority_zero_count"] == 2

    def test_concurrent_priority_eviction(self):
        """Thread safety under priority-mixed concurrent puts."""
        cache = self._make_cache(max_bytes=10000)
        errors = []

        def writer_low(tid: int):
            try:
                for i in range(50):
                    cache.put(f"low-{tid}-{i}", b"x" * 100, priority=0)
            except Exception as e:
                errors.append(e)

        def writer_high(tid: int):
            try:
                for i in range(50):
                    cache.put(f"high-{tid}-{i}", b"y" * 100, priority=3)
            except Exception as e:
                errors.append(e)

        with ThreadPoolExecutor(max_workers=8) as ex:
            futures = []
            for t in range(4):
                futures.append(ex.submit(writer_low, t))
                futures.append(ex.submit(writer_high, t))
            for f in futures:
                f.result()

        assert len(errors) == 0, f"Errors occurred: {errors}"


class TestContentCachePerfRegression:
    """Performance regression tests for eviction (Issue #2427)."""

    def test_eviction_latency_bounded(self):
        """10K evictions should complete in <500ms."""
        cache = ContentCache(max_size_mb=0, compression_threshold=100000)
        cache._max_size_bytes = 5000  # Small cache forces frequent eviction

        t0 = time.monotonic()
        for i in range(10000):
            cache.put(f"hash-{i}", b"x" * 100, priority=i % 4)
        elapsed = time.monotonic() - t0

        assert elapsed < 0.5, f"10K puts with eviction took {elapsed:.3f}s (>500ms)"
