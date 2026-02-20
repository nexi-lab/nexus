"""Integration tests for WatchCacheManager (Issue #2065).

Full pipeline: write via metastore -> drain changes -> cache invalidated ->
next get() sees fresh data.

Uses InMemoryMetastore + real MetadataCache + real ReadSetAwareCache.
"""

from __future__ import annotations

import asyncio

import pytest

from nexus.core.metadata import FileMetadata
from nexus.core.read_set import ReadSet, ReadSetRegistry
from nexus.storage.cache import _CACHE_MISS, MetadataCache
from nexus.storage.read_set_cache import ReadSetAwareCache
from nexus.storage.watch_cache_manager import WatchCacheManager
from tests.helpers.in_memory_metadata_store import InMemoryMetastore


def _make_meta(path: str, etag: str = "abc") -> FileMetadata:
    return FileMetadata(
        path=path,
        backend_name="local",
        physical_path=f"phys_{path}",
        size=100,
        etag=etag,
    )


class TestWatchCacheIntegrationPipeline:
    """Full integration: metastore -> watch -> cache invalidation -> fresh reads."""

    def setup_method(self) -> None:
        self.store = InMemoryMetastore(zone_id="integration-zone")
        self.registry = ReadSetRegistry()
        self.base_cache = MetadataCache(
            path_cache_size=64,
            list_cache_size=32,
            kv_cache_size=32,
            exists_cache_size=64,
            ttl_seconds=300,
        )
        self.rsc = ReadSetAwareCache(
            base_cache=self.base_cache,
            registry=self.registry,
        )
        self.wm = WatchCacheManager(
            self.store,
            self.rsc,
            poll_interval_ms=5,
        )

    def test_write_invalidates_cached_entry(self) -> None:
        """Cache entry is invalidated when metastore reports a change."""
        # 1. Populate cache with a read-set entry
        meta_v1 = _make_meta("/doc.txt", etag="v1")
        rs = ReadSet(query_id="cache:/doc.txt", zone_id="integration-zone")
        rs.record_read("file", "/doc.txt", revision=1)
        self.rsc.put_path("/doc.txt", meta_v1, read_set=rs, zone_revision=1)

        # Verify cached
        assert self.base_cache.get_path("/doc.txt") is not _CACHE_MISS

        # 2. Simulate a remote write (appears in change feed)
        meta_v2 = _make_meta("/doc.txt", etag="v2")
        self.store.put(meta_v2)

        # 3. Watch polls and invalidates
        self.wm._poll_once()

        # 4. Verify invalidation was attempted
        stats = self.wm.get_stats()
        assert stats["watch_invalidations"] == 1

    def test_multiple_zones_independent(self) -> None:
        """Changes in different zones are tracked independently."""
        store_z1 = InMemoryMetastore(zone_id="z1")
        store_z2 = InMemoryMetastore(zone_id="z2")

        _, rsc = (
            ReadSetRegistry(),
            ReadSetAwareCache(
                base_cache=MetadataCache(
                    path_cache_size=64,
                    list_cache_size=32,
                    kv_cache_size=32,
                    exists_cache_size=64,
                    ttl_seconds=300,
                ),
                registry=ReadSetRegistry(),
            ),
        )

        wm1 = WatchCacheManager(store_z1, rsc)
        wm2 = WatchCacheManager(store_z2, rsc)

        store_z1.put(_make_meta("/z1_file.txt"))
        store_z2.put(_make_meta("/z2_file.txt"))

        wm1._poll_once()
        wm2._poll_once()

        assert wm1.get_stats()["watch_invalidations"] == 1
        assert wm2.get_stats()["watch_invalidations"] == 1

    @pytest.mark.asyncio
    async def test_async_full_pipeline(self) -> None:
        """Full async pipeline: start, write, wait, verify invalidation."""
        meta = _make_meta("/async_test.txt")
        rs = ReadSet(query_id="cache:/async_test.txt", zone_id="integration-zone")
        rs.record_read("file", "/async_test.txt", revision=0)
        self.rsc.put_path("/async_test.txt", meta, read_set=rs, zone_revision=0)

        await self.wm.start()

        # Simulate remote write
        self.store.put(_make_meta("/async_test.txt", etag="updated"))

        # Wait for poll
        await asyncio.sleep(0.05)

        stats = self.wm.get_stats()
        assert stats["watch_invalidations"] >= 1
        assert stats["watch_running"] is True

        await self.wm.stop()
        assert stats["watch_running"] is not True or not self.wm._running

    def test_rapid_writes_batched(self) -> None:
        """Multiple rapid writes are batched in a single poll."""
        for i in range(10):
            self.store.put(_make_meta(f"/rapid_{i}.txt"))

        self.wm._poll_once()

        stats = self.wm.get_stats()
        assert stats["watch_invalidations"] == 10
        assert stats["watch_polls"] == 1

    def test_overflow_threshold_clears_cache(self) -> None:
        """When batch exceeds threshold, full cache clear is triggered."""
        wm = WatchCacheManager(
            self.store,
            self.rsc,
            buffer_overflow_threshold=5,
        )

        # Put some entries in cache
        for i in range(3):
            meta = _make_meta(f"/cached_{i}.txt")
            self.rsc.put_path(f"/cached_{i}.txt", meta)

        # Generate more changes than threshold
        for i in range(10):
            self.store.put(_make_meta(f"/overflow_{i}.txt"))

        wm._poll_once()

        stats = wm.get_stats()
        assert stats["watch_overflow_clears"] == 1
        assert stats["watch_invalidations"] == 0  # skipped individual
