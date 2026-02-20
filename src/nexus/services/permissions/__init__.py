"""Backward-compat shim — canonical: ``nexus.bricks.rebac`` (Issue #2179).

All permission/ReBAC implementation files have moved to ``nexus.bricks.rebac``.
This package re-exports for backward compatibility.
"""

from __future__ import annotations

import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import sys
from types import ModuleType
from typing import Any


class _PermissionsRedirector(importlib.abc.MetaPathFinder):
    """Redirect ``nexus.services.permissions.X`` → ``nexus.bricks.rebac.X``.

    CPython's ``_load_unlocked`` calls ``_init_module_attrs(override=True)``
    which **mutates** the module returned by ``create_module``.  If we handed
    back the canonical module directly, its ``__name__`` / ``__spec__`` would
    be overwritten with the alias values, corrupting the canonical entry in
    ``sys.modules`` and causing duplicate class identity (different
    ``ZoneIsolationError`` objects, etc.).

    The fix: ``_AliasLoader.create_module`` returns a thin *wrapper* module
    whose ``__dict__`` points at the canonical's ``__dict__``.  CPython mutates
    the wrapper, not the original, preserving class identity across paths.
    """

    _PREFIX = "nexus.services.permissions."
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

        # Build a spec for the alias name.  For packages (modules with
        # __path__), set is_package so _init_module_attrs gets correct
        # submodule_search_locations.
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
    """Loader that creates a thin wrapper sharing the canonical dict.

    The wrapper absorbs CPython's ``_init_module_attrs(override=True)``
    mutations (``__name__``, ``__spec__``, etc.) so the canonical module
    stays pristine.
    """

    def __init__(self, module: ModuleType) -> None:
        self._module = module

    def create_module(self, spec: importlib.machinery.ModuleSpec) -> ModuleType:
        # Create a wrapper that shares __dict__ with the canonical module.
        # This means attribute lookups (classes, functions) resolve to the
        # same objects, preserving isinstance() identity.
        wrapper = ModuleType(spec.name)
        wrapper.__dict__.update(self._module.__dict__)
        # Restore key package attributes from the canonical module.
        if hasattr(self._module, "__path__"):
            wrapper.__path__ = list(self._module.__path__)
        wrapper.__package__ = getattr(self._module, "__package__", self._module.__name__)
        # Also alias in sys.modules so _find_and_load's early-return works.
        sys.modules[spec.name] = wrapper
        return wrapper

    def exec_module(self, module: ModuleType) -> None:
        pass  # Already populated via __dict__.update.


if not any(isinstance(f, _PermissionsRedirector) for f in sys.meta_path):
    sys.meta_path.insert(0, _PermissionsRedirector())
