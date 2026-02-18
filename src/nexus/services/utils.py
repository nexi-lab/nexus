"""Service-layer utilities (Issue #1287, Decision 6A/13A).

Provides ``@run_sync`` decorator to replace the repetitive
``async def method(): def _impl(): ...; return await asyncio.to_thread(_impl)``
pattern used across 16 service files (76 occurrences).
"""


import asyncio
from collections.abc import Callable, Coroutine
from functools import wraps
from typing import Any, TypeVar

_T = TypeVar("_T")


def run_sync(fn: Callable[..., _T]) -> Callable[..., Coroutine[Any, Any, _T]]:
    """Convert a sync method to async by running it in a thread pool.

    Wraps the decorated function with ``asyncio.to_thread()`` so it runs
    in the default executor without blocking the event loop.

    The resulting function passes ``inspect.iscoroutinefunction()`` checks,
    preserving Protocol compliance for async interfaces.

    Usage::

        class MyService:
            @run_sync
            def my_method(self, path: str) -> dict:
                # Sync implementation — runs in thread pool
                return {"result": "value"}

            # Equivalent to the old pattern:
            # async def my_method(self, path: str) -> dict:
            #     def _impl():
            #         return {"result": "value"}
            #     return await asyncio.to_thread(_impl)

    Note:
        The decorated function appears as ``async`` to callers and to
        ``inspect.iscoroutinefunction()``. The original sync function
        is preserved as ``fn.__wrapped__`` by ``@functools.wraps``.
    """

    @wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> _T:
        return await asyncio.to_thread(fn, *args, **kwargs)

    return wrapper
