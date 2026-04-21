"""Typed runtime dependencies for connector registrations (Issue #3830).

Each connector declares what it needs at runtime via ``RUNTIME_DEPS`` on
the class (or ``runtime_deps=`` on ``@register_connector``). The factory
calls :func:`check_runtime_deps` right before instantiation and raises
:class:`nexus.contracts.exceptions.MissingDependencyError` when anything
is missing — all missing deps in one message, not first-fail.

Dep types:

* :class:`PythonDep` — importable module name (optionally associated with
  pip extras, used to construct the install hint).
* :class:`BinaryDep` — executable that must be on ``PATH`` (plus a literal
  install-hint string the connector author picks).
* :class:`ServiceDep` — a server-side subsystem (``kernel``, ``metastore``,
  ``token_manager``…). Rejected cleanly on slim wheels where
  ``nexus.server`` is excluded.
"""

from __future__ import annotations

import functools
import importlib.util
import shutil
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PythonDep:
    """A Python importable module that must be available."""

    module: str
    extras: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BinaryDep:
    """An executable that must be on PATH."""

    name: str
    install_hint: str


@dataclass(frozen=True, slots=True)
class ServiceDep:
    """A server-side subsystem required at runtime.

    On slim wheels (where ``nexus.server`` is excluded), any ``ServiceDep``
    fails mount with a ``requires full nexus install`` message.
    """

    name: str


RuntimeDep = PythonDep | BinaryDep | ServiceDep


@functools.cache
def _server_available() -> bool:
    """Return True when the server runtime is importable.

    Used to decide whether ``ServiceDep`` entries can be satisfied. Cached
    for the process lifetime — module presence does not change at runtime.
    """
    return importlib.util.find_spec("nexus.server") is not None


def check_runtime_deps(
    deps: tuple[RuntimeDep, ...],
    *,
    server_available: bool | None = None,
) -> list[tuple[RuntimeDep, str]]:
    """Return (dep, reason) pairs for every unmet dep.

    Collects **all** failures; the caller renders them in a single error so
    the user sees everything they need to install in one pass.

    Args:
        deps: Tuple of runtime-dep declarations.
        server_available: Override for ``_server_available()``; tests use
            this to exercise slim vs. full paths without touching the real
            module state.

    Returns:
        List of ``(dep, reason_string)`` tuples — empty list when all deps
        are satisfied.
    """
    if server_available is None:
        server_available = _server_available()
    missing: list[tuple[RuntimeDep, str]] = []
    for dep in deps:
        match dep:
            case PythonDep(module=mod, extras=extras):
                if importlib.util.find_spec(mod) is None:
                    if extras:
                        hint = f"pip install nexus-fs[{','.join(extras)}]"
                    else:
                        hint = f"pip install {mod}"
                    missing.append((dep, f"python '{mod}': install with: {hint}"))
            case BinaryDep(name=name, install_hint=hint):
                if shutil.which(name) is None:
                    missing.append((dep, f"binary '{name}': not on PATH — install with: {hint}"))
            case ServiceDep(name=name):
                if not server_available:
                    missing.append(
                        (
                            dep,
                            f"service '{name}': requires a full nexus install "
                            f"(slim wheel has no server runtime)",
                        )
                    )
    return missing


__all__ = [
    "BinaryDep",
    "PythonDep",
    "RuntimeDep",
    "ServiceDep",
    "check_runtime_deps",
]
