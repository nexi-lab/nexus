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

import contextlib
import importlib
import importlib.util
import json
import logging
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


def _validate_manifest(value: object, *, strict: bool, where: str) -> AnyManifest | None:
    """Coerce a loaded MANIFEST value into a typed AnyManifest.

    Only accepts a discriminated-union subclass (ConnectorManifest,
    BrickManifest, PluginManifest). The bare ExtensionManifest base IS
    instantiable and only requires the base fields, so a manifest like
    `MANIFEST = ExtensionManifest(kind='connector', ...)` would skip
    per-kind required-field validation. We reject the base class explicitly
    and route every accepted instance through parse_manifest so the index
    contract is enforced uniformly.
    """
    if value is None:
        if strict:
            raise RuntimeError(f"no MANIFEST constant in {where}")
        logger.warning("No MANIFEST constant in %s; skipping", where)
        return None

    from nexus.extensions.manifest import ExtensionManifest

    if isinstance(value, ExtensionManifest):
        if type(value) is ExtensionManifest:
            msg = (
                f"MANIFEST in {where} is the abstract ExtensionManifest base; "
                "use ConnectorManifest, BrickManifest, or PluginManifest"
            )
            if strict:
                raise RuntimeError(msg)
            logger.warning(msg)
            return None
        # Re-validate via the discriminated union so per-kind required fields
        # get checked even when the user constructed a subclass directly.
        try:
            return parse_manifest(value.model_dump())
        except Exception as exc:  # noqa: BLE001
            if strict:
                raise RuntimeError(f"MANIFEST in {where} failed re-validation: {exc}") from exc
            logger.warning("Invalid MANIFEST in %s: %s", where, exc)
            return None

    # Try to parse a dict-shaped manifest.
    if not isinstance(value, dict):
        if strict:
            raise RuntimeError(f"MANIFEST in {where} is not a manifest instance or dict")
        logger.warning("MANIFEST in %s is not a manifest instance or dict", where)
        return None
    try:
        return parse_manifest(value)
    except Exception as exc:  # noqa: BLE001 — surface in logs / strict raise
        if strict:
            raise RuntimeError(f"MANIFEST in {where} is not a valid manifest: {exc}") from exc
        logger.warning("Invalid MANIFEST in %s: %s", where, exc)
        return None


def _load_manifest_module(manifest_file: Path, *, strict: bool = False) -> AnyManifest | None:
    """Import a `_manifest.py` file in isolation and return its MANIFEST constant.

    ``strict=False`` (runtime default) — broken modules, missing MANIFEST,
    and invalid MANIFEST shapes are logged at WARN level and return None so
    sibling extensions keep loading.

    ``strict=True`` (index build/verify) — any failure raises so the CI hook
    catches a malformed in-tree manifest instead of silently dropping it from
    the regenerated and committed indexes.
    """
    try:
        spec = importlib.util.spec_from_file_location(
            f"_nexus_manifest_{manifest_file.parent.name}", manifest_file
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"could not build spec for {manifest_file}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception as exc:
        if strict:
            raise RuntimeError(f"failed to load manifest at {manifest_file}: {exc}") from exc
        logger.warning("Skipping broken manifest at %s: %s", manifest_file, exc)  # noqa: BLE001
        return None

    raw = getattr(module, "MANIFEST", None)
    return _validate_manifest(raw, strict=strict, where=str(manifest_file))


def _translate_legacy_dep(dep: object) -> object:
    """Convert a legacy nexus.backends.base.runtime_deps.RuntimeDep to a new
    nexus.extensions.manifest.RuntimeDep, or return None if untranslatable."""
    from nexus.extensions.manifest import RuntimeDep as _RD

    cls_name = type(dep).__name__
    if cls_name == "PythonDep":
        return _RD(kind="python", name=getattr(dep, "package", None) or dep.module)
    if cls_name == "BinaryDep":
        return _RD(kind="binary", name=dep.name)
    if cls_name == "ServiceDep":
        return _RD(kind="service", name=dep.name)
    return None


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

        Conventions:
        - ``nexus.plugins`` — historical group, values are ``module:Class``.
          Synthesize a PluginManifest from the entry-point metadata so the
          plugin shows up in introspection without importing it.
        - ``nexus.connectors`` / ``nexus.bricks`` — new groups, values are
          plain module paths pointing at a `_manifest.py`-style module that
          exposes ``MANIFEST``. A colon here is a misconfiguration: refusing
          to synthesize a fake plugin keeps the manifest's `kind` honest.
        """
        value = ep.value or ""
        if ":" in value:
            if group != "nexus.plugins":
                logger.warning(
                    "Entry point %s in group %s uses class-style value %r; "
                    "this group expects a manifest module path. Skipping.",
                    ep.name,
                    group,
                    value,
                )
                return None
            module_path, _, attr = value.partition(":")
            from nexus.extensions.manifest import PluginManifest

            try:
                # metadata_complete=False — synthesized legacy entry has no
                # hooks/commands/runtime_deps; consumers must treat empty
                # defaults as unknown rather than authoritative.
                return PluginManifest(
                    name=ep.name,
                    module=module_path,
                    factory=attr or ep.name,
                    entry_point_group=group,
                    metadata_complete=False,
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
        raw = getattr(module, "MANIFEST", None)
        return _validate_manifest(raw, strict=False, where=f"entry_point:{group}/{ep.name}")

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

    def load_legacy_connector_manifest(self) -> None:
        """Bridge the legacy ``CONNECTOR_MANIFEST`` tuple into this store.

        ``nexus.backends._manifest.CONNECTOR_MANIFEST`` predates this layer and
        is the source of truth for built-in connector inventory. Reading it
        does NOT import any connector module — the dataclass entries carry
        only metadata. Without this adapter the new introspection surface
        would silently omit every shipped connector except the few migrated
        to ``_manifest.py``, producing false negatives in the CLI / HTTP /
        ``available_only`` paths.
        """
        try:
            from nexus.backends._manifest import (
                CONNECTOR_MANIFEST as _LEGACY_CONNECTOR_MANIFEST,
            )
        except ImportError as exc:
            logger.debug("Legacy connector manifest not present: %s", exc)
            return

        from nexus.extensions.manifest import ConnectorManifest as _CM

        for entry in _LEGACY_CONNECTOR_MANIFEST:
            try:
                runtime_deps = tuple(_translate_legacy_dep(d) for d in entry.runtime_deps)
                # Preserve None service_name — several legacy entries
                # (cas_*, local_connector, github_connector, gws_github)
                # intentionally have no unified service mapping. Fabricating
                # entry.name there would produce bogus service identifiers.
                manifest = _CM(
                    name=entry.name,
                    module=entry.module_path,
                    factory=entry.class_name,
                    description=entry.description,
                    service_name=entry.service_name,
                    runtime_deps=tuple(d for d in runtime_deps if d is not None),
                    # Legacy inventory has no connection_args/capabilities/
                    # config_mapping/user_scoped — flag the record as partial
                    # so consumers don't treat empty defaults as authoritative.
                    metadata_complete=False,
                )
            except Exception as exc:  # noqa: BLE001 — isolation
                logger.warning("Skipping legacy connector entry %s: %s", entry.name, exc)
                continue
            # The new _manifest.py path wins (it loaded first via the index
            # or fs scan). Legacy entry is the fallback.
            with contextlib.suppress(DuplicateManifestError):
                self._register(manifest, source="legacy_inventory")

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

        - python deps: looked up with ``importlib.metadata.distribution`` so
          PyPI distribution names that don't match an importable module name
          (``google-api-python-client``, ``slack-sdk``, etc.) resolve correctly.
        - binary deps: ``shutil.which`` against PATH.
        - service deps: marked unchecked (we can't probe a service without
          opening a connection); they are surfaced via ``missing_services``
          so callers know the report is incomplete and ``available`` is False
          unless every declared service is reachable out-of-band.
        - import_probes: ``importlib.import_module`` — the canonical
          importability signal and the only thing that may execute
          extension-adjacent code.
        """
        import shutil
        from importlib.metadata import PackageNotFoundError, distribution

        probe_failures: list[str] = []
        for probe in manifest.import_probes:
            try:
                importlib.import_module(probe)
            except Exception as exc:  # noqa: BLE001 — degraded probe must not crash check()
                # Native libs, version skew, init-time RuntimeError, OSError on
                # missing system deps — all valid "probe failed" signals. Logging
                # at DEBUG keeps healthy paths quiet while preserving diagnostics.
                logger.debug("Probe %s failed for %s: %s", probe, manifest.name, exc)
                probe_failures.append(probe)

        missing_python: list[str] = []
        for d in manifest.runtime_deps:
            if d.kind != "python":
                continue
            try:
                distribution(d.name)
            except PackageNotFoundError:
                missing_python.append(d.name)

        missing_binary = [
            d.name
            for d in manifest.runtime_deps
            if d.kind == "binary" and shutil.which(d.name) is None
        ]

        # Service deps: defer to the legacy availability check
        # (``nexus.server`` importable). On full installs this is True so
        # service-dependent extensions report available; on slim it's False
        # and the same extensions correctly surface as missing.
        try:
            from nexus.backends.base.runtime_deps import _server_available

            server_present = _server_available()
        except ImportError:
            # Legacy module isn't shipped — assume service is reachable so we
            # don't false-positive every connector that declares a service dep.
            server_present = True
        missing_services = tuple(
            d.name for d in manifest.runtime_deps if d.kind == "service" and not server_present
        )

        available = (
            not probe_failures
            and not missing_python
            and not missing_binary
            and not missing_services
        )

        return CheckReport(
            available=available,
            missing_python_deps=tuple(missing_python),
            missing_binary_deps=tuple(missing_binary),
            missing_services=missing_services,
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

    Population order:
      1. JSON index shipped in the wheel (when present).
      2. Entry points from importlib.metadata.
      3. Filesystem scan of `nexus/{backends/connectors,bricks,plugins}/*/_manifest.py`.
         Always runs in production: in slim builds the JSON index is excluded,
         so this is the only source for shipped in-tree manifests. Cheap because
         it only walks immediate children that contain a `_manifest.py` file.
         The legacy `NEXUS_EXTENSIONS_DEV_SCAN` env var stays as a no-op for
         compatibility — production behavior no longer depends on it.

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

        # 3. Filesystem scan of shipped manifest modules. The store's
        #    first-source-wins precedence means this is a no-op for keys
        #    already loaded from index/entry-points, so it adds zero overhead
        #    when the index is fresh.
        nexus_root = Path(__file__).parent.parent
        for subdir in ("backends/connectors", "bricks", "plugins"):
            root = nexus_root / subdir
            if root.exists():
                store.load_filesystem(root)

        # 4. Legacy connector inventory (CONNECTOR_MANIFEST tuple) so the
        #    new introspection surface lists every shipped connector even
        #    before they're individually migrated to _manifest.py.
        store.load_legacy_connector_manifest()

        _STORE = store
        return store


def reset_store() -> None:
    """Drop the cached singleton. Test-only; production code should not call this."""
    global _STORE
    with _STORE_LOCK:
        _STORE = None
