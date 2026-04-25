"""DriverLifecycleCoordinator — kernel primitive for driver mount lifecycle.

Linux analogue: ``register_filesystem()`` + ``kern_mount()`` + ``kill_sb()``.
Orthogonal to ``ServiceRegistry`` lifecycle orchestration (services vs drivers).

Services have singleton cardinality and are boot-triggered.
Drivers have N-per-type cardinality and are mount-triggered.

Responsibilities:
    1. Add/remove backend in kernel MountTable (via ``PyKernel.add_mount``)
    2. Register/unregister backend's hook_spec with KernelDispatch
    3. Broadcast mount/unmount events via KernelDispatch hooks

The Rust kernel is the single source of truth for routing, mount existence,
backend ownership, and metadata.  This Python-side coordinator is a thin
bookkeeping layer for lifecycle events (mount/unmount dispatch).

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
    """Kernel primitive: driver mount lifecycle (Python bookkeeping).

    Rust DLC (``dlc.rs``) owns routing table + metastore + dcache.
    Python DLC dispatches mount/unmount events.

    Parallel to ServiceRegistry lifecycle orchestration (services vs drivers).
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

    def resolve_path(self, path: str, zone_id: str = ROOT_ZONE_ID) -> "tuple[str, str, str] | None":
        """Resolve virtual path → (backend_name, backend_path, mount_point).

        Delegates LPM to Rust VFSRouter (kernel-internal).

        Returns None when the path is not covered by any mount (e.g.
        IPC pipe/stream paths) or when no Rust kernel is available.

        Note: Returns (backend_name, backend_path, user_mp) NOT a backend
        object.  Callers that need the backend name should use the first
        element.  Callers that need I/O should use kernel syscalls.
        """
        if self._kernel is None:
            return None
        try:
            rr = self._kernel.route(path, zone_id)
        except (ValueError, Exception):
            return None
        user_mp = extract_zone_id(rr.mount_point)[1]
        return (rr.backend_name, rr.backend_path, user_mp)
