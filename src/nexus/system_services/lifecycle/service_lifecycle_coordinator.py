"""ServiceLifecycleCoordinator — bridge between ServiceRegistry and BrickLifecycleManager.

Provides five Linux-inspired verbs for service lifecycle management:

    insmod  → register_service()   — register instance in both Registry and BLM
    mount   → mount_service()      — BLM mount + register VFS hooks
    umount  → unmount_service()    — unregister VFS hooks + BLM unmount
    rmmod   → unregister_service() — remove from both Registry and BLM
    swap    → swap_service()       — atomic replace + drain + hook swap

The coordinator lives at the System Services tier (not kernel) — it composes
kernel-owned ServiceRegistry with system-service-tier BrickLifecycleManager.

Hot-swap flow (HotSwappable services only):
    1. Validate old service implements HotSwappable (TypeError if not)
    2. Call old_service.drain() — stop accepting new work
    3. Drain ServiceRef refcount → 0 (wait for in-flight calls)
    4. Unregister old VFS hooks (from old_service.hook_spec())
    5. Atomic replace in ServiceRegistry
    6. BLM state transitions for old (unmount+unregister) and new (register+mount)
    7. Register new VFS hooks (from new_service.hook_spec() if HotSwappable)
    8. Call new_service.activate() if HotSwappable

Static (non-HotSwappable) services cannot be hot-swapped — they raise TypeError.
Use full restart instead.

Issue #1452 Phase 3.
Issue #1577: HotSwappable + PersistentService protocol integration.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from nexus.contracts.protocols.service_hooks import HookSpec
from nexus.contracts.protocols.service_lifecycle import HotSwappable, PersistentService

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

    __slots__ = ("_registry", "_blm", "_dispatch", "_hook_specs", "_hooks_on_dispatch")

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
        # Tracks services whose hooks were pre-registered on dispatch at
        # initialize() time by _enlist_hook().  activate_hot_swappable_services()
        # skips _register_hooks() for these to avoid double registration.
        self._hooks_on_dispatch: set[str] = set()

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
        is_remote: bool = False,
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
            is_remote=is_remote,
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
    # enlist — the ONE entry point for all four quadrants (Issue #1502)
    # ------------------------------------------------------------------

    async def enlist(
        self,
        name: str,
        instance: Any,
        *,
        exports: tuple[str, ...] = (),
        depends_on: tuple[str, ...] = (),
    ) -> None:
        """Enlist a service into the four-quadrant lifecycle system.

        This is the **single entry point** all services must call — the
        "strong label" that marks a service as migrated to the new contract.
        The coordinator auto-detects the quadrant via ``isinstance`` checks:

        - **Q1** (neither protocol): register only — no lifecycle, no hooks.
        - **Q2** (HotSwappable): register + capture ``hook_spec()`` + activate.
        - **Q3** (PersistentService): register + ``start()``.
        - **Q4** (both): register + ``start()`` + capture hooks + activate.

        Used by lifespan startup functions for services created *after*
        ``NexusFS.bootstrap()`` has already been called.  For pre-bootstrap
        factory services, ``register_service()`` + auto-lifecycle at bootstrap
        is the equivalent path.
        """
        self.register_service(name, instance, exports=exports, depends_on=depends_on)

        # Q3 / Q4: auto-start persistent background work
        if isinstance(instance, PersistentService):
            await instance.start()
            logger.info("[COORDINATOR] enlist %r — started (PersistentService)", name)

        # Q2 / Q4: auto-capture hooks and activate
        if isinstance(instance, HotSwappable):
            spec = instance.hook_spec()
            self._hook_specs[name] = spec
            if not spec.is_empty:
                self._register_hooks(name)
            await instance.activate()
            logger.info("[COORDINATOR] enlist %r — activated (HotSwappable)", name)

        if not isinstance(instance, PersistentService) and not isinstance(instance, HotSwappable):
            logger.info("[COORDINATOR] enlist %r — registered (Q1 static)", name)

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
        """Hot-swap a service: validate → drain → hook swap → BLM cycle.

        Only HotSwappable services can be swapped.  Static services raise
        TypeError — use full restart instead.

        Flow for HotSwappable services:
            1. Validate old service is HotSwappable (TypeError if not)
            2. Call old_service.drain() — stop accepting new work
            3. Drain ServiceRef refcount → 0 (in-flight calls complete)
            4. Unregister old VFS hooks (from old hook_spec or old_service.hook_spec())
            5. Atomic replace in ServiceRegistry
            6. BLM cycle for old → new
            7. Register new VFS hooks (from hook_spec param, new_service.hook_spec(), or retroactive)
            8. Call new_service.activate() if HotSwappable

        Args:
            name: Service registry name.
            new_instance: Replacement service instance.
            exports: EXPORT_SYMBOL methods for the new instance.
            hook_spec: Explicit HookSpec override (bypasses protocol auto-detect).
            timeout: BLM mount timeout.
            drain_timeout: Max wait for in-flight calls to complete.

        Raises:
            TypeError: If old service is not HotSwappable.
            KeyError: If service is not registered.
        """
        # --- Resolve old instance ---
        old_info = self._registry.service_info(name)
        if old_info is None:
            raise KeyError(f"swap_service: {name!r} not registered")
        old_instance = old_info.instance

        # --- Guard: only HotSwappable services can be swapped ---
        if not isinstance(old_instance, HotSwappable):
            raise TypeError(
                f"swap_service: {name!r} ({type(old_instance).__name__}) is not HotSwappable. "
                f"Static services cannot be hot-swapped — use full restart instead."
            )

        # Snapshot old state
        old_blm_spec = self._blm.get_spec(name)

        # Resolve old hook spec: explicit retroactive > protocol auto-detect
        old_hook_spec = self._hook_specs.get(name)
        if old_hook_spec is None:
            old_hook_spec = old_instance.hook_spec()
            if old_hook_spec is not None and not old_hook_spec.is_empty:
                self._hook_specs[name] = old_hook_spec

        # Step 1: Drain old service (service-internal cleanup)
        await old_instance.drain()
        logger.debug("[COORDINATOR] swap %r — old service drained", name)

        # Step 2: Drain ServiceRef refcount (wait for in-flight calls)
        await self._drain(name, timeout=drain_timeout)

        # Step 3: Unregister old hooks
        if old_hook_spec is not None:
            self._unregister_hooks_for_spec(old_hook_spec)

        # Step 4: Atomic replace — nx.service(name) now returns new instance
        self._registry.replace_service(name, new_instance, exports=exports)
        logger.info("[COORDINATOR] swap %r — atomic replace done", name)

        # Step 5: BLM cycle for old → new
        from nexus.contracts.protocols.brick_lifecycle import BrickState

        status = self._blm.get_status(name)
        if status is not None and status.state == BrickState.ACTIVE:
            await self._blm.unmount(name)
        status = self._blm.get_status(name)
        if status is not None and status.state == BrickState.UNMOUNTED:
            await self._blm.unregister(name)

        self._blm.register(
            name,
            new_instance,
            protocol_name=old_blm_spec.protocol_name
            if old_blm_spec
            else type(new_instance).__name__,
            depends_on=old_blm_spec.depends_on if old_blm_spec else (),
        )
        await self._blm.mount(name, timeout=timeout)

        # Step 6: Register new hooks — explicit param > protocol > clear
        new_hook_spec = hook_spec
        if new_hook_spec is None and isinstance(new_instance, HotSwappable):
            new_hook_spec = new_instance.hook_spec()

        if new_hook_spec is not None and not new_hook_spec.is_empty:
            self._hook_specs[name] = new_hook_spec
        elif name in self._hook_specs:
            del self._hook_specs[name]

        status = self._blm.get_status(name)
        if status is not None and status.state == BrickState.ACTIVE:
            self._register_hooks(name)

        # Step 7: Activate new service if HotSwappable
        if isinstance(new_instance, HotSwappable):
            await new_instance.activate()

        logger.info("[COORDINATOR] swap %r — complete", name)

    # ------------------------------------------------------------------
    # Distro classification — persistent vs invocation-compatible
    # ------------------------------------------------------------------

    def classify_distro(self) -> tuple[bool, list[str]]:
        """Determine whether this distro requires a persistent process.

        Scans all registered services for ``PersistentService`` protocol.
        Returns ``(is_persistent, service_names)`` where ``is_persistent``
        is True if any service requires background workers.

        Used by nexusd startup and CLI ``nexus info`` to report distro type.
        """
        persistent_names: list[str] = []
        for info in self._registry.list_all():
            if isinstance(info.instance, PersistentService):
                persistent_names.append(info.name)
        return bool(persistent_names), persistent_names

    def classify_hot_swappable(self) -> tuple[list[str], list[str]]:
        """Classify services into hot-swappable vs static.

        Returns ``(hot_swappable_names, static_names)``.
        """
        hot: list[str] = []
        static: list[str] = []
        for info in self._registry.list_all():
            if isinstance(info.instance, HotSwappable):
                hot.append(info.name)
            else:
                static.append(info.name)
        return hot, static

    # ------------------------------------------------------------------
    # Auto-lifecycle — four-quadrant "one-click" management (Issue #1580)
    # ------------------------------------------------------------------

    async def start_persistent_services(self, *, timeout: float = 30.0) -> list[str]:
        """Auto-start all PersistentService instances in dependency order.

        Scans ServiceRegistry for services implementing PersistentService,
        calls start() in BLM dependency order.  Services only need to
        implement the protocol — kernel handles the rest.

        Idempotent — PersistentService.start() is idempotent by contract.

        Returns list of started service names.
        """
        started: list[str] = []
        for name in self._ordered_names():
            info = self._registry.service_info(name)
            if info is None:
                continue
            if not isinstance(info.instance, PersistentService):
                continue
            try:
                await asyncio.wait_for(info.instance.start(), timeout=timeout)
                started.append(name)
                logger.info("[COORDINATOR] auto-started persistent service %r", name)
            except TimeoutError:
                logger.error("[COORDINATOR] timeout starting %r after %.1fs", name, timeout)
            except Exception as exc:
                logger.error("[COORDINATOR] failed to start %r: %s", name, exc)
        if started:
            logger.info("[COORDINATOR] started %d persistent services: %s", len(started), started)
        return started

    async def stop_persistent_services(self, *, timeout: float = 10.0) -> list[str]:
        """Auto-stop all PersistentService instances in reverse dependency order.

        Called during shutdown.  Mirrors start_persistent_services().
        """
        stopped: list[str] = []
        for name in self._ordered_names(reverse=True):
            info = self._registry.service_info(name)
            if info is None:
                continue
            if not isinstance(info.instance, PersistentService):
                continue
            try:
                await asyncio.wait_for(info.instance.stop(), timeout=timeout)
                stopped.append(name)
                logger.info("[COORDINATOR] auto-stopped persistent service %r", name)
            except TimeoutError:
                logger.error("[COORDINATOR] timeout stopping %r after %.1fs", name, timeout)
            except Exception as exc:
                logger.error("[COORDINATOR] failed to stop %r: %s", name, exc)
        if stopped:
            logger.info("[COORDINATOR] stopped %d persistent services: %s", len(stopped), stopped)
        return stopped

    async def activate_hot_swappable_services(self) -> list[str]:
        """Auto-activate all HotSwappable services: register hooks + activate().

        Scans ServiceRegistry for HotSwappable instances, registers their
        hook_spec() into KernelDispatch, then calls activate().  Services
        only need to implement the protocol — kernel handles the rest.

        Idempotent — activate() is idempotent by contract.

        Returns list of activated service names.
        """
        activated: list[str] = []
        for name in self._ordered_names():
            info = self._registry.service_info(name)
            if info is None:
                continue
            if not isinstance(info.instance, HotSwappable):
                continue
            try:
                # Auto-capture hook_spec from protocol if not already set
                spec = self._hook_specs.get(name)
                if spec is None:
                    spec = info.instance.hook_spec()
                    if spec is not None and not spec.is_empty:
                        self._hook_specs[name] = spec
                # Register hooks into dispatch (skip if pre-registered by _enlist_hook)
                if name not in self._hooks_on_dispatch:
                    self._register_hooks(name)
                # Activate service
                await info.instance.activate()
                activated.append(name)
                logger.info("[COORDINATOR] auto-activated hot-swappable service %r", name)
            except Exception as exc:
                logger.error("[COORDINATOR] failed to activate %r: %s", name, exc)
        if activated:
            logger.info(
                "[COORDINATOR] activated %d hot-swappable services: %s",
                len(activated),
                activated,
            )
        return activated

    async def deactivate_hot_swappable_services(self) -> list[str]:
        """Auto-deactivate all HotSwappable services: drain + unregister hooks.

        Called during shutdown.  Mirrors activate_hot_swappable_services().
        """
        deactivated: list[str] = []
        for name in self._ordered_names(reverse=True):
            info = self._registry.service_info(name)
            if info is None:
                continue
            if not isinstance(info.instance, HotSwappable):
                continue
            try:
                await info.instance.drain()
                self._unregister_hooks(name)
                deactivated.append(name)
                logger.info("[COORDINATOR] auto-deactivated hot-swappable service %r", name)
            except Exception as exc:
                logger.error("[COORDINATOR] failed to deactivate %r: %s", name, exc)
        if deactivated:
            logger.info(
                "[COORDINATOR] deactivated %d hot-swappable services: %s",
                len(deactivated),
                deactivated,
            )
        return deactivated

    def _ordered_names(self, *, reverse: bool = False) -> list[str]:
        """Return service names in BLM dependency order (or reverse for shutdown)."""
        try:
            if reverse:
                levels = self._blm.compute_shutdown_order()
            else:
                levels = self._blm.compute_startup_order()
            return [name for level in levels for name in level]
        except Exception:
            # Fallback: no ordering guarantee
            return [info.name for info in self._registry.list_all()]

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
