"""Unified registry base for named-component registries.

Provides ``BaseRegistry[T]``, a generic, thread-safe registry that
eliminates the duplicated register/get/list/clear/discover boilerplate
found across the codebase.  ``BrickRegistry`` adds mandatory Protocol
compliance checking on top.

**Intentionally excluded registries:**

* ``SkillRegistry`` -- async, dual-store, tier hierarchy, ReBAC.
* ``RouterRegistry`` -- ordered list, thin API, ASGI middleware.

Their domain-specific logic far outweighs the ~9 LOC of common boilerplate,
so forcing them into ``BaseRegistry`` would *add* complexity.

Design doc: NEXUS-LEGO-ARCHITECTURE.md S5.2, S12.5, S19.5.
"""

from __future__ import annotations

import importlib
import inspect
import logging
import pkgutil
import threading
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Generic, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class BaseRegistry(Generic[T]):
    """Generic named-component registry.

    Thread-safe, deterministic iteration (sorted keys), and optional
    protocol validation at registration time.

    Example::

        from nexus.core.registry import BaseRegistry
        reg: BaseRegistry[int] = BaseRegistry("counters")
        reg.register("hits", 0)
        assert reg.get("hits") == 0
    """

    def __init__(self, name: str = "registry", *, protocol: type | None = None) -> None:
        self._name = name
        self._protocol = protocol
        self._items: dict[str, T] = {}
        self._lock = threading.Lock()

    # -- mutators ----------------------------------------------------------

    def register(self, key: str, item: T, *, allow_overwrite: bool = False) -> None:
        """Register *item* under *key*.

        Raises ``ValueError`` on duplicate keys unless *allow_overwrite* is
        ``True``.  When a *protocol* was given at construction time, *item*
        is validated against it first.
        """
        if self._protocol is not None:
            _validate_protocol_compliance(item, self._protocol)

        with self._lock:
            if key in self._items and not allow_overwrite:
                raise ValueError(
                    f"{self._name}: key {key!r} already registered.  "
                    f"Pass allow_overwrite=True to replace."
                )
            self._items[key] = item
            logger.debug("%s: registered %r", self._name, key)

    def unregister(self, key: str) -> T | None:
        """Remove and return the item for *key*, or ``None`` if absent."""
        with self._lock:
            item = self._items.pop(key, None)
        if item is not None:
            logger.debug("%s: unregistered %r", self._name, key)
        return item

    def clear(self) -> None:
        """Remove all entries."""
        with self._lock:
            self._items.clear()
        logger.debug("%s: cleared", self._name)

    # -- accessors ---------------------------------------------------------

    def get(self, key: str) -> T | None:
        """Return the item for *key*, or ``None``."""
        return self._items.get(key)

    def get_or_raise(self, key: str) -> T:
        """Return the item for *key*; raise ``KeyError`` if absent."""
        with self._lock:
            if key in self._items:
                return self._items[key]
            available = ", ".join(sorted(self._items))
        raise KeyError(f"{self._name}: {key!r} not found.  Available: [{available}]")

    def list_names(self) -> list[str]:
        """Sorted list of registered keys."""
        with self._lock:
            return sorted(self._items)

    def list_all(self) -> list[T]:
        """All registered items, sorted by key."""
        with self._lock:
            return [self._items[k] for k in sorted(self._items)]

    # -- discovery ---------------------------------------------------------

    def discover_from_package(
        self,
        package_name: str,
        base_class: type,
        *,
        key_fn: Callable[[type], str] | None = None,
    ) -> int:
        """Scan *package_name* for concrete subclasses of *base_class*.

        Each discovered class is instantiated (zero-arg) and registered
        using *key_fn(cls)* as the key (default: ``cls.__name__``).

        Returns the number of successfully registered items.
        """
        count = 0
        _key_fn = key_fn or (lambda cls: cls.__name__)

        try:
            package = importlib.import_module(package_name)
        except Exception:
            logger.error("Failed to import package %s", package_name, exc_info=True)
            return 0

        for _, module_name, is_pkg in pkgutil.iter_modules(package.__path__):
            if is_pkg:
                continue
            fqn = f"{package_name}.{module_name}"
            try:
                module = importlib.import_module(fqn)
            except Exception:
                logger.warning("Failed to import %s", fqn)
                continue

            for attr_name, obj in inspect.getmembers(module, inspect.isclass):
                if issubclass(obj, base_class) and obj is not base_class and obj.__module__ == fqn:
                    try:
                        instance = obj()
                        self.register(_key_fn(obj), instance, allow_overwrite=True)  # type: ignore[arg-type]
                        count += 1
                        logger.debug("Discovered %s from %s", attr_name, fqn)
                    except Exception:
                        logger.warning("Failed to instantiate %s from %s", attr_name, fqn)

        if count:
            logger.info("%s: discovered %d items from %s", self._name, count, package_name)
        return count

    # -- dunder helpers ----------------------------------------------------

    def __len__(self) -> int:
        return len(self._items)

    def __contains__(self, key: object) -> bool:
        return key in self._items

    def __iter__(self) -> Iterator[str]:
        with self._lock:
            return iter(sorted(self._items))

    def __repr__(self) -> str:
        with self._lock:
            return f"{type(self).__name__}(name={self._name!r}, items={sorted(self._items)})"


# ---------------------------------------------------------------------------
# BrickRegistry -- BaseRegistry + mandatory Protocol validation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BrickInfo:
    """Immutable descriptor for a registered brick."""

    name: str
    brick_cls: type
    protocol: type
    metadata: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))


class BrickRegistry(BaseRegistry[BrickInfo]):
    """Registry that enforces runtime Protocol compliance.

    Every registered brick **must** satisfy a ``@runtime_checkable`` Protocol.
    """

    def __init__(self) -> None:
        super().__init__(name="bricks")

    def register_brick(
        self,
        name: str,
        brick_cls: type,
        protocol: type,
        *,
        metadata: dict[str, Any] | None = None,
        allow_overwrite: bool = False,
    ) -> None:
        """Register a brick class with mandatory Protocol validation."""
        _validate_protocol_compliance(brick_cls, protocol)
        info = BrickInfo(
            name=name,
            brick_cls=brick_cls,
            protocol=protocol,
            metadata=MappingProxyType(metadata or {}),
        )
        self.register(name, info, allow_overwrite=allow_overwrite)

    def list_by_protocol(self, protocol: type) -> list[BrickInfo]:
        """Return all bricks that were registered under *protocol*."""
        return [info for info in self.list_all() if info.protocol is protocol]

    def get_brick_class(self, name: str) -> type:
        """Convenience: get the class for *name* (raises ``KeyError``)."""
        return self.get_or_raise(name).brick_cls


# ---------------------------------------------------------------------------
# Protocol validation helper
# ---------------------------------------------------------------------------


def _validate_protocol_compliance(obj: Any, protocol: type) -> None:
    """Ensure *obj* (class or instance) satisfies *protocol*.

    Uses ``isinstance`` for ``@runtime_checkable`` Protocols, with a
    fallback to attribute-based checking for non-runtime-checkable ones.

    Raises ``TypeError`` when validation fails.
    """
    # Fast path: runtime_checkable Protocol
    is_runtime = getattr(protocol, "_is_runtime_protocol", False)

    if is_runtime:
        # For classes, Python's isinstance checks structural subtyping on
        # the class object itself (checking for method descriptors).
        # For instances, it checks the instance's type.
        if not isinstance(obj, protocol):
            raise TypeError(f"{obj!r} does not satisfy protocol {protocol.__name__}")
        return

    # Slow path: attribute-level checking
    # Python 3.12+ exposes __protocol_attrs__; older versions need inspect.
    attrs = getattr(protocol, "__protocol_attrs__", None)
    if attrs is None:
        # Fallback: gather non-dunder methods/properties from the protocol
        attrs = {name for name, _ in inspect.getmembers(protocol) if not name.startswith("_")}

    target = obj if isinstance(obj, type) else type(obj)
    missing = [a for a in attrs if not hasattr(target, a)]
    if missing:
        raise TypeError(
            f"{target.__name__} missing protocol attributes: {', '.join(sorted(missing))}"
        )
