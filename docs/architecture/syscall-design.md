# Syscall Design

**Status:** Implemented. Source of truth for syscall inventory and design rationale.
**See also:** `KERNEL-ARCHITECTURE.md` §2 for the kernel-level view.

---

## 1. Architecture: Linux kernel + libc Pattern

Two layers, mirroring Linux kernel + glibc:

```
Linux:   Application → libc read()  → syscall(NR_read) → kernel sys_read()
Nexus:   Client      → nx.read()    →                  → NexusFS.sys_read()
                        ↑ Tier 2 (contracts/)               ↑ Tier 1 (core/)
                        No sys_ prefix                       sys_ prefix
                        Composes primitives                  Atomic primitives
```

- **Tier 1 (kernel)**: Abstract `sys_*` methods on `NexusFilesystem`. Implemented by `NexusFS`.
  All POSIX-aligned, path-addressed. No hash-addressing at kernel level.
- **Tier 2 (convenience)**: Concrete methods on `NexusFilesystem`. Compose Tier 1 syscalls.
  Half POSIX VFS-aligned, half HDFS/GFS-aligned (content access via driver).

---

## 2. Kernel Syscall Table

All path-addressed. No hash-addressing (CAS is driver detail, not kernel concern).

### Tier 1 — Abstract Syscalls (11)

| # | Plane | Syscall | Signature | POSIX Ref |
|---|-------|---------|-----------|-----------|
| 1 | Content | `sys_read` | `(path, count=None, offset=0) → bytes` | `pread(2)` |
| 2 | Content | `sys_write` | `(path, buf, count=None, offset=0) → dict` | `write(2)` |
| 3 | Metadata | `sys_stat` | `(path, include_lock=False) → dict \| None` | `stat(2)` — include_lock=True appends advisory lock state (zero cost when False) |
| 4 | Metadata | `sys_setattr` | `(path, **attrs) → dict` | `chmod/chown/utimes` + `mknod` (DT_DIR, DT_PIPE, DT_STREAM, DT_MOUNT) |
| 5 | Namespace | `sys_unlink` | `(path, recursive=False) → dict` | `unlink(2)` |
| 6 | Namespace | `sys_rename` | `(old, new) → dict` | `rename(2)` |
| 7 | Namespace | `sys_copy` | `(src, dst) → dict` | — (server-side copy, Issue #3329) |
| 8 | Directory | `sys_readdir` | `(path, recursive=True, limit=None) → list` | `readdir(3)` — `/__sys__/locks/` returns active locks (like `/proc/locks`) |
| 9 | Locking | `sys_lock` | `(path, mode, ttl, max_holders, lock_id=None) → str \| None` | `fcntl(F_SETLK)` — acquire (lock_id=None) or extend TTL (lock_id=existing) |
| 10 | Locking | `sys_unlock` | `(path, lock_id=None, force=False) → bool` | `flock(LOCK_UN)` — release by lock_id, or force-release all holders |
| 11 | Watch | `sys_watch` | `(path, timeout, recursive) → dict \| None` | `inotify(7)` |

### Tier 2 — Concrete Convenience (not abstract, composing Tier 1)

| Method | Tier | Composes | Notes |
|--------|------|----------|-------|
| `rmdir` | 2 | `sys_unlink(recursive=)` | Thin delegation, overridable |
| `access` | 2 | `sys_stat` | Returns `True` if stat succeeds |
| `is_directory` | 2 | `sys_stat` | Checks `is_directory` field |

`sys_setattr` is the universal creation/management syscall:
- `mkdir(path)` = `sys_setattr(path, entry_type=DT_DIR)` (Tier 2)
- `mount` = `sys_setattr(path, entry_type=DT_MOUNT, backend=...)`
- `mkpipe` = `sys_setattr(path, entry_type=DT_PIPE)`
- `mkstream` = `sys_setattr(path, entry_type=DT_STREAM)`
- `/__sys__/` paths = kernel management (service register/unregister)

### What's NOT a kernel syscall

Hash-addressed content operations (`read_content`, `write_content`, `stream`,
`write_stream`) stay on **ObjectStoreABC** (driver level):

- Hash-addressing implies CAS, but not all backends use CAS. Kernel is backend-agnostic.
- Linux doesn't expose `sys_read_block(lba)` — that's the block device driver's concern.
- HDFS separates: ClientProtocol (path-based, NameNode) vs DataTransferProtocol
  (block-based, DataNode). Our ObjectStoreABC = DataNode equivalent.

---

## 3. Convenience Layer (NexusFilesystem Tier 2)

Defined in `contracts/filesystem/filesystem_abc.py` as concrete methods.
NexusFS inherits them — callers use `nx.read(path)` directly.

### VFS Half — POSIX-aligned

| Method | Composes | Behavior |
|--------|----------|----------|
| `read(path, count, offset)` | `sys_stat` + `sys_read` | POSIX pread semantics |
| `write(path, buf, consistency=)` | `sys_write` + `sys_setattr` | Write + metadata update, consistency param |
| `mkdir(path, parents, exist_ok)` | `sys_setattr(entry_type=DT_DIR)` | Directory creation with hooks + events |
| `rmdir(path, recursive)` | `rmdir` | Lenient defaults (recursive=True) |
| `append(path, content)` | `read` + `write` | Shell `>>` semantics |
| `edit(path, edits)` | `read` + transform + `write` | Apply diffs |
| `write_batch(files)` | N × `write()` | Batch file writes |
| `access(path)` | `sys_stat` | Existence check |
| `is_directory(path)` | `sys_stat` | Directory check |
| `lock_acquire(path, mode, ttl)` | `sys_lock` | Dict wrapper for gRPC Call RPC (sys_lock returns raw str) |
| `lock(path, mode, timeout)` | `sys_lock` (retry loop) | Blocking lock (like `fcntl(F_SETLKW)`) |
| `unlock(lock_id, path)` | `sys_unlock` | Release lock |
| `locked(path)` | `lock` + `unlock` | Async context manager |

### HDFS Half — Driver-level content access

| Method | Delegates to | Purpose |
|--------|-------------|---------|
| `read_content(hash)` | `ObjectStoreABC.read_content(hash)` | Direct blob access by hash |
| `write_content(content)` | `ObjectStoreABC.write_content(content)` | Direct blob store, return hash |
| `stream(hash)` | `ObjectStoreABC.stream(hash)` | Streaming blob read |
| `write_stream(path)` | `ObjectStoreABC.write_stream(path)` | Streaming blob write |

### Higher-level

| Method | Composes |
|--------|----------|
| `glob(pattern)` | `sys_readdir` + filter |
| `grep(pattern, path)` | `sys_readdir` + `sys_read` + regex |

---

## 4. Key Design Decisions

### 4.1 sys_read / sys_write: Content-only (POSIX pread/pwrite)

`sys_write` is content-only (SRP). Metadata updates are handled by `sys_setattr`
or Tier 2 `write()`. File must exist — `sys_write` to a non-existent path raises
`NexusFileNotFoundError`. Creation goes through `sys_setattr`.

CAS read-modify-write for offset writes is handled internally by the driver.
Kernel does not know whether backend is CAS or path-addressed.

### 4.2 sys_unlink: Unified delete (files + directories)

`sys_unlink` handles both files and directories (with `recursive=` param).
`rmdir` is Tier 2 convenience that delegates to `sys_unlink(recursive=)`.
CAS content is freed when refcount reaches zero.

### 4.3 sys_setattr: Universal creation/management

`sys_setattr` is the Swiss Army knife — creation, attribute updates, and special
inode types all flow through it:

- **Create**: `entry_type=DT_DIR/DT_PIPE/DT_STREAM/DT_MOUNT` creates the inode
- **Update**: No `entry_type` updates mutable metadata fields
- **Idempotent open**: Same `entry_type` on existing path recovers the buffer (pipes/streams)
- **`/__sys__/`**: Kernel management namespace (service register, config, etc.)

### 4.4 sys_lock / sys_unlock: Advisory locks (POSIX fcntl)

Exposed as kernel syscalls (not service-layer). Two syscalls cover all lock
operations (POSIX `fcntl(F_SETLK)` pattern — same syscall for acquire and extend):

- `sys_lock(path, lock_id=None)` — acquire (lock_id=None) or extend TTL (lock_id=existing)
- `sys_unlock(path, lock_id=None, force=False)` — release by lock_id or force-release all holders

Lock state query via existing syscalls (no dedicated lock-query syscall):
- `sys_stat(path, include_lock=True)` — appends lock info to stat result (zero cost when False)
- `sys_readdir("/__sys__/locks/")` — list all active locks (virtual namespace, like `/proc/locks`)

Tier 2: `lock_acquire()` wraps sys_lock with dict return for gRPC; `lock()`
provides blocking retry (`F_SETLKW`); `locked()` provides async context manager.
See `lock-architecture.md` §3.

### 4.5 sys_copy: Server-side copy (Issue #3329)

Uses backend-native server-side copy when available (GCS, S3), streaming for
cross-backend, read+write as fallback. Holds VFS locks internally — callers
must NOT hold locks when calling `sys_copy`.

### 4.6 sys_watch: File change notification (inotify)

Waits for file changes with timeout. Returns `FileEvent` dict or `None` on
timeout. Supports recursive watching. Backed by `FileWatcher` kernel primitive.

### 4.7 Hash-addressed ops: Driver level, not kernel

```
Kernel:  sys_read(path)        → internal: path → metadata → hash → driver.read_content(hash)
                                  kernel knows path, does not know hash
Driver:  object_store.read_content(hash) → bytes
                                  driver knows hash, does not care about path
```

Federation content replication uses ObjectStoreABC directly (like HDFS DataTransferProtocol
between DataNodes — separate from NameNode API).

---

## 5. POSIX Alignment Summary

| Syscall | Aligned? | Notes |
|---------|----------|-------|
| `sys_stat` | ✅ | dict vs struct stat (Pythonic) |
| `sys_setattr` | ✅ | Bundles chmod/chown/utimes + mknod (DT_DIR, DT_PIPE, DT_STREAM, DT_MOUNT) |
| `sys_readdir` | ✅ | No opendir/closedir (acceptable simplification), supports pagination |
| `sys_rename` | ✅ | — |
| `sys_unlink` | ✅ | Unified delete (files + dirs), metadata-only (CAS GC pattern) |
| `sys_copy` | ✅ | No direct POSIX equivalent; server-side optimization |
| `sys_read` | ✅ | count/offset (pread semantics) |
| `sys_write` | ✅ | count/offset, content-only (SRP) |
| `sys_lock` | ✅ | fcntl(F_SETLK) — acquire + extend (lock_id param) |
| `sys_unlock` | ✅ | flock(LOCK_UN) — release + force (force param) |
| `sys_watch` | ✅ | inotify(7) equivalent |

Tier 2 demotions (no longer Tier 1):
- `access` → Tier 2 (derives from `sys_stat`)
- `is_directory` → Tier 2 (derives from `sys_stat`)
- `rmdir` → Tier 2 (delegates to `sys_unlink`)

---

## 6. Verification

```bash
uv run pytest tests/ -x -o "addopts="
uv run mypy src/nexus/contracts/filesystem/ src/nexus/core/nexus_fs.py
uv run ruff check src/
PYTHONPATH=src uv run lint-imports
```

---

## 7. Long-term Architecture: Collapse to RPC Boundary (decided 2026-04-02)

### 7.1 Problem: NexusFilesystem is a redundant boundary

The current kernel boundary (`NexusFilesystem`) is a Python ABC whose
`sys_*` methods mirror the gRPC proto RPC definitions almost 1:1. This
means the ABC is not a real abstraction — it's just the wire protocol's
Python projection. All transport adapters converge on the same methods:

```
gRPC servicer        ──┐
HTTP/FastAPI routers  ──┤
FUSE operations       ──┼──→  NexusFilesystem.sys_read/write/...
Python SDK (nexus-fs)  ──┤
MCP                   ──┘
Future: driver API    ──┘
```

This creates several problems:

| Problem | Root cause |
|---------|-----------|
| Kernel FFI facade exists | Bypassing ABC's Python call overhead |
| `Arc<Inner>` on 5+ structs | Sharing state across FFI boundary |
| GIL safety clone-then-call pattern | Rust calling Python callbacks |
| Dual code paths (Rust fast + Python fallback) | Every feature maintained in two places |
| Hook count sync via `AtomicU64` | Kernel straddles two languages |
| 6 files to touch per new feature | ABC → impl → Kernel → stubs → proto → servicer |

No production storage system puts an internal ABC below its wire protocol:

| System | Kernel boundary |
|--------|----------------|
| Linux | syscall ABI |
| PostgreSQL | wire protocol |
| Redis | RESP protocol |
| CockroachDB | SQL / gRPC |
| etcd | gRPC |

### 7.2 Target: RPC as kernel boundary (transport-agnostic)

The boundary should be at the **RPC abstraction level** — not gRPC
specifically (which mandates HTTP/2 + protobuf + network), but the
procedure call contract itself: "given an operation name + arguments,
return a result." This is the common ancestor of all transports.

```
Transport adapters (thin, many):
┌─────────────────────────┐
│ gRPC    (tonic / grpcio) │──┐
│ HTTP    (axum / FastAPI)  │──┤       ┌───────────────────────────┐
│ FUSE    (fuse3)           │──┼──────→│  Rust kernel (pub fn)      │
│ PyO3    (in-process)      │──┤       │  sys_read(ctx, path, ...)  │
│ Driver  (OS syscall hook) │──┤       │  sys_write(ctx, path, ...) │
│ MCP                       │──┘       │  sys_stat(ctx, path, ...)  │
└─────────────────────────┘       └───────────────────────────┘
                                           │
                                      ┌────┴────┐
                                      │Backends │
                                      │ CAS: pure Rust              │
                                      │ S3/GCS: PyO3 → Python       │
                                      └─────────┘
```

Key design decisions:

1. **Kernel = Rust `pub fn`**, not ABC, not trait. One implementation, not
   an interface-with-one-impl pattern.
2. **Transport adapters are thin**: gRPC adapter deserializes → calls
   `kernel::sys_read` → serializes response. ~20 lines per RPC.
3. **In-process calls use PyO3** (for `nexus-fs` Python package, FUSE
   mount, unit tests). ~100ns FFI overhead, no network, no serialization.
4. **Hooks/observers** become Rust middleware/interceptors on the kernel
   functions, not a cross-language callback dance.
5. **Python backends** continue to exist, called via PyO3 embedded
   interpreter (same pattern as Phase F's backend callback — already
   proven to work).

### 7.3 What this eliminates

| Artifact | Status after collapse |
|----------|----------------------|
| `NexusFilesystem` (ABC → Protocol) | **Done** — now a Protocol, not ABC (PR 7a) |
| `Kernel` struct | **Done** — owns all core state: DCache, Router, Trie, Hooks, Observers, Metastore (PR 7b) |
| `Arc<Inner>` on 5+ structs | **Done** — all structs are fields of `Kernel` |
| Dispatch (KernelDispatch → DispatchMixin) | **Done** — Rust Kernel owns registries, DispatchMixin provides Python API (PR 7c) |
| Overlay feature | **Deleted** — CAS dedup makes it unnecessary (PR 7, -1354 lines) |
| CDC reassembly | **Done** — chunked_manifest detection + reassembly in Rust CAS engine |
| `stubs/nexus_kernel/__init__.pyi` | **Auto-generated** — `codegen_kernel_abi.py` reads Rust source |
| Module rename | **Done** — `nexus_fast` → `nexus_kernel` (PR 8) |
| `_backend_read` elimination | **Done** — all sys_read paths go through Rust kernel; Python `_backend_read` deleted (#1817 PR #3848) |
| `sys_write` metadata in Rust | **Done** — Rust kernel builds metadata after CAS write; Python `_write_internal`/`_build_write_metadata` deleted (#1817 PR #3848) |
| PIPE/STREAM in Rust | **Done** — sys_read/sys_write dispatch to PipeManager/StreamManager in Rust; `pipe_read_nowait`/`pipe_destroy` bypasses deleted (#1817 PR #3852) |
| Advisory lock in Rust | **Done** — `LockManager` in Rust (lock_manager.rs): LocalLocks + DistributedLocks; Python `sys_lock`/`sys_unlock` = thin wrappers |
| Connector via gRPC | **Done** — external/remote backends route through Rust gRPC adapter, not Python ObjectStoreABC (#1960 PR #3843) |

### 7.4 Concrete code shape

```rust
// kernel/mod.rs — THE kernel. Not a trait, just functions.
pub fn sys_read(ctx: &KernelCtx, path: &str, offset: u64, count: Option<u64>) -> Result<Bytes> {
    // validate → route → dcache → vfs_lock → CAS/backend read → unlock
    // This is what Kernel.sys_read() does today, minus the FFI wrapper.
}

// grpc/vfs_service.rs — thin tonic adapter
async fn read(&self, req: Request<ReadRequest>) -> Result<Response<ReadResponse>> {
    let data = kernel::sys_read(&self.ctx, &req.path, req.offset, req.count)?;
    Ok(Response::new(ReadResponse { data }))
}

// python/mod.rs — thin PyO3 adapter (for nexus-fs embed, FUSE, tests)
#[pyfunction]
fn sys_read(ctx: &PyKernelCtx, path: &str, offset: u64, count: Option<u64>) -> PyResult<Py<PyBytes>> {
    let data = kernel::sys_read(&ctx.inner, path, offset, count)?;
    Ok(PyBytes::new(py, &data))
}
```

One implementation. Two thin bindings (gRPC + PyO3). Zero ABCs.

### 7.5 Migration path from current state

All work done in Phases A-G is directly reusable:

| Current (Phase G) | Target |
|-------------------|--------|
| `Kernel.sys_read` logic | `kernel::sys_read()` body (identical) |
| `RustPathRouterInner` | `kernel::Router` (struct field, no Arc) |
| `RustDCacheInner` | `kernel::DCache` (struct field, no Arc) |
| `VFSLockManagerInner` | `kernel::VfsLock` (struct field, no Arc) |
| `CASEngine` | `kernel::CasEngine` (unchanged) |
| `read_backend` PyO3 callback | `kernel::call_python_backend()` (unchanged) |

Migration phases (incremental, each a PR):

1. **Rust kernel crate**: Extract `kernel/mod.rs` with `pub fn sys_read/write`
   from current `Kernel` logic. Kernel struct becomes the single
   kernel entry point.
2. **PyO3 binding adapter**: Replace `Kernel` pyclass with thin
   `#[pyfunction]` bindings to `kernel::sys_*`. NexusFS calls these
   directly instead of through Kernel.
3. **Delete NexusFilesystem**: Move Tier 2 methods to a standalone
   Python module that calls the PyO3 `sys_*` functions.
4. **tonic adapter** (optional, parallel): Add gRPC serving via tonic,
   calling the same `kernel::sys_*` functions.

### 7.6 Relationship to current plan phases

| Phase | Status | Relationship to §7 |
|-------|--------|-------------------|
| A-G | Done | Logic **reused verbatim** in `Kernel.sys_read/write` |
| H (all Tier 1 syscalls) | Done | `sys_stat` + plan methods in Kernel |
| I (io_uring) | **Deferred indefinitely** | ~1-2μs per syscall (negligible). Rust-native async covers batch workloads. |
| §7 PR 7a (ABC → Protocol) | Done | `NexusFilesystemABC(ABC)` → `NexusFilesystem(Protocol)`, 28 files |
| §7 PR 7b (Metastore adapter) | Done | `PyMetastoreAdapter` in Rust, `set_metastore()`, dcache-miss fallback |
| §7 PR 7c (Dispatch collapse) | Done | `_resolve_and_read` deleted, `_read_via_dlc` → `_backend_read`, `KernelDispatch` → `DispatchMixin` |
| §7 PR 7d (Crate rename) | Done | `rust/nexus_pyo3` → `rust/nexus_kernel` |
| §7 PR 7e (Dispatch traits) | Done | `InterceptHook`/`PathResolver`/`MutationObserver` Rust traits + PyO3 adapters |
| §7 PR 7f (CDC Rust) | Done | CDC chunked_manifest detection + reassembly in Rust CAS engine |
| §7 PR 7g (Overlay deleted) | Done | Overlay feature deleted (-1354 lines), CAS dedup replaces it |
| §7 PR 8 (Codegen) | Done | `codegen_kernel_abi.py` generates stubs, protocols, exports from Rust source |
| §7 PR 8 (Module rename) | Done | `nexus_fast` → `nexus_kernel` (Python module name, 90+ files) |
| **§7 remaining** | **Done** | `_backend_read` deleted, sys_write metadata moved to Rust, PIPE/STREAM dispatched in Rust, advisory locks in Rust, connectors via gRPC — all completed in #1817/#1960 |

The key insight: **Phase H is the last phase that adds logic.** The §7
collapse is a **refactoring** that changes the boundary, not the logic.

---

## 8. Version History

| Version | Date | Changes |
|---------|------|---------|
| §1–§7 | 2026-03 | Initial syscall design, POSIX alignment, convenience layer, key decisions, collapse plan |
| §8 | 2026-04-10 | Added version history table |
| §11 | 2026-04-10 | KERNEL-ARCHITECTURE.md §2.4.1: formal 4 dispatch contracts (RESOLVE, INTERCEPT PRE, INTERCEPT POST, OBSERVE) with ordering, error semantics, and zero-overhead invariant. Phase 18 docs. |
| §7.3, §7.6 | 2026-04-23 | §7 collapse roadmap fully completed: `_backend_read` deleted, sys_write metadata in Rust, PIPE/STREAM dispatched in Rust, advisory locks in Rust, connectors via gRPC. All "Remaining" items → Done (#1817, #1960). |
