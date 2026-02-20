"""Unit tests for WatchCacheManager (Issue #2065).

TDD: These tests were written BEFORE the implementation was finalized.

Tests the Kubernetes Informer-style watch cache that polls MetastoreABC
for replication changes and routes them through ReadSetAwareCache for
proactive cache invalidation in multi-node deployments.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from nexus.core.metadata import FileMetadata
from nexus.core.metadata_change import MetadataChange
from nexus.core.metastore import MetastoreABC
from nexus.core.read_set import ReadSetRegistry
from nexus.storage.cache import MetadataCache
from nexus.storage.read_set_cache import ReadSetAwareCache
from nexus.storage.watch_cache_manager import WatchCacheManager
from tests.helpers.in_memory_metadata_store import InMemoryMetastore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_cache() -> tuple[MetadataCache, ReadSetAwareCache]:
    """Create a MetadataCache + ReadSetAwareCache pair for testing."""
    base = MetadataCache(
        path_cache_size=64,
        list_cache_size=32,
        kv_cache_size=32,
        exists_cache_size=64,
        ttl_seconds=300,
    )
    registry = ReadSetRegistry()
    rsc = ReadSetAwareCache(base_cache=base, registry=registry)
    return base, rsc


def _make_meta(path: str) -> FileMetadata:
    return FileMetadata(
        path=path,
        backend_name="local",
        physical_path=f"phys_{path}",
        size=100,
        etag=f"etag_{path}",
    )


# ---------------------------------------------------------------------------
# Phase 1: MetastoreABC default
# ---------------------------------------------------------------------------


class TestMetastoreABCDefault:
    """MetastoreABC.drain_changes() returns [] by default."""

    def test_drain_changes_empty_by_default(self):
        """Default implementation returns an empty list."""

        class MinimalStore(MetastoreABC):
            def get(self, path):
                return None

            def put(self, metadata, *, consistency="sc"):
                pass

            def delete(self, path, *, consistency="sc"):
                return None

            def exists(self, path):
                return False

            def list(self, prefix="", recursive=True, **kw):
                return []

            def close(self):
                pass

        store = MinimalStore()
        assert store.drain_changes() == []
        assert store.drain_changes(since_revision=100) == []


# ---------------------------------------------------------------------------
# Phase 2: InMemoryMetastore change tracking
# ---------------------------------------------------------------------------


class TestInMemoryMetastoreChanges:
    """InMemoryMetastore records changes and implements drain_changes()."""

    def test_put_records_change(self):
        store = InMemoryMetastore(zone_id="z1")
        meta = _make_meta("/a.txt")
        store.put(meta)

        changes = store.drain_changes()
        assert len(changes) == 1
        assert changes[0].path == "/a.txt"
        assert changes[0].operation == "put"
        assert changes[0].zone_id == "z1"
        assert changes[0].revision == 1

    def test_delete_records_change(self):
        store = InMemoryMetastore(zone_id="z2")
        meta = _make_meta("/b.txt")
        store.put(meta)
        store.delete("/b.txt")

        changes = store.drain_changes()
        assert len(changes) == 2
        assert changes[0].operation == "put"
        assert changes[1].operation == "delete"
        assert changes[1].zone_id == "z2"

    def test_drain_clears_buffer(self):
        store = InMemoryMetastore()
        store.put(_make_meta("/a.txt"))
        store.put(_make_meta("/b.txt"))

        first = store.drain_changes()
        assert len(first) == 2

        second = store.drain_changes()
        assert len(second) == 0

    def test_drain_since_revision(self):
        store = InMemoryMetastore()
        store.put(_make_meta("/a.txt"))  # rev 1
        store.put(_make_meta("/b.txt"))  # rev 2
        store.put(_make_meta("/c.txt"))  # rev 3

        changes = store.drain_changes(since_revision=1)
        assert len(changes) == 2
        assert changes[0].revision == 2
        assert changes[1].revision == 3

    def test_revision_increments(self):
        store = InMemoryMetastore()
        store.put(_make_meta("/a.txt"))
        store.put(_make_meta("/b.txt"))
        store.delete("/a.txt")

        changes = store.drain_changes()
        revisions = [c.revision for c in changes]
        assert revisions == [1, 2, 3]

    def test_delete_nonexistent_no_change(self):
        store = InMemoryMetastore()
        store.delete("/nonexistent.txt")
        assert store.drain_changes() == []


# ---------------------------------------------------------------------------
# Phase 3: WatchCacheManager unit tests
# ---------------------------------------------------------------------------


class TestWatchCacheManagerPoll:
    """WatchCacheManager polls metastore and invalidates cache."""

    def test_poll_once_calls_invalidate(self):
        store = InMemoryMetastore(zone_id="z1")
        _, rsc = _make_cache()

        wm = WatchCacheManager(store, rsc, poll_interval_ms=10)

        store.put(_make_meta("/a.txt"))
        store.put(_make_meta("/b.txt"))

        wm._poll_once()

        stats = wm.get_stats()
        assert stats["watch_invalidations"] == 2
        assert stats["watch_polls"] == 1
        assert stats["watch_empty_polls"] == 0

    def test_empty_poll_no_op(self):
        store = InMemoryMetastore()
        _, rsc = _make_cache()

        wm = WatchCacheManager(store, rsc)
        wm._poll_once()

        stats = wm.get_stats()
        assert stats["watch_polls"] == 1
        assert stats["watch_empty_polls"] == 1
        assert stats["watch_invalidations"] == 0

    def test_multiple_changes_batch(self):
        store = InMemoryMetastore(zone_id="z1")
        _, rsc = _make_cache()

        wm = WatchCacheManager(store, rsc)

        for i in range(5):
            store.put(_make_meta(f"/file{i}.txt"))

        wm._poll_once()

        stats = wm.get_stats()
        assert stats["watch_invalidations"] == 5
        assert stats["watch_last_revision"] == 5

    def test_revision_tracking(self):
        store = InMemoryMetastore()
        _, rsc = _make_cache()

        wm = WatchCacheManager(store, rsc)

        store.put(_make_meta("/a.txt"))  # rev 1
        wm._poll_once()
        assert wm._last_revision == 1

        store.put(_make_meta("/b.txt"))  # rev 2
        wm._poll_once()
        assert wm._last_revision == 2

        # No new changes after rev 2
        wm._poll_once()
        assert wm._last_revision == 2

    def test_zone_aware_invalidation(self):
        store = InMemoryMetastore(zone_id="zone-42")
        _, rsc = _make_cache()

        # Use a mock to verify zone_id is passed through
        rsc.invalidate_for_write = MagicMock(return_value=0)

        wm = WatchCacheManager(store, rsc)
        store.put(_make_meta("/x.txt"))
        wm._poll_once()

        rsc.invalidate_for_write.assert_called_once_with(
            "/x.txt",
            1,
            zone_id="zone-42",
        )

    def test_overflow_triggers_full_clear(self):
        store = InMemoryMetastore()
        _, rsc = _make_cache()

        wm = WatchCacheManager(store, rsc, buffer_overflow_threshold=3)

        rsc.clear = MagicMock()

        for i in range(5):
            store.put(_make_meta(f"/file{i}.txt"))

        wm._poll_once()

        rsc.clear.assert_called_once()
        stats = wm.get_stats()
        assert stats["watch_overflow_clears"] == 1
        assert stats["watch_invalidations"] == 0  # skipped individual invalidation
        assert stats["watch_last_revision"] == 5

    def test_error_recovery(self):
        store = InMemoryMetastore()
        _, rsc = _make_cache()

        wm = WatchCacheManager(store, rsc)

        # Make drain_changes raise an exception
        store.drain_changes = MagicMock(side_effect=RuntimeError("PyO3 error"))

        # _poll_once raises, but the async loop catches it
        with pytest.raises(RuntimeError, match="PyO3 error"):
            wm._poll_once()

    def test_stats_tracking(self):
        store = InMemoryMetastore()
        _, rsc = _make_cache()

        wm = WatchCacheManager(store, rsc)

        stats = wm.get_stats()
        assert stats["watch_polls"] == 0
        assert stats["watch_empty_polls"] == 0
        assert stats["watch_invalidations"] == 0
        assert stats["watch_overflow_clears"] == 0
        assert stats["watch_errors"] == 0
        assert stats["watch_last_revision"] == 0
        assert stats["watch_running"] is False


# ---------------------------------------------------------------------------
# Async lifecycle tests
# ---------------------------------------------------------------------------


class TestWatchCacheManagerLifecycle:
    """Tests for start/stop lifecycle and async poll loop."""

    @pytest.mark.asyncio
    async def test_start_and_stop(self):
        store = InMemoryMetastore()
        _, rsc = _make_cache()

        wm = WatchCacheManager(store, rsc, poll_interval_ms=5)

        await wm.start()
        assert wm._running is True
        assert wm._task is not None

        await wm.stop()
        assert wm._running is False
        assert wm._task is None

    @pytest.mark.asyncio
    async def test_double_start_is_noop(self):
        store = InMemoryMetastore()
        _, rsc = _make_cache()

        wm = WatchCacheManager(store, rsc, poll_interval_ms=5)
        await wm.start()
        task1 = wm._task

        await wm.start()  # second start should be no-op
        assert wm._task is task1

        await wm.stop()

    @pytest.mark.asyncio
    async def test_poll_loop_processes_changes(self):
        store = InMemoryMetastore(zone_id="z1")
        _, rsc = _make_cache()

        wm = WatchCacheManager(store, rsc, poll_interval_ms=5)
        await wm.start()

        store.put(_make_meta("/a.txt"))
        store.put(_make_meta("/b.txt"))

        # Give the poll loop time to process
        await asyncio.sleep(0.05)

        stats = wm.get_stats()
        assert stats["watch_invalidations"] >= 2

        await wm.stop()

    @pytest.mark.asyncio
    async def test_graceful_shutdown(self):
        store = InMemoryMetastore()
        _, rsc = _make_cache()

        wm = WatchCacheManager(store, rsc, poll_interval_ms=5)
        await wm.start()

        # Stop should cancel cleanly without exceptions
        await wm.stop()

        stats = wm.get_stats()
        assert stats["watch_running"] is False

    @pytest.mark.asyncio
    async def test_error_in_poll_loop_doesnt_crash(self):
        """Transient exceptions in poll loop are caught and retried."""

        class _FailOnceMetastore(InMemoryMetastore):
            def __init__(self) -> None:
                super().__init__()
                self._call_count = 0

            def drain_changes(self, since_revision: int = 0):
                self._call_count += 1
                if self._call_count == 1:
                    raise RuntimeError("transient error")
                return super().drain_changes(since_revision)

        store = _FailOnceMetastore()
        _, rsc = _make_cache()

        wm = WatchCacheManager(store, rsc, poll_interval_ms=5)

        await wm.start()
        await asyncio.sleep(0.05)  # let the loop run a few cycles

        # The loop should still be running despite the error
        assert wm._running is True
        stats = wm.get_stats()
        assert stats["watch_errors"] >= 1
        assert stats["watch_polls"] >= 2  # recovered and polled again

        await wm.stop()


# ---------------------------------------------------------------------------
# MetadataChange dataclass tests
# ---------------------------------------------------------------------------


class TestMetadataChange:
    """Tests for the MetadataChange frozen dataclass."""

    def test_frozen(self):
        """Frozen dataclass rejects attribute mutation."""
        import dataclasses

        change = MetadataChange(revision=1, path="/a.txt", operation="put", zone_id="z1")
        assert dataclasses.is_dataclass(change)
        # Frozen: cannot replace with invalid field
        with pytest.raises(dataclasses.FrozenInstanceError):
            change.__delattr__("revision")

    def test_slots(self):
        change = MetadataChange(revision=1, path="/a.txt", operation="put", zone_id="z1")
        assert not hasattr(change, "__dict__")

    def test_equality(self):
        a = MetadataChange(revision=1, path="/a.txt", operation="put", zone_id="z1")
        b = MetadataChange(revision=1, path="/a.txt", operation="put", zone_id="z1")
        assert a == b

    def test_fields(self):
        change = MetadataChange(revision=42, path="/foo/bar", operation="delete", zone_id="z2")
        assert change.revision == 42
        assert change.path == "/foo/bar"
        assert change.operation == "delete"
        assert change.zone_id == "z2"


# ---------------------------------------------------------------------------
# End-to-end cache invalidation flow
# ---------------------------------------------------------------------------


class TestWatchInvalidationFlow:
    """Test that watch cache actually invalidates cached entries."""

    def test_cached_entry_invalidated_by_watch(self):
        """Write to cache, then simulate a remote write via drain_changes.

        The cached entry should be invalidated after the watch poll.
        """
        store = InMemoryMetastore(zone_id="z1")
        base_cache, rsc = _make_cache()

        # Populate cache
        meta = _make_meta("/data.txt")
        rsc.put_path("/data.txt", meta)

        # Verify it's cached
        from nexus.storage.cache import _CACHE_MISS

        assert base_cache.get_path("/data.txt") is not _CACHE_MISS

        # Simulate a remote write (appears via metastore change feed)
        store.put(meta)

        # Poll picks it up
        wm = WatchCacheManager(store, rsc)
        wm._poll_once()

        # The path should have been invalidated via fallback
        # (no read set registered, so fallback path-based invalidation)
        stats = wm.get_stats()
        assert stats["watch_invalidations"] == 1

    @pytest.mark.asyncio
    async def test_full_async_flow(self):
        """Full async: start manager, write, wait for invalidation."""
        store = InMemoryMetastore(zone_id="z1")
        base_cache, rsc = _make_cache()

        meta = _make_meta("/live.txt")
        rsc.put_path("/live.txt", meta)

        wm = WatchCacheManager(store, rsc, poll_interval_ms=5)
        await wm.start()

        # Simulate remote write
        store.put(meta)

        # Wait for poll to process
        await asyncio.sleep(0.05)

        stats = wm.get_stats()
        assert stats["watch_invalidations"] >= 1

        await wm.stop()
