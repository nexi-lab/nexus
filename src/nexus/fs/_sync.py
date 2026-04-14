"""Synchronous wrapper for nexus-fs operations.

Now that SlimNexusFS is fully sync, SyncNexusFS is a thin pass-through
that delegates directly — no event loop bridging needed.

Usage:
    from nexus.fs._sync import SyncNexusFS

    with SyncNexusFS(facade) as fs:
        content = fs.read("/s3/bucket/file.txt")
"""

from __future__ import annotations

from typing import Any, TypeVar, cast

from anyio.from_thread import BlockingPortalProvider

T = TypeVar("T")


def run_sync(coro: Any) -> Any:
    """Run an async coroutine synchronously.

    Uses anyio's BlockingPortalProvider for safe sync->async bridging.
    Still needed by _cli.py and _fsspec.py for async connect() etc.
    """
    _provider = BlockingPortalProvider()
    with _provider as portal:
        return portal.call(lambda: coro)


class PortalRunner:
    """Persistent sync-to-async bridge.

    Starts a background event loop on first call and keeps it alive
    until close() is called.  Still needed by _fsspec.py.
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
        import contextlib

        with contextlib.suppress(Exception):
            self.close()


class SyncNexusFS:
    """Synchronous wrapper around the NexusFS facade.

    Since SlimNexusFS is now fully sync, this wrapper delegates
    directly without any async bridging.  Kept for API compatibility.

    Use as a context manager or call close() when done::

        with SyncNexusFS(facade) as fs:
            content = fs.read("/path")
    """

    def __init__(self, async_fs: Any) -> None:
        self._async = async_fs

    # -- Context manager -------------------------------------------------------

    def __enter__(self) -> SyncNexusFS:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    # -- Expose the public facade methods as sync versions ---------------------

    def read(self, path: str) -> bytes:
        return cast(bytes, self._async.read(path))

    def write(self, path: str, content: bytes) -> dict[str, Any]:
        return cast(dict[str, Any], self._async.write(path, content))

    def ls(self, path: str = "/", detail: bool = False) -> list[Any]:
        return cast(list[Any], self._async.ls(path, detail=detail))

    def stat(self, path: str) -> dict[str, Any] | None:
        return cast(dict[str, Any] | None, self._async.stat(path))

    def delete(self, path: str) -> None:
        self._async.delete(path)

    def mkdir(self, path: str, parents: bool = True) -> None:
        self._async.mkdir(path, parents=parents)

    def rmdir(self, path: str, recursive: bool = False) -> None:
        self._async.rmdir(path, recursive=recursive)

    def rename(self, old_path: str, new_path: str) -> None:
        self._async.rename(old_path, new_path)

    def exists(self, path: str) -> bool:
        return cast(bool, self._async.exists(path))

    def copy(self, src: str, dst: str) -> dict[str, Any]:
        return cast(dict[str, Any], self._async.copy(src, dst))

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
            self._async.edit(
                path,
                edits,
                if_match=if_match,
                fuzzy_threshold=fuzzy_threshold,
                preview=preview,
            ),
        )

    def read_range(self, path: str, start: int, end: int) -> bytes:
        return cast(bytes, self._async.read_range(path, start, end))

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
            self._async.grep(
                pattern,
                path,
                ignore_case=ignore_case,
                max_results=max_results,
            ),
        )

    def glob(self, pattern: str, path: str = "/") -> list[str]:
        return cast(list[str], self._async.glob(pattern, path))

    def list_mounts(self) -> list[str]:
        """List all mount points."""
        return cast(list[str], self._async.list_mounts())

    def unmount(self, mount_point: str) -> None:
        """Remove a mount and clean up all associated state."""
        self._async.unmount(mount_point)

    def write_batch(self, files: list[tuple[str, bytes]]) -> list[dict[str, Any]]:
        return cast(list[dict[str, Any]], self._async.write_batch(files))

    def read_batch(
        self,
        paths: list[str],
        *,
        partial: bool = False,
    ) -> list[dict[str, Any]]:
        return cast(list[dict[str, Any]], self._async.read_batch(paths, partial=partial))

    def close(self) -> None:
        """Clean up resources held by the underlying facade."""
        if hasattr(self._async, "close"):
            import contextlib

            with contextlib.suppress(Exception):
                self._async.close()
