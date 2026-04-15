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


def DictMetastore(  # noqa: N802
    storage_path: Any = None,
    *_args: Any,
    **_kwargs: Any,
) -> RustMetastoreProxy:
    """Return a fresh kernel-backed metastore for tests.

    Delegates to the production ``nexus.storage.dict_metastore``
    factory so the test helper and the shipped symbol stay in
    lockstep. The factory signature accepts (and ignores) any extra
    positional / keyword arguments the old class constructor took so
    existing ``DictMetastore()``, ``DictMetastore(storage_path=...)``
    etc. still work without editing call sites.
    """
    from nexus.storage.dict_metastore import DictMetastore as _factory

    return _factory(storage_path)
