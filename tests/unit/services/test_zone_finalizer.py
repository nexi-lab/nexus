"""Tests for Zone Finalizer Protocol — ordered cleanup on deprovision (Issue #2061).

TDD: Tests written FIRST, implementation follows.
"""

import asyncio
from unittest.mock import AsyncMock

import pytest

from nexus.contracts.protocols.brick_lifecycle import (
    BrickState,
    ZoneDeprovisionReport,
    ZoneState,
)
from nexus.system_services.lifecycle.brick_lifecycle import BrickLifecycleManager
from nexus.system_services.lifecycle.brick_reconciler import BrickReconciler
from tests.unit.services.conftest import (
    make_drain_only_brick as _make_drain_only_brick,
)
from tests.unit.services.conftest import (
    make_finalize_only_brick as _make_finalize_only_brick,
)
from tests.unit.services.conftest import (
    make_lifecycle_brick as _make_lifecycle_brick,
)
from tests.unit.services.conftest import (
    make_zone_aware_brick as _make_zone_aware_brick,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def manager() -> BrickLifecycleManager:
    return BrickLifecycleManager()


@pytest.fixture
def reconciler(manager: BrickLifecycleManager) -> BrickReconciler:
    return BrickReconciler(
        lifecycle_manager=manager,
        reconcile_interval=30.0,
        health_check_timeout=2.0,
        max_retries=3,
    )


def _register_and_mount_zone_aware(
    manager: BrickLifecycleManager,
    name: str,
    brick: AsyncMock,
    *,
    depends_on: tuple[str, ...] = (),
) -> None:
    """Register a brick and force it to ACTIVE for zone testing."""
    manager.register(name, brick, protocol_name=f"{name}Proto", depends_on=depends_on)
    manager._force_state(name, BrickState.ACTIVE)
    entry = manager._bricks[name]
    entry.started_at = 1.0


# ---------------------------------------------------------------------------
# TestZoneDeprovision — happy path and state transitions
# ---------------------------------------------------------------------------


class TestZoneDeprovision:
    """Test deprovision_zone() orchestration."""

    @pytest.mark.asyncio
    async def test_deprovision_happy_path(self, manager: BrickLifecycleManager) -> None:
        """drain+finalize called on all zone-aware bricks."""
        brick_a = _make_zone_aware_brick("a")
        brick_b = _make_zone_aware_brick("b")
        _register_and_mount_zone_aware(manager, "a", brick_a)
        _register_and_mount_zone_aware(manager, "b", brick_b)

        report = await manager.deprovision_zone("zone-1")

        assert isinstance(report, ZoneDeprovisionReport)
        assert report.zone_id == "zone-1"
        assert report.zone_state == ZoneState.DESTROYED
        assert report.bricks_drained == 2
        assert report.bricks_finalized == 2
        assert report.drain_errors == 0
        assert report.finalize_errors == 0
        assert report.forced is False

        brick_a.drain.assert_awaited_once_with("zone-1")
        brick_a.finalize.assert_awaited_once_with("zone-1")
        brick_b.drain.assert_awaited_once_with("zone-1")
        brick_b.finalize.assert_awaited_once_with("zone-1")

    @pytest.mark.asyncio
    async def test_deprovision_marks_zone_terminating_then_destroyed(
        self, manager: BrickLifecycleManager
    ) -> None:
        """Zone transitions ACTIVE → TERMINATING → DESTROYED."""
        brick = _make_zone_aware_brick("a")
        observed_states: list[ZoneState] = []

        async def _track_drain(zone_id: str) -> None:
            observed_states.append(manager.get_zone_state(zone_id))

        brick.drain = AsyncMock(side_effect=_track_drain)
        _register_and_mount_zone_aware(manager, "a", brick)

        assert manager.get_zone_state("zone-1") == ZoneState.ACTIVE
        report = await manager.deprovision_zone("zone-1")
        assert observed_states == [ZoneState.TERMINATING]
        assert report.zone_state == ZoneState.DESTROYED
        assert manager.get_zone_state("zone-1") == ZoneState.DESTROYED

    @pytest.mark.asyncio
    async def test_deprovision_nonexistent_zone_is_noop(
        self, manager: BrickLifecycleManager
    ) -> None:
        """Deprovisioning a zone with no zone-aware bricks is a no-op."""
        # Register a normal brick (no drain/finalize)
        brick = _make_lifecycle_brick("plain")
        manager.register("plain", brick, protocol_name="PlainProto")
        manager._force_state("plain", BrickState.ACTIVE)

        report = await manager.deprovision_zone("zone-empty")
        assert report.zone_id == "zone-empty"
        assert report.bricks_drained == 0
        assert report.bricks_finalized == 0
        assert report.zone_state == ZoneState.DESTROYED


# ---------------------------------------------------------------------------
# TestDrainPhase
# ---------------------------------------------------------------------------


class TestDrainPhase:
    """Test the drain phase of zone deprovision."""

    @pytest.mark.asyncio
    async def test_drain_called_on_zone_aware_bricks_only(
        self, manager: BrickLifecycleManager
    ) -> None:
        """drain() only called on bricks with drain method."""
        zone_brick = _make_zone_aware_brick("za")
        plain_brick = _make_lifecycle_brick("plain")
        _register_and_mount_zone_aware(manager, "za", zone_brick)
        _register_and_mount_zone_aware(manager, "plain", plain_brick)

        report = await manager.deprovision_zone("zone-1")
        zone_brick.drain.assert_awaited_once_with("zone-1")
        assert report.bricks_drained == 1

    @pytest.mark.asyncio
    async def test_drain_skipped_for_stateless_bricks(self, manager: BrickLifecycleManager) -> None:
        """Stateless bricks (no drain/finalize) are skipped entirely."""
        from tests.unit.services.conftest import make_stateless_brick

        stateless = make_stateless_brick("pay")
        manager.register("pay", stateless, protocol_name="PayProto")
        manager._force_state("pay", BrickState.ACTIVE)

        report = await manager.deprovision_zone("zone-1")
        assert report.bricks_drained == 0
        assert report.bricks_finalized == 0

    @pytest.mark.asyncio
    async def test_drain_only_implementation(self, manager: BrickLifecycleManager) -> None:
        """Brick with drain() but no finalize() — only drain is called."""
        brick = _make_drain_only_brick("d")
        _register_and_mount_zone_aware(manager, "d", brick)

        report = await manager.deprovision_zone("zone-1")
        brick.drain.assert_awaited_once_with("zone-1")
        assert report.bricks_drained == 1
        assert report.bricks_finalized == 0

    @pytest.mark.asyncio
    async def test_drain_concurrent_with_semaphore(self, manager: BrickLifecycleManager) -> None:
        """Drain phase respects max_concurrent_drain semaphore."""
        max_concurrent_observed = 0
        current_concurrent = 0
        lock = asyncio.Lock()

        async def _slow_drain(zone_id: str) -> None:
            nonlocal max_concurrent_observed, current_concurrent
            async with lock:
                current_concurrent += 1
                if current_concurrent > max_concurrent_observed:
                    max_concurrent_observed = current_concurrent
            await asyncio.sleep(0.01)
            async with lock:
                current_concurrent -= 1

        # Create 10 zone-aware bricks
        for i in range(10):
            brick = _make_zone_aware_brick(f"b{i}")
            brick.drain = AsyncMock(side_effect=_slow_drain)
            _register_and_mount_zone_aware(manager, f"b{i}", brick)

        await manager.deprovision_zone("zone-1", max_concurrent_drain=3)
        # Concurrency should be bounded by semaphore
        assert max_concurrent_observed <= 3


# ---------------------------------------------------------------------------
# TestFinalizePhase
# ---------------------------------------------------------------------------


class TestFinalizePhase:
    """Test the finalize phase of zone deprovision."""

    @pytest.mark.asyncio
    async def test_finalize_called_in_reverse_dag_order(
        self, manager: BrickLifecycleManager
    ) -> None:
        """finalize() called in reverse-DAG order (dependents before dependencies)."""
        order: list[str] = []

        def _make_ordered_brick(name: str) -> AsyncMock:
            brick = _make_zone_aware_brick(name)

            async def _track_finalize(zone_id: str) -> None:
                order.append(name)

            brick.finalize = AsyncMock(side_effect=_track_finalize)
            return brick

        brick_a = _make_ordered_brick("a")
        brick_b = _make_ordered_brick("b")
        brick_c = _make_ordered_brick("c")

        manager.register("a", brick_a, protocol_name="AP")
        manager._force_state("a", BrickState.ACTIVE)
        manager.register("b", brick_b, protocol_name="BP", depends_on=("a",))
        manager._force_state("b", BrickState.ACTIVE)
        manager.register("c", brick_c, protocol_name="CP", depends_on=("b",))
        manager._force_state("c", BrickState.ACTIVE)

        await manager.deprovision_zone("zone-1")

        # c should finalize before b, b before a (reverse DAG)
        assert order.index("c") < order.index("b")
        assert order.index("b") < order.index("a")

    @pytest.mark.asyncio
    async def test_finalize_only_implementation(self, manager: BrickLifecycleManager) -> None:
        """Brick with finalize() but no drain() — only finalize is called."""
        brick = _make_finalize_only_brick("f")
        _register_and_mount_zone_aware(manager, "f", brick)

        report = await manager.deprovision_zone("zone-1")
        brick.finalize.assert_awaited_once_with("zone-1")
        assert report.bricks_drained == 0
        assert report.bricks_finalized == 1

    @pytest.mark.asyncio
    async def test_finalize_continues_on_single_brick_failure(
        self, manager: BrickLifecycleManager
    ) -> None:
        """One brick's finalize() failure doesn't block others."""
        brick_a = _make_zone_aware_brick("a")
        brick_a.finalize = AsyncMock(side_effect=RuntimeError("finalize failed"))
        brick_b = _make_zone_aware_brick("b")

        _register_and_mount_zone_aware(manager, "a", brick_a)
        _register_and_mount_zone_aware(manager, "b", brick_b)

        report = await manager.deprovision_zone("zone-1")
        # b's finalize should still have been called
        brick_b.finalize.assert_awaited_once_with("zone-1")
        assert report.finalize_errors == 1
        assert report.bricks_finalized == 1
        # Zone still ends up DESTROYED
        assert report.zone_state == ZoneState.DESTROYED

    @pytest.mark.asyncio
    async def test_finalize_skipped_for_non_zone_aware(
        self, manager: BrickLifecycleManager
    ) -> None:
        """finalize() not called on bricks without that method."""
        plain = _make_lifecycle_brick("plain")
        _register_and_mount_zone_aware(manager, "plain", plain)

        report = await manager.deprovision_zone("zone-1")
        assert report.bricks_finalized == 0


# ---------------------------------------------------------------------------
# TestPartialProtocol
# ---------------------------------------------------------------------------


class TestPartialProtocol:
    """Test bricks that implement only part of ZoneAwareBrickProtocol."""

    @pytest.mark.asyncio
    async def test_brick_with_drain_only(self, manager: BrickLifecycleManager) -> None:
        brick = _make_drain_only_brick("d")
        _register_and_mount_zone_aware(manager, "d", brick)

        report = await manager.deprovision_zone("zone-1")
        brick.drain.assert_awaited_once_with("zone-1")
        assert report.bricks_drained == 1
        assert report.bricks_finalized == 0

    @pytest.mark.asyncio
    async def test_brick_with_finalize_only(self, manager: BrickLifecycleManager) -> None:
        brick = _make_finalize_only_brick("f")
        _register_and_mount_zone_aware(manager, "f", brick)

        report = await manager.deprovision_zone("zone-1")
        brick.finalize.assert_awaited_once_with("zone-1")
        assert report.bricks_drained == 0
        assert report.bricks_finalized == 1

    @pytest.mark.asyncio
    async def test_brick_with_both(self, manager: BrickLifecycleManager) -> None:
        brick = _make_zone_aware_brick("both")
        _register_and_mount_zone_aware(manager, "both", brick)

        report = await manager.deprovision_zone("zone-1")
        brick.drain.assert_awaited_once_with("zone-1")
        brick.finalize.assert_awaited_once_with("zone-1")
        assert report.bricks_drained == 1
        assert report.bricks_finalized == 1

    @pytest.mark.asyncio
    async def test_brick_with_neither(self, manager: BrickLifecycleManager) -> None:
        brick = _make_lifecycle_brick("plain")
        _register_and_mount_zone_aware(manager, "plain", brick)

        report = await manager.deprovision_zone("zone-1")
        assert report.bricks_drained == 0
        assert report.bricks_finalized == 0


# ---------------------------------------------------------------------------
# TestGracePeriod
# ---------------------------------------------------------------------------


class TestGracePeriod:
    """Test grace period timeouts during deprovision."""

    @pytest.mark.asyncio
    async def test_drain_timeout_proceeds_to_finalize(self, manager: BrickLifecycleManager) -> None:
        """If drain exceeds grace period, finalize still runs."""
        brick = _make_zone_aware_brick("slow")

        async def _slow_drain(zone_id: str) -> None:
            await asyncio.sleep(10)  # Way over grace period

        brick.drain = AsyncMock(side_effect=_slow_drain)
        _register_and_mount_zone_aware(manager, "slow", brick)

        report = await manager.deprovision_zone("zone-1", grace_period=0.1)
        # Finalize should still have been called despite drain timeout
        brick.finalize.assert_awaited_once_with("zone-1")
        assert report.forced is True
        assert report.zone_state == ZoneState.DESTROYED

    @pytest.mark.asyncio
    async def test_finalize_timeout_forces_destroy(self, manager: BrickLifecycleManager) -> None:
        """If finalize exceeds grace period, zone is still destroyed."""
        brick = _make_zone_aware_brick("slow")

        async def _slow_finalize(zone_id: str) -> None:
            await asyncio.sleep(10)

        brick.finalize = AsyncMock(side_effect=_slow_finalize)
        _register_and_mount_zone_aware(manager, "slow", brick)

        report = await manager.deprovision_zone("zone-1", grace_period=0.1)
        assert report.forced is True
        assert report.zone_state == ZoneState.DESTROYED

    @pytest.mark.asyncio
    async def test_both_timeout_still_marks_destroyed(self, manager: BrickLifecycleManager) -> None:
        """Both drain and finalize timeout — zone still reaches DESTROYED."""
        brick = _make_zone_aware_brick("stuck")

        async def _hang(zone_id: str) -> None:
            await asyncio.sleep(10)

        brick.drain = AsyncMock(side_effect=_hang)
        brick.finalize = AsyncMock(side_effect=_hang)
        _register_and_mount_zone_aware(manager, "stuck", brick)

        report = await manager.deprovision_zone("zone-1", grace_period=0.1)
        assert report.forced is True
        assert report.zone_state == ZoneState.DESTROYED

    @pytest.mark.asyncio
    async def test_within_grace_period_completes_normally(
        self, manager: BrickLifecycleManager
    ) -> None:
        """Fast operations complete without forced flag."""
        brick = _make_zone_aware_brick("fast")
        _register_and_mount_zone_aware(manager, "fast", brick)

        report = await manager.deprovision_zone("zone-1", grace_period=30.0)
        assert report.forced is False
        assert report.zone_state == ZoneState.DESTROYED


# ---------------------------------------------------------------------------
# TestReconcilerZoneAwareness
# ---------------------------------------------------------------------------


class TestReconcilerZoneAwareness:
    """Test reconciler filters for terminating zones."""

    @pytest.mark.asyncio
    async def test_reconciler_skips_self_healing_for_terminating_zone(
        self,
        manager: BrickLifecycleManager,
        reconciler: BrickReconciler,
    ) -> None:
        """Bricks in terminating zones should not be auto-healed."""
        brick = _make_zone_aware_brick("search")
        brick.start = AsyncMock(side_effect=RuntimeError("fail"))
        manager.register("search", brick, protocol_name="SP")
        await manager.mount("search")
        _s = manager.get_status("search")
        assert _s is not None
        assert _s.state == BrickState.FAILED

        # Mark zone as terminating
        reconciler.mark_zone_terminating("zone-1", brick_names={"search"})

        # Fix brick so it would normally succeed
        brick.start = AsyncMock(return_value=None)
        result = await reconciler.reconcile()

        # Should skip self-healing for search (in terminating zone)
        search_drifts = [d for d in result.drifts if d.brick_name == "search"]
        assert all(d.action.value == "skip" for d in search_drifts) or len(search_drifts) == 0

    @pytest.mark.asyncio
    async def test_reconciler_resumes_after_zone_destroyed(
        self,
        manager: BrickLifecycleManager,
        reconciler: BrickReconciler,
    ) -> None:
        """After zone is destroyed, normal reconciliation resumes."""
        brick = _make_lifecycle_brick("search")
        manager.register("search", brick, protocol_name="SP")
        # Brick stays REGISTERED — should be mounted

        reconciler.mark_zone_terminating("zone-1", brick_names={"search"})
        reconciler.mark_zone_destroyed("zone-1")

        result = await reconciler.reconcile()
        # After zone is destroyed, search should be picked up again
        search_drifts = [d for d in result.drifts if d.brick_name == "search"]
        assert len(search_drifts) >= 1

    @pytest.mark.asyncio
    async def test_health_checks_skipped_for_terminating_zone(
        self,
        manager: BrickLifecycleManager,
        reconciler: BrickReconciler,
    ) -> None:
        """Health checks skipped for bricks in terminating zones."""
        brick = _make_zone_aware_brick("search")
        brick.health_check = AsyncMock(return_value=False)  # Would fail
        manager.register("search", brick, protocol_name="SP")
        await manager.mount("search")
        _s = manager.get_status("search")
        assert _s is not None
        assert _s.state == BrickState.ACTIVE

        reconciler.mark_zone_terminating("zone-1", brick_names={"search"})
        await reconciler.reconcile()

        # Should NOT have transitioned to FAILED (health check skipped)
        _s = manager.get_status("search")
        assert _s is not None
        assert _s.state == BrickState.ACTIVE


# ---------------------------------------------------------------------------
# TestDAGOperationHelper
# ---------------------------------------------------------------------------


class TestDAGOperationHelper:
    """Test _run_dag_operation() helper (extracted from mount_all/unmount_all)."""

    @pytest.mark.asyncio
    async def test_mount_all_uses_helper_regression(self, manager: BrickLifecycleManager) -> None:
        """mount_all still works after refactor to use _run_dag_operation()."""
        brick_a = _make_lifecycle_brick("a")
        brick_b = _make_lifecycle_brick("b")

        manager.register("a", brick_a, protocol_name="AP")
        manager.register("b", brick_b, protocol_name="BP", depends_on=("a",))

        report = await manager.mount_all()
        assert report.active == 2
        _s = manager.get_status("a")
        assert _s is not None
        assert _s.state == BrickState.ACTIVE
        _s = manager.get_status("b")
        assert _s is not None
        assert _s.state == BrickState.ACTIVE

    @pytest.mark.asyncio
    async def test_unmount_all_uses_helper_regression(self, manager: BrickLifecycleManager) -> None:
        """unmount_all still works after refactor."""
        brick_a = _make_lifecycle_brick("a")
        brick_b = _make_lifecycle_brick("b")

        manager.register("a", brick_a, protocol_name="AP")
        manager.register("b", brick_b, protocol_name="BP", depends_on=("a",))

        await manager.mount_all()
        await manager.unmount_all()
        _s = manager.get_status("a")
        assert _s is not None
        assert _s.state == BrickState.UNMOUNTED
        _s = manager.get_status("b")
        assert _s is not None
        assert _s.state == BrickState.UNMOUNTED

    @pytest.mark.asyncio
    async def test_helper_respects_filter_fn(self, manager: BrickLifecycleManager) -> None:
        """_run_dag_operation() respects filter_fn to skip bricks."""
        brick_a = _make_lifecycle_brick("a")
        brick_b = _make_lifecycle_brick("b")

        manager.register("a", brick_a, protocol_name="AP")
        manager.register("b", brick_b, protocol_name="BP")

        # Only mount "a" (not "b") by using mount directly
        await manager.mount("a")
        _s = manager.get_status("a")
        assert _s is not None
        assert _s.state == BrickState.ACTIVE
        _s = manager.get_status("b")
        assert _s is not None
        assert _s.state == BrickState.REGISTERED

        # unmount_all should only unmount ACTIVE bricks
        await manager.unmount_all()
        _s = manager.get_status("a")
        assert _s is not None
        assert _s.state == BrickState.UNMOUNTED
        _s = manager.get_status("b")
        assert _s is not None
        assert _s.state == BrickState.REGISTERED

    @pytest.mark.asyncio
    async def test_helper_respects_max_concurrent(self, manager: BrickLifecycleManager) -> None:
        """Drain phase respects max_concurrent semaphore via deprovision_zone."""
        max_concurrent_observed = 0
        current_concurrent = 0
        lock = asyncio.Lock()

        async def _tracked_drain(zone_id: str) -> None:
            nonlocal max_concurrent_observed, current_concurrent
            async with lock:
                current_concurrent += 1
                if current_concurrent > max_concurrent_observed:
                    max_concurrent_observed = current_concurrent
            await asyncio.sleep(0.01)
            async with lock:
                current_concurrent -= 1

        # Create 6 bricks at the same DAG level (no deps)
        for i in range(6):
            brick = _make_zone_aware_brick(f"b{i}")
            brick.drain = AsyncMock(side_effect=_tracked_drain)
            _register_and_mount_zone_aware(manager, f"b{i}", brick)

        await manager.deprovision_zone("zone-1", max_concurrent_drain=2)
        assert max_concurrent_observed <= 2
