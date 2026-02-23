"""Integration test: IOProfile → ContentCache priority flow (Issue #2427).

Validates the full chain: IOProfile.cache_priority flows into ContentCache.put()
and priority-aware eviction protects high-priority entries.
"""

from nexus.contracts.io_profile import IOProfile
from nexus.storage.content_cache import ContentCache


class TestIOProfileCachePriorityFlow:
    """Integration: IOProfile cache_priority → ContentCache eviction."""

    def test_fast_read_content_survives_eviction(self):
        """FAST_READ (cache_priority=3) content should survive eviction
        when lower-priority content is present.

        This validates the full IOProfile → ContentCache contract.
        """
        # Verify IOProfile cache_priority values are what we expect
        assert IOProfile.FAST_READ.config().cache_priority == 3
        assert IOProfile.APPEND_ONLY.config().cache_priority == 0
        assert IOProfile.ARCHIVE.config().cache_priority == 0

        # Small cache to force eviction
        cache = ContentCache(max_size_mb=0, compression_threshold=100000)
        cache._max_size_bytes = 1000

        # Fill with APPEND_ONLY content (priority=0)
        append_priority = IOProfile.APPEND_ONLY.config().cache_priority
        for i in range(3):
            cache.put(f"append-{i}", b"x" * 300, priority=append_priority)

        # Add FAST_READ content (priority=3) — triggers eviction of priority=0
        fast_read_priority = IOProfile.FAST_READ.config().cache_priority
        cache.put("model-weights", b"y" * 300, priority=fast_read_priority)

        # FAST_READ content should survive
        assert cache.get("model-weights") is not None

        # Add more APPEND_ONLY to further stress eviction
        cache.put("append-new", b"z" * 300, priority=append_priority)

        # FAST_READ should STILL survive (priority=3 protected)
        assert cache.get("model-weights") is not None, (
            "FAST_READ content (priority=3) must survive eviction"
        )

    def test_archive_should_not_be_cached(self):
        """ARCHIVE profile (cache_priority=0) content should be evictable.

        In the FUSE handler, ARCHIVE bypasses cache entirely.
        At the ContentCache level, priority=0 means "evict first."
        """
        cache = ContentCache(max_size_mb=0, compression_threshold=100000)
        cache._max_size_bytes = 600

        archive_priority = IOProfile.ARCHIVE.config().cache_priority
        edit_priority = IOProfile.EDIT.config().cache_priority

        # ARCHIVE content
        cache.put("cold-data", b"a" * 300, priority=archive_priority)
        # EDIT content
        cache.put("active-doc", b"b" * 300, priority=edit_priority)

        # Both fit
        assert cache.get("cold-data") is not None
        assert cache.get("active-doc") is not None

        # Add more — forces eviction. ARCHIVE (priority=0) goes first.
        cache.put("new-doc", b"c" * 300, priority=edit_priority)

        assert cache.get("cold-data") is None, "ARCHIVE content should be evicted first"
        assert cache.get("active-doc") is not None, "EDIT content should survive"

    def test_all_profiles_have_valid_cache_priority(self):
        """All IOProfile values should have cache_priority in [0, 3]."""
        for profile in IOProfile:
            priority = profile.config().cache_priority
            assert 0 <= priority <= 3, f"{profile.name} has invalid cache_priority={priority}"
