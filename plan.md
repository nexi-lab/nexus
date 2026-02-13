# Plan: #1396 — Split nexus_fast lib.rs (3,587 LOC) into modules

## Approved Decisions

| # | Section | Decision | Choice |
|---|---------|----------|--------|
| 1 | Architecture | Module split granularity | **1A**: 7 modules (rebac, grep, glob_filter, cache, tiger_cache, similarity, blake3_hash) |
| 2 | Architecture | Non-interned dedup | **2A**: Defer to follow-up PR (move both paths into rebac.rs now) |
| 3 | Architecture | Namespace config parsing DRY | **3A**: Extract `parse_namespace_configs()` helper |
| 4 | Architecture | Python API | **4A**: Keep flat (Rust modules only, Python API unchanged) |
| 5 | Code Quality | Tuple parsing DRY | **5A**: Extract `parse_tuples_from_py()` helper |
| 6 | Code Quality | 8-param functions | **6B**: Defer PermissionContext struct to dedup PR |
| 7 | Code Quality | Constants | **7A**: Hoist MAX_DEPTH and PARALLEL_THRESHOLD to module-level |
| 8 | Code Quality | visited.clone() | **8A**: Defer optimization to dedup PR |
| 9 | Tests | Direct nexus_fast tests | **9B**: Add Python integration tests |
| 10 | Tests | Export smoke test | **10A**: Add 30-line export assertion test |
| 11 | Tests | E2E validation | **11A**: Run e2e permission suite with FastAPI server |
| 12 | Tests | Cargo test | **12A**: Defer to follow-up |
| 13 | Performance | Verify zero overhead | **13A**: Before/after benchmark comparison |
| 14 | Performance | JSON round-trip | **14A**: Defer (LRU cache mitigates) |
| 15 | Performance | Linear scan + graph caching | **15A+16A**: Defer (fixed by interned unification) |

## File Structure

### New Files

```
rust/nexus_fast/src/
    lib.rs              # ~80 LOC: mod declarations + #[pymodule] re-exports
    rebac.rs            # ~1,050 LOC: types, interned types, graph, permission engine, helpers
    grep.rs             # ~500 LOC: GrepMatch, SearchMode, grep_bulk, grep_files_mmap, helpers
    glob_filter.rs      # ~160 LOC: glob_match_bulk, filter_paths
    cache.rs            # ~510 LOC: BloomFilter, L1MetadataCache, CacheMetadata
    tiger_cache.rs      # ~180 LOC: filter_paths_with_tiger_cache, intersect, any, stats
    similarity.rs       # ~380 LOC: cosine/dot/euclidean f32+i8, batch, top_k
    blake3_hash.rs      # ~60 LOC: hash_content, hash_content_smart

tests/unit/core/
    test_nexus_fast_exports.py   # ~50 LOC: smoke test for all 25 exports
    test_nexus_fast.py           # ~200 LOC: direct integration tests for Rust functions
```

### Modified Files

```
rust/nexus_fast/src/lib.rs      # Gutted from 3,587 LOC to ~80 LOC
```

### Unchanged Files

```
rust/nexus_fast/Cargo.toml      # No changes needed (same deps)
src/nexus/core/rebac_fast.py    # No changes (imports nexus_fast.* unchanged)
src/nexus/core/grep_fast.py     # No changes
src/nexus/core/glob_fast.py     # No changes
(all other Python consumers)    # No changes
```

## Module Breakdown

### 1. `lib.rs` (~80 LOC) — Module hub + PyModule

```rust
mod rebac;
mod grep;
mod glob_filter;
mod cache;
mod tiger_cache;
mod similarity;
mod blake3_hash;

#[pymodule]
fn nexus_fast(m: &Bound<PyModule>) -> PyResult<()> {
    // Re-export all 23 functions + 2 classes from submodules
    m.add_function(wrap_pyfunction!(rebac::compute_permissions_bulk, m)?)?;
    m.add_function(wrap_pyfunction!(rebac::compute_permission_single, m)?)?;
    // ... all other re-exports
    m.add_class::<cache::BloomFilter>()?;
    m.add_class::<cache::L1MetadataCache>()?;
    Ok(())
}
```

### 2. `rebac.rs` (~1,050 LOC) — Permission engine

Contains:
- Type definitions: Entity, ReBACTuple, NamespaceConfig, RelationConfig, MemoCache, etc.
- Interned types: InternedEntity, InternedTuple, InternedGraph, etc.
- Non-interned types: ReBACGraph (deferred for deletion in follow-up)
- Thread-local caches: GRAPH_CACHE, NAMESPACE_CONFIG_CACHE
- Constants: `REBAC_MAX_DEPTH`, `PERMISSION_PARALLEL_THRESHOLD`, `NAMESPACE_CACHE_CAPACITY`
- Helper functions (new — DRY extractions):
  - `pub(crate) fn parse_namespace_configs()` — replaces 4 duplicated blocks
  - `pub(crate) fn parse_tuples_from_py()` — replaces 3 duplicated blocks
- Permission computation: compute_permission_interned, _shared, check_relation_with_usersets_*
- Non-interned permission: compute_permission, check_relation_with_usersets
- PyFunctions: compute_permissions_bulk, compute_permission_single, expand_subjects, list_objects_for_subject
- Internal helpers: expand_permission, add_direct_subjects, collect_candidate_objects_for_subject, get_permission_relations, find_subject_groups

### 3. `grep.rs` (~500 LOC) — Content search

Contains:
- GrepMatch struct
- SearchMode enum
- is_literal_pattern()
- Constants: GREP_MMAP_PARALLEL_THRESHOLD, GREP_MMAP_MAX_FILE_SIZE
- PyFunctions: grep_bulk, grep_files_mmap
- Internal: grep_files_mmap_sequential, grep_files_mmap_parallel, grep_single_file_mmap

### 4. `glob_filter.rs` (~160 LOC) — Glob + path filtering

Contains:
- Constant: GLOB_PARALLEL_THRESHOLD
- PyFunctions: glob_match_bulk, filter_paths

### 5. `cache.rs` (~510 LOC) — BloomFilter + L1MetadataCache

Contains:
- CacheMetadata struct
- PyClasses: BloomFilter, L1MetadataCache
- All #[pymethods] implementations

### 6. `tiger_cache.rs` (~180 LOC) — Roaring Bitmap integration

Contains:
- PyFunctions: filter_paths_with_tiger_cache, filter_paths_with_tiger_cache_parallel,
  intersect_paths_with_tiger_cache, any_path_accessible_tiger_cache, tiger_cache_bitmap_stats

### 7. `similarity.rs` (~380 LOC) — SIMD vector similarity

Contains:
- Constant: SIMILARITY_PARALLEL_THRESHOLD
- PyFunctions: cosine_similarity_f32, dot_product_f32, euclidean_sq_f32,
  batch_cosine_similarity_f32, top_k_similar_f32,
  cosine_similarity_i8, batch_cosine_similarity_i8, top_k_similar_i8

### 8. `blake3_hash.rs` (~60 LOC) — BLAKE3 hashing

Contains:
- PyFunctions: hash_content, hash_content_smart

### 9. `io.rs` — File I/O (read_file, read_files_bulk)

Actually, read_file and read_files_bulk don't fit cleanly into grep. Let me include them in a small `io.rs` module.

Contains:
- PyFunctions: read_file, read_files_bulk

## DRY Helper Extractions

### `parse_namespace_configs()` (replaces 4 copies)

```rust
/// Parse namespace configs from Python dict with LRU caching.
/// Replaces 4 duplicated blocks across compute_permissions_bulk,
/// compute_permission_single, expand_subjects, list_objects_for_subject.
pub(crate) fn parse_namespace_configs(
    py: Python<'_>,
    namespace_configs: &Bound<PyDict>,
) -> PyResult<AHashMap<String, NamespaceConfig>> {
    let mut namespaces = AHashMap::new();
    for (key, value) in namespace_configs.iter() {
        let obj_type: String = key.extract()?;
        let config_dict: Bound<'_, PyDict> = value.extract()?;
        let json_module = py.import("json")?;
        let config_json_py = json_module.call_method1("dumps", (config_dict,))?;
        let config_json: String = config_json_py.extract()?;

        let mut hasher = DefaultHasher::new();
        obj_type.hash(&mut hasher);
        config_json.hash(&mut hasher);
        let cache_key = hasher.finish();

        let config: NamespaceConfig = NAMESPACE_CONFIG_CACHE.with(|cache| {
            let mut cache_ref = cache.borrow_mut();
            if let Some((cached_type, cached_config)) = cache_ref.get(&cache_key) {
                if cached_type == &obj_type {
                    return Ok::<NamespaceConfig, pyo3::PyErr>(cached_config.clone());
                }
            }
            let parsed: NamespaceConfig = serde_json::from_str(&config_json).map_err(|e| {
                pyo3::exceptions::PyValueError::new_err(format!("JSON parse error: {}", e))
            })?;
            cache_ref.put(cache_key, (obj_type.clone(), parsed.clone()));
            Ok(parsed)
        })?;
        namespaces.insert(obj_type, config);
    }
    Ok(namespaces)
}
```

### `parse_tuples_from_py()` (replaces 3 copies)

```rust
/// Parse ReBAC tuples from Python list of dicts.
/// Replaces 3 duplicated blocks across compute_permission_single,
/// expand_subjects, list_objects_for_subject.
pub(crate) fn parse_tuples_from_py(tuples: &Bound<PyList>) -> PyResult<Vec<ReBACTuple>> {
    tuples
        .iter()
        .map(|item| {
            let dict: Bound<'_, PyDict> = item.extract()?;
            Ok(ReBACTuple {
                subject_type: dict
                    .get_item("subject_type")?
                    .ok_or_else(|| pyo3::exceptions::PyKeyError::new_err("missing 'subject_type'"))?
                    .extract()?,
                subject_id: dict
                    .get_item("subject_id")?
                    .ok_or_else(|| pyo3::exceptions::PyKeyError::new_err("missing 'subject_id'"))?
                    .extract()?,
                subject_relation: dict
                    .get_item("subject_relation")?
                    .and_then(|v| v.extract().ok()),
                relation: dict
                    .get_item("relation")?
                    .ok_or_else(|| pyo3::exceptions::PyKeyError::new_err("missing 'relation'"))?
                    .extract()?,
                object_type: dict
                    .get_item("object_type")?
                    .ok_or_else(|| pyo3::exceptions::PyKeyError::new_err("missing 'object_type'"))?
                    .extract()?,
                object_id: dict
                    .get_item("object_id")?
                    .ok_or_else(|| pyo3::exceptions::PyKeyError::new_err("missing 'object_id'"))?
                    .extract()?,
            })
        })
        .collect()
}
```

## Implementation Steps (TDD Order)

### Phase 0: Baseline Benchmarks

**Step 0.1**: Capture pre-split performance baseline
- Build current Rust extension: `cd rust/nexus_fast && maturin develop --release`
- Run permission benchmarks: `pytest tests/benchmarks/test_core_operations.py -k permission -v`
- Record: single check latency, bulk check latency, binary .so size
- Capture export list: `python -c "import nexus_fast; print(dir(nexus_fast))"`

### Phase 1: Create Module Files (Mechanical Move)

**Step 1.1**: Create `src/rebac.rs`
- Move lines 1-1185 + 1890-2487 (types, graphs, permission engine, expansion, listing)
- Add necessary `use` imports at top
- Make types/functions used by lib.rs `pub(crate)` or `pub`
- Extract `parse_namespace_configs()` and `parse_tuples_from_py()` helpers
- Hoist `MAX_DEPTH` to module-level `pub(crate) const REBAC_MAX_DEPTH: u32 = 50;`

**Step 1.2**: Create `src/grep.rs`
- Move lines 1281-1764 (GrepMatch, SearchMode, grep functions)
- Add necessary `use` imports
- Mark pyfunction items `pub`

**Step 1.3**: Create `src/glob_filter.rs`
- Move lines 1766-1888 (glob_match_bulk, filter_paths)
- Hoist `GLOB_PARALLEL_THRESHOLD` as module constant

**Step 1.4**: Create `src/cache.rs`
- Move lines 2489-2989 (BloomFilter, L1MetadataCache, CacheMetadata)
- Mark pyclasses `pub`

**Step 1.5**: Create `src/tiger_cache.rs`
- Move lines 2991-3150 (all tiger cache functions)

**Step 1.6**: Create `src/similarity.rs`
- Move lines 3152-3499 (all similarity functions)
- Hoist `SIMILARITY_PARALLEL_THRESHOLD` as module constant

**Step 1.7**: Create `src/blake3_hash.rs`
- Move lines 3501-3551 (hash_content, hash_content_smart)

**Step 1.8**: Create `src/io.rs`
- Move read_file (lines 2374-2427) and read_files_bulk (lines 2429-2487)

**Step 1.9**: Rewrite `src/lib.rs` as thin module hub
- `mod rebac; mod grep; mod glob_filter; mod cache; mod tiger_cache; mod similarity; mod blake3_hash; mod io;`
- `#[pymodule]` with all 25 re-exports

### Phase 2: Build + Compile Verification

**Step 2.1**: `cargo build` — fix compilation errors
- Fix missing imports, visibility issues, cross-module references
- Ensure all `use` statements are correct in each module

**Step 2.2**: `maturin develop --release` — build Python extension
- Verify the .so is built successfully

### Phase 3: Testing

**Step 3.1**: Create `tests/unit/core/test_nexus_fast_exports.py`
- Smoke test asserting all 25 exports exist
- Run: `pytest tests/unit/core/test_nexus_fast_exports.py -v`

**Step 3.2**: Create `tests/unit/core/test_nexus_fast.py`
- Direct integration tests for key functions:
  - compute_permissions_bulk with simple tuples
  - compute_permission_single
  - grep_bulk with known content
  - grep_files_mmap with temp files
  - glob_match_bulk
  - filter_paths
  - BloomFilter: add, check, clear
  - L1MetadataCache: put, get, remove, stats
  - tiger cache functions with serialized bitmaps
  - cosine_similarity_f32, batch, top_k
  - hash_content, hash_content_smart
  - read_file, read_files_bulk

**Step 3.3**: Run existing test suite
- `pytest tests/unit/ -x -v` — no regressions
- `pytest tests/benchmarks/test_core_operations.py -k permission -v` — compare with baseline

### Phase 4: E2E Validation

**Step 4.1**: Start FastAPI server with permissions enabled
- `nexus serve --enforce-permissions` (or equivalent)

**Step 4.2**: Run e2e permission tests
- `pytest tests/e2e/test_namespace_permissions_e2e.py -v`
- `pytest tests/e2e/test_async_files_permissions_e2e.py -v`
- Check server logs for any errors or performance anomalies

### Phase 5: Post-split Benchmarks

**Step 5.1**: Compare before/after numbers
- Permission check latency (should be identical within noise)
- Binary .so size (should be identical with LTO)
- Export list (should be identical)

### Phase 6: Commit

**Step 6.1**: Commit with conventional format
- `fix(#1396): split nexus_fast lib.rs (3,587 LOC) into 8 domain modules`

## Deferred to Follow-up Issues

| Issue | Description | Blocked by |
|-------|-------------|------------|
| **Unify on interned path** | Delete non-interned ReBACGraph + compute_permission (~520 LOC), unify all functions on interned path | This PR (module split) |
| **PermissionContext struct** | Bundle 8-param functions into context struct, remove #[allow(clippy::too_many_arguments)] | Interned unification |
| **visited.clone() optimization** | Replace HashSet cloning with insert/remove or thread-local pattern | Interned unification |
| **cargo test infrastructure** | Add #[cfg(test)] modules for pure-Rust logic | Extract non-PyO3 core |
| **add_direct_subjects O(n) scan** | Add reverse index to InternedGraph | Interned unification |
| **compute_permission_single graph caching** | Reuse GRAPH_CACHE from bulk path | Interned unification |
| **Python submodules** | Optional: nexus_fast.rebac.*, nexus_fast.search.* | This PR |

## Risk Assessment

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| Missing pub visibility | High | cargo build catches at compile time |
| Missing pymodule re-export | Medium | test_nexus_fast_exports.py catches immediately |
| Import path changes in Python | None | Python API unchanged (Decision #4A) |
| Performance regression | Very low | LTO + same code; before/after benchmarks verify |
| Thread-local cache duplication | Very low | GRAPH_CACHE stays in rebac.rs only |
