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
    BrickDependency,
    BrickHealthReport,
    BrickLifecycleProtocol,
    BrickState,
    BrickStatus,
)

# ---------------------------------------------------------------------------
# BrickState enum tests
# ---------------------------------------------------------------------------


class TestBrickState:
    """Verify BrickState is a proper StrEnum with 5 lifecycle states + FAILED."""

    def test_is_enum(self) -> None:
        assert issubclass(BrickState, Enum)

    def test_has_five_lifecycle_states(self) -> None:
        expected = {"REGISTERED", "STARTING", "ACTIVE", "STOPPING", "UNREGISTERED"}
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
        assert names.index("STOPPING") < names.index("UNREGISTERED")


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
    """Verify lifecycle phase constants for HookEngine integration."""

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
