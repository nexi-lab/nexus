# Design: Issue #4239 - ReBAC Path Prefix Tuples

- **Status:** Approved design, awaiting spec review
- **Date:** 2026-06-12
- **Owner:** windoliver
- **Issue:** https://github.com/nexi-lab/nexus/issues/4239
- **Base branch:** `origin/develop`

## Problem

`POST /api/v2/rebac/tuples` accepts any string as a tuple `object_id`, but
runtime authorization treats that value as an exact object identifier. Operators
can write tuples such as `/workspaces/**`, `/workspaces/*`, or `/**`, but those
tuples do not authorize any file unless the requested file path is exactly the
same string.

That blocks common static-auth and service-principal setups where file paths are
created dynamically under a workspace, agent filesystem, or tenant bucket. The
operator cannot grant blanket read access to a path tree without pre-creating a
tuple for every file or running a reconciler that mints tuples after every write.

## Context From Code Inspection

The active search/list authorization path checks exact file paths and their
ancestor directories:

- `src/nexus/bricks/rebac/permission_filter_chain.py` builds `read` checks for
  the requested path and ancestors only.
- `src/nexus/bricks/rebac/enforcer.py` does the same for sequential and batched
  filesystem permission checks.
- `src/nexus/bricks/rebac/graph/traversal.py` queries direct tuples with
  `object_id = ?`.
- `src/nexus/bricks/rebac/batch/bulk_checker.py` fetches tuples for exact entity
  IDs only, then computes permissions in memory.
- `src/nexus/bricks/rebac/graph/bulk_evaluator.py` indexes direct tuples by
  exact `(subject, relation, object_type, object_id)`.

Existing prefix helper work in `src/nexus/lib/prefix_helpers.py` covers path
visibility and descendant scans, but it does not change ReBAC tuple semantics.

## Goal

Support explicit file path pattern tuples for direct ReBAC grants:

- `object_id` ending in `/**` grants the same relation on the prefix path and all
  descendants.
- `object_id` ending in `/*` grants the same relation on direct children one
  path segment below the prefix.
- Exact `object_id` values keep their current behavior.
- Matching is limited to `object_type="file"` path-like object IDs.
- Search, list, and file permission checks all see the same semantics.

## Non-Goals

- Do not implement folder-object inheritance or a new parent-of relation model in
  this issue.
- Do not materialize one tuple per file on write.
- Do not change tuple storage schema.
- Do not treat interior `*` characters as glob syntax.
- Do not add negative or deny tuples.
- Do not change static-auth subject resolution or API-key grant creation.

## Design

### 1. Path Pattern Helper

Add a small helper module in the ReBAC brick, for example
`src/nexus/bricks/rebac/path_patterns.py`, with pure functions:

```text
is_path_pattern(object_type, object_id) -> bool
path_pattern_candidates(object_type, object_id) -> list[str]
path_pattern_matches(pattern_object_id, requested_object_id) -> bool
```

Semantics:

- A pattern is valid only when `object_type == "file"` and `object_id` starts
  with `/`.
- `/**` is a special root recursive pattern and matches every absolute file path,
  including `/`.
- `/prefix/**` matches `/prefix` and any descendant such as `/prefix/a.txt` or
  `/prefix/a/b.txt`.
- `/prefix/*` matches exactly one segment below `/prefix`, such as
  `/prefix/a.txt`, but not `/prefix/a/b.txt` and not `/prefix`.
- Anything else is exact and handled by existing code.

For a requested object ID, `path_pattern_candidates()` should return the finite
set of pattern IDs that could match it. Example for
`/workspaces/ws1/a.md`:

```text
/workspaces/ws1/a.md
/workspaces/ws1/a.md/**
/workspaces/ws1/*
/workspaces/ws1/**
/workspaces/**
/**
```

The exact path remains first so existing direct grants keep priority and cache
behavior.

### 2. Single Check Direct Tuple Lookup

Update `PermissionComputer` direct tuple lookup in
`src/nexus/bricks/rebac/graph/traversal.py` so direct concrete, wildcard, and
userset-as-subject tuple queries can match the requested exact object ID plus
the finite candidate pattern object IDs.

The SQL should remain indexed and bounded by using `object_id IN (...)`, not a
database `LIKE` scan. Candidate generation is deterministic and depends only on
the requested path depth. Existing non-file object IDs keep the current
`object_id = ?` path.

Condition evaluation stays unchanged: if a matching pattern tuple has ABAC
conditions, the same condition evaluation path runs before allowing access.

### 3. Bulk Check Fetch and Evaluation

Update `BulkPermissionChecker` in
`src/nexus/bricks/rebac/batch/bulk_checker.py` to include pattern candidates for
file object IDs when building the `entity_list`. This lets the bulk SQL fetch
pattern tuples together with exact and ancestor tuples.

Update `src/nexus/bricks/rebac/graph/bulk_evaluator.py` direct-relation matching
so a fetched pattern tuple can satisfy an exact requested file object. Exact
matches should still use the existing direct index; pattern checks can use a
separate prefix-pattern index or a small helper over pattern tuples. The key
requirement is that bulk `filter_list()` and single `rebac_check()` make the same
allow/deny decision.

### 4. Cache Invalidation

Existing tuple writes already invalidate the exact object ID in L1 and related
permission caches. Pattern tuple writes need descendant invalidation:

- When writing or deleting `/prefix/**`, invalidate cached file permission
  results under `/prefix`.
- When writing or deleting `/prefix/*`, use the same descendant invalidation
  scope for correctness, even though the match is only one level. Broader
  invalidation is acceptable here because tuple writes are less frequent than
  reads and stale allows/denies are security-sensitive.
- Exact tuple invalidation remains unchanged.

The existing `invalidate_object_prefix()` and boundary-cache prefix invalidation
paths should be reused where possible rather than introducing a new cache layer.

### 5. API and Documentation Behavior

The tuple write API continues to store the supplied `object_id` string. The
behavioral change is in authorization semantics, not in request validation or
storage. API docs and upgrade notes should describe the supported suffixes:

- `/**` for recursive path-tree grants.
- `/*` for single-level child grants.
- No other glob syntax is supported.

## Alternatives Considered

### Recommended: Explicit Pattern Tuples

This is the approved approach. It solves the operator pain with no schema
change, no per-file tuple fanout, and a bounded candidate set for lookup.

### Materialize Per-File Tuples

This would preserve exact-match semantics but doubles write work and creates
race windows between file creation and tuple reconciliation. It also does not
help paths created by external backends unless every write path participates.

### Folder Object Inheritance

This is a cleaner long-term authorization model, but it is larger than this
issue. It requires clearer folder entity semantics, parent relation lifecycle,
and cross-cache invalidation rules. This is out of scope for #4239 and requires
a separate design.

## Tests

Follow TDD for implementation.

Add helper tests for:

- `/**` matches `/`, `/a`, and `/a/b.txt`.
- `/workspaces/**` matches `/workspaces`, `/workspaces/ws1/a.md`, and deeper
  descendants.
- `/workspaces/*` matches `/workspaces/a.md` but not `/workspaces/a/b.md` and not
  `/workspaces`.
- Partial prefixes do not match, for example `/workspaces/**` does not match
  `/workspaces2/a.md`.
- Non-file object types and relative object IDs are not treated as path
  patterns.

Add manager/enforcer tests for:

- Direct `rebac_check()` allows a descendant from a `/**` tuple.
- Direct `rebac_check()` allows a single child from a `/*` tuple and denies a
  grandchild.
- `rebac_check_bulk()` returns the same decisions as direct checks.
- `PermissionEnforcer.filter_list()` returns only paths covered by the pattern
  tuple.
- Wildcard subject `("*", "*")` plus path pattern still behaves as public access.
- Userset-as-subject path pattern grants work when the caller is a member of the
  userset.
- ABAC conditions on a pattern tuple are evaluated.
- Cache invalidation after a pattern tuple write changes a previously cached
  denial to allow.

## Verification

Targeted verification should include:

```bash
uv run --no-sync pytest tests/unit/bricks/rebac/test_path_patterns.py -o "addopts="
uv run --no-sync pytest tests/unit/core/test_rebac.py -k "path_pattern or prefix" -o "addopts="
uv run --no-sync pytest tests/unit/bricks/rebac/test_permission_filter_chain.py -o "addopts="
uv run --no-sync ruff check src/nexus/bricks/rebac/path_patterns.py src/nexus/bricks/rebac/graph/traversal.py src/nexus/bricks/rebac/batch/bulk_checker.py src/nexus/bricks/rebac/graph/bulk_evaluator.py
uv run --no-sync ruff format --check src/nexus/bricks/rebac/path_patterns.py src/nexus/bricks/rebac/graph/traversal.py src/nexus/bricks/rebac/batch/bulk_checker.py src/nexus/bricks/rebac/graph/bulk_evaluator.py
git diff --check
```

If implementation touches API docs or upgrade notes, include those files in the
ruff or markdown checks used by the repo.
