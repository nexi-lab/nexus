"""Unit tests for TaskEventBroadcaster (SSE live-refresh).

Verifies the lightweight asyncio.Event-based broadcaster that replaced
the old DT_STREAM SSE mechanism.

Run with:
    pytest tests/unit/server/test_task_event_broadcaster.py -v
"""

import asyncio

import pytest

from nexus.server.api.v2.routers.task_manager import TaskEventBroadcaster


class TestTaskEventBroadcaster:
    """Core broadcaster behaviour."""

    @pytest.mark.asyncio
    async def test_wait_blocks_until_signal(self) -> None:
        """wait() should block until on_task_signal fires."""
        b = TaskEventBroadcaster()
        fired = False

        async def _waiter() -> None:
            nonlocal fired
            await b.wait()
            fired = True

        task = asyncio.create_task(_waiter())
        await asyncio.sleep(0.05)
        assert not fired, "wait() should block"

        # Fire signal — should unblock the waiter
        b.on_task_signal("task_created", {"id": "t-1"})
        await asyncio.sleep(0.05)
        assert fired, "wait() should have resolved after signal"
        task.cancel()

    @pytest.mark.asyncio
    async def test_multiple_waiters_all_unblocked(self) -> None:
        """All concurrent waiters should be woken by a single signal."""
        b = TaskEventBroadcaster()
        results: list[bool] = []

        async def _waiter() -> None:
            await b.wait()
            results.append(True)

        tasks = [asyncio.create_task(_waiter()) for _ in range(5)]
        await asyncio.sleep(0.05)
        assert len(results) == 0

        b.on_task_signal("task_updated", {"id": "t-1"})
        await asyncio.sleep(0.05)
        assert len(results) == 5, f"Expected 5 waiters woken, got {len(results)}"

        for t in tasks:
            t.cancel()

    @pytest.mark.asyncio
    async def test_auto_reset_after_wait(self) -> None:
        """After wait() returns, subsequent wait() should block again."""
        b = TaskEventBroadcaster()

        # First cycle: signal then wait
        b.on_task_signal("task_created", {"id": "t-1"})
        # Event is set — wait() should return immediately after clear+wait
        # Actually, wait() calls clear() then wait(), so pre-set event gets cleared.
        # We need to fire the signal while wait() is blocked.

        resolved = False

        async def _waiter() -> None:
            nonlocal resolved
            await b.wait()
            resolved = True

        task = asyncio.create_task(_waiter())
        await asyncio.sleep(0.05)
        b.on_task_signal("task_created", {"id": "t-1"})
        await asyncio.sleep(0.05)
        assert resolved

        # Second cycle: should block again
        resolved = False
        task2 = asyncio.create_task(_waiter())
        await asyncio.sleep(0.05)
        assert not resolved, "wait() should block again after first cycle"

        b.on_task_signal("task_updated", {"id": "t-2"})
        await asyncio.sleep(0.05)
        assert resolved
        task.cancel()
        task2.cancel()

    @pytest.mark.asyncio
    async def test_signal_type_agnostic(self) -> None:
        """Broadcaster should fire for any signal type."""
        b = TaskEventBroadcaster()

        for sig_type in ("task_created", "task_updated", "task_deleted", "custom"):
            done = False

            async def _waiter() -> None:
                nonlocal done
                await b.wait()
                done = True

            task = asyncio.create_task(_waiter())
            await asyncio.sleep(0.05)
            b.on_task_signal(sig_type, {"id": "t-1"})
            await asyncio.sleep(0.05)
            assert done, f"Should fire for signal type '{sig_type}'"
            task.cancel()

    @pytest.mark.asyncio
    async def test_conforms_to_signal_handler_protocol(self) -> None:
        """Broadcaster should satisfy the TaskSignalHandler protocol."""
        import inspect

        b = TaskEventBroadcaster()
        # Structural check: must have on_task_signal(self, signal_type, payload)
        assert hasattr(b, "on_task_signal")
        assert callable(b.on_task_signal)
        sig = inspect.signature(b.on_task_signal)
        params = list(sig.parameters.keys())
        assert params == ["_signal_type", "_payload"]
