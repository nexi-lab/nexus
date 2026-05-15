# Tiger/Prefix Helpers in Descendant-Access Hot Paths

**Issue:** #3951
**Date:** 2026-04-30

## Problem

Descendant-access and visibility code in three hot paths performs Python set/list scans over accessible resource maps and descendant paths, even though the kernel already exposes `batch_prefix_check` and `any_path_starts_with` Rust primitives that do this work faster and at a single validated boundary.

Affected paths:
- `visibility.py:compute_from_tiger_bitmap()` — int_id loop + manual `resource_map.get_resource_id()` + Python `startswith`
- `visibility.py:compute_batch_visibility()` — same int_id loop, then `any(p.startswith(...))` per directory
- `descendant_access.py:has_access()` Tiger fallback (lines 305–335) — int_id loop + `_resource_map` private access + Python `startswith`
- `enforcer.py:has_accessible_descendants_batch()` — inline `try: import nexus_runtime` with duplicated Python fallback

## Design

### Approach

A thin `_prefix_helpers.py` wrapper module in the rebac cache layer. Two public functions, each attempting Rust first and falling back to Python inline. Call sites replace their loops/try-excepts with these helpers. No new public APIs, no schema changes, no new dependencies.

### 1. `_rust_compat.py` additions

Add two re-exports via `_get()`:

```python
batch_prefix_check = _get("batch_prefix_check")
any_path_starts_with = _get("any_path_starts_with")
```

Both are module-level symbols in `nexus_runtime` not currently in `_rust_compat.py`. They are gated by `_nf_snapshot is None` (i.e., `RUST_AVAILABLE`) through the existing `_get()` mechanism. They are read-only path helpers — a stale ABI returning wrong results here is not a security risk.

### 2. New file: `nexus/bricks/rebac/cache/_prefix_helpers.py`

```python
from nexus._rust_compat import any_path_starts_with as _rust_any
from nexus._rust_compat import batch_prefix_check as _rust_batch

def any_path_under_prefix(paths: list[str] | set[str], prefix: str) -> bool:
    paths_list = list(paths) if isinstance(paths, set) else paths
    if _rust_any is not None:
        return bool(_rust_any(paths_list, prefix))
    norm = prefix.rstrip("/") + "/"
    return any(p == prefix or p.startswith(norm) for p in paths_list)

def batch_paths_under_prefixes(
    paths: list[str] | set[str], prefixes: list[str]
) -> list[bool]:
    paths_list = list(paths) if isinstance(paths, set) else paths
    if _rust_batch is not None:
        return list(_rust_batch(paths_list, prefixes))
    norm_map = {p: p.rstrip("/") + "/" for p in prefixes}
    return [
        any(path == pfx or path.startswith(norm_map[pfx]) for path in paths_list)
        for pfx in prefixes
    ]
```

`set` → `list` coercion is O(n) but required since Rust expects a list. `_rust_any`/`_rust_batch` are bound at import time (no per-call attribute lookup).

### 3. `visibility.py` — `DirectoryVisibilityCache`

**`compute_from_tiger_bitmap()`**: Replace `get_accessible_resources()` + int_id loop + `_resource_map` access with `get_accessible_paths()` + `any_path_under_prefix()`.

```python
accessible_paths = self._tiger_cache.get_accessible_paths(
    subject_type=subject_type, subject_id=subject_id,
    permission=permission, resource_type="file", zone_id=zone_id,
)
if accessible_paths is None:
    return None  # cache miss — unchanged behaviour
if not accessible_paths:
    self.set_visible(..., False, "no_accessible_resources")
    return False
result = any_path_under_prefix(accessible_paths, dir_path)
self.set_visible(..., result, "bitmap_prefix" if result else "no_descendants_in_bitmap")
return result
```

**`compute_batch_visibility()`**: Same `get_accessible_paths()` swap, then `batch_paths_under_prefixes()` replaces the `any()` loop.

```python
accessible_paths = self._tiger_cache.get_accessible_paths(...)
if accessible_paths is None:
    return {}  # cache miss — unchanged behaviour
visible_flags = batch_paths_under_prefixes(accessible_paths, dir_paths)
results = {}
for dp, visible in zip(dir_paths, visible_flags):
    reason = "batch_bitmap" if visible else "no_descendants_in_bitmap"
    self.set_visible(zone_id, subject_type, subject_id, dp, visible, reason)
    results[dp] = visible
return results
```

Both methods stop accessing `self._tiger_cache._resource_map` directly.

### 4. `descendant_access.py` — `DescendantAccessChecker.has_access()`

Replace the Tiger fallback block (lines 305–335) — `get_accessible_resources()` + int_id loop + `_resource_map` private access:

```python
if tiger_cache is not None:
    try:
        accessible_paths = tiger_cache.get_accessible_paths(
            subject_type=subject_tuple[0], subject_id=subject_tuple[1],
            permission=rebac_permission, resource_type="file", zone_id=zone_id,
        )
        if accessible_paths:
            if any_path_under_prefix(accessible_paths, path):
                if self._dir_visibility_cache is not None:
                    self._dir_visibility_cache.set_visible(
                        zone_id, context.subject_type, subject_id,
                        path, True, "tiger_fallback"
                    )
                return True
    except Exception:
        logger.debug("has_access: Tiger Cache fallback failed, using individual checks")
```

Out-of-scope notes (follow-up issues):
- Lines 211–228: "OPTIMIZATION 5 (legacy)" fetches `accessible_ids` but never acts on them (dead code)
- `has_access_bulk()` still uses metadata store + Python set filtering — separate optimization

### 5. `enforcer.py` — cleanup only

`has_accessible_descendants_batch()` lines 347–365: replace inline try/except with the new helper.

```python
# before: ~14 lines of try/except + duplicated Python fallback
# after:
from nexus.bricks.rebac.cache._prefix_helpers import batch_paths_under_prefixes
results_list = batch_paths_under_prefixes(list(accessible_paths), list(prefixes))
results = dict(zip(prefixes, results_list, strict=True))
```

Net: ~12 lines deleted, 2 added.

## Data Flow

```
has_access() / compute_from_tiger_bitmap()
  └─ tiger_cache.get_accessible_paths()      # int_id → path, already in TigerCache
       └─ any_path_under_prefix(paths, dir)
            ├─ [Rust] _rust_any(paths, dir)   # nexus_runtime.any_path_starts_with
            └─ [Python] any(p.startswith(..)) # unchanged fallback logic

compute_batch_visibility() / has_accessible_descendants_batch()
  └─ tiger_cache.get_accessible_paths()
       └─ batch_paths_under_prefixes(paths, dirs)
            ├─ [Rust] _rust_batch(paths, dirs) # nexus_runtime.batch_prefix_check
            └─ [Python] loop any(..)           # unchanged fallback logic
```

## Error Handling / Fallback

- If `nexus_runtime` absent or stale: `_rust_any`/`_rust_batch` are `None` at import time, Python fallback runs automatically — no try/except needed at call sites
- If `get_accessible_paths()` returns `None` (cache miss): both `compute_from_tiger_bitmap` returns `None` and `compute_batch_visibility` returns `{}` — unchanged from current behaviour
- Tiger fallback block in `descendant_access.py` retains its `except Exception` wrapper — any failure falls through to the individual-check slow path, same as before

## Tests

**`tests/bricks/rebac/cache/test_prefix_helpers.py`** — unit tests:
- `any_path_under_prefix`: exact match, descendant, non-match, root `/`, trailing-slash variants
- `batch_paths_under_prefixes`: result length matches prefixes, order preserved
- Python fallback path explicitly (mock `_rust_any`/`_rust_batch` to `None`)

**`tests/bricks/rebac/cache/test_prefix_helpers_perf.py`** — perf regression guard:
- 100K paths × 50 prefixes via `batch_paths_under_prefixes` — assert completes < 500ms
- `any_path_under_prefix` with 50K paths — assert completes < 100ms
- Both with Rust available and mocked out (verifies Python fallback is not catastrophically slow)

## Acceptance Criteria (from issue)

- Descendant-access checks use kernel batch/prefix helpers when available ✓
- Python fallback remains behaviorally identical when Rust is absent/stale ✓
- Benchmarks/targeted perf tests for large accessible-resource maps and large directory descendant sets ✓
