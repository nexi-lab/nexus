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
    """A Python importable module that must be available.

    ``module`` is the dotted import name (what ``find_spec`` probes and
    what the connector code ``import``s). ``package`` is the pip install
    target to recommend when the module is missing; it differs from
    ``module`` whenever the distribution name on PyPI is not the same as
    the import name (``google-cloud-storage`` → ``google.cloud.storage``,
    ``google-api-python-client`` → ``googleapiclient``, etc.). When
    ``package`` is ``None`` the hint falls back to ``module`` — which is
    only safe for deps whose PyPI name equals the import name.
    """

    module: str
    extras: tuple[str, ...] = ()
    package: str | None = None

    @property
    def install_target(self) -> str:
        """Return the name to use in ``pip install`` fallback hints."""
        return self.package or self.module


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
# tuple of dotted module paths that must all resolve before the service is
# usable. When ``check_runtime_deps`` evaluates a ``ServiceDep``, it probes
# every module in the tuple instead of ``nexus.server`` as a whole — so a
# connector that only needs e.g. ``token_manager`` doesn't get falsely
# gated out when the server package is present but the auth module isn't
# (or vice versa).
#
# Services without an entry here fall back to ``_server_available()``, the
# coarser "full install present" check.
#
# ``token_manager`` keeps a per-module probe so that any external or
# legacy connector / extension manifest that still declares
# ``ServiceDep("token_manager")`` resolves against the auth/oauth bricks
# that the slim wheel force-includes (see packages/nexus-fs/pyproject.toml)
# — the probe must not fall through to the coarser server-runtime check
# (Issue #3947). The probe tuple also lists ``sqlalchemy`` because the
# token_manager module imports it at top level: a base slim install ships
# the bricks file but not sqlalchemy (it lives in the OAuth connector
# extras), so a presence-only probe would falsely report the service
# satisfied and the consumer would hit a raw ``ModuleNotFoundError`` at
# instantiation time. Built-in OAuth manifest entries no longer carry this
# ServiceDep — their PythonDep gates already cover what slim users see —
# but the mapping stays so third-party plugins keep working.
_SERVICE_MODULES: dict[str, tuple[str, ...]] = {
    "token_manager": ("nexus.bricks.auth.oauth.token_manager", "sqlalchemy"),
    "kernel": ("nexus.core.kernel",),
    "record_store": ("nexus.storage.record_store",),
    "metastore": ("nexus.storage",),
}


# Minimum installed-distribution versions per service. Each entry maps
# ``ServiceDep.name`` → ``{distribution_name: (major, minor, ...)}``. A
# service that lists a distribution here is satisfied only when the
# matching entry from ``importlib.metadata.version`` parses to a tuple
# ``>= min_version``. ``find_spec`` alone cannot distinguish SQLAlchemy
# 1.x (compatible API gone) from 2.x — token_manager imports
# ``nexus.storage.models`` which uses ``sqlalchemy.orm.mapped_column``
# and other 2.x-only symbols, so a SQLAlchemy 1.x install would pass the
# presence probe and crash at import time. Pinning the minimum here
# turns that into a clean ``MissingDependencyError`` (Issue #3947).
_SERVICE_MIN_VERSIONS: dict[str, dict[str, tuple[int, ...]]] = {
    "token_manager": {"sqlalchemy": (2, 0)},
}


def _parse_version_prefix(text: str) -> tuple[int, ...]:
    """Parse a leading dotted-int prefix from a PEP 440 version string.

    Returns ``(major, minor, patch, ...)`` for whatever leading
    integer-only segments exist (``"2.0.30"`` → ``(2, 0, 30)``,
    ``"2.0.0rc3"`` → ``(2, 0, 0)``). Non-integer / pre-release / post-
    release suffixes stop parsing — we only need the comparable numeric
    head for our minimum-version probes, and pulling in ``packaging`` as
    a slim base dep would inflate the wheel for one comparison.
    """
    parts: list[int] = []
    for chunk in text.split("."):
        digits = ""
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def _is_prerelease(text: str) -> bool:
    """Return True for PEP 440 prerelease / dev versions.

    Per PEP 440, ``X.Y.Z<marker>`` (e.g. ``2.0.0rc1``, ``2.0.0a3``,
    ``2.0.0.dev0``) sorts strictly **before** the corresponding final
    release ``X.Y.Z``. Post-releases (``X.Y.Z.postN``) and local
    versions (``X.Y.Z+local``) sort at-or-after the final, so they are
    not flagged here. We avoid pulling in ``packaging`` to keep the
    slim wheel's base dependency list thin.
    """
    i = 0
    while i < len(text) and (text[i].isdigit() or text[i] == "."):
        i += 1
    suffix = text[i:].lower()
    if not suffix:
        return False
    if suffix.startswith("+"):
        return False
    if suffix.startswith("post"):
        return False
    if suffix.startswith("dev"):
        return True
    return suffix[0].isalpha()


def _meets_min_version(distribution: str, minimum: tuple[int, ...]) -> bool:
    """Return True when ``distribution`` reports a version ≥ ``minimum``.

    Returns False on lookup / parse failure so the service is reported
    missing rather than silently passing an unverifiable install.
    Prereleases of the minimum (e.g. ``2.0.0rc1`` against minimum
    ``(2, 0)``) are rejected — they sort strictly below the final
    release and may lack the APIs the consumer expects (Issue #3947).
    """
    try:
        installed_text = importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return False
    installed = _parse_version_prefix(installed_text)
    if not installed:
        return False
    # Normalize both sides with trailing zeros so PEP 440 implicit-zero
    # padding lines up: minimum ``(2, 0)`` means the same release as
    # ``2.0.0``, so an installed ``(2, 0, 0)`` must compare equal to it.
    width = max(len(installed), len(minimum))
    installed_padded = installed + (0,) * (width - len(installed))
    minimum_padded = minimum + (0,) * (width - len(minimum))
    if _is_prerelease(installed_text):
        # Prerelease X.Y.Z<marker> sorts strictly below X.Y.Z final, so
        # require strict greater-than the minimum:
        #   * 2.0.0rc1  → (2, 0, 0) ≯ (2, 0, 0) → reject
        #   * 2.0.1rc1  → (2, 0, 1) >  (2, 0, 0) → accept (already past 2.0.0)
        #   * 2.5.0rc1  → (2, 5, 0) >  (2, 0, 0) → accept
        return installed_padded > minimum_padded
    return installed_padded >= minimum_padded


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


def _service_available(name: str) -> bool:
    """Return True when the named service's implementing modules are importable.

    Every module listed in ``_SERVICE_MODULES[name]`` must resolve **and**
    every distribution in ``_SERVICE_MIN_VERSIONS[name]`` must report a
    version ≥ its minimum for the service to count as available. Services
    without an entry fall back to ``_server_available()``, the coarser
    "full install present" check.

    The result is intentionally **not** cached: a service probe may include
    an installable third-party dep (e.g. ``sqlalchemy`` for the token
    manager) which can appear mid-process when users ``pip install`` an
    OAuth extra. Caching would freeze the negative answer and break the
    retry-after-install path. The fallback ``_server_available`` is still
    cached because ``nexus.server`` cannot appear without a process
    restart (Issue #3947).
    """
    module_paths = _SERVICE_MODULES.get(name)
    if module_paths is None:
        return _server_available()
    for module_path in module_paths:
        try:
            spec = importlib.util.find_spec(module_path)
        except (ImportError, ModuleNotFoundError, ValueError):
            return False
        if spec is None:
            return False
    for distribution, minimum in _SERVICE_MIN_VERSIONS.get(name, {}).items():
        if not _meets_min_version(distribution, minimum):
            return False
    return True


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
                        hint = f"pip install {dep.install_target}"
                    missing.append((dep, f"python '{mod}': install with: {hint}"))
            case BinaryDep(name=name, install_hint=hint):
                if shutil.which(name) is None:
                    missing.append((dep, f"binary '{name}': not on PATH — install with: {hint}"))
            case ServiceDep(name=name):
                available = (
                    server_available if server_available is not None else _service_available(name)
                )
                if not available:
                    missing.append((dep, _service_dep_reason(name, server_available)))
    return missing


def _service_dep_reason(name: str, server_available_override: bool | None) -> str:
    """Build a human-readable reason for a missing ``ServiceDep``.

    When the override forces unavailability, the failure is by definition
    "no server runtime" so the legacy message stands. Otherwise, identify
    the specific module that didn't resolve. If it looks like an
    installable third-party package (no ``nexus.`` prefix), point the user
    at ``pip install <pkg>``; otherwise fall back to the "requires a full
    nexus install" message that matches the existing semantics for
    server-only services (Issue #3947).
    """
    if server_available_override is False:
        return f"service '{name}': requires a full nexus install (slim wheel has no server runtime)"
    module_paths = _SERVICE_MODULES.get(name)
    if module_paths:
        for module_path in module_paths:
            try:
                spec = importlib.util.find_spec(module_path)
            except (ImportError, ModuleNotFoundError, ValueError):
                spec = None
            if spec is None:
                if not module_path.startswith("nexus."):
                    return (
                        f"service '{name}': missing python '{module_path}' — "
                        f"install with: pip install {module_path}"
                    )
                return (
                    f"service '{name}': missing module '{module_path}' "
                    f"(requires a full nexus install)"
                )
        for distribution, minimum in _SERVICE_MIN_VERSIONS.get(name, {}).items():
            if not _meets_min_version(distribution, minimum):
                spec_str = ".".join(str(part) for part in minimum)
                return (
                    f"service '{name}': '{distribution}' version too old "
                    f"(need >= {spec_str}) — "
                    f"install with: pip install '{distribution}>={spec_str}'"
                )
    return f"service '{name}': requires a full nexus install (slim wheel has no server runtime)"


__all__ = [
    "BinaryDep",
    "PythonDep",
    "RuntimeDep",
    "ServiceDep",
    "check_runtime_deps",
]
