# Design: Issue #4244 - PgFtsBackend Search Latency Regression

- **Status:** Approved design, awaiting spec review
- **Date:** 2026-05-27
- **Owner:** windoliver
- **Issue:** https://github.com/nexi-lab/nexus/issues/4244
- **Base branch:** `origin/develop`
- **Related work:** #3699 direct pgvector/pg_search backends, #3980 page-BM25, #4243 edge image triage bundle

## Problem

The current edge image at digest `sha256:df146704d372813fc8925f0025f8dade9074370709cd76753cfc6adb56b76f39`
regressed `nexus.search.query` latency on a 6289-document Postgres deployment.
The issue reports:

- `PgFtsBackend` plus `PgVectorBackend` in production.
- Average search latency around 1484 ms and p99 around 7946 ms.
- Previous edge baseline around p50 200 ms / p99 550 ms.
- Current smoke around p50 596 ms / p99 5022 ms.
- Per-query `latency_breakdown.backend_ms` around 976-1005 ms for single-hit
  hybrid search.
- `permission_filter_ms` around 358 ms even with `NEXUS_ALLOW_ADMIN_BYPASS=true`.

Two things are broken from an operator perspective:

1. The response timings do not identify which backend leg is responsible for
   the ~1s backend cost.
2. Admin-bypass search responses still report non-zero permission-filter time,
   which makes downstream diagnosis misleading and may reflect avoidable work.

## Context From Code Inspection

`origin/develop` has already switched search away from `txtai`:

- `src/nexus/bricks/search/pg_fts_backend.py` owns Postgres keyword search.
- `src/nexus/bricks/search/pg_vector_backend.py` owns Postgres vector search.
- `src/nexus/bricks/search/daemon.py` composes hybrid search through
  `_search_via_backends()`.
- `src/nexus/server/api/v2/routers/search.py` reports `backend_ms`,
  `rerank_ms`, and `permission_filter_ms`.
- `src/nexus/lib/rebac_filter.py` now prefers `filter_list()` when an
  `OperationContext` is provided, so search results use the same inherited
  directory-grant semantics as list/read operations.

The relevant hybrid Postgres path is:

```text
HTTP /api/v2/search/query
  -> SearchDaemon.search(...)
  -> SearchDaemon._search_via_backends(...)
  -> asyncio.gather(
       PgFtsBackend.keyword_search(...),
       PgFtsBackend.keyword_search_pages(...),
       PgVectorBackend.semantic_search(...),
     )
  -> rrf_fusion(chunk, page)
  -> rrf_fusion(keyword, dense)
  -> apply_rebac_filter(...)
```

Today, the daemon records one coarse `backend_ms` around the whole backend
composition. That hides whether the regression is chunk BM25, page BM25,
vector KNN, query embedding, fusion, or permission filtering.

## Goal

Make issue #4244 debuggable and fix the confirmed admin-bypass accounting bug
without broad search rewrites.

The implementation must:

- Add evidence that isolates backend latency by leg.
- Make true admin bypass report `permission_filter_ms = 0.0` and skip
  unnecessary permission-filter calls.
- Add a Postgres PgFts corpus-growth benchmark for 1000 / 5000 / 25000 docs.
- Preserve inherited directory-grant semantics for non-admin search callers.
- Keep changes narrow to search timing, ReBAC filtering accounting, and
  benchmark/test coverage.

## Non-Goals

- Do not redesign search ranking.
- Do not remove page-BM25.
- Do not change ReBAC authorization semantics for ordinary users.
- Do not add a new production metrics backend.
- Do not make a live Postgres service mandatory for ordinary unit-test runs.
- Do not solve downstream fan-out concurrency in the consuming app.

## Design

### 1. Admin-bypass accounting in `apply_rebac_filter`

Add an explicit fast path in `src/nexus/lib/rebac_filter.py` before result
path normalization and before `filter_list()` or `filter_search_results()` is
called:

```text
if caller is admin and permission_enforcer.allow_admin_bypass is true:
    return original results, 0.0
```

The admin flag should accept either `operation_context.is_admin` or
`auth_result["is_admin"]` because HTTP and MCP callers can arrive with slightly
different context shapes. The bypass should also require
`permission_enforcer.allow_admin_bypass` so the production kill switch remains
authoritative.

This matches the current `PermissionEnforcer.filter_list()` and
`filter_search_results()` behavior: both return input paths immediately when
admin bypass is active. Moving that branch into the shared helper improves
accounting and avoids timing path normalization plus a filter call as
permission-filter work.

For non-admin callers, keep the existing behavior:

- `operation_context` plus `filter_list()` uses inherited grant semantics.
- `check()` fallback recovers exact readable paths denied by a fast list path.
- `filter_search_results()` remains the duck-typed fallback when no operation
  context is available.

### 2. Per-leg backend timing

Extend `SearchDaemon._search_via_backends()` timing without changing its public
return type. The daemon should populate `last_search_timing` with stable keys:

- `backend_ms`: total time spent in `_search_via_backends()`.
- `embed_ms`: query embedding time when semantic or hybrid search embeds a
  query; `0.0` when no embedding is attempted.
- `keyword_ms`: chunk keyword leg time.
- `page_keyword_ms`: page-BM25 leg time; `0.0` for SQLite or keyword-only paths
  without page search.
- `vector_ms`: vector backend time.
- `fusion_ms`: RRF fusion time.
- `rerank_ms`: keep existing key, `0.0` for the direct PgFts/PgVector path.

Implementation detail: use small local async wrappers around each gathered leg
so each awaited backend call records its own elapsed milliseconds. Keep the
outer `backend_ms` around the whole method so existing clients keep a single
headline number.

The FastAPI search router should include these timing keys in
`latency_breakdown` when they are present on `search_daemon.last_search_timing`.
Existing fields remain stable, and missing optional keys are simply omitted.

### 3. PgFts corpus-growth benchmark

Add a benchmark under `tests/benchmarks/` for PgFtsBackend search latency over
seeded Postgres data:

```text
tests/benchmarks/test_pg_fts_backend_latency.py
```

The benchmark should:

- Skip unless `NEXUS_TEST_DATABASE_URL`, `NEXUS_DATABASE_URL`, or
  `POSTGRES_URL` points at a live Postgres instance.
- Create or truncate only the minimal `file_paths` and `document_chunks` tables
  needed by `PgFtsBackend`.
- Seed 1000, 5000, and 25000 documents with one unique query token per corpus.
- Run chunk keyword search and page keyword search separately.
- Record p50 and p99 through `pytest-benchmark` or a deterministic small sample
  loop.
- Emit enough context in assertion messages or benchmark extras to identify
  whether the page-level path scales non-linearly.

The first implementation should avoid a hard absolute performance threshold
because local and CI Postgres hosts vary. The benchmark's purpose is to produce
comparable numbers for this regression and future PR review. If CI later runs a
stable Postgres benchmark host, thresholding can be added as a separate issue.

### 4. Tests

Follow TDD for production behavior changes.

Add unit coverage for the ReBAC accounting fast path:

- Admin plus `allow_admin_bypass=True` returns the original result list.
- `permission_filter_ms` is exactly `0.0`.
- `filter_list`, `filter_search_results`, and `check` are not called.
- Non-admin operation-context tests continue to exercise `filter_list()` and the
  exact-read fallback.

Add search timing coverage around fake backends:

- Hybrid Postgres-like search records `keyword_ms`, `page_keyword_ms`,
  `vector_ms`, `fusion_ms`, and `backend_ms`.
- Keyword search records `keyword_ms` and `backend_ms`.
- Router responses preserve the existing `latency_breakdown` fields and include
  optional per-leg keys when present.

Postgres benchmark coverage remains opt-in and skipped without a configured DB.

## Error Handling

Search behavior must remain fail-soft where it already is fail-soft:

- If embeddings cannot be generated, hybrid search still falls back to keyword
  results.
- If a backend raises today, existing error behavior should not become broader.
- If the benchmark cannot find Postgres, it skips with a clear message.

The admin-bypass fast path must not swallow non-admin permission errors because
it only runs when the enforcer explicitly has bypass enabled and the caller is
admin.

## Verification

Local verification for the implementation branch should include:

```bash
uv run --no-sync pytest tests/unit/server/routers/test_search_rebac_filter.py -o "addopts="
uv run --no-sync pytest tests/unit/bricks/search/test_daemon_backend_routing.py -o "addopts="
uv run --no-sync pytest tests/integration/services/test_search_router.py -o "addopts="
uv run --no-sync ruff check src/nexus/lib/rebac_filter.py src/nexus/bricks/search/daemon.py src/nexus/server/api/v2/routers/search.py tests/unit/server/routers/test_search_rebac_filter.py tests/unit/bricks/search/test_daemon_backend_routing.py tests/integration/services/test_search_router.py
uv run --no-sync ruff format --check src/nexus/lib/rebac_filter.py src/nexus/bricks/search/daemon.py src/nexus/server/api/v2/routers/search.py tests/unit/server/routers/test_search_rebac_filter.py tests/unit/bricks/search/test_daemon_backend_routing.py tests/integration/services/test_search_router.py
git diff --check
```

Opt-in Postgres benchmark command:

```bash
NEXUS_TEST_DATABASE_URL=postgresql://... \
uv run --no-sync pytest tests/benchmarks/test_pg_fts_backend_latency.py \
  -o "addopts=" --benchmark-min-rounds=3 -v
```

## Acceptance Criteria

- `permission_filter_ms` is `0.0` for true admin bypass and tests prove no
  filter call was made.
- Non-admin inherited directory grants continue to work through `filter_list()`.
- `/api/v2/search/query` includes per-leg timing fields when the daemon records
  them.
- Hybrid Pg search timing identifies chunk keyword, page keyword, vector,
  embedding, fusion, and total backend time.
- A skipped-by-default Postgres corpus-growth benchmark exists for 1000 / 5000 /
  25000 docs.
- No unrelated search ranking or ReBAC authorization semantics change.
