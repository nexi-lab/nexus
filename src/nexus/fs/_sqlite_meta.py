"""``SQLiteMetastore`` compatibility factory.

Historical note — this module used to implement a Python-only
``MetastoreABC`` subclass backed by a local SQLite file as the slim
package's metadata store for environments without the Rust kernel.
The kernel is now the single source of truth for metastore state, and
``nexus-fs`` declares ``nexus-runtime`` as a required dep, so the
Python-only path no longer exists.

This file preserves the ``SQLiteMetastore`` import path as a thin
factory **function** — not a class — that returns a
``RustMetastoreProxy`` wired to a fresh bare ``Kernel`` with its
redb-backed metastore pointed at ``db_path``. The ``.db`` suffix is
rewritten to ``.redb`` so an existing sqlite file from a previous run
is not accidentally overwritten.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar, cast

from nexus.core.metastore import RustMetastoreProxy

F = TypeVar("F", bound=Callable[..., Any])


def _retry_on_busy(fn: F) -> F:
    """Retry on ``sqlite3.OperationalError: database is locked`` with
    exponential backoff.

    The kernel-backed metastore no longer contends on a process-wide
    SQLite writer lock, so this decorator is mostly cosmetic on the
    new code path. It still exists because callers (and historic
    tests) expect the retry semantics, and future SQLite-backed
    sidecars (auth profile store, record store) may share a file
    with multiple writers.
    """
    import functools
    import sqlite3
    import time

    _MAX_RETRIES = 5
    _BASE_DELAY = 0.001  # 1 ms

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        last_exc: BaseException | None = None
        for attempt in range(_MAX_RETRIES):
            try:
                return fn(*args, **kwargs)
            except sqlite3.OperationalError as exc:
                msg = str(exc).lower()
                if "locked" not in msg and "busy" not in msg:
                    # A different SQLite error — do not retry.
                    raise
                last_exc = exc
                # Exponential backoff: 1 ms, 2 ms, 4 ms, 8 ms, 16 ms.
                time.sleep(_BASE_DELAY * (2**attempt))
        assert last_exc is not None  # narrow for mypy
        raise last_exc

    return cast(F, wrapper)


# Process-local cache: redb file → shared PyKernel. ``redb`` enforces
# exclusive-file access within a process, so every SQLiteMetastore call
# targeting the same path must funnel through one kernel. Per-proxy
# kernels (the old behaviour) deadlocked multi-threaded tests and CLI
# flows that `mount` twice in quick succession (Issue #3765 Cat-5/6).
_KERNEL_CACHE: dict[str, Any] = {}
_KERNEL_CACHE_LOCK: Any = None  # lazily initialized to avoid import-time cost


def _get_cache_lock() -> Any:
    import threading

    global _KERNEL_CACHE_LOCK
    if _KERNEL_CACHE_LOCK is None:
        _KERNEL_CACHE_LOCK = threading.Lock()
    return _KERNEL_CACHE_LOCK


def _get_or_open_kernel(redb_path: str) -> Any:
    with _get_cache_lock():
        existing = _KERNEL_CACHE.get(redb_path)
        if existing is not None:
            return existing
        from nexus_runtime import PyKernel

        kernel = PyKernel()
        kernel.set_metastore_path(redb_path)
        _KERNEL_CACHE[redb_path] = kernel
        return kernel


def _evict_kernel_cache(kernel: Any) -> None:
    """Remove a kernel from the shared cache when its metastore is released.

    Called by ``NexusFS.close()`` right after ``kernel.release_metastores()``
    so subsequent ``SQLiteMetastore(path)`` calls in the same process get a
    fresh kernel that reopens the redb file.
    """
    with _get_cache_lock():
        for path, cached in list(_KERNEL_CACHE.items()):
            if cached is kernel:
                _KERNEL_CACHE.pop(path, None)


def SQLiteMetastore(
    db_path: str | Path, *, _args: Any = None, **_kwargs: Any
) -> RustMetastoreProxy:  # noqa: N802
    """Return a kernel-backed metastore compatible with the old API.

    Args:
        db_path: Path the previous SQLite class wrote to. Rewritten to
            a ``.redb`` sibling so the kernel's redb store opens in
            its own file. Any existing sqlite db at ``db_path`` is
            left untouched.

    Returns:
        A ``RustMetastoreProxy`` backed by a process-shared kernel
        keyed by ``db_path`` (redb file). Multiple proxies pointing
        at the same path share the underlying kernel so redb's
        exclusive-file lock is honoured across threads.
    """
    redb_path = Path(str(db_path)).with_suffix(".redb")
    redb_path.parent.mkdir(parents=True, exist_ok=True)
    redb_str = str(redb_path)
    kernel = _get_or_open_kernel(redb_str)
    # ``RustMetastoreProxy`` calls ``kernel.set_metastore_path`` in its
    # constructor. Passing ``None`` skips that — the path was already
    # wired in ``_get_or_open_kernel``, so a second call would try to
    # reopen the redb file and hit the same lock error we're fixing.
    return RustMetastoreProxy(kernel, None)
