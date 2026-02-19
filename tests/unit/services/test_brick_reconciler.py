"""Tests for BrickReconciler — self-healing with requeue and backoff (Issue #2059).

TDD: Tests written FIRST. 8 test classes covering:
1. Creation and configuration
2. Enqueue mechanism
3. Successful recovery
4. Backoff on failure
5. Dead-letter after max attempts
6. Health poll cycle
7. State-change callback trigger
8. Graceful shutdown
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
from nexus.services.protocols.brick_reconciler import (
    BrickReconcilerConfig,
)

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


def _make_failing_brick(error: Exception | None = None) -> MagicMock:
    brick = _make_lifecycle_brick("failing")
    brick.start = AsyncMock(side_effect=error or RuntimeError("Connection refused"))
    return brick


# ---------------------------------------------------------------------------
# 1. Creation and configuration
# ---------------------------------------------------------------------------


class TestReconcilerCreation:
    """Test reconciler construction and configuration."""

    def test_creates_with_defaults(self) -> None:
        manager = BrickLifecycleManager()
        reconciler = BrickReconciler(manager)
        assert reconciler._config == BrickReconcilerConfig()

    def test_creates_with_custom_config(self) -> None:
        manager = BrickLifecycleManager()
        config = BrickReconcilerConfig(max_attempts=5, base_delay=2.0)
        reconciler = BrickReconciler(manager, config=config)
        assert reconciler._config.max_attempts == 5
        assert reconciler._config.base_delay == 2.0

    def test_registers_state_change_callback(self) -> None:
        manager = BrickLifecycleManager()
        BrickReconciler(manager)
        assert manager.on_state_change is not None


# ---------------------------------------------------------------------------
# 2. Enqueue mechanism
# ---------------------------------------------------------------------------


class TestEnqueue:
    """Test the brick enqueue mechanism."""

    def test_enqueue_adds_to_queue(self) -> None:
        manager = BrickLifecycleManager()
        reconciler = BrickReconciler(manager)
        reconciler.enqueue("search")
        assert not reconciler._queue.empty()
        assert "search" in reconciler._queued

    def test_enqueue_multiple_bricks(self) -> None:
        manager = BrickLifecycleManager()
        reconciler = BrickReconciler(manager)
        reconciler.enqueue("search")
        reconciler.enqueue("cache")
        assert reconciler._queue.qsize() == 2

    def test_enqueue_deduplicates(self) -> None:
        """Same brick enqueued twice should only appear once."""
        manager = BrickLifecycleManager()
        reconciler = BrickReconciler(manager)
        reconciler.enqueue("search")
        reconciler.enqueue("search")
        assert reconciler._queue.qsize() == 1


# ---------------------------------------------------------------------------
# 3. Successful recovery
# ---------------------------------------------------------------------------


class TestRecoveryAttempt:
    """Test that successful recovery resets backoff state."""

    @pytest.mark.asyncio
    async def test_successful_recovery_resets_backoff(self) -> None:
        """A brick that recovers should have its backoff state cleared."""
        manager = BrickLifecycleManager()
        config = BrickReconcilerConfig(
            health_check_interval=999,  # disable polling
            base_delay=0.01,
            max_delay=0.1,
        )
        reconciler = BrickReconciler(manager, config=config)

        # Register a brick that fails initially
        brick = _make_failing_brick()
        manager.register("search", brick, protocol_name="SP")
        await manager.mount("search")
        assert manager.get_status("search").state == BrickState.FAILED  # type: ignore[union-attr]

        # Fix the brick
        brick.start = AsyncMock(return_value=None)

        # Run one recovery cycle
        await reconciler._attempt_recovery("search")

        assert manager.get_status("search").state == BrickState.ACTIVE  # type: ignore[union-attr]
        # Backoff state should be cleared
        assert "search" not in reconciler._backoff

    @pytest.mark.asyncio
    async def test_recovery_calls_reset_then_mount(self) -> None:
        """Recovery should: reset (FAILED→REGISTERED) then mount (→ACTIVE)."""
        manager = BrickLifecycleManager()
        config = BrickReconcilerConfig(base_delay=0.01)
        reconciler = BrickReconciler(manager, config=config)

        brick = _make_lifecycle_brick("search")
        manager.register("search", brick, protocol_name="SP")
        # Force to FAILED
        manager._force_state("search", BrickState.FAILED)
        manager._bricks["search"].error = "transient error"

        await reconciler._attempt_recovery("search")

        # Should be ACTIVE now
        assert manager.get_status("search").state == BrickState.ACTIVE  # type: ignore[union-attr]
        brick.start.assert_awaited_once()


# ---------------------------------------------------------------------------
# 4. Backoff on failure
# ---------------------------------------------------------------------------


class TestBackoffOnFailure:
    """Test that failed recovery increases backoff and requeues."""

    @pytest.mark.asyncio
    async def test_failed_recovery_increments_backoff(self) -> None:
        manager = BrickLifecycleManager()
        config = BrickReconcilerConfig(base_delay=0.01, max_delay=1.0, max_attempts=5)
        reconciler = BrickReconciler(manager, config=config)

        # Register a permanently failing brick
        brick = _make_failing_brick()
        manager.register("search", brick, protocol_name="SP")
        manager._force_state("search", BrickState.FAILED)

        await reconciler._attempt_recovery("search")

        # Backoff state should be recorded
        assert "search" in reconciler._backoff
        assert reconciler._backoff["search"].attempt == 1

    @pytest.mark.asyncio
    async def test_consecutive_failures_increase_attempt_count(self) -> None:
        manager = BrickLifecycleManager()
        config = BrickReconcilerConfig(base_delay=0.01, max_delay=0.1, max_attempts=10)
        reconciler = BrickReconciler(manager, config=config)

        brick = _make_failing_brick()
        manager.register("search", brick, protocol_name="SP")
        manager._force_state("search", BrickState.FAILED)

        # First attempt
        await reconciler._attempt_recovery("search")
        assert reconciler._backoff["search"].attempt == 1

        # Second attempt (brick is FAILED again after mount fails)
        await reconciler._attempt_recovery("search")
        assert reconciler._backoff["search"].attempt == 2


# ---------------------------------------------------------------------------
# 5. Dead-letter after max attempts
# ---------------------------------------------------------------------------


class TestDeadLetter:
    """Test that bricks exceeding max_attempts are not requeued."""

    @pytest.mark.asyncio
    async def test_exceeds_max_attempts_not_requeued(self) -> None:
        manager = BrickLifecycleManager()
        config = BrickReconcilerConfig(base_delay=0.01, max_delay=0.05, max_attempts=2)
        reconciler = BrickReconciler(manager, config=config)

        brick = _make_failing_brick()
        manager.register("search", brick, protocol_name="SP")
        manager._force_state("search", BrickState.FAILED)

        # Exhaust attempts
        from nexus.services.protocols.brick_reconciler import BackoffState

        reconciler._backoff["search"] = BackoffState(attempt=2, last_delay=0.05, next_retry_at=0.0)

        await reconciler._attempt_recovery("search")

        # Should NOT be in the queue (dead-lettered)
        assert reconciler._queue.empty()
        # Brick stays FAILED
        assert manager.get_status("search").state == BrickState.FAILED  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_dead_letter_cleans_backoff_state(self) -> None:
        """Dead-lettered bricks should have their _backoff entry removed (no leak)."""
        manager = BrickLifecycleManager()
        config = BrickReconcilerConfig(base_delay=0.01, max_delay=0.05, max_attempts=2)
        reconciler = BrickReconciler(manager, config=config)

        brick = _make_failing_brick()
        manager.register("search", brick, protocol_name="SP")
        manager._force_state("search", BrickState.FAILED)

        from nexus.services.protocols.brick_reconciler import BackoffState

        reconciler._backoff["search"] = BackoffState(attempt=2, last_delay=0.05, next_retry_at=0.0)

        await reconciler._attempt_recovery("search")

        # Backoff state should be cleaned up after dead-lettering
        assert "search" not in reconciler._backoff


# ---------------------------------------------------------------------------
# 6. Health poll cycle
# ---------------------------------------------------------------------------


class TestHealthPollCycle:
    """Test the periodic health check polling."""

    @pytest.mark.asyncio
    async def test_detects_unhealthy_brick(self) -> None:
        """Health poll should enqueue bricks that fail health_check."""
        manager = BrickLifecycleManager()
        config = BrickReconcilerConfig(health_check_interval=0.05)
        reconciler = BrickReconciler(manager, config=config)

        brick = _make_lifecycle_brick("search")
        manager.register("search", brick, protocol_name="SP")
        await manager.mount("search")
        assert manager.get_status("search").state == BrickState.ACTIVE  # type: ignore[union-attr]

        # Make health_check return False
        brick.health_check = AsyncMock(return_value=False)

        # Run one poll cycle
        await reconciler._poll_health()

        # Brick should now be FAILED and enqueued
        assert manager.get_status("search").state == BrickState.FAILED  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_healthy_bricks_not_enqueued(self) -> None:
        manager = BrickLifecycleManager()
        reconciler = BrickReconciler(manager)

        brick = _make_lifecycle_brick("search")
        brick.health_check = AsyncMock(return_value=True)
        manager.register("search", brick, protocol_name="SP")
        await manager.mount("search")

        await reconciler._poll_health()
        assert reconciler._queue.empty()

    @pytest.mark.asyncio
    async def test_concurrent_health_checks(self) -> None:
        """Multiple bricks checked concurrently via asyncio.gather."""
        manager = BrickLifecycleManager()
        reconciler = BrickReconciler(manager)

        # Register 3 bricks
        for name in ("a", "b", "c"):
            brick = _make_lifecycle_brick(name)
            brick.health_check = AsyncMock(return_value=True)
            manager.register(name, brick, protocol_name=f"{name}P")
            await manager.mount(name)

        # All healthy — poll should complete quickly
        t0 = time.monotonic()
        await reconciler._poll_health()
        elapsed = time.monotonic() - t0

        # Should be concurrent (not 3 x timeout)
        assert elapsed < 2.0
        assert reconciler._queue.empty()


# ---------------------------------------------------------------------------
# 7. State-change callback trigger
# ---------------------------------------------------------------------------


class TestStateChangeCallback:
    """Test that FAILED transitions immediately enqueue bricks."""

    @pytest.mark.asyncio
    async def test_failure_enqueues_brick(self) -> None:
        manager = BrickLifecycleManager()
        reconciler = BrickReconciler(manager)

        brick = _make_failing_brick()
        manager.register("search", brick, protocol_name="SP")
        await manager.mount("search")  # Will fail → callback fires

        # Brick should be in the queue and tracked in _queued set
        assert not reconciler._queue.empty()
        assert "search" in reconciler._queued
        queued = reconciler._queue.get_nowait()
        assert queued == "search"

    @pytest.mark.asyncio
    async def test_non_failure_transition_does_not_enqueue(self) -> None:
        manager = BrickLifecycleManager()
        reconciler = BrickReconciler(manager)

        brick = _make_lifecycle_brick("search")
        manager.register("search", brick, protocol_name="SP")
        await manager.mount("search")  # Success

        # Queue should be empty (success transitions don't enqueue)
        assert reconciler._queue.empty()


# ---------------------------------------------------------------------------
# 8. Graceful shutdown
# ---------------------------------------------------------------------------


class TestShutdown:
    """Test graceful shutdown of the reconciler."""

    @pytest.mark.asyncio
    async def test_start_stop_lifecycle(self) -> None:
        manager = BrickLifecycleManager()
        config = BrickReconcilerConfig(health_check_interval=0.05)
        reconciler = BrickReconciler(manager, config=config)

        await reconciler.start()
        assert reconciler._reconcile_task is not None
        assert reconciler._health_poll_task is not None

        await reconciler.stop()
        assert reconciler._reconcile_task is None
        assert reconciler._health_poll_task is None

    @pytest.mark.asyncio
    async def test_stop_is_idempotent(self) -> None:
        manager = BrickLifecycleManager()
        reconciler = BrickReconciler(manager)

        await reconciler.stop()  # Never started — should not crash
        await reconciler.stop()  # Double stop — should not crash

    @pytest.mark.asyncio
    async def test_reconciler_runs_and_stops_cleanly(self) -> None:
        """Reconciler should process items and stop without errors."""
        manager = BrickLifecycleManager()
        config = BrickReconcilerConfig(
            health_check_interval=0.05,
            base_delay=0.01,
            max_delay=0.05,
        )
        reconciler = BrickReconciler(manager, config=config)

        # Register and fail a brick
        brick = _make_failing_brick()
        manager.register("search", brick, protocol_name="SP")
        manager._force_state("search", BrickState.FAILED)
        reconciler.enqueue("search")

        await reconciler.start()
        # Give it time to process
        await asyncio.sleep(0.2)
        await reconciler.stop()

        # Should have attempted recovery at least once
        assert reconciler._backoff.get("search") is not None
