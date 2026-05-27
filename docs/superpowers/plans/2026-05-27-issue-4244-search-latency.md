# Issue #4244 Search Latency Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make PgFtsBackend search latency debuggable, fix admin-bypass `permission_filter_ms` accounting, and add an opt-in Postgres corpus-growth benchmark.

**Architecture:** Keep behavior changes narrow. Put the admin-bypass skip in the shared ReBAC filter helper, record per-leg timings inside `SearchDaemon._search_via_backends()`, serialize optional timing fields in the HTTP router, and add a skipped-by-default benchmark that seeds an isolated Postgres schema.

**Tech Stack:** Python 3.14, FastAPI router tests, pytest, pytest-asyncio, SQLAlchemy async engine, asyncpg, `uv run --no-sync`, Ruff.

---

## File Structure

- Modify `tests/unit/server/routers/test_search_rebac_filter.py`: add failing tests for admin-bypass accounting and no filter calls.
- Modify `src/nexus/lib/rebac_filter.py`: add the shared admin-bypass fast path.
- Modify `tests/unit/bricks/search/test_daemon_backend_routing.py`: add fake-backend tests for per-leg timing.
- Modify `src/nexus/bricks/search/daemon.py`: record backend timing details and preserve them through the outer search wrapper.
- Modify `tests/integration/services/test_search_router.py`: assert optional timing keys appear in `latency_breakdown`.
- Modify `src/nexus/server/api/v2/routers/search.py`: add optional timing fields to non-graph search responses.
- Create `tests/benchmarks/test_pg_fts_backend_latency.py`: opt-in corpus-growth benchmark for chunk and page PgFtsBackend searches.

## Task 1: Admin Bypass ReBAC Accounting

**Files:**
- Modify: `tests/unit/server/routers/test_search_rebac_filter.py`
- Modify: `src/nexus/lib/rebac_filter.py`

- [ ] **Step 1: Write failing tests for admin-bypass accounting**

Append the import and tests below in `tests/unit/server/routers/test_search_rebac_filter.py`.

Add the import near the existing imports:

```python
from types import SimpleNamespace
```

Add these tests inside `class TestApplyRebacFilterNoOpPaths` after `test_enforcer_without_filter_search_results_method`:

```python
    def test_admin_bypass_from_auth_result_skips_filter_work(self) -> None:
        results = [_StubResult("/a.py"), _StubResult("/b.py")]
        enforcer = MagicMock()
        enforcer.allow_admin_bypass = True
        enforcer.filter_list = MagicMock(return_value=[])
        enforcer.filter_search_results = MagicMock(return_value=[])
        enforcer.check = MagicMock(return_value=False)

        filtered, filter_ms = _apply_rebac_filter(
            results=results,
            permission_enforcer=enforcer,
            auth_result=_auth(is_admin=True),
            zone_id=ROOT_ZONE_ID,
        )

        assert filtered is results
        assert filter_ms == 0.0
        enforcer.filter_list.assert_not_called()
        enforcer.filter_search_results.assert_not_called()
        enforcer.check.assert_not_called()

    def test_admin_bypass_from_operation_context_skips_filter_work(self) -> None:
        results = [_StubResult("/a.py"), _StubResult("/b.py")]
        op_context = SimpleNamespace(is_admin=True)
        enforcer = MagicMock()
        enforcer.allow_admin_bypass = True
        enforcer.filter_list = MagicMock(return_value=[])
        enforcer.filter_search_results = MagicMock(return_value=[])
        enforcer.check = MagicMock(return_value=False)

        filtered, filter_ms = _apply_rebac_filter(
            results=results,
            permission_enforcer=enforcer,
            auth_result=_auth(is_admin=False),
            zone_id=ROOT_ZONE_ID,
            operation_context=op_context,
        )

        assert filtered is results
        assert filter_ms == 0.0
        enforcer.filter_list.assert_not_called()
        enforcer.filter_search_results.assert_not_called()
        enforcer.check.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run --no-sync pytest tests/unit/server/routers/test_search_rebac_filter.py::TestApplyRebacFilterNoOpPaths -o "addopts=" -q
```

Expected: the two new tests fail because `apply_rebac_filter()` still calls `filter_search_results()` or `filter_list()` and returns filtered results instead of the original list with `0.0` timing.

- [ ] **Step 3: Implement the minimal admin-bypass fast path**

In `src/nexus/lib/rebac_filter.py`, add this helper after `normalize_path()`:

```python
def _admin_bypass_enabled(
    permission_enforcer: Any,
    auth_result: dict[str, Any],
    operation_context: Any | None,
) -> bool:
    """Return whether search filtering should skip work for admin bypass."""
    if not bool(getattr(permission_enforcer, "allow_admin_bypass", False)):
        return False
    context_is_admin = bool(getattr(operation_context, "is_admin", False))
    auth_is_admin = bool(auth_result.get("is_admin", False))
    return context_is_admin or auth_is_admin
```

Then update the start of `apply_rebac_filter()` so it reads:

```python
    if permission_enforcer is None:
        return results, 0.0

    if _admin_bypass_enabled(permission_enforcer, auth_result, operation_context):
        return results, 0.0

    use_filter_list = operation_context is not None and hasattr(permission_enforcer, "filter_list")
```

- [ ] **Step 4: Run focused ReBAC tests to verify green**

Run:

```bash
uv run --no-sync pytest tests/unit/server/routers/test_search_rebac_filter.py -o "addopts=" -q
```

Expected: all tests in `test_search_rebac_filter.py` pass, including the existing inherited directory grant tests.

- [ ] **Step 5: Commit Task 1**

Run:

```bash
git add tests/unit/server/routers/test_search_rebac_filter.py src/nexus/lib/rebac_filter.py
git commit -m "fix(search): skip ReBAC timing for admin bypass"
```

Expected: commit succeeds after pre-commit hooks.

## Task 2: SearchDaemon Per-Leg Backend Timing

**Files:**
- Modify: `tests/unit/bricks/search/test_daemon_backend_routing.py`
- Modify: `src/nexus/bricks/search/daemon.py`

- [ ] **Step 1: Write failing unit tests for per-leg backend timings**

Append this helper code to `tests/unit/bricks/search/test_daemon_backend_routing.py` after `_daemon_with_backend_result()`:

```python
def _hit(path: str, score: float):
    from nexus.bricks.search.results import BaseSearchResult

    return BaseSearchResult(
        path=path,
        chunk_text=f"{path} body",
        score=score,
        chunk_index=0,
        keyword_score=score,
        vector_score=score,
        zone_id="root",
    )


def _daemon_with_real_backend_timing():
    from nexus.bricks.search.daemon import SearchDaemon
    from nexus.bricks.search.pg_fts_backend import PgFtsBackend

    daemon = SearchDaemon.__new__(SearchDaemon)
    daemon.last_search_timing = {}

    fts = PgFtsBackend.__new__(PgFtsBackend)

    async def keyword_search(self, query: str, path: str, k: int, zone_id: str):
        return [_hit("/chunk.md", 3.0)]

    async def keyword_search_pages(self, query: str, path: str, k: int, zone_id: str):
        return [_hit("/page.md", 2.0)]

    class VectorBackend:
        async def semantic_search(self, query_vector, path: str, k: int, zone_id: str):
            return [_hit("/dense.md", 1.0)]

    async def embed_query(self, query: str):
        return [0.1, 0.2, 0.3]

    fts.keyword_search = MethodType(keyword_search, fts)
    fts.keyword_search_pages = MethodType(keyword_search_pages, fts)
    daemon._fts_backend = fts
    daemon._vector_backend = VectorBackend()
    daemon._embed_query = MethodType(embed_query, daemon)
    return daemon
```

Append these tests at the bottom of the file:

```python
@pytest.mark.asyncio
async def test_pg_hybrid_backend_timing_records_each_leg():
    daemon = _daemon_with_real_backend_timing()

    results = await daemon._search_via_backends(
        "needle",
        search_type="hybrid",
        limit=3,
        path_filter="/",
        zone_id="root",
    )

    assert [result.path for result in results]
    timing = daemon.last_search_timing
    assert timing["backend_ms"] >= 0.0
    assert timing["embed_ms"] >= 0.0
    assert timing["keyword_ms"] >= 0.0
    assert timing["page_keyword_ms"] >= 0.0
    assert timing["vector_ms"] >= 0.0
    assert timing["fusion_ms"] >= 0.0
    assert timing["rerank_ms"] == 0.0


@pytest.mark.asyncio
async def test_keyword_backend_timing_records_keyword_and_total():
    daemon = _daemon_with_real_backend_timing()

    results = await daemon._search_via_backends(
        "needle",
        search_type="keyword",
        limit=3,
        path_filter="/",
        zone_id="root",
    )

    assert [result.path for result in results] == ["/chunk.md"]
    timing = daemon.last_search_timing
    assert timing["backend_ms"] >= 0.0
    assert timing["keyword_ms"] >= 0.0
    assert timing["embed_ms"] == 0.0
    assert timing["page_keyword_ms"] == 0.0
    assert timing["vector_ms"] == 0.0
    assert timing["fusion_ms"] == 0.0
    assert timing["rerank_ms"] == 0.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run --no-sync pytest tests/unit/bricks/search/test_daemon_backend_routing.py -o "addopts=" -q
```

Expected: the new timing tests fail with missing keys such as `embed_ms` or `keyword_ms`.

- [ ] **Step 3: Add per-leg timing inside `_search_via_backends()`**

In `src/nexus/bricks/search/daemon.py`, replace the body of `_search_via_backends()` from `path = path_filter or "/"` through the final hybrid return with this implementation:

```python
        path = path_filter or "/"
        timing: dict[str, float] = {
            "backend_ms": 0.0,
            "embed_ms": 0.0,
            "keyword_ms": 0.0,
            "page_keyword_ms": 0.0,
            "vector_ms": 0.0,
            "fusion_ms": 0.0,
            "rerank_ms": 0.0,
        }
        backend_start = time.perf_counter()

        async def _time_leg(name: str, awaitable: Any) -> Any:
            leg_start = time.perf_counter()
            try:
                return await awaitable
            finally:
                timing[name] = (time.perf_counter() - leg_start) * 1000

        def _publish_timing() -> None:
            timing["backend_ms"] = (time.perf_counter() - backend_start) * 1000
            self.last_search_timing = dict(timing)

        if search_type == "keyword":
            results = await _time_leg(
                "keyword_ms",
                self._fts_backend.keyword_search(query, path, limit, zone_id),
            )
            _publish_timing()
            return [self._coerce_to_search_result(r, search_type=search_type) for r in results]

        if search_type == "semantic":
            embed_start = time.perf_counter()
            qvec = await self._embed_query(query)
            timing["embed_ms"] = (time.perf_counter() - embed_start) * 1000
            if qvec is None:
                _publish_timing()
                return []
            results = await _time_leg(
                "vector_ms",
                self._vector_backend.semantic_search(qvec, path, limit, zone_id),
            )
            _publish_timing()
            return [self._coerce_to_search_result(r, search_type=search_type) for r in results]

        embed_start = time.perf_counter()
        qvec = await self._embed_query(query)
        timing["embed_ms"] = (time.perf_counter() - embed_start) * 1000
        if qvec is None:
            results = await _time_leg(
                "keyword_ms",
                self._fts_backend.keyword_search(query, path, limit, zone_id),
            )
            _publish_timing()
            return [self._coerce_to_search_result(r, search_type=search_type) for r in results]

        is_pg = isinstance(self._fts_backend, PgFtsBackend)
        if is_pg:
            chunk_kw, page_kw, dense = await asyncio.gather(
                _time_leg(
                    "keyword_ms",
                    self._fts_backend.keyword_search(query, path, limit * 2, zone_id),
                ),
                _time_leg(
                    "page_keyword_ms",
                    self._fts_backend.keyword_search_pages(query, path, limit * 2, zone_id),
                ),
                _time_leg(
                    "vector_ms",
                    self._vector_backend.semantic_search(qvec, path, limit * 2, zone_id),
                ),
                return_exceptions=False,
            )
        else:
            chunk_kw, dense = await asyncio.gather(
                _time_leg(
                    "keyword_ms",
                    self._fts_backend.keyword_search(query, path, limit * 2, zone_id),
                ),
                _time_leg(
                    "vector_ms",
                    self._vector_backend.semantic_search(qvec, path, limit * 2, zone_id),
                ),
                return_exceptions=False,
            )
            page_kw = []

        fusion_start = time.perf_counter()
        kw_fused = rrf_fusion(chunk_kw, page_kw, k=60, limit=limit * 2, id_key=None)
        fused = rrf_fusion(kw_fused, dense, k=60, limit=limit, id_key=None)
        timing["fusion_ms"] = (time.perf_counter() - fusion_start) * 1000
        _publish_timing()
        return [self._coerce_to_search_result(item, search_type="hybrid") for item in fused]
```

- [ ] **Step 4: Preserve detailed timing in outer search wrappers**

In `src/nexus/bricks/search/daemon.py`, replace both places where `_search_on_current_loop()` overwrites `self.last_search_timing` after `_search_via_backends()` with a merge that preserves optional keys.

For the keyword branch around the current `backend_ms = ...` line, use:

```python
                    backend_ms = (time.perf_counter() - backend_start) * 1000
                    backend_attempted = True
                    backend_timing = dict(getattr(self, "last_search_timing", {}) or {})
                    backend_timing["backend_ms"] = backend_ms
                    backend_timing.setdefault("rerank_ms", 0.0)
                    self.last_search_timing = backend_timing
```

For the later `if has_new_backends and not backend_attempted:` branch, use:

```python
                backend_ms = (time.perf_counter() - backend_start) * 1000
                backend_timing = dict(getattr(self, "last_search_timing", {}) or {})
                backend_timing["backend_ms"] = backend_ms
                backend_timing.setdefault("rerank_ms", 0.0)
                self.last_search_timing = backend_timing
                if hybrid_keyword_ms:
                    self.last_search_timing["keyword_ms"] = hybrid_keyword_ms
```

Leave the legacy keyword fallback timing unchanged because it has no per-leg backend data.

- [ ] **Step 5: Run focused daemon timing tests**

Run:

```bash
uv run --no-sync pytest tests/unit/bricks/search/test_daemon_backend_routing.py -o "addopts=" -q
```

Expected: all daemon backend routing tests pass.

- [ ] **Step 6: Commit Task 2**

Run:

```bash
git add tests/unit/bricks/search/test_daemon_backend_routing.py src/nexus/bricks/search/daemon.py
git commit -m "feat(search): expose per-leg backend timings"
```

Expected: commit succeeds after pre-commit hooks.

## Task 3: HTTP Search Latency Breakdown Serialization

**Files:**
- Modify: `tests/integration/services/test_search_router.py`
- Modify: `src/nexus/server/api/v2/routers/search.py`

- [ ] **Step 1: Write failing router test for optional timing fields**

In `tests/integration/services/test_search_router.py`, update the `app()` fixture after `mock_daemon.search = mock_search`:

```python
        mock_daemon.last_search_timing = {
            "backend_ms": 12.3456,
            "rerank_ms": 0.0,
            "embed_ms": 1.111,
            "keyword_ms": 2.222,
            "page_keyword_ms": 3.333,
            "vector_ms": 4.444,
            "fusion_ms": 5.555,
        }
```

Add this test after `test_valid_query`:

```python
    def test_latency_breakdown_includes_backend_leg_timings(self, client: "TestClient") -> None:
        resp = client.get("/api/v2/search/query?q=hello")

        assert resp.status_code == 200
        breakdown = resp.json()["latency_breakdown"]
        assert breakdown["backend_ms"] == 12.35
        assert breakdown["rerank_ms"] == 0.0
        assert breakdown["embed_ms"] == 1.11
        assert breakdown["keyword_ms"] == 2.22
        assert breakdown["page_keyword_ms"] == 3.33
        assert breakdown["vector_ms"] == 4.44
        assert breakdown["fusion_ms"] == 5.55
        assert "permission_filter_ms" in breakdown
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run --no-sync pytest tests/integration/services/test_search_router.py::TestSearchQueryEndpoint::test_latency_breakdown_includes_backend_leg_timings -o "addopts=" -q
```

Expected: fail with missing optional timing keys such as `embed_ms`.

- [ ] **Step 3: Add optional timing serialization**

In `src/nexus/server/api/v2/routers/search.py`, replace the response construction around the current inline `latency_breakdown` dict in the non-graph branch with:

```python
        latency_breakdown = {
            "total_ms": round(latency_ms, 2),
            "backend_ms": round(backend_ms, 2),
            "rerank_ms": round(rerank_ms, 2),
            "permission_filter_ms": round(filter_ms, 2),
        }
        for timing_key in (
            "embed_ms",
            "keyword_ms",
            "page_keyword_ms",
            "vector_ms",
            "fusion_ms",
        ):
            if timing_key in daemon_timing:
                latency_breakdown[timing_key] = round(float(daemon_timing[timing_key]), 2)

        response = {
            "query": q,
            "search_type": search_type,
            "graph_mode": "none",
            "results": [_serialize_search_result(r) for r in results],
            "total": len(results),
            "latency_ms": round(latency_ms, 2),
            "latency_breakdown": latency_breakdown,
            **_rebac_denial_stats(pre_filter_count, post_filter_count, effective_limit),
        }
```

Do not add these optional fields to the graph branch in this task because graph search does not use `_search_via_backends()`.

- [ ] **Step 4: Run router tests**

Run:

```bash
uv run --no-sync pytest tests/integration/services/test_search_router.py -o "addopts=" -q
```

Expected: all search router integration tests pass.

- [ ] **Step 5: Commit Task 3**

Run:

```bash
git add tests/integration/services/test_search_router.py src/nexus/server/api/v2/routers/search.py
git commit -m "feat(search): include backend leg timings in responses"
```

Expected: commit succeeds after pre-commit hooks.

## Task 4: PgFtsBackend Corpus-Growth Benchmark

**Files:**
- Create: `tests/benchmarks/test_pg_fts_backend_latency.py`

- [ ] **Step 1: Add the opt-in benchmark file**

Create `tests/benchmarks/test_pg_fts_backend_latency.py` with this complete content:

```python
"""PgFtsBackend corpus-growth latency benchmark for issue #4244.

Runs only when a Postgres URL is configured. The test creates an isolated schema
and sets search_path on the benchmark engine so it does not truncate application
tables in public.
"""

from __future__ import annotations

import math
import os
import time
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from nexus.bricks.search.pg_fts_backend import PgFtsBackend

_SCHEMA = "nexus_pg_fts_bench_4244"
_CORPUS_SIZES = (1000, 5000, 25000)
_SAMPLES_PER_MODE = 7


def _pg_url() -> str | None:
    url = (
        os.environ.get("NEXUS_TEST_DATABASE_URL")
        or os.environ.get("NEXUS_DATABASE_URL")
        or os.environ.get("POSTGRES_URL")
    )
    if not url:
        return None
    if url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url[len("postgresql://") :]
    if url.startswith("postgres://"):
        return "postgresql+asyncpg://" + url[len("postgres://") :]
    return url


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = max(0, min(len(ordered) - 1, math.ceil((percentile / 100.0) * len(ordered)) - 1))
    return ordered[index]


@pytest_asyncio.fixture
async def pg_fts_bench_engine() -> AsyncIterator[AsyncEngine]:
    url = _pg_url()
    if not url:
        pytest.skip(
            "No Postgres URL configured. Set NEXUS_TEST_DATABASE_URL to run PgFts benchmarks."
        )

    admin_engine = create_async_engine(url, echo=False)
    try:
        async with admin_engine.begin() as conn:
            await conn.execute(text(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE"))
            await conn.execute(text(f"CREATE SCHEMA {_SCHEMA}"))

        engine = create_async_engine(
            url,
            echo=False,
            connect_args={"server_settings": {"search_path": _SCHEMA}},
        )
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text("""
                    CREATE TABLE file_paths (
                        path_id TEXT PRIMARY KEY,
                        zone_id TEXT NOT NULL,
                        virtual_path TEXT NOT NULL,
                        deleted_at TIMESTAMPTZ,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    )
                    """)
                )
                await conn.execute(
                    text("""
                    CREATE TABLE document_chunks (
                        chunk_id TEXT PRIMARY KEY,
                        path_id TEXT NOT NULL REFERENCES file_paths(path_id) ON DELETE CASCADE,
                        chunk_index INTEGER NOT NULL,
                        chunk_text TEXT NOT NULL,
                        chunk_tokens INTEGER NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    )
                    """)
                )
                has_bm25 = (
                    await conn.execute(text("SELECT 1 FROM pg_am WHERE amname = 'bm25' LIMIT 1"))
                ).first()
                if has_bm25 is not None:
                    await conn.execute(
                        text("""
                        CREATE INDEX idx_chunks_bm25_bench
                        ON document_chunks USING bm25(chunk_text)
                        WITH (text_config='english')
                        """)
                    )

            yield engine
        finally:
            await engine.dispose()
    finally:
        async with admin_engine.begin() as conn:
            await conn.execute(text(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE"))
        await admin_engine.dispose()


async def _seed_corpus(engine: AsyncEngine, corpus_size: int) -> str:
    token = f"issue4244unique{corpus_size}"
    file_rows = []
    chunk_rows = []
    for index in range(corpus_size):
        path_id = f"p{index:05d}"
        file_rows.append(
            {
                "path_id": path_id,
                "zone_id": "root",
                "virtual_path": f"/bench/doc-{index:05d}.md",
            }
        )
        chunk_rows.append(
            {
                "chunk_id": f"c{index:05d}",
                "path_id": path_id,
                "chunk_index": 0,
                "chunk_text": (
                    f"Document {index} has common benchmark text. "
                    f"The matching document token is {token}."
                    if index == corpus_size - 1
                    else f"Document {index} has common benchmark text without the target token."
                ),
                "chunk_tokens": 16,
            }
        )

    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE document_chunks, file_paths"))
        await conn.execute(
            text("""
            INSERT INTO file_paths (path_id, zone_id, virtual_path)
            VALUES (:path_id, :zone_id, :virtual_path)
            """),
            file_rows,
        )
        await conn.execute(
            text("""
            INSERT INTO document_chunks (
                chunk_id, path_id, chunk_index, chunk_text, chunk_tokens
            )
            VALUES (:chunk_id, :path_id, :chunk_index, :chunk_text, :chunk_tokens)
            """),
            chunk_rows,
        )
    return token


async def _measure(label: str, call) -> tuple[list[float], int]:
    latencies: list[float] = []
    result_count = 0
    for _ in range(_SAMPLES_PER_MODE):
        started = time.perf_counter()
        results = await call()
        latencies.append((time.perf_counter() - started) * 1000)
        result_count = len(results)
    p50 = _percentile(latencies, 50)
    p99 = _percentile(latencies, 99)
    print(f"{label}: p50={p50:.2f}ms p99={p99:.2f}ms samples={latencies}")
    return latencies, result_count


@pytest.mark.asyncio
@pytest.mark.benchmark
async def test_pg_fts_backend_latency_scales_by_corpus_size(
    pg_fts_bench_engine: AsyncEngine,
) -> None:
    backend = PgFtsBackend(pg_fts_bench_engine)
    await backend.startup()

    for corpus_size in _CORPUS_SIZES:
        token = await _seed_corpus(pg_fts_bench_engine, corpus_size)

        _, chunk_count = await _measure(
            f"chunk corpus={corpus_size}",
            lambda token=token: backend.keyword_search(token, "/", 10, "root"),
        )
        _, page_count = await _measure(
            f"page corpus={corpus_size}",
            lambda token=token: backend.keyword_search_pages(token, "/", 10, "root"),
        )

        assert chunk_count >= 1
        assert page_count >= 1
```

- [ ] **Step 2: Run the benchmark without Postgres to verify clean skip**

Run:

```bash
env -u NEXUS_TEST_DATABASE_URL -u NEXUS_DATABASE_URL -u POSTGRES_URL \
  uv run --no-sync pytest tests/benchmarks/test_pg_fts_backend_latency.py -o "addopts=" -q
```

Expected: the benchmark file is collected and skipped with the "No Postgres URL configured" message.

- [ ] **Step 3: Commit Task 4**

Run:

```bash
git add tests/benchmarks/test_pg_fts_backend_latency.py
git commit -m "bench(search): add PgFts corpus latency benchmark"
```

Expected: commit succeeds after pre-commit hooks.

## Task 5: Final Verification

**Files:**
- Verify all files changed by Tasks 1-4.

- [ ] **Step 1: Run focused tests**

Run:

```bash
uv run --no-sync pytest tests/unit/server/routers/test_search_rebac_filter.py -o "addopts=" -q
uv run --no-sync pytest tests/unit/bricks/search/test_daemon_backend_routing.py -o "addopts=" -q
uv run --no-sync pytest tests/integration/services/test_search_router.py -o "addopts=" -q
```

Expected: all focused test files pass.

- [ ] **Step 2: Run benchmark skip check**

Run:

```bash
env -u NEXUS_TEST_DATABASE_URL -u NEXUS_DATABASE_URL -u POSTGRES_URL \
  uv run --no-sync pytest tests/benchmarks/test_pg_fts_backend_latency.py -o "addopts=" -q
```

Expected: one skipped benchmark test when no Postgres URL is configured.

- [ ] **Step 3: Run lint and format checks**

Run:

```bash
uv run --no-sync ruff check \
  src/nexus/lib/rebac_filter.py \
  src/nexus/bricks/search/daemon.py \
  src/nexus/server/api/v2/routers/search.py \
  tests/unit/server/routers/test_search_rebac_filter.py \
  tests/unit/bricks/search/test_daemon_backend_routing.py \
  tests/integration/services/test_search_router.py \
  tests/benchmarks/test_pg_fts_backend_latency.py
uv run --no-sync ruff format --check \
  src/nexus/lib/rebac_filter.py \
  src/nexus/bricks/search/daemon.py \
  src/nexus/server/api/v2/routers/search.py \
  tests/unit/server/routers/test_search_rebac_filter.py \
  tests/unit/bricks/search/test_daemon_backend_routing.py \
  tests/integration/services/test_search_router.py \
  tests/benchmarks/test_pg_fts_backend_latency.py
```

Expected: both Ruff commands pass.

- [ ] **Step 4: Run whitespace check**

Run:

```bash
git diff --check
```

Expected: no whitespace errors.

- [ ] **Step 5: Inspect final diff**

Run:

```bash
git diff --stat origin/develop...HEAD
git log --oneline origin/develop..HEAD
```

Expected: commits show the spec commit plus Task 1-4 implementation commits. The diff is limited to the files listed in this plan.
