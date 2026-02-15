"""Integration test for namespace factory wiring with L3 persistent views (Issue #1265).

Verifies the factory function (create_namespace_manager) correctly wires up the
L3 persistent view store with a real database, and that the full reconnection
flow works end-to-end:

1. Create NamespaceManager via factory with real SQLite engine
2. Grant permissions via ReBAC
3. Build namespace (triggers L3 save)
4. Simulate reconnection (new NamespaceManager, same engine)
5. Verify L3 restores the namespace instantly

This is the same code path used by fastapi_server.py when permissions are enabled.
"""

from __future__ import annotations

import pytest

from nexus.cache.persistent_view_postgres import PostgresPersistentViewStore
from nexus.rebac.namespace_factory import create_namespace_manager
from nexus.rebac.namespace_manager import MountEntry
from nexus.rebac.manager import EnhancedReBACManager
from nexus.storage.record_store import SQLAlchemyRecordStore

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def record_store():
    """Create an in-memory SQLite RecordStore with all tables."""
    rs = SQLAlchemyRecordStore(db_url="sqlite:///:memory:")
    yield rs
    rs.close()


@pytest.fixture
def rebac_manager(record_store):
    """Create an EnhancedReBACManager."""
    manager = EnhancedReBACManager(
        engine=record_store.engine,
        cache_ttl_seconds=300,
        max_depth=10,
    )
    yield manager
    manager.close()


def _grant(rebac, subject_type, subject_id, path, zone_id=None):
    """Helper: grant read access to a file path via ReBAC."""
    rebac.rebac_write(
        subject=("user", subject_id) if subject_type == "user" else (subject_type, subject_id),
        relation="direct_viewer",
        object=("file", path),
        zone_id=zone_id,
    )


# ---------------------------------------------------------------------------
# Factory Wiring Tests
# ---------------------------------------------------------------------------


class TestFactoryWiring:
    """Tests for create_namespace_manager() factory function."""

    def test_factory_creates_manager_with_l3(self, rebac_manager, record_store):
        """Factory with record_store enables L3 persistent store."""
        ns = create_namespace_manager(rebac_manager=rebac_manager, record_store=record_store)

        assert ns._persistent_store is not None
        assert isinstance(ns._persistent_store, PostgresPersistentViewStore)

    def test_factory_without_record_store_disables_l3(self, rebac_manager):
        """Factory without record_store disables L3 (graceful degradation)."""
        ns = create_namespace_manager(rebac_manager=rebac_manager, record_store=None)

        assert ns._persistent_store is None

    def test_factory_reads_env_config(self, rebac_manager, record_store, monkeypatch):
        """Factory reads NEXUS_NAMESPACE_CACHE_TTL and REVISION_WINDOW from env."""
        monkeypatch.setenv("NEXUS_NAMESPACE_CACHE_TTL", "600")
        monkeypatch.setenv("NEXUS_NAMESPACE_REVISION_WINDOW", "20")

        ns = create_namespace_manager(rebac_manager=rebac_manager, record_store=record_store)

        # Verify the config was read (check internal state)
        assert ns._revision_window == 20


# ---------------------------------------------------------------------------
# Agent Reconnection E2E Flow
# ---------------------------------------------------------------------------


class TestAgentReconnectionFlow:
    """Tests for the full agent reconnection flow via factory."""

    def test_reconnection_restores_from_l3(self, rebac_manager, record_store):
        """Full flow: build → disconnect → reconnect → L3 restores namespace.

        This simulates what happens in production:
        1. Server starts, creates NamespaceManager via factory
        2. User makes requests, namespace built from ReBAC (saved to L3)
        3. Server restarts (or worker recycled)
        4. New NamespaceManager created via factory (same RecordStore)
        5. L3 restores namespace instantly (no ReBAC query)
        """
        # --- Session 1: Initial namespace build ---
        ns1 = create_namespace_manager(rebac_manager=rebac_manager, record_store=record_store)

        # Grant permissions
        _grant(rebac_manager, "user", "agent-1", "/workspace/proj/main.py")
        _grant(rebac_manager, "user", "agent-1", "/workspace/proj/utils.py")

        # First access: L2 miss → L3 miss → ReBAC rebuild → saves to L3
        entries1 = ns1.get_mount_table(("user", "agent-1"))
        assert len(entries1) == 1
        assert entries1[0] == MountEntry(virtual_path="/workspace/proj")
        assert ns1.metrics["l3_hits"] == 0
        assert ns1.metrics["mount_table_rebuilds"] == 1

        # --- Session 2: Reconnection (new NamespaceManager, same DB) ---
        ns2 = create_namespace_manager(rebac_manager=rebac_manager, record_store=record_store)

        # Second access: L2 miss (new instance) → L3 hit → restore
        entries2 = ns2.get_mount_table(("user", "agent-1"))
        assert len(entries2) == 1
        assert entries2[0] == MountEntry(virtual_path="/workspace/proj")
        assert ns2.metrics["l3_hits"] == 1
        assert ns2.metrics["mount_table_rebuilds"] == 0  # No ReBAC query!

    def test_two_users_l3_isolation(self, rebac_manager, record_store):
        """Two users with different grants → L3 stores separate views."""
        ns = create_namespace_manager(rebac_manager=rebac_manager, record_store=record_store)

        # Grant different paths to different users
        _grant(rebac_manager, "user", "alice", "/workspace/alice-proj/data.csv")
        _grant(rebac_manager, "user", "bob", "/workspace/bob-proj/report.txt")

        # Build both namespaces
        alice_entries = ns.get_mount_table(("user", "alice"))
        bob_entries = ns.get_mount_table(("user", "bob"))

        assert alice_entries == [MountEntry(virtual_path="/workspace/alice-proj")]
        assert bob_entries == [MountEntry(virtual_path="/workspace/bob-proj")]

        # Reconnect
        ns2 = create_namespace_manager(rebac_manager=rebac_manager, record_store=record_store)

        alice_entries2 = ns2.get_mount_table(("user", "alice"))
        bob_entries2 = ns2.get_mount_table(("user", "bob"))

        assert alice_entries2 == alice_entries
        assert bob_entries2 == bob_entries
        assert ns2.metrics["l3_hits"] == 2

    def test_is_visible_after_reconnection(self, rebac_manager, record_store):
        """is_visible() works correctly after L3 restore."""
        ns1 = create_namespace_manager(rebac_manager=rebac_manager, record_store=record_store)

        _grant(rebac_manager, "user", "alice", "/workspace/proj/a.txt")
        _grant(rebac_manager, "user", "alice", "/workspace/proj/b.txt")

        # Build namespace
        assert ns1.is_visible(("user", "alice"), "/workspace/proj/a.txt")
        assert not ns1.is_visible(("user", "alice"), "/other/secret.txt")

        # Reconnect
        ns2 = create_namespace_manager(rebac_manager=rebac_manager, record_store=record_store)

        # Same visibility after L3 restore
        assert ns2.is_visible(("user", "alice"), "/workspace/proj/a.txt")
        assert ns2.is_visible(("user", "alice"), "/workspace/proj/b.txt")
        assert not ns2.is_visible(("user", "alice"), "/other/secret.txt")

    def test_filter_visible_after_reconnection(self, rebac_manager, record_store):
        """filter_visible() works correctly after L3 restore."""
        ns1 = create_namespace_manager(rebac_manager=rebac_manager, record_store=record_store)

        _grant(rebac_manager, "user", "alice", "/workspace/proj/a.txt")

        # Build namespace
        ns1.get_mount_table(("user", "alice"))

        # Reconnect
        ns2 = create_namespace_manager(rebac_manager=rebac_manager, record_store=record_store)

        paths = ["/workspace/proj/a.txt", "/other/b.txt", "/workspace/proj/c.txt"]
        visible = ns2.filter_visible(("user", "alice"), paths)
        assert visible == ["/workspace/proj/a.txt", "/workspace/proj/c.txt"]


# ---------------------------------------------------------------------------
# L3 Metrics Visibility
# ---------------------------------------------------------------------------


class TestL3Metrics:
    """Tests for L3 metrics in the factory-created NamespaceManager."""

    def test_l3_hits_in_metrics(self, rebac_manager, record_store):
        """l3_hits metric is exposed and incremented correctly."""
        ns1 = create_namespace_manager(rebac_manager=rebac_manager, record_store=record_store)

        _grant(rebac_manager, "user", "alice", "/workspace/proj/a.txt")
        ns1.get_mount_table(("user", "alice"))

        # Reconnect
        ns2 = create_namespace_manager(rebac_manager=rebac_manager, record_store=record_store)
        ns2.get_mount_table(("user", "alice"))

        metrics = ns2.metrics
        assert "l3_hits" in metrics
        assert metrics["l3_hits"] == 1
        assert metrics["mount_table_rebuilds"] == 0


# ---------------------------------------------------------------------------
# Zero-Grant Safety
# ---------------------------------------------------------------------------


class TestZeroGrantSafety:
    """Tests for fail-closed behavior with L3."""

    def test_zero_grants_after_reconnection(self, rebac_manager, record_store):
        """User with no grants → empty namespace, even after L3 restore."""
        ns1 = create_namespace_manager(rebac_manager=rebac_manager, record_store=record_store)

        # Build namespace for user with no grants
        entries1 = ns1.get_mount_table(("user", "nobody"))
        assert entries1 == []

        # Reconnect
        ns2 = create_namespace_manager(rebac_manager=rebac_manager, record_store=record_store)

        # L3 restores empty namespace (fail-closed preserved)
        entries2 = ns2.get_mount_table(("user", "nobody"))
        assert entries2 == []
        assert not ns2.is_visible(("user", "nobody"), "/workspace/anything")
