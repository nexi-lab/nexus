# Unified Kernel Lock Architecture

**Issues**: #909, #906, #908, #910, #805
**Prerequisite**: #1323 (OCC + lock extraction from kernel write path)
**Status**: Design complete. Implementation in follow-up PR(s).

---

## 1. Current State

| What | Where | Latency | Scope |
|------|-------|---------|-------|
| **VFSLockManager** | `core/lock_fast.py` | ~200ns Rust / ~500ns Python | Local, path-level RW, hierarchical |
| **RaftLockManager** | `raft/lock_manager.py` | ~5-10ms | Distributed, mutex + semaphore, zone-scoped |
| **PassthroughBackend.lock()** | `backends/passthrough.py` | ~1us | Local, in-memory mutex + semaphore |
| **_StripeLock** | `backends/cas_blob_store.py` | ~1us | Local, CAS hash-stripe threading.Lock |
| **LockProtocol** | `contracts/protocols/lock.py` | — | Service contract (lock/unlock/extend_lock) |
| **LockManagerProtocol** | `lib/distributed_lock.py` | — | Internal (acquire/release/extend) |
| ~12 `asyncio.Semaphore` | scattered | — | Ad-hoc concurrency bounding |

**Problems**:
1. Two user-facing APIs (`LockProtocol` vs `LockManagerProtocol`) for the same operation
2. VFSLockManager exists but is NOT wired into sys_read/sys_write — reads race with writes
3. Single-node Raft lock = 10000x overhead for no benefit
4. No local kernel semaphore — Raft semaphore works but no standalone equivalent
5. PassthroughBackend.lock() duplicates logic that should live in kernel

---

## 2. Three-Layer Model

```
                     AI Users / Services
                           |
                  lib/lock.py (unified API)        ← Layer 2: routing
                  lock() / unlock() / locked()
                  sem_acquire() / sem_release() / semaphore()
                           |
                   _LockRouter.auto_detect
                           |
              +------------+------------+
              |                         |
         LOCAL path               DISTRIBUTED path
         (always present)         (conditional driver, DI'd at boot)
              |                         |
       VFSLockManager            RaftLockManager          ← Layer 1: primitives
       VFSSemaphore              (max_holders>1)
       ~200ns, sync              ~5-10ms, async
```

**Layer 1 — Kernel Primitives** (`core/`): Always present. Internal, fast.

| Primitive | Linux Analogue | Use Case |
|-----------|---------------|----------|
| VFSLockManager (exists) | `i_rwsem` | sys_read/sys_write path-level RW lock |
| VFSSemaphore / `VFSSemaphoreProtocol` (**new**) | `struct semaphore` | Named holder-tracked counting semaphore |
| _StripeLock (exists) | spinlock stripe | CAS metadata RMW (internal, unchanged) |

VFSLockManager = **RW lock** (read shares, write excludes, hierarchical).
VFSSemaphore = **counting semaphore** (N holders, no read/write distinction).
Separate primitives — different invariants, like Linux `i_rwsem` vs `struct semaphore`.

**Layer 2 — Unified Public API** (`lib/lock.py`): User-facing, routes automatically.

**Locks** (path-addressed):
```python
async def lock(path, *, mode="write", timeout=30, ttl=30,
               force_local=False, force_distributed=False) -> str | None
async def unlock(lock_id, path) -> bool
async def extend(lock_id, path, *, ttl=30) -> bool
async with locked(path, mode="write") as lock_id: ...
```

**Semaphores** (name-addressed):
```python
async def sem_acquire(name, *, max_holders, timeout=30, ttl=30,
                      force_local=False, force_distributed=False) -> str | None
async def sem_release(holder_id, name) -> bool
async with semaphore(name, max_holders=N) as holder_id: ...
```

**Layer 3 — Contracts** (`contracts/protocols/lock.py`): Keep `LockProtocol` unchanged.
Add `SemaphoreProtocol` (sem_acquire/sem_release with `_context` param).

---

## 3. Kernel Primitives

### 3.1 VFSLockManager (exists — wire into syscalls)

Already in `core/lock_fast.py` with Rust + Python fallback. Target: auto-acquire
around every syscall I/O.

```python
# sys_read (pseudo-code)
handle = self._vfs_lock.acquire(path, "read", timeout_ms=5000)
try:
    data = backend.read_content(content_hash)
finally:
    self._vfs_lock.release(handle)
```

| Syscall | Lock Mode | Failure |
|---------|----------|---------|
| `sys_read` | shared (read) | Timeout → EBUSY |
| `sys_write` | exclusive (write) | Timeout → EBUSY |
| `sys_rename` | exclusive on both old + new | Timeout → EBUSY |
| `sys_unlink` | exclusive (write) | Timeout → EBUSY |

Properties: synchronous, ~200ns, hierarchical (`write("/a/b")` blocks `read("/a/b/c")`),
no TTL (held for syscall duration only), not user-visible (like `i_rwsem`).

**Kernel lock vs user lock**: Kernel VFS lock is **mandatory** (enforced on every I/O).
User advisory lock via `lib/lock.py` is **cooperative** (like `flock(2)`). Both coexist
on the same path without conflict — different instances, different granularity.

### 3.2 VFSSemaphore (new)

Local kernel counterpart to RaftLockManager's semaphore (`max_holders > 1`).
Behavior must be identical for transparent routing.

#### Semantic Note: Holder-Tracked Semaphore

Our semaphore is **holder-tracked**: each `acquire` returns a unique `holder_id`,
`release` requires that ID. This differs from classical Dijkstra P/V (anonymous counter).

| | Classical P/V | Nexus Semaphore |
|---|---|---|
| Acquire | Anonymous, decrement | Returns `holder_id` |
| Release | Anyone can signal | Only holder can release |
| Tracking | None | Per-holder: ID, acquired_at, expires_at |

This is standard for distributed semaphores (Consul sessions, ZK ephemeral nodes,
Redis sorted sets) — holder tracking is required for crash recovery and TTL expiry.
We retain "semaphore" naming because: (1) function signatures are self-documenting,
(2) industry distributed semaphores universally track holders, (3) alternative names
(`SlotLock`, `CountedLock`) lack consensus and training data for AI callers.

#### Interface

```python
class VFSSemaphore:
    """Holder-tracked counting semaphore — local kernel primitive.

    Matches RaftLockManager.acquire(max_holders=N) semantics:
    SSOT max_holders, TTL expiry, ownership verification.
    """
    def acquire(self, name: str, max_holders: int,
                timeout_ms: int = 30000, ttl_ms: int = 30000) -> str | None
    def release(self, name: str, holder_id: str) -> bool
    def extend(self, name: str, holder_id: str, ttl_ms: int = 30000) -> bool
    def info(self, name: str) -> SemaphoreInfo | None
    def force_release(self, name: str) -> bool
```

Implementation: Rust via PyO3 + Python fallback. `dict[name, SemaphoreState]`
under single mutex. Lazy TTL expiry on acquire.

| Behavior | RaftLockManager | VFSSemaphore |
|----------|----------------|-------------|
| max_holders | SSOT, mismatch → ValueError | Same |
| TTL | redb, checked on acquire | In-memory, checked on acquire |
| Holder ID | UUID | UUID |
| Blocking | Async retry + exp. backoff | Sync spin + backoff |
| Cleanup | Entry removed when empty | Same |

---

## 4. Two-Lock Architecture (revised)

Through architecture review, the original LockRouter plan was rejected.
Key insight: advisory locks and I/O locks are **fundamentally different concerns**.

### 4.1 Why No Router

1. **Writes converge**: In all deployment modes (standalone, REMOTE, federation),
   writes to the same path converge to a single process. VFSLockManager
   (in-memory, ~200ns) is sufficient for I/O serialization.
2. **Advisory locks ARE metadata**: Like HDFS leases in the NameNode's
   FSImage+EditLog, advisory locks should live in the metastore — visible,
   queryable, Raft-replicated in federation, persistent with TTL cleanup.
3. **Factory DI suffices**: `factory.py` injects `LocalLockManager` (standalone)
   or `RaftLockManager` (federation). Both implement `LockManagerProtocol`.
   No runtime routing needed.

### 4.2 Two Locks

```
┌──────────────────────────────────────┬──────────────────────────────────────┐
│  I/O Lock (core/)                    │  Advisory Lock (metastore)           │
├──────────────────────────────────────┼──────────────────────────────────────┤
│  VFSLockManager — in-memory HashMap  │  MetastoreABC.acquire_lock() — redb │
│  ~200ns, sync, handle-based          │  ~5μs standalone / ~ms Raft         │
│  Process-scoped (crash → released)   │  TTL-based (expire → released)      │
│  Kernel-internal (sys_read/write)    │  User/service-facing (coordination) │
│  Metadata-invisible                  │  Metadata-visible, queryable        │
└──────────────────────────────────────┴──────────────────────────────────────┘
```

**Fingerprint**: Advisory locks require `ttl > 0` (mandatory, prevents orphans).
I/O locks have no TTL (kernel manages lifecycle in try/finally).

**Restart behavior**: Advisory locks survive in redb. Dead holders stop renewing →
TTL expires → auto-released. No orphans.

### 4.3 DI Model

```python
# factory.py (pseudo-code)
if metastore.supports_locks:
    if dist.enable_locks:
        lock_manager = RaftLockManager(metadata_store)   # federation
    else:
        lock_manager = LocalLockManager(metadata_store)  # standalone
```

| Profile | Metastore | lock_manager → |
|---------|-----------|----------------|
| minimal / embedded | redb (supports_locks=True) | LocalLockManager |
| lite / full | redb (supports_locks=True) | LocalLockManager |
| cloud / federation | redb + Raft (supports_locks=True) | RaftLockManager |
| remote | RemoteMetastore (supports_locks=False) | None (server-side) |

Callers see only `LockManagerProtocol`. Same async API regardless of backend.

---

## 5. Summary

| Primitive | Location | Latency | Visibility | TTL | Scope |
|-----------|----------|---------|------------|-----|-------|
| VFSLockManager | `core/lock_fast.py` | ~200ns | Kernel-internal | No | Local |
| VFSSemaphore | `core/semaphore.py` (new) | ~200ns | Kernel-internal | Yes | Local |
| RaftLockManager | `raft/lock_manager.py` | ~5-10ms | Internal | Yes | Distributed (zone) |
| _StripeLock | `backends/cas_blob_store.py` | ~1us | Backend-internal | No | Local (per-hash) |
| lib/lock.py | `lib/lock.py` (new) | Auto | **User-facing** | Auto | Auto |

---

## 6. Design Decisions

**D1: Two locks, not one** — I/O lock (VFSLockManager, kernel-internal, ~200ns) and
advisory lock (MetastoreABC, user-facing, TTL-based) are fundamentally different.
Like Linux `i_rwsem` vs `flock(2)`.

**D2: Advisory locks are metadata** — stored in MetastoreABC (redb), queryable,
Raft-replicated in federation. Like HDFS leases in NameNode FSImage+EditLog.

**D3: Factory DI, not runtime routing** — `LocalLockManager` or `RaftLockManager`
injected at boot. No `_LockRouter`, no runtime auto-detect. Simpler, testable.

**D4: PassthroughBackend.lock() deprecated** — duplicates kernel lock logic.
EventsService should migrate to `LockManagerProtocol`.

**D5: asyncio.Semaphore stays as-is** — internal concurrency limiters (not advisory
locks). No names, TTL, or cross-node semantics needed.

**D6: Kernel lock mandatory, advisory lock cooperative** — sys_read/sys_write always
acquire VFSLockManager. Advisory locks are cooperative like `flock(2)`.
