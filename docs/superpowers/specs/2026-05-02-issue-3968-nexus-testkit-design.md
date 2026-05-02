# Nexus Testkit Package Design (#3968)

**Issue:** [#3968](https://github.com/nexi-lab/nexus/issues/3968) - test: create Nexus testkit for fake backends, bricks, and profile matrices
**Date:** 2026-05-02
**Status:** design approved in chat for an incremental first slice

## Context

The test tree already has useful reusable pieces, but they are scattered:

- `tests/helpers/` contains in-memory filesystem, metastore, record-store, context, websocket, edge-case, and failure helpers.
- Root and nested `conftest.py` files provide server, database, auth, and e2e fixtures.
- Backend and connector suites define local fake implementations inline, such as the contract-test `_MockBackend` and factory-dependency `_StubBackend`.
- Profile behavior is tested directly against `DeploymentProfile`, but there is no reusable matrix object for tests that need to run across embedded, lite, sandbox, full, cloud, remote, and cluster/federation-like cases.
- Optional-service tests probe Postgres, Redis/Dragonfly, NATS, and server endpoints independently.

The first testkit slice should create a stable home for common test doubles and matrices without moving the entire test tree at once.

## Goals

- Define `tests/testkit` as the reusable pytest-only package for Nexus tests.
- Provide fake/in-memory backends, auth contexts, profile matrices, optional-service helpers, and common assertions.
- Reuse or re-export existing helpers where they are already correct.
- Migrate one representative backend or connector test suite to prove the new package API.
- Document how new tests should use fake backends, profile matrices, and optional-service helpers.
- Keep testkit imports lightweight: no production-only optional dependencies at module import time.

## Non-Goals

- Moving every helper from `tests/helpers` in this first branch.
- Publishing `nexus.testing` or any installed production-facing testkit API.
- Introducing `testcontainers` or Docker Python clients as required test dependencies.
- Rewriting e2e service fixtures wholesale.
- Changing production behavior outside of imports needed to support tests.

## Decisions

| Decision | Choice |
|---|---|
| Package location | `tests/testkit`, imported as `testkit` through existing pytest `pythonpath = ["tests"]` |
| Migration strategy | Additive compatibility first; migrate one representative suite |
| Existing helpers | Re-export stable helpers instead of duplicating them |
| Optional services | Probe URLs/env lazily and skip with explicit reasons |
| Profile matrix | Explicit case dataclasses plus pytest param helpers |
| Container helpers | Config/probe helpers only, with no unconditional Docker or client imports |

## Architecture

`tests/testkit` is a pytest-only package with small modules grouped by testing concern.

```
tests/testkit/
├── __init__.py
├── backends.py
├── auth.py
├── profiles.py
├── containers.py
└── assertions.py
```

### `testkit.backends`

Responsibilities:

- Export a reusable in-memory backend implementing the common `Backend` contract methods used by unit and integration tests.
- Export the existing `FailingBackend` wrapper from `tests.helpers.failing_backend`.
- Export existing lightweight helpers such as `DictMetastore`, `InMemoryNexusFS`, and `InMemoryRecordStore` through stable names.
- Provide a minimal connector/backend stub usable for factory and runtime-dependency tests.

The in-memory backend should avoid optional cloud libraries. It may import core Nexus backend contracts because tests already depend on the local package under test.

### `testkit.auth`

Responsibilities:

- Export `TEST_CONTEXT` and `TEST_ADMIN_CONTEXT` from `tests.helpers.test_context`.
- Provide small builders for operation contexts with custom `user_id`, groups, admin state, and zone-like metadata when tests need variants.
- Keep auth fixtures as pure data builders; do not import FastAPI auth routers or OAuth provider SDKs at module import time.

### `testkit.profiles`

Responsibilities:

- Define `ProfileCase`, an explicit dataclass that describes a deployment profile test case.
- Provide matrix constructors for:
  - local lightweight profiles: `embedded`, `lite`, `sandbox`, `full`
  - remote/client profile: `remote`
  - server/cloud profile: `cloud`
  - federation-capable profile: `cluster`
- Provide helpers that convert cases into `pytest.param(...)` with stable IDs and skip marks when a profile requires optional external services.

Shape:

```python
@dataclass(frozen=True)
class ProfileCase:
    profile: DeploymentProfile
    id: str
    expected_bricks: frozenset[str]
    expected_drivers: frozenset[str]
    external_services: tuple[str, ...] = ()
    marks: tuple[pytest.MarkDecorator, ...] = ()

    def param(self) -> pytest.ParameterSet:
        return pytest.param(self, id=self.id, marks=list(self.marks))
```

Tests should be able to select a subset without understanding the full profile hierarchy:

```python
@pytest.mark.parametrize("case", local_profile_params())
def test_enabled_bricks(case: ProfileCase) -> None:
    assert case.expected_bricks == case.profile.default_bricks()
```

### `testkit.containers`

Responsibilities:

- Centralize optional-service probing and config construction for Postgres, Redis/Dragonfly, NATS, and server smoke tests.
- Read URLs from existing environment conventions:
  - Postgres: `NEXUS_DATABASE_URL`, `POSTGRES_URL`, `DATABASE_URL`
  - Redis/Dragonfly: `NEXUS_DRAGONFLY_URL`, `REDIS_URL`, `NEXUS_DRAGONFLY_COORDINATION_URL`
  - NATS: `NEXUS_NATS_URL`
- Provide skip helpers with clear reasons.
- Provide simple network port probes and env-derived config dictionaries.

This module must not import `psycopg2`, `asyncpg`, `redis`, `nats`, `docker`, or `testcontainers` at import time. Tests that need a real client import the client inside the test or fixture after the helper says the service is available.

### `testkit.assertions`

Responsibilities:

- Provide high-signal assertions for common patterns:
  - missing dependency errors include backend name, missing item names, and install hints
  - event payloads include expected path, event type, and zone fields
  - metadata dictionaries include expected keys and values without requiring exact equality
  - permission decisions match expected allow/deny state
- Keep assertions generic and dependency-light.

## Data Flow

1. A test imports a focused testkit helper, for example `from testkit.profiles import local_profile_params`.
2. The helper returns plain Python objects or pytest parameter sets.
3. The test passes those objects into production code or existing pytest fixtures.
4. Optional-service helpers check environment variables and network reachability before the test imports optional client packages or starts service-specific setup.
5. Compatibility re-exports keep existing helpers available while new tests move to the clearer `testkit` namespace.

## Error Handling

- Optional-service helpers return or raise pytest skips with explicit service names and required environment variables.
- Missing dependency assertion helpers should compare structured fields on `MissingDependencyError` where available, then check the message for install hints.
- Profile matrices should fail fast if a case's expected bricks or drivers drift from `DeploymentProfile.default_bricks()` or `.default_drivers()` in the testkit's own tests.
- Testkit modules should avoid broad side effects at import time. Environment reads are allowed only in helper functions.

## Migration Plan

The first implementation migrates one representative suite, preferably `tests/integration/backends/test_factory_dep_check.py`, because it already has a local connector stub and dependency assertions that belong in the testkit. The migrated suite should:

- Import the reusable stub backend from `testkit.backends`.
- Import a dependency assertion helper from `testkit.assertions`.
- Use the new testkit API without changing the tested production behavior.

The first implementation should also add a backend-suite consumer for the profile matrix at `tests/unit/backends/test_profile_matrix.py`. That test should import `testkit.profiles` and assert each `ProfileCase` mirrors `DeploymentProfile.default_drivers()` and `default_bricks()`. Keeping this under `tests/unit/backends` satisfies the connector/backend-suite acceptance criterion without forcing unrelated connector tests to grow profile concerns.

Existing `tests/helpers` modules remain in place. Follow-up migrations can move backend contract fakes, event-bus helpers, server smoke helpers, and duplicated auth fixtures in smaller branches.

## Documentation

Add `docs/development/testkit.md` with:

- Purpose and package boundary.
- Fake backend example.
- Auth context example.
- Profile matrix example.
- Optional-service probe and skip example.
- Migration guidance from `tests/helpers` to `testkit`.
- Rule that testkit modules must not import optional production dependencies at module import time.

## Testing Strategy

Add focused tests for the testkit itself:

- `tests/unit/testkit/test_profiles.py` verifies matrix cases mirror `DeploymentProfile` defaults and produce stable pytest IDs.
- `tests/unit/testkit/test_containers.py` verifies env-derived URLs, probe result shapes, and skip reasons using monkeypatching.
- `tests/unit/testkit/test_assertions.py` verifies dependency assertion helpers catch wrong backend names, missing item names, and install hints.
- `tests/unit/backends/test_profile_matrix.py` verifies a backend-suite consumer can use the profile matrix.
- The migrated backend/connector dependency suite proves real consumers can use backend fakes and assertions from the package.

Targeted verification commands:

```bash
pytest tests/unit/testkit -q
pytest tests/unit/backends/test_profile_matrix.py -q
pytest tests/integration/backends/test_factory_dep_check.py -q
```

## Acceptance Criteria Mapping

| Acceptance criterion | Design coverage |
|---|---|
| Define `tests/testkit` or package equivalent | Create `tests/testkit` package |
| Migrate existing helper implementations into testkit without breaking current tests | Re-export existing helpers and migrate one representative suite |
| Add profile matrix helper and use it in at least one connector/backend test suite | Provide `testkit.profiles`; use it in `tests/unit/backends/test_profile_matrix.py` |
| Add docs for fake backends/profile matrices | Add `docs/development/testkit.md` |
| Avoid production-only optional deps unless requested | Lazy optional-service imports and module-level dependency rule |

## Risks

- A broad migration would produce noisy diffs and merge conflicts. The first slice intentionally avoids that.
- Re-exporting helpers from `tests.helpers` can preserve legacy names longer than ideal. This is acceptable because follow-up migrations can retire compatibility exports once callers move.
- Profile expectations can drift when `DeploymentProfile` changes. Testkit tests should assert each case mirrors production defaults so drift is caught quickly.
