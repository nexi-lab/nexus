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
       Rust kernel does not track (stream_backend_factory, connector
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
from nexus.contracts.protocols.service_hooks import HookSpec
from nexus.core.path_utils import canonicalize_path, extract_zone_id, normalize_path

if TYPE_CHECKING:
    from nexus.core.metastore import MetastoreABC
    from nexus.core.object_store import ObjectStoreABC
    from nexus.remote.rpc_transport import RPCTransportPool

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _PyMountInfo:
    """Python-only fields for a mount.

    The kernel (``PyKernel``) owns mount points and backend adapters. The
    Python side additionally tracks the per-mount ``MetastoreABC`` (e.g.
    a ``RaftMetadataStore`` for federation zones) so ``PathRouter.route``
    can populate ``RouteResult.metastore`` with the target zone's store
    instead of a single global one. Without this, writes land in the
    root-zone metastore regardless of which federation zone the path
    targets — cross-node replication silently misses.
    """

    backend: "ObjectStoreABC"
    metastore: "MetastoreABC"
    readonly: bool
    admin_only: bool
    io_profile: str
    stream_backend_factory: Any
    zone_id: str


class DriverLifecycleCoordinator:
    """Kernel primitive: driver mount lifecycle.

    Manages driver mount lifecycle: routing table + VFS hook registration
    + mount/unmount notification via KernelDispatch.

    Parallel to ServiceRegistry lifecycle orchestration (services vs drivers).
    """

    __slots__ = (
        "_mounts",
        "_dispatch",
        "_kernel",
        "_mount_specs",
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
        self._mount_specs: dict[str, HookSpec] = {}
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

    def mount(
        self,
        mount_point: str,
        backend: "ObjectStoreABC",
        *,
        metastore: "MetastoreABC | None" = None,
        readonly: bool = False,
        admin_only: bool = False,
        io_profile: str = "balanced",
        stream_backend_factory: Any = None,
        zone_id: str = ROOT_ZONE_ID,
    ) -> None:
        """Mount a backend with full lifecycle: routing + hooks + notification.

        Records a ``_PyMountInfo`` in the DLC map, registers the mount in
        the Rust kernel via ``add_mount``, attaches a ``ZoneMetastore`` for
        federation zones, registers VFS hooks, and dispatches a mount event.
        """
        normalized = normalize_path(mount_point)
        canonical = canonicalize_path(normalized, zone_id)

        # Federation mounts pass the target zone's RaftMetadataStore via
        # ``metastore=``; standalone mounts don't pass one and fall back to
        # the kernel's global metastore (which the caller already wired into
        # NexusFS.metadata). Cache the per-mount store so PathRouter.route
        # can return it on RouteResult — otherwise every federation write
        # hits the root-zone store and never replicates.
        effective_metastore = metastore if metastore is not None else self._dispatch.metadata

        info = _PyMountInfo(
            backend=backend,
            metastore=effective_metastore,
            readonly=readonly,
            admin_only=admin_only,
            io_profile=io_profile,
            stream_backend_factory=stream_backend_factory,
            zone_id=zone_id,
        )
        self._mounts[canonical] = info

        _backend_name = backend.name if isinstance(backend.name, str) else str(backend.name)
        # CAS-local detection: Rust takes ownership of the backend natively.
        _is_cas_local = getattr(backend, "has_root_path", False) and type(
            backend
        ).__name__.startswith("CAS")
        _local_root = str(getattr(backend, "root_path", None)) if _is_cas_local else None

        # Standalone metastore: hand Rust the redb path so it constructs its
        # own Metastore. Federation metastores attach below via
        # PyZoneHandle.attach_to_kernel_mount.
        _ms_path = getattr(metastore, "_redb_path", None) if metastore is not None else None

        if self._kernel is not None:
            with contextlib.suppress(Exception):
                self._kernel.add_mount(
                    normalized,
                    zone_id,
                    readonly,
                    admin_only,
                    io_profile,
                    _backend_name,
                    _local_root,
                    True,  # fsync
                    py_backend=backend,
                    metastore_path=str(_ms_path) if _ms_path else None,
                )

            # Federation hook — wire per-zone ZoneMetastore into the kernel.
            if metastore is not None:
                engine = getattr(metastore, "_engine", None)
                if engine is not None and hasattr(engine, "attach_to_kernel_mount"):
                    try:
                        engine.attach_to_kernel_mount(self._kernel, normalized, zone_id)
                    except Exception as exc:  # pragma: no cover — logged
                        logger.warning(
                            "[DRIVER] attach_to_kernel_mount failed for %s (zone=%s): %s",
                            normalized,
                            zone_id,
                            exc,
                        )

        self._register_backend_hooks(normalized, backend)
        self._dispatch.dispatch_event("mount", normalized)

    def unmount(self, mount_point: str, zone_id: str = ROOT_ZONE_ID) -> bool:
        """Unmount with full lifecycle: unhook + notify + remove.

        Returns True if mount was removed, False if not found.
        """
        try:
            normalized = normalize_path(mount_point)
        except ValueError:
            return False
        canonical = canonicalize_path(normalized, zone_id)
        if canonical not in self._mounts:
            return False

        # Fire unmount event BEFORE unregistering hooks (observers must still be active)
        try:
            self._dispatch.dispatch_event("unmount", normalized)
        except Exception as exc:
            logger.warning("[DRIVER] on_unmount notification failed for %s: %s", normalized, exc)

        spec = self._mount_specs.pop(normalized, None)
        if spec is not None:
            self._unregister_hooks_for_spec(spec)

        if self._kernel is not None:
            with contextlib.suppress(Exception):
                self._kernel.kernel_unmount(normalized, zone_id)
            with contextlib.suppress(Exception):
                self._kernel.remove_mount(normalized, zone_id)

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

    # ------------------------------------------------------------------
    # Internal hook registration
    # ------------------------------------------------------------------

    def _register_backend_hooks(self, mount_point: str, backend: "ObjectStoreABC") -> None:
        if not hasattr(backend, "hook_spec"):
            return
        spec: HookSpec = backend.hook_spec()
        if spec is None or spec.is_empty:
            return
        self._mount_specs[mount_point] = spec
        self._register_hooks_for_spec(spec)
        logger.debug("[DRIVER] registered %d hooks for mount %s", spec.total_hooks, mount_point)

    def _register_hooks_for_spec(self, spec: HookSpec) -> None:
        d = self._dispatch
        for h in spec.resolvers:
            d.register_resolver(h)
        for h in spec.read_hooks:
            d.register_intercept_read(h)
        for h in spec.write_hooks:
            d.register_intercept_write(h)
        for h in spec.write_batch_hooks:
            d.register_intercept_write_batch(h)
        for h in spec.delete_hooks:
            d.register_intercept_delete(h)
        for h in spec.rename_hooks:
            d.register_intercept_rename(h)
        for h in spec.copy_hooks:
            d.register_intercept_copy(h)
        for h in spec.mkdir_hooks:
            d.register_intercept_mkdir(h)
        for h in spec.rmdir_hooks:
            d.register_intercept_rmdir(h)
        # spec.observers: no-op — observer dispatch is fully Rust-native.

    def _unregister_hooks_for_spec(self, spec: HookSpec) -> None:
        d = self._dispatch
        for h in spec.resolvers:
            d.unregister_resolver(h)
        for h in spec.read_hooks:
            d.unregister_intercept_read(h)
        for h in spec.write_hooks:
            d.unregister_intercept_write(h)
        for h in spec.write_batch_hooks:
            d.unregister_intercept_write_batch(h)
        for h in spec.delete_hooks:
            d.unregister_intercept_delete(h)
        for h in spec.rename_hooks:
            d.unregister_intercept_rename(h)
        for h in spec.copy_hooks:
            d.unregister_intercept_copy(h)
        for h in spec.mkdir_hooks:
            d.unregister_intercept_mkdir(h)
        for h in spec.rmdir_hooks:
            d.unregister_intercept_rmdir(h)
        # spec.observers: no-op — observer dispatch is fully Rust-native.
