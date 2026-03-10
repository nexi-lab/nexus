"""ServiceLifecycleCoordinator — bridge between ServiceRegistry and BrickLifecycleManager.

Provides five Linux-inspired verbs for service lifecycle management:

    insmod  → register_service()   — register instance in both Registry and BLM
    mount   → mount_service()      — BLM mount + register VFS hooks
    umount  → unmount_service()    — unregister VFS hooks + BLM unmount
    rmmod   → unregister_service() — remove from both Registry and BLM
    swap    → swap_service()       — atomic replace + drain + hook swap

The coordinator lives at the System Services tier (not kernel) — it composes
kernel-owned ServiceRegistry with system-service-tier BrickLifecycleManager.

Hot-swap flow:
    1. Atomic replace in ServiceRegistry (new calls immediately get new instance)
    2. Drain old instance's refcount → 0 (ServiceRef proxies track in-flight calls)
    3. Unregister old VFS hooks
    4. Register new VFS hooks
    5. BLM state transitions for old (unmount+unregister) and new (register+mount)

Issue #1452 Phase 3.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from nexus.contracts.protocols.service_hooks import HookSpec

if TYPE_CHECKING:
    from nexus.core.kernel_dispatch import KernelDispatch
    from nexus.core.service_registry import ServiceRegistry
    from nexus.system_services.lifecycle.brick_lifecycle import BrickLifecycleManager

logger = logging.getLogger(__name__)

DEFAULT_DRAIN_TIMEOUT: float = 10.0


class ServiceLifecycleCoordinator:
    """Bridges ServiceRegistry + BrickLifecycleManager for unified service lifecycle.

    Kernel stays pure — this coordinator lives at system services tier.
    """

    __slots__ = ("_registry", "_blm", "_dispatch", "_hook_specs")

    def __init__(
        self,
        service_registry: ServiceRegistry,
        lifecycle_manager: BrickLifecycleManager,
        dispatch: KernelDispatch,
    ) -> None:
        self._registry = service_registry
        self._blm = lifecycle_manager
        self._dispatch = dispatch
        self._hook_specs: dict[str, HookSpec] = {}

    # ------------------------------------------------------------------
    # insmod — register service in both Registry and BLM
    # ------------------------------------------------------------------

    def register_service(
        self,
        name: str,
        instance: Any,
        *,
        dependencies: tuple[str, ...] = (),
        exports: tuple[str, ...] = (),
        hook_spec: HookSpec | None = None,
        depends_on: tuple[str, ...] = (),
        protocol_name: str = "",
    ) -> None:
        """Register a service in both ServiceRegistry and BrickLifecycleManager."""
        self._registry.register_service(
            name,
            instance,
            dependencies=dependencies,
            exports=exports,
        )
        self._blm.register(
            name,
            instance,
            protocol_name=protocol_name or type(instance).__name__,
            depends_on=depends_on,
        )
        if hook_spec is not None:
            self._hook_specs[name] = hook_spec
        logger.info(
            "[COORDINATOR] insmod %r (exports=%d, hooks=%d)",
            name,
            len(exports),
            hook_spec.total_hooks if hook_spec else 0,
        )

    # ------------------------------------------------------------------
    # mount — BLM mount + register VFS hooks
    # ------------------------------------------------------------------

    async def mount_service(self, name: str, *, timeout: float = 5.0) -> None:
        """Mount a service: BLM state → ACTIVE, then register VFS hooks."""
        await self._blm.mount(name, timeout=timeout)

        from nexus.contracts.protocols.brick_lifecycle import BrickState

        status = self._blm.get_status(name)
        if status is not None and status.state == BrickState.ACTIVE:
            self._register_hooks(name)
            logger.info("[COORDINATOR] mount %r — hooks registered", name)
        else:
            logger.warning("[COORDINATOR] mount %r — BLM not ACTIVE, hooks skipped", name)

    # ------------------------------------------------------------------
    # umount — unregister VFS hooks + BLM unmount
    # ------------------------------------------------------------------

    async def unmount_service(self, name: str) -> None:
        """Unmount: unregister VFS hooks, then BLM unmount."""
        self._unregister_hooks(name)
        await self._blm.unmount(name)
        logger.info("[COORDINATOR] umount %r", name)

    # ------------------------------------------------------------------
    # rmmod — unregister from both Registry and BLM
    # ------------------------------------------------------------------

    async def unregister_service(self, name: str) -> None:
        """Fully remove a service: unmount if active, then unregister from both."""
        from nexus.contracts.protocols.brick_lifecycle import BrickState

        status = self._blm.get_status(name)
        if status is not None and status.state == BrickState.ACTIVE:
            await self.unmount_service(name)

        # BLM: UNMOUNTED → UNREGISTERED
        status = self._blm.get_status(name)
        if status is not None and status.state == BrickState.UNMOUNTED:
            await self._blm.unregister(name)

        # ServiceRegistry: remove (with dependency guard)
        self._registry.unregister_service(name)
        self._hook_specs.pop(name, None)
        logger.info("[COORDINATOR] rmmod %r", name)

    # ------------------------------------------------------------------
    # swap — atomic replace + drain + hook swap (the hot-swap verb)
    # ------------------------------------------------------------------

    async def swap_service(
        self,
        name: str,
        new_instance: Any,
        *,
        exports: tuple[str, ...] = (),
        hook_spec: HookSpec | None = None,
        timeout: float = 5.0,
        drain_timeout: float = DEFAULT_DRAIN_TIMEOUT,
    ) -> None:
        """Hot-swap a service: atomic replace → drain → hook swap → BLM cycle.

        1. Atomic replace in ServiceRegistry (zero gap — new calls get new instance)
        2. Drain old refcount (wait for in-flight calls on old instance to complete)
        3. Unregister old VFS hooks
        4. Cycle BLM for old instance (unmount + unregister)
        5. Register + mount new instance in BLM
        6. Register new VFS hooks
        """
        # Snapshot old state
        old_spec = self._blm.get_spec(name)
        old_hook_spec = self._hook_specs.get(name)

        # Step 1: Atomic replace — nx.service(name) now returns new instance
        self._registry.replace_service(name, new_instance, exports=exports)
        logger.info("[COORDINATOR] swap %r — atomic replace done", name)

        # Step 2: Drain old instance (wait for in-flight ServiceRef calls)
        await self._drain(name, timeout=drain_timeout)

        # Step 3: Unregister old hooks
        if old_hook_spec is not None:
            self._unregister_hooks_for_spec(old_hook_spec)

        # Step 4: BLM cycle for old — unmount + unregister
        from nexus.contracts.protocols.brick_lifecycle import BrickState

        status = self._blm.get_status(name)
        if status is not None and status.state == BrickState.ACTIVE:
            await self._blm.unmount(name)
        status = self._blm.get_status(name)
        if status is not None and status.state == BrickState.UNMOUNTED:
            await self._blm.unregister(name)

        # Step 5: BLM register + mount new
        self._blm.register(
            name,
            new_instance,
            protocol_name=old_spec.protocol_name if old_spec else type(new_instance).__name__,
            depends_on=old_spec.depends_on if old_spec else (),
        )
        await self._blm.mount(name, timeout=timeout)

        # Step 6: Register new hooks
        if hook_spec is not None:
            self._hook_specs[name] = hook_spec
        elif name in self._hook_specs:
            del self._hook_specs[name]

        status = self._blm.get_status(name)
        if status is not None and status.state == BrickState.ACTIVE:
            self._register_hooks(name)

        logger.info("[COORDINATOR] swap %r — complete", name)

    # ------------------------------------------------------------------
    # Hook spec management (retroactive spec capture for existing services)
    # ------------------------------------------------------------------

    def set_hook_spec(self, name: str, spec: HookSpec) -> None:
        """Set/replace the HookSpec for a service (retroactive capture)."""
        self._hook_specs[name] = spec

    def get_hook_spec(self, name: str) -> HookSpec | None:
        """Return the HookSpec for a service, or None."""
        return self._hook_specs.get(name)

    # ------------------------------------------------------------------
    # Drain — wait for refcount → 0
    # ------------------------------------------------------------------

    async def _drain(self, name: str, *, timeout: float) -> None:
        """Wait for all in-flight calls on *name* to complete (refcount → 0)."""
        refcounts = self._registry._refcounts
        drain_events = self._registry._drain_events

        current = refcounts.get(name, 0)
        if current <= 0:
            return

        evt = asyncio.Event()
        drain_events[name] = evt
        try:
            await asyncio.wait_for(evt.wait(), timeout=timeout)
            logger.debug("[COORDINATOR] drain %r — completed normally", name)
        except TimeoutError:
            remaining = refcounts.get(name, 0)
            logger.warning(
                "[COORDINATOR] drain %r — timed out after %.1fs (%d in-flight calls remaining)",
                name,
                timeout,
                remaining,
            )
        finally:
            drain_events.pop(name, None)

    # ------------------------------------------------------------------
    # Internal hook register/unregister
    # ------------------------------------------------------------------

    def _register_hooks(self, name: str) -> None:
        spec = self._hook_specs.get(name)
        if spec is None or spec.is_empty:
            return
        self._register_hooks_for_spec(spec)

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
        for h in spec.mkdir_hooks:
            d.register_intercept_mkdir(h)
        for h in spec.rmdir_hooks:
            d.register_intercept_rmdir(h)
        for h in spec.observers:
            d.register_observe(h)

    def _unregister_hooks(self, name: str) -> None:
        spec = self._hook_specs.get(name)
        if spec is None or spec.is_empty:
            return
        self._unregister_hooks_for_spec(spec)

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
        for h in spec.mkdir_hooks:
            d.unregister_intercept_mkdir(h)
        for h in spec.rmdir_hooks:
            d.unregister_intercept_rmdir(h)
        for h in spec.observers:
            d.unregister_observe(h)
