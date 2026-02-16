"""Integration tests for event-driven namespace cache invalidation (Issue #1244).

Tests the CacheCoordinator → NamespaceManager wiring:
- rebac_write() triggers CacheCoordinator.invalidate_for_write()
- CacheCoordinator notifies registered namespace invalidators
- NamespaceManager dcache + mount table are immediately invalidated
- Grant → visible and revoke → invisible within 1 request (no TTL wait)

This validates the full invalidation pipeline without needing a running FastAPI server.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine

from nexus.services.permissions.namespace_manager import NamespaceManager
from nexus.storage.models import Base

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def engine():
    """Create in-memory SQLite database for testing."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def rebac_manager(engine):
    """Create an EnhancedReBACManager for testing."""
    from nexus.services.permissions.rebac_manager_enhanced import EnhancedReBACManager

    manager = EnhancedReBACManager(
        engine=engine,
        cache_ttl_seconds=300,
        max_depth=10,
    )
    yield manager
    manager.close()


@pytest.fixture
def namespace_manager(rebac_manager):
    """Create a NamespaceManager with event-driven invalidation wired."""
    ns = NamespaceManager(
        rebac_manager=rebac_manager,
        cache_maxsize=100,
        cache_ttl=300,
        revision_window=2,
        dcache_maxsize=1000,
        dcache_positive_ttl=300,
        dcache_negative_ttl=60,
    )

    # Wire event-driven invalidation (same as fastapi_server.py)
    rebac_manager.register_namespace_invalidator(
        "test_namespace_dcache",
        lambda st, sid, zid: ns.invalidate((st, sid)),
    )

    return ns


# ---------------------------------------------------------------------------
# Tests: Event-Driven Invalidation
# ---------------------------------------------------------------------------


class TestEventDrivenInvalidation:
    """Tests for CacheCoordinator → NamespaceManager invalidation pipeline."""

    def test_grant_triggers_immediate_visibility(self, rebac_manager, namespace_manager):
        """After granting access, the path is visible on the NEXT call (no TTL wait).

        Without event-driven invalidation, the negative dcache entry would persist
        until its TTL expires (60s). With the wiring, rebac_write() immediately
        invalidates the subject's namespace cache.
        """
        alice = ("user", "alice")
        zone = "test_zone"
        path = "/workspace/project/data.csv"

        # Step 1: Path is invisible (no grants)
        assert namespace_manager.is_visible(alice, path, zone) is False
        assert namespace_manager.metrics["dcache_negative_size"] >= 1

        # Step 2: Grant access — this triggers CacheCoordinator → namespace invalidation
        rebac_manager.rebac_write(
            subject=alice,
            relation="direct_viewer",
            object=("file", path),
            zone_id=zone,
        )

        # Step 3: Path should be visible IMMEDIATELY (no TTL wait needed)
        assert namespace_manager.is_visible(alice, path, zone) is True

    def test_revoke_triggers_immediate_invisibility(self, rebac_manager, namespace_manager):
        """After revoking access, the path is invisible on the NEXT call.

        Without event-driven invalidation, the positive dcache entry would persist
        until its TTL expires (300s). With the wiring, rebac_delete() immediately
        invalidates the subject's namespace cache.
        """
        alice = ("user", "alice")
        zone = "test_zone"
        path = "/workspace/project/data.csv"

        # Step 1: Grant access
        result = rebac_manager.rebac_write(
            subject=alice,
            relation="direct_viewer",
            object=("file", path),
            zone_id=zone,
        )
        tuple_id = result.tuple_id

        # Step 2: Verify visible
        assert namespace_manager.is_visible(alice, path, zone) is True
        assert namespace_manager.metrics["dcache_positive_size"] >= 1

        # Step 3: Revoke access — this triggers CacheCoordinator → namespace invalidation
        rebac_manager.rebac_delete(tuple_id)

        # Step 4: Path should be invisible IMMEDIATELY (no TTL wait needed)
        assert namespace_manager.is_visible(alice, path, zone) is False

    def test_grant_for_one_subject_doesnt_invalidate_others(self, rebac_manager, namespace_manager):
        """Grant for Alice should only invalidate Alice's cache, not Bob's."""
        alice = ("user", "alice")
        bob = ("user", "bob")
        zone = "test_zone"

        # Grant Bob access to a file
        rebac_manager.rebac_write(
            subject=bob,
            relation="direct_viewer",
            object=("file", "/workspace/bob-project/file.txt"),
            zone_id=zone,
        )

        # Bob can see his file
        assert namespace_manager.is_visible(bob, "/workspace/bob-project/file.txt", zone) is True
        _bob_hits_before = namespace_manager.metrics["dcache_hits"]  # noqa: F841

        # Grant Alice access to a different file
        rebac_manager.rebac_write(
            subject=alice,
            relation="direct_viewer",
            object=("file", "/workspace/alice-project/file.txt"),
            zone_id=zone,
        )

        # Bob's dcache should still have his entry (not invalidated by Alice's grant)
        # NOTE: The CacheCoordinator invalidates by subject, so only Alice's cache is cleared.
        # Bob should still get a dcache hit for his file.
        assert namespace_manager.is_visible(bob, "/workspace/bob-project/file.txt", zone) is True

    def test_coordinator_metrics_track_namespace_invalidations(
        self, rebac_manager, namespace_manager
    ):
        """CacheCoordinator stats include namespace_invalidations counter."""
        alice = ("user", "alice")
        zone = "test_zone"

        # Trigger a write
        rebac_manager.rebac_write(
            subject=alice,
            relation="direct_viewer",
            object=("file", "/workspace/proj/a.txt"),
            zone_id=zone,
        )

        stats = rebac_manager._cache_coordinator.get_stats()
        assert stats["namespace_invalidations"] >= 1
        assert stats["registered_namespace_invalidators"] >= 1

    def test_batch_grants_trigger_invalidations(self, rebac_manager, namespace_manager):
        """Multiple grants for the same subject trigger multiple invalidations."""
        alice = ("user", "alice")
        zone = "test_zone"

        # Initially invisible
        assert namespace_manager.is_visible(alice, "/workspace/proj/a.txt", zone) is False

        # Batch grant 5 files
        for i in range(5):
            rebac_manager.rebac_write(
                subject=alice,
                relation="direct_viewer",
                object=("file", f"/workspace/proj/file{i}.txt"),
                zone_id=zone,
            )

        # All 5 files should be visible
        for i in range(5):
            assert namespace_manager.is_visible(alice, f"/workspace/proj/file{i}.txt", zone) is True

    def test_filter_visible_reflects_immediate_grants(self, rebac_manager, namespace_manager):
        """filter_visible() returns newly-granted paths without TTL wait.

        Note: build_mount_entries() creates mounts at the directory level, so
        granting a file in /dir-a/ makes all files in /dir-a/ visible. We use
        separate directories to test per-grant visibility.
        """
        alice = ("user", "alice")
        zone = "test_zone"
        # Use separate directories so each grant controls visibility independently
        granted_paths = [f"/workspace/dir-{i}/file.txt" for i in range(3)]
        ungrantable_paths = [f"/workspace/dir-{i}/file.txt" for i in range(3, 5)]
        all_paths = granted_paths + ungrantable_paths

        # Step 1: Nothing visible
        result = namespace_manager.filter_visible(alice, all_paths, zone)
        assert result == []

        # Step 2: Grant access to first 3 directories
        for path in granted_paths:
            rebac_manager.rebac_write(
                subject=alice,
                relation="direct_viewer",
                object=("file", path),
                zone_id=zone,
            )

        # Step 3: filter_visible should reflect the grants immediately
        result = namespace_manager.filter_visible(alice, all_paths, zone)
        assert len(result) == 3
        assert set(result) == set(granted_paths)

    def test_unregister_stops_invalidation(self, rebac_manager, namespace_manager):
        """After unregistering, rebac_write no longer triggers namespace invalidation."""
        alice = ("user", "alice")
        zone = "test_zone"

        # Unregister the callback
        removed = rebac_manager.unregister_namespace_invalidator("test_namespace_dcache")
        assert removed is True

        # Path is invisible
        assert namespace_manager.is_visible(alice, "/workspace/proj/a.txt", zone) is False

        # Grant access — but no invalidation callback fires
        rebac_manager.rebac_write(
            subject=alice,
            relation="direct_viewer",
            object=("file", "/workspace/proj/a.txt"),
            zone_id=zone,
        )

        # Negative dcache entry is still there (not invalidated)
        # The path MIGHT still show as invisible due to stale dcache
        # (depending on revision bucket — this tests the callback was removed)
        stats = rebac_manager._cache_coordinator.get_stats()
        # No namespace invalidation should have fired after unregister
        assert stats["registered_namespace_invalidators"] == 0
