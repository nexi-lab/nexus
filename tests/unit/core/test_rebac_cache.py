"""Unit tests for ReBAC L1 cache implementation."""

import time

import pytest

from nexus.core.rebac_cache import ReBACPermissionCache


class TestReBACPermissionCache:
    """Test suite for in-memory L1 permission cache."""

    def test_cache_basic_operations(self):
        """Test basic get/set operations."""
        cache = ReBACPermissionCache(max_size=100, ttl_seconds=60)

        # Test cache miss
        result = cache.get("agent", "alice", "read", "file", "/doc.txt")
        assert result is None

        # Test cache set and hit
        cache.set("agent", "alice", "read", "file", "/doc.txt", True)
        result = cache.get("agent", "alice", "read", "file", "/doc.txt")
        assert result is True

        # Test different permission on same subject/object
        result = cache.get("agent", "alice", "write", "file", "/doc.txt")
        assert result is None

    def test_cache_ttl_expiration(self):
        """Test that cache entries expire after TTL."""
        cache = ReBACPermissionCache(max_size=100, ttl_seconds=1)  # 1 second TTL

        cache.set("agent", "alice", "read", "file", "/doc.txt", True)

        # Should hit immediately
        result = cache.get("agent", "alice", "read", "file", "/doc.txt")
        assert result is True

        # Wait for expiration
        time.sleep(1.5)

        # Should miss after expiration
        result = cache.get("agent", "alice", "read", "file", "/doc.txt")
        assert result is None

    def test_cache_invalidation_subject(self):
        """Test invalidating all entries for a subject."""
        cache = ReBACPermissionCache(max_size=100, ttl_seconds=60)

        # Set multiple entries for alice
        cache.set("agent", "alice", "read", "file", "/doc1.txt", True)
        cache.set("agent", "alice", "write", "file", "/doc2.txt", True)
        cache.set("agent", "bob", "read", "file", "/doc3.txt", True)

        # Invalidate alice's entries
        count = cache.invalidate_subject("agent", "alice")
        assert count == 2

        # Alice's entries should be gone
        assert cache.get("agent", "alice", "read", "file", "/doc1.txt") is None
        assert cache.get("agent", "alice", "write", "file", "/doc2.txt") is None

        # Bob's entry should still exist
        assert cache.get("agent", "bob", "read", "file", "/doc3.txt") is True

    def test_cache_invalidation_object(self):
        """Test invalidating all entries for an object."""
        cache = ReBACPermissionCache(max_size=100, ttl_seconds=60)

        # Set multiple entries for same object
        cache.set("agent", "alice", "read", "file", "/doc.txt", True)
        cache.set("agent", "bob", "write", "file", "/doc.txt", True)
        cache.set("agent", "alice", "read", "file", "/other.txt", False)

        # Invalidate entries for /doc.txt
        count = cache.invalidate_object("file", "/doc.txt")
        assert count == 2

        # /doc.txt entries should be gone
        assert cache.get("agent", "alice", "read", "file", "/doc.txt") is None
        assert cache.get("agent", "bob", "write", "file", "/doc.txt") is None

        # /other.txt entry should still exist
        assert cache.get("agent", "alice", "read", "file", "/other.txt") is False

    def test_cache_invalidation_subject_object_pair(self):
        """Test precise invalidation for subject-object pair."""
        cache = ReBACPermissionCache(max_size=100, ttl_seconds=60)

        # Set multiple entries
        cache.set("agent", "alice", "read", "file", "/doc.txt", True)
        cache.set("agent", "alice", "write", "file", "/doc.txt", True)
        cache.set("agent", "alice", "read", "file", "/other.txt", True)
        cache.set("agent", "bob", "read", "file", "/doc.txt", True)

        # Invalidate only alice's entries for /doc.txt
        count = cache.invalidate_subject_object_pair("agent", "alice", "file", "/doc.txt")
        assert count == 2  # read and write permissions

        # Alice's entries for /doc.txt should be gone
        assert cache.get("agent", "alice", "read", "file", "/doc.txt") is None
        assert cache.get("agent", "alice", "write", "file", "/doc.txt") is None

        # Other entries should still exist
        assert cache.get("agent", "alice", "read", "file", "/other.txt") is True
        assert cache.get("agent", "bob", "read", "file", "/doc.txt") is True

    def test_cache_invalidation_prefix(self):
        """Test invalidating entries by object ID prefix."""
        cache = ReBACPermissionCache(max_size=100, ttl_seconds=60)

        # Set entries for files in different directories
        cache.set("agent", "alice", "read", "file", "/workspace/doc1.txt", True)
        cache.set("agent", "alice", "read", "file", "/workspace/doc2.txt", True)
        cache.set("agent", "alice", "read", "file", "/other/doc3.txt", True)

        # Invalidate all /workspace/* entries
        count = cache.invalidate_object_prefix("file", "/workspace/")
        assert count == 2

        # /workspace entries should be gone
        assert cache.get("agent", "alice", "read", "file", "/workspace/doc1.txt") is None
        assert cache.get("agent", "alice", "read", "file", "/workspace/doc2.txt") is None

        # /other entry should still exist
        assert cache.get("agent", "alice", "read", "file", "/other/doc3.txt") is True

    def test_cache_metrics(self):
        """Test cache metrics tracking."""
        cache = ReBACPermissionCache(max_size=100, ttl_seconds=60, enable_metrics=True)

        # Perform operations
        cache.get("agent", "alice", "read", "file", "/doc.txt")  # miss
        cache.set("agent", "alice", "read", "file", "/doc.txt", True)
        cache.get("agent", "alice", "read", "file", "/doc.txt")  # hit
        cache.get("agent", "alice", "read", "file", "/doc.txt")  # hit

        # Check stats
        stats = cache.get_stats()
        assert stats["hits"] == 2
        assert stats["misses"] == 1
        assert stats["sets"] == 1
        assert stats["total_requests"] == 3
        assert stats["hit_rate_percent"] == pytest.approx(66.67, rel=0.01)
        assert stats["avg_lookup_time_ms"] >= 0

    def test_cache_tenant_isolation(self):
        """Test that tenants are properly isolated."""
        cache = ReBACPermissionCache(max_size=100, ttl_seconds=60)

        # Set entries for different tenants
        cache.set("agent", "alice", "read", "file", "/doc.txt", True, tenant_id="tenant1")
        cache.set("agent", "alice", "read", "file", "/doc.txt", False, tenant_id="tenant2")

        # Verify isolation
        result1 = cache.get("agent", "alice", "read", "file", "/doc.txt", tenant_id="tenant1")
        result2 = cache.get("agent", "alice", "read", "file", "/doc.txt", tenant_id="tenant2")

        assert result1 is True
        assert result2 is False

    def test_cache_write_tracking(self):
        """Test write frequency tracking for adaptive TTL."""
        cache = ReBACPermissionCache(max_size=100, ttl_seconds=60, enable_adaptive_ttl=True)

        # Track writes
        cache.track_write("/workspace/doc.txt")
        cache.track_write("/workspace/doc.txt")
        cache.track_write("/workspace/doc.txt")

        # Verify write frequency is tracked (internal state check)
        assert "/workspace/doc.txt" in cache._write_frequency
        count, _ = cache._write_frequency["/workspace/doc.txt"]
        assert count == 3

    def test_cache_clear(self):
        """Test clearing all cache entries."""
        cache = ReBACPermissionCache(max_size=100, ttl_seconds=60)

        # Add entries
        cache.set("agent", "alice", "read", "file", "/doc1.txt", True)
        cache.set("agent", "bob", "write", "file", "/doc2.txt", False)

        stats = cache.get_stats()
        assert stats["current_size"] == 2

        # Clear cache
        cache.clear()

        stats = cache.get_stats()
        assert stats["current_size"] == 0
        assert cache.get("agent", "alice", "read", "file", "/doc1.txt") is None

    def test_cache_reset_stats(self):
        """Test resetting cache statistics."""
        cache = ReBACPermissionCache(max_size=100, ttl_seconds=60, enable_metrics=True)

        # Generate some metrics
        cache.get("agent", "alice", "read", "file", "/doc.txt")  # miss
        cache.set("agent", "alice", "read", "file", "/doc.txt", True)
        cache.get("agent", "alice", "read", "file", "/doc.txt")  # hit

        # Reset stats
        cache.reset_stats()

        # Verify reset
        stats = cache.get_stats()
        assert stats["hits"] == 0
        assert stats["misses"] == 0
        assert stats["sets"] == 0
        assert stats["total_requests"] == 0

        # Cache entries should still exist
        assert cache.get("agent", "alice", "read", "file", "/doc.txt") is True
