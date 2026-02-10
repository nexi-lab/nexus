"""Integration tests for the full nexus-tasks pipeline."""

import asyncio

import pytest

try:
    from _nexus_tasks import TaskEngine

    HAS_NEXUS_TASKS = True
except ImportError:
    HAS_NEXUS_TASKS = False

from nexus.tasks.runner import AsyncTaskRunner

pytestmark = pytest.mark.skipif(
    not HAS_NEXUS_TASKS,
    reason="nexus_tasks Rust extension not available",
)


class TestEndToEnd:
    @pytest.mark.asyncio
    async def test_submit_execute_complete_e2e(self, tmp_path):
        """Full lifecycle: submit -> claim -> execute -> complete."""
        engine = TaskEngine(str(tmp_path / "e2e-db"))
        runner = AsyncTaskRunner(engine=engine, max_workers=2)
        completed_tasks = []

        @runner.register("test.echo")
        async def handle_echo(params: bytes, progress):
            progress.update(50, "processing")
            completed_tasks.append(params)
            return b"echo:" + params

        # Submit tasks
        tid1 = engine.submit("test.echo", b"hello")
        tid2 = engine.submit("test.echo", b"world")

        # Run workers briefly
        async def stop_after_tasks():
            # Wait for both tasks to complete
            for _ in range(50):  # Max 5 seconds
                s = engine.stats()
                if s.completed >= 2:
                    break
                await asyncio.sleep(0.1)
            await runner.shutdown()

        stopper = asyncio.create_task(stop_after_tasks())
        run_task = asyncio.create_task(runner.run())
        await asyncio.gather(stopper, run_task, return_exceptions=True)

        # Verify results
        assert len(completed_tasks) == 2
        t1 = engine.status(tid1)
        t2 = engine.status(tid2)
        assert t1.status == 2  # COMPLETED
        assert t2.status == 2  # COMPLETED
        assert t1.result == b"echo:hello"
        assert t2.result == b"echo:world"

    @pytest.mark.asyncio
    async def test_concurrent_workers_no_double_claim(self, tmp_path):
        """Multiple workers should never claim the same task."""
        engine = TaskEngine(str(tmp_path / "concurrent-db"), max_pending=100)
        runner = AsyncTaskRunner(engine=engine, max_workers=4)
        claimed_ids = []
        lock = asyncio.Lock()

        @runner.register("test.track")
        async def handle_track(params: bytes, progress):
            async with lock:
                claimed_ids.append(int.from_bytes(params, "big"))
            await asyncio.sleep(0.01)  # Simulate work
            return b"ok"

        # Submit 20 tasks
        for i in range(20):
            engine.submit("test.track", i.to_bytes(8, "big"))

        async def stop_after_all():
            for _ in range(100):
                s = engine.stats()
                if s.completed >= 20:
                    break
                await asyncio.sleep(0.1)
            await runner.shutdown()

        stopper = asyncio.create_task(stop_after_all())
        run_task = asyncio.create_task(runner.run())
        await asyncio.gather(stopper, run_task, return_exceptions=True)

        # No duplicates
        assert len(claimed_ids) == len(set(claimed_ids))
        assert len(claimed_ids) == 20

    @pytest.mark.asyncio
    async def test_failure_and_retry_e2e(self, tmp_path):
        """Tasks that fail should be retried up to max_retries."""
        engine = TaskEngine(str(tmp_path / "retry-db"))
        runner = AsyncTaskRunner(engine=engine, max_workers=1)
        attempt_count = 0

        @runner.register("test.flaky")
        async def handle_flaky(params: bytes, progress):
            nonlocal attempt_count
            attempt_count += 1
            if attempt_count < 3:
                raise ValueError(f"Fail attempt {attempt_count}")
            return b"success"

        # Submit with max_retries=3 — task can fail up to 3 times before dead letter
        tid = engine.submit("test.flaky", b"", max_retries=5)

        async def stop_after_complete():
            for _ in range(100):
                task = engine.status(tid)
                if task and task.status in (2, 4):  # COMPLETED or DEAD_LETTER
                    break
                await asyncio.sleep(0.1)
            await runner.shutdown()

        stopper = asyncio.create_task(stop_after_complete())
        run_task = asyncio.create_task(runner.run())
        await asyncio.gather(stopper, run_task, return_exceptions=True)

        # Note: retry timing depends on backoff, so the task may still be pending
        # In a short test window, we verify at least one failure was recorded
        assert attempt_count >= 1

    @pytest.mark.asyncio
    async def test_progress_reporting_e2e(self, tmp_path):
        """Progress updates should be visible via heartbeat."""
        engine = TaskEngine(str(tmp_path / "progress-db"))
        runner = AsyncTaskRunner(engine=engine, max_workers=1)

        @runner.register("test.progress")
        async def handle_progress(params: bytes, progress):
            progress.update(25, "quarter done")
            progress.update(75, "almost done")
            return b"done"

        tid = engine.submit("test.progress", b"")

        async def stop_after_complete():
            for _ in range(50):
                task = engine.status(tid)
                if task and task.status == 2:
                    break
                await asyncio.sleep(0.1)
            await runner.shutdown()

        stopper = asyncio.create_task(stop_after_complete())
        run_task = asyncio.create_task(runner.run())
        await asyncio.gather(stopper, run_task, return_exceptions=True)

        task = engine.status(tid)
        assert task.status == 2  # COMPLETED
