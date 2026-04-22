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
import importlib.metadata
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

    Used as the fallback gate for ``ServiceDep`` when there's no
    service-specific probe. Cached for the process lifetime — module
    presence does not change at runtime.
    """
    return importlib.util.find_spec("nexus.server") is not None


# Per-service probe mapping. Each entry maps a ``ServiceDep.name`` to the
# dotted module path that implements the service. When ``check_runtime_deps``
# evaluates a ``ServiceDep``, it tests the specific module rather than
# ``nexus.server`` as a whole — so a connector that only needs
# ``token_manager`` doesn't get falsely gated out when the server package
# is present but the auth module isn't (or vice versa).
#
# Services without an entry here fall back to ``_server_available()``, the
# coarser "full install present" check.
_SERVICE_MODULES: dict[str, str] = {
    "token_manager": "nexus.bricks.auth.oauth.token_manager",
    "kernel": "nexus.core.kernel",
    "record_store": "nexus.storage.record_store",
    "metastore": "nexus.storage",
}


@functools.cache
def _nexus_fs_extras_available() -> bool:
    """Return True when the active distribution exposes ``nexus-fs`` extras.

    The runtime ships under two mutually exclusive distributions:
    ``nexus-fs`` (slim wheel) defines extras like ``[gcs]``, ``[gmail]``;
    ``nexus-ai-fs`` (full monorepo) does **not** — it pulls those packages
    in as hard deps. Hardcoding ``pip install nexus-fs[...]`` as the hint
    would direct full-runtime users to install a conflicting distribution.

    Return True only when the nexus namespace resolves to ``nexus-fs``
    alone; in every other case (full install, ambiguous multi-dist,
    development checkout, detection failure) return False so the hint
    falls back to the raw module name.
    """
    try:
        dists = importlib.metadata.packages_distributions().get("nexus", ())
    except Exception:
        return False
    dist_set = set(dists)
    return dist_set == {"nexus-fs"}


@functools.cache
def _service_available(name: str) -> bool:
    """Return True when the named service's implementing module is importable."""
    module_path = _SERVICE_MODULES.get(name)
    if module_path is None:
        return _server_available()
    try:
        return importlib.util.find_spec(module_path) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


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
        server_available: Override for ``_server_available()`` / per-service
            probes; tests use this to exercise slim vs. full paths without
            touching the real module state. When set, it forces the answer
            for **every** ``ServiceDep`` (including services with a specific
            probe).

    Returns:
        List of ``(dep, reason_string)`` tuples — empty list when all deps
        are satisfied.
    """
    missing: list[tuple[RuntimeDep, str]] = []
    for dep in deps:
        match dep:
            case PythonDep(module=mod, extras=extras):
                # find_spec() raises ModuleNotFoundError for dotted names
                # when the parent package is absent (e.g. "google.cloud.storage"
                # when "google" is missing). Treat any import-resolution
                # failure as "not installed" so the user still gets a clean
                # MissingDependencyError with install hint.
                try:
                    spec = importlib.util.find_spec(mod)
                except (ImportError, ModuleNotFoundError, ValueError):
                    spec = None
                if spec is None:
                    if extras and _nexus_fs_extras_available():
                        hint = f"pip install nexus-fs[{','.join(extras)}]"
                    else:
                        hint = f"pip install {mod}"
                    missing.append((dep, f"python '{mod}': install with: {hint}"))
            case BinaryDep(name=name, install_hint=hint):
                if shutil.which(name) is None:
                    missing.append((dep, f"binary '{name}': not on PATH — install with: {hint}"))
            case ServiceDep(name=name):
                available = (
                    server_available if server_available is not None else _service_available(name)
                )
                if not available:
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
