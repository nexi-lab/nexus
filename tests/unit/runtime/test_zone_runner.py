import asyncio
import threading

import pytest

from nexus.runtime.zone_runner import ZoneRunner


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


async def _return_value(value: str) -> str:
    return value
