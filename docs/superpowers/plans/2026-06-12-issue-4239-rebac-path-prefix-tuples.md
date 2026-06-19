# Issue #4239 ReBAC Path Prefix Tuples Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add explicit `/**` and `/*` file path-pattern tuple semantics to ReBAC checks, bulk filtering, and cache invalidation.

**Architecture:** Keep tuple storage unchanged and make pattern matching a bounded lookup problem. Generate finite candidate object IDs for each requested file path, query those candidates with indexed `IN` predicates, evaluate exact matches before broader pattern matches, and skip Rust bulk acceleration when fetched tuples contain path patterns until the Rust engine learns the same semantics.

**Tech Stack:** Python 3.14, SQLAlchemy, pytest, existing ReBAC manager/enforcer tests, `uv run --no-sync`, Ruff.

---

## File Structure

- Create `src/nexus/bricks/rebac/path_patterns.py`: pure helper for file path-pattern detection, prefix extraction, candidate generation, and match checks.
- Create `tests/unit/bricks/rebac/test_path_patterns.py`: unit tests for helper semantics.
- Create `tests/unit/bricks/rebac/test_path_pattern_permissions.py`: manager, bulk, enforcer, userset, ABAC, and cache invalidation coverage.
- Modify `src/nexus/bricks/rebac/graph/traversal.py`: single-check direct tuple lookup uses candidate object IDs for direct, wildcard, and userset-as-subject rows.
- Modify `src/nexus/bricks/rebac/batch/bulk_checker.py`: bulk tuple fetch includes path-pattern candidate object IDs.
- Modify `src/nexus/bricks/rebac/graph/bulk_evaluator.py`: in-memory direct relation evaluation recognizes fetched path-pattern tuples.
- Modify `src/nexus/bricks/rebac/utils/fast.py`: skip Rust bulk acceleration when tuple graphs contain path-pattern tuples.
- Modify `src/nexus/bricks/rebac/cache/coordinator.py`: invalidate cached descendant permission results when a pattern tuple changes.
- Modify `docs/guides/user-guide.md`: document the supported suffixes in the ReBAC user guide section.

## Task 1: Path Pattern Helper

**Files:**
- Create: `tests/unit/bricks/rebac/test_path_patterns.py`
- Create: `src/nexus/bricks/rebac/path_patterns.py`

- [ ] **Step 1: Write failing helper tests**

Create `tests/unit/bricks/rebac/test_path_patterns.py`:

```python
"""Path-pattern tuple helper tests for ReBAC issue #4239."""

from nexus.bricks.rebac.path_patterns import (
    is_path_pattern,
    path_pattern_candidates,
    path_pattern_matches,
    path_pattern_prefix,
)


def test_recursive_root_pattern_matches_all_absolute_paths() -> None:
    assert is_path_pattern("file", "/**")
    assert path_pattern_prefix("file", "/**") == "/"
    assert path_pattern_matches("/**", "/")
    assert path_pattern_matches("/**", "/a")
    assert path_pattern_matches("/**", "/a/b.txt")


def test_recursive_prefix_matches_prefix_and_descendants_only() -> None:
    assert is_path_pattern("file", "/workspaces/**")
    assert path_pattern_prefix("file", "/workspaces/**") == "/workspaces"
    assert path_pattern_matches("/workspaces/**", "/workspaces")
    assert path_pattern_matches("/workspaces/**", "/workspaces/ws1/a.md")
    assert not path_pattern_matches("/workspaces/**", "/workspaces2/a.md")


def test_single_level_pattern_matches_direct_children_only() -> None:
    assert is_path_pattern("file", "/workspaces/*")
    assert path_pattern_prefix("file", "/workspaces/*") == "/workspaces"
    assert path_pattern_matches("/workspaces/*", "/workspaces/a.md")
    assert not path_pattern_matches("/workspaces/*", "/workspaces")
    assert not path_pattern_matches("/workspaces/*", "/workspaces/a/b.md")
    assert not path_pattern_matches("/workspaces/*", "/workspaces2/a.md")


def test_root_single_level_pattern_matches_one_root_child() -> None:
    assert is_path_pattern("file", "/*")
    assert path_pattern_prefix("file", "/*") == "/"
    assert path_pattern_matches("/*", "/a.md")
    assert not path_pattern_matches("/*", "/")
    assert not path_pattern_matches("/*", "/a/b.md")


def test_non_file_and_relative_ids_are_not_patterns() -> None:
    assert not is_path_pattern("group", "/workspaces/**")
    assert not is_path_pattern("file", "workspaces/**")
    assert path_pattern_prefix("group", "/workspaces/**") is None
    assert path_pattern_candidates("group", "/workspaces/a.md") == ["/workspaces/a.md"]
    assert path_pattern_candidates("file", "workspaces/a.md") == ["workspaces/a.md"]


def test_candidates_are_bounded_and_ordered_from_exact_to_broad() -> None:
    assert path_pattern_candidates("file", "/workspaces/ws1/a.md") == [
        "/workspaces/ws1/a.md",
        "/workspaces/ws1/a.md/**",
        "/workspaces/ws1/*",
        "/workspaces/ws1/**",
        "/workspaces/**",
        "/**",
    ]
    assert path_pattern_candidates("file", "/a.md") == ["/a.md", "/a.md/**", "/*", "/**"]
    assert path_pattern_candidates("file", "/") == ["/", "/**"]
```

- [ ] **Step 2: Run helper tests to verify they fail**

Run:

```bash
uv run --no-sync pytest tests/unit/bricks/rebac/test_path_patterns.py -o "addopts=" -q
```

Expected: collection fails with `ModuleNotFoundError: No module named 'nexus.bricks.rebac.path_patterns'`.

- [ ] **Step 3: Add the helper implementation**

Create `src/nexus/bricks/rebac/path_patterns.py`:

```python
"""Helpers for explicit ReBAC file path-pattern tuple object IDs."""

from __future__ import annotations

RECURSIVE_SUFFIX = "/**"
SINGLE_LEVEL_SUFFIX = "/*"


def _is_absolute_file_path(object_type: str, object_id: str) -> bool:
    return object_type == "file" and object_id.startswith("/")


def _dedupe_preserving_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _ancestors_inclusive(path: str) -> list[str]:
    if path == "/":
        return ["/"]
    parts = [part for part in path.strip("/").split("/") if part]
    ancestors = ["/" + "/".join(parts[:idx]) for idx in range(len(parts), 0, -1)]
    ancestors.append("/")
    return ancestors


def _parent(path: str) -> str | None:
    if path == "/":
        return None
    parts = [part for part in path.strip("/").split("/") if part]
    if len(parts) <= 1:
        return "/"
    return "/" + "/".join(parts[:-1])


def is_path_pattern(object_type: str, object_id: str) -> bool:
    """Return whether an object id is a supported file path pattern."""
    if not _is_absolute_file_path(object_type, object_id):
        return False
    return object_id in (RECURSIVE_SUFFIX, SINGLE_LEVEL_SUFFIX) or object_id.endswith(
        (RECURSIVE_SUFFIX, SINGLE_LEVEL_SUFFIX)
    )


def path_pattern_prefix(object_type: str, object_id: str) -> str | None:
    """Return the concrete prefix for a supported path pattern."""
    if not is_path_pattern(object_type, object_id):
        return None
    if object_id in (RECURSIVE_SUFFIX, SINGLE_LEVEL_SUFFIX):
        return "/"
    if object_id.endswith(RECURSIVE_SUFFIX):
        return object_id[: -len(RECURSIVE_SUFFIX)] or "/"
    return object_id[: -len(SINGLE_LEVEL_SUFFIX)] or "/"


def path_pattern_matches(pattern_object_id: str, requested_object_id: str) -> bool:
    """Return whether a pattern object id grants access to a requested path."""
    if not requested_object_id.startswith("/"):
        return pattern_object_id == requested_object_id
    if pattern_object_id == requested_object_id:
        return True
    if pattern_object_id.endswith(RECURSIVE_SUFFIX):
        prefix = (
            "/"
            if pattern_object_id == RECURSIVE_SUFFIX
            else pattern_object_id[: -len(RECURSIVE_SUFFIX)]
        )
        return prefix == "/" or requested_object_id == prefix or requested_object_id.startswith(
            prefix + "/"
        )
    if pattern_object_id.endswith(SINGLE_LEVEL_SUFFIX):
        prefix = (
            "/"
            if pattern_object_id == SINGLE_LEVEL_SUFFIX
            else pattern_object_id[: -len(SINGLE_LEVEL_SUFFIX)]
        )
        if prefix == "/":
            remainder = requested_object_id.strip("/")
        elif requested_object_id.startswith(prefix + "/"):
            remainder = requested_object_id[len(prefix) + 1 :]
        else:
            return False
        return bool(remainder) and "/" not in remainder
    return False


def path_pattern_candidates(object_type: str, object_id: str) -> list[str]:
    """Return exact and pattern object IDs that can match a requested object."""
    if not _is_absolute_file_path(object_type, object_id):
        return [object_id]

    candidates = [object_id]
    if object_id != "/":
        candidates.append(object_id.rstrip("/") + RECURSIVE_SUFFIX)

    parent = _parent(object_id)
    if parent is not None:
        candidates.append(SINGLE_LEVEL_SUFFIX if parent == "/" else parent + SINGLE_LEVEL_SUFFIX)

    for ancestor in _ancestors_inclusive(object_id):
        recursive = RECURSIVE_SUFFIX if ancestor == "/" else ancestor + RECURSIVE_SUFFIX
        candidates.append(recursive)

    return _dedupe_preserving_order(candidates)
```

- [ ] **Step 4: Run helper tests to verify green**

Run:

```bash
uv run --no-sync pytest tests/unit/bricks/rebac/test_path_patterns.py -o "addopts=" -q
```

Expected: all helper tests pass.

- [ ] **Step 5: Commit Task 1**

Run:

```bash
git add tests/unit/bricks/rebac/test_path_patterns.py src/nexus/bricks/rebac/path_patterns.py
git commit -m "feat(rebac): add path pattern helpers"
```

Expected: commit succeeds after hooks.

## Task 2: Single ReBAC Check Pattern Lookup

**Files:**
- Create: `tests/unit/bricks/rebac/test_path_pattern_permissions.py`
- Modify: `src/nexus/bricks/rebac/graph/traversal.py`

- [ ] **Step 1: Write failing direct-check tests**

Create `tests/unit/bricks/rebac/test_path_pattern_permissions.py`:

```python
"""ReBAC path-pattern tuple behavior for issue #4239."""

from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("pyroaring")

from sqlalchemy import create_engine

from nexus.bricks.rebac.consistency.metastore_namespace_store import MetastoreNamespaceStore
from nexus.bricks.rebac.manager import ReBACManager
from nexus.storage.models import Base
from tests.testkit.metadata import InMemoryNexusFS


@pytest.fixture
def rebac_manager():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    manager = ReBACManager(
        engine=engine,
        cache_ttl_seconds=300,
        max_depth=10,
        namespace_store=MetastoreNamespaceStore(InMemoryNexusFS()),
    )
    try:
        yield manager
    finally:
        manager.close()


def test_recursive_path_pattern_grants_descendant_read(rebac_manager) -> None:
    rebac_manager.rebac_write(
        subject=("user", "admin"),
        relation="read",
        object=("file", "/workspaces/**"),
        zone_id="root",
    )

    assert rebac_manager.rebac_check(
        subject=("user", "admin"),
        permission="read",
        object=("file", "/workspaces/ws1/a.md"),
        zone_id="root",
    )


def test_recursive_path_pattern_grants_prefix_path(rebac_manager) -> None:
    rebac_manager.rebac_write(
        subject=("user", "admin"),
        relation="read",
        object=("file", "/workspaces/**"),
        zone_id="root",
    )

    assert rebac_manager.rebac_check(
        subject=("user", "admin"),
        permission="read",
        object=("file", "/workspaces"),
        zone_id="root",
    )


def test_single_level_path_pattern_grants_child_but_not_grandchild(rebac_manager) -> None:
    rebac_manager.rebac_write(
        subject=("user", "admin"),
        relation="read",
        object=("file", "/workspaces/*"),
        zone_id="root",
    )

    assert rebac_manager.rebac_check(
        subject=("user", "admin"),
        permission="read",
        object=("file", "/workspaces/a.md"),
        zone_id="root",
    )
    assert not rebac_manager.rebac_check(
        subject=("user", "admin"),
        permission="read",
        object=("file", "/workspaces/ws1/a.md"),
        zone_id="root",
    )


def test_path_pattern_does_not_partial_prefix_match(rebac_manager) -> None:
    rebac_manager.rebac_write(
        subject=("user", "admin"),
        relation="read",
        object=("file", "/workspaces/**"),
        zone_id="root",
    )

    assert not rebac_manager.rebac_check(
        subject=("user", "admin"),
        permission="read",
        object=("file", "/workspaces2/a.md"),
        zone_id="root",
    )


def test_wildcard_subject_path_pattern_grants_public_read(rebac_manager) -> None:
    rebac_manager.rebac_write(
        subject=("*", "*"),
        relation="read",
        object=("file", "/public/**"),
        zone_id="root",
    )

    assert rebac_manager.rebac_check(
        subject=("user", "anonymous"),
        permission="read",
        object=("file", "/public/a.md"),
        zone_id="root",
    )


def test_userset_subject_path_pattern_grants_member_read(rebac_manager) -> None:
    rebac_manager.rebac_write(
        subject=("user", "alice"),
        relation="member",
        object=("group", "eng"),
        zone_id="root",
    )
    rebac_manager.rebac_write(
        subject=("group", "eng", "member"),
        relation="read",
        object=("file", "/team/**"),
        zone_id="root",
    )

    assert rebac_manager.rebac_check(
        subject=("user", "alice"),
        permission="read",
        object=("file", "/team/spec.md"),
        zone_id="root",
    )
    assert not rebac_manager.rebac_check(
        subject=("user", "bob"),
        permission="read",
        object=("file", "/team/spec.md"),
        zone_id="root",
    )


def test_path_pattern_conditions_are_evaluated(rebac_manager) -> None:
    rebac_manager.rebac_write(
        subject=("user", "alice"),
        relation="read",
        object=("file", "/secure/**"),
        zone_id="root",
        conditions={"department": "eng"},
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )

    assert rebac_manager.rebac_check(
        subject=("user", "alice"),
        permission="read",
        object=("file", "/secure/plan.md"),
        zone_id="root",
        context={"department": "eng"},
    )
    assert not rebac_manager.rebac_check(
        subject=("user", "alice"),
        permission="read",
        object=("file", "/secure/plan.md"),
        zone_id="root",
        context={"department": "sales"},
    )
```

- [ ] **Step 2: Run direct-check tests to verify they fail**

Run:

```bash
uv run --no-sync pytest tests/unit/bricks/rebac/test_path_pattern_permissions.py -k "not bulk and not filter_list and not cache" -o "addopts=" -q
```

Expected: pattern grant tests fail because direct lookup still uses `object_id = ?`.

- [ ] **Step 3: Update direct tuple lookup queries**

In `src/nexus/bricks/rebac/graph/traversal.py`, add this import near the existing imports:

```python
from nexus.bricks.rebac.path_patterns import path_pattern_candidates
```

Add these methods inside `PermissionComputer` before `_query_direct_tuple()`:

```python
    @staticmethod
    def _candidate_object_ids(obj: Entity) -> list[str]:
        return path_pattern_candidates(obj.entity_type, obj.entity_id)

    @staticmethod
    def _ordered_matching_rows(rows: list[dict[str, Any]], candidates: list[str]) -> list[dict[str, Any]]:
        ordered: list[dict[str, Any]] = []
        for candidate in candidates:
            ordered.extend(row for row in rows if row["object_id"] == candidate)
        return ordered
```

Replace `_query_direct_tuple()` with:

```python
    def _query_direct_tuple(
        self,
        cursor: Any,
        subject: Entity,
        relation: str,
        obj: Entity,
        zone_id: str | None,
    ) -> dict[str, Any] | None:
        """Query for a direct concrete subject tuple."""
        now_iso = datetime.now(UTC).isoformat()
        fix = self._repo.fix_sql_placeholders
        candidates = self._candidate_object_ids(obj)
        bind_slots = ", ".join("?" for _ in candidates)

        if zone_id is None:
            cursor.execute(
                fix(
                    f"""
                    SELECT tuple_id, subject_type, subject_id, subject_relation,
                           relation, object_type, object_id, conditions, expires_at
                    FROM rebac_tuples
                    WHERE subject_type = ? AND subject_id = ?
                      AND subject_relation IS NULL
                      AND relation = ?
                      AND object_type = ? AND object_id IN ({bind_slots})
                      AND (expires_at IS NULL OR expires_at >= ?)
                      AND zone_id IS NULL
                    """
                ),
                (
                    subject.entity_type,
                    subject.entity_id,
                    relation,
                    obj.entity_type,
                    *candidates,
                    now_iso,
                ),
            )
        else:
            cursor.execute(
                fix(
                    f"""
                    SELECT tuple_id, subject_type, subject_id, subject_relation,
                           relation, object_type, object_id, conditions, expires_at
                    FROM rebac_tuples
                    WHERE subject_type = ? AND subject_id = ?
                      AND subject_relation IS NULL
                      AND relation = ?
                      AND object_type = ? AND object_id IN ({bind_slots})
                      AND (expires_at IS NULL OR expires_at >= ?)
                      AND zone_id = ?
                    """
                ),
                (
                    subject.entity_type,
                    subject.entity_id,
                    relation,
                    obj.entity_type,
                    *candidates,
                    now_iso,
                    zone_id,
                ),
            )

        rows = [dict(row) for row in cursor.fetchall()]
        ordered_rows = self._ordered_matching_rows(rows, candidates)
        return ordered_rows[0] if ordered_rows else None
```

Update `_check_wildcard_access()` and `_check_userset_grants()` the same way:

- Compute `candidates = self._candidate_object_ids(obj)`.
- Use `object_id IN ({bind_slots})`.
- Expand params with `*candidates`.
- Iterate rows through `self._ordered_matching_rows(rows, candidates)` before evaluating conditions.

For `_check_wildcard_access()`, return the first row whose conditions pass. For `_check_userset_grants()`, preserve the existing recursive `has_direct_relation()` membership check, but iterate ordered candidate rows.

- [ ] **Step 4: Run direct-check tests to verify green**

Run:

```bash
uv run --no-sync pytest tests/unit/bricks/rebac/test_path_pattern_permissions.py -k "not bulk and not filter_list and not cache" -o "addopts=" -q
```

Expected: direct, wildcard, userset, and ABAC tests pass.

- [ ] **Step 5: Commit Task 2**

Run:

```bash
git add tests/unit/bricks/rebac/test_path_pattern_permissions.py src/nexus/bricks/rebac/graph/traversal.py
git commit -m "feat(rebac): match path patterns in direct checks"
```

Expected: commit succeeds after hooks.

## Task 3: Bulk Check and Filter List Pattern Matching

**Files:**
- Modify: `tests/unit/bricks/rebac/test_path_pattern_permissions.py`
- Modify: `src/nexus/bricks/rebac/batch/bulk_checker.py`
- Modify: `src/nexus/bricks/rebac/graph/bulk_evaluator.py`
- Modify: `src/nexus/bricks/rebac/utils/fast.py`

- [ ] **Step 1: Add failing bulk and filter-list tests**

Append these imports to `tests/unit/bricks/rebac/test_path_pattern_permissions.py`:

```python
from nexus.bricks.rebac.enforcer import PermissionEnforcer
from nexus.contracts.types import OperationContext
```

Append these tests:

```python
def test_rebac_check_bulk_matches_recursive_path_pattern(rebac_manager) -> None:
    rebac_manager.rebac_write(
        subject=("user", "admin"),
        relation="read",
        object=("file", "/workspaces/**"),
        zone_id="root",
    )

    checks = [
        (("user", "admin"), "read", ("file", "/workspaces/ws1/a.md")),
        (("user", "admin"), "read", ("file", "/private/a.md")),
    ]

    assert rebac_manager.rebac_check_bulk(checks, zone_id="root") == {
        checks[0]: True,
        checks[1]: False,
    }


def test_filter_list_matches_recursive_path_pattern(rebac_manager) -> None:
    rebac_manager.rebac_write(
        subject=("user", "admin"),
        relation="read",
        object=("file", "/workspaces/**"),
        zone_id="root",
    )
    enforcer = PermissionEnforcer(rebac_manager=rebac_manager)
    context = OperationContext(
        user_id="admin",
        groups=[],
        subject_type="user",
        subject_id="admin",
        zone_id="root",
    )

    assert enforcer.filter_list(
        ["/workspaces/ws1/a.md", "/workspaces/ws2/b.md", "/private/c.md"],
        context,
    ) == ["/workspaces/ws1/a.md", "/workspaces/ws2/b.md"]
```

- [ ] **Step 2: Run bulk tests to verify they fail**

Run:

```bash
uv run --no-sync pytest tests/unit/bricks/rebac/test_path_pattern_permissions.py -k "bulk or filter_list" -o "addopts=" -q
```

Expected: bulk and filter-list tests fail because the bulk fetch/evaluator path still sees only exact object IDs.

- [ ] **Step 3: Add pattern candidates to bulk tuple fetch**

In `src/nexus/bricks/rebac/batch/bulk_checker.py`, add this import near the other imports:

```python
from nexus.bricks.rebac.path_patterns import path_pattern_candidates
```

In `_phase_fetch_tuples()`, after the loop that fills `file_paths`, add:

```python
        for file_path in file_paths:
            for candidate in path_pattern_candidates("file", file_path):
                all_objects.add(("file", candidate))
```

This expands the `entity_list` so SQL fetches exact rows plus pattern rows such as `/workspaces/**`.

- [ ] **Step 4: Update the in-memory bulk evaluator**

In `src/nexus/bricks/rebac/graph/bulk_evaluator.py`, add imports near the top:

```python
from dataclasses import dataclass

from nexus.bricks.rebac.path_patterns import is_path_pattern, path_pattern_matches
```

Add this dataclass before `build_direct_index()`:

```python
@dataclass(frozen=True)
class DirectRelationIndex:
    exact: frozenset[tuple[str, str, str, str, str]]
    patterns: tuple[dict[str, Any], ...]
```

Replace `build_direct_index()` with:

```python
def build_direct_index(
    tuples_graph: list[dict[str, Any]],
) -> DirectRelationIndex:
    """Build exact and path-pattern direct relation indexes."""
    exact: set[tuple[str, str, str, str, str]] = set()
    patterns: list[dict[str, Any]] = []

    for tuple_data in tuples_graph:
        if tuple_data.get("conditions") or tuple_data.get("subject_relation") is not None:
            continue
        if is_path_pattern(tuple_data["object_type"], tuple_data["object_id"]):
            patterns.append(tuple_data)
            continue
        exact.add(
            (
                tuple_data["subject_type"],
                tuple_data["subject_id"],
                tuple_data["relation"],
                tuple_data["object_type"],
                tuple_data["object_id"],
            )
        )

    return DirectRelationIndex(exact=frozenset(exact), patterns=tuple(patterns))
```

Update the `direct_index` type hints in `compute_permission()` and `check_direct_relation()` from `frozenset[...] | None` to `DirectRelationIndex | None`.

Replace the `direct_index` block in `check_direct_relation()` with:

```python
    if direct_index is not None:
        key = (subject.entity_type, subject.entity_id, permission, obj.entity_type, obj.entity_id)
        if key in direct_index.exact:
            return True
        wildcard_key = ("*", "*", permission, obj.entity_type, obj.entity_id)
        if wildcard_key in direct_index.exact:
            return True

        for tuple_data in direct_index.patterns:
            if (
                tuple_data["relation"] != permission
                or tuple_data["object_type"] != obj.entity_type
                or not path_pattern_matches(tuple_data["object_id"], obj.entity_id)
            ):
                continue
            if (
                tuple_data["subject_type"] == subject.entity_type
                and tuple_data["subject_id"] == subject.entity_id
            ) or (tuple_data["subject_type"] == "*" and tuple_data["subject_id"] == "*"):
                return True
        return False
```

In the linear fallback loop, replace `tuple_data["object_id"] != obj.entity_id` with:

```python
            or not (
                tuple_data["object_id"] == obj.entity_id
                or path_pattern_matches(tuple_data["object_id"], obj.entity_id)
            )
```

- [ ] **Step 5: Skip Rust when path-pattern tuples are present**

In `src/nexus/bricks/rebac/utils/fast.py`, add this import near the existing imports:

```python
from nexus.bricks.rebac.path_patterns import is_path_pattern
```

In `check_permissions_bulk_with_fallback()`, before the `if RUST_AVAILABLE and not force_python:` block, add:

```python
    has_path_pattern_tuple = any(
        is_path_pattern(str(t.get("object_type", "")), str(t.get("object_id", "")))
        for t in tuples
    )
    if has_path_pattern_tuple:
        logger.debug("Skipping Rust ReBAC bulk path because path-pattern tuples are present")
        return _check_permissions_bulk_python(checks, tuples, namespace_configs)
```

This keeps the Python evaluator authoritative for this feature.

- [ ] **Step 6: Run bulk tests to verify green**

Run:

```bash
uv run --no-sync pytest tests/unit/bricks/rebac/test_path_pattern_permissions.py -k "bulk or filter_list" -o "addopts=" -q
```

Expected: bulk and filter-list tests pass.

- [ ] **Step 7: Commit Task 3**

Run:

```bash
git add tests/unit/bricks/rebac/test_path_pattern_permissions.py src/nexus/bricks/rebac/batch/bulk_checker.py src/nexus/bricks/rebac/graph/bulk_evaluator.py src/nexus/bricks/rebac/utils/fast.py
git commit -m "feat(rebac): apply path patterns in bulk checks"
```

Expected: commit succeeds after hooks.

## Task 4: Pattern Tuple Cache Invalidation

**Files:**
- Modify: `tests/unit/bricks/rebac/test_path_pattern_permissions.py`
- Modify: `src/nexus/bricks/rebac/cache/coordinator.py`
- Modify: `src/nexus/bricks/rebac/path_patterns.py`

- [ ] **Step 1: Add a failing cached-denial invalidation test**

Append this test to `tests/unit/bricks/rebac/test_path_pattern_permissions.py`:

```python
def test_path_pattern_write_invalidates_cached_descendant_denial(rebac_manager) -> None:
    target = ("file", "/workspaces/ws1/a.md")

    assert not rebac_manager.rebac_check(
        subject=("user", "admin"),
        permission="read",
        object=target,
        zone_id="root",
    )

    rebac_manager.rebac_write(
        subject=("user", "admin"),
        relation="read",
        object=("file", "/workspaces/**"),
        zone_id="root",
    )

    assert rebac_manager.rebac_check(
        subject=("user", "admin"),
        permission="read",
        object=target,
        zone_id="root",
    )
```

- [ ] **Step 2: Run invalidation test to verify it fails**

Run:

```bash
uv run --no-sync pytest tests/unit/bricks/rebac/test_path_pattern_permissions.py::test_path_pattern_write_invalidates_cached_descendant_denial -o "addopts=" -q
```

Expected: the final assertion fails because the cached denial for `/workspaces/ws1/a.md` was not invalidated by writing `/workspaces/**`.

- [ ] **Step 3: Make prefix extraction reusable**

In `src/nexus/bricks/rebac/path_patterns.py`, keep the existing `path_pattern_prefix()` function from Task 1 unchanged. The cache coordinator will use it to translate `/prefix/**` and `/prefix/*` into `/prefix`.

- [ ] **Step 4: Invalidate descendant cache entries for pattern tuples**

In `src/nexus/bricks/rebac/cache/coordinator.py`, add this import near the existing imports:

```python
from nexus.bricks.rebac.path_patterns import path_pattern_prefix
```

In `invalidate_for_tuple_change()`, after the direct `invalidate_subject_object_pair()` block and before eager recompute, add:

```python
            pattern_prefix = path_pattern_prefix(obj.entity_type, obj.entity_id)
            if pattern_prefix is not None:
                if self._l1_cache:
                    self._l1_cache.invalidate_object_prefix(
                        obj.entity_type,
                        pattern_prefix,
                        zone_id,
                    )
                if self._boundary_cache and obj.entity_type == "file":
                    self._boundary_cache.invalidate_path_prefix(effective_zone_id, pattern_prefix)
                should_eager_recompute = False
```

This invalidates descendant L1 entries and boundary-cache entries for both recursive and single-level patterns. It also disables eager recompute for pattern tuples because the changed object ID is not the concrete leaf path being checked.

- [ ] **Step 5: Run invalidation test to verify green**

Run:

```bash
uv run --no-sync pytest tests/unit/bricks/rebac/test_path_pattern_permissions.py::test_path_pattern_write_invalidates_cached_descendant_denial -o "addopts=" -q
```

Expected: the cached-denial invalidation test passes.

- [ ] **Step 6: Commit Task 4**

Run:

```bash
git add tests/unit/bricks/rebac/test_path_pattern_permissions.py src/nexus/bricks/rebac/cache/coordinator.py src/nexus/bricks/rebac/path_patterns.py
git commit -m "fix(rebac): invalidate cached descendants for path patterns"
```

Expected: commit succeeds after hooks.

## Task 5: Documentation

**Files:**
- Modify: `docs/guides/user-guide.md`

- [ ] **Step 1: Update the ReBAC guide text**

In `docs/guides/user-guide.md`, find the "Sandbox ReBAC, hub-zone, and MCP tool boundaries" section. After the tuple CLI examples around `nexus rebac create agent alice direct_owner file /zone/local/notes/todo.txt`, add:

```markdown
Path-style `file` tuple object IDs also support two explicit suffixes for bulk
grants:

- `/prefix/**` grants the relation on `/prefix` and every descendant below it.
- `/prefix/*` grants the relation on direct children of `/prefix` only.

No other glob syntax is expanded. Values such as `/prefix/*.md` or interior
`*` characters are stored literally.
```

- [ ] **Step 2: Review the rendered context in plain text**

Run:

```bash
sed -n '640,670p' docs/guides/user-guide.md
```

Expected: the new paragraph sits after the ReBAC CLI examples and does not interrupt a fenced code block.

- [ ] **Step 3: Commit Task 5**

Run:

```bash
git add docs/guides/user-guide.md
git commit -m "docs(rebac): document path pattern tuple suffixes"
```

Expected: commit succeeds after hooks.

## Task 6: Focused Verification

**Files:**
- No file edits.

- [ ] **Step 1: Run all new path-pattern tests**

Run:

```bash
uv run --no-sync pytest tests/unit/bricks/rebac/test_path_patterns.py tests/unit/bricks/rebac/test_path_pattern_permissions.py -o "addopts=" -q
```

Expected: all tests pass.

- [ ] **Step 2: Run existing filter-chain regression tests**

Run:

```bash
uv run --no-sync pytest tests/unit/bricks/rebac/test_permission_filter_chain.py -o "addopts=" -q
```

Expected: all existing filter-chain tests pass.

- [ ] **Step 3: Run targeted Ruff checks**

Run:

```bash
uv run --no-sync ruff check src/nexus/bricks/rebac/path_patterns.py src/nexus/bricks/rebac/graph/traversal.py src/nexus/bricks/rebac/batch/bulk_checker.py src/nexus/bricks/rebac/graph/bulk_evaluator.py src/nexus/bricks/rebac/utils/fast.py src/nexus/bricks/rebac/cache/coordinator.py tests/unit/bricks/rebac/test_path_patterns.py tests/unit/bricks/rebac/test_path_pattern_permissions.py
```

Expected: Ruff reports no issues.

- [ ] **Step 4: Run targeted format checks**

Run:

```bash
uv run --no-sync ruff format --check src/nexus/bricks/rebac/path_patterns.py src/nexus/bricks/rebac/graph/traversal.py src/nexus/bricks/rebac/batch/bulk_checker.py src/nexus/bricks/rebac/graph/bulk_evaluator.py src/nexus/bricks/rebac/utils/fast.py src/nexus/bricks/rebac/cache/coordinator.py tests/unit/bricks/rebac/test_path_patterns.py tests/unit/bricks/rebac/test_path_pattern_permissions.py
```

Expected: Ruff format reports no changes needed.

- [ ] **Step 5: Check whitespace**

Run:

```bash
git diff --check
```

Expected: no output.

- [ ] **Step 6: Commit verification-only fixes if hooks changed files**

Run this only when a formatter or hook modified tracked files:

```bash
git status --short
git add src/nexus/bricks/rebac tests/unit/bricks/rebac docs/guides/user-guide.md
git commit -m "chore(rebac): apply path pattern verification cleanup"
```

Expected: either there is nothing to commit, or the cleanup commit contains only formatter/hook changes from this task.
