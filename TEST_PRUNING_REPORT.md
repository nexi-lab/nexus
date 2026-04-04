# Test Suite Pruning Report

**Date:** 2026-03-24
**Suite size:** ~14,000 Python tests across 844 files (~277K lines of test code)

---

## 1. Immediate Deletions (safe, no risk)

### 1a. Duplicate unit test file — `test_rebac_manager_operations.py`

`tests/unit/core/test_rebac_manager_operations.py` (574 lines, ~22 tests) is a near-complete
duplicate of `tests/unit/core/test_nexus_fs_rebac_mixin.py` (906 lines). Same fixture setup,
same operations, same assertions. The mixin file is a strict superset.

**Action:** Migrate 2-3 unique tests (`test_create_prevents_cycles`, `test_cache_hit_on_repeated_check`,
`test_cache_invalidated_on_write`) to `test_nexus_fs_rebac_mixin.py`, then delete the file.

**Savings:** ~550 lines

### 1b. Unreachable code blocks (3 files)

| File | Line | Issue |
|------|------|-------|
| `tests/e2e/self_contained/test_write_back_integration.py` | 101 | Unreachable code after `return` |
| `tests/e2e/server/test_write_back_e2e.py` | 68 | Unreachable code after `return` |
| `tests/unit/backends/test_streaming.py` | 194 | Unreachable code after `return` |

### 1c. Misplaced e2e tests that are actually unit tests

`tests/e2e/self_contained/test_device_capabilities_e2e.py` functions `test_mismatch_warning_logged`
and `test_no_warning_for_matching_profile` call `warn_if_profile_exceeds_device()` directly — not
through HTTP. These duplicate existing unit tests in `tests/unit/core/test_device_capabilities.py`.

**Action:** Delete those 2 test functions from the e2e file.

---

## 2. Consolidation via `@pytest.mark.parametrize` (high ROI)

Tests that follow identical patterns and differ only in inputs. Consolidating these
reduces lines without losing coverage.

| File | Tests to Consolidate | Pattern | Est. Savings |
|------|---------------------|---------|-------------|
| `test_mcp_server_tools.py` | 14 error-handling tests | `mock.side_effect = X; call(); assert "Error"` | ~250 lines |
| `test_mcp_server_tools.py` | 3 sandbox-not-available tests | All set `sandbox_available=False`, check same thing | ~50 lines |
| `test_service_lifecycle_coordinator.py` | 6 protocol conformance tests | `isinstance(svc, Protocol)` checks | ~80 lines |
| `test_service_lifecycle_coordinator.py` | 4 quadrant classification tests | `classify(svc) == expected` | ~40 lines |
| `test_service_lifecycle_coordinator.py` | 4 activate/deactivate reject tests | Identical structure | ~30 lines |
| `test_nexus_fs_rebac_behavior.py` | 6 share_with_user/group mirrors | Same logic, user vs group | ~90 lines |
| `test_traversal_edge_cases.py` | 2 intersection denied tests | Differ only in which relation granted | ~30 lines |
| `test_traversal_edge_cases.py` | 2 depth-limit boundary tests | `(chain_len, expected)` | ~30 lines |
| `test_tuple_repository.py` | 6 cross-zone validation tests | One-liner static method calls | ~60 lines |
| `test_pipe.py` | 5 nowait-raises tests | `op() → raises(X)` | ~50 lines |
| `test_pipe.py` | 3 manager error tests | `close/destroy/read nonexistent` | ~30 lines |

**Total parametrize savings: ~740 lines**

---

## 3. Near-Duplicate Test Deletion

Tests that test the same behavior with the same assertions, just organized differently.

| File | Duplicate A | Duplicate B | Action |
|------|------------|------------|--------|
| `test_brick_lifecycle.py` | `TestBrickRegistration.test_unregister_removes_brick` | `TestUnregister.test_unregister_from_unmounted` | Delete one |
| `test_brick_lifecycle.py` | `TestBrickRegistration.test_force_unregister_removes_brick` | `TestUnregister.test_force_unregister_bypasses_state` | Delete one |
| `test_brick_reconciler.py` | `TestReconcilePerBrick.test_healthy_no_action` clears backoff | `TestBackoffStrategy.test_clear_on_success` clears backoff | Delete one |
| `test_rebac.py` | `test_cache_invalidation_on_write` | `test_write_invalidates_then_check_repopulates` (superset) | Delete weaker |
| `test_rebac.py` | Caching test in `test_caching` | Same check repeated in `test_cache_invalidation_on_write` | Consolidate |
| `test_federated_search.py` | `test_mints_delegation_for_remote_zone` | `test_remote_zone_uses_transport` (~80% shared code) | Merge |

**Total near-duplicate savings: ~200 lines**

---

## 4. Unused Variables (vulture findings)

151 unused variables found at 90%+ confidence. Most are pytest fixtures consumed
by side-effect (e.g., `clean_db`, `setup_permissions`) — these are false positives.

**Genuine dead code worth cleaning:**
- `tests/e2e/server/test_contextual_chunking_e2e.py`: 21 unused tuple-unpacking vars
  (`doc_summary`, `next_chunks`, `prev_chunks`) across 7 tests — use `_` placeholders
- `tests/benchmarks/test_indexing_benchmarks.py`: `compute_lines`, `parallel` unused
- `tests/unit/bricks/search/test_federated_search.py`: `path_filter`, `alpha` unused 5 times
- `tests/unit/auth/test_protocols.py`: `create_api_key`, `create_agents`, `import_skills` unused

---

## 5. Collection Errors (12 files)

These tests fail to collect — they're skipped silently in CI but represent tech debt.

| Error | Files | Root Cause |
|-------|-------|-----------|
| `No module 'hypothesis'` | 10 files | Missing `pytest.importorskip("hypothesis")` guard |
| `No module 'nexus_kernel'` | 2 files (`test_shm_pipe.py`, `test_shm_stream.py`) | Missing skip when Rust ext not built |
| `No module 'pytest_alembic'` | 1 file | Missing optional dep guard |

**Action:** Add `pytest.importorskip()` at the top of each file. This is a 1-line fix per file
and makes `--collect-only` clean.

---

## 6. Total Estimated Savings

| Category | Lines Saved | Files Affected |
|----------|-------------|----------------|
| Delete duplicate `test_rebac_manager_operations.py` | ~550 | 1 deleted, 1 modified |
| Parametrize consolidation | ~740 | 10 files |
| Near-duplicate deletion | ~200 | 6 files |
| Unreachable code removal | ~10 | 3 files |
| Misplaced e2e test deletion | ~30 | 1 file |
| **Total** | **~1,530 lines** | **~20 files** |

This represents ~0.6% of test code. The real win is not line count but **maintenance burden**:
these redundant tests break on refactors without providing additional safety.

---

## 7. Automated Weekly Monitoring (NEW)

Created `.github/workflows/test-health.yml` — runs every Monday at 9am UTC or on manual trigger.

**Checks performed:**
1. Collection errors (broken imports)
2. Dead test code (vulture scan)
3. Slowest 50 tests
4. Coverage overlap analysis (per-test context Jaccard similarity)
5. Test count trends by directory

Results are uploaded as artifacts and summarized in the GitHub Actions step summary.

**Supporting script:** `scripts/find_redundant_tests.py` — analyzes `coverage.json` with
per-test contexts to find redundant test pairs (Jaccard >=0.95) and subset tests.

---

## 8. Recommended Execution Order

1. **Fix collection errors** (12 files, 1-line fixes) — immediate, low risk
2. **Delete `test_rebac_manager_operations.py`** — biggest single win
3. **Parametrize `test_mcp_server_tools.py`** — largest consolidation target
4. **Parametrize `test_service_lifecycle_coordinator.py`** — second largest
5. **Clean up unused variables** — mechanical, no risk
6. **Merge remaining near-duplicates** — requires careful review
7. **Enable test-health workflow** — merge the PR to start weekly monitoring
