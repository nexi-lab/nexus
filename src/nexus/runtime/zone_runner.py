from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import logging
import threading
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any, TypeVar

T = TypeVar("T")

logger = logging.getLogger(__name__)
_CURRENT = threading.local()


class ZoneRunner:
    """Dedicated thread and asyncio loop for one zone."""

    def __init__(self, zone_id: str, join_timeout: float = 5.0) -> None:
        if not zone_id:
            raise ValueError("zone_id is required")
        self.zone_id = zone_id
        self._join_timeout = join_timeout
        self._lock = threading.RLock()
        self._ready = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._closed = False
        self._stopping = False

    @property
    def is_alive(self) -> bool:
        thread = self._thread
        return bool(thread and thread.is_alive())

    def start(self) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError(f"ZoneRunner {self.zone_id!r} has been stopped")
            thread = self._thread
            if thread is None or not thread.is_alive():
                self._ready.clear()
                thread = threading.Thread(
                    target=self._thread_main,
                    name=f"nexus-zone-{self.zone_id}",
                    daemon=True,
                )
                self._thread = thread
                thread.start()
        self._ready.wait(timeout=5.0)
        with self._lock:
            loop = self._loop
            stopped = self._closed
            thread_alive = thread.is_alive()
        if stopped:
            raise RuntimeError(f"ZoneRunner {self.zone_id!r} has been stopped")
        if loop is None or not thread_alive:
            raise RuntimeError(f"ZoneRunner {self.zone_id!r} did not start")

    async def call(self, work: Callable[[], Awaitable[T]]) -> T:
        if self.is_current_runner():
            return await work()
        await asyncio.to_thread(self.start)
        self._before_submit()

        async def invoke() -> T:
            return await work()

        submitted = self._submit(invoke)
        try:
            return await asyncio.wrap_future(submitted)
        except asyncio.CancelledError:
            submitted.cancel()
            raise

    def call_sync(self, work: Callable[[], Awaitable[T]]) -> T:
        if self.is_current_runner():
            raise RuntimeError("call_sync cannot run on owning runner thread")
        self.start()
        self._before_submit()

        async def invoke() -> T:
            return await work()

        return self._submit(invoke).result()

    def stop(self) -> None:
        owns_shutdown = False
        with self._lock:
            thread = self._thread
            if self._closed:
                stopping = self._stopping
                if not (stopping and thread is not None and thread.is_alive()):
                    return
            else:
                self._closed = True
                self._stopping = True
                self._ready.set()
                owns_shutdown = True
            if not owns_shutdown:
                wait_thread = thread
            else:
                loop = self._loop
        if not owns_shutdown:
            if wait_thread is not None and wait_thread is not threading.current_thread():
                wait_thread.join(timeout=self._join_timeout)
                return
            return
        if thread is None:
            with self._lock:
                self._stopping = False
            return
        if loop is None:
            self._ready.wait(timeout=self._join_timeout)
            loop = self._loop
            if loop is None:
                thread.join(timeout=self._join_timeout)
                if thread.is_alive():
                    logger.warning("Zone runner %s thread did not terminate", self.zone_id)
                else:
                    with self._lock:
                        self._stopping = False
                return
        if thread is threading.current_thread():
            loop.call_soon(loop.stop)
            return
        join_timeout = self._join_timeout * 2
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._cancel_pending(loop, timeout=self._join_timeout),
                loop,
            )
            future.result(timeout=self._join_timeout * 2)
        except (TimeoutError, concurrent.futures.TimeoutError):
            logger.warning("Timed out draining zone runner %s", self.zone_id)
            future.cancel()
            future.add_done_callback(self._consume_submitted_result)
            loop.call_soon_threadsafe(loop.stop)
        except RuntimeError:
            logger.debug(
                "Zone runner %s loop unavailable during stop",
                self.zone_id,
                exc_info=True,
            )
            return
        thread.join(timeout=join_timeout)
        if thread.is_alive():
            logger.warning("Zone runner %s thread did not terminate", self.zone_id)
        else:
            with self._lock:
                self._stopping = False

    def is_current_runner(self) -> bool:
        return getattr(_CURRENT, "runner", None) is self

    def _require_loop(self) -> asyncio.AbstractEventLoop:
        loop = self._loop
        if loop is None:
            raise RuntimeError(f"ZoneRunner {self.zone_id!r} is not running")
        return loop

    def _before_submit(self) -> None:
        return None

    def _submit(
        self,
        invoke: Callable[[], Coroutine[Any, Any, T]],
    ) -> concurrent.futures.Future[T]:
        with self._lock:
            if self._closed or self._stopping:
                raise RuntimeError(f"ZoneRunner {self.zone_id!r} has been stopped")
            loop = self._require_loop()
            submitted = invoke()
            try:
                return asyncio.run_coroutine_threadsafe(submitted, loop)
            except RuntimeError as exc:
                submitted.close()
                raise RuntimeError(f"ZoneRunner {self.zone_id!r} has been stopped") from exc

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        _CURRENT.runner = self
        self._loop = loop
        self._ready.set()
        if self._closed:
            loop.stop()
        try:
            loop.run_forever()
        finally:
            try:
                loop.run_until_complete(
                    self._cancel_pending(loop, stop=False, timeout=self._join_timeout)
                )
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                _CURRENT.runner = None
                asyncio.set_event_loop(None)
                loop.close()
                self._loop = None
                with self._lock:
                    self._stopping = False

    async def _cancel_pending(
        self,
        loop: asyncio.AbstractEventLoop,
        *,
        stop: bool = True,
        timeout: float | None = None,
    ) -> None:
        current = asyncio.current_task(loop=loop)
        tasks = [
            task for task in asyncio.all_tasks(loop) if task is not current and not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            if timeout is None:
                await asyncio.gather(*tasks, return_exceptions=True)
            else:
                done, pending = await asyncio.wait(tasks, timeout=timeout)
                for task in done:
                    with contextlib.suppress(BaseException):
                        task.result()
                if pending:
                    logger.warning(
                        "Closing zone runner %s with %d pending task(s)",
                        self.zone_id,
                        len(pending),
                    )
                    for task in pending:
                        task.cancel()
                        self._mark_task_abandoned(task)
        if stop:
            loop.stop()

    @staticmethod
    def _consume_submitted_result(future: concurrent.futures.Future[Any]) -> None:
        with contextlib.suppress(BaseException):
            future.result()

    @staticmethod
    def _mark_task_abandoned(task: asyncio.Task[object]) -> None:
        if hasattr(task, "_log_destroy_pending"):
            task._log_destroy_pending = False


class ZoneRegistry:
    """Lazy registry of one ZoneRunner per zone touched in this process."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._runners: dict[str, ZoneRunner] = {}

    def runner_for(self, zone_id: str) -> ZoneRunner:
        if not zone_id:
            raise ValueError("zone_id is required")
        with self._lock:
            runner = self._runners.get(zone_id)
            if runner is None:
                runner = ZoneRunner(zone_id)
                self._runners[zone_id] = runner
            return runner

    def all(self) -> tuple[ZoneRunner, ...]:
        with self._lock:
            return tuple(self._runners.values())

    def stop_all(self) -> None:
        for runner in self.all():
            runner.stop()
