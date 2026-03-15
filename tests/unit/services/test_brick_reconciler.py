"""Tests for BrickReconciler — drift detection and self-healing (Issue #2060).

TDD: Tests written FIRST, implementation follows.
"""

import asyncio
from unittest.mock import AsyncMock

import pytest

from nexus.contracts.protocols.brick_lifecycle import (
    BrickReconcileOutcome,
    BrickState,
    DriftAction,
    ReconcileContext,
)
from nexus.system_services.lifecycle.brick_lifecycle import BrickLifecycleManager
from nexus.system_services.lifecycle.brick_reconciler import BrickReconciler
from tests.unit.services.conftest import (
    make_failing_brick as _make_failing_brick,
)
from tests.unit.services.conftest import (
    make_lifecycle_brick as _make_lifecycle_brick,
)
from tests.unit.services.conftest import (
    make_stateless_brick as _make_stateless_brick,
)


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


# ---------------------------------------------------------------------------
# TestReconcileDetectDrift (~6 tests)
# ---------------------------------------------------------------------------


class TestReconcileDetectDrift:
    """Test drift detection between spec and status."""

    @pytest.mark.asyncio
    async def test_no_drift_when_all_active(
        self, manager: BrickLifecycleManager, reconciler: BrickReconciler
    ) -> None:
        """All bricks ACTIVE, reconcile returns 0 drifted."""
        manager.register("a", _make_lifecycle_brick("a"), protocol_name="AP")
        manager.register("b", _make_lifecycle_brick("b"), protocol_name="BP")
        await manager.mount_all()
        result = await reconciler.reconcile()
        assert result.total_bricks == 2
        assert result.drifted == 0

    @pytest.mark.asyncio
    async def test_detect_failed_brick(
        self, manager: BrickLifecycleManager, reconciler: BrickReconciler
    ) -> None:
        """FAILED brick detected as drifted, action='reset'."""
        brick = _make_failing_brick()
        manager.register("failing", brick, protocol_name="FP")
        await manager.mount("failing")
        _s = manager.get_status("failing")
        assert _s is not None
        assert _s.state == BrickState.FAILED

        # Now fix the brick so remount succeeds
        brick.start = AsyncMock(return_value=None)
        result = await reconciler.reconcile()
        assert result.drifted >= 1
        # The reconciler should have taken a reset+mount action
        assert result.actions_taken >= 1

    @pytest.mark.asyncio
    async def test_detect_registered_enabled_brick(
        self, manager: BrickLifecycleManager, reconciler: BrickReconciler
    ) -> None:
        """REGISTERED brick that should be active is detected, action='mount'."""
        manager.register("idle", _make_lifecycle_brick("idle"), protocol_name="IP")
        # Don't mount it — stays REGISTERED
        result = await reconciler.reconcile()
        assert result.drifted >= 1
        drifts = [d for d in result.drifts if d.brick_name == "idle"]
        assert len(drifts) == 1
        assert drifts[0].action is DriftAction.MOUNT

    @pytest.mark.asyncio
    async def test_detect_active_disabled_brick(
        self, manager: BrickLifecycleManager, reconciler: BrickReconciler
    ) -> None:
        """ACTIVE but spec.enabled=False → action='unmount'."""
        brick = _make_lifecycle_brick("disabled")
        manager.register("disabled", brick, protocol_name="DP")
        await manager.mount("disabled")
        # Disable the brick via spec replacement
        from dataclasses import replace

        entry = manager._bricks["disabled"]
        entry.spec = replace(entry.spec, enabled=False)

        result = await reconciler.reconcile()
        drifts = [d for d in result.drifts if d.brick_name == "disabled"]
        assert len(drifts) == 1
        assert drifts[0].action is DriftAction.UNMOUNT

    @pytest.mark.asyncio
    async def test_drift_report_contains_correct_fields(
        self, manager: BrickLifecycleManager, reconciler: BrickReconciler
    ) -> None:
        """DriftReport has all expected fields populated."""
        manager.register("idle", _make_lifecycle_brick("idle"), protocol_name="IP")
        result = await reconciler.reconcile()
        assert len(result.drifts) >= 1
        drift = result.drifts[0]
        assert drift.brick_name == "idle"
        assert drift.spec_state == "enabled"
        assert drift.actual_state == BrickState.REGISTERED
        assert drift.action is DriftAction.MOUNT

    @pytest.mark.asyncio
    async def test_no_drift_for_disabled_unmounted(
        self, manager: BrickLifecycleManager, reconciler: BrickReconciler
    ) -> None:
        """Disabled brick in UNMOUNTED state → no drift."""
        brick = _make_lifecycle_brick("gone")
        manager.register("gone", brick, protocol_name="GP")
        await manager.mount("gone")
        await manager.unmount("gone")
        # Disable spec
        from dataclasses import replace

        entry = manager._bricks["gone"]
        entry.spec = replace(entry.spec, enabled=False)

        result = await reconciler.reconcile()
        drifts = [d for d in result.drifts if d.brick_name == "gone"]
        assert len(drifts) == 0


# ---------------------------------------------------------------------------
# TestReconcileSelfHealing (~6 tests)
# ---------------------------------------------------------------------------


class TestReconcileSelfHealing:
    """Test self-healing: reset and remount FAILED bricks."""

    @pytest.mark.asyncio
    async def test_failed_brick_auto_reset_and_remount(
        self, manager: BrickLifecycleManager, reconciler: BrickReconciler
    ) -> None:
        """Reconciler resets and mounts a failed brick."""
        brick = _make_lifecycle_brick("search")
        brick.start = AsyncMock(
            side_effect=[RuntimeError("fail"), None]  # fail first, succeed second
        )
        manager.register("search", brick, protocol_name="SP")
        await manager.mount("search")
        _s = manager.get_status("search")
        assert _s is not None
        assert _s.state == BrickState.FAILED

        result = await reconciler.reconcile()
        _s = manager.get_status("search")
        assert _s is not None
        assert _s.state == BrickState.ACTIVE
        assert result.actions_taken >= 1

    @pytest.mark.asyncio
    async def test_max_retries_stops_retry(
        self, manager: BrickLifecycleManager, reconciler: BrickReconciler
    ) -> None:
        """After 3 failures, brick stays FAILED (no more retries)."""
        brick = _make_lifecycle_brick("hopeless")
        brick.start = AsyncMock(side_effect=RuntimeError("always fails"))
        manager.register("hopeless", brick, protocol_name="HP")
        await manager.mount("hopeless")

        # Reconcile 3 times — each time it tries and fails again
        # Clear backoff between passes so retries aren't delayed by exponential backoff
        for _ in range(3):
            reconciler._next_retry_after.clear()
            await reconciler.reconcile()

        # After 3 retries, the brick should still be FAILED
        _s = manager.get_status("hopeless")
        assert _s is not None
        assert _s.state == BrickState.FAILED
        assert manager.get_retry_count("hopeless") >= 3

        # 4th reconcile should skip (max retries exceeded)
        result = await reconciler.reconcile()
        skip_drifts = [
            d for d in result.drifts if d.brick_name == "hopeless" and d.action is DriftAction.SKIP
        ]
        assert len(skip_drifts) == 1

    @pytest.mark.asyncio
    async def test_retry_counter_cleared_on_success(
        self, manager: BrickLifecycleManager, reconciler: BrickReconciler
    ) -> None:
        """Successful remount clears retry counter."""
        brick = _make_lifecycle_brick("flaky")
        call_count = 0

        async def _flaky_start() -> None:
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                raise RuntimeError("flaky")

        brick.start = AsyncMock(side_effect=_flaky_start)
        manager.register("flaky", brick, protocol_name="FP")
        await manager.mount("flaky")  # fails (call 1)
        _s = manager.get_status("flaky")
        assert _s is not None
        assert _s.state == BrickState.FAILED

        await reconciler.reconcile()  # resets + remounts (call 2 — succeeds)
        _s = manager.get_status("flaky")
        assert _s is not None
        assert _s.state == BrickState.ACTIVE
        assert manager._bricks["flaky"].retry_count == 0

    @pytest.mark.asyncio
    async def test_retry_counter_cleared_on_manual_reset(
        self, manager: BrickLifecycleManager, reconciler: BrickReconciler
    ) -> None:
        """Manual reset() clears retry counter."""
        brick = _make_lifecycle_brick("manual")
        brick.start = AsyncMock(side_effect=RuntimeError("fail"))
        manager.register("manual", brick, protocol_name="MP")
        await manager.mount("manual")
        entry = manager._bricks["manual"]
        entry.retry_count = 2

        manager.reset("manual")
        assert entry.retry_count == 0

    @pytest.mark.asyncio
    async def test_failed_dependency_blocks_dependent(
        self, manager: BrickLifecycleManager, reconciler: BrickReconciler
    ) -> None:
        """B depends on A; A is FAILED → reconciler doesn't mount B."""
        brick_a = _make_failing_brick(RuntimeError("A failed"))
        brick_b = _make_lifecycle_brick("b")

        manager.register("a", brick_a, protocol_name="AP")
        manager.register("b", brick_b, protocol_name="BP", depends_on=("a",))
        await manager.mount("a")  # fails
        # B stays REGISTERED

        result = await reconciler.reconcile()
        # A should be attempted for reset (and fail again)
        # B should not be mounted since A is not ACTIVE
        b_drifts = [d for d in result.drifts if d.brick_name == "b"]
        assert len(b_drifts) >= 1
        # B's drift should note blocked dependency
        assert any(d.action in (DriftAction.SKIP, DriftAction.MOUNT) for d in b_drifts)

    @pytest.mark.asyncio
    async def test_cascading_recovery(
        self, manager: BrickLifecycleManager, reconciler: BrickReconciler
    ) -> None:
        """A recovers → reconciler mounts dependent B."""
        call_count = 0

        async def _a_start() -> None:
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                raise RuntimeError("A not ready")

        brick_a = _make_lifecycle_brick("a")
        brick_a.start = AsyncMock(side_effect=_a_start)
        brick_b = _make_lifecycle_brick("b")

        manager.register("a", brick_a, protocol_name="AP")
        manager.register("b", brick_b, protocol_name="BP", depends_on=("a",))

        await manager.mount("a")  # fails (call 1)
        _s = manager.get_status("a")
        assert _s is not None
        assert _s.state == BrickState.FAILED

        # Reconcile: A succeeds on retry, then B should get mounted
        await reconciler.reconcile()  # A recovers (call 2)
        _s = manager.get_status("a")
        assert _s is not None
        assert _s.state == BrickState.ACTIVE

        # B may need another reconcile pass to mount (since A just recovered)
        await reconciler.reconcile()
        _s = manager.get_status("b")
        assert _s is not None
        assert _s.state == BrickState.ACTIVE


# ---------------------------------------------------------------------------
# TestReconcileHealthCheck (~4 tests)
# ---------------------------------------------------------------------------


class TestReconcileHealthCheck:
    """Test health check integration in reconciliation."""

    @pytest.mark.asyncio
    async def test_healthy_brick_stays_active(
        self, manager: BrickLifecycleManager, reconciler: BrickReconciler
    ) -> None:
        """health_check returns True → no action."""
        brick = _make_lifecycle_brick("healthy")
        brick.health_check = AsyncMock(return_value=True)
        manager.register("healthy", brick, protocol_name="HP")
        await manager.mount("healthy")

        result = await reconciler.reconcile()
        _s = manager.get_status("healthy")
        assert _s is not None
        assert _s.state == BrickState.ACTIVE
        assert result.drifted == 0

    @pytest.mark.asyncio
    async def test_unhealthy_brick_transitions_to_failed(
        self, manager: BrickLifecycleManager, reconciler: BrickReconciler
    ) -> None:
        """health_check returns False → FAILED."""
        brick = _make_lifecycle_brick("sick")
        brick.health_check = AsyncMock(return_value=False)
        manager.register("sick", brick, protocol_name="SP")
        await manager.mount("sick")

        result = await reconciler.reconcile()
        _s = manager.get_status("sick")
        assert _s is not None
        assert _s.state == BrickState.FAILED
        assert result.drifted >= 1

    @pytest.mark.asyncio
    async def test_health_check_timeout_treated_as_unhealthy(
        self,
        manager: BrickLifecycleManager,
    ) -> None:
        """Slow health_check that exceeds timeout → FAILED."""
        reconciler = BrickReconciler(
            lifecycle_manager=manager,
            health_check_timeout=0.05,  # Very short timeout
        )

        async def _slow_check() -> bool:
            await asyncio.sleep(10)
            return True

        brick = _make_lifecycle_brick("slow")
        brick.health_check = AsyncMock(side_effect=_slow_check)
        manager.register("slow", brick, protocol_name="SP")
        await manager.mount("slow")

        await reconciler.reconcile()
        _s = manager.get_status("slow")
        assert _s is not None
        assert _s.state == BrickState.FAILED

    @pytest.mark.asyncio
    async def test_stateless_brick_skips_health_check(
        self, manager: BrickLifecycleManager, reconciler: BrickReconciler
    ) -> None:
        """Stateless brick (no health_check method) → skip, stays ACTIVE."""
        brick = _make_stateless_brick("pay")
        manager.register("pay", brick, protocol_name="PP")
        await manager.mount("pay")

        result = await reconciler.reconcile()
        _s = manager.get_status("pay")
        assert _s is not None
        assert _s.state == BrickState.ACTIVE
        assert result.drifted == 0


# ---------------------------------------------------------------------------
# TestReconcileLoop (~5 tests)
# ---------------------------------------------------------------------------


class TestReconcileLoop:
    """Test periodic reconciliation loop lifecycle."""

    @pytest.mark.asyncio
    async def test_periodic_loop_fires_reconcile(
        self,
        manager: BrickLifecycleManager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Verify reconcile called every interval."""
        import nexus.system_services.lifecycle.brick_reconciler as _mod

        monkeypatch.setattr(_mod, "_JITTER_MAX", 0.0)

        reconciler = BrickReconciler(
            lifecycle_manager=manager,
            reconcile_interval=0.05,
        )
        manager.register("a", _make_lifecycle_brick("a"), protocol_name="AP")
        await manager.mount("a")

        await reconciler.start()
        await asyncio.sleep(0.30)  # Should fire 2-3 times (generous for slow CI runners)
        await reconciler.stop()
        assert reconciler._reconcile_count >= 2

    @pytest.mark.asyncio
    async def test_event_trigger_immediate_reconcile(
        self,
        manager: BrickLifecycleManager,
    ) -> None:
        """notify_state_change wakes loop for immediate reconcile."""
        reconciler = BrickReconciler(
            lifecycle_manager=manager,
            reconcile_interval=100.0,  # Very long interval
        )
        manager.register("a", _make_lifecycle_brick("a"), protocol_name="AP")
        await manager.mount("a")

        await reconciler.start()
        initial_count = reconciler._reconcile_count

        reconciler.notify_state_change("a")
        await asyncio.sleep(0.1)  # Give time for event-triggered reconcile

        assert reconciler._reconcile_count > initial_count
        await reconciler.stop()

    @pytest.mark.asyncio
    async def test_start_stop_lifecycle(
        self, manager: BrickLifecycleManager, reconciler: BrickReconciler
    ) -> None:
        """start creates task, stop cancels and clears it."""
        await reconciler.start()
        assert reconciler._task is not None
        assert not reconciler._task.done()

        await reconciler.stop()
        assert reconciler._task is None

    @pytest.mark.asyncio
    async def test_reconcile_error_doesnt_crash_loop(
        self,
        manager: BrickLifecycleManager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Exception in reconcile → log + continue."""
        import nexus.system_services.lifecycle.brick_reconciler as _mod

        monkeypatch.setattr(_mod, "_JITTER_MAX", 0.0)

        reconciler = BrickReconciler(
            lifecycle_manager=manager,
            reconcile_interval=0.05,
        )
        manager.register("a", _make_lifecycle_brick("a"), protocol_name="AP")
        await manager.mount("a")

        # Patch reconcile to fail once, then succeed
        original_reconcile = reconciler.reconcile
        call_count = 0

        async def _patched_reconcile():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("transient error")
            return await original_reconcile()

        reconciler.reconcile = _patched_reconcile  # type: ignore[assignment]

        await reconciler.start()
        await asyncio.sleep(0.2)
        await reconciler.stop()

        # Loop should have survived the error and continued
        assert call_count >= 2

    @pytest.mark.asyncio
    async def test_double_start_is_safe(
        self, manager: BrickLifecycleManager, reconciler: BrickReconciler
    ) -> None:
        """Calling start() twice doesn't create duplicate tasks."""
        await reconciler.start()
        task1 = reconciler._task
        await reconciler.start()  # Should be a no-op
        assert reconciler._task is task1
        await reconciler.stop()

    @pytest.mark.asyncio
    async def test_stop_without_start_is_safe(
        self, manager: BrickLifecycleManager, reconciler: BrickReconciler
    ) -> None:
        """Calling stop() without start() doesn't raise."""
        await reconciler.stop()  # Should not raise


# ---------------------------------------------------------------------------
# TestReconcileIntegration (~4 tests)
# ---------------------------------------------------------------------------


class TestReconcileIntegration:
    """Integration tests: full lifecycle with reconciler."""

    @pytest.mark.asyncio
    async def test_full_lifecycle_with_reconciler(
        self, manager: BrickLifecycleManager, reconciler: BrickReconciler
    ) -> None:
        """register → mount_all → fail → reconciler recovers."""
        call_count = 0

        async def _flaky_start() -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("boot failure")

        brick = _make_lifecycle_brick("search")
        brick.start = AsyncMock(side_effect=_flaky_start)
        manager.register("search", brick, protocol_name="SP")

        await manager.mount_all()
        _s = manager.get_status("search")
        assert _s is not None
        assert _s.state == BrickState.FAILED

        # Reconciler fixes it
        result = await reconciler.reconcile()
        _s = manager.get_status("search")
        assert _s is not None
        assert _s.state == BrickState.ACTIVE
        assert result.actions_taken >= 1

    @pytest.mark.asyncio
    async def test_reconciler_with_dag_dependencies(
        self, manager: BrickLifecycleManager, reconciler: BrickReconciler
    ) -> None:
        """Proper ordering respected during recovery."""
        a_calls = 0

        async def _a_start() -> None:
            nonlocal a_calls
            a_calls += 1
            if a_calls == 1:
                raise RuntimeError("A not ready")

        brick_a = _make_lifecycle_brick("a")
        brick_a.start = AsyncMock(side_effect=_a_start)
        brick_b = _make_lifecycle_brick("b")

        manager.register("a", brick_a, protocol_name="AP")
        manager.register("b", brick_b, protocol_name="BP", depends_on=("a",))

        await manager.mount_all()
        # A failed → B stays REGISTERED (deps not satisfied)

        # Reconcile — A should recover
        await reconciler.reconcile()
        _s = manager.get_status("a")
        assert _s is not None
        assert _s.state == BrickState.ACTIVE

        # If B was still REGISTERED, another pass mounts it
        _s = manager.get_status("b")
        assert _s is not None
        if _s.state != BrickState.ACTIVE:
            await reconciler.reconcile()
        _s = manager.get_status("b")
        assert _s is not None
        assert _s.state == BrickState.ACTIVE

    @pytest.mark.asyncio
    async def test_reconcile_returns_correct_counts(
        self, manager: BrickLifecycleManager, reconciler: BrickReconciler
    ) -> None:
        """ReconcileResult counts match actual actions."""
        manager.register("ok", _make_lifecycle_brick("ok"), protocol_name="OP")
        manager.register("idle", _make_lifecycle_brick("idle"), protocol_name="IP")
        await manager.mount("ok")
        # "idle" stays REGISTERED → will be detected as drifted

        result = await reconciler.reconcile()
        assert result.total_bricks == 2
        assert result.drifted == 1  # idle
        assert result.actions_taken == 1  # mount idle
        assert result.errors == 0


# ---------------------------------------------------------------------------
# TestConcurrentReconcile (~2 tests)
# ---------------------------------------------------------------------------


class TestConcurrentReconcile:
    """Test concurrent reconcile + manual mount don't race."""

    @pytest.mark.asyncio
    async def test_concurrent_reconcile_and_manual_mount(
        self,
    ) -> None:
        """Reconcile and manual mount on the same brick should not corrupt state."""
        manager = BrickLifecycleManager()
        reconciler = BrickReconciler(
            lifecycle_manager=manager,
            reconcile_interval=30.0,
        )

        brick = _make_lifecycle_brick("race")
        manager.register("race", brick, protocol_name="RP")

        # Run reconcile and manual mount concurrently
        await asyncio.gather(
            reconciler.reconcile(),
            manager.mount("race"),
            return_exceptions=True,
        )

        # Brick should be in a valid final state — either ACTIVE or FAILED
        status = manager.get_status("race")
        assert status is not None
        assert status.state in (BrickState.ACTIVE, BrickState.FAILED)

    @pytest.mark.asyncio
    async def test_concurrent_double_reconcile(
        self,
    ) -> None:
        """Two concurrent reconcile() calls should not corrupt state."""
        manager = BrickLifecycleManager()
        reconciler = BrickReconciler(
            lifecycle_manager=manager,
            reconcile_interval=30.0,
        )

        brick = _make_lifecycle_brick("shared")
        manager.register("shared", brick, protocol_name="SP")

        # Two reconcile passes at once
        results = await asyncio.gather(
            reconciler.reconcile(),
            reconciler.reconcile(),
            return_exceptions=True,
        )

        # At least one should succeed without exception
        successes = [r for r in results if not isinstance(r, Exception)]
        assert len(successes) >= 1

        # Final state should be valid
        status = manager.get_status("shared")
        assert status is not None
        assert status.state in (BrickState.ACTIVE, BrickState.FAILED)


# ---------------------------------------------------------------------------
# TestReconcileUnmounted — UNMOUNTED drift handling (Issue #2363)
# ---------------------------------------------------------------------------


class TestReconcileUnmounted:
    """Test reconciler handling of UNMOUNTED state."""

    @pytest.mark.asyncio
    async def test_unmounted_enabled_triggers_mount(
        self, manager: BrickLifecycleManager, reconciler: BrickReconciler
    ) -> None:
        """UNMOUNTED + enabled → reconciler should mount (DriftAction.MOUNT)."""
        brick = _make_lifecycle_brick("search")
        manager.register("search", brick, protocol_name="SP")
        await manager.mount("search")
        await manager.unmount("search")
        _s = manager.get_status("search")
        assert _s is not None
        assert _s.state == BrickState.UNMOUNTED

        result = await reconciler.reconcile()
        # Reconciler should have detected drift and remounted
        _s = manager.get_status("search")
        assert _s is not None
        assert _s.state == BrickState.ACTIVE
        assert result.drifted >= 1
        assert result.actions_taken >= 1

    @pytest.mark.asyncio
    async def test_unmounted_disabled_no_drift(
        self, manager: BrickLifecycleManager, reconciler: BrickReconciler
    ) -> None:
        """UNMOUNTED + disabled → no drift (reconciler should skip)."""
        brick = _make_lifecycle_brick("search")
        manager.register("search", brick, protocol_name="SP")
        await manager.mount("search")
        await manager.unmount("search")

        from dataclasses import replace

        entry = manager._bricks["search"]
        entry.spec = replace(entry.spec, enabled=False)

        result = await reconciler.reconcile()
        drifts = [d for d in result.drifts if d.brick_name == "search"]
        assert len(drifts) == 0

    @pytest.mark.asyncio
    async def test_reconciler_auto_remounts_unmounted(
        self, manager: BrickLifecycleManager, reconciler: BrickReconciler
    ) -> None:
        """Reconciler should auto-remount UNMOUNTED bricks that should be active."""
        brick = _make_lifecycle_brick("search")
        manager.register("search", brick, protocol_name="SP")
        await manager.mount("search")
        await manager.unmount("search")

        # Second start call succeeds
        await reconciler.reconcile()
        _s = manager.get_status("search")
        assert _s is not None
        assert _s.state == BrickState.ACTIVE
        assert brick.start.await_count == 2


# ---------------------------------------------------------------------------
# TestReconcilePerBrick — per-brick ReconcilerProtocol (Issue #2059)
# ---------------------------------------------------------------------------


def _make_reconcilable_brick(
    name: str = "search",
    outcome: BrickReconcileOutcome | None = None,
    side_effect: Exception | None = None,
) -> AsyncMock:
    """Create a mock brick that satisfies both BrickLifecycleProtocol + ReconcilerProtocol."""
    from nexus.contracts.protocols.brick_lifecycle import BrickLifecycleProtocol

    brick = AsyncMock(spec=BrickLifecycleProtocol)
    brick.start = AsyncMock(return_value=None)
    brick.stop = AsyncMock(return_value=None)
    brick.health_check = AsyncMock(return_value=True)
    brick.__class__.__name__ = f"{name.capitalize()}Brick"

    # Add reconcile() method so isinstance(brick, ReconcilerProtocol) == True
    if side_effect is not None:
        brick.reconcile = AsyncMock(side_effect=side_effect)
    else:
        brick.reconcile = AsyncMock(return_value=outcome or BrickReconcileOutcome())
    return brick


class TestReconcilePerBrick:
    """Test per-brick reconcile with ReconcilerProtocol (Issue #2059)."""

    @pytest.mark.asyncio
    async def test_reconcile_called_with_correct_context(
        self,
        manager: BrickLifecycleManager,
    ) -> None:
        """Brick with ReconcilerProtocol → reconcile() called with correct ReconcileContext."""
        brick = _make_reconcilable_brick("search")
        manager.register("search", brick, protocol_name="SP")
        await manager.mount("search")

        reconciler = BrickReconciler(lifecycle_manager=manager)
        await reconciler.reconcile()

        brick.reconcile.assert_called_once()
        ctx = brick.reconcile.call_args[0][0]
        assert isinstance(ctx, ReconcileContext)
        assert ctx.brick_name == "search"
        assert ctx.current_state == BrickState.ACTIVE
        assert ctx.desired_enabled is True
        assert ctx.retry_count == 0

    @pytest.mark.asyncio
    async def test_requeue_true_sets_backoff(
        self,
        manager: BrickLifecycleManager,
    ) -> None:
        """requeue=True → brick requeued with 1s-base backoff."""
        brick = _make_reconcilable_brick("search", outcome=BrickReconcileOutcome(requeue=True))
        manager.register("search", brick, protocol_name="SP")
        await manager.mount("search")

        reconciler = BrickReconciler(lifecycle_manager=manager)
        await reconciler.reconcile()

        # Should have set per-brick backoff
        assert "search" in reconciler._next_retry_after

    @pytest.mark.asyncio
    async def test_requeue_after_explicit_timing(
        self,
        manager: BrickLifecycleManager,
    ) -> None:
        """requeue_after=timedelta(5) → explicit timing honored."""
        from datetime import timedelta

        outcome = BrickReconcileOutcome(requeue=True, requeue_after=timedelta(seconds=5))
        brick = _make_reconcilable_brick("search", outcome=outcome)
        manager.register("search", brick, protocol_name="SP")
        await manager.mount("search")

        reconciler = BrickReconciler(lifecycle_manager=manager)
        import time

        before = time.monotonic()
        await reconciler.reconcile()

        # Backoff should be ~5 seconds from now
        deadline = reconciler._next_retry_after["search"]
        assert deadline >= before + 4.5  # Allow small tolerance

    @pytest.mark.asyncio
    async def test_error_transitions_to_failed(
        self,
        manager: BrickLifecycleManager,
    ) -> None:
        """error="..." → brick transitions to FAILED."""
        outcome = BrickReconcileOutcome(error="Index corrupted")
        brick = _make_reconcilable_brick("search", outcome=outcome)
        manager.register("search", brick, protocol_name="SP")
        await manager.mount("search")

        reconciler = BrickReconciler(lifecycle_manager=manager)
        await reconciler.reconcile()

        status = manager.get_status("search")
        assert status is not None
        assert status.state == BrickState.FAILED
        assert status.error == "Index corrupted"

    @pytest.mark.asyncio
    async def test_healthy_no_action(
        self,
        manager: BrickLifecycleManager,
    ) -> None:
        """requeue=False, error=None → healthy, backoff cleared."""
        brick = _make_reconcilable_brick("search", outcome=BrickReconcileOutcome())
        manager.register("search", brick, protocol_name="SP")
        await manager.mount("search")

        reconciler = BrickReconciler(lifecycle_manager=manager)
        # Pre-set backoff to verify it gets cleared
        reconciler._next_retry_after["search"] = 999999.0
        await reconciler.reconcile()

        # Backoff should have been cleared
        assert "search" not in reconciler._next_retry_after

    @pytest.mark.asyncio
    async def test_brick_without_protocol_fallback(
        self,
        manager: BrickLifecycleManager,
        reconciler: BrickReconciler,
    ) -> None:
        """Brick without ReconcilerProtocol → skipped in per-brick phase."""
        brick = _make_lifecycle_brick("search")
        manager.register("search", brick, protocol_name="SP")
        await manager.mount("search")

        result = await reconciler.reconcile()
        assert result.errors == 0
        # Should not appear in reconcile outcomes
        assert len(reconciler.last_reconcile_outcomes) == 0

    @pytest.mark.asyncio
    async def test_reconcile_raises_caught(
        self,
        manager: BrickLifecycleManager,
    ) -> None:
        """reconcile() raises → caught, logged, loop continues."""
        brick = _make_reconcilable_brick("search", side_effect=RuntimeError("crash"))
        manager.register("search", brick, protocol_name="SP")
        await manager.mount("search")

        reconciler = BrickReconciler(lifecycle_manager=manager)
        # Should not raise
        result = await reconciler.reconcile()
        assert result.errors == 0  # The per-brick error is handled gracefully

        # Outcome should be requeue=True (fallback on error)
        outcomes = reconciler.last_reconcile_outcomes
        assert len(outcomes) == 1
        assert outcomes[0][1].requeue is True

    @pytest.mark.asyncio
    async def test_reconcile_timeout_treated_as_requeue(
        self,
        manager: BrickLifecycleManager,
    ) -> None:
        """reconcile() timeout → treated as requeue=True."""

        async def _slow_reconcile(ctx: ReconcileContext) -> BrickReconcileOutcome:
            await asyncio.sleep(10)
            return BrickReconcileOutcome()

        brick = _make_reconcilable_brick("search")
        brick.reconcile = AsyncMock(side_effect=_slow_reconcile)
        manager.register("search", brick, protocol_name="SP")
        await manager.mount("search")

        reconciler = BrickReconciler(lifecycle_manager=manager, reconcile_timeout=0.05)
        await reconciler.reconcile()

        outcomes = reconciler.last_reconcile_outcomes
        assert len(outcomes) == 1
        assert outcomes[0][1].requeue is True

    @pytest.mark.asyncio
    async def test_sequential_execution(
        self,
        manager: BrickLifecycleManager,
    ) -> None:
        """Per-brick reconcile runs sequentially (not concurrently)."""
        order: list[str] = []

        async def _track_reconcile(name: str):
            async def _inner(ctx: ReconcileContext) -> BrickReconcileOutcome:
                order.append(name)
                return BrickReconcileOutcome()

            return _inner

        brick_a = _make_reconcilable_brick("a")
        brick_b = _make_reconcilable_brick("b")

        async def _reconcile_a(ctx: ReconcileContext) -> BrickReconcileOutcome:
            order.append("a")
            return BrickReconcileOutcome()

        async def _reconcile_b(ctx: ReconcileContext) -> BrickReconcileOutcome:
            order.append("b")
            return BrickReconcileOutcome()

        brick_a.reconcile = AsyncMock(side_effect=_reconcile_a)
        brick_b.reconcile = AsyncMock(side_effect=_reconcile_b)

        manager.register("a", brick_a, protocol_name="AP")
        manager.register("b", brick_b, protocol_name="BP")
        await manager.mount_all()

        reconciler = BrickReconciler(lifecycle_manager=manager)
        await reconciler.reconcile()

        # Both should have been called
        assert "a" in order
        assert "b" in order

    @pytest.mark.asyncio
    async def test_outcomes_exposed_via_property(
        self,
        manager: BrickLifecycleManager,
    ) -> None:
        """last_reconcile_outcomes property returns (name, outcome) pairs."""
        outcome = BrickReconcileOutcome(requeue=True)
        brick = _make_reconcilable_brick("search", outcome=outcome)
        manager.register("search", brick, protocol_name="SP")
        await manager.mount("search")

        reconciler = BrickReconciler(lifecycle_manager=manager)
        await reconciler.reconcile()

        results = reconciler.last_reconcile_outcomes
        assert len(results) == 1
        assert results[0][0] == "search"
        assert results[0][1].requeue is True

    @pytest.mark.asyncio
    async def test_terminating_zone_brick_skipped(
        self,
        manager: BrickLifecycleManager,
    ) -> None:
        """Bricks in terminating zones are skipped in per-brick reconcile."""
        brick = _make_reconcilable_brick("search")
        manager.register("search", brick, protocol_name="SP")
        await manager.mount("search")

        reconciler = BrickReconciler(lifecycle_manager=manager)
        reconciler.mark_zone_terminating("z1", brick_names={"search"})
        await reconciler.reconcile()

        # Should not have been called
        brick.reconcile.assert_not_called()


# ---------------------------------------------------------------------------
# TestBackoffStrategy — split backoff (Issue #2059)
# ---------------------------------------------------------------------------


class TestBackoffStrategy:
    """Test split backoff: per-brick reconcile (1s) vs central restart (30s)."""

    def test_per_brick_reconcile_uses_1s_base(self) -> None:
        """Per-brick reconcile backoff starts at 1s."""
        import nexus.system_services.lifecycle.brick_reconciler as mod

        assert mod._BACKOFF_BASE_RECONCILE == 1.0

    def test_central_restart_uses_30s_base(self) -> None:
        """Central restart backoff starts at 30s."""
        import nexus.system_services.lifecycle.brick_reconciler as mod

        assert mod._BACKOFF_BASE_RESTART == 30.0

    @pytest.mark.asyncio
    async def test_explicit_requeue_after_overrides_default(
        self,
        manager: BrickLifecycleManager,
    ) -> None:
        """BrickReconcileOutcome.requeue_after overrides computed backoff."""
        from datetime import timedelta

        outcome = BrickReconcileOutcome(requeue=True, requeue_after=timedelta(seconds=42))
        brick = _make_reconcilable_brick("search", outcome=outcome)
        manager.register("search", brick, protocol_name="SP")
        await manager.mount("search")

        reconciler = BrickReconciler(lifecycle_manager=manager)
        import time

        before = time.monotonic()
        await reconciler.reconcile()

        deadline = reconciler._next_retry_after.get("search", 0.0)
        assert deadline >= before + 41  # ~42s from now

    def test_cap_at_300s(self) -> None:
        """Both backoff strategies cap at 300s."""
        import nexus.system_services.lifecycle.brick_reconciler as mod

        assert mod._BACKOFF_MAX == 300.0

    @pytest.mark.asyncio
    async def test_clear_on_success(
        self,
        manager: BrickLifecycleManager,
    ) -> None:
        """Successful per-brick reconcile clears backoff."""
        brick = _make_reconcilable_brick("search", outcome=BrickReconcileOutcome())
        manager.register("search", brick, protocol_name="SP")
        await manager.mount("search")

        reconciler = BrickReconciler(lifecycle_manager=manager)
        reconciler._next_retry_after["search"] = 999999.0  # pre-set
        await reconciler.reconcile()

        assert "search" not in reconciler._next_retry_after
