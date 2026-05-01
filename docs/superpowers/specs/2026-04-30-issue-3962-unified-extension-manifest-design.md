# Unified Extension Manifest — Design

**Issue:** [#3962](https://github.com/nexi-lab/nexus/issues/3962) — refactor: unify plugin, connector, and brick discovery metadata
**Date:** 2026-04-30
**Author:** windoliver

## Context

Nexus has three independent discovery paths solving overlapping problems:

| Path | Today's discovery | Today's metadata |
|---|---|---|
| `PluginRegistry` | `importlib.metadata.entry_points("nexus.plugins")` | `PluginMetadata` dataclass + `PluginInfo` |
| `ConnectorRegistry` | `@register_connector` decorator + `_manifest.py` placeholders | `ConnectorInfo` (runtime_deps, capabilities, connection_args) |
| `factory/_bricks.py` | `pkgutil.iter_modules("nexus.bricks")` scanning `brick_factory.py` | Module-level constants + `BrickFactoryDescriptor` |

Each path identifies an extension, inspects metadata, validates dependencies, instantiates it, and wires lifecycle hooks. The metadata models diverge; the introspection surface diverges (separate CLIs); failure isolation is uneven; "list extensions without importing optional deps" is only partly possible today.

## Goal

One **extension metadata model** describing plugins, connectors, and bricks without collapsing their runtime interfaces. Existing registries become thin adapters over a shared store. Introspection is metadata-first: enumerate everything without importing impl modules.

## Non-Goals

- Replacing connector/brick/plugin runtime protocols.
- Heavyweight DI framework.
- Wiring of `produces`/`consumes` between bricks (declared, not consumed yet — follow-up).
- Migrating every in-tree extension (one of each kind, rest stay on legacy via adapters).

## Decisions Locked

| Decision | Choice |
|---|---|
| Source of truth | Hybrid: Python dataclasses canonical, generated `extensions.json` index for zero-import enumeration |
| Backwards compat | Adapter shim with `DeprecationWarning`, two-minor-release window (N+2) |
| CLI shape | New cross-kind `nexus extensions` + existing per-kind CLIs delegate underneath |
| Contract-shaping priority | Connectors first (richest existing model) → bricks → plugins; not a PR sequence, just whose fields drive the base shape |
| Manifest model | Pydantic v2 discriminated union; base + kind-specific subclasses |
| Migration scope | One in-tree extension per kind migrated as proof; remainder via follow-up issues |

---

## Architecture

Five layers, top to bottom. The manifest contract is the foundation; everything else depends on it.

```
                                ┌──────────────────────────┐
                                │ Introspection (list/info)│
                                └────────────┬─────────────┘
                                             ▼
            ┌────────────┬────────────┬────────────┐
            │ Connector  │   Brick    │   Plugin   │  per-kind adapters
            │  Registry  │  Factory   │  Registry  │  (runtime construction)
            └─────┬──────┴─────┬──────┴─────┬──────┘
                  │            │            │
                  └──────┬─────┴────────────┘
                         ▼
                ┌──────────────────┐
                │  Manifest Store  │  lazy lookup, no impl imports
                └────────┬─────────┘
                         │
        ┌────────────────┼─────────────────────┐
        ▼                ▼                     ▼
  entry_points     filesystem scan      extensions.json (built)
```

**Boundary rule:** `nexus.extensions.manifest` has zero dependencies on connector/brick/plugin runtime code. Everything else imports it; it imports nothing else.

### Layer 1 — Manifest contract

`nexus/extensions/manifest.py` exports a Pydantic v2 discriminated union.

```python
class RuntimeDep(BaseModel):
    kind: Literal["python", "binary", "service"]
    name: str
    extras: tuple[str, ...] = ()
    install_hint: str | None = None

class ExtensionManifest(BaseModel):
    name: str                              # unique within kind
    kind: Literal["connector", "brick", "plugin"]   # discriminator
    module: str                            # dotted path; NOT imported by manifest
    factory: str                           # callable/class name in module
    description: str = ""
    runtime_deps: tuple[RuntimeDep, ...] = ()
    config_schema: str | None = None       # dotted path to Pydantic model
    profile_gate: str | None = None        # None == always enabled
    import_probes: tuple[str, ...] = ()    # optional dep modules; checked w/o raising

class ConnectorManifest(ExtensionManifest):
    kind: Literal["connector"] = "connector"
    service_name: str
    capabilities: frozenset[BackendFeature] = frozenset()
    connection_args: dict[str, ConnectionArg] = {}
    user_scoped: bool = False
    config_mapping: dict[str, str] = {}

class BrickManifest(ExtensionManifest):
    kind: Literal["brick"] = "brick"
    tier: Literal["independent", "dependent"]
    result_key: str
    produces: tuple[str, ...] = ()
    consumes: tuple[str, ...] = ()

class PluginManifest(ExtensionManifest):
    kind: Literal["plugin"] = "plugin"
    entry_point_group: str = "nexus.plugins"
    hooks: dict[str, str] = {}             # hook_name -> dotted callable path
    commands: dict[str, str] = {}          # cmd_name -> dotted callable path
    # version/author/homepage pulled from importlib.metadata (package-level)

AnyManifest = Annotated[
    Union[ConnectorManifest, BrickManifest, PluginManifest],
    Field(discriminator="kind"),
]
```

**Storage convention:** each extension declares its manifest in a sibling `_manifest.py` module that imports nothing from the impl. Index generation imports only this file.

```
src/nexus/backends/s3/
    _manifest.py        # MANIFEST = ConnectorManifest(...)   ← imported by index
    backend.py          # actual S3Backend class                ← imported only on use
```

### Layer 2 — Manifest store

`nexus/extensions/store.py`:

```python
class ManifestStore:
    def list(self, *, kind: Kind | None = None,
             profile: ProfileSet | None = None,
             include_unavailable: bool = False) -> list[AnyManifest]: ...
    def get(self, name: str, kind: Kind) -> AnyManifest: ...
    def check(self, m: AnyManifest) -> CheckReport: ...
    def resolve_factory(self, m: AnyManifest) -> Callable: ...
```

**Population sources** (precedence, first hit wins):

1. **Pre-built JSON index** (`nexus/extensions/_index/extensions.json`, shipped in wheel)
2. **`importlib.metadata.entry_points`** for groups `nexus.connectors`, `nexus.bricks`, `nexus.plugins` (entry-point target = `_manifest` module)
3. **Filesystem scan** dev fallback (`NEXUS_EXTENSIONS_DEV_SCAN=1`) walking `src/nexus/{backends,bricks,plugins}/*/_manifest.py`

**Laziness invariants:**

- `list()`, `get()`, `check()` never import an impl module.
- `resolve_factory()` is the only entry point that imports impl.
- Importing `nexus.extensions.store` does not transitively import any extension impl.

### Layer 3 — Index generator

`nexus.extensions.index` build tool (`python -m nexus.extensions.index build`):

- Imports each declared `_manifest` module.
- Validates with the Pydantic discriminated union.
- Writes `extensions.json` deterministic (sorted by `(kind, name)`, stable formatting).
- Hooked into `pyproject.toml` build path.
- CI drift check: regenerate, diff against committed file, fail on mismatch.

JSON format:

```json
{
  "schema_version": 1,
  "generated_at": "2026-04-30T12:00:00Z",
  "manifests": [
    {
      "kind": "connector",
      "name": "s3",
      "module": "nexus.backends.s3.backend",
      "factory": "S3Backend",
      "service_name": "s3",
      "capabilities": ["streaming"],
      "runtime_deps": [
        {"kind": "python", "name": "boto3", "install_hint": "pip install nexus[s3]"}
      ],
      "import_probes": ["boto3"]
    }
  ]
}
```

### Layer 4 — Per-kind adapters

Existing names stay, signatures preserved. Internals delegate to the store.

**`ConnectorRegistry`** (`nexus/backends/base/registry.py`):

```python
class ConnectorRegistry:
    @classmethod
    def list(cls) -> list[ConnectorInfo]:
        return [_to_connector_info(m) for m in store.list(kind="connector")]
    @classmethod
    def get(cls, name: str) -> ConnectorInfo:
        return _to_connector_info(store.get(name, kind="connector"))
```

`@register_connector(...)` stays callable — emits `DeprecationWarning`, writes a `ConnectorManifest` into the store at decorator time. `ConnectorManifestEntry` becomes a thin wrapper around `ConnectorManifest`.

**Brick factory** (`nexus/factory/_bricks.py`):

`_discover_brick_factories(tier)` becomes `store.list(kind="brick")` filtered by tier. Module-level `BRICK_NAME`/`TIER`/`MANIFEST` constants in `brick_factory.py` are read by a transitional reader that synthesizes a `BrickManifest` when no sibling `_manifest.py` exists. The manual wiring section in `_bricks.py` (search/zoekt observer callbacks, task dispatch pipe consumer, wallet/manifest-resolver/tool-namespace conditional blocks, governance services, ReBAC circuit breaker, snapshot/delegation/IPC/version/auth bricks) stays in place; it is runtime composition, not metadata. Promotion of `produces`/`consumes` declarations into automatic wiring is a follow-up.

**`PluginRegistry`** (`nexus/plugins/registry.py`):

`PluginInfo` becomes a view over `PluginManifest` plus `loaded` runtime state. `discover()` reads from store. `_load_plugin()` calls `store.resolve_factory()`. `PluginMetadata` wraps `PluginManifest`. `version`/`author` continue to pull from `importlib.metadata`.

**Deprecation policy:**

| Surface | Status | Removal |
|---|---|---|
| `@register_connector(...)` | DeprecationWarning | N+2 minor |
| `CONNECTION_ARGS` class attr | Read as fallback if no manifest | N+2 minor |
| `ConnectorManifestEntry` | Wrapper alias around `ConnectorManifest` | N+2 minor |
| Module-level brick constants | Read as fallback | N+2 minor |
| `PluginMetadata` | Wrapper around `PluginManifest` | N+2 minor |

`N` = release this lands in. Each warning includes a link to the manifest migration doc.

### Layer 5 — Introspection API + CLI + HTTP

**Programmatic API** (`nexus.extensions.introspect`):

```python
def list_extensions(*, kind: Kind | None = None,
                    profile: ProfileSet | None = None,
                    available_only: bool = False) -> list[AnyManifest]: ...
def get_extension(name: str, kind: Kind) -> AnyManifest: ...
def check_extension(name: str, kind: Kind) -> CheckReport: ...
def list_kinds() -> list[Kind]: ...
```

**CLI** (`nexus extensions ...`):

```
nexus extensions list                            # all kinds: name + kind + status
  --kind connector|brick|plugin                  # filter
  --available-only                               # hide entries with missing deps
  --profile <name>                               # filter by profile gate
  --format table|json|yaml                       # default table

nexus extensions info <name> [--kind <k>]        # full manifest dump
nexus extensions check <name> [--kind <k>]       # CheckReport
nexus extensions kinds                           # list registered kinds
```

`--kind` optional unless name collides across kinds. Existing `nexus plugins ...` and `nexus connectors ...` keep their surfaces; internals delegate to the introspection API. No new brick CLI (bricks remain internal infra).

Sample `nexus extensions list`:

```
KIND       NAME            STATUS         PROFILE    DEPS
connector  s3              available      —          boto3
connector  gdrive          missing-deps   —          google-api-python-client
brick      search          available      search     —
brick      task-manager    available      —          —
plugin     koi             loaded         —          —
```

**HTTP API:** new `/api/extensions` endpoint mirroring CLI verbs. JSON shape matches `AnyManifest` Pydantic serialization. `GET /api/extensions`, `GET /api/extensions?kind=connector`, `GET /api/extensions/{kind}/{name}`. Existing `/api/connectors` endpoint stays unchanged.

**Performance target:** `list` on cold store with JSON index = 1 file read + 1 Pydantic parse, <50ms for ~50 extensions.

---

## Error Handling

**Manifest validation** (parse-time, before registration):

| Scenario | Behavior |
|---|---|
| Invalid Pydantic shape | `ManifestValidationError` with file + field path; build/CI fails |
| Duplicate `(kind, name)` from same source | `DuplicateManifestError` listing both source paths |
| Reserved name (`*`, leading `_`, `nexus`, `nexus-*`, empty string) | `ReservedNameError` at parse time |
| Unknown `kind` discriminator | Pydantic union validation error |
| Empty `module` / `factory` | Pydantic field validator |

**Discovery** (loading the store):

| Scenario | Behavior |
|---|---|
| `extensions.json` missing | Fall back to entry-points + fs scan; INFO logged once |
| Schema version mismatch | Warn + ignore index, fall back to live discovery |
| Corrupt JSON | `IndexCorruptError`; fall back; ERROR logged |
| Entry-point `_manifest` raises ImportError | Per-extension isolation: WARN + skip, others continue |
| Filesystem `_manifest.py` syntax error | Same isolation pattern |
| Two connector manifests share `service_name` (connector-only field) | `ConnectorRegistry` validation kicks in at adapter layer |

**Runtime** (`resolve_factory`, instantiation):

| Scenario | Behavior |
|---|---|
| Impl module missing optional dep | `MissingDependencyError` with `runtime_deps` install hints (preserved) |
| Factory callable not in module | `FactoryResolutionError` with manifest path |
| Profile gate disables extension | DEBUG log; brick set to `None` (preserved) |
| `import_probes` fail but `runtime_deps` empty | WARN — manifest mis-declared |

**Failure isolation invariant:** one broken extension never blocks discovery of others. Tested explicitly.

**Source precedence on duplicate `(kind, name)` across sources:** JSON index > entry-points > filesystem scan. Reasoning: index is curated/built; entry-points are installed-package-declared; fs scan is dev-only.

**Logging contract:**

- Manifest validation failure → ERROR (build fails)
- Per-extension import failure during discovery → WARN once per extension
- Index miss / fallback → INFO once per process
- Profile gate disabled → DEBUG (matches brick behavior today)

---

## Testing Strategy

Each test maps to an acceptance criterion. No extras.

**Unit — `tests/extensions/test_manifest.py`:**

1. Discriminated union: each kind parses; wrong `kind` rejected; missing required field rejected.
2. `RuntimeDep` discriminated union for python/binary/service.
3. JSON round-trip: `model_dump_json()` → parse → equal.
4. Reserved-name validator rejects each banned pattern.
5. `produces`/`consumes` round-trips on `BrickManifest`.

**Unit — `tests/extensions/test_store.py`:**

6. **Duplicate names**: same `(kind, name)` from different sources → resolution-order winner; same source → `DuplicateManifestError`.
7. **Reserved names**: store rejects manifest with reserved name at registration.
8. **Missing deps**: `check()` reports missing python/binary/service deps without importing impl; `import_probes` failure surfaces.
9. **Import failure isolation**: broken `_manifest.py` does not block siblings; WARN logged.
10. **Lazy loading**: `list()`/`get()` do not import impl (`sys.modules` snapshot); `resolve_factory()` does.
11. **Source precedence**: index > entry-points > fs scan via mocks.
12. **Profile filter**: `list(profile=...)` correct; `None` gate always returned.

**Index — `tests/extensions/test_index.py`:**

13. Generator output deterministic (sorted, stable formatting).
14. CI drift check: regenerate, diff against committed file.
15. Schema version embedded; mismatch triggers index-ignore.

**Adapter parity — `tests/extensions/test_adapter_parity.py`:**

16. `ConnectorRegistry.list()` identical for new `_manifest.py` vs legacy `@register_connector`.
17. Brick discovery identical for new manifests vs legacy module constants.
18. `PluginRegistry.discover()` identical for both styles.
19. `DeprecationWarning` emitted by `@register_connector`; not by manifest path.

**Introspection / CLI — `tests/extensions/test_introspect.py`, `tests/cli/test_extensions_cli.py`:**

20. `list_extensions`, `get_extension`, `check_extension` cover the API.
21. `nexus extensions list/info/check/kinds` smoke against seeded store.
22. `--available-only` / `--profile` filters.
23. `--format json|yaml` matches Pydantic serialization.

**HTTP — `tests/api/test_extensions_endpoint.py`:**

24. `GET /api/extensions` returns full list; `?kind=` filters; `GET /api/extensions/{kind}/{name}` returns single; 404 on unknown.

**Migration smoke (one per kind, end-to-end):**

25. **Connector**: pick one in-tree (e.g. `s3`), migrate to `_manifest.py`, run existing connector test suite — all pass; remove old decorator, suite still passes.
26. **Brick**: pick one in-tree, same flow.
27. **Plugin**: pick one in-tree, same flow.

**Test fixtures:** synthetic manifest set in `tests/extensions/fixtures/` covering all kinds + intentionally broken cases (missing field, reserved name, ImportError on load).

**Out of scope (follow-up issues):**

- Wiring of `produces`/`consumes` (declared, not consumed yet).
- HTTP API auth/rate-limiting beyond endpoint shape.
- CLI `--format` cosmetic regressions.
- Migration of remaining in-tree extensions.

---

## Acceptance Criteria Mapping

| Criterion (issue #3962) | Where addressed |
|---|---|
| Define a shared extension manifest contract | Layer 1, `nexus/extensions/manifest.py` |
| Migrate connector manifest placeholders onto the shared model | Layer 4 connector adapter; one connector migrated |
| Migrate brick factory descriptors onto the shared model | Layer 4 brick adapter; one brick migrated |
| Keep entry-point plugin discovery working through the same metadata path | Layer 4 plugin adapter; one plugin migrated |
| Add an introspection API/CLI path that lists extensions, deps, profile gates, config fields without importing optional deps | Layer 5; lazy invariants enforced in store |
| Add tests for duplicate names, reserved names, missing deps, import failure isolation, lazy loading | Tests 6–10 |

---

## Migration Plan (PR sequence)

**This issue lands in 1–3 PRs.** Suggested split:

1. **PR 1** — Layers 1–3: manifest contract, store, index generator, JSON index format, CI drift check. No adapter changes yet. Pure new code + tests 1–15.
2. **PR 2** — Layer 4: connector adapter + one connector migrated; brick adapter + one brick migrated; plugin adapter + one plugin migrated. DeprecationWarnings hooked. Tests 16–19, 25–27.
3. **PR 3** — Layer 5: introspection API, `nexus extensions` CLI, `/api/extensions` endpoint. Tests 20–24.

Splitting may compress to 2 PRs if PR 2 stays reviewable.

**Follow-up issues** (out of scope for this design):

- Migrate remaining in-tree connectors/bricks/plugins to `_manifest.py`.
- Consume `produces`/`consumes` to auto-wire dependent bricks (replace manual section in `_bricks.py`).
- Removal of legacy fallbacks at N+2.
