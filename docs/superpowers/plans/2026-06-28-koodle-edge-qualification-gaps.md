# Koodle Edge Qualification Gaps Implementation Plan

> **For Codex:** Execute every change test-first. Keep subsystem diffs independent,
> run each focused suite before its commit, and never deploy or mutate Koodle production.

**Goal:** Fix all eight Nexus-owned defects found while qualifying Koodle against the
current edge image, then open one upstream draft PR against `develop`.

**Architecture:** Preserve public APIs and fix each defect at its owning layer:
anchor-aware macro expansion, finite vector results, collapse-before-gate mutation
handling, correct HTTP/VFS adapters, request-scoped zone sessions plus schema alignment,
and bounded lifecycle-managed cache warmup. Koodle compatibility fallbacks and
`nexus-vfs` are excluded.

**Stack:** Python 3.14, FastAPI, SQLAlchemy/Alembic, PostgreSQL + pgvector, pytest,
Ruff, mypy, `uv`, and GitHub CLI.

**Worktree:** `/Users/tafeng/.codex/worktrees/21d7/nexus-upstream-gaps`

---

## Task 1: Search correctness

**Files:**

- Modify `src/nexus/bricks/search/macro_chunk.py`
- Modify `src/nexus/bricks/search/pg_vector_backend.py`
- Modify `src/nexus/bricks/search/daemon.py`
- Test `tests/unit/bricks/search/test_macro_chunk_expand.py`
- Test `tests/unit/bricks/search/test_pg_vector_backend.py`
- Test `tests/integration/bricks/search/test_search_mutation_flow.py`

### 1.1 Macro window: RED

Add one over-budget heading section with two hits near opposite ends. Expand both in a
single call and assert each macro contains its own anchor, their texts differ, and line
bounds match their actual windows.

Run `uv run pytest tests/unit/bricks/search/test_macro_chunk_expand.py -q --override-ini='addopts='`.

Expected failure: the second hit reuses the first cached macro.

### 1.2 Macro window: GREEN

Compute `_window_for_anchor()` before lookup and cache by
`(path, window_lo, window_hi)`. Preserve sharing for identical actual windows and the
best-effort exception contract. Re-run the test file.

### 1.3 Pgvector: RED

Add tests proving empty, all-zero, NaN, and infinity query vectors return `[]` without
opening a connection. Add mock-row tests proving `None`, NaN, and infinity scores are
dropped while finite scores retain ordering. Assert the SQL contains a stored halfvec
norm guard.

Run `uv run pytest tests/unit/bricks/search/test_pg_vector_backend.py -q --override-ini='addopts='`.

Expected failure: invalid queries execute SQL and invalid scores reach result creation.

### 1.4 Pgvector: GREEN

Require every component to pass `math.isfinite()` and the query Euclidean norm to be
strictly positive. Add `l2_norm(c.embedding) > 0` to SQL; pgvector documents this for
`halfvec`, whose zero vectors are not valid cosine-index candidates. Build results in a
loop that skips missing/non-finite scores. Re-run the test file.

### 1.5 Mutation ordering: RED

Add three cases for one `(zone_id, doc_id)` batch:

1. unresolved UPSERT then DELETE keeps only DELETE and creates no retry/park;
2. unresolved UPSERT then newer resolved UPSERT keeps only the newer event;
3. newest unresolved UPSERT still raises the existing unresolved-mutation error.

Run `uv run pytest tests/integration/bricks/search/test_search_mutation_flow.py -q --override-ini='addopts='`.

Expected failure: the obsolete UPSERT is gated first.

### 1.6 Mutation ordering: GREEN

Resolve the raw batch, call `_collapse_resolved_mutations()`, then apply
`_gate_unresolved()` for non-legacy consumers. Remove redundant post-resolution
collapse at both consumer call sites. Preserve the legacy refresh delete path.

### 1.7 Verify and commit

Run all three search test files together, then Ruff on the six changed files. Stage
only this task and commit `fix(search): preserve valid expansion and mutation results`.

---

## Task 2: Async file permission and VFS grep correctness

**Files:**

- Modify `src/nexus/server/api/v2/routers/async_files.py`
- Test `src/nexus/server/api/v2/routers/tests/test_files_search.py`
- Test `tests/integration/server/api/v2/routers/test_files_ops.py`
- Test, if its fixtures are needed, `tests/integration/server/api/v2/routers/test_batch_write_read.py`

### 2.1 Permission mapping: RED

Parameterize representative read, write, list/exists, search, and bulk routes so their
checker/NexusFS mocks raise built-in `PermissionError`. Assert HTTP 403 and preserve
existing per-item bulk behavior. Keep the existing `NexusPermissionError` coverage.

Run the two primary router test files. Expected failure: built-in permission denials
flow through the generic HTTP 500 arm.

### 2.2 Permission mapping: GREEN

Use one module-level permission-exception tuple or equivalent helper in every affected
`async_files.py` permission arm, including nested bulk handling. Preserve distinct
invalid-path, not-found, and validation responses. Do not catch arbitrary exceptions.

### 2.3 Grep semantics: RED

Add tests for:

- a virtual `/workspace/doc.md` read through `fs.read()`;
- a positively resolved local physical file searched by mmap but reported by its
  original virtual path;
- a mixed local/virtual set merged under one global limit;
- invalid regex returning HTTP 400 before scanning;
- empty local mmap results not suppressing virtual scanning.

Expected failure: virtual paths are passed to host mmap and return a false empty result.

### 2.4 Grep semantics: GREEN

Resolve virtual paths to physical paths only through a Nexus service/backend API. Send
only positively resolved local files to `grep_files_mmap`, keep a reverse response-path
map, and use compiled Python regex plus `fs.read()` for unresolved paths. Merge both
lanes under one limit. Never use `Path.is_file()` on a virtual string as authority.

### 2.5 Verify and commit

Run the three affected router suites with `--override-ini='addopts='`, then Ruff on
changed files. Commit `fix(api): preserve permission and virtual grep semantics`.

---

## Task 3: Zone sessions and graph-schema alignment

**Files:**

- Modify `src/nexus/server/auth/zone_routes.py`
- Add `alembic/versions/align_graph_zone_columns.py`
- Test `tests/e2e/server/test_zone_routes_e2e.py`
- Test `tests/e2e/server/test_zone_deprovision_permissions_e2e.py`
- Add `tests/migrations/test_graph_zone_column_migration.py`
- Verify `tests/e2e/postgres/migrations/test_harness.py`

### 3.1 API-key sessions: RED

Build route-level apps where `request.app.state.session_factory` is valid but the
module-global `DatabaseLocalAuth` provider is absent. Cover pure `DatabaseAPIKeyAuth`
and chained static+database auth for create/get/list/delete. Assert no route returns 503
after successful authentication.

Run `uv run pytest tests/e2e/server/test_zone_routes_e2e.py -q --override-ini='addopts='`.

Expected failure: create/get/delete fail during dependency resolution.

### 3.2 API-key sessions: GREEN

Make `_get_session_factory(request)` prefer `app.state.session_factory`, then unwrap
the active app-state provider, then use the legacy local-auth and NexusFS fallbacks.
Add `Request` to create/delete, remove their local-auth-only dependencies, and use the
helper in create/get/list/delete. Keep authentication and authorization unchanged.

### 3.3 Schema migration: RED

Add a focused migration test with two preconditions: legacy graph tables containing
`tenant_id`, and already-correct tables containing `zone_id`. Assert upgrade ends with
`zone_id`, preserves rows, and safely avoids a second rename. Add a deprovision test
with target/control graph and ReBAC rows; only target rows may be removed.

Run the new migration test and the zone-deprovision suite. Expected failure: no
alignment revision exists and migrated schemas reject `zone_id` cleanup SQL.

### 3.4 Schema migration: GREEN

Create revision `align_graph_zone_columns` with down revision
`add_chunk_heading_prefix`. Inspect both graph tables and rename `tenant_id` to
`zone_id` only if legacy exists and target does not. Preserve data and rename/rebuild
tenant-named indexes and uniqueness constraints to zone-named equivalents on
PostgreSQL. Keep `rebac_tuples.zone_id` unchanged. Support the repository's SQLite
migration harness and PostgreSQL using guarded or dialect-specific operations.

### 3.5 Verify and commit

Run both zone suites, the new migration test, and
`TestMigrationPreChecks::test_single_head_fast`. Run
`uv run alembic -c alembic/alembic.ini heads` and require exactly
`align_graph_zone_columns`. Run Ruff, then commit
`fix(zones): support API key sessions and migrated graph data`.

---

## Task 4: Bounded lifecycle-managed startup warmup

**Files:**

- Modify `src/nexus/server/lifespan/permissions.py`
- Modify `src/nexus/server/cache_warmer.py`
- Add `tests/unit/server/test_startup_cache_warmup.py`
- Modify `tests/e2e/server/test_cache_warmup_e2e.py`

### 4.1 Startup lifecycle: RED

Assert false values for `NEXUS_CACHE_WARMUP_ENABLED` create no task; default and true
values create one tracked task; invalid numeric configuration follows the existing
skip/log behavior. Assert `startup_permissions()` returns the task for shutdown
cancellation/awaiting.

Run `uv run pytest tests/unit/server/test_startup_cache_warmup.py -q --override-ini='addopts='`.

Expected failure: the enable flag is ignored and the task is fire-and-forget.

### 4.2 Startup lifecycle: GREEN

Parse standard true/false values with a compatibility default of true. Return an empty
or one-task list from `_startup_cache_warmup()` and extend the lifespan background-task
collection. Preserve unavailable-service and invalid-config logging behavior.

### 4.3 Bounded traversal: RED

Use an instrumented fake tree wider/deeper than the limits. Assert discovery stops at
`max_files`, does not list below `depth`, performs zero calls for non-positive limits,
never calls recursive `search.glob()`, and sends only files to metadata warmup.

Expected failure: current code recursively enumerates everything before slicing.

### 4.4 Bounded traversal: GREEN

Replace glob with a private breadth-first discovery helper using non-recursive,
paginated listing. Queue child directories only below `depth` and stop requesting
pages/directories as soon as `max_files` files are collected. Accept both
`PaginatedResult` and list results for lightweight test fakes. Preserve zone/context
propagation and content-warmup behavior.

### 4.5 Verify and commit

Run the new unit suite and cache warmup e2e suite, then Ruff on changed files. Commit
`fix(startup): bound and track cache warmup`.

---

## Task 5: Integrated verification, reviews, and draft PR

### 5.1 Scope and dependency audit

Run `git status --short`, `git diff --stat origin/develop...HEAD`,
`git diff --check origin/develop...HEAD`, and
`git diff --name-only origin/develop...HEAD`. Restore `uv.lock` if tooling changed it
without an intentional dependency update. Reject any Koodle, Railway, Vercel, Supabase,
or `nexus-vfs` file.

### 5.2 Combined verification

Run all affected search, file-router, zone, migration, and warmup test files in one
pytest invocation with repository `addopts` disabled. Run `ruff check`,
`ruff format --check`, and focused mypy on all changed production Python files. Re-run
the single-head migration pre-check.

### 5.3 Two-stage review

Have one reviewer compare the diff to the approved design/plan and resolve every
missing or extra behavior. Then have a separate reviewer inspect code quality,
lifecycle/concurrency behavior, SQL safety, migration idempotence, and test strength.
Resolve findings and rerun affected checks.

### 5.4 Freshness and publication

Fetch `origin/develop`; if it advanced, rebase and rerun all affected checks. Push
`codex/fix-edge-qualification-gaps` and open one draft PR titled
`fix: close Koodle edge qualification gaps` against `develop`.

The PR body must enumerate all eight fixes, historical links (#4354, #4352, #4398,
#4399, #4337/#4350, #4252, #1076), exact test results, pgvector behavior, and the note
that production was not deployed. Report the PR URL and CI state; do not merge or deploy.
