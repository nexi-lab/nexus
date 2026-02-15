"""Unit tests for L3 persistent namespace views (Issue #1265).

Tests cover:
- L3 hit: L2 miss → L3 has valid view → restores to L2, returns
- L3 miss: L2 miss → L3 returns None → falls through to ReBAC
- L3 stale: L2 miss → L3 has old revision bucket → discards, rebuilds
- L3 save on rebuild: After ReBAC rebuild, save_view() called with correct args
- L3 disabled: persistent_store=None → no L3 calls, works as before
- DB failure on load: load_view() throws → falls through to ReBAC
- DB failure on save: save_view() throws → continues without error
- grants_hash preserved: L3 restore populates L2 with correct grants_hash
- Metrics: l3_hits counter increments correctly
- Empty mount table: L3 stores/restores empty mount paths correctly
- Zone isolation: View for zone A, load for zone B → miss
- Concurrent writes: Two threads save same subject → no crash
- Invalidation: Revision bucket changes → L3 stale → rebuild
- Revoked access: Grant removed → revision bump → smaller mount table
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine

from nexus.core.persistent_view_store import PersistentView
from nexus.rebac.namespace_manager import MountEntry, NamespaceManager
from nexus.storage.models import Base

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def engine():
    """Create in-memory SQLite database for testing."""
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def enhanced_rebac_manager(engine):
    """Create an EnhancedReBACManager for testing."""
    from nexus.rebac.manager import EnhancedReBACManager

    manager = EnhancedReBACManager(
        engine=engine,
        cache_ttl_seconds=300,
        max_depth=10,
    )
    yield manager
    manager.close()


@pytest.fixture
def mock_persistent_store():
    """Create a mock PersistentViewStore."""
    store = MagicMock()
    store.save_view = MagicMock(return_value=None)
    store.load_view = MagicMock(return_value=None)
    store.delete_views = MagicMock(return_value=0)
    return store


@pytest.fixture
def namespace_manager_with_l3(enhanced_rebac_manager, mock_persistent_store):
    """Create a NamespaceManager with L3 persistent store."""
    return NamespaceManager(
        rebac_manager=enhanced_rebac_manager,
        cache_maxsize=100,
        cache_ttl=60,
        revision_window=10,
        persistent_store=mock_persistent_store,
    )


@pytest.fixture
def namespace_manager_no_l3(enhanced_rebac_manager):
    """Create a NamespaceManager without L3 (persistent_store=None)."""
    return NamespaceManager(
        rebac_manager=enhanced_rebac_manager,
        cache_maxsize=100,
        cache_ttl=60,
        revision_window=10,
    )


def _grant_file(rebac_manager, subject_type, subject_id, path, zone_id=None):
    """Helper: create a ReBAC grant for a file path (direct_viewer → read)."""
    rebac_manager.rebac_write(
        subject=(subject_type, subject_id),
        relation="direct_viewer",
        object=("file", path),
        zone_id=zone_id,
    )


_SENTINEL = object()


def _make_persistent_view(
    subject_type="user",
    subject_id="alice",
    zone_id=None,
    mount_paths=_SENTINEL,
    grants_hash="abcdef0123456789",
    revision_bucket=0,
):
    """Helper: create a PersistentView with defaults."""
    from datetime import UTC, datetime

    if mount_paths is _SENTINEL:
        resolved_paths: tuple[str, ...] = ("/workspace/proj",)
    else:
        resolved_paths = tuple(mount_paths)  # type: ignore[arg-type]

    return PersistentView(
        subject_type=subject_type,
        subject_id=subject_id,
        zone_id=zone_id,
        mount_paths=resolved_paths,
        grants_hash=grants_hash,
        revision_bucket=revision_bucket,
        created_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# L3 Hit: L2 miss → L3 has valid view → restores to L2
# ---------------------------------------------------------------------------


class TestL3Hit:
    """Tests for successful L3 cache hits."""

    def test_l3_hit_restores_mount_table(
        self, namespace_manager_with_l3, mock_persistent_store, enhanced_rebac_manager
    ):
        """L2 miss + L3 hit with matching revision → restores to L2."""
        # Set up: L3 has a valid view
        view = _make_persistent_view(
            mount_paths=["/workspace/proj"],
            revision_bucket=0,
        )
        mock_persistent_store.load_view.return_value = view

        # Act: query mount table (L2 is empty, should hit L3)
        entries = namespace_manager_with_l3.get_mount_table(("user", "alice"))

        # Assert: restored from L3
        assert len(entries) == 1
        assert entries[0] == MountEntry(virtual_path="/workspace/proj")
        mock_persistent_store.load_view.assert_called_once_with("user", "alice", None)

    def test_l3_hit_populates_l2_cache(self, namespace_manager_with_l3, mock_persistent_store):
        """L3 hit should populate L2 so next call hits L2 (no L3 load)."""
        view = _make_persistent_view(mount_paths=["/workspace/proj"], revision_bucket=0)
        mock_persistent_store.load_view.return_value = view

        # First call: L3 hit
        namespace_manager_with_l3.get_mount_table(("user", "alice"))
        assert mock_persistent_store.load_view.call_count == 1

        # Second call: should hit L2 (no additional L3 load)
        namespace_manager_with_l3.get_mount_table(("user", "alice"))
        assert mock_persistent_store.load_view.call_count == 1

    def test_l3_hit_increments_metric(self, namespace_manager_with_l3, mock_persistent_store):
        """L3 hit should increment l3_hits metric."""
        view = _make_persistent_view(mount_paths=["/workspace/proj"], revision_bucket=0)
        mock_persistent_store.load_view.return_value = view

        assert namespace_manager_with_l3.metrics["l3_hits"] == 0
        namespace_manager_with_l3.get_mount_table(("user", "alice"))
        assert namespace_manager_with_l3.metrics["l3_hits"] == 1


# ---------------------------------------------------------------------------
# L3 Miss: L3 returns None → falls through to ReBAC
# ---------------------------------------------------------------------------


class TestL3Miss:
    """Tests for L3 cache misses."""

    def test_l3_miss_triggers_rebac_rebuild(
        self, namespace_manager_with_l3, mock_persistent_store, enhanced_rebac_manager
    ):
        """L3 returns None → falls through to ReBAC rebuild."""
        mock_persistent_store.load_view.return_value = None

        # Grant a file via ReBAC
        _grant_file(enhanced_rebac_manager, "user", "alice", "/workspace/proj/a.txt")

        entries = namespace_manager_with_l3.get_mount_table(("user", "alice"))
        assert len(entries) == 1
        assert entries[0] == MountEntry(virtual_path="/workspace/proj")

        # L3 was consulted but returned None
        mock_persistent_store.load_view.assert_called_once()
        # Rebuild should also save to L3
        mock_persistent_store.save_view.assert_called_once()


# ---------------------------------------------------------------------------
# L3 Stale: old revision bucket → discards, rebuilds
# ---------------------------------------------------------------------------


class TestL3Stale:
    """Tests for stale L3 views."""

    def test_stale_l3_view_triggers_rebuild(
        self, namespace_manager_with_l3, mock_persistent_store, enhanced_rebac_manager
    ):
        """L3 view with old revision bucket → discard + rebuild from ReBAC."""
        # L3 has a view from revision_bucket=999 (stale)
        stale_view = _make_persistent_view(
            mount_paths=["/old/path"],
            revision_bucket=999,
        )
        mock_persistent_store.load_view.return_value = stale_view

        # Grant actual file
        _grant_file(enhanced_rebac_manager, "user", "alice", "/workspace/real/a.txt")

        entries = namespace_manager_with_l3.get_mount_table(("user", "alice"))
        # Should get fresh data from ReBAC, not stale L3
        assert len(entries) == 1
        assert entries[0] == MountEntry(virtual_path="/workspace/real")

    def test_stale_l3_does_not_increment_l3_hits(
        self, namespace_manager_with_l3, mock_persistent_store, enhanced_rebac_manager
    ):
        """Stale L3 view should NOT increment l3_hits."""
        stale_view = _make_persistent_view(revision_bucket=999)
        mock_persistent_store.load_view.return_value = stale_view

        _grant_file(enhanced_rebac_manager, "user", "alice", "/workspace/proj/a.txt")
        namespace_manager_with_l3.get_mount_table(("user", "alice"))

        assert namespace_manager_with_l3.metrics["l3_hits"] == 0


# ---------------------------------------------------------------------------
# L3 Save on Rebuild
# ---------------------------------------------------------------------------


class TestL3SaveOnRebuild:
    """Tests for L3 persistence after ReBAC rebuild."""

    def test_save_view_called_after_rebuild(
        self, namespace_manager_with_l3, mock_persistent_store, enhanced_rebac_manager
    ):
        """After ReBAC rebuild, save_view() is called with correct args."""
        mock_persistent_store.load_view.return_value = None

        _grant_file(enhanced_rebac_manager, "user", "bob", "/workspace/data/file.csv")

        namespace_manager_with_l3.get_mount_table(("user", "bob"))

        mock_persistent_store.save_view.assert_called_once()
        call_kwargs = mock_persistent_store.save_view.call_args
        args = call_kwargs[1] if call_kwargs[1] else {}
        # If called with positional args
        if not args:
            pos_args = call_kwargs[0]
            assert pos_args[0] == "user"  # subject_type
            assert pos_args[1] == "bob"  # subject_id
            assert pos_args[3] == ["/workspace/data"]  # mount_paths
        else:
            assert args["subject_type"] == "user"
            assert args["subject_id"] == "bob"
            assert args["mount_paths"] == ["/workspace/data"]

    def test_save_includes_grants_hash(
        self, namespace_manager_with_l3, mock_persistent_store, enhanced_rebac_manager
    ):
        """save_view() should include a non-empty grants_hash."""
        mock_persistent_store.load_view.return_value = None

        _grant_file(enhanced_rebac_manager, "user", "charlie", "/workspace/x/y.txt")
        namespace_manager_with_l3.get_mount_table(("user", "charlie"))

        call_args = mock_persistent_store.save_view.call_args
        # grants_hash is the 5th positional arg or keyword
        grants_hash = call_args[1]["grants_hash"] if call_args[1] else call_args[0][4]
        assert len(grants_hash) == 16
        assert all(c in "0123456789abcdef" for c in grants_hash)


# ---------------------------------------------------------------------------
# L3 Disabled (persistent_store=None)
# ---------------------------------------------------------------------------


class TestL3Disabled:
    """Tests for graceful degradation when L3 is disabled."""

    def test_no_l3_works_normally(self, namespace_manager_no_l3, enhanced_rebac_manager):
        """With persistent_store=None, system works as before (L2 + ReBAC)."""
        _grant_file(enhanced_rebac_manager, "user", "alice", "/workspace/proj/a.txt")

        entries = namespace_manager_no_l3.get_mount_table(("user", "alice"))
        assert len(entries) == 1
        assert entries[0] == MountEntry(virtual_path="/workspace/proj")

    def test_no_l3_metrics_show_zero_l3_hits(self, namespace_manager_no_l3):
        """l3_hits should be 0 when L3 is disabled."""
        assert namespace_manager_no_l3.metrics["l3_hits"] == 0


# ---------------------------------------------------------------------------
# DB Failure on Load
# ---------------------------------------------------------------------------


class TestL3LoadFailure:
    """Tests for L3 load errors (graceful fallback to ReBAC)."""

    def test_load_exception_falls_through_to_rebac(
        self, namespace_manager_with_l3, mock_persistent_store, enhanced_rebac_manager
    ):
        """load_view() throws → falls through to ReBAC rebuild."""
        mock_persistent_store.load_view.side_effect = RuntimeError("DB connection lost")

        _grant_file(enhanced_rebac_manager, "user", "alice", "/workspace/proj/a.txt")

        entries = namespace_manager_with_l3.get_mount_table(("user", "alice"))
        assert len(entries) == 1
        assert entries[0] == MountEntry(virtual_path="/workspace/proj")


# ---------------------------------------------------------------------------
# DB Failure on Save
# ---------------------------------------------------------------------------


class TestL3SaveFailure:
    """Tests for L3 save errors (best-effort, non-blocking)."""

    def test_save_exception_does_not_break_rebuild(
        self, namespace_manager_with_l3, mock_persistent_store, enhanced_rebac_manager
    ):
        """save_view() throws → rebuild still succeeds, returns correct data."""
        mock_persistent_store.load_view.return_value = None
        mock_persistent_store.save_view.side_effect = RuntimeError("DB write failed")

        _grant_file(enhanced_rebac_manager, "user", "alice", "/workspace/proj/a.txt")

        entries = namespace_manager_with_l3.get_mount_table(("user", "alice"))
        assert len(entries) == 1
        assert entries[0] == MountEntry(virtual_path="/workspace/proj")


# ---------------------------------------------------------------------------
# grants_hash Preserved
# ---------------------------------------------------------------------------


class TestGrantsHashPreserved:
    """Tests for grants_hash round-trip through L3."""

    def test_l3_restore_preserves_grants_hash(
        self, namespace_manager_with_l3, mock_persistent_store
    ):
        """L3 restore should populate L2 with correct grants_hash."""
        expected_hash = "deadbeef12345678"
        view = _make_persistent_view(
            mount_paths=["/workspace/proj"],
            grants_hash=expected_hash,
            revision_bucket=0,
        )
        mock_persistent_store.load_view.return_value = view

        namespace_manager_with_l3.get_mount_table(("user", "alice"))

        actual_hash = namespace_manager_with_l3.get_grants_hash(("user", "alice"))
        assert actual_hash == expected_hash


# ---------------------------------------------------------------------------
# Empty Mount Table
# ---------------------------------------------------------------------------


class TestEmptyMountTable:
    """Tests for L3 with empty mount paths."""

    def test_l3_stores_empty_mount_paths(
        self, namespace_manager_with_l3, mock_persistent_store, enhanced_rebac_manager
    ):
        """Subject with no grants → empty mount_paths saved to L3."""
        mock_persistent_store.load_view.return_value = None

        # No grants for this subject → empty mount table
        entries = namespace_manager_with_l3.get_mount_table(("user", "nobody"))
        assert entries == []

        # save_view should still be called (with empty mount_paths)
        mock_persistent_store.save_view.assert_called_once()
        call_args = mock_persistent_store.save_view.call_args
        mount_paths = call_args[1]["mount_paths"] if call_args[1] else call_args[0][3]
        assert mount_paths == []

    def test_l3_restores_empty_mount_paths(self, namespace_manager_with_l3, mock_persistent_store):
        """L3 with empty mount_paths → restores empty mount table."""
        view = _make_persistent_view(mount_paths=[], revision_bucket=0)
        mock_persistent_store.load_view.return_value = view

        entries = namespace_manager_with_l3.get_mount_table(("user", "nobody"))
        assert entries == []
        assert namespace_manager_with_l3.metrics["l3_hits"] == 1


# ---------------------------------------------------------------------------
# Zone Isolation
# ---------------------------------------------------------------------------


class TestZoneIsolation:
    """Tests for L3 zone-based isolation."""

    def test_zone_a_view_not_returned_for_zone_b(
        self, namespace_manager_with_l3, mock_persistent_store, enhanced_rebac_manager
    ):
        """View stored for zone A, load for zone B → L3 returns None → rebuild."""
        # L3 returns None for zone B (only has zone A)
        mock_persistent_store.load_view.return_value = None

        _grant_file(
            enhanced_rebac_manager, "user", "alice", "/workspace/proj/a.txt", zone_id="zone-b"
        )

        entries = namespace_manager_with_l3.get_mount_table(("user", "alice"), zone_id="zone-b")
        assert len(entries) == 1

        # load_view called with zone_id="zone-b"
        mock_persistent_store.load_view.assert_called_with("user", "alice", "zone-b")


# ---------------------------------------------------------------------------
# Concurrent Writes
# ---------------------------------------------------------------------------


class TestConcurrentAccess:
    """Tests for thread safety of L3 interactions."""

    def test_concurrent_l3_access_no_crash(
        self, namespace_manager_with_l3, mock_persistent_store, enhanced_rebac_manager
    ):
        """Multiple threads triggering L3 save for same subject → no crash."""
        mock_persistent_store.load_view.return_value = None

        _grant_file(enhanced_rebac_manager, "user", "alice", "/workspace/proj/a.txt")

        errors: list[Exception] = []

        def _query():
            try:
                namespace_manager_with_l3.get_mount_table(("user", "alice"))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=_query) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Concurrent access errors: {errors}"


# ---------------------------------------------------------------------------
# Invalidation: Revision Bucket Changes
# ---------------------------------------------------------------------------


class TestRevisionInvalidation:
    """Tests for L3 invalidation via revision bucket changes."""

    def test_revision_change_invalidates_l3(self, enhanced_rebac_manager, mock_persistent_store):
        """When zone revision changes bucket, L3 view should be stale → rebuild."""
        from unittest.mock import patch

        ns = NamespaceManager(
            rebac_manager=enhanced_rebac_manager,
            cache_maxsize=100,
            cache_ttl=60,
            revision_window=10,
            persistent_store=mock_persistent_store,
        )

        # L3 has view at bucket 0
        view = _make_persistent_view(
            mount_paths=["/workspace/old"],
            revision_bucket=0,
        )
        mock_persistent_store.load_view.return_value = view

        # First access: revision is 0, bucket is 0 → L3 hit
        with patch.object(enhanced_rebac_manager, "_get_zone_revision", return_value=0):
            entries = ns.get_mount_table(("user", "alice"))
        assert entries[0] == MountEntry(virtual_path="/workspace/old")
        assert ns.metrics["l3_hits"] == 1

        # Clear L2 cache to force L3 re-check
        ns.invalidate(("user", "alice"))

        # Grant a file so ReBAC rebuild returns something
        _grant_file(enhanced_rebac_manager, "user", "alice", "/workspace/new/x.txt")

        # Now revision jumps to 15 → bucket 1, but L3 view is at bucket 0 → stale
        with patch.object(enhanced_rebac_manager, "_get_zone_revision", return_value=15):
            entries = ns.get_mount_table(("user", "alice"))

        mount_paths = [e.virtual_path for e in entries]
        assert "/workspace/new" in mount_paths
        # l3_hits should still be 1 (the stale view was discarded)
        assert ns.metrics["l3_hits"] == 1


# ---------------------------------------------------------------------------
# Revoked Access
# ---------------------------------------------------------------------------


class TestRevokedAccess:
    """Tests for grant revocation → L3 stale → smaller mount table."""

    def test_revoked_grant_produces_smaller_mount_table(
        self, enhanced_rebac_manager, mock_persistent_store
    ):
        """Grant removed → revision bump → L3 stale → rebuild with fewer mounts."""
        ns = NamespaceManager(
            rebac_manager=enhanced_rebac_manager,
            cache_maxsize=100,
            cache_ttl=60,
            revision_window=10,
            persistent_store=mock_persistent_store,
        )

        # L3 has old view with 2 mount paths
        old_view = _make_persistent_view(
            mount_paths=["/workspace/a", "/workspace/b"],
            revision_bucket=999,  # Intentionally stale
        )
        mock_persistent_store.load_view.return_value = old_view

        # Only grant one path
        _grant_file(enhanced_rebac_manager, "user", "alice", "/workspace/a/file.txt")

        entries = ns.get_mount_table(("user", "alice"))
        # Should only have /workspace/a (not /workspace/b — revoked)
        assert len(entries) == 1
        assert entries[0] == MountEntry(virtual_path="/workspace/a")


# ---------------------------------------------------------------------------
# is_visible with L3
# ---------------------------------------------------------------------------


class TestIsVisibleWithL3:
    """Tests for is_visible() interaction with L3 layer."""

    def test_is_visible_uses_l3_restored_mount_table(
        self, namespace_manager_with_l3, mock_persistent_store
    ):
        """is_visible() should use mount table restored from L3."""
        view = _make_persistent_view(
            mount_paths=["/workspace/proj"],
            revision_bucket=0,
        )
        mock_persistent_store.load_view.return_value = view

        assert namespace_manager_with_l3.is_visible(("user", "alice"), "/workspace/proj/file.txt")
        assert not namespace_manager_with_l3.is_visible(("user", "alice"), "/other/path")

    def test_filter_visible_uses_l3_restored_mount_table(
        self, namespace_manager_with_l3, mock_persistent_store
    ):
        """filter_visible() should use mount table restored from L3."""
        view = _make_persistent_view(
            mount_paths=["/workspace/proj"],
            revision_bucket=0,
        )
        mock_persistent_store.load_view.return_value = view

        paths = ["/workspace/proj/a.txt", "/other/b.txt", "/workspace/proj/c.txt"]
        visible = namespace_manager_with_l3.filter_visible(("user", "alice"), paths)
        assert visible == ["/workspace/proj/a.txt", "/workspace/proj/c.txt"]
