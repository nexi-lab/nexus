# Issue #3778 — SANDBOX Deployment Profile Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `NEXUS_PROFILE=sandbox` deployment tier that boots with zero external services (SQLite + in-memory LRU + BM25S + local disk), exposes MCP + `/health` + `/api/v2/features` only, and falls back to keyword search (with a `semantic_degraded=true` flag) when federated semantic is unreachable.

**Architecture:** New `DeploymentProfile.SANDBOX` enum member sits as a proper subset of `FULL`. Profile selects defaults via `_apply_sandbox_defaults()` in config.py and a new `"inmem"` option in the existing cache factory. Route-level allowlist filters the FastAPI app for sandbox mode. Federated search gains a degraded-fallback path. One Dockerfile produces two image tags via `ARG NEXUS_PROFILE_EXTRAS`.

**Tech Stack:** Python 3.11+, SQLite, `bm25s`, `cachetools`, FastAPI, pydantic v2, existing `nexus.contracts.cache_store.InMemoryCacheStore`, existing `nexus.bricks.search` stack (txtai backend remains available but off by default in SANDBOX).

**Spec:** [`docs/superpowers/specs/2026-04-17-3778-sandbox-profile-design.md`](../specs/2026-04-17-3778-sandbox-profile-design.md) (commits `d41922cff`, `6b77e4be7`).

**Issue:** [nexi-lab/nexus#3778](https://github.com/nexi-lab/nexus/issues/3778).

---

## File Structure

### New files

| Path | Responsibility |
|---|---|
| `tests/unit/core/test_sandbox_profile.py` | SANDBOX enum + brick set + perf tuning unit tests |
| `tests/unit/test_config_sandbox.py` | `_apply_sandbox_defaults` defaults + override-wins tests |
| `tests/unit/cache/test_cache_factory_inmem.py` | `"inmem"` cache backend selection tests |
| `tests/unit/bricks/search/test_federated_degraded.py` | Federation-unreachable → degraded flag tests |
| `tests/unit/server/test_sandbox_route_allowlist.py` | Route-level allowlist filter unit test |
| `tests/integration/test_sandbox_boot.py` | Integration: boot w/ zero external services, <5s |
| `tests/integration/test_sandbox_memory.py` | Memory benchmark, marker-gated |
| `tests/e2e/self_contained/test_sandbox_mcp.py` | MCP stdio sandbox smoke |
| `docs/deployment/sandbox-profile.md` | User-facing docs |

### Modified files

| Path | Change |
|---|---|
| `src/nexus/contracts/deployment_profile.py` | Add `SANDBOX` enum + `_SANDBOX_BRICKS` + registry entry |
| `src/nexus/lib/performance_tuning.py` | Add `SANDBOX` tuning entry |
| `src/nexus/config.py` | Allow `"sandbox"` in profile validator; add `_apply_sandbox_defaults` helper; call it from loaders |
| `src/nexus/cache/settings.py` | Extend `cache_backend` Literal to include `"inmem"`; validate |
| `src/nexus/cache/factory.py` | Add `"inmem"` branch that constructs `InMemoryCacheStore` |
| `src/nexus/bricks/search/results.py` | Add `semantic_degraded: bool \| None` field to `BaseSearchResult` |
| `src/nexus/bricks/search/federated_search.py` | Define `FederationUnreachableError`; raise when all peers fail |
| `src/nexus/bricks/search/search_service.py` | Sandbox semantic path: try federation, fall back to BM25S + degraded flag |
| `src/nexus/server/fastapi_server.py` | After router includes, call `_filter_routes_for_sandbox(app)` when profile is sandbox |
| `tests/unit/core/test_deployment_profile.py` | Extend `test_valid_profiles` to include `"sandbox"` |
| `pyproject.toml` | Add `sandbox` extra |
| `Dockerfile` | Add `ARG NEXUS_PROFILE_EXTRAS=all,performance,...`; interpolate into `uv pip install` |
| `.github/workflows/docker.yml` (or equivalent) | Matrix builds `nexus:latest` + `nexus:sandbox` |

---

## Task Sequencing

Tasks 1–9 are the functional changes, TDD-ordered so each task leaves the repo green. Tasks 10–15 are packaging + tests + docs. Each task commits independently.

```
Task 1  Enum + brick set         ──┐
Task 2  Perf tuning                ├─ Profile exists
Task 3  Config validator           │
Task 4  Config defaults            │
Task 5  Cache "inmem" settings     ├─ Boot-zero-services possible
Task 6  Cache factory inmem        │
Task 7  Search result field        ├─ Degraded flag plumbed
Task 8  Federation error + detect  │
Task 9  Search service fallback    │
Task 10 HTTP route allowlist       ├─ Sandbox HTTP surface
Task 11 pyproject extra            ├─ Packaging
Task 12 Dockerfile build-arg       │
Task 13 Integration boot test      ├─ Verification
Task 14 Memory benchmark (gated)   │
Task 15 MCP e2e + CI + docs        ┘
```

---

## Task 1: Add `DeploymentProfile.SANDBOX` enum + brick set

**Files:**
- Modify: `src/nexus/contracts/deployment_profile.py:128-225`
- Test: `tests/unit/core/test_sandbox_profile.py` (create)
- Modify: `tests/unit/core/test_deployment_profile.py:249` (add `"sandbox"` to `test_valid_profiles`)

- [ ] **Step 1: Create failing unit test file**

Create `tests/unit/core/test_sandbox_profile.py`:

```python
"""Tests for DeploymentProfile.SANDBOX (Issue #3778).

SANDBOX is the lightweight profile for agent sandboxes — boots with zero
external services (SQLite + in-mem LRU + BM25S), exposes only MCP +
/health + /api/v2/features.
"""

import pytest

from nexus.contracts.deployment_profile import (
    BRICK_EVENTLOG,
    BRICK_LLM,
    BRICK_MCP,
    BRICK_NAMESPACE,
    BRICK_OBSERVABILITY,
    BRICK_PARSERS,
    BRICK_PAY,
    BRICK_PERMISSIONS,
    BRICK_SANDBOX,
    BRICK_SEARCH,
    BRICK_WORKFLOWS,
    DeploymentProfile,
)


class TestSandboxProfileEnum:
    def test_enum_value(self) -> None:
        assert DeploymentProfile.SANDBOX == "sandbox"
        assert DeploymentProfile("sandbox") is DeploymentProfile.SANDBOX

    def test_default_bricks_includes_core(self) -> None:
        bricks = DeploymentProfile.SANDBOX.default_bricks()
        assert BRICK_EVENTLOG in bricks
        assert BRICK_NAMESPACE in bricks
        assert BRICK_PERMISSIONS in bricks
        assert BRICK_SEARCH in bricks
        assert BRICK_MCP in bricks
        assert BRICK_PARSERS in bricks

    def test_default_bricks_excludes_heavy(self) -> None:
        bricks = DeploymentProfile.SANDBOX.default_bricks()
        assert BRICK_LLM not in bricks
        assert BRICK_PAY not in bricks
        assert BRICK_SANDBOX not in bricks  # sandbox provisioning brick
        assert BRICK_WORKFLOWS not in bricks
        assert BRICK_OBSERVABILITY not in bricks

    def test_sandbox_superset_of_lite(self) -> None:
        sandbox = DeploymentProfile.SANDBOX.default_bricks()
        lite = DeploymentProfile.LITE.default_bricks()
        assert lite.issubset(sandbox)

    def test_sandbox_subset_of_full(self) -> None:
        sandbox = DeploymentProfile.SANDBOX.default_bricks()
        full = DeploymentProfile.FULL.default_bricks()
        assert sandbox.issubset(full)

    def test_sandbox_size(self) -> None:
        """SANDBOX = LITE (6) + 3 adds (SEARCH, MCP, PARSERS) = 9 bricks."""
        assert len(DeploymentProfile.SANDBOX.default_bricks()) == 9
```

- [ ] **Step 2: Run test, verify fail**

```bash
pytest tests/unit/core/test_sandbox_profile.py -v
```

Expected: FAIL with `AttributeError: SANDBOX` or `ValueError: 'sandbox' is not a valid DeploymentProfile`.

- [ ] **Step 3: Add `SANDBOX` to enum**

In `src/nexus/contracts/deployment_profile.py`, in the `DeploymentProfile` StrEnum class (around line 128), add:

```python
    SANDBOX = "sandbox"
```

Update the class docstring (line 117) to include:

```
    - sandbox: Agent sandbox (zero external services; SQLite + in-mem cache + BM25S; #3778)
```

Update module docstring (lines 13-17):

```
Profile hierarchy (superset relationship):
    slim ⊂ cluster ⊂ embedded ⊂ lite ⊂ sandbox ⊂ full ⊆ cloud
```

- [ ] **Step 4: Add `_SANDBOX_BRICKS` and registry entry**

After the `_LITE_BRICKS` definition (around line 182), add:

```python
_SANDBOX_BRICKS: frozenset[str] = _LITE_BRICKS | frozenset(
    {
        BRICK_SEARCH,
        BRICK_MCP,
        BRICK_PARSERS,
    }
)
```

Note: `BRICK_FEDERATION` is intentionally excluded. Federation is auto-detected from ZoneManager (same as FULL); no brick flag is needed or checked at boot.

In the `_PROFILE_BRICKS` dict (around line 217), add:

```python
    DeploymentProfile.SANDBOX: _SANDBOX_BRICKS,
```

- [ ] **Step 5: Run test, verify pass**

```bash
pytest tests/unit/core/test_sandbox_profile.py -v
```

Expected: all 6 tests PASS.

- [ ] **Step 6: Extend existing enum test**

In `tests/unit/core/test_deployment_profile.py:249`, change:

```python
        for p in ["slim", "embedded", "lite", "full", "cloud"]:
```

to:

```python
        for p in ["slim", "embedded", "lite", "sandbox", "full", "cloud"]:
```

- [ ] **Step 7: Run full profile test suite**

```bash
pytest tests/unit/core/test_deployment_profile.py tests/unit/core/test_sandbox_profile.py -v
```

Expected: all tests PASS.

- [ ] **Step 8: Commit**

```bash
git add src/nexus/contracts/deployment_profile.py tests/unit/core/test_sandbox_profile.py tests/unit/core/test_deployment_profile.py
git commit -m "feat(#3778): add DeploymentProfile.SANDBOX enum + brick set"
```

---

## Task 2: Add SANDBOX performance tuning

**Files:**
- Modify: `src/nexus/lib/performance_tuning.py` (around line 451, after LITE tuning)
- Test: `tests/unit/core/test_sandbox_profile.py` (append to existing file)

- [ ] **Step 1: Add failing tuning test**

Append to `tests/unit/core/test_sandbox_profile.py`:

```python
class TestSandboxTuning:
    def test_tuning_resolves(self) -> None:
        from nexus.lib.performance_tuning import resolve_profile_tuning

        tuning = resolve_profile_tuning(DeploymentProfile.SANDBOX)
        assert tuning is not None

    def test_tuning_is_small(self) -> None:
        """SANDBOX should have smaller pools than FULL."""
        from nexus.lib.performance_tuning import resolve_profile_tuning

        sandbox = resolve_profile_tuning(DeploymentProfile.SANDBOX)
        full = resolve_profile_tuning(DeploymentProfile.FULL)
        assert sandbox.concurrency.default_workers < full.concurrency.default_workers
        assert sandbox.storage.db_pool_size < full.storage.db_pool_size

    def test_tuning_disables_asyncpg_pool(self) -> None:
        """SANDBOX uses SQLite — no asyncpg pool."""
        from nexus.lib.performance_tuning import resolve_profile_tuning

        tuning = resolve_profile_tuning(DeploymentProfile.SANDBOX)
        assert tuning.pool.asyncpg_max_size == 0
```

- [ ] **Step 2: Run, verify fail**

```bash
pytest tests/unit/core/test_sandbox_profile.py::TestSandboxTuning -v
```

Expected: FAIL (KeyError or unsupported profile).

- [ ] **Step 3: Add `SANDBOX` tuning entry**

In `src/nexus/lib/performance_tuning.py`, after the LITE tuning block (around line 528), add:

```python
_SANDBOX_TUNING = ProfileTuning(
    concurrency=ConcurrencyTuning(
        default_workers=2,
        thread_pool_size=8,
        max_async_concurrency=4,
        task_runner_workers=2,
    ),
    network=NetworkTuning(
        default_http_timeout=10.0,
        webhook_timeout=5.0,
        long_operation_timeout=30.0,
    ),
    storage=StorageTuning(
        write_buffer_flush_ms=100,
        write_buffer_max_size=50,
        changelog_chunk_size=100,
        db_pool_size=2,
        db_max_overflow=2,
    ),
    search=SearchTuning(
        grep_parallel_workers=2,
        list_parallel_workers=2,
        search_max_concurrency=2,
        vector_pool_workers=0,  # no local vector backend
    ),
    cache=CacheTuning(
        tiger_max_workers=1,
        tiger_batch_size=20,
    ),
    # Reuse LITE values for remaining slices
    background_task=_LITE_TUNING.background_task,
    resiliency=_LITE_TUNING.resiliency,
    connector=_LITE_TUNING.connector,
    pool=PoolTuning(
        asyncpg_min_size=0,
        asyncpg_max_size=0,  # SQLite, no asyncpg
        httpx_max_connections=10,
        remote_pool_maxsize=10,
    ),
    eviction=_LITE_TUNING.eviction,
    qos=_LITE_TUNING.qos,
)
```

And in the `resolve_profile_tuning` dispatch (lines around 704+), add the mapping:

```python
    DeploymentProfile.SANDBOX: _SANDBOX_TUNING,
```

Note: read the actual dispatch shape (dict vs if/elif) in `performance_tuning.py` near `resolve_profile_tuning` and insert `SANDBOX` in the same style.

- [ ] **Step 4: Run, verify pass**

```bash
pytest tests/unit/core/test_sandbox_profile.py::TestSandboxTuning -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/lib/performance_tuning.py tests/unit/core/test_sandbox_profile.py
git commit -m "feat(#3778): add SANDBOX performance tuning (small pools, no asyncpg)"
```

---

## Task 3: Allow `"sandbox"` in NexusConfig profile validator

**Files:**
- Modify: `src/nexus/config.py:357-376`
- Test: `tests/unit/core/test_deployment_profile.py:249` (already extended in Task 1; re-run here)

- [ ] **Step 1: Verify failing test**

Task 1 already added `"sandbox"` to `test_valid_profiles`. Run it:

```bash
pytest tests/unit/core/test_deployment_profile.py::TestNexusConfigProfile::test_valid_profiles -v
```

Expected: FAIL — `NexusConfig(profile="sandbox")` raises ValueError because validator rejects it.

- [ ] **Step 2: Extend validator**

In `src/nexus/config.py`, locate the `validate_profile` field validator (around line 357):

```python
    @field_validator("profile")
    @classmethod
    def validate_profile(cls, v: str) -> str:
        allowed = ["slim", "cluster", "embedded", "lite", "full", "cloud", "remote", "auto"]
        if v not in allowed:
            raise ValueError(f"profile must be one of {allowed}, got '{v}'")
        return v
```

Change to:

```python
    @field_validator("profile")
    @classmethod
    def validate_profile(cls, v: str) -> str:
        allowed = [
            "slim", "cluster", "embedded", "lite", "sandbox",
            "full", "cloud", "remote", "auto",
        ]
        if v not in allowed:
            raise ValueError(f"profile must be one of {allowed}, got '{v}'")
        return v
```

- [ ] **Step 3: Run, verify pass**

```bash
pytest tests/unit/core/test_deployment_profile.py::TestNexusConfigProfile -v
```

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add src/nexus/config.py
git commit -m "feat(#3778): accept 'sandbox' in NexusConfig profile validator"
```

---

## Task 4: `_apply_sandbox_defaults` — profile-gated config defaults

**Files:**
- Modify: `src/nexus/config.py` (new function; hook into `_load_from_environment` + `_load_from_dict`)
- Test: `tests/unit/test_config_sandbox.py` (create)

- [ ] **Step 1: Create failing test**

Create `tests/unit/test_config_sandbox.py`:

```python
"""Tests for SANDBOX profile config defaults (Issue #3778)."""

from pathlib import Path

import pytest

from nexus.config import NexusConfig, _apply_sandbox_defaults


class TestApplySandboxDefaults:
    def test_non_sandbox_profile_is_untouched(self) -> None:
        cfg = NexusConfig(profile="full", data_dir=None)
        result = _apply_sandbox_defaults(cfg)
        assert result.data_dir == cfg.data_dir  # unchanged
        assert result.backend == cfg.backend

    def test_sandbox_sets_local_backend_when_unset(self) -> None:
        cfg = NexusConfig(profile="sandbox", backend=None)
        result = _apply_sandbox_defaults(cfg)
        assert result.backend == "local"

    def test_sandbox_sets_data_dir(self) -> None:
        cfg = NexusConfig(profile="sandbox", data_dir=None)
        result = _apply_sandbox_defaults(cfg)
        assert result.data_dir is not None
        assert result.data_dir.endswith("nexus/sandbox") or "sandbox" in result.data_dir

    def test_sandbox_sets_sqlite_paths(self) -> None:
        cfg = NexusConfig(profile="sandbox", data_dir="/tmp/test-sandbox")
        result = _apply_sandbox_defaults(cfg)
        assert result.db_path == "/tmp/test-sandbox/nexus.db"
        assert result.metastore_path == "/tmp/test-sandbox/nexus.db"
        assert result.record_store_path == "/tmp/test-sandbox/nexus.db"

    def test_sandbox_cache_size_default(self) -> None:
        cfg = NexusConfig(profile="sandbox", cache_size_mb=None)
        result = _apply_sandbox_defaults(cfg)
        assert result.cache_size_mb == 64

    def test_sandbox_vector_search_default_off(self) -> None:
        cfg = NexusConfig(profile="sandbox", enable_vector_search=None)
        result = _apply_sandbox_defaults(cfg)
        assert result.enable_vector_search is False

    def test_explicit_user_values_win(self) -> None:
        cfg = NexusConfig(
            profile="sandbox",
            backend="gcs",
            data_dir="/custom/path",
            cache_size_mb=512,
            enable_vector_search=True,
        )
        result = _apply_sandbox_defaults(cfg)
        assert result.backend == "gcs"
        assert result.data_dir == "/custom/path"
        assert result.cache_size_mb == 512
        assert result.enable_vector_search is True
```

- [ ] **Step 2: Run, verify fail**

```bash
pytest tests/unit/test_config_sandbox.py -v
```

Expected: FAIL with ImportError for `_apply_sandbox_defaults`.

- [ ] **Step 3: Implement `_apply_sandbox_defaults`**

In `src/nexus/config.py`, after the `_load_from_environment()` function (around line 540), add:

```python
def _apply_sandbox_defaults(cfg: "NexusConfig") -> "NexusConfig":
    """Apply SANDBOX profile defaults (Issue #3778).

    When profile=sandbox, fill in unset fields with lightweight values
    (local backend, SQLite paths under ~/.nexus/sandbox/, small cache,
    no vector search). User-set values always win.

    This runs after env/YAML merge so user overrides are visible.
    """
    import os as _os
    from pathlib import Path as _Path

    if cfg.profile != "sandbox":
        return cfg

    updates: dict[str, Any] = {}

    if cfg.backend is None:
        updates["backend"] = "local"

    if cfg.data_dir is None:
        updates["data_dir"] = str(_Path.home() / ".nexus" / "sandbox")
    data_dir = updates.get("data_dir", cfg.data_dir)

    # SQLite paths default to a single file under data_dir
    db_path = f"{data_dir}/nexus.db"
    if cfg.db_path is None:
        updates["db_path"] = db_path
    if cfg.metastore_path is None:
        updates["metastore_path"] = db_path
    if cfg.record_store_path is None:
        updates["record_store_path"] = db_path

    if cfg.cache_size_mb is None:
        updates["cache_size_mb"] = 64

    if cfg.enable_vector_search is None:
        updates["enable_vector_search"] = False

    if not updates:
        return cfg

    return cfg.model_copy(update=updates)
```

- [ ] **Step 4: Hook into loaders**

In `src/nexus/config.py`:

- In `_load_from_environment()` (around line 540, just before `return NexusConfig(**env_config)`), change the return to:

```python
    return _apply_sandbox_defaults(NexusConfig(**env_config))
```

- In `_load_from_dict()` (around line 452, the final `return NexusConfig(**merged)` line), change to:

```python
    return _apply_sandbox_defaults(NexusConfig(**merged))
```

- [ ] **Step 5: Run test, verify pass**

```bash
pytest tests/unit/test_config_sandbox.py -v
```

Expected: all 7 tests PASS.

- [ ] **Step 6: Run full config test suite for regressions**

```bash
pytest tests/unit/test_config_sandbox.py tests/unit/core/test_deployment_profile.py -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/nexus/config.py tests/unit/test_config_sandbox.py
git commit -m "feat(#3778): _apply_sandbox_defaults for profile-gated config"
```

---

## Task 5: Extend `CacheSettings.cache_backend` with `"inmem"`

**Files:**
- Modify: `src/nexus/cache/settings.py:70-72, 153-170`
- Test: `tests/unit/cache/test_cache_factory_inmem.py` (create)

- [ ] **Step 1: Create failing test file**

Create `tests/unit/cache/test_cache_factory_inmem.py`:

```python
"""Tests for the 'inmem' cache backend option (Issue #3778)."""

import pytest

from nexus.cache.settings import CacheSettings


class TestInMemCacheBackend:
    def test_inmem_accepted(self) -> None:
        settings = CacheSettings(cache_backend="inmem")
        assert settings.cache_backend == "inmem"

    def test_inmem_does_not_require_dragonfly_url(self) -> None:
        settings = CacheSettings(cache_backend="inmem", dragonfly_url=None)
        # Should not raise during validate() — inmem needs nothing.
        settings.validate()

    def test_invalid_backend_still_rejected(self) -> None:
        with pytest.raises(ValueError):
            CacheSettings(cache_backend="bogus").validate()
```

- [ ] **Step 2: Run, verify fail**

```bash
pytest tests/unit/cache/test_cache_factory_inmem.py::TestInMemCacheBackend -v
```

Expected: FAIL — Pydantic/dataclass Literal rejects `"inmem"`.

- [ ] **Step 3: Extend Literal**

In `src/nexus/cache/settings.py` line 70-72, change:

```python
    cache_backend: Literal["auto", "dragonfly", "postgres"] = field(
        default_factory=lambda: os.environ.get("NEXUS_CACHE_BACKEND", "auto")  # type: ignore
    )
```

to:

```python
    cache_backend: Literal["auto", "dragonfly", "postgres", "inmem"] = field(
        default_factory=lambda: os.environ.get("NEXUS_CACHE_BACKEND", "auto")  # type: ignore
    )
```

- [ ] **Step 4: Extend validate()**

In `src/nexus/cache/settings.py` around line 164, the existing validator:

```python
        if self.cache_backend not in ("auto", "dragonfly", "postgres"):
            raise ValueError(
                f"Invalid NEXUS_CACHE_BACKEND: {self.cache_backend}. "
                ...
            )
```

Change the tuple to include `"inmem"`:

```python
        if self.cache_backend not in ("auto", "dragonfly", "postgres", "inmem"):
            raise ValueError(
                f"Invalid NEXUS_CACHE_BACKEND: {self.cache_backend}. "
                "Must be 'auto', 'dragonfly', 'postgres', or 'inmem'."
            )
```

- [ ] **Step 5: Run test, verify pass**

```bash
pytest tests/unit/cache/test_cache_factory_inmem.py::TestInMemCacheBackend -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/nexus/cache/settings.py tests/unit/cache/test_cache_factory_inmem.py
git commit -m "feat(#3778): add 'inmem' option to CacheSettings.cache_backend"
```

---

## Task 6: Cache factory wires `"inmem"` → `InMemoryCacheStore`

**Files:**
- Modify: `src/nexus/cache/factory.py:120-175` (the `initialize()` method)
- Test: `tests/unit/cache/test_cache_factory_inmem.py` (append)

- [ ] **Step 1: Append factory test**

Append to `tests/unit/cache/test_cache_factory_inmem.py`:

```python
class TestCacheFactoryInMem:
    @pytest.mark.asyncio
    async def test_inmem_backend_builds_inmemory_store(self) -> None:
        from nexus.cache.factory import CacheFactory
        from nexus.contracts.cache_store import InMemoryCacheStore

        settings = CacheSettings(cache_backend="inmem", dragonfly_url=None)
        factory = CacheFactory(settings)
        await factory.initialize()
        try:
            assert isinstance(factory._cache_store, InMemoryCacheStore)
            assert factory._has_cache_store is True
        finally:
            await factory.shutdown()

    @pytest.mark.asyncio
    async def test_inmem_backend_basic_get_set(self) -> None:
        from nexus.cache.factory import CacheFactory

        settings = CacheSettings(cache_backend="inmem")
        factory = CacheFactory(settings)
        await factory.initialize()
        try:
            store = factory._cache_store
            await store.set("k", b"v")
            assert await store.get("k") == b"v"
        finally:
            await factory.shutdown()
```

- [ ] **Step 2: Run, verify fail**

```bash
pytest tests/unit/cache/test_cache_factory_inmem.py::TestCacheFactoryInMem -v
```

Expected: FAIL — factory has no `"inmem"` branch; falls through to NullCacheStore.

- [ ] **Step 3: Add inmem branch to factory**

In `src/nexus/cache/factory.py`, inside `CacheFactory.initialize()` around line 125 (where the existing code checks `if self._settings.dragonfly_url and self._settings.cache_backend in ("auto", "dragonfly"):`), add an earlier branch:

```python
        # Issue #3778: explicit inmem backend for SANDBOX profile
        if self._settings.cache_backend == "inmem":
            from nexus.contracts.cache_store import InMemoryCacheStore

            self._cache_store = InMemoryCacheStore()
            self._has_cache_store = True
            self._initialized = True
            logger.info("Cache factory initialized with InMemoryCacheStore (SANDBOX)")
            return
```

(Place this immediately before the existing `if self._settings.dragonfly_url and ...` block so it short-circuits.)

- [ ] **Step 4: Run test, verify pass**

```bash
pytest tests/unit/cache/test_cache_factory_inmem.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/cache/factory.py tests/unit/cache/test_cache_factory_inmem.py
git commit -m "feat(#3778): cache factory wires 'inmem' to InMemoryCacheStore"
```

---

## Task 7: Add `semantic_degraded` to `BaseSearchResult`

**Files:**
- Modify: `src/nexus/bricks/search/results.py:14-52`
- Test: inline with Task 9 tests (this change is trivial; verified via Task 9)

- [ ] **Step 1: Add the field**

In `src/nexus/bricks/search/results.py`, inside the `BaseSearchResult` dataclass, add a new field after `context: str | None = None`:

```python
    semantic_degraded: bool | None = None  # Issue #3778: federation fell back to BM25S
```

(Placed last among optional fields so constructor positional args aren't affected.)

- [ ] **Step 2: Run all search tests for regressions**

```bash
pytest tests/unit/bricks/search/ -v
```

Expected: PASS (field has default `None`; no existing test should change behavior).

- [ ] **Step 3: Commit**

```bash
git add src/nexus/bricks/search/results.py
git commit -m "feat(#3778): add semantic_degraded flag to BaseSearchResult"
```

---

## Task 8: `FederationUnreachableError` + "all peers failed" detection

**Files:**
- Modify: `src/nexus/bricks/search/federated_search.py`
- Test: `tests/unit/bricks/search/test_federated_degraded.py` (create)

- [ ] **Step 1: Create failing test**

Create `tests/unit/bricks/search/test_federated_degraded.py`:

```python
"""Tests for federation-unreachable → degraded flag (Issue #3778)."""

import pytest

from nexus.bricks.search.federated_search import (
    FederatedSearchResponse,
    FederationUnreachableError,
    ZoneFailure,
)


class TestFederationUnreachableDetection:
    def test_error_class_exists(self) -> None:
        err = FederationUnreachableError("all peers down")
        assert isinstance(err, Exception)

    def test_response_with_all_failures_is_unreachable(self) -> None:
        """Helper that classifies response as unreachable when every peer failed."""
        from nexus.bricks.search.federated_search import is_all_peers_failed

        resp = FederatedSearchResponse(
            results=[],
            zones_searched=["a", "b"],
            zones_failed=[
                ZoneFailure(zone_id="a", error="timeout"),
                ZoneFailure(zone_id="b", error="connection refused"),
            ],
        )
        assert is_all_peers_failed(resp) is True

    def test_response_with_partial_failure_is_not_unreachable(self) -> None:
        from nexus.bricks.search.federated_search import is_all_peers_failed

        resp = FederatedSearchResponse(
            results=[{"path": "/x", "score": 1.0}],
            zones_searched=["a", "b"],
            zones_failed=[ZoneFailure(zone_id="b", error="timeout")],
        )
        assert is_all_peers_failed(resp) is False

    def test_response_with_zero_peers_is_unreachable(self) -> None:
        from nexus.bricks.search.federated_search import is_all_peers_failed

        resp = FederatedSearchResponse(
            results=[],
            zones_searched=[],
            zones_failed=[],
        )
        assert is_all_peers_failed(resp) is True
```

- [ ] **Step 2: Run, verify fail**

```bash
pytest tests/unit/bricks/search/test_federated_degraded.py -v
```

Expected: FAIL — `FederationUnreachableError` and `is_all_peers_failed` don't exist yet.

- [ ] **Step 3: Add error class + helper**

In `src/nexus/bricks/search/federated_search.py`, after the existing dataclass definitions (near line 94), add:

```python
class FederationUnreachableError(Exception):
    """Raised (or signaled) when federated search cannot reach any peer.

    Issue #3778: SANDBOX profile treats this as a signal to fall back to
    local BM25S and stamp results with `semantic_degraded=True`.
    """


def is_all_peers_failed(response: FederatedSearchResponse) -> bool:
    """Return True when the response reflects zero reachable peers.

    Equivalent to: zero peers configured, or every configured peer failed.
    """
    if not response.zones_searched and not response.zones_failed:
        return True
    if not response.results and len(response.zones_failed) >= len(response.zones_searched):
        return True
    return False
```

- [ ] **Step 4: Run test, verify pass**

```bash
pytest tests/unit/bricks/search/test_federated_degraded.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/bricks/search/federated_search.py tests/unit/bricks/search/test_federated_degraded.py
git commit -m "feat(#3778): FederationUnreachableError + is_all_peers_failed helper"
```

---

## Task 9: Search service — sandbox semantic fallback with degraded flag

**Files:**
- Modify: `src/nexus/bricks/search/search_service.py` (semantic path + sandbox branch)
- Test: `tests/unit/bricks/search/test_federated_degraded.py` (append)

- [ ] **Step 1: Append integration-style test**

Append to `tests/unit/bricks/search/test_federated_degraded.py`:

```python
class TestSearchServiceSandboxFallback:
    @pytest.mark.asyncio
    async def test_sandbox_all_peers_fail_returns_bm25s_with_degraded_flag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When SANDBOX + semantic query + all peers unreachable, fall back
        to BM25S with semantic_degraded=True on every result."""
        from nexus.bricks.search.search_service import SearchService
        from nexus.bricks.search.results import BaseSearchResult
        from nexus.bricks.search.federated_search import (
            FederatedSearchResponse,
            ZoneFailure,
        )

        async def _fake_federated(*args, **kwargs):
            return FederatedSearchResponse(
                results=[],
                zones_searched=["peer-a"],
                zones_failed=[ZoneFailure(zone_id="peer-a", error="timeout")],
            )

        async def _fake_bm25s(*args, **kwargs):
            return [
                BaseSearchResult(path="/a.py", chunk_text="hit", score=1.0),
            ]

        svc = SearchService.__new__(SearchService)  # construct without deps
        svc._profile = "sandbox"
        svc._federated_search = _fake_federated
        svc._bm25s_search = _fake_bm25s

        results = await svc.semantic_search(query="x", zone_id="z")

        assert len(results) == 1
        assert results[0].semantic_degraded is True

    @pytest.mark.asyncio
    async def test_sandbox_partial_peer_success_no_degraded_flag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from nexus.bricks.search.search_service import SearchService
        from nexus.bricks.search.federated_search import (
            FederatedSearchResponse,
            ZoneFailure,
        )

        async def _fake_federated(*args, **kwargs):
            return FederatedSearchResponse(
                results=[{"path": "/x.py", "score": 0.9, "chunk_text": "ok"}],
                zones_searched=["peer-a", "peer-b"],
                zones_failed=[ZoneFailure(zone_id="peer-b", error="timeout")],
            )

        svc = SearchService.__new__(SearchService)
        svc._profile = "sandbox"
        svc._federated_search = _fake_federated
        svc._bm25s_search = None  # shouldn't be called

        results = await svc.semantic_search(query="x", zone_id="z")

        assert all(r.semantic_degraded in (None, False) for r in results)

    @pytest.mark.asyncio
    async def test_sandbox_warn_only_once_per_session(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Repeated fallback should not flood the log."""
        import logging as _logging
        from nexus.bricks.search.search_service import SearchService
        from nexus.bricks.search.results import BaseSearchResult
        from nexus.bricks.search.federated_search import (
            FederatedSearchResponse,
            ZoneFailure,
        )

        async def _fake_federated(*args, **kwargs):
            return FederatedSearchResponse(
                results=[], zones_searched=["a"],
                zones_failed=[ZoneFailure(zone_id="a", error="x")],
            )

        async def _fake_bm25s(*args, **kwargs):
            return [BaseSearchResult(path="/a", chunk_text="h", score=1.0)]

        svc = SearchService.__new__(SearchService)
        svc._profile = "sandbox"
        svc._federated_search = _fake_federated
        svc._bm25s_search = _fake_bm25s
        svc._degraded_warned = False  # fresh session

        with caplog.at_level(_logging.WARNING):
            await svc.semantic_search(query="x", zone_id="z")
            await svc.semantic_search(query="x", zone_id="z")
            await svc.semantic_search(query="x", zone_id="z")

        warn_records = [r for r in caplog.records
                        if r.levelno == _logging.WARNING
                        and "semantic_degraded" in r.getMessage().lower()]
        assert len(warn_records) == 1
```

- [ ] **Step 2: Run, verify fail**

```bash
pytest tests/unit/bricks/search/test_federated_degraded.py::TestSearchServiceSandboxFallback -v
```

Expected: FAIL — `semantic_search` sandbox path doesn't exist.

- [ ] **Step 3: Implement sandbox fallback in SearchService**

In `src/nexus/bricks/search/search_service.py`, inside the `SearchService` class, add/adapt the semantic path. Add a dedicated method `semantic_search`:

```python
    async def semantic_search(self, query: str, *, zone_id: str, limit: int = 10):
        """Semantic search path. Sandbox profile delegates to federation and
        falls back to BM25S with semantic_degraded=True when no peer responds.

        Issue #3778.
        """
        from nexus.bricks.search.federated_search import is_all_peers_failed
        from nexus.bricks.search.results import BaseSearchResult

        if getattr(self, "_profile", None) == "sandbox":
            fed_resp = await self._federated_search(query, zone_id=zone_id, limit=limit)
            if not is_all_peers_failed(fed_resp):
                # Build results normally; no degraded flag.
                return [
                    BaseSearchResult(
                        path=r.get("path", ""),
                        chunk_text=r.get("chunk_text", ""),
                        score=float(r.get("score", 0.0)),
                        zone_id=r.get("zone_id"),
                    )
                    for r in fed_resp.results
                ]
            # Degraded path: log once, fall back to BM25S.
            if not getattr(self, "_degraded_warned", False):
                logger.warning(
                    "Federation unreachable; SANDBOX falling back to BM25S. "
                    "Results will carry semantic_degraded=True."
                )
                self._degraded_warned = True
            bm25s_results = await self._bm25s_search(query, zone_id=zone_id, limit=limit)
            return [
                BaseSearchResult(
                    path=r.path,
                    chunk_text=r.chunk_text,
                    score=r.score,
                    zone_id=r.zone_id,
                    semantic_degraded=True,
                )
                for r in bm25s_results
            ]

        # Non-sandbox path: delegate to existing semantic route (txtai, etc.)
        return await self._default_semantic_search(query, zone_id=zone_id, limit=limit)
```

Also add class attribute defaults in `__init__` for the three fields used above:

```python
        # Issue #3778
        self._profile = getattr(cfg, "profile", "full") if cfg is not None else "full"
        self._degraded_warned = False
```

And wire `_federated_search` / `_bm25s_search` / `_default_semantic_search` to the existing implementations — read the file's current structure and bind them in `__init__` (they already exist under different names; adapt the bindings).

- [ ] **Step 4: Run, verify pass**

```bash
pytest tests/unit/bricks/search/test_federated_degraded.py -v
```

Expected: PASS.

- [ ] **Step 5: Run full search tests**

```bash
pytest tests/unit/bricks/search/ -v
```

Expected: PASS (no regressions in non-sandbox paths).

- [ ] **Step 6: Commit**

```bash
git add src/nexus/bricks/search/search_service.py tests/unit/bricks/search/test_federated_degraded.py
git commit -m "feat(#3778): SANDBOX semantic fallback to BM25S with degraded flag"
```

---

## Task 10: FastAPI route-level allowlist for sandbox

**Files:**
- Modify: `src/nexus/server/fastapi_server.py` (add filter + invocation)
- Test: `tests/unit/server/test_sandbox_route_allowlist.py` (create)

- [ ] **Step 1: Create failing test**

Create `tests/unit/server/test_sandbox_route_allowlist.py`:

```python
"""Tests for SANDBOX route-level allowlist (Issue #3778)."""

import pytest
from fastapi import APIRouter, FastAPI
from starlette.routing import Route


class TestSandboxRouteFilter:
    def test_filter_retains_allowlisted_routes(self) -> None:
        from nexus.server.fastapi_server import _filter_routes_for_sandbox

        app = FastAPI()

        @app.get("/health")
        def _h() -> dict:
            return {"ok": True}

        @app.get("/api/v2/features")
        def _f() -> dict:
            return {}

        @app.get("/api/v2/skills/list")
        def _s() -> dict:
            return {}

        @app.get("/api/v2/pay/charge")
        def _p() -> dict:
            return {}

        _filter_routes_for_sandbox(app)

        paths = {r.path for r in app.router.routes if isinstance(r, Route)}
        assert "/health" in paths
        assert "/api/v2/features" in paths
        assert "/api/v2/skills/list" not in paths
        assert "/api/v2/pay/charge" not in paths

    def test_filter_preserves_openapi_docs(self) -> None:
        """FastAPI's built-in /openapi.json and /docs must survive."""
        from nexus.server.fastapi_server import _filter_routes_for_sandbox

        app = FastAPI()
        _filter_routes_for_sandbox(app)
        paths = {r.path for r in app.router.routes if isinstance(r, Route)}
        assert "/openapi.json" in paths

    def test_filter_idempotent(self) -> None:
        from nexus.server.fastapi_server import _filter_routes_for_sandbox

        app = FastAPI()

        @app.get("/health")
        def _h() -> dict:
            return {}

        _filter_routes_for_sandbox(app)
        before = len(app.router.routes)
        _filter_routes_for_sandbox(app)
        assert len(app.router.routes) == before
```

- [ ] **Step 2: Run, verify fail**

```bash
pytest tests/unit/server/test_sandbox_route_allowlist.py -v
```

Expected: FAIL — `_filter_routes_for_sandbox` does not exist.

- [ ] **Step 3: Implement filter**

In `src/nexus/server/fastapi_server.py`, near the top imports, add:

```python
from starlette.routing import Route as _StarletteRoute
```

Then add a module-level constant and helper (place them near the other profile-resolution code, around line 220):

```python
SANDBOX_HTTP_ALLOWLIST: frozenset[str] = frozenset(
    {
        "/health",
        "/api/v2/features",
        # FastAPI built-ins:
        "/openapi.json",
        "/docs",
        "/docs/oauth2-redirect",
        "/redoc",
    }
)


def _filter_routes_for_sandbox(app: "FastAPI") -> None:
    """Issue #3778: remove every route not in SANDBOX_HTTP_ALLOWLIST.

    Idempotent. Only affects `Route` entries (leaves `Mount` alone).
    """
    kept = []
    for r in app.router.routes:
        if isinstance(r, _StarletteRoute) and r.path not in SANDBOX_HTTP_ALLOWLIST:
            continue
        kept.append(r)
    app.router.routes = kept
```

- [ ] **Step 4: Invoke filter when profile is sandbox**

In `fastapi_server.py`, after all `app.include_router(...)` calls complete (but before `return app`), add:

```python
    # Issue #3778: SANDBOX profile restricts HTTP surface.
    if _profile_str == "sandbox":
        _filter_routes_for_sandbox(app)
```

Use the `_profile_str` variable already resolved at line 204.

- [ ] **Step 5: Run test, verify pass**

```bash
pytest tests/unit/server/test_sandbox_route_allowlist.py -v
```

Expected: PASS.

- [ ] **Step 6: Run existing server tests for regressions**

```bash
pytest tests/unit/server/ -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/nexus/server/fastapi_server.py tests/unit/server/test_sandbox_route_allowlist.py
git commit -m "feat(#3778): route-level allowlist filter for SANDBOX profile"
```

---

## Task 11: Add `sandbox` pip extra

**Files:**
- Modify: `pyproject.toml` (optional-dependencies section)

- [ ] **Step 1: Add the extra**

In `pyproject.toml`, in the `[project.optional-dependencies]` section (around line 195, near `sandbox-monty`), add a new `sandbox` extra:

```toml
sandbox = [
    # Issue #3778: SANDBOX profile — agent sandbox runtime (zero external services)
    "bm25s>=0.2",
    "cachetools>=5.0",
    "pdf-inspector",  # version matches whatever #3757 ships; pulled in when PARSERS brick is enabled
    "tokenizers>=0.15",
]
```

- [ ] **Step 2: Verify it resolves**

```bash
uv pip compile --extra=sandbox pyproject.toml -o /tmp/sandbox-reqs.txt 2>&1 | tail -20
```

Expected: no errors; resolved set does NOT include `asyncpg`, `psycopg`, `redis`, `txtai`, `sentence-transformers`, `markitdown`.

Sanity check:

```bash
grep -E "(asyncpg|psycopg|redis|txtai|sentence-transformers|markitdown)" /tmp/sandbox-reqs.txt && echo "UNEXPECTED" || echo "OK: no heavy deps"
```

Expected: `OK: no heavy deps`.

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml
git commit -m "feat(#3778): add 'sandbox' pip extra (bm25s, cachetools, pdf-inspector, tokenizers)"
```

---

## Task 12: Dockerfile build-arg for profile extras

**Files:**
- Modify: `Dockerfile` (around line 84-92)

- [ ] **Step 1: Add ARG and interpolate**

In `Dockerfile`, before the `RUN uv pip install --system` block (around line 80), add:

```dockerfile
ARG NEXUS_PROFILE_EXTRAS=all,performance,compression,monitoring,docker,event-streaming,sentry,pay
```

Change the existing install lines (85 and 88) from:

```dockerfile
uv pip install --system -i $(cat /tmp/pip_index) \
    ".[all,performance,compression,monitoring,docker,event-streaming,sentry,pay]" \
    "txtai[ann]>=9.0"; \
```

to:

```dockerfile
uv pip install --system -i $(cat /tmp/pip_index) \
    ".[${NEXUS_PROFILE_EXTRAS}]"; \
```

Same change for the `else` branch on line 88-91 (drop the `txtai` + `sentence-transformers` lines — they're only pulled in when the extras list includes them, which is true for `all` but not for `sandbox`).

Updated block:

```dockerfile
ARG NEXUS_PROFILE_EXTRAS=all,performance,compression,monitoring,docker,event-streaming,sentry,pay

RUN set -eux; \
    if [ -n "$TARGETPLATFORM" ] && [ "$TARGETPLATFORM" != "linux/amd64" ]; then \
        uv pip install --system -i $(cat /tmp/pip_index) ".[${NEXUS_PROFILE_EXTRAS}]"; \
    else \
        uv pip install --system -i $(cat /tmp/pip_index) ".[${NEXUS_PROFILE_EXTRAS}]"; \
    fi
```

(Adjust the surrounding shell logic to match the actual file — read `Dockerfile` lines 80-95 and preserve existing conditional.)

- [ ] **Step 2: Build both tags locally**

```bash
# Full image
docker build -t nexus:latest .
# Sandbox image
docker build -t nexus:sandbox --build-arg NEXUS_PROFILE_EXTRAS=sandbox .
```

- [ ] **Step 3: Assert sandbox image size**

```bash
docker image inspect nexus:sandbox --format '{{.Size}}' | awk '{print $1/1024/1024 " MB"}'
```

Expected: under ~300MB. Record the number in the commit message.

- [ ] **Step 4: Smoke-test sandbox image**

```bash
docker run --rm -d --name nexus-sandbox-test \
  -e NEXUS_PROFILE=sandbox \
  -e NEXUS_DATA_DIR=/tmp/nexus-sandbox \
  -p 8000:8000 \
  nexus:sandbox
sleep 5
curl -fsS http://localhost:8000/health
docker stop nexus-sandbox-test
```

Expected: `/health` returns 200. Container boots in <5s.

- [ ] **Step 5: Commit**

```bash
git add Dockerfile
git commit -m "feat(#3778): Dockerfile ARG NEXUS_PROFILE_EXTRAS for dual image tags"
```

---

## Task 13: Integration test — boot with zero external services

**Files:**
- Test: `tests/integration/test_sandbox_boot.py` (create)

- [ ] **Step 1: Create integration test**

Create `tests/integration/test_sandbox_boot.py`:

```python
"""Integration test: SANDBOX boots with zero external services (Issue #3778).

No PostgreSQL, no Dragonfly/Redis, no Zoekt. Uses SQLite + in-mem LRU
+ BM25S. Target: boot in <5s, /health returns 200, disabled routers 404.
"""

import time
from pathlib import Path

import httpx
import pytest

import nexus


@pytest.mark.asyncio
async def test_sandbox_boots_without_external_services(tmp_path: Path) -> None:
    """Boot nexus with profile=sandbox; no PG/Dragonfly running on host."""
    t0 = time.monotonic()
    nx = await nexus.connect(
        config={
            "profile": "sandbox",
            "data_dir": str(tmp_path / "nexus"),
        }
    )
    boot_time = time.monotonic() - t0
    try:
        assert boot_time < 5.0, f"Boot took {boot_time:.2f}s, exceeds 5s budget"

        # Basic FS op works
        nx.write("/hello.txt", b"hello")
        assert nx.sys_read("/hello.txt") == b"hello"
    finally:
        nx.close()


@pytest.mark.asyncio
async def test_sandbox_http_surface_is_restricted(tmp_path: Path) -> None:
    """HTTP surface on SANDBOX: only /health and /api/v2/features."""
    from nexus.server.fastapi_server import build_app

    import os
    os.environ["NEXUS_PROFILE"] = "sandbox"
    os.environ["NEXUS_DATA_DIR"] = str(tmp_path / "nexus")
    try:
        app = build_app()  # or whatever the app-construction function is
        async with httpx.AsyncClient(app=app, base_url="http://testserver") as client:
            r_health = await client.get("/health")
            assert r_health.status_code == 200

            r_features = await client.get("/api/v2/features")
            assert r_features.status_code == 200
            body = r_features.json()
            assert body["profile"] == "sandbox"

            r_pay = await client.get("/api/v2/pay/status")
            assert r_pay.status_code == 404

            r_skills = await client.get("/api/v2/skills/list")
            assert r_skills.status_code == 404
    finally:
        del os.environ["NEXUS_PROFILE"]
        del os.environ["NEXUS_DATA_DIR"]


@pytest.mark.asyncio
async def test_sandbox_features_endpoint_reports_enabled_bricks(tmp_path: Path) -> None:
    from nexus.server.fastapi_server import build_app

    import os
    os.environ["NEXUS_PROFILE"] = "sandbox"
    os.environ["NEXUS_DATA_DIR"] = str(tmp_path / "nexus")
    try:
        app = build_app()
        async with httpx.AsyncClient(app=app, base_url="http://testserver") as client:
            r = await client.get("/api/v2/features")
            body = r.json()
            assert body["profile"] == "sandbox"
            enabled = set(body["enabled_bricks"])
            assert {"search", "mcp", "federation", "parsers",
                    "eventlog", "namespace", "permissions"}.issubset(enabled)
            assert "llm" not in enabled
            assert "pay" not in enabled
            assert "observability" not in enabled
    finally:
        del os.environ["NEXUS_PROFILE"]
        del os.environ["NEXUS_DATA_DIR"]
```

If `build_app()` is named differently (check `fastapi_server.py`), substitute the real function name.

- [ ] **Step 2: Run integration test**

```bash
pytest tests/integration/test_sandbox_boot.py -v
```

Expected: all 3 tests PASS. Boot time asserted <5s.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/test_sandbox_boot.py
git commit -m "test(#3778): integration — SANDBOX boot, HTTP allowlist, features endpoint"
```

---

## Task 14: Memory benchmark (marker-gated)

**Files:**
- Test: `tests/integration/test_sandbox_memory.py` (create)
- Modify: `pyproject.toml` (register pytest marker)

- [ ] **Step 1: Register marker**

In `pyproject.toml`, under `[tool.pytest.ini_options]` → `markers`, add:

```toml
markers = [
    # ... existing markers ...
    "sandbox_memory: SANDBOX memory benchmark (skipped by default, run with --sandbox-memory)",
]
```

- [ ] **Step 2: Create marker-gated memory benchmark**

Create `tests/integration/test_sandbox_memory.py`:

```python
"""Memory benchmark for SANDBOX profile (Issue #3778).

Gated behind `pytest -m sandbox_memory` — skipped by default because
RSS sampling is flaky on shared CI runners.

Target: < 300MB idle RSS after booting + indexing 100 small files.
"""

from pathlib import Path

import psutil
import pytest

import nexus


@pytest.mark.sandbox_memory
@pytest.mark.asyncio
async def test_sandbox_idle_rss_under_300mb(tmp_path: Path) -> None:
    nx = await nexus.connect(
        config={"profile": "sandbox", "data_dir": str(tmp_path / "nexus")}
    )
    try:
        # Write 100 small files so indexing runs at least once
        for i in range(100):
            nx.write(f"/file-{i:03d}.txt", f"content {i} — keyword{i % 7}".encode())

        # Let background tasks settle
        import asyncio
        await asyncio.sleep(1.0)

        rss_bytes = psutil.Process().memory_info().rss
        rss_mb = rss_bytes / 1024 / 1024
        print(f"SANDBOX idle RSS: {rss_mb:.1f} MB")
        assert rss_mb < 300, f"RSS {rss_mb:.1f}MB exceeds 300MB target"
    finally:
        nx.close()
```

- [ ] **Step 3: Run (opt-in)**

```bash
pytest tests/integration/test_sandbox_memory.py -v -m sandbox_memory
```

Expected: PASS. Note the RSS number printed; include in commit.

- [ ] **Step 4: Verify it's skipped by default**

```bash
pytest tests/integration/test_sandbox_memory.py -v
```

Expected: 1 skipped.

- [ ] **Step 5: Commit**

```bash
git add tests/integration/test_sandbox_memory.py pyproject.toml
git commit -m "test(#3778): SANDBOX memory benchmark (marker-gated)"
```

---

## Task 15: MCP e2e + CI matrix + user docs

**Files:**
- Test: `tests/e2e/self_contained/test_sandbox_mcp.py` (create)
- Modify: `.github/workflows/*.yml` — CI matrix for dual image build
- Create: `docs/deployment/sandbox-profile.md`

### 15a. MCP e2e test

- [ ] **Step 1: Locate an existing MCP stdio e2e test for shape**

```bash
ls tests/e2e/self_contained/mcp/ 2>/dev/null
```

- [ ] **Step 2: Read existing MCP e2e harness**

```bash
cat tests/e2e/self_contained/mcp/conftest.py
```

The conftest provides a fixture (typically `mcp_client` or `mcp_session`) that spawns a nexus MCP subprocess and returns a JSON-RPC helper. Identify:
- The fixture name
- How it receives env vars (likely via `monkeypatch` or a parametrized fixture)
- How it returns a client object with a `call_tool(name, args)` method

- [ ] **Step 3: Create e2e test using the existing fixture**

Create `tests/e2e/self_contained/test_sandbox_mcp.py`. Use the fixture name observed in Step 2 — the template below names it `mcp_client` with a `with_env` parametrization; adapt to the real name:

```python
"""E2E: SANDBOX nexus + MCP stdio client (Issue #3778).

Uses the shared MCP e2e fixture from tests/e2e/self_contained/mcp/conftest.py.
"""

from pathlib import Path

import pytest


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_sandbox_mcp_search_returns_semantic_degraded_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mcp_client
) -> None:
    """With SANDBOX profile and no peers configured, semantic search
    must return results (BM25S fallback) with `semantic_degraded=True`."""
    monkeypatch.setenv("NEXUS_PROFILE", "sandbox")
    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path / "nexus"))

    # Write a file so BM25S has something to index
    await mcp_client.call_tool(
        "write_file", {"path": "/README.md", "content": "hello world from sandbox"}
    )

    resp = await mcp_client.call_tool(
        "search", {"query": "sandbox", "mode": "semantic", "zone_id": "default"}
    )

    assert "results" in resp
    assert len(resp["results"]) >= 1
    # Every result must carry the degraded flag because no peer is configured
    assert all(r.get("semantic_degraded") is True for r in resp["results"])
```

If the existing fixture has a different name or shape, adapt the import/signature — do not fabricate a new MCP client.

- [ ] **Step 4: If no usable fixture exists, xfail the test**

If `tests/e2e/self_contained/mcp/conftest.py` doesn't expose a usable fixture, mark the test `@pytest.mark.xfail(reason="Issue #3778 — blocked on MCP e2e harness; see #<followup>")` and open a follow-up issue. Do **not** write a bespoke MCP JSON-RPC client inline — that duplicates infrastructure.

- [ ] **Step 3: Run**

```bash
pytest tests/e2e/self_contained/test_sandbox_mcp.py -v -m e2e
```

Expected: PASS (or xfail with a linked issue if the e2e infrastructure needs work).

### 15b. CI matrix for dual image build

- [ ] **Step 4: Find the existing Docker workflow**

```bash
ls .github/workflows/ | grep -iE "docker|build|release"
```

- [ ] **Step 5: Add a `profile` matrix axis**

In the Docker build workflow, add matrix entries:

```yaml
jobs:
  build-image:
    strategy:
      matrix:
        profile:
          - tag: latest
            extras: "all,performance,compression,monitoring,docker,event-streaming,sentry,pay"
          - tag: sandbox
            extras: "sandbox"
    steps:
      - uses: actions/checkout@v4
      - uses: docker/setup-buildx-action@v3
      - uses: docker/build-push-action@v5
        with:
          context: .
          tags: ghcr.io/nexi-lab/nexus:${{ matrix.profile.tag }}
          build-args: |
            NEXUS_PROFILE_EXTRAS=${{ matrix.profile.extras }}
          push: ${{ github.event_name != 'pull_request' }}
```

Adapt to the actual workflow schema in the repo.

- [ ] **Step 6: Add image-size assertion for sandbox tag**

After the build step, add:

```yaml
      - name: Assert sandbox image size
        if: matrix.profile.tag == 'sandbox'
        run: |
          size_bytes=$(docker image inspect ghcr.io/nexi-lab/nexus:sandbox --format '{{.Size}}')
          size_mb=$((size_bytes / 1024 / 1024))
          echo "Sandbox image size: ${size_mb} MB"
          test $size_mb -lt 300 || { echo "Image too large: ${size_mb}MB"; exit 1; }
```

### 15c. User docs

- [ ] **Step 7: Create `docs/deployment/sandbox-profile.md`**

```markdown
# SANDBOX deployment profile

Nexus's `sandbox` profile is the lightweight runtime for running one Nexus
inside each AI-agent sandbox. It boots with **zero external services** and
targets ~200-300MB RAM and <5s boot time.

## When to use

- You want per-agent isolation: one Nexus instance per sandbox, with its
  own storage + policy boundary.
- The agent's outer orchestrator (e.g.
  [agentenv](https://github.com/windoliver/agentenv)) provisions the
  sandbox and injects `NEXUS_URL` / `NEXUS_API_KEY` so the sandbox can
  federate to a hub or peer Nexus.
- You don't want to operate PostgreSQL + Dragonfly inside every sandbox.

Use the `full` profile for a shared Nexus hub; use `sandbox` for the
per-sandbox clients that talk to it.

## What you get

| Surface | SANDBOX | FULL |
|---|---|---|
| Storage | SQLite (single file) | PostgreSQL |
| Cache | In-process LRU | Dragonfly/Redis |
| Keyword search | BM25S mmap | BM25S + Zoekt |
| Semantic search | Federated to peers; BM25S fallback | Local txtai + federation |
| HTTP surface | `/health`, `/api/v2/features` | Full `/api/v2/*` |
| MCP | Yes | Yes |
| Target RSS | <300MB | Multi-GB |
| Boot time | <5s | 15-60s |

## Running

### From pip

```bash
pip install 'nexus-ai-fs[sandbox]'
NEXUS_PROFILE=sandbox nexus serve
```

### From Docker

```bash
docker run --rm \
  -e NEXUS_PROFILE=sandbox \
  -e NEXUS_DATA_DIR=/data \
  -v sandbox-data:/data \
  -p 8000:8000 \
  ghcr.io/nexi-lab/nexus:sandbox
```

### Config file

```yaml
profile: sandbox
# SANDBOX defaults fill these in automatically; override only if needed:
#   backend: local
#   data_dir: ~/.nexus/sandbox
#   db_path: ~/.nexus/sandbox/nexus.db
#   cache_size_mb: 64
#   enable_vector_search: false

features:
  # Everything else off; override to re-enable specific bricks:
  # workflows: true
```

## Federation

SANDBOX delegates semantic search to configured peer zones. Point it at
a hub zone via the federation config:

```yaml
federation:
  peers:
    - zone_id: main-hub
      url: https://nexus.example.com
      token: ${NEXUS_HUB_TOKEN}
```

When all peers are unreachable, search returns BM25S keyword results
stamped with `semantic_degraded=true`. The MCP client can surface this
to the agent so it knows the results are keyword-only.

## What's off by default

- `pay`, `llm`, `workflows`, `sandbox` brick, `observability`, `uploads`,
  `resiliency`, `access_manifest`, `catalog`, `delegation`, `identity`,
  `share_link`, `versioning`, `workspace`, `portability`, `snapshot`,
  `task_manager`, `acp`, `discovery`, `memory`, `skills`.

Re-enable any of these with `features.<brick>: true` — Nexus will log a
warning about the override.

## Troubleshooting

- **Boot fails with `ModuleNotFoundError: bm25s`**: install the extras
  with `pip install 'nexus-ai-fs[sandbox]'`.
- **Boot tries to connect to Postgres/Redis**: you have a leftover
  `NEXUS_DATABASE_URL` or `NEXUS_DRAGONFLY_URL` in your env. Unset them
  or explicitly set `NEXUS_CACHE_BACKEND=inmem`.
- **Semantic search returns `semantic_degraded=true`**: no peer is
  reachable. Check `federation.peers` in your config + network access.
```

- [ ] **Step 8: Commit 15a/b/c together**

```bash
git add tests/e2e/self_contained/test_sandbox_mcp.py \
        .github/workflows/ \
        docs/deployment/sandbox-profile.md
git commit -m "test+ci+docs(#3778): MCP e2e, CI dual image build, user docs"
```

---

## Final Verification

- [ ] **Run full test suite**

```bash
pytest tests/unit/core/test_sandbox_profile.py \
       tests/unit/core/test_deployment_profile.py \
       tests/unit/test_config_sandbox.py \
       tests/unit/cache/test_cache_factory_inmem.py \
       tests/unit/bricks/search/test_federated_degraded.py \
       tests/unit/server/test_sandbox_route_allowlist.py \
       tests/integration/test_sandbox_boot.py \
       -v
```

Expected: all PASS.

- [ ] **Run docker smoke**

```bash
docker build -t nexus:sandbox --build-arg NEXUS_PROFILE_EXTRAS=sandbox .
docker image inspect nexus:sandbox --format '{{.Size}}' | awk '{print $1/1024/1024 " MB"}'
docker run --rm -d --name nexus-sb -e NEXUS_PROFILE=sandbox -p 8000:8000 nexus:sandbox
sleep 5
curl -fsS http://localhost:8000/health
docker stop nexus-sb
```

- [ ] **Verify acceptance criteria from issue**

- [ ] `NEXUS_PROFILE=sandbox nexus serve` boots with zero external services (Task 13)
- [ ] Search works (BM25S + federated semantic) (Tasks 8, 9, 15a)
- [ ] MCP server responds to standard tools (Task 15a)
- [ ] Memory <300MB idle (Task 14, marker-gated)
- [ ] Boot time <5s (Task 13 assertion)

- [ ] **Open PR**

Target `develop`. Reference #3778 in title and body. Include:
- Link to spec: `docs/superpowers/specs/2026-04-17-3778-sandbox-profile-design.md`
- Link to plan: `docs/superpowers/plans/2026-04-17-3778-sandbox-profile.md`
- Sandbox image size (from Task 13)
- Memory benchmark result (from Task 15, if run)

---

## Known adjustments the implementer may need

The plan relies on a few assumptions that may need local adaptation:

1. **`SearchService` constructor shape**: Task 9 binds `_federated_search`, `_bm25s_search`, `_default_semantic_search` onto the service. Read the current `__init__` in `bricks/search/search_service.py` and rebind against the actual attribute names.

2. **FastAPI app-construction function**: Task 13 calls `build_app()`. If the actual function is `create_app()` or the app is created at module scope, adapt the import.

3. **MCP stdio entry point**: Task 15a uses `nexus.bricks.mcp.stdio_server` as the module path. If the actual entry point differs, update it after reading `bricks/mcp/brick_factory.py`.

4. **Dockerfile conditional shell**: Task 12 assumes the existing Dockerfile has a platform-conditional pip install. Read lines 80-95 and preserve the conditional.

5. **CI workflow filename**: Task 15b refers to the Docker workflow generically. Find the actual file under `.github/workflows/`.

6. **`pdf-inspector` version**: Task 12 pins no version. After #3757 lands, update the pin in `pyproject.toml` to match.

None of these should change the plan's decisions — only the exact identifiers.
