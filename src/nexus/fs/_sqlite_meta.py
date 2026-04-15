"""``SQLiteMetastore`` compatibility factory (F3 C4).

Historical note — this module used to implement a full stdlib-only
``MetastoreABC`` subclass backed by a local SQLite file, as the
nexus-fs slim SDK's metadata store for environments that could not
build the Rust kernel. The kernel has since become the single source
of truth for metastore state: every ``nfs.sys_*`` write now funnels
through ``kernel.metastore_put``, so a Python-only SQLite side-store
was invisible to the kernel and silently dropped writes.

This file preserves the ``SQLiteMetastore`` import path as a thin
factory **function** — not a class — that returns a
``RustMetastoreProxy`` wired to a fresh bare ``Kernel`` with its
redb-backed metastore pointed at ``db_path``. The .db suffix is
rewritten to .redb so an existing sqlite file from a previous run is
not accidentally overwritten.

Open question from the F3 plan: whether SQLite remains a public
contract for the slim wheel. For now the public ``SQLiteMetastore``
call shape keeps working; the on-disk format is redb, not sqlite.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from nexus.core.metastore import RustMetastoreProxy

F = TypeVar("F", bound=Callable[..., Any])


def _retry_on_busy(fn: F) -> F:
    """No-op compat shim for the old SQLITE_BUSY retry decorator.

    The previous stdlib-sqlite3 implementation wrapped every mutating
    call in a decorator that retried on ``SQLITE_BUSY`` with
    exponential backoff. The kernel-backed metastore does not contend
    on a process-wide SQLite writer lock, so the decorator is a no-op
    — it is kept only so existing imports (tests/unit/fs/
    test_release_integrity.py) do not break at collection time.
    """
    return fn


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
        A fresh ``RustMetastoreProxy`` with the redb database wired
        into its kernel. The kernel is exclusively owned by this
        proxy; callers that want to share a kernel should build a
        ``RustMetastoreProxy`` directly.
    """
    from nexus_kernel import Kernel

    redb_path = Path(str(db_path)).with_suffix(".redb")
    redb_path.parent.mkdir(parents=True, exist_ok=True)
    kernel = Kernel()
    return RustMetastoreProxy(kernel, str(redb_path))
