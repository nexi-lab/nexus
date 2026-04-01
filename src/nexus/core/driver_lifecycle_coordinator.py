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

import logging
from typing import TYPE_CHECKING

from nexus.contracts.protocols.service_hooks import HookSpec

if TYPE_CHECKING:
    from nexus.core.kernel_dispatch import KernelDispatch
    from nexus.core.metastore import MetastoreABC
    from nexus.core.object_store import ObjectStoreABC
    from nexus.core.router import PathRouter
    from nexus.remote.rpc_transport import RPCTransportPool

logger = logging.getLogger(__name__)


class DriverLifecycleCoordinator:
    """Kernel primitive: driver mount lifecycle.

    Manages driver mount lifecycle: routing table + VFS hook registration
    + mount/unmount notification via KernelDispatch.

    Parallel to ServiceRegistry lifecycle orchestration (services vs drivers).
    """

    __slots__ = (
        "_router",
        "_dispatch",
        "_mount_specs",
        "_backend_pool",
        "_self_address",
        "_transport_pool",
    )

    def __init__(
        self,
        router: "PathRouter",
        dispatch: "KernelDispatch",
        *,
        self_address: str | None = None,
        transport_pool: "RPCTransportPool | None" = None,
    ) -> None:
        self._router = router
        self._dispatch = dispatch
        self._mount_specs: dict[str, HookSpec] = {}
        self._backend_pool: dict[str, ObjectStoreABC] = {}
        self._self_address: str | None = self_address
        self._transport_pool: RPCTransportPool | None = transport_pool

    def backend_key(self, backend: "ObjectStoreABC") -> str:
        """Canonical pool key for a backend: ``name@self_address`` or just ``name``.

        Used by kernel write path to store the correct ``backend_name`` in
        metadata so that read path can resolve it back via ``resolve_backend()``.
        """
        return f"{backend.name}@{self._self_address}" if self._self_address else backend.name

    def register_backend(self, backend: "ObjectStoreABC") -> str:
        """Register a backend in the driver pool. Returns the pool key.

        Key = backend_key(backend). Called automatically on mount().
        """
        key = self.backend_key(backend)
        self._backend_pool[key] = backend
        return key

    def resolve_backend(self, backend_name: str) -> "ObjectStoreABC":
        """Resolve backend from pool by backend_name.

        Pool hit → cached backend (local or RemoteBackend).
        Pool miss + remote origin → lazy-create RemoteBackend, register, return.

        Raises:
            KeyError: backend_name not in pool and cannot create remote.
        """
        cached = self._backend_pool.get(backend_name)
        if cached is not None:
            return cached
        # Pool miss — must be a remote backend we haven't seen yet.
        from nexus.contracts.backend_address import BackendAddress

        addr = BackendAddress.parse(backend_name)
        if not addr.has_origin:
            raise KeyError(f"Backend '{backend_name}' not in pool and has no origin address")
        origin = addr.origins[0]
        if self._transport_pool is None:
            raise KeyError(f"Cannot create RemoteBackend for '{origin}': no transport pool")
        from nexus.backends.storage.remote import RemoteBackend

        transport = self._transport_pool.get(origin)
        remote = RemoteBackend(transport)
        self._backend_pool[backend_name] = remote
        return remote

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
        """Mount a backend with full lifecycle: routing + pool + hooks + notification."""
        # 1. Add to routing table
        self._router.add_mount(
            mount_point,
            backend,
            metastore=metastore,
            readonly=readonly,
            admin_only=admin_only,
            io_profile=io_profile,
        )

        # 2. Register in backend pool
        self.register_backend(backend)

        # 3. Register hook_spec
        self._register_backend_hooks(mount_point, backend)

        # 4. Broadcast mount event
        self._dispatch.notify_mount(mount_point, backend)

    def unmount(self, mount_point: str) -> bool:
        """Unmount with full lifecycle: unhook + notify + remove.

        Returns True if mount was removed, False if not found.
        """
        info = self._router.get_mount(mount_point)
        if info is None:
            return False

        backend = info.backend

        # 1. Unregister VFS hooks
        spec = self._mount_specs.pop(mount_point, None)
        if spec is not None:
            self._unregister_hooks_for_spec(spec)

        # 2. Broadcast unmount event (best-effort)
        try:
            self._dispatch.notify_unmount(mount_point, backend)
        except Exception as exc:
            logger.warning("[DRIVER] on_unmount notification failed for %s: %s", mount_point, exc)

        # 3. Remove from routing table
        self._router.remove_mount(mount_point)
        return True

    # ------------------------------------------------------------------
    # Internal hook registration (mirrors ServiceRegistry lifecycle)
    # ------------------------------------------------------------------

    def _register_backend_hooks(self, mount_point: str, backend: "ObjectStoreABC") -> None:
        """Extract and register hook_spec from backend."""
        if not hasattr(backend, "hook_spec"):
            return

        spec: HookSpec = backend.hook_spec()
        if spec is None or spec.is_empty:
            return

        self._mount_specs[mount_point] = spec
        self._register_hooks_for_spec(spec)
        logger.debug(
            "[DRIVER] registered %d hooks for mount %s",
            spec.total_hooks,
            mount_point,
        )

    def _register_hooks_for_spec(self, spec: HookSpec) -> None:
        """Register all hooks from a HookSpec into KernelDispatch."""
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
        for h in spec.observers:
            d.register_observe(h)
        for h in spec.mount_hooks:
            d.register_mount_hook(h)
        for h in spec.unmount_hooks:
            d.register_unmount_hook(h)

    def _unregister_hooks_for_spec(self, spec: HookSpec) -> None:
        """Unregister all hooks from a HookSpec from KernelDispatch."""
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
        for h in spec.observers:
            d.unregister_observe(h)
        for h in spec.mount_hooks:
            d.unregister_mount_hook(h)
        for h in spec.unmount_hooks:
            d.unregister_unmount_hook(h)


# trigger CI
