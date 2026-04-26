"""Synchronous wrapper for async nexus-fs operations.

Uses anyio's BlockingPortalProvider to safely bridge sync -> async,
replacing the broken nest_asyncio approach (fails on Python 3.14+).

Three supported scenarios:
1. No event loop (normal Python script) -- creates one internally
2. Existing event loop (Jupyter) -- uses BlockingPortal worker thread
3. Multiple threads -- each gets its own portal, thread-safe

Usage:
    from nexus.fs._sync import SyncNexusFS

    with SyncNexusFS(kernel) as fs:
        content = fs.read("/s3/bucket/file.txt")
"""

from __future__ import annotations

from typing import Any, TypeVar, cast

from anyio.from_thread import BlockingPortalProvider

from nexus.fs._helpers import LOCAL_CONTEXT

T = TypeVar("T")


def run_sync(coro: Any) -> Any:
    """Run an async coroutine synchronously.

    Uses anyio's BlockingPortalProvider for safe sync->async bridging.
    Creates a new event loop per call — use PortalRunner for repeated calls.
    """
    _provider = BlockingPortalProvider()
    with _provider as portal:
        return portal.call(lambda: coro)


class PortalRunner:
    """Persistent sync-to-async bridge.

    Starts a background event loop on first call and keeps it alive
    until close() is called.  Avoids the overhead of starting/stopping
    an event loop on every call.

    Thread-safe — multiple threads can call concurrently via the
    underlying BlockingPortal.

    Usage::

        runner = PortalRunner()
        result = runner(some_coroutine())
        runner.close()

        # or as context manager:
        with PortalRunner() as runner:
            result = runner(some_coroutine())
    """

    def __init__(self) -> None:
        self._provider = BlockingPortalProvider()
        self._portal: Any = None

    def __call__(self, coro: Any) -> Any:
        if self._portal is None:
            self._portal = self._provider.__enter__()
        return self._portal.call(lambda: coro)

    def close(self) -> None:
        """Shut down the background event loop."""
        if self._portal is not None:
            self._provider.__exit__(None, None, None)
            self._portal = None

    def __enter__(self) -> PortalRunner:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def __del__(self) -> None:
        # Best-effort cleanup.  Prefer explicit close() or context manager.
        import contextlib

        with contextlib.suppress(Exception):
            self.close()


class SyncNexusFS:
    """Synchronous wrapper around a ``NexusFS`` kernel.

    Most kernel methods are already synchronous (``sys_read``,
    ``sys_stat``, ...), but the wrapper still keeps a ``PortalRunner``
    for the few entry points that may run a coroutine (e.g. resource
    cleanup) and to preserve the original ``with SyncNexusFS(...)``
    contract.

    Use as a context manager or call ``close()`` when done::

        with SyncNexusFS(kernel) as fs:
            content = fs.read("/path")

    Thread-safe — multiple threads can use the same SyncNexusFS instance.
    """

    def __init__(self, kernel: Any) -> None:
        self._kernel = kernel
        self._runner = PortalRunner()

    # -- Context manager -------------------------------------------------------

    def __enter__(self) -> SyncNexusFS:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    # -- Sync wrappers around kernel sys_* / public methods --------------------

    def read(self, path: str) -> bytes:
        return cast(bytes, self._kernel.sys_read(path, context=LOCAL_CONTEXT))

    def write(self, path: str, content: bytes) -> dict[str, Any]:
        return cast(dict[str, Any], self._kernel.write(path, content, context=LOCAL_CONTEXT))

    def ls(self, path: str = "/", detail: bool = False) -> list[Any]:
        return list(
            self._kernel.sys_readdir(path, recursive=False, details=detail, context=LOCAL_CONTEXT)
        )

    def stat(self, path: str) -> dict[str, Any] | None:
        return cast(
            dict[str, Any] | None,
            self._kernel.sys_stat(path, context=LOCAL_CONTEXT),
        )

    def delete(self, path: str) -> None:
        self._kernel.sys_unlink(path, context=LOCAL_CONTEXT)

    def mkdir(self, path: str, parents: bool = True) -> None:
        self._kernel.mkdir(path, parents=parents, exist_ok=True, context=LOCAL_CONTEXT)

    def rmdir(self, path: str, recursive: bool = False) -> None:
        self._kernel.rmdir(path, recursive=recursive, context=LOCAL_CONTEXT)

    def rename(self, old_path: str, new_path: str) -> None:
        self._kernel.sys_rename(old_path, new_path, context=LOCAL_CONTEXT)

    def exists(self, path: str) -> bool:
        return cast(bool, self._kernel.access(path, context=LOCAL_CONTEXT))

    def copy(self, src: str, dst: str) -> dict[str, Any]:
        return cast(dict[str, Any], self._kernel.sys_copy(src, dst, context=LOCAL_CONTEXT))

    def edit(
        self,
        path: str,
        edits: list[tuple[str, str]] | list[dict[str, Any]],
        *,
        if_match: str | None = None,
        fuzzy_threshold: float = 0.85,
        preview: bool = False,
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self._kernel.edit(
                path,
                edits,
                context=LOCAL_CONTEXT,
                if_match=if_match,
                fuzzy_threshold=fuzzy_threshold,
                preview=preview,
            ),
        )

    def read_range(self, path: str, start: int, end: int) -> bytes:
        return cast(bytes, self._kernel.read_range(path, start, end, context=LOCAL_CONTEXT))

    def grep(
        self,
        pattern: str,
        path: str = "/",
        *,
        ignore_case: bool = False,
        max_results: int = 1000,
    ) -> list[dict[str, Any]]:
        from nexus.fs._helpers import grep as _grep

        return _grep(
            self._kernel,
            pattern,
            path,
            ignore_case=ignore_case,
            max_results=max_results,
        )

    def glob(self, pattern: str, path: str = "/") -> list[str]:
        from nexus.fs._helpers import glob as _glob

        return _glob(self._kernel, pattern, path)

    def list_mounts(self) -> list[str]:
        from nexus.fs._helpers import list_mounts as _list_mounts

        return _list_mounts(self._kernel)

    def unmount(self, mount_point: str) -> None:
        from nexus.fs._helpers import unmount as _unmount

        _unmount(self._kernel, mount_point)

    def write_batch(self, files: list[tuple[str, bytes]]) -> list[dict[str, Any]]:
        return cast(
            list[dict[str, Any]],
            self._kernel.write_batch(files, context=LOCAL_CONTEXT),
        )

    def read_batch(
        self,
        paths: list[str],
        *,
        partial: bool = False,
    ) -> list[dict[str, Any]]:
        return cast(
            list[dict[str, Any]],
            self._kernel.read_batch(paths, partial=partial, context=LOCAL_CONTEXT),
        )

    def close(self) -> None:
        """Close the underlying kernel and release the portal worker."""
        import contextlib

        from nexus.fs._helpers import close as _close_kernel

        with contextlib.suppress(Exception):
            _close_kernel(self._kernel)
        self._runner.close()
