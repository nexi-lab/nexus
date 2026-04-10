"""Synchronous wrapper for async nexus-fs operations.

Uses anyio's BlockingPortalProvider to safely bridge sync -> async,
replacing the broken nest_asyncio approach (fails on Python 3.14+).

Three supported scenarios:
1. No event loop (normal Python script) -- creates one internally
2. Existing event loop (Jupyter) -- uses BlockingPortal worker thread
3. Multiple threads -- each gets its own portal, thread-safe

Usage:
    from nexus.fs._sync import SyncNexusFS

    with SyncNexusFS(async_facade) as fs:
        content = fs.read("/s3/bucket/file.txt")
"""

from __future__ import annotations

from typing import Any, TypeVar, cast

from anyio.from_thread import BlockingPortalProvider

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
    """Synchronous wrapper around the async NexusFS facade.

    Keeps a persistent event loop alive across calls via PortalRunner.
    Use as a context manager or call close() when done::

        with SyncNexusFS(async_facade) as fs:
            content = fs.read("/path")

    Thread-safe — multiple threads can use the same SyncNexusFS instance.
    """

    def __init__(self, async_fs: Any) -> None:
        self._async = async_fs
        self._runner = PortalRunner()

    # -- Context manager -------------------------------------------------------

    def __enter__(self) -> SyncNexusFS:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    # -- Expose the public facade methods as sync versions ---------------------

    def read(self, path: str) -> bytes:
        return cast(bytes, self._runner(self._async.read(path)))

    def write(self, path: str, content: bytes) -> dict[str, Any]:
        return cast(dict[str, Any], self._runner(self._async.write(path, content)))

    def ls(self, path: str = "/", detail: bool = False) -> list[Any]:
        return cast(
            list[Any],
            self._runner(self._async.ls(path, detail=detail)),
        )

    def stat(self, path: str) -> dict[str, Any] | None:
        return cast(dict[str, Any] | None, self._runner(self._async.stat(path)))

    def delete(self, path: str) -> None:
        self._runner(self._async.delete(path))

    def mkdir(self, path: str, parents: bool = True) -> None:
        self._runner(self._async.mkdir(path, parents=parents))

    def rmdir(self, path: str, recursive: bool = False) -> None:
        self._runner(self._async.rmdir(path, recursive=recursive))

    def rename(self, old_path: str, new_path: str) -> None:
        self._runner(self._async.rename(old_path, new_path))

    def exists(self, path: str) -> bool:
        return cast(bool, self._runner(self._async.exists(path)))

    def copy(self, src: str, dst: str) -> dict[str, Any]:
        return cast(dict[str, Any], self._runner(self._async.copy(src, dst)))

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
            self._runner(
                self._async.edit(
                    path,
                    edits,
                    if_match=if_match,
                    fuzzy_threshold=fuzzy_threshold,
                    preview=preview,
                )
            ),
        )

    def read_range(self, path: str, start: int, end: int) -> bytes:
        return cast(bytes, self._runner(self._async.read_range(path, start, end)))

    def grep(
        self,
        pattern: str,
        path: str = "/",
        *,
        ignore_case: bool = False,
        max_results: int = 1000,
    ) -> list[dict[str, Any]]:
        return cast(
            list[dict[str, Any]],
            self._runner(
                self._async.grep(
                    pattern,
                    path,
                    ignore_case=ignore_case,
                    max_results=max_results,
                )
            ),
        )

    def glob(self, pattern: str, path: str = "/") -> list[str]:
        return cast(list[str], self._runner(self._async.glob(pattern, path)))

    def list_mounts(self) -> list[str]:
        """List all mount points (synchronous -- no portal needed)."""
        return cast(list[str], self._async.list_mounts())

    def unmount(self, mount_point: str) -> None:
        """Remove a mount and clean up all associated state (synchronous wrapper)."""
        self._runner(self._async.unmount(mount_point))

    def write_batch(self, files: list[tuple[str, bytes]]) -> list[dict[str, Any]]:
        return cast(
            list[dict[str, Any]],
            self._runner(self._async.write_batch(files)),
        )

    def read_batch(
        self,
        paths: list[str],
        *,
        partial: bool = False,
    ) -> list[dict[str, Any]]:
        return cast(
            list[dict[str, Any]],
            self._runner(self._async.read_batch(paths, partial=partial)),
        )

    def close(self) -> None:
        """Clean up resources held by the underlying async facade and portal."""
        if hasattr(self._async, "close"):
            import contextlib

            with contextlib.suppress(Exception):
                self._runner(self._async.close())
        self._runner.close()
