"""Cross-mount cache coherence tests (Issue #3397).

Verifies that two FUSELeaseCoordinator instances sharing a single
LocalLeaseManager correctly invalidate each other's caches when
mutations occur.  Uses SystemClock for real-time coordination since
the coordinator's validity cache uses time.monotonic() directly.
"""

import pytest

from nexus.fuse.cache import FUSECacheManager
from nexus.fuse.lease_coordinator import FUSELeaseCoordinator
from nexus.lib.lease import LocalLeaseManager, SystemClock


@pytest.fixture()
def shared_lease_manager() -> LocalLeaseManager:
    """Shared lease manager used by both mounts.

    Uses SystemClock (not ManualClock) because the coordinator's local
    validity cache compares against time.monotonic() directly.
    """
    return LocalLeaseManager(zone_id="test-zone", clock=SystemClock(), sweep_interval=3600.0)


def _make_coordinator(
    lease_manager: LocalLeaseManager,
    holder_id: str,
) -> FUSELeaseCoordinator:
    """Create a coordinator with a fresh cache backed by the shared lease manager."""
    cache = FUSECacheManager(
        attr_cache_size=128,
        attr_cache_ttl=300,  # long TTL so TTL expiry doesn't interfere
        content_cache_size=128,
        parsed_cache_size=16,
    )
    return FUSELeaseCoordinator(
        cache=cache,
        lease_manager=lease_manager,
        holder_id=holder_id,
        lease_ttl=30.0,
        acquire_timeout=5.0,
    )


class TestCrossMountCoherence:
    """Two coordinators (mount-A, mount-B) sharing one LocalLeaseManager."""

    def test_write_invalidates_other_mount_attr_cache(
        self,
        shared_lease_manager: LocalLeaseManager,
    ) -> None:
        """Mount A writes → Mount B's attr cache is invalidated."""
        coord_a = _make_coordinator(shared_lease_manager, "mount-A")
        coord_b = _make_coordinator(shared_lease_manager, "mount-B")
        try:
            # Mount B reads and caches attrs via lease
            attrs_b = coord_b.lease_gated_get(
                path="/file.txt",
                cache_get=lambda: coord_b.get_attr("/file.txt"),
                cache_set=lambda v: coord_b.cache_attr("/file.txt", v),
                fetch_fn=lambda: {"st_size": 100},
            )
            assert attrs_b == {"st_size": 100}
            assert coord_b.get_attr("/file.txt") == {"st_size": 100}

            # Mount A writes the file → invalidate + revoke
            coord_a.invalidate_and_revoke(["/file.txt"])

            # Give the async revocation callback time to execute
            import time

            time.sleep(0.3)

            # Mount B's cache should be cleared by revocation callback
            assert coord_b.get_attr("/file.txt") is None
            assert not coord_b._check_validity("/file.txt")
        finally:
            coord_a.close()
            coord_b.close()

    def test_write_invalidates_other_mount_content_cache(
        self,
        shared_lease_manager: LocalLeaseManager,
    ) -> None:
        """Mount A writes → Mount B's content cache is invalidated."""
        coord_a = _make_coordinator(shared_lease_manager, "mount-A")
        coord_b = _make_coordinator(shared_lease_manager, "mount-B")
        try:
            # Mount B reads content
            content_b = coord_b.lease_gated_get(
                path="/file.txt",
                cache_get=lambda: coord_b.get_content("/file.txt"),
                cache_set=lambda v: coord_b.cache_content("/file.txt", v),
                fetch_fn=lambda: b"original content",
            )
            assert content_b == b"original content"

            # Mount A writes
            coord_a.invalidate_and_revoke(["/file.txt"])

            import time

            time.sleep(0.3)

            # Mount B's content cache should be cleared
            assert coord_b.get_content("/file.txt") is None
        finally:
            coord_a.close()
            coord_b.close()

    def test_delete_invalidates_other_mount(
        self,
        shared_lease_manager: LocalLeaseManager,
    ) -> None:
        """Mount A deletes file → Mount B's caches are invalidated."""
        coord_a = _make_coordinator(shared_lease_manager, "mount-A")
        coord_b = _make_coordinator(shared_lease_manager, "mount-B")
        try:
            # Mount B caches data
            coord_b.lease_gated_get(
                path="/deleted.txt",
                cache_get=lambda: coord_b.get_attr("/deleted.txt"),
                cache_set=lambda v: coord_b.cache_attr("/deleted.txt", v),
                fetch_fn=lambda: {"st_size": 50},
            )
            assert coord_b.get_attr("/deleted.txt") is not None

            # Mount A deletes
            coord_a.invalidate_and_revoke(["/deleted.txt"])

            import time

            time.sleep(0.3)

            assert coord_b.get_attr("/deleted.txt") is None
        finally:
            coord_a.close()
            coord_b.close()

    def test_rename_invalidates_both_paths(
        self,
        shared_lease_manager: LocalLeaseManager,
    ) -> None:
        """Mount A renames → both old and new paths invalidated on Mount B."""
        coord_a = _make_coordinator(shared_lease_manager, "mount-A")
        coord_b = _make_coordinator(shared_lease_manager, "mount-B")
        try:
            # Mount B caches old path (via lease)
            coord_b.lease_gated_get(
                path="/old.txt",
                cache_get=lambda: coord_b.get_attr("/old.txt"),
                cache_set=lambda v: coord_b.cache_attr("/old.txt", v),
                fetch_fn=lambda: {"st_size": 10},
            )
            # Mount B also caches new path (via lease)
            coord_b.lease_gated_get(
                path="/new.txt",
                cache_get=lambda: coord_b.get_attr("/new.txt"),
                cache_set=lambda v: coord_b.cache_attr("/new.txt", v),
                fetch_fn=lambda: {"st_size": 0},
            )

            # Mount A renames old → new
            coord_a.invalidate_and_revoke(["/old.txt", "/new.txt"])

            import time

            time.sleep(0.3)

            assert coord_b.get_attr("/old.txt") is None
            assert coord_b.get_attr("/new.txt") is None
        finally:
            coord_a.close()
            coord_b.close()

    def test_concurrent_readers_both_acquire_shared_leases(
        self,
        shared_lease_manager: LocalLeaseManager,
    ) -> None:
        """Both mounts can hold SHARED_READ leases simultaneously."""
        coord_a = _make_coordinator(shared_lease_manager, "mount-A")
        coord_b = _make_coordinator(shared_lease_manager, "mount-B")
        try:
            # Both mounts read the same file
            result_a = coord_a.lease_gated_get(
                path="/shared.txt",
                cache_get=lambda: coord_a.get_attr("/shared.txt"),
                cache_set=lambda v: coord_a.cache_attr("/shared.txt", v),
                fetch_fn=lambda: {"st_size": 200},
            )
            result_b = coord_b.lease_gated_get(
                path="/shared.txt",
                cache_get=lambda: coord_b.get_attr("/shared.txt"),
                cache_set=lambda v: coord_b.cache_attr("/shared.txt", v),
                fetch_fn=lambda: {"st_size": 200},
            )

            assert result_a == {"st_size": 200}
            assert result_b == {"st_size": 200}

            # Both should have valid leases
            assert coord_a._check_validity("/shared.txt")
            assert coord_b._check_validity("/shared.txt")
        finally:
            coord_a.close()
            coord_b.close()

    def test_write_during_read_invalidates_reader(
        self,
        shared_lease_manager: LocalLeaseManager,
    ) -> None:
        """Mount B is reading, Mount A writes → Mount B re-fetches on next read."""
        coord_a = _make_coordinator(shared_lease_manager, "mount-A")
        coord_b = _make_coordinator(shared_lease_manager, "mount-B")
        try:
            # Mount B reads (gets v1)
            v1 = coord_b.lease_gated_get(
                path="/data.txt",
                cache_get=lambda: coord_b.get_content("/data.txt"),
                cache_set=lambda v: coord_b.cache_content("/data.txt", v),
                fetch_fn=lambda: b"version-1",
            )
            assert v1 == b"version-1"

            # Mount A writes (v2)
            coord_a.invalidate_and_revoke(["/data.txt"])

            import time

            time.sleep(0.3)

            # Mount B reads again — should get v2 from fetch (cache was invalidated)
            v2 = coord_b.lease_gated_get(
                path="/data.txt",
                cache_get=lambda: coord_b.get_content("/data.txt"),
                cache_set=lambda v: coord_b.cache_content("/data.txt", v),
                fetch_fn=lambda: b"version-2",
            )
            assert v2 == b"version-2"
        finally:
            coord_a.close()
            coord_b.close()

    def test_local_invalidation_is_immediate(
        self,
        shared_lease_manager: LocalLeaseManager,
    ) -> None:
        """Writer's own cache is invalidated immediately (Decision 4A)."""
        coord_a = _make_coordinator(shared_lease_manager, "mount-A")
        try:
            # Mount A reads and caches
            coord_a.lease_gated_get(
                path="/file.txt",
                cache_get=lambda: coord_a.get_attr("/file.txt"),
                cache_set=lambda v: coord_a.cache_attr("/file.txt", v),
                fetch_fn=lambda: {"st_size": 100},
            )
            assert coord_a.get_attr("/file.txt") is not None

            # Mount A writes — should be invalidated IMMEDIATELY (no async wait)
            coord_a.invalidate_and_revoke(["/file.txt"])
            assert coord_a.get_attr("/file.txt") is None
        finally:
            coord_a.close()


class TestCoherenceEdgeCases:
    """Edge cases for cross-mount coherence."""

    def test_invalidate_nonexistent_path_is_safe(
        self,
        shared_lease_manager: LocalLeaseManager,
    ) -> None:
        """Invalidating a path with no cache entry or lease is a no-op."""
        coord = _make_coordinator(shared_lease_manager, "mount-A")
        try:
            coord.invalidate_and_revoke(["/nonexistent.txt"])
            # Should not raise
        finally:
            coord.close()

    def test_multiple_invalidations_same_path(
        self,
        shared_lease_manager: LocalLeaseManager,
    ) -> None:
        """Multiple rapid invalidations on same path don't cause issues."""
        coord_a = _make_coordinator(shared_lease_manager, "mount-A")
        coord_b = _make_coordinator(shared_lease_manager, "mount-B")
        try:
            coord_b.lease_gated_get(
                path="/rapid.txt",
                cache_get=lambda: coord_b.get_attr("/rapid.txt"),
                cache_set=lambda v: coord_b.cache_attr("/rapid.txt", v),
                fetch_fn=lambda: {"st_size": 1},
            )

            # Rapid-fire invalidations from mount A
            for _ in range(10):
                coord_a.invalidate_and_revoke(["/rapid.txt"])

            import time

            time.sleep(0.3)

            assert coord_b.get_attr("/rapid.txt") is None
        finally:
            coord_a.close()
            coord_b.close()
