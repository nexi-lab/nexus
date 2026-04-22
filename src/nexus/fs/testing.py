"""Testing utilities for nexus-fs.

Import only in test code — this module is not part of the public API and
carries no backwards-compatibility guarantee.

Usage::

    from nexus.fs.testing import ephemeral_mount

    def test_something():
        with ephemeral_mount("local:///tmp/test-xyz") as fs:
            # mount is active here; mounts.json is never touched
            assert "/local/tmp/test-xyz" in fs.list_mounts()
        # mount is torn down here even if the test raised

    @pytest.mark.asyncio
    async def test_something_async():
        async with async_ephemeral_mount("local:///tmp/test-xyz") as fs:
            assert "/local/tmp/test-xyz" in fs.list_mounts()
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Iterator
from typing import Any


@contextlib.contextmanager
def ephemeral_mount(*uris: str, **kwargs: Any) -> Iterator[Any]:
    """Mount backends without writing to mounts.json.

    Guarantees teardown via ``SlimNexusFS.close()`` even if the body raises.
    Suitable for sync test code.  For async tests use ``async_ephemeral_mount``.

    Args:
        *uris: Backend URIs to mount (same as ``nexus.fs.mount``).
        **kwargs: Forwarded to ``nexus.fs.mount`` (e.g. ``at=``, ``mount_overrides=``).
            ``ephemeral`` is always forced to ``True``; passing it explicitly is a no-op.

    Yields:
        SlimNexusFS facade with all backends mounted.

    Example::

        def test_read_local():
            with ephemeral_mount("local:///tmp/test-data") as fs:
                content = fs.read_sync("/local/tmp/test-data/readme.txt")
    """
    kwargs["ephemeral"] = True
    from nexus.fs import mount
    from nexus.fs._sync import run_sync

    fs: Any = None
    with _isolated_state_dir():
        try:
            fs = run_sync(mount(*uris, **kwargs))
            yield fs
        finally:
            if fs is not None:
                _close_fs_sync(fs)


@contextlib.asynccontextmanager
async def async_ephemeral_mount(*uris: str, **kwargs: Any) -> AsyncIterator[Any]:
    """Async variant of ``ephemeral_mount`` for use in async test functions.

    Example::

        @pytest.mark.asyncio
        async def test_read_local():
            async with async_ephemeral_mount("local:///tmp/test-data") as fs:
                content = fs.read("/local/tmp/test-data/readme.txt")
    """
    kwargs["ephemeral"] = True
    from nexus.fs import mount

    fs: Any = None
    with _isolated_state_dir():
        try:
            fs = await mount(*uris, **kwargs)
            yield fs
        finally:
            if fs is not None:
                await _close_fs_async(fs)


@contextlib.contextmanager
def _isolated_state_dir() -> Iterator[None]:
    """Give each ephemeral_mount its own NEXUS_FS_STATE_DIR so nested calls
    don't collide on the same redb/SQLite metadata file.

    Each ephemeral_mount spins up its own ``Kernel`` + metastore. Two kernels
    opening the same redb file raise ``"Database already open. Cannot acquire
    lock."`` — redb enforces exclusive-access per file. We keep the outer
    caller's override (if set) intact on exit so the caller's monkeypatched
    state_dir is still the one mounts.json (or its absence) is asserted on.
    """
    import os
    import tempfile

    outer = os.environ.get("NEXUS_FS_STATE_DIR")
    tmp = tempfile.mkdtemp(prefix="nexus-ephemeral-")
    os.environ["NEXUS_FS_STATE_DIR"] = tmp
    try:
        yield
    finally:
        if outer is None:
            os.environ.pop("NEXUS_FS_STATE_DIR", None)
        else:
            os.environ["NEXUS_FS_STATE_DIR"] = outer
        # Best-effort cleanup — on Windows SQLite/redb may still hold file
        # handles briefly even after close(), so ignore removal errors.
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


def _close_fs_sync(fs: Any) -> None:
    """Best-effort synchronous close of a NexusFS (close is sync)."""
    close = getattr(fs, "close", None)
    if close is None:
        return
    with contextlib.suppress(Exception):
        close()


async def _close_fs_async(fs: Any) -> None:
    """Best-effort close of a SlimNexusFS (close is sync)."""
    close = getattr(fs, "close", None)
    if close is None:
        return
    with contextlib.suppress(Exception):
        close()
