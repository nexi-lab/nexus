# Design: Issue #4139 - Exhaustive API/RPC Surface Matrix Enforcement

- **Status:** Approved
- **Date:** 2026-05-22
- **Owner:** windoliver
- **Issue:** https://github.com/nexi-lab/nexus/issues/4139
- **Depends on:** #4161 / PR #4164 surface-map scaffold
- **Related epics:** #4119 (lite), #4120 (sandbox), #4121 (full)

## Goal

Complete the shared profile CLI/RPC coverage matrix work by turning the #4164
surface-map scaffold into an enforceable, deterministic inventory workflow.
The result should make drift visible in CI and require every external surface to
fit the shared model:

```text
profile -> module/group -> external API/RPC surface -> how/when to use -> correctness tests -> performance classification -> gap issue
```

This is full-scope #4139 work. It is not a docs-only pass. The implementation
must preserve the existing extractor/renderer contract while adding validation
that proves every surface is either owned, tested, classified, and documented, or
explicitly linked to a gap issue.

## Existing Foundation

PR #4164 already landed:

- `scripts/gen_api_surface_coverage.py`
- `scripts/render_api_surface_coverage.py`
- `scripts/surface_coverage/`
- `docs/architecture/api-rpc-surface-coverage.yaml`
- `docs/architecture/api-rpc-surface-coverage.html`
- `docs/architecture/api-rpc-surface-contract.md`
- `docs/architecture/api-rpc-surface-gaps.yaml`
- `docs/architecture/api-rpc-surface-overrides.yaml`
- `tests/architecture/test_inventory.py`

#4139 builds on that foundation. It should not replace the scaffold with a new
artifact shape.

## Non-Goals

- Do not redesign the HTML page.
- Do not create a second source of truth outside
  `docs/architecture/api-rpc-surface-coverage.yaml`.
- Do not hand-author a static API list.
- Do not require every profile subissue to be completed in this PR. Instead,
  make incomplete rows fail or report through structured, intentional validation
  paths.
- Do not make the cluster kernel binary build automatic inside ordinary unit
  tests. Runtime discovery may be skipped with a clear message when
  `nexusd-cluster` is not built, but the workflow and command must exist.

## Architecture

#4139 adds a validation layer next to extraction and rendering:

```text
source anchors
  -> scripts/gen_api_surface_coverage.py
  -> docs/architecture/api-rpc-surface-coverage.yaml
  -> scripts/surface_coverage/validate.py
  -> tests/architecture/test_inventory.py
  -> docs/architecture/api-rpc-surface-coverage.html
```

The extractor continues to discover surfaces. The renderer continues to publish
the browsable view. The new validator owns completeness rules that can be tested
independently and then called from CI-facing tests.

## Components

### `scripts/surface_coverage/validate.py`

Create a focused validator module that accepts a loaded `SurfaceCoverage` object
and returns structured validation findings. Findings should include:

- `code`: stable machine-readable rule id,
- `operation_id`: row id when applicable,
- `field`: field name when applicable,
- `severity`: `error` or `warning`,
- `message`: concise human-readable explanation.

Validation rules:

- `supported_missing_owner`: supported rows need `owning_issue`.
- `supported_missing_test`: supported rows need `correctness_test`.
- `supported_missing_perf_class`: supported rows need `perf_class`.
- `supported_missing_perf_link`: supported rows need `perf_link`.
- `gap_missing_issue`: rows with `missing_needed`, `deprecated`,
  `unavailable`, stale status, or explicit gap semantics need `gap_issue`.
- `invalid_test_reference`: `correctness_test` must look like a repository path
  plus optional line reference and the path must exist.
- `invalid_perf_reference`: benchmark-like `perf_link` paths must exist; plain
  rationale text is allowed for `not_perf_sensitive`.
- `missing_profile_status`: every row must have `lite`, `sandbox`, and `full`.
- `new_surface_drift`: freshly extracted surfaces absent from committed YAML are
  hard failures.
- `render_drift`: rendered HTML must match committed HTML.

The validator must be deterministic: same input produces findings in the same
order.

### Runtime Discovery

Add a runtime discovery helper for:

```python
create_app(...).state.exposed_methods
```

This helper should be isolated so ordinary schema validation can run without a
built cluster kernel binary. It should:

- try to build a minimal app using the supported test/runtime factory already in
  the repo,
- collect exposed method names from `app.state.exposed_methods`,
- compare those names to matrix rows with `grpc_expose` or `grpc_call`
  transport cells,
- report runtime-only methods as validation findings,
- report matrix rows missing from runtime discovery where that route is expected,
- skip with a clear message when `nexusd-cluster` / `nexus-cluster` is unavailable.

The documented workflow remains:

```bash
cargo build --release -p nexus-cluster --bin nexusd-cluster
uv run pytest tests/architecture/test_inventory.py -v
```

### `tests/architecture/test_inventory.py`

Promote the existing warn-only tests into hard assertions for #4139-owned
contracts:

- schema loads successfully,
- extraction freshness matches committed YAML,
- render output is deterministic,
- validation findings contain no `error`,
- runtime discovery test either passes after `nexusd-cluster` is built or skips
  with an explicit cluster-binary-missing reason.

The tests should preserve useful failure text. A failing row should tell the
developer which operation and field need attention and which command to run.

### `docs/architecture/api-rpc-surface-gaps.yaml`

Treat this file as the backlog source for known missing-needed rows. The
validator should require `gap_issue` for every missing operation. Existing rows
without gap issues should either receive issue links or remain as validation
errors so the gap is visible.

### `docs/architecture/api-rpc-surface-coverage.yaml`

Keep this as the authoritative matrix. The implementation may update rows with
owner, gap, correctness, and performance fields where evidence already exists in
the repository. It should not invent fake tests or fake benchmark links. Unknown
or not-yet-proven surfaces remain visible as validation failures or linked gaps.

## Data Flow

1. Run extractor.
2. Merge discovered surfaces into the committed YAML while preserving
   human-owned fields.
3. Load committed YAML.
4. Run validator.
5. Optionally run runtime discovery if `nexusd-cluster` is available.
6. Render HTML from YAML.
7. CI fails on schema, freshness, render, validation, or runtime-discovery
   errors.

## Error Handling

- Missing cluster kernel binary: skip only the runtime-discovery test, with the
  exact build command in the skip reason.
- Missing source path in `correctness_test` or `perf_link`: validation error.
- Deleted source surface that remains in YAML: validation error unless it is a
  documented stale/deprecated row with `gap_issue`.
- Missing gap issue: validation error.
- Unsupported profile behavior without explicit status: validation error.

## Testing Strategy

Use test-first implementation.

Focused unit tests:

- validator accepts a complete supported row,
- validator rejects supported rows missing owner/test/perf fields,
- validator rejects missing-needed rows without gap issues,
- validator checks test/perf paths deterministically,
- validator sorts findings deterministically,
- runtime discovery comparison reports runtime-only methods,
- runtime discovery comparison reports expected matrix methods missing at
  runtime,
- freshness and render drift are hard failures.

Integration-style architecture tests:

- `uv run pytest tests/architecture/test_inventory.py -v`
- `uv run python scripts/gen_api_surface_coverage.py`
- `uv run python scripts/render_api_surface_coverage.py`

Runtime workflow verification, when the cluster binary is available:

```bash
cargo build --release -p nexus-cluster --bin nexusd-cluster
uv run pytest tests/architecture/test_inventory.py -v
```

## Acceptance Criteria

- CI-facing architecture tests hard-fail when a new CLI/RPC/MCP/HTTP/SDK/profile
  surface lacks a matrix row.
- CI-facing architecture tests hard-fail when a supported row has no owning
  story issue.
- CI-facing architecture tests hard-fail when a supported row lacks correctness
  coverage.
- CI-facing architecture tests hard-fail when a supported row lacks performance
  classification and evidence/rationale.
- Rows with `missing_needed`, stale, deprecated, unsupported, or profile-blocked
  status require linked gap issues.
- Runtime-discovered `create_app(...).state.exposed_methods` is part of the
  workflow after `nexusd-cluster` is built.
- HTML rendering remains deterministic and derived from the YAML matrix.
- The implementation updates #4139-relevant docs so contributors know the exact
  commands for extraction, rendering, validation, and runtime discovery.

## Rollout Notes

The first implementation may expose many existing incomplete rows. That is
acceptable only if each incomplete row is either linked to a gap issue or fails
with a clear validator finding. The goal is not to hide incompleteness; the goal
is to make it impossible to close profile coverage work without a traceable
owner, test, performance class, and gap policy.
