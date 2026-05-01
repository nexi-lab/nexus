"""ManifestStore — lazy registry of extension manifests.

Population sources (precedence, first hit wins per (kind, name)):
1. Pre-built JSON index shipped in the wheel.
2. importlib.metadata entry points for nexus.{connectors,bricks,plugins}.
3. Filesystem scan of src/nexus/{backends,bricks,plugins}/*/_manifest.py
   (dev fallback only, controlled by NEXUS_EXTENSIONS_DEV_SCAN env var).

The store NEVER imports an extension impl module from list/get/check.
Only resolve_factory imports impl, and only on demand.
"""

from __future__ import annotations

from dataclasses import dataclass

from nexus.extensions.errors import DuplicateManifestError
from nexus.extensions.manifest import AnyManifest
from nexus.extensions.types import Kind


@dataclass(frozen=True)
class CheckReport:
    """Result of ManifestStore.check() — what's missing for an extension to run."""

    available: bool
    missing_python_deps: tuple[str, ...] = ()
    missing_binary_deps: tuple[str, ...] = ()
    missing_services: tuple[str, ...] = ()
    import_probe_failures: tuple[str, ...] = ()
    profile_gate_disabled: bool = False


class ManifestStore:
    """In-process registry of extension manifests.

    Construction is cheap. Population is lazy via load_*() methods, called by
    the module-level singleton accessor `get_store()`. Tests construct
    `ManifestStore()` directly and use `_register()` to seed.
    """

    def __init__(self) -> None:
        # Keyed by (kind, name) -> (manifest, source_label).
        self._entries: dict[tuple[Kind, str], tuple[AnyManifest, str]] = {}
        # Per-source insertion: tracks which (kind, name) pairs have been added
        # by each source label; lets _register detect "same source twice".
        self._source_entries: dict[str, set[tuple[Kind, str]]] = {}

    # --- read API (lazy: never imports impl modules) ---

    def list(
        self,
        *,
        kind: Kind | None = None,
        profile: frozenset[str] | None = None,
    ) -> list[AnyManifest]:
        results: list[AnyManifest] = []
        for (k, _name), (m, _src) in sorted(self._entries.items()):
            if kind is not None and k != kind:
                continue
            if profile is not None and m.profile_gate is not None and m.profile_gate not in profile:
                continue
            results.append(m)
        return results

    def get(self, name: str, kind: Kind) -> AnyManifest:
        try:
            return self._entries[(kind, name)][0]
        except KeyError:
            raise KeyError(f"No manifest for {kind}/{name}") from None

    # --- internal write API (populated by load_*()) ---

    def _register(self, manifest: AnyManifest, *, source: str) -> None:
        key = (manifest.kind, manifest.name)
        seen_in_source = self._source_entries.setdefault(source, set())
        if key in seen_in_source:
            existing_source = self._entries[key][1]
            raise DuplicateManifestError(
                kind=manifest.kind,
                name=manifest.name,
                sources=(existing_source, source),
            )
        # Cross-source: respect precedence — first source wins.
        if key in self._entries:
            return
        self._entries[key] = (manifest, source)
        seen_in_source.add(key)
