# Nexus Idle RSS Tuning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Drop nexusd demo-profile cold-start RSS from 1.5 GB → ≤450 MB by skipping heavy ML model loads when not opted in, right-sizing pool defaults, capping BLAS threads + glibc arenas, and adding a regression test.

**Architecture:** Six change surfaces — search runtime three-way auto resolver (env-driven default = BM25), txtai BM25 fast-path, `_FULL_TUNING` pool right-sizing, lifespan boot hygiene (stack_size + gc + idle trimmer), Dockerfile allocator/BLAS envs + jemalloc, docker-entrypoint safety net. Backed by a `pytest -m demo_memory` integration test booting the demo compose stack and asserting `/proc/$pid/status` thresholds.

**Tech Stack:** Python 3.14, FastAPI, anyio, SQLAlchemy + asyncpg, txtai (BM25 + pgvector), Docker (Debian slim), glibc, jemalloc, pytest.

**Spec:** `docs/superpowers/specs/2026-05-02-nexus-idle-rss-tuning-design.md`
**Issue:** #3997

**File map:**
- Modify: `src/nexus/lib/performance_tuning.py:572-625` (`_FULL_TUNING` numbers)
- Modify: `tests/unit/core/test_performance_tuning.py:280-347` (assertion updates)
- Modify: `src/nexus/server/lifespan/search.py:27-49` (three-way resolver)
- Modify: `src/nexus/server/lifespan/search.py:69-96` (boot-mode log line)
- Create: `tests/unit/server/lifespan/test_search_runtime_config.py`
- Modify: `src/nexus/bricks/search/daemon.py:137` (`txtai_model: str | None`)
- Modify: `src/nexus/bricks/search/txtai_backend.py:_startup_impl` (BM25 fast-path)
- Create: `tests/unit/bricks/search/test_txtai_backend_bm25_only.py`
- Modify: `src/nexus/server/lifespan/__init__.py` (boot tweaks + idle trimmer)
- Create: `tests/unit/server/lifespan/test_boot_tweaks.py`
- Modify: `Dockerfile:200-203` (jemalloc + LD_PRELOAD + MALLOC + BLAS envs)
- Modify: `Dockerfile:328-329` (drop NEXUS_TXTAI_RERANKER)
- Modify: `dockerfiles/docker-entrypoint.sh:62-66` (jemalloc safety net)
- Create: `tests/integration/test_demo_memory.py`
- Create: `.github/workflows/demo-memory.yml`

---

## Task 1: Update `_FULL_TUNING` pool numbers

**Files:**
- Modify: `src/nexus/lib/performance_tuning.py:572-625`
- Modify: `tests/unit/core/test_performance_tuning.py:287-347`

**Why:** `_FULL_TUNING` defaults are sized for multi-tenant workload but apply to single-process demo. Drop to web-evidence backed values: `thread_pool_size=40` (anyio default), `db_pool=5+5` (Cloud SQL sample), `httpx_max_connections=20`, `connector_max_workers=6`. `_CLOUD_TUNING` untouched — already sized for multi-tenant.

- [ ] **Step 1: Update existing FULL profile assertions to new values**

Edit `tests/unit/core/test_performance_tuning.py` lines 287-347 to assert the new targets. Replace each old number:

```python
def test_full_concurrency(self) -> None:
    c = DeploymentProfile.FULL.tuning().concurrency
    assert c.default_workers == 4
    assert c.thread_pool_size == 40  # was 200; anyio upstream default (#3997)
    assert c.max_async_concurrency == 10
    assert c.task_runner_workers == 4

def test_full_storage(self) -> None:
    s = DeploymentProfile.FULL.tuning().storage
    assert s.write_buffer_flush_ms == 100
    assert s.write_buffer_max_size == 100
    assert s.changelog_chunk_size == 500
    assert s.db_pool_size == 5  # was 20; Cloud SQL sample sizing (#3997)
    assert s.db_max_overflow == 5  # was 30; single-tenant burst (#3997)

def test_full_connector(self) -> None:
    cn = DeploymentProfile.FULL.tuning().connector
    assert cn.blob_operation_timeout == 60.0
    assert cn.large_upload_timeout == 300.0
    assert cn.connector_max_workers == 6  # was 20; blob ops are I/O-bound (#3997)

def test_full_pool(self) -> None:
    p = DeploymentProfile.FULL.tuning().pool
    assert p.asyncpg_min_size == 2
    assert p.asyncpg_max_size == 5
    assert p.httpx_max_connections == 20  # was 100; HTTP/2 collapses anyway (#3997)
    assert p.remote_pool_maxsize == 10  # was 20 (#3997)
```

- [ ] **Step 2: Run the failing tests**

Run: `uv run pytest tests/unit/core/test_performance_tuning.py::TestConcreteValues -v`
Expected: 4 failures (concurrency, storage, connector, pool) — values still 200/20+30/20/100+20.

- [ ] **Step 3: Update `_FULL_TUNING` numbers**

Edit `src/nexus/lib/performance_tuning.py:572-625`. Change the six fields:

```python
_FULL_TUNING = ProfileTuning(
    concurrency=ConcurrencyTuning(
        default_workers=4,
        thread_pool_size=40,  # Issue #3997: was 200; anyio upstream default
        max_async_concurrency=10,
        task_runner_workers=4,
    ),
    network=NetworkTuning(
        default_http_timeout=30.0,
        webhook_timeout=10.0,
        long_operation_timeout=120.0,
    ),
    storage=StorageTuning(
        write_buffer_flush_ms=100,
        write_buffer_max_size=100,
        changelog_chunk_size=500,
        db_pool_size=5,  # Issue #3997: was 20; Cloud SQL sample
        db_max_overflow=5,  # Issue #3997: was 30; single-tenant burst
    ),
    search=SearchTuning(
        grep_parallel_workers=4,
        list_parallel_workers=10,
        search_max_concurrency=10,
        vector_pool_workers=2,
    ),
    cache=CacheTuning(
        tiger_max_workers=4,
        tiger_batch_size=100,
    ),
    background_task=BackgroundTaskTuning(
        sandbox_cleanup_interval=300,
        session_cleanup_interval=3600,
        daily_gc_interval=86400,
        heartbeat_flush_interval=60,
        stale_agent_check_interval=300,
        stale_agent_threshold=300,
    ),
    resiliency=ResiliencyTuning(
        default_max_retries=3,
        retry_base_backoff_ms=50,
        circuit_breaker_failure_threshold=5,
        circuit_breaker_timeout=30.0,
    ),
    connector=ConnectorTuning(
        blob_operation_timeout=60.0,
        large_upload_timeout=300.0,
        connector_max_workers=6,  # Issue #3997: was 20; I/O-bound
    ),
    pool=PoolTuning(
        asyncpg_min_size=2,
        asyncpg_max_size=5,
        httpx_max_connections=20,  # Issue #3997: was 100; HTTP/2 collapses
        remote_pool_maxsize=10,  # Issue #3997: was 20
    ),
    eviction=EvictionTuning(
        memory_high_watermark_pct=85,
        memory_low_watermark_pct=75,
        max_active_agents=1000,
        eviction_batch_size=20,
        checkpoint_timeout_seconds=10.0,
        eviction_cooldown_seconds=60,
        eviction_poll_interval_seconds=120,
        checkpoint_cleanup_interval_seconds=3600,
        checkpoint_max_age_seconds=86400,
        max_concurrent_transitions=10,
    ),
    qos=QoSTuning(
        premium=QoSClassConfig(
            max_concurrent_tasks=20, scheduling_weight=3, eviction_priority=2, preemptible=False
        ),
        standard=QoSClassConfig(
            max_concurrent_tasks=10, scheduling_weight=1, eviction_priority=1, preemptible=False
        ),
        spot=QoSClassConfig(
            max_concurrent_tasks=5, scheduling_weight=1, eviction_priority=0, preemptible=True
        ),
    ),
)
```

- [ ] **Step 4: Run the test, verify pass**

Run: `uv run pytest tests/unit/core/test_performance_tuning.py -v`
Expected: PASS (all profiles, including monotonicity check `embedded ≤ lite ≤ full ≤ cloud` — full's `thread_pool_size=40` is still ≥ lite's smaller values, ≤ cloud's 400; storage `db_pool_size=5` ≥ embedded's 3 ≥ lite, ≤ cloud's 30 — verify the monotonicity test passes).

If monotonicity fails (lite has higher value than new full), inspect `_LITE_TUNING` at the same file. Expected: lite has 4 thread_pool_size, 2 db_pool_size — both ≤ new full values, so OK.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/lib/performance_tuning.py tests/unit/core/test_performance_tuning.py
git commit -m "perf(profile): right-size _FULL_TUNING pools for single-tenant (#3997)

thread_pool_size 200→40 (anyio upstream default)
db_pool_size 20→5, db_max_overflow 30→5 (Cloud SQL sample)
httpx_max_connections 100→20, remote_pool_maxsize 20→10
connector_max_workers 20→6"
```

---

## Task 2: Search runtime three-way auto resolver

**Files:**
- Modify: `src/nexus/server/lifespan/search.py:27-49`
- Create: `tests/unit/server/lifespan/test_search_runtime_config.py`

**Why:** Today `_resolve_txtai_runtime_config` always returns a model string (defaults to `sentence-transformers/all-MiniLM-L6-v2` → 900 MB at boot). After this task: returns `(None, None)` when no key + no explicit local model — txtai backend (Task 3) takes BM25 fast-path. OpenAI key → API embeddings. Explicit local → opt-in heavy.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/server/lifespan/test_search_runtime_config.py`:

```python
"""Three-way auto-resolution for txtai runtime (Issue #3997).

Default (no env): BM25 keyword-only — no model load.
OPENAI_API_KEY set: API embeddings — ~0 RAM.
NEXUS_TXTAI_MODEL=local: opt-in heavy load.
"""

import pytest

from nexus.server.lifespan.search import _resolve_txtai_runtime_config


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    for k in (
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "NEXUS_TXTAI_MODEL",
        "NEXUS_TXTAI_USE_API_EMBEDDINGS",
    ):
        monkeypatch.delenv(k, raising=False)


def test_default_returns_bm25():
    """No env -> (None, None) -> BM25 keyword-only path."""
    assert _resolve_txtai_runtime_config() == (None, None)


def test_openai_key_only(monkeypatch):
    """OPENAI_API_KEY alone -> openai/text-embedding-3-small with key."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    model, vectors = _resolve_txtai_runtime_config()
    assert model == "openai/text-embedding-3-small"
    assert vectors == {"api_key": "sk-test"}


def test_openai_key_with_base_url(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://proxy.example/v1")
    model, vectors = _resolve_txtai_runtime_config()
    assert model == "openai/text-embedding-3-small"
    assert vectors == {"api_key": "sk-test", "api_base": "https://proxy.example/v1"}


def test_explicit_local_model_wins_over_key(monkeypatch):
    """User-set local model overrides API mode even when key is present."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("NEXUS_TXTAI_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
    model, vectors = _resolve_txtai_runtime_config()
    assert model == "sentence-transformers/all-MiniLM-L6-v2"
    assert vectors is None


def test_explicit_local_model_no_key(monkeypatch):
    """User-set local model without key still loads locally."""
    monkeypatch.setenv("NEXUS_TXTAI_MODEL", "sentence-transformers/all-mpnet-base-v2")
    model, vectors = _resolve_txtai_runtime_config()
    assert model == "sentence-transformers/all-mpnet-base-v2"
    assert vectors is None


def test_explicit_openai_model_uses_key(monkeypatch):
    """Explicit openai/* model + key -> use API."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("NEXUS_TXTAI_MODEL", "openai/text-embedding-3-large")
    model, vectors = _resolve_txtai_runtime_config()
    assert model == "openai/text-embedding-3-large"
    assert vectors == {"api_key": "sk-test"}


def test_use_api_flag_with_key(monkeypatch):
    """NEXUS_TXTAI_USE_API_EMBEDDINGS=true + key -> default openai model."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("NEXUS_TXTAI_USE_API_EMBEDDINGS", "true")
    model, vectors = _resolve_txtai_runtime_config()
    assert model == "openai/text-embedding-3-small"
    assert vectors == {"api_key": "sk-test"}


def test_use_api_flag_no_key(monkeypatch):
    """NEXUS_TXTAI_USE_API_EMBEDDINGS=true without key -> still BM25 (no key to use)."""
    monkeypatch.setenv("NEXUS_TXTAI_USE_API_EMBEDDINGS", "true")
    assert _resolve_txtai_runtime_config() == (None, None)
```

- [ ] **Step 2: Run test, verify failure**

Run: `uv run pytest tests/unit/server/lifespan/test_search_runtime_config.py -v`
Expected: FAILS — `test_default_returns_bm25` returns `("sentence-transformers/all-MiniLM-L6-v2", None)` instead of `(None, None)`. Several others fail similarly.

- [ ] **Step 3: Update the resolver**

Replace `_resolve_txtai_runtime_config` at `src/nexus/server/lifespan/search.py:27-49` with:

```python
def _resolve_txtai_runtime_config() -> tuple[str | None, dict[str, str] | None]:
    """Resolve embedding model + vectors config from env (Issue #3997).

    Three-way auto:
    - explicit local model (sentence-transformers/...) wins; opt-in to ~900 MB
    - OPENAI_API_KEY present: API embeddings (~0 RAM)
    - neither: returns (None, None) -> txtai BM25 keyword-only fast-path
    """
    explicit_model = os.environ.get("NEXUS_TXTAI_MODEL", "").strip()
    openai_api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    openai_base_url = os.environ.get("OPENAI_BASE_URL", "").strip()
    use_api_explicit = _env_truthy("NEXUS_TXTAI_USE_API_EMBEDDINGS")

    # Explicit local model wins (heavy opt-in)
    if explicit_model and not explicit_model.startswith("openai/"):
        return explicit_model, None

    # OpenAI key present -> API embeddings, ~0 RAM
    if openai_api_key and (use_api_explicit or not explicit_model or explicit_model.startswith("openai/")):
        model = explicit_model or "openai/text-embedding-3-small"
        vectors: dict[str, str] = {"api_key": openai_api_key}
        if openai_base_url:
            vectors["api_base"] = openai_base_url
        return model, vectors

    # No key, no explicit local model -> BM25 keyword-only
    return None, None
```

- [ ] **Step 4: Run test, verify pass**

Run: `uv run pytest tests/unit/server/lifespan/test_search_runtime_config.py -v`
Expected: 8 PASS.

- [ ] **Step 5: Add boot-mode log line in startup_search**

Edit `src/nexus/server/lifespan/search.py` after `txtai_model, txtai_vectors = _resolve_txtai_runtime_config()` (line 69):

```python
        txtai_model, txtai_vectors = _resolve_txtai_runtime_config()
        # Issue #3997: surface mode in boot logs so operators see whether
        # heavy local model, remote API embeddings, or BM25-only is active.
        if txtai_model is None:
            _mode = "bm25-only"
        elif txtai_model.startswith("openai/"):
            _mode = "openai-api"
        else:
            _mode = "local"
        logger.info("Search backend mode: %s (model=%s)", _mode, txtai_model or "<none>")
```

- [ ] **Step 6: Commit**

```bash
git add src/nexus/server/lifespan/search.py tests/unit/server/lifespan/test_search_runtime_config.py
git commit -m "feat(search): three-way runtime config auto-resolve (#3997)

Default (no env) returns (None, None) -> BM25 keyword-only.
OPENAI_API_KEY -> API embeddings.
NEXUS_TXTAI_MODEL=sentence-transformers/... -> opt-in local."
```

---

## Task 3: txtai backend BM25 fast-path

**Files:**
- Modify: `src/nexus/bricks/search/daemon.py:137` (relax type)
- Modify: `src/nexus/bricks/search/txtai_backend.py` (`_startup_impl` early branch)
- Create: `tests/unit/bricks/search/test_txtai_backend_bm25_only.py`

**Why:** When `_resolve_txtai_runtime_config` returns `(None, None)` (Task 2), the backend must skip `Embeddings(config_with_path)` entirely and start in BM25-only mode. Current code unconditionally builds the heavy hybrid config and only falls back to BM25 via try/except. Take the BM25 path intentionally when `model is None`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/bricks/search/test_txtai_backend_bm25_only.py`:

```python
"""txtai backend BM25-only fast-path when model is None (Issue #3997)."""

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def fake_embeddings(monkeypatch):
    """Replace txtai.Embeddings with a recording mock."""
    captured: list[dict] = []

    class _FakeEmbeddings:
        def __init__(self, config=None):
            captured.append(dict(config) if isinstance(config, dict) else {"_path_form": config})

        def exists(self, *_a, **_kw):  # pragma: no cover - probe path
            return False

        def load(self, *_a, **_kw):  # pragma: no cover
            return None

        def count(self):
            return 0

        def close(self):
            return None

    fake_module = MagicMock()
    fake_module.Embeddings = _FakeEmbeddings
    monkeypatch.setattr("txtai.Embeddings", _FakeEmbeddings, raising=False)
    return captured


@pytest.mark.asyncio
async def test_bm25_only_path_when_model_is_none(fake_embeddings):
    """model=None -> single Embeddings({keyword:True, ...}) call, no model path."""
    from nexus.bricks.search.txtai_backend import TxtaiBackend

    backend = TxtaiBackend(
        model=None,
        vectors=None,
        reranker=None,
        sparse=False,
        graph=False,
        database_url=None,
    )
    await backend._startup_impl()

    # Exactly one Embeddings(...) call, with BM25 config (no "path" key).
    assert len(fake_embeddings) == 1
    cfg = fake_embeddings[0]
    assert cfg.get("keyword") is True
    assert "path" not in cfg
    assert backend._hybrid is False
    assert backend._started is True


@pytest.mark.asyncio
async def test_model_set_takes_normal_path(fake_embeddings):
    """model='openai/...' -> Embeddings(config_with_path) called normally."""
    from nexus.bricks.search.txtai_backend import TxtaiBackend

    backend = TxtaiBackend(
        model="openai/text-embedding-3-small",
        vectors={"api_key": "sk-x"},
        reranker=None,
        sparse=False,
        graph=False,
        database_url=None,
    )
    await backend._startup_impl()

    # First call should include "path" (heavy mode), not just keyword=True.
    assert any("path" in c for c in fake_embeddings)
```

Adjust the `TxtaiBackend(...)` constructor argument names to match the actual class signature — read `src/nexus/bricks/search/txtai_backend.py` for the `__init__` signature first if these don't match.

- [ ] **Step 2: Run test, verify failure**

Run: `uv run pytest tests/unit/bricks/search/test_txtai_backend_bm25_only.py -v`
Expected: FAIL — current code calls `Embeddings(config_with_path=...)` even when model=None (today model defaults to sentence-transformers).

- [ ] **Step 3: Relax `DaemonConfig.txtai_model` type**

Edit `src/nexus/bricks/search/daemon.py:137`:

```python
    # txtai backend config (Issue #2663, #3997: optional for BM25-only mode)
    txtai_model: str | None = None  # None -> BM25 keyword-only fast-path
    txtai_vectors: dict[str, Any] | None = None
```

(Default flipped from `"sentence-transformers/all-MiniLM-L6-v2"` to `None`. Reason: with the resolver fix, the default-None case is the most common one; explicit string callers still work.)

- [ ] **Step 4: Add BM25 fast-path branch in `_startup_impl`**

Edit `src/nexus/bricks/search/txtai_backend.py:_startup_impl`. After the `try: from txtai import Embeddings` block (after line 357) and before the GPU detection block (line 359), insert:

```python
        # Issue #3997: BM25 fast-path. When no embedding model is configured
        # (resolver returned (None, None) — typical default deploy), skip the
        # heavy Embeddings(path=...) load and start txtai with keyword-only
        # config directly. Saves ~900 MB RSS at boot.
        if self._model is None:
            content_store: bool | str = self._database_url or True
            bm25_config: dict[str, Any] = {
                "keyword": True,
                "content": content_store,
                "objects": True,
            }
            try:
                self._embeddings = Embeddings(bm25_config)
                self._hybrid = False
                self._started = True
                self._configure_litellm()
                logger.info(
                    "txtai backend started in BM25-only mode "
                    "(no embedding model configured)"
                )
                return
            except Exception:
                logger.error(
                    "BM25-only init failed; entering degraded mode (no results).",
                    exc_info=True,
                )
                self._embeddings = None
                self._started = True
                return
```

- [ ] **Step 5: Run test, verify pass**

Run: `uv run pytest tests/unit/bricks/search/test_txtai_backend_bm25_only.py -v`
Expected: 2 PASS.

- [ ] **Step 6: Run wider txtai tests to ensure no regression**

Run: `uv run pytest tests/unit/bricks/search/ tests/integration/bricks/search/ -v -x`
Expected: PASS. If any test asserts model is always set, update it to allow `None`.

- [ ] **Step 7: Commit**

```bash
git add src/nexus/bricks/search/daemon.py src/nexus/bricks/search/txtai_backend.py tests/unit/bricks/search/test_txtai_backend_bm25_only.py
git commit -m "feat(search): BM25-only fast-path when no model configured (#3997)

DaemonConfig.txtai_model now Optional[str], default None.
TxtaiBackend._startup_impl skips Embeddings(path=...) when model is None,
goes straight to keyword+content+objects config. Saves ~900 MB RSS at boot."
```

---

## Task 4: Lifespan boot tweaks (stack_size, gc, idle trimmer)

**Files:**
- Modify: `src/nexus/server/lifespan/__init__.py` (add to lifespan ctx + new helper)
- Create: `tests/unit/server/lifespan/test_boot_tweaks.py`

**Why:** Cap pthread stack 8 MB → 1 MB, reduce GC frequency, freeze boot-time heap, run a 60s idle trimmer that calls `gc.collect()` + `malloc_trim(0)` to release memory back to the kernel during long-running idle periods.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/server/lifespan/test_boot_tweaks.py`:

```python
"""Lifespan boot-time memory tweaks (Issue #3997)."""

import asyncio
import gc
import threading
from unittest.mock import MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_apply_boot_tweaks_sets_stack_size_and_gc_threshold():
    """_apply_boot_tweaks sets 1 MB stack and adjusted GC threshold."""
    from nexus.server.lifespan import _apply_boot_tweaks

    orig_stack = threading.stack_size()
    orig_thresh = gc.get_threshold()
    try:
        _apply_boot_tweaks()
        assert threading.stack_size() == 1 << 20
        assert gc.get_threshold() == (50_000, 10, 10)
    finally:
        # Restore for other tests
        threading.stack_size(orig_stack)
        gc.set_threshold(*orig_thresh)


@pytest.mark.asyncio
async def test_idle_trimmer_invokes_gc_and_malloc_trim():
    """_idle_trimmer calls gc.collect + libc.malloc_trim per tick."""
    from nexus.server.lifespan import _idle_trimmer

    fake_libc = MagicMock()
    fake_libc.malloc_trim = MagicMock(return_value=1)

    with patch("ctypes.CDLL", return_value=fake_libc), patch(
        "asyncio.sleep", side_effect=[None, asyncio.CancelledError()]
    ):
        with patch("gc.collect") as mock_collect:
            with pytest.raises(asyncio.CancelledError):
                await _idle_trimmer()
            assert mock_collect.call_count >= 1
            assert fake_libc.malloc_trim.call_count >= 1


@pytest.mark.asyncio
async def test_idle_trimmer_disabled_when_libc_unavailable():
    """_idle_trimmer exits cleanly when libc.so.6 not loadable (musl/Alpine)."""
    from nexus.server.lifespan import _idle_trimmer

    with patch("ctypes.CDLL", side_effect=OSError("no libc")):
        # Should return normally without hanging or raising
        await asyncio.wait_for(_idle_trimmer(), timeout=1.0)


@pytest.mark.asyncio
async def test_idle_trimmer_disabled_when_malloc_trim_missing():
    """_idle_trimmer exits when symbol absent (not glibc)."""
    from nexus.server.lifespan import _idle_trimmer

    fake_libc = MagicMock()
    # Simulate AttributeError on probe
    type(fake_libc).malloc_trim = property(
        fget=lambda self: (_ for _ in ()).throw(AttributeError("no symbol"))
    )

    with patch("ctypes.CDLL", return_value=fake_libc):
        await asyncio.wait_for(_idle_trimmer(), timeout=1.0)
```

- [ ] **Step 2: Run test, verify failure**

Run: `uv run pytest tests/unit/server/lifespan/test_boot_tweaks.py -v`
Expected: FAIL — `_apply_boot_tweaks` and `_idle_trimmer` don't exist.

- [ ] **Step 3: Add helpers + wire into lifespan**

Edit `src/nexus/server/lifespan/__init__.py`. At the top of the file (after existing imports), add:

```python
import ctypes
import gc
import threading
```

Add these helpers between the existing `_wire_query_observer` function (line 105) and the `@asynccontextmanager`-decorated `lifespan` (line 127):

```python
def _apply_boot_tweaks() -> None:
    """Apply Python-level memory hygiene at lifespan startup (Issue #3997).

    Must be called before the first thread is spawned and before any heavy
    module is imported, so the new stack size and GC threshold take effect
    for everything that follows.

    - threading.stack_size(1 << 20): cap pthread stack 8 MB -> 1 MB
      (~250 MB VmData savings over 32 threads, near-zero RSS impact).
    - gc.set_threshold(50_000, 10, 10): fewer GC pauses (~20% latency win).
    """
    threading.stack_size(1 << 20)
    gc.set_threshold(50_000, 10, 10)


async def _idle_trimmer() -> None:
    """Background task: every 60s, gc.collect() + malloc_trim(0) (Issue #3997).

    Releases freed heap pages back to the kernel during long-running idle
    periods. Glibc-only (no-op on musl/Alpine).
    """
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        _probe = libc.malloc_trim  # raises AttributeError if symbol missing
    except (OSError, AttributeError):
        logger.info("malloc_trim unavailable, idle trimmer disabled")
        return

    while True:
        await asyncio.sleep(60)
        try:
            gc.collect()
            libc.malloc_trim(0)
        except Exception:
            logger.exception("idle_trimmer iteration failed")
```

Then modify the `lifespan` async context manager. After line 147 `bg_tasks: list[asyncio.Task] = []`, insert as the very first action:

```python
    # Issue #3997: apply boot-time memory hygiene before any thread spawn or
    # heavy import. Must run before svc init since LifespanServices.from_app
    # may touch threadpool state.
    _apply_boot_tweaks()
```

After the existing `_wire_query_observer(app, svc)` call (line 216) and immediately before the `yield` (line 218), insert:

```python
    # Issue #3997: idle trimmer + freeze boot-time heap.
    # gc.freeze() moves all currently-tracked objects to a permanent generation
    # so they are not scanned by future GC cycles. Must run AFTER all warmup
    # imports/services have completed.
    bg_tasks.append(asyncio.create_task(_idle_trimmer(), name="idle_trimmer"))
    gc.freeze()
```

- [ ] **Step 4: Run test, verify pass**

Run: `uv run pytest tests/unit/server/lifespan/test_boot_tweaks.py -v`
Expected: 4 PASS.

- [ ] **Step 5: Run wider lifespan tests**

Run: `uv run pytest tests/unit/server/lifespan/ tests/integration/server/lifespan/ -v -x`
Expected: PASS. If any existing test holds reference to `gc.get_threshold()` defaults, update or skip it.

- [ ] **Step 6: Commit**

```bash
git add src/nexus/server/lifespan/__init__.py tests/unit/server/lifespan/test_boot_tweaks.py
git commit -m "perf(lifespan): boot tweaks + idle trimmer (#3997)

threading.stack_size 8M->1M, gc.set_threshold tuned, gc.freeze after warmup,
60s background _idle_trimmer (gc.collect + malloc_trim) for long-running
idle release. Glibc-only; no-op on musl/Alpine."
```

---

## Task 5: Dockerfile — jemalloc + MALLOC + BLAS envs, drop reranker

**Files:**
- Modify: `Dockerfile:180-203` (add libjemalloc2 install + MALLOC + BLAS envs)
- Modify: `Dockerfile:328-329` (drop NEXUS_TXTAI_RERANKER hardcode)

**Why:** Image-level allocator + thread caps. `MALLOC_ARENA_MAX=2` saves 400-800 MB RSS by capping glibc arenas (default `8 × cpu_count`). jemalloc preload saves another 300-600 MB by replacing glibc malloc entirely. BLAS thread caps prevent numpy/torch from spawning `cpu_count` worker threads at import (each = 8 MB stack + arena). Drop hardcoded reranker — opt-in only when user sets `NEXUS_TXTAI_RERANKER`.

- [ ] **Step 1: Add libjemalloc2 to apt install**

Edit `Dockerfile:180`. Find the `apt-get install -y --no-install-recommends \` block ending around line 181 with `gosu`. Add `libjemalloc2` to the list. Current code:

```dockerfile
RUN apt-get update && apt-get install -y --no-install-recommends \
        # ... existing packages ...
        gosu \
    && rm -rf /var/lib/apt/lists/*
```

Append `libjemalloc2`:

```dockerfile
RUN apt-get update && apt-get install -y --no-install-recommends \
        # ... existing packages ...
        gosu \
        libjemalloc2 \
    && rm -rf /var/lib/apt/lists/*
```

(If you can't see the exact existing list, run: `sed -n '175,185p' Dockerfile` to confirm the block, then insert `libjemalloc2 \` before the `&& rm -rf` line.)

- [ ] **Step 2: Update LD_PRELOAD + add MALLOC + BLAS envs**

Edit `Dockerfile:200-203`. Replace:

```dockerfile
# LD_PRELOAD: libgomp only (always safe). When torch is installed, the entrypoint
# extends LD_PRELOAD to include libc10.so (see docker-entrypoint.sh).
ENV LD_PRELOAD="/usr/lib/libgomp.so.1"
ENV GLIBC_TUNABLES="glibc.rtld.optional_static_tls=16384"
```

With:

```dockerfile
# LD_PRELOAD: jemalloc first (must intercept allocator calls before any other lib),
# then libgomp. When torch is installed, the entrypoint extends LD_PRELOAD to
# include libc10.so (see docker-entrypoint.sh). The entrypoint also strips
# jemalloc from LD_PRELOAD if the .so is missing at runtime (defensive).
ENV LD_PRELOAD="/usr/lib/x86_64-linux-gnu/libjemalloc.so.2:/usr/lib/libgomp.so.1"
ENV GLIBC_TUNABLES="glibc.rtld.optional_static_tls=16384"

# Issue #3997: cap glibc arenas (default 8*cpu_count -> 200-400 MB RSS bloat
# on threaded Python) and lower trim threshold so free() returns pages to
# the kernel.
ENV MALLOC_ARENA_MAX=2 \
    MALLOC_TRIM_THRESHOLD_=131072

# Issue #3997: cap BLAS worker threads. numpy/torch/sentence-transformers spawn
# cpu_count BLAS threads at first import; each = 8 MB pthread stack + glibc arena.
# Caps must be set BEFORE Python imports numpy/torch -> Dockerfile ENV (entrypoint
# is too late for some import paths). Users opting into local embeddings should
# override at `docker run -e OMP_NUM_THREADS=N` for throughput.
ENV OPENBLAS_NUM_THREADS=1 \
    NUMEXPR_NUM_THREADS=1 \
    VECLIB_MAXIMUM_THREADS=1
# Note: OMP_NUM_THREADS and MKL_ENABLE_INSTRUCTIONS are set in
# docker-entrypoint.sh (already at value 1) for compatibility with the
# faiss/torch SIMD-portability defaults.
```

- [ ] **Step 3: Drop the NEXUS_TXTAI_RERANKER hardcode**

Edit `Dockerfile:322-329`. Current:

```dockerfile
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    NEXUS_HOST=0.0.0.0 \
    NEXUS_PORT=2026 \
    NEXUS_PROFILE=full \
    NEXUS_DATA_DIR=/app/data \
    NEXUS_TXTAI_RERANKER=cross-encoder/ms-marco-MiniLM-L-2-v2 \
    NEXUS_TXTAI_SPARSE=false
```

Change to:

```dockerfile
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    NEXUS_HOST=0.0.0.0 \
    NEXUS_PORT=2026 \
    NEXUS_PROFILE=full \
    NEXUS_DATA_DIR=/app/data \
    NEXUS_TXTAI_SPARSE=false
# Issue #3997: NEXUS_TXTAI_RERANKER is no longer set by default — the
# cross-encoder/ms-marco model loaded ~300 MB unconditionally. Operators
# wanting reranking opt in: -e NEXUS_TXTAI_RERANKER=cross-encoder/ms-marco-MiniLM-L-2-v2
```

- [ ] **Step 4: Build and confirm jemalloc package present**

Run: `docker build --target builder -t nexus:rss-test . 2>&1 | tail -20`
Expected: builds without error.

Then: `docker build -t nexus:rss-test .` (full build).

Confirm jemalloc:
```bash
docker run --rm nexus:rss-test ls -la /usr/lib/x86_64-linux-gnu/libjemalloc.so.2
```
Expected: file exists, ~600 KB.

Confirm envs:
```bash
docker run --rm nexus:rss-test env | grep -E "MALLOC|OPENBLAS|NUMEXPR|VECLIB|LD_PRELOAD" | sort
```
Expected:
```
LD_PRELOAD=/usr/lib/x86_64-linux-gnu/libjemalloc.so.2:/usr/lib/libgomp.so.1
MALLOC_ARENA_MAX=2
MALLOC_TRIM_THRESHOLD_=131072
NUMEXPR_NUM_THREADS=1
OPENBLAS_NUM_THREADS=1
VECLIB_MAXIMUM_THREADS=1
```

Confirm reranker dropped:
```bash
docker run --rm nexus:rss-test env | grep NEXUS_TXTAI_RERANKER
```
Expected: empty output.

- [ ] **Step 5: Commit**

```bash
git add Dockerfile
git commit -m "perf(docker): jemalloc + MALLOC + BLAS caps; drop reranker hardcode (#3997)

Install libjemalloc2, prepend to LD_PRELOAD. Cap glibc arenas
(MALLOC_ARENA_MAX=2) + lower trim threshold. Cap OPENBLAS/NUMEXPR/
VECLIB to 1 thread. Drop hardcoded NEXUS_TXTAI_RERANKER (~300 MB
unconditional load) — operators opt in instead."
```

---

## Task 6: Entrypoint safety net — strip jemalloc if missing

**Files:**
- Modify: `dockerfiles/docker-entrypoint.sh:60-67`

**Why:** If `libjemalloc.so.2` is missing at runtime (custom image, base diverges, mount issue), the dynamic linker would fail every binary in the container. Defensive check: probe the file, log warning, strip from LD_PRELOAD before continuing. Falls back to glibc + arena cap (which still saves 400-800 MB).

- [ ] **Step 1: Add safety net at top of entrypoint**

Edit `dockerfiles/docker-entrypoint.sh`. Insert this block at line 56 (after the SIMD/OMP defaults at lines 51-54, before the existing `# LD_PRELOAD fallback` block at line 57):

```bash
# ---------------------------------------------------------------------------
# jemalloc safety net (Issue #3997)
# Dockerfile prepends /usr/lib/x86_64-linux-gnu/libjemalloc.so.2 to LD_PRELOAD.
# If the file is missing at runtime (custom base image, broken layer, etc.),
# strip it so the dynamic linker doesn't abort every command in the container.
# Glibc + MALLOC_ARENA_MAX=2 fallback still saves 400-800 MB RSS.
# ---------------------------------------------------------------------------
_jemalloc_path="/usr/lib/x86_64-linux-gnu/libjemalloc.so.2"
if [ -n "${LD_PRELOAD:-}" ] && [ ! -e "$_jemalloc_path" ]; then
    case ":${LD_PRELOAD}:" in
        *":${_jemalloc_path}:"*)
            echo "WARN: ${_jemalloc_path} missing; stripping from LD_PRELOAD (glibc fallback)" >&2
            export LD_PRELOAD="${LD_PRELOAD//${_jemalloc_path}:/}"
            export LD_PRELOAD="${LD_PRELOAD//:${_jemalloc_path}/}"
            export LD_PRELOAD="${LD_PRELOAD//${_jemalloc_path}/}"
            ;;
    esac
fi
unset _jemalloc_path
```

- [ ] **Step 2: Smoke test the strip logic locally**

Run a quick bash test (no Docker needed):

```bash
bash -c '
LD_PRELOAD="/missing/jemalloc.so.2:/real/libgomp.so.1"
_jemalloc_path="/missing/jemalloc.so.2"
if [ -n "${LD_PRELOAD:-}" ] && [ ! -e "$_jemalloc_path" ]; then
    case ":${LD_PRELOAD}:" in
        *":${_jemalloc_path}:"*)
            export LD_PRELOAD="${LD_PRELOAD//${_jemalloc_path}:/}"
            export LD_PRELOAD="${LD_PRELOAD//:${_jemalloc_path}/}"
            export LD_PRELOAD="${LD_PRELOAD//${_jemalloc_path}/}"
            ;;
    esac
fi
echo "Result: $LD_PRELOAD"
'
```
Expected output: `Result: /real/libgomp.so.1`.

- [ ] **Step 3: Build image and confirm jemalloc still loads end-to-end**

Run: `docker build -t nexus:rss-test .`

Boot a quick container and confirm the entrypoint did NOT strip jemalloc (because it exists):
```bash
docker run --rm nexus:rss-test sh -c 'ls -la /usr/lib/x86_64-linux-gnu/libjemalloc.so.2 && echo "LD_PRELOAD=$LD_PRELOAD"'
```
Expected: file present + LD_PRELOAD includes both jemalloc and libgomp.

- [ ] **Step 4: Commit**

```bash
git add dockerfiles/docker-entrypoint.sh
git commit -m "fix(docker): strip jemalloc from LD_PRELOAD if missing (#3997)

Defensive: if /usr/lib/.../libjemalloc.so.2 is missing at runtime
(custom base, layer issue), strip it from LD_PRELOAD before exec
so dynamic linker doesn't abort every command. Glibc + arena cap
fallback still saves 400-800 MB RSS."
```

---

## Task 7: Demo memory regression test

**Files:**
- Create: `tests/integration/test_demo_memory.py`

**Why:** Acceptance criterion from issue #3997: pytest test boots demo via Docker, sleeps 30s, asserts `VmRSS_kB < 450 * 1024`, `Threads < 20`, `VmData_kB < 4 * 1024 * 1024`. Marker `@pytest.mark.demo_memory` so it doesn't run in default suite.

- [ ] **Step 1: Register the pytest marker**

Edit `pyproject.toml` (search for `[tool.pytest.ini_options]` or wherever markers are declared). Add `demo_memory` to the markers list:

```toml
[tool.pytest.ini_options]
# ... existing config ...
markers = [
    # ... existing markers ...
    "demo_memory: nexusd demo profile RSS regression (slow, requires Docker)",
]
```

If markers aren't currently declared, add the section. Verify the existing config block first with: `grep -n "markers" pyproject.toml`.

- [ ] **Step 2: Write the test**

Create `tests/integration/test_demo_memory.py`:

```python
"""Demo profile cold-start memory regression (Issue #3997).

Boots the demo compose stack, waits for readiness, then reads
/proc/$pid/status from inside the container and asserts that idle
RSS, VmData, and thread count are within the targets agreed in the
issue:

    VmRSS  <= 450 MB
    VmData <= 4 GB
    Threads <= 20

Skipped by default. Run with:
    uv run pytest -m demo_memory tests/integration/test_demo_memory.py -v
"""

import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DEMO_COMPOSE = REPO_ROOT / "nexus-stack.yml"  # adjust if demo compose is elsewhere
PROJECT_PREFIX = "nexus-demo-mem"
READINESS_TIMEOUT = 60  # seconds
LAZY_INIT_BUFFER = 30  # seconds (matches issue reproduction)
RSS_LIMIT_KB = 450 * 1024
VMDATA_LIMIT_KB = 4 * 1024 * 1024
THREADS_LIMIT = 20


def _docker_available() -> bool:
    return shutil.which("docker") is not None


@pytest.mark.demo_memory
@pytest.mark.skipif(not _docker_available(), reason="docker CLI not available")
def test_demo_idle_rss_under_limit():
    project = f"{PROJECT_PREFIX}-{uuid.uuid4().hex[:8]}"
    container = f"{project}-nexus-1"

    env = os.environ.copy()

    try:
        subprocess.check_call(
            ["docker", "compose", "-p", project, "-f", str(DEMO_COMPOSE), "up", "-d"],
            env=env,
            cwd=REPO_ROOT,
        )

        # Poll readiness up to READINESS_TIMEOUT seconds
        deadline = time.time() + READINESS_TIMEOUT
        ready = False
        while time.time() < deadline:
            rc = subprocess.call(
                ["docker", "exec", container, "curl", "-fsS",
                 "http://localhost:2026/healthz/ready"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            if rc == 0:
                ready = True
                break
            time.sleep(2)
        assert ready, f"{container} did not become ready within {READINESS_TIMEOUT}s"

        # Lazy bg init (skeleton indexer, search daemon hooks)
        time.sleep(LAZY_INIT_BUFFER)

        # Pull /proc/$pid/status from inside container
        status = subprocess.check_output(
            ["docker", "exec", container, "sh", "-c",
             "pid=$(pgrep -f nexusd | head -1); cat /proc/$pid/status"]
        ).decode()

        metrics = _parse_proc_status(status)

        # Diagnostics on failure: dump full status + first 20 lines of maps
        def _diag():
            try:
                maps = subprocess.check_output(
                    ["docker", "exec", container, "sh", "-c",
                     "pid=$(pgrep -f nexusd | head -1); head -20 /proc/$pid/maps"]
                ).decode()
            except Exception as e:
                maps = f"<diag failed: {e}>"
            return f"\n--- /proc/$pid/status ---\n{status}\n--- /proc/$pid/maps (head) ---\n{maps}"

        assert metrics["VmRSS_kB"] < RSS_LIMIT_KB, (
            f"VmRSS={metrics['VmRSS_kB']} kB exceeds {RSS_LIMIT_KB} kB" + _diag()
        )
        assert metrics["Threads"] < THREADS_LIMIT, (
            f"Threads={metrics['Threads']} exceeds {THREADS_LIMIT}" + _diag()
        )
        assert metrics["VmData_kB"] < VMDATA_LIMIT_KB, (
            f"VmData={metrics['VmData_kB']} kB exceeds {VMDATA_LIMIT_KB} kB" + _diag()
        )

    finally:
        subprocess.call(
            ["docker", "compose", "-p", project, "-f", str(DEMO_COMPOSE), "down", "-v"],
            env=env,
            cwd=REPO_ROOT,
        )


def _parse_proc_status(blob: str) -> dict[str, int]:
    """Parse /proc/$pid/status into {VmRSS_kB, VmData_kB, Threads}."""
    out: dict[str, int] = {}
    for line in blob.splitlines():
        if line.startswith("VmRSS:"):
            out["VmRSS_kB"] = int(line.split()[1])
        elif line.startswith("VmData:"):
            out["VmData_kB"] = int(line.split()[1])
        elif line.startswith("Threads:"):
            out["Threads"] = int(line.split()[1])
    missing = {"VmRSS_kB", "VmData_kB", "Threads"} - out.keys()
    assert not missing, f"could not parse {missing} from /proc/$pid/status"
    return out
```

Verify the demo compose path: `ls nexus-stack.yml` from repo root. If demo uses a different file (e.g. `compose.demo.yml`), adjust `DEMO_COMPOSE` accordingly. Look at how the issue reproduces it: `docker compose -p nexus-demo up -d`.

- [ ] **Step 3: Run the test (full Docker round-trip)**

Run: `uv run pytest -m demo_memory tests/integration/test_demo_memory.py -v -s`
Expected: PASS — VmRSS reported in stdout under 450 MB, threads under 20.

If it fails: read the diagnostic dump in the assertion message. Common causes:
- jemalloc not preloaded (check LD_PRELOAD inside container)
- BLAS env not active before numpy import (verify `pip show numpy` location)
- search daemon still loading model (check `docker logs <container> | grep "Search backend mode"`)

- [ ] **Step 4: Commit**

```bash
git add tests/integration/test_demo_memory.py pyproject.toml
git commit -m "test(memory): demo profile RSS regression test (#3997)

pytest -m demo_memory boots demo compose, polls /healthz/ready,
parses /proc/\$pid/status from inside container.
Asserts VmRSS<450MB, Threads<20, VmData<4GB.
Diagnostic dump on failure (full status + maps head)."
```

---

## Task 8: CI workflow for memory regression

**Files:**
- Create: `.github/workflows/demo-memory.yml`

**Why:** Run the regression test on `develop` push + nightly cron + manual dispatch. Not blocking PR CI by default — operators can promote to required-status when stable.

- [ ] **Step 1: Inspect existing workflow patterns**

Run: `ls .github/workflows/ | head -10`
Expected: list of existing workflow files. Pick one (e.g. an integration-test workflow) and read its docker-setup steps to follow the established pattern.

Run: `head -40 .github/workflows/$(ls .github/workflows/ | grep -i 'integration\|test' | head -1)`
Expected: example of how runners are declared, how docker buildx is set up, how compose-using tests are launched.

- [ ] **Step 2: Create the workflow**

Create `.github/workflows/demo-memory.yml` (adjust runner labels and Python setup steps to match the patterns from Step 1):

```yaml
name: Demo Memory Regression

on:
  push:
    branches: [develop]
  schedule:
    - cron: "0 7 * * *"  # 07:00 UTC nightly
  workflow_dispatch:

jobs:
  demo-memory:
    name: nexusd demo idle RSS <= 450 MB
    runs-on: ubuntu-latest
    timeout-minutes: 20
    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Set up Docker Buildx
        uses: docker/setup-buildx-action@v3

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.14"

      - name: Install uv
        run: pip install uv

      - name: Install project (test extras only)
        run: uv sync --frozen --extra test

      - name: Build nexus image
        run: docker build -t nexus:demo-mem .

      - name: Run demo memory regression
        run: uv run pytest -m demo_memory tests/integration/test_demo_memory.py -v -s
        env:
          # Test target image tag
          NEXUS_TEST_IMAGE: nexus:demo-mem

      - name: Dump container logs on failure
        if: failure()
        run: |
          for c in $(docker ps -a --format '{{.Names}}' | grep nexus-demo-mem); do
            echo "=== $c ==="
            docker logs --tail 200 "$c" || true
          done
```

If the test references the locally-built image differently (e.g. compose pulls from registry by default), update `nexus-stack.yml` indirection or pass the image via env override in the test. Match the pattern used by other integration workflows in the repo.

- [ ] **Step 3: Validate the YAML**

Run: `uv run python -c "import yaml; yaml.safe_load(open('.github/workflows/demo-memory.yml'))"`
Expected: no output (no error).

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/demo-memory.yml
git commit -m "ci: nightly + on-push demo memory regression workflow (#3997)

Runs pytest -m demo_memory against built image. develop push +
07:00 UTC cron + manual dispatch. Not blocking PR CI by default."
```

---

## Task 9: End-to-end manual verification

**Files:**
- None (verification only)

**Why:** Confirm the full stack matches acceptance criteria before opening PR. Catches integration gaps unit tests miss (entrypoint env propagation, compose env passthrough, etc.).

- [ ] **Step 1: Build fresh image**

```bash
docker build -t nexus:rss-final .
```
Expected: clean build.

- [ ] **Step 2: Boot demo, sleep 30, measure**

```bash
docker compose -p nexus-rss-verify up -d
sleep 30
docker exec nexus-rss-verify-nexus-1 sh -c \
    'pid=$(pgrep -f nexusd | head -1); cat /proc/$pid/status | grep -E "^(VmRSS|VmHWM|VmData|Threads)"'
```
Expected: `VmRSS < 460000` (kB), `Threads < 20`, `VmData < 4194304`.

- [ ] **Step 3: Confirm jemalloc loaded**

```bash
docker exec nexus-rss-verify-nexus-1 sh -c \
    'pid=$(pgrep -f nexusd | head -1); grep -c jemalloc /proc/$pid/maps'
```
Expected: count > 0.

- [ ] **Step 4: Confirm BM25-only mode in logs**

```bash
docker logs nexus-rss-verify-nexus-1 2>&1 | grep -i "search backend mode"
```
Expected: `Search backend mode: bm25-only (model=<none>)`.

- [ ] **Step 5: Confirm no reranker loaded**

```bash
docker logs nexus-rss-verify-nexus-1 2>&1 | grep -i reranker
```
Expected: empty output (or only "Reranker init failed" suppressed because `_reranker_model` is None).

- [ ] **Step 6: Confirm BLAS caps active**

```bash
docker exec nexus-rss-verify-nexus-1 env | grep -E "OMP|OPENBLAS|MKL|NUMEXPR|VECLIB"
```
Expected: all =1.

- [ ] **Step 7: Functional smoke — search still works**

```bash
ADMIN_KEY=$(docker logs nexus-rss-verify-nexus-1 2>&1 | grep "API Key:" | head -1 | sed -n 's/.*API Key: *//p')
curl -fsS -H "Authorization: Bearer $ADMIN_KEY" \
    "http://localhost:2026/api/v2/search/query?q=test&zone_id=root"
```
Expected: 200 response with empty or sample results — proves BM25 path works end-to-end.

- [ ] **Step 8: Tear down**

```bash
docker compose -p nexus-rss-verify down -v
```

- [ ] **Step 9: If any check fails, capture diagnostic and STOP**

Don't open a PR with failing manual verification. Loop back to the relevant earlier task.

---

## Self-Review

After completing all tasks, this plan has been reviewed against the spec:

**Spec coverage:**
- Six change surfaces (Section 1) → Tasks 1-6
- Boot data flow (Section 2) → Tasks 2, 3, 4
- Component interfaces (Section 3) → Tasks 1-6 produce them
- Error handling (Section 4) → Task 6 (entrypoint), Task 4 (idle trimmer fallback), Task 3 (BM25 fallback already exists)
- Testing strategy (Section 5) → Tasks 1-4 (unit), Task 7 (integration), Task 8 (CI)

**Gaps:** None identified. The "behavioral envelopes" table in spec is verified by Task 9.

**Type consistency:** `_resolve_txtai_runtime_config() -> tuple[str | None, dict[str, str] | None]` consistent in Task 2 (signature) and Task 3 (caller passes `model: str | None` to `DaemonConfig.txtai_model: str | None`).

**Placeholder scan:** No "TBD"/"TODO"/"similar to". Each step has actual code or actual command.
