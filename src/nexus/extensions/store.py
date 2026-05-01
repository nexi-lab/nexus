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

import importlib
import importlib.util
import json
import logging
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from importlib.metadata import entry_points as _stdlib_entry_points
from pathlib import Path
from typing import Any

from nexus.extensions.errors import (
    DuplicateManifestError,
    FactoryResolutionError,
    IndexCorruptError,
)
from nexus.extensions.manifest import AnyManifest, parse_manifest
from nexus.extensions.types import Kind

INDEX_SCHEMA_VERSION = 1

logger = logging.getLogger(__name__)


def _entry_points(group: str):
    """Indirection so tests can monkeypatch without touching the stdlib import."""
    return _stdlib_entry_points(group=group)


def _load_manifest_module(manifest_file: Path) -> AnyManifest | None:
    """Import a `_manifest.py` file in isolation and return its MANIFEST constant.

    Returns None on any failure (broken module, missing MANIFEST). Failures are
    logged at WARN level so siblings keep loading.
    """
    try:
        spec = importlib.util.spec_from_file_location(
            f"_nexus_manifest_{manifest_file.parent.name}", manifest_file
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"could not build spec for {manifest_file}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 — isolation is intentional
        logger.warning("Skipping broken manifest at %s: %s", manifest_file, exc)
        return None

    manifest = getattr(module, "MANIFEST", None)
    if manifest is None:
        logger.warning("No MANIFEST constant in %s; skipping", manifest_file)
    return manifest


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

    # --- discovery loaders ---

    _ENTRY_POINT_GROUPS: tuple[str, ...] = (
        "nexus.connectors",
        "nexus.bricks",
        "nexus.plugins",
    )

    def load_entry_points(self) -> None:
        """Discover manifests via importlib.metadata entry points.

        Two entry-point conventions are supported:

        1. Manifest modules — value is a plain module path (no colon). The
           module is imported and its top-level ``MANIFEST`` constant is
           registered. New extensions should ship this way.
        2. Legacy class targets — value is ``module:attr`` (e.g. existing
           ``nexus.plugins`` entries pointing at a NexusPlugin subclass). For
           these we synthesize a PluginManifest from the entry-point metadata
           WITHOUT importing the implementation, so introspection still lists
           the plugin even when its optional dependencies are missing.
        """
        for group in self._ENTRY_POINT_GROUPS:
            for ep in _entry_points(group):
                manifest = self._manifest_from_entry_point(group, ep)
                if manifest is None:
                    continue
                try:
                    self._register(manifest, source=f"entry_point:{group}/{ep.name}")
                except DuplicateManifestError as exc:
                    logger.warning("Duplicate entry-point manifest: %s", exc)

    def _manifest_from_entry_point(self, group: str, ep) -> AnyManifest | None:  # noqa: ANN001
        """Build a manifest for one entry point without importing extension impl.

        Class-style targets (``module:attr``) get a synthesized PluginManifest;
        module-style targets are imported only to read the MANIFEST constant.
        """
        value = ep.value or ""
        if ":" in value:
            # Legacy class target — synthesize without import.
            module_path, _, attr = value.partition(":")
            from nexus.extensions.manifest import PluginManifest

            try:
                return PluginManifest(
                    name=ep.name,
                    module=module_path,
                    factory=attr or ep.name,
                    entry_point_group=group,
                )
            except Exception as exc:  # noqa: BLE001 — surface validation failure
                logger.warning("Skipping invalid entry-point %s in %s: %s", ep.name, group, exc)
                return None

        # Module-style target — import and read MANIFEST. Per-entry isolation:
        # a broken module must not block siblings.
        try:
            module = importlib.import_module(value)
        except Exception as exc:  # noqa: BLE001 — isolation
            logger.warning("Failed to load entry point %s in group %s: %s", ep.name, group, exc)
            return None
        manifest = getattr(module, "MANIFEST", None)
        if manifest is None:
            logger.warning("Entry point %s in group %s has no MANIFEST", ep.name, group)
            return None
        return manifest

    def load_json_index(self, path: Path) -> None:
        """Load manifests from a pre-built `extensions.json` index.

        Behavior:
        - Missing file → INFO log, return (callers fall back to entry-points + fs scan).
        - Malformed JSON → IndexCorruptError.
        - Schema-version mismatch → WARN, skip.
        """
        path = Path(path)
        if not path.exists():
            logger.info("No extensions.json at %s; falling back to live discovery", path)
            return

        try:
            payload = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            raise IndexCorruptError(f"extensions.json at {path} is not valid JSON: {exc}") from exc

        version = payload.get("schema_version")
        if version != INDEX_SCHEMA_VERSION:
            logger.warning(
                "extensions.json schema_version=%s does not match expected %s; ignoring index",
                version,
                INDEX_SCHEMA_VERSION,
            )
            return

        for raw in payload.get("manifests", []):
            try:
                manifest = parse_manifest(raw)
            except Exception as exc:  # noqa: BLE001 — surfacing in log
                logger.warning("Skipping malformed manifest in index: %s", exc)
                continue
            try:
                self._register(manifest, source="json_index")
            except DuplicateManifestError as exc:
                logger.warning("Duplicate index manifest: %s", exc)

    def load_filesystem(self, root: Path) -> None:
        """Scan `root/*/  _manifest.py` and register every `MANIFEST` constant.

        Per-extension import isolation: a broken `_manifest.py` is logged at
        WARN level and skipped; siblings continue loading.
        """
        for child in sorted(Path(root).iterdir()):
            if not child.is_dir():
                continue
            manifest_file = child / "_manifest.py"
            if not manifest_file.exists():
                continue
            manifest = _load_manifest_module(manifest_file)
            if manifest is None:
                continue
            try:
                self._register(manifest, source=f"fs_scan:{manifest_file}")
            except DuplicateManifestError as exc:
                logger.warning("Duplicate manifest skipped: %s", exc)

    # --- introspection API ---

    def check(self, manifest: AnyManifest) -> CheckReport:
        """Run import probes and per-dep checks to report availability.

        - python deps: ``importlib.util.find_spec`` (does NOT execute the module).
        - binary deps: ``shutil.which`` against PATH.
        - service deps: marked unchecked (we can't probe a service without
          opening a connection); they are surfaced via ``unchecked_services``
          so callers know the report is incomplete and ``available`` is False
          unless every declared service is reachable out-of-band.
        - import_probes: ``importlib.import_module`` (the only thing that may
          actually execute extension-adjacent code).
        """
        import shutil

        probe_failures: list[str] = []
        for probe in manifest.import_probes:
            try:
                importlib.import_module(probe)
            except ImportError:
                probe_failures.append(probe)

        missing_python: list[str] = []
        for d in manifest.runtime_deps:
            if d.kind != "python":
                continue
            try:
                spec = importlib.util.find_spec(d.name)
            except (ImportError, ValueError):
                spec = None
            if spec is None:
                missing_python.append(d.name)

        missing_binary = [
            d.name
            for d in manifest.runtime_deps
            if d.kind == "binary" and shutil.which(d.name) is None
        ]
        unchecked_services = tuple(d.name for d in manifest.runtime_deps if d.kind == "service")

        available = (
            not probe_failures
            and not missing_python
            and not missing_binary
            and not unchecked_services
        )

        return CheckReport(
            available=available,
            missing_python_deps=tuple(missing_python),
            missing_binary_deps=tuple(missing_binary),
            missing_services=unchecked_services,
            import_probe_failures=tuple(probe_failures),
            profile_gate_disabled=False,
        )

    # --- runtime API (the only place that imports impl) ---

    def resolve_factory(self, manifest: AnyManifest) -> Callable[..., Any]:
        """Import the impl module and return the named factory callable.

        This is the ONLY method on the store that imports an extension impl
        module. Callers must accept that this triggers optional-dependency
        imports and may raise ImportError chains.
        """
        try:
            module = importlib.import_module(manifest.module)
        except ImportError as exc:
            raise FactoryResolutionError(
                manifest_name=manifest.name,
                module=manifest.module,
                factory=manifest.factory,
                detail=f"import failed: {exc}",
            ) from exc

        try:
            return getattr(module, manifest.factory)
        except AttributeError:
            raise FactoryResolutionError(
                manifest_name=manifest.name,
                module=manifest.module,
                factory=manifest.factory,
                detail="attribute not found in module",
            ) from None


_STORE: ManifestStore | None = None
_STORE_LOCK = threading.Lock()


def get_store() -> ManifestStore:
    """Return the process-wide manifest store, lazily populating on first call.

    Population order: JSON index > entry points > (optional) filesystem scan.
    The filesystem scan is gated behind the NEXUS_EXTENSIONS_DEV_SCAN env var.

    Thread-safe: concurrent first calls all return the same fully-populated
    store. Population happens inside a lock; the unlocked fast path returns
    the singleton once it's been published.
    """
    global _STORE
    if _STORE is not None:
        return _STORE

    with _STORE_LOCK:
        if _STORE is not None:
            return _STORE

        store = ManifestStore()

        # 1. JSON index (shipped with the wheel). Path resolved at import time.
        index_path = Path(__file__).parent / "_index" / "extensions.json"
        store.load_json_index(index_path)

        # 2. Entry points (third-party packages declaring nexus.* groups).
        store.load_entry_points()

        # 3. Filesystem scan (dev-only fallback).
        if os.environ.get("NEXUS_EXTENSIONS_DEV_SCAN") == "1":
            for subdir in ("backends/connectors", "bricks", "plugins"):
                root = Path(__file__).parent.parent / subdir
                if root.exists():
                    store.load_filesystem(root)

        _STORE = store
        return store


def reset_store() -> None:
    """Drop the cached singleton. Test-only; production code should not call this."""
    global _STORE
    with _STORE_LOCK:
        _STORE = None
