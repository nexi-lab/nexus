"""``DictMetastore`` compatibility factory (F3 C5).

Historical note — this module used to provide a JSON-backed
``MetastoreABC`` subclass as the no-Rust fallback for quickstarts and
SDK checkouts without a Rust toolchain. The kernel-backed metastore
(F3 C1–C4) replaces it: every write goes through
``kernel.metastore_put`` which persists into redb (when a path is
supplied) or into the kernel's built-in ``MemoryMetastore`` (when
none is).

This file preserves the ``DictMetastore`` import path as a thin
factory **function** — not a class — that returns a
``RustMetastoreProxy`` wired to a fresh bare ``Kernel``. When the
caller passes a ``storage_path`` argument the path is rewritten to a
``.redb`` sibling and handed to ``kernel.set_metastore_path``, so
round-trip-after-close tests keep working on redb's durable format.
The previous class body (``_store`` dict, JSON flush, etc.) is gone.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from nexus.core.metastore import RustMetastoreProxy


def DictMetastore(  # noqa: N802 — compat factory name
    storage_path: str | Path | None = None,
    *_args: Any,
    **_kwargs: Any,
) -> RustMetastoreProxy:
    """Return a kernel-backed metastore compatible with the old API.

    Args:
        storage_path: Optional on-disk path for durability across
            reopens. The ``.json`` / ``.db`` extension (if any) is
            rewritten to ``.redb`` so the kernel opens its own redb
            database at a sibling location — the previous JSON file
            is left untouched. When ``None``, the returned proxy uses
            the kernel's built-in in-memory metastore (no durability).

    Returns:
        A fresh ``RustMetastoreProxy`` with an exclusively owned
        ``Kernel``. Callers that want to share a kernel across
        multiple proxies should build ``RustMetastoreProxy`` directly.
    """
    from nexus_kernel import Kernel

    kernel = Kernel()
    if storage_path is not None:
        path = Path(str(storage_path))
        redb_path = path.with_suffix(".redb")
        redb_path.parent.mkdir(parents=True, exist_ok=True)
        kernel.set_metastore_path(str(redb_path))
    return RustMetastoreProxy(kernel)
