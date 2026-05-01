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

Use `FailingBackend` to inject backend failures:

```python
from nexus.backends.storage.path_local import PathLocalBackend
from tests.testkit import FailingBackend, make_test_nexus

backend = FailingBackend(
    PathLocalBackend(root_path=tmp_path / "data"),
    fail_on_nth=1,
    fail_on_methods=["read_content"],
)
nx = make_test_nexus(tmp_path, backend=backend)
```

Use `DictMetastore` for an isolated dict-backed metastore compatible with the
production metadata-store interface. Use `InMemoryRecordStore` for in-memory
SQL-backed auth and record-store tests.

## Profile Matrices

Use `pytest_profile_params` for cross-profile tests:

```python
import pytest

from tests.testkit.profiles import TestProfile, pytest_profile_params


@pytest.mark.parametrize("profile", pytest_profile_params("slim", "sandbox", "remote"))
def test_profile_behavior(profile: TestProfile) -> None:
    assert profile.config["profile"] in {"slim", "sandbox", "remote"}
```

Profiles that need unavailable services, such as `server`, `remote`, and
`federation`, are skipped by default. Pass `include_unavailable=True` only when
the test provides the required live fixture.

## Optional Service Helpers

Container and service helpers live in `tests.testkit.containers`. They are lazy:
importing the module must not import Docker, Redis, NATS, or Postgres clients.
Call a helper inside a fixture or test when the service is explicitly needed:

```python
from tests.testkit.containers import patched_service_env, postgres_service


def test_postgres_case(monkeypatch):
    service = postgres_service()
    with patched_service_env(monkeypatch, service):
        ...
```

When a service is unavailable, helpers skip with a clear reason. Use
`patched_service_env` when a test needs service URLs in environment variables;
it restores the environment automatically after the context exits.

## Assertion Helpers

Repeated structural checks should use `tests.testkit.assertions` when the helper
makes the test clearer:

```python
from tests.testkit.assertions import assert_dependency_failure, assert_event_matches


assert_dependency_failure(exc.value, "redis")
assert_event_matches(event, path="/docs/a.txt", event_type="write")
```

Keep one-off domain behavior in the test body. Assertion helpers should clarify
common shapes, not hide unique test logic.

## Fixtures

Import explicit fixtures from `tests.testkit.fixtures` in suite `conftest.py`
files when a suite needs them:

```python
from tests.testkit.fixtures import record_store as record_store
```

Avoid adding broad autouse fixtures to the testkit. Prefer explicit imports so
test dependencies stay visible.

## Compatibility Imports

Old imports under `tests.helpers.*` and `tests.conftest.make_test_nexus` remain
as compatibility wrappers, but new tests should use `tests.testkit`.
