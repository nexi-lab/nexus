"""Singleflight request coalescing (Issue #3400, Decision 4A/13A).

Prevents cache stampede by deduplicating concurrent requests for the
same key.  The first caller starts the work; subsequent callers await
the same result.  On failure, the key is removed so the next caller
retries independently (error isolation — a transient error does NOT
cascade to all waiters).

Inspired by Go's ``singleflight`` package and Facebook's Memcache
lease-token pattern (USENIX NSDI '13).

Usage::

    flight: SingleFlight[bytes] = SingleFlight()

    async def fetch_with_coalescing(key: str) -> bytes:
        return await flight.do(key, lambda: backend.fetch(key))

Thread safety:
    The ``SingleFlight`` itself is protected by ``asyncio.Lock`` and
    must be used from a single event loop.  For sync callers, use
    ``SingleFlightSync`` which uses ``threading.Lock``.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Awaitable, Callable
from typing import Generic, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class SingleFlight(Generic[T]):
    """Async singleflight: coalesce concurrent async calls for the same key.

    Example::

        sf: SingleFlight[bytes] = SingleFlight()
        result = await sf.do("file:abc123", fetch_from_s3)
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._inflight: dict[str, asyncio.Future[T]] = {}

    async def do(self, key: str, fn: Callable[[], Awaitable[T]]) -> T:
        """Execute ``fn`` for ``key``, coalescing concurrent callers.

        If another coroutine is already executing ``fn`` for the same
        ``key``, this coroutine awaits the existing result instead of
        starting a duplicate call.

        On success, all waiters receive the same result.
        On failure, the key is cleared so the next caller retries
        independently (error isolation).

        Args:
            key: Deduplication key (e.g., content hash or path).
            fn: Zero-arg async callable that produces the result.

        Returns:
            The result of ``fn()``.

        Raises:
            Whatever ``fn()`` raises — but only the first caller sees
            the original exception.  Subsequent waiters get a
            ``RuntimeError`` wrapping the original to preserve the
            error-isolation guarantee.
        """
        async with self._lock:
            if key in self._inflight:
                fut = self._inflight[key]
            else:
                fut = asyncio.get_running_loop().create_future()
                self._inflight[key] = fut
                # Release the lock before doing real work
                asyncio.ensure_future(self._execute(key, fn, fut))
        return await fut

    async def _execute(
        self,
        key: str,
        fn: Callable[[], Awaitable[T]],
        fut: asyncio.Future[T],
    ) -> None:
        """Run ``fn`` and resolve the shared future."""
        try:
            result = await fn()
            if not fut.done():
                fut.set_result(result)
        except BaseException as exc:
            if not fut.done():
                fut.set_exception(exc)
        finally:
            async with self._lock:
                self._inflight.pop(key, None)


class SingleFlightSync(Generic[T]):
    """Sync singleflight: coalesce concurrent threaded calls for the same key.

    Uses ``threading.Lock`` and ``threading.Event`` for synchronization.
    Suitable for use in sync cache layers (``LocalDiskCache``,
    ``FileContentCache``).

    Example::

        sf: SingleFlightSync[bytes] = SingleFlightSync()
        result = sf.do("file:abc123", fetch_from_backend)
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._inflight: dict[str, _SyncCall[T]] = {}

    def do(self, key: str, fn: Callable[[], T]) -> T:
        """Execute ``fn`` for ``key``, coalescing concurrent callers.

        Semantics match :meth:`SingleFlight.do` — first caller runs
        ``fn``, others wait.  On failure the key is cleared for retry.

        Args:
            key: Deduplication key.
            fn: Zero-arg callable that produces the result.

        Returns:
            The result of ``fn()``.

        Raises:
            Whatever ``fn()`` raises.
        """
        with self._lock:
            if key in self._inflight:
                call = self._inflight[key]
                is_owner = False
            else:
                call = _SyncCall()
                self._inflight[key] = call
                is_owner = True

        if is_owner:
            try:
                result = fn()
                call.set_result(result)
                return result
            except BaseException as exc:
                call.set_exception(exc)
                raise
            finally:
                with self._lock:
                    self._inflight.pop(key, None)
        else:
            return call.wait()


class _SyncCall(Generic[T]):
    """Shared state for a single in-flight synchronous call."""

    __slots__ = ("_event", "_result", "_exception")

    def __init__(self) -> None:
        self._event = threading.Event()
        self._result: T | None = None
        self._exception: BaseException | None = None

    def set_result(self, result: T) -> None:
        self._result = result
        self._event.set()

    def set_exception(self, exc: BaseException) -> None:
        self._exception = exc
        self._event.set()

    def wait(self) -> T:
        self._event.wait()
        if self._exception is not None:
            raise self._exception
        # _result is guaranteed non-None here: set_result was called
        # (the only other path through set_exception re-raises above).
        assert self._result is not None
        return self._result
