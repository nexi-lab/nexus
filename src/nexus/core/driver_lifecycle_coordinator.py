"""DriverLifecycleCoordinator — kernel primitive for driver mount lifecycle.

Linux analogue: ``register_filesystem()`` + ``kern_mount()`` + ``kill_sb()``.
Orthogonal to ``ServiceRegistry`` lifecycle orchestration (services vs drivers).

Services have singleton cardinality and are boot-triggered.
Drivers have N-per-type cardinality and are mount-triggered.

Responsibilities:
    1. Add/remove backend in kernel MountTable (via ``PyKernel.add_mount``)
    2. Register/unregister backend's hook_spec with KernelDispatch
    3. Broadcast mount/unmount events via KernelDispatch hooks
    4. Own a Python-side map of ``_PyMountInfo`` records for fields the
       Rust kernel does not track (connector
       backend refs) — the kernel is the single source of truth for
       routing (F2 MountTable migration).

Kernel-owned: created in ``NexusFS.__init__()`` (like ServiceRegistry).
Always available after kernel construction.

Issue #1811, #1320, #3584.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.core.path_utils import canonicalize_path, extract_zone_id, normalize_path

if TYPE_CHECKING:
    from nexus.core.object_store import ObjectStoreABC
    from nexus.remote.rpc_transport import RPCTransportPool

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _PyMountInfo:
    """Python-only fields for a mount.

    The kernel (``PyKernel``) is the single source of truth for routing —
    it owns mount points and backend adapters plus per-zone metastores.
    This dataclass keeps the Python-only references the kernel does not
    carry: connector backend objects (some backends are still Python)
    and the zone id of the mount.
    """

    backend: "ObjectStoreABC"
    zone_id: str
    is_external: bool = False


class DriverLifecycleCoordinator:
    """Kernel primitive: driver mount lifecycle (Python bookkeeping).

    Rust DLC (``dlc.rs``) owns routing table + metastore + dcache.
    Python DLC stores ``_PyMountInfo`` (backend refs) + dispatches events.

    Parallel to ServiceRegistry lifecycle orchestration (services vs drivers).
    """

    __slots__ = (
        "_mounts",
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
        self._mounts: dict[str, _PyMountInfo] = {}
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

    def resolve_backend(self, backend_name: str) -> "ObjectStoreABC":
        """Resolve backend by scanning the DLC mount map.

        Local name → scan mounts for a matching ``backend.name``.
        Remote origin → lazy-create RemoteBackend via transport pool.
        """
        from nexus.contracts.backend_address import BackendAddress

        bare_name = backend_name.split(":")[0] if ":" in backend_name else backend_name
        bare_name = bare_name.split("@")[0] if "@" in bare_name else bare_name
        for info in self._mounts.values():
            if info.backend.name == bare_name:
                return info.backend

        addr = BackendAddress.parse(backend_name)
        if addr.has_origin:
            origin = addr.origins[0]
            if self._transport_pool is None:
                raise KeyError(f"Cannot create RemoteBackend for '{origin}': no transport pool")
            from nexus.backends.storage.remote import RemoteBackend

            transport = self._transport_pool.get(origin)
            return RemoteBackend(transport)

        raise KeyError(f"Backend '{backend_name}' not found in mount table")

    # ------------------------------------------------------------------
    # Mount / unmount
    # ------------------------------------------------------------------

    def _store_mount_info(
        self,
        mount_point: str,
        backend: "ObjectStoreABC",
        *,
        zone_id: str = ROOT_ZONE_ID,
        is_external: bool = False,
    ) -> None:
        """Store Python-side mount info + dispatch mount event.

        Kernel-side wiring (routing table, metastore, dcache, lock manager)
        is handled by Rust ``Kernel::sys_setattr(DT_MOUNT)`` before this
        method is called.
        """
        normalized = normalize_path(mount_point)
        canonical = canonicalize_path(normalized, zone_id)

        info = _PyMountInfo(
            backend=backend,
            zone_id=zone_id,
            is_external=is_external,
        )
        self._mounts[canonical] = info
        self._dispatch.dispatch_event("mount", normalized)

    def unmount(self, mount_point: str, zone_id: str = ROOT_ZONE_ID) -> bool:
        """Unmount: notify + Rust DLC unmount + remove Python bookkeeping.

        Returns True if mount was removed, False if not found.
        """
        try:
            normalized = normalize_path(mount_point)
        except ValueError:
            return False
        canonical = canonicalize_path(normalized, zone_id)
        if canonical not in self._mounts:
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

        del self._mounts[canonical]
        return True

    # ------------------------------------------------------------------
    # DLC read helpers (used by PathRouter, zone_manager, etc.)
    # ------------------------------------------------------------------

    def get_mount_info(
        self, mount_point: str, zone_id: str = ROOT_ZONE_ID
    ) -> "_PyMountInfo | None":
        """Return the ``_PyMountInfo`` for an exact mount point, or None."""
        try:
            normalized = normalize_path(mount_point)
        except ValueError:
            return None
        canonical = canonicalize_path(normalized, zone_id)
        return self._mounts.get(canonical)

    def get_mount_info_canonical(self, canonical: str) -> "_PyMountInfo | None":
        """Direct lookup by canonical key (``/{zone_id}{mount_point}``)."""
        return self._mounts.get(canonical)

    def list_mounts(self) -> "list[tuple[str, _PyMountInfo]]":
        """Return all ``(canonical_key, _PyMountInfo)`` pairs."""
        return list(self._mounts.items())

    def get_root_backend(self, zone_id: str = ROOT_ZONE_ID) -> "ObjectStoreABC | None":
        """Return the backend mounted at ``/`` for the given zone, or None."""
        info = self.get_mount_info("/", zone_id)
        return info.backend if info is not None else None

    def mount_points(self, zone_id: str | None = None) -> list[str]:
        """Return user-facing mount points (no zone prefix).

        If ``zone_id`` is provided, only mounts in that zone are returned.
        """
        result: list[str] = []
        for canonical in self._mounts:
            z, user_mp = extract_zone_id(canonical)
            if zone_id is not None and z != zone_id:
                continue
            result.append(user_mp)
        return sorted(result)
