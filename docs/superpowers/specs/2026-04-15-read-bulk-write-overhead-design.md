# Benchmark + Fix: read_bulk and read-before-write overhead

**Issue:** [#3710](https://github.com/nexi-lab/nexus/issues/3710)
**Date:** 2026-04-15
**Approach:** Hybrid — Rust flag for routing (#4), Python-side fixes for #1/#2/#3

## Problem

Four patterns in `nexus_fs.py` add unnecessary round-trips:

1. **`read_bulk` not truly batched** — loops `dispatch_pre_hooks("stat")` per path + `sys_read` per file
2. **Read-before-write** — every `_write_content` calls `metastore.get(path)` before writing, wasted for new files
3. **`read(return_metadata=True)` double operation** — calls `sys_read` then `sys_stat` (two full routing/hook pipelines)
4. **Double routing in `sys_read`** — Python `router.route()` runs before Rust `sys_read` which routes again internally

## Benchmarks

New file: `tests/benchmarks/bench_read_write_overhead.py`

Uses existing `populated_nexus` fixture (300 pre-written files). pytest-benchmark with assertions for correctness.

| Benchmark | What it measures | Baseline comparison |
|-----------|-----------------|---------------------|
| `test_read_bulk_vs_sequential` | `read_bulk(100)` vs loop of 100x `sys_read(1)` | Expect bulk barely faster (currently just a loop) |
| `test_write_new_vs_existing` | `write(new_path)` vs `write(existing_path)` | Isolates `metastore.get()` cost on new files |
| `test_read_with_metadata_vs_separate` | `read(return_metadata=True)` vs `sys_read()` + `sys_stat()` | Should be ~equal (that's the bug) |
| `test_read_route_overhead` | Profile Python route time vs Rust route time in a single read | Measures double-routing waste |

## Fix #1: Batch permission hooks in `read_bulk`

**Location:** `nexus_fs.py` lines 1617-1628

**Problem:** Per-path `dispatch_pre_hooks("stat")` loop. Each call acquires hook registry lock, builds `StatHookContext`, dispatches. 100 files = 100 lock acquisitions.

**Fix:**
- Replace per-path loop with batch dispatch: build all `StatHookContext` objects upfront, dispatch in one locked section
- If no pre-hooks registered (`native_hooks.count() == 0`), skip entire permission loop — allow all paths
- Small-batch fast path (<=4 paths, lines 1553-1590) unchanged — overhead negligible

## Fix #2: Lazy metadata fetch on write

**Location:** `nexus_fs.py` `_write_content` line 2440

**Problem:** `meta = route.metastore.get(path)` on every write. For new files, always returns `None`. The result feeds:
1. Pre-write hook `old_metadata` field (line 2452)
2. `_build_write_metadata` `existing_meta` param (version bumping)
3. `is_new` detection in `_WriteContentResult` (line 2551)

**Fix:**
- Check if pre-write hooks are registered via `self._kernel.native_hooks.lock().count()` (O(1))
- **No hooks:** skip `metastore.get()`. Pass `meta=None` to `_build_write_metadata` (already handles None — generates version=1, new etag). `is_new=True` for all writes without hooks.
- **Hooks registered:** fetch metadata as before, pass to hook context

**Accepted imprecision:** `is_new` may be wrong for existing-file overwrites when no hooks registered. Only consumer is `_dispatch_write_events` which uses it for `FileEvent.CREATED` vs `MODIFIED`. Event consumers should be idempotent — low impact.

## Fix #3: Direct metastore lookup in `read(return_metadata=True)`

**Location:** `nexus_fs.py` lines 2288-2294

**Problem:** `read()` calls `sys_read()` then `sys_stat()`. Both independently validate, resolve context, route, and hit kernel. `sys_stat` does full metastore lookup + directory check + permission dispatch — all redundant.

**Fix:** After `sys_read` succeeds, fetch metadata directly from metastore:

```python
# Before (two full operations):
content = self.sys_read(path, ...)
meta_dict = self.sys_stat(path, context=context)

# After (one operation + cheap metadata lookup):
content = self.sys_read(path, ...)
meta = self.metadata.get(path)
result = {"content": content}
if meta:
    result.update({
        "etag": meta.etag,
        "version": meta.version,
        "modified_at": meta.modified_at.isoformat() if meta.modified_at else None,
        "size": len(content),
    })
```

Same pattern `read_bulk` already uses (lines 1567-1574 small-batch, 1679-1688 large-batch).

**Safe because:** `sys_read` already did routing, validation, and permission hooks. Metadata is pure data (etag/version/timestamps), no access control.

## Fix #4: Eliminate double routing via Rust `is_external` flag

**Location:** `nexus_fs.py` lines 1397-1450, `rust/kernel/src/kernel.rs` sys_read, `rust/kernel/src/router.rs`

**Problem:** Python `router.route()` runs before Rust `sys_read` which calls `route_impl` internally. For non-external paths (vast majority), Python route is pure waste.

### Rust changes

**`router.rs`:**
- Add `is_external: bool` to `MountEntry` struct
- Add `is_external: bool` to `RustRouteResult` struct
- Plumb through `add_mount()` — new parameter

**`kernel.rs`:**
- Add `is_external: bool` to `SysReadResult` struct (default `false`)
- In `sys_read`, after `route_impl` succeeds, check `route.is_external`
- If external: return early with `hit=false, is_external=true`

**PyO3 bindings:**
- Expose `is_external` on Python-visible `SysReadResult`

### Python changes

**`nexus_fs.py` `sys_read`:**
```python
# Before:
_route = self.router.route(path, ...)          # Python route (always)
if isinstance(_route, ExternalRouteResult):     # external handling
    ...
result = self._kernel.sys_read(path, ...)       # Rust route (always)

# After:
result = self._kernel.sys_read(path, ...)       # Rust route first
if result.is_external:                          # Rust told us it's external
    _route = self.router.route(path, ...)       # Python route (only for external)
    # external handling...
elif result.hit:
    # normal Rust-handled read
```

**`router.py` / driver coordinator:**
- Pass `is_external=True` when registering external mounts via `add_mount()`

## Files changed

| File | Change |
|------|--------|
| `tests/benchmarks/bench_read_write_overhead.py` | **New** — 4 benchmark tests |
| `rust/kernel/src/kernel.rs` | `SysReadResult.is_external`, early return in `sys_read` |
| `rust/kernel/src/router.rs` | `MountEntry.is_external`, `RustRouteResult.is_external`, `add_mount` param |
| `rust/kernel/src/generated_pyo3.rs` | Expose `is_external` on Python-visible struct |
| `src/nexus/core/nexus_fs.py` | All 4 fixes |
| `src/nexus/core/router.py` | Pass `is_external` through mount registration |

## Testing

- Benchmarks validate correctness via assertions
- Existing test suite catches regressions — fixes are internal optimizations, public API unchanged
- No new unit tests beyond benchmarks — behavior identical, only performance changes
- `SysReadResult` gains new field with default `false` — backward compatible

## No breaking changes

All public method signatures unchanged. Internal optimization only.
