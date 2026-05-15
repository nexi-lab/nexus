"""Public introspection API for nexus.extensions.

Wraps the manifest store with a stable public surface that the CLI and
HTTP endpoint consume. Lazy: never imports an extension impl module.
"""

from __future__ import annotations

from typing import get_args

from nexus.extensions.manifest import AnyManifest
from nexus.extensions.store import CheckReport, get_store
from nexus.extensions.types import Kind


def list_extensions(
    *,
    kind: Kind | None = None,
    profile: frozenset[str] | None = None,
    available_only: bool = False,
) -> list[AnyManifest]:
    """Return registered manifests, optionally filtered.

    Never imports an extension impl module.
    """
    store = get_store()
    manifests = store.list(kind=kind, profile=profile)
    if available_only:
        manifests = [m for m in manifests if store.check(m).available]
    return manifests


def get_extension(name: str, kind: Kind) -> AnyManifest:
    """Return a single manifest by (kind, name). Raises KeyError if missing."""
    return get_store().get(name, kind=kind)


def check_extension(name: str, kind: Kind) -> CheckReport:
    """Return a CheckReport for the named manifest.

    Reports missing python/binary/service deps and import-probe failures.
    """
    store = get_store()
    return store.check(store.get(name, kind=kind))


def list_kinds() -> list[Kind]:
    """Return the registered extension kinds."""
    return list(get_args(Kind))
