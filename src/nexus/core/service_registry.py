"""Kernel service symbol table + lifecycle coordinator — ``/proc/modules`` of Nexus.

Provides ``ServiceRegistry``, a typed registry for wired service instances
**with integrated lifecycle orchestration**.  Merges the former
``ServiceLifecycleCoordinator`` (services/lifecycle) into the kernel-owned
registry — like Linux ``kernel/module/main.c`` handling both symbol table and
lifecycle in one module.

``enlist()`` is the **single public entry point** for all service registration.
It auto-detects lifecycle requirements:

    On-demand service       — register only, duck-type hook_spec() for hooks
    BackgroundService       — register + start (deferred pre-bootstrap)

Hook management is automatic: if an instance has a ``hook_spec()`` method
(duck-typed), the kernel captures and registers hooks at enlist() time.

Linux analogy:

    insmod          → registry.register_service("search", svc)
    EXPORT_SYMBOL() → nx.service("search")
    rmmod           → registry.unregister("search")
    /proc/modules   → registry.snapshot()
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import logging
import threading
from dataclasses import dataclass
from typing import Any

from nexus.contracts.protocols.service_hooks import HookSpec
from nexus.contracts.protocols.service_lifecycle import BackgroundService
from nexus.lib.registry import BaseRegistry

logger = logging.getLogger(__name__)


DEFAULT_DRAIN_TIMEOUT: float = 10.0


def _declares_hook_spec(instance: Any) -> bool:
    """Return True only when ``hook_spec`` is a real attribute on the object.

    Dynamic proxies can synthesize arbitrary public attributes via
    ``__getattr__``. Lifecycle detection must ignore those synthetic attrs,
    otherwise bootstrap can try to call a non-existent ``hook_spec`` method.
    """
    try:
        attr = inspect.getattr_static(instance, "hook_spec")
    except AttributeError:
        return False
    return callable(attr)


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
        drain_events: dict[str, threading.Event],
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
    exports: tuple[str, ...] = ()


class ServiceRegistry(BaseRegistry["ServiceInfo"]):
    """Kernel service symbol table + lifecycle coordinator.

    Inherits ``BaseRegistry``: thread-safe register/get/list/unregister.
    Adds: dependency validation, convenience accessors, bulk registration,
    and integrated lifecycle orchestration (formerly ServiceLifecycleCoordinator).
    """

    def __init__(self, dispatch: Any = None) -> None:
        super().__init__(name="services")
        # Shared ref-counting state for ServiceRef proxies / drain
        self._refcounts: dict[str, int] = {}
        self._drain_events: dict[str, threading.Event] = {}

        # Lifecycle orchestration state (formerly SLC)
        self._dispatch: Any = dispatch
        self._hook_specs: dict[str, HookSpec] = {}
        self._bootstrapped: bool = False

    # -- registration ------------------------------------------------------

    def register_service(
        self,
        name: str,
        instance: Any,
        *,
        exports: tuple[str, ...] | list[str] = (),
        allow_overwrite: bool = False,
    ) -> None:
        """Register a service instance under *name* (``insmod``).

        Validates that all *exports* exist as attributes on the instance.
        """
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
            exports=exp,
        )
        self.register(name, info, allow_overwrite=allow_overwrite)

    def unregister_service(self, name: str) -> ServiceInfo | None:
        """Remove a service (``rmmod``).

        Returns the removed ServiceInfo, or None if not found.
        """
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
                    "exports": list(info.exports),
                }
            )
        return result

    # =====================================================================
    # Lifecycle orchestration (merged from ServiceLifecycleCoordinator)
    # =====================================================================

    # -- mark_bootstrapped — phase transition signal -----------------------

    def mark_bootstrapped(self) -> None:
        """Mark that bootstrap() has completed.

        After this, enlist() auto-starts Q3 BackgroundService instances
        immediately instead of deferring to start_background_services().
        """
        self._bootstrapped = True

    # -- enlist — the ONE entry point for all services --------------------

    def enlist(
        self,
        name: str,
        instance: Any,
        *,
        exports: tuple[str, ...] = (),
        allow_overwrite: bool = False,
    ) -> None:
        """Enlist a service into the lifecycle system.

        This is the **single entry point** all services must call.
        Auto-detects lifecycle requirements:

        - **On-demand**: register only.
        - **BackgroundService**: register + ``start()`` (post-bootstrap).
        - **Duck-typed hook_spec()**: auto-capture and register hooks.

        Post-bootstrap, BackgroundService instances are auto-started immediately.
        Pre-bootstrap, start() is deferred to start_background_services().
        """
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

        # Auto-start background work (only post-bootstrap)
        if isinstance(instance, BackgroundService) and self._bootstrapped:
            from nexus.lib.sync_bridge import run_sync

            coro = instance.start()
            if asyncio.iscoroutine(coro):
                run_sync(coro, timeout=30.0)
            logger.info("[COORDINATOR] enlist %r — started (BackgroundService)", name)

        # Auto-capture hooks via duck-typed hook_spec()
        if _declares_hook_spec(instance):
            spec = self._ensure_hook_spec(name, instance)
            if spec is not None and not spec.is_empty:
                self._register_hooks(name)
            logger.info("[COORDINATOR] enlist %r — hooks registered", name)

        if not isinstance(instance, BackgroundService) and not _declares_hook_spec(instance):
            logger.info("[COORDINATOR] enlist %r — registered (on-demand)", name)

    # -- rmmod — unregister from Registry ----------------------------------

    def unregister_service_full(self, name: str) -> None:
        """Fully remove a service: unmount hooks, then unregister."""
        self._unregister_hooks(name)
        self.unregister_service(name)
        self._hook_specs.pop(name, None)
        logger.info("[COORDINATOR] rmmod %r", name)

    # -- swap — atomic replace + drain + hook swap -------------------------

    def swap_service(
        self,
        name: str,
        new_instance: Any,
        *,
        exports: tuple[str, ...] = (),
        hook_spec: HookSpec | None = None,
        drain_timeout: float = DEFAULT_DRAIN_TIMEOUT,
    ) -> None:
        """Hot-swap a service: refcount drain → unhook → replace → rehook.

        Unified path for all services (#1452).  No separate drain()/activate().
        """
        # --- Resolve old instance ---
        old_info = self.service_info(name)
        if old_info is None:
            raise KeyError(f"swap_service: {name!r} not registered")
        old_instance = old_info.instance

        # Resolve old hook spec
        old_hook_spec = self._hook_specs.get(name)
        if old_hook_spec is None and _declares_hook_spec(old_instance):
            old_hook_spec = old_instance.hook_spec()
            if old_hook_spec is not None and not old_hook_spec.is_empty:
                self._hook_specs[name] = old_hook_spec

        # Step 1: Drain ServiceRef refcount (wait for in-flight calls)
        self._drain(name, timeout=drain_timeout)

        # Step 2: Unregister old hooks
        if old_hook_spec is not None:
            self._unregister_hooks_for_spec(old_hook_spec)

        # Step 3: Atomic replace — nx.service(name) now returns new instance
        exp = tuple(exports)
        bad_exports = [e for e in exp if not hasattr(new_instance, e)]
        if bad_exports:
            raise ValueError(
                f"services: {name!r} replacement declares invalid exports: {bad_exports}"
            )
        new_info = ServiceInfo(
            name=name,
            instance=new_instance,
            exports=exp or old_info.exports,
        )
        self.register(name, new_info, allow_overwrite=True)
        logger.info("[COORDINATOR] swap %r — atomic replace done", name)

        # Step 4: Register new hooks — explicit param > duck-type > clear
        new_hook_spec = hook_spec
        if new_hook_spec is None and _declares_hook_spec(new_instance):
            new_hook_spec = new_instance.hook_spec()

        if new_hook_spec is not None and not new_hook_spec.is_empty:
            self._hook_specs[name] = new_hook_spec
        elif name in self._hook_specs:
            del self._hook_specs[name]

        self._register_hooks(name)

        logger.info("[COORDINATOR] swap %r — complete", name)

    # -- Auto-lifecycle — BackgroundService management ----------------------

    def start_background_services(self, *, timeout: float = 30.0) -> list[str]:
        """Auto-start all BackgroundService instances in dependency order."""
        from nexus.lib.sync_bridge import run_sync

        started: list[str] = []
        for name in self._ordered_names():
            info = self.service_info(name)
            if info is None:
                continue
            if not isinstance(info.instance, BackgroundService):
                continue
            try:
                coro = info.instance.start()
                if asyncio.iscoroutine(coro):
                    run_sync(coro, timeout=timeout)
                started.append(name)
                logger.info("[COORDINATOR] auto-started background service %r", name)
            except TimeoutError:
                logger.error("[COORDINATOR] timeout starting %r after %.1fs", name, timeout)
            except Exception as exc:
                logger.error("[COORDINATOR] failed to start %r: %s", name, exc)
        if started:
            logger.info("[COORDINATOR] started %d background services: %s", len(started), started)
        return started

    def stop_background_services(self, *, timeout: float = 10.0) -> list[str]:
        """Auto-stop all BackgroundService instances in reverse dependency order."""
        from nexus.lib.sync_bridge import run_sync

        stopped: list[str] = []
        for name in self._ordered_names(reverse=True):
            info = self.service_info(name)
            if info is None:
                continue
            if not isinstance(info.instance, BackgroundService):
                continue
            try:
                coro = info.instance.stop()
                if asyncio.iscoroutine(coro):
                    run_sync(coro, timeout=timeout)
                stopped.append(name)
                logger.info("[COORDINATOR] auto-stopped background service %r", name)
            except TimeoutError:
                logger.error("[COORDINATOR] timeout stopping %r after %.1fs", name, timeout)
            except Exception as exc:
                logger.error("[COORDINATOR] failed to stop %r: %s", name, exc)
        if stopped:
            logger.info("[COORDINATOR] stopped %d background services: %s", len(stopped), stopped)
        return stopped

    def close_all_services(self) -> None:
        """Call close() on all services that have it. Reverse registration order.

        Handles sync cleanup (rebac_manager.close(), audit_store.close(), etc.)
        that previously required manual _close_callbacks in _lifecycle.py.
        Runs BEFORE pillar close so DB connections are still open.
        """
        for name in self._ordered_names(reverse=True):
            info = self.service_info(name)
            if info is None:
                continue
            instance = info.instance
            if instance is not None and hasattr(instance, "close") and callable(instance.close):
                try:
                    instance.close()
                except Exception as exc:
                    logger.debug("[COORDINATOR] close(%r) failed (best-effort): %s", name, exc)

    def _unregister_all_hooks(self) -> None:
        """Unregister all hooks from dispatch. Used by aclose()."""
        for name in list(self._hook_specs):
            self._unregister_hooks(name)

    def _ordered_names(self, *, reverse: bool = False) -> list[str]:
        """Return service names in registration order (or reverse for shutdown)."""
        names = [info.name for info in self.list_all()]
        if reverse:
            names.reverse()
        return names

    # -- Hook spec management ----------------------------------------------

    def _ensure_hook_spec(self, name: str, instance: Any) -> HookSpec | None:
        """Capture HookSpec via duck-typed hook_spec() if not already stored."""
        spec = self._hook_specs.get(name)
        if spec is None and _declares_hook_spec(instance):
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

    def _drain(self, name: str, *, timeout: float) -> None:
        """Wait for all in-flight calls on *name* to complete (refcount → 0)."""
        current = self._refcounts.get(name, 0)
        if current <= 0:
            return

        evt = threading.Event()
        self._drain_events[name] = evt
        try:
            if not evt.wait(timeout=timeout):
                remaining = self._refcounts.get(name, 0)
                logger.warning(
                    "[COORDINATOR] drain %r — timed out after %.1fs (%d in-flight calls remaining)",
                    name,
                    timeout,
                    remaining,
                )
            else:
                logger.debug("[COORDINATOR] drain %r — completed normally", name)
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
        for h in spec.copy_hooks:
            d.register_intercept_copy(h)
        for h in spec.mkdir_hooks:
            d.register_intercept_mkdir(h)
        for h in spec.rmdir_hooks:
            d.register_intercept_rmdir(h)
        for h in spec.stat_hooks:
            d.register_intercept_stat(h)
        for h in spec.access_hooks:
            d.register_intercept_access(h)
        # spec.observers: no-op — observer dispatch is fully Rust-native.

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
        for h in spec.copy_hooks:
            d.unregister_intercept_copy(h)
        for h in spec.mkdir_hooks:
            d.unregister_intercept_mkdir(h)
        for h in spec.rmdir_hooks:
            d.unregister_intercept_rmdir(h)
        for h in spec.stat_hooks:
            d.unregister_intercept_stat(h)
        for h in spec.access_hooks:
            d.unregister_intercept_access(h)
        # spec.observers: no-op — observer dispatch is fully Rust-native.
