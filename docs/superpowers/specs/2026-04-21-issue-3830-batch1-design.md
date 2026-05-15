# Design: Issue #3830 Batch 1 (A.2 + A.3 + C)

**Issue:** [#3830](https://github.com/nexi-lab/nexus/issues/3830) — follow-ups from sub-project A-full (PR [#3834](https://github.com/nexi-lab/nexus/pull/3834)).

**Scope:** Three sub-projects batched into one PR:

- **A.2** — delete the deprecated `ConnectorInfo.requires` property.
- **A.3** — connector declarations manifest so primary-Python-dep-missing connectors register a placeholder and `BackendFactory.create()` raises `MissingDependencyError` instead of `"Unsupported backend type"`.
- **C** — expand `nexus-fs` pip extras so install-hint strings (`pip install nexus-fs[gmail]`, etc.) resolve to real pip targets.

**Order of work inside the PR:** A.3 first (establishes the manifest story), then C (wires up the extras that A.3's hints point at), then A.2 (removes the backwards-compat shim only after we're sure all deps flow through `runtime_deps` cleanly).

**Dependencies on A-full:** This PR is stacked on `worktree-giggly-juggling-abelson` (PR #3834) which must be merged first or rebased.

## Summary

A-full delivered the typed `RuntimeDep` schema + mount-time factory check, but has a large hole: connectors whose primary Python dep is missing **never enter the registry** (today's `_register_optional_backends()` loop catches `ImportError` silently). Users still see `RuntimeError("Unsupported backend type")` — the exact pre-change bad error.

A.3 closes that hole by pre-registering *placeholder* `ConnectorInfo` entries for every known connector before attempting to import its module. On import success, `@register_connector` overwrites the placeholder with the real class-bound info. On import failure, the placeholder stays, and the factory's existing `check_runtime_deps` call fires cleanly — `MissingDependencyError` with every missing dep enumerated.

A.2 is pure cleanup: the derived `requires` property was kept for one release while callers migrated. Grep shows exactly one live reader (`mount_service.py:842`); update that one caller to read `info.runtime_deps` directly and delete the property.

C is small but load-bearing for A-full's install hints: `pip install nexus-fs[gmail]` currently fails because the `gmail` extra doesn't exist. Add the 6 missing extras (`gmail`, `gcalendar`, `slack`, `anthropic`, `openai`, `x`) and wire them into `[all]`.

## A.3: Declarations manifest

### Problem

`src/nexus/backends/__init__.py:27-79` has:

```python
_OPTIONAL_BACKENDS: dict[str, tuple[str, str]] = {
    "CASLocalBackend": ("nexus.backends.storage.cas_local", "CASLocalBackend"),
    "PathGCSBackend": ("nexus.backends.storage.path_gcs", "PathGCSBackend"),
    # ... 22 entries ...
}
```

Keyed by **class name** (for the lazy `__getattr__` path), not **connector name** (what users mount with). No dep info. `_register_optional_backends()` imports each module and swallows `ImportError` at DEBUG level. Net effect: primary-import-missing connectors are silently absent from the registry.

### Solution

New module `src/nexus/backends/_manifest.py`:

```python
from __future__ import annotations

from dataclasses import dataclass

from nexus.backends.base.runtime_deps import (
    BinaryDep,
    PythonDep,
    RuntimeDep,
    ServiceDep,
)


@dataclass(frozen=True, slots=True)
class ConnectorManifestEntry:
    """Static declaration of a built-in connector.

    Read before any connector module is imported — lets the registry
    pre-register placeholder ``ConnectorInfo`` entries so that
    ``BackendFactory.create()`` can raise ``MissingDependencyError``
    with a full install hint when the connector's own module fails to
    import (typically because its primary Python dep is missing).
    """

    name: str                                  # registry key, e.g. "path_gcs"
    module_path: str                           # dotted import path
    class_name: str                            # for the legacy __getattr__ cache
    description: str
    category: str
    runtime_deps: tuple[RuntimeDep, ...] = ()
    service_name: str | None = None


_GWS_RUNTIME_DEPS = (
    BinaryDep("gws", "brew install nexi-lab/tap/gws"),
    ServiceDep("token_manager"),
)

_GH_RUNTIME_DEPS = (
    BinaryDep("gh", "brew install gh"),
    ServiceDep("token_manager"),
)

CONNECTOR_MANIFEST: tuple[ConnectorManifestEntry, ...] = (
    ConnectorManifestEntry(
        name="path_gcs",
        module_path="nexus.backends.storage.path_gcs",
        class_name="PathGCSBackend",
        description="Google Cloud Storage with direct path mapping",
        category="storage",
        runtime_deps=(PythonDep("google.cloud.storage", extras=("gcs",)),),
        service_name="gcs",
    ),
    ConnectorManifestEntry(
        name="cas_gcs",
        module_path="nexus.backends.storage.cas_gcs",
        class_name="CASGCSBackend",
        description="Google Cloud Storage with CAS addressing",
        category="storage",
        runtime_deps=(PythonDep("google.cloud.storage", extras=("gcs",)),),
        service_name="gcs",
    ),
    # ... every other registered connector ...
)
```

One entry per connector — 22 total, matching A-full's migration inventory. Module import is NEVER triggered by reading the manifest.

### Registration flow (replaces the existing `_register_optional_backends`)

```python
def _register_optional_backends() -> None:
    """Pre-register placeholders from the manifest, then attempt imports.

    Phase 1: For every manifest entry, register a placeholder
    ``ConnectorInfo`` with ``connector_class=None`` and the manifest's
    ``runtime_deps``. Import is NOT attempted.

    Phase 2: For every manifest entry, try to import ``module_path``.
    On success, the module's ``@register_connector`` runs during import
    and overwrites the placeholder via ``allow_overwrite=True``. On
    ImportError, the placeholder stays.

    Phase 3 (existing): External plugins via entry-points, YAML-config
    connectors — unchanged.
    """
    global _optional_backends_registered
    if _optional_backends_registered:
        return
    with _registration_lock:
        if _optional_backends_registered:
            return
        _optional_backends_registered = True

        from nexus.backends._manifest import CONNECTOR_MANIFEST
        from nexus.backends.base.registry import ConnectorRegistry

        # Phase 1: placeholders
        for entry in CONNECTOR_MANIFEST:
            ConnectorRegistry.register_placeholder(entry)

        # Phase 2: real imports — overwrite placeholders on success
        seen_modules: set[str] = set()
        for entry in CONNECTOR_MANIFEST:
            if entry.module_path in seen_modules:
                continue
            seen_modules.add(entry.module_path)
            try:
                importlib.import_module(entry.module_path)
            except ImportError as e:
                _logger.debug(
                    "Connector module %s not available: %s (placeholder registered)",
                    entry.module_path, e,
                )

        # Phase 3 (existing): entry points + YAML configs
        # ... unchanged code ...
```

### New `ConnectorRegistry.register_placeholder()` and class-binding semantics

```python
@classmethod
def register_placeholder(cls, entry: ConnectorManifestEntry) -> None:
    """Register a placeholder ConnectorInfo from manifest.

    Called before connector modules are imported. If the module import
    later succeeds, ``@register_connector`` *binds* the real class to
    this placeholder (see :meth:`register` below) — it does NOT fully
    overwrite. If import fails, the placeholder remains and
    BackendFactory.create() raises MissingDependencyError.
    """
    info = ConnectorInfo(
        name=entry.name,
        connector_class=None,
        description=entry.description,
        category=entry.category,
        runtime_deps=entry.runtime_deps,
        service_name=entry.service_name,
    )
    cls._base.register(entry.name, info, allow_overwrite=True)
```

### `ConnectorRegistry.register()` — placeholder binding

Current `register()` raises if an entry with the same name exists and a different class. After A.3, an entry with `connector_class=None` means "placeholder from manifest — bind my class into it." Update the existing-entry branch:

```python
existing = cls._base.get(name)
if existing is not None:
    if existing.connector_class is None:
        # Placeholder binding path. Attach the class; preserve the
        # manifest-sourced metadata (runtime_deps, description,
        # category, service_name). Caller-supplied runtime_deps /
        # class-attr RUNTIME_DEPS are IGNORED for built-in connectors
        # (manifest is the single source of truth).
        bound = ConnectorInfo(
            name=name,
            connector_class=connector_class,
            description=existing.description,
            category=existing.category,
            runtime_deps=existing.runtime_deps,
            service_name=existing.service_name,
            user_scoped=getattr(connector_class, "user_scoped", False) or False,
            config_mapping=derive_config_mapping(connector_class),
            backend_features=getattr(connector_class, "_BACKEND_FEATURES", frozenset()),
        )
        cls._base.register(name, bound, allow_overwrite=True)
        return

    if existing.connector_class is not connector_class:
        raise ValueError(...)  # existing idempotency check, unchanged
    return  # same class, idempotent, unchanged
```

### Rule for built-in `@register_connector` calls

Every built-in connector's `@register_connector` call now passes **only the name** (the registry key). All other metadata — `description`, `category`, `runtime_deps`, `service_name` — lives in the manifest. Example:

```python
# Before (A-full):
@register_connector(
    "path_gcs",
    description="Google Cloud Storage with direct path mapping",
    category="storage",
    runtime_deps=(PythonDep("google.cloud.storage", extras=("gcs",)),),
    service_name="gcs",
)
class PathGCSBackend(PathAddressingEngine):
    ...

# After (A.3):
@register_connector("path_gcs")
class PathGCSBackend(PathAddressingEngine):
    ...
```

If a built-in connector passes metadata kwargs to `@register_connector`, those kwargs are silently ignored (the placeholder-binding path preserves manifest metadata). A `UserWarning` fires when any metadata kwarg is non-default, pointing the author at the manifest. This prevents drift.

External plugins (not in the manifest) continue to call `register()` through the no-existing-entry branch; their decorator kwargs populate a fresh `ConnectorInfo`. No behavior change for plugins.

### `ConnectorInfo` change

`connector_class` becomes `type[Backend] | None`. Every non-test reader today already assumes it's non-None (they instantiate it); update them to either handle `None` or trust that `BackendFactory.create()` has already gated on it.

### `BackendFactory.create()` update

After the existing `check_runtime_deps` call, add one more line:

```python
missing = check_runtime_deps(info.runtime_deps)
if missing:
    raise MissingDependencyError(backend=backend_type, missing=missing)

if info.connector_class is None:
    # Deps are satisfied (or the manifest declared none), but the
    # module import failed for a different reason — syntax error,
    # circular import, etc. Surface a clear error.
    raise RuntimeError(
        f"Connector '{backend_type}' is declared in the manifest but "
        f"its module failed to import. Check logs for the original "
        f"ImportError."
    )

connector_cls = info.connector_class
```

### Class-level `RUNTIME_DEPS` attrs (removal)

After A.3 the manifest is the single source of truth for built-in connectors. Every `runtime_deps=` kwarg added to `@register_connector` calls in A-full — including the shared `_GWS_RUNTIME_DEPS` / `_GH_RUNTIME_DEPS` constants — moves into the manifest. The `@register_connector` calls shrink to name-only.

This cleans up ~30 lines of metadata duplication across connector files and eliminates the drift risk between manifest and class declarations.

The decorator `runtime_deps=` kwarg stays in the signature for external plugins (they don't appear in the manifest and register directly). No change to that code path.

### Registry-vs-mount gap: now closed

Before A.3:

| Scenario | Behavior |
|---|---|
| `google.cloud.storage` installed, mount `path_gcs` | success |
| `google.cloud.storage` missing, mount `path_gcs` | `RuntimeError("Unsupported backend type: path_gcs")` |
| `gws` binary missing, mount `gws_gmail` | `MissingDependencyError` with `brew install` hint (A-full) |

After A.3:

| Scenario | Behavior |
|---|---|
| `google.cloud.storage` installed, mount `path_gcs` | success (unchanged) |
| `google.cloud.storage` missing, mount `path_gcs` | `MissingDependencyError` with `pip install nexus-fs[gcs]` hint |
| `gws` binary missing, mount `gws_gmail` | `MissingDependencyError` (unchanged) |

### Tests

- `tests/unit/backends/test_manifest.py` (new) — every `CONNECTOR_MANIFEST` entry has a non-empty `name` / `module_path` / `class_name`; union of names is unique; all runtime_deps are typed.
- `tests/unit/backends/test_registry.py` (extend) — `register_placeholder` stores `connector_class=None`; overwrite-on-import succeeds; re-register under same name with a real class replaces the placeholder.
- `tests/integration/backends/test_factory_placeholder.py` (new) — simulate a connector whose manifest-declared deps exist but whose module import fails with a syntax error → expect the "module failed to import" RuntimeError. Simulate the common case: manifest says `PythonDep("not_installed")` + module import fails via ImportError → expect `MissingDependencyError`.
- Update `tests/integration/backends/test_factory_dep_check.py` — every stub connector needs to either be registered through the manifest (cleaner) or through the existing direct-`register_connector` path (current). Keep the current tests; just verify they still pass.

## A.2: Delete deprecated `requires` property

Single live reader in production code:

```python
# src/nexus/bricks/mount/mount_service.py:842
"requires": c.requires,
```

Update to:

```python
# src/nexus/bricks/mount/mount_service.py:842
"requires": [
    d.module for d in c.runtime_deps
    if isinstance(d, PythonDep)
],
```

Add `from nexus.backends.base.runtime_deps import PythonDep` to the imports at the top of `mount_service.py`.

Then delete the `@property requires` method from `ConnectorInfo` in `src/nexus/backends/base/registry.py` and the two tests that exercised it (`test_requires_property_derives_from_python_deps`, `test_requires_property_empty_when_no_python_deps` in `test_registry.py`). Those were always scaffolding for the transition; the real coverage is the `runtime_deps` field itself.

The `requires` keyword on `ConnectorRegistry.register()` and `@register_connector` stays deprecated (ignored + DeprecationWarning) — existing plugin authors may still pass it. Removing it is a separate breaking change outside this PR.

## C: Pip extras expansion

### Current state

`packages/nexus-fs/pyproject.toml`:

```toml
[project.optional-dependencies]
s3 = ["boto3>=1.28"]
gcs = ["google-cloud-storage>=2.0"]
gdrive = [
    "google-api-python-client>=2.0",
    "google-auth-oauthlib>=1.0",
]
edit = ["rapidfuzz>=3.0"]
fsspec = ["fsspec>=2024.0"]
tui = ["textual>=1.0"]
all = [
    "nexus-fs[s3,gcs,gdrive,fsspec,tui,edit]",
]
```

### Target state

```toml
[project.optional-dependencies]
s3 = ["boto3>=1.28"]
gcs = ["google-cloud-storage>=2.0"]
gdrive = [
    "google-api-python-client>=2.0",
    "google-auth-oauthlib>=1.0",
]
gmail = [
    "google-api-python-client>=2.0",
    "google-auth-oauthlib>=1.0",
]
gcalendar = [
    "google-api-python-client>=2.0",
    "google-auth-oauthlib>=1.0",
]
slack = ["slack-sdk>=3.0"]
anthropic = ["anthropic>=0.30"]
openai = ["openai>=1.0"]
x = ["requests-oauthlib>=1.3"]
edit = ["rapidfuzz>=3.0"]
fsspec = ["fsspec>=2024.0"]
tui = ["textual>=1.0"]
all = [
    "nexus-fs[s3,gcs,gdrive,gmail,gcalendar,slack,anthropic,openai,x,fsspec,tui,edit]",
]
```

Notes:
- `gmail` / `gcalendar` / `gdrive` each pull the same two `google-*` packages; they're separate extras so users who want only `gmail` don't get a misleading `gdrive` hint. Package-level deduplication at pip install time is automatic.
- `gws` / `github` are **not** added to extras — they're binary CLIs (`brew install`), not pip installs.
- `x` pulls `requests-oauthlib` to match the connector's actual runtime dep (A-full discovery).

### Root pyproject.toml

The full-install `pyproject.toml` at the repo root has everything bundled into base `dependencies` — these extras are slim-package-only. No change to root pyproject.

### Verification

After merge, `pip install nexus-fs[gmail]` should pull `google-api-python-client` and `google-auth-oauthlib`. A smoke test in CI or a docs sidebar — out of scope for this PR (sub-project D territory).

## Rollout

One PR stacked on `worktree-giggly-juggling-abelson`. Commits in order:

1. **A.3 — manifest module** — `_manifest.py` with all 22 entries
2. **A.3 — registry support** — `register_placeholder`, `ConnectorInfo.connector_class: ... | None`, factory update
3. **A.3 — registration flow** — rewrite `_register_optional_backends`, drop `_OPTIONAL_BACKENDS` dict
4. **A.3 — remove class-level `RUNTIME_DEPS`** — scrub connector files, keep decorator `runtime_deps=` for plugins
5. **A.3 — tests** — new test files, update existing
6. **C — pyproject extras** — add 6 extras + update `[all]`
7. **A.2 — update `mount_service.py:842`** — read `runtime_deps` directly
8. **A.2 — delete `requires` property + the two scaffolding tests**

## Risks

- **Placeholder-overwrite ordering races.** If Phase-2 imports run on a worker thread while Phase-1 placeholders aren't fully flushed, a mount could see `connector_class=None` even for a connector whose module imported cleanly. Mitigation: the existing `_registration_lock` already guards the sequence; keep it.
- **`ConnectorInfo.connector_class: type[Backend] | None` type change.** Any code doing `info.connector_class(...)` or `isinstance(..., info.connector_class)` without a None-check must be updated. Grep shows `BackendFactory.create` is the one hot reader; the new "module failed to import" branch handles the None. Others (tests) will surface via mypy.
- **`_OPTIONAL_BACKENDS` dict removal** breaks the legacy `from nexus.backends import PathGCSBackend` attribute-access pattern (via `__getattr__`). Grep for such usages first; if any exist, keep a thin `_OPTIONAL_BACKENDS` view derived from the manifest just for `__getattr__`.
- **Extras metadata drift.** `gmail` / `gcalendar` / `gdrive` all list the same packages; a future version bump of `google-api-python-client` must update three places. Acceptable given low frequency.

## Acceptance criteria

1. `CONNECTOR_MANIFEST` has one entry per registered built-in connector (22 total).
2. `nexus.backends.base.registry.ConnectorRegistry.register_placeholder()` exists and stores `connector_class=None`.
3. On an environment where `google.cloud.storage` isn't importable, `BackendFactory.create("path_gcs", {})` raises `MissingDependencyError` (not `RuntimeError("Unsupported backend type")`).
4. `ConnectorInfo.requires` property is removed; `mount_service.py:842` reads `runtime_deps` directly; one existing test (`TestConnectorInfoRuntimeDeps`'s two `requires_property_*` tests) is removed.
5. `packages/nexus-fs/pyproject.toml` has 6 new extras (`gmail`, `gcalendar`, `slack`, `anthropic`, `openai`, `x`) and `[all]` is updated.
6. Full test suite still passes (modulo the pre-existing unrelated `test_wrap_compressed`).

## Follow-ups (out of scope for this PR)

- **B** — extract `PathCLIBackend` to a slim-safe module.
- **D** — CI matrix (slim × full × every connector), docs rewrite.
- Potential A.4: remove the `requires=` kwarg from `ConnectorRegistry.register()` and the decorator after external plugin authors have a release to migrate.
