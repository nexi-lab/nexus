"""Tests for BrickLifecycleProtocol and lifecycle data models (Issue #1704).

TDD: Tests written FIRST, implementation follows.
"""

import dataclasses
from enum import Enum

import pytest

from nexus.services.protocols.brick_lifecycle import (
    BRICK_STARTED,
    BRICK_STOPPED,
    POST_MOUNT,
    POST_UNMOUNT,
    PRE_MOUNT,
    PRE_UNMOUNT,
    RECONCILE_COMPLETED,
    RECONCILE_STARTED,
    BrickDependency,
    BrickHealthReport,
    BrickLifecycleProtocol,
    BrickReconcileOutcome,
    BrickSpec,
    BrickState,
    BrickStatus,
    DriftAction,
    DriftReport,
    LifecycleManagerProtocol,
    ReconcileContext,
    ReconcileResult,
    ReconcilerProtocol,
)

# ---------------------------------------------------------------------------
# BrickState enum tests
# ---------------------------------------------------------------------------


class TestBrickState:
    """Verify BrickState is a proper StrEnum with 6 lifecycle states + FAILED."""

    def test_is_enum(self) -> None:
        assert issubclass(BrickState, Enum)

    def test_has_six_lifecycle_states(self) -> None:
        expected = {"REGISTERED", "STARTING", "ACTIVE", "STOPPING", "UNMOUNTED", "UNREGISTERED"}
        actual = {s.name for s in BrickState if s.name != "FAILED"}
        assert expected == actual

    def test_has_failed_state(self) -> None:
        assert hasattr(BrickState, "FAILED")
        assert BrickState.FAILED.value == "failed"

    def test_values_are_lowercase_strings(self) -> None:
        for state in BrickState:
            assert isinstance(state.value, str)
            assert state.value == state.name.lower()

    def test_ordering_reflects_lifecycle(self) -> None:
        """States should be defined in lifecycle order for readability."""
        names = [s.name for s in BrickState]
        assert names.index("REGISTERED") < names.index("STARTING")
        assert names.index("STARTING") < names.index("ACTIVE")
        assert names.index("ACTIVE") < names.index("STOPPING")
        assert names.index("STOPPING") < names.index("UNMOUNTED")
        assert names.index("UNMOUNTED") < names.index("UNREGISTERED")


# ---------------------------------------------------------------------------
# BrickStatus frozen dataclass tests
# ---------------------------------------------------------------------------


class TestBrickStatus:
    """Verify BrickStatus is a proper frozen, slots dataclass."""

    def test_frozen(self) -> None:
        status = BrickStatus(
            name="search",
            state=BrickState.ACTIVE,
            protocol_name="SearchProtocol",
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            status.state = BrickState.FAILED  # type: ignore[misc]

    def test_slots(self) -> None:
        assert hasattr(BrickStatus, "__slots__")

    def test_fields(self) -> None:
        fields = {f.name for f in dataclasses.fields(BrickStatus)}
        assert fields == {
            "name",
            "state",
            "protocol_name",
            "error",
            "started_at",
            "stopped_at",
            "unmounted_at",
        }

    def test_defaults(self) -> None:
        status = BrickStatus(
            name="pay",
            state=BrickState.REGISTERED,
            protocol_name="PaymentProtocol",
        )
        assert status.error is None
        assert status.started_at is None
        assert status.stopped_at is None
        assert status.unmounted_at is None

    def test_with_error(self) -> None:
        status = BrickStatus(
            name="search",
            state=BrickState.FAILED,
            protocol_name="SearchProtocol",
            error="Connection refused",
        )
        assert status.state == BrickState.FAILED
        assert status.error == "Connection refused"

    def test_with_timestamps(self) -> None:
        status = BrickStatus(
            name="search",
            state=BrickState.ACTIVE,
            protocol_name="SearchProtocol",
            started_at=1700000000.0,
        )
        assert status.started_at == 1700000000.0


# ---------------------------------------------------------------------------
# BrickDependency frozen dataclass tests
# ---------------------------------------------------------------------------


class TestBrickDependency:
    """Verify BrickDependency is a proper frozen, slots dataclass."""

    def test_frozen(self) -> None:
        dep = BrickDependency(brick_name="rag", depends_on=("search", "llm"))
        with pytest.raises(dataclasses.FrozenInstanceError):
            dep.brick_name = "changed"  # type: ignore[misc]

    def test_slots(self) -> None:
        assert hasattr(BrickDependency, "__slots__")

    def test_fields(self) -> None:
        fields = {f.name for f in dataclasses.fields(BrickDependency)}
        assert fields == {"brick_name", "depends_on"}

    def test_depends_on_is_tuple(self) -> None:
        dep = BrickDependency(brick_name="rag", depends_on=("search",))
        assert isinstance(dep.depends_on, tuple)

    def test_empty_dependencies(self) -> None:
        dep = BrickDependency(brick_name="pay", depends_on=())
        assert dep.depends_on == ()


# ---------------------------------------------------------------------------
# BrickHealthReport frozen dataclass tests
# ---------------------------------------------------------------------------


class TestBrickHealthReport:
    """Verify BrickHealthReport is a proper frozen, slots dataclass."""

    def test_frozen(self) -> None:
        report = BrickHealthReport(
            total=3,
            active=2,
            failed=1,
            bricks=(),
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            report.total = 5  # type: ignore[misc]

    def test_slots(self) -> None:
        assert hasattr(BrickHealthReport, "__slots__")

    def test_fields(self) -> None:
        fields = {f.name for f in dataclasses.fields(BrickHealthReport)}
        assert fields == {"total", "active", "failed", "bricks"}

    def test_bricks_is_tuple_of_statuses(self) -> None:
        s1 = BrickStatus(name="a", state=BrickState.ACTIVE, protocol_name="AP")
        s2 = BrickStatus(name="b", state=BrickState.FAILED, protocol_name="BP", error="err")
        report = BrickHealthReport(total=2, active=1, failed=1, bricks=(s1, s2))
        assert len(report.bricks) == 2
        assert report.bricks[0].name == "a"
        assert report.bricks[1].state == BrickState.FAILED

    def test_healthy_report(self) -> None:
        """All active bricks = healthy system."""
        s1 = BrickStatus(name="a", state=BrickState.ACTIVE, protocol_name="AP")
        report = BrickHealthReport(total=1, active=1, failed=0, bricks=(s1,))
        assert report.failed == 0

    def test_empty_report(self) -> None:
        report = BrickHealthReport(total=0, active=0, failed=0, bricks=())
        assert report.total == 0


# ---------------------------------------------------------------------------
# Lifecycle phase constants
# ---------------------------------------------------------------------------


class TestLifecyclePhaseConstants:
    """Verify lifecycle phase constants."""

    def test_mount_phase_values(self) -> None:
        assert PRE_MOUNT == "pre_mount"
        assert POST_MOUNT == "post_mount"
        assert PRE_UNMOUNT == "pre_unmount"
        assert POST_UNMOUNT == "post_unmount"

    def test_brick_event_values(self) -> None:
        assert BRICK_STARTED == "brick_started"
        assert BRICK_STOPPED == "brick_stopped"

    def test_phases_are_strings(self) -> None:
        for phase in (
            PRE_MOUNT,
            POST_MOUNT,
            PRE_UNMOUNT,
            POST_UNMOUNT,
            BRICK_STARTED,
            BRICK_STOPPED,
        ):
            assert isinstance(phase, str)

    def test_phases_are_unique(self) -> None:
        phases = [PRE_MOUNT, POST_MOUNT, PRE_UNMOUNT, POST_UNMOUNT, BRICK_STARTED, BRICK_STOPPED]
        assert len(phases) == len(set(phases))


# ---------------------------------------------------------------------------
# BrickLifecycleProtocol structural tests
# ---------------------------------------------------------------------------


class TestBrickLifecycleProtocol:
    """Verify BrickLifecycleProtocol is runtime_checkable with expected methods."""

    def test_is_runtime_checkable(self) -> None:
        assert getattr(BrickLifecycleProtocol, "_is_runtime_protocol", False)

    def test_expected_methods(self) -> None:
        expected = {"start", "stop", "health_check"}
        actual = {
            name
            for name in dir(BrickLifecycleProtocol)
            if not name.startswith("_") and callable(getattr(BrickLifecycleProtocol, name))
        }
        assert expected <= actual

    def test_stateless_brick_does_not_satisfy(self) -> None:
        """A plain object without start/stop/health_check should not satisfy."""

        class StatelessBrick:
            def process(self) -> None: ...

        assert not isinstance(StatelessBrick(), BrickLifecycleProtocol)

    def test_lifecycle_brick_satisfies(self) -> None:
        """An object with start/stop/health_check should satisfy via duck typing."""

        class LifecycleBrick:
            async def start(self) -> None: ...
            async def stop(self) -> None: ...
            async def health_check(self) -> bool:
                return True

        assert isinstance(LifecycleBrick(), BrickLifecycleProtocol)

    def test_partial_does_not_satisfy(self) -> None:
        """An object with only start (missing stop, health_check) should not satisfy."""

        class PartialBrick:
            async def start(self) -> None: ...

        assert not isinstance(PartialBrick(), BrickLifecycleProtocol)


# ---------------------------------------------------------------------------
# BrickSpec frozen dataclass tests (Issue #2060)
# ---------------------------------------------------------------------------


class TestBrickSpec:
    """Verify BrickSpec is a proper frozen, slots dataclass."""

    def test_frozen(self) -> None:
        spec = BrickSpec(name="search", protocol_name="SearchProtocol")
        with pytest.raises(dataclasses.FrozenInstanceError):
            spec.name = "changed"  # type: ignore[misc]

    def test_slots(self) -> None:
        assert hasattr(BrickSpec, "__slots__")

    def test_fields(self) -> None:
        fields = {f.name for f in dataclasses.fields(BrickSpec)}
        assert fields == {"name", "protocol_name", "depends_on", "enabled"}

    def test_defaults(self) -> None:
        spec = BrickSpec(name="pay", protocol_name="PaymentProtocol")
        assert spec.depends_on == ()
        assert spec.enabled is True

    def test_enabled_field(self) -> None:
        spec = BrickSpec(name="search", protocol_name="SP", enabled=False)
        assert spec.enabled is False

    def test_depends_on_is_tuple(self) -> None:
        spec = BrickSpec(name="rag", protocol_name="RP", depends_on=("search", "llm"))
        assert isinstance(spec.depends_on, tuple)
        assert spec.depends_on == ("search", "llm")

    def test_equality(self) -> None:
        s1 = BrickSpec(name="a", protocol_name="AP")
        s2 = BrickSpec(name="a", protocol_name="AP")
        assert s1 == s2


# ---------------------------------------------------------------------------
# DriftReport frozen dataclass tests (Issue #2060)
# ---------------------------------------------------------------------------


class TestDriftReport:
    """Verify DriftReport is a proper frozen, slots dataclass."""

    def test_frozen(self) -> None:
        report = DriftReport(
            brick_name="search",
            spec_state="enabled",
            actual_state=BrickState.FAILED,
            action=DriftAction.RESET,
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            report.action = DriftAction.SKIP  # type: ignore[misc]

    def test_slots(self) -> None:
        assert hasattr(DriftReport, "__slots__")

    def test_fields(self) -> None:
        fields = {f.name for f in dataclasses.fields(DriftReport)}
        assert fields == {"brick_name", "spec_state", "actual_state", "action", "detail"}

    def test_detail_default(self) -> None:
        report = DriftReport(
            brick_name="search",
            spec_state="enabled",
            actual_state=BrickState.FAILED,
            action=DriftAction.RESET,
        )
        assert report.detail == ""

    def test_with_detail(self) -> None:
        report = DriftReport(
            brick_name="search",
            spec_state="enabled",
            actual_state=BrickState.FAILED,
            action=DriftAction.RESET,
            detail="Brick failed, will reset and remount",
        )
        assert "reset and remount" in report.detail


# ---------------------------------------------------------------------------
# ReconcileResult frozen dataclass tests (Issue #2060)
# ---------------------------------------------------------------------------


class TestReconcileResult:
    """Verify ReconcileResult is a proper frozen, slots dataclass."""

    def test_frozen(self) -> None:
        result = ReconcileResult(total_bricks=5, drifted=1, actions_taken=1, errors=0)
        with pytest.raises(dataclasses.FrozenInstanceError):
            result.drifted = 0  # type: ignore[misc]

    def test_slots(self) -> None:
        assert hasattr(ReconcileResult, "__slots__")

    def test_fields(self) -> None:
        fields = {f.name for f in dataclasses.fields(ReconcileResult)}
        assert fields == {"total_bricks", "drifted", "actions_taken", "errors", "drifts"}

    def test_drifts_default(self) -> None:
        result = ReconcileResult(total_bricks=3, drifted=0, actions_taken=0, errors=0)
        assert result.drifts == ()

    def test_with_drifts(self) -> None:
        d = DriftReport(
            brick_name="search",
            spec_state="enabled",
            actual_state=BrickState.FAILED,
            action=DriftAction.RESET,
        )
        result = ReconcileResult(total_bricks=3, drifted=1, actions_taken=1, errors=0, drifts=(d,))
        assert len(result.drifts) == 1
        assert result.drifts[0].brick_name == "search"


# ---------------------------------------------------------------------------
# Reconcile phase constants (Issue #2060)
# ---------------------------------------------------------------------------


class TestReconcilePhaseConstants:
    """Verify reconcile lifecycle phase constants."""

    def test_reconcile_started_value(self) -> None:
        assert RECONCILE_STARTED == "reconcile_started"

    def test_reconcile_completed_value(self) -> None:
        assert RECONCILE_COMPLETED == "reconcile_completed"

    def test_reconcile_phases_are_strings(self) -> None:
        assert isinstance(RECONCILE_STARTED, str)
        assert isinstance(RECONCILE_COMPLETED, str)

    def test_reconcile_phases_are_unique_from_existing(self) -> None:
        all_phases = {
            PRE_MOUNT,
            POST_MOUNT,
            PRE_UNMOUNT,
            POST_UNMOUNT,
            BRICK_STARTED,
            BRICK_STOPPED,
            RECONCILE_STARTED,
            RECONCILE_COMPLETED,
        }
        assert len(all_phases) == 8


# ---------------------------------------------------------------------------
# ReconcileContext frozen dataclass tests (Issue #2059)
# ---------------------------------------------------------------------------


class TestReconcileContext:
    """Verify ReconcileContext is a proper frozen, slots dataclass."""

    def test_frozen(self) -> None:
        ctx = ReconcileContext(
            brick_name="search",
            current_state=BrickState.ACTIVE,
            desired_enabled=True,
            retry_count=0,
            last_error=None,
            last_healthy_at=1000.0,
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            ctx.brick_name = "changed"

    def test_slots(self) -> None:
        assert hasattr(ReconcileContext, "__slots__")

    def test_fields_accessible(self) -> None:
        ctx = ReconcileContext(
            brick_name="search",
            current_state=BrickState.FAILED,
            desired_enabled=True,
            retry_count=2,
            last_error="Connection refused",
            last_healthy_at=None,
        )
        assert ctx.brick_name == "search"
        assert ctx.current_state == BrickState.FAILED
        assert ctx.desired_enabled is True
        assert ctx.retry_count == 2
        assert ctx.last_error == "Connection refused"
        assert ctx.last_healthy_at is None


# ---------------------------------------------------------------------------
# BrickReconcileOutcome frozen dataclass tests (Issue #2059)
# ---------------------------------------------------------------------------


class TestBrickReconcileOutcome:
    """Verify BrickReconcileOutcome is a proper frozen, slots dataclass."""

    def test_defaults(self) -> None:
        outcome = BrickReconcileOutcome()
        assert outcome.requeue is False
        assert outcome.requeue_after is None
        assert outcome.error is None

    def test_requeue_true(self) -> None:
        outcome = BrickReconcileOutcome(requeue=True)
        assert outcome.requeue is True
        assert outcome.requeue_after is None

    def test_explicit_requeue_after(self) -> None:
        from datetime import timedelta

        outcome = BrickReconcileOutcome(requeue=True, requeue_after=timedelta(seconds=5))
        assert outcome.requeue is True
        assert outcome.requeue_after == timedelta(seconds=5)
        assert outcome.requeue_after.total_seconds() == 5.0

    def test_error_set(self) -> None:
        outcome = BrickReconcileOutcome(error="Index corrupted")
        assert outcome.error == "Index corrupted"

    def test_frozen(self) -> None:
        outcome = BrickReconcileOutcome()
        with pytest.raises(dataclasses.FrozenInstanceError):
            outcome.requeue = True


# ---------------------------------------------------------------------------
# ReconcilerProtocol structural tests (Issue #2059)
# ---------------------------------------------------------------------------


class TestReconcilerProtocol:
    """Verify ReconcilerProtocol is runtime_checkable with expected method."""

    def test_is_runtime_checkable(self) -> None:
        assert getattr(ReconcilerProtocol, "_is_runtime_protocol", False)

    def test_duck_typed_class_satisfies(self) -> None:
        class SelfHealingBrick:
            async def reconcile(self, ctx: ReconcileContext) -> BrickReconcileOutcome:
                return BrickReconcileOutcome()

        assert isinstance(SelfHealingBrick(), ReconcilerProtocol)

    def test_plain_class_does_not_satisfy(self) -> None:
        class PlainBrick:
            pass

        assert not isinstance(PlainBrick(), ReconcilerProtocol)


# ---------------------------------------------------------------------------
# LifecycleManagerProtocol structural tests (Issue #2059)
# ---------------------------------------------------------------------------


class TestLifecycleManagerProtocol:
    """Verify LifecycleManagerProtocol has expected methods."""

    def test_expected_methods(self) -> None:
        expected = {
            "iter_bricks",
            "get_status",
            "fail_brick",
            "reset_for_retry",
            "mount",
            "unmount",
            "clear_retry_count",
            "get_retry_count",
        }
        actual = {
            name
            for name in dir(LifecycleManagerProtocol)
            if not name.startswith("_") and callable(getattr(LifecycleManagerProtocol, name))
        }
        assert expected <= actual
