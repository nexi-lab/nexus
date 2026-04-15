"""Factory for a kernel-backed in-memory metastore (F3 C4).

Historical note — the previous ``DictMetastore`` was a Python
``MetastoreABC`` subclass that lived outside the Rust kernel's SSOT.
Tests that built ``DictMetastore()`` therefore wrote into a Python
``dict`` the kernel could not see, so ``nfs.sys_write`` (which now
routes all metastore puts through ``kernel.metastore_put``) silently
dropped writes on the floor for anything that subsequently read via
``nfs.metadata.get``.

After F3 C1 the Rust kernel boots with a ``MemoryMetastore`` by
default, and ``RustMetastoreProxy`` is a drop-in ``MetastoreABC``
backed by it. This module keeps the import path ``DictMetastore``
stable for the ~80 test call sites, but it is no longer a class — it
is a thin factory that hands back a ``RustMetastoreProxy`` wired to a
fresh bare kernel. The previous Python implementation is gone; this
file does not define ``class DictMetastore`` and does not inherit
from ``MetastoreABC``.
"""

from __future__ import annotations

from typing import Any

from nexus.core.metastore import RustMetastoreProxy


def DictMetastore(*_args: Any, **_kwargs: Any) -> RustMetastoreProxy:  # noqa: N802
    """Return a fresh kernel-backed metastore for tests.

    The factory signature accepts (and ignores) any positional or
    keyword arguments the old class constructor took, so existing
    ``DictMetastore()``, ``DictMetastore(storage_path=...)`` etc. still
    work without editing call sites. The returned object is a
    ``RustMetastoreProxy`` over a freshly constructed ``Kernel``; the
    kernel's default in-memory metastore persists for the lifetime of
    the proxy.
    """
    from nexus_kernel import Kernel

    return RustMetastoreProxy(Kernel())
