"""Unit tests for AsyncTaskRunner."""

import asyncio
from unittest.mock import MagicMock

import pytest

from nexus.tasks.runner import AsyncTaskRunner, ProgressReporter


class FakeTaskRecord:
    """Minimal fake matching PyTaskRecord interface."""

    def __init__(self, task_id: int, task_type: str, params: bytes = b""):
        self.task_id = task_id
        self.task_type = task_type
        self.params = params
        self.status = 1  # RUNNING
        self.attempt = 1


class TestRegisterExecutor:
    def test_register_stores_executor(self):
        engine = MagicMock()
        runner = AsyncTaskRunner(engine=engine)

        @runner.register("test.echo")
        async def handle_echo(params, progress):
            return b"ok"

        assert "test.echo" in runner._executors
        assert runner._executors["test.echo"] is handle_echo

    def test_register_multiple_types(self):
        engine = MagicMock()
        runner = AsyncTaskRunner(engine=engine)

        @runner.register("type_a")
        async def handle_a(params, progress):
            return b"a"

        @runner.register("type_b")
        async def handle_b(params, progress):
            return b"b"

        assert len(runner._executors) == 2


class TestProgressReporter:
    def test_update_calls_heartbeat(self):
        engine = MagicMock()
        engine.heartbeat.return_value = True
        reporter = ProgressReporter(_engine=engine, _task_id=42)

        result = reporter.update(50, "halfway")

        assert result is True
        engine.heartbeat.assert_called_once_with(42, 50, "halfway")

    def test_update_returns_false_on_cancelled(self):
        engine = MagicMock()
        engine.heartbeat.return_value = False
        reporter = ProgressReporter(_engine=engine, _task_id=42)

        result = reporter.update(50, "check")

        assert result is False


class TestWorkerClaimAndComplete:
    @pytest.mark.asyncio
    async def test_worker_completes_task(self):
        engine = MagicMock()
        task = FakeTaskRecord(1, "test.echo", b"input")

        # claim_next returns task once, then None forever
        engine.claim_next.side_effect = [task, None]

        runner = AsyncTaskRunner(engine=engine, max_workers=1)

        @runner.register("test.echo")
        async def handle(params, progress):
            return b"result"

        # Run worker briefly
        runner._shutdown = False

        async def stop_after_delay():
            await asyncio.sleep(0.3)
            runner._shutdown = True

        stopper = asyncio.create_task(stop_after_delay())
        worker = asyncio.create_task(runner._worker(0))
        await asyncio.gather(stopper, worker, return_exceptions=True)

        engine.complete.assert_called_once_with(1, b"result")

    @pytest.mark.asyncio
    async def test_worker_handles_failure(self):
        engine = MagicMock()
        task = FakeTaskRecord(1, "test.fail", b"")
        engine.claim_next.side_effect = [task, None]

        runner = AsyncTaskRunner(engine=engine, max_workers=1)

        @runner.register("test.fail")
        async def handle(params, progress):
            raise ValueError("boom")

        runner._shutdown = False

        async def stop_after_delay():
            await asyncio.sleep(0.3)
            runner._shutdown = True

        stopper = asyncio.create_task(stop_after_delay())
        worker = asyncio.create_task(runner._worker(0))
        await asyncio.gather(stopper, worker, return_exceptions=True)

        engine.fail.assert_called_once_with(1, "boom")

    @pytest.mark.asyncio
    async def test_worker_fails_unknown_task_type(self):
        engine = MagicMock()
        task = FakeTaskRecord(1, "unknown.type", b"")
        engine.claim_next.side_effect = [task, None]

        runner = AsyncTaskRunner(engine=engine, max_workers=1)
        # No executor registered for "unknown.type"

        runner._shutdown = False

        async def stop_after_delay():
            await asyncio.sleep(0.3)
            runner._shutdown = True

        stopper = asyncio.create_task(stop_after_delay())
        worker = asyncio.create_task(runner._worker(0))
        await asyncio.gather(stopper, worker, return_exceptions=True)

        engine.fail.assert_called_once()
        call_args = engine.fail.call_args
        assert call_args[0][0] == 1
        assert "unknown.type" in call_args[0][1]


class TestBackoff:
    @pytest.mark.asyncio
    async def test_backoff_on_empty_queue(self):
        engine = MagicMock()
        engine.claim_next.return_value = None

        runner = AsyncTaskRunner(engine=engine, max_workers=1)
        runner._shutdown = False

        claim_count = 0
        original_claim = engine.claim_next

        def counting_claim(*args, **kwargs):
            nonlocal claim_count
            claim_count += 1
            return original_claim.return_value

        engine.claim_next.side_effect = counting_claim

        async def stop_after_delay():
            await asyncio.sleep(0.5)
            runner._shutdown = True

        stopper = asyncio.create_task(stop_after_delay())
        worker = asyncio.create_task(runner._worker(0))
        await asyncio.gather(stopper, worker, return_exceptions=True)

        # With backoff, should have fewer claims than a tight loop would
        # At 100ms initial backoff doubling: ~100ms, ~200ms → ~2-3 claims in 0.5s
        assert 1 <= claim_count <= 6


class TestGracefulShutdown:
    @pytest.mark.asyncio
    async def test_shutdown_sets_flag(self):
        engine = MagicMock()
        runner = AsyncTaskRunner(engine=engine)

        assert runner._shutdown is False
        await runner.shutdown()
        assert runner._shutdown is True


class TestRequeueLoop:
    @pytest.mark.asyncio
    async def test_requeue_loop_calls_engine(self):
        engine = MagicMock()
        engine.requeue_abandoned.return_value = 0

        runner = AsyncTaskRunner(engine=engine, requeue_interval=0.1)
        runner._shutdown = False

        async def stop_after_delay():
            await asyncio.sleep(0.35)
            runner._shutdown = True

        stopper = asyncio.create_task(stop_after_delay())
        requeue = asyncio.create_task(runner._requeue_loop())
        await asyncio.gather(stopper, requeue, return_exceptions=True)

        assert engine.requeue_abandoned.call_count >= 2
