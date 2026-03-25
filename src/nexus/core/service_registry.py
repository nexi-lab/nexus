"""Kernel service symbol table + lifecycle coordinator — ``/proc/modules`` of Nexus.

Provides ``ServiceRegistry``, a typed registry for wired service instances
**with integrated lifecycle orchestration**.  Merges the former
``ServiceLifecycleCoordinator`` (system_services/lifecycle) into the kernel-owned
registry — like Linux ``kernel/module/main.c`` handling both symbol table and
lifecycle in one module.

``enlist()`` is the **single public entry point** for all service registration.
It auto-detects the service quadrant and applies appropriate lifecycle:

    Q1 (restart-required) — register only
    Q2 (HotSwappable)   — register + capture hook_spec + activate
    Q3 (Persistent)     — register + start (deferred pre-bootstrap)
    Q4 (both)           — register + start + hooks + activate

Linux analogy:

    insmod          → registry.register_service("search", svc)
    EXPORT_SYMBOL() → nx.service("search")
    rmmod           → registry.unregister("search")
    /proc/modules   → registry.snapshot()
"""

from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from nexus.contracts.protocols.service_hooks import HookSpec
from nexus.contracts.protocols.service_lifecycle import (
    HotSwappable,
    PersistentService,
    ServiceQuadrant,
)
from nexus.lib.registry import BaseRegistry

if TYPE_CHECKING:
    from nexus.core.kernel_dispatch import KernelDispatch

logger = logging.getLogger(__name__)

DEFAULT_DRAIN_TIMEOUT: float = 10.0


# ---------------------------------------------------------------------------
# ServiceRef — transparent ref-counting proxy for hot-swap drain
# ---------------------------------------------------------------------------


class ServiceRef:
    """Transparent proxy returned by ``ServiceRegistry.service()``.

    Wraps every method call with acquire/release on a shared refcount dict,
    enabling ``swap_service()`` to drain in-flight calls before unmounting.

    Callers see no difference — ``nx.service("search").glob(...)`` works
    identically whether ``glob`` is sync or async.

    Note: A ``with nx.use_service()`` context manager is intentionally **not**
    provided.  Ref-counting happens automatically on every method call via
    ``__getattr__``, so callers never need to manually acquire/release.
    All 118+ call-sites in ``src/`` are fire-and-forget with no long-lived
    references — the proxy pattern handles everything transparently.
    """

    __slots__ = ("_instance", "_name", "_refcounts", "_drain_events")

    def __init__(
        self,
        instance: Any,
        name: str,
        refcounts: dict[str, int],
        drain_events: dict[str, asyncio.Event],
    ) -> None:
        object.__setattr__(self, "_instance", instance)
        object.__setattr__(self, "_name", name)
        object.__setattr__(self, "_refcounts", refcounts)
        object.__setattr__(self, "_drain_events", drain_events)

    @property
    def _service_instance(self) -> Any:
        """Escape hatch: access the raw underlying instance."""
        return object.__getattribute__(self, "_instance")

    def __getattr__(self, attr: str) -> Any:
        instance = object.__getattribute__(self, "_instance")
        val = getattr(instance, attr)
        if not callable(val):
            return val

        name = object.__getattribute__(self, "_name")
        refcounts = object.__getattribute__(self, "_refcounts")
        drain_events = object.__getattribute__(self, "_drain_events")

        if asyncio.iscoroutinefunction(val):

            @functools.wraps(val)
            async def _async_wrap(*a: Any, **kw: Any) -> Any:
                refcounts[name] = refcounts.get(name, 0) + 1
                try:
                    return await val(*a, **kw)
                finally:
                    refcounts[name] -= 1
                    if refcounts[name] <= 0:
                        evt = drain_events.get(name)
                        if evt is not None:
                            evt.set()

            return _async_wrap

        @functools.wraps(val)
        def _sync_wrap(*a: Any, **kw: Any) -> Any:
            refcounts[name] = refcounts.get(name, 0) + 1
            try:
                return val(*a, **kw)
            finally:
                refcounts[name] -= 1
                if refcounts[name] <= 0:
                    evt = drain_events.get(name)
                    if evt is not None:
                        evt.set()

        return _sync_wrap

    def __setattr__(self, attr: str, value: Any) -> None:
        """Delegate attribute writes to the underlying instance."""
        instance = object.__getattribute__(self, "_instance")
        setattr(instance, attr, value)

    def __repr__(self) -> str:
        instance = object.__getattribute__(self, "_instance")
        name = object.__getattribute__(self, "_name")
        return f"ServiceRef({name!r}, {type(instance).__name__})"


@dataclass(frozen=True)
class ServiceInfo:
    """Immutable service registration descriptor (``struct module``).

    Unlike ``BrickInfo.brick_cls`` (stores a *class*), ``instance`` stores
    a live service object — wired services are singletons created at link().
    """

    name: str
    instance: Any
    dependencies: tuple[str, ...] = ()
    exports: tuple[str, ...] = ()
    profile_gate: str | None = None
    is_remote: bool = False
    metadata: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))


class ServiceRegistry(BaseRegistry["ServiceInfo"]):
    """Kernel service symbol table + lifecycle coordinator.

    Inherits ``BaseRegistry``: thread-safe register/get/list/unregister.
    Adds: dependency validation, convenience accessors, bulk registration,
    and integrated lifecycle orchestration (formerly ServiceLifecycleCoordinator).
    """

    def __init__(self, dispatch: KernelDispatch | None = None) -> None:
        super().__init__(name="services")
        # Shared ref-counting state for ServiceRef proxies / drain
        self._refcounts: dict[str, int] = {}
        self._drain_events: dict[str, asyncio.Event] = {}

        # Lifecycle orchestration state (formerly SLC)
        self._dispatch: KernelDispatch | None = dispatch
        self._hook_specs: dict[str, HookSpec] = {}
        # Tracks services whose hooks were pre-registered on dispatch at
        # initialize() time by _enlist_hook().  activate_hot_swappable_services()
        # skips _register_hooks() for these to avoid double registration.
        self._hooks_on_dispatch: set[str] = set()
        self._bootstrapped: bool = False

    # -- registration ------------------------------------------------------

    def register_service(
        self,
        name: str,
        instance: Any,
        *,
        dependencies: tuple[str, ...] | list[str] = (),
        exports: tuple[str, ...] | list[str] = (),
        profile_gate: str | None = None,
        is_remote: bool = False,
        metadata: dict[str, Any] | None = None,
        allow_overwrite: bool = False,
    ) -> None:
        """Register a service instance under *name* (``insmod``).

        Validates that all declared *dependencies* are already registered
        and that all *exports* exist as attributes on the instance.
        """
        deps = tuple(dependencies)
        # Dependency validation — fail-fast on missing prerequisites.
        missing = [d for d in deps if d not in self]
        if missing:
            raise ValueError(
                f"services: cannot register {name!r} — missing dependencies: {missing}"
            )

        # EXPORT_SYMBOL validation — every declared export must exist.
        exp = tuple(exports)
        bad_exports = [e for e in exp if not hasattr(instance, e)]
        if bad_exports:
            raise ValueError(
                f"services: {name!r} declares exports not found on instance: {bad_exports}"
            )

        info = ServiceInfo(
            name=name,
            instance=instance,
            dependencies=deps,
            exports=exp,
            profile_gate=profile_gate,
            is_remote=is_remote,
            metadata=MappingProxyType(metadata or {}),
        )
        self.register(name, info, allow_overwrite=allow_overwrite)

    def replace_service(
        self,
        name: str,
        new_instance: Any,
        *,
        exports: tuple[str, ...] | list[str] = (),
    ) -> ServiceInfo:
        """Atomically swap the instance for *name*. ``service(name)`` never returns None.

        Preserves the old ServiceInfo's dependencies/profile_gate/is_remote.
        Returns the **old** ServiceInfo.

        Raises:
            KeyError: If *name* is not registered.
            ValueError: If new exports are invalid.
        """
        old_info = self.get(name)
        if old_info is None:
            raise KeyError(f"services: {name!r} not registered — cannot replace")

        exp = tuple(exports)
        bad_exports = [e for e in exp if not hasattr(new_instance, e)]
        if bad_exports:
            raise ValueError(
                f"services: {name!r} replacement declares invalid exports: {bad_exports}"
            )

        new_info = ServiceInfo(
            name=name,
            instance=new_instance,
            dependencies=old_info.dependencies,
            exports=exp or old_info.exports,
            profile_gate=old_info.profile_gate,
            is_remote=old_info.is_remote,
            metadata=old_info.metadata,
        )
        self.register(name, new_info, allow_overwrite=True)
        return old_info

    def unregister_service(self, name: str) -> ServiceInfo | None:
        """Remove a service (``rmmod``). Dependency guard: refuses if dependents exist.

        Returns the removed ServiceInfo, or None if not found.
        """
        dependents = [i.name for i in self.list_all() if name in i.dependencies]
        if dependents:
            raise ValueError(f"services: cannot unregister {name!r} — depended on by: {dependents}")
        return self.unregister(name)

    # -- convenience accessors ---------------------------------------------

    def service(self, name: str) -> ServiceRef | None:
        """Primary lookup API (``EXPORT_SYMBOL``).

        Returns a ``ServiceRef`` proxy wrapping the instance. The proxy
        is transparent — all attribute/method access delegates to the
        underlying instance — but adds per-call ref-counting so that
        ``swap_service()`` can drain in-flight operations before unmount.
        """
        info = self.get(name)
        if info is None:
            return None
        return ServiceRef(info.instance, name, self._refcounts, self._drain_events)

    def service_or_raise(self, name: str) -> Any:
        """Like :meth:`service` but raises ``KeyError`` if absent."""
        return self.get_or_raise(name).instance

    def service_info(self, name: str) -> ServiceInfo | None:
        """Return the full ``ServiceInfo`` envelope, or ``None``."""
        return self.get(name)

    # -- diagnostics -------------------------------------------------------

    def snapshot(self) -> list[dict[str, Any]]:
        """Diagnostic snapshot — ``cat /proc/modules``."""
        result = []
        for info in self.list_all():
            result.append(
                {
                    "name": info.name,
                    "type": type(info.instance).__name__,
                    "dependencies": list(info.dependencies),
                    "exports": list(info.exports),
                    "profile_gate": info.profile_gate,
                    "is_remote": info.is_remote,
                    "metadata": dict(info.metadata),
                }
            )
        return result

    # =====================================================================
    # Lifecycle orchestration (merged from ServiceLifecycleCoordinator)
    # =====================================================================

    # -- mark_bootstrapped — phase transition signal -----------------------

    def mark_bootstrapped(self) -> None:
        """Mark that bootstrap() has completed.

        After this, enlist() auto-starts Q3 PersistentService instances
        immediately instead of deferring to start_persistent_services().
        """
        self._bootstrapped = True

    # -- insmod — register service in Registry (internal) ------------------

    def _register_service(
        self,
        name: str,
        instance: Any,
        *,
        exports: tuple[str, ...] = (),
        allow_overwrite: bool = False,
    ) -> None:
        """Register a service in ServiceRegistry."""
        self.register_service(
            name,
            instance,
            exports=exports,
            allow_overwrite=allow_overwrite,
        )
        logger.info(
            "[COORDINATOR] insmod %r (exports=%d)",
            name,
            len(exports),
        )

    # -- enlist — the ONE entry point for all four quadrants ---------------

    async def enlist(
        self,
        name: str,
        instance: Any,
        *,
        exports: tuple[str, ...] = (),
        depends_on: tuple[str, ...] = (),
        allow_overwrite: bool = False,
    ) -> None:
        """Enlist a service into the four-quadrant lifecycle system.

        This is the **single entry point** all services must call — the
        "strong label" that marks a service as migrated to the new contract.
        The coordinator auto-detects the quadrant via ``isinstance`` checks:

        - **Q1** (neither protocol): register only — no lifecycle, no hooks.
        - **Q2** (HotSwappable): register + capture ``hook_spec()`` + activate.
        - **Q3** (PersistentService): register + ``start()``.
        - **Q4** (both): register + ``start()`` + capture hooks + activate.

        Post-bootstrap, Q3 services are auto-started immediately.
        Pre-bootstrap, Q3 start() is deferred to start_persistent_services().

        Args:
            depends_on: Accepted for call-site compatibility; currently unused
                (BLM dependency ordering removed).
        """
        del depends_on  # accepted but unused (BLM removed)
        self._register_service(name, instance, exports=exports, allow_overwrite=allow_overwrite)

        # Q3 / Q4: auto-start persistent background work (only post-bootstrap)
        if isinstance(instance, PersistentService) and self._bootstrapped:
            await instance.start()
            logger.info("[COORDINATOR] enlist %r — started (PersistentService)", name)

        # Q2 / Q4: auto-capture hooks and activate
        if isinstance(instance, HotSwappable):
            spec = self._ensure_hook_spec(name, instance)
            if spec is not None and not spec.is_empty:
                self._register_hooks(name)
                self._hooks_on_dispatch.add(name)
            await instance.activate()
            logger.info("[COORDINATOR] enlist %r — activated (HotSwappable)", name)

        if not isinstance(instance, PersistentService) and not isinstance(instance, HotSwappable):
            logger.info("[COORDINATOR] enlist %r — registered (Q1 restart-required)", name)

    # -- mount — register VFS hooks ----------------------------------------

    async def _mount_service(self, name: str) -> None:
        """Mount a service: register VFS hooks."""
        self._register_hooks(name)
        logger.info("[COORDINATOR] mount %r — hooks registered", name)

    # -- umount — unregister VFS hooks ------------------------------------

    async def _unmount_service(self, name: str) -> None:
        """Unmount: unregister VFS hooks."""
        self._unregister_hooks(name)
        logger.info("[COORDINATOR] umount %r", name)

    # -- rmmod — unregister from Registry ----------------------------------

    async def unregister_service_full(self, name: str) -> None:
        """Fully remove a service: unmount hooks, then unregister."""
        await self._unmount_service(name)
        self.unregister_service(name)
        self._hook_specs.pop(name, None)
        logger.info("[COORDINATOR] rmmod %r", name)

    # -- swap — atomic replace + drain + hook swap -------------------------

    async def swap_service(
        self,
        name: str,
        new_instance: Any,
        *,
        exports: tuple[str, ...] = (),
        hook_spec: HookSpec | None = None,
        drain_timeout: float = DEFAULT_DRAIN_TIMEOUT,
    ) -> None:
        """Hot-swap a service: validate → drain → hook swap → activate.

        Only HotSwappable services can be swapped.  Restart-required services raise
        TypeError — use full restart instead.

        Flow for HotSwappable services:
            1. Validate old service is HotSwappable (TypeError if not)
            2. Call old_service.drain() — stop accepting new work
            3. Drain ServiceRef refcount → 0 (in-flight calls complete)
            4. Unregister old VFS hooks (from old hook_spec or old_service.hook_spec())
            5. Atomic replace in ServiceRegistry
            6. Register new VFS hooks (from hook_spec param, new_service.hook_spec(), or retroactive)
            7. Call new_service.activate() if HotSwappable
        """
        # --- Resolve old instance ---
        old_info = self.service_info(name)
        if old_info is None:
            raise KeyError(f"swap_service: {name!r} not registered")
        old_instance = old_info.instance

        # --- Guard: only HotSwappable services can be swapped ---
        quadrant = ServiceQuadrant.of(old_instance)
        if not quadrant.is_hot_swappable:
            raise TypeError(
                f"swap_service: {name!r} is {quadrant.label} — cannot hot-swap. "
                f"Only Q2/Q4 services (HotSwappable) support runtime swap. "
                f"Use full restart instead."
            )

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
        self.replace_service(name, new_instance, exports=exports)
        logger.info("[COORDINATOR] swap %r — atomic replace done", name)

        # Step 5: Register new hooks — explicit param > protocol > clear
        new_hook_spec = hook_spec
        if new_hook_spec is None and isinstance(new_instance, HotSwappable):
            new_hook_spec = new_instance.hook_spec()

        if new_hook_spec is not None and not new_hook_spec.is_empty:
            self._hook_specs[name] = new_hook_spec
        elif name in self._hook_specs:
            del self._hook_specs[name]

        self._register_hooks(name)

        # Step 6: Activate new service if HotSwappable
        if isinstance(new_instance, HotSwappable):
            await new_instance.activate()

        logger.info("[COORDINATOR] swap %r — complete", name)

    # -- Diagnostics — quadrant classification -----------------------------

    def classify_all(self) -> dict[str, ServiceQuadrant]:
        """Return quadrant classification for all registered services."""
        return {info.name: ServiceQuadrant.of(info.instance) for info in self.list_all()}

    # -- Single-service activate / deactivate (internal) -------------------

    async def _activate_service(self, name: str) -> None:
        """Activate a single HotSwappable service: register hooks + activate()."""
        info = self.service_info(name)
        if info is None:
            raise KeyError(f"activate_service: {name!r} not registered")
        quadrant = ServiceQuadrant.of(info.instance)
        if not quadrant.is_hot_swappable:
            raise TypeError(
                f"activate_service: {name!r} is {quadrant.label} — cannot activate. "
                f"Only Q2/Q4 services (HotSwappable) support activate/drain."
            )
        self._ensure_hook_spec(name, info.instance)
        self._register_hooks(name)
        await info.instance.activate()
        logger.info("[COORDINATOR] _activate_service %r — done (%s)", name, quadrant.label)

    async def _deactivate_service(self, name: str) -> None:
        """Deactivate a single HotSwappable service: drain + unregister hooks."""
        info = self.service_info(name)
        if info is None:
            raise KeyError(f"deactivate_service: {name!r} not registered")
        quadrant = ServiceQuadrant.of(info.instance)
        if not quadrant.is_hot_swappable:
            raise TypeError(
                f"deactivate_service: {name!r} is {quadrant.label} — cannot deactivate. "
                f"Only Q2/Q4 services (HotSwappable) support activate/drain."
            )
        await info.instance.drain()
        self._unregister_hooks(name)
        logger.info("[COORDINATOR] _deactivate_service %r — done (%s)", name, quadrant.label)

    # -- Auto-lifecycle — four-quadrant management -------------------------

    async def start_persistent_services(self, *, timeout: float = 30.0) -> list[str]:
        """Auto-start all PersistentService instances in dependency order."""
        started: list[str] = []
        for name in self._ordered_names():
            info = self.service_info(name)
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
        """Auto-stop all PersistentService instances in reverse dependency order."""
        stopped: list[str] = []
        for name in self._ordered_names(reverse=True):
            info = self.service_info(name)
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
        """Auto-activate all HotSwappable services: register hooks + activate()."""
        activated: list[str] = []
        for name in self._ordered_names():
            info = self.service_info(name)
            if info is None:
                continue
            if not isinstance(info.instance, HotSwappable):
                continue
            try:
                # Auto-capture hook_spec from protocol if not already set
                self._ensure_hook_spec(name, info.instance)
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
        """Auto-deactivate all HotSwappable services: drain + unregister hooks."""
        deactivated: list[str] = []
        for name in self._ordered_names(reverse=True):
            info = self.service_info(name)
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
        """Return service names in registration order (or reverse for shutdown)."""
        names = [info.name for info in self.list_all()]
        if reverse:
            names.reverse()
        return names

    # -- Hook spec management ----------------------------------------------

    def _ensure_hook_spec(self, name: str, instance: Any) -> HookSpec | None:
        """Capture HookSpec from protocol if not already stored."""
        spec = self._hook_specs.get(name)
        if spec is None and isinstance(instance, HotSwappable):
            spec = instance.hook_spec()
            if spec is not None:
                self._hook_specs[name] = spec
        return spec

    def _set_hook_spec(self, name: str, spec: HookSpec) -> None:
        """Set/replace the HookSpec for a service (retroactive capture)."""
        self._hook_specs[name] = spec

    def _get_hook_spec(self, name: str) -> HookSpec | None:
        """Return the HookSpec for a service, or None."""
        return self._hook_specs.get(name)

    # -- Drain — wait for refcount → 0 ------------------------------------

    async def _drain(self, name: str, *, timeout: float) -> None:
        """Wait for all in-flight calls on *name* to complete (refcount → 0)."""
        current = self._refcounts.get(name, 0)
        if current <= 0:
            return

        evt = asyncio.Event()
        self._drain_events[name] = evt
        try:
            await asyncio.wait_for(evt.wait(), timeout=timeout)
            logger.debug("[COORDINATOR] drain %r — completed normally", name)
        except TimeoutError:
            remaining = self._refcounts.get(name, 0)
            logger.warning(
                "[COORDINATOR] drain %r — timed out after %.1fs (%d in-flight calls remaining)",
                name,
                timeout,
                remaining,
            )
        finally:
            self._drain_events.pop(name, None)

    # -- Internal hook register/unregister ---------------------------------

    def _register_hooks(self, name: str) -> None:
        spec = self._hook_specs.get(name)
        if spec is None or spec.is_empty:
            return
        self._register_hooks_for_spec(spec)

    def _register_hooks_for_spec(self, spec: HookSpec) -> None:
        d = self._dispatch
        if d is None:
            return
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

    def _unregister_hooks(self, name: str) -> None:
        spec = self._hook_specs.get(name)
        if spec is None or spec.is_empty:
            return
        self._unregister_hooks_for_spec(spec)

    def _unregister_hooks_for_spec(self, spec: HookSpec) -> None:
        d = self._dispatch
        if d is None:
            return
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
