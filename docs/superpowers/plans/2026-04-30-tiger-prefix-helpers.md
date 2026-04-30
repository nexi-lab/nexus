# Tiger/Prefix Helpers in Descendant-Access Hot Paths

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route `DescendantAccessChecker` and `DirectoryVisibilityCache` hot paths through kernel `batch_prefix_check`/`any_path_starts_with` Rust primitives, replacing Python int-ID loops with a thin guarded helper, while retaining identical Python fallback behaviour.

**Architecture:** A new `_prefix_helpers.py` module in the rebac cache layer exposes two functions (`any_path_under_prefix`, `batch_paths_under_prefixes`) that try Rust first and fall back to Python inline. `_rust_compat.py` gains two new re-exports. Four call sites are updated; one (enforcer.py) is cleaned up.

**Tech Stack:** Python 3.11+, `nexus_runtime` (Rust extension, optional), `pytest`, `unittest.mock`

---

## File Map

| Action | Path | Responsibility |
|--------|------|----------------|
| Modify | `src/nexus/_rust_compat.py` | Add `batch_prefix_check`, `any_path_starts_with` re-exports |
| Create | `src/nexus/bricks/rebac/cache/_prefix_helpers.py` | Rust-guarded prefix helpers with Python fallback |
| Modify | `src/nexus/bricks/rebac/cache/visibility.py` | Use `get_accessible_paths()` + helpers in both compute methods |
| Modify | `src/nexus/services/namespace/descendant_access.py` | Replace Tiger fallback int-ID loop |
| Modify | `src/nexus/bricks/rebac/enforcer.py` | Remove inline try/except; use helper |
| Create | `tests/unit/rebac/test_prefix_helpers.py` | Unit + fallback tests for `_prefix_helpers` |
| Create | `tests/unit/rebac/bench_prefix_helpers.py` | Perf regression guard |

---

## Task 1: Add `_rust_compat.py` re-exports and create `_prefix_helpers.py`

**Files:**
- Modify: `src/nexus/_rust_compat.py:228` (after last re-export line)
- Create: `src/nexus/bricks/rebac/cache/_prefix_helpers.py`
- Create: `tests/unit/rebac/test_prefix_helpers.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/rebac/test_prefix_helpers.py`:

```python
"""Unit tests for rebac cache prefix helpers (Issue #3951)."""
from __future__ import annotations

import pytest

pytest.importorskip("pyroaring")  # matches rebac test convention


# ---------------------------------------------------------------------------
# any_path_under_prefix
# ---------------------------------------------------------------------------

def test_any_path_under_prefix_descendant():
    from nexus.bricks.rebac.cache._prefix_helpers import any_path_under_prefix
    assert any_path_under_prefix(["/a/b/c", "/x/y"], "/a/b") is True


def test_any_path_under_prefix_exact_match():
    from nexus.bricks.rebac.cache._prefix_helpers import any_path_under_prefix
    assert any_path_under_prefix(["/a/b", "/x/y"], "/a/b") is True


def test_any_path_under_prefix_no_match():
    from nexus.bricks.rebac.cache._prefix_helpers import any_path_under_prefix
    assert any_path_under_prefix(["/x/y", "/z"], "/a/b") is False


def test_any_path_under_prefix_root():
    from nexus.bricks.rebac.cache._prefix_helpers import any_path_under_prefix
    assert any_path_under_prefix(["/a/b"], "/") is True


def test_any_path_under_prefix_trailing_slash_prefix():
    from nexus.bricks.rebac.cache._prefix_helpers import any_path_under_prefix
    # Callers may pass prefix with or without trailing slash
    assert any_path_under_prefix(["/a/b/c"], "/a/b/") is True


def test_any_path_under_prefix_set_input():
    from nexus.bricks.rebac.cache._prefix_helpers import any_path_under_prefix
    assert any_path_under_prefix({"/a/b/c"}, "/a/b") is True


def test_any_path_under_prefix_empty_paths():
    from nexus.bricks.rebac.cache._prefix_helpers import any_path_under_prefix
    assert any_path_under_prefix([], "/a/b") is False


def test_any_path_under_prefix_no_partial_match():
    from nexus.bricks.rebac.cache._prefix_helpers import any_path_under_prefix
    # "/a/bc" must NOT match prefix "/a/b"
    assert any_path_under_prefix(["/a/bc"], "/a/b") is False


# ---------------------------------------------------------------------------
# batch_paths_under_prefixes
# ---------------------------------------------------------------------------

def test_batch_paths_under_prefixes_basic():
    from nexus.bricks.rebac.cache._prefix_helpers import batch_paths_under_prefixes
    result = batch_paths_under_prefixes(["/a/b/c", "/x/y"], ["/a/b", "/z"])
    assert result == [True, False]


def test_batch_paths_under_prefixes_order_preserved():
    from nexus.bricks.rebac.cache._prefix_helpers import batch_paths_under_prefixes
    result = batch_paths_under_prefixes(["/a/b/c"], ["/z", "/a", "/b"])
    assert result == [False, True, False]


def test_batch_paths_under_prefixes_empty_paths():
    from nexus.bricks.rebac.cache._prefix_helpers import batch_paths_under_prefixes
    assert batch_paths_under_prefixes([], ["/a", "/b"]) == [False, False]


def test_batch_paths_under_prefixes_empty_prefixes():
    from nexus.bricks.rebac.cache._prefix_helpers import batch_paths_under_prefixes
    assert batch_paths_under_prefixes(["/a/b"], []) == []


def test_batch_paths_under_prefixes_result_length_matches_prefixes():
    from nexus.bricks.rebac.cache._prefix_helpers import batch_paths_under_prefixes
    prefixes = ["/a", "/b", "/c", "/d"]
    result = batch_paths_under_prefixes(["/a/x"], prefixes)
    assert len(result) == len(prefixes)


# ---------------------------------------------------------------------------
# Python fallback paths (mock Rust to None)
# ---------------------------------------------------------------------------

def test_any_path_under_prefix_python_fallback(monkeypatch):
    import nexus.bricks.rebac.cache._prefix_helpers as ph
    monkeypatch.setattr(ph, "_rust_any", None)
    assert ph.any_path_under_prefix(["/a/b/c"], "/a/b") is True
    assert ph.any_path_under_prefix(["/a/bc"], "/a/b") is False


def test_batch_paths_under_prefixes_python_fallback(monkeypatch):
    import nexus.bricks.rebac.cache._prefix_helpers as ph
    monkeypatch.setattr(ph, "_rust_batch", None)
    result = ph.batch_paths_under_prefixes(["/a/b/c"], ["/a/b", "/z"])
    assert result == [True, False]


def test_python_fallback_no_partial_match(monkeypatch):
    import nexus.bricks.rebac.cache._prefix_helpers as ph
    monkeypatch.setattr(ph, "_rust_any", None)
    # "/a/bc" must NOT match "/a/b" — guard against off-by-one in norm logic
    assert ph.any_path_under_prefix(["/a/bc"], "/a/b") is False
```

- [ ] **Step 2: Run tests — expect ImportError**

```bash
pytest tests/unit/rebac/test_prefix_helpers.py -v 2>&1 | head -20
```

Expected: `ModuleNotFoundError: No module named 'nexus.bricks.rebac.cache._prefix_helpers'`

- [ ] **Step 3: Add re-exports to `_rust_compat.py`**

In `src/nexus/_rust_compat.py`, after the last line (`glob_match_bulk = _get("glob_match_bulk")`), append:

```python

# Prefix / bitmap helpers (Issue #3951)
any_path_starts_with = _get("any_path_starts_with")
batch_prefix_check = _get("batch_prefix_check")
```

- [ ] **Step 4: Create `_prefix_helpers.py`**

Create `src/nexus/bricks/rebac/cache/_prefix_helpers.py`:

```python
"""Rust-guarded prefix helpers for descendant-access hot paths (Issue #3951).

Both functions try the kernel primitive first; if unavailable they fall back
to pure-Python with identical semantics.
"""

from __future__ import annotations

from nexus._rust_compat import any_path_starts_with as _rust_any
from nexus._rust_compat import batch_prefix_check as _rust_batch


def any_path_under_prefix(paths: "list[str] | set[str]", prefix: str) -> bool:
    """Return True if any path equals prefix or is a descendant of it.

    Safe for trailing-slash variation: "/a/b/" and "/a/b" both match
    descendants like "/a/b/c".
    """
    paths_list: list[str] = list(paths) if isinstance(paths, set) else paths
    if _rust_any is not None:
        return bool(_rust_any(paths_list, prefix))
    exact = prefix.rstrip("/")
    norm = exact + "/"
    return any(p == exact or p.startswith(norm) for p in paths_list)


def batch_paths_under_prefixes(
    paths: "list[str] | set[str]",
    prefixes: list[str],
) -> list[bool]:
    """For each prefix, return True if any path equals it or is a descendant.

    Result order matches the order of *prefixes*.
    """
    paths_list: list[str] = list(paths) if isinstance(paths, set) else paths
    if _rust_batch is not None:
        return list(_rust_batch(paths_list, prefixes))
    results: list[bool] = []
    for pfx in prefixes:
        exact = pfx.rstrip("/")
        norm = exact + "/"
        results.append(any(p == exact or p.startswith(norm) for p in paths_list))
    return results
```

- [ ] **Step 5: Run tests — expect PASS**

```bash
pytest tests/unit/rebac/test_prefix_helpers.py -v
```

Expected: all tests PASS (Rust tests may skip if `nexus_runtime` not installed — that is fine).

- [ ] **Step 6: Commit**

```bash
git add src/nexus/_rust_compat.py \
        src/nexus/bricks/rebac/cache/_prefix_helpers.py \
        tests/unit/rebac/test_prefix_helpers.py
git commit -m "feat(rebac): add _prefix_helpers with Rust-guarded any/batch prefix checks"
```

---

## Task 2: Update `visibility.py` — `compute_from_tiger_bitmap`

**Files:**
- Modify: `src/nexus/bricks/rebac/cache/visibility.py:170-252`

- [ ] **Step 1: Write failing test**

Append to `tests/unit/rebac/test_prefix_helpers.py`:

```python
# ---------------------------------------------------------------------------
# DirectoryVisibilityCache.compute_from_tiger_bitmap — refactor contract
# ---------------------------------------------------------------------------

def test_compute_from_tiger_bitmap_calls_get_accessible_paths():
    """After refactor, method must use get_accessible_paths (not get_accessible_resources)."""
    from unittest.mock import MagicMock
    from nexus.bricks.rebac.cache.visibility import DirectoryVisibilityCache

    tiger_cache = MagicMock()
    tiger_cache.get_accessible_paths.return_value = {"/a/b/c", "/a/b/d"}
    # If the old code runs, it calls get_accessible_resources which is NOT mocked
    # with a useful return value — it would return a MagicMock (truthy but not iterable
    # in the expected way), causing the int-ID loop to fail or return wrong result.
    tiger_cache.get_accessible_resources.return_value = set()  # force wrong result if called

    cache = DirectoryVisibilityCache(tiger_cache=tiger_cache)
    result = cache.compute_from_tiger_bitmap("z1", "user", "u1", "/a/b", "read")

    assert result is True
    tiger_cache.get_accessible_paths.assert_called_once_with(
        subject_type="user",
        subject_id="u1",
        permission="read",
        resource_type="file",
        zone_id="z1",
    )


def test_compute_from_tiger_bitmap_cache_miss_returns_none():
    from unittest.mock import MagicMock
    from nexus.bricks.rebac.cache.visibility import DirectoryVisibilityCache

    tiger_cache = MagicMock()
    tiger_cache.get_accessible_paths.return_value = None

    cache = DirectoryVisibilityCache(tiger_cache=tiger_cache)
    result = cache.compute_from_tiger_bitmap("z1", "user", "u1", "/a/b", "read")
    assert result is None


def test_compute_from_tiger_bitmap_no_accessible_returns_false():
    from unittest.mock import MagicMock
    from nexus.bricks.rebac.cache.visibility import DirectoryVisibilityCache

    tiger_cache = MagicMock()
    tiger_cache.get_accessible_paths.return_value = set()

    cache = DirectoryVisibilityCache(tiger_cache=tiger_cache)
    result = cache.compute_from_tiger_bitmap("z1", "user", "u1", "/a/b", "read")
    assert result is False


def test_compute_from_tiger_bitmap_no_descendants_returns_false():
    from unittest.mock import MagicMock
    from nexus.bricks.rebac.cache.visibility import DirectoryVisibilityCache

    tiger_cache = MagicMock()
    tiger_cache.get_accessible_paths.return_value = {"/x/y/z"}

    cache = DirectoryVisibilityCache(tiger_cache=tiger_cache)
    result = cache.compute_from_tiger_bitmap("z1", "user", "u1", "/a/b", "read")
    assert result is False
```

- [ ] **Step 2: Run — expect FAIL**

```bash
pytest tests/unit/rebac/test_prefix_helpers.py::test_compute_from_tiger_bitmap_calls_get_accessible_paths -v
```

Expected: FAIL — current implementation calls `get_accessible_resources`, not `get_accessible_paths`.

- [ ] **Step 3: Rewrite `compute_from_tiger_bitmap` in `visibility.py`**

Replace lines 199–252 (everything after `self._bitmap_computes += 1`) with:

```python
        accessible_paths = self._tiger_cache.get_accessible_paths(
            subject_type=subject_type,
            subject_id=subject_id,
            permission=permission,
            resource_type="file",
            zone_id=zone_id,
        )

        if accessible_paths is None:
            return None  # cache miss — caller falls through to slow path

        if not accessible_paths:
            self.set_visible(
                zone_id, subject_type, subject_id, dir_path, False, "no_accessible_resources"
            )
            return False

        from nexus.bricks.rebac.cache._prefix_helpers import any_path_under_prefix

        result = any_path_under_prefix(accessible_paths, dir_path)
        reason = f"bitmap_prefix:{dir_path}" if result else "no_descendants_in_bitmap"
        self.set_visible(zone_id, subject_type, subject_id, dir_path, result, reason)
        logger.debug("[DirVisCache] BITMAP_COMPUTE: %s visible=%s", dir_path, result)
        return result
```

The full method after editing (`visibility.py:170`):

```python
    def compute_from_tiger_bitmap(
        self,
        zone_id: str,
        subject_type: str,
        subject_id: str,
        dir_path: str,
        permission: str = "read",
    ) -> bool | None:
        """Compute directory visibility from Tiger Cache bitmap.

        Complexity: O(bitmap_size) vs O(n_descendants * permission_check)

        Returns:
            True if directory is visible (has accessible descendants),
            False if not visible,
            None if Tiger Cache is unavailable or cache miss
        """
        if not self._tiger_cache:
            return None

        self._bitmap_computes += 1

        accessible_paths = self._tiger_cache.get_accessible_paths(
            subject_type=subject_type,
            subject_id=subject_id,
            permission=permission,
            resource_type="file",
            zone_id=zone_id,
        )

        if accessible_paths is None:
            return None  # cache miss — caller falls through to slow path

        if not accessible_paths:
            self.set_visible(
                zone_id, subject_type, subject_id, dir_path, False, "no_accessible_resources"
            )
            return False

        from nexus.bricks.rebac.cache._prefix_helpers import any_path_under_prefix

        result = any_path_under_prefix(accessible_paths, dir_path)
        reason = f"bitmap_prefix:{dir_path}" if result else "no_descendants_in_bitmap"
        self.set_visible(zone_id, subject_type, subject_id, dir_path, result, reason)
        logger.debug("[DirVisCache] BITMAP_COMPUTE: %s visible=%s", dir_path, result)
        return result
```

- [ ] **Step 4: Run tests — expect PASS**

```bash
pytest tests/unit/rebac/test_prefix_helpers.py -v
```

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/bricks/rebac/cache/visibility.py \
        tests/unit/rebac/test_prefix_helpers.py
git commit -m "refactor(visibility): compute_from_tiger_bitmap via get_accessible_paths + prefix helper"
```

---

## Task 3: Update `visibility.py` — `compute_batch_visibility`

**Files:**
- Modify: `src/nexus/bricks/rebac/cache/visibility.py:254-318`

- [ ] **Step 1: Write failing test**

Append to `tests/unit/rebac/test_prefix_helpers.py`:

```python
# ---------------------------------------------------------------------------
# DirectoryVisibilityCache.compute_batch_visibility — refactor contract
# ---------------------------------------------------------------------------

def test_compute_batch_visibility_correct_results():
    from unittest.mock import MagicMock
    from nexus.bricks.rebac.cache.visibility import DirectoryVisibilityCache

    tiger_cache = MagicMock()
    tiger_cache.get_accessible_paths.return_value = {"/a/b/c", "/x/y/z"}
    tiger_cache.get_accessible_resources.return_value = set()  # wrong result if called

    cache = DirectoryVisibilityCache(tiger_cache=tiger_cache)
    result = cache.compute_batch_visibility("z1", "user", "u1", ["/a/b", "/x/y", "/nope"], "read")

    assert result == {"/a/b": True, "/x/y": True, "/nope": False}


def test_compute_batch_visibility_cache_miss_returns_empty():
    from unittest.mock import MagicMock
    from nexus.bricks.rebac.cache.visibility import DirectoryVisibilityCache

    tiger_cache = MagicMock()
    tiger_cache.get_accessible_paths.return_value = None

    cache = DirectoryVisibilityCache(tiger_cache=tiger_cache)
    result = cache.compute_batch_visibility("z1", "user", "u1", ["/a/b"], "read")
    assert result == {}


def test_compute_batch_visibility_no_tiger_cache_returns_empty():
    from nexus.bricks.rebac.cache.visibility import DirectoryVisibilityCache

    cache = DirectoryVisibilityCache(tiger_cache=None)
    result = cache.compute_batch_visibility("z1", "user", "u1", ["/a/b"], "read")
    assert result == {}
```

- [ ] **Step 2: Run — expect FAIL**

```bash
pytest tests/unit/rebac/test_prefix_helpers.py::test_compute_batch_visibility_correct_results -v
```

Expected: FAIL — current implementation calls `get_accessible_resources` and loops int IDs.

- [ ] **Step 3: Rewrite `compute_batch_visibility` in `visibility.py`**

Replace lines 279–318 (everything after `if not self._tiger_cache: return {}`) with:

```python
        accessible_paths = self._tiger_cache.get_accessible_paths(
            subject_type=subject_type,
            subject_id=subject_id,
            permission=permission,
            resource_type="file",
            zone_id=zone_id,
        )

        if accessible_paths is None:
            return {}  # cache miss

        if not accessible_paths:
            results: dict[str, bool] = {}
            for dp in dir_paths:
                self.set_visible(zone_id, subject_type, subject_id, dp, False, "no_accessible_resources")
                results[dp] = False
            return results

        from nexus.bricks.rebac.cache._prefix_helpers import batch_paths_under_prefixes

        visible_flags = batch_paths_under_prefixes(accessible_paths, dir_paths)
        results = {}
        for dp, visible in zip(dir_paths, visible_flags, strict=True):
            reason = "batch_bitmap" if visible else "no_descendants_in_bitmap"
            self.set_visible(zone_id, subject_type, subject_id, dp, visible, reason)
            results[dp] = visible
        return results
```

The full method signature stays unchanged:

```python
    def compute_batch_visibility(
        self,
        zone_id: str,
        subject_type: str,
        subject_id: str,
        dir_paths: list[str],
        permission: str = "read",
    ) -> dict[str, bool]:
```

- [ ] **Step 4: Run tests — expect PASS**

```bash
pytest tests/unit/rebac/test_prefix_helpers.py -v
```

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/bricks/rebac/cache/visibility.py \
        tests/unit/rebac/test_prefix_helpers.py
git commit -m "refactor(visibility): compute_batch_visibility via get_accessible_paths + batch helper"
```

---

## Task 4: Update `descendant_access.py` Tiger fallback

**Files:**
- Modify: `src/nexus/services/namespace/descendant_access.py:305-335`

- [ ] **Step 1: Write failing test**

Append to `tests/unit/rebac/test_prefix_helpers.py`:

```python
# ---------------------------------------------------------------------------
# DescendantAccessChecker.has_access — Tiger fallback uses get_accessible_paths
# ---------------------------------------------------------------------------

def test_has_access_tiger_fallback_uses_get_accessible_paths():
    """Tiger fallback must call get_accessible_paths, not loop _resource_map."""
    from unittest.mock import MagicMock
    from nexus.services.namespace.descendant_access import DescendantAccessChecker

    tiger_cache = MagicMock()
    tiger_cache.get_accessible_paths.return_value = {"/workspace/joe/file.txt"}

    # spec=[] means only explicitly-set attributes exist.
    # hasattr() returns False for everything else, so the faster optimisation
    # paths (tiger_check_access, rebac_check_bulk, rebac_check_bulk_sync,
    # tiger_get_accessible_resources) are all skipped, letting the code reach
    # the Tiger Cache bitmap fallback at line 305.
    rebac_manager = MagicMock(spec=[])
    rebac_manager._tiger_cache = tiger_cache

    rebac_service = MagicMock(spec=[])
    rebac_service.rebac_check_sync = MagicMock(return_value=False)  # direct access denied

    ctx = MagicMock()
    ctx.is_admin = False
    ctx.is_system = False
    ctx.subject_id = "joe"
    ctx.subject_type = "user"
    ctx.zone_id = "z1"

    metadata_store = MagicMock()
    metadata_store.list.return_value = []

    checker = DescendantAccessChecker(
        rebac_manager=rebac_manager,
        rebac_service=rebac_service,
        dir_visibility_cache=None,
        permission_enforcer=MagicMock(),
        metadata_store=metadata_store,
    )

    from nexus.contracts.types import Permission
    result = checker.has_access("/workspace/joe", Permission.READ, ctx)

    assert result is True
    tiger_cache.get_accessible_paths.assert_called_once()
```

- [ ] **Step 2: Run — expect FAIL**

```bash
pytest tests/unit/rebac/test_prefix_helpers.py::test_has_access_tiger_fallback_uses_get_accessible_paths -v
```

Expected: FAIL — current implementation calls `get_accessible_resources` + loops `_resource_map`.

- [ ] **Step 3: Replace Tiger fallback in `descendant_access.py`**

Replace lines 305–335 (the `# Issue #3192` block through the end of its `except` clause):

```python
        # Issue #3192 / #3951: Tiger Cache bitmap before individual fallback
        tiger_cache = (
            getattr(self._rebac_manager, "_tiger_cache", None) if self._rebac_manager else None
        )
        if tiger_cache is not None:
            try:
                accessible_paths = tiger_cache.get_accessible_paths(
                    subject_type=subject_tuple[0],
                    subject_id=subject_tuple[1],
                    permission=rebac_permission,
                    resource_type="file",
                    zone_id=zone_id,
                )
                if accessible_paths:
                    from nexus.bricks.rebac.cache._prefix_helpers import any_path_under_prefix

                    if any_path_under_prefix(accessible_paths, path):
                        if self._dir_visibility_cache is not None:
                            self._dir_visibility_cache.set_visible(
                                zone_id,
                                context.subject_type,
                                subject_id,
                                path,
                                True,
                                "tiger_fallback",
                            )
                        return True
            except Exception:
                logger.debug("has_access: Tiger Cache fallback failed, using individual checks")
```

- [ ] **Step 4: Run tests — expect PASS**

```bash
pytest tests/unit/rebac/test_prefix_helpers.py -v
```

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/services/namespace/descendant_access.py \
        tests/unit/rebac/test_prefix_helpers.py
git commit -m "refactor(descendant-access): Tiger fallback via get_accessible_paths + prefix helper"
```

---

## Task 5: Clean up `enforcer.py` inline try/except

**Files:**
- Modify: `src/nexus/bricks/rebac/enforcer.py:347-365`

- [ ] **Step 1: Locate the block to replace**

In `src/nexus/bricks/rebac/enforcer.py` find this block (around line 347):

```python
            # RUST_FALLBACK: rebac enforcer — nexus_runtime for batch permission checks
            # Try Rust-accelerated prefix matching (Issue #1565)
            try:
                import nexus_runtime

                results_list = nexus_runtime.batch_prefix_check(
                    list(accessible_paths), list(prefixes)
                )
                results = dict(zip(prefixes, results_list, strict=True))
            except (ImportError, AttributeError):
                # Python fallback (same logic, O(N×M))
                results = {}
                for prefix in prefixes:
                    prefix_normalized = prefix.rstrip("/") + "/"
                    prefix_exact = prefix.rstrip("/")
                    results[prefix] = any(
                        p.startswith(prefix_normalized) or p == prefix_exact
                        for p in accessible_paths
                    )
```

- [ ] **Step 2: Replace with helper call**

Replace that entire block with:

```python
            from nexus.bricks.rebac.cache._prefix_helpers import batch_paths_under_prefixes

            results_list = batch_paths_under_prefixes(list(accessible_paths), list(prefixes))
            results = dict(zip(prefixes, results_list, strict=True))
```

- [ ] **Step 3: Run the full rebac unit test suite**

```bash
pytest tests/unit/rebac/ -v
```

Expected: all existing tests PASS (no behaviour change — the helper uses the same Rust primitive and the same Python fallback semantics).

- [ ] **Step 4: Commit**

```bash
git add src/nexus/bricks/rebac/enforcer.py
git commit -m "refactor(enforcer): remove inline nexus_runtime try/except; use _prefix_helpers"
```

---

## Task 6: Perf regression guard

**Files:**
- Create: `tests/unit/rebac/bench_prefix_helpers.py`

- [ ] **Step 1: Create perf test file**

Create `tests/unit/rebac/bench_prefix_helpers.py`:

```python
"""Perf regression guard for prefix helpers (Issue #3951).

Not a microbenchmark — asserts that large-scale calls complete within
generous wall-clock bounds. Run in CI with: pytest tests/unit/rebac/bench_prefix_helpers.py -v
"""
from __future__ import annotations

import time

import pytest

pytest.importorskip("pyroaring")


def _make_paths(n: int) -> list[str]:
    """Generate n distinct file paths under /workspace/."""
    return [f"/workspace/user_{i % 1000}/project_{i // 1000}/file_{i}.txt" for i in range(n)]


def _make_prefixes(n: int) -> list[str]:
    """Generate n distinct directory prefixes."""
    return [f"/workspace/user_{i}" for i in range(n)]


# ---------------------------------------------------------------------------
# any_path_under_prefix
# ---------------------------------------------------------------------------

def test_any_path_under_prefix_50k_paths_under_500ms():
    from nexus.bricks.rebac.cache._prefix_helpers import any_path_under_prefix

    paths = _make_paths(50_000)
    prefix = "/workspace/user_999"  # match near the end

    start = time.perf_counter()
    result = any_path_under_prefix(paths, prefix)
    elapsed_ms = (time.perf_counter() - start) * 1000

    assert result is True
    assert elapsed_ms < 500, f"any_path_under_prefix over 50K paths took {elapsed_ms:.1f}ms (limit 500ms)"


def test_any_path_under_prefix_python_fallback_50k_paths_under_500ms(monkeypatch):
    import nexus.bricks.rebac.cache._prefix_helpers as ph
    monkeypatch.setattr(ph, "_rust_any", None)

    paths = _make_paths(50_000)
    prefix = "/workspace/user_999"

    start = time.perf_counter()
    result = ph.any_path_under_prefix(paths, prefix)
    elapsed_ms = (time.perf_counter() - start) * 1000

    assert result is True
    assert elapsed_ms < 500, f"Python fallback over 50K paths took {elapsed_ms:.1f}ms (limit 500ms)"


# ---------------------------------------------------------------------------
# batch_paths_under_prefixes
# ---------------------------------------------------------------------------

def test_batch_paths_under_prefixes_100k_paths_50_prefixes_under_500ms():
    from nexus.bricks.rebac.cache._prefix_helpers import batch_paths_under_prefixes

    paths = _make_paths(100_000)
    prefixes = _make_prefixes(50)

    start = time.perf_counter()
    results = batch_paths_under_prefixes(paths, prefixes)
    elapsed_ms = (time.perf_counter() - start) * 1000

    assert len(results) == 50
    assert elapsed_ms < 500, f"batch_paths_under_prefixes 100K×50 took {elapsed_ms:.1f}ms (limit 500ms)"


def test_batch_paths_under_prefixes_python_fallback_100k_paths_50_prefixes_under_2000ms(monkeypatch):
    import nexus.bricks.rebac.cache._prefix_helpers as ph
    monkeypatch.setattr(ph, "_rust_batch", None)

    paths = _make_paths(100_000)
    prefixes = _make_prefixes(50)

    start = time.perf_counter()
    results = ph.batch_paths_under_prefixes(paths, prefixes)
    elapsed_ms = (time.perf_counter() - start) * 1000

    assert len(results) == 50
    # Python fallback is O(N×M) — generous 2s limit to avoid CI flakiness
    assert elapsed_ms < 2000, f"Python fallback 100K×50 took {elapsed_ms:.1f}ms (limit 2000ms)"
```

- [ ] **Step 2: Run perf tests**

```bash
pytest tests/unit/rebac/bench_prefix_helpers.py -v
```

Expected: all PASS. If Rust is available, the 500ms limits should be comfortably met. Python fallback may be slower but must stay under 2s for 100K×50.

- [ ] **Step 3: Run full test suite to check no regressions**

```bash
pytest tests/unit/rebac/ -v
```

Expected: all PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/unit/rebac/bench_prefix_helpers.py
git commit -m "test(rebac): add perf regression guard for prefix helpers"
```

---

## Final Verification

- [ ] **Run the full rebac test suite one more time**

```bash
pytest tests/unit/rebac/ -v --tb=short 2>&1 | tail -20
```

Expected: all PASS, no warnings about `_resource_map` private access or inline `nexus_runtime` imports in the changed files.

- [ ] **Confirm no remaining private `_resource_map` access in changed files**

```bash
grep -n "_resource_map" \
  src/nexus/bricks/rebac/cache/visibility.py \
  src/nexus/services/namespace/descendant_access.py
```

Expected: no output (both files stop accessing `_resource_map` directly).

- [ ] **Confirm `enforcer.py` no longer imports `nexus_runtime` directly**

```bash
grep -n "import nexus_runtime" src/nexus/bricks/rebac/enforcer.py
```

Expected: no output.
