from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
from collections.abc import Awaitable, Callable
from typing import TypeVar

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

    @property
    def is_alive(self) -> bool:
        thread = self._thread
        return bool(thread and thread.is_alive())

    def start(self) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError(f"ZoneRunner {self.zone_id!r} has been stopped")
            if self._thread is not None and self._thread.is_alive():
                return
            self._ready.clear()
            thread = threading.Thread(
                target=self._thread_main,
                name=f"nexus-zone-{self.zone_id}",
                daemon=True,
            )
            self._thread = thread
            thread.start()
        self._ready.wait(timeout=5.0)
        if self._loop is None:
            raise RuntimeError(f"ZoneRunner {self.zone_id!r} did not start")

    async def call(self, work: Callable[[], Awaitable[T]]) -> T:
        if self.is_current_runner():
            return await work()
        self.start()
        loop = self._require_loop()

        async def invoke() -> T:
            return await work()

        submitted = asyncio.run_coroutine_threadsafe(invoke(), loop)
        try:
            return await asyncio.wrap_future(submitted)
        except asyncio.CancelledError:
            submitted.cancel()
            raise

    def call_sync(self, work: Callable[[], Awaitable[T]]) -> T:
        if self.is_current_runner():
            raise RuntimeError("call_sync cannot run on owning runner thread")
        self.start()
        loop = self._require_loop()

        async def invoke() -> T:
            return await work()

        return asyncio.run_coroutine_threadsafe(invoke(), loop).result()

    def stop(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            loop = self._loop
            thread = self._thread
        if loop is None or thread is None:
            return
        if thread is threading.current_thread():
            loop.call_soon(loop.stop)
            return
        try:
            future = asyncio.run_coroutine_threadsafe(self._cancel_pending(loop), loop)
            future.result(timeout=self._join_timeout)
        except (TimeoutError, concurrent.futures.TimeoutError):
            logger.warning("Timed out draining zone runner %s", self.zone_id)
            loop.call_soon_threadsafe(loop.stop)
        except RuntimeError:
            return
        thread.join(timeout=self._join_timeout)
        if thread.is_alive():
            logger.warning("Zone runner %s thread did not terminate", self.zone_id)

    def is_current_runner(self) -> bool:
        return getattr(_CURRENT, "runner", None) is self

    def _require_loop(self) -> asyncio.AbstractEventLoop:
        loop = self._loop
        if loop is None:
            raise RuntimeError(f"ZoneRunner {self.zone_id!r} is not running")
        return loop

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        _CURRENT.runner = self
        self._loop = loop
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            try:
                loop.run_until_complete(self._cancel_pending(loop, stop=False))
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                _CURRENT.runner = None
                asyncio.set_event_loop(None)
                loop.close()
                self._loop = None

    async def _cancel_pending(
        self,
        loop: asyncio.AbstractEventLoop,
        *,
        stop: bool = True,
    ) -> None:
        current = asyncio.current_task(loop=loop)
        tasks = [
            task for task in asyncio.all_tasks(loop) if task is not current and not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if stop:
            loop.stop()


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
