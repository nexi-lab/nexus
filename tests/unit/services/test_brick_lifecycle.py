"""Tests for BrickLifecycleManager — state machine + DAG (Issue #1704).

TDD: Tests written FIRST, implementation follows.
"""

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("pyroaring")
pytest.importorskip("hypothesis")

from hypothesis import settings
from hypothesis.stateful import Bundle, RuleBasedStateMachine, initialize, rule

from nexus.contracts.protocols.brick_lifecycle import (
    EVENT_FAILED,
    EVENT_MOUNT,
    EVENT_RESET,
    EVENT_STARTED,
    EVENT_STOPPED,
    EVENT_UNMOUNT,
    EVENT_UNMOUNTED,
    EVENT_UNREGISTER,
    BrickSpec,
    BrickState,
)
from nexus.system_services.lifecycle.brick_lifecycle import (
    MAX_TRANSITION_HISTORY,
    BrickLifecycleManager,
    CyclicDependencyError,
    InvalidTransitionError,
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

    @pytest.mark.asyncio
    async def test_unregister_nonexistent_raises(self, manager: BrickLifecycleManager) -> None:
        with pytest.raises(KeyError, match="not found"):
            await manager.unregister("nonexistent")


# ---------------------------------------------------------------------------
# State machine transition matrix (parametrized)
# ---------------------------------------------------------------------------

# Valid transitions: (from_state, event, expected_state)
VALID_TRANSITIONS = [
    (BrickState.REGISTERED, EVENT_MOUNT, BrickState.STARTING),
    (BrickState.REGISTERED, EVENT_FAILED, BrickState.FAILED),  # Issue #2060: 5B
    (BrickState.STARTING, EVENT_STARTED, BrickState.ACTIVE),
    (BrickState.STARTING, EVENT_FAILED, BrickState.FAILED),
    (BrickState.ACTIVE, EVENT_UNMOUNT, BrickState.STOPPING),
    (BrickState.ACTIVE, EVENT_FAILED, BrickState.FAILED),
    (BrickState.STOPPING, EVENT_STOPPED, BrickState.UNMOUNTED),  # Issue #2363
    (BrickState.STOPPING, EVENT_FAILED, BrickState.FAILED),
    (BrickState.UNMOUNTED, EVENT_MOUNT, BrickState.STARTING),  # Issue #2363: re-mount
    (BrickState.UNMOUNTED, EVENT_UNREGISTER, BrickState.UNREGISTERED),  # Issue #2363
    (BrickState.UNMOUNTED, EVENT_FAILED, BrickState.FAILED),  # Issue #2363
    (BrickState.FAILED, EVENT_RESET, BrickState.REGISTERED),  # Issue #2060: 7A
]

# Programmatic invalid transition generation (Issue #2363: 9A)
ALL_EVENTS = [
    EVENT_MOUNT,
    EVENT_STARTED,
    EVENT_FAILED,
    EVENT_UNMOUNT,
    EVENT_STOPPED,
    EVENT_UNMOUNTED,
    EVENT_UNREGISTER,
    EVENT_RESET,
]
_VALID_SET = {(s, e) for s, e, _ in VALID_TRANSITIONS}
INVALID_TRANSITIONS = [(s, e) for s in BrickState for e in ALL_EVENTS if (s, e) not in _VALID_SET]


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
        _s = manager.get_status("test")
        assert _s is not None
        assert _s.state == expected_state

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
        _s = manager.get_status("search")
        assert _s is not None
        assert _s.state == BrickState.ACTIVE
        brick.start.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_mount_stateless_brick(self, manager: BrickLifecycleManager) -> None:
        """Stateless brick mount should go directly to ACTIVE (no start() call)."""
        brick = _make_stateless_brick("pay")
        manager.register("pay", brick, protocol_name="PaymentProtocol")
        await manager.mount("pay")
        _s = manager.get_status("pay")
        assert _s is not None
        assert _s.state == BrickState.ACTIVE

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
        """Unmount should: ACTIVE→STOPPING→(stop())→UNMOUNTED."""
        brick = _make_lifecycle_brick("search")
        manager.register("search", brick, protocol_name="SearchProtocol")
        await manager.mount("search")
        await manager.unmount("search")
        _s = manager.get_status("search")
        assert _s is not None
        assert _s.state == BrickState.UNMOUNTED
        brick.stop.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unmount_stateless_brick(self, manager: BrickLifecycleManager) -> None:
        """Stateless brick unmount should go directly to UNMOUNTED."""
        brick = _make_stateless_brick("pay")
        manager.register("pay", brick, protocol_name="PaymentProtocol")
        await manager.mount("pay")
        await manager.unmount("pay")
        _s = manager.get_status("pay")
        assert _s is not None
        assert _s.state == BrickState.UNMOUNTED

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

        _s = manager.get_status("a")
        assert _s is not None
        assert _s.state == BrickState.ACTIVE
        _s = manager.get_status("b")
        assert _s is not None
        assert _s.state == BrickState.FAILED
        _s = manager.get_status("c")
        assert _s is not None
        assert _s.state == BrickState.ACTIVE


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
        _s = manager.get_status("search")
        assert _s is not None
        assert _s.state == BrickState.FAILED

        manager.reset("search")
        _s = manager.get_status("search")
        assert _s is not None
        assert _s.state == BrickState.REGISTERED

        await manager.mount("search")
        _s = manager.get_status("search")
        assert _s is not None
        assert _s.state == BrickState.ACTIVE

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
        _s = manager.get_status("test")
        assert _s is not None
        assert _s.state == BrickState.FAILED

    @pytest.mark.asyncio
    async def test_dag_failure_skips_dependent(self, manager: BrickLifecycleManager) -> None:
        """If A fails, B (depends on A) stays REGISTERED after mount_all."""
        brick_a = _make_failing_brick(RuntimeError("A failed"))
        brick_b = _make_lifecycle_brick("b")

        manager.register("a", brick_a, protocol_name="AP")
        manager.register("b", brick_b, protocol_name="BP", depends_on=("a",))

        await manager.mount_all()

        _s = manager.get_status("a")
        assert _s is not None
        assert _s.state == BrickState.FAILED
        # B depends on A; mount_all skips bricks whose deps aren't ACTIVE.
        # B stays REGISTERED — the reconciler will mount it once A recovers.
        _s = manager.get_status("b")
        assert _s is not None
        assert _s.state == BrickState.REGISTERED


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
        self._unregistered: set[str] = set()
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
        if status is not None and status.state in (
            BrickState.REGISTERED,
            BrickState.UNMOUNTED,
        ):
            self._run(self.manager.mount(name))
            self._mounted.add(name)

    @rule(name=bricks)
    def unmount_brick(self, name: str) -> None:
        status = self.manager.get_status(name)
        if status is not None and status.state == BrickState.ACTIVE:
            self._run(self.manager.unmount(name))
            self._mounted.discard(name)

    @rule(name=bricks)
    def remount_brick(self, name: str) -> None:
        """Issue #2363: re-mount from UNMOUNTED."""
        status = self.manager.get_status(name)
        if status is not None and status.state == BrickState.UNMOUNTED:
            self._run(self.manager.remount(name))
            self._mounted.add(name)

    @rule(name=bricks)
    def unregister_brick(self, name: str) -> None:
        """Issue #2363: unregister from UNMOUNTED."""
        status = self.manager.get_status(name)
        if status is not None and status.state == BrickState.UNMOUNTED:
            self._run(self.manager.unregister(name))
            self._unregistered.add(name)

    @rule()
    def check_health_invariant(self) -> None:
        """Health report counters should always be consistent."""
        report = self.manager.health()
        assert report.total == len(self.manager._bricks)
        assert report.active + report.failed <= report.total
        for brick_status in report.bricks:
            assert isinstance(brick_status.state, BrickState)
            # UNMOUNTED bricks are not counted as active or failed
            if brick_status.state == BrickState.UNMOUNTED:
                assert brick_status.state not in (BrickState.ACTIVE, BrickState.FAILED)

    @rule(name=bricks)
    def check_status_invariant(self, name: str) -> None:
        """Status should be retrievable for non-unregistered bricks."""
        if name in self._unregistered:
            # UNREGISTERED bricks should not be in the dict
            assert self.manager.get_status(name) is None
        else:
            status = self.manager.get_status(name)
            assert status is not None
            assert isinstance(status.state, BrickState)


TestBrickLifecycleHypothesis = BrickLifecycleStateMachine.TestCase
TestBrickLifecycleHypothesis.settings = settings(max_examples=50, stateful_step_count=20)

# ---------------------------------------------------------------------------
# Remount tests (Issue #2363)
# ---------------------------------------------------------------------------


class TestRemount:
    """Test re-mounting from UNMOUNTED state."""

    @pytest.fixture
    def manager(self) -> BrickLifecycleManager:
        return BrickLifecycleManager()

    @pytest.mark.asyncio
    async def test_remount_lifecycle_brick(self, manager: BrickLifecycleManager) -> None:
        """UNMOUNTED → STARTING → ACTIVE."""
        brick = _make_lifecycle_brick("search")
        manager.register("search", brick, protocol_name="SearchProtocol")
        await manager.mount("search")
        await manager.unmount("search")
        _s = manager.get_status("search")
        assert _s is not None
        assert _s.state == BrickState.UNMOUNTED

        await manager.remount("search")
        _s = manager.get_status("search")
        assert _s is not None
        assert _s.state == BrickState.ACTIVE
        assert brick.start.await_count == 2  # mounted twice

    @pytest.mark.asyncio
    async def test_remount_stateless_brick(self, manager: BrickLifecycleManager) -> None:
        """Stateless brick re-mount: UNMOUNTED → ACTIVE."""
        brick = _make_stateless_brick("pay")
        manager.register("pay", brick, protocol_name="PaymentProtocol")
        await manager.mount("pay")
        await manager.unmount("pay")
        _s = manager.get_status("pay")
        assert _s is not None
        assert _s.state == BrickState.UNMOUNTED

        await manager.remount("pay")
        _s = manager.get_status("pay")
        assert _s is not None
        assert _s.state == BrickState.ACTIVE

    @pytest.mark.asyncio
    async def test_remount_from_non_unmounted_raises(self, manager: BrickLifecycleManager) -> None:
        """remount() should raise if brick is not UNMOUNTED."""
        brick = _make_lifecycle_brick("search")
        manager.register("search", brick, protocol_name="SearchProtocol")
        with pytest.raises(InvalidTransitionError):
            await manager.remount("search")  # Still REGISTERED

    @pytest.mark.asyncio
    async def test_remount_after_failed_stop(self, manager: BrickLifecycleManager) -> None:
        """If stop() fails during unmount, brick goes to FAILED, not UNMOUNTED."""
        brick = _make_lifecycle_brick("search")
        brick.stop = AsyncMock(side_effect=RuntimeError("drain error"))
        manager.register("search", brick, protocol_name="SearchProtocol")
        await manager.mount("search")
        await manager.unmount("search")
        _s = manager.get_status("search")
        assert _s is not None
        assert _s.state == BrickState.FAILED

    @pytest.mark.asyncio
    async def test_mount_from_unmounted_via_mount(self, manager: BrickLifecycleManager) -> None:
        """mount() should also work from UNMOUNTED (not just remount())."""
        brick = _make_lifecycle_brick("search")
        manager.register("search", brick, protocol_name="SearchProtocol")
        await manager.mount("search")
        await manager.unmount("search")
        _s = manager.get_status("search")
        assert _s is not None
        assert _s.state == BrickState.UNMOUNTED

        await manager.mount("search")
        _s = manager.get_status("search")
        assert _s is not None
        assert _s.state == BrickState.ACTIVE

    @pytest.mark.asyncio
    async def test_remount_with_dependencies(self, manager: BrickLifecycleManager) -> None:
        """Re-mounting a brick with dependencies should work if deps are ACTIVE."""
        brick_a = _make_lifecycle_brick("a")
        brick_b = _make_lifecycle_brick("b")
        manager.register("a", brick_a, protocol_name="AP")
        manager.register("b", brick_b, protocol_name="BP", depends_on=("a",))
        await manager.mount_all()

        # Unmount b only
        await manager.unmount("b")
        _s = manager.get_status("b")
        assert _s is not None
        assert _s.state == BrickState.UNMOUNTED

        # Remount b — a is still ACTIVE
        await manager.remount("b")
        _s = manager.get_status("b")
        assert _s is not None
        assert _s.state == BrickState.ACTIVE


# ---------------------------------------------------------------------------
# Unregister tests (Issue #2363)
# ---------------------------------------------------------------------------


class TestUnregister:
    """Test unregister lifecycle operation."""

    @pytest.fixture
    def manager(self) -> BrickLifecycleManager:
        return BrickLifecycleManager()

    @pytest.mark.asyncio
    async def test_unregister_from_unmounted(self, manager: BrickLifecycleManager) -> None:
        """UNMOUNTED → unregister → UNREGISTERED (removed from dict)."""
        brick = _make_lifecycle_brick("search")
        manager.register("search", brick, protocol_name="SearchProtocol")
        await manager.mount("search")
        await manager.unmount("search")
        _s = manager.get_status("search")
        assert _s is not None
        assert _s.state == BrickState.UNMOUNTED

        await manager.unregister("search")
        assert manager.get_status("search") is None

    @pytest.mark.asyncio
    async def test_unregister_from_non_unmounted_raises(
        self, manager: BrickLifecycleManager
    ) -> None:
        """unregister() should raise if brick is not UNMOUNTED."""
        brick = _make_lifecycle_brick("search")
        manager.register("search", brick, protocol_name="SearchProtocol")
        with pytest.raises(InvalidTransitionError):
            await manager.unregister("search")  # Still REGISTERED

    @pytest.mark.asyncio
    async def test_unregister_removes_from_dict(self, manager: BrickLifecycleManager) -> None:
        """After unregister, brick should not appear in _bricks dict."""
        brick = _make_lifecycle_brick("search")
        manager.register("search", brick, protocol_name="SearchProtocol")
        await manager.mount("search")
        await manager.unmount("search")
        await manager.unregister("search")
        assert "search" not in manager._bricks

    def test_force_unregister_bypasses_state(self, manager: BrickLifecycleManager) -> None:
        """_force_unregister() should remove regardless of state (testing only)."""
        brick = _make_lifecycle_brick("search")
        manager.register("search", brick, protocol_name="SearchProtocol")
        manager._force_unregister("search")
        assert manager.get_status("search") is None


# ---------------------------------------------------------------------------
# Shutdown tests (Issue #2363)
# ---------------------------------------------------------------------------


class TestShutdown:
    """Test shutdown_all and unregister_all."""

    @pytest.fixture
    def manager(self) -> BrickLifecycleManager:
        return BrickLifecycleManager()

    @pytest.mark.asyncio
    async def test_shutdown_all_unmounts_then_unregisters(
        self, manager: BrickLifecycleManager
    ) -> None:
        """shutdown_all chains unmount_all → unregister_all."""
        manager.register("a", _make_lifecycle_brick("a"), protocol_name="AP")
        manager.register("b", _make_lifecycle_brick("b"), protocol_name="BP")
        await manager.mount_all()

        await manager.shutdown_all()

        # All bricks should have been unregistered (removed from dict)
        assert len(manager._bricks) == 0

    @pytest.mark.asyncio
    async def test_unregister_all(self, manager: BrickLifecycleManager) -> None:
        """unregister_all removes all UNMOUNTED bricks."""
        manager.register("a", _make_lifecycle_brick("a"), protocol_name="AP")
        manager.register("b", _make_lifecycle_brick("b"), protocol_name="BP")
        await manager.mount_all()
        await manager.unmount_all()

        # Both should be UNMOUNTED now
        _s = manager.get_status("a")
        assert _s is not None
        assert _s.state == BrickState.UNMOUNTED
        _s = manager.get_status("b")
        assert _s is not None
        assert _s.state == BrickState.UNMOUNTED

        await manager.unregister_all()
        assert len(manager._bricks) == 0

    @pytest.mark.asyncio
    async def test_unmounted_at_is_set(self, manager: BrickLifecycleManager) -> None:
        """unmounted_at should be set when brick enters UNMOUNTED."""
        brick = _make_lifecycle_brick("search")
        manager.register("search", brick, protocol_name="SearchProtocol")
        await manager.mount("search")
        await manager.unmount("search")
        status = manager.get_status("search")
        assert status is not None
        assert status.unmounted_at is not None
        assert status.unmounted_at > 0


# ---------------------------------------------------------------------------
# Transition history tests
# ---------------------------------------------------------------------------


class TestTransitionHistory:
    """Test transition recording and get_transitions()."""

    @pytest.fixture
    def manager(self) -> BrickLifecycleManager:
        return BrickLifecycleManager()

    @pytest.mark.asyncio
    async def test_mount_records_transitions(self, manager: BrickLifecycleManager) -> None:
        """Mount should record REGISTERED→STARTING and STARTING→ACTIVE transitions."""
        brick = _make_lifecycle_brick("search")
        manager.register("search", brick, protocol_name="SearchProtocol")
        await manager.mount("search")

        transitions = manager.get_transitions("search")
        assert len(transitions) == 2
        # First: REGISTERED → STARTING (mount event)
        _, event0, from0, to0 = transitions[0]
        assert event0 == EVENT_MOUNT
        assert from0 == "REGISTERED"
        assert to0 == "STARTING"
        # Second: STARTING → ACTIVE (started event)
        _, event1, from1, to1 = transitions[1]
        assert event1 == EVENT_STARTED
        assert from1 == "STARTING"
        assert to1 == "ACTIVE"

    def test_cap_enforcement(self, manager: BrickLifecycleManager) -> None:
        """More than MAX_TRANSITION_HISTORY transitions should keep only the last N."""
        brick = _make_lifecycle_brick("search")
        manager.register("search", brick, protocol_name="SearchProtocol")

        # Force many transitions by alternating states
        for _ in range(MAX_TRANSITION_HISTORY + 20):
            manager._force_state("search", BrickState.REGISTERED)

        transitions = manager.get_transitions("search")
        assert len(transitions) == MAX_TRANSITION_HISTORY

    def test_get_transitions_unknown_raises(self, manager: BrickLifecycleManager) -> None:
        """get_transitions() should raise KeyError for unknown brick."""
        with pytest.raises(KeyError, match="not found"):
            manager.get_transitions("nonexistent")

    @pytest.mark.asyncio
    async def test_transitions_survive_reset(self, manager: BrickLifecycleManager) -> None:
        """Transitions should NOT be cleared on reset()."""
        brick = _make_lifecycle_brick("search")
        brick.start = AsyncMock(side_effect=[RuntimeError("fail"), None])
        manager.register("search", brick, protocol_name="SearchProtocol")

        await manager.mount("search")  # REGISTERED→STARTING→FAILED
        transitions_before = manager.get_transitions("search")
        assert len(transitions_before) > 0

        manager.reset("search")  # FAILED→REGISTERED
        transitions_after = manager.get_transitions("search")
        # Should have MORE transitions (the reset itself adds one)
        assert len(transitions_after) > len(transitions_before)

    def test_force_state_records_transition(self, manager: BrickLifecycleManager) -> None:
        """_force_state() should record a transition with event='force'."""
        brick = _make_lifecycle_brick("search")
        manager.register("search", brick, protocol_name="SearchProtocol")
        manager._force_state("search", BrickState.ACTIVE)

        transitions = manager.get_transitions("search")
        assert len(transitions) == 1
        _, event, from_state, to_state = transitions[0]
        assert event == "force"
        assert from_state == "REGISTERED"
        assert to_state == "ACTIVE"
