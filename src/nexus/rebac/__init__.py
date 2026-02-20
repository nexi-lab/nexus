"""Backward-compatibility shim — delegates to ``nexus.bricks.rebac`` (Issue #2179).

All code has moved to ``nexus.bricks.rebac``.  This shim ensures that
existing ``from nexus.rebac import ...`` and
``from nexus.rebac.<submodule> import ...`` statements continue to work
without modification via a sys.meta_path redirector.

Consumers should migrate to ``from nexus.bricks.rebac import ...`` at
their convenience.
"""

from __future__ import annotations

import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import sys
from types import ModuleType
from typing import Any


class _ReBACRedirector(importlib.abc.MetaPathFinder):
    """Redirect ``nexus.rebac.X`` → ``nexus.bricks.rebac.X`` transparently.

    Uses a wrapper-module approach so that CPython's
    ``_init_module_attrs(override=True)`` mutates the wrapper, **not** the
    canonical module.  This preserves class identity (``isinstance``) across
    all three import paths (``nexus.bricks.rebac``, ``nexus.rebac``,
    ``nexus.services.permissions``).
    """

    _PREFIX = "nexus.rebac."
    _CANONICAL = "nexus.bricks.rebac."

    def find_spec(
        self,
        fullname: str,
        _path: Any = None,
        _target: ModuleType | None = None,
    ) -> importlib.machinery.ModuleSpec | None:
        if not fullname.startswith(self._PREFIX):
            return None

        canonical = self._CANONICAL + fullname[len(self._PREFIX) :]

        if canonical in sys.modules:
            mod = sys.modules[canonical]
        else:
            canonical_spec = importlib.util.find_spec(canonical)
            if canonical_spec is None:
                return None
            mod = importlib.import_module(canonical)

        is_pkg = hasattr(mod, "__path__")
        spec = importlib.machinery.ModuleSpec(
            fullname,
            _AliasLoader(mod),
            is_package=is_pkg,
        )
        if is_pkg:
            spec.submodule_search_locations = list(mod.__path__)
        return spec


class _AliasLoader(importlib.abc.Loader):
    """Loader that creates a thin wrapper sharing the canonical dict."""

    def __init__(self, module: ModuleType) -> None:
        self._module = module

    def create_module(self, spec: importlib.machinery.ModuleSpec) -> ModuleType:
        wrapper = ModuleType(spec.name)
        wrapper.__dict__.update(self._module.__dict__)
        if hasattr(self._module, "__path__"):
            wrapper.__path__ = list(self._module.__path__)
        wrapper.__package__ = getattr(self._module, "__package__", self._module.__name__)
        sys.modules[spec.name] = wrapper
        return wrapper

    def exec_module(self, module: ModuleType) -> None:
        pass


# Install the redirector so nexus.rebac.X submodule imports work.
if not any(isinstance(f, _ReBACRedirector) for f in sys.meta_path):
    sys.meta_path.insert(0, _ReBACRedirector())

# Eagerly re-export types (no circular import risk — contracts is leaf-level).
from nexus.contracts.rebac_types import (  # noqa: E402
    WILDCARD_SUBJECT,
    CheckResult,
    ConsistencyLevel,
    ConsistencyMode,
    ConsistencyRequirement,
    Entity,
    GraphLimitExceeded,
    GraphLimits,
    TraversalStats,
    WriteResult,
)

# Lazy imports — delegate to nexus.bricks.rebac.__getattr__
_LAZY_IMPORTS = {
    "ReBACManager",
    "PermissionEnforcer",
    "EntityRegistry",
    "NamespaceManager",
    "AsyncReBACManager",
    "AsyncPermissionEnforcer",
    "MemoryPermissionEnforcer",
    "AsyncCircuitBreaker",
    "CircuitBreakerConfig",
}

__all__ = [
    "CheckResult",
    "ConsistencyLevel",
    "ConsistencyMode",
    "ConsistencyRequirement",
    "Entity",
    "GraphLimitExceeded",
    "GraphLimits",
    "TraversalStats",
    "WILDCARD_SUBJECT",
    "WriteResult",
    *sorted(_LAZY_IMPORTS),
]


def __getattr__(name: str) -> object:
    if name in _LAZY_IMPORTS:
        import nexus.bricks.rebac  # noqa: F811

        return getattr(nexus.bricks.rebac, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
