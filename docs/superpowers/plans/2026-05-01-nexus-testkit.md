# Nexus Testkit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a canonical `tests.testkit` package, migrate Nexus tests to use it, and keep old helper imports working.

**Architecture:** `tests/testkit` becomes the owner of reusable test helpers; old `tests/helpers/*` files and `tests.conftest.make_test_nexus` become compatibility wrappers. The implementation is split by helper family so each slice can be tested independently before broad import rewrites.

**Tech Stack:** Python 3.14, pytest, uv, ruff, existing Nexus test helpers, existing `nexus_runtime`/`nexus.factory` test boot path.

---

## File Structure

Create:

- `tests/testkit/__init__.py`: public exports for common helpers.
- `tests/testkit/backends.py`: `FailingBackend`.
- `tests/testkit/metadata.py`: `DictMetastore`, `FailingMetastore`, `MetastoreError`, `InMemoryNexusFS`.
- `tests/testkit/records.py`: `InMemoryRecordStore`.
- `tests/testkit/auth.py`: `TEST_CONTEXT`, `TEST_ADMIN_CONTEXT`, context factory helpers.
- `tests/testkit/nexus_factory.py`: `make_test_nexus`.
- `tests/testkit/profiles.py`: `TestProfile`, profile registry, pytest param helpers.
- `tests/testkit/containers.py`: lazy optional-service helpers.
- `tests/testkit/assertions.py`: repeated assertion helpers.
- `tests/testkit/edge_cases.py`: existing path/content edge-case data.
- `tests/testkit/websocket.py`: `MockWebSocket`.
- `tests/testkit/fixtures.py`: shared pytest fixtures for explicit import/re-export.
- `tests/unit/test_testkit_imports.py`: canonical export and compatibility tests.
- `tests/unit/test_testkit_profiles.py`: profile matrix behavior tests.
- `tests/unit/test_testkit_containers.py`: lazy import and skip behavior tests.
- `tests/unit/test_testkit_assertions.py`: assertion helper behavior tests.
- `docs/contributing/testing-testkit.md`: testkit usage docs.

Modify:

- `tests/conftest.py`: re-export `make_test_nexus` from `tests.testkit`.
- `tests/unit/conftest.py`: import `InMemoryRecordStore` from `tests.testkit`.
- `tests/helpers/*.py`: reduce helper files to compatibility wrappers.
- Internal test files importing `tests.helpers.*`, `helpers.mock_websocket`, or `tests.conftest.make_test_nexus`.
- One backend/connector suite to use the profile matrix, recommended target: `tests/unit/backends/test_runtime_deps.py`.

---

### Task 1: Add Canonical Testkit Import Surface

**Files:**
- Create: `tests/unit/test_testkit_imports.py`
- Create: `tests/testkit/__init__.py`
- Create: `tests/testkit/backends.py`
- Create: `tests/testkit/metadata.py`
- Create: `tests/testkit/records.py`
- Create: `tests/testkit/auth.py`
- Create: `tests/testkit/nexus_factory.py`
- Create: `tests/testkit/edge_cases.py`
- Create: `tests/testkit/websocket.py`
- Modify: `tests/helpers/dict_metastore.py`
- Modify: `tests/helpers/failing_backend.py`
- Modify: `tests/helpers/failing_metastore.py`
- Modify: `tests/helpers/in_memory_record_store.py`
- Modify: `tests/helpers/inmemory_nexus_fs.py`
- Modify: `tests/helpers/test_context.py`
- Modify: `tests/helpers/edge_cases.py`
- Modify: `tests/helpers/mock_websocket.py`
- Modify: `tests/conftest.py`

- [ ] **Step 1: Write the failing import test**

Create `tests/unit/test_testkit_imports.py`:

```python
"""Tests for the canonical tests.testkit package."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path


def test_common_helpers_exported_from_testkit() -> None:
    from tests.testkit import (
        DictMetastore,
        FailingBackend,
        FailingMetastore,
        InMemoryNexusFS,
        InMemoryRecordStore,
        MockWebSocket,
        TEST_ADMIN_CONTEXT,
        TEST_CONTEXT,
        make_test_nexus,
    )

    assert callable(DictMetastore)
    assert FailingBackend.__name__ == "FailingBackend"
    assert FailingMetastore.__name__ == "FailingMetastore"
    assert InMemoryNexusFS.__name__ == "InMemoryNexusFS"
    assert InMemoryRecordStore.__name__ == "InMemoryRecordStore"
    assert MockWebSocket.__name__ == "MockWebSocket"
    assert TEST_CONTEXT.user_id == "test"
    assert TEST_ADMIN_CONTEXT.is_admin is True
    assert callable(make_test_nexus)


def test_compatibility_imports_point_to_canonical_objects() -> None:
    from tests.conftest import make_test_nexus as compat_make_test_nexus
    from tests.helpers.dict_metastore import DictMetastore as CompatDictMetastore
    from tests.helpers.failing_backend import FailingBackend as CompatFailingBackend
    from tests.helpers.failing_metastore import FailingMetastore as CompatFailingMetastore
    from tests.helpers.in_memory_record_store import InMemoryRecordStore as CompatRecordStore
    from tests.helpers.inmemory_nexus_fs import InMemoryNexusFS as CompatNexusFS
    from tests.helpers.mock_websocket import MockWebSocket as CompatMockWebSocket
    from tests.helpers.test_context import TEST_CONTEXT as COMPAT_TEST_CONTEXT
    from tests.testkit import (
        DictMetastore,
        FailingBackend,
        FailingMetastore,
        InMemoryNexusFS,
        InMemoryRecordStore,
        MockWebSocket,
        TEST_CONTEXT,
        make_test_nexus,
    )

    assert CompatDictMetastore is DictMetastore
    assert CompatFailingBackend is FailingBackend
    assert CompatFailingMetastore is FailingMetastore
    assert CompatRecordStore is InMemoryRecordStore
    assert CompatNexusFS is InMemoryNexusFS
    assert CompatMockWebSocket is MockWebSocket
    assert COMPAT_TEST_CONTEXT is TEST_CONTEXT
    assert compat_make_test_nexus is make_test_nexus


def test_dict_metastore_factory_returns_usable_store(tmp_path: Path) -> None:
    from tests.testkit import DictMetastore

    store = DictMetastore(tmp_path / "metadata.redb")
    try:
        assert hasattr(store, "get")
        assert hasattr(store, "put")
        assert store.get("/missing") is None
    finally:
        store.close()


def test_make_test_nexus_export_has_stable_name() -> None:
    from tests.testkit import make_test_nexus

    assert isinstance(make_test_nexus, Callable)
    assert make_test_nexus.__name__ == "make_test_nexus"
```

- [ ] **Step 2: Run the import test to verify it fails**

Run:

```bash
uv run pytest tests/unit/test_testkit_imports.py -q -n0
```

Expected: FAIL with `ModuleNotFoundError: No module named 'tests.testkit'`.

- [ ] **Step 3: Create canonical modules by moving implementation ownership**

Create the directory:

```bash
mkdir -p tests/testkit
```

Move/copy the existing helper implementations into canonical modules:

```bash
cp tests/helpers/failing_backend.py tests/testkit/backends.py
cp tests/helpers/in_memory_record_store.py tests/testkit/records.py
cp tests/helpers/edge_cases.py tests/testkit/edge_cases.py
cp tests/helpers/mock_websocket.py tests/testkit/websocket.py
```

Create `tests/testkit/auth.py`:

```python
"""Shared auth and operation context helpers for tests."""

from __future__ import annotations

from collections.abc import Iterable

from nexus.contracts.types import OperationContext

TEST_CONTEXT = OperationContext(
    user_id="test",
    groups=[],
    is_admin=False,
)

TEST_ADMIN_CONTEXT = OperationContext(
    user_id="test-admin",
    groups=[],
    is_admin=True,
)


def operation_context(
    *,
    user_id: str = "test",
    groups: Iterable[str] = (),
    zone_id: str | None = None,
    is_system: bool = False,
    is_admin: bool = False,
) -> OperationContext:
    """Build an OperationContext for tests with explicit identity fields."""
    return OperationContext(
        user_id=user_id,
        groups=list(groups),
        zone_id=zone_id,
        is_system=is_system,
        is_admin=is_admin,
    )
```

Create `tests/testkit/metadata.py` by combining the existing logic from:

- `tests/helpers/dict_metastore.py`
- `tests/helpers/failing_metastore.py`
- `tests/helpers/inmemory_nexus_fs.py`

The final file must expose exactly these names:

```python
__all__ = [
    "DictMetastore",
    "FailingMetastore",
    "InMemoryNexusFS",
    "MetastoreError",
]
```

Use the current implementation bodies unchanged except for imports and module docstrings. The `DictMetastore` factory must still delegate to `nexus.storage.dict_metastore.DictMetastore`; `FailingMetastore` must keep `MetastoreError`; `InMemoryNexusFS` must keep the four `sys_*` methods currently present.

Create `tests/testkit/nexus_factory.py` by moving the current `make_test_nexus` implementation from `tests/conftest.py`. Keep the current `nexus_runtime.PyKernel` path and transport/federation wiring block. Change the context import inside the function to:

```python
from tests.testkit.auth import TEST_ADMIN_CONTEXT, TEST_CONTEXT
```

Create `tests/testkit/__init__.py`:

```python
"""Reusable Nexus testkit helpers."""

from tests.testkit.auth import TEST_ADMIN_CONTEXT, TEST_CONTEXT, operation_context
from tests.testkit.backends import FailingBackend
from tests.testkit.metadata import DictMetastore, FailingMetastore, InMemoryNexusFS, MetastoreError
from tests.testkit.nexus_factory import make_test_nexus
from tests.testkit.records import InMemoryRecordStore
from tests.testkit.websocket import MockWebSocket

__all__ = [
    "DictMetastore",
    "FailingBackend",
    "FailingMetastore",
    "InMemoryNexusFS",
    "InMemoryRecordStore",
    "MetastoreError",
    "MockWebSocket",
    "TEST_ADMIN_CONTEXT",
    "TEST_CONTEXT",
    "make_test_nexus",
    "operation_context",
]
```

- [ ] **Step 4: Replace old helper files with wrappers**

Replace `tests/helpers/dict_metastore.py`:

```python
"""Compatibility wrapper for tests.testkit.metadata."""

from tests.testkit.metadata import DictMetastore

__all__ = ["DictMetastore"]
```

Replace `tests/helpers/failing_backend.py`:

```python
"""Compatibility wrapper for tests.testkit.backends."""

from tests.testkit.backends import FailingBackend

__all__ = ["FailingBackend"]
```

Replace `tests/helpers/failing_metastore.py`:

```python
"""Compatibility wrapper for tests.testkit.metadata."""

from tests.testkit.metadata import FailingMetastore, MetastoreError

__all__ = ["FailingMetastore", "MetastoreError"]
```

Replace `tests/helpers/in_memory_record_store.py`:

```python
"""Compatibility wrapper for tests.testkit.records."""

from tests.testkit.records import InMemoryRecordStore

__all__ = ["InMemoryRecordStore"]
```

Replace `tests/helpers/inmemory_nexus_fs.py`:

```python
"""Compatibility wrapper for tests.testkit.metadata."""

from tests.testkit.metadata import InMemoryNexusFS

__all__ = ["InMemoryNexusFS"]
```

Replace `tests/helpers/test_context.py`:

```python
"""Compatibility wrapper for tests.testkit.auth."""

from tests.testkit.auth import TEST_ADMIN_CONTEXT, TEST_CONTEXT, operation_context

__all__ = ["TEST_ADMIN_CONTEXT", "TEST_CONTEXT", "operation_context"]
```

Replace `tests/helpers/edge_cases.py`:

```python
"""Compatibility wrapper for tests.testkit.edge_cases."""

from tests.testkit.edge_cases import (
    EDGE_CONTENT,
    PATHS_THAT_SHOULD_NORMALIZE_OR_REJECT,
    SPECIAL_PATHS,
    UNICODE_PATHS,
)

__all__ = [
    "EDGE_CONTENT",
    "PATHS_THAT_SHOULD_NORMALIZE_OR_REJECT",
    "SPECIAL_PATHS",
    "UNICODE_PATHS",
]
```

Replace `tests/helpers/mock_websocket.py`:

```python
"""Compatibility wrapper for tests.testkit.websocket."""

from tests.testkit.websocket import MockWebSocket

__all__ = ["MockWebSocket"]
```

Edit `tests/conftest.py`:

- Keep all pytest hooks, environment setup, and autouse fixtures.
- Delete the body of the local `make_test_nexus` function.
- Add this import near the other imports:

```python
from tests.testkit import make_test_nexus
```

- [ ] **Step 5: Run the import test to verify it passes**

Run:

```bash
uv run pytest tests/unit/test_testkit_imports.py -q -n0
```

Expected: PASS.

- [ ] **Step 6: Run current helper-dependent smoke tests**

Run:

```bash
uv run pytest tests/unit/storage/test_dict_metastore.py tests/unit/core/test_nexus_fs_read_batch.py tests/unit/core/test_nexus_fs_write_batch.py -q -n0
```

Expected: PASS.

- [ ] **Step 7: Commit Task 1**

Run:

```bash
git add tests/testkit tests/helpers tests/conftest.py tests/unit/test_testkit_imports.py
git commit -m "test: introduce canonical nexus testkit"
```

---

### Task 2: Add Profile Matrix Helpers

**Files:**
- Create: `tests/testkit/profiles.py`
- Create: `tests/unit/test_testkit_profiles.py`
- Modify: `tests/testkit/__init__.py`
- Modify: `tests/unit/backends/test_runtime_deps.py`

- [ ] **Step 1: Write failing profile matrix tests**

Create `tests/unit/test_testkit_profiles.py`:

```python
"""Tests for shared test profile matrices."""

from __future__ import annotations

import pytest


def test_profile_matrix_returns_named_profiles() -> None:
    from tests.testkit.profiles import profile_matrix

    profiles = profile_matrix("slim", "sandbox")

    assert [profile.name for profile in profiles] == ["slim", "sandbox"]
    assert profiles[0].config["profile"] == "slim"
    assert profiles[1].config["profile"] == "sandbox"
    assert profiles[0].is_available is True
    assert profiles[1].is_available is True


def test_profile_matrix_unknown_profile_raises() -> None:
    from tests.testkit.profiles import profile_matrix

    with pytest.raises(ValueError, match="Unknown test profile"):
        profile_matrix("does-not-exist")


def test_pytest_profile_params_have_stable_ids() -> None:
    from tests.testkit.profiles import pytest_profile_params

    params = pytest_profile_params("slim", "sandbox")

    assert [param.id for param in params] == ["profile=slim", "profile=sandbox"]


def test_unavailable_profiles_are_skipped_by_default() -> None:
    from tests.testkit.profiles import pytest_profile_params

    params = pytest_profile_params("remote", "federation")

    assert [param.id for param in params] == ["profile=remote", "profile=federation"]
    assert all(param.marks for param in params)
    reasons = [mark.kwargs["reason"] for param in params for mark in param.marks]
    assert any("remote URL" in reason for reason in reasons)
    assert any("federation" in reason for reason in reasons)


def test_include_unavailable_returns_unmarked_params() -> None:
    from tests.testkit.profiles import pytest_profile_params

    params = pytest_profile_params("remote", include_unavailable=True)

    assert params[0].id == "profile=remote"
    assert params[0].marks == ()
```

- [ ] **Step 2: Run profile tests to verify they fail**

Run:

```bash
uv run pytest tests/unit/test_testkit_profiles.py -q -n0
```

Expected: FAIL with `ModuleNotFoundError` or missing `profiles`.

- [ ] **Step 3: Implement `tests/testkit/profiles.py`**

Create `tests/testkit/profiles.py`:

```python
"""Shared deployment profile matrices for tests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest


@dataclass(frozen=True)
class TestProfile:
    """Profile metadata used by parametrized tests."""

    name: str
    config: dict[str, Any]
    requires_server: bool = False
    requires_remote: bool = False
    requires_federation: bool = False
    reason: str | None = None

    @property
    def is_available(self) -> bool:
        return not (self.requires_server or self.requires_remote or self.requires_federation)

    @property
    def skip_reason(self) -> str | None:
        if self.is_available:
            return None
        if self.reason:
            return self.reason
        if self.requires_remote:
            return "requires remote URL and API key"
        if self.requires_federation:
            return "requires federation test environment"
        if self.requires_server:
            return "requires live Nexus server fixture"
        return "profile is not available in this test environment"


_PROFILES: dict[str, TestProfile] = {
    "slim": TestProfile("slim", {"profile": "slim"}),
    "sandbox": TestProfile("sandbox", {"profile": "sandbox"}),
    "embedded": TestProfile("embedded", {"profile": "embedded"}),
    "server": TestProfile(
        "server",
        {"profile": "full"},
        requires_server=True,
        reason="requires live Nexus server fixture",
    ),
    "remote": TestProfile(
        "remote",
        {"profile": "remote"},
        requires_remote=True,
        reason="requires remote URL and API key",
    ),
    "federation": TestProfile(
        "federation",
        {"profile": "cluster"},
        requires_federation=True,
        reason="requires federation test environment",
    ),
}


def profile_matrix(*names: str) -> tuple[TestProfile, ...]:
    """Return profile metadata in caller-specified order."""
    selected = names or tuple(_PROFILES)
    unknown = [name for name in selected if name not in _PROFILES]
    if unknown:
        known = ", ".join(sorted(_PROFILES))
        raise ValueError(f"Unknown test profile(s): {unknown}. Known profiles: {known}")
    return tuple(_PROFILES[name] for name in selected)


def pytest_profile_params(
    *names: str,
    include_unavailable: bool = False,
) -> list[pytest.ParameterSet]:
    """Return pytest params with stable IDs and skip marks for unavailable profiles."""
    params: list[pytest.ParameterSet] = []
    for profile in profile_matrix(*names):
        marks = ()
        if not include_unavailable and not profile.is_available:
            marks = (pytest.mark.skip(reason=profile.skip_reason),)
        params.append(pytest.param(profile, id=f"profile={profile.name}", marks=marks))
    return params


__all__ = ["TestProfile", "profile_matrix", "pytest_profile_params"]
```

- [ ] **Step 4: Export profile helpers from `tests/testkit/__init__.py`**

Add:

```python
from tests.testkit.profiles import TestProfile, profile_matrix, pytest_profile_params
```

Add these names to `__all__`:

```python
"TestProfile",
"profile_matrix",
"pytest_profile_params",
```

- [ ] **Step 5: Run profile tests to verify they pass**

Run:

```bash
uv run pytest tests/unit/test_testkit_profiles.py -q -n0
```

Expected: PASS.

- [ ] **Step 6: Use profile matrix in one backend suite**

Modify `tests/unit/backends/test_runtime_deps.py`.

Add this import near the top:

```python
from tests.testkit.profiles import TestProfile, pytest_profile_params
```

Add this test near the existing `TestDepTypes` class:

```python
@pytest.mark.parametrize(
    "profile",
    pytest_profile_params("slim", "sandbox", "remote"),
)
def test_runtime_dep_profile_matrix_ids_are_usable(profile: TestProfile) -> None:
    """Smoke-test the shared profile matrix in a backend-facing suite."""
    assert profile.config["profile"] in {"slim", "sandbox", "remote"}
```

Expected behavior: `slim` and `sandbox` run; `remote` is collected with a skip mark.

- [ ] **Step 7: Run backend profile-matrix target**

Run:

```bash
uv run pytest tests/unit/backends/test_runtime_deps.py -q -n0
```

Expected: PASS with one skipped test for `profile=remote`.

- [ ] **Step 8: Commit Task 2**

Run:

```bash
git add tests/testkit tests/unit/test_testkit_profiles.py tests/unit/backends/test_runtime_deps.py
git commit -m "test: add profile matrix helpers"
```

---

### Task 3: Add Lazy Container Helpers

**Files:**
- Create: `tests/testkit/containers.py`
- Create: `tests/unit/test_testkit_containers.py`
- Modify: `tests/testkit/__init__.py`

- [ ] **Step 1: Write failing lazy-import tests**

Create `tests/unit/test_testkit_containers.py`:

```python
"""Tests for optional service helpers in tests.testkit.containers."""

from __future__ import annotations

import sys

import pytest


def test_containers_module_does_not_import_optional_clients_eagerly() -> None:
    before = set(sys.modules)

    import tests.testkit.containers as containers

    imported = set(sys.modules) - before
    assert containers.ServiceInfo.__name__ == "ServiceInfo"
    assert "docker" not in imported
    assert "redis" not in imported
    assert "nats" not in imported
    assert "psycopg2" not in imported
    assert "asyncpg" not in imported


def test_service_info_context_manager_runs_cleanup_once() -> None:
    from tests.testkit.containers import ServiceInfo

    calls: list[str] = []
    service = ServiceInfo(
        name="redis",
        url="redis://localhost:6379/0",
        env={"REDIS_URL": "redis://localhost:6379/0"},
        cleanup=lambda: calls.append("cleanup"),
    )

    with service as entered:
        assert entered is service

    assert calls == ["cleanup"]


def test_skip_unavailable_service_raises_pytest_skip() -> None:
    from tests.testkit.containers import skip_unavailable_service

    with pytest.raises(pytest.skip.Exception, match="Postgres is not available"):
        skip_unavailable_service("Postgres")
```

- [ ] **Step 2: Run container tests to verify they fail**

Run:

```bash
uv run pytest tests/unit/test_testkit_containers.py -q -n0
```

Expected: FAIL with missing `tests.testkit.containers`.

- [ ] **Step 3: Implement `tests/testkit/containers.py`**

Create `tests/testkit/containers.py`:

```python
"""Lazy optional-service helpers for integration and e2e tests."""

from __future__ import annotations

import socket
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

import pytest


@dataclass(frozen=True)
class ServiceInfo:
    """Connection metadata for an optional service used by tests."""

    name: str
    url: str
    env: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, object] = field(default_factory=dict)
    cleanup: Callable[[], None] | None = None

    def __enter__(self) -> "ServiceInfo":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self.cleanup is not None:
            self.cleanup()


def _is_tcp_open(host: str, port: int, *, timeout: float = 0.25) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def skip_unavailable_service(name: str, reason: str | None = None) -> None:
    message = reason or f"{name} is not available"
    pytest.skip(message)


def postgres_service(
    url: str = "postgresql://postgres:nexus@localhost:5432/nexus",
    *,
    host: str = "localhost",
    port: int = 5432,
) -> ServiceInfo:
    """Return Postgres service metadata or skip when the service is unavailable."""
    if not _is_tcp_open(host, port):
        skip_unavailable_service("Postgres")
    return ServiceInfo("postgres", url, {"NEXUS_DATABASE_URL": url})


def redis_service(
    url: str = "redis://localhost:6379/0",
    *,
    host: str = "localhost",
    port: int = 6379,
) -> ServiceInfo:
    """Return Redis/Dragonfly service metadata or skip when unavailable."""
    if not _is_tcp_open(host, port):
        skip_unavailable_service("Redis/Dragonfly")
    return ServiceInfo("redis", url, {"REDIS_URL": url, "NEXUS_DRAGONFLY_URL": url})


def nats_service(
    url: str = "nats://localhost:4222",
    *,
    host: str = "localhost",
    port: int = 4222,
) -> ServiceInfo:
    """Return NATS service metadata or skip when unavailable."""
    if not _is_tcp_open(host, port):
        skip_unavailable_service("NATS")
    return ServiceInfo("nats", url, {"NEXUS_NATS_URL": url})


def server_smoke_service(
    base_url: str = "http://127.0.0.1:2026",
    *,
    host: str = "127.0.0.1",
    port: int = 2026,
) -> ServiceInfo:
    """Return live Nexus server metadata or skip when unavailable."""
    if not _is_tcp_open(host, port):
        skip_unavailable_service("Nexus server")
    return ServiceInfo("server", base_url, {"NEXUS_BASE_URL": base_url})


@contextmanager
def patched_service_env(monkeypatch: pytest.MonkeyPatch, service: ServiceInfo) -> Iterator[ServiceInfo]:
    """Patch service environment variables for the duration of a test."""
    for key, value in service.env.items():
        monkeypatch.setenv(key, value)
    yield service


__all__ = [
    "ServiceInfo",
    "nats_service",
    "patched_service_env",
    "postgres_service",
    "redis_service",
    "server_smoke_service",
    "skip_unavailable_service",
]
```

- [ ] **Step 4: Export container helpers from `tests/testkit/__init__.py`**

Add:

```python
from tests.testkit.containers import ServiceInfo
```

Add `"ServiceInfo"` to `__all__`.

- [ ] **Step 5: Run container tests to verify they pass**

Run:

```bash
uv run pytest tests/unit/test_testkit_containers.py -q -n0
```

Expected: PASS.

- [ ] **Step 6: Commit Task 3**

Run:

```bash
git add tests/testkit tests/unit/test_testkit_containers.py
git commit -m "test: add lazy container helpers"
```

---

### Task 4: Add Assertion Helpers

**Files:**
- Create: `tests/testkit/assertions.py`
- Create: `tests/unit/test_testkit_assertions.py`
- Modify: `tests/testkit/__init__.py`

- [ ] **Step 1: Write failing assertion helper tests**

Create `tests/unit/test_testkit_assertions.py`:

```python
"""Tests for reusable testkit assertions."""

from __future__ import annotations

import pytest

from nexus.contracts.exceptions import BackendError, MissingDependencyError


def test_assert_metadata_contains_accepts_dict() -> None:
    from tests.testkit.assertions import assert_metadata_contains

    assert_metadata_contains({"path": "/a.txt", "size": 3}, path="/a.txt", size=3)


def test_assert_metadata_contains_accepts_object() -> None:
    from tests.testkit.assertions import assert_metadata_contains

    class Metadata:
        path = "/a.txt"
        size = 3

    assert_metadata_contains(Metadata(), path="/a.txt", size=3)


def test_assert_metadata_contains_reports_mismatch() -> None:
    from tests.testkit.assertions import assert_metadata_contains

    with pytest.raises(AssertionError, match="metadata.size"):
        assert_metadata_contains({"size": 3}, size=4)


def test_assert_permission_denied_accepts_response_shape() -> None:
    from tests.testkit.assertions import assert_permission_denied

    assert_permission_denied({"status_code": 403, "detail": "permission denied"})


def test_assert_dependency_failure_accepts_missing_dependency_error() -> None:
    from tests.testkit.assertions import assert_dependency_failure

    err = MissingDependencyError(backend="gcs", missing=[])

    assert_dependency_failure(err, "gcs")


def test_assert_event_matches_checks_selected_fields() -> None:
    from tests.testkit.assertions import assert_event_matches

    assert_event_matches(
        {"path": "/x.txt", "event_type": "file_write", "zone_id": "root"},
        path="/x.txt",
        event_type="file_write",
        zone_id="root",
    )


def test_assert_event_matches_reports_selected_field() -> None:
    from tests.testkit.assertions import assert_event_matches

    with pytest.raises(AssertionError, match="event.path"):
        assert_event_matches({"path": "/x.txt"}, path="/y.txt")


def test_assert_dependency_failure_rejects_wrong_error() -> None:
    from tests.testkit.assertions import assert_dependency_failure

    with pytest.raises(AssertionError, match="dependency failure"):
        assert_dependency_failure(BackendError("different", backend="local"), "gcs")
```

- [ ] **Step 2: Run assertion tests to verify they fail**

Run:

```bash
uv run pytest tests/unit/test_testkit_assertions.py -q -n0
```

Expected: FAIL with missing `tests.testkit.assertions`.

- [ ] **Step 3: Implement `tests/testkit/assertions.py`**

Create `tests/testkit/assertions.py`:

```python
"""Reusable assertions for Nexus tests."""

from __future__ import annotations

from typing import Any

from nexus.contracts.exceptions import MissingDependencyError


def _read_field(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        return value.get(key)
    return getattr(value, key, None)


def assert_metadata_contains(metadata: Any, **expected: Any) -> None:
    """Assert selected metadata fields match expected values."""
    for key, expected_value in expected.items():
        actual = _read_field(metadata, key)
        assert actual == expected_value, f"metadata.{key}: expected {expected_value!r}, got {actual!r}"


def assert_permission_denied(value: Any) -> None:
    """Assert an exception or response-like value represents permission denial."""
    if isinstance(value, dict):
        status = value.get("status_code") or value.get("status")
        detail = str(value.get("detail") or value.get("message") or "")
        assert status in {401, 403} or "permission" in detail.lower(), (
            f"expected permission denied response, got {value!r}"
        )
        return

    text = str(value).lower()
    assert "permission" in text or "forbidden" in text or "unauthorized" in text, (
        f"expected permission denied error, got {value!r}"
    )


def assert_dependency_failure(value: Any, dependency_name: str) -> None:
    """Assert an error/response identifies a missing dependency."""
    if isinstance(value, MissingDependencyError):
        assert dependency_name in str(value), (
            f"dependency failure did not mention {dependency_name!r}: {value!r}"
        )
        return

    text = str(value)
    assert "dependency" in text.lower() and dependency_name in text, (
        f"expected dependency failure for {dependency_name!r}, got {value!r}"
    )


def assert_event_matches(
    event: Any,
    *,
    path: str | None = None,
    event_type: str | None = None,
    zone_id: str | None = None,
) -> None:
    """Assert selected event fields match expected values."""
    expected = {
        "path": path,
        "event_type": event_type,
        "zone_id": zone_id,
    }
    for key, expected_value in expected.items():
        if expected_value is None:
            continue
        actual = _read_field(event, key)
        assert actual == expected_value, f"event.{key}: expected {expected_value!r}, got {actual!r}"


__all__ = [
    "assert_dependency_failure",
    "assert_event_matches",
    "assert_metadata_contains",
    "assert_permission_denied",
]
```

- [ ] **Step 4: Export assertion helpers from `tests/testkit/__init__.py`**

Add:

```python
from tests.testkit.assertions import (
    assert_dependency_failure,
    assert_event_matches,
    assert_metadata_contains,
    assert_permission_denied,
)
```

Add these names to `__all__`:

```python
"assert_dependency_failure",
"assert_event_matches",
"assert_metadata_contains",
"assert_permission_denied",
```

- [ ] **Step 5: Run assertion tests to verify they pass**

Run:

```bash
uv run pytest tests/unit/test_testkit_assertions.py -q -n0
```

Expected: PASS.

- [ ] **Step 6: Commit Task 4**

Run:

```bash
git add tests/testkit tests/unit/test_testkit_assertions.py
git commit -m "test: add reusable test assertions"
```

---

### Task 5: Add Explicit Testkit Fixtures

**Files:**
- Create: `tests/testkit/fixtures.py`
- Modify: `tests/unit/conftest.py`

- [ ] **Step 1: Write failing fixture import test**

Append to `tests/unit/test_testkit_imports.py`:

```python
def test_testkit_fixture_functions_are_importable() -> None:
    from tests.testkit.fixtures import isolated_db, record_store

    assert isolated_db.__name__ == "isolated_db"
    assert record_store.__name__ == "record_store"
```

- [ ] **Step 2: Run import test to verify it fails**

Run:

```bash
uv run pytest tests/unit/test_testkit_imports.py::test_testkit_fixture_functions_are_importable -q -n0
```

Expected: FAIL with missing `tests.testkit.fixtures`.

- [ ] **Step 3: Implement `tests/testkit/fixtures.py`**

Create `tests/testkit/fixtures.py`:

```python
"""Explicit pytest fixtures backed by tests.testkit helpers."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import suppress
from pathlib import Path

import pytest

from tests.testkit.records import InMemoryRecordStore


@pytest.fixture
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Create an isolated SQLite database path and clear DB override env vars."""
    monkeypatch.delenv("NEXUS_DATABASE_URL", raising=False)
    monkeypatch.delenv("POSTGRES_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    db_path = tmp_path / f"test_db_{str(uuid.uuid4())[:8]}.db"
    yield db_path

    if db_path.exists():
        with suppress(Exception):
            db_path.unlink()


@pytest.fixture
def record_store() -> Iterator[InMemoryRecordStore]:
    """Provide an in-memory SQL-backed RecordStoreABC."""
    store = InMemoryRecordStore()
    try:
        yield store
    finally:
        store.close()


__all__ = ["isolated_db", "record_store"]
```

- [ ] **Step 4: Re-export the record store fixture in `tests/unit/conftest.py`**

In `tests/unit/conftest.py`, replace the local `record_store` fixture implementation with:

```python
from tests.testkit.fixtures import record_store
```

Keep the unit-specific `isolated_db` fixture in place for now because it is suite-local and already documented in that file.

- [ ] **Step 5: Run fixture import and unit conftest smoke tests**

Run:

```bash
uv run pytest tests/unit/test_testkit_imports.py::test_testkit_fixture_functions_are_importable tests/unit/storage/test_create_agent_api_key.py -q -n0
```

Expected: PASS.

- [ ] **Step 6: Commit Task 5**

Run:

```bash
git add tests/testkit/fixtures.py tests/unit/conftest.py tests/unit/test_testkit_imports.py
git commit -m "test: add explicit testkit fixtures"
```

---

### Task 6: Migrate Internal Imports

**Files:**
- Modify: test files under `tests/` that import `tests.helpers.*`, bare `helpers.mock_websocket`, or `tests.conftest.make_test_nexus`.
- Modify: `src/nexus/bricks/auth/tests/test_auth_database_key.py` if it imports `tests.helpers.in_memory_record_store`.

- [ ] **Step 1: Record remaining old imports before migration**

Run:

```bash
rg -n "from tests\.helpers|import tests\.helpers|from helpers\.mock_websocket|from tests\.conftest import make_test_nexus|from tests\.helpers" tests src
```

Expected: output lists current old import call sites.

- [ ] **Step 2: Mechanically rewrite imports**

Run these exact replacements:

```bash
perl -0pi -e 's/from tests\.helpers\.dict_metastore import DictMetastore/from tests.testkit.metadata import DictMetastore/g' $(rg -l "tests\.helpers\.dict_metastore" tests src)
perl -0pi -e 's/from tests\.helpers\.failing_backend import FailingBackend/from tests.testkit.backends import FailingBackend/g' $(rg -l "tests\.helpers\.failing_backend" tests src)
perl -0pi -e 's/from tests\.helpers\.failing_metastore import FailingMetastore, MetastoreError/from tests.testkit.metadata import FailingMetastore, MetastoreError/g' $(rg -l "tests\.helpers\.failing_metastore" tests src)
perl -0pi -e 's/from tests\.helpers\.failing_metastore import FailingMetastore/from tests.testkit.metadata import FailingMetastore/g' $(rg -l "tests\.helpers\.failing_metastore" tests src)
perl -0pi -e 's/from tests\.helpers\.in_memory_record_store import InMemoryRecordStore/from tests.testkit.records import InMemoryRecordStore/g' $(rg -l "tests\.helpers\.in_memory_record_store" tests src)
perl -0pi -e 's/from tests\.helpers\.inmemory_nexus_fs import InMemoryNexusFS/from tests.testkit.metadata import InMemoryNexusFS/g' $(rg -l "tests\.helpers\.inmemory_nexus_fs" tests src)
perl -0pi -e 's/from tests\.helpers\.test_context import TEST_ADMIN_CONTEXT, TEST_CONTEXT/from tests.testkit.auth import TEST_ADMIN_CONTEXT, TEST_CONTEXT/g' $(rg -l "tests\.helpers\.test_context" tests src)
perl -0pi -e 's/from tests\.helpers\.test_context import TEST_CONTEXT/from tests.testkit.auth import TEST_CONTEXT/g' $(rg -l "tests\.helpers\.test_context" tests src)
perl -0pi -e 's/from tests\.helpers\.test_context import TEST_ADMIN_CONTEXT/from tests.testkit.auth import TEST_ADMIN_CONTEXT/g' $(rg -l "tests\.helpers\.test_context" tests src)
perl -0pi -e 's/from tests\.helpers\.edge_cases import/from tests.testkit.edge_cases import/g' $(rg -l "tests\.helpers\.edge_cases" tests src)
perl -0pi -e 's/from tests\.helpers\.mock_websocket import MockWebSocket/from tests.testkit.websocket import MockWebSocket/g' $(rg -l "tests\.helpers\.mock_websocket" tests src)
perl -0pi -e 's/from helpers\.mock_websocket import MockWebSocket/from tests.testkit.websocket import MockWebSocket/g' $(rg -l "helpers\.mock_websocket" tests src)
perl -0pi -e 's/from tests\.conftest import make_test_nexus/from tests.testkit import make_test_nexus/g' $(rg -l "from tests\.conftest import make_test_nexus" tests src)
```

- [ ] **Step 3: Run import scan again**

Run:

```bash
rg -n "from tests\.helpers|import tests\.helpers|from helpers\.mock_websocket|from tests\.conftest import make_test_nexus" tests src
```

Expected: no output, except compatibility wrapper files under `tests/helpers` if the scan includes wrapper internals. If wrappers appear, confirm no test file outside `tests/helpers` still imports old paths.

- [ ] **Step 4: Run focused migrated import tests**

Run:

```bash
uv run pytest tests/unit/test_testkit_imports.py tests/unit/core/test_nexus_fs_read_batch.py tests/unit/core/test_nexus_fs_write_batch.py tests/unit/storage/test_create_agent_api_key.py -q -n0
```

Expected: PASS.

- [ ] **Step 5: Run broader affected unit slices**

Run:

```bash
uv run pytest tests/unit/core tests/unit/storage tests/unit/backends -q -n0
```

Expected: PASS or known pre-existing skips only.

- [ ] **Step 6: Commit Task 6**

Run:

```bash
git add tests src
git commit -m "test: migrate test imports to testkit"
```

---

### Task 7: Add Testkit Documentation

**Files:**
- Create: `docs/contributing/testing-testkit.md`
- Modify: `tests/unit/README.md` if present on the implementation branch.

- [ ] **Step 1: Write documentation**

Create `docs/contributing/testing-testkit.md`:

```markdown
# Nexus Testkit

`tests.testkit` is the canonical package for reusable test helpers in Nexus.
Use it for fake backends, fake metadata stores, record stores, operation contexts,
profile matrices, optional service helpers, and repeated assertions.

## Common Imports

```python
from tests.testkit import (
    DictMetastore,
    FailingBackend,
    InMemoryNexusFS,
    InMemoryRecordStore,
    TEST_CONTEXT,
    make_test_nexus,
)
```

Use `make_test_nexus(tmp_path)` when a test needs a real `NexusFS` instance
through the production factory path. Use `InMemoryNexusFS` only for tests that
exercise VFS-backed stores and do not need a full filesystem.

## Fake Backends And Stores

Use `FailingBackend` to inject backend failures by call count or method name:

```python
from tests.testkit import FailingBackend, make_test_nexus
from nexus.backends.storage.path_local import PathLocalBackend

backend = FailingBackend(
    PathLocalBackend(root_path=str(tmp_path / "data")),
    fail_on_methods=["read_content"],
)
nx = make_test_nexus(tmp_path, backend=backend)
```

Use `DictMetastore` for an isolated kernel-backed metastore and
`InMemoryRecordStore` for in-memory SQL-backed auth/record tests.

## Profile Matrices

Use `pytest_profile_params` for cross-profile tests:

```python
import pytest
from tests.testkit.profiles import TestProfile, pytest_profile_params

@pytest.mark.parametrize("profile", pytest_profile_params("slim", "sandbox", "remote"))
def test_profile_behavior(profile: TestProfile) -> None:
    assert profile.config["profile"] in {"slim", "sandbox", "remote"}
```

Profiles that need unavailable services, such as `remote` and `federation`, are
skipped by default. Pass `include_unavailable=True` only when the test provides
the required live fixture.

## Optional Service Helpers

Container/service helpers live in `tests.testkit.containers`. They are lazy:
importing the module must not import Docker, Redis, NATS, or Postgres clients.
Call a helper inside a fixture or test when the service is explicitly needed:

```python
from tests.testkit.containers import postgres_service, patched_service_env

def test_postgres_case(monkeypatch):
    service = postgres_service()
    with patched_service_env(monkeypatch, service):
        ...
```

When a service is unavailable, helpers skip with a clear reason.

## Compatibility Imports

Old imports under `tests.helpers.*` and `tests.conftest.make_test_nexus` remain
as compatibility wrappers, but new tests should use `tests.testkit`.
```

- [ ] **Step 2: Link from `tests/unit/README.md` if present**

If `tests/unit/README.md` exists, add a short section:

```markdown
## Shared Test Helpers

Use `tests.testkit` for reusable fake backends, record stores, operation
contexts, profile matrices, and optional service helpers. See
`docs/contributing/testing-testkit.md`.
```

If `tests/unit/README.md` does not exist on this branch, skip this edit.

- [ ] **Step 3: Run doc and import smoke tests**

Run:

```bash
uv run pytest tests/unit/test_testkit_imports.py -q -n0
```

Expected: PASS.

- [ ] **Step 4: Commit Task 7**

Run:

```bash
git add docs/contributing/testing-testkit.md tests/unit/README.md
git commit -m "docs: document nexus testkit"
```

If `tests/unit/README.md` does not exist, use:

```bash
git add docs/contributing/testing-testkit.md
git commit -m "docs: document nexus testkit"
```

---

### Task 8: Final Verification And Cleanup

**Files:**
- Modify only files required by test failures found in this task.

- [ ] **Step 1: Run lint on the migrated surface**

Run:

```bash
uv run ruff check tests/testkit tests/helpers tests/conftest.py tests/unit/test_testkit_imports.py tests/unit/test_testkit_profiles.py tests/unit/test_testkit_containers.py tests/unit/test_testkit_assertions.py
```

Expected: PASS.

- [ ] **Step 2: Run focused unit tests**

Run:

```bash
uv run pytest tests/unit/test_testkit_imports.py tests/unit/test_testkit_profiles.py tests/unit/test_testkit_containers.py tests/unit/test_testkit_assertions.py -q -n0
```

Expected: PASS.

- [ ] **Step 3: Run affected suite slices**

Run:

```bash
uv run pytest tests/unit/storage tests/unit/core tests/unit/backends tests/integration/connectors -q -n0
```

Expected: PASS or documented pre-existing skips only.

- [ ] **Step 4: Scan for old internal imports**

Run:

```bash
rg -n "from tests\.helpers|import tests\.helpers|from helpers\.mock_websocket|from tests\.conftest import make_test_nexus" tests src
```

Expected: no internal test imports remain outside compatibility wrappers. If `tests/helpers/*.py` appears, confirm every hit is a wrapper importing from `tests.testkit`.

- [ ] **Step 5: Scan for eager optional service imports in testkit**

Run:

```bash
rg -n "^(import|from) (docker|redis|nats|psycopg2|asyncpg)" tests/testkit
```

Expected: no output.

- [ ] **Step 6: Check git status**

Run:

```bash
git status --short
```

Expected: no unstaged changes unless Task 8 required a fix. If fixes were needed, commit them:

```bash
git add tests docs
git commit -m "test: finalize nexus testkit migration"
```

---

## Self-Review Notes

- Spec coverage: package creation, helper migration, profile matrix, container helpers, assertion helpers, docs, lazy optional dependencies, and compatibility wrappers are covered by Tasks 1-8.
- Placeholder scan: no unresolved marker text or unspecified implementation blocks remain. Mechanical migration commands are explicit.
- Type consistency: profile helpers consistently use `TestProfile`, `profile_matrix`, and `pytest_profile_params`; container helpers consistently use `ServiceInfo`; assertion helpers use the function names exported in `tests/testkit/__init__.py`.
