# Write Path Extraction from Kernel (#1323)

**Task**: #1323 (Extract OCC/CAS from kernel write path)
**Depends on**: #1318 (sys_read/sys_write POSIX alignment — merged)
**Blocks**: #1321 (Tier 2 convenience layer), #1320 (sys_unlink metadata-only)
**Status**: Design complete. Implementation in progress.

---

## 1. Problem: Two Meanings of "CAS" Conflated in Kernel

The codebase conflates two orthogonal concepts under "CAS":

| Concept | What It Means | Where It Belongs |
|---------|--------------|-----------------|
| **CAS Addressing** | Content identified by hash (SHA-256). Same content = same key. Dedup. | ObjectStoreABC (driver) |
| **CAS Concurrency** | Compare-And-Swap: check etag before write. `if_match`, `if_none_match`, `force`. | Application / service layer |

Linux kernel analogy:
- **CAS addressing** ≈ block device LBA addressing. The kernel doesn't care if the disk is SSD, HDD, or NVMe. That's driver detail.
- **CAS concurrency** ≈ file locking. `write(2)` doesn't do compare-and-swap. Applications use `flock(2)` + retry for concurrency control.

**Currently both live inside `_write_internal()` (kernel):**

```python
# nexus_fs.py:2625 — _write_internal is kernel-internal but has CAS concurrency params
def _write_internal(self, path, content, context,
                    if_match,         # ← CAS concurrency (doesn't belong)
                    if_none_match,    # ← CAS concurrency (doesn't belong)
                    force,            # ← CAS concurrency (doesn't belong)
                   ) -> dict[str, Any]:  # ← returns dict (should be int)

    # Lines 2683-2704: OCC logic in kernel
    if not force:
        if if_none_match and meta is not None:
            raise FileExistsError(...)        # ← CAS concurrency
        if if_match is not None:
            if meta.etag != if_match:
                raise ConflictError(...)      # ← CAS concurrency

    # Lines 2706-2720: Content write via driver (correct)
    content_hash = route.backend.write_content(content, context=context).content_hash

    # Lines 2740-2875: Metadata update, events, permissions (belongs in kernel)
    ...
```

This is wrong at every level:
1. `_write_internal` is a kernel method with application-level concurrency params
2. `NexusFS.write()` (Tier 2) exposes `if_match`/`force` — but Tier 2 should only compose Tier 1 primitives
3. `append()` and `edit()` need CAS but can only get it through `write()`
4. CLI uses `cast(Any, nx)` to bypass type checking for CAS params

---

## 2. Where/How/When Framework

Three orthogonal dimensions of storage:

### Where (4 Storage Pillars)

Already correct — MetastoreABC, ObjectStoreABC, RecordStoreABC, CacheStoreABC.

### How (Addressing Strategy)

CAS addressing (hash-based) is a **driver-level** concern, not kernel. The driver decides
how to map content to storage locations.

**Current state**: Already correctly encapsulated in ObjectStoreABC backends.
Each backend has its own addressing internally:

| Backend | Addressing | Hash Algorithm |
|---------|-----------|---------------|
| LocalBackend | CAS (hash) | BLAKE3 |
| GCSBackend | CAS (hash) | SHA-256 |
| PassthroughBackend | Path (filesystem) | N/A |
| RemoteBackend | Delegated | Delegated |

**Design: Composable AddressingStrategy at driver level**

```python
class AddressingStrategy(Protocol):
    """How content maps to storage locations."""
    def content_path(self, content: bytes) -> str: ...
    def is_content_addressed(self) -> bool: ...

class CASAddressing:
    """Hash-based content addressing (current default)."""
    def __init__(self, algorithm: str = "blake3"):
        self._algorithm = algorithm
    def content_path(self, content: bytes) -> str:
        h = hash_content(content, self._algorithm)
        return f"cas/{h[:2]}/{h[2:4]}/{h}"
    def is_content_addressed(self) -> bool:
        return True

class PathAddressing:
    """Direct path-based storage (for passthrough/connector backends)."""
    def content_path(self, content: bytes) -> str:
        raise NotImplementedError("Path addressing uses explicit paths")
    def is_content_addressed(self) -> bool:
        return False

# Driver composition (future, not this PR):
class LocalBackend(ObjectStoreABC):
    def __init__(self, root: str, addressing: AddressingStrategy = CASAddressing()):
        self._addressing = addressing
```

This is **not implemented in this PR** — just documenting the direction. The current
backends already work correctly. This composition enables quickly switching from CAS
to path addressing when needed.

### When (Concurrency Control — Lock Architecture)

Three complementary lock layers:

```
┌─────────────────────────────────────────────────────────────┐
│  Layer 3: Application Retry (agent code)                     │
│    pattern: read(etag) → modify → stat(etag) → write        │
│    scope: per-agent, best-effort                             │
├─────────────────────────────────────────────────────────────┤
│  Layer 2: RaftLockManager (distributed advisory)             │
│    pattern: events_service.locked(path) / write(lock=True)   │
│    scope: cross-node, cross-process (same zone)              │
│    location: raft/lock_manager.py (service layer)            │
│    mechanism: Raft consensus via LockStoreProtocol            │
├─────────────────────────────────────────────────────────────┤
│  Layer 1: VFSLockManager (kernel i_rwsem)                    │
│    pattern: automatic, transparent in sys_write              │
│    scope: single-process, protects coroutine concurrency     │
│    location: core/lock_fast.py (kernel-internal)             │
│    mechanism: Rust rwsem or Python threading.RLock fallback   │
└─────────────────────────────────────────────────────────────┘
```

**Deployment mode coverage:**

| Mode | VFSLockManager | RaftLockManager | Who protects cross-agent? |
|------|---------------|----------------|--------------------------|
| Server (single process) | All agents' requests in one process | Via Raft | VFSLockManager (all requests funnel through) |
| Embedded (per-agent) | Only intra-process coroutines | Needs Raft cluster | RaftLockManager (explicit) |
| Embedded + no Raft | Only intra-process | None | No cross-agent protection |

**VFSLockManager is kernel-internal, NOT DI:**

```python
# NexusFS.__init__ — VFSLockManager created internally (like Linux i_rwsem)
class NexusFS:
    def __init__(self, ...):
        ...
        self._vfs_lock = create_vfs_lock_manager()  # kernel-internal, not injected

    def sys_write(self, path, buf, *, ...):
        handle = self._vfs_lock.acquire(path, "write", timeout_ms=5000)
        try:
            ... # actual write
        finally:
            self._vfs_lock.release(handle)
```

Unlike drivers/services (which are DI-injected), VFSLockManager is a kernel primitive:
- Created at kernel init, not configurable from outside
- Analogous to Linux `i_rwsem` (always present per inode)
- No factory/orchestrator involvement

---

## 3. Design: Decompose `_write_internal` into Orthogonal Concerns

### 3.1 Target Architecture

```
                        ┌─────────────────────────────────────────────┐
Application:            │  Agent does: read(etag) → modify → retry   │
                        │  Or: acquire_lock → write → release_lock   │
                        └────────┬──────────────────────┬─────────────┘
                                 │                      │
                        ┌────────▼──────────┐  ┌───────▼───────────┐
RPC/HTTP boundary:      │  If-Match check   │  │  Lock middleware   │
                        │  (stat → compare) │  │  (flock pattern)  │
                        └────────┬──────────┘  └───────┬───────────┘
                                 │                      │
                        ┌────────▼──────────────────────▼─────────────┐
Kernel (Tier 2):        │  write(path, buf) = sys_write + sys_stat    │
                        │  No CAS params. No lock params.             │
                        └────────┬──────────────────────┬─────────────┘
                                 │                      │
Kernel (Tier 1):        ┌────────▼────────┐    ┌───────▼──────────┐
                        │ sys_write(path, │    │ sys_setattr(path,│
                        │   buf) → int    │    │   **attrs) → FM  │
                        │ [VFSLock auto]  │    └──────────────────┘
                        └────────┬────────┘
                                 │
Driver:                 ┌────────▼────────────────────────────────┐
                        │ ObjectStoreABC.write_content(content)   │
                        │   → WriteResult(content_hash, size)     │
                        │ [AddressingStrategy internal]            │
                        └─────────────────────────────────────────┘
```

### 3.2 What Changes

| Component | Before (#1318) | After (#1323) |
|-----------|---------------|---------------|
| `sys_write` | `force=True` → `_write_internal` | Direct write + VFSLock. No CAS params. |
| `write()` | `if_match/force/lock` → `_write_internal` | Compose: `sys_write` + `sys_stat`. No CAS. No lock. |
| `_write_internal` | 250-line monolith with CAS + metadata + events | **Inlined into `sys_write`.** |
| `append()` | `write(if_match=etag)` | `sys_read` + concat + `sys_write`. No CAS. |
| `edit()` | `write(if_match=etag)` | `sys_read` + transform + `sys_write`. No CAS. |
| OCC/CAS check | In `_write_internal` lines 2683-2704 | RPC handler (stat → compare → write). |
| `ConflictError` | Raised by kernel | Raised by RPC handler / application. |
| CLI `--if-match` | `cast(Any, nx).write(if_match=...)` | CLI-level: stat → compare → write → retry. |
| Lock in `write()` | `write(lock=True)` → `_acquire_lock_sync` | **Removed from write().** App uses `events_service.locked()`. |

### 3.3 `_write_internal` Decomposition

The 250-line `_write_internal` method does 6 things. Here's where each goes:

| Concern | Lines | Destination |
|---------|-------|-------------|
| 1. OCC check (`if_match`, `if_none_match`) | 2683-2704 | **Delete.** Application/service concern. |
| 2. Backend write (`backend.write_content`) | 2706-2720 | → `sys_write` |
| 3. Metadata update (`metadata.put`) | 2740-2754 | → `sys_write` (Phase A) or `sys_setattr` (Phase B) |
| 4. Event notification (`_dispatch.notify`) | 2757-2772 | → `sys_write` (post-write hook) |
| 5. Permission operations (ReBAC) | 2793-2837 | → `sys_write` (post-write hook) |
| 6. Write observer callbacks | 2867 | → `sys_write` (post-write hook) |

After extraction, `sys_write` absorbs concerns 2-6 (the actual kernel write path),
and concern 1 (OCC) leaves the kernel entirely.

---

## 4. CAS Concurrency: Where It Goes

### 4.1 Linux Model

Linux provides three concurrency mechanisms, none inside `write(2)`:

| Mechanism | Linux | Nexus Equivalent |
|-----------|-------|-----------------|
| Kernel i_rwsem | `inode_lock → write → inode_unlock` | VFSLockManager in sys_write (automatic) |
| Advisory lock | `flock(fd, LOCK_EX)` | `events_service.locked(path)` (distributed) |
| App-level CAS | `read(fd) → compare → write(fd) → retry` | Agent reads etag, compares, retries |

### 4.2 HTTP/RPC Layer CAS (Transitional)

For HTTP API backward compatibility, the `If-Match` header handling stays in the
**RPC/HTTP handler layer** (server code, not kernel):

```python
# server/rpc/handlers/filesystem.py — CAS check at RPC boundary, not in kernel
async def handle_write(params, nexus_fs):
    if params.if_match:
        meta = nexus_fs.sys_stat(params.path)
        if meta and meta.etag != params.if_match:
            raise ConflictError(path=params.path,
                                expected_etag=params.if_match,
                                current_etag=meta.etag)
    nexus_fs.sys_write(params.path, params.buf)
```

### 4.3 Application-Level CAS Pattern (Recommended)

```python
# Agent code (application level)
def safe_update(nx, path, transform_fn, max_retries=3):
    for attempt in range(max_retries):
        meta = nx.sys_stat(path)
        content = nx.sys_read(path)
        new_content = transform_fn(content)
        current_meta = nx.sys_stat(path)
        if current_meta.etag != meta.etag:
            continue  # Retry — someone else wrote
        nx.sys_write(path, new_content)
        return
    raise ConflictError("Max retries exceeded")
```

### 4.4 Cloud-Native CAS (Future Optimization)

Some backends have native CAS (S3 `If-Match`, GCS `if_generation_match`).
These are **backend driver optimizations**, not kernel concerns. If a backend supports
native CAS, its `write_content()` method can accept an optional etag for atomic
compare-and-write. The kernel never knows about it.

---

## 5. Implementation Phases

### Phase 1: Extract OCC + wire VFSLock (This PR)

**Goal**: `_write_internal` deleted. OCC leaves kernel. VFSLockManager wired to sys_write.

1. **Wire VFSLockManager into sys_write** (`core/nexus_fs.py`)
   - `NexusFS.__init__`: `self._vfs_lock = create_vfs_lock_manager()` (kernel-internal, not DI)
   - Remove from `BrickServices.vfs_lock_manager` / factory orchestrator DI path
   - `sys_write`: acquire write lock before write, release in finally
   - `sys_read`: acquire read lock (allows concurrent reads)

2. **Inline `_write_internal` into `sys_write`**
   - Remove `if_match`, `if_none_match`, `force` params
   - Move concerns 2-6 directly into `sys_write`
   - Delete `_write_internal` method

3. **Move OCC check to RPC handler** (`server/rpc/handlers/filesystem.py`)
   - `handle_write`: check `if_match` via `sys_stat` before calling `sys_write`
   - `handle_delta_write`: same pattern
   - HTTP `async_files.py`: same pattern

4. **Clean up `NexusFS.write()`**
   - Remove `if_match`, `if_none_match`, `force`, `lock`, `lock_timeout` params
   - Compose: `sys_write(path, buf)` + `sys_stat(path)` for metadata return
   - No CAS logic, no lock logic

5. **Update `append()` and `edit()`**
   - Remove `if_match`/`force` kwargs
   - Just compose `sys_read` + transform + `sys_write`

6. **Update CLI** (`file_ops.py`)
   - `nexus write --if-match`: implement CAS at CLI level (stat → compare → write)
   - Remove `cast(Any, nx)` hacks

7. **Update MCP** (`bricks/mcp/server.py`)
   - `edit_file` tool: remove `if_match` from kernel call, do stat+compare at MCP layer

8. **Update ScopedFilesystem**
   - Remove CAS/lock params from `write()`

9. **Update tests**
   - Migrate all CAS tests to use stat+compare+write pattern
   - Keep `ConflictError` tests (now in RPC handler layer)
   - Add VFSLockManager integration tests

### Phase 2: sys_write content-only, metadata via sys_setattr (Future)

This is the full POSIX alignment from syscall-design.md §4.1:
- `sys_write` only writes content, does NOT update metadata
- Tier 2 `write()` = `sys_write()` + `sys_setattr()` (explicit composition)
- Requires `sys_setattr` to be fully implemented first

### Phase 3: AddressingStrategy driver composition (Future)

- Extract addressing from backend implementations into composable strategy
- `LocalBackend(addressing=CASAddressing())` → easy switch to path addressing
- Not blocking — backends already work correctly

---

## 6. Files Changed (Phase 1)

| File | Change | Scope |
|------|--------|-------|
| `src/nexus/core/nexus_fs.py` | Wire VFSLock, inline _write_internal, clean write() | Core |
| `src/nexus/core/lock_fast.py` | No change (already correct) | Core |
| `src/nexus/factory/orchestrator.py` | Remove VFSLockManager DI wiring | Factory |
| `src/nexus/server/rpc/handlers/filesystem.py` | Add OCC check before sys_write | RPC |
| `src/nexus/server/rpc/handlers/delta.py` | Same pattern | RPC |
| `src/nexus/server/api/v2/routers/async_files.py` | Move If-Match to HTTP handler | HTTP |
| `src/nexus/cli/commands/file_ops.py` | CLI-level CAS: stat → compare → write | CLI |
| `src/nexus/bricks/mcp/server.py` | MCP-level CAS for edit_file | Brick |
| `src/nexus/bricks/filesystem/scoped_filesystem.py` | Remove CAS/lock from write() | Brick |
| `src/nexus/server/_rpc_param_overrides.py` | Remove if_match/force from WriteParams | RPC |
| `src/nexus/contracts/filesystem/filesystem_abc.py` | Clean write() ABC signature | ABC |
| Tests (6+ files) | Migrate CAS tests, add VFSLock tests | Tests |

---

## 7. What Stays, What Goes

### Stays in Kernel
- `etag` field on FileMetadata (metadata, not CAS logic)
- `physical_path = content_hash` mapping (CAS addressing, via ObjectStoreABC)
- `version` counter increment on write
- `ConflictError` exception class (contracts — used by RPC handlers)
- VFSLockManager (kernel-internal, auto-acquired on sys_write/sys_read)

### Leaves Kernel
- `if_match` parameter (→ RPC handler / CLI / application)
- `if_none_match` parameter (→ RPC handler / application)
- `force` parameter (→ deleted, was only "skip CAS check")
- `lock` / `lock_timeout` on write() (→ app uses `events_service.locked()`)
- OCC etag comparison logic (→ RPC handler / CLI)
- `_write_internal` method (→ inlined into `sys_write`)
- `_acquire_lock_sync` / `_release_lock_sync` (→ deleted, was sync bridge for DI lock)

### Moves to Kernel-Internal
- VFSLockManager creation (was: DI via factory → BrickServices → now: NexusFS.__init__)

---

## 8. Verification

```bash
# Full test suite
uv run pytest tests/ -x

# Type check
uv run mypy src/nexus/contracts/filesystem/ src/nexus/core/nexus_fs.py

# Lint
uv run ruff check src/

# Import boundary
PYTHONPATH=src uv run lint-imports

# Smoke: verify no CAS params on kernel methods
uv run python -c "
import inspect
from nexus.core.nexus_fs import NexusFS
sig = inspect.signature(NexusFS.sys_write)
params = list(sig.parameters.keys())
assert 'if_match' not in params, f'if_match still in sys_write: {params}'
assert 'force' not in params, f'force still in sys_write: {params}'

sig2 = inspect.signature(NexusFS.write)
params2 = list(sig2.parameters.keys())
assert 'if_match' not in params2, f'if_match still in write(): {params2}'
assert 'force' not in params2, f'force still in write(): {params2}'
assert 'lock' not in params2, f'lock still in write(): {params2}'
print('OK: No CAS/lock params in kernel')
"
```
