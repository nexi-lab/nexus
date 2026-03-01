"""Benchmark: BrickLifecycleManager at 100+ bricks scale (#2370).

Validates that:
- Registration, mount, unmount, and full lifecycle perform within budget at scale.
- Hot-swap churn doesn't leak memory or corrupt state.
- DAG topological sort scales linearly.
- Zone deprovision drain-before-finalize ordering holds under load.

Run:
    PYTHONPATH=src python -m pytest tests/benchmarks/bench_brick_lifecycle.py -v -s \
        -p no:xdist -o "addopts=" --noconftest

Uses manual ``time.perf_counter`` timing with explicit performance budgets.

Issue #2370: Load-test hot-swap brick lifecycle at 100+ bricks scale.
"""

import asyncio
import gc
import time
import tracemalloc
import weakref
from collections.abc import Callable, Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.contracts.protocols.brick_lifecycle import (
    BrickLifecycleProtocol,
    BrickState,
    ZoneState,
)
from nexus.system_services.lifecycle.brick_lifecycle import BrickLifecycleManager

# ---------------------------------------------------------------------------
# Constants / performance budgets (2x CI safety factor)
# ---------------------------------------------------------------------------

BUDGET_REGISTER_100_MS = 200  # 2 ms/brick (includes mock creation overhead)
BUDGET_MOUNT_ALL_100_MS = 500  # 5 ms/brick
BUDGET_UNMOUNT_ALL_100_MS = 500  # 5 ms/brick
BUDGET_FULL_CYCLE_100_MS = 1200  # 12 ms/brick
BUDGET_HOT_SWAP_100_CYCLES_MS = 2000  # 20 ms/cycle
BUDGET_DAG_SORT_100_MS = 20  # 0.2 ms/node
BUDGET_HEALTH_REPORT_100_MS = 10  # 0.1 ms/brick
BUDGET_ZONE_DEPROVISION_100_MS = 500  # 5 ms/brick
MEMORY_GROWTH_TOLERANCE_KB = 512  # over 500 cycles

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_lifecycle_brick(name: str = "test") -> MagicMock:
    """Create a mock brick satisfying BrickLifecycleProtocol."""
    brick = AsyncMock(spec=BrickLifecycleProtocol)
    brick.start = AsyncMock(return_value=None)
    brick.stop = AsyncMock(return_value=None)
    brick.health_check = AsyncMock(return_value=True)
    brick.__class__.__name__ = f"{name.capitalize()}Brick"
    return brick


def _make_stateless_brick(name: str = "pay") -> MagicMock:
    """Create a mock brick without lifecycle methods."""
    brick = MagicMock()
    brick.__class__.__name__ = f"{name.capitalize()}Brick"
    if hasattr(brick, "start"):
        del brick.start
    if hasattr(brick, "stop"):
        del brick.stop
    if hasattr(brick, "health_check"):
        del brick.health_check
    return brick


def _make_zone_aware_brick(name: str = "test") -> MagicMock:
    """Create a mock brick with drain/finalize for zone deprovision."""
    brick = _make_lifecycle_brick(name)
    brick.drain = AsyncMock(return_value=None)
    brick.finalize = AsyncMock(return_value=None)
    return brick


def _make_failing_brick(name: str = "failing", error: Exception | None = None) -> MagicMock:
    """Create a mock brick whose start() raises."""
    brick = _make_lifecycle_brick(name)
    brick.start = AsyncMock(side_effect=error or RuntimeError("Connection refused"))
    return brick


def _build_dag_bricks(
    manager: BrickLifecycleManager,
    count: int,
    levels: int = 1,
    *,
    brick_factory: Callable[[str], Any] = _make_lifecycle_brick,
    mix_stateless: bool = False,
) -> list[str]:
    """Register ``count`` bricks with deterministic round-robin DAG deps.

    Args:
        manager: Lifecycle manager to register bricks into.
        count: Number of bricks to register.
        levels: Number of DAG depth levels (1 = flat / no deps).
        brick_factory: Factory callable to create brick instances.
        mix_stateless: If True, every 5th brick is stateless.

    Returns:
        List of registered brick names in order.
    """
    names: list[str] = []
    per_level = max(1, count // levels)

    for i in range(count):
        name = f"brick_{i:04d}"
        brick = _make_stateless_brick(name) if mix_stateless and i % 5 == 4 else brick_factory(name)

        # Compute dependencies: bricks in later levels depend on bricks in previous levels
        deps: tuple[str, ...] = ()
        if levels > 1 and i >= per_level:
            dep_idx = i % per_level
            dep_name = f"brick_{dep_idx:04d}"
            deps = (dep_name,)

        manager.register(name, brick, protocol_name=f"Proto{i}", depends_on=deps)
        names.append(name)

    return names


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def loop() -> Generator[asyncio.AbstractEventLoop, None, None]:
    """Shared event loop for benchmark tests."""
    _loop = asyncio.new_event_loop()
    yield _loop
    _loop.close()


# ---------------------------------------------------------------------------
# 1. TestRegistrationThroughput
# ---------------------------------------------------------------------------


class TestRegistrationThroughput:
    """Benchmark brick registration at scale."""

    @pytest.mark.parametrize("n", [100, 200, 500])
    def test_register_n_bricks(self, n: int) -> None:
        """Registration of N bricks completes within budget."""
        budget_ms = BUDGET_REGISTER_100_MS * (n / 100)

        # Warmup: one throwaway cycle to pay import/JIT costs
        warmup_mgr = BrickLifecycleManager()
        _build_dag_bricks(warmup_mgr, 50, levels=1)

        t0 = time.perf_counter()
        manager = BrickLifecycleManager()
        _build_dag_bricks(manager, n, levels=1)
        elapsed_ms = (time.perf_counter() - t0) * 1000

        report = manager.health()
        assert report.total == n
        assert elapsed_ms < budget_ms, (
            f"Registration of {n} bricks took {elapsed_ms:.1f}ms (budget: {budget_ms:.0f}ms)"
        )

    def test_registration_is_not_quadratic(self) -> None:
        """Registration time scales roughly linearly, not quadratically.

        Compares t(200) vs t(100). Linear → ratio ≈2x. Quadratic → ratio ≈4x.
        We allow up to 3x to account for mock overhead variance.
        """
        # Warmup: pay import/JIT costs upfront
        warmup_mgr = BrickLifecycleManager()
        _build_dag_bricks(warmup_mgr, 100, levels=1)

        times: list[float] = []
        for n in [100, 200]:
            t0 = time.perf_counter()
            m = BrickLifecycleManager()
            _build_dag_bricks(m, n, levels=1)
            times.append(time.perf_counter() - t0)

        ratio = times[1] / max(times[0], 1e-9)
        assert ratio < 3.0, f"Registration scaling ratio {ratio:.2f}x suggests non-linear growth"


# ---------------------------------------------------------------------------
# 2. TestMountAllAtScale
# ---------------------------------------------------------------------------


class TestMountAllAtScale:
    """Benchmark mount_all with DAG ordering at scale."""

    def test_mount_all_100_dag(self, loop: asyncio.AbstractEventLoop) -> None:
        """Mount 100 bricks with 5-level DAG within budget."""
        manager = BrickLifecycleManager()
        _build_dag_bricks(manager, 100, levels=5)

        t0 = time.perf_counter()
        report = loop.run_until_complete(manager.mount_all())
        elapsed_ms = (time.perf_counter() - t0) * 1000

        assert report.active == 100
        assert report.failed == 0
        assert elapsed_ms < BUDGET_MOUNT_ALL_100_MS, (
            f"mount_all(100) took {elapsed_ms:.1f}ms (budget: {BUDGET_MOUNT_ALL_100_MS}ms)"
        )

    def test_mount_all_200_dag(self, loop: asyncio.AbstractEventLoop) -> None:
        """Mount 200 bricks with 10-level DAG within budget."""
        budget_ms = BUDGET_MOUNT_ALL_100_MS * 2
        manager = BrickLifecycleManager()
        _build_dag_bricks(manager, 200, levels=10)

        t0 = time.perf_counter()
        report = loop.run_until_complete(manager.mount_all())
        elapsed_ms = (time.perf_counter() - t0) * 1000

        assert report.active == 200
        assert elapsed_ms < budget_ms

    def test_mount_all_fan_in_100(self, loop: asyncio.AbstractEventLoop) -> None:
        """100:1 fan-in: 100 bricks all depend on 1 root brick."""
        manager = BrickLifecycleManager()

        # Register root brick
        root = _make_lifecycle_brick("root")
        manager.register("root", root, protocol_name="Root")

        # Register 100 bricks that all depend on root
        for i in range(100):
            name = f"leaf_{i:04d}"
            brick = _make_lifecycle_brick(name)
            manager.register(name, brick, protocol_name=f"Leaf{i}", depends_on=("root",))

        t0 = time.perf_counter()
        report = loop.run_until_complete(manager.mount_all())
        elapsed_ms = (time.perf_counter() - t0) * 1000

        assert report.active == 101  # root + 100 leaves
        assert report.failed == 0
        assert elapsed_ms < BUDGET_MOUNT_ALL_100_MS


# ---------------------------------------------------------------------------
# 3. TestUnmountAllAtScale
# ---------------------------------------------------------------------------


class TestUnmountAllAtScale:
    """Benchmark unmount_all at scale."""

    def test_unmount_all_100_repeated(self, loop: asyncio.AbstractEventLoop) -> None:
        """Mount + unmount 100 bricks, repeated 3 times for consistency."""
        budget_ms = BUDGET_UNMOUNT_ALL_100_MS
        timings: list[float] = []

        for _ in range(3):
            manager = BrickLifecycleManager()
            _build_dag_bricks(manager, 100, levels=5)
            loop.run_until_complete(manager.mount_all())

            t0 = time.perf_counter()
            loop.run_until_complete(manager.unmount_all())
            timings.append((time.perf_counter() - t0) * 1000)

        avg_ms = sum(timings) / len(timings)
        assert avg_ms < budget_ms, f"unmount_all(100) avg {avg_ms:.1f}ms (budget: {budget_ms}ms)"


# ---------------------------------------------------------------------------
# 4. TestFullLifecycleCycle
# ---------------------------------------------------------------------------


class TestFullLifecycleCycle:
    """Benchmark full lifecycle: register → mount → unmount → unregister."""

    def test_full_cycle_100(self, loop: asyncio.AbstractEventLoop) -> None:
        """Full lifecycle for 100 bricks within budget."""
        t0 = time.perf_counter()

        manager = BrickLifecycleManager()
        names = _build_dag_bricks(manager, 100, levels=5, mix_stateless=True)

        async def _run() -> None:
            await manager.mount_all()
            await manager.unmount_all()
            for name in reversed(names):
                await manager.unregister(name)

        loop.run_until_complete(_run())

        elapsed_ms = (time.perf_counter() - t0) * 1000

        assert len(manager._bricks) == 0  # noqa: SLF001
        assert elapsed_ms < BUDGET_FULL_CYCLE_100_MS, (
            f"Full cycle (100) took {elapsed_ms:.1f}ms (budget: {BUDGET_FULL_CYCLE_100_MS}ms)"
        )


# ---------------------------------------------------------------------------
# 5. TestMountAllWithFailures
# ---------------------------------------------------------------------------


class TestMountAllWithFailures:
    """Benchmark mount_all with 10% failure rate (fail-forward)."""

    def test_mount_all_10pct_failures(self, loop: asyncio.AbstractEventLoop) -> None:
        """10% of bricks fail to start; rest proceed (fail-forward)."""
        manager = BrickLifecycleManager()

        total = 100
        fail_count = total // 10  # 10 failing bricks

        for i in range(total):
            name = f"brick_{i:04d}"
            brick = _make_failing_brick(name) if i < fail_count else _make_lifecycle_brick(name)
            manager.register(name, brick, protocol_name=f"Proto{i}")

        t0 = time.perf_counter()
        report = loop.run_until_complete(manager.mount_all())
        elapsed_ms = (time.perf_counter() - t0) * 1000

        assert report.failed == fail_count
        assert report.active == total - fail_count
        assert elapsed_ms < BUDGET_MOUNT_ALL_100_MS, (
            f"mount_all with 10% failures took {elapsed_ms:.1f}ms"
        )


# ---------------------------------------------------------------------------
# 6. TestHotSwapChurn
# ---------------------------------------------------------------------------


class TestHotSwapChurn:
    """Benchmark hot-swap: register → mount → unmount → unregister in batches."""

    def test_hot_swap_concurrent_100_cycles(self, loop: asyncio.AbstractEventLoop) -> None:
        """100 hot-swap cycles in batches of 5, error rate < 5%, strict invariants."""
        manager = BrickLifecycleManager()
        total_cycles = 100
        batch_size = 5
        errors = 0

        async def _hot_swap_one(cycle: int) -> bool:
            """Run a single hot-swap cycle. Returns True on success."""
            name = f"hot_{cycle:04d}"
            try:
                brick = _make_lifecycle_brick(name)
                manager.register(name, brick, protocol_name=f"HotProto{cycle}")
                await manager.mount(name)
                status = manager.get_status(name)
                if status is None or status.state != BrickState.ACTIVE:
                    return False
                await manager.unmount(name)
                await manager.unregister(name)
                return True
            except Exception:
                return False

        async def _run_all() -> int:
            err = 0
            for batch_start in range(0, total_cycles, batch_size):
                batch_end = min(batch_start + batch_size, total_cycles)
                results = await asyncio.gather(
                    *(_hot_swap_one(i) for i in range(batch_start, batch_end)),
                    return_exceptions=True,
                )
                for r in results:
                    if isinstance(r, BaseException) or r is not True:
                        err += 1
            return err

        t0 = time.perf_counter()
        errors = loop.run_until_complete(_run_all())
        elapsed_ms = (time.perf_counter() - t0) * 1000

        error_rate = errors / total_cycles
        assert error_rate < 0.05, f"Error rate {error_rate:.1%} exceeds 5% threshold"
        assert elapsed_ms < BUDGET_HOT_SWAP_100_CYCLES_MS, (
            f"Hot-swap 100 cycles took {elapsed_ms:.1f}ms (budget: {BUDGET_HOT_SWAP_100_CYCLES_MS}ms)"
        )

        # Strict invariant: no bricks left registered after all cycles
        report = manager.health()
        assert report.total == 0, f"Leaked {report.total} bricks after hot-swap churn"


# ---------------------------------------------------------------------------
# 7. TestMemoryStability
# ---------------------------------------------------------------------------


class TestMemoryStability:
    """Detect memory leaks via tracemalloc + weakref after 500 hot-swap cycles."""

    def test_no_memory_leak_500_cycles(self, loop: asyncio.AbstractEventLoop) -> None:
        """500 register/mount/unmount/unregister cycles should not leak memory."""
        manager = BrickLifecycleManager()
        weak_refs: list[weakref.ref[Any]] = []
        warmup_cycles = 10
        measure_cycles = 500

        async def _cycle(idx: int, *, collect_refs: bool = False) -> None:
            name = f"mem_{idx:04d}"
            brick = _make_lifecycle_brick(name)
            if collect_refs:
                weak_refs.append(weakref.ref(brick))
            manager.register(name, brick, protocol_name=f"MemProto{idx}")
            await manager.mount(name)
            await manager.unmount(name)
            await manager.unregister(name)

        async def _run() -> tuple[int, int]:
            # Warmup phase — don't measure
            for i in range(warmup_cycles):
                await _cycle(i)

            # Force GC before snapshot
            gc.collect()
            tracemalloc.start()
            snap_before = tracemalloc.take_snapshot()

            # Measurement phase
            for i in range(warmup_cycles, warmup_cycles + measure_cycles):
                await _cycle(i, collect_refs=True)

            gc.collect()
            snap_after = tracemalloc.take_snapshot()
            tracemalloc.stop()

            # Compute memory growth
            stats = snap_after.compare_to(snap_before, "lineno")
            growth_bytes = sum(s.size_diff for s in stats if s.size_diff > 0)
            growth_kb = growth_bytes / 1024

            return int(growth_kb), len([r for r in weak_refs if r() is not None])

        growth_kb, alive_refs = loop.run_until_complete(_run())

        # All brick references should be dead
        gc.collect()
        alive_after_gc = sum(1 for r in weak_refs if r() is not None)
        assert alive_after_gc == 0, f"{alive_after_gc} brick refs still alive after GC (expected 0)"

        # Memory growth within tolerance
        assert growth_kb < MEMORY_GROWTH_TOLERANCE_KB, (
            f"Memory grew {growth_kb}KB over {measure_cycles} cycles "
            f"(tolerance: {MEMORY_GROWTH_TOLERANCE_KB}KB)"
        )

        # Manager should be empty
        assert manager.health().total == 0


# ---------------------------------------------------------------------------
# 8. TestDAGOrderingAtScale
# ---------------------------------------------------------------------------


class TestDAGOrderingAtScale:
    """Benchmark DAG topological sort speed."""

    @pytest.mark.parametrize("n", [100, 200, 500])
    def test_dag_sort_speed(self, n: int) -> None:
        """compute_startup_order for N bricks with DAG deps is within budget."""
        budget_ms = BUDGET_DAG_SORT_100_MS * (n / 100)
        manager = BrickLifecycleManager()
        _build_dag_bricks(manager, n, levels=5)

        # Warm up
        manager.compute_startup_order()

        t0 = time.perf_counter()
        levels = manager.compute_startup_order()
        elapsed_ms = (time.perf_counter() - t0) * 1000

        total_bricks = sum(len(level) for level in levels)
        assert total_bricks == n
        assert elapsed_ms < budget_ms, (
            f"DAG sort({n}) took {elapsed_ms:.1f}ms (budget: {budget_ms:.0f}ms)"
        )

    def test_shutdown_order_reversed(self, loop: asyncio.AbstractEventLoop) -> None:
        """Shutdown order is exact reverse of startup order."""
        manager = BrickLifecycleManager()
        _build_dag_bricks(manager, 100, levels=5)

        startup = manager.compute_startup_order()
        shutdown = manager.compute_shutdown_order()

        assert shutdown == list(reversed(startup))


# ---------------------------------------------------------------------------
# 9. TestHealthReportAtScale
# ---------------------------------------------------------------------------


class TestHealthReportAtScale:
    """Benchmark health report generation at scale."""

    @pytest.mark.parametrize("n", [100, 500])
    def test_health_report(self, loop: asyncio.AbstractEventLoop, n: int) -> None:
        """health() for N bricks within budget."""
        budget_ms = BUDGET_HEALTH_REPORT_100_MS * (n / 100)
        manager = BrickLifecycleManager()
        _build_dag_bricks(manager, n, levels=1)
        loop.run_until_complete(manager.mount_all())

        t0 = time.perf_counter()
        report = manager.health()
        elapsed_ms = (time.perf_counter() - t0) * 1000

        assert report.total == n
        assert report.active == n
        assert elapsed_ms < budget_ms, (
            f"health({n}) took {elapsed_ms:.1f}ms (budget: {budget_ms:.0f}ms)"
        )


# ---------------------------------------------------------------------------
# 10. TestZoneDeprovisionAtScale
# ---------------------------------------------------------------------------


class TestZoneDeprovisionAtScale:
    """Benchmark zone deprovision with drain-before-finalize ordering."""

    def test_deprovision_100_zone_bricks(self, loop: asyncio.AbstractEventLoop) -> None:
        """Deprovision 100 zone-aware bricks within budget."""
        manager = BrickLifecycleManager()
        _build_dag_bricks(manager, 100, levels=1, brick_factory=_make_zone_aware_brick)
        loop.run_until_complete(manager.mount_all())

        t0 = time.perf_counter()
        report = loop.run_until_complete(manager.deprovision_zone("zone-1", grace_period=30.0))
        elapsed_ms = (time.perf_counter() - t0) * 1000

        assert report.zone_state == ZoneState.DESTROYED
        assert report.bricks_drained == 100
        assert report.bricks_finalized == 100
        assert report.drain_errors == 0
        assert report.finalize_errors == 0
        assert not report.forced
        assert elapsed_ms < BUDGET_ZONE_DEPROVISION_100_MS

    def test_deprovision_200_zone_dag(self, loop: asyncio.AbstractEventLoop) -> None:
        """Deprovision 200 zone-aware bricks with 10-level DAG, drain-before-finalize."""
        budget_ms = BUDGET_ZONE_DEPROVISION_100_MS * 2
        manager = BrickLifecycleManager()
        names = _build_dag_bricks(manager, 200, levels=10, brick_factory=_make_zone_aware_brick)
        loop.run_until_complete(manager.mount_all())

        # Track drain/finalize timestamps to verify ordering
        drain_times: dict[str, float] = {}
        finalize_times: dict[str, float] = {}

        for name in names:
            entry = manager._bricks[name]  # noqa: SLF001

            async def _drain_with_ts(zone_id: str, _name: str = name) -> None:
                drain_times[_name] = time.monotonic()

            async def _finalize_with_ts(zone_id: str, _name: str = name) -> None:
                finalize_times[_name] = time.monotonic()

            entry.instance.drain = AsyncMock(side_effect=_drain_with_ts)
            entry.instance.finalize = AsyncMock(side_effect=_finalize_with_ts)

        t0 = time.perf_counter()
        report = loop.run_until_complete(manager.deprovision_zone("zone-2", grace_period=30.0))
        elapsed_ms = (time.perf_counter() - t0) * 1000

        assert report.zone_state == ZoneState.DESTROYED
        assert not report.forced
        assert elapsed_ms < budget_ms

        # Verify drain-before-finalize for every brick that has both timestamps
        for name in names:
            if name in drain_times and name in finalize_times:
                assert drain_times[name] <= finalize_times[name], (
                    f"Brick {name}: drain ({drain_times[name]:.6f}) "
                    f"happened after finalize ({finalize_times[name]:.6f})"
                )
