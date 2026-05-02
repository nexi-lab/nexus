# Nexus Testkit

`tests/testkit` is the shared helper package for Nexus tests. It is importable
as `testkit` during pytest runs because `pyproject.toml` adds `tests` to
`pythonpath`. Keep testkit code under `tests/`; do not move it into `src/`
unless the project intentionally creates an installed testing API.

## Package Boundary

Use testkit for reusable test doubles, auth contexts, deployment profile
matrices, optional-service probes, and high-signal assertions. Keep one-off
fixtures close to the suite that owns them.

Testkit modules must not import production-only optional dependencies at module
import time. For example, `testkit.containers` can probe a NATS TCP port, but it
must not import `nats` until a test that explicitly needs the NATS client does so.

## Fake Backend

```python
from testkit.backends import InMemoryBackend


def test_roundtrip() -> None:
    backend = InMemoryBackend()
    result = backend.write_content(b"hello")

    assert backend.read_content(result.content_id) == b"hello"
```

Use `FactoryStubBackend` when a test only needs a connector class that satisfies
registration and factory conformance checks.

```python
from nexus.backends.base.registry import register_connector
from nexus.backends.base.runtime_deps import PythonDep
from testkit.backends import FactoryStubBackend


@register_connector("stub_ok", runtime_deps=(PythonDep("json"),))
class StubOK(FactoryStubBackend):
    pass
```

Existing helpers such as `DictMetastore`, `InMemoryNexusFS`,
`InMemoryRecordStore`, and `FailingBackend` are re-exported from
`testkit.backends` for new tests.

## Auth Contexts

```python
from testkit.auth import make_context, make_zone_context


ctx = make_context(user_id="alice", groups=["eng"], zone_id="zone-a")
zone_ctx = make_zone_context("zone-b", user_id="bob", perms="r")
```

Use `TEST_CONTEXT` and `TEST_ADMIN_CONTEXT` for the default shared identities.
Use builders when a test needs a specific user, group, zone, or admin flag.

## Profile Matrices

```python
import pytest

from testkit.profiles import ProfileCase, local_profile_params


@pytest.mark.parametrize("case", local_profile_params())
def test_profile_defaults(case: ProfileCase) -> None:
    assert case.expected_bricks == case.profile.default_bricks()
    assert case.expected_drivers == case.profile.default_drivers()
```

Use `all_profile_params()` when a test genuinely covers every deployment profile.
Use `local_profile_params()` for tests that should avoid remote, federation, or
cloud-service assumptions.

## Optional Services

```python
from testkit.containers import postgres_probe, require_service


def test_pg_backed_feature() -> None:
    probe = require_service(postgres_probe())
    assert probe.url is not None
```

Supported environment conventions:

| Service | Environment variables |
|---|---|
| Postgres | `NEXUS_DATABASE_URL`, `POSTGRES_URL`, `DATABASE_URL` |
| Redis/Dragonfly | `NEXUS_DRAGONFLY_URL`, `REDIS_URL`, `NEXUS_DRAGONFLY_COORDINATION_URL` |
| NATS | `NEXUS_NATS_URL` |

## Assertions

```python
from testkit.assertions import assert_missing_dependency_error


assert_missing_dependency_error(
    err,
    backend="stub_missing_py",
    count=1,
    missing_names=("definitely_not_a_real_module_xyz",),
    install_hints=("pip install nexus-fs[gcs]",),
)
```

Prefer assertion helpers when they make failure output clearer than raw string
checks in individual tests.

## Migration Guidance

When touching a suite that already imports from `tests.helpers`, leave unrelated
imports alone. Move only helpers needed by the current change into `testkit` or
switch to existing testkit exports. This keeps migrations small and reduces merge
conflicts across the large test tree.
