"""DriverLifecycleCoordinator — kernel primitive for driver mount lifecycle.

Linux analogue: ``register_filesystem()`` + ``kern_mount()`` + ``kill_sb()``.
Orthogonal to ``ServiceRegistry`` lifecycle orchestration (services vs drivers).

Services have singleton cardinality and are boot-triggered.
Drivers have N-per-type cardinality and are mount-triggered.

Responsibilities:
    1. Add/remove backend in PathRouter (routing table)
    2. Register/unregister backend's hook_spec with KernelDispatch
    3. Broadcast mount/unmount events via KernelDispatch hooks

Kernel-owned: created in ``NexusFS.__init__()`` (like ServiceRegistry).
Always available after kernel construction.

Boot timing:
    NexusFS.__init__()     → creates DriverLifecycleCoordinator
    create_nexus_fs()      → coordinator.mount("/", backend)  # unified lifecycle

Issue #1811, #1320.
"""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING, Any

from nexus.contracts.protocols.service_hooks import HookSpec

if TYPE_CHECKING:
    from nexus.core.metastore import MetastoreABC
    from nexus.core.mount_table import MountTable
    from nexus.core.object_store import ObjectStoreABC
    from nexus.remote.rpc_transport import RPCTransportPool

logger = logging.getLogger(__name__)


class DriverLifecycleCoordinator:
    """Kernel primitive: driver mount lifecycle.

    Manages driver mount lifecycle: routing table + VFS hook registration
    + mount/unmount notification via KernelDispatch.

    Parallel to ServiceRegistry lifecycle orchestration (services vs drivers).
    """

    __slots__ = (
        "_mount_table",
        "_dispatch",
        "_mount_specs",
        "_self_address",
        "_transport_pool",
    )

    def __init__(
        self,
        mount_table: "MountTable",
        dispatch: Any,
        *,
        self_address: str | None = None,
        transport_pool: "RPCTransportPool | None" = None,
    ) -> None:
        self._mount_table = mount_table
        self._dispatch = dispatch
        self._mount_specs: dict[str, HookSpec] = {}
        self._self_address: str | None = self_address
        self._transport_pool: RPCTransportPool | None = transport_pool

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
        """Resolve backend by scanning mount_table entries.

        Local name → scan mount table for matching backend.name.
        Remote origin → lazy-create RemoteBackend via transport pool.

        Raises:
            KeyError: backend_name not found and cannot create remote.
        """
        from nexus.contracts.backend_address import BackendAddress

        # Scan mount table for matching backend
        bare_name = backend_name.split(":")[0] if ":" in backend_name else backend_name
        bare_name = bare_name.split("@")[0] if "@" in bare_name else bare_name
        for entry in self._mount_table._entries.values():
            if entry.backend.name == bare_name:
                return entry.backend

        # Check for remote origin
        addr = BackendAddress.parse(backend_name)
        if addr.has_origin:
            origin = addr.origins[0]
            if self._transport_pool is None:
                raise KeyError(f"Cannot create RemoteBackend for '{origin}': no transport pool")
            from nexus.backends.storage.remote import RemoteBackend

            transport = self._transport_pool.get(origin)
            return RemoteBackend(transport)

        raise KeyError(f"Backend '{backend_name}' not found in mount table")

    def mount(
        self,
        mount_point: str,
        backend: "ObjectStoreABC",
        *,
        metastore: "MetastoreABC | None" = None,
        readonly: bool = False,
        admin_only: bool = False,
        io_profile: str = "balanced",
    ) -> None:
        """Mount a backend with full lifecycle: routing + hooks + notification."""
        self._mount_table.add(
            mount_point,
            backend,
            metastore=metastore,
            readonly=readonly,
            admin_only=admin_only,
            io_profile=io_profile,
        )
        self._register_backend_hooks(mount_point, backend)
        self._dispatch.dispatch_event("mount", mount_point)

    def unmount(self, mount_point: str, zone_id: str = "root") -> bool:
        """Unmount with full lifecycle: unhook + notify + remove.

        Returns True if mount was removed, False if not found.
        """
        entry = self._mount_table.get(mount_point)
        if entry is None:
            return False

        # Fire unmount event BEFORE unregistering hooks (observers must still be active)
        try:
            self._dispatch.dispatch_event("unmount", mount_point)
        except Exception as exc:
            logger.warning("[DRIVER] on_unmount notification failed for %s: %s", mount_point, exc)

        spec = self._mount_specs.pop(mount_point, None)
        if spec is not None:
            self._unregister_hooks_for_spec(spec)

        _kernel = getattr(self._mount_table, "_kernel", None)
        if _kernel is not None:
            with contextlib.suppress(Exception):
                _kernel.kernel_unmount(mount_point, zone_id)
        self._mount_table.remove(mount_point, zone_id)
        return True

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
