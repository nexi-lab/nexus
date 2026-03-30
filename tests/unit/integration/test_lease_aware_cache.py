"""Integration tests for lease-aware cache (Issue #3400).

Tests the interaction between LeaseManager and cache layers:
- FileContentCache staleness tracking driven by lease acquire/revoke
- FUSECacheManager invalidation on lease revocation
- Eviction predicate behavior with lease state
- LeaseManager callback integration with cache invalidation
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nexus.contracts.protocols.lease import Lease, LeaseState
from nexus.fuse.cache import FUSECacheManager
from nexus.lib.lease import LocalLeaseManager, ManualClock
from nexus.storage.eviction import is_eviction_candidate
from nexus.storage.file_cache import FileContentCache

READ = LeaseState.SHARED_READ
WRITE = LeaseState.EXCLUSIVE_WRITE

ZONE = "zone1"
PATH = "/mnt/gcs/file.txt"
HOLDER = "agent-A"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def clock() -> ManualClock:
    return ManualClock(_now=1000.0)


@pytest.fixture()
def mgr(clock: ManualClock) -> LocalLeaseManager:
    return LocalLeaseManager(zone_id=ZONE, clock=clock, sweep_interval=999.0)


@pytest.fixture()
def file_cache(tmp_path: Path) -> FileContentCache:
    return FileContentCache(tmp_path)


@pytest.fixture()
def fuse_cache() -> FUSECacheManager:
    return FUSECacheManager(attr_cache_size=128, attr_cache_ttl=60, content_cache_size=128)


# ---------------------------------------------------------------------------
# FileContentCache staleness driven by lease state
# ---------------------------------------------------------------------------


class TestFileContentCacheStaleness:
    def test_file_cache_staleness_on_lease_revoke(
        self, file_cache: FileContentCache, tmp_path: Path
    ) -> None:
        """Acquire lease, write content, revoke lease -> read returns None."""
        file_cache.mark_lease_acquired(ZONE, PATH)
        file_cache.write(ZONE, PATH, b"hello world")

        # Content is readable while lease is active
        assert file_cache.read(ZONE, PATH) == b"hello world"

        # Revoke the lease
        file_cache.mark_lease_revoked(ZONE, PATH)

        # After revocation, read returns None (content is stale)
        assert file_cache.read(ZONE, PATH) is None

    def test_file_cache_fresh_with_active_lease(self, file_cache: FileContentCache) -> None:
        """Acquire lease, mark acquired, write content -> read returns content."""
        file_cache.mark_lease_acquired(ZONE, PATH)
        file_cache.write(ZONE, PATH, b"fresh data")

        assert file_cache.has_active_lease(ZONE, PATH) is True
        assert file_cache.read(ZONE, PATH) == b"fresh data"

    def test_file_cache_write_clears_staleness(self, file_cache: FileContentCache) -> None:
        """Mark stale -> write new content -> read returns new content."""
        # Write initial content
        file_cache.write(ZONE, PATH, b"original")

        # Mark stale
        file_cache.mark_lease_revoked(ZONE, PATH)
        assert file_cache.is_stale(ZONE, PATH) is True
        assert file_cache.read(ZONE, PATH) is None

        # Write fresh content (clears staleness)
        file_cache.write(ZONE, PATH, b"refreshed")
        assert file_cache.is_stale(ZONE, PATH) is False
        assert file_cache.read(ZONE, PATH) == b"refreshed"


# ---------------------------------------------------------------------------
# FUSECacheManager invalidation on lease revocation
# ---------------------------------------------------------------------------


class TestFUSECacheLeaseRevocation:
    def test_fuse_cache_invalidation_on_lease_revoke(self, fuse_cache: FUSECacheManager) -> None:
        """Cache content in FUSECacheManager, call on_lease_revoked -> get_content returns None."""
        fuse_cache.cache_content(PATH, b"cached bytes")
        assert fuse_cache.get_content(PATH) == b"cached bytes"

        fuse_cache.on_lease_revoked(PATH)
        assert fuse_cache.get_content(PATH) is None

    def test_fuse_cache_attr_invalidation_on_lease_revoke(
        self, fuse_cache: FUSECacheManager
    ) -> None:
        """Cache attr in FUSECacheManager, call on_lease_revoked -> get_attr returns None."""
        attrs = {"st_size": 1024, "st_mode": 0o100644}
        fuse_cache.cache_attr(PATH, attrs)
        assert fuse_cache.get_attr(PATH) is not None

        fuse_cache.on_lease_revoked(PATH)
        assert fuse_cache.get_attr(PATH) is None

    def test_fuse_cache_parsed_invalidation_on_lease_revoke(
        self, fuse_cache: FUSECacheManager
    ) -> None:
        """Cache parsed in FUSECacheManager, call on_lease_revoked -> get_parsed returns None."""
        fuse_cache.cache_parsed(PATH, "txt", b"parsed text content")
        assert fuse_cache.get_parsed(PATH, "txt") == b"parsed text content"

        fuse_cache.on_lease_revoked(PATH)
        assert fuse_cache.get_parsed(PATH, "txt") is None


# ---------------------------------------------------------------------------
# Full lease lifecycle with FileContentCache
# ---------------------------------------------------------------------------


class TestLeaseLifecycleWithFileCache:
    def test_lease_lifecycle_with_file_cache(self, file_cache: FileContentCache) -> None:
        """Full lifecycle: acquire -> write -> verify fresh -> revoke -> verify stale -> write -> verify fresh again."""
        # 1. Acquire lease
        file_cache.mark_lease_acquired(ZONE, PATH)
        assert file_cache.has_active_lease(ZONE, PATH) is True
        assert file_cache.is_stale(ZONE, PATH) is False

        # 2. Write content
        file_cache.write(ZONE, PATH, b"v1")
        assert file_cache.read(ZONE, PATH) == b"v1"

        # 3. Revoke lease
        file_cache.mark_lease_revoked(ZONE, PATH)
        assert file_cache.has_active_lease(ZONE, PATH) is False
        assert file_cache.is_stale(ZONE, PATH) is True

        # 4. Read returns None (stale)
        assert file_cache.read(ZONE, PATH) is None
        # Text read also returns None
        assert file_cache.read_text(ZONE, PATH) is None
        # Meta read also returns None
        assert file_cache.read_meta(ZONE, PATH) is None

        # 5. Write new content (clears staleness)
        file_cache.write(ZONE, PATH, b"v2")
        assert file_cache.is_stale(ZONE, PATH) is False
        assert file_cache.read(ZONE, PATH) == b"v2"

    def test_multiple_paths_independent_staleness(self, file_cache: FileContentCache) -> None:
        """Multiple paths with different lease states are independent."""
        path_a = "/mnt/gcs/a.txt"
        path_b = "/mnt/gcs/b.txt"

        # Write content to both paths
        file_cache.mark_lease_acquired(ZONE, path_a)
        file_cache.mark_lease_acquired(ZONE, path_b)
        file_cache.write(ZONE, path_a, b"content-a")
        file_cache.write(ZONE, path_b, b"content-b")

        # Revoke lease on path_a only
        file_cache.mark_lease_revoked(ZONE, path_a)

        # path_a is stale, path_b is fresh
        assert file_cache.is_stale(ZONE, path_a) is True
        assert file_cache.is_stale(ZONE, path_b) is False
        assert file_cache.read(ZONE, path_a) is None
        assert file_cache.read(ZONE, path_b) == b"content-b"

    def test_zone_isolation_in_staleness(self, file_cache: FileContentCache) -> None:
        """Same path in different zones — one stale, one fresh."""
        zone_a = "us-east-1"
        zone_b = "eu-west-1"
        vpath = "/mnt/gcs/shared.txt"

        # Write content to both zones
        file_cache.mark_lease_acquired(zone_a, vpath)
        file_cache.mark_lease_acquired(zone_b, vpath)
        file_cache.write(zone_a, vpath, b"data-east")
        file_cache.write(zone_b, vpath, b"data-west")

        # Revoke lease in zone_a only
        file_cache.mark_lease_revoked(zone_a, vpath)

        # zone_a is stale, zone_b is fresh
        assert file_cache.is_stale(zone_a, vpath) is True
        assert file_cache.is_stale(zone_b, vpath) is False
        assert file_cache.read(zone_a, vpath) is None
        assert file_cache.read(zone_b, vpath) == b"data-west"


# ---------------------------------------------------------------------------
# Eviction predicate with lease state
# ---------------------------------------------------------------------------


class TestEvictionPredicateWithLease:
    def test_eviction_predicate_with_lease(self) -> None:
        """Test is_eviction_candidate with various combinations."""
        # Pass 1: only no-lease + priority 0 are candidates
        assert is_eviction_candidate(has_active_lease=False, priority=0, pass_number=1) is True
        assert is_eviction_candidate(has_active_lease=False, priority=1, pass_number=1) is False
        assert is_eviction_candidate(has_active_lease=True, priority=0, pass_number=1) is False
        assert is_eviction_candidate(has_active_lease=True, priority=1, pass_number=1) is False

        # Pass 2: no-lease regardless of priority
        assert is_eviction_candidate(has_active_lease=False, priority=0, pass_number=2) is True
        assert is_eviction_candidate(has_active_lease=False, priority=5, pass_number=2) is True
        assert is_eviction_candidate(has_active_lease=True, priority=0, pass_number=2) is False
        assert is_eviction_candidate(has_active_lease=True, priority=5, pass_number=2) is False

        # Pass 3: everything is a candidate (emergency)
        assert is_eviction_candidate(has_active_lease=False, priority=0, pass_number=3) is True
        assert is_eviction_candidate(has_active_lease=True, priority=10, pass_number=3) is True

    def test_eviction_predicate_pass_escalation(self) -> None:
        """Verify pass 1 is most restrictive, pass 3 is emergency."""
        # Entry with active lease + high priority: never evicted until pass 3
        assert is_eviction_candidate(has_active_lease=True, priority=5, pass_number=1) is False
        assert is_eviction_candidate(has_active_lease=True, priority=5, pass_number=2) is False
        assert is_eviction_candidate(has_active_lease=True, priority=5, pass_number=3) is True

        # Entry with no lease + priority 1: not evicted until pass 2
        assert is_eviction_candidate(has_active_lease=False, priority=1, pass_number=1) is False
        assert is_eviction_candidate(has_active_lease=False, priority=1, pass_number=2) is True

        # Entry with no lease + priority 0: evicted from pass 1
        assert is_eviction_candidate(has_active_lease=False, priority=0, pass_number=1) is True

        # Pass 4+ (beyond 3) also acts as emergency
        assert is_eviction_candidate(has_active_lease=True, priority=99, pass_number=4) is True


# ---------------------------------------------------------------------------
# LeaseManager callback integration with FileContentCache
# ---------------------------------------------------------------------------


class TestLeaseManagerCallbackIntegration:
    @pytest.mark.asyncio()
    async def test_lease_manager_callback_integration(
        self,
        mgr: LocalLeaseManager,
        file_cache: FileContentCache,
        clock: ManualClock,
    ) -> None:
        """Register async callback on LeaseManager; acquire+revoke fires callback.

        The callback calls mark_lease_revoked on the FileContentCache,
        demonstrating the full integration path.
        """
        callback_invocations: list[tuple[str, str]] = []

        async def on_revoke(lease: Lease, reason: str) -> None:
            callback_invocations.append((lease.resource_id, reason))
            # Bridge: propagate revocation to file cache
            file_cache.mark_lease_revoked(ZONE, lease.resource_id)

        mgr.register_revocation_callback("file-cache-bridge", on_revoke)

        # Prepare file cache state
        file_cache.mark_lease_acquired(ZONE, "r1")
        file_cache.write(ZONE, "r1", b"cached content")
        assert file_cache.read(ZONE, "r1") == b"cached content"

        # Acquire lease in LeaseManager
        lease = await mgr.acquire("r1", HOLDER, READ)
        assert lease is not None

        # Revoke lease in LeaseManager
        revoked = await mgr.revoke("r1", holder_id=HOLDER)
        assert len(revoked) == 1

        # Verify callback was invoked
        assert len(callback_invocations) == 1
        assert callback_invocations[0] == ("r1", "explicit")

        # Verify FileContentCache is now stale (set by callback)
        assert file_cache.is_stale(ZONE, "r1") is True
        assert file_cache.read(ZONE, "r1") is None
