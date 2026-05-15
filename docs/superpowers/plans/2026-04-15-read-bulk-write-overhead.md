# read_bulk and read-before-write overhead — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Benchmark and fix 4 round-trip inefficiencies in NexusFS read/write paths (Issue #3710).

**Architecture:** Hybrid approach — add `is_external` flag in Rust `SysReadResult` to eliminate double routing (#4), fix the other 3 inefficiencies in Python. Each fix is independent and committed separately. Benchmarks written first (TDD-style) to measure before/after.

**Tech Stack:** Python (nexus_fs.py), Rust (kernel.rs, router.rs, generated_pyo3.rs), pytest-benchmark

---

## File Map

| File | Role | Tasks |
|------|------|-------|
| `tests/benchmarks/bench_read_write_overhead.py` | **Create** — 4 benchmark tests | 1 |
| `rust/kernel/src/router.rs` | **Modify** — `MountEntry.is_external`, `RustRouteResult.is_external` | 2 |
| `rust/kernel/src/kernel.rs` | **Modify** — `SysReadResult.is_external`, plumb through `add_mount`/`sys_read` | 2 |
| `rust/kernel/src/generated_pyo3.rs` | **Modify** — `PySysReadResult.is_external`, `PyRustRouteResult.is_external`, `add_mount` param | 2 |
| `src/nexus/core/nexus_fs.py` | **Modify** — all 4 Python-side fixes | 3, 4, 5, 6 |
| `src/nexus/core/mount_table.py` | **Modify** — pass `is_external` through mount registration | 3 |

---

### Task 1: Write benchmark tests

**Files:**
- Create: `tests/benchmarks/bench_read_write_overhead.py`

- [ ] **Step 1: Create benchmark file with `test_read_bulk_vs_sequential`**

```python
"""Benchmarks for read_bulk and read-before-write overhead (Issue #3710).

Run: pytest tests/benchmarks/bench_read_write_overhead.py -v --benchmark-only
"""

import time

import pytest


@pytest.mark.benchmark_file_ops
class TestReadBulkOverhead:
    """Benchmark read_bulk vs sequential reads."""

    def test_read_bulk_vs_sequential(self, benchmark, populated_nexus):
        """read_bulk(100) should be faster than 100x sys_read."""
        nx = populated_nexus
        paths = [f"/many_files/file_{i:04d}.txt" for i in range(100)]

        # Verify files exist
        for p in paths[:3]:
            assert nx.sys_read(p) is not None

        def bulk_read():
            return nx.read_bulk(paths)

        result = benchmark(bulk_read)
        # All 100 files should be returned
        assert len(result) == 100
        assert all(v is not None for v in result.values())


@pytest.mark.benchmark_file_ops
class TestReadBulkSequentialBaseline:
    """Baseline: sequential sys_read for comparison."""

    def test_sequential_read_100(self, benchmark, populated_nexus):
        """100x sys_read — baseline to compare against read_bulk."""
        nx = populated_nexus
        paths = [f"/many_files/file_{i:04d}.txt" for i in range(100)]

        def sequential_read():
            results = {}
            for p in paths:
                results[p] = nx.sys_read(p)
            return results

        result = benchmark(sequential_read)
        assert len(result) == 100
        assert all(v is not None for v in result.values())
```

- [ ] **Step 2: Add `test_write_new_vs_existing` benchmarks**

Append to the same file:

```python
@pytest.mark.benchmark_file_ops
class TestWriteNewFile:
    """Benchmark write to new path — measures metastore.get() waste."""

    def test_write_new_path(self, benchmark, benchmark_nexus):
        """Write to a path that doesn't exist yet."""
        nx = benchmark_nexus
        counter = [0]

        def write_new():
            counter[0] += 1
            return nx.write(f"/bench_new_{counter[0]}.txt", b"hello")

        result = benchmark(write_new)
        assert "etag" in result


@pytest.mark.benchmark_file_ops
class TestWriteExistingFile:
    """Benchmark write to existing path — baseline comparison."""

    def test_write_existing_path(self, benchmark, benchmark_nexus):
        """Write to a path that already exists."""
        nx = benchmark_nexus
        nx.write("/bench_existing.txt", b"initial")

        counter = [0]

        def write_existing():
            counter[0] += 1
            return nx.write("/bench_existing.txt", f"update {counter[0]}".encode())

        result = benchmark(write_existing)
        assert "etag" in result
```

- [ ] **Step 3: Add `test_read_with_metadata_vs_separate` benchmarks**

Append to the same file:

```python
@pytest.mark.benchmark_file_ops
class TestReadWithMetadata:
    """Benchmark read(return_metadata=True) — currently does read + stat."""

    def test_read_return_metadata(self, benchmark, populated_nexus):
        """read(return_metadata=True) — measures combined overhead."""
        nx = populated_nexus

        def read_with_meta():
            return nx.read("/test_small.bin", return_metadata=True)

        result = benchmark(read_with_meta)
        assert isinstance(result, dict)
        assert len(result["content"]) == 1024
        assert "etag" in result


@pytest.mark.benchmark_file_ops
class TestReadPlusStat:
    """Baseline: separate sys_read + sys_stat."""

    def test_read_plus_stat_separate(self, benchmark, populated_nexus):
        """sys_read + sys_stat separately — baseline for read(return_metadata)."""
        nx = populated_nexus

        def read_then_stat():
            content = nx.sys_read("/test_small.bin")
            meta = nx.sys_stat("/test_small.bin")
            return {"content": content, **meta}

        result = benchmark(read_then_stat)
        assert len(result["content"]) == 1024
        assert "etag" in result
```

- [ ] **Step 4: Add `test_read_route_overhead` benchmark**

Append to the same file:

```python
@pytest.mark.benchmark_file_ops
class TestRouteOverhead:
    """Benchmark Python route vs Rust route in a single read."""

    def test_python_route_time(self, benchmark, populated_nexus):
        """Measure Python router.route() cost in isolation."""
        nx = populated_nexus

        def route_only():
            return nx.router.route("/test_small.bin", is_admin=True, check_write=False)

        result = benchmark(route_only)
        assert result is not None

    def test_rust_sys_read(self, benchmark, populated_nexus):
        """Measure Rust sys_read (includes Rust-side routing)."""
        nx = populated_nexus

        def rust_read():
            return nx.sys_read("/test_small.bin")

        result = benchmark(rust_read)
        assert len(result) == 1024
```

- [ ] **Step 5: Run benchmarks to verify they pass**

Run: `pytest tests/benchmarks/bench_read_write_overhead.py -v --benchmark-disable`
Expected: All tests PASS (benchmark-disable runs them once without timing)

- [ ] **Step 6: Commit**

```bash
git add tests/benchmarks/bench_read_write_overhead.py
git commit -m "bench(#3710): add read_bulk and write overhead benchmarks

Measures 4 inefficiencies: bulk vs sequential read, new vs existing
write, read+metadata vs separate ops, Python vs Rust routing."
```

---

### Task 2: Rust — add `is_external` flag to route and read results

**Files:**
- Modify: `rust/kernel/src/router.rs:20-46` (MountEntry, RustRouteResult)
- Modify: `rust/kernel/src/router.rs:214-236` (add_mount)
- Modify: `rust/kernel/src/kernel.rs:128-140` (SysReadResult)
- Modify: `rust/kernel/src/kernel.rs:720-749` (Kernel::add_mount)
- Modify: `rust/kernel/src/kernel.rs:1401-1445` (sys_read)
- Modify: `rust/kernel/src/generated_pyo3.rs:826-832` (PySysReadResult)
- Modify: `rust/kernel/src/generated_pyo3.rs:893-912` (PyRustRouteResult)
- Modify: `rust/kernel/src/generated_pyo3.rs:1127-1303` (PyKernel::add_mount)
- Modify: `rust/kernel/src/generated_pyo3.rs:1663-1669` (sys_read conversion)
- Modify: `rust/kernel/src/generated_pyo3.rs:1932-1938` (_read_batch conversion)

- [ ] **Step 1: Add `is_external` to `MountEntry` and `RustRouteResult` in router.rs**

In `rust/kernel/src/router.rs`:

```rust
// MountEntry (line 20)
pub(crate) struct MountEntry {
    pub(crate) readonly: bool,
    pub(crate) admin_only: bool,
    pub(crate) io_profile: String,
    #[allow(dead_code)]
    pub(crate) backend_name: String,
    pub(crate) backend: Option<Box<dyn ObjectStore>>,
    pub(crate) is_external: bool,
}

// RustRouteResult (line 41)
pub struct RustRouteResult {
    pub mount_point: String,
    pub backend_path: String,
    pub readonly: bool,
    pub io_profile: String,
    pub is_external: bool,
}
```

- [ ] **Step 2: Plumb `is_external` through `add_mount` and `route_impl` in router.rs**

Update `add_mount` (line 214):

```rust
pub(crate) fn add_mount(
    &self,
    mount_point: &str,
    zone_id: &str,
    readonly: bool,
    admin_only: bool,
    io_profile: &str,
    backend_name: &str,
    backend: Option<Box<dyn ObjectStore>>,
    is_external: bool,
) -> Result<(), std::io::Error> {
    let canonical = canonicalize(mount_point, zone_id);
    self.mounts.write().insert(
        canonical,
        MountEntry {
            readonly,
            admin_only,
            io_profile: io_profile.to_string(),
            backend_name: backend_name.to_string(),
            backend,
            is_external,
        },
    );
    Ok(())
}
```

Update `route_impl` to include `is_external` in the result. Find the return site where `RustRouteResult` is constructed and add `is_external: entry.is_external`. There are multiple return paths — find them:

```bash
grep -n "RustRouteResult" rust/kernel/src/router.rs
```

Each `RustRouteResult { ... }` construction must include `is_external: entry.is_external`.

- [ ] **Step 3: Add `is_external` to `SysReadResult` in kernel.rs**

In `rust/kernel/src/kernel.rs` (line 128):

```rust
pub struct SysReadResult {
    pub hit: bool,
    pub data: Option<Vec<u8>>,
    pub post_hook_needed: bool,
    pub content_hash: Option<String>,
    pub entry_type: u8,
    pub is_external: bool,
}
```

Update the `miss()` closure in `sys_read` (line 1406) to include `is_external: false`.

- [ ] **Step 4: Plumb `is_external` through `Kernel::add_mount` in kernel.rs**

Update `Kernel::add_mount` (line 720) to accept and forward `is_external`:

```rust
pub fn add_mount(
    &self,
    mount_point: &str,
    zone_id: &str,
    readonly: bool,
    admin_only: bool,
    io_profile: &str,
    backend_name: &str,
    backend: Option<Box<dyn crate::backend::ObjectStore>>,
    metastore_path: Option<&str>,
    is_external: bool,
) -> Result<(), KernelError> {
    if let Some(ms_path) = metastore_path {
        let ms = RedbMetastore::open(std::path::Path::new(ms_path))
            .map_err(|e| KernelError::IOError(format!("RedbMetastore: {e:?}")))?;
        let canonical = canonicalize(mount_point, zone_id);
        self.mount_metastores.insert(canonical, Box::new(ms));
    }
    self.router
        .add_mount(
            mount_point,
            zone_id,
            readonly,
            admin_only,
            io_profile,
            backend_name,
            backend,
            is_external,
        )
        .map_err(KernelError::from)
}
```

- [ ] **Step 5: Return `is_external` from `sys_read` when route is external**

In `sys_read` (line 1438), after routing succeeds, check `route.is_external`:

```rust
// 2. Route (pure Rust LPM)
let route = match self
    .router
    .route_impl(path, &ctx.zone_id, ctx.is_admin, false)
{
    Ok(r) => r,
    Err(_) => return miss(),
};

// 2b. External mount — signal Python to handle via connector backend
if route.is_external {
    return Ok(SysReadResult {
        hit: false,
        data: None,
        post_hook_needed: false,
        content_hash: None,
        entry_type: 0,
        is_external: true,
    });
}
```

Also update the successful read return path (where `hit: true`) to include `is_external: false`.

Find all `SysReadResult { ... }` constructions in kernel.rs and add `is_external: false` (or `true` for the external early-return above). Search with:

```bash
grep -n "SysReadResult" rust/kernel/src/kernel.rs
```

- [ ] **Step 6: Update PyO3 bindings — `PySysReadResult`, `PyRustRouteResult`, `add_mount`**

In `rust/kernel/src/generated_pyo3.rs`:

Update `PySysReadResult` (line 826):
```rust
#[pyclass(name = "SysReadResult", get_all)]
pub struct PySysReadResult {
    pub hit: bool,
    pub data: Option<Py<PyBytes>>,
    pub post_hook_needed: bool,
    pub content_hash: Option<String>,
    pub entry_type: u8,
    pub is_external: bool,
}
```

Update `PyRustRouteResult` (line 893):
```rust
#[pyclass(name = "RustRouteResult")]
pub struct PyRustRouteResult {
    #[pyo3(get)]
    pub mount_point: String,
    #[pyo3(get)]
    pub backend_path: String,
    #[pyo3(get)]
    pub readonly: bool,
    #[pyo3(get)]
    pub io_profile: String,
    #[pyo3(get)]
    pub is_external: bool,
}
```

Update the `From<RustRouteResult>` impl (line 904):
```rust
impl From<RustRouteResult> for PyRustRouteResult {
    fn from(r: RustRouteResult) -> Self {
        Self {
            mount_point: r.mount_point,
            backend_path: r.backend_path,
            readonly: r.readonly,
            io_profile: r.io_profile,
            is_external: r.is_external,
        }
    }
}
```

Add `is_external: bool` parameter to `PyKernel::add_mount` (line 1127). Add it after `metastore_path`:
```rust
    metastore_path: Option<&str>,
    is_external: bool,
```

Forward it in the `self.inner.add_mount(...)` call (line 1291):
```rust
self.inner
    .add_mount(
        mount_point,
        zone_id,
        readonly,
        admin_only,
        io_profile,
        backend_name,
        backend,
        metastore_path,
        is_external,
    )
    .map_err(Into::into)
```

Update all `PySysReadResult { ... }` constructions to include `is_external: result.is_external`:
- Line 1663 (sys_read): add `is_external: result.is_external,`
- Line 1932 (_read_batch): add `is_external: r.is_external,`

- [ ] **Step 7: Fix router.rs tests**

Update test constructions in `router.rs` that call `add_mount` — add `false` for the new `is_external` param. Search:

```bash
grep -n "add_mount" rust/kernel/src/router.rs
```

- [ ] **Step 8: Build and test Rust**

Run: `cd rust/kernel && cargo build 2>&1 | tail -20`
Expected: Build succeeds

Run: `cd rust/kernel && cargo test 2>&1 | tail -20`
Expected: All tests pass

- [ ] **Step 9: Commit**

```bash
git add rust/kernel/src/router.rs rust/kernel/src/kernel.rs rust/kernel/src/generated_pyo3.rs
git commit -m "feat(#3710): add is_external flag to SysReadResult and route

Rust router and kernel now propagate is_external through add_mount,
route_impl, and sys_read. External mounts return early from sys_read
with is_external=true, signaling Python to handle via connector."
```

---

### Task 3: Fix #4 — Eliminate double routing in `sys_read` + wire `is_external` through mount registration

**Files:**
- Modify: `src/nexus/core/nexus_fs.py:1388-1450` (sys_read routing restructure)
- Modify: `src/nexus/core/mount_table.py:157-168,207-235` (add is_external param)

- [ ] **Step 1: Update mount_table.py to pass `is_external` to Kernel.add_mount**

In `src/nexus/core/mount_table.py`, the `_sync_to_kernel` method (line 157) and `add` method (line 224) call `kernel.add_mount(...)`. We need to detect external mounts and pass `is_external=True`.

External mounts are identified by metadata `entry_type == 5` on the mount point. But at mount registration time, the metadata may not exist yet. Instead, detect via the backend: external storage backends are registered via `ExternalRouteResult` which is detected in the router by checking `_route_meta.is_external_storage`. The mount table doesn't know this — so we pass a new `is_external` kwarg through the `add()` method and the `MountEntry`.

Update `MountEntry` — find its definition:

```bash
grep -n "class MountEntry" src/nexus/core/mount_table.py
```

Add `is_external: bool = False` field to MountEntry.

Update `add()` method to accept and store `is_external`:

```python
def add(
    self,
    mount_point: str,
    backend: "ObjectStoreABC",
    *,
    metastore: "MetastoreABC | None" = None,
    readonly: bool = False,
    admin_only: bool = False,
    io_profile: str = "balanced",
    stream_backend_factory: Any = None,
    zone_id: str = ROOT_ZONE_ID,
    is_external: bool = False,
) -> None:
```

Pass `is_external=is_external` in the `MountEntry(...)` constructor.

Pass `is_external=entry.is_external` (or `is_external=is_external`) in both `kernel.add_mount(...)` calls (lines 157 and 224):

```python
kernel.add_mount(
    mount_point,
    zone_id,
    entry.readonly,
    entry.admin_only,
    entry.io_profile,
    _backend_name,
    _local_root,
    True,
    py_backend=backend,
    metastore_path=str(_ms_path) if _ms_path else None,
    is_external=entry.is_external,
)
```

Also update `_sync_to_kernel` similarly, reading `entry.is_external` from the stored MountEntry.

- [ ] **Step 2: Update callers that register external mounts**

The main entry point is `DriverLifecycleCoordinator.mount()` at `src/nexus/core/driver_lifecycle_coordinator.py:115-135`. Add `is_external: bool = False` parameter and forward it to `self._mount_table.add(...)`:

```python
def mount(
    self,
    mount_point: str,
    backend: "ObjectStoreABC",
    *,
    metastore: "MetastoreABC | None" = None,
    readonly: bool = False,
    admin_only: bool = False,
    io_profile: str = "balanced",
    is_external: bool = False,
) -> None:
    """Mount a backend with full lifecycle: routing + hooks + notification."""
    self._mount_table.add(
        mount_point,
        backend,
        metastore=metastore,
        readonly=readonly,
        admin_only=admin_only,
        io_profile=io_profile,
        is_external=is_external,
    )
```

Then find callers that mount external/connector backends and pass `is_external=True`. Search:

```bash
grep -rn "coordinator.*mount\|\.mount(" src/nexus/bricks/mount/mount_service.py | head -20
```

At each call site where an external connector backend is mounted (identified by the backend being an external connector, or the metadata `entry_type=5`), add `is_external=True`.

Also update `MountRestoreDTO` in `src/nexus/bricks/mount/mount_manager.py:28-39` to include `is_external: bool = False` if mount restoration needs to preserve this flag.

- [ ] **Step 3: Restructure `sys_read` to use Rust-first routing**

In `src/nexus/core/nexus_fs.py`, rewrite the routing section of `sys_read` (lines 1388-1470). The key change: call Rust `sys_read` first, only fall back to Python `router.route()` when `result.is_external` is true.

Current flow (lines 1388-1470):
```
1. Python router.route() → check ExternalRouteResult
2. If external → handle connector read, return
3. Rust kernel.sys_read() → normal read
```

New flow:
```
1. Rust kernel.sys_read()
2. If result.is_external → Python router.route() → handle connector read, return
3. If result.hit → return data
4. Else → fallback (pipe, virtual readme, etc.)
```

Replace lines 1388-1470 with:

```python
        path = self._validate_path(path)
        context = self._parse_context(context)
        _handled, _resolve_hint = self.resolve_read(path, context=context)
        if _handled:
            content = _resolve_hint or b""
            if offset or count is not None:
                content = (
                    content[offset : offset + count] if count is not None else content[offset:]
                )
            return content

        _is_admin = (
            getattr(context, "is_admin", False)
            if context is not None and not isinstance(context, dict)
            else (context.get("is_admin", False) if isinstance(context, dict) else False)
        )

        # PRE-INTERCEPT hooks dispatched by Rust sys_read (dispatch_pre_hooks)

        # ── KERNEL (Rust — pre-hooks + route + backend read) ──
        _rust_ctx = self._build_rust_ctx(context, _is_admin)
        result = self._kernel.sys_read(path, _rust_ctx)

        # External mount — Rust detected is_external, delegate to Python connector
        if result.is_external:
            from nexus.core.router import ExternalRouteResult

            _route = self.router.route(
                path, is_admin=_is_admin, check_write=False, zone_id=self._zone_id
            )
            if isinstance(_route, ExternalRouteResult) and _route.backend is not None:
                _route_backend_path = getattr(_route, "backend_path", "") or ""
                _route_mount_point = getattr(_route, "mount_point", "") or ""
                _ctx = (
                    _dc_replace(
                        context,
                        backend_path=_route_backend_path,
                        virtual_path=path,
                        mount_path=_route_mount_point,
                    )
                    if context
                    else OperationContext(
                        user_id="anonymous",
                        groups=[],
                        backend_path=_route_backend_path,
                        virtual_path=path,
                        mount_path=_route_mount_point,
                    )
                )
                # Virtual .readme/ overlay check (Issue #3728)
                from nexus.backends.connectors.schema_generator import (
                    dispatch_virtual_readme_read,
                )
                _virtual_data = dispatch_virtual_readme_read(
                    _route.backend,
                    _route_mount_point,
                    _route_backend_path,
                    context=_ctx,
                )
                if _virtual_data is not None:
                    data = _virtual_data
                else:
                    data = _route.backend.read_content(_route_backend_path, context=_ctx)
                if offset or count is not None:
                    data = data[offset : offset + count] if count is not None else data[offset:]
                return data
```

Keep the existing post-`sys_read` handling for pipes, streams, virtual readme fallback, etc. — just move it after the external check. The DT_PIPE/DT_STREAM handling (checking `result.entry_type`) stays as-is after the external block.

- [ ] **Step 4: Run tests**

Run: `pytest tests/ -x -q --timeout=60 -k "read" 2>&1 | tail -30`
Expected: All read-related tests pass

Run: `pytest tests/benchmarks/bench_read_write_overhead.py -v --benchmark-disable`
Expected: All benchmarks pass

- [ ] **Step 5: Commit**

```bash
git add src/nexus/core/nexus_fs.py src/nexus/core/mount_table.py
git commit -m "perf(#3710): eliminate double routing in sys_read

Rust sys_read now returns is_external flag. Python only calls
router.route() for external mounts. Standard reads skip the
redundant Python route entirely."
```

---

### Task 4: Fix #1 — Batch permission hooks in `read_bulk`

**Files:**
- Modify: `src/nexus/core/nexus_fs.py:1608-1638` (read_bulk permission loop)

- [ ] **Step 1: Replace per-path hook loop with batch-aware dispatch**

In `src/nexus/core/nexus_fs.py`, replace the permission check loop in `read_bulk` (lines 1608-1638):

Current code:
```python
        perm_start = time.time()
        allowed_set: set[str]
        try:
            from nexus.contracts.exceptions import PermissionDeniedError
            from nexus.contracts.types import OperationContext
            from nexus.contracts.vfs_hooks import StatHookContext as _SHC

            ctx = self._resolve_cred(context)
            assert isinstance(ctx, OperationContext), "Context must be OperationContext"
            allowed: list[str] = []
            for p in validated_paths:
                try:
                    self._kernel.dispatch_pre_hooks(
                        "stat", _SHC(path=p, context=ctx, permission="READ")
                    )
                    allowed.append(p)
                except PermissionDeniedError:
                    pass
            allowed_set = set(allowed)
        except Exception as e:
            logger.error("[READ-BULK] Permission check failed: %s", e)
            if not skip_errors:
                raise
                # If skip_errors, assume no files are allowed
                allowed_set = set()
```

New code:
```python
        perm_start = time.time()
        allowed_set: set[str]
        try:
            ctx = self._resolve_cred(context)

            # Fast path: no stat hooks registered → all paths allowed
            if self._kernel.hook_count("stat") == 0:
                allowed_set = set(validated_paths)
            else:
                from nexus.contracts.exceptions import PermissionDeniedError
                from nexus.contracts.types import OperationContext
                from nexus.contracts.vfs_hooks import StatHookContext as _SHC

                assert isinstance(ctx, OperationContext), "Context must be OperationContext"
                allowed: list[str] = []
                for p in validated_paths:
                    try:
                        self._kernel.dispatch_pre_hooks(
                            "stat", _SHC(path=p, context=ctx, permission="READ")
                        )
                        allowed.append(p)
                    except PermissionDeniedError:
                        pass
                allowed_set = set(allowed)
        except Exception as e:
            logger.error("[READ-BULK] Permission check failed: %s", e)
            if not skip_errors:
                raise
            # If skip_errors, assume no files are allowed
            allowed_set = set()
```

Note: also fix the unreachable code bug on the original line `allowed_set = set()` — it was after a `raise` and could never execute. The new code moves it to the `except` block directly.

- [ ] **Step 2: Run tests**

Run: `pytest tests/ -x -q --timeout=60 -k "bulk or read_bulk" 2>&1 | tail -20`
Expected: All bulk read tests pass

Run: `pytest tests/benchmarks/bench_read_write_overhead.py::TestReadBulkOverhead -v --benchmark-disable`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add src/nexus/core/nexus_fs.py
git commit -m "perf(#3710): skip per-path hook loop in read_bulk when no stat hooks

When no stat hooks are registered (hook_count('stat') == 0), read_bulk
skips the per-path dispatch_pre_hooks loop entirely. Also fixes
unreachable code in the error handler."
```

---

### Task 5: Fix #2 — Lazy metadata fetch on write

**Files:**
- Modify: `src/nexus/core/nexus_fs.py:2438-2454` (_write_content metadata fetch)

- [ ] **Step 1: Make metadata fetch conditional on write hooks**

In `src/nexus/core/nexus_fs.py`, replace the metadata fetch in `_write_content` (lines 2438-2454):

Current code:
```python
        # Get existing metadata for permission check and update detection (single query)
        now = datetime.now(UTC)
        meta = _meta if _meta is not None else route.metastore.get(path)

        # PRE-INTERCEPT: pre-write hooks (Issue #899)
        # Hook handles existing-file (owner fast-path) vs new-file (parent check)
        from nexus.contracts.vfs_hooks import WriteHookContext as _WHC

        self._kernel.dispatch_pre_hooks(
            "write",
            _WHC(
                path=path,
                content=content,
                context=context,
                old_metadata=meta,
            ),
        )
```

New code:
```python
        # Get existing metadata — lazy when no write hooks (avoids wasted metastore query on new files)
        now = datetime.now(UTC)
        if _meta is not None:
            meta = _meta
        elif self._kernel.hook_count("write") > 0:
            # Hooks need old_metadata for permission check (owner fast-path vs parent check)
            meta = route.metastore.get(path)
        else:
            meta = None

        # PRE-INTERCEPT: pre-write hooks (Issue #899)
        from nexus.contracts.vfs_hooks import WriteHookContext as _WHC

        self._kernel.dispatch_pre_hooks(
            "write",
            _WHC(
                path=path,
                content=content,
                context=context,
                old_metadata=meta,
            ),
        )
```

This preserves all existing behavior when hooks are registered. When no write hooks exist, `dispatch_pre_hooks` is a no-op (the Rust side checks `has_hooks("write")` and returns early), and `meta=None` flows correctly through `_build_write_metadata` (which already handles `None` — generates version=1, new etag).

- [ ] **Step 2: Run tests**

Run: `pytest tests/ -x -q --timeout=60 -k "write" 2>&1 | tail -20`
Expected: All write tests pass

Run: `pytest tests/benchmarks/bench_read_write_overhead.py::TestWriteNewFile -v --benchmark-disable`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add src/nexus/core/nexus_fs.py
git commit -m "perf(#3710): skip metastore.get() on write when no write hooks

When no write hooks are registered, _write_content skips the
pre-write metastore.get(path) query. New files no longer pay for
a lookup that always returns None."
```

---

### Task 6: Fix #3 — Direct metastore lookup in `read(return_metadata=True)`

**Files:**
- Modify: `src/nexus/core/nexus_fs.py:2288-2305` (read method)

- [ ] **Step 1: Replace sys_stat with direct metastore lookup**

In `src/nexus/core/nexus_fs.py`, replace lines 2288-2305:

Current code:
```python
        content = self.sys_read(path, count=count, offset=offset, context=context)

        if not return_metadata:
            return content

        # Compose with sys_stat for metadata
        meta_dict = self.sys_stat(path, context=context)
        result: dict[str, Any] = {"content": content}
        if meta_dict:
            result.update(
                {
                    "etag": meta_dict.get("etag"),
                    "version": meta_dict.get("version"),
                    "modified_at": meta_dict.get("modified_at"),
                    "size": len(content),
                }
            )
        return result
```

New code:
```python
        content = self.sys_read(path, count=count, offset=offset, context=context)

        if not return_metadata:
            return content

        # Direct metastore lookup — sys_read already validated, routed, and checked permissions.
        # Avoids redundant sys_stat pipeline (validate → route → hooks → metastore).
        # Same pattern as read_bulk (lines 1567-1574, 1679-1688).
        meta = self.metadata.get(self._validate_path(path))
        result: dict[str, Any] = {"content": content}
        if meta:
            result.update(
                {
                    "etag": meta.etag,
                    "version": meta.version,
                    "modified_at": meta.modified_at.isoformat() if meta.modified_at else None,
                    "size": len(content),
                }
            )
        return result
```

Note: `meta.modified_at` is a `datetime` object from the metastore, but `sys_stat` returns it as an ISO string. Match the `sys_stat` format by calling `.isoformat()`. Check callers of `read(return_metadata=True)` — they access `result["modified_at"]` but only for display or storage, not for datetime operations, so string format is correct.

- [ ] **Step 2: Run tests**

Run: `pytest tests/ -x -q --timeout=60 -k "read" 2>&1 | tail -20`
Expected: All read tests pass

Run: `pytest tests/benchmarks/bench_read_write_overhead.py::TestReadWithMetadata -v --benchmark-disable`
Expected: PASS

- [ ] **Step 3: Run full test suite**

Run: `pytest tests/ -x -q --timeout=120 2>&1 | tail -30`
Expected: All tests pass — no regressions from any of the 4 fixes

- [ ] **Step 4: Commit**

```bash
git add src/nexus/core/nexus_fs.py
git commit -m "perf(#3710): use direct metastore lookup in read(return_metadata)

Replace redundant sys_stat call with metadata.get() after sys_read.
Eliminates duplicate validation, routing, and hook dispatch when
reading file content with metadata."
```

---

### Task 7: Final verification — run benchmarks with timing

**Files:** None (verification only)

- [ ] **Step 1: Run benchmarks with timing**

Run: `pytest tests/benchmarks/bench_read_write_overhead.py -v --benchmark-only --benchmark-group-by=group 2>&1 | tail -50`
Expected: All benchmarks complete with timing data. Document the results.

- [ ] **Step 2: Run full test suite for regression check**

Run: `pytest tests/ -x -q --timeout=120 2>&1 | tail -30`
Expected: All tests pass

- [ ] **Step 3: Run mypy type check**

Run: `mypy src/nexus/core/nexus_fs.py src/nexus/core/mount_table.py --no-error-summary 2>&1 | tail -20`
Expected: No new type errors
