"""Tests for BrickLifecycleManager — state machine + DAG (Issue #1704).

TDD: Tests written FIRST, implementation follows.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from hypothesis import settings
from hypothesis.stateful import Bundle, RuleBasedStateMachine, initialize, rule

from nexus.services.brick_lifecycle import (
    BrickLifecycleManager,
    CyclicDependencyError,
    InvalidTransitionError,
)
from nexus.services.protocols.brick_lifecycle import (
    BrickSpec,
    BrickState,
)
from tests.unit.services.conftest import (
    make_failing_brick as _make_failing_brick,
)
from tests.unit.services.conftest import (
    make_lifecycle_brick as _make_lifecycle_brick,
)
from tests.unit.services.conftest import (
    make_stateless_brick as _make_stateless_brick,
)

# ---------------------------------------------------------------------------
# BrickLifecycleManager — Registration tests
# ---------------------------------------------------------------------------


class TestBrickRegistration:
    """Test brick registration into the lifecycle manager."""

    @pytest.fixture
    def manager(self) -> BrickLifecycleManager:
        return BrickLifecycleManager()

    def test_register_brick(self, manager: BrickLifecycleManager) -> None:
        brick = _make_lifecycle_brick("search")
        manager.register("search", brick, protocol_name="SearchProtocol")
        status = manager.get_status("search")
        assert status is not None
        assert status.state == BrickState.REGISTERED
        assert status.protocol_name == "SearchProtocol"

    def test_register_duplicate_raises(self, manager: BrickLifecycleManager) -> None:
        brick = _make_lifecycle_brick("search")
        manager.register("search", brick, protocol_name="SearchProtocol")
        with pytest.raises(ValueError, match="already registered"):
            manager.register("search", brick, protocol_name="SearchProtocol")

    def test_register_with_dependencies(self, manager: BrickLifecycleManager) -> None:
        brick = _make_lifecycle_brick("rag")
        manager.register(
            "rag",
            brick,
            protocol_name="RAGProtocol",
            depends_on=("search", "llm"),
        )
        status = manager.get_status("rag")
        assert status is not None
        assert status.state == BrickState.REGISTERED

    def test_register_stateless_brick(self, manager: BrickLifecycleManager) -> None:
        brick = _make_stateless_brick("pay")
        manager.register("pay", brick, protocol_name="PaymentProtocol")
        status = manager.get_status("pay")
        assert status is not None
        assert status.state == BrickState.REGISTERED

    def test_unregister_removes_brick(self, manager: BrickLifecycleManager) -> None:
        brick = _make_lifecycle_brick("search")
        manager.register("search", brick, protocol_name="SearchProtocol")
        manager.unregister("search")
        assert manager.get_status("search") is None

    def test_unregister_nonexistent_raises(self, manager: BrickLifecycleManager) -> None:
        with pytest.raises(KeyError, match="not found"):
            manager.unregister("nonexistent")


# ---------------------------------------------------------------------------
# State machine transition matrix (parametrized)
# ---------------------------------------------------------------------------


# Valid transitions: (from_state, event, expected_state)
VALID_TRANSITIONS = [
    (BrickState.REGISTERED, "mount", BrickState.STARTING),
    (BrickState.REGISTERED, "failed", BrickState.FAILED),  # Issue #2060: 5B
    (BrickState.STARTING, "started", BrickState.ACTIVE),
    (BrickState.STARTING, "failed", BrickState.FAILED),
    (BrickState.ACTIVE, "unmount", BrickState.STOPPING),
    (BrickState.ACTIVE, "failed", BrickState.FAILED),
    (BrickState.STOPPING, "stopped", BrickState.UNREGISTERED),
    (BrickState.STOPPING, "failed", BrickState.FAILED),
    (BrickState.FAILED, "reset", BrickState.REGISTERED),  # Issue #2060: 7A
]

# Invalid transitions: (from_state, event)
INVALID_TRANSITIONS = [
    (BrickState.REGISTERED, "started"),
    (BrickState.REGISTERED, "stopped"),
    (BrickState.REGISTERED, "unmount"),
    (BrickState.STARTING, "mount"),
    (BrickState.STARTING, "unmount"),
    (BrickState.STARTING, "stopped"),
    (BrickState.ACTIVE, "mount"),
    (BrickState.ACTIVE, "started"),
    (BrickState.ACTIVE, "stopped"),
    (BrickState.STOPPING, "mount"),
    (BrickState.STOPPING, "started"),
    (BrickState.STOPPING, "unmount"),
    (BrickState.UNREGISTERED, "mount"),
    (BrickState.UNREGISTERED, "unmount"),
    (BrickState.UNREGISTERED, "started"),
    (BrickState.UNREGISTERED, "stopped"),
    (BrickState.FAILED, "mount"),
    (BrickState.FAILED, "started"),
    (BrickState.FAILED, "stopped"),
    (BrickState.FAILED, "unmount"),
]


class TestStateTransitions:
    """Parametrized tests for state machine transition matrix."""

    @pytest.fixture
    def manager(self) -> BrickLifecycleManager:
        return BrickLifecycleManager()

    @pytest.mark.parametrize(
        ("from_state", "event", "expected_state"),
        VALID_TRANSITIONS,
        ids=[f"{f.name}->{e}->{t.name}" for f, e, t in VALID_TRANSITIONS],
    )
    def test_valid_transition(
        self,
        manager: BrickLifecycleManager,
        from_state: BrickState,
        event: str,
        expected_state: BrickState,
    ) -> None:
        brick = _make_lifecycle_brick("test")
        manager.register("test", brick, protocol_name="TestProtocol")
        # Force the brick into from_state
        manager._force_state("test", from_state)
        # Apply the event
        manager._transition("test", event)
        assert manager.get_status("test").state == expected_state  # type: ignore[union-attr]

    @pytest.mark.parametrize(
        ("from_state", "event"),
        INVALID_TRANSITIONS,
        ids=[f"{f.name}->{e}" for f, e in INVALID_TRANSITIONS],
    )
    def test_invalid_transition_raises(
        self,
        manager: BrickLifecycleManager,
        from_state: BrickState,
        event: str,
    ) -> None:
        brick = _make_lifecycle_brick("test")
        manager.register("test", brick, protocol_name="TestProtocol")
        manager._force_state("test", from_state)
        with pytest.raises(InvalidTransitionError):
            manager._transition("test", event)


# ---------------------------------------------------------------------------
# DAG topological sort tests
# ---------------------------------------------------------------------------


class TestDependencyDAG:
    """Test DAG construction and topological sort for startup/shutdown."""

    @pytest.fixture
    def manager(self) -> BrickLifecycleManager:
        return BrickLifecycleManager()

    def test_no_dependencies_single_level(self, manager: BrickLifecycleManager) -> None:
        """All independent bricks should be at level 0."""
        manager.register("a", _make_lifecycle_brick("a"), protocol_name="AP")
        manager.register("b", _make_lifecycle_brick("b"), protocol_name="BP")
        manager.register("c", _make_lifecycle_brick("c"), protocol_name="CP")
        levels = manager.compute_startup_order()
        # All at level 0 — single level with all 3 bricks
        assert len(levels) == 1
        assert set(levels[0]) == {"a", "b", "c"}

    def test_linear_dependency_chain(self, manager: BrickLifecycleManager) -> None:
        """A→B→C should produce 3 levels."""
        manager.register("a", _make_lifecycle_brick("a"), protocol_name="AP")
        manager.register("b", _make_lifecycle_brick("b"), protocol_name="BP", depends_on=("a",))
        manager.register("c", _make_lifecycle_brick("c"), protocol_name="CP", depends_on=("b",))
        levels = manager.compute_startup_order()
        assert len(levels) == 3
        assert levels[0] == ["a"]
        assert levels[1] == ["b"]
        assert levels[2] == ["c"]

    def test_diamond_dependency(self, manager: BrickLifecycleManager) -> None:
        """Diamond: A←B, A←C, B←D, C←D should produce 3 levels."""
        manager.register("a", _make_lifecycle_brick("a"), protocol_name="AP")
        manager.register("b", _make_lifecycle_brick("b"), protocol_name="BP", depends_on=("a",))
        manager.register("c", _make_lifecycle_brick("c"), protocol_name="CP", depends_on=("a",))
        manager.register("d", _make_lifecycle_brick("d"), protocol_name="DP", depends_on=("b", "c"))
        levels = manager.compute_startup_order()
        assert len(levels) == 3
        assert levels[0] == ["a"]
        assert set(levels[1]) == {"b", "c"}
        assert levels[2] == ["d"]

    def test_cyclic_dependency_raises(self, manager: BrickLifecycleManager) -> None:
        """Cyclic: A→B→C→A should raise CyclicDependencyError."""
        manager.register("a", _make_lifecycle_brick("a"), protocol_name="AP", depends_on=("c",))
        manager.register("b", _make_lifecycle_brick("b"), protocol_name="BP", depends_on=("a",))
        manager.register("c", _make_lifecycle_brick("c"), protocol_name="CP", depends_on=("b",))
        with pytest.raises(CyclicDependencyError):
            manager.compute_startup_order()

    def test_shutdown_order_is_reverse_of_startup(self, manager: BrickLifecycleManager) -> None:
        """Shutdown order should be the exact reverse of startup order."""
        manager.register("a", _make_lifecycle_brick("a"), protocol_name="AP")
        manager.register("b", _make_lifecycle_brick("b"), protocol_name="BP", depends_on=("a",))
        manager.register("c", _make_lifecycle_brick("c"), protocol_name="CP", depends_on=("b",))
        manager.compute_startup_order()  # ensure no cycle
        shutdown = manager.compute_shutdown_order()
        # Reverse: C first, then B, then A
        assert len(shutdown) == 3
        assert shutdown[0] == ["c"]
        assert shutdown[1] == ["b"]
        assert shutdown[2] == ["a"]

    def test_missing_dependency_raises(self, manager: BrickLifecycleManager) -> None:
        """Depending on unregistered brick should raise."""
        manager.register(
            "b", _make_lifecycle_brick("b"), protocol_name="BP", depends_on=("nonexistent",)
        )
        with pytest.raises(KeyError, match="nonexistent"):
            manager.compute_startup_order()


# ---------------------------------------------------------------------------
# Mount/unmount lifecycle tests
# ---------------------------------------------------------------------------


class TestMountUnmount:
    """Test the full mount and unmount lifecycle operations."""

    @pytest.fixture
    def manager(self) -> BrickLifecycleManager:
        return BrickLifecycleManager()

    @pytest.mark.asyncio
    async def test_mount_lifecycle_brick(self, manager: BrickLifecycleManager) -> None:
        """Mount should: REGISTERED→STARTING→(start())→ACTIVE."""
        brick = _make_lifecycle_brick("search")
        manager.register("search", brick, protocol_name="SearchProtocol")
        await manager.mount("search")
        assert manager.get_status("search").state == BrickState.ACTIVE  # type: ignore[union-attr]
        brick.start.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_mount_stateless_brick(self, manager: BrickLifecycleManager) -> None:
        """Stateless brick mount should go directly to ACTIVE (no start() call)."""
        brick = _make_stateless_brick("pay")
        manager.register("pay", brick, protocol_name="PaymentProtocol")
        await manager.mount("pay")
        assert manager.get_status("pay").state == BrickState.ACTIVE  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_mount_failing_brick_transitions_to_failed(
        self, manager: BrickLifecycleManager
    ) -> None:
        """If start() raises, brick transitions to FAILED."""
        brick = _make_failing_brick(RuntimeError("Connection refused"))
        manager.register("failing", brick, protocol_name="FP")
        await manager.mount("failing")
        status = manager.get_status("failing")
        assert status is not None
        assert status.state == BrickState.FAILED
        assert "Connection refused" in (status.error or "")

    @pytest.mark.asyncio
    async def test_unmount_lifecycle_brick(self, manager: BrickLifecycleManager) -> None:
        """Unmount should: ACTIVE→STOPPING→(stop())→UNREGISTERED."""
        brick = _make_lifecycle_brick("search")
        manager.register("search", brick, protocol_name="SearchProtocol")
        await manager.mount("search")
        await manager.unmount("search")
        assert manager.get_status("search").state == BrickState.UNREGISTERED  # type: ignore[union-attr]
        brick.stop.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unmount_stateless_brick(self, manager: BrickLifecycleManager) -> None:
        """Stateless brick unmount should go directly to UNREGISTERED."""
        brick = _make_stateless_brick("pay")
        manager.register("pay", brick, protocol_name="PaymentProtocol")
        await manager.mount("pay")
        await manager.unmount("pay")
        assert manager.get_status("pay").state == BrickState.UNREGISTERED  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_mount_nonexistent_raises(self, manager: BrickLifecycleManager) -> None:
        with pytest.raises(KeyError, match="not found"):
            await manager.mount("nonexistent")

    @pytest.mark.asyncio
    async def test_unmount_non_active_raises(self, manager: BrickLifecycleManager) -> None:
        """Can only unmount ACTIVE bricks."""
        brick = _make_lifecycle_brick("search")
        manager.register("search", brick, protocol_name="SearchProtocol")
        with pytest.raises(InvalidTransitionError):
            await manager.unmount("search")  # Still REGISTERED, not ACTIVE

    @pytest.mark.asyncio
    async def test_mount_with_timeout(self, manager: BrickLifecycleManager) -> None:
        """Brick that exceeds timeout should transition to FAILED."""

        async def slow_start() -> None:
            await asyncio.sleep(10)

        brick = _make_lifecycle_brick("slow")
        brick.start = AsyncMock(side_effect=slow_start)
        manager.register("slow", brick, protocol_name="SP")
        await manager.mount("slow", timeout=0.1)
        status = manager.get_status("slow")
        assert status is not None
        assert status.state == BrickState.FAILED
        assert "timeout" in (status.error or "").lower()


# ---------------------------------------------------------------------------
# Health reporting tests
# ---------------------------------------------------------------------------


class TestHealthReport:
    """Test BrickHealthReport generation."""

    @pytest.fixture
    def manager(self) -> BrickLifecycleManager:
        return BrickLifecycleManager()

    def test_empty_report(self, manager: BrickLifecycleManager) -> None:
        report = manager.health()
        assert report.total == 0
        assert report.active == 0
        assert report.failed == 0
        assert report.bricks == ()

    @pytest.mark.asyncio
    async def test_mixed_health_report(self, manager: BrickLifecycleManager) -> None:
        """Report with active and failed bricks."""
        manager.register("a", _make_lifecycle_brick("a"), protocol_name="AP")
        manager.register("b", _make_failing_brick(), protocol_name="BP")
        await manager.mount("a")
        await manager.mount("b")  # Will fail
        report = manager.health()
        assert report.total == 2
        assert report.active == 1
        assert report.failed == 1

    @pytest.mark.asyncio
    async def test_health_report_includes_all_bricks(self, manager: BrickLifecycleManager) -> None:
        manager.register("a", _make_lifecycle_brick("a"), protocol_name="AP")
        manager.register("b", _make_lifecycle_brick("b"), protocol_name="BP")
        await manager.mount("a")
        # b stays REGISTERED
        report = manager.health()
        assert report.total == 2
        names = {s.name for s in report.bricks}
        assert names == {"a", "b"}


# ---------------------------------------------------------------------------
# Mount all / unmount all (DAG-ordered)
# ---------------------------------------------------------------------------


class TestMountAllUnmountAll:
    """Test batch mount/unmount respecting DAG order."""

    @pytest.fixture
    def manager(self) -> BrickLifecycleManager:
        return BrickLifecycleManager()

    @pytest.mark.asyncio
    async def test_mount_all_respects_dag_order(self, manager: BrickLifecycleManager) -> None:
        """Bricks should start in topological order."""
        order: list[str] = []

        def _make_tracked_brick(name: str) -> MagicMock:
            brick = _make_lifecycle_brick(name)

            async def _track_start() -> None:
                order.append(name)

            brick.start = AsyncMock(side_effect=_track_start)
            return brick

        brick_a = _make_tracked_brick("a")
        brick_b = _make_tracked_brick("b")
        brick_c = _make_tracked_brick("c")

        manager.register("a", brick_a, protocol_name="AP")
        manager.register("b", brick_b, protocol_name="BP", depends_on=("a",))
        manager.register("c", brick_c, protocol_name="CP", depends_on=("b",))

        await manager.mount_all()

        # a must start before b, b before c
        assert order.index("a") < order.index("b")
        assert order.index("b") < order.index("c")

    @pytest.mark.asyncio
    async def test_unmount_all_reverses_order(self, manager: BrickLifecycleManager) -> None:
        """Shutdown should stop in reverse topological order."""
        order: list[str] = []

        def _make_tracked_stop_brick(name: str) -> MagicMock:
            brick = _make_lifecycle_brick(name)

            async def _track_stop() -> None:
                order.append(name)

            brick.stop = AsyncMock(side_effect=_track_stop)
            return brick

        brick_a = _make_tracked_stop_brick("a")
        brick_b = _make_tracked_stop_brick("b")

        manager.register("a", brick_a, protocol_name="AP")
        manager.register("b", brick_b, protocol_name="BP", depends_on=("a",))

        await manager.mount_all()
        await manager.unmount_all()

        # b must stop before a (reverse of start)
        assert order.index("b") < order.index("a")

    @pytest.mark.asyncio
    async def test_mount_all_continues_on_failure(self, manager: BrickLifecycleManager) -> None:
        """One brick failure should not prevent others from starting."""
        brick_a = _make_lifecycle_brick("a")
        brick_b = _make_failing_brick(RuntimeError("fail"))
        brick_c = _make_lifecycle_brick("c")

        manager.register("a", brick_a, protocol_name="AP")
        manager.register("b", brick_b, protocol_name="BP")
        manager.register("c", brick_c, protocol_name="CP")

        await manager.mount_all()

        assert manager.get_status("a").state == BrickState.ACTIVE  # type: ignore[union-attr]
        assert manager.get_status("b").state == BrickState.FAILED  # type: ignore[union-attr]
        assert manager.get_status("c").state == BrickState.ACTIVE  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Reset, Spec, and _safe_lifecycle_op tests (Issue #2060)
# ---------------------------------------------------------------------------


class TestResetAndSpec:
    """Test reset(), get_spec(), all_specs(), and _safe_lifecycle_op."""

    @pytest.fixture
    def manager(self) -> BrickLifecycleManager:
        return BrickLifecycleManager()

    @pytest.mark.asyncio
    async def test_reset_failed_brick_then_remount(self, manager: BrickLifecycleManager) -> None:
        """FAILED → reset → REGISTERED → mount → ACTIVE."""
        brick = _make_lifecycle_brick("search")
        # First start fails
        brick.start = AsyncMock(
            side_effect=[RuntimeError("fail"), None]  # fail first, succeed second
        )
        manager.register("search", brick, protocol_name="SearchProtocol")
        await manager.mount("search")
        assert manager.get_status("search").state == BrickState.FAILED  # type: ignore[union-attr]

        manager.reset("search")
        assert manager.get_status("search").state == BrickState.REGISTERED  # type: ignore[union-attr]

        await manager.mount("search")
        assert manager.get_status("search").state == BrickState.ACTIVE  # type: ignore[union-attr]

    def test_reset_clears_error_and_timestamps(self, manager: BrickLifecycleManager) -> None:
        brick = _make_lifecycle_brick("search")
        manager.register("search", brick, protocol_name="SearchProtocol")
        # Force into FAILED with error
        manager._force_state("search", BrickState.FAILED)
        entry = manager._bricks["search"]
        entry.error = "some error"
        entry.started_at = 123.0
        entry.stopped_at = 456.0
        entry.retry_count = 2

        manager.reset("search")
        assert entry.error is None
        assert entry.started_at is None
        assert entry.stopped_at is None
        assert entry.retry_count == 0

    def test_reset_nonexistent_raises_keyerror(self, manager: BrickLifecycleManager) -> None:
        with pytest.raises(KeyError, match="not found"):
            manager.reset("nonexistent")

    def test_reset_non_failed_raises_invalid_transition(
        self, manager: BrickLifecycleManager
    ) -> None:
        brick = _make_lifecycle_brick("search")
        manager.register("search", brick, protocol_name="SearchProtocol")
        with pytest.raises(InvalidTransitionError):
            manager.reset("search")  # REGISTERED, not FAILED

    def test_get_spec_returns_frozen_spec(self, manager: BrickLifecycleManager) -> None:
        brick = _make_lifecycle_brick("search")
        manager.register("search", brick, protocol_name="SearchProtocol", depends_on=("llm",))
        spec = manager.get_spec("search")
        assert spec is not None
        assert isinstance(spec, BrickSpec)
        assert spec.name == "search"
        assert spec.protocol_name == "SearchProtocol"
        assert spec.depends_on == ("llm",)
        assert spec.enabled is True

    def test_get_spec_nonexistent_returns_none(self, manager: BrickLifecycleManager) -> None:
        assert manager.get_spec("nonexistent") is None

    def test_all_specs_returns_all(self, manager: BrickLifecycleManager) -> None:
        manager.register("a", _make_lifecycle_brick("a"), protocol_name="AP")
        manager.register("b", _make_lifecycle_brick("b"), protocol_name="BP")
        specs = manager.all_specs()
        assert len(specs) == 2
        assert "a" in specs
        assert "b" in specs
        assert specs["a"].name == "a"
        assert specs["b"].protocol_name == "BP"

    @pytest.mark.asyncio
    async def test_safe_lifecycle_op_catches_and_fails(
        self, manager: BrickLifecycleManager
    ) -> None:
        """_safe_mount should catch exceptions and transition brick to FAILED."""
        brick = _make_lifecycle_brick("test")
        manager.register("test", brick, protocol_name="TP")
        # The _safe_mount calls mount which on exception transitions to FAILED
        brick.start = AsyncMock(side_effect=RuntimeError("boom"))
        await manager._safe_mount("test", timeout=5.0)
        assert manager.get_status("test").state == BrickState.FAILED  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_dag_failure_skips_dependent(self, manager: BrickLifecycleManager) -> None:
        """If A fails, B (depends on A) stays REGISTERED after mount_all."""
        brick_a = _make_failing_brick(RuntimeError("A failed"))
        brick_b = _make_lifecycle_brick("b")

        manager.register("a", brick_a, protocol_name="AP")
        manager.register("b", brick_b, protocol_name="BP", depends_on=("a",))

        await manager.mount_all()

        assert manager.get_status("a").state == BrickState.FAILED  # type: ignore[union-attr]
        # B depends on A; mount_all skips bricks whose deps aren't ACTIVE.
        # B stays REGISTERED — the reconciler will mount it once A recovers.
        assert manager.get_status("b").state == BrickState.REGISTERED  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Hypothesis stateful test — random transition sequences
# ---------------------------------------------------------------------------


class BrickLifecycleStateMachine(RuleBasedStateMachine):
    """Property-based test: random lifecycle operations should never corrupt state.

    Uses a single event loop per test case instead of ``asyncio.run()`` per rule
    to avoid creating/destroying loops on every step (asyncio.Lock objects bind
    to the running loop, so a shared loop prevents cross-loop issues).
    """

    bricks = Bundle("bricks")

    def __init__(self) -> None:
        super().__init__()
        self._loop = asyncio.new_event_loop()
        self.manager = BrickLifecycleManager()
        self._mounted: set[str] = set()
        self._counter = 0

    def teardown(self) -> None:
        self._loop.close()

    def _run(self, coro: Any) -> Any:  # noqa: ANN401
        """Run an async coroutine on the shared event loop."""
        return self._loop.run_until_complete(coro)

    @initialize(target=bricks)
    def init_brick(self) -> str:
        name = f"brick_{self._counter}"
        self._counter += 1
        brick = _make_lifecycle_brick(name)
        self.manager.register(name, brick, protocol_name=f"{name}Proto")
        return name

    @rule(target=bricks)
    def add_brick(self) -> str:
        name = f"brick_{self._counter}"
        self._counter += 1
        brick = _make_lifecycle_brick(name)
        self.manager.register(name, brick, protocol_name=f"{name}Proto")
        return name

    @rule(name=bricks)
    def mount_brick(self, name: str) -> None:
        status = self.manager.get_status(name)
        if status is not None and status.state == BrickState.REGISTERED:
            self._run(self.manager.mount(name))
            self._mounted.add(name)

    @rule(name=bricks)
    def unmount_brick(self, name: str) -> None:
        status = self.manager.get_status(name)
        if status is not None and status.state == BrickState.ACTIVE:
            self._run(self.manager.unmount(name))
            self._mounted.discard(name)

    @rule()
    def check_health_invariant(self) -> None:
        """Health report counters should always be consistent."""
        report = self.manager.health()
        assert report.total == len(self.manager._bricks)
        assert report.active + report.failed <= report.total
        for brick_status in report.bricks:
            assert isinstance(brick_status.state, BrickState)

    @rule(name=bricks)
    def check_status_invariant(self, name: str) -> None:
        """Status should always be retrievable for registered bricks."""
        status = self.manager.get_status(name)
        assert status is not None
        assert isinstance(status.state, BrickState)


TestBrickLifecycleHypothesis = BrickLifecycleStateMachine.TestCase
TestBrickLifecycleHypothesis.settings = settings(max_examples=50, stateful_step_count=20)
