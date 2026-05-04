# Nexus Testkit Design

## Context

Issue #3968 asks for a reusable Nexus testkit for unit, integration, and e2e tests. The current test support code is useful but scattered:

- `tests/helpers/*` contains reusable doubles such as `FailingBackend`, `FailingMetastore`, `InMemoryRecordStore`, `InMemoryNexusFS`, context constants, edge-case data, and websocket doubles.
- `tests/conftest.py` owns `make_test_nexus`, which is imported directly by tests even though `conftest.py` is also pytest configuration.
- `tests/unit/conftest.py`, `tests/e2e/conftest.py`, and many suite-local files define fixtures that overlap in purpose: database isolation, record stores, server clients, containers, operation contexts, and profile-specific boot helpers.
- Profile coverage is currently ad hoc. Individual suites test `slim`, `sandbox`, `remote`, and server behavior, but there is no shared matrix API for adding cross-profile coverage safely.

The selected scope is a full migration: establish `tests/testkit` as the canonical package, update internal imports broadly, and keep compatibility wrappers for old paths.

## Goals

- Create a canonical `tests.testkit` package for fake backends, fake metadata/storage stores, auth/context fixtures, profile matrices, container fixtures, and assertion helpers.
- Move reusable helper implementations into `tests/testkit` without breaking existing tests or external references that import old helper paths.
- Migrate internal test imports from `tests.helpers.*` and `tests.conftest.make_test_nexus` to `tests.testkit.*`.
- Add a profile matrix helper and use it in at least one connector/backend test suite.
- Document how new tests should use fake backends, profile matrices, and container fixtures.
- Keep optional production/runtime dependencies lazy. Import Docker, Redis, NATS, Postgres clients, or connector-specific dependencies only inside helper functions that explicitly need them.

## Non-Goals

- Do not publish a separate PyPI package in this pass.
- Do not rewrite existing e2e tests around a new orchestration framework.
- Do not remove compatibility imports under `tests.helpers` or `tests.conftest`; old paths should keep working while internal tests migrate to the canonical package.
- Do not add mandatory Docker, Postgres, Redis/Dragonfly, NATS, or connector dependency imports during normal unit-test collection.

## Architecture

Create `tests/testkit` with small modules grouped by responsibility:

- `tests/testkit/__init__.py`: public convenience exports for the most common helpers.
- `tests/testkit/backends.py`: backend doubles and failure probes, starting with `FailingBackend`.
- `tests/testkit/metadata.py`: metastore and VFS-backed store doubles, including `DictMetastore`, `FailingMetastore`, `MetastoreError`, and `InMemoryNexusFS`.
- `tests/testkit/records.py`: record-store fixtures, starting with `InMemoryRecordStore`.
- `tests/testkit/auth.py`: shared `OperationContext` constants and context factory helpers.
- `tests/testkit/nexus_factory.py`: `make_test_nexus` and any direct NexusFS construction helpers.
- `tests/testkit/profiles.py`: profile matrix definitions and pytest-param helpers.
- `tests/testkit/containers.py`: lazy container/service helpers for Postgres, Redis/Dragonfly, NATS, and server smoke tests.
- `tests/testkit/assertions.py`: assertion helpers for events, metadata, permissions, and dependency failures.
- `tests/testkit/edge_cases.py`: path and content edge-case datasets.
- `tests/testkit/websocket.py`: `MockWebSocket`.
- `tests/testkit/fixtures.py`: optional pytest fixtures that can be imported or re-exported by suite `conftest.py` files.

Keep `tests/helpers/*` as thin wrappers that import and re-export the matching symbols from `tests.testkit`. Keep `tests/conftest.py` focused on pytest process configuration and compatibility re-export of `make_test_nexus`.

## Public API

The canonical imports for new tests are:

```python
from tests.testkit import (
    DictMetastore,
    FailingBackend,
    InMemoryNexusFS,
    InMemoryRecordStore,
    TEST_ADMIN_CONTEXT,
    TEST_CONTEXT,
    make_test_nexus,
)
```

More specific imports are also supported:

```python
from tests.testkit.profiles import profile_matrix, pytest_profile_params
from tests.testkit.containers import postgres_service, redis_service, nats_service
from tests.testkit.assertions import assert_permission_denied, assert_dependency_failure
```

Compatibility wrappers preserve existing import paths:

```python
from tests.helpers.dict_metastore import DictMetastore
from tests.helpers.in_memory_record_store import InMemoryRecordStore
from tests.conftest import make_test_nexus
```

The compatibility wrappers should contain no logic beyond imports and `__all__`. That keeps implementation ownership in `tests/testkit` while reducing migration risk.

## Profile Matrix Design

`tests/testkit/profiles.py` defines an explicit profile enum-like data model for test coverage:

```python
from dataclasses import dataclass
from typing import Any

@dataclass(frozen=True)
class TestProfile:
    name: str
    config: dict[str, Any]
    requires_server: bool = False
    requires_remote: bool = False
    requires_federation: bool = False
    reason: str | None = None
```

The first supported matrix should include:

- `slim`: local, fast, no optional services.
- `sandbox`: local, zero external services.
- `embedded`: local process with durable local data paths.
- `server`: marked as requiring a live server fixture.
- `remote`: marked as requiring a remote URL/API key.
- `federation`: marked as requiring federation/docker setup.

`pytest_profile_params(*names, include_unavailable=False)` returns `pytest.param` values with stable IDs. Profiles that require unavailable services should be skipped with explicit reasons unless a test opts into them and provides the needed fixture. This makes profile expansion safe for default local and CI runs.

The first migrated use should be a small backend or connector suite that already exercises profile-dependent behavior and does not need external services. A good initial target is a unit-level backend/connector test that can run `slim` and `sandbox`, with server/remote/federation params skipped by default.

## Container Fixture Design

`tests/testkit/containers.py` should expose lazy helpers, not autouse fixtures:

- `postgres_service(...)`
- `redis_service(...)`
- `nats_service(...)`
- `server_smoke_service(...)`

Each helper should:

- Check whether the required service is reachable or can be started.
- Import optional libraries inside the helper body.
- Return a small dataclass with URLs, credentials, cleanup callbacks, and service metadata.
- Skip with a clear pytest reason when dependencies or services are unavailable.
- Avoid mutating global environment variables unless used through a context manager fixture that restores them.

This keeps normal unit-test collection independent from Docker and network service dependencies.

## Assertion Helpers

`tests/testkit/assertions.py` should start with small, behavior-oriented helpers:

- `assert_metadata_contains(metadata, **expected)`
- `assert_permission_denied(exc_info_or_response)`
- `assert_dependency_failure(exc_info_or_response, dependency_name)`
- `assert_event_matches(event, *, path=None, event_type=None, zone_id=None)`

The helpers should avoid hiding large behavior checks. They should make repeated structural assertions clearer, especially around error shape, metadata shape, permissions, and event payloads.

## Migration Strategy

1. Create `tests/testkit` and copy the implementation code from `tests/helpers` and root `make_test_nexus`.
2. Replace `tests/helpers/*` with compatibility wrappers.
3. Update root and suite `conftest.py` files to import canonical testkit helpers where practical.
4. Use a mechanical import rewrite for internal tests:
   - `tests.helpers.dict_metastore` -> `tests.testkit.metadata`
   - `tests.helpers.failing_backend` -> `tests.testkit.backends`
   - `tests.helpers.failing_metastore` -> `tests.testkit.metadata`
   - `tests.helpers.in_memory_record_store` -> `tests.testkit.records`
   - `tests.helpers.inmemory_nexus_fs` -> `tests.testkit.metadata`
   - `tests.helpers.test_context` -> `tests.testkit.auth`
   - `tests.helpers.edge_cases` -> `tests.testkit.edge_cases`
   - `tests.helpers.mock_websocket` and bare `helpers.mock_websocket` -> `tests.testkit.websocket`
   - `tests.conftest import make_test_nexus` -> `tests.testkit import make_test_nexus`
5. Add profile matrix tests and update one backend/connector suite to use the shared matrix.
6. Add documentation at `docs/contributing/testing-testkit.md` and reference it from `tests/unit/README.md` if that file still exists on the implementation branch.
7. Run focused tests after each migration slice, then broader affected suites.

## Testing Plan

Use TDD for new behavior:

- Add failing tests for canonical imports and compatibility wrappers before moving implementation.
- Add failing tests proving `tests.testkit.profiles` returns stable pytest params and skips unavailable service profiles.
- Add failing tests proving `tests.testkit.containers` does not import optional service dependencies at module import time.
- Add failing tests for any new assertion helper behavior.

Verification commands for the implementation plan should include:

```bash
uv run pytest tests/unit/storage/test_dict_metastore.py -q -n0
uv run pytest tests/unit/core/test_nexus_fs_read_batch.py tests/unit/core/test_nexus_fs_write_batch.py -q -n0
uv run pytest tests/unit/backends -q -n0
uv run pytest tests/integration/connectors -q -n0
uv run ruff check tests/testkit tests/helpers tests/conftest.py
```

Broader suite commands may be needed after the import migration, but the implementation plan should start with focused slices so failures identify the migration boundary.

## Risks And Mitigations

- Import churn can create broad failures. Mitigation: keep compatibility wrappers and use mechanical rewrites with focused test runs.
- `conftest.py` imports are special under pytest. Mitigation: keep the root `make_test_nexus` compatibility re-export until all internal imports have moved.
- Optional dependency imports can slow or break collection. Mitigation: add explicit tests that import `tests.testkit.containers` without Docker, Redis, NATS, or Postgres service clients being imported eagerly.
- Profile names may not exactly match deployment profile names in production code. Mitigation: keep test profile metadata explicit and map each test profile to a concrete config dictionary instead of relying on string inference.
- Full migration touches many files. Mitigation: split implementation into independent tasks by helper family and test suite group.

## Approval State

The user selected the full-migration scope and approved continuing with this design direction on May 1, 2026.
