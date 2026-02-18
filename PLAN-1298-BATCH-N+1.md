# Plan: #1298 Batch-optimize top 5 N+1 query paths

## Phase 1: Core batch primitives (Issues 4+5)

### 1.1 Add `has_accessible_descendants_batch()` to PermissionEnforcer
**File:** `src/nexus/core/permissions.py`

- Add method `has_accessible_descendants_batch(prefixes: list[str], context: OperationContext) -> dict[str, bool]`
- Load Tiger bitmap ONCE (steps 1-2 of existing method)
- Decode roaring bitmap ONCE
- Build set of allowed paths from decoded IDs
- For each prefix, check if any allowed path starts with `prefix + "/"`
- Fallback-to-True for all prefixes if:
  - Tiger cache is None
  - Bitmap bytes is None
  - Any exception during decode/scan
- Log warning ONCE (not per-prefix) on fallback
- Add `[BATCH-OPT]` timing log

### 1.2 Add `filter_paths_by_permission_sync()` helper (optional)
**File:** `src/nexus/core/permissions.py`

- The existing `filter_list()` already does batch permission filtering (sync)
- Verify it can be used in `batch_read` context — if so, no new method needed
- The async version `filter_paths_by_permission()` in `async_permissions.py` already exists

## Phase 2: Fix `batch_read()` N+1 (Issues 1, 6, 7 partial)

### 2.1 Refactor `AsyncNexusFS.batch_read()`
**File:** `src/nexus/core/async_nexus_fs.py:705-749`

Replace:
```python
# OLD: N individual permission checks
for path in paths:
    await self._acheck_permission(validated_path, Permission.READ, context)

# OLD: N individual metadata lookups
for path in paths:
    meta = await self.metadata.aget(path)
```

With:
```python
# NEW: Validate all paths first
validated_paths = [self._validate_path(p) for p in paths]

# NEW: Batch permission filter (returns allowed subset)
if self._enforce_permissions and self._permission_enforcer:
    ctx = self._get_context(context)
    if not ctx.is_system and not ctx.is_admin:
        allowed = await self._permission_enforcer.filter_paths_by_permission(
            validated_paths, ctx
        )
        allowed_set = set(allowed)
        denied_paths = [p for p in validated_paths if p not in allowed_set]
        # Return None for denied paths (not an error)
        for dp in denied_paths:
            result[dp] = None
        validated_paths = allowed
    # Log denied paths at DEBUG level

# NEW: Batch metadata lookup
batch_meta = await self.metadata.aget_batch(validated_paths)
```

- Return `None` for denied paths (not exception) — matches list_dir pattern
- Log denied count at DEBUG level
- Add `[BATCH-OPT]` timing for permission filter + metadata batch

## Phase 3: Fix cross-zone N+1 (Issues 2, 7 partial)

### 3.1 Replace individual `metadata.get()` with `get_batch()`
**File:** `src/nexus/core/nexus_fs_search.py:889-897`

Replace:
```python
for ct_path in cross_zone_paths:
    if ct_path not in existing_paths:
        ct_meta = self.metadata.get(ct_path)
        if ct_meta:
            all_files.append(ct_meta)
```

With:
```python
# Collect paths needing fetch
fetch_paths = [p for p in cross_zone_paths if p not in existing_paths]
if fetch_paths:
    batch_results = self.metadata.get_batch(fetch_paths)
    for path, meta in batch_results.items():
        if meta is not None:
            all_files.append(meta)
```

- Add `[BATCH-OPT]` timing log

## Phase 4: Fix connector detailed listing N+1 (Issues 3, 7 partial)

### 4.1 Replace individual `metadata.get()` with `get_batch()` in connector details
**File:** `src/nexus/core/nexus_fs_search.py:523-583`

Replace the per-entry `metadata.get(entry_path)` loop with:
```python
# Batch fetch all metadata at once
batch_meta = self.metadata.get_batch(all_paths)
for entry_path in all_paths:
    file_meta = batch_meta.get(entry_path)
    # ... rest of logic unchanged (is_directory fallback stays sequential)
```

- Keep `is_directory()` sequential (per decision 3B)
- Add `[BATCH-OPT]` timing log

## Phase 5: Replace per-dir `has_accessible_descendants()` with batch (Issues 4, 5)

### 5.1 Fast path directory filtering
**File:** `src/nexus/core/nexus_fs_search.py:691-723`

Replace:
```python
for entry in dir_entries:
    if entry["type"] == "directory":
        if self._permission_enforcer.has_accessible_descendants(entry_path, context):
            _preapproved_dirs.add(entry_path)
```

With:
```python
dir_paths = [
    f"{path.rstrip('/')}/{entry['name']}"
    for entry in dir_entries
    if entry["type"] == "directory"
]
if dir_paths:
    accessible = self._permission_enforcer.has_accessible_descendants_batch(
        dir_paths, context
    )
    _preapproved_dirs = {p for p, is_ok in accessible.items() if is_ok}
```

### 5.2 Connector directory filtering
**File:** `src/nexus/core/nexus_fs_search.py:508-514`

Replace list comprehension with batch call:
```python
if dir_paths:
    accessible = self._permission_enforcer.has_accessible_descendants_batch(
        [d.rstrip("/") for d in dir_paths], filter_ctx
    )
    filtered_dirs = [d for d in dir_paths if accessible.get(d.rstrip("/"), True)]
```

## Phase 6: Tests

### 6.1 Unit tests for `has_accessible_descendants_batch()` (5 tests)
**File:** `tests/unit/core/test_permission_enforcer.py`

1. `test_has_accessible_descendants_batch_empty` — empty list returns empty dict
2. `test_has_accessible_descendants_batch_all_accessible` — all prefixes have descendants
3. `test_has_accessible_descendants_batch_mixed` — some accessible, some not
4. `test_has_accessible_descendants_batch_no_tiger_cache` — fallback returns all True
5. `test_has_accessible_descendants_batch_decode_error` — fallback returns all True

### 6.2 Unit tests for `batch_read` mixed permissions (3 unit + 1 integration)
**File:** `tests/unit/core/test_async_nexus_fs.py`

1. `test_batch_read_mixed_permissions` — some allowed, some denied → allowed files returned, denied get None
2. `test_batch_read_all_denied` — all paths denied → all None, no exception
3. `test_batch_read_permissions_disabled` — backward compat, all data returned

**File:** `tests/integration/test_async_files_integration.py`

4. `test_batch_read_with_permission_filtering` — integration with real permission enforcer

### 6.3 Integration test for cross-zone batch metadata
**File:** `tests/integration/test_cross_zone_list.py` (new)

1. `test_list_with_cross_zone_shares_uses_batch` — create shares, list directory, verify shared files appear

### 6.4 E2E smoke test with permissions
**File:** `tests/e2e/test_batch_optimization_e2e.py` (new)

1. `test_list_with_permissions_returns_correct_files` — start server, create files with permissions, list, verify
2. `test_batch_read_with_permissions_returns_correct_files` — batch read with mixed permissions

## Phase 7: Manual performance verification (12C)

1. Start `nexus serve` with `--enforce-permissions`
2. Create test zone with 100+ files and permissions
3. Run list and batch_read operations
4. Grep logs for `[BATCH-OPT]` entries
5. Compare with baseline timing from `[LIST-TIMING]` logs
6. Verify no performance regression

## Files Modified

| File | Changes |
|------|---------|
| `src/nexus/core/permissions.py` | Add `has_accessible_descendants_batch()` |
| `src/nexus/core/async_nexus_fs.py` | Refactor `batch_read()` |
| `src/nexus/core/nexus_fs_search.py` | 3 batch replacements (cross-zone, connector, fast-path dirs) |
| `tests/unit/core/test_permission_enforcer.py` | 5 new tests |
| `tests/unit/core/test_async_nexus_fs.py` | 3 new tests |
| `tests/integration/test_async_files_integration.py` | 1 new test |
| `tests/integration/test_cross_zone_list.py` | 1 new test (new file) |
| `tests/e2e/test_batch_optimization_e2e.py` | 2 new tests (new file) |

## Estimated effort: ~8-10 hours

## Risk assessment: LOW
- All batch APIs (`get_batch`, `aget_batch`, `filter_paths_by_permission`, `rebac_check_bulk`) already exist and are tested
- Changes are additive (new batch method) or substitutive (swap loop → batch call)
- Fallback-to-True semantics preserved
- Existing timing instrumentation helps catch regressions
