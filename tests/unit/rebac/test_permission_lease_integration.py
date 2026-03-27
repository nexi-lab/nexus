"""Integration tests for permission write leases (Issue #3394).

Tests the full composition:
- PermissionCheckHook with PermissionLeaseTable (fast path + slow path)
- CacheCoordinator with lease invalidation callback
- Security regression: permission revocation invalidates leases

Decision record:
    - #9C: Both unit and integration tests for security-critical path
    - #2C: Callback registration pattern for CacheCoordinator
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

pytest.importorskip("pyroaring")

from nexus.bricks.rebac.cache.coordinator import CacheCoordinator
from nexus.bricks.rebac.cache.permission_lease import PermissionLeaseTable
from nexus.bricks.rebac.permission_hook import PermissionCheckHook
from nexus.contracts.vfs_hooks import WriteHookContext
from nexus.lib.lease import ManualClock

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_context(
    agent_id: str | None = "agent-A",
    user_id: str | None = "user-1",
    zone_id: str | None = "zone-a",
    **kwargs: object,
) -> MagicMock:
    """Create a mock OperationContext with agent_id/user_id/zone_id."""
    ctx = MagicMock()
    ctx.agent_id = agent_id
    ctx.user_id = user_id
    ctx.zone_id = zone_id
    for k, v in kwargs.items():
        setattr(ctx, k, v)
    return ctx


def _make_write_ctx(
    path: str = "/workspace/file.txt",
    context: MagicMock | None = None,
    old_metadata: MagicMock | None = None,
    **kwargs: object,
) -> WriteHookContext:
    """Create a WriteHookContext for hook testing."""
    if context is None:
        context = _make_context()
    return WriteHookContext(
        path=path,
        content=b"data",
        context=context,
        old_metadata=old_metadata,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(0.0)


@pytest.fixture
def lease_table(clock: ManualClock) -> PermissionLeaseTable:
    return PermissionLeaseTable(clock=clock, ttl=30.0)


@pytest.fixture
def checker() -> MagicMock:
    """Mock permission checker — check() raises PermissionError on denial."""
    return MagicMock()


@pytest.fixture
def metadata_store() -> MagicMock:
    return MagicMock()


@pytest.fixture
def hook(
    checker: MagicMock,
    metadata_store: MagicMock,
    lease_table: PermissionLeaseTable,
) -> PermissionCheckHook:
    """PermissionCheckHook wired with a PermissionLeaseTable."""
    return PermissionCheckHook(
        checker=checker,
        metadata_store=metadata_store,
        default_context=_make_context(),
        enforce_permissions=True,
        lease_table=lease_table,
    )


# ---------------------------------------------------------------------------
# Hook fast path / slow path
# ---------------------------------------------------------------------------


class TestPermissionLeaseHookIntegration:
    """Tests for the lease-aware on_pre_write fast path."""

    def test_first_write_does_full_check_and_stamps_lease(
        self, hook: PermissionCheckHook, checker: MagicMock, lease_table: PermissionLeaseTable
    ) -> None:
        """First write to a path: full ReBAC check + lease stamp."""
        ctx = _make_write_ctx(old_metadata=MagicMock())
        hook.on_pre_write(ctx)

        checker.check.assert_called_once()
        assert lease_table.stats()["lease_stamps"] == 1
        assert lease_table.stats()["lease_misses"] == 1

    def test_second_write_uses_lease_fast_path(
        self, hook: PermissionCheckHook, checker: MagicMock, lease_table: PermissionLeaseTable
    ) -> None:
        """Second write to same (path, agent): lease hit, skip ReBAC check."""
        ctx = _make_write_ctx(old_metadata=MagicMock())
        hook.on_pre_write(ctx)  # first: full check + stamp
        checker.check.reset_mock()

        hook.on_pre_write(ctx)  # second: lease hit

        checker.check.assert_not_called()
        assert lease_table.stats()["lease_hits"] == 1

    def test_new_file_leases_on_parent_directory(
        self, hook: PermissionCheckHook, checker: MagicMock, lease_table: PermissionLeaseTable
    ) -> None:
        """New file: check WRITE on parent, stamp lease on parent path."""
        ctx1 = _make_write_ctx(path="/workspace/src/file1.py", old_metadata=None)
        hook.on_pre_write(ctx1)  # checks /workspace/src, stamps lease

        checker.check.reset_mock()

        ctx2 = _make_write_ctx(path="/workspace/src/file2.py", old_metadata=None)
        hook.on_pre_write(ctx2)  # same parent → lease hit

        checker.check.assert_not_called()
        assert lease_table.stats()["lease_hits"] == 1

    def test_different_agents_have_independent_leases(
        self, hook: PermissionCheckHook, checker: MagicMock
    ) -> None:
        """Different agents' leases don't interfere."""
        ctx_a = _make_write_ctx(context=_make_context(agent_id="agent-A"), old_metadata=MagicMock())
        ctx_b = _make_write_ctx(context=_make_context(agent_id="agent-B"), old_metadata=MagicMock())

        hook.on_pre_write(ctx_a)  # stamp for agent-A
        checker.check.reset_mock()

        hook.on_pre_write(ctx_b)  # agent-B: no lease → full check
        checker.check.assert_called_once()

    def test_permission_denied_does_not_stamp_lease(
        self, hook: PermissionCheckHook, checker: MagicMock, lease_table: PermissionLeaseTable
    ) -> None:
        """If ReBAC check raises PermissionError, no lease is stamped."""
        checker.check.side_effect = PermissionError("Access denied")
        ctx = _make_write_ctx(old_metadata=MagicMock())

        with pytest.raises(PermissionError):
            hook.on_pre_write(ctx)

        assert lease_table.stats()["lease_stamps"] == 0

    def test_enforce_permissions_false_skips_everything(
        self, checker: MagicMock, metadata_store: MagicMock, lease_table: PermissionLeaseTable
    ) -> None:
        """With enforce_permissions=False, no lease check or ReBAC check."""
        hook = PermissionCheckHook(
            checker=checker,
            metadata_store=metadata_store,
            default_context=_make_context(),
            enforce_permissions=False,
            lease_table=lease_table,
        )
        ctx = _make_write_ctx(old_metadata=MagicMock())
        hook.on_pre_write(ctx)

        checker.check.assert_not_called()
        assert lease_table.stats()["lease_stamps"] == 0
        assert lease_table.stats()["lease_hits"] == 0


# ---------------------------------------------------------------------------
# agent_id edge cases (Decision #7A)
# ---------------------------------------------------------------------------


class TestPermissionLeaseAgentIdEdgeCases:
    """Edge cases for agent_id extraction from context."""

    def test_none_agent_id_skips_lease(
        self, hook: PermissionCheckHook, checker: MagicMock, lease_table: PermissionLeaseTable
    ) -> None:
        """agent_id=None: no lease stamped, always does full check."""
        ctx = _make_write_ctx(context=_make_context(agent_id=None), old_metadata=MagicMock())
        hook.on_pre_write(ctx)
        hook.on_pre_write(ctx)  # second write

        assert checker.check.call_count == 2  # both did full check
        assert lease_table.stats()["lease_stamps"] == 0

    def test_none_context_falls_back_to_default(
        self,
        checker: MagicMock,
        metadata_store: MagicMock,
        lease_table: PermissionLeaseTable,
    ) -> None:
        """context=None: uses default_context for both check and lease."""
        default_ctx = _make_context(agent_id="default-agent")
        hook = PermissionCheckHook(
            checker=checker,
            metadata_store=metadata_store,
            default_context=default_ctx,
            enforce_permissions=True,
            lease_table=lease_table,
        )
        ctx = _make_write_ctx(context=None, old_metadata=MagicMock())
        hook.on_pre_write(ctx)

        assert lease_table.stats()["lease_stamps"] == 1
        checker.check.assert_called_once()

    def test_context_without_agent_id_attr_skips_lease(
        self, checker: MagicMock, metadata_store: MagicMock, lease_table: PermissionLeaseTable
    ) -> None:
        """If context object doesn't have agent_id attribute, skip lease."""
        bare_context = MagicMock(spec=[])  # no attributes
        hook = PermissionCheckHook(
            checker=checker,
            metadata_store=metadata_store,
            default_context=bare_context,
            enforce_permissions=True,
            lease_table=lease_table,
        )
        ctx = _make_write_ctx(context=bare_context, old_metadata=MagicMock())
        hook.on_pre_write(ctx)
        hook.on_pre_write(ctx)

        assert checker.check.call_count == 2
        assert lease_table.stats()["lease_stamps"] == 0


# ---------------------------------------------------------------------------
# Inheritance-aware: new-file stamp covers existing-file writes
# ---------------------------------------------------------------------------


class TestPermissionLeaseInheritanceHook:
    """Ancestor walk through the hook: parent stamp covers child writes."""

    def test_new_file_stamp_covers_subsequent_existing_file_writes(
        self, hook: PermissionCheckHook, checker: MagicMock, lease_table: PermissionLeaseTable
    ) -> None:
        """New-file write stamps parent dir; existing-file write in same dir hits via ancestor."""
        # 1. New file → checks parent /workspace, stamps /workspace
        new_ctx = _make_write_ctx(path="/workspace/file1.py", old_metadata=None)
        hook.on_pre_write(new_ctx)
        assert checker.check.call_count == 1
        checker.check.reset_mock()

        # 2. Existing file in same dir → ancestor walk finds /workspace lease
        existing_ctx = _make_write_ctx(path="/workspace/file2.py", old_metadata=MagicMock())
        hook.on_pre_write(existing_ctx)
        checker.check.assert_not_called()  # lease hit via ancestor walk

    def test_new_file_stamp_does_not_cover_different_directory(
        self, hook: PermissionCheckHook, checker: MagicMock, lease_table: PermissionLeaseTable
    ) -> None:
        """Parent stamp on /workspace does NOT cover /other."""
        new_ctx = _make_write_ctx(path="/workspace/file1.py", old_metadata=None)
        hook.on_pre_write(new_ctx)
        checker.check.reset_mock()

        other_ctx = _make_write_ctx(path="/other/file2.py", old_metadata=MagicMock())
        hook.on_pre_write(other_ctx)
        checker.check.assert_called_once()  # no ancestor match → full check


# ---------------------------------------------------------------------------
# Agent invalidation
# ---------------------------------------------------------------------------


class TestPermissionLeaseAgentInvalidation:
    """invalidate_agent() clears leases for a terminated/changed agent."""

    def test_agent_invalidation_forces_recheck(
        self, hook: PermissionCheckHook, checker: MagicMock, lease_table: PermissionLeaseTable
    ) -> None:
        """After invalidating an agent, their writes require full checks."""
        ctx = _make_write_ctx(old_metadata=MagicMock())
        hook.on_pre_write(ctx)  # stamp
        checker.check.reset_mock()

        lease_table.invalidate_agent("agent-A")

        hook.on_pre_write(ctx)  # must do full check again
        checker.check.assert_called_once()

    def test_agent_invalidation_does_not_affect_other_agents(
        self, hook: PermissionCheckHook, checker: MagicMock, lease_table: PermissionLeaseTable
    ) -> None:
        """Invalidating agent-A leaves agent-B's leases intact."""
        ctx_a = _make_write_ctx(context=_make_context(agent_id="agent-A"), old_metadata=MagicMock())
        ctx_b = _make_write_ctx(context=_make_context(agent_id="agent-B"), old_metadata=MagicMock())
        hook.on_pre_write(ctx_a)
        hook.on_pre_write(ctx_b)
        checker.check.reset_mock()

        lease_table.invalidate_agent("agent-A")

        hook.on_pre_write(ctx_b)
        checker.check.assert_not_called()  # agent-B lease still valid


# ---------------------------------------------------------------------------
# No lease table (backwards compatibility)
# ---------------------------------------------------------------------------


class TestPermissionLeaseBackwardsCompat:
    """Ensure the hook works correctly without a lease_table (default=None)."""

    def test_hook_without_lease_table(self, checker: MagicMock, metadata_store: MagicMock) -> None:
        """Hook with lease_table=None works exactly like before #3394."""
        hook = PermissionCheckHook(
            checker=checker,
            metadata_store=metadata_store,
            default_context=_make_context(),
            enforce_permissions=True,
            lease_table=None,
        )
        ctx = _make_write_ctx(old_metadata=MagicMock())
        hook.on_pre_write(ctx)
        hook.on_pre_write(ctx)

        assert checker.check.call_count == 2  # no lease fast path


# ---------------------------------------------------------------------------
# CacheCoordinator lease invalidation (Decision #2C)
# ---------------------------------------------------------------------------


class TestCoordinatorLeaseInvalidation:
    """CacheCoordinator lease invalidation callback integration."""

    def test_register_and_invoke_lease_invalidator(self) -> None:
        """Lease invalidation callback is called during invalidate_for_write."""
        invocations: list[str] = []
        coordinator = CacheCoordinator(
            zone_graph_cache={"zone-a": {"tuples": []}},
        )
        coordinator.register_lease_invalidator(
            "perm-lease", lambda zone_id: invocations.append(zone_id)
        )

        coordinator.invalidate_for_write(
            zone_id="zone-a",
            subject=("user", "alice"),
            relation="editor",
            object=("file", "/doc.txt"),
        )

        assert invocations == ["zone-a"]

    def test_lease_invalidator_called_after_l1_before_boundary(self) -> None:
        """Lease invalidation is step 3: after L1, before boundary."""
        call_order: list[str] = []
        l1 = MagicMock()
        l1.invalidate_subject.side_effect = lambda *a, **kw: call_order.append("l1")
        coordinator = CacheCoordinator(
            l1_cache=l1,
            zone_graph_cache={"zone-a": {"tuples": []}},
        )
        coordinator.register_lease_invalidator("lease", lambda zone_id: call_order.append("lease"))
        coordinator.register_boundary_invalidator(
            "boundary", lambda *a: call_order.append("boundary")
        )

        coordinator.invalidate_for_write(
            zone_id="zone-a",
            subject=("user", "alice"),
            relation="direct_viewer",
            object=("file", "/doc.txt"),
        )

        assert "l1" in call_order
        assert "lease" in call_order
        assert "boundary" in call_order
        assert call_order.index("l1") < call_order.index("lease")
        assert call_order.index("lease") < call_order.index("boundary")

    def test_lease_invalidator_failure_does_not_block_others(self) -> None:
        """A failing lease invalidator must not block boundary/visibility."""
        boundary_called = False

        def failing_lease(zone_id: str) -> None:
            raise RuntimeError("lease-boom")

        def boundary_cb(zone_id, subj_type, subj_id, perm, obj_path):
            nonlocal boundary_called
            boundary_called = True

        coordinator = CacheCoordinator(
            zone_graph_cache={"zone-a": {"tuples": []}},
        )
        coordinator.register_lease_invalidator("fail", failing_lease)
        coordinator.register_boundary_invalidator("ok", boundary_cb)

        coordinator.invalidate_for_write(
            zone_id="zone-a",
            subject=("user", "alice"),
            relation="direct_viewer",
            object=("file", "/doc.txt"),
        )

        assert boundary_called is True

    def test_duplicate_lease_registration_is_idempotent(self) -> None:
        """Re-registering the same callback_id must not create duplicates."""
        coordinator = CacheCoordinator()
        cb = lambda zone_id: None  # noqa: E731
        coordinator.register_lease_invalidator("lease-1", cb)
        coordinator.register_lease_invalidator("lease-1", cb)

        assert coordinator.get_stats()["registered_lease_invalidators"] == 1

    def test_unregister_lease_invalidator(self) -> None:
        """Unregistering a lease invalidator removes it."""
        coordinator = CacheCoordinator()
        coordinator.register_lease_invalidator("lease-1", lambda z: None)
        assert coordinator.unregister_lease_invalidator("lease-1") is True
        assert coordinator.get_stats()["registered_lease_invalidators"] == 0
        assert coordinator.unregister_lease_invalidator("lease-1") is False

    def test_lease_invalidation_stats(self) -> None:
        """lease_invalidations counter increments on each invalidate_for_write."""
        coordinator = CacheCoordinator(
            zone_graph_cache={"zone-a": {"tuples": []}},
        )
        coordinator.register_lease_invalidator("lease", lambda z: None)

        coordinator.invalidate_for_write(
            zone_id="zone-a",
            subject=("user", "alice"),
            relation="editor",
            object=("file", "/doc.txt"),
        )
        coordinator.invalidate_for_write(
            zone_id="zone-a",
            subject=("user", "bob"),
            relation="viewer",
            object=("file", "/other.txt"),
        )

        assert coordinator.get_stats()["lease_invalidations"] == 2

    def test_invalidate_all_also_clears_leases(self) -> None:
        """invalidate_all() invokes lease invalidation too."""
        invocations: list[str] = []
        coordinator = CacheCoordinator(
            zone_graph_cache={"zone-a": {"tuples": []}},
        )
        coordinator.register_lease_invalidator("lease", lambda zone_id: invocations.append(zone_id))

        coordinator.invalidate_all(zone_id="zone-a")

        assert "zone-a" in invocations


# ---------------------------------------------------------------------------
# Security regression test (Decision #9C)
# ---------------------------------------------------------------------------


class TestSecurityRegressionPermissionRevocation:
    """CRITICAL: permission revocation must invalidate leases.

    This is the most important test in this file. It verifies the full
    cycle: grant → write → lease → revoke → invalidate → write denied.

    A failure here means an agent could write after their permission
    was revoked — a security vulnerability.
    """

    def test_full_cycle_grant_write_revoke_deny(self) -> None:
        """Grant → write (stamp lease) → revoke → write (lease gone → ReBAC check → deny)."""
        clock = ManualClock(0.0)
        lease_table = PermissionLeaseTable(clock=clock, ttl=30.0)
        checker = MagicMock()  # check() returns normally = grant
        hook = PermissionCheckHook(
            checker=checker,
            metadata_store=MagicMock(),
            default_context=_make_context(),
            enforce_permissions=True,
            lease_table=lease_table,
        )
        coordinator = CacheCoordinator(
            zone_graph_cache={"zone-a": {"tuples": []}},
        )

        # Wire lease invalidation callback
        coordinator.register_lease_invalidator(
            "perm-lease", lambda zone_id: lease_table.invalidate_all()
        )

        # Step 1: Agent writes successfully (full check + stamp)
        ctx = _make_write_ctx(old_metadata=MagicMock())
        hook.on_pre_write(ctx)
        assert lease_table.stats()["lease_stamps"] == 1
        checker.check.reset_mock()

        # Step 2: Agent writes again (lease hit, skip check)
        hook.on_pre_write(ctx)
        checker.check.assert_not_called()

        # Step 3: Admin revokes permission → CacheCoordinator fires
        coordinator.invalidate_for_write(
            zone_id="zone-a",
            subject=("user", "user-1"),
            relation="editor",
            object=("file", "/workspace/file.txt"),
        )

        # Step 4: Lease table is now empty
        assert lease_table.active_count == 0

        # Step 5: Agent tries to write → no lease → full ReBAC check
        checker.check.side_effect = PermissionError("Access denied: no longer editor")

        with pytest.raises(PermissionError, match="no longer editor"):
            hook.on_pre_write(ctx)

        # Verify the full check was invoked (lease was properly invalidated)
        checker.check.assert_called_once()

    def test_lease_expiry_also_forces_recheck(self) -> None:
        """Even without explicit revocation, TTL expiry forces a recheck."""
        clock = ManualClock(0.0)
        lease_table = PermissionLeaseTable(clock=clock, ttl=30.0)
        checker = MagicMock()
        hook = PermissionCheckHook(
            checker=checker,
            metadata_store=MagicMock(),
            default_context=_make_context(),
            enforce_permissions=True,
            lease_table=lease_table,
        )

        # Write 1: full check + stamp
        ctx = _make_write_ctx(old_metadata=MagicMock())
        hook.on_pre_write(ctx)
        checker.check.reset_mock()

        # Write 2: lease hit
        hook.on_pre_write(ctx)
        checker.check.assert_not_called()

        # Advance past TTL
        clock.advance(31.0)

        # Write 3: lease expired → full check again
        hook.on_pre_write(ctx)
        checker.check.assert_called_once()

    def test_multiple_agents_revocation_clears_all(self) -> None:
        """Zone-wide invalidation clears leases for ALL agents."""
        clock = ManualClock(0.0)
        lease_table = PermissionLeaseTable(clock=clock, ttl=30.0)
        checker = MagicMock()
        hook = PermissionCheckHook(
            checker=checker,
            metadata_store=MagicMock(),
            default_context=_make_context(),
            enforce_permissions=True,
            lease_table=lease_table,
        )
        coordinator = CacheCoordinator(
            zone_graph_cache={"zone-a": {"tuples": []}},
        )
        coordinator.register_lease_invalidator(
            "perm-lease", lambda zone_id: lease_table.invalidate_all()
        )

        # Both agents write and get leases
        for agent in ("agent-A", "agent-B"):
            ctx = _make_write_ctx(
                context=_make_context(agent_id=agent),
                old_metadata=MagicMock(),
            )
            hook.on_pre_write(ctx)

        assert lease_table.active_count == 2

        # Permission change invalidates ALL leases
        coordinator.invalidate_for_write(
            zone_id="zone-a",
            subject=("user", "user-1"),
            relation="editor",
            object=("file", "/workspace/file.txt"),
        )

        assert lease_table.active_count == 0
