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

## 4. Routing: Kernel Primitive + Conditional Driver

### 4.1 Linux VFS Lock Analogy

```
Linux:   flock(fd, LOCK_EX)  →  VFS (fs/locks.c)  →  ext4: local    / NFS: → lockd/NLM
Nexus:   lib.lock.lock(path) →  _LockRouter        →  VFSLock: local / Raft: → consensus
                                  (always present)     (always)         (conditional)
```

Key properties borrowed from Linux:
- **User code is the same** regardless of backend — `lock("/path")` everywhere
- **Distributed driver is conditionally loaded** — only when Raft service is installed
  AND node has joined a zone (like lockd only when NFS is mounted)
- **Never silent downgrade** — if caller expects distributed and it's unavailable,
  error (like Linux `ENOLCK`), don't silently give local lock

### 4.2 Routing Rules

| Scenario | Behavior |
|----------|----------|
| `lock("/path")`, no Raft | Local. Caller made no distributed claim. |
| `lock("/path")`, Raft available | Distributed. Best available consistency. |
| `force_local=True` | Always local. Caller explicitly chose local scope. |
| `force_distributed=True`, Raft available | Distributed. |
| `force_distributed=True`, **no Raft** | **`ServiceUnavailableError`** (like `ENOLCK`). |

**Never silent downgrade**: two nodes each holding "local exclusive lock" = no actual
mutual exclusion. Worse than failing. Same code transparently upgrades when Raft is
added later — user code unchanged.

### 4.3 DI Model

Distributed path is **DI'd at boot by factory.py**, not user-managed:

```python
# factory.py (pseudo-code)
vfs_lock = create_vfs_lock_manager()         # always
vfs_sem = VFSSemaphore()                      # always
distributed = RaftLockManager(raft_store) if raft_store else None  # conditional
configure_locks(local_lock=vfs_lock, local_sem=vfs_sem, distributed=distributed)
```

Pattern: kernel primitives always present, distributed driver conditionally loaded via DI.
Same pattern as all other Nexus subsystems (metastore, event bus, etc.).

### 4.4 Mode Matrix

| Profile | Raft | lock() → | sem_acquire() → |
|---------|------|----------|----------------|
| kernel / embedded / lite | No | VFSLockManager | VFSSemaphore |
| full | Optional | Auto-detect | Auto-detect |
| cloud / federation | Yes | RaftLockManager | RaftLockManager |

### 4.5 Unlock Routing Registry

`_registry[lock_id] = "local" | "distributed"` — set on acquire, looked up on release,
cleaned up on release. Prevents cross-backend confusion. Leaked entries: distributed
expire via TTL; local are process-scoped (lost on crash anyway).

### 4.6 Semaphore → Raft Mapping

Semaphore names map to reserved VFS namespace for Raft:
`sem_acquire("upload_slots", max_holders=5)` →
`RaftLockManager.acquire(zone_id, "/__sem__/upload_slots", max_holders=5)`.
Reuses existing Raft lock infra, zero new wire protocol.

---

## 5. Migration Path

| Phase | What | Test |
|-------|------|------|
| 1 | `core/semaphore.py` — VFSSemaphore (Rust + Python fallback) | SSOT, TTL, concurrent holders |
| 2 | `lib/lock.py` — _LockRouter + unified API | Mock backends, routing logic |
| 3 | Wire VFSLockManager into sys_read/sys_write | Concurrent R/W race tests |
| 4 | EventsService delegates to lib/lock.py, deprecate PassthroughBackend.lock() | Existing E2E passes |
| 5 | SemaphoreProtocol in contracts/ | Protocol conformance |
| 6 | REST API routes through lib/lock.py | Existing REST E2E passes |
| 7 | factory.py wires configure_locks() | Per-profile integration tests |

---

## 6. Summary

| Primitive | Location | Latency | Visibility | TTL | Scope |
|-----------|----------|---------|------------|-----|-------|
| VFSLockManager | `core/lock_fast.py` | ~200ns | Kernel-internal | No | Local |
| VFSSemaphore | `core/semaphore.py` (new) | ~200ns | Kernel-internal | Yes | Local |
| RaftLockManager | `raft/lock_manager.py` | ~5-10ms | Internal | Yes | Distributed (zone) |
| _StripeLock | `backends/cas_blob_store.py` | ~1us | Backend-internal | No | Local (per-hash) |
| lib/lock.py | `lib/lock.py` (new) | Auto | **User-facing** | Auto | Auto |

---

## 7. Design Decisions

**D1: VFSSemaphore mirrors Raft semaphore exactly** — same SSOT, TTL, ownership.
Users write code once, routing is transparent across deployment profiles.

**D2: Separate RW lock and semaphore** — different invariants (hierarchical path RW
vs named counting). Linux keeps `i_rwsem` / `struct semaphore` separate for same reason.

**D3: lib/lock.py routes, doesn't implement** — thin router. Logic in kernel primitives
(core/) or distributed driver (raft/). Follows lib = libc, core = kernel pattern.

**D4: PassthroughBackend.lock() deprecated** — duplicates kernel lock logic.
EventsService migrates to `lib.lock.lock()`.

**D5: asyncio.Semaphore stays as-is** — internal concurrency limiters (not VFS
semaphores). No names, TTL, or cross-node semantics needed. Lighter weight for
purely local ephemeral use.

**D6: Kernel lock mandatory, user lock advisory** — sys_read/sys_write always
acquire VFSLockManager. lib/lock.py is cooperative like `flock(2)`.

**D7: Never silent downgrade distributed → local** — `force_distributed=True` without
Raft raises `ServiceUnavailableError`. Default (no force) auto-detects best available.
