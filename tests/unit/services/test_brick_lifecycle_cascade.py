"""Tests for cascade unmount, dependency edges, and reverse-edge index (Issue #2991).

Covers:
    - Cascade unmount: unmounting A cascades to dependents B, C
    - Reverse-edge index maintenance on register/unregister
    - get_dependents() and get_all_dependents()
    - DAG-ordered cascade in diamond/chain topologies
    - Hypothesis property-based DAG test
"""

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from hypothesis import settings
from hypothesis.stateful import Bundle, RuleBasedStateMachine, initialize, rule

from nexus.services.protocols.brick_lifecycle import (
    BrickState,
)
from nexus.system_services.lifecycle.brick_lifecycle import (
    BrickLifecycleManager,
)
from tests.unit.services.conftest import (
    make_lifecycle_brick as _make_lifecycle_brick,
)
from tests.unit.services.conftest import (
    make_stateless_brick as _make_stateless_brick,
)

# ---------------------------------------------------------------------------
# Reverse-edge index tests
# ---------------------------------------------------------------------------


class TestReverseEdgeIndex:
    """Test _depended_by index maintenance."""

    @pytest.fixture
    def manager(self) -> BrickLifecycleManager:
        return BrickLifecycleManager()

    def test_register_populates_depended_by(self, manager: BrickLifecycleManager) -> None:
        """Registering B with depends_on=(A,) adds B to A's depended_by set."""
        manager.register("a", _make_lifecycle_brick("a"), protocol_name="AP")
        manager.register("b", _make_lifecycle_brick("b"), protocol_name="BP", depends_on=("a",))
        assert manager.get_dependents("a") == {"b"}
        assert manager.get_dependents("b") == set()

    def test_multiple_dependents(self, manager: BrickLifecycleManager) -> None:
        """Multiple bricks depending on the same brick."""
        manager.register("a", _make_lifecycle_brick("a"), protocol_name="AP")
        manager.register("b", _make_lifecycle_brick("b"), protocol_name="BP", depends_on=("a",))
        manager.register("c", _make_lifecycle_brick("c"), protocol_name="CP", depends_on=("a",))
        assert manager.get_dependents("a") == {"b", "c"}

    def test_transitive_dependents(self, manager: BrickLifecycleManager) -> None:
        """get_all_dependents returns transitive closure."""
        manager.register("a", _make_lifecycle_brick("a"), protocol_name="AP")
        manager.register("b", _make_lifecycle_brick("b"), protocol_name="BP", depends_on=("a",))
        manager.register("c", _make_lifecycle_brick("c"), protocol_name="CP", depends_on=("b",))
        # a -> b -> c
        assert manager.get_all_dependents("a") == {"b", "c"}
        assert manager.get_all_dependents("b") == {"c"}
        assert manager.get_all_dependents("c") == set()

    def test_diamond_dependents(self, manager: BrickLifecycleManager) -> None:
        """Diamond: A←B, A←C, B←D, C←D."""
        manager.register("a", _make_lifecycle_brick("a"), protocol_name="AP")
        manager.register("b", _make_lifecycle_brick("b"), protocol_name="BP", depends_on=("a",))
        manager.register("c", _make_lifecycle_brick("c"), protocol_name="CP", depends_on=("a",))
        manager.register("d", _make_lifecycle_brick("d"), protocol_name="DP", depends_on=("b", "c"))
        assert manager.get_dependents("a") == {"b", "c"}
        assert manager.get_all_dependents("a") == {"b", "c", "d"}

    @pytest.mark.asyncio
    async def test_unregister_cleans_depended_by(self, manager: BrickLifecycleManager) -> None:
        """Unregistering a brick removes it from _depended_by."""
        manager.register("a", _make_lifecycle_brick("a"), protocol_name="AP")
        manager.register("b", _make_lifecycle_brick("b"), protocol_name="BP", depends_on=("a",))
        assert manager.get_dependents("a") == {"b"}

        await manager.mount("b")
        await manager.unmount("b")
        await manager.unregister("b")
        assert manager.get_dependents("a") == set()

    def test_force_unregister_cleans_depended_by(self, manager: BrickLifecycleManager) -> None:
        """_force_unregister also cleans reverse-edge index."""
        manager.register("a", _make_lifecycle_brick("a"), protocol_name="AP")
        manager.register("b", _make_lifecycle_brick("b"), protocol_name="BP", depends_on=("a",))
        assert manager.get_dependents("a") == {"b"}

        manager._force_unregister("b")
        assert manager.get_dependents("a") == set()

    def test_get_dependents_nonexistent(self, manager: BrickLifecycleManager) -> None:
        """get_dependents for unknown brick returns empty set."""
        assert manager.get_dependents("nonexistent") == set()

    def test_get_all_dependents_nonexistent(self, manager: BrickLifecycleManager) -> None:
        """get_all_dependents for unknown brick returns empty set."""
        assert manager.get_all_dependents("nonexistent") == set()


# ---------------------------------------------------------------------------
# Cascade unmount tests (Issue 9A)
# ---------------------------------------------------------------------------


class TestCascadeUnmount:
    """Test that unmounting a brick cascades to its active dependents."""

    @pytest.fixture
    def manager(self) -> BrickLifecycleManager:
        return BrickLifecycleManager()

    @pytest.mark.asyncio
    async def test_unmount_cascades_to_single_dependent(
        self, manager: BrickLifecycleManager
    ) -> None:
        """Unmounting A should also unmount B (which depends on A)."""
        brick_a = _make_lifecycle_brick("a")
        brick_b = _make_lifecycle_brick("b")
        manager.register("a", brick_a, protocol_name="AP")
        manager.register("b", brick_b, protocol_name="BP", depends_on=("a",))
        await manager.mount_all()
        assert manager.get_status("a").state == BrickState.ACTIVE
        assert manager.get_status("b").state == BrickState.ACTIVE

        await manager.unmount("a")

        assert manager.get_status("a").state == BrickState.UNMOUNTED
        assert manager.get_status("b").state == BrickState.UNMOUNTED
        brick_b.stop.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unmount_cascades_to_chain(self, manager: BrickLifecycleManager) -> None:
        """Unmounting A should cascade through A→B→C."""
        brick_a = _make_lifecycle_brick("a")
        brick_b = _make_lifecycle_brick("b")
        brick_c = _make_lifecycle_brick("c")
        manager.register("a", brick_a, protocol_name="AP")
        manager.register("b", brick_b, protocol_name="BP", depends_on=("a",))
        manager.register("c", brick_c, protocol_name="CP", depends_on=("b",))
        await manager.mount_all()

        await manager.unmount("a")

        assert manager.get_status("a").state == BrickState.UNMOUNTED
        assert manager.get_status("b").state == BrickState.UNMOUNTED
        assert manager.get_status("c").state == BrickState.UNMOUNTED

    @pytest.mark.asyncio
    async def test_unmount_cascade_respects_dag_order(self, manager: BrickLifecycleManager) -> None:
        """Dependents should be stopped deepest-first (C before B)."""
        order: list[str] = []

        def _make_tracked(name: str) -> MagicMock:
            brick = _make_lifecycle_brick(name)

            async def _track_stop() -> None:
                order.append(name)

            brick.stop = AsyncMock(side_effect=_track_stop)
            return brick

        brick_a = _make_tracked("a")
        brick_b = _make_tracked("b")
        brick_c = _make_tracked("c")
        manager.register("a", brick_a, protocol_name="AP")
        manager.register("b", brick_b, protocol_name="BP", depends_on=("a",))
        manager.register("c", brick_c, protocol_name="CP", depends_on=("b",))
        await manager.mount_all()

        await manager.unmount("a")

        # C should stop before B, B before A (reverse DAG)
        assert order.index("c") < order.index("b")
        assert order.index("b") < order.index("a")

    @pytest.mark.asyncio
    async def test_unmount_cascade_diamond(self, manager: BrickLifecycleManager) -> None:
        """Diamond: unmounting A cascades to B, C, D."""
        manager.register("a", _make_lifecycle_brick("a"), protocol_name="AP")
        manager.register("b", _make_lifecycle_brick("b"), protocol_name="BP", depends_on=("a",))
        manager.register("c", _make_lifecycle_brick("c"), protocol_name="CP", depends_on=("a",))
        manager.register("d", _make_lifecycle_brick("d"), protocol_name="DP", depends_on=("b", "c"))
        await manager.mount_all()

        await manager.unmount("a")

        for name in ("a", "b", "c", "d"):
            assert manager.get_status(name).state == BrickState.UNMOUNTED

    @pytest.mark.asyncio
    async def test_unmount_no_cascade_when_no_dependents(
        self, manager: BrickLifecycleManager
    ) -> None:
        """Unmounting a leaf brick should not affect others."""
        manager.register("a", _make_lifecycle_brick("a"), protocol_name="AP")
        manager.register("b", _make_lifecycle_brick("b"), protocol_name="BP", depends_on=("a",))
        await manager.mount_all()

        await manager.unmount("b")

        assert manager.get_status("a").state == BrickState.ACTIVE
        assert manager.get_status("b").state == BrickState.UNMOUNTED

    @pytest.mark.asyncio
    async def test_unmount_cascade_skips_already_unmounted(
        self, manager: BrickLifecycleManager
    ) -> None:
        """If a dependent is already UNMOUNTED, skip it during cascade."""
        manager.register("a", _make_lifecycle_brick("a"), protocol_name="AP")
        manager.register("b", _make_lifecycle_brick("b"), protocol_name="BP", depends_on=("a",))
        manager.register("c", _make_lifecycle_brick("c"), protocol_name="CP", depends_on=("a",))
        await manager.mount_all()

        # Manually unmount C first
        await manager.unmount("c")
        assert manager.get_status("c").state == BrickState.UNMOUNTED

        # Now unmount A — should cascade to B but skip C
        await manager.unmount("a")
        assert manager.get_status("a").state == BrickState.UNMOUNTED
        assert manager.get_status("b").state == BrickState.UNMOUNTED
        assert manager.get_status("c").state == BrickState.UNMOUNTED

    @pytest.mark.asyncio
    async def test_unmount_cascade_with_stateless_brick(
        self, manager: BrickLifecycleManager
    ) -> None:
        """Cascade should work with stateless bricks (no stop() call needed)."""
        manager.register("a", _make_lifecycle_brick("a"), protocol_name="AP")
        manager.register("b", _make_stateless_brick("b"), protocol_name="BP", depends_on=("a",))
        await manager.mount_all()

        await manager.unmount("a")

        assert manager.get_status("a").state == BrickState.UNMOUNTED
        assert manager.get_status("b").state == BrickState.UNMOUNTED

    @pytest.mark.asyncio
    async def test_unmount_cascade_partial_failure(self, manager: BrickLifecycleManager) -> None:
        """If a dependent's stop() fails, the cascade continues."""
        brick_a = _make_lifecycle_brick("a")
        brick_b = _make_lifecycle_brick("b")
        brick_b.stop = AsyncMock(side_effect=RuntimeError("stop failed"))
        brick_c = _make_lifecycle_brick("c")
        manager.register("a", brick_a, protocol_name="AP")
        manager.register("b", brick_b, protocol_name="BP", depends_on=("a",))
        manager.register("c", brick_c, protocol_name="CP", depends_on=("a",))
        await manager.mount_all()

        await manager.unmount("a")

        # B goes to FAILED (stop raised), C and A go to UNMOUNTED
        assert manager.get_status("b").state == BrickState.FAILED
        assert manager.get_status("c").state == BrickState.UNMOUNTED
        assert manager.get_status("a").state == BrickState.UNMOUNTED

    @pytest.mark.asyncio
    async def test_wide_fanout_cascade(self, manager: BrickLifecycleManager) -> None:
        """Root brick with 5 direct dependents — all should cascade."""
        manager.register("root", _make_lifecycle_brick("root"), protocol_name="RP")
        for i in range(5):
            name = f"leaf_{i}"
            manager.register(
                name, _make_lifecycle_brick(name), protocol_name=f"LP{i}", depends_on=("root",)
            )
        await manager.mount_all()

        await manager.unmount("root")

        assert manager.get_status("root").state == BrickState.UNMOUNTED
        for i in range(5):
            assert manager.get_status(f"leaf_{i}").state == BrickState.UNMOUNTED


# ---------------------------------------------------------------------------
# Dependency edge verification for factory bricks (Issue 10A)
# ---------------------------------------------------------------------------


class TestFactoryDependencyEdges:
    """Verify that declared depends_on edges in _FACTORY_BRICKS match reality."""

    def test_delegation_depends_on_reputation(self) -> None:
        """DelegationService constructor requires reputation_service."""
        from nexus.factory._helpers import _FACTORY_BRICKS

        edges = {name: deps for name, _, deps in _FACTORY_BRICKS}
        assert "reputation_service" in edges.get("delegation_service", ())

    def test_ipc_depends_on_event_bus(self) -> None:
        """IPC brick depends on event_bus for inter-process messaging."""
        from nexus.factory._helpers import _FACTORY_BRICKS

        edges = {name: deps for name, _, deps in _FACTORY_BRICKS}
        assert "event_bus" in edges.get("ipc_vfs_driver", ())

    def test_no_cycles_in_factory_bricks(self) -> None:
        """The declared dependency graph must be acyclic."""
        from nexus.factory._helpers import _FACTORY_BRICKS

        manager = BrickLifecycleManager()
        for name, protocol, depends_on in _FACTORY_BRICKS:
            manager.register(
                name, _make_stateless_brick(name), protocol_name=protocol, depends_on=depends_on
            )
        # Also register workflow_engine
        manager.register(
            "workflow_engine",
            _make_stateless_brick("workflow"),
            protocol_name="WorkflowProtocol",
            depends_on=("event_bus",),
        )
        # Should not raise CyclicDependencyError
        levels = manager.compute_startup_order()
        assert len(levels) > 0

    def test_all_dependencies_exist_in_factory_bricks(self) -> None:
        """Every depends_on target must itself be in _FACTORY_BRICKS or workflow_engine."""
        from nexus.factory._helpers import _FACTORY_BRICKS

        all_names = {name for name, _, _ in _FACTORY_BRICKS}
        all_names.add("workflow_engine")
        for name, _, depends_on in _FACTORY_BRICKS:
            for dep in depends_on:
                assert dep in all_names, (
                    f"Brick {name!r} depends on {dep!r} which is not in _FACTORY_BRICKS"
                )


# ---------------------------------------------------------------------------
# Extended CI guard test (Issue 11A)
# ---------------------------------------------------------------------------


class TestGlobalRegistrationGuard:
    """Verify all manager.register() calls across the codebase are accounted for."""

    def test_all_register_calls_are_known(self) -> None:
        """Every brick registered via manager.register() must be in a known list."""
        from nexus.factory._helpers import _FACTORY_BRICKS, _LATE_BRICKS

        factory_names = {name for name, _, _ in _FACTORY_BRICKS}
        factory_names.add("workflow_engine")  # special-cased
        late_names = {name for name, _, _ in _LATE_BRICKS}
        lifespan_names = {"search"}  # registered in lifespan/search.py

        all_known = factory_names | late_names | lifespan_names

        # These are ALL the brick names that should be registered with lifecycle manager
        expected_minimum = {
            # Infrastructure
            "event_bus",
            "lock_manager",
            # Core bricks
            "manifest_resolver",
            "manifest_metrics",
            "chunked_upload_service",
            "snapshot_service",
            "task_queue_service",
            "ipc_vfs_driver",
            "ipc_storage_driver",
            "ipc_provisioner",
            "wallet_provisioner",
            "delegation_service",
            "reputation_service",
            "version_service",
            "workflow_engine",
            # Middleware & tools
            "api_key_creator",
            "tool_namespace_middleware",
            # Observability & resilience
            "agent_event_log",
            "rebac_circuit_breaker",
            # Memory
            "memory_permission",
            # Governance
            "governance_anomaly_service",
            "governance_collusion_service",
            "governance_graph_service",
            "governance_response_service",
            # Search
            "zoekt_pipe_consumer",
            # Late bricks (create_nexus_fs)
            "parsers",
            "cache",
            # Lifespan
            "search",
        }

        assert expected_minimum.issubset(all_known), (
            f"Missing from known registration lists: {expected_minimum - all_known}"
        )


# ---------------------------------------------------------------------------
# Hypothesis property-based DAG test (Issue 12A)
# ---------------------------------------------------------------------------


class BrickDAGStateMachine(RuleBasedStateMachine):
    """Property-based test: random DAG operations maintain invariants.

    Invariant: No brick should be ACTIVE while any of its dependencies
    is not ACTIVE (after cascade unmount implementation).
    """

    bricks = Bundle("bricks")

    def __init__(self) -> None:
        super().__init__()
        self._loop = asyncio.new_event_loop()
        self.manager = BrickLifecycleManager()
        self._names: list[str] = []
        self._counter = 0

    def teardown(self) -> None:
        self._loop.close()

    def _run(self, coro: Any) -> Any:  # noqa: ANN401
        return self._loop.run_until_complete(coro)

    @initialize(target=bricks)
    def init_root(self) -> str:
        name = f"b_{self._counter}"
        self._counter += 1
        manager = self.manager
        manager.register(name, _make_lifecycle_brick(name), protocol_name=f"{name}P")
        self._names.append(name)
        return name

    @rule(target=bricks, parent=bricks)
    def add_dependent(self, parent: str) -> str:
        """Add a new brick that depends on an existing one."""
        name = f"b_{self._counter}"
        self._counter += 1
        # Only add dependency if parent still exists
        parent_status = self.manager.get_status(parent)
        if parent_status is not None:
            self.manager.register(
                name,
                _make_lifecycle_brick(name),
                protocol_name=f"{name}P",
                depends_on=(parent,),
            )
        else:
            self.manager.register(name, _make_lifecycle_brick(name), protocol_name=f"{name}P")
        self._names.append(name)
        return name

    @rule(target=bricks)
    def add_independent(self) -> str:
        """Add a new brick with no dependencies."""
        name = f"b_{self._counter}"
        self._counter += 1
        self.manager.register(name, _make_lifecycle_brick(name), protocol_name=f"{name}P")
        self._names.append(name)
        return name

    @rule(name=bricks)
    def mount_brick(self, name: str) -> None:
        status = self.manager.get_status(name)
        if (
            status is not None
            and status.state in (BrickState.REGISTERED, BrickState.UNMOUNTED)
            and self.manager._deps_satisfied(name)
        ):
            self._run(self.manager.mount(name))

    @rule(name=bricks)
    def unmount_brick(self, name: str) -> None:
        status = self.manager.get_status(name)
        if status is not None and status.state == BrickState.ACTIVE:
            self._run(self.manager.unmount(name))

    @rule()
    def mount_all(self) -> None:
        self._run(self.manager.mount_all())

    @rule()
    def check_dag_invariant(self) -> None:
        """INVARIANT: No ACTIVE brick should have a non-ACTIVE dependency."""
        for name, entry in self.manager._bricks.items():
            if entry.state == BrickState.ACTIVE:
                for dep_name in entry.depends_on:
                    dep = self.manager._bricks.get(dep_name)
                    if dep is not None:
                        assert dep.state == BrickState.ACTIVE, (
                            f"Brick {name!r} is ACTIVE but its dependency "
                            f"{dep_name!r} is {dep.state.name}"
                        )


TestBrickDAGHypothesis = BrickDAGStateMachine.TestCase
TestBrickDAGHypothesis.settings = settings(max_examples=50, stateful_step_count=20)
