"""Tests for ReplayEngine and ProxyBrick._do_forward() (#11-A)."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from nexus.proxy.circuit_breaker import AsyncCircuitBreaker
from nexus.proxy.config import ProxyBrickConfig
from nexus.proxy.errors import RemoteCallError
from nexus.proxy.queue_protocol import InMemoryQueue, QueuedOperation
from nexus.proxy.replay_engine import ReplayEngine


def _make_op(
    op_id: int = 1,
    method: str = "read",
    kwargs: dict | None = None,
    retry_count: int = 0,
) -> QueuedOperation:
    return QueuedOperation(
        id=op_id,
        method=method,
        args_json="[]",
        kwargs_json=json.dumps(kwargs or {"path": "/a"}),
        payload_ref=None,
        retry_count=retry_count,
        created_at=1000.0,
    )


@pytest.fixture()
def transport() -> AsyncMock:
    t = AsyncMock()
    t.call = AsyncMock(return_value={"ok": True})
    t.stream_upload = AsyncMock(return_value={"ok": True})
    t.close = AsyncMock()
    return t


@pytest.fixture()
def circuit() -> AsyncCircuitBreaker:
    return AsyncCircuitBreaker(failure_threshold=3, recovery_timeout=1.0, half_open_max_calls=1)


@pytest.fixture()
def queue() -> AsyncMock:
    q = AsyncMock()
    q.initialize = AsyncMock()
    q.enqueue = AsyncMock(return_value=1)
    q.dequeue_batch = AsyncMock(return_value=[])
    q.mark_done = AsyncMock()
    q.mark_failed = AsyncMock()
    q.mark_dead_letter = AsyncMock()
    q.pending_count = AsyncMock(return_value=0)
    q.close = AsyncMock()
    return q


# ---------------------------------------------------------------------------
# ReplayEngine tests
# ---------------------------------------------------------------------------


class TestReplayProcessesBatch:
    async def test_dequeues_and_replays(
        self, queue: AsyncMock, transport: AsyncMock, circuit: AsyncCircuitBreaker
    ) -> None:
        ops = [_make_op(1, "read"), _make_op(2, "write")]
        queue.dequeue_batch = AsyncMock(side_effect=[ops, []])

        engine = ReplayEngine(
            queue=queue,
            transport=transport,
            circuit=circuit,
            batch_size=10,
            poll_interval=0.01,
        )

        # Run engine briefly
        task = asyncio.create_task(engine.run())
        await asyncio.sleep(0.05)
        await engine.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert transport.call.call_count >= 2
        assert queue.mark_done.call_count >= 2


class TestReplayMarksDoneOnSuccess:
    async def test_mark_done_called(
        self, queue: AsyncMock, transport: AsyncMock, circuit: AsyncCircuitBreaker
    ) -> None:
        ops = [_make_op(42, "read")]
        queue.dequeue_batch = AsyncMock(side_effect=[ops, []])

        engine = ReplayEngine(
            queue=queue, transport=transport, circuit=circuit, batch_size=10, poll_interval=0.01
        )

        task = asyncio.create_task(engine.run())
        await asyncio.sleep(0.05)
        await engine.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        queue.mark_done.assert_any_call(42)


class TestReplayMarksFailedOnConnectionError:
    async def test_connection_error_handling(
        self, queue: AsyncMock, transport: AsyncMock, circuit: AsyncCircuitBreaker
    ) -> None:
        import httpx

        ops = [_make_op(1)]
        queue.dequeue_batch = AsyncMock(side_effect=[ops, []])
        transport.call = AsyncMock(
            side_effect=RemoteCallError("read", cause=httpx.ConnectError("fail"))
        )

        engine = ReplayEngine(
            queue=queue, transport=transport, circuit=circuit, batch_size=10, poll_interval=0.01
        )

        task = asyncio.create_task(engine.run())
        await asyncio.sleep(0.05)
        await engine.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        queue.mark_failed.assert_any_call(1)


class TestReplayDeadLettersInvalidJson:
    async def test_bad_kwargs_json(
        self, queue: AsyncMock, transport: AsyncMock, circuit: AsyncCircuitBreaker
    ) -> None:
        bad_op = QueuedOperation(
            id=99,
            method="read",
            args_json="[]",
            kwargs_json="not-valid-json",
            payload_ref=None,
            retry_count=0,
            created_at=1000.0,
        )
        queue.dequeue_batch = AsyncMock(side_effect=[[bad_op], []])

        engine = ReplayEngine(
            queue=queue, transport=transport, circuit=circuit, batch_size=10, poll_interval=0.01
        )

        task = asyncio.create_task(engine.run())
        await asyncio.sleep(0.05)
        await engine.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        queue.mark_dead_letter.assert_any_call(99)


class TestReplayStopsBatchOnConnectionError:
    async def test_break_on_connection_error(
        self, queue: AsyncMock, transport: AsyncMock, circuit: AsyncCircuitBreaker
    ) -> None:
        import httpx

        ops = [_make_op(1), _make_op(2)]
        queue.dequeue_batch = AsyncMock(side_effect=[ops, []])
        transport.call = AsyncMock(
            side_effect=RemoteCallError("read", cause=httpx.ConnectError("fail"))
        )

        engine = ReplayEngine(
            queue=queue, transport=transport, circuit=circuit, batch_size=10, poll_interval=0.01
        )

        task = asyncio.create_task(engine.run())
        await asyncio.sleep(0.05)
        await engine.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # Should have broken on first op — only 1 mark_failed, not 2
        assert queue.mark_failed.call_count == 1


class TestReplayStopCancelsCleanly:
    async def test_stop(
        self, queue: AsyncMock, transport: AsyncMock, circuit: AsyncCircuitBreaker
    ) -> None:
        engine = ReplayEngine(
            queue=queue, transport=transport, circuit=circuit, batch_size=10, poll_interval=0.01
        )

        task = asyncio.create_task(engine.run())
        await asyncio.sleep(0.03)
        await engine.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert engine._stopped is True


class TestReplayContinuesAfterNonConnectionError:
    async def test_non_connection_error(
        self, queue: AsyncMock, transport: AsyncMock, circuit: AsyncCircuitBreaker
    ) -> None:
        ops = [_make_op(1), _make_op(2)]
        queue.dequeue_batch = AsyncMock(side_effect=[ops, []])
        # First call fails with non-connection error, second succeeds
        transport.call = AsyncMock(
            side_effect=[
                RemoteCallError("read", status_code=500, cause=RuntimeError("server")),
                {"ok": True},
            ]
        )

        engine = ReplayEngine(
            queue=queue, transport=transport, circuit=circuit, batch_size=10, poll_interval=0.01
        )

        task = asyncio.create_task(engine.run())
        await asyncio.sleep(0.05)
        await engine.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # First op failed, second succeeded
        queue.mark_failed.assert_any_call(1)
        queue.mark_done.assert_any_call(2)


# ---------------------------------------------------------------------------
# ProxyBrick._do_forward() tests
# ---------------------------------------------------------------------------


class TestDoForwardRegularCall:
    async def test_regular_call(self, transport: AsyncMock, circuit: AsyncCircuitBreaker) -> None:
        from nexus.proxy.brick import ProxyBrick

        config = ProxyBrickConfig(remote_url="http://test:8000")
        q = InMemoryQueue()
        await q.initialize()
        proxy = ProxyBrick(config, transport=transport, queue=q)

        result = await proxy._do_forward("read", path="/a")
        transport.call.assert_called_once_with("read", params={"path": "/a"})
        assert result == {"ok": True}


class TestDoForwardStreamingCall:
    async def test_streaming_call(self, transport: AsyncMock, circuit: AsyncCircuitBreaker) -> None:
        from nexus.proxy.brick import ProxyBrick

        config = ProxyBrickConfig(remote_url="http://test:8000")
        q = InMemoryQueue()
        await q.initialize()
        proxy = ProxyBrick(config, transport=transport, queue=q)

        result = await proxy._do_forward("write", data=b"hello", path="/b")
        transport.stream_upload.assert_called_once_with("write", b"hello", params={"path": "/b"})
        assert result == {"ok": True}


# ---------------------------------------------------------------------------
# ProxyBrick lifecycle tests
# ---------------------------------------------------------------------------


class TestProxyBrickLifecycle:
    async def test_double_stop_is_safe(self, transport: AsyncMock) -> None:
        """stop() called twice does not crash."""
        from nexus.proxy.brick import ProxyBrick

        config = ProxyBrickConfig(remote_url="http://test:8000")
        q = InMemoryQueue()
        await q.initialize()
        proxy = ProxyBrick(config, transport=transport, queue=q)

        await proxy.start()
        await proxy.stop()
        await proxy.stop()  # second stop must not raise

    async def test_stop_before_start(self, transport: AsyncMock) -> None:
        """stop() before start() does not crash."""
        from nexus.proxy.brick import ProxyBrick

        config = ProxyBrickConfig(remote_url="http://test:8000")
        q = InMemoryQueue()
        await q.initialize()
        proxy = ProxyBrick(config, transport=transport, queue=q)

        await proxy.stop()  # never started — must not raise

    async def test_start_stop_start(self, transport: AsyncMock) -> None:
        """Can restart after stopping."""
        from nexus.proxy.brick import ProxyBrick

        config = ProxyBrickConfig(remote_url="http://test:8000")
        q = InMemoryQueue()
        await q.initialize()
        proxy = ProxyBrick(config, transport=transport, queue=q)

        await proxy.start()
        await proxy.stop()
        await proxy.start()
        # Verify the replay engine is running after restart
        assert proxy._replay_engine is not None
        assert proxy._replay_task is not None
        assert not proxy._stopped
        await proxy.stop()


# ---------------------------------------------------------------------------
# ReplayEngine.wake() tests
# ---------------------------------------------------------------------------


class TestReplayEngineWake:
    async def test_wake_triggers_immediate_poll(
        self, queue: AsyncMock, transport: AsyncMock, circuit: AsyncCircuitBreaker
    ) -> None:
        """wake() causes the engine to check queue without waiting for poll interval."""
        ops = [_make_op(1, "read")]
        queue.dequeue_batch = AsyncMock(side_effect=[ops, []])

        engine = ReplayEngine(
            queue=queue,
            transport=transport,
            circuit=circuit,
            batch_size=10,
            poll_interval=60.0,  # very long — would time out test if wake doesn't work
        )

        task = asyncio.create_task(engine.run())
        # Give the loop a moment to start waiting
        await asyncio.sleep(0.02)

        # Wake should cause immediate processing instead of waiting 60s
        engine.wake()
        await asyncio.sleep(0.05)

        await engine.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # The engine should have processed the queued op promptly
        assert transport.call.call_count >= 1
        queue.mark_done.assert_any_call(1)

    async def test_wake_is_idempotent(
        self, queue: AsyncMock, transport: AsyncMock, circuit: AsyncCircuitBreaker
    ) -> None:
        """Calling wake() multiple times is safe."""
        engine = ReplayEngine(
            queue=queue,
            transport=transport,
            circuit=circuit,
            batch_size=10,
            poll_interval=0.01,
        )

        # Calling wake multiple times before run — must not raise
        engine.wake()
        engine.wake()
        engine.wake()

        task = asyncio.create_task(engine.run())
        await asyncio.sleep(0.03)

        # Calling wake multiple times during run — must not raise
        engine.wake()
        engine.wake()

        await engine.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# ProxyBrick wake-after-enqueue tests
# ---------------------------------------------------------------------------


class TestProxyBrickWakeAfterEnqueue:
    async def test_connection_error_wakes_replay(self, transport: AsyncMock) -> None:
        """When a connection error causes enqueue, replay engine gets woken."""
        import httpx

        from nexus.proxy.brick import ProxyBrick
        from nexus.proxy.errors import OfflineQueuedError

        config = ProxyBrickConfig(remote_url="http://test:8000")
        q = InMemoryQueue()
        await q.initialize()
        proxy = ProxyBrick(config, transport=transport, queue=q)
        await proxy.start()

        # Spy on the replay engine's wake method
        assert proxy._replay_engine is not None
        original_wake = proxy._replay_engine.wake
        wake_calls = 0

        def counting_wake() -> None:
            nonlocal wake_calls
            wake_calls += 1
            original_wake()

        proxy._replay_engine.wake = counting_wake  # type: ignore[assignment]

        # Simulate a connection error on the transport
        transport.call = AsyncMock(
            side_effect=RemoteCallError("read", cause=httpx.ConnectError("fail"))
        )

        with pytest.raises(OfflineQueuedError):
            await proxy._do_forward("read", path="/a")

        assert wake_calls >= 1, "replay engine should have been woken after enqueue"

        await proxy.stop()
