# Issue #2074: Deduplicate Consistency Modules Between rebac/ and services/permissions/

## Agreed Decisions (from interactive review)

| # | Decision | Option |
|---|----------|--------|
| 1 | `services/permissions/` is canonical | B — ORM-based, modern, stated canonical |
| 2 | 2-arg `increment_version_token` | A — remove `ConnectionHelper`, update `manager.py` |
| 3 | Incremental migration by subdirectory | A — testable commits |
| 4 | Keep superset `utils/zone.py` | A — shim `rebac/` to `services/permissions/` |
| 5 | Convert `rebac/` subdirs to re-export shims | A — one source of truth |
| 6 | ORM versions are canonical | A — type-safe, consistent |
| 7 | Remove `ConnectionHelper` protocol | A — dead abstraction, YAGNI |
| 8 | Accept bidirectional shims with docs | A — temporary, migration-in-progress |
| 9 | Add shim verification tests | A — lightweight import identity tests |
| 10 | Update stale test imports | A — 7 lines, same PR |
| 11 | Add edge case tests for `revision.py` | A — 3-4 targeted tests |
| 12 | Add integration test for manager→ORM wiring | A — regression safety |
| 13 | ORM overhead is negligible | A — no pre-compilation needed |
| 14 | Version token perf is pre-existing | A — document, don't change |
| 15 | Import chain is fine | A — Python module cache |
| 16 | begin/connect distinction is correct | A — keep as-is |

## Implementation Plan

### Phase 1: `consistency/` subdirectory (3 files)

**Step 1.1: Update `manager.py` — fix the 3→2 arg call**
- File: `src/nexus/rebac/manager.py`
- Line 38-41: Change import from `nexus.rebac.consistency.revision` → `nexus.services.permissions.consistency.revision`
- Line 42: Change import from `nexus.rebac.consistency.zone_manager` → `nexus.services.permissions.consistency.zone_manager`
- Line 2868: Change `increment_version_token(self.engine, self._repo, zone_id)` → `increment_version_token(self.engine, zone_id)`

**Step 1.2: Convert `rebac/consistency/revision.py` to shim**
- Delete all implementation code (157 lines)
- Replace with 5-line re-export from `nexus.services.permissions.consistency.revision`
- Remove `ConnectionHelper` protocol entirely

**Step 1.3: Convert `rebac/consistency/zone_manager.py` to shim**
- Delete all implementation code
- Replace with re-export from `nexus.services.permissions.consistency.zone_manager`

**Step 1.4: Convert `rebac/consistency/__init__.py` to shim**
- Re-export all 4 symbols from `nexus.services.permissions.consistency`

**Step 1.5: Add shim verification test**
- New file: `tests/unit/rebac/consistency/test_shim_reexport.py`
- Assert: `nexus.rebac.consistency.X is nexus.services.permissions.consistency.X` for all 4 symbols

**Step 1.6: Add edge case tests for `revision.py`**
- Add to `tests/unit/services/permissions/consistency/test_revision.py`:
  - `test_empty_zone_id_works`
  - `test_get_revision_after_increment`

### Phase 2: `utils/` subdirectory (3 files + __init__)

**Step 2.1: Convert `rebac/utils/zone.py` to shim**
- Re-export all symbols (including the 6 extra functions in the `services/permissions` superset)

**Step 2.2: Convert `rebac/utils/changelog.py` to shim**
- Re-export `insert_changelog_entry` from canonical path

**Step 2.3: Convert `rebac/utils/fast.py` to shim**
- Re-export all fast/Rust-accelerated functions

**Step 2.4: Convert `rebac/utils/__init__.py` to shim**

### Phase 3: `graph/` subdirectory (4 files + __init__)

**Step 3.1: Convert `rebac/graph/bulk_evaluator.py` to shim** (byte-identical)
**Step 3.2: Convert `rebac/graph/expand.py` to shim** (1-line diff)
**Step 3.3: Convert `rebac/graph/traversal.py` to shim** (ORM upgrade)
**Step 3.4: Convert `rebac/graph/zone_traversal.py` to shim** (ORM upgrade)
**Step 3.5: Convert `rebac/graph/__init__.py` to shim**

### Phase 4: `tuples/` subdirectory (1 file + __init__)

**Step 4.1: Convert `rebac/tuples/repository.py` to shim**
- Note: `manager.py` uses `TupleRepository` extensively. The shim preserves the import path.
**Step 4.2: Convert `rebac/tuples/__init__.py` to shim**

### Phase 5: `directory/` subdirectory (1 file + __init__)

**Step 5.1: Update `rebac/directory/expander.py` internal import**
- Change `from nexus.rebac.consistency.revision import get_zone_revision_for_grant` to import from canonical
**Step 5.2: Convert `rebac/directory/expander.py` to shim**
**Step 5.3: Convert `rebac/directory/__init__.py` to shim**

### Phase 6: `batch/` subdirectory (1 file + __init__)

**Step 6.1: Convert `rebac/batch/bulk_checker.py` to shim**
**Step 6.2: Convert `rebac/batch/__init__.py` to shim**

### Phase 7: `cache/tiger/` subdirectory (5 files + __init__)

**Step 7.1: Merge `get_accessible_paths_list` into `services/permissions/cache/tiger/bitmap_cache.py`**
- Copy the method from `rebac/cache/tiger/bitmap_cache.py` (if it exists only there)

**Step 7.2: Convert `rebac/cache/tiger/bitmap_cache.py` to shim**
**Step 7.3: Convert `rebac/cache/tiger/expander.py` to shim**
**Step 7.4: Convert `rebac/cache/tiger/facade.py` to shim**
**Step 7.5: Convert `rebac/cache/tiger/resource_map.py` to shim**
**Step 7.6: Convert `rebac/cache/tiger/updater.py` to shim**
**Step 7.7: Convert `rebac/cache/tiger/__init__.py` to shim**

### Phase 8: Update all `manager.py` imports (bulk)

- Update all remaining `from nexus.rebac.{subdir}` imports in `manager.py` to point to `services/permissions/`
- This ensures `manager.py` directly uses the canonical path (not going through shims)
- Includes: batch, cache.tiger, graph, tuples, utils, directory

### Phase 9: Update test imports

- `tests/unit/services/permissions/test_tuple_repository.py:26` → canonical path
- `tests/e2e/server/test_directory_grants_e2e.py:389,426` → canonical path
- `tests/e2e/postgres/test_read_replica.py:81` → canonical path
- `tests/benchmarks/test_core_operations.py:484,517,560` → canonical path
- `tests/unit/core/test_cross_zone_sharing.py:16` → canonical path

### Phase 10: Add integration test

- New test: verify `manager._get_version_token()` works through the ORM path
- Use existing SQLite test fixtures

### Phase 11: Run validation

- Run full unit test suite: `pytest tests/unit/ -x -q`
- Run consistency-specific tests: `pytest tests/unit/services/permissions/consistency/ -v`
- Run e2e with FastAPI nexus serve with permissions enabled
- Verify no performance regressions in logs

## Files Changed (Summary)

**Converted to shims (24 files):**
- `src/nexus/rebac/consistency/__init__.py`
- `src/nexus/rebac/consistency/revision.py`
- `src/nexus/rebac/consistency/zone_manager.py`
- `src/nexus/rebac/utils/__init__.py`
- `src/nexus/rebac/utils/zone.py`
- `src/nexus/rebac/utils/changelog.py`
- `src/nexus/rebac/utils/fast.py`
- `src/nexus/rebac/graph/__init__.py`
- `src/nexus/rebac/graph/bulk_evaluator.py`
- `src/nexus/rebac/graph/expand.py`
- `src/nexus/rebac/graph/traversal.py`
- `src/nexus/rebac/graph/zone_traversal.py`
- `src/nexus/rebac/tuples/__init__.py`
- `src/nexus/rebac/tuples/repository.py`
- `src/nexus/rebac/directory/__init__.py`
- `src/nexus/rebac/directory/expander.py`
- `src/nexus/rebac/batch/__init__.py`
- `src/nexus/rebac/batch/bulk_checker.py`
- `src/nexus/rebac/cache/tiger/__init__.py`
- `src/nexus/rebac/cache/tiger/bitmap_cache.py`
- `src/nexus/rebac/cache/tiger/expander.py`
- `src/nexus/rebac/cache/tiger/facade.py`
- `src/nexus/rebac/cache/tiger/resource_map.py`
- `src/nexus/rebac/cache/tiger/updater.py`

**Modified (imports updated):**
- `src/nexus/rebac/manager.py` (import paths + 3→2 arg fix)

**New test files:**
- `tests/unit/rebac/consistency/test_shim_reexport.py`

**Updated test files:**
- `tests/unit/services/permissions/consistency/test_revision.py` (edge cases)
- `tests/unit/services/permissions/test_tuple_repository.py` (import path)
- `tests/e2e/server/test_directory_grants_e2e.py` (import path)
- `tests/e2e/postgres/test_read_replica.py` (import path)
- `tests/benchmarks/test_core_operations.py` (import path)
- `tests/unit/core/test_cross_zone_sharing.py` (import path)

## Risk Mitigation

- **Shims preserve all import paths** — no external breakage
- **Incremental by subdirectory** — each phase is independently committable and revertible
- **Shim verification test** catches broken re-exports immediately
- **Integration test** validates the critical `manager.py` → ORM wiring
- **`manager.py` updated BEFORE shims** — ensures the 3→2 arg change is tested before old code is deleted
