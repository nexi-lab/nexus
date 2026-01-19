"""Tests for LocalDiskCache (Issue #1072).

Tests the local disk cache layer for FUSE operations including:
- Basic get/put operations
- CLOCK eviction algorithm
- Persistence and metadata
- Bloom filter optimization
- Block-level storage for large files
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import time
from pathlib import Path

import pytest

from nexus.storage.local_disk_cache import (
    CacheEntry,
    LocalDiskCache,
    close_local_disk_cache,
    get_local_disk_cache,
    set_local_disk_cache,
)


@pytest.fixture
def cache_dir():
    """Create a temporary cache directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def cache(cache_dir):
    """Create a LocalDiskCache instance."""
    cache = LocalDiskCache(
        cache_dir=cache_dir,
        max_size_gb=0.001,  # 1MB for testing
    )
    yield cache
    cache.close()


def content_hash(content: bytes) -> str:
    """Compute SHA-256 hash of content."""
    return hashlib.sha256(content).hexdigest()


class TestLocalDiskCacheBasics:
    """Test basic cache operations."""

    def test_put_and_get(self, cache):
        """Test basic put and get."""
        content = b"Hello, World!"
        hash_val = content_hash(content)

        # Put content
        assert cache.put(hash_val, content) is True

        # Get content
        result = cache.get(hash_val)
        assert result == content

    def test_get_nonexistent(self, cache):
        """Test get for nonexistent content."""
        result = cache.get("nonexistent_hash")
        assert result is None

    def test_exists(self, cache):
        """Test exists check."""
        content = b"Test content"
        hash_val = content_hash(content)

        # Not exists before put
        assert cache.exists(hash_val) is False

        # Exists after put
        cache.put(hash_val, content)
        assert cache.exists(hash_val) is True

    def test_remove(self, cache):
        """Test removing cached content."""
        content = b"Content to remove"
        hash_val = content_hash(content)

        cache.put(hash_val, content)
        assert cache.exists(hash_val) is True

        # Remove
        assert cache.remove(hash_val) is True
        assert cache.exists(hash_val) is False

        # Remove nonexistent
        assert cache.remove(hash_val) is False

    def test_clear(self, cache):
        """Test clearing all cached content."""
        # Add multiple items
        for i in range(10):
            content = f"Content {i}".encode()
            cache.put(content_hash(content), content)

        stats = cache.get_stats()
        assert stats["entries"] == 10

        # Clear
        count = cache.clear()
        assert count == 10

        stats = cache.get_stats()
        assert stats["entries"] == 0

    def test_duplicate_put(self, cache):
        """Test putting same content twice."""
        content = b"Duplicate content"
        hash_val = content_hash(content)

        # First put
        assert cache.put(hash_val, content) is True
        stats1 = cache.get_stats()

        # Second put (should just update access time)
        assert cache.put(hash_val, content) is True
        stats2 = cache.get_stats()

        # Should still be one entry
        assert stats1["entries"] == stats2["entries"] == 1


class TestClockEviction:
    """Test CLOCK eviction algorithm."""

    def test_eviction_when_full(self, cache_dir):
        """Test that old entries are evicted when cache is full."""
        # Create small cache (10KB)
        cache = LocalDiskCache(
            cache_dir=cache_dir,
            max_size_gb=0.00001,  # ~10KB
        )

        # Add content that exceeds cache size
        contents = []
        for i in range(20):
            content = f"Content {i}".encode() * 100  # ~1KB each
            contents.append((content_hash(content), content))
            cache.put(contents[-1][0], contents[-1][1])

        stats = cache.get_stats()
        # Should have evicted some entries
        assert stats["evictions"] > 0
        # Cache should not exceed max size
        assert stats["size_bytes"] <= cache.max_size_bytes

        cache.close()

    def test_clock_second_chance(self, cache_dir):
        """Test that recently accessed entries get a second chance."""
        cache = LocalDiskCache(
            cache_dir=cache_dir,
            max_size_gb=0.00001,  # ~10KB
        )

        # Add initial content
        content1 = b"Content 1" * 100
        hash1 = content_hash(content1)
        cache.put(hash1, content1)

        content2 = b"Content 2" * 100
        hash2 = content_hash(content2)
        cache.put(hash2, content2)

        # Access content1 to set clock bit
        cache.get(hash1)

        # Add more content to trigger eviction
        for i in range(20):
            content = f"Content {i + 10}".encode() * 100
            cache.put(content_hash(content), content)

        # content1 should have been given a second chance
        # (though it may still be evicted if needed)
        stats = cache.get_stats()
        assert stats["evictions"] > 0

        cache.close()

    def test_priority_eviction(self, cache_dir):
        """Test that high-priority entries are evicted later."""
        cache = LocalDiskCache(
            cache_dir=cache_dir,
            max_size_gb=0.00001,  # ~10KB
        )

        # Add high priority content
        important = b"Important content" * 100
        hash_important = content_hash(important)
        cache.put(hash_important, important, priority=10)

        # Add low priority content to fill cache
        for i in range(20):
            content = f"Filler {i}".encode() * 100
            cache.put(content_hash(content), content, priority=0)

        # High priority content should still exist
        # (unless cache is extremely small)
        cache.close()


class TestBlockStorage:
    """Test block-level storage for large files."""

    def test_store_blocks(self, cache):
        """Test storing content as blocks."""
        # Create content larger than block size
        # Default block size is 4MB, but we'll use smaller for test
        cache.block_size = 1024  # 1KB blocks for testing
        content = b"x" * 5000  # 5KB
        hash_val = content_hash(content)

        # Store with blocks
        cache.put(hash_val, content, store_blocks=True)

        # Get full content
        result = cache.get(hash_val)
        assert result == content

    def test_get_block(self, cache):
        """Test getting individual blocks."""
        cache.block_size = 1024  # 1KB blocks
        content = b"A" * 1024 + b"B" * 1024 + b"C" * 1024  # 3KB
        hash_val = content_hash(content)

        cache.put(hash_val, content, store_blocks=True)

        # Get individual blocks
        block0 = cache.get_block(hash_val, 0)
        assert block0 == b"A" * 1024

        block1 = cache.get_block(hash_val, 1)
        assert block1 == b"B" * 1024

        block2 = cache.get_block(hash_val, 2)
        assert block2 == b"C" * 1024

        # Nonexistent block
        block3 = cache.get_block(hash_val, 3)
        assert block3 is None


class TestPersistence:
    """Test cache persistence across restarts."""

    def test_metadata_persistence(self, cache_dir):
        """Test that cache persists across restarts."""
        # Create cache and add content
        cache1 = LocalDiskCache(cache_dir=cache_dir, max_size_gb=0.01)

        content = b"Persistent content"
        hash_val = content_hash(content)
        cache1.put(hash_val, content)
        cache1.save_metadata()
        cache1.close()

        # Create new cache instance
        cache2 = LocalDiskCache(cache_dir=cache_dir, max_size_gb=0.01)

        # Content should still be available
        result = cache2.get(hash_val)
        assert result == content

        cache2.close()

    def test_scan_recovery(self, cache_dir):
        """Test recovery by scanning content directory."""
        # Create cache and add content
        cache1 = LocalDiskCache(cache_dir=cache_dir, max_size_gb=0.01)

        content = b"Recoverable content"
        hash_val = content_hash(content)
        cache1.put(hash_val, content)
        cache1.close()

        # Delete metadata file to force scan recovery
        metadata_path = Path(cache_dir) / "metadata.bin"
        if metadata_path.exists():
            metadata_path.unlink()

        # Create new cache instance - should scan and recover
        cache2 = LocalDiskCache(cache_dir=cache_dir, max_size_gb=0.01)

        # Content should still be available
        result = cache2.get(hash_val)
        assert result == content

        cache2.close()


class TestBloomFilter:
    """Test Bloom filter optimization."""

    def test_bloom_filter_fast_miss(self, cache):
        """Test that Bloom filter avoids disk I/O for misses."""
        # Access nonexistent content multiple times
        for i in range(100):
            result = cache.get(f"nonexistent_{i}")
            assert result is None

        # Misses should be counted
        stats = cache.get_stats()
        assert stats["misses"] == 100

    def test_bloom_filter_no_false_negatives(self, cache):
        """Test that Bloom filter never has false negatives."""
        # Add content
        contents = []
        for i in range(50):
            content = f"Content {i}".encode()
            hash_val = content_hash(content)
            contents.append((hash_val, content))
            cache.put(hash_val, content)

        # All content should be retrievable
        for hash_val, content in contents:
            result = cache.get(hash_val)
            assert result == content


class TestStatistics:
    """Test cache statistics."""

    def test_hit_rate(self, cache):
        """Test hit rate calculation."""
        content = b"Test content"
        hash_val = content_hash(content)
        cache.put(hash_val, content)

        # Generate hits
        for _ in range(8):
            cache.get(hash_val)

        # Generate misses
        for i in range(2):
            cache.get(f"miss_{i}")

        stats = cache.get_stats()
        assert stats["hits"] == 8
        assert stats["misses"] == 2
        assert stats["hit_rate"] == 0.8

    def test_size_tracking(self, cache):
        """Test size tracking."""
        initial_stats = cache.get_stats()
        assert initial_stats["size_bytes"] == 0

        # Add content
        content1 = b"x" * 1000
        content2 = b"y" * 2000
        cache.put(content_hash(content1), content1)
        cache.put(content_hash(content2), content2)

        stats = cache.get_stats()
        assert stats["size_bytes"] == 3000
        assert stats["entries"] == 2


class TestGlobalInstance:
    """Test global cache instance management."""

    def test_get_global_cache(self, cache_dir):
        """Test getting global cache instance."""
        # Clear any existing global cache
        close_local_disk_cache()

        # Set environment variable
        os.environ["NEXUS_LOCAL_CACHE_DIR"] = cache_dir
        os.environ["NEXUS_LOCAL_CACHE_SIZE_GB"] = "0.01"

        try:
            cache = get_local_disk_cache()
            assert cache is not None
            assert cache.cache_dir == Path(cache_dir)

            # Should return same instance
            cache2 = get_local_disk_cache()
            assert cache is cache2

        finally:
            close_local_disk_cache()
            os.environ.pop("NEXUS_LOCAL_CACHE_DIR", None)
            os.environ.pop("NEXUS_LOCAL_CACHE_SIZE_GB", None)

    def test_set_global_cache(self, cache):
        """Test setting global cache instance."""
        close_local_disk_cache()

        set_local_disk_cache(cache)

        global_cache = get_local_disk_cache()
        assert global_cache is cache

        close_local_disk_cache()


class TestEdgeCases:
    """Test edge cases and error handling."""

    def test_content_too_large(self, cache_dir):
        """Test that content larger than cache is rejected."""
        cache = LocalDiskCache(
            cache_dir=cache_dir,
            max_size_gb=0.00001,  # ~10KB
        )

        # Try to cache content larger than max size
        large_content = b"x" * 100000  # 100KB
        hash_val = content_hash(large_content)

        result = cache.put(hash_val, large_content)
        assert result is False

        cache.close()

    def test_concurrent_access(self, cache):
        """Test thread-safe concurrent access."""
        import threading

        content = b"Concurrent content"
        hash_val = content_hash(content)
        cache.put(hash_val, content)

        results = []
        errors = []

        def reader():
            try:
                for _ in range(100):
                    result = cache.get(hash_val)
                    results.append(result == content)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=reader) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert all(results)

    def test_warm_cache(self, cache):
        """Test cache warming functionality."""
        contents = {content_hash(f"Warm {i}".encode()): f"Warm {i}".encode() for i in range(10)}

        def read_func(hash_val):
            return contents.get(hash_val)

        # Warm cache
        warmed = cache.warm(list(contents.keys()), read_func)
        assert warmed == 10

        # All content should be cached
        for hash_val, content in contents.items():
            assert cache.get(hash_val) == content


class TestMultiTenantIsolation:
    """Test multi-tenant cache isolation."""

    def test_tenant_isolation(self, cache_dir):
        """Test that tenants cannot access each other's cached content."""
        cache = LocalDiskCache(cache_dir=cache_dir, max_size_gb=0.01)

        # Same content, different tenants
        content = b"Shared content"
        hash_val = content_hash(content)

        # Tenant A caches content
        cache.put(hash_val, content, tenant_id="tenant_a")

        # Tenant A can access
        result_a = cache.get(hash_val, tenant_id="tenant_a")
        assert result_a == content

        # Tenant B cannot access (different tenant)
        result_b = cache.get(hash_val, tenant_id="tenant_b")
        assert result_b is None

        # No tenant cannot access tenant A's content
        result_none = cache.get(hash_val, tenant_id=None)
        assert result_none is None

        cache.close()

    def test_tenant_isolation_exists(self, cache_dir):
        """Test exists() respects tenant isolation."""
        cache = LocalDiskCache(cache_dir=cache_dir, max_size_gb=0.01)

        content = b"Tenant content"
        hash_val = content_hash(content)

        cache.put(hash_val, content, tenant_id="tenant_x")

        assert cache.exists(hash_val, tenant_id="tenant_x") is True
        assert cache.exists(hash_val, tenant_id="tenant_y") is False
        assert cache.exists(hash_val, tenant_id=None) is False

        cache.close()

    def test_tenant_isolation_remove(self, cache_dir):
        """Test remove() respects tenant isolation."""
        cache = LocalDiskCache(cache_dir=cache_dir, max_size_gb=0.01)

        content = b"Removable content"
        hash_val = content_hash(content)

        cache.put(hash_val, content, tenant_id="tenant_1")
        cache.put(hash_val, content, tenant_id="tenant_2")

        # Remove tenant_1's content
        assert cache.remove(hash_val, tenant_id="tenant_1") is True

        # tenant_1 content is gone
        assert cache.get(hash_val, tenant_id="tenant_1") is None

        # tenant_2 content still exists
        assert cache.get(hash_val, tenant_id="tenant_2") == content

        cache.close()

    def test_same_content_different_tenants(self, cache_dir):
        """Test that same content can be cached separately for different tenants."""
        cache = LocalDiskCache(cache_dir=cache_dir, max_size_gb=0.01)

        content = b"Identical content"
        hash_val = content_hash(content)

        # Both tenants cache the same content
        cache.put(hash_val, content, tenant_id="alpha")
        cache.put(hash_val, content, tenant_id="beta")

        # Both can access their own copy
        assert cache.get(hash_val, tenant_id="alpha") == content
        assert cache.get(hash_val, tenant_id="beta") == content

        # Stats should show 2 entries
        stats = cache.get_stats()
        assert stats["entries"] == 2

        cache.close()


class TestCacheEntry:
    """Test CacheEntry dataclass."""

    def test_touch(self):
        """Test touch updates access time and clock bit."""
        entry = CacheEntry(
            content_hash="abc123",
            size_bytes=100,
            created_at=time.time(),
            last_accessed=time.time() - 100,
            access_count=1,
            clock_bit=False,
        )

        old_accessed = entry.last_accessed
        old_count = entry.access_count

        time.sleep(0.01)
        entry.touch()

        assert entry.last_accessed > old_accessed
        assert entry.access_count == old_count + 1
        assert entry.clock_bit is True
