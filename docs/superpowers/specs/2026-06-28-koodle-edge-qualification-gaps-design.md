# Koodle edge qualification gaps — upstream Nexus fixes

- **Date**: 2026-06-28
- **Base**: `nexi-lab/nexus@0e7d247064af51486bd4b402383a834567f5fa65`
- **Status**: design approved by requester (all validated upstream gaps in one PR)
- **Deployment constraint**: do not modify or deploy Koodle production Railway

## Context

Koodle was qualified locally against the published Nexus edge image that corresponds
to the base commit above. Browser- and API-backed validation exposed eight defects
whose root causes are in Nexus rather than Koodle or `nexus-vfs`. The Koodle wrapper
contains temporary compatibility patches, but those should not become the permanent
implementation.

The objective is one upstream draft PR against `develop`, organized as independent
commits so maintainers can review each subsystem separately. The original `~/nexus`
checkout is dirty and remains untouched; work is isolated in a dedicated worktree.

## Approaches considered

### A. One comprehensive PR with subsystem commits — selected

Fix all eight validated gaps in one draft PR, split into search, file API,
zone/schema, and startup-lifecycle commits. This gives Koodle one upstream revision to
qualify before deployment and keeps each concern independently reviewable. The tradeoff
is a broader PR.

### B. Three or four independent PRs

Split search, file/HTTP, zone/schema, and startup performance into separate PRs. This
reduces review surface per PR but creates multiple merge and image-publication gates
before Koodle can deploy a single known-good Nexus version.

### C. Correctness-only release blocker

Fix the seven correctness/API defects and defer startup warmup. This is the smallest
release-blocker diff, but it leaves the observed large-namespace startup performance
regression unresolved and does not satisfy the request to fix all validated gaps.

## Scope

### 1. Search correctness

#### Macro expansion cache anchors

`expand_results()` currently caches a macro by `(path, section_lo, section_hi)`, even
though an over-budget macro window is also a function of the hit anchor. Two distant
hits in one large section can therefore receive the same macro, and the later result
may not contain its own matched chunk.

Compute the anchor-specific window first and cache the stitched result by its actual
`(path, window_lo, window_hi)` bounds. Every expanded hit must contain its own anchor.
This is a corrective follow-up to #4398/#4399; it does not change search ranking or
the opt-in `expand=macro` contract.

#### Finite pgvector scores

Cosine similarity is undefined for a zero-norm vector. Reject empty, zero-norm, and
non-finite query vectors before SQL. Exclude zero-norm stored embeddings in the query
where the pgvector/PostgreSQL function is available, and defensively discard database
rows whose score is `NULL` or non-finite before constructing a result. The HTTP
serialization boundary must never emit `NaN`, infinity, or an accidental `null`
score for a semantic result.

#### Collapse mutations before unresolved gating

The mutation resolver currently applies retry/parking to an unresolved UPSERT before
collapsing later events for the same `(zone_id, doc_id)`. Resolve the batch, collapse
it to the newest mutation per document, then apply the unresolved gate. A later DELETE
or resolved UPSERT must supersede an obsolete unresolved write; the newest unresolved
UPSERT must continue to block under the existing bounded-retry contract from #4337.

### 2. File API correctness and UX

#### Permission denials map to HTTP 403

The active ReBAC checker raises Python's built-in `PermissionError`, while the async
file router generally catches only `NexusPermissionError`; the generic arm turns a
normal authorization denial into HTTP 500. Treat both exception types as permission
denials in every affected async file route, while preserving any existing
`AccessDeniedError` handling and bulk-result semantics. This closes the still-valid
behavior recorded in #4354 without masking unrelated exceptions.

#### Grep reads virtual files through NexusFS

`GET /api/v2/files/grep` passes Nexus virtual paths directly to the host mmap helper.
The helper returns an empty list on host `OSError`, which is incorrectly interpreted
as a successful no-match and suppresses the VFS fallback.

Use mmap only for paths that Nexus positively resolves to local physical files. Keep
a virtual-to-physical mapping so response paths remain virtual, and run the VFS read
fallback for unresolved or non-local paths. Mixed local and virtual working sets must
search both sources, respect the global limit, and preserve invalid-regex HTTP 400
behavior. A coincidental host path with the same string as a virtual path must not be
treated as authoritative.

### 3. Zone API and schema correctness

#### Request-scoped session resolution

Zone create/get/delete endpoints depend on the module-global `DatabaseLocalAuth`
provider, so pure database API-key and chained-auth deployments authenticate and then
fail dependency resolution with 503. Remove that local-auth-only dependency. Resolve
the session factory from `request.app.state.session_factory` first, followed by the
active app-state provider and legacy NexusFS/local-auth fallbacks. List/create/get/
delete then share one request-scoped database-session path.

#### Align migrated graph columns with the ORM

The graph-table migration created `entities.tenant_id` and
`relationships.tenant_id`, while the current ORM and finalizer use `zone_id`. Add an
idempotent Alembic migration that renames legacy `tenant_id` columns to `zone_id` while
preserving data, indexes, and uniqueness constraints. It must be safe for databases
whose columns are already correct. `rebac_tuples.zone_id` remains unchanged.

Zone deprovision tests will seed target-zone and control-zone graph/ReBAC records and
prove only the target zone is removed. The temporary Koodle query rewrite to
`tenant_id` is not copied upstream because it would preserve the schema split.

### 4. Bounded startup cache warmup

Startup currently schedules warmup unconditionally; `CacheWarmer` recursively globs
the entire namespace and only then slices to `max_files`, while depth values of two or
greater all become `**/*`. On large workspaces this produces an avoidable full scan.

Honor `NEXUS_CACHE_WARMUP_ENABLED` (default remains enabled for compatibility), parse
standard true/false values, and skip task creation when disabled. Replace the
full-tree glob with bounded paginated listing/traversal that stops at both configured
depth and file count. Return the created task through the lifespan background-task
list so shutdown cancellation and task exceptions follow the existing lifecycle
contract. Invalid non-positive limits should perform no scan.

The implementation must bound discovery work rather than merely truncate the final
result. Tests will use an instrumented fake listing service to assert that traversal
does not request additional pages/directories after the cap is reached.

## Testing strategy

Each production change is test-driven: add the regression, observe it fail against
the base commit, then implement the narrow fix.

1. Unit tests for anchor-specific macro windows and finite pgvector inputs/results.
2. Mutation-flow tests for superseding DELETE, superseding resolved UPSERT, and newest
   unresolved UPSERT behavior.
3. Router tests using the real built-in `PermissionError`, plus VFS-only, local-only,
   and mixed-path grep cases.
4. Zone endpoint tests with database API-key and chained auth, and migration-backed
   PostgreSQL coverage for legacy and already-renamed graph schemas.
5. Warmup tests for disabled startup, max-file early stop, depth enforcement, task
   tracking, failure observation, and shutdown cancellation.
6. Focused lint/type checks, the affected unit/integration/e2e suites, Alembic upgrade
   checks, and final diff review. No production deploy is part of validation.

## PR organization

The draft PR targets `develop` and uses four focused implementation commits after this
design/plan documentation:

1. search expansion, vector-score, and mutation ordering fixes;
2. async file permission and VFS grep fixes;
3. zone session resolution and graph-schema migration;
4. bounded, configurable startup warmup.

The PR body will include the exact base/image qualification context, linked historical
issues/PRs, regression commands, and a deployment note that Koodle should consume a
new edge digest only after upstream CI and local qualification pass.

## Out of scope

- Koodle SDK schema fields and `macroText ?? chunkText` context assembly from Koodle
  #1431; those remain Koodle-owned and require Nexus #4399 or newer.
- Koodle-side nullable-score compatibility and agent fallback behavior after the
  upstream server stops producing non-finite scores.
- Changes to `nexus-vfs`; none of the validated root causes are in that repository.
- Any production Railway, Vercel, or Supabase mutation or deployment.

## Success criteria

1. All eight regressions fail on the base behavior and pass with the patch.
2. Existing focused test suites, lint, and type checks remain green.
3. No unrelated files from the dirty `~/nexus` checkout enter the branch.
4. A draft PR is open against `nexi-lab/nexus:develop` with CI-visible commits and
   no production deployment.
