"""Kernel service symbol table — ``/proc/modules`` of Nexus.

Provides ``ServiceRegistry``, a typed registry for wired service instances.
Extends ``BaseRegistry[ServiceInfo]`` with dependency validation, convenience
accessors, bulk registration, and diagnostic snapshots.

Phase 1 (Issue #1452): infrastructure + dual-write.
Phase 2: caller migration (``nx.search_service`` → ``nx.service("search")``) + delete setattr.
Phase 3: ``EXPORT_SYMBOL`` pattern + runtime hot-swap.

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
from typing import Any

from nexus.lib.registry import BaseRegistry

logger = logging.getLogger(__name__)


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
    """Kernel service symbol table — ``/proc/modules`` of Nexus.

    Inherits ``BaseRegistry``: thread-safe register/get/list/unregister.
    Adds: dependency validation, convenience accessors, bulk registration.
    """

    def __init__(self) -> None:
        super().__init__(name="services")
        # Shared ref-counting state for ServiceRef proxies / drain
        self._refcounts: dict[str, int] = {}
        self._drain_events: dict[str, asyncio.Event] = {}

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
