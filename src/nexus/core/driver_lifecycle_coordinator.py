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
    create_nexus_fs()  → router.add_mount("/", backend)   # before __init__
    NexusFS.__init__() → creates DriverLifecycleCoordinator
    _do_link()         → adopt_existing_mount("/")          # retroactive hook_spec

Issue #1811, #1320.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from nexus.contracts.protocols.service_hooks import HookSpec

if TYPE_CHECKING:
    from nexus.core.kernel_dispatch import KernelDispatch
    from nexus.core.router import PathRouter

logger = logging.getLogger(__name__)


class DriverLifecycleCoordinator:
    """Kernel primitive: driver mount lifecycle.

    Manages driver mount lifecycle: routing table + VFS hook registration
    + mount/unmount notification via KernelDispatch.

    Parallel to ServiceRegistry lifecycle orchestration (services vs drivers).
    """

    __slots__ = ("_router", "_dispatch", "_mount_specs")

    def __init__(self, router: "PathRouter", dispatch: "KernelDispatch") -> None:
        self._router = router
        self._dispatch = dispatch
        self._mount_specs: dict[str, HookSpec] = {}

    def mount(
        self,
        mount_point: str,
        backend: Any,
        *,
        readonly: bool = False,
        admin_only: bool = False,
        io_profile: str = "balanced",
    ) -> None:
        """Mount a backend with full lifecycle: routing + hooks + notification.

        1. Add to routing table (PathRouter)
        2. Register VFS hooks from hook_spec (fixes CAS wiring bug #1320)
        3. Broadcast mount event via KernelDispatch
        """
        # 1. Add to routing table
        self._router.add_mount(
            mount_point,
            backend,
            readonly=readonly,
            admin_only=admin_only,
            io_profile=io_profile,
        )

        # 2. Register hook_spec
        self._register_backend_hooks(mount_point, backend)

        # 3. Broadcast mount event
        self._dispatch.notify_mount(mount_point, backend)

    def adopt_existing_mount(self, mount_point: str) -> None:
        """Adopt a backend already in the routing table.

        For mounts that predate coordinator creation (root mount in
        create_nexus_fs).  Registers hook_spec VFS hooks and broadcasts
        mount notification.
        """
        info = self._router.get_mount(mount_point)
        if info is None:
            logger.debug("[DRIVER] adopt_existing_mount(%s): not found", mount_point)
            return

        backend = info.backend

        # Register hook_spec
        self._register_backend_hooks(mount_point, backend)

        # Broadcast mount event
        self._dispatch.notify_mount(mount_point, backend)

        logger.debug(
            "[DRIVER] adopted existing mount %s (backend=%s)",
            mount_point,
            getattr(backend, "name", "?"),
        )

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

    def _register_backend_hooks(self, mount_point: str, backend: Any) -> None:
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
