"""DriverLifecycleCoordinator — Python-side mount-event broadcaster.

Linux analogue: ``udev`` listening to kernel uevents.  This coordinator
does NOT own a routing table, a metastore, or a dcache — those live in
the Rust kernel:

    routing table → ``rust/kernel/src/core/vfs_router.rs::VFSRouter``
                     (`entries: DashMap<canonical_key, MountEntry>`)
    metastore     → ``Kernel::metastore`` (global default) +
                     per-mount ``MountEntry::metastore``
    dcache        → ``Kernel::dcache`` (a kernel primitive; not part of
                     MetaStore — exposed via ``Kernel::dcache_arc()`` so
                     federation install hooks can wire invalidation)

The Rust ``DriverLifecycleCoordinator`` (``rust/kernel/src/core/dlc.rs``)
threads mount mutations into those kernel-owned tables.  This Python
class only:
    1. Generates canonical backend keys for AuthProfile lookup (via
       ``backend_key()``) — federated nodes append ``@self_address``.
    2. Fires the Python ``KernelDispatch`` ``unmount`` event so brick
       hooks (e.g. ``CasLocalBackend._on_unmount``) get notified — the
       Rust kernel does not yet have a parallel hook-firing primitive.

Once the Rust kernel grows an unmount-hook firing primitive, this whole
class becomes deletable.  Until then it stays as the smallest possible
event-bus shim.

Kernel-owned: created in ``NexusFS.__init__()`` (like ServiceRegistry).
Always available after kernel construction.

Issue #1811, #1320, #3584.
"""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING, Any

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.core.path_utils import extract_zone_id, normalize_path

if TYPE_CHECKING:
    from nexus.core.object_store import ObjectStoreABC
    from nexus.remote.rpc_transport import RPCTransportPool

logger = logging.getLogger(__name__)


class DriverLifecycleCoordinator:
    """Python-side mount-event broadcaster.

    Wraps the Rust kernel's ``DriverLifecycleCoordinator`` (``dlc.rs``)
    only to fire the Python ``KernelDispatch`` ``unmount`` event after a
    Rust unmount completes — the Rust kernel does not yet have a
    parallel hook-firing primitive.  ``backend_key()`` generates the
    canonical backend key (with optional federated ``@self_address``
    suffix) for AuthProfile resolution.

    Routing table, metastore, dcache, and mount existence checks all
    live Rust-side; this class delegates every read/mutation through
    ``self._kernel`` and never caches kernel state.
    """

    __slots__ = (
        "_dispatch",
        "_kernel",
        "_self_address",
        "_transport_pool",
    )

    def __init__(
        self,
        dispatch: Any,
        *,
        kernel: Any,
        self_address: str | None = None,
        transport_pool: "RPCTransportPool | None" = None,
    ) -> None:
        self._dispatch = dispatch
        self._kernel = kernel
        self._self_address: str | None = self_address
        self._transport_pool: RPCTransportPool | None = transport_pool

    # ------------------------------------------------------------------
    # Backend key helpers
    # ------------------------------------------------------------------

    def backend_key(self, backend: "ObjectStoreABC", mount_point: str = "") -> str:
        """Canonical key for a backend.

        Format: ``name`` for single-mount, ``name:mount_point`` when a
        mount_point is given and differs from ``/``.  Federated nodes append
        ``@self_address``.
        """
        base = backend.name
        if mount_point and mount_point != "/":
            base = f"{backend.name}:{mount_point}"
        return f"{base}@{self._self_address}" if self._self_address else base

    # ------------------------------------------------------------------
    # Mount / unmount
    # ------------------------------------------------------------------

    def unmount(self, mount_point: str, zone_id: str = ROOT_ZONE_ID) -> bool:
        """Unmount: notify + Rust DLC unmount + remove Python bookkeeping.

        Returns True if mount was removed, False if not found.
        """
        try:
            normalized = normalize_path(mount_point)
        except ValueError:
            return False

        # Check with Rust kernel if mount exists
        if self._kernel is not None and not self._kernel.has_mount(normalized, zone_id):
            return False

        # Fire unmount event BEFORE removing state
        try:
            self._dispatch.dispatch_event("unmount", normalized)
        except Exception as exc:
            logger.warning("[DRIVER] on_unmount notification failed for %s: %s", normalized, exc)

        # Rust DLC handles metastore delete + dcache evict + routing remove
        if self._kernel is not None:
            with contextlib.suppress(Exception):
                self._kernel.kernel_unmount(normalized, zone_id)

        return True

    # ------------------------------------------------------------------
    # Kernel-delegated queries (thin wrappers for backward compat)
    # ------------------------------------------------------------------

    def mount_points(self, zone_id: str | None = None) -> list[str]:
        """Return user-facing mount points (no zone prefix).

        If ``zone_id`` is provided, only mounts in that zone are returned.
        Delegates to Rust kernel ``get_mount_points()``.
        """
        if self._kernel is None:
            return []
        result: list[str] = []
        for canonical in self._kernel.get_mount_points():
            z, user_mp = extract_zone_id(canonical)
            if zone_id is not None and z != zone_id:
                continue
            result.append(user_mp)
        return sorted(result)
