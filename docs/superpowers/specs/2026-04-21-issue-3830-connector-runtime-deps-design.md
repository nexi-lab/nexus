# Design: Typed runtime-dep schema + mount-time dep check

**Issue:** [#3830](https://github.com/nexi-lab/nexus/issues/3830) — Unify slim and full connector pools behind one registry + per-connector runtime deps.

**Scope:** Sub-project **A-full** of the four-part decomposition (A=runtime-dep schema + check, B=PathCLIBackend slim extract, C=extras expansion, D=CI matrix + docs). A is the foundation; B/C/D depend on it.

## Summary

Replace the untyped `ConnectorInfo.requires: list[str]` field with a typed `RUNTIME_DEPS` declaration (`PythonDep` / `BinaryDep` / `ServiceDep`) and enforce it at mount time through `BackendFactory.create()`. On missing deps the factory raises a new `MissingDependencyError` that enumerates every missing dep with an actionable install hint in one round-trip. Migrate all ~22 currently-registered connectors in the same PR.

Out-of-scope (deferred to later sub-projects): pip extras expansion, PathCLIBackend slim-safe extraction, slim×full CI matrix, deletion of the legacy `requires` property.

## Context

`nexus-fs` (slim) and `nexus-ai-fs` (full) today ship different connector pools via hatch-wheel include/exclude rules. Slim users who try a connector only present in full get `ImportError` or `PathNotMounted` — no install hint, no actionable path forward. The registry already has a `requires: list[str]` field, but it is untyped strings, never checked, and only used by `cli/commands/connectors.py` for display. We keep inheriting this because every new connector PR must answer "is this slim-safe?" without any structural help.

This sub-project gives the registry a typed runtime-dep schema plus a single mount-time enforcement point so later work (extras, PathCLIBackend extract, CI matrix) can lean on it rather than re-inventing dep declarations.

## Design

### 1. Runtime-dep types

New module `src/nexus/backends/base/runtime_deps.py`:

```python
from __future__ import annotations
from dataclasses import dataclass

@dataclass(frozen=True, slots=True)
class PythonDep:
    """A Python importable module that must be available."""
    module: str                    # importable dotted name, e.g. "google.api_core"
    extras: tuple[str, ...] = ()   # pip extras that pull this in, e.g. ("gcs",)

@dataclass(frozen=True, slots=True)
class BinaryDep:
    """An executable that must be on PATH."""
    name: str                      # binary basename, e.g. "gws"
    install_hint: str              # actionable install command shown on failure

@dataclass(frozen=True, slots=True)
class ServiceDep:
    """A server-side subsystem that the connector requires at runtime.

    Advertised for discovery. On slim wheels (where `nexus.server` is
    excluded), any ServiceDep causes mount to fail cleanly with
    `requires_server=True`-style messaging.
    """
    name: str                      # e.g. "kernel", "metastore", "token_manager"

RuntimeDep = PythonDep | BinaryDep | ServiceDep
```

Frozen + slots for hashability and memory. The union type enables exhaustive `match` in the check logic.

### 2. Declaration on the connector class

Primary site — class attr `RUNTIME_DEPS` on the connector:

```python
@register_connector("path_gcs", category="storage")
class PathGCSBackend(PathAddressingEngine):
    RUNTIME_DEPS: ClassVar[tuple[RuntimeDep, ...]] = (
        PythonDep("google.cloud.storage", extras=("gcs",)),
    )
    # ...
```

Optional decorator override for connectors that don't own their class (e.g. YAML-loaded dynamic classes in `cli/loader.py`):

```python
@register_connector("cli:mycli", runtime_deps=(BinaryDep("mycli", "..."),))
```

Resolution order at registration: decorator arg wins if both present and a `UserWarning` is emitted; otherwise class attr; otherwise empty tuple.

### 3. Registry wiring

Extend `ConnectorInfo`:

```python
@dataclass
class ConnectorInfo:
    # ... existing fields ...
    runtime_deps: tuple[RuntimeDep, ...] = ()

    @property
    def requires(self) -> list[str]:
        """Deprecated — derived from runtime_deps. Use runtime_deps directly.

        Scheduled for removal once no internal readers remain (tracked in A.2).
        """
        return [d.module for d in self.runtime_deps if isinstance(d, PythonDep)]
```

`ConnectorRegistry.register()`:
- If `runtime_deps` kwarg is provided, use it; else read class attr `RUNTIME_DEPS`; else `()`.
- Validate every entry `isinstance(d, RuntimeDep)`; raise `ValueError` on bad entry (fail at import, not at mount).
- If both decorator arg and class attr are set and differ, decorator wins; emit `UserWarning`.

### 4. Mount-time enforcement

New `MissingDependencyError(BackendError)` in `nexus/contracts/exceptions.py`:

```python
class MissingDependencyError(BackendError):
    """One or more runtime dependencies for a connector are missing.

    Attributes:
        backend: connector name
        missing: list of (dep, reason) pairs
    """
    def __init__(self, backend: str, missing: list[tuple[RuntimeDep, str]]) -> None:
        self.missing = missing
        lines = [f"mount '{backend}' failed: missing {len(missing)} runtime dep(s)"]
        for dep, reason in missing:
            lines.append(f"  - {reason}")
        super().__init__("\n".join(lines), backend=backend)
```

New helper in `runtime_deps.py`:

```python
def check_runtime_deps(
    deps: tuple[RuntimeDep, ...],
    *,
    server_available: bool | None = None,
) -> list[tuple[RuntimeDep, str]]:
    """Return list of (dep, reason) pairs for every unmet dep.

    Collects *all* failures (not first-fail) so the caller can show the
    user every missing piece in one pass.
    """
    if server_available is None:
        server_available = _server_available()
    missing: list[tuple[RuntimeDep, str]] = []
    for dep in deps:
        match dep:
            case PythonDep(module=mod, extras=extras):
                if importlib.util.find_spec(mod) is None:
                    hint = (
                        f"pip install nexus-fs[{','.join(extras)}]"
                        if extras else f"pip install {mod}"
                    )
                    missing.append((dep, f"python '{mod}': install with: {hint}"))
            case BinaryDep(name=name, install_hint=hint):
                if shutil.which(name) is None:
                    missing.append((dep, f"binary '{name}': not on PATH — install with: {hint}"))
            case ServiceDep(name=name):
                if not server_available:
                    missing.append((
                        dep,
                        f"service '{name}': requires a full nexus install (slim wheel has no server runtime)",
                    ))
    return missing


@functools.cache
def _server_available() -> bool:
    return importlib.util.find_spec("nexus.server") is not None
```

`_server_available()` is cached — the module-presence boolean never changes within a process.

Integration in `BackendFactory.create()` (insert after `get_info`, before kwargs build):

```python
info = ConnectorRegistry.get_info(backend_type)

missing = check_runtime_deps(info.runtime_deps)
if missing:
    raise MissingDependencyError(backend=backend_type, missing=missing)

connector_cls = info.connector_class
# ... existing flow ...
```

### 5. Per-connector migration inventory

All 22 `@register_connector` sites updated in the same PR. Full list:

| Connector | Module | RUNTIME_DEPS |
|---|---|---|
| `cas_local` | `backends/storage/cas_local.py` | `()` |
| `path_local` | `backends/storage/path_local.py` | `()` |
| `local_connector` | `backends/storage/local_connector.py` | `()` |
| `path_gcs` | `backends/storage/path_gcs.py` | `PythonDep("google.cloud.storage", extras=("gcs",))` |
| `cas_gcs` | `backends/storage/cas_gcs.py` | `PythonDep("google.cloud.storage", extras=("gcs",))` |
| `path_s3` | `backends/storage/path_s3.py` | `PythonDep("boto3", extras=("s3",))` |
| `path_gdrive` | `backends/connectors/gdrive/connector.py` | `PythonDep("googleapiclient", extras=("gdrive",)), PythonDep("google_auth_oauthlib", extras=("gdrive",))` |
| `gmail` | `backends/connectors/gmail/connector.py` | `PythonDep("googleapiclient", extras=("gmail",))` |
| `gcalendar` | `backends/connectors/calendar/connector.py` | `PythonDep("googleapiclient", extras=("gcalendar",))` |
| `x` | `backends/connectors/x/connector.py` | `()` (uses httpx — core) |
| `slack` | `backends/connectors/slack/connector.py` | `PythonDep("slack_sdk", extras=("slack",))` |
| `hn` | `backends/connectors/hn/connector.py` | `()` (uses httpx — core) |
| `anthropic_native` | `backends/compute/anthropic_native.py` | `PythonDep("anthropic", extras=("anthropic",))` |
| `openai_compatible` | `backends/compute/openai_compatible.py` | `PythonDep("openai", extras=("openai",))` |
| `gws:gmail`, `gws:calendar`, `gws:sheets`, `gws:docs`, `gws:chat`, `gws:drive` | `backends/connectors/gws/connector.py` | `BinaryDep("gws", "brew install nexi-lab/tap/gws"), ServiceDep("token_manager")` |
| `github` (2 registrations) | `backends/connectors/github/connector.py` | `BinaryDep("gh", "brew install gh"), ServiceDep("token_manager")` |

Exact import-module names are verified per connector during implementation (grep import lines); install hints for binary deps taken from each tool's official install docs.

### 6. Backwards compatibility

- `ConnectorInfo.requires` becomes a `@property` derived from `RUNTIME_DEPS`. Every current caller continues to read it as a list of strings. Tracked for removal in follow-up A.2 once no internal readers remain.
- `@register_connector(requires=[...])` keyword is kept for one release as a soft deprecation: when passed, it is ignored and a `DeprecationWarning` is emitted pointing at `RUNTIME_DEPS` / `runtime_deps=`. No current external code depends on it (registry is internal), but keeping it avoids churn in the same PR.
- Existing decorator args (`description`, `category`, `service_name`) unchanged.

### 7. Tests

| Test file | Covers |
|---|---|
| `tests/unit/backends/test_runtime_deps.py` (new) | Each dep type check in isolation (present / missing). Aggregation behavior (all missing collected). Cached `_server_available`. |
| `tests/unit/backends/test_registry.py` (extend) | `runtime_deps` kwarg on `@register_connector`. Decorator vs. class-attr precedence + warning. Bad-type validation. Legacy `requires` property derived correctly. |
| `tests/integration/backends/test_factory_dep_check.py` (new) | Real `BackendFactory.create()` path with a fake connector that declares all three dep types, asserting `MissingDependencyError` enumerates all missing. |

No changes to existing connector tests — `RUNTIME_DEPS` is additive and mock fixtures already patch `find_spec`/`shutil.which` transitively through connector setup.

## Rollout

One PR. All 22 connectors migrated together. Flow:

1. Add `runtime_deps.py` + `MissingDependencyError`.
2. Extend `ConnectorInfo`, `ConnectorRegistry.register`, and `@register_connector`.
3. Wire check into `BackendFactory.create()`.
4. Update every connector's registration with `RUNTIME_DEPS`.
5. Add tests.
6. Verify `nexus connectors list` still works (reads `requires` property — should still render).

No feature flag. The only user-visible change is *better* mount errors, which is strictly an improvement over today's `ImportError`.

## Risks

- **Registration-vs-mount gap for primary Python deps.** Today `_register_optional_backends()` in `backends/__init__.py` calls `importlib.import_module(...)` inside a `try/except ImportError` that silently debug-logs. If a connector's primary Python dep is missing, its module never imports, `@register_connector` never runs, and the registry has no entry. `BackendFactory.create()` then raises `RuntimeError("Unsupported backend type")` before the new dep check can run. The check lands cleanly for: (a) `BinaryDep` (import succeeds, binary absent), (b) `ServiceDep` (import succeeds, server runtime absent), (c) *secondary* `PythonDep` (module imports but some submodule dep is missing). It does **not** yet land for primary-import-failure cases — those still produce today's unhelpful error. Closing this gap needs a declarations manifest (connector → (module, class, RUNTIME_DEPS)) that is read *before* attempting the module import; if deps aren't met, a placeholder `ConnectorInfo` with `connector_class=None` is registered and `BackendFactory.create()` raises `MissingDependencyError` on lookup. That refactor is deferred to sub-project **A.3** to keep this PR single-purpose. Full slim-safety of every connector's registration module is also a precondition — some connectors do top-level `from X import Y` that would need to move behind lazy imports or be covered by the manifest fallback. This is an honest narrowing of the scope sold to the user, not a pivot: A still delivers the typed-dep schema, the check, all 22 migrations, and full wins for binary/service deps.
- **False positives from `find_spec`**: a module may be importable but broken (native lib mismatch). Accepted — `find_spec` matches how pip thinks about installation; runtime exceptions still surface.
- **`nexus.server` probe is implicit.** If someone in the future adds `nexus.server` back to the slim wheel, `ServiceDep` stops rejecting on slim. Mitigated by the CI matrix (sub-project D) that would catch this.
- **22-connector PR is large.** Mitigated: each connector change is 1–5 lines + import; diff per file is trivial; reviewer can scan the inventory table above and spot-check.
- **`google.api_core` vs. `googleapiclient` confusion**: the inventory table lists module names to verify; implementation verifies each by reading the connector's actual imports, not by guessing.

## Acceptance criteria (for this sub-project)

1. `RuntimeDep` union exported from `nexus.backends.base.runtime_deps`.
2. `ConnectorInfo.runtime_deps` populated from decorator or class attr; validated at register-time.
3. `BackendFactory.create()` raises `MissingDependencyError` enumerating every missing dep with an install hint.
4. All 22 registered connectors declare `RUNTIME_DEPS`.
5. Unit + integration tests pass.
6. `nexus connectors list` CLI output unchanged (`requires` property still works).

## Follow-ups (not this sub-project)

- **A.2** — remove the deprecated `ConnectorInfo.requires` property once no internal readers remain (grep shows zero).
- **A.3** — declarations manifest so primary-Python-dep-missing connectors register a placeholder `ConnectorInfo` (no `connector_class`) and `BackendFactory.create()` raises `MissingDependencyError` instead of `"Unsupported backend type"`. Closes the registration-vs-mount gap called out in Risks.
- **B** — extract `PathCLIBackend` + `DisplayPathMixin` + CLI scaffolding to a slim-safe module; update pre-commit brick-check allowlist.
- **C** — expand `nexus-fs` pip extras (`[gws]`, `[notion]`, `[all]`) so `install_hint` strings become accurate pip commands.
- **D** — connector CI matrix running every connector against slim and full install profiles; rewrite docs to deprecate "slim has a subset" framing.
