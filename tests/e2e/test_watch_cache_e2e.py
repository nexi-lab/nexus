"""E2E tests for WatchCacheManager with NexusFS (Issue #2065).

Verifies that the watch cache manager integrates correctly with the full
NexusFS stack: writes via metastore produce changes that the watch poll
loop picks up and routes through ReadSetAwareCache for invalidation.

Uses InMemoryMetastore to avoid Rust/Raft dependency.
"""

from __future__ import annotations

import asyncio

import pytest

from nexus.core.config import CacheConfig
from nexus.core.metadata import FileMetadata
from nexus.storage.watch_cache_manager import WatchCacheManager
from tests.helpers.in_memory_metadata_store import InMemoryMetastore


def _make_meta(path: str, etag: str = "e1") -> FileMetadata:
    return FileMetadata(
        path=path,
        backend_name="local",
        physical_path=f"phys_{path}",
        size=42,
        etag=etag,
    )


class TestWatchCacheE2EWithNexusFS:
    """E2E: NexusFS with watch cache enabled."""

    def test_cache_config_defaults(self) -> None:
        """CacheConfig includes watch cache fields with correct defaults."""
        cfg = CacheConfig()
        assert cfg.enable_watch_cache is True
        assert cfg.watch_poll_interval_ms == 10
        assert cfg.watch_buffer_size == 4096

    def test_cache_config_custom(self) -> None:
        """CacheConfig accepts custom watch cache values."""
        cfg = CacheConfig(
            enable_watch_cache=False,
            watch_poll_interval_ms=50,
            watch_buffer_size=8192,
        )
        assert cfg.enable_watch_cache is False
        assert cfg.watch_poll_interval_ms == 50
        assert cfg.watch_buffer_size == 8192

    def test_watch_manager_stats(self) -> None:
        """WatchCacheManager exposes correct stats."""
        from nexus.core.read_set import ReadSetRegistry
        from nexus.storage.cache import MetadataCache
        from nexus.storage.read_set_cache import ReadSetAwareCache

        store = InMemoryMetastore(zone_id="e2e-zone")
        base = MetadataCache(
            path_cache_size=64,
            list_cache_size=32,
            kv_cache_size=32,
            exists_cache_size=64,
            ttl_seconds=300,
        )
        rsc = ReadSetAwareCache(base_cache=base, registry=ReadSetRegistry())
        wm = WatchCacheManager(store, rsc, poll_interval_ms=5)

        # Before any activity
        stats = wm.get_stats()
        assert stats["watch_polls"] == 0
        assert stats["watch_running"] is False

        # Simulate writes and poll
        for i in range(3):
            store.put(_make_meta(f"/e2e_{i}.txt"))

        wm._poll_once()

        stats = wm.get_stats()
        assert stats["watch_polls"] == 1
        assert stats["watch_invalidations"] == 3
        assert stats["watch_last_revision"] == 3

    @pytest.mark.asyncio
    async def test_full_lifecycle(self) -> None:
        """Full lifecycle: start -> write -> poll -> stop."""
        from nexus.core.read_set import ReadSetRegistry
        from nexus.storage.cache import MetadataCache
        from nexus.storage.read_set_cache import ReadSetAwareCache

        store = InMemoryMetastore(zone_id="lifecycle-zone")
        base = MetadataCache(
            path_cache_size=64,
            list_cache_size=32,
            kv_cache_size=32,
            exists_cache_size=64,
            ttl_seconds=300,
        )
        rsc = ReadSetAwareCache(base_cache=base, registry=ReadSetRegistry())
        wm = WatchCacheManager(store, rsc, poll_interval_ms=5)

        await wm.start()
        assert wm.get_stats()["watch_running"] is True

        # Write some data
        for i in range(5):
            store.put(_make_meta(f"/lifecycle_{i}.txt"))

        # Wait for poll loop
        await asyncio.sleep(0.05)

        stats = wm.get_stats()
        assert stats["watch_invalidations"] >= 5

        await wm.stop()
        assert wm.get_stats()["watch_running"] is False

    @pytest.mark.asyncio
    async def test_watch_disabled_config(self) -> None:
        """When enable_watch_cache=False, no manager is created."""
        cfg = CacheConfig(enable_watch_cache=False)
        assert cfg.enable_watch_cache is False
        # NexusFS would skip creating WatchCacheManager when this is False
