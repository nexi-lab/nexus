# Typed runtime-dep schema + mount-time dep check — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace untyped `ConnectorInfo.requires: list[str]` with a typed `RUNTIME_DEPS` schema (`PythonDep` / `BinaryDep` / `ServiceDep`), enforce at mount time via `BackendFactory.create()`, and migrate all ~22 registered connectors.

**Architecture:** New `runtime_deps.py` module holds the dataclass union + `check_runtime_deps()` helper. `ConnectorInfo` gains a `runtime_deps` field, populated from either the `runtime_deps=` decorator kwarg or a `RUNTIME_DEPS` class attribute (decorator wins). `BackendFactory.create()` runs the check right after `get_info()` and raises `MissingDependencyError` (subclass of `BackendError`) with every missing dep enumerated. Legacy `requires` becomes a derived `@property` for one release.

**Tech Stack:** Python 3.14, dataclasses, `importlib.util.find_spec`, `shutil.which`, pytest.

**Spec:** [`docs/superpowers/specs/2026-04-21-issue-3830-connector-runtime-deps-design.md`](../specs/2026-04-21-issue-3830-connector-runtime-deps-design.md)

---

## File structure

### New files
- `src/nexus/backends/base/runtime_deps.py` — `PythonDep` / `BinaryDep` / `ServiceDep` dataclasses, `RuntimeDep` union, `check_runtime_deps()`, `_server_available()`.
- `tests/unit/backends/test_runtime_deps.py` — unit tests for dep types + checker.
- `tests/integration/backends/test_factory_dep_check.py` — integration test for `BackendFactory.create()` raising `MissingDependencyError`.

### Modified files
- `src/nexus/contracts/exceptions.py` — add `MissingDependencyError(BackendError)`.
- `src/nexus/backends/base/registry.py` — extend `ConnectorInfo`, `ConnectorRegistry.register()`, `register_connector` decorator; deprecate `requires`.
- `src/nexus/backends/base/factory.py` — call `check_runtime_deps()` before instantiation.
- `tests/unit/backends/test_registry.py` — tests for `runtime_deps` kwarg, precedence, validation, legacy `requires` derivation.
- 22 connector registration sites (see Task 8–11).

---

## Task 1: Runtime-dep types module

**Files:**
- Create: `src/nexus/backends/base/runtime_deps.py`
- Test: `tests/unit/backends/test_runtime_deps.py`

- [ ] **Step 1: Write failing tests for dep dataclasses**

Create `tests/unit/backends/test_runtime_deps.py`:

```python
"""Unit tests for runtime_deps module (Issue #3830, sub-project A)."""

from __future__ import annotations

import pytest

from nexus.backends.base.runtime_deps import (
    BinaryDep,
    PythonDep,
    RuntimeDep,
    ServiceDep,
)


class TestDepTypes:
    def test_python_dep_defaults(self) -> None:
        dep = PythonDep("google.cloud.storage")
        assert dep.module == "google.cloud.storage"
        assert dep.extras == ()

    def test_python_dep_with_extras(self) -> None:
        dep = PythonDep("google.cloud.storage", extras=("gcs",))
        assert dep.extras == ("gcs",)

    def test_python_dep_is_frozen(self) -> None:
        dep = PythonDep("boto3")
        with pytest.raises(AttributeError):
            dep.module = "other"  # type: ignore[misc]

    def test_binary_dep_requires_hint(self) -> None:
        dep = BinaryDep(name="gws", install_hint="brew install nexi-lab/tap/gws")
        assert dep.name == "gws"
        assert dep.install_hint == "brew install nexi-lab/tap/gws"

    def test_service_dep_name(self) -> None:
        dep = ServiceDep(name="token_manager")
        assert dep.name == "token_manager"

    def test_runtime_dep_union_accepts_all_three(self) -> None:
        deps: tuple[RuntimeDep, ...] = (
            PythonDep("httpx"),
            BinaryDep("gws", "brew install gws"),
            ServiceDep("kernel"),
        )
        assert len(deps) == 3
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/unit/backends/test_runtime_deps.py::TestDepTypes -v
```

Expected: `ImportError: No module named 'nexus.backends.base.runtime_deps'`.

- [ ] **Step 3: Create the runtime_deps module**

Create `src/nexus/backends/base/runtime_deps.py`:

```python
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

    Used to decide whether ``ServiceDep`` entries can be satisfied. Cached
    for the process lifetime — module presence does not change at runtime.
    """
    return importlib.util.find_spec("nexus.server") is not None


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
        server_available: Override for ``_server_available()``; tests use
            this to exercise slim vs. full paths without touching the real
            module state.

    Returns:
        List of ``(dep, reason_string)`` tuples — empty list when all deps
        are satisfied.
    """
    if server_available is None:
        server_available = _server_available()
    missing: list[tuple[RuntimeDep, str]] = []
    for dep in deps:
        match dep:
            case PythonDep(module=mod, extras=extras):
                if importlib.util.find_spec(mod) is None:
                    if extras:
                        hint = f"pip install nexus-fs[{','.join(extras)}]"
                    else:
                        hint = f"pip install {mod}"
                    missing.append((dep, f"python '{mod}': install with: {hint}"))
            case BinaryDep(name=name, install_hint=hint):
                if shutil.which(name) is None:
                    missing.append(
                        (dep, f"binary '{name}': not on PATH — install with: {hint}")
                    )
            case ServiceDep(name=name):
                if not server_available:
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
```

- [ ] **Step 4: Run tests to verify they pass**

```
pytest tests/unit/backends/test_runtime_deps.py::TestDepTypes -v
```

Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/backends/base/runtime_deps.py tests/unit/backends/test_runtime_deps.py
git commit -m "feat(backends): add typed RuntimeDep schema (issue #3830)"
```

---

## Task 2: check_runtime_deps helper tests

**Files:**
- Test: `tests/unit/backends/test_runtime_deps.py` (extend)

- [ ] **Step 1: Write failing tests for the checker**

Append to `tests/unit/backends/test_runtime_deps.py`:

```python
from unittest.mock import patch

from nexus.backends.base.runtime_deps import check_runtime_deps


class TestCheckRuntimeDeps:
    def test_empty_deps_returns_empty(self) -> None:
        assert check_runtime_deps(()) == []

    def test_satisfied_python_dep(self) -> None:
        # 'json' is always present in stdlib.
        assert check_runtime_deps((PythonDep("json"),)) == []

    def test_missing_python_dep_without_extras(self) -> None:
        missing = check_runtime_deps((PythonDep("definitely_not_a_real_module_xyz"),))
        assert len(missing) == 1
        dep, reason = missing[0]
        assert isinstance(dep, PythonDep)
        assert "pip install definitely_not_a_real_module_xyz" in reason

    def test_missing_python_dep_with_extras(self) -> None:
        missing = check_runtime_deps(
            (PythonDep("definitely_not_a_real_module_xyz", extras=("gcs", "gdrive")),)
        )
        assert len(missing) == 1
        _, reason = missing[0]
        assert "pip install nexus-fs[gcs,gdrive]" in reason

    def test_satisfied_binary_dep(self) -> None:
        # 'sh' is on PATH on every POSIX system + in CI images.
        assert check_runtime_deps((BinaryDep("sh", "n/a"),)) == []

    def test_missing_binary_dep(self) -> None:
        missing = check_runtime_deps(
            (BinaryDep("definitely_not_a_real_binary_xyz", "brew install xyz"),)
        )
        assert len(missing) == 1
        _, reason = missing[0]
        assert "not on PATH" in reason
        assert "brew install xyz" in reason

    def test_service_dep_satisfied_when_server_available(self) -> None:
        missing = check_runtime_deps(
            (ServiceDep("token_manager"),), server_available=True
        )
        assert missing == []

    def test_service_dep_missing_when_slim(self) -> None:
        missing = check_runtime_deps(
            (ServiceDep("token_manager"),), server_available=False
        )
        assert len(missing) == 1
        _, reason = missing[0]
        assert "service 'token_manager'" in reason
        assert "full nexus install" in reason

    def test_aggregates_all_missing(self) -> None:
        deps: tuple[RuntimeDep, ...] = (
            PythonDep("definitely_not_a_real_module_xyz", extras=("gws",)),
            BinaryDep("definitely_not_a_real_binary_xyz", "brew install xyz"),
            ServiceDep("kernel"),
            PythonDep("json"),  # satisfied — should not appear in output
        )
        missing = check_runtime_deps(deps, server_available=False)
        assert len(missing) == 3
        reasons = [r for _, r in missing]
        assert any("definitely_not_a_real_module_xyz" in r for r in reasons)
        assert any("definitely_not_a_real_binary_xyz" in r for r in reasons)
        assert any("service 'kernel'" in r for r in reasons)

    def test_server_available_is_cached(self) -> None:
        from nexus.backends.base.runtime_deps import _server_available

        _server_available.cache_clear()
        with patch(
            "nexus.backends.base.runtime_deps.importlib.util.find_spec"
        ) as mock_find:
            mock_find.return_value = object()
            _server_available()
            _server_available()
            assert mock_find.call_count == 1
        _server_available.cache_clear()
```

- [ ] **Step 2: Run tests to verify they pass**

```
pytest tests/unit/backends/test_runtime_deps.py -v
```

Expected: all tests pass (6 from Task 1 + 10 new = 16).

- [ ] **Step 3: Commit**

```bash
git add tests/unit/backends/test_runtime_deps.py
git commit -m "test(backends): cover check_runtime_deps behavior (issue #3830)"
```

---

## Task 3: MissingDependencyError exception

**Files:**
- Modify: `src/nexus/contracts/exceptions.py`
- Test: `tests/unit/backends/test_runtime_deps.py` (extend)

- [ ] **Step 1: Write failing test for the exception**

Append to `tests/unit/backends/test_runtime_deps.py`:

```python
from nexus.contracts.exceptions import BackendError, MissingDependencyError


class TestMissingDependencyError:
    def test_is_backend_error(self) -> None:
        err = MissingDependencyError(backend="gws_gmail", missing=[])
        assert isinstance(err, BackendError)

    def test_enumerates_all_missing(self) -> None:
        missing = [
            (PythonDep("x", extras=("gws",)), "python 'x': install with: pip install nexus-fs[gws]"),
            (BinaryDep("gws", "brew install gws"), "binary 'gws': not on PATH — install with: brew install gws"),
        ]
        err = MissingDependencyError(backend="gws_gmail", missing=missing)
        msg = str(err)
        assert "gws_gmail" in msg
        assert "2 runtime dep" in msg
        assert "python 'x'" in msg
        assert "binary 'gws'" in msg

    def test_missing_attribute_exposed(self) -> None:
        pairs = [(PythonDep("x"), "python 'x': install with: pip install x")]
        err = MissingDependencyError(backend="x", missing=pairs)
        assert err.missing == pairs
```

- [ ] **Step 2: Run to verify failure**

```
pytest tests/unit/backends/test_runtime_deps.py::TestMissingDependencyError -v
```

Expected: `ImportError: cannot import name 'MissingDependencyError'`.

- [ ] **Step 3: Add the exception class**

In `src/nexus/contracts/exceptions.py`, add after the `BackendError` class definition (around line 175, before `DatabaseError`):

```python
class MissingDependencyError(BackendError):
    """One or more runtime dependencies for a connector are missing.

    Raised by ``BackendFactory.create()`` when a connector's
    ``RUNTIME_DEPS`` cannot be satisfied in the current environment.  Each
    entry in ``missing`` is a ``(dep, human_reason)`` pair — the reason
    string already contains the install hint.

    This is an **expected** error: the user has an actionable path
    forward (install the hint, switch profiles, etc.), so it is logged at
    INFO level without stack traces.

    Attributes:
        backend: connector name that failed to mount
        missing: list of (RuntimeDep, reason) pairs for every unmet dep
    """

    is_expected = True  # User-correctable — install the dep

    def __init__(
        self,
        backend: str,
        missing: list[tuple[Any, str]],
    ) -> None:
        self.missing = missing
        count = len(missing)
        lines = [f"missing {count} runtime dep(s)"]
        for _, reason in missing:
            lines.append(f"  - {reason}")
        super().__init__("\n".join(lines), backend=backend)
```

- [ ] **Step 4: Verify tests pass**

```
pytest tests/unit/backends/test_runtime_deps.py::TestMissingDependencyError -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/contracts/exceptions.py tests/unit/backends/test_runtime_deps.py
git commit -m "feat(contracts): add MissingDependencyError for runtime-dep failures (issue #3830)"
```

---

## Task 4: Extend ConnectorInfo with runtime_deps + deprecated requires property

**Files:**
- Modify: `src/nexus/backends/base/registry.py:197-258` (ConnectorInfo dataclass)
- Test: `tests/unit/backends/test_registry.py` (extend)

- [ ] **Step 1: Write failing tests**

Append to `tests/unit/backends/test_registry.py`:

```python
from nexus.backends.base.runtime_deps import BinaryDep, PythonDep, RuntimeDep


class TestConnectorInfoRuntimeDeps:
    def test_default_empty_tuple(self) -> None:
        from nexus.backends.base.registry import ConnectorInfo

        info = ConnectorInfo(name="t", connector_class=object)  # type: ignore[arg-type]
        assert info.runtime_deps == ()

    def test_runtime_deps_stored(self) -> None:
        from nexus.backends.base.registry import ConnectorInfo

        deps: tuple[RuntimeDep, ...] = (
            PythonDep("boto3", extras=("s3",)),
            BinaryDep("gws", "brew install gws"),
        )
        info = ConnectorInfo(
            name="t", connector_class=object, runtime_deps=deps  # type: ignore[arg-type]
        )
        assert info.runtime_deps == deps

    def test_requires_property_derives_from_python_deps(self) -> None:
        from nexus.backends.base.registry import ConnectorInfo

        deps: tuple[RuntimeDep, ...] = (
            PythonDep("boto3", extras=("s3",)),
            PythonDep("httpx"),
            BinaryDep("gws", "brew install gws"),  # not included in requires
        )
        info = ConnectorInfo(
            name="t", connector_class=object, runtime_deps=deps  # type: ignore[arg-type]
        )
        assert info.requires == ["boto3", "httpx"]

    def test_requires_property_empty_when_no_python_deps(self) -> None:
        from nexus.backends.base.registry import ConnectorInfo

        info = ConnectorInfo(
            name="t",
            connector_class=object,  # type: ignore[arg-type]
            runtime_deps=(BinaryDep("gws", "brew install gws"),),
        )
        assert info.requires == []
```

- [ ] **Step 2: Run to verify failure**

```
pytest tests/unit/backends/test_registry.py::TestConnectorInfoRuntimeDeps -v
```

Expected: `TypeError: __init__() got an unexpected keyword argument 'runtime_deps'`.

- [ ] **Step 3: Extend ConnectorInfo**

In `src/nexus/backends/base/registry.py`:

Add import near the top (after existing imports, before `TYPE_CHECKING` block):

```python
from nexus.backends.base.runtime_deps import PythonDep, RuntimeDep
```

Remove `requires: list[str] = field(default_factory=list)` from the `ConnectorInfo` dataclass (line 213–214) and replace with:

```python
    runtime_deps: tuple[RuntimeDep, ...] = ()
    """Typed runtime dependencies (Issue #3830).

    Populated at registration from either ``@register_connector(runtime_deps=...)``
    or the class attribute ``RUNTIME_DEPS``. Checked by
    :meth:`nexus.backends.base.factory.BackendFactory.create` before
    instantiation.
    """
```

Add the deprecated property inside `ConnectorInfo` (place it right after the existing `connection_args` property around line 236, before `get_required_args`):

```python
    @property
    def requires(self) -> list[str]:
        """Deprecated — derived from ``runtime_deps``.

        Returns the module names of every :class:`PythonDep`.  New code
        should read ``runtime_deps`` directly; this property exists so that
        current callers (``cli/commands/connectors.py``,
        ``server/api/v2/routers/connectors.py``, tests) keep working for
        one release.  Removal is tracked as follow-up A.2 of Issue #3830.
        """
        return [d.module for d in self.runtime_deps if isinstance(d, PythonDep)]
```

- [ ] **Step 4: Verify new tests pass**

```
pytest tests/unit/backends/test_registry.py::TestConnectorInfoRuntimeDeps -v
```

Expected: 4 passed.

- [ ] **Step 5: Run the full registry test module to catch regressions**

```
pytest tests/unit/backends/test_registry.py -v
```

Expected: all tests pass. If pre-existing tests read `info.requires` as a list, they should still work (derived from empty `runtime_deps`).

- [ ] **Step 6: Commit**

```bash
git add src/nexus/backends/base/registry.py tests/unit/backends/test_registry.py
git commit -m "feat(backends): add runtime_deps field to ConnectorInfo; derive legacy requires (issue #3830)"
```

---

## Task 5: Extend ConnectorRegistry.register() + register_connector decorator

**Files:**
- Modify: `src/nexus/backends/base/registry.py:321-391` (`ConnectorRegistry.register`), `535-582` (`register_connector` decorator)
- Test: `tests/unit/backends/test_registry.py` (extend)

- [ ] **Step 1: Write failing tests**

Append to `tests/unit/backends/test_registry.py`:

```python
import warnings


class TestRegisterRuntimeDeps:
    def setup_method(self) -> None:
        from nexus.backends.base.registry import ConnectorRegistry

        ConnectorRegistry.clear()

    def teardown_method(self) -> None:
        from nexus.backends.base.registry import ConnectorRegistry

        ConnectorRegistry.clear()

    def test_decorator_kwarg_populates_runtime_deps(self) -> None:
        from nexus.backends.base.registry import (
            ConnectorRegistry,
            register_connector,
        )

        @register_connector(
            "t_deco",
            runtime_deps=(PythonDep("boto3", extras=("s3",)),),
        )
        class T:  # minimal stub class — satisfies protocol via extra attrs
            name = "t_deco"
            write_content = read_content = delete_content = content_exists = None
            get_content_size = mkdir = rmdir = is_directory = None
            check_connection = lambda self: None  # noqa: E731
            user_scoped = False
            is_connected = True
            has_root_path = False
            has_token_manager = False
            backend_features: frozenset = frozenset()
            has_feature = lambda self, f: False  # noqa: E731

        info = ConnectorRegistry.get_info("t_deco")
        assert info.runtime_deps == (PythonDep("boto3", extras=("s3",)),)

    def test_class_attr_populates_runtime_deps_when_no_decorator_arg(self) -> None:
        from nexus.backends.base.registry import (
            ConnectorRegistry,
            register_connector,
        )

        @register_connector("t_attr")
        class T:
            RUNTIME_DEPS = (BinaryDep("gws", "brew install gws"),)
            name = "t_attr"
            write_content = read_content = delete_content = content_exists = None
            get_content_size = mkdir = rmdir = is_directory = None
            check_connection = lambda self: None  # noqa: E731
            user_scoped = False
            is_connected = True
            has_root_path = False
            has_token_manager = False
            backend_features: frozenset = frozenset()
            has_feature = lambda self, f: False  # noqa: E731

        info = ConnectorRegistry.get_info("t_attr")
        assert info.runtime_deps == (BinaryDep("gws", "brew install gws"),)

    def test_decorator_arg_wins_when_both_present(self) -> None:
        from nexus.backends.base.registry import (
            ConnectorRegistry,
            register_connector,
        )

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")

            @register_connector("t_both", runtime_deps=(PythonDep("httpx"),))
            class T:
                RUNTIME_DEPS = (BinaryDep("gws", "brew install gws"),)
                name = "t_both"
                write_content = read_content = delete_content = content_exists = None
                get_content_size = mkdir = rmdir = is_directory = None
                check_connection = lambda self: None  # noqa: E731
                user_scoped = False
                is_connected = True
                has_root_path = False
                has_token_manager = False
                backend_features: frozenset = frozenset()
                has_feature = lambda self, f: False  # noqa: E731

            assert any(
                issubclass(w.category, UserWarning) and "runtime_deps" in str(w.message)
                for w in caught
            )

        info = ConnectorRegistry.get_info("t_both")
        assert info.runtime_deps == (PythonDep("httpx"),)

    def test_bad_runtime_dep_type_raises(self) -> None:
        from nexus.backends.base.registry import ConnectorRegistry

        with pytest.raises(ValueError, match="RUNTIME_DEPS"):
            ConnectorRegistry.register(
                name="t_bad",
                connector_class=type(
                    "T",
                    (),
                    {
                        "name": "t_bad",
                        "write_content": None,
                        "read_content": None,
                        "delete_content": None,
                        "content_exists": None,
                        "get_content_size": None,
                        "mkdir": None,
                        "rmdir": None,
                        "is_directory": None,
                        "check_connection": lambda self: None,
                        "user_scoped": False,
                        "is_connected": True,
                        "has_root_path": False,
                        "has_token_manager": False,
                        "backend_features": frozenset(),
                        "has_feature": lambda self, f: False,
                    },
                ),
                runtime_deps=("not-a-dep-instance",),  # type: ignore[arg-type]
            )

    def test_legacy_requires_kwarg_emits_deprecation(self) -> None:
        from nexus.backends.base.registry import (
            ConnectorRegistry,
            register_connector,
        )

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")

            @register_connector("t_legacy", requires=["httpx"])
            class T:
                name = "t_legacy"
                write_content = read_content = delete_content = content_exists = None
                get_content_size = mkdir = rmdir = is_directory = None
                check_connection = lambda self: None  # noqa: E731
                user_scoped = False
                is_connected = True
                has_root_path = False
                has_token_manager = False
                backend_features: frozenset = frozenset()
                has_feature = lambda self, f: False  # noqa: E731

            assert any(
                issubclass(w.category, DeprecationWarning) for w in caught
            )
```

- [ ] **Step 2: Run to verify failure**

```
pytest tests/unit/backends/test_registry.py::TestRegisterRuntimeDeps -v
```

Expected: `TypeError` on `runtime_deps=` kwarg.

- [ ] **Step 3: Update `ConnectorRegistry.register()`**

Replace the `register` classmethod signature (line 321–391) with:

```python
    @classmethod
    def register(
        cls,
        name: str,
        connector_class: "type[Backend]",
        description: str = "",
        category: str = "storage",
        requires: list[str] | None = None,
        service_name: str | None = None,
        runtime_deps: "tuple[RuntimeDep, ...] | None" = None,
    ) -> None:
        """Register a connector class.

        Args:
            name: Unique identifier for the connector
            connector_class: The connector class to register
            description: Human-readable description
            category: Category for grouping
            requires: **Deprecated** — list of pip-package names. Prefer
                ``runtime_deps`` with :class:`PythonDep` entries.
            service_name: Unified service name for service_map integration
            runtime_deps: Typed runtime dependencies (Issue #3830). Takes
                precedence over the class attribute ``RUNTIME_DEPS``.

        Raises:
            ValueError: If a connector with the same name is already
                registered, if ``runtime_deps`` contains non-RuntimeDep
                entries, or if the connector class does not satisfy
                ConnectorProtocol.
        """
        import warnings

        if requires is not None:
            warnings.warn(
                "register_connector(requires=...) is deprecated; "
                "use runtime_deps=(PythonDep(...), ...) instead.",
                DeprecationWarning,
                stacklevel=3,
            )

        # Validate ConnectorProtocol conformance (Issue #1703).
        missing = [m for m in _CONNECTOR_PROTOCOL_MEMBERS if not hasattr(connector_class, m)]
        if missing:
            raise ValueError(
                f"Connector '{name}' ({connector_class.__name__}) does not satisfy "
                f"ConnectorProtocol. Missing members: {', '.join(sorted(missing))}"
            )

        existing = cls._base.get(name)
        if existing is not None:
            if existing.connector_class is not connector_class:
                raise ValueError(
                    f"Connector '{name}' is already registered to "
                    f"{existing.connector_class.__name__}. "
                    f"Cannot register {connector_class.__name__}."
                )
            return

        user_scoped = getattr(connector_class, "user_scoped", False)
        if isinstance(user_scoped, property):
            user_scoped = False

        config_mapping = derive_config_mapping(connector_class)

        backend_features: frozenset[BackendFeature] = getattr(
            connector_class, "_BACKEND_FEATURES", frozenset()
        )

        # Resolve runtime_deps: decorator arg wins, else class attr, else ().
        class_attr_deps = getattr(connector_class, "RUNTIME_DEPS", None)
        resolved_deps: tuple[RuntimeDep, ...]
        if runtime_deps is not None:
            if class_attr_deps is not None and tuple(class_attr_deps) != tuple(runtime_deps):
                warnings.warn(
                    f"Connector '{name}': runtime_deps= decorator arg overrides "
                    f"class attribute RUNTIME_DEPS.",
                    UserWarning,
                    stacklevel=3,
                )
            resolved_deps = tuple(runtime_deps)
        elif class_attr_deps is not None:
            resolved_deps = tuple(class_attr_deps)
        else:
            resolved_deps = ()

        bad = [d for d in resolved_deps if not isinstance(d, (PythonDep, BinaryDep, ServiceDep))]
        if bad:
            raise ValueError(
                f"Connector '{name}': RUNTIME_DEPS entries must be PythonDep / "
                f"BinaryDep / ServiceDep, got: {bad!r}"
            )

        info = ConnectorInfo(
            name=name,
            connector_class=connector_class,
            description=description,
            category=category,
            user_scoped=user_scoped,
            config_mapping=config_mapping,
            service_name=service_name,
            backend_features=backend_features,
            runtime_deps=resolved_deps,
        )
        cls._base.register(name, info, allow_overwrite=True)
```

Also add the import for `BinaryDep` and `ServiceDep` at the top of `registry.py`:

Replace the import line added in Task 4 (`from nexus.backends.base.runtime_deps import PythonDep, RuntimeDep`) with:

```python
from nexus.backends.base.runtime_deps import (
    BinaryDep,
    PythonDep,
    RuntimeDep,
    ServiceDep,
)
```

- [ ] **Step 4: Update `register_connector` decorator**

Replace the decorator (line 535–582) with:

```python
def register_connector(
    name: str,
    description: str = "",
    category: str = "storage",
    requires: list[str] | None = None,
    service_name: str | None = None,
    runtime_deps: "tuple[RuntimeDep, ...] | None" = None,
) -> "Callable[[type[Backend]], type[Backend]]":
    """Decorator to register a connector class.

    Args:
        name: Unique identifier for the connector
        description: Human-readable description
        category: Category for grouping
        requires: **Deprecated** — use ``runtime_deps=`` instead.
        service_name: Unified service name for service_map integration
        runtime_deps: Typed runtime deps (Issue #3830). Takes precedence
            over the class attribute ``RUNTIME_DEPS``.

    Example::

        @register_connector(
            "path_s3",
            runtime_deps=(PythonDep("boto3", extras=("s3",)),),
        )
        class PathS3Backend(PathAddressingEngine):
            ...
    """

    def decorator(cls: "type[Backend]") -> "type[Backend]":
        ConnectorRegistry.register(
            name=name,
            connector_class=cls,
            description=description,
            category=category,
            requires=requires,
            service_name=service_name,
            runtime_deps=runtime_deps,
        )
        return cls

    return decorator
```

- [ ] **Step 5: Verify tests pass**

```
pytest tests/unit/backends/test_registry.py -v
```

Expected: all tests pass, including the 5 new ones and all pre-existing registry tests.

- [ ] **Step 6: Commit**

```bash
git add src/nexus/backends/base/registry.py tests/unit/backends/test_registry.py
git commit -m "feat(backends): wire runtime_deps through ConnectorRegistry.register (issue #3830)"
```

---

## Task 6: Wire check into BackendFactory.create()

**Files:**
- Modify: `src/nexus/backends/base/factory.py:44-99` (`create` method)
- Test: `tests/integration/backends/test_factory_dep_check.py` (new)

- [ ] **Step 1: Write failing integration test**

Create `tests/integration/backends/test_factory_dep_check.py`:

```python
"""Integration test: BackendFactory.create() raises MissingDependencyError.

Covers Issue #3830 — typed runtime-dep check at mount time.
"""

from __future__ import annotations

import pytest

from nexus.backends.base.factory import BackendFactory
from nexus.backends.base.registry import ConnectorRegistry, register_connector
from nexus.backends.base.runtime_deps import BinaryDep, PythonDep, ServiceDep
from nexus.contracts.exceptions import MissingDependencyError


class _StubBackend:
    """Minimal ConnectorProtocol-compliant stub."""

    name = "stub"
    write_content = read_content = delete_content = content_exists = None
    get_content_size = mkdir = rmdir = is_directory = None
    user_scoped = False
    is_connected = True
    has_root_path = False
    has_token_manager = False
    backend_features: frozenset = frozenset()

    def check_connection(self):  # noqa: ANN201
        return None

    def has_feature(self, f):  # noqa: ANN001, ANN201
        return False

    def __init__(self, **kwargs):  # noqa: ANN003, ANN204
        pass


@pytest.fixture(autouse=True)
def _clean_registry():
    # Snapshot + restore so we don't affect other tests in the integration run.
    names_before = set(ConnectorRegistry.list_available())
    yield
    for nm in set(ConnectorRegistry.list_available()) - names_before:
        ConnectorRegistry._base._items.pop(nm, None)  # type: ignore[attr-defined]


class TestFactoryDepCheck:
    def test_satisfied_deps_allow_instantiation(self) -> None:
        @register_connector(
            "stub_ok",
            runtime_deps=(PythonDep("json"),),  # stdlib — always present
        )
        class _OK(_StubBackend):
            pass

        instance = BackendFactory.create("stub_ok", {})
        assert isinstance(instance, _OK)

    def test_missing_python_dep_raises(self) -> None:
        @register_connector(
            "stub_missing_py",
            runtime_deps=(PythonDep("definitely_not_a_real_module_xyz", extras=("gcs",)),),
        )
        class _M(_StubBackend):
            pass

        with pytest.raises(MissingDependencyError) as exc_info:
            BackendFactory.create("stub_missing_py", {})
        err = exc_info.value
        assert err.backend == "stub_missing_py"
        assert len(err.missing) == 1
        assert "pip install nexus-fs[gcs]" in str(err)

    def test_missing_binary_dep_raises(self) -> None:
        @register_connector(
            "stub_missing_bin",
            runtime_deps=(BinaryDep("definitely_not_a_real_binary_xyz", "brew install xyz"),),
        )
        class _M(_StubBackend):
            pass

        with pytest.raises(MissingDependencyError) as exc_info:
            BackendFactory.create("stub_missing_bin", {})
        assert "brew install xyz" in str(exc_info.value)

    def test_all_missing_enumerated_together(self) -> None:
        @register_connector(
            "stub_many_missing",
            runtime_deps=(
                PythonDep("definitely_not_a_real_module_xyz", extras=("gws",)),
                BinaryDep("definitely_not_a_real_binary_xyz", "brew install xyz"),
            ),
        )
        class _M(_StubBackend):
            pass

        with pytest.raises(MissingDependencyError) as exc_info:
            BackendFactory.create("stub_many_missing", {})
        err = exc_info.value
        assert len(err.missing) == 2
        msg = str(err)
        assert "definitely_not_a_real_module_xyz" in msg
        assert "definitely_not_a_real_binary_xyz" in msg
```

- [ ] **Step 2: Run to verify failure**

```
pytest tests/integration/backends/test_factory_dep_check.py -v
```

Expected: tests fail — factory does not call the check yet, so `test_missing_*` tests all fail on "expected MissingDependencyError, got ...".

- [ ] **Step 3: Wire check into factory**

In `src/nexus/backends/base/factory.py`, update `BackendFactory.create`:

Replace lines 64–75 (from `from nexus.backends.base.registry import` through the `connector_cls = info.connector_class` line) with:

```python
        from nexus.backends.base.registry import (
            ConnectorRegistry,
            _ensure_optional_backends_registered,
        )
        from nexus.backends.base.runtime_deps import check_runtime_deps
        from nexus.contracts.exceptions import MissingDependencyError

        _ensure_optional_backends_registered()

        try:
            info = ConnectorRegistry.get_info(backend_type)
        except KeyError:
            raise RuntimeError(f"Unsupported backend type: {backend_type}") from None

        missing = check_runtime_deps(info.runtime_deps)
        if missing:
            raise MissingDependencyError(backend=backend_type, missing=missing)

        connector_cls = info.connector_class
```

- [ ] **Step 4: Verify tests pass**

```
pytest tests/integration/backends/test_factory_dep_check.py -v
```

Expected: 4 passed.

- [ ] **Step 5: Run wider factory/registry test suite to catch regressions**

```
pytest tests/unit/backends/ tests/integration/backends/ -v
```

Expected: all pass. If anything breaks, it's likely a test that already exercises the factory with a connector whose class attribute `RUNTIME_DEPS` happens to declare a dep that's not installed in CI — unlikely before Task 7+ migrations but check.

- [ ] **Step 6: Commit**

```bash
git add src/nexus/backends/base/factory.py tests/integration/backends/test_factory_dep_check.py
git commit -m "feat(backends): check runtime deps in BackendFactory.create (issue #3830)"
```

---

## Task 7: Migrate storage connectors (6)

**Files:**
- Modify: `src/nexus/backends/storage/cas_local.py`, `path_local.py`, `local_connector.py`, `path_gcs.py`, `cas_gcs.py`, `path_s3.py`

**Migration summary** — each change is: remove `requires=[...]` kwarg, add `runtime_deps=(...)` kwarg with the typed equivalent. For connectors with no external deps, simply remove `requires=` (if present).

- [ ] **Step 1: Migrate `path_gcs.py`**

Current (lines 38–44):

```python
@register_connector(
    "path_gcs",
    description="Google Cloud Storage with direct path mapping",
    category="storage",
    requires=["google-cloud-storage"],
    service_name="gcs",
)
class PathGCSBackend(PathAddressingEngine):
```

Replace with:

```python
@register_connector(
    "path_gcs",
    description="Google Cloud Storage with direct path mapping",
    category="storage",
    runtime_deps=(PythonDep("google.cloud.storage", extras=("gcs",)),),
    service_name="gcs",
)
class PathGCSBackend(PathAddressingEngine):
```

Add import (near top of file, grouped with other nexus.backends imports):

```python
from nexus.backends.base.runtime_deps import PythonDep
```

- [ ] **Step 2: Migrate `cas_gcs.py`**

Same change as Task 7.1 but for the `cas_gcs` registration (line 39). Replace `requires=["google-cloud-storage"]` with `runtime_deps=(PythonDep("google.cloud.storage", extras=("gcs",)),)`. Add the `PythonDep` import.

- [ ] **Step 3: Migrate `path_s3.py`**

Replace `requires=["boto3"]` (line 37) with `runtime_deps=(PythonDep("boto3", extras=("s3",)),)`. Add the `PythonDep` import.

- [ ] **Step 4: Migrate `cas_local.py`, `path_local.py`, `local_connector.py`**

Open each file. These connectors have no external deps — they likely don't have a `requires=` kwarg, but double-check. If they do, remove it. Otherwise no change. Skip this step if all three already have no `requires=` kwarg.

Run:

```
grep -nH 'requires=' src/nexus/backends/storage/cas_local.py src/nexus/backends/storage/path_local.py src/nexus/backends/storage/local_connector.py
```

If any line matches, delete it.

- [ ] **Step 5: Run tests**

```
pytest tests/unit/backends/ tests/integration/backends/ -v
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/nexus/backends/storage/
git commit -m "refactor(backends/storage): migrate storage connectors to typed runtime_deps (issue #3830)"
```

---

## Task 8: Migrate API connectors (gdrive, gmail, calendar, x, slack, hn)

**Files:**
- Modify: `src/nexus/backends/connectors/gdrive/connector.py:61-67`, `gmail/connector.py:65-71`, `calendar/connector.py:55-61` and `calendar/connector.py:601-605`, `x/connector.py:58-64`, `slack/connector.py:59-65`, `hn/connector.py:42-48`

Each of these currently has a `requires=[...]` kwarg. Replace with typed `runtime_deps=(...)` and add the `PythonDep` import.

**Before running these steps, verify exact import module names for each connector** by grepping the connector source for `from google.` / `from googleapiclient.` / `import slack_sdk` / etc. The extras names chosen below match the proposed slim pyproject extras (sub-project C fills them in).

- [ ] **Step 1: Migrate `gdrive/connector.py`**

Replace the decorator call around lines 61–67:

```python
@register_connector(
    "path_gdrive",
    description="Google Drive with OAuth user-scoped auth",
    category="api",
    requires=["google-api-python-client", "google-auth-oauthlib"],
    service_name="google-drive",
)
```

with:

```python
@register_connector(
    "path_gdrive",
    description="Google Drive with OAuth user-scoped auth",
    category="api",
    runtime_deps=(
        PythonDep("googleapiclient", extras=("gdrive",)),
        PythonDep("google_auth_oauthlib", extras=("gdrive",)),
    ),
    service_name="google-drive",
)
```

(Keep the existing `description` / `category` / `service_name` values; the example above shows the structure — copy the originals from the file.)

Add `from nexus.backends.base.runtime_deps import PythonDep`.

- [ ] **Step 2: Migrate `gmail/connector.py`**

Replace `requires=["google-api-python-client", "google-auth-oauthlib"]` (around line 69) with:

```python
    runtime_deps=(
        PythonDep("googleapiclient", extras=("gmail",)),
        PythonDep("google_auth_oauthlib", extras=("gmail",)),
    ),
```

Add `PythonDep` import.

- [ ] **Step 3: Migrate `calendar/connector.py` (two registrations)**

There are two `@register_connector` calls in this file (lines 55 and 601). Update both. Replace each `requires=["google-api-python-client", "google-auth-oauthlib"]` with:

```python
    runtime_deps=(
        PythonDep("googleapiclient", extras=("gcalendar",)),
        PythonDep("google_auth_oauthlib", extras=("gcalendar",)),
    ),
```

Add `PythonDep` import.

- [ ] **Step 4: Migrate `x/connector.py`**

Replace `requires=["requests-oauthlib"]` (line 62) with:

```python
    runtime_deps=(PythonDep("requests_oauthlib", extras=("x",)),),
```

Add `PythonDep` import.

- [ ] **Step 5: Migrate `slack/connector.py`**

Replace `requires=["slack-sdk"]` (line 63) with:

```python
    runtime_deps=(PythonDep("slack_sdk", extras=("slack",)),),
```

Add `PythonDep` import.

- [ ] **Step 6: Migrate `hn/connector.py`**

Replace `requires=["httpx"]` (line 46) with:

```python
    runtime_deps=(),
```

(httpx is a core nexus-fs dep, always present — no RuntimeDep entry needed. Empty tuple is fine and documents the intent.)

The `PythonDep` import is not needed if `runtime_deps=()` is the only change — skip adding the import.

- [ ] **Step 7: Run tests**

```
pytest tests/unit/backends/ tests/integration/backends/ tests/e2e/test_connector_e2e.py -v
```

Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add src/nexus/backends/connectors/gdrive/ src/nexus/backends/connectors/gmail/ src/nexus/backends/connectors/calendar/ src/nexus/backends/connectors/x/ src/nexus/backends/connectors/slack/ src/nexus/backends/connectors/hn/
git commit -m "refactor(backends/connectors): migrate API connectors to typed runtime_deps (issue #3830)"
```

---

## Task 9: Migrate compute connectors (anthropic_native, openai_compatible)

**Files:**
- Modify: `src/nexus/backends/compute/anthropic_native.py:65-72`, `openai_compatible.py:70-76`

- [ ] **Step 1: Migrate `anthropic_native.py`**

Replace `requires=["anthropic"]` (line 69) with:

```python
    runtime_deps=(PythonDep("anthropic", extras=("anthropic",)),),
```

Add `from nexus.backends.base.runtime_deps import PythonDep`.

- [ ] **Step 2: Migrate `openai_compatible.py`**

Replace `requires=["openai"]` (line 74) with:

```python
    runtime_deps=(PythonDep("openai", extras=("openai",)),),
```

Add `PythonDep` import.

- [ ] **Step 3: Run tests**

```
pytest tests/unit/backends/ tests/integration/backends/ -v
```

Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add src/nexus/backends/compute/
git commit -m "refactor(backends/compute): migrate compute connectors to typed runtime_deps (issue #3830)"
```

---

## Task 10: Migrate CLI connectors (gws × 6, github × 2)

**Files:**
- Modify: `src/nexus/backends/connectors/gws/connector.py` (6 `@register_connector` calls at lines 85, 177, 368, 461, 584, 1300), `github/connector.py:38-56` (two stacked decorators).

All of these back onto `PathCLIBackend` with a binary + a token-manager service dep. Use a shared constant to avoid repetition.

- [ ] **Step 1: Migrate `gws/connector.py` — define shared deps, update all six registrations**

Near the top of `src/nexus/backends/connectors/gws/connector.py` (after existing imports from `nexus.backends`), add:

```python
from nexus.backends.base.runtime_deps import BinaryDep, ServiceDep

_GWS_RUNTIME_DEPS = (
    BinaryDep("gws", "brew install nexi-lab/tap/gws"),
    ServiceDep("token_manager"),
)
```

For each of the six `@register_connector(...)` calls in this file (lines 85, 177, 368, 461, 584, 1300 — names `gws_gmail`, `gws_calendar`, `gws_sheets`, `gws_docs`, `gws_chat`, `gws_drive`), replace the decorator signature to add `runtime_deps=_GWS_RUNTIME_DEPS,` and (if present) remove any `requires=[...]` kwarg.

Example — the `gws_sheets` decorator at line 85:

```python
@register_connector(
    "gws_sheets",
    description="Google Sheets via gws CLI",
    category="cli",
    runtime_deps=_GWS_RUNTIME_DEPS,
    service_name="gws",
)
```

Apply the same change to all six. Exact line/block varies — locate each via `grep -n "@register_connector" src/nexus/backends/connectors/gws/connector.py` and edit.

- [ ] **Step 2: Migrate `github/connector.py` — two stacked decorators**

Add imports:

```python
from nexus.backends.base.runtime_deps import BinaryDep, ServiceDep
```

And a shared constant right above the two decorators at line 38:

```python
_GH_RUNTIME_DEPS = (
    BinaryDep("gh", "brew install gh"),
    ServiceDep("token_manager"),
)
```

Update both stacked decorators (lines 38 and 48) to add `runtime_deps=_GH_RUNTIME_DEPS,`:

```python
@register_connector(
    "github_connector",
    description="GitHub via gh CLI",
    category="cli",
    runtime_deps=_GH_RUNTIME_DEPS,
)
@register_connector(
    "gws_github",
    description="GitHub via gh CLI (deprecated alias, use github_connector)",
    category="cli",
    runtime_deps=_GH_RUNTIME_DEPS,
)
class GitHubConnector(PathCLIBackend):
    ...
```

- [ ] **Step 3: Run tests**

```
pytest tests/unit/backends/ tests/integration/backends/ tests/integration/backends/connectors/cli/ -v
```

Expected: all pass.

Note: `nexus.server` is available in the full monorepo (this worktree), so `ServiceDep("token_manager")` is satisfied in tests. CI-slim coverage is sub-project D.

- [ ] **Step 4: Commit**

```bash
git add src/nexus/backends/connectors/gws/ src/nexus/backends/connectors/github/
git commit -m "refactor(backends/connectors): migrate CLI connectors to typed runtime_deps (issue #3830)"
```

---

## Task 11: Smoke-test CLI and factory paths end-to-end

**Files:**
- No new files — verification only.

- [ ] **Step 1: Verify `nexus connectors list` CLI still works**

The `requires` property derivation from `runtime_deps` is meant to preserve existing CLI output. Run:

```
nexus connectors list
```

Expected: table of connectors with non-empty `requires` columns for ones with `PythonDep` entries (matches pre-migration behavior). Binary/service deps are not shown — that's a future CLI update (sub-project D).

If the CLI isn't installed in the env, skip this step and instead run the unit test that exercises the list path:

```
pytest tests/unit/cli/test_connectors_command.py -v
```

- [ ] **Step 2: Verify `server/api/v2/routers/connectors.py` still reads `requires` without error**

```
pytest tests/unit/server/api/v2/routers/test_connectors_router.py -v
```

If no such test file, instead grep-verify the reader still works:

```
grep -n '\.requires' src/nexus/server/api/v2/routers/connectors.py
```

Then run any test under `tests/` that imports `server/api/v2/routers/connectors.py`:

```
pytest -k connectors_router -v
```

Expected: passes.

- [ ] **Step 3: Full test suite**

```
pytest tests/unit/backends/ tests/integration/backends/ -v
```

Expected: all pass, no new failures vs baseline.

- [ ] **Step 4: Type-check the two edited modules**

```
mypy src/nexus/backends/base/runtime_deps.py src/nexus/backends/base/registry.py src/nexus/backends/base/factory.py src/nexus/contracts/exceptions.py
```

Expected: no new errors. Ignore pre-existing noise in surrounding files.

- [ ] **Step 5: Commit any final fixes**

If steps 1–4 surfaced tweaks (e.g., a test needed updating to assert the new property behavior), fix them and commit:

```bash
git add <changed files>
git commit -m "fix(backends): address runtime_deps migration follow-ups (issue #3830)"
```

---

## Task 12: Final sanity + PR prep

- [ ] **Step 1: Verify no leftover `requires=[...]` kwargs**

```
grep -rn "requires=\[" src/nexus/backends/ src/nexus/backends/connectors/ src/nexus/backends/compute/
```

Expected: no matches (all migrated). If any remain, they're legitimately testing the deprecation warning — check and migrate if they are real connectors.

- [ ] **Step 2: Verify every connector has either runtime_deps or RUNTIME_DEPS or is known-depless**

```
grep -rn "@register_connector" src/nexus/backends/ | wc -l
```

Expected: count matches the inventory (~22 registrations). Manually spot-check a few that don't have `runtime_deps=` — they should be `cas_local`, `path_local`, `local_connector` with no external deps.

- [ ] **Step 3: Run full test suite one more time**

```
pytest tests/ -x --ignore=tests/e2e -q
```

Expected: all pass. E2E is ignored to keep the run quick; if time allows run e2e too.

- [ ] **Step 4: Review git log**

```
git log --oneline $(git merge-base HEAD develop)..HEAD
```

Expected: clean TDD-shaped history — test → impl → test → impl → migrations → verify. One commit per task or per logical change.

- [ ] **Step 5: Push branch and open PR**

```
git push -u origin <branch>
gh pr create --title "feat(backends): typed runtime-dep schema + mount-time check (#3830)" --body "$(cat <<'EOF'
## Summary
- Add typed ``RuntimeDep`` union (``PythonDep`` / ``BinaryDep`` / ``ServiceDep``) in ``nexus.backends.base.runtime_deps``.
- ``ConnectorInfo`` gains ``runtime_deps: tuple[RuntimeDep, ...]``; legacy ``requires`` becomes a derived property.
- ``BackendFactory.create()`` raises new ``MissingDependencyError`` enumerating every missing dep with an install hint.
- All 22 registered connectors migrated to typed deps.

Sub-project A-full of Issue #3830. Follow-ups tracked as A.2 (delete legacy ``requires``), A.3 (declarations manifest so primary-import-missing connectors register a placeholder), B (PathCLIBackend slim extract), C (pip extras expansion), D (CI matrix).

Spec: ``docs/superpowers/specs/2026-04-21-issue-3830-connector-runtime-deps-design.md``.

## Test plan
- [x] Unit tests: ``tests/unit/backends/test_runtime_deps.py`` (16 tests)
- [x] Registry tests: ``tests/unit/backends/test_registry.py`` (extended with 9 new tests)
- [x] Integration test: ``tests/integration/backends/test_factory_dep_check.py`` (4 tests)
- [x] Existing CLI / server routers still read ``info.requires`` without breakage (derived from ``runtime_deps``)
- [x] ``mypy`` clean on edited modules

Addresses #3830 (sub-project A only; A.2/A.3/B/C/D tracked separately).
EOF
)"
```

Expected: PR opens cleanly, CI passes.

---

## Self-review notes

Coverage check against spec:

- Spec §1 (RuntimeDep types) → Task 1
- Spec §2 (Declaration site + decorator arg + class attr) → Task 5
- Spec §3 (ConnectorInfo extension + deprecated `requires` property) → Task 4, Task 5
- Spec §4 (MissingDependencyError + check_runtime_deps + factory wiring) → Tasks 2, 3, 6
- Spec §5 (Per-connector migration — 22 sites) → Tasks 7–10
- Spec §6 (Backwards compat — derived property + deprecation warning) → Task 4, Task 5 (warning), Task 11 (verification)
- Spec §7 (Tests — 3 test files) → Tasks 1, 2, 3, 5, 6
- Spec rollout steps → all mapped to tasks

Known gaps (documented in spec Risks, deferred to A.3):
- Primary-Python-dep-missing connectors still produce "Unsupported backend type" because their modules fail to import at registration time. This plan does not attempt to fix that — sub-project A.3 will introduce a declarations manifest.

No placeholders, no "TODO" steps, no "similar to Task N" omissions.
