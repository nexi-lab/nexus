"""Unit tests for PermissionLeaseTable (Issue #3394).

Tests the TTL-based permission lease cache: stamp, check, invalidation,
edge cases, and metrics.  Uses ManualClock for deterministic time control.

Decision record:
    - #5C: Simple TTL dict (not LeaseManager)
    - #10A: Reuse ManualClock from lib/lease.py
    - #11B: Individual test per edge case
    - #12A: Skip Hypothesis (data structure too simple)
"""

from __future__ import annotations

import pytest

pytest.importorskip("pyroaring")

from nexus.bricks.rebac.cache.permission_lease import PermissionLeaseTable
from nexus.lib.lease import ManualClock

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def clock() -> ManualClock:
    """Deterministic clock starting at t=0."""
    return ManualClock(0.0)


@pytest.fixture
def table(clock: ManualClock) -> PermissionLeaseTable:
    """PermissionLeaseTable with deterministic clock and default TTL (30s)."""
    return PermissionLeaseTable(clock=clock, ttl=30.0)


# ---------------------------------------------------------------------------
# Basic operations
# ---------------------------------------------------------------------------


class TestPermissionLeaseBasicOps:
    """Stamp, check, and invalidation fundamentals."""

    def test_stamp_then_check_returns_true(self, table: PermissionLeaseTable) -> None:
        """A freshly stamped lease is valid."""
        table.stamp("/workspace/src", "agent-A")
        assert table.check("/workspace/src", "agent-A") is True

    def test_check_without_stamp_returns_false(self, table: PermissionLeaseTable) -> None:
        """No lease exists by default."""
        assert table.check("/workspace/src", "agent-A") is False

    def test_stamp_overwrites_previous(
        self, table: PermissionLeaseTable, clock: ManualClock
    ) -> None:
        """Re-stamping extends the lease TTL."""
        table.stamp("/file.txt", "agent-A")
        clock.advance(25.0)  # 5s left on original 30s TTL
        table.stamp("/file.txt", "agent-A")  # re-stamp: new 30s from now
        clock.advance(10.0)  # original would have expired, but re-stamp is valid
        assert table.check("/file.txt", "agent-A") is True

    def test_invalidate_all_clears_everything(self, table: PermissionLeaseTable) -> None:
        """Zone-wide invalidation removes all leases."""
        table.stamp("/a", "agent-A")
        table.stamp("/b", "agent-B")
        table.stamp("/c", "agent-A")
        table.invalidate_all()
        assert table.check("/a", "agent-A") is False
        assert table.check("/b", "agent-B") is False
        assert table.check("/c", "agent-A") is False

    def test_invalidate_path_clears_all_agents_for_path(self, table: PermissionLeaseTable) -> None:
        """Path-targeted invalidation removes leases for all agents on that path."""
        table.stamp("/shared/dir", "agent-A")
        table.stamp("/shared/dir", "agent-B")
        table.stamp("/other/dir", "agent-A")
        table.invalidate_path("/shared/dir")
        assert table.check("/shared/dir", "agent-A") is False
        assert table.check("/shared/dir", "agent-B") is False
        assert table.check("/other/dir", "agent-A") is True  # untouched

    def test_multiple_agents_on_same_path(self, table: PermissionLeaseTable) -> None:
        """Different agents have independent leases on the same path."""
        table.stamp("/workspace", "agent-A")
        table.stamp("/workspace", "agent-B")
        assert table.check("/workspace", "agent-A") is True
        assert table.check("/workspace", "agent-B") is True
        assert table.check("/workspace", "agent-C") is False

    def test_multiple_paths_for_same_agent(self, table: PermissionLeaseTable) -> None:
        """Same agent can have leases on different paths."""
        table.stamp("/path/a", "agent-X")
        table.stamp("/path/b", "agent-X")
        assert table.check("/path/a", "agent-X") is True
        assert table.check("/path/b", "agent-X") is True


# ---------------------------------------------------------------------------
# TTL expiry
# ---------------------------------------------------------------------------


class TestPermissionLeaseTTLExpiry:
    """Deterministic TTL expiry using ManualClock."""

    def test_check_returns_false_after_ttl(
        self, table: PermissionLeaseTable, clock: ManualClock
    ) -> None:
        """Lease expires after TTL."""
        table.stamp("/file.txt", "agent-A")
        clock.advance(30.0)  # exactly at TTL
        assert table.check("/file.txt", "agent-A") is False

    def test_check_returns_true_just_before_ttl(
        self, table: PermissionLeaseTable, clock: ManualClock
    ) -> None:
        """Lease is valid just before TTL."""
        table.stamp("/file.txt", "agent-A")
        clock.advance(29.99)
        assert table.check("/file.txt", "agent-A") is True

    def test_custom_ttl_per_stamp(self, clock: ManualClock) -> None:
        """Individual stamps can specify a custom TTL."""
        table = PermissionLeaseTable(clock=clock, ttl=30.0)
        table.stamp("/short", "agent-A", ttl=5.0)
        table.stamp("/long", "agent-A", ttl=60.0)

        clock.advance(10.0)
        assert table.check("/short", "agent-A") is False  # expired at 5s
        assert table.check("/long", "agent-A") is True  # still valid at 60s

    def test_expiry_mid_burst_falls_through(
        self, table: PermissionLeaseTable, clock: ManualClock
    ) -> None:
        """When TTL expires during a burst, check returns False (safe fallback)."""
        table.stamp("/file.txt", "agent-A")
        assert table.check("/file.txt", "agent-A") is True

        clock.advance(31.0)
        assert table.check("/file.txt", "agent-A") is False  # expired

        # Re-stamp simulates what the hook does after a full ReBAC check
        table.stamp("/file.txt", "agent-A")
        assert table.check("/file.txt", "agent-A") is True


# ---------------------------------------------------------------------------
# Edge cases (Decision #11B: individual test per case)
# ---------------------------------------------------------------------------


class TestPermissionLeaseEdgeCases:
    """Edge cases for robustness and security."""

    def test_enforce_permissions_false_skips_lease(self) -> None:
        """When enforce_permissions is False, the hook skips everything.

        This test verifies the hook integration — the table itself has no
        concept of enforce_permissions, but the hook should not call check()
        or stamp() when permissions are disabled.
        (Tested via hook integration test, this verifies table is uninvolved.)
        """
        clock = ManualClock(0.0)
        table = PermissionLeaseTable(clock=clock)
        # Table stays empty when not used
        assert table.active_count == 0

    def test_path_normalization_trailing_slash(self, table: PermissionLeaseTable) -> None:
        """Trailing slashes are stripped for consistent keying."""
        table.stamp("/workspace/src/", "agent-A")
        assert table.check("/workspace/src", "agent-A") is True
        assert table.check("/workspace/src/", "agent-A") is True

    def test_path_normalization_root(self, table: PermissionLeaseTable) -> None:
        """Root path '/' is not stripped."""
        table.stamp("/", "agent-A")
        assert table.check("/", "agent-A") is True

    def test_size_cap_clears_table(self, clock: ManualClock) -> None:
        """Table clears when max_entries is exceeded (Decision #13D)."""
        table = PermissionLeaseTable(clock=clock, max_entries=5)
        for i in range(5):
            table.stamp(f"/file{i}", "agent-A")
        assert table.active_count == 5

        # 6th stamp triggers clear, then inserts
        table.stamp("/file_new", "agent-A")
        assert table.active_count == 1
        assert table.check("/file_new", "agent-A") is True
        assert table.check("/file0", "agent-A") is False  # cleared

    def test_invalidate_all_on_empty_table(self, table: PermissionLeaseTable) -> None:
        """Invalidating an empty table is a no-op (no metrics increment)."""
        table.invalidate_all()
        assert table.stats()["lease_invalidations"] == 0

    def test_invalidate_path_nonexistent(self, table: PermissionLeaseTable) -> None:
        """Invalidating a path with no leases is a no-op."""
        table.stamp("/exists", "agent-A")
        table.invalidate_path("/nonexistent")
        assert table.check("/exists", "agent-A") is True
        assert table.stats()["lease_invalidations"] == 0

    def test_invalidate_agent_clears_all_paths_for_agent(self, table: PermissionLeaseTable) -> None:
        """Agent invalidation removes all leases for that agent only."""
        table.stamp("/a", "agent-A")
        table.stamp("/b", "agent-A")
        table.stamp("/a", "agent-B")
        table.invalidate_agent("agent-A")
        assert table.check("/a", "agent-A") is False
        assert table.check("/b", "agent-A") is False
        assert table.check("/a", "agent-B") is True  # untouched

    def test_invalidate_agent_nonexistent(self, table: PermissionLeaseTable) -> None:
        """Invalidating an agent with no leases is a no-op."""
        table.stamp("/a", "agent-A")
        table.invalidate_agent("agent-X")
        assert table.check("/a", "agent-A") is True
        assert table.stats()["lease_invalidations"] == 0


# ---------------------------------------------------------------------------
# Inheritance-aware ancestor walk
# ---------------------------------------------------------------------------


class TestPermissionLeaseInheritance:
    """Ancestor walk: a lease on a parent covers writes to descendants."""

    def test_parent_lease_covers_child_file(self, table: PermissionLeaseTable) -> None:
        """Lease stamped on directory covers check for a file in that directory."""
        table.stamp("/workspace/src", "agent-A")
        assert table.check("/workspace/src/file.py", "agent-A") is True

    def test_grandparent_lease_covers_deep_descendant(self, table: PermissionLeaseTable) -> None:
        """Lease on grandparent covers deeply nested file."""
        table.stamp("/workspace", "agent-A")
        assert table.check("/workspace/src/nested/file.py", "agent-A") is True

    def test_root_lease_covers_any_path(self, table: PermissionLeaseTable) -> None:
        """Lease on root covers any path."""
        table.stamp("/", "agent-A")
        assert table.check("/workspace/src/file.py", "agent-A") is True

    def test_sibling_lease_does_not_cover(self, table: PermissionLeaseTable) -> None:
        """Lease on /a does NOT cover /b (not an ancestor)."""
        table.stamp("/workspace/a", "agent-A")
        assert table.check("/workspace/b", "agent-A") is False

    def test_child_lease_does_not_cover_parent(self, table: PermissionLeaseTable) -> None:
        """Lease on child does NOT cover parent (walk is upward only)."""
        table.stamp("/workspace/src/file.py", "agent-A")
        assert table.check("/workspace/src", "agent-A") is False

    def test_ancestor_walk_different_agents_independent(self, table: PermissionLeaseTable) -> None:
        """Agent B's parent lease doesn't help agent A."""
        table.stamp("/workspace/src", "agent-B")
        assert table.check("/workspace/src/file.py", "agent-A") is False
        assert table.check("/workspace/src/file.py", "agent-B") is True

    def test_exact_match_preferred_over_ancestor(
        self, table: PermissionLeaseTable, clock: ManualClock
    ) -> None:
        """Exact match is checked first (and returned) before ancestors."""
        table.stamp("/workspace/src", "agent-A", ttl=5.0)
        table.stamp("/workspace/src/file.py", "agent-A", ttl=30.0)
        clock.advance(10.0)  # parent expired, child still valid
        assert table.check("/workspace/src/file.py", "agent-A") is True

    def test_new_file_scenario_many_files_same_dir(self, table: PermissionLeaseTable) -> None:
        """Real-world: new-file write stamps parent, then existing file writes hit ancestor."""
        # First write: new file → stamps parent dir (from on_pre_write logic)
        table.stamp("/workspace/src", "agent-A")

        # Subsequent writes to existing files in same dir → ancestor walk hits
        assert table.check("/workspace/src/file1.py", "agent-A") is True
        assert table.check("/workspace/src/file2.py", "agent-A") is True
        assert table.check("/workspace/src/file3.py", "agent-A") is True

    def test_ancestor_walk_expired_ancestor_is_skip(
        self, table: PermissionLeaseTable, clock: ManualClock
    ) -> None:
        """Expired ancestor lease is skipped, walk continues but ultimately misses."""
        table.stamp("/workspace", "agent-A")
        clock.advance(31.0)  # expired
        assert table.check("/workspace/src/file.py", "agent-A") is False


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


class TestPermissionLeaseMetrics:
    """Verify operational counters (Decision #14C: add metrics)."""

    def test_hit_miss_counters(self, table: PermissionLeaseTable, clock: ManualClock) -> None:
        """Hits and misses are tracked correctly."""
        table.stamp("/file.txt", "agent-A")
        table.check("/file.txt", "agent-A")  # hit
        table.check("/file.txt", "agent-A")  # hit
        table.check("/other.txt", "agent-A")  # miss
        table.check("/file.txt", "agent-B")  # miss

        stats = table.stats()
        assert stats["lease_hits"] == 2
        assert stats["lease_misses"] == 2

    def test_stamp_counter(self, table: PermissionLeaseTable) -> None:
        """Stamp count tracks total stamps (including re-stamps)."""
        table.stamp("/a", "agent-A")
        table.stamp("/b", "agent-A")
        table.stamp("/a", "agent-A")  # re-stamp
        assert table.stats()["lease_stamps"] == 3

    def test_invalidation_counter(self, table: PermissionLeaseTable) -> None:
        """Invalidation count tracks clear events."""
        table.stamp("/a", "agent-A")
        table.invalidate_all()
        table.stamp("/b", "agent-B")
        table.invalidate_all()
        assert table.stats()["lease_invalidations"] == 2

    def test_active_leases_count(self, table: PermissionLeaseTable, clock: ManualClock) -> None:
        """active_leases reflects current table size (may include expired)."""
        table.stamp("/a", "agent-A")
        table.stamp("/b", "agent-B")
        assert table.stats()["active_leases"] == 2
        table.invalidate_all()
        assert table.stats()["active_leases"] == 0

    def test_expired_entries_still_in_active_count(
        self, table: PermissionLeaseTable, clock: ManualClock
    ) -> None:
        """Expired entries stay in the table until evicted (lazy eviction).

        active_leases counts table entries, not valid leases. This is
        expected — cleanup happens via invalidate_all() or size cap.
        """
        table.stamp("/file.txt", "agent-A")
        clock.advance(60.0)  # well past TTL
        # Entry is still in table, just expired
        assert table.stats()["active_leases"] == 1
        # But check returns False
        assert table.check("/file.txt", "agent-A") is False
