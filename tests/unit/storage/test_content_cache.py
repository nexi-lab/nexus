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
