"""Kernel service symbol table — ``/proc/modules`` of Nexus.

Provides ``ServiceRegistry``, a typed registry for wired service instances.
Extends ``BaseRegistry[ServiceInfo]`` with dependency validation, convenience
accessors, bulk registration, and diagnostic snapshots.

Phase 1 (Issue #1452): infrastructure + dual-write alongside ``bind_wired_services()``.
Phase 2: caller migration (``nx.search_service`` → ``nx.service("search")``).
Phase 3: ``EXPORT_SYMBOL`` pattern + runtime hot-swap.

Linux analogy:

    insmod          → registry.register_service("search", svc)
    EXPORT_SYMBOL() → nx.service("search")
    rmmod           → registry.unregister("search")
    /proc/modules   → registry.snapshot()
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from nexus.lib.registry import BaseRegistry

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ServiceInfo:
    """Immutable service registration descriptor (``struct module``).

    Unlike ``BrickInfo.brick_cls`` (stores a *class*), ``instance`` stores
    a live service object — wired services are singletons created at link().
    """

    name: str
    instance: Any
    dependencies: tuple[str, ...] = ()
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

    # -- registration ------------------------------------------------------

    def register_service(
        self,
        name: str,
        instance: Any,
        *,
        dependencies: tuple[str, ...] | list[str] = (),
        profile_gate: str | None = None,
        is_remote: bool = False,
        metadata: dict[str, Any] | None = None,
        allow_overwrite: bool = False,
    ) -> None:
        """Register a service instance under *name* (``insmod``).

        Validates that all declared *dependencies* are already registered.
        """
        deps = tuple(dependencies)
        # Dependency validation — fail-fast on missing prerequisites.
        missing = [d for d in deps if d not in self]
        if missing:
            raise ValueError(
                f"services: cannot register {name!r} — missing dependencies: {missing}"
            )

        info = ServiceInfo(
            name=name,
            instance=instance,
            dependencies=deps,
            profile_gate=profile_gate,
            is_remote=is_remote,
            metadata=MappingProxyType(metadata or {}),
        )
        self.register(name, info, allow_overwrite=allow_overwrite)

    # -- convenience accessors ---------------------------------------------

    def service(self, name: str) -> Any | None:
        """Primary lookup API (``EXPORT_SYMBOL``).

        Returns the service *instance*, not the ``ServiceInfo`` envelope.
        """
        info = self.get(name)
        return info.instance if info is not None else None

    def service_or_raise(self, name: str) -> Any:
        """Like :meth:`service` but raises ``KeyError`` if absent."""
        return self.get_or_raise(name).instance

    def service_info(self, name: str) -> ServiceInfo | None:
        """Return the full ``ServiceInfo`` envelope, or ``None``."""
        return self.get(name)

    # -- bulk registration -------------------------------------------------

    def register_many(
        self,
        services: dict[str, Any],
        *,
        is_remote: bool = False,
    ) -> int:
        """Register multiple services at once (skips ``None`` values).

        Used by factory ``populate_service_registry()`` for batch wiring.
        Returns the number of services actually registered.
        """
        count = 0
        for name, instance in services.items():
            if instance is None:
                continue
            self.register_service(name, instance, is_remote=is_remote)
            count += 1
        return count

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
                    "profile_gate": info.profile_gate,
                    "is_remote": info.is_remote,
                    "metadata": dict(info.metadata),
                }
            )
        return result
