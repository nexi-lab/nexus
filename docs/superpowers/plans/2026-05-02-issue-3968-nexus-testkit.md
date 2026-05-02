# Nexus Testkit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create the first incremental `tests/testkit` package for reusable Nexus test fakes, auth contexts, profile matrices, optional-service helpers, and assertions.

**Architecture:** Keep the package under `tests/testkit` so it is pytest-only and not installed as production API. Add focused modules for profiles, auth, containers, backends, and assertions, re-exporting stable helpers from `tests/helpers` where useful. Migrate `tests/integration/backends/test_factory_dep_check.py` and add `tests/unit/backends/test_profile_matrix.py` as the first backend-suite consumers.

**Tech Stack:** Python 3.14, pytest, Nexus test helpers, `nexus.contracts.deployment_profile`, `nexus.contracts.types.OperationContext`, `nexus.backends.base` contracts.

---

## File Structure

- Create `tests/testkit/__init__.py`: package docstring and public module boundary.
- Create `tests/testkit/profiles.py`: `ProfileCase` dataclass and profile matrix constructors/pytest params.
- Create `tests/testkit/auth.py`: auth context re-exports and builders.
- Create `tests/testkit/containers.py`: env URL helpers, TCP probes, skip helper, and smoke config builders.
- Create `tests/testkit/backends.py`: in-memory backend, factory stub backend, and compatibility re-exports from `tests/helpers`.
- Create `tests/testkit/assertions.py`: missing dependency, event, metadata, and permission assertion helpers.
- Create `tests/unit/testkit/test_profiles.py`: unit tests for profile matrix behavior.
- Create `tests/unit/testkit/test_auth.py`: unit tests for context builders.
- Create `tests/unit/testkit/test_containers.py`: unit tests for env/probe/skip helpers.
- Create `tests/unit/testkit/test_backends.py`: unit tests for in-memory backend and re-exports.
- Create `tests/unit/testkit/test_assertions.py`: unit tests for common assertion helpers.
- Create `tests/unit/backends/test_profile_matrix.py`: backend-suite consumer of `testkit.profiles`.
- Modify `tests/integration/backends/test_factory_dep_check.py`: replace local stub/assertion logic with testkit imports.
- Create `docs/development/testkit.md`: usage guide for new tests.

---

### Task 1: Profile Matrix Testkit

**Files:**
- Create: `tests/testkit/__init__.py`
- Create: `tests/testkit/profiles.py`
- Create: `tests/unit/testkit/test_profiles.py`
- Create: `tests/unit/backends/test_profile_matrix.py`

- [ ] **Step 1: Write failing profile matrix tests**

Create `tests/unit/testkit/test_profiles.py`:

```python
from __future__ import annotations

import pytest

from nexus.contracts.deployment_profile import DeploymentProfile
from testkit.profiles import (
    ProfileCase,
    all_profile_cases,
    all_profile_params,
    local_profile_cases,
    local_profile_params,
    profile_params,
    remote_profile_cases,
    service_profile_cases,
)


def test_all_profile_cases_cover_deployment_profiles_in_stable_order() -> None:
    assert [case.profile for case in all_profile_cases()] == [
        DeploymentProfile.CLUSTER,
        DeploymentProfile.EMBEDDED,
        DeploymentProfile.LITE,
        DeploymentProfile.SANDBOX,
        DeploymentProfile.FULL,
        DeploymentProfile.CLOUD,
        DeploymentProfile.REMOTE,
    ]


def test_profile_cases_mirror_deployment_profile_defaults() -> None:
    for case in all_profile_cases():
        assert isinstance(case, ProfileCase)
        assert case.id == case.profile.value
        assert case.expected_bricks == case.profile.default_bricks()
        assert case.expected_drivers == case.profile.default_drivers()


def test_profile_subsets_are_explicit() -> None:
    assert [case.profile for case in local_profile_cases()] == [
        DeploymentProfile.EMBEDDED,
        DeploymentProfile.LITE,
        DeploymentProfile.SANDBOX,
        DeploymentProfile.FULL,
    ]
    assert [case.profile for case in remote_profile_cases()] == [DeploymentProfile.REMOTE]
    assert [case.profile for case in service_profile_cases()] == [
        DeploymentProfile.CLUSTER,
        DeploymentProfile.CLOUD,
    ]


def test_profile_params_keep_stable_pytest_ids() -> None:
    params = all_profile_params()
    assert [param.id for param in params] == [case.id for case in all_profile_cases()]


def test_local_profile_params_keep_stable_pytest_ids() -> None:
    params = local_profile_params()
    assert [param.id for param in params] == [case.id for case in local_profile_cases()]


def test_profile_params_can_skip_external_service_cases() -> None:
    params = profile_params(service_profile_cases(), skip_external=True)
    by_id = {param.id: param for param in params}

    for profile_id in ("cluster", "cloud"):
        mark_names = [mark.name for mark in by_id[profile_id].marks]
        assert "skip" in mark_names


@pytest.mark.parametrize("case", all_profile_params())
def test_param_values_are_profile_cases(case: ProfileCase) -> None:
    assert case.expected_bricks == case.profile.default_bricks()
    assert case.expected_drivers == case.profile.default_drivers()
```

Create `tests/unit/backends/test_profile_matrix.py`:

```python
from __future__ import annotations

import pytest

from testkit.profiles import ProfileCase, all_profile_params


@pytest.mark.parametrize("case", all_profile_params())
def test_backend_profile_matrix_tracks_profile_defaults(case: ProfileCase) -> None:
    assert case.expected_bricks == case.profile.default_bricks()
    assert case.expected_drivers == case.profile.default_drivers()
```

- [ ] **Step 2: Run profile tests to verify RED**

Run:

```bash
pytest tests/unit/testkit/test_profiles.py tests/unit/backends/test_profile_matrix.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'testkit'`.

- [ ] **Step 3: Add minimal package and profile matrix implementation**

Create `tests/testkit/__init__.py`:

```python
"""Reusable pytest-only helpers for Nexus tests."""
```

Create `tests/testkit/profiles.py`:

```python
"""Deployment profile matrices for Nexus tests."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

import pytest

from nexus.contracts.deployment_profile import DeploymentProfile


@dataclass(frozen=True, slots=True)
class ProfileCase:
    """Explicit deployment-profile case for parametrized tests."""

    profile: DeploymentProfile
    id: str
    expected_bricks: frozenset[str]
    expected_drivers: frozenset[str]
    external_services: tuple[str, ...] = ()
    marks: tuple[pytest.MarkDecorator, ...] = ()

    def param(self, *, skip_external: bool = False) -> object:
        marks = list(self.marks)
        if skip_external and self.external_services:
            services = ", ".join(self.external_services)
            marks.append(
                pytest.mark.skip(
                    reason=f"profile {self.id} requires optional service(s): {services}"
                )
            )
        return pytest.param(self, id=self.id, marks=marks)


_ALL_PROFILES: tuple[DeploymentProfile, ...] = (
    DeploymentProfile.CLUSTER,
    DeploymentProfile.EMBEDDED,
    DeploymentProfile.LITE,
    DeploymentProfile.SANDBOX,
    DeploymentProfile.FULL,
    DeploymentProfile.CLOUD,
    DeploymentProfile.REMOTE,
)

_LOCAL_PROFILES: tuple[DeploymentProfile, ...] = (
    DeploymentProfile.EMBEDDED,
    DeploymentProfile.LITE,
    DeploymentProfile.SANDBOX,
    DeploymentProfile.FULL,
)

_REMOTE_PROFILES: tuple[DeploymentProfile, ...] = (DeploymentProfile.REMOTE,)

_SERVICE_PROFILES: tuple[DeploymentProfile, ...] = (
    DeploymentProfile.CLUSTER,
    DeploymentProfile.CLOUD,
)

_EXTERNAL_SERVICES: dict[DeploymentProfile, tuple[str, ...]] = {
    DeploymentProfile.CLUSTER: ("federation",),
    DeploymentProfile.CLOUD: ("postgres", "redis", "nats"),
}


def profile_case(profile: DeploymentProfile) -> ProfileCase:
    """Build a `ProfileCase` from the production profile defaults."""

    return ProfileCase(
        profile=profile,
        id=profile.value,
        expected_bricks=profile.default_bricks(),
        expected_drivers=profile.default_drivers(),
        external_services=_EXTERNAL_SERVICES.get(profile, ()),
    )


def profile_cases(profiles: Iterable[DeploymentProfile]) -> tuple[ProfileCase, ...]:
    """Build profile cases in the caller-provided order."""

    return tuple(profile_case(profile) for profile in profiles)


def all_profile_cases() -> tuple[ProfileCase, ...]:
    """Return every known deployment profile in stable enum order."""

    return profile_cases(_ALL_PROFILES)


def local_profile_cases() -> tuple[ProfileCase, ...]:
    """Return profiles that run without remote/client or service-cluster semantics."""

    return profile_cases(_LOCAL_PROFILES)


def remote_profile_cases() -> tuple[ProfileCase, ...]:
    """Return remote-client profile cases."""

    return profile_cases(_REMOTE_PROFILES)


def service_profile_cases() -> tuple[ProfileCase, ...]:
    """Return profiles associated with federation or external services."""

    return profile_cases(_SERVICE_PROFILES)


def profile_params(
    cases: Iterable[ProfileCase],
    *,
    skip_external: bool = False,
) -> list[object]:
    """Convert profile cases into pytest parameters with stable IDs."""

    return [case.param(skip_external=skip_external) for case in cases]


def all_profile_params(*, skip_external: bool = False) -> list[object]:
    """Return pytest params for every deployment profile."""

    return profile_params(all_profile_cases(), skip_external=skip_external)


def local_profile_params(*, skip_external: bool = False) -> list[object]:
    """Return pytest params for local deployment profiles."""

    return profile_params(local_profile_cases(), skip_external=skip_external)
```

- [ ] **Step 4: Run profile tests to verify GREEN**

Run:

```bash
pytest tests/unit/testkit/test_profiles.py tests/unit/backends/test_profile_matrix.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit profile matrix slice**

Run:

```bash
git add tests/testkit/__init__.py tests/testkit/profiles.py tests/unit/testkit/test_profiles.py tests/unit/backends/test_profile_matrix.py
git commit -m "test: add profile matrix testkit"
```

Expected: commit succeeds.

---

### Task 2: Auth and Optional-Service Helpers

**Files:**
- Create: `tests/testkit/auth.py`
- Create: `tests/testkit/containers.py`
- Create: `tests/unit/testkit/test_auth.py`
- Create: `tests/unit/testkit/test_containers.py`

- [ ] **Step 1: Write failing auth helper tests**

Create `tests/unit/testkit/test_auth.py`:

```python
from __future__ import annotations

from testkit.auth import (
    TEST_ADMIN_CONTEXT,
    TEST_CONTEXT,
    make_admin_context,
    make_context,
    make_zone_context,
)


def test_shared_contexts_are_reexported() -> None:
    assert TEST_CONTEXT.user_id == "test"
    assert TEST_CONTEXT.is_admin is False
    assert TEST_ADMIN_CONTEXT.user_id == "test-admin"
    assert TEST_ADMIN_CONTEXT.is_admin is True


def test_make_context_builds_user_context() -> None:
    ctx = make_context(user_id="alice", groups=["eng"], zone_id="zone-a")
    assert ctx.user_id == "alice"
    assert ctx.groups == ["eng"]
    assert ctx.zone_id == "zone-a"
    assert ctx.zone_set == ("zone-a",)
    assert ctx.zone_perms == (("zone-a", "rw"),)
    assert ctx.is_admin is False


def test_make_admin_context_sets_admin_flag() -> None:
    ctx = make_admin_context(user_id="root", groups=["ops"])
    assert ctx.user_id == "root"
    assert ctx.groups == ["ops"]
    assert ctx.is_admin is True


def test_make_zone_context_sets_zone_permissions() -> None:
    ctx = make_zone_context("zone-b", user_id="bob", perms="r")
    assert ctx.user_id == "bob"
    assert ctx.zone_id == "zone-b"
    assert ctx.zone_set == ("zone-b",)
    assert ctx.zone_perms == (("zone-b", "r"),)
```

- [ ] **Step 2: Write failing optional-service helper tests**

Create `tests/unit/testkit/test_containers.py`:

```python
from __future__ import annotations

import pytest

from testkit.containers import (
    ServiceProbe,
    get_env_url,
    nats_url,
    parse_host_port,
    postgres_probe,
    postgres_url,
    probe_tcp_service,
    redis_url,
    require_service,
    server_smoke_config,
)


def test_get_env_url_returns_first_configured_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ONE", raising=False)
    monkeypatch.setenv("TWO", "value-two")
    monkeypatch.setenv("THREE", "value-three")

    assert get_env_url(("ONE", "TWO", "THREE")) == "value-two"


def test_postgres_url_uses_existing_env_order(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NEXUS_DATABASE_URL", raising=False)
    monkeypatch.setenv("POSTGRES_URL", "postgresql://u:p@db:5432/nexus")
    monkeypatch.setenv("DATABASE_URL", "postgresql://ignored")

    assert postgres_url() == "postgresql://u:p@db:5432/nexus"


def test_redis_url_uses_dragonfly_before_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_DRAGONFLY_URL", "redis://dragonfly:6379/0")
    monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")

    assert redis_url() == "redis://dragonfly:6379/0"


def test_nats_url_defaults_to_localhost(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NEXUS_NATS_URL", raising=False)

    assert nats_url() == "nats://localhost:4222"


def test_parse_host_port_supports_scheme_and_bare_host() -> None:
    assert parse_host_port("postgresql://u:p@db.example:5433/nexus", 5432) == (
        "db.example",
        5433,
    )
    assert parse_host_port("localhost:4222", 4222) == ("localhost", 4222)


def test_postgres_probe_reports_missing_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("NEXUS_DATABASE_URL", "POSTGRES_URL", "DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)

    probe = postgres_probe()
    assert probe.name == "postgres"
    assert probe.available is False
    assert "NEXUS_DATABASE_URL" in probe.reason


def test_probe_tcp_service_reports_success(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[tuple[str, int], float]] = []

    class _Conn:
        def close(self) -> None:
            pass

    def fake_create_connection(address: tuple[str, int], timeout: float) -> _Conn:
        calls.append((address, timeout))
        return _Conn()

    monkeypatch.setattr("socket.create_connection", fake_create_connection)

    probe = probe_tcp_service("nats", "nats://localhost:4222", 4222, timeout=0.1)

    assert probe == ServiceProbe(
        name="nats",
        url="nats://localhost:4222",
        host="localhost",
        port=4222,
        available=True,
        reason="available",
    )
    assert calls == [(("localhost", 4222), 0.1)]


def test_require_service_skips_when_probe_is_unavailable() -> None:
    probe = ServiceProbe(
        name="postgres",
        url=None,
        host=None,
        port=None,
        available=False,
        reason="set NEXUS_DATABASE_URL",
    )

    with pytest.raises(pytest.skip.Exception) as exc_info:
        require_service(probe)

    assert "set NEXUS_DATABASE_URL" in str(exc_info.value)


def test_server_smoke_config_is_explicit() -> None:
    assert server_smoke_config(port=2028, api_key="key") == {
        "host": "127.0.0.1",
        "port": 2028,
        "base_url": "http://127.0.0.1:2028",
        "api_key": "key",
    }
```

- [ ] **Step 3: Run auth/container tests to verify RED**

Run:

```bash
pytest tests/unit/testkit/test_auth.py tests/unit/testkit/test_containers.py -q
```

Expected: FAIL with import errors for `testkit.auth` and `testkit.containers`.

- [ ] **Step 4: Implement auth helpers**

Create `tests/testkit/auth.py`:

```python
"""Auth and identity fixtures for Nexus tests."""

from __future__ import annotations

from nexus.contracts.types import OperationContext
from tests.helpers.test_context import TEST_ADMIN_CONTEXT, TEST_CONTEXT


def make_context(
    *,
    user_id: str = "test",
    groups: list[str] | None = None,
    zone_id: str | None = None,
    zone_set: tuple[str, ...] = (),
    zone_perms: tuple[tuple[str, str], ...] = (),
    agent_id: str | None = None,
    subject_type: str = "user",
    subject_id: str | None = None,
    is_admin: bool = False,
    is_system: bool = False,
) -> OperationContext:
    """Build an `OperationContext` with explicit identity fields."""

    return OperationContext(
        user_id=user_id,
        groups=list(groups or []),
        zone_id=zone_id,
        zone_set=zone_set,
        zone_perms=zone_perms,
        agent_id=agent_id,
        subject_type=subject_type,
        subject_id=subject_id,
        is_admin=is_admin,
        is_system=is_system,
    )


def make_admin_context(
    *,
    user_id: str = "test-admin",
    groups: list[str] | None = None,
    zone_id: str | None = None,
) -> OperationContext:
    """Build an admin `OperationContext` for tests."""

    return make_context(
        user_id=user_id,
        groups=groups,
        zone_id=zone_id,
        is_admin=True,
    )


def make_zone_context(
    zone_id: str,
    *,
    user_id: str = "test",
    groups: list[str] | None = None,
    perms: str = "rw",
    is_admin: bool = False,
) -> OperationContext:
    """Build a context scoped to one zone with the provided permission string."""

    return make_context(
        user_id=user_id,
        groups=groups,
        zone_id=zone_id,
        zone_perms=((zone_id, perms),),
        is_admin=is_admin,
    )
```

- [ ] **Step 5: Implement optional-service helpers**

Create `tests/testkit/containers.py`:

```python
"""Optional-service probes and smoke-test config helpers."""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from urllib.parse import urlparse

import pytest

POSTGRES_ENV_VARS: tuple[str, ...] = (
    "NEXUS_DATABASE_URL",
    "POSTGRES_URL",
    "DATABASE_URL",
)

REDIS_ENV_VARS: tuple[str, ...] = (
    "NEXUS_DRAGONFLY_URL",
    "REDIS_URL",
    "NEXUS_DRAGONFLY_COORDINATION_URL",
)

NATS_ENV_VARS: tuple[str, ...] = ("NEXUS_NATS_URL",)


@dataclass(frozen=True, slots=True)
class ServiceProbe:
    """Result of checking whether an optional service is reachable."""

    name: str
    url: str | None
    host: str | None
    port: int | None
    available: bool
    reason: str


def get_env_url(names: tuple[str, ...]) -> str | None:
    """Return the first non-empty URL from the provided environment names."""

    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def postgres_url() -> str | None:
    """Return the configured Postgres URL, if present."""

    return get_env_url(POSTGRES_ENV_VARS)


def redis_url() -> str | None:
    """Return the configured Redis or Dragonfly URL, if present."""

    return get_env_url(REDIS_ENV_VARS)


def nats_url(default: str = "nats://localhost:4222") -> str:
    """Return the configured NATS URL, defaulting to localhost."""

    return get_env_url(NATS_ENV_VARS) or default


def parse_host_port(url: str, default_port: int) -> tuple[str, int]:
    """Extract host and port from a service URL or `host:port` string."""

    raw = url if "://" in url else f"//{url}"
    parsed = urlparse(raw)
    if parsed.hostname is None:
        raise ValueError(f"service URL has no host: {url!r}")
    return parsed.hostname, parsed.port or default_port


def _missing_probe(name: str, env_names: tuple[str, ...]) -> ServiceProbe:
    return ServiceProbe(
        name=name,
        url=None,
        host=None,
        port=None,
        available=False,
        reason=f"set one of {', '.join(env_names)}",
    )


def probe_tcp_service(
    name: str,
    url: str,
    default_port: int,
    *,
    timeout: float = 0.25,
) -> ServiceProbe:
    """Probe a TCP service without importing the service's Python client."""

    host, port = parse_host_port(url, default_port)
    try:
        conn = socket.create_connection((host, port), timeout=timeout)
        conn.close()
    except OSError as exc:
        return ServiceProbe(
            name=name,
            url=url,
            host=host,
            port=port,
            available=False,
            reason=f"{name} unavailable at {host}:{port}: {exc}",
        )
    return ServiceProbe(
        name=name,
        url=url,
        host=host,
        port=port,
        available=True,
        reason="available",
    )


def postgres_probe(*, timeout: float = 0.25) -> ServiceProbe:
    """Probe configured Postgres, or return an unavailable probe when unset."""

    url = postgres_url()
    if url is None:
        return _missing_probe("postgres", POSTGRES_ENV_VARS)
    return probe_tcp_service("postgres", url, 5432, timeout=timeout)


def redis_probe(*, timeout: float = 0.25) -> ServiceProbe:
    """Probe configured Redis or Dragonfly, or return unavailable when unset."""

    url = redis_url()
    if url is None:
        return _missing_probe("redis", REDIS_ENV_VARS)
    return probe_tcp_service("redis", url, 6379, timeout=timeout)


def nats_probe(*, timeout: float = 0.25) -> ServiceProbe:
    """Probe configured NATS, defaulting to localhost."""

    return probe_tcp_service("nats", nats_url(), 4222, timeout=timeout)


def require_service(probe: ServiceProbe) -> ServiceProbe:
    """Skip the current pytest test when the probe is unavailable."""

    if not probe.available:
        pytest.skip(probe.reason)
    return probe


def server_smoke_config(
    *,
    host: str = "127.0.0.1",
    port: int,
    api_key: str = "test-api-key",
) -> dict[str, object]:
    """Build a small server-smoke config dictionary."""

    return {
        "host": host,
        "port": port,
        "base_url": f"http://{host}:{port}",
        "api_key": api_key,
    }
```

- [ ] **Step 6: Run auth/container tests to verify GREEN**

Run:

```bash
pytest tests/unit/testkit/test_auth.py tests/unit/testkit/test_containers.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit auth/container slice**

Run:

```bash
git add tests/testkit/auth.py tests/testkit/containers.py tests/unit/testkit/test_auth.py tests/unit/testkit/test_containers.py
git commit -m "test: add auth and service testkit helpers"
```

Expected: commit succeeds.

---

### Task 3: Backend Fakes and Assertion Helpers

**Files:**
- Create: `tests/testkit/backends.py`
- Create: `tests/testkit/assertions.py`
- Create: `tests/unit/testkit/test_backends.py`
- Create: `tests/unit/testkit/test_assertions.py`

- [ ] **Step 1: Write failing backend helper tests**

Create `tests/unit/testkit/test_backends.py`:

```python
from __future__ import annotations

import pytest

from nexus.contracts.exceptions import NexusFileNotFoundError
from testkit.backends import (
    DictMetastore,
    FactoryStubBackend,
    FailingBackend,
    InMemoryBackend,
    InMemoryNexusFS,
    InMemoryRecordStore,
)


def test_in_memory_backend_round_trips_content() -> None:
    backend = InMemoryBackend()
    result = backend.write_content(b"hello")

    assert result.content_id
    assert result.version == result.content_id
    assert result.size == 5
    assert backend.read_content(result.content_id) == b"hello"
    assert backend.content_exists(result.content_id) is True
    assert backend.get_content_size(result.content_id) == 5


def test_in_memory_backend_raises_for_missing_content() -> None:
    backend = InMemoryBackend()

    with pytest.raises(NexusFileNotFoundError):
        backend.read_content("missing")


def test_in_memory_backend_tracks_directories() -> None:
    backend = InMemoryBackend()

    backend.mkdir("/a/b", parents=True, exist_ok=True)

    assert backend.is_directory("/a") is True
    assert backend.is_directory("/a/b") is True
    assert backend.list_dir("/") == ["a"]
    assert backend.list_dir("/a") == ["b"]


def test_factory_stub_backend_accepts_arbitrary_kwargs() -> None:
    backend = FactoryStubBackend(token="secret")

    assert backend.kwargs == {"token": "secret"}
    assert backend.name == "stub"
    assert backend.has_feature("anything") is False


def test_existing_helpers_are_reexported() -> None:
    assert callable(DictMetastore)
    assert callable(InMemoryNexusFS)
    assert callable(InMemoryRecordStore)
    assert callable(FailingBackend)
```

- [ ] **Step 2: Write failing assertion helper tests**

Create `tests/unit/testkit/test_assertions.py`:

```python
from __future__ import annotations

from types import SimpleNamespace

import pytest

from nexus.backends.base.runtime_deps import BinaryDep, PythonDep
from nexus.contracts.exceptions import MissingDependencyError
from testkit.assertions import (
    assert_event_payload,
    assert_metadata_contains,
    assert_missing_dependency_error,
    assert_permission_decision,
)


def _missing_error() -> MissingDependencyError:
    return MissingDependencyError(
        backend="stub_backend",
        missing=[
            (
                PythonDep("missing_module", extras=("gcs",)),
                "python 'missing_module': install with: pip install nexus-fs[gcs]",
            ),
            (
                BinaryDep("missing_bin", "brew install missing-bin"),
                "binary 'missing_bin': not on PATH - install with: brew install missing-bin",
            ),
        ],
    )


def test_assert_missing_dependency_error_accepts_expected_details() -> None:
    assert_missing_dependency_error(
        _missing_error(),
        backend="stub_backend",
        count=2,
        missing_names=("missing_module", "missing_bin"),
        install_hints=("pip install nexus-fs[gcs]", "brew install missing-bin"),
    )


def test_assert_missing_dependency_error_rejects_wrong_backend() -> None:
    with pytest.raises(AssertionError, match="expected backend"):
        assert_missing_dependency_error(_missing_error(), backend="other")


def test_assert_event_payload_supports_dicts() -> None:
    assert_event_payload(
        {"event_type": "file_write", "path": "/docs/a.txt", "zone_id": "zone-a"},
        event_type="file_write",
        path="/docs/a.txt",
        zone_id="zone-a",
    )


def test_assert_event_payload_supports_objects() -> None:
    event = SimpleNamespace(type="file_delete", path="/docs/b.txt", zone_id="zone-b")

    assert_event_payload(event, event_type="file_delete", path="/docs/b.txt", zone_id="zone-b")


def test_assert_metadata_contains_checks_subset() -> None:
    assert_metadata_contains(
        {"path": "/docs/a.txt", "content_type": "text/plain", "size": 12},
        {"path": "/docs/a.txt", "size": 12},
    )


def test_assert_permission_decision_supports_bool_and_objects() -> None:
    assert_permission_decision(True, allowed=True)
    assert_permission_decision(SimpleNamespace(allowed=False), allowed=False)


def test_assert_permission_decision_rejects_wrong_state() -> None:
    with pytest.raises(AssertionError, match="permission decision"):
        assert_permission_decision(False, allowed=True)
```

- [ ] **Step 3: Run backend/assertion tests to verify RED**

Run:

```bash
pytest tests/unit/testkit/test_backends.py tests/unit/testkit/test_assertions.py -q
```

Expected: FAIL with import errors for `testkit.backends` and `testkit.assertions`.

- [ ] **Step 4: Implement backend helpers**

Create `tests/testkit/backends.py`:

```python
"""Backend and storage fakes for Nexus tests."""

from __future__ import annotations

import hashlib
from typing import Any

from nexus.backends.base.backend import Backend, HandlerStatusResponse
from nexus.contracts.exceptions import BackendError, NexusFileNotFoundError
from nexus.core.object_store import WriteResult
from tests.helpers.dict_metastore import DictMetastore
from tests.helpers.failing_backend import FailingBackend
from tests.helpers.in_memory_record_store import InMemoryRecordStore
from tests.helpers.inmemory_nexus_fs import InMemoryNexusFS


def _normalize_dir(path: str) -> str:
    if not path or path == "/":
        return "/"
    return "/" + path.strip("/")


class InMemoryBackend(Backend):
    """Small in-memory backend for backend contract and wrapper tests."""

    def __init__(self, *, name: str = "memory") -> None:
        self._name = name
        self._content: dict[str, bytes] = {}
        self._dirs: set[str] = {"/"}

    @property
    def name(self) -> str:
        return self._name

    def write_content(
        self,
        content: bytes,
        content_id: str = "",
        *,
        offset: int = 0,
        context: Any = None,
    ) -> WriteResult:
        if content_id and offset:
            if content_id not in self._content:
                raise NexusFileNotFoundError(content_id)
            existing = self._content[content_id]
            content = existing[:offset] + content + existing[offset + len(content) :]
        stored_id = content_id or hashlib.sha256(content).hexdigest()
        self._content[stored_id] = bytes(content)
        return WriteResult(content_id=stored_id, version=stored_id, size=len(content))

    def read_content(self, content_id: str, context: Any = None) -> bytes:
        if content_id not in self._content:
            raise NexusFileNotFoundError(content_id)
        return self._content[content_id]

    def delete_content(self, content_id: str, context: Any = None) -> None:
        if content_id not in self._content:
            raise NexusFileNotFoundError(content_id)
        del self._content[content_id]

    def content_exists(self, content_id: str, context: Any = None) -> bool:
        return content_id in self._content

    def get_content_size(self, content_id: str, context: Any = None) -> int:
        return len(self.read_content(content_id, context=context))

    def mkdir(
        self,
        path: str,
        parents: bool = False,
        exist_ok: bool = False,
        context: Any = None,
    ) -> None:
        normalized = _normalize_dir(path)
        if normalized in self._dirs and not exist_ok:
            raise BackendError("Directory exists", backend=self.name, path=normalized)
        if parents:
            parts = normalized.strip("/").split("/")
            current = ""
            for part in parts:
                current = f"{current}/{part}" if current else f"/{part}"
                self._dirs.add(current)
        self._dirs.add(normalized)

    def rmdir(self, path: str, recursive: bool = False, context: Any = None) -> None:
        normalized = _normalize_dir(path)
        if normalized not in self._dirs:
            raise NexusFileNotFoundError(normalized)
        children = {d for d in self._dirs if d != normalized and d.startswith(normalized + "/")}
        if children and not recursive:
            raise BackendError("Directory not empty", backend=self.name, path=normalized)
        self._dirs.difference_update(children)
        if normalized != "/":
            self._dirs.remove(normalized)

    def is_directory(self, path: str, context: Any = None) -> bool:
        return _normalize_dir(path) in self._dirs

    def list_dir(self, path: str, context: Any = None) -> list[str]:
        normalized = _normalize_dir(path)
        prefix = normalized if normalized == "/" else normalized + "/"
        children: set[str] = set()
        for directory in self._dirs:
            if directory == normalized or not directory.startswith(prefix):
                continue
            rest = directory[len(prefix) :].strip("/")
            if rest:
                children.add(rest.split("/", 1)[0])
        return sorted(children)

    def check_connection(self, context: Any = None) -> HandlerStatusResponse:
        return HandlerStatusResponse(success=True)


class FactoryStubBackend:
    """Minimal connector class for BackendFactory registration tests."""

    name = "stub"
    user_scoped = False
    is_connected = True
    has_root_path = False
    has_token_manager = False
    backend_features: frozenset[Any] = frozenset()

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs

    def write_content(
        self,
        content: bytes,
        content_id: str = "stub",
        *,
        offset: int = 0,
        context: Any = None,
    ) -> WriteResult:
        stored_id = content_id or "stub"
        return WriteResult(content_id=stored_id, version=stored_id, size=len(content))

    def read_content(self, content_id: str, context: Any = None) -> bytes:
        return b""

    def delete_content(self, content_id: str, context: Any = None) -> None:
        return None

    def content_exists(self, content_id: str, context: Any = None) -> bool:
        return True

    def get_content_size(self, content_id: str, context: Any = None) -> int:
        return 0

    def mkdir(
        self,
        path: str,
        parents: bool = False,
        exist_ok: bool = False,
        context: Any = None,
    ) -> None:
        return None

    def rmdir(self, path: str, recursive: bool = False, context: Any = None) -> None:
        return None

    def is_directory(self, path: str, context: Any = None) -> bool:
        return False

    def check_connection(self, context: Any = None) -> HandlerStatusResponse:
        return HandlerStatusResponse(success=True)

    def has_feature(self, feature: Any) -> bool:
        return False
```

- [ ] **Step 5: Implement assertion helpers**

Create `tests/testkit/assertions.py`:

```python
"""Common assertions for Nexus tests."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from nexus.contracts.exceptions import MissingDependencyError


def _value(obj: Any, *names: str) -> Any:
    for name in names:
        if isinstance(obj, Mapping) and name in obj:
            return obj[name]
        if hasattr(obj, name):
            return getattr(obj, name)
    return None


def assert_missing_dependency_error(
    err: MissingDependencyError,
    *,
    backend: str,
    count: int | None = None,
    missing_names: tuple[str, ...] = (),
    install_hints: tuple[str, ...] = (),
) -> None:
    """Assert structured and rendered details on `MissingDependencyError`."""

    assert err.backend == backend, f"expected backend {backend!r}, got {err.backend!r}"
    if count is not None:
        assert len(err.missing) == count, f"expected {count} missing deps, got {len(err.missing)}"

    message = str(err)
    for name in missing_names:
        assert name in message, f"missing dependency name {name!r} not found in {message!r}"
    for hint in install_hints:
        assert hint in message, f"install hint {hint!r} not found in {message!r}"


def assert_event_payload(
    event: Any,
    *,
    event_type: str | None = None,
    path: str | None = None,
    zone_id: str | None = None,
) -> None:
    """Assert common event payload fields on dict-like or object-like events."""

    if event_type is not None:
        actual_type = _value(event, "event_type", "type")
        assert actual_type == event_type, f"expected event type {event_type!r}, got {actual_type!r}"
    if path is not None:
        actual_path = _value(event, "path")
        assert actual_path == path, f"expected event path {path!r}, got {actual_path!r}"
    if zone_id is not None:
        actual_zone = _value(event, "zone_id", "zone")
        assert actual_zone == zone_id, f"expected event zone {zone_id!r}, got {actual_zone!r}"


def assert_metadata_contains(actual: Mapping[str, Any], expected: Mapping[str, Any]) -> None:
    """Assert that metadata contains an expected subset."""

    for key, value in expected.items():
        assert key in actual, f"metadata missing key {key!r}"
        assert actual[key] == value, f"metadata {key!r}: expected {value!r}, got {actual[key]!r}"


def assert_permission_decision(decision: Any, *, allowed: bool) -> None:
    """Assert allow/deny state on bool or object decisions."""

    actual = decision if isinstance(decision, bool) else _value(decision, "allowed", "allow")
    assert actual is allowed, f"permission decision expected allowed={allowed!r}, got {actual!r}"
```

- [ ] **Step 6: Run backend/assertion tests to verify GREEN**

Run:

```bash
pytest tests/unit/testkit/test_backends.py tests/unit/testkit/test_assertions.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit backend/assertion slice**

Run:

```bash
git add tests/testkit/backends.py tests/testkit/assertions.py tests/unit/testkit/test_backends.py tests/unit/testkit/test_assertions.py
git commit -m "test: add backend fakes and testkit assertions"
```

Expected: commit succeeds.

---

### Task 4: Migrate Factory Dependency Check Suite

**Files:**
- Modify: `tests/integration/backends/test_factory_dep_check.py`

- [ ] **Step 1: Run existing suite before migration**

Run:

```bash
pytest tests/integration/backends/test_factory_dep_check.py -q
```

Expected: PASS before the refactor.

- [ ] **Step 2: Replace local stub and dependency assertions with testkit imports**

Modify `tests/integration/backends/test_factory_dep_check.py` to this content:

```python
"""Integration test: BackendFactory.create() raises MissingDependencyError.

Covers Issue #3830 - typed runtime-dep check at mount time.
"""

from __future__ import annotations

from typing import Any

import pytest

from nexus.backends.base.factory import BackendFactory
from nexus.backends.base.registry import ConnectorRegistry, register_connector
from nexus.backends.base.runtime_deps import BinaryDep, PythonDep
from nexus.contracts.exceptions import MissingDependencyError
from testkit.assertions import assert_missing_dependency_error
from testkit.backends import FactoryStubBackend


@pytest.fixture(autouse=True)
def _clean_registry() -> Any:
    # Snapshot + restore so we do not affect other tests in the integration run.
    names_before = set(ConnectorRegistry.list_available())
    yield
    for nm in set(ConnectorRegistry.list_available()) - names_before:
        ConnectorRegistry._base._items.pop(nm, None)


class TestFactoryDepCheck:
    def test_satisfied_deps_allow_instantiation(self) -> None:
        @register_connector(
            "stub_ok",
            runtime_deps=(PythonDep("json"),),  # stdlib - always present
        )
        class _OK(FactoryStubBackend):
            pass

        instance = BackendFactory.create("stub_ok", {})
        assert isinstance(instance, _OK)

    def test_missing_python_dep_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Force slim-install hint formatting. Otherwise the raw module
        # name is emitted under the monorepo's full distribution.
        monkeypatch.setattr(
            "nexus.backends.base.runtime_deps._nexus_fs_extras_available",
            lambda: True,
        )

        @register_connector(
            "stub_missing_py",
            runtime_deps=(PythonDep("definitely_not_a_real_module_xyz", extras=("gcs",)),),
        )
        class _M(FactoryStubBackend):
            pass

        with pytest.raises(MissingDependencyError) as exc_info:
            BackendFactory.create("stub_missing_py", {})

        assert_missing_dependency_error(
            exc_info.value,
            backend="stub_missing_py",
            count=1,
            missing_names=("definitely_not_a_real_module_xyz",),
            install_hints=("pip install nexus-fs[gcs]",),
        )

    def test_missing_binary_dep_raises(self) -> None:
        @register_connector(
            "stub_missing_bin",
            runtime_deps=(BinaryDep("definitely_not_a_real_binary_xyz", "brew install xyz"),),
        )
        class _M(FactoryStubBackend):
            pass

        with pytest.raises(MissingDependencyError) as exc_info:
            BackendFactory.create("stub_missing_bin", {})

        assert_missing_dependency_error(
            exc_info.value,
            backend="stub_missing_bin",
            count=1,
            missing_names=("definitely_not_a_real_binary_xyz",),
            install_hints=("brew install xyz",),
        )

    def test_all_missing_enumerated_together(self) -> None:
        @register_connector(
            "stub_many_missing",
            runtime_deps=(
                PythonDep("definitely_not_a_real_module_xyz", extras=("gws",)),
                BinaryDep("definitely_not_a_real_binary_xyz", "brew install xyz"),
            ),
        )
        class _M(FactoryStubBackend):
            pass

        with pytest.raises(MissingDependencyError) as exc_info:
            BackendFactory.create("stub_many_missing", {})

        assert_missing_dependency_error(
            exc_info.value,
            backend="stub_many_missing",
            count=2,
            missing_names=(
                "definitely_not_a_real_module_xyz",
                "definitely_not_a_real_binary_xyz",
            ),
        )
```

- [ ] **Step 3: Run migrated suite to verify GREEN**

Run:

```bash
pytest tests/integration/backends/test_factory_dep_check.py -q
```

Expected: PASS.

- [ ] **Step 4: Run all testkit tests with migrated consumer**

Run:

```bash
pytest tests/unit/testkit tests/unit/backends/test_profile_matrix.py tests/integration/backends/test_factory_dep_check.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit migrated suite**

Run:

```bash
git add tests/integration/backends/test_factory_dep_check.py
git commit -m "test: migrate backend dep checks to testkit"
```

Expected: commit succeeds.

---

### Task 5: Testkit Documentation

**Files:**
- Create: `docs/development/testkit.md`

- [ ] **Step 1: Create documentation directory**

Run:

```bash
mkdir -p docs/development
```

Expected: directory exists.

- [ ] **Step 2: Add testkit usage guide**

Create `docs/development/testkit.md`:

```markdown
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
```

- [ ] **Step 3: Run documentation diff check**

Run:

```bash
git diff --check -- docs/development/testkit.md
```

Expected: no output and exit code 0.

- [ ] **Step 4: Commit docs**

Run:

```bash
git add docs/development/testkit.md
git commit -m "docs: add nexus testkit guide"
```

Expected: commit succeeds.

---

### Task 6: Final Verification

**Files:**
- Verify all files changed by Tasks 1-5.

- [ ] **Step 1: Run targeted testkit verification**

Run:

```bash
pytest tests/unit/testkit -q
```

Expected: PASS.

- [ ] **Step 2: Run backend profile matrix consumer**

Run:

```bash
pytest tests/unit/backends/test_profile_matrix.py -q
```

Expected: PASS.

- [ ] **Step 3: Run migrated backend dependency suite**

Run:

```bash
pytest tests/integration/backends/test_factory_dep_check.py -q
```

Expected: PASS.

- [ ] **Step 4: Run combined target set**

Run:

```bash
pytest tests/unit/testkit tests/unit/backends/test_profile_matrix.py tests/integration/backends/test_factory_dep_check.py -q
```

Expected: PASS.

- [ ] **Step 5: Run whitespace and status checks**

Run:

```bash
git diff --check
git status --short --branch
```

Expected: `git diff --check` exits 0. `git status --short --branch` shows the current branch and no unstaged changes after all commits.

---

## Spec Coverage Checklist

- `tests/testkit` package: Task 1 creates the package and Task 2/3 fill modules.
- Fake/in-memory backend helpers: Task 3 adds `InMemoryBackend`, `FactoryStubBackend`, and helper re-exports.
- Fake auth/profile providers: Task 2 adds auth builders; Task 1 adds profile cases.
- Profile matrix used in backend suite: Task 1 creates `tests/unit/backends/test_profile_matrix.py`.
- Container fixture helpers: Task 2 adds env/probe/skip helpers without optional client imports.
- Assertion helpers: Task 3 adds dependency, event, metadata, and permission assertions.
- Representative migration: Task 4 migrates `tests/integration/backends/test_factory_dep_check.py`.
- Documentation: Task 5 adds `docs/development/testkit.md`.
- Optional dependency import rule: Task 2 uses only stdlib socket/env parsing and pytest.
