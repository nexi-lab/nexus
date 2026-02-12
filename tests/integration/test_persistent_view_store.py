"""Integration tests for PostgresPersistentViewStore (Issue #1265).

Tests with SQLite-backed store — verifies real SQL operations, JSON round-trip,
upsert semantics, and concurrent access patterns.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine

from nexus.cache.persistent_view_postgres import PostgresPersistentViewStore
from nexus.storage.models import Base

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def engine():
    """Create in-memory SQLite database with persistent_namespace_views table."""
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def store(engine):
    """Create a PostgresPersistentViewStore backed by SQLite."""
    return PostgresPersistentViewStore(engine)


# ---------------------------------------------------------------------------
# Round-Trip: save → load → verify all fields
# ---------------------------------------------------------------------------


class TestRoundTrip:
    """Tests for save/load round-trip."""

    def test_save_and_load_basic(self, store):
        """Save a view and load it back — all fields match."""
        store.save_view(
            subject_type="user",
            subject_id="alice",
            zone_id=None,
            mount_paths=["/workspace/a", "/workspace/b"],
            grants_hash="abcdef0123456789",
            revision_bucket=42,
        )

        view = store.load_view("user", "alice", None)

        assert view is not None
        assert view.subject_type == "user"
        assert view.subject_id == "alice"
        assert view.zone_id is None  # None ↔ "default" mapping
        assert view.mount_paths == ("/workspace/a", "/workspace/b")
        assert view.grants_hash == "abcdef0123456789"
        assert view.revision_bucket == 42
        assert view.created_at is not None

    def test_save_with_zone_id(self, store):
        """Save a view with explicit zone_id and load it back."""
        store.save_view(
            subject_type="agent",
            subject_id="bot-1",
            zone_id="prod-zone",
            mount_paths=["/data/shared"],
            grants_hash="1234567890abcdef",
            revision_bucket=5,
        )

        view = store.load_view("agent", "bot-1", "prod-zone")

        assert view is not None
        assert view.zone_id == "prod-zone"
        assert view.mount_paths == ("/data/shared",)

    def test_load_nonexistent_returns_none(self, store):
        """Loading a view that doesn't exist returns None."""
        view = store.load_view("user", "nobody", None)
        assert view is None


# ---------------------------------------------------------------------------
# Upsert: save twice → only latest data
# ---------------------------------------------------------------------------


class TestUpsert:
    """Tests for upsert (overwrite) semantics."""

    def test_second_save_overwrites_first(self, store):
        """Saving twice for same subject → latest data wins."""
        store.save_view(
            subject_type="user",
            subject_id="alice",
            zone_id=None,
            mount_paths=["/old/path"],
            grants_hash="old_hash_1234567",
            revision_bucket=1,
        )

        store.save_view(
            subject_type="user",
            subject_id="alice",
            zone_id=None,
            mount_paths=["/new/path/a", "/new/path/b"],
            grants_hash="new_hash_7654321",
            revision_bucket=2,
        )

        view = store.load_view("user", "alice", None)

        assert view is not None
        assert view.mount_paths == ("/new/path/a", "/new/path/b")
        assert view.grants_hash == "new_hash_7654321"
        assert view.revision_bucket == 2


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


class TestDelete:
    """Tests for delete_views()."""

    def test_delete_removes_view(self, store):
        """save → delete → load returns None."""
        store.save_view(
            subject_type="user",
            subject_id="alice",
            zone_id=None,
            mount_paths=["/workspace"],
            grants_hash="hash123456789012",
            revision_bucket=1,
        )

        deleted = store.delete_views("user", "alice")
        assert deleted == 1

        view = store.load_view("user", "alice", None)
        assert view is None

    def test_delete_nonexistent_returns_zero(self, store):
        """Deleting a view that doesn't exist returns 0."""
        deleted = store.delete_views("user", "nobody")
        assert deleted == 0

    def test_delete_removes_all_zones(self, store):
        """delete_views removes views for all zones of a subject."""
        store.save_view("user", "alice", "zone-a", ["/a"], "hash_a_1234567890", 1)
        store.save_view("user", "alice", "zone-b", ["/b"], "hash_b_1234567890", 1)

        deleted = store.delete_views("user", "alice")
        assert deleted == 2

        assert store.load_view("user", "alice", "zone-a") is None
        assert store.load_view("user", "alice", "zone-b") is None


# ---------------------------------------------------------------------------
# Zone Isolation
# ---------------------------------------------------------------------------


class TestZoneIsolation:
    """Tests for zone-based isolation."""

    def test_different_zones_are_independent(self, store):
        """View for zone A is not returned when loading zone B."""
        store.save_view("user", "alice", "zone-a", ["/data/a"], "hash_a_1234567890", 1)

        view_a = store.load_view("user", "alice", "zone-a")
        view_b = store.load_view("user", "alice", "zone-b")

        assert view_a is not None
        assert view_a.mount_paths == ("/data/a",)
        assert view_b is None

    def test_same_subject_different_zones(self, store):
        """Same subject can have different views per zone."""
        store.save_view("user", "alice", "zone-a", ["/data/a"], "hash_a_1234567890", 1)
        store.save_view("user", "alice", "zone-b", ["/data/b"], "hash_b_1234567890", 2)

        view_a = store.load_view("user", "alice", "zone-a")
        view_b = store.load_view("user", "alice", "zone-b")

        assert view_a is not None
        assert view_a.mount_paths == ("/data/a",)
        assert view_b is not None
        assert view_b.mount_paths == ("/data/b",)


# ---------------------------------------------------------------------------
# Multiple Subjects
# ---------------------------------------------------------------------------


class TestMultipleSubjects:
    """Tests for independent subject storage."""

    def test_three_subjects_independent(self, store):
        """Three different subjects stored and loaded independently."""
        store.save_view("user", "alice", None, ["/alice"], "hash_alice_12345", 1)
        store.save_view("user", "bob", None, ["/bob"], "hash_bob_12345678", 1)
        store.save_view("agent", "bot-1", None, ["/bot"], "hash_bot_12345678", 1)

        assert store.load_view("user", "alice", None).mount_paths == ("/alice",)
        assert store.load_view("user", "bob", None).mount_paths == ("/bob",)
        assert store.load_view("agent", "bot-1", None).mount_paths == ("/bot",)


# ---------------------------------------------------------------------------
# JSON Round-Trip (special characters)
# ---------------------------------------------------------------------------


class TestJsonRoundTrip:
    """Tests for JSON serialization of mount paths."""

    def test_special_chars_in_paths(self, store):
        """Mount paths with special characters survive JSON round-trip."""
        paths = ["/workspace/my project", "/data/file (1)", "/data/résumé"]

        store.save_view("user", "alice", None, paths, "hash_special_1234", 1)

        view = store.load_view("user", "alice", None)
        assert view is not None
        assert view.mount_paths == tuple(paths)

    def test_empty_mount_paths(self, store):
        """Empty mount_paths list survives JSON round-trip."""
        store.save_view("user", "alice", None, [], "hash_empty_123456", 1)

        view = store.load_view("user", "alice", None)
        assert view is not None
        assert view.mount_paths == ()

    def test_many_mount_paths(self, store):
        """Large number of mount paths survives JSON round-trip."""
        paths = [f"/workspace/project-{i}" for i in range(100)]

        store.save_view("user", "alice", None, paths, "hash_many_12345678", 1)

        view = store.load_view("user", "alice", None)
        assert view is not None
        assert view.mount_paths == tuple(paths)


# ---------------------------------------------------------------------------
# Agent Reconnection Flow
# ---------------------------------------------------------------------------


class TestAgentReconnection:
    """Tests for the agent reconnection use case."""

    def test_reconnection_flow(self, engine, store):
        """Full flow: build namespace → clear L2 → restore from L3."""
        from nexus.core.namespace_manager import MountEntry, NamespaceManager
        from nexus.core.rebac_manager_enhanced import EnhancedReBACManager

        rebac = EnhancedReBACManager(engine=engine, cache_ttl_seconds=300, max_depth=10)
        try:
            # Grant files
            rebac.rebac_write(
                subject=("user", "agent-1"),
                relation="direct_viewer",
                object=("file", "/workspace/proj/main.py"),
                zone_id=None,
            )

            ns = NamespaceManager(
                rebac_manager=rebac,
                cache_maxsize=100,
                cache_ttl=60,
                revision_window=10,
                persistent_store=store,
            )

            # First access: L2 miss → L3 miss → ReBAC rebuild → saves to L3
            entries = ns.get_mount_table(("user", "agent-1"))
            assert len(entries) == 1
            assert entries[0] == MountEntry(virtual_path="/workspace/proj")

            # Simulate reconnection: clear L2
            ns.invalidate(("user", "agent-1"))

            # Second access: L2 miss → L3 hit → restore
            entries = ns.get_mount_table(("user", "agent-1"))
            assert len(entries) == 1
            assert entries[0] == MountEntry(virtual_path="/workspace/proj")
            assert ns.metrics["l3_hits"] == 1
        finally:
            rebac.close()


# ---------------------------------------------------------------------------
# Concurrent Save (ThreadPoolExecutor)
# ---------------------------------------------------------------------------


class TestConcurrentSave:
    """Tests for thread safety of save operations."""

    def test_concurrent_save_same_subject(self):
        """5 threads saving same subject concurrently → no crash, view persisted.

        Note: SQLite has limited concurrent write support, so some writes may
        fail with contention errors. The important properties are:
        1. No unhandled crashes
        2. At least one write succeeds
        3. The final view has valid data
        In production (PostgreSQL), all writes succeed via ON CONFLICT.
        """
        from sqlalchemy.pool import StaticPool

        # Use StaticPool to share a single connection across threads
        shared_engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(shared_engine)
        shared_store = PostgresPersistentViewStore(shared_engine)

        successes = []

        def _save(i):
            try:
                shared_store.save_view(
                    subject_type="user",
                    subject_id="alice",
                    zone_id=None,
                    mount_paths=[f"/workspace/v{i}"],
                    grants_hash=f"hash_{i:016d}",
                    revision_bucket=i,
                )
                successes.append(i)
            except Exception:
                pass  # SQLite contention errors expected

        with ThreadPoolExecutor(max_workers=5) as executor:
            list(executor.map(_save, range(5)))

        # At least one write should have succeeded
        assert len(successes) >= 1, "No writes succeeded"

        # Final view should be valid
        view = shared_store.load_view("user", "alice", None)
        assert view is not None
        assert len(view.mount_paths) == 1
