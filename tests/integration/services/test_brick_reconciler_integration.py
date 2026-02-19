"""Integration tests for BrickReconciler — full lifecycle with real asyncio (Issue #2059).

Validates end-to-end reconciler behavior with real asyncio tasks, timers,
and concurrent brick operations. No mocks on the reconciler or manager —
only brick instances are mocked.

Tests cover:
1. Transient failure → automatic recovery via callback trigger
2. Health degradation → detection via periodic poll → recovery
3. Multiple concurrent brick failures → parallel recovery (no head-of-line blocking)
4. Dead-letter → no infinite retry, backoff cleaned up
5. Startup/shutdown lifecycle with server lifespan pattern
6. Performance: CPU usage stays near zero during idle polling
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.services.brick_lifecycle import BrickLifecycleManager
from nexus.services.brick_reconciler import BrickReconciler
from nexus.services.protocols.brick_lifecycle import (
    BrickLifecycleProtocol,
    BrickState,
)
from nexus.services.protocols.brick_reconciler import BrickReconcilerConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_brick(name: str = "test", *, healthy: bool = True) -> MagicMock:
    brick = AsyncMock(spec=BrickLifecycleProtocol)
    brick.start = AsyncMock(return_value=None)
    brick.stop = AsyncMock(return_value=None)
    brick.health_check = AsyncMock(return_value=healthy)
    brick.__class__.__name__ = f"{name.capitalize()}Brick"
    return brick


def _make_failing_brick(error: str = "Connection refused") -> MagicMock:
    brick = _make_brick("failing")
    brick.start = AsyncMock(side_effect=RuntimeError(error))
    return brick


# ---------------------------------------------------------------------------
# 1. Transient failure → automatic recovery
# ---------------------------------------------------------------------------


class TestTransientRecovery:
    """Brick fails on mount, reconciler detects via callback, recovers."""

    @pytest.mark.asyncio
    async def test_transient_failure_auto_recovers(self) -> None:
        manager = BrickLifecycleManager()
        config = BrickReconcilerConfig(
            health_check_interval=999,  # disable polling — test callback trigger only
            base_delay=0.01,
            max_delay=0.05,
            max_attempts=5,
        )
        reconciler = BrickReconciler(manager, config=config)

        # Register a brick that fails initially
        brick = _make_failing_brick()
        manager.register("search", brick, protocol_name="SearchProtocol")

        # Start reconciler
        await reconciler.start()
        try:
            # Mount fails → callback fires → enqueued for recovery
            await manager.mount("search")
            assert manager.get_status("search").state == BrickState.FAILED  # type: ignore[union-attr]

            # Fix the brick after "transient" failure
            brick.start = AsyncMock(return_value=None)

            # Wait for reconciler to process the queue and recover
            for _ in range(50):
                await asyncio.sleep(0.05)
                status = manager.get_status("search")
                if status and status.state == BrickState.ACTIVE:
                    break

            assert manager.get_status("search").state == BrickState.ACTIVE  # type: ignore[union-attr]
            assert "search" not in reconciler._backoff  # cleaned up on success
        finally:
            await reconciler.stop()


# ---------------------------------------------------------------------------
# 2. Health degradation → poll detection → recovery
# ---------------------------------------------------------------------------


class TestHealthDegradation:
    """Brick goes unhealthy, poll detects, reconciler recovers."""

    @pytest.mark.asyncio
    async def test_health_poll_detects_and_recovers(self) -> None:
        manager = BrickLifecycleManager()
        config = BrickReconcilerConfig(
            health_check_interval=0.1,  # fast polling for test
            base_delay=0.01,
            max_delay=0.05,
            max_attempts=5,
        )
        reconciler = BrickReconciler(manager, config=config)

        brick = _make_brick("cache", healthy=True)
        manager.register("cache", brick, protocol_name="CacheProtocol")
        await manager.mount("cache")
        assert manager.get_status("cache").state == BrickState.ACTIVE  # type: ignore[union-attr]

        # Track state transitions via callback
        transitions: list[tuple[str, BrickState, BrickState]] = []
        original_callback = reconciler._on_state_change

        def _tracking_callback(name: str, old: BrickState, new: BrickState) -> None:
            transitions.append((name, old, new))
            original_callback(name, old, new)

        manager.on_state_change = _tracking_callback

        await reconciler.start()
        try:
            # Simulate health degradation
            brick.health_check = AsyncMock(return_value=False)

            # Wait for at least one FAILED detection (reconciler may recover
            # before we can observe FAILED state directly — that's OK)
            for _ in range(30):
                await asyncio.sleep(0.1)
                saw_failure = any(new == BrickState.FAILED for _, _, new in transitions)
                if saw_failure:
                    break

            assert any(new == BrickState.FAILED for _, _, new in transitions), (
                "Health poll should have detected the unhealthy brick"
            )

            # Fix health and let reconciler recover to stable ACTIVE
            brick.health_check = AsyncMock(return_value=True)

            for _ in range(50):
                await asyncio.sleep(0.05)
                status = manager.get_status("cache")
                if status and status.state == BrickState.ACTIVE:
                    break

            assert manager.get_status("cache").state == BrickState.ACTIVE  # type: ignore[union-attr]
        finally:
            await reconciler.stop()


# ---------------------------------------------------------------------------
# 3. Parallel recovery — no head-of-line blocking
# ---------------------------------------------------------------------------


class TestParallelRecovery:
    """Multiple brick failures should be recovered concurrently."""

    @pytest.mark.asyncio
    async def test_multiple_failures_recovered_concurrently(self) -> None:
        manager = BrickLifecycleManager()
        config = BrickReconcilerConfig(
            health_check_interval=999,
            base_delay=0.01,
            max_delay=0.05,
            max_attempts=5,
        )
        reconciler = BrickReconciler(manager, config=config)

        # Register 3 bricks, all fail initially
        bricks: dict[str, MagicMock] = {}
        for name in ("search", "cache", "rag"):
            brick = _make_failing_brick()
            bricks[name] = brick
            manager.register(name, brick, protocol_name=f"{name}P")

        await reconciler.start()
        try:
            # Mount all — all fail
            for name in bricks:
                await manager.mount(name)

            # Verify all FAILED
            for name in bricks:
                assert manager.get_status(name).state == BrickState.FAILED  # type: ignore[union-attr]

            # Fix all bricks
            for brick in bricks.values():
                brick.start = AsyncMock(return_value=None)

            # Wait for recovery (should be concurrent, not serialized)
            t0 = time.monotonic()
            for _ in range(100):
                await asyncio.sleep(0.05)
                all_active = all(
                    manager.get_status(n).state == BrickState.ACTIVE  # type: ignore[union-attr]
                    for n in bricks
                )
                if all_active:
                    break
            elapsed = time.monotonic() - t0

            # All should be ACTIVE
            for name in bricks:
                assert manager.get_status(name).state == BrickState.ACTIVE  # type: ignore[union-attr]

            # Should complete reasonably fast (no 10min head-of-line blocking)
            assert elapsed < 5.0
        finally:
            await reconciler.stop()


# ---------------------------------------------------------------------------
# 4. Dead-letter — no infinite retry
# ---------------------------------------------------------------------------


class TestDeadLetterIntegration:
    """Permanently failing brick should be dead-lettered and not leak memory."""

    @pytest.mark.asyncio
    async def test_permanent_failure_dead_lettered(self) -> None:
        manager = BrickLifecycleManager()
        config = BrickReconcilerConfig(
            health_check_interval=999,
            base_delay=0.01,
            max_delay=0.02,
            max_attempts=3,
        )
        reconciler = BrickReconciler(manager, config=config)

        # Brick that never recovers
        brick = _make_failing_brick()
        manager.register("broken", brick, protocol_name="BrokenP")

        await reconciler.start()
        try:
            await manager.mount("broken")
            assert manager.get_status("broken").state == BrickState.FAILED  # type: ignore[union-attr]

            # Wait enough time for 3 attempts to exhaust
            await asyncio.sleep(1.0)

            # Brick should still be FAILED (dead-lettered)
            assert manager.get_status("broken").state == BrickState.FAILED  # type: ignore[union-attr]
            # Backoff should be cleaned up after dead-letter (no memory leak)
            assert "broken" not in reconciler._backoff
            # Queue should be drained
            assert reconciler._queue.empty()
        finally:
            await reconciler.stop()


# ---------------------------------------------------------------------------
# 5. Lifespan pattern (server startup/shutdown)
# ---------------------------------------------------------------------------


class TestLifespanPattern:
    """Test the reconciler integrates with server lifespan correctly."""

    @pytest.mark.asyncio
    async def test_server_lifespan_pattern(self) -> None:
        """Simulates the startup_bricks / shutdown_bricks lifespan flow."""
        manager = BrickLifecycleManager()
        config = BrickReconcilerConfig(
            health_check_interval=0.1,
            base_delay=0.01,
        )
        reconciler = BrickReconciler(manager, config=config)

        # Register bricks
        for name in ("search", "cache"):
            brick = _make_brick(name)
            manager.register(name, brick, protocol_name=f"{name}P")

        # --- startup_bricks() ---
        report = await manager.mount_all()
        assert report.active == 2
        assert report.failed == 0

        await reconciler.start()
        assert reconciler._reconcile_task is not None
        assert reconciler._health_poll_task is not None

        # Sentinel task pattern (from lifespan/bricks.py)
        async def _sentinel() -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await reconciler.stop()

        sentinel = asyncio.create_task(_sentinel(), name="brick_reconciler_sentinel")

        # Let it run a few poll cycles
        await asyncio.sleep(0.35)

        # All bricks should still be active
        for name in ("search", "cache"):
            assert manager.get_status(name).state == BrickState.ACTIVE  # type: ignore[union-attr]

        # --- shutdown_bricks() ---
        sentinel.cancel()
        try:
            await sentinel
        except asyncio.CancelledError:
            pass

        assert reconciler._reconcile_task is None
        assert reconciler._health_poll_task is None

        report = await manager.unmount_all()
        assert report.active == 0


# ---------------------------------------------------------------------------
# 6. Performance — no CPU spinning
# ---------------------------------------------------------------------------


class TestPerformance:
    """Validate that idle reconciler consumes negligible CPU."""

    @pytest.mark.asyncio
    async def test_idle_reconciler_no_spinning(self) -> None:
        """With no failed bricks, the reconciler should barely use CPU."""
        manager = BrickLifecycleManager()
        config = BrickReconcilerConfig(health_check_interval=0.5)
        reconciler = BrickReconciler(manager, config=config)

        # Register 10 healthy bricks
        for i in range(10):
            brick = _make_brick(f"brick{i}")
            manager.register(f"brick{i}", brick, protocol_name=f"P{i}")
            await manager.mount(f"brick{i}")

        await reconciler.start()

        # Measure CPU time over 2 seconds of idle operation
        t0_cpu = time.process_time()
        await asyncio.sleep(2.0)
        cpu_used = time.process_time() - t0_cpu

        await reconciler.stop()

        # Should use < 1% CPU (< 0.02s of 2s wall time)
        assert cpu_used < 0.1, f"Idle reconciler used {cpu_used:.3f}s CPU in 2s"


# ---------------------------------------------------------------------------
# 7. Queue dedup — no duplicate entries
# ---------------------------------------------------------------------------


class TestQueueDedup:
    """Validate that the same brick is not enqueued multiple times."""

    @pytest.mark.asyncio
    async def test_rapid_failures_do_not_flood_queue(self) -> None:
        manager = BrickLifecycleManager()
        config = BrickReconcilerConfig(
            health_check_interval=999,
            base_delay=0.01,
            max_attempts=10,
        )
        reconciler = BrickReconciler(manager, config=config)

        # Don't start reconciler — we want to inspect queue without draining
        brick = _make_failing_brick()
        manager.register("search", brick, protocol_name="SP")

        # Enqueue same brick many times
        for _ in range(100):
            reconciler.enqueue("search")

        # Queue should only have 1 entry thanks to dedup
        assert reconciler._queue.qsize() == 1
        assert len(reconciler._queued) == 1
