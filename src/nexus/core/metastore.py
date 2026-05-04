"""``RustMetastoreProxy`` — minimal compat shim around a kernel handle.

After the post-DCache MetaStore SSOT cleanup (commits R → W2) every
behaviour that used to live on the Python proxy moved into one of two
places:

* ``kernel.metastore_*`` PyO3 bindings — for the 1:1 forwards
  (``get`` / ``put`` / ``exists`` / ``delete`` / ``rename_path`` /
  ``put_if_version`` / ``is_implicit_directory`` /
  ``get_file_metadata{,_bulk}`` / ``metastore_get_batch`` /
  ``metastore_put_batch``).
* ``nexus.kernel_helpers`` free functions — for the five non-trivial
  wrappers (JSON-encoded ``set_file_metadata`` / ``list`` and
  ``list_iter`` recursive=False post-filter / ``list_paginated`` dataclass
  wrap / ``get_searchable_text_bulk`` None-filter / parsed_text warning
  shim).

W3a: this file shrinks ``RustMetastoreProxy`` to a 2-attribute compat
shim. Constructors that still accept ``metadata_store: RustMetastoreProxy``
read ``proxy._rust_kernel`` to reach the kernel; the redb-path
side-effect inside ``__init__`` keeps ``nexus.connect()`` / factory
boot wiring working unchanged. W3b will rip the parameter out entirely
and have callers receive a ``PyKernel`` directly, but that's a
contract change that touches ~50 service constructors and is
deliberately deferred.

The previous deep proxy (~250 LOC of pass-through methods) is gone —
no caller in the codebase reads any method other than
``_rust_kernel`` after W2. The shim is verifiably equivalent because
every removed method was a 1:1 forward (or a wrapper that already
moved to ``kernel_helpers``).
"""

from __future__ import annotations

from typing import Any


class RustMetastoreProxy:
    """Compat shim — holds a ``PyKernel`` reference for legacy callers.

    Constructed by ``nexus._open_local_metastore`` and the factory
    boot path; passed into services that still take a
    ``metadata_store=`` parameter. Internally, every consumer reads
    ``self._rust_kernel`` and calls the kernel directly.

    Args:
        kernel: a ``nexus_runtime.PyKernel`` instance.
        redb_path: when not ``None``, calls
            ``kernel.set_metastore_path(redb_path)`` so the kernel
            opens the redb file. Pass ``None`` for federation mode
            (per-mount ``ZoneMetastore`` handles routing).
    """

    __slots__ = ("_rust_kernel",)

    def __init__(self, kernel: Any, redb_path: str | None = None, /) -> None:
        self._rust_kernel = kernel
        if redb_path is not None:
            kernel.set_metastore_path(redb_path)
