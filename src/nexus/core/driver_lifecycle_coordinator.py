"""DriverLifecycleCoordinator — Python-side unmount-event broadcaster.

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
class only fires the Python ``KernelDispatch`` ``unmount`` event after a
Rust unmount completes so brick hooks (e.g. ``CasLocalBackend._on_unmount``)
get notified — the Rust kernel does not yet have a parallel hook-firing
primitive.

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
from typing import Any

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.core.path_utils import normalize_path

logger = logging.getLogger(__name__)


class DriverLifecycleCoordinator:
    """Python-side unmount-event broadcaster.

    Wraps the Rust kernel's ``DriverLifecycleCoordinator`` (``dlc.rs``)
    only to fire the Python ``KernelDispatch`` ``unmount`` event after a
    Rust unmount completes — the Rust kernel does not yet have a
    parallel hook-firing primitive.

    Routing table, metastore, dcache, and mount existence checks all
    live Rust-side; this class delegates every read/mutation through
    ``self._kernel`` and never caches kernel state.
    """

    __slots__ = (
        "_dispatch",
        "_kernel",
    )

    def __init__(
        self,
        dispatch: Any,
        *,
        kernel: Any,
    ) -> None:
        self._dispatch = dispatch
        self._kernel = kernel

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

        # Check with Rust kernel if mount exists (sys_stat + DT_MOUNT check)
        if self._kernel is not None:
            from nexus.contracts.metadata import DT_MOUNT

            try:
                stat = self._kernel.sys_stat(normalized, zone_id)
                if stat.get("entry_type") != DT_MOUNT:
                    return False
            except Exception:
                return False

        # Fire unmount event BEFORE removing state
        try:
            self._dispatch.dispatch_event("unmount", normalized)
        except Exception as exc:
            logger.warning("[DRIVER] on_unmount notification failed for %s: %s", normalized, exc)

        # Rust DLC handles metastore delete + dcache evict + routing remove
        if self._kernel is not None:
            with contextlib.suppress(Exception):
                from nexus_runtime import PyOperationContext

                self._kernel.sys_unlink(
                    normalized, PyOperationContext(is_system=True, zone_id=zone_id)
                )

        return True

    # ------------------------------------------------------------------
    # Kernel-delegated queries (thin wrappers for backward compat)
    # ------------------------------------------------------------------

    def mount_points(self, zone_id: str | None = None) -> list[str]:
        """Return user-facing mount points (no zone prefix).

        If ``zone_id`` is provided, only mounts in that zone are returned.
        Delegates to Rust kernel ``get_top_level_mounts(zone_id)``.
        """
        if self._kernel is None:
            return []
        mounts = self._kernel.get_top_level_mounts(zone_id or "root")
        return sorted(mounts)
