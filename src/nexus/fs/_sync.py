"""Synchronous wrapper for async nexus-fs operations.

Uses anyio's BlockingPortalProvider to safely bridge sync -> async,
replacing the broken nest_asyncio approach (fails on Python 3.14+).

Three supported scenarios:
1. No event loop (normal Python script) -- creates one internally
2. Existing event loop (Jupyter) -- uses BlockingPortal worker thread
3. Multiple threads -- each gets its own portal, thread-safe

Usage:
    from nexus.fs._sync import SyncNexusFS

    sync_fs = SyncNexusFS(async_facade)
    content = sync_fs.read("/s3/bucket/file.txt")
"""

from __future__ import annotations

import functools
from typing import Any, TypeVar, cast

from anyio.from_thread import BlockingPortalProvider

T = TypeVar("T")


def run_sync(coro: Any) -> Any:
    """Run an async coroutine synchronously.

    Uses anyio's BlockingPortalProvider for safe sync->async bridging.
    Works in all three scenarios: no event loop, existing loop (Jupyter),
    and multi-threaded contexts.
    """
    _provider = BlockingPortalProvider()
    with _provider as portal:
        return portal.call(lambda: coro)


class SyncNexusFS:
    """Synchronous wrapper around the async NexusFS facade.

    Delegates every call to the async facade via an anyio BlockingPortal.
    Thread-safe -- multiple threads can use the same SyncNexusFS instance.

    The BlockingPortalProvider manages a shared event loop: the first thread
    to enter starts the loop, and the last thread to exit shuts it down.
    ``portal.call(func, *args)`` accepts a callable (sync or async) and
    positional arguments; if the callable returns a coroutine it is awaited
    automatically.  For calls that require keyword arguments we use
    ``functools.partial`` to bind them before handing the callable to the
    portal.
    """

    def __init__(self, async_fs: Any) -> None:
        self._async = async_fs
        self._portal_provider = BlockingPortalProvider()

    # -- Expose the public facade methods as sync versions ----------------

    def read(self, path: str) -> bytes:
        with self._portal_provider as portal:
            return cast(bytes, portal.call(self._async.read, path))

    def write(self, path: str, content: bytes) -> dict[str, Any]:
        with self._portal_provider as portal:
            return cast(dict[str, Any], portal.call(self._async.write, path, content))

    def ls(self, path: str = "/", detail: bool = False) -> list[Any]:
        with self._portal_provider as portal:
            return cast(
                list[Any],
                portal.call(functools.partial(self._async.ls, path, detail=detail)),
            )

    def stat(self, path: str) -> dict[str, Any] | None:
        with self._portal_provider as portal:
            return cast(dict[str, Any] | None, portal.call(self._async.stat, path))

    def delete(self, path: str) -> None:
        with self._portal_provider as portal:
            portal.call(self._async.delete, path)

    def mkdir(self, path: str, parents: bool = True) -> None:
        with self._portal_provider as portal:
            portal.call(functools.partial(self._async.mkdir, path, parents=parents))

    def rename(self, old_path: str, new_path: str) -> None:
        with self._portal_provider as portal:
            portal.call(self._async.rename, old_path, new_path)

    def exists(self, path: str) -> bool:
        with self._portal_provider as portal:
            return cast(bool, portal.call(self._async.exists, path))

    def copy(self, src: str, dst: str) -> dict[str, Any]:
        with self._portal_provider as portal:
            return cast(dict[str, Any], portal.call(self._async.copy, src, dst))

    def read_range(self, path: str, start: int, end: int) -> bytes:
        with self._portal_provider as portal:
            return cast(bytes, portal.call(self._async.read_range, path, start, end))

    def list_mounts(self) -> list[str]:
        """List all mount points (synchronous -- no portal needed)."""
        return cast(list[str], self._async.list_mounts())

    def close(self) -> None:
        """Clean up resources held by the underlying async facade."""
        if hasattr(self._async, "close"):
            with self._portal_provider as portal:
                portal.call(self._async.close)
