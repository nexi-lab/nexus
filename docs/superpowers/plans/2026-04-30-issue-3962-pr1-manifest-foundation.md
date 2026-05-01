# Issue #3962 — PR 1: Manifest Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land Layers 1–3 of the unified extension manifest design — Pydantic discriminated-union manifest contract, lazy `ManifestStore`, and the `extensions.json` index generator with CI drift check. No adapter changes yet; existing `ConnectorRegistry` / brick factory / `PluginRegistry` keep working unchanged.

**Architecture:** Five-layer design from spec [`2026-04-30-issue-3962-unified-extension-manifest-design.md`](../specs/2026-04-30-issue-3962-unified-extension-manifest-design.md). This plan covers Layers 1–3: a new package `nexus.extensions` containing `types.py` (shared pure data types extracted from `backends/base/registry.py`), `manifest.py` (Pydantic discriminated union), `store.py` (lazy lookup with three population sources), and `index.py` (build-time JSON generator). All new code; old code untouched.

**Tech Stack:** Python 3.12+, Pydantic v2 (already in repo), `importlib.metadata`, pytest.

**Spec coverage in this PR:** acceptance-criterion items 1 (manifest contract), 5 (introspection plumbing — store + index foundation, CLI/HTTP comes in PR 3), 6 (tests for duplicate names, reserved names, missing deps, import failure isolation, lazy loading).

---

## File Structure

**New files (all under `src/nexus/extensions/`):**

| Path | Responsibility |
|---|---|
| `src/nexus/extensions/__init__.py` | Package init; re-export `ExtensionManifest`, `AnyManifest`, store singleton accessor |
| `src/nexus/extensions/types.py` | Pure data types: `ArgType` enum, `ConnectionArg` dataclass (moved from `backends/base/registry.py`); `Kind` literal alias |
| `src/nexus/extensions/manifest.py` | Pydantic models: `RuntimeDep`, `ExtensionManifest`, `ConnectorManifest`, `BrickManifest`, `PluginManifest`, `AnyManifest` |
| `src/nexus/extensions/errors.py` | Error classes: `ManifestValidationError`, `DuplicateManifestError`, `ReservedNameError`, `IndexCorruptError`, `FactoryResolutionError` |
| `src/nexus/extensions/store.py` | `ManifestStore` class + `CheckReport` dataclass + module-level singleton |
| `src/nexus/extensions/index.py` | Index generator (`build` / `verify` subcommands) |
| `src/nexus/extensions/_index/__init__.py` | Marker package for the shipped JSON |
| `src/nexus/extensions/_index/extensions.json` | Generated artifact (committed; CI verifies) |

**Modified files:**

| Path | Reason |
|---|---|
| `src/nexus/backends/base/registry.py` | `ArgType` and `ConnectionArg` re-export from `nexus.extensions.types` to preserve public API; no behavior change |
| `pyproject.toml` | Register `nexus-extensions-index` console script for the generator; add to dev/test extras if needed |
| `.pre-commit-config.yaml` | Add hook to verify `extensions.json` is up to date |

**New test files:**

| Path | Coverage |
|---|---|
| `tests/extensions/__init__.py` | Empty package init |
| `tests/extensions/test_types.py` | `ArgType` / `ConnectionArg` round-trip + import path stability |
| `tests/extensions/test_manifest.py` | Discriminated union, validators, JSON round-trip |
| `tests/extensions/test_store.py` | Store API, source precedence, isolation, lazy invariants |
| `tests/extensions/test_index.py` | Generator determinism, schema version, drift |
| `tests/extensions/fixtures/__init__.py` | Empty |
| `tests/extensions/fixtures/conftest.py` | Synthetic manifest fixtures |
| `tests/extensions/fixtures/broken_manifest.py` | Intentionally raises ImportError on import (isolation test) |

---

## Pre-flight check (one-time)

- [ ] **Step P1: Confirm Pydantic v2 in repo**

```bash
python -c "import pydantic; print(pydantic.VERSION)"
```

Expected: a version starting with `2.`. If 1.x, stop and escalate — design assumes v2 discriminated unions.

- [ ] **Step P2: Confirm worktree is clean**

```bash
git status --short
```

Expected: empty output.

---

## Task 1: Create `nexus.extensions` package skeleton

**Files:**
- Create: `src/nexus/extensions/__init__.py`
- Create: `tests/extensions/__init__.py`
- Test: `tests/extensions/test_package.py`

- [ ] **Step 1.1: Write the failing import test**

Create `tests/extensions/test_package.py`:

```python
"""Smoke test: nexus.extensions package importable with zero side effects."""
import sys


def test_package_imports_without_backend_deps():
    """Importing nexus.extensions must NOT pull in backend/brick/plugin runtime."""
    # Snapshot loaded modules, then import.
    before = set(sys.modules)
    import nexus.extensions  # noqa: F401

    new_modules = set(sys.modules) - before
    forbidden_prefixes = (
        "nexus.backends.connectors.",
        "nexus.bricks.",
        "nexus.plugins.base",
    )
    leaked = [m for m in new_modules if m.startswith(forbidden_prefixes)]
    assert not leaked, f"Importing nexus.extensions leaked: {leaked}"
```

- [ ] **Step 1.2: Run test to verify it fails**

Run: `pytest tests/extensions/test_package.py -v`
Expected: `ModuleNotFoundError: No module named 'nexus.extensions'`

- [ ] **Step 1.3: Create the package**

Create `src/nexus/extensions/__init__.py`:

```python
"""Nexus unified extension metadata layer.

This package owns the manifest contract and discovery store shared by
plugins, connectors, and bricks. It MUST NOT import any extension impl
module — keeping that boundary lets introspection enumerate extensions
without triggering optional-dependency imports.
"""

__all__: list[str] = []
```

Create `tests/extensions/__init__.py` (empty file).

- [ ] **Step 1.4: Run test to verify it passes**

Run: `pytest tests/extensions/test_package.py -v`
Expected: PASS.

- [ ] **Step 1.5: Commit**

```bash
git add src/nexus/extensions/__init__.py tests/extensions/__init__.py tests/extensions/test_package.py
git commit -m "feat(#3962): scaffold nexus.extensions package"
```

---

## Task 2: Extract `ArgType` and `ConnectionArg` to `nexus.extensions.types`

**Why:** `ConnectorManifest` (Task 5) needs `ConnectionArg` as a field. `manifest.py` cannot import from `backends/base/registry.py` without violating the boundary rule. Moving these pure data types to `nexus.extensions.types` lets manifest.py and connector code both depend on it.

**Files:**
- Create: `src/nexus/extensions/types.py`
- Modify: `src/nexus/backends/base/registry.py` (re-export, preserve public API)
- Test: `tests/extensions/test_types.py`

- [ ] **Step 2.1: Write the failing tests**

Create `tests/extensions/test_types.py`:

```python
"""Pure data types — must be importable with zero backend deps."""
import sys

from nexus.extensions.types import ArgType, ConnectionArg


def test_arg_type_values():
    assert ArgType.STRING.value == "string"
    assert ArgType.SECRET.value == "secret"
    assert ArgType.PASSWORD.value == "password"
    assert ArgType.INTEGER.value == "integer"
    assert ArgType.BOOLEAN.value == "boolean"
    assert ArgType.PATH.value == "path"
    assert ArgType.OAUTH.value == "oauth"


def test_connection_arg_round_trip():
    arg = ConnectionArg(type=ArgType.STRING, description="bucket name", required=True)
    d = arg.to_dict()
    assert d == {
        "type": "string",
        "description": "bucket name",
        "required": True,
        "default": None,
        "secret": False,
        "env_var": None,
    }


def test_connection_arg_with_config_key():
    arg = ConnectionArg(
        type=ArgType.STRING, description="x", required=True, config_key="bucket"
    )
    d = arg.to_dict()
    assert d["config_key"] == "bucket"


def test_types_module_has_no_backend_imports():
    """Importing nexus.extensions.types must not load any nexus.backends module."""
    # Force a clean reload by checking what's in sys.modules after import.
    import nexus.extensions.types  # noqa: F401

    forbidden = [m for m in sys.modules if m.startswith("nexus.backends.")]
    # types.py itself doesn't import nexus.backends — verify by looking at what
    # imports the types module pulled in.
    types_mod = sys.modules["nexus.extensions.types"]
    # Walk the module's __dict__ looking for backend imports — there should be
    # zero references to nexus.backends in the resolved module globals.
    for name, val in vars(types_mod).items():
        if hasattr(val, "__module__") and val.__module__:
            assert not val.__module__.startswith("nexus.backends."), (
                f"{name} resolves to {val.__module__}"
            )


def test_backwards_compat_reexport():
    """ArgType and ConnectionArg must remain importable from old location."""
    from nexus.backends.base.registry import ArgType as A2, ConnectionArg as C2

    assert A2 is ArgType
    assert C2 is ConnectionArg
```

- [ ] **Step 2.2: Run tests to verify they fail**

Run: `pytest tests/extensions/test_types.py -v`
Expected: `ModuleNotFoundError: No module named 'nexus.extensions.types'`.

- [ ] **Step 2.3: Create `nexus/extensions/types.py`**

Create `src/nexus/extensions/types.py`:

```python
"""Pure data types shared by the manifest contract and connector code.

This module is foundational: nexus.extensions.manifest, nexus.extensions.store,
and nexus.backends.* all depend on it. It depends on nothing in the nexus
package — keep it that way.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal

Kind = Literal["connector", "brick", "plugin"]


class ArgType(Enum):
    """Types for connection arguments.

    Used to indicate how arguments should be handled in UI/CLI and validation.
    """

    STRING = "string"
    SECRET = "secret"
    PASSWORD = "password"
    INTEGER = "integer"
    BOOLEAN = "boolean"
    PATH = "path"
    OAUTH = "oauth"


@dataclass
class ConnectionArg:
    """Definition of a connection argument for a connector."""

    type: ArgType
    description: str
    required: bool = True
    default: Any = None
    secret: bool = False
    env_var: str | None = None
    config_key: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "type": self.type.value,
            "description": self.description,
            "required": self.required,
            "default": self.default,
            "secret": self.secret,
            "env_var": self.env_var,
        }
        if self.config_key is not None:
            result["config_key"] = self.config_key
        return result
```

- [ ] **Step 2.4: Modify `backends/base/registry.py` to re-export**

Read the existing definitions first to confirm exact location:

```bash
grep -n "^class ArgType\|^class ConnectionArg\|^@dataclass$" src/nexus/backends/base/registry.py | head -10
```

Then edit `src/nexus/backends/base/registry.py`:

Replace the body of `class ArgType(Enum): ...` (currently around line 114, the entire class block including its docstring and members) and the `@dataclass` block defining `class ConnectionArg:` (currently around line 142–200) with a single re-export. Find the lines containing the original `class ArgType(Enum):` declaration through the end of `ConnectionArg.to_dict()` (locate end with `grep -n "def to_dict" src/nexus/backends/base/registry.py`).

Add this import near the top of the file (after the existing imports, before the first class definition):

```python
from nexus.extensions.types import ArgType, ConnectionArg

__all__ = [..., "ArgType", "ConnectionArg"]  # if __all__ exists, append; otherwise skip
```

Delete the original `class ArgType(Enum):` and `class ConnectionArg:` definitions. Leave a comment at the deletion site:

```python
# ArgType and ConnectionArg are now defined in nexus.extensions.types
# and re-exported above for backwards compatibility.
```

- [ ] **Step 2.5: Run all tests to verify**

Run: `pytest tests/extensions/test_types.py -v`
Expected: all 5 tests PASS.

Run: `pytest tests/backends/ -x -q 2>&1 | tail -20`
Expected: no failures from the move (all existing connector tests still pass — they import `ArgType` / `ConnectionArg` from the same place via re-export).

- [ ] **Step 2.6: Commit**

```bash
git add src/nexus/extensions/types.py src/nexus/backends/base/registry.py tests/extensions/test_types.py
git commit -m "refactor(#3962): extract ArgType and ConnectionArg to nexus.extensions.types"
```

---

## Task 3: Error classes for manifest layer

**Files:**
- Create: `src/nexus/extensions/errors.py`
- Test: `tests/extensions/test_errors.py`

- [ ] **Step 3.1: Write the failing test**

Create `tests/extensions/test_errors.py`:

```python
from nexus.extensions.errors import (
    DuplicateManifestError,
    FactoryResolutionError,
    IndexCorruptError,
    ManifestValidationError,
    ReservedNameError,
)


def test_all_inherit_from_extension_error():
    from nexus.extensions.errors import ExtensionError

    for cls in (
        ManifestValidationError,
        DuplicateManifestError,
        ReservedNameError,
        IndexCorruptError,
        FactoryResolutionError,
    ):
        assert issubclass(cls, ExtensionError)


def test_duplicate_manifest_error_carries_sources():
    err = DuplicateManifestError(
        kind="connector", name="s3", sources=("entry_point", "fs_scan")
    )
    msg = str(err)
    assert "connector" in msg
    assert "s3" in msg
    assert "entry_point" in msg
    assert "fs_scan" in msg


def test_reserved_name_error_carries_name_and_pattern():
    err = ReservedNameError(name="_foo", pattern="leading underscore")
    msg = str(err)
    assert "_foo" in msg
    assert "leading underscore" in msg
```

- [ ] **Step 3.2: Run test to verify it fails**

Run: `pytest tests/extensions/test_errors.py -v`
Expected: `ModuleNotFoundError: No module named 'nexus.extensions.errors'`.

- [ ] **Step 3.3: Create `errors.py`**

Create `src/nexus/extensions/errors.py`:

```python
"""Exception types for the extension metadata layer."""

from __future__ import annotations


class ExtensionError(Exception):
    """Base class for all extension-layer errors."""


class ManifestValidationError(ExtensionError):
    """Raised when a manifest fails Pydantic validation.

    Carries the source path so the user can locate the bad declaration.
    """

    def __init__(self, source: str, detail: str) -> None:
        super().__init__(f"Invalid manifest at {source}: {detail}")
        self.source = source
        self.detail = detail


class DuplicateManifestError(ExtensionError):
    """Raised when the same (kind, name) pair is declared twice from one source."""

    def __init__(self, kind: str, name: str, sources: tuple[str, ...]) -> None:
        super().__init__(
            f"Duplicate manifest for {kind}/{name} declared by: {', '.join(sources)}"
        )
        self.kind = kind
        self.name = name
        self.sources = sources


class ReservedNameError(ExtensionError):
    """Raised when a manifest declares a reserved name."""

    def __init__(self, name: str, pattern: str) -> None:
        super().__init__(
            f"Manifest name '{name}' matches reserved pattern: {pattern}"
        )
        self.name = name
        self.pattern = pattern


class IndexCorruptError(ExtensionError):
    """Raised when extensions.json is unreadable or malformed."""


class FactoryResolutionError(ExtensionError):
    """Raised when the factory callable named in a manifest can't be resolved."""

    def __init__(self, manifest_name: str, module: str, factory: str, detail: str) -> None:
        super().__init__(
            f"Cannot resolve factory '{factory}' for manifest '{manifest_name}' "
            f"in module '{module}': {detail}"
        )
        self.manifest_name = manifest_name
        self.module = module
        self.factory = factory
        self.detail = detail
```

- [ ] **Step 3.4: Run test to verify it passes**

Run: `pytest tests/extensions/test_errors.py -v`
Expected: 3 tests PASS.

- [ ] **Step 3.5: Commit**

```bash
git add src/nexus/extensions/errors.py tests/extensions/test_errors.py
git commit -m "feat(#3962): error classes for extension manifest layer"
```

---

## Task 4: `RuntimeDep` model

**Files:**
- Create: `src/nexus/extensions/manifest.py` (initial — only `RuntimeDep` for now)
- Test: `tests/extensions/test_manifest.py`

- [ ] **Step 4.1: Write the failing test**

Create `tests/extensions/test_manifest.py`:

```python
"""Pydantic discriminated-union manifest contract tests."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from nexus.extensions.manifest import RuntimeDep


class TestRuntimeDep:
    def test_python_dep(self):
        dep = RuntimeDep(kind="python", name="boto3")
        assert dep.kind == "python"
        assert dep.name == "boto3"
        assert dep.extras == ()
        assert dep.install_hint is None

    def test_binary_dep_with_hint(self):
        dep = RuntimeDep(
            kind="binary", name="git", install_hint="apt install git"
        )
        assert dep.install_hint == "apt install git"

    def test_service_dep(self):
        dep = RuntimeDep(kind="service", name="postgres")
        assert dep.kind == "service"

    def test_invalid_kind_rejected(self):
        with pytest.raises(ValidationError):
            RuntimeDep(kind="cosmic", name="x")

    def test_extras_immutable(self):
        dep = RuntimeDep(kind="python", name="a", extras=("b", "c"))
        assert dep.extras == ("b", "c")

    def test_json_round_trip(self):
        dep = RuntimeDep(kind="python", name="boto3", extras=("s3",))
        data = dep.model_dump()
        again = RuntimeDep.model_validate(data)
        assert again == dep
```

- [ ] **Step 4.2: Run test to verify it fails**

Run: `pytest tests/extensions/test_manifest.py -v`
Expected: `ModuleNotFoundError: No module named 'nexus.extensions.manifest'`.

- [ ] **Step 4.3: Implement `RuntimeDep`**

Create `src/nexus/extensions/manifest.py`:

```python
"""Pydantic discriminated-union manifest contract.

This module defines the data shape every extension (plugin, connector, brick)
declares in its sibling _manifest.py file. It must NOT import any
extension impl module — that boundary keeps introspection lazy.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class RuntimeDep(BaseModel):
    """A dependency required to actually run an extension.

    Distinct from ``import_probes`` — runtime_deps are declarative and used to
    generate human-readable install hints; probes are best-effort module
    presence checks for ``nexus extensions check``.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["python", "binary", "service"]
    name: str
    extras: tuple[str, ...] = ()
    install_hint: str | None = None
```

- [ ] **Step 4.4: Run test to verify it passes**

Run: `pytest tests/extensions/test_manifest.py -v -k RuntimeDep`
Expected: 6 tests PASS.

- [ ] **Step 4.5: Commit**

```bash
git add src/nexus/extensions/manifest.py tests/extensions/test_manifest.py
git commit -m "feat(#3962): RuntimeDep Pydantic model"
```

---

## Task 5: `ExtensionManifest` base + reserved-name validator

**Files:**
- Modify: `src/nexus/extensions/manifest.py` (append `ExtensionManifest`)
- Modify: `tests/extensions/test_manifest.py` (append tests)

- [ ] **Step 5.1: Write the failing tests**

Append to `tests/extensions/test_manifest.py`:

```python
from nexus.extensions.manifest import ExtensionManifest
from nexus.extensions.errors import ReservedNameError


class TestExtensionManifestBase:
    """The base class is abstract — exercised through the kind-specific subclasses
    in later tests, but reserved-name validation lives here and applies to every
    subclass."""

    def _make(self, name: str) -> ExtensionManifest:
        # Use plugin kind for these tests since it has the fewest required fields.
        from nexus.extensions.manifest import PluginManifest  # imported lazily

        return PluginManifest(
            name=name, module="x.y", factory="Z", entry_point_group="nexus.plugins"
        )

    @pytest.mark.parametrize(
        "bad_name,reason",
        [
            ("", "empty"),
            ("_leading", "leading underscore"),
            ("nexus", "reserved nexus name"),
            ("nexus-internal", "reserved nexus prefix"),
            ("*", "glob"),
        ],
    )
    def test_reserved_name_rejected(self, bad_name: str, reason: str):
        with pytest.raises(ReservedNameError):
            self._make(bad_name)

    def test_normal_name_accepted(self):
        m = self._make("my-extension")
        assert m.name == "my-extension"

    def test_module_required(self):
        from nexus.extensions.manifest import PluginManifest

        with pytest.raises(ValidationError):
            PluginManifest(
                name="ok", module="", factory="Z", entry_point_group="nexus.plugins"
            )

    def test_factory_required(self):
        from nexus.extensions.manifest import PluginManifest

        with pytest.raises(ValidationError):
            PluginManifest(
                name="ok", module="x.y", factory="", entry_point_group="nexus.plugins"
            )
```

- [ ] **Step 5.2: Run test to verify it fails**

Run: `pytest tests/extensions/test_manifest.py::TestExtensionManifestBase -v`
Expected: `ImportError: cannot import name 'ExtensionManifest'` (or `PluginManifest`).

- [ ] **Step 5.3: Implement `ExtensionManifest` + validator**

Append to `src/nexus/extensions/manifest.py`:

```python
import re

from pydantic import field_validator

from nexus.extensions.errors import ReservedNameError
from nexus.extensions.types import Kind

# Reserved name patterns. Matched in order; first hit wins for the error message.
_RESERVED_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("empty", re.compile(r"^$")),
    ("leading underscore", re.compile(r"^_")),
    ("glob", re.compile(r"^\*$")),
    ("reserved nexus name", re.compile(r"^nexus$")),
    ("reserved nexus prefix", re.compile(r"^nexus-")),
)


def _validate_name(name: str) -> str:
    for label, pattern in _RESERVED_PATTERNS:
        if pattern.match(name):
            raise ReservedNameError(name=name, pattern=label)
    return name


class ExtensionManifest(BaseModel):
    """Base contract for all extension manifests.

    Subclassed by ConnectorManifest, BrickManifest, PluginManifest. The
    discriminator is the ``kind`` field — Pydantic v2 picks the subclass based
    on its value when parsing AnyManifest.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    kind: Kind
    module: str  # dotted path; NOT imported by manifest layer
    factory: str  # callable/class name in module
    description: str = ""
    runtime_deps: tuple[RuntimeDep, ...] = ()
    config_schema: str | None = None
    profile_gate: str | None = None
    import_probes: tuple[str, ...] = ()

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        return _validate_name(v)

    @field_validator("module", "factory")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v:
            raise ValueError("must be non-empty")
        return v
```

- [ ] **Step 5.4: Run tests (will still partially fail until Task 6 adds PluginManifest)**

Run: `pytest tests/extensions/test_manifest.py::TestExtensionManifestBase -v`
Expected: tests fail with `cannot import name 'PluginManifest'`. Proceed to Task 6 — these tests will pass once `PluginManifest` exists.

- [ ] **Step 5.5: Commit**

```bash
git add src/nexus/extensions/manifest.py tests/extensions/test_manifest.py
git commit -m "feat(#3962): ExtensionManifest base + reserved-name validator"
```

---

## Task 6: `ConnectorManifest`, `BrickManifest`, `PluginManifest` subclasses

**Files:**
- Modify: `src/nexus/extensions/manifest.py`
- Modify: `tests/extensions/test_manifest.py`

- [ ] **Step 6.1: Write the failing tests**

Append to `tests/extensions/test_manifest.py`:

```python
from nexus.extensions.manifest import (
    AnyManifest,
    BrickManifest,
    ConnectorManifest,
    PluginManifest,
    parse_manifest,
)
from nexus.extensions.types import ArgType, ConnectionArg


class TestConnectorManifest:
    def test_minimal(self):
        m = ConnectorManifest(
            name="hn", module="nexus.backends.connectors.hn.connector",
            factory="HNConnector", service_name="hn",
        )
        assert m.kind == "connector"
        assert m.service_name == "hn"
        assert m.capabilities == frozenset()
        assert m.user_scoped is False

    def test_with_capabilities_and_args(self):
        m = ConnectorManifest(
            name="x", module="m", factory="F", service_name="x",
            capabilities=frozenset({"streaming"}),
            connection_args={
                "url": ConnectionArg(type=ArgType.STRING, description="endpoint")
            },
            user_scoped=True,
        )
        assert "streaming" in m.capabilities
        assert "url" in m.connection_args


class TestBrickManifest:
    def test_independent_tier(self):
        m = BrickManifest(
            name="search", module="nexus.bricks.search.brick_factory",
            factory="create", tier="independent", result_key="search_service",
            profile_gate="search",
        )
        assert m.kind == "brick"
        assert m.tier == "independent"
        assert m.profile_gate == "search"

    def test_dependent_tier_with_artifacts(self):
        m = BrickManifest(
            name="upload", module="m", factory="create",
            tier="dependent", result_key="upload_service",
            produces=("upload_observer",), consumes=("artifact_bus",),
        )
        assert m.produces == ("upload_observer",)
        assert m.consumes == ("artifact_bus",)

    def test_invalid_tier_rejected(self):
        with pytest.raises(ValidationError):
            BrickManifest(
                name="x", module="m", factory="F",
                tier="weird", result_key="r",
            )


class TestPluginManifest:
    def test_minimal(self):
        m = PluginManifest(
            name="koi", module="koi.plugin", factory="KoiPlugin",
        )
        assert m.kind == "plugin"
        assert m.entry_point_group == "nexus.plugins"
        assert m.hooks == {}
        assert m.commands == {}

    def test_hooks_and_commands(self):
        m = PluginManifest(
            name="x", module="m", factory="F",
            hooks={"on_boot": "m.on_boot"},
            commands={"do": "m.do_command"},
        )
        assert m.hooks["on_boot"] == "m.on_boot"


class TestDiscriminatedUnion:
    def test_parse_connector(self):
        data = {
            "kind": "connector", "name": "hn", "module": "m", "factory": "F",
            "service_name": "hn",
        }
        m = parse_manifest(data)
        assert isinstance(m, ConnectorManifest)

    def test_parse_brick(self):
        data = {
            "kind": "brick", "name": "search", "module": "m", "factory": "F",
            "tier": "independent", "result_key": "r",
        }
        m = parse_manifest(data)
        assert isinstance(m, BrickManifest)

    def test_parse_plugin(self):
        data = {
            "kind": "plugin", "name": "koi", "module": "m", "factory": "F",
        }
        m = parse_manifest(data)
        assert isinstance(m, PluginManifest)

    def test_unknown_kind_rejected(self):
        data = {"kind": "robot", "name": "x", "module": "m", "factory": "F"}
        with pytest.raises(ValidationError):
            parse_manifest(data)

    def test_json_round_trip_each_kind(self):
        manifests: list[AnyManifest] = [
            ConnectorManifest(
                name="hn", module="m", factory="F", service_name="hn"
            ),
            BrickManifest(
                name="search", module="m", factory="F",
                tier="independent", result_key="r",
            ),
            PluginManifest(name="koi", module="m", factory="F"),
        ]
        for original in manifests:
            data = original.model_dump()
            parsed = parse_manifest(data)
            assert parsed == original
```

- [ ] **Step 6.2: Run test to verify it fails**

Run: `pytest tests/extensions/test_manifest.py -v`
Expected: import errors for `ConnectorManifest`, `BrickManifest`, `PluginManifest`, `AnyManifest`, `parse_manifest`.

- [ ] **Step 6.3: Implement subclasses + discriminated union**

Append to `src/nexus/extensions/manifest.py`:

```python
from typing import Annotated, Any, Union

from pydantic import Field, TypeAdapter

from nexus.extensions.types import ConnectionArg


class ConnectorManifest(ExtensionManifest):
    kind: Literal["connector"] = "connector"  # type: ignore[assignment]
    service_name: str
    capabilities: frozenset[str] = frozenset()
    connection_args: dict[str, ConnectionArg] = Field(default_factory=dict)
    user_scoped: bool = False
    config_mapping: dict[str, str] = Field(default_factory=dict)

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)


class BrickManifest(ExtensionManifest):
    kind: Literal["brick"] = "brick"  # type: ignore[assignment]
    tier: Literal["independent", "dependent"]
    result_key: str
    produces: tuple[str, ...] = ()
    consumes: tuple[str, ...] = ()


class PluginManifest(ExtensionManifest):
    kind: Literal["plugin"] = "plugin"  # type: ignore[assignment]
    entry_point_group: str = "nexus.plugins"
    hooks: dict[str, str] = Field(default_factory=dict)
    commands: dict[str, str] = Field(default_factory=dict)


AnyManifest = Annotated[
    Union[ConnectorManifest, BrickManifest, PluginManifest],
    Field(discriminator="kind"),
]


_ANY_ADAPTER: TypeAdapter[AnyManifest] = TypeAdapter(AnyManifest)


def parse_manifest(data: dict[str, Any]) -> AnyManifest:
    """Parse a raw dict into the correct manifest subclass via discriminator."""
    return _ANY_ADAPTER.validate_python(data)
```

**Note:** `frozenset[str]` for `capabilities` — strings, not enum. `BackendFeature` is a connector-runtime concept; the manifest stores capability names as opaque strings, and the `ConnectorRegistry` adapter (PR 2) maps strings back to the enum. This honors the boundary rule.

- [ ] **Step 6.4: Run tests to verify they pass**

Run: `pytest tests/extensions/test_manifest.py -v`
Expected: all tests PASS (including the `TestExtensionManifestBase` tests from Task 5).

- [ ] **Step 6.5: Update `nexus/extensions/__init__.py` to re-export**

Replace `src/nexus/extensions/__init__.py`:

```python
"""Nexus unified extension metadata layer."""

from nexus.extensions.errors import (
    DuplicateManifestError,
    ExtensionError,
    FactoryResolutionError,
    IndexCorruptError,
    ManifestValidationError,
    ReservedNameError,
)
from nexus.extensions.manifest import (
    AnyManifest,
    BrickManifest,
    ConnectorManifest,
    ExtensionManifest,
    PluginManifest,
    RuntimeDep,
    parse_manifest,
)
from nexus.extensions.types import ArgType, ConnectionArg, Kind

__all__ = [
    "AnyManifest",
    "ArgType",
    "BrickManifest",
    "ConnectionArg",
    "ConnectorManifest",
    "DuplicateManifestError",
    "ExtensionError",
    "ExtensionManifest",
    "FactoryResolutionError",
    "IndexCorruptError",
    "Kind",
    "ManifestValidationError",
    "PluginManifest",
    "ReservedNameError",
    "RuntimeDep",
    "parse_manifest",
]
```

- [ ] **Step 6.6: Re-run package import test**

Run: `pytest tests/extensions/test_package.py -v`
Expected: PASS — the boundary rule still holds (no backend imports leaked).

- [ ] **Step 6.7: Commit**

```bash
git add src/nexus/extensions/manifest.py src/nexus/extensions/__init__.py tests/extensions/test_manifest.py
git commit -m "feat(#3962): ConnectorManifest/BrickManifest/PluginManifest + discriminated union"
```

---

## Task 7: `CheckReport` dataclass + `ManifestStore` skeleton

**Files:**
- Create: `src/nexus/extensions/store.py`
- Test: `tests/extensions/test_store.py`
- Test fixtures: `tests/extensions/fixtures/__init__.py`, `tests/extensions/fixtures/conftest.py`

- [ ] **Step 7.1: Write the failing test**

Create `tests/extensions/fixtures/__init__.py` (empty file).

Create `tests/extensions/fixtures/conftest.py`:

```python
"""Fixtures for store tests — synthetic manifests covering all kinds."""
from __future__ import annotations

import pytest

from nexus.extensions.manifest import (
    AnyManifest,
    BrickManifest,
    ConnectorManifest,
    PluginManifest,
    RuntimeDep,
)


@pytest.fixture
def hn_manifest() -> ConnectorManifest:
    return ConnectorManifest(
        name="hn", module="nexus.backends.connectors.hn.connector",
        factory="HNConnector", service_name="hn",
        runtime_deps=(RuntimeDep(kind="python", name="httpx"),),
    )


@pytest.fixture
def search_manifest() -> BrickManifest:
    return BrickManifest(
        name="search", module="nexus.bricks.search.brick_factory",
        factory="create", tier="independent", result_key="search_service",
        profile_gate="search",
    )


@pytest.fixture
def koi_manifest() -> PluginManifest:
    return PluginManifest(
        name="koi", module="koi.plugin", factory="KoiPlugin",
    )


@pytest.fixture
def all_manifests(
    hn_manifest: ConnectorManifest,
    search_manifest: BrickManifest,
    koi_manifest: PluginManifest,
) -> list[AnyManifest]:
    return [hn_manifest, search_manifest, koi_manifest]
```

Create `tests/extensions/test_store.py`:

```python
"""ManifestStore tests — list/get/check/resolve_factory + lazy invariants."""
from __future__ import annotations

import pytest

from nexus.extensions.errors import DuplicateManifestError
from nexus.extensions.store import CheckReport, ManifestStore

pytest_plugins = ["tests.extensions.fixtures.conftest"]


class TestStoreBasics:
    def test_empty_store_lists_nothing(self):
        store = ManifestStore()
        assert store.list() == []

    def test_register_and_list(self, hn_manifest):
        store = ManifestStore()
        store._register(hn_manifest, source="test")
        assert store.list() == [hn_manifest]

    def test_register_multiple_kinds(self, all_manifests):
        store = ManifestStore()
        for m in all_manifests:
            store._register(m, source="test")
        assert len(store.list()) == 3
        assert len(store.list(kind="connector")) == 1
        assert len(store.list(kind="brick")) == 1
        assert len(store.list(kind="plugin")) == 1

    def test_get_by_name_and_kind(self, hn_manifest):
        store = ManifestStore()
        store._register(hn_manifest, source="test")
        assert store.get("hn", kind="connector") is hn_manifest

    def test_get_unknown_raises_keyerror(self):
        store = ManifestStore()
        with pytest.raises(KeyError):
            store.get("ghost", kind="connector")

    def test_duplicate_same_source_raises(self, hn_manifest):
        store = ManifestStore()
        store._register(hn_manifest, source="entry_point")
        with pytest.raises(DuplicateManifestError) as excinfo:
            store._register(hn_manifest, source="entry_point")
        assert excinfo.value.kind == "connector"
        assert excinfo.value.name == "hn"


class TestCheckReport:
    def test_check_report_shape(self):
        report = CheckReport(
            available=True,
            missing_python_deps=(),
            missing_binary_deps=(),
            missing_services=(),
            import_probe_failures=(),
            profile_gate_disabled=False,
        )
        assert report.available is True
```

- [ ] **Step 7.2: Run test to verify it fails**

Run: `pytest tests/extensions/test_store.py -v`
Expected: `ModuleNotFoundError: No module named 'nexus.extensions.store'`.

- [ ] **Step 7.3: Implement `store.py` skeleton**

Create `src/nexus/extensions/store.py`:

```python
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

from dataclasses import dataclass, field
from typing import Iterable

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
    the module-level singleton accessor `get_store()` (see Task 9+). Tests
    construct `ManifestStore()` directly and use `_register()` to seed.
    """

    def __init__(self) -> None:
        # Keyed by (kind, name) → (manifest, source_label).
        self._entries: dict[tuple[Kind, str], tuple[AnyManifest, str]] = {}
        # Per-source insertion: tracks which sources have already added the
        # given (kind, name); used to detect "same source declared twice".
        self._source_entries: dict[str, set[tuple[Kind, str]]] = {}

    # --- read API (lazy: never imports impl modules) ---

    def list(
        self,
        *,
        kind: Kind | None = None,
        profile: frozenset[str] | None = None,
        include_unavailable: bool = True,
    ) -> list[AnyManifest]:
        results: list[AnyManifest] = []
        for (k, _name), (m, _src) in sorted(self._entries.items()):
            if kind is not None and k != kind:
                continue
            if profile is not None and m.profile_gate not in (None, *profile):
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
                kind=manifest.kind, name=manifest.name,
                sources=(existing_source, source),
            )
        # Cross-source: respect precedence — first source wins.
        if key in self._entries:
            return
        self._entries[key] = (manifest, source)
        seen_in_source.add(key)
```

- [ ] **Step 7.4: Run tests to verify they pass**

Run: `pytest tests/extensions/test_store.py -v`
Expected: all 7 tests PASS.

- [ ] **Step 7.5: Commit**

```bash
git add src/nexus/extensions/store.py tests/extensions/test_store.py tests/extensions/fixtures/
git commit -m "feat(#3962): ManifestStore skeleton + CheckReport"
```

---

## Task 8: Source precedence + multi-source registration

**Files:**
- Modify: `src/nexus/extensions/store.py`
- Modify: `tests/extensions/test_store.py`

- [ ] **Step 8.1: Write the failing test**

Append to `tests/extensions/test_store.py`:

```python
class TestSourcePrecedence:
    def test_first_source_wins(self, hn_manifest):
        """When the same (kind, name) is registered from different sources,
        the first one wins — JSON index > entry-points > fs scan."""
        store = ManifestStore()
        store._register(hn_manifest, source="json_index")
        # Construct a different manifest with the same identity from a later source.
        from nexus.extensions.manifest import ConnectorManifest

        alt = ConnectorManifest(
            name="hn", module="some.other.module", factory="Other",
            service_name="hn",
        )
        store._register(alt, source="entry_point")  # should be ignored
        got = store.get("hn", kind="connector")
        assert got.module == hn_manifest.module  # original kept

    def test_get_returns_winning_source(self, hn_manifest):
        store = ManifestStore()
        store._register(hn_manifest, source="entry_point")
        store._register(hn_manifest, source="fs_scan")  # ignored
        # No exception — second register from a different source is allowed and
        # silently no-ops.
        assert store.get("hn", kind="connector") is hn_manifest

    def test_different_kinds_same_name_coexist(self):
        """A connector named 'foo' and a plugin named 'foo' can both exist."""
        from nexus.extensions.manifest import ConnectorManifest, PluginManifest

        store = ManifestStore()
        c = ConnectorManifest(name="foo", module="m", factory="F", service_name="foo")
        p = PluginManifest(name="foo", module="m", factory="F")
        store._register(c, source="test")
        store._register(p, source="test")
        assert store.get("foo", kind="connector") is c
        assert store.get("foo", kind="plugin") is p
```

- [ ] **Step 8.2: Run tests to verify they pass**

Run: `pytest tests/extensions/test_store.py::TestSourcePrecedence -v`
Expected: all 3 tests PASS — the existing `_register` already implements first-source-wins.

- [ ] **Step 8.3: No code change needed — verify by re-running the full test file**

Run: `pytest tests/extensions/test_store.py -v`
Expected: all tests still pass.

- [ ] **Step 8.4: Commit**

```bash
git add tests/extensions/test_store.py
git commit -m "test(#3962): source precedence + multi-kind name coexistence"
```

---

## Task 9: Profile filter

**Files:**
- Modify: `tests/extensions/test_store.py`

- [ ] **Step 9.1: Write the failing test**

Append to `tests/extensions/test_store.py`:

```python
class TestProfileFilter:
    def test_no_profile_filter_returns_all(self, all_manifests):
        store = ManifestStore()
        for m in all_manifests:
            store._register(m, source="test")
        assert len(store.list()) == 3

    def test_profile_filter_includes_ungated(self, all_manifests):
        """Manifests with profile_gate=None (most plugins/connectors) always appear."""
        store = ManifestStore()
        for m in all_manifests:
            store._register(m, source="test")
        # search has profile_gate="search"; hn and koi are ungated.
        listed = store.list(profile=frozenset({"other"}))
        names = {m.name for m in listed}
        assert "hn" in names  # ungated → included
        assert "koi" in names  # ungated → included
        assert "search" not in names  # gated under "search", profile is "other"

    def test_profile_filter_includes_matching_gate(self, all_manifests):
        store = ManifestStore()
        for m in all_manifests:
            store._register(m, source="test")
        listed = store.list(profile=frozenset({"search"}))
        names = {m.name for m in listed}
        assert "search" in names  # gate matches profile
```

- [ ] **Step 9.2: Run tests to verify they pass**

Run: `pytest tests/extensions/test_store.py::TestProfileFilter -v`
Expected: all 3 tests PASS — `list(profile=...)` already implemented in Task 7.

- [ ] **Step 9.3: Commit**

```bash
git add tests/extensions/test_store.py
git commit -m "test(#3962): profile-gate filter on store.list()"
```

---

## Task 10: `resolve_factory` — the only entry point that imports impl

**Files:**
- Modify: `src/nexus/extensions/store.py`
- Modify: `tests/extensions/test_store.py`

- [ ] **Step 10.1: Write the failing test**

Append to `tests/extensions/test_store.py`:

```python
class TestResolveFactory:
    def test_resolve_imports_target_module(self, monkeypatch, tmp_path):
        """resolve_factory imports the impl module and returns the named attr."""
        # Create a temp module on the path.
        import sys

        mod_path = tmp_path / "synthetic_target.py"
        mod_path.write_text("def make(): return 'hi'\n")
        monkeypatch.syspath_prepend(str(tmp_path))

        from nexus.extensions.manifest import PluginManifest

        store = ManifestStore()
        m = PluginManifest(
            name="synthetic", module="synthetic_target", factory="make",
        )
        store._register(m, source="test")

        factory = store.resolve_factory(m)
        assert callable(factory)
        assert factory() == "hi"
        assert "synthetic_target" in sys.modules

    def test_resolve_unknown_module_raises(self):
        from nexus.extensions.errors import FactoryResolutionError
        from nexus.extensions.manifest import PluginManifest

        store = ManifestStore()
        m = PluginManifest(
            name="ghost", module="nonexistent.module.path", factory="X",
        )
        store._register(m, source="test")
        with pytest.raises(FactoryResolutionError) as excinfo:
            store.resolve_factory(m)
        assert "nonexistent.module.path" in str(excinfo.value)

    def test_resolve_unknown_factory_raises(self, monkeypatch, tmp_path):
        import sys

        mod_path = tmp_path / "has_no_factory.py"
        mod_path.write_text("x = 1\n")
        monkeypatch.syspath_prepend(str(tmp_path))

        from nexus.extensions.errors import FactoryResolutionError
        from nexus.extensions.manifest import PluginManifest

        store = ManifestStore()
        m = PluginManifest(
            name="bad", module="has_no_factory", factory="missing_callable",
        )
        store._register(m, source="test")
        with pytest.raises(FactoryResolutionError) as excinfo:
            store.resolve_factory(m)
        assert "missing_callable" in str(excinfo.value)
```

- [ ] **Step 10.2: Run test to verify it fails**

Run: `pytest tests/extensions/test_store.py::TestResolveFactory -v`
Expected: `AttributeError: 'ManifestStore' object has no attribute 'resolve_factory'`.

- [ ] **Step 10.3: Implement `resolve_factory`**

In `src/nexus/extensions/store.py`, add to the imports at top:

```python
import importlib
from typing import Any, Callable
```

Append to the `ManifestStore` class:

```python
    def resolve_factory(self, manifest: AnyManifest) -> Callable[..., Any]:
        """Import the impl module and return the named factory callable.

        This is the ONLY method on the store that imports an extension impl
        module. Callers must accept that this triggers optional-dependency
        imports and may raise ImportError chains.
        """
        from nexus.extensions.errors import FactoryResolutionError

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
                detail=f"attribute not found in module",
            ) from None
```

- [ ] **Step 10.4: Run tests to verify they pass**

Run: `pytest tests/extensions/test_store.py::TestResolveFactory -v`
Expected: 3 tests PASS.

- [ ] **Step 10.5: Commit**

```bash
git add src/nexus/extensions/store.py tests/extensions/test_store.py
git commit -m "feat(#3962): ManifestStore.resolve_factory"
```

---

## Task 11: Lazy-load invariant test (the load-bearing one)

**Files:**
- Modify: `tests/extensions/test_store.py`

This task does NOT add new code — it adds the test that proves the laziness invariant: `list()`/`get()` never import the impl module. If this test ever fails, the design is broken.

- [ ] **Step 11.1: Write the lazy-load test**

Append to `tests/extensions/test_store.py`:

```python
class TestLazyInvariant:
    def test_list_does_not_import_impl(self, monkeypatch, tmp_path):
        """list() and get() must not trigger impl module imports."""
        import sys

        # Create an impl module that records when it's imported.
        impl_path = tmp_path / "lazy_impl_target.py"
        impl_path.write_text(
            "import os\n"
            "with open(os.environ['LAZY_PROBE_FILE'], 'a') as f:\n"
            "    f.write('imported\\n')\n"
            "def F(): return 1\n"
        )
        probe_file = tmp_path / "probe.txt"
        monkeypatch.setenv("LAZY_PROBE_FILE", str(probe_file))
        monkeypatch.syspath_prepend(str(tmp_path))

        # Make sure the module isn't already loaded.
        sys.modules.pop("lazy_impl_target", None)

        from nexus.extensions.manifest import PluginManifest

        store = ManifestStore()
        m = PluginManifest(name="lazy", module="lazy_impl_target", factory="F")
        store._register(m, source="test")

        # list() and get() — must NOT cause the import.
        assert store.list() == [m]
        assert store.get("lazy", kind="plugin") is m
        assert "lazy_impl_target" not in sys.modules
        assert not probe_file.exists()

        # resolve_factory() — must cause the import.
        factory = store.resolve_factory(m)
        assert factory() == 1
        assert "lazy_impl_target" in sys.modules
        assert probe_file.read_text() == "imported\n"
```

- [ ] **Step 11.2: Run tests to verify they pass**

Run: `pytest tests/extensions/test_store.py::TestLazyInvariant -v`
Expected: PASS.

- [ ] **Step 11.3: Commit**

```bash
git add tests/extensions/test_store.py
git commit -m "test(#3962): lazy-load invariant — list/get never import impl"
```

---

## Task 12: Filesystem scan loader

**Files:**
- Modify: `src/nexus/extensions/store.py`
- Modify: `tests/extensions/test_store.py`

- [ ] **Step 12.1: Write the failing test**

Append to `tests/extensions/test_store.py`:

```python
class TestFilesystemLoader:
    def test_load_from_directory(self, tmp_path, monkeypatch):
        """Scan a directory tree for `_manifest.py` files and register MANIFEST."""
        # Build a fake extensions tree:
        # tmp_path/
        #   alpha/_manifest.py        → ConnectorManifest
        #   beta/_manifest.py         → BrickManifest
        #   gamma/__init__.py         → not a manifest dir (ignored)

        (tmp_path / "alpha").mkdir()
        (tmp_path / "alpha" / "_manifest.py").write_text(
            "from nexus.extensions.manifest import ConnectorManifest\n"
            "MANIFEST = ConnectorManifest(\n"
            "    name='alpha', module='m.alpha', factory='F', service_name='alpha',\n"
            ")\n"
        )
        (tmp_path / "beta").mkdir()
        (tmp_path / "beta" / "_manifest.py").write_text(
            "from nexus.extensions.manifest import BrickManifest\n"
            "MANIFEST = BrickManifest(\n"
            "    name='beta', module='m.beta', factory='F',\n"
            "    tier='independent', result_key='r',\n"
            ")\n"
        )
        (tmp_path / "gamma").mkdir()
        (tmp_path / "gamma" / "__init__.py").write_text("")

        monkeypatch.syspath_prepend(str(tmp_path))

        store = ManifestStore()
        store.load_filesystem(tmp_path)

        assert {m.name for m in store.list()} == {"alpha", "beta"}

    def test_filesystem_load_skips_broken_module(self, tmp_path, monkeypatch, caplog):
        """A broken `_manifest.py` doesn't block siblings; warning logged."""
        import logging

        (tmp_path / "good").mkdir()
        (tmp_path / "good" / "_manifest.py").write_text(
            "from nexus.extensions.manifest import PluginManifest\n"
            "MANIFEST = PluginManifest(name='good', module='m', factory='F')\n"
        )
        (tmp_path / "broken").mkdir()
        (tmp_path / "broken" / "_manifest.py").write_text(
            "raise RuntimeError('intentional')\n"
        )
        monkeypatch.syspath_prepend(str(tmp_path))

        store = ManifestStore()
        with caplog.at_level(logging.WARNING):
            store.load_filesystem(tmp_path)

        # 'good' loaded; 'broken' isolated.
        assert {m.name for m in store.list()} == {"good"}
        assert any("broken" in r.message for r in caplog.records)
```

- [ ] **Step 12.2: Run test to verify it fails**

Run: `pytest tests/extensions/test_store.py::TestFilesystemLoader -v`
Expected: `AttributeError: 'ManifestStore' object has no attribute 'load_filesystem'`.

- [ ] **Step 12.3: Implement `load_filesystem`**

Add to imports at top of `src/nexus/extensions/store.py`:

```python
import logging
from pathlib import Path

logger = logging.getLogger(__name__)
```

Append to the `ManifestStore` class:

```python
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
            try:
                # Use importlib to load the file directly. The file's parent
                # must be on sys.path for relative imports inside the manifest
                # to work — callers are expected to have ensured this.
                spec = importlib.util.spec_from_file_location(
                    f"_nexus_manifest_{child.name}", manifest_file
                )
                if spec is None or spec.loader is None:
                    raise ImportError(f"could not build spec for {manifest_file}")
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
            except Exception as exc:
                logger.warning(
                    "Skipping broken manifest at %s: %s", manifest_file, exc
                )
                continue

            manifest = getattr(module, "MANIFEST", None)
            if manifest is None:
                logger.warning(
                    "No MANIFEST constant in %s; skipping", manifest_file
                )
                continue

            try:
                self._register(manifest, source=f"fs_scan:{manifest_file}")
            except DuplicateManifestError as exc:
                logger.warning("Duplicate manifest skipped: %s", exc)
```

Add to imports:

```python
import importlib.util
```

- [ ] **Step 12.4: Run tests to verify they pass**

Run: `pytest tests/extensions/test_store.py::TestFilesystemLoader -v`
Expected: 2 tests PASS.

- [ ] **Step 12.5: Commit**

```bash
git add src/nexus/extensions/store.py tests/extensions/test_store.py
git commit -m "feat(#3962): ManifestStore.load_filesystem with per-extension isolation"
```

---

## Task 13: Entry-point loader

**Files:**
- Modify: `src/nexus/extensions/store.py`
- Modify: `tests/extensions/test_store.py`

- [ ] **Step 13.1: Write the failing test**

Append to `tests/extensions/test_store.py`:

```python
class TestEntryPointLoader:
    def test_load_from_entry_points(self, monkeypatch):
        """Entry points whose target is a `_manifest` module are registered."""
        from importlib.metadata import EntryPoint

        # Build synthetic entry points pointing at a module we'll register
        # in sys.modules ahead of time (avoids real package install).
        import sys
        import types

        fake_mod = types.ModuleType("fake_pkg.alpha._manifest")
        from nexus.extensions.manifest import ConnectorManifest

        fake_mod.MANIFEST = ConnectorManifest(
            name="alpha", module="fake_pkg.alpha.connector",
            factory="F", service_name="alpha",
        )
        sys.modules["fake_pkg.alpha._manifest"] = fake_mod

        ep = EntryPoint(
            name="alpha", value="fake_pkg.alpha._manifest", group="nexus.connectors"
        )

        def fake_entry_points(group: str):
            if group == "nexus.connectors":
                return [ep]
            return []

        monkeypatch.setattr(
            "nexus.extensions.store._entry_points", fake_entry_points
        )

        store = ManifestStore()
        store.load_entry_points()

        names = {m.name for m in store.list()}
        assert "alpha" in names

    def test_entry_point_import_failure_isolated(self, monkeypatch, caplog):
        """A broken entry point logs WARN and doesn't block others."""
        import logging
        from importlib.metadata import EntryPoint

        good_ep = EntryPoint(
            name="good", value="fake_pkg.good._manifest", group="nexus.plugins"
        )
        bad_ep = EntryPoint(
            name="bad", value="nonexistent.module._manifest", group="nexus.plugins"
        )

        import sys
        import types

        good_mod = types.ModuleType("fake_pkg.good._manifest")
        from nexus.extensions.manifest import PluginManifest

        good_mod.MANIFEST = PluginManifest(
            name="good", module="m", factory="F"
        )
        sys.modules["fake_pkg.good._manifest"] = good_mod

        def fake_entry_points(group: str):
            if group == "nexus.plugins":
                return [good_ep, bad_ep]
            return []

        monkeypatch.setattr(
            "nexus.extensions.store._entry_points", fake_entry_points
        )

        store = ManifestStore()
        with caplog.at_level(logging.WARNING):
            store.load_entry_points()

        assert {m.name for m in store.list()} == {"good"}
        assert any("bad" in r.message for r in caplog.records)
```

- [ ] **Step 13.2: Run test to verify it fails**

Run: `pytest tests/extensions/test_store.py::TestEntryPointLoader -v`
Expected: `AttributeError: 'ManifestStore' object has no attribute 'load_entry_points'`.

- [ ] **Step 13.3: Implement `load_entry_points`**

Add to imports at top of `src/nexus/extensions/store.py`:

```python
from importlib.metadata import entry_points as _stdlib_entry_points
```

Add this module-level function (used for monkeypatching in tests):

```python
def _entry_points(group: str):
    """Indirection so tests can monkeypatch without touching the stdlib import."""
    return _stdlib_entry_points(group=group)
```

Append to the `ManifestStore` class:

```python
    _ENTRY_POINT_GROUPS: tuple[str, ...] = (
        "nexus.connectors",
        "nexus.bricks",
        "nexus.plugins",
    )

    def load_entry_points(self) -> None:
        """Discover manifests via importlib.metadata entry points.

        Entry-point targets must be `_manifest` modules — i.e., the entry-point
        value points to a module whose top level defines `MANIFEST` as a
        manifest instance. This is the documented contract for third-party
        extensions.
        """
        for group in self._ENTRY_POINT_GROUPS:
            for ep in _entry_points(group):
                try:
                    module = importlib.import_module(ep.value)
                except Exception as exc:
                    logger.warning(
                        "Failed to load entry point %s in group %s: %s",
                        ep.name, group, exc,
                    )
                    continue
                manifest = getattr(module, "MANIFEST", None)
                if manifest is None:
                    logger.warning(
                        "Entry point %s in group %s has no MANIFEST",
                        ep.name, group,
                    )
                    continue
                try:
                    self._register(manifest, source=f"entry_point:{group}/{ep.name}")
                except DuplicateManifestError as exc:
                    logger.warning("Duplicate entry-point manifest: %s", exc)
```

- [ ] **Step 13.4: Run tests to verify they pass**

Run: `pytest tests/extensions/test_store.py::TestEntryPointLoader -v`
Expected: 2 tests PASS.

- [ ] **Step 13.5: Commit**

```bash
git add src/nexus/extensions/store.py tests/extensions/test_store.py
git commit -m "feat(#3962): ManifestStore.load_entry_points with isolation"
```

---

## Task 14: JSON index loader

**Files:**
- Modify: `src/nexus/extensions/store.py`
- Modify: `tests/extensions/test_store.py`

- [ ] **Step 14.1: Write the failing test**

Append to `tests/extensions/test_store.py`:

```python
class TestJsonIndexLoader:
    SCHEMA_VERSION = 1

    def _index_payload(self, manifests):
        """Serialize manifests to the index format."""
        return {
            "schema_version": self.SCHEMA_VERSION,
            "generated_at": "2026-04-30T12:00:00Z",
            "manifests": sorted(
                (m.model_dump(mode="json") for m in manifests),
                key=lambda d: (d["kind"], d["name"]),
            ),
        }

    def test_load_index_round_trip(self, tmp_path, all_manifests):
        import json

        index_file = tmp_path / "extensions.json"
        index_file.write_text(json.dumps(self._index_payload(all_manifests)))

        store = ManifestStore()
        store.load_json_index(index_file)

        assert {m.name for m in store.list()} == {"hn", "search", "koi"}

    def test_missing_index_falls_back_silently(self, tmp_path, caplog):
        import logging

        store = ManifestStore()
        with caplog.at_level(logging.INFO):
            store.load_json_index(tmp_path / "does_not_exist.json")

        assert store.list() == []
        # An INFO log explains the fall-through.
        assert any(
            "extensions.json" in r.message for r in caplog.records
        )

    def test_corrupt_json_raises(self, tmp_path):
        from nexus.extensions.errors import IndexCorruptError

        bad = tmp_path / "extensions.json"
        bad.write_text("{ not json")
        store = ManifestStore()
        with pytest.raises(IndexCorruptError):
            store.load_json_index(bad)

    def test_schema_version_mismatch_warns_and_skips(self, tmp_path, caplog):
        import json
        import logging

        bad = tmp_path / "extensions.json"
        bad.write_text(json.dumps({
            "schema_version": 999, "generated_at": "x", "manifests": [],
        }))

        store = ManifestStore()
        with caplog.at_level(logging.WARNING):
            store.load_json_index(bad)

        assert store.list() == []
        assert any("schema_version" in r.message for r in caplog.records)
```

- [ ] **Step 14.2: Run test to verify it fails**

Run: `pytest tests/extensions/test_store.py::TestJsonIndexLoader -v`
Expected: `AttributeError: 'ManifestStore' object has no attribute 'load_json_index'`.

- [ ] **Step 14.3: Implement `load_json_index`**

Add to imports at top of `src/nexus/extensions/store.py`:

```python
import json
```

Add a constant near the top of the module:

```python
INDEX_SCHEMA_VERSION = 1
```

Append to the `ManifestStore` class:

```python
    def load_json_index(self, path: Path) -> None:
        """Load manifests from a pre-built `extensions.json` index.

        Behavior:
        - Missing file → INFO log, return (so callers can fall back to
          entry-points + fs scan).
        - Malformed JSON → IndexCorruptError.
        - Schema-version mismatch → WARN, skip (caller falls back).
        """
        from nexus.extensions.errors import IndexCorruptError
        from nexus.extensions.manifest import parse_manifest

        path = Path(path)
        if not path.exists():
            logger.info(
                "No extensions.json at %s; falling back to live discovery", path
            )
            return

        try:
            payload = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            raise IndexCorruptError(
                f"extensions.json at {path} is not valid JSON: {exc}"
            ) from exc

        version = payload.get("schema_version")
        if version != INDEX_SCHEMA_VERSION:
            logger.warning(
                "extensions.json schema_version=%s does not match expected %s; "
                "ignoring index",
                version, INDEX_SCHEMA_VERSION,
            )
            return

        for raw in payload.get("manifests", []):
            try:
                manifest = parse_manifest(raw)
            except Exception as exc:
                logger.warning("Skipping malformed manifest in index: %s", exc)
                continue
            try:
                self._register(manifest, source="json_index")
            except DuplicateManifestError as exc:
                logger.warning("Duplicate index manifest: %s", exc)
```

- [ ] **Step 14.4: Run tests to verify they pass**

Run: `pytest tests/extensions/test_store.py::TestJsonIndexLoader -v`
Expected: 4 tests PASS.

- [ ] **Step 14.5: Commit**

```bash
git add src/nexus/extensions/store.py tests/extensions/test_store.py
git commit -m "feat(#3962): ManifestStore.load_json_index"
```

---

## Task 15: `check()` — dependency report without importing impl

**Files:**
- Modify: `src/nexus/extensions/store.py`
- Modify: `tests/extensions/test_store.py`

- [ ] **Step 15.1: Write the failing test**

Append to `tests/extensions/test_store.py`:

```python
class TestCheckMethod:
    def test_check_all_available(self, monkeypatch):
        """An extension whose runtime_deps are all satisfied is available."""
        # python deps satisfied: import_probes are all importable.
        # 'sys' is always importable.
        from nexus.extensions.manifest import PluginManifest, RuntimeDep

        store = ManifestStore()
        m = PluginManifest(
            name="ok", module="m", factory="F",
            runtime_deps=(RuntimeDep(kind="python", name="sys"),),
            import_probes=("sys",),
        )
        store._register(m, source="test")
        report = store.check(m)
        assert report.available is True
        assert report.missing_python_deps == ()
        assert report.import_probe_failures == ()

    def test_check_missing_python_dep(self):
        from nexus.extensions.manifest import PluginManifest, RuntimeDep

        store = ManifestStore()
        m = PluginManifest(
            name="needs", module="m", factory="F",
            runtime_deps=(
                RuntimeDep(kind="python", name="totally_not_a_real_pkg_xyz"),
            ),
            import_probes=("totally_not_a_real_pkg_xyz",),
        )
        store._register(m, source="test")
        report = store.check(m)
        assert report.available is False
        assert "totally_not_a_real_pkg_xyz" in report.import_probe_failures

    def test_check_does_not_import_impl(self, monkeypatch, tmp_path):
        """check() must not import the impl module — only probes."""
        import sys

        impl = tmp_path / "impl_no_import.py"
        impl.write_text("raise RuntimeError('impl import side-effect')\n")
        monkeypatch.syspath_prepend(str(tmp_path))
        sys.modules.pop("impl_no_import", None)

        from nexus.extensions.manifest import PluginManifest

        store = ManifestStore()
        m = PluginManifest(
            name="lazy_check", module="impl_no_import", factory="F",
            import_probes=("sys",),  # only probe sys, not the impl module
        )
        store._register(m, source="test")
        report = store.check(m)
        assert report.available is True
        assert "impl_no_import" not in sys.modules
```

- [ ] **Step 15.2: Run test to verify it fails**

Run: `pytest tests/extensions/test_store.py::TestCheckMethod -v`
Expected: `AttributeError: 'ManifestStore' object has no attribute 'check'`.

- [ ] **Step 15.3: Implement `check`**

Append to the `ManifestStore` class:

```python
    def check(self, manifest: AnyManifest) -> CheckReport:
        """Run import probes and dependency declarations to report availability.

        Does NOT import the manifest's impl module. Only `import_probes` are
        attempted; binary/service deps are reported as declared (we don't
        execute them here).
        """
        probe_failures: list[str] = []
        for probe in manifest.import_probes:
            try:
                importlib.import_module(probe)
            except ImportError:
                probe_failures.append(probe)

        missing_python = tuple(
            d.name for d in manifest.runtime_deps
            if d.kind == "python" and d.name in probe_failures
        )
        # Binary and service deps are declarative; we report them as "missing"
        # only if their corresponding import_probe failed (best-effort).
        # In practice, a follow-up could shell out to `which` for binary deps.
        missing_binary = tuple(
            d.name for d in manifest.runtime_deps if d.kind == "binary"
        )
        missing_service = tuple(
            d.name for d in manifest.runtime_deps if d.kind == "service"
        )

        # We don't gate "available" on binary/service deps in this PR — those
        # are advisory until an active checker is added (out of scope).
        available = not probe_failures

        return CheckReport(
            available=available,
            missing_python_deps=missing_python,
            missing_binary_deps=() if available else missing_binary,
            missing_services=() if available else missing_service,
            import_probe_failures=tuple(probe_failures),
            profile_gate_disabled=False,
        )
```

- [ ] **Step 15.4: Run tests to verify they pass**

Run: `pytest tests/extensions/test_store.py::TestCheckMethod -v`
Expected: 3 tests PASS.

- [ ] **Step 15.5: Commit**

```bash
git add src/nexus/extensions/store.py tests/extensions/test_store.py
git commit -m "feat(#3962): ManifestStore.check — declarative dependency report"
```

---

## Task 16: Module-level singleton `get_store()`

**Files:**
- Modify: `src/nexus/extensions/store.py`
- Modify: `src/nexus/extensions/__init__.py`
- Modify: `tests/extensions/test_store.py`

- [ ] **Step 16.1: Write the failing test**

Append to `tests/extensions/test_store.py`:

```python
class TestSingleton:
    def test_get_store_returns_same_instance(self):
        from nexus.extensions.store import get_store, reset_store

        reset_store()
        s1 = get_store()
        s2 = get_store()
        assert s1 is s2

    def test_reset_store_clears_state(self, hn_manifest):
        from nexus.extensions.store import get_store, reset_store

        reset_store()
        s1 = get_store()
        s1._register(hn_manifest, source="test")
        assert len(s1.list()) == 1

        reset_store()
        s2 = get_store()
        assert s2 is not s1
        assert s2.list() == []
```

- [ ] **Step 16.2: Run test to verify it fails**

Run: `pytest tests/extensions/test_store.py::TestSingleton -v`
Expected: `ImportError: cannot import name 'get_store'`.

- [ ] **Step 16.3: Implement singleton**

Append to `src/nexus/extensions/store.py`:

```python
_STORE: ManifestStore | None = None


def get_store() -> ManifestStore:
    """Return the process-wide manifest store, lazily populating on first call.

    Population order: JSON index > entry points > (optional) filesystem scan.
    The filesystem scan is gated behind the NEXUS_EXTENSIONS_DEV_SCAN env var.
    """
    import os

    global _STORE
    if _STORE is not None:
        return _STORE

    store = ManifestStore()

    # 1. JSON index (shipped with the wheel). Path resolved at import time.
    index_path = (
        Path(__file__).parent / "_index" / "extensions.json"
    )
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
    _STORE = None
```

- [ ] **Step 16.4: Update `nexus/extensions/__init__.py` to re-export**

Edit `src/nexus/extensions/__init__.py` to add the imports:

Replace `__all__` and the imports block to include `get_store` from `nexus.extensions.store`:

```python
from nexus.extensions.store import CheckReport, ManifestStore, get_store
```

Append `"CheckReport"`, `"ManifestStore"`, `"get_store"` to `__all__`.

- [ ] **Step 16.5: Run tests to verify they pass**

Run: `pytest tests/extensions/test_store.py::TestSingleton -v`
Expected: 2 tests PASS.

Run: `pytest tests/extensions/ -v`
Expected: ALL tests in `tests/extensions/` PASS.

- [ ] **Step 16.6: Commit**

```bash
git add src/nexus/extensions/store.py src/nexus/extensions/__init__.py tests/extensions/test_store.py
git commit -m "feat(#3962): module-level get_store singleton"
```

---

## Task 17: Index generator — `python -m nexus.extensions.index build`

**Files:**
- Create: `src/nexus/extensions/index.py`
- Create: `src/nexus/extensions/_index/__init__.py`
- Create: `src/nexus/extensions/_index/extensions.json` (initial empty)
- Create: `tests/extensions/test_index.py`

- [ ] **Step 17.1: Create the index package**

Create `src/nexus/extensions/_index/__init__.py` (empty file).

Create `src/nexus/extensions/_index/extensions.json` (initial empty):

```json
{
  "schema_version": 1,
  "generated_at": "2026-04-30T00:00:00Z",
  "manifests": []
}
```

- [ ] **Step 17.2: Write the failing test**

Create `tests/extensions/test_index.py`:

```python
"""Index generator — deterministic JSON build + drift detection."""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

from nexus.extensions.index import build_index, verify_index
from nexus.extensions.manifest import (
    BrickManifest,
    ConnectorManifest,
    PluginManifest,
)
from nexus.extensions.store import INDEX_SCHEMA_VERSION


pytest_plugins = ["tests.extensions.fixtures.conftest"]


class TestBuildIndex:
    def test_build_serializes_manifests(self, tmp_path, all_manifests):
        out = tmp_path / "extensions.json"
        build_index(manifests=all_manifests, output_path=out)

        payload = json.loads(out.read_text())
        assert payload["schema_version"] == INDEX_SCHEMA_VERSION
        assert "generated_at" in payload
        names = {m["name"] for m in payload["manifests"]}
        assert names == {"hn", "search", "koi"}

    def test_build_is_deterministic(self, tmp_path, all_manifests):
        """Same input → same bytes (excluding generated_at)."""
        out1 = tmp_path / "a.json"
        out2 = tmp_path / "b.json"
        build_index(manifests=all_manifests, output_path=out1, frozen_time="X")
        build_index(
            manifests=list(reversed(all_manifests)), output_path=out2, frozen_time="X"
        )

        # Output is deterministic: sorted by (kind, name), stable formatting.
        assert out1.read_text() == out2.read_text()

    def test_index_round_trip_via_store(self, tmp_path, all_manifests):
        from nexus.extensions.store import ManifestStore

        out = tmp_path / "extensions.json"
        build_index(manifests=all_manifests, output_path=out)

        store = ManifestStore()
        store.load_json_index(out)
        assert {m.name for m in store.list()} == {"hn", "search", "koi"}


class TestVerifyIndex:
    def test_verify_passes_on_match(self, tmp_path, all_manifests):
        out = tmp_path / "extensions.json"
        build_index(manifests=all_manifests, output_path=out, frozen_time="X")
        # Re-run with same input → no drift.
        result = verify_index(manifests=all_manifests, expected_path=out, frozen_time="X")
        assert result.is_clean is True
        assert result.diff is None

    def test_verify_detects_drift(self, tmp_path, all_manifests, hn_manifest):
        out = tmp_path / "extensions.json"
        build_index(manifests=all_manifests, output_path=out, frozen_time="X")
        # Drop one manifest → drift expected.
        without_hn = [m for m in all_manifests if m.name != "hn"]
        result = verify_index(manifests=without_hn, expected_path=out, frozen_time="X")
        assert result.is_clean is False
        assert result.diff is not None
```

- [ ] **Step 17.3: Run test to verify it fails**

Run: `pytest tests/extensions/test_index.py -v`
Expected: `ModuleNotFoundError: No module named 'nexus.extensions.index'`.

- [ ] **Step 17.4: Implement `index.py`**

Create `src/nexus/extensions/index.py`:

```python
"""Index generator for `extensions.json`.

Builds a deterministic JSON snapshot of all in-tree manifests at install/CI
time. The result is consumed by `ManifestStore.load_json_index` for fast,
zero-import enumeration.

Usage:
    python -m nexus.extensions.index build [--output PATH]
    python -m nexus.extensions.index verify [--against PATH]
"""

from __future__ import annotations

import argparse
import datetime as _dt
import difflib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from nexus.extensions.manifest import AnyManifest
from nexus.extensions.store import INDEX_SCHEMA_VERSION


def _serialize(manifests: Iterable[AnyManifest], frozen_time: str | None) -> str:
    sorted_manifests = sorted(
        (m.model_dump(mode="json") for m in manifests),
        key=lambda d: (d["kind"], d["name"]),
    )
    payload = {
        "schema_version": INDEX_SCHEMA_VERSION,
        "generated_at": frozen_time
        or _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds").replace(
            "+00:00", "Z"
        ),
        "manifests": sorted_manifests,
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def build_index(
    *,
    manifests: Iterable[AnyManifest],
    output_path: Path,
    frozen_time: str | None = None,
) -> None:
    """Write a deterministic JSON index to `output_path`.

    `frozen_time` is for testing — pass a fixed string to make output bit-stable.
    """
    output_path.write_text(_serialize(manifests, frozen_time=frozen_time))


@dataclass(frozen=True)
class VerifyResult:
    is_clean: bool
    diff: str | None


def verify_index(
    *,
    manifests: Iterable[AnyManifest],
    expected_path: Path,
    frozen_time: str | None = None,
) -> VerifyResult:
    """Compare the generated index against the on-disk file.

    `generated_at` is read from the on-disk file and reused when regenerating,
    so verify only flags structural drift — not clock differences.

    Returns a `VerifyResult` with a unified diff if drift is detected.
    """
    on_disk = expected_path.read_text() if expected_path.exists() else ""

    # Pull generated_at from the on-disk file so the comparison is bit-stable
    # regardless of when verify runs. Test callers can override via frozen_time.
    if frozen_time is None and on_disk:
        try:
            existing = json.loads(on_disk)
            frozen_time = existing.get("generated_at")
        except json.JSONDecodeError:
            frozen_time = None

    fresh = _serialize(manifests, frozen_time=frozen_time)

    if fresh == on_disk:
        return VerifyResult(is_clean=True, diff=None)

    diff = "".join(
        difflib.unified_diff(
            on_disk.splitlines(keepends=True),
            fresh.splitlines(keepends=True),
            fromfile=str(expected_path),
            tofile="<generated>",
        )
    )
    return VerifyResult(is_clean=False, diff=diff)


def _discover_in_tree_manifests() -> list[AnyManifest]:
    """Walk the source tree for `_manifest.py` files and return their MANIFEST."""
    from nexus.extensions.store import ManifestStore

    store = ManifestStore()
    repo_root = Path(__file__).parent.parent  # src/nexus/
    for subdir in ("backends/connectors", "bricks", "plugins"):
        root = repo_root / subdir
        if root.exists():
            store.load_filesystem(root)
    return store.list()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nexus-extensions-index")
    sub = parser.add_subparsers(dest="cmd", required=True)

    build_p = sub.add_parser("build", help="Generate extensions.json")
    build_p.add_argument(
        "--output", type=Path,
        default=Path(__file__).parent / "_index" / "extensions.json",
    )

    verify_p = sub.add_parser("verify", help="Check extensions.json is up to date")
    verify_p.add_argument(
        "--against", type=Path,
        default=Path(__file__).parent / "_index" / "extensions.json",
    )

    args = parser.parse_args(argv)
    manifests = _discover_in_tree_manifests()

    if args.cmd == "build":
        build_index(manifests=manifests, output_path=args.output)
        print(f"Wrote {len(manifests)} manifests to {args.output}")
        return 0

    if args.cmd == "verify":
        result = verify_index(manifests=manifests, expected_path=args.against)
        if result.is_clean:
            print(f"OK: {args.against} is up to date")
            return 0
        print(f"DRIFT: {args.against} differs from generated output")
        if result.diff:
            print(result.diff)
        return 1

    return 2


def _cli_entry() -> None:
    """Console-script wrapper — propagates main()'s return code as exit status."""
    sys.exit(main())


if __name__ == "__main__":
    _cli_entry()
```

- [ ] **Step 17.5: Run tests to verify they pass**

Run: `pytest tests/extensions/test_index.py -v`
Expected: 5 tests PASS.

- [ ] **Step 17.6: Verify the CLI runs**

Run: `python -m nexus.extensions.index build --output /tmp/test_extensions.json`
Expected: prints `Wrote 0 manifests to /tmp/test_extensions.json` (no in-tree `_manifest.py` files exist yet — they appear in PR 2).

Run: `python -m nexus.extensions.index verify --against /tmp/test_extensions.json`
Expected: `OK: /tmp/test_extensions.json is up to date` (return code 0).

- [ ] **Step 17.7: Commit**

```bash
git add src/nexus/extensions/index.py src/nexus/extensions/_index/ tests/extensions/test_index.py
git commit -m "feat(#3962): index generator with build/verify subcommands"
```

---

## Task 18: Pre-commit hook for index drift

**Files:**
- Modify: `.pre-commit-config.yaml`

- [ ] **Step 18.1: Read existing pre-commit config**

```bash
head -50 .pre-commit-config.yaml
```

Locate the `repos:` section and the `local` hooks block (most repos have one).

- [ ] **Step 18.2: Add the drift-check hook**

In `.pre-commit-config.yaml`, append a new hook to the existing `local` repo (or create a `local` repo if absent). The hook entry:

```yaml
  - repo: local
    hooks:
      - id: extensions-index-verify
        name: Verify extensions.json is up to date
        entry: python -m nexus.extensions.index verify
        language: system
        pass_filenames: false
        files: ^(src/nexus/extensions/|src/nexus/(backends|bricks|plugins)/.*/_manifest\.py$)
```

If the `local` repo block already exists, only add the `id: extensions-index-verify` entry under its `hooks:` list. Don't duplicate the `repo: local` header.

- [ ] **Step 18.3: Test the hook locally**

Run: `pre-commit run extensions-index-verify --all-files`
Expected: PASS — the empty `extensions.json` matches what the generator produces against zero in-tree `_manifest.py` files.

If the hook is not yet installed:

```bash
pre-commit install
pre-commit run extensions-index-verify --all-files
```

- [ ] **Step 18.4: Verify the hook detects drift**

Make a deliberate drift:

```bash
echo '{"schema_version": 1, "generated_at": "X", "manifests": [{"kind":"plugin","name":"ghost","module":"m","factory":"F","entry_point_group":"nexus.plugins","hooks":{},"commands":{},"description":"","runtime_deps":[],"config_schema":null,"profile_gate":null,"import_probes":[]}]}' > src/nexus/extensions/_index/extensions.json
pre-commit run extensions-index-verify --all-files
```

Expected: hook FAILS with a drift diff. Restore the file:

```bash
git checkout src/nexus/extensions/_index/extensions.json
```

- [ ] **Step 18.5: Commit**

```bash
git add .pre-commit-config.yaml
git commit -m "chore(#3962): pre-commit hook to verify extensions.json"
```

---

## Task 19: pyproject.toml console script

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 19.1: Find the existing scripts table**

```bash
grep -n "scripts\]\|project.scripts" pyproject.toml | head -5
```

- [ ] **Step 19.2: Add the console script entry**

In `pyproject.toml`, under the existing `[project.scripts]` table (or create one in the `[project]` block if absent), add:

```toml
nexus-extensions-index = "nexus.extensions.index:_cli_entry"
```

If `[project.scripts]` does not exist, add it after the `[project]` table:

```toml
[project.scripts]
nexus-extensions-index = "nexus.extensions.index:_cli_entry"
```

(Other existing scripts stay; this is one additional entry.)

- [ ] **Step 19.3: Reinstall the package and verify the script**

Run: `pip install -e . --quiet` (or whatever the repo uses for editable installs)

Run: `nexus-extensions-index verify`
Expected: `OK: ...extensions.json is up to date` (return code 0).

- [ ] **Step 19.4: Commit**

```bash
git add pyproject.toml
git commit -m "chore(#3962): expose nexus-extensions-index console script"
```

---

## Task 20: Final integration sweep

**Files:**
- No code changes — this task verifies the whole PR holds together.

- [ ] **Step 20.1: Run the full extensions test suite**

Run: `pytest tests/extensions/ -v`
Expected: all tests PASS, no warnings other than expected DeprecationWarnings (none expected in PR 1).

- [ ] **Step 20.2: Run the broader test suite to confirm no regressions**

Run: `pytest tests/backends/ -x -q 2>&1 | tail -20`
Expected: no failures introduced by Task 2's `ConnectionArg` extraction.

Run: `pytest tests/ -x -q --ignore=tests/extensions 2>&1 | tail -30`
Expected: no failures introduced by this PR.

- [ ] **Step 20.3: Verify package import is still pure**

Run: `python -c "import sys; before = set(sys.modules); import nexus.extensions; new = set(sys.modules) - before; bad = [m for m in new if m.startswith(('nexus.backends.connectors.', 'nexus.bricks.', 'nexus.plugins.base'))]; assert not bad, bad; print('clean')"`
Expected: `clean`.

- [ ] **Step 20.4: Verify the index generator runs against the empty in-tree state**

Run: `python -m nexus.extensions.index verify`
Expected: `OK: src/nexus/extensions/_index/extensions.json is up to date`.

- [ ] **Step 20.5: Run pre-commit on the worktree to catch lint/type issues**

Run: `pre-commit run --all-files 2>&1 | tail -30`
Expected: all hooks PASS (or only fail on unrelated pre-existing issues — fix any new failures from this PR).

- [ ] **Step 20.6: Final commit (if any fixes were needed in 20.1–20.5)**

If steps above triggered fixes:

```bash
git add -A
git commit -m "fix(#3962): post-integration cleanup"
```

If no fixes needed, skip this step.

- [ ] **Step 20.7: Verify branch is ready for PR**

Run: `git log --oneline develop..HEAD`
Expected: a clean sequence of focused commits, one per task. No fixup commits, no WIPs.

---

## Spec Coverage Map

| Spec section | PR 1 task |
|---|---|
| Layer 1 — Manifest contract | Tasks 4–6 |
| Layer 2 — Manifest store | Tasks 7–16 |
| Layer 3 — Index generator | Task 17 |
| Pure types extraction (boundary rule) | Task 2 |
| CI drift check | Task 18 |
| Console script | Task 19 |
| Lazy invariants | Tasks 11, 15 |
| Failure isolation | Tasks 12, 13 |
| Reserved names | Task 5 |
| Source precedence | Task 8 |
| Profile filter | Task 9 |
| JSON schema versioning | Task 14 |

**Out of this PR (covered by PR 2 and PR 3 plans, written after this lands):**

- Per-kind adapters (`ConnectorRegistry`, brick factory, `PluginRegistry`).
- Migration of one connector / brick / plugin to `_manifest.py`.
- Deprecation warnings on legacy decorators / constants.
- Introspection API (`nexus.extensions.introspect`).
- `nexus extensions` CLI.
- `/api/extensions` HTTP endpoint.
- HTTP/CLI tests.
- Adapter parity tests.

---

## Definition of Done for PR 1

- [ ] All 20 tasks complete with commits.
- [ ] `pytest tests/extensions/` is green.
- [ ] `pytest tests/backends/` is green (no regressions from Task 2).
- [ ] `python -m nexus.extensions.index verify` returns 0.
- [ ] `pre-commit run --all-files` is green.
- [ ] PR description references issue #3962 and links the spec doc.
- [ ] PR description notes that PR 2 and PR 3 will follow with adapters and introspection respectively.
