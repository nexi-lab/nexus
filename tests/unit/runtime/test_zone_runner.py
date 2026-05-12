import asyncio
import threading
import time

import pytest

from nexus.runtime.zone_runner import ZoneRunner


def test_empty_zone_id_raises() -> None:
    with pytest.raises(ValueError, match="zone_id is required"):
        ZoneRunner("")


def test_join_timeout_can_be_positional() -> None:
    runner = ZoneRunner("zone-a", 1.0)

    try:
        assert not runner.is_alive
    finally:
        runner.stop()


@pytest.mark.asyncio
async def test_call_runs_on_dedicated_loop_and_thread() -> None:
    runner = ZoneRunner("zone-a")
    caller_loop = asyncio.get_running_loop()
    caller_thread = threading.get_ident()

    async def work() -> tuple[int, asyncio.AbstractEventLoop]:
        return threading.get_ident(), asyncio.get_running_loop()

    try:
        worker_thread, worker_loop = await runner.call(work)
    finally:
        runner.stop()

    assert worker_thread != caller_thread
    assert worker_loop is not caller_loop


def test_start_uses_daemon_thread_named_for_zone() -> None:
    runner = ZoneRunner("zone-a")

    try:
        runner.start()
        thread = runner._thread
        assert thread is not None
        assert thread.daemon
        assert thread.name == "nexus-zone-zone-a"
    finally:
        runner.stop()


def test_concurrent_call_waits_for_startup_readiness() -> None:
    runner = _DelayedStartRunner("zone-a", join_timeout=1.0)
    start_errors: list[BaseException] = []
    call_errors: list[BaseException] = []
    call_results: list[str] = []

    async def work() -> str:
        return "ok"

    def start_runner() -> None:
        try:
            runner.start()
        except BaseException as exc:
            start_errors.append(exc)

    def call_runner() -> None:
        try:
            call_results.append(runner.call_sync(work))
        except BaseException as exc:
            call_errors.append(exc)

    starter = threading.Thread(target=start_runner)
    caller = threading.Thread(target=call_runner)
    starter.start()
    try:
        assert runner.startup_blocked.wait(2.0)
        caller.start()
        caller.join(0.1)

        assert caller.is_alive()

        runner.release_startup.set()
        starter.join(2.0)
        caller.join(2.0)

        assert start_errors == []
        assert call_errors == []
        assert call_results == ["ok"]
    finally:
        runner.release_startup.set()
        starter.join(2.0)
        caller.join(2.0)
        runner.stop()


def test_call_sync_runs_from_sync_code() -> None:
    runner = ZoneRunner("zone-a")

    async def work() -> str:
        return "ok"

    try:
        assert runner.call_sync(work) == "ok"
    finally:
        runner.stop()


@pytest.mark.asyncio
async def test_call_propagates_exception() -> None:
    runner = ZoneRunner("zone-a")

    async def work() -> str:
        raise ValueError("boom")

    try:
        with pytest.raises(ValueError, match="boom"):
            await runner.call(work)
    finally:
        runner.stop()


@pytest.mark.asyncio
async def test_call_cancellation_cancels_submitted_work() -> None:
    runner = ZoneRunner("zone-a")
    started = threading.Event()
    cancelled = threading.Event()

    async def work() -> str:
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return "done"

    task = asyncio.create_task(runner.call(work))
    try:
        assert await asyncio.to_thread(started.wait, 2.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await asyncio.to_thread(cancelled.wait, 2.0)
    finally:
        task.cancel()
        runner.stop()


@pytest.mark.asyncio
async def test_same_runner_reentry_does_not_deadlock() -> None:
    runner = ZoneRunner("zone-a")

    async def nested() -> str:
        return await runner.call(lambda: _return_value("nested-ok"))

    try:
        assert await runner.call(nested) == "nested-ok"
    finally:
        runner.stop()


def test_call_sync_from_owner_thread_raises() -> None:
    runner = ZoneRunner("zone-a")

    async def work() -> str:
        with pytest.raises(RuntimeError, match="call_sync cannot run on owning runner thread"):
            runner.call_sync(lambda: _return_value("bad"))
        return "ok"

    try:
        assert runner.call_sync(work) == "ok"
    finally:
        runner.stop()


def test_stop_is_idempotent() -> None:
    runner = ZoneRunner("zone-a")
    runner.start()

    runner.stop()
    runner.stop()

    assert not runner.is_alive


def test_start_after_stop_raises() -> None:
    runner = ZoneRunner("zone-a")

    runner.stop()

    with pytest.raises(RuntimeError, match="ZoneRunner 'zone-a' has been stopped"):
        runner.start()


def test_stop_cancels_pending_tasks() -> None:
    runner = ZoneRunner("zone-a")
    started = threading.Event()
    cancelled = threading.Event()

    async def pending_work() -> None:
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def schedule_pending_work() -> None:
        asyncio.create_task(pending_work())

    try:
        runner.call_sync(schedule_pending_work)
        assert started.wait(2.0)
        runner.stop()
        assert cancelled.wait(2.0)
        assert not runner.is_alive
    finally:
        runner.stop()


def test_stop_terminates_when_pending_task_suppresses_cancellation() -> None:
    runner = ZoneRunner("zone-a", join_timeout=0.1)
    started = threading.Event()
    cancelled = threading.Event()
    release = threading.Event()

    async def stubborn_work() -> None:
        started.set()
        while not release.is_set():
            try:
                await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                cancelled.set()

    async def schedule_stubborn_work() -> None:
        asyncio.create_task(stubborn_work())

    try:
        runner.call_sync(schedule_stubborn_work)
        assert started.wait(2.0)

        before_stop = time.monotonic()
        runner.stop()
        stop_duration = time.monotonic() - before_stop

        assert cancelled.wait(2.0)
        assert stop_duration < 1.0
        assert not runner.is_alive
    finally:
        release.set()
        thread = runner._thread
        if thread is not None:
            thread.join(2.0)
        runner.stop()


def test_stop_during_startup_does_not_strand_runner() -> None:
    runner = _DelayedStartRunner("zone-a", join_timeout=0.1)
    start_errors: list[BaseException] = []

    def start_runner() -> None:
        try:
            runner.start()
        except BaseException as exc:
            start_errors.append(exc)

    starter = threading.Thread(target=start_runner)
    starter.start()
    try:
        assert runner.startup_blocked.wait(2.0)

        runner.stop()
        runner.release_startup.set()
        starter.join(2.0)
        runner_thread = runner._thread
        if runner_thread is not None:
            runner_thread.join(2.0)

        stranded = runner.is_alive
        runner.stop()

        assert len(start_errors) == 1
        assert isinstance(start_errors[0], RuntimeError)
        assert "has been stopped" in str(start_errors[0])
        assert not stranded
        assert not runner.is_alive
    finally:
        runner.release_startup.set()
        starter.join(2.0)
        if runner.is_alive:
            loop = runner._loop
            thread = runner._thread
            if loop is not None:
                loop.call_soon_threadsafe(loop.stop)
            if thread is not None:
                thread.join(2.0)


async def _return_value(value: str) -> str:
    return value


class _DelayedStartRunner(ZoneRunner):
    def __init__(self, zone_id: str, join_timeout: float = 5.0) -> None:
        super().__init__(zone_id, join_timeout)
        self.startup_blocked = threading.Event()
        self.release_startup = threading.Event()

    def _thread_main(self) -> None:
        self.startup_blocked.set()
        self.release_startup.wait(2.0)
        super()._thread_main()
