# Unified Kernel Lock Architecture

**Issues**: #909, #906, #908, #910, #805
**Prerequisite**: #1323 (OCC + lock extraction from kernel write path)
**Status**: Implemented (PR #2732, #2733, #2734).

---

## 1. Lock Inventory

| What | Where | Latency | Scope |
|------|-------|---------|-------|
| **VFSLockManager** | `core/lock_fast.py` | ~200ns Rust / ~500ns Python | Local, path-level RW, hierarchical |
| **VFSSemaphore** | `lib/semaphore.py` | ~200ns Rust / Python | Local, holder-tracked counting semaphore |
| **AdvisoryLockManager** | `lib/distributed_lock.py` | — | ABC: async advisory lock API (zone_id bound at construction) |
| **LocalLockManager** | `lib/distributed_lock.py` | ~500ns–1μs | Standalone advisory locks via VFSSemaphore |
| **RaftLockManager** | `raft/lock_manager.py` | ~5-10ms | Distributed advisory locks, zone-scoped |
| ~~LockStoreProtocol~~ | deleted (Phase 2) | — | Was low-level store interface — only one implementer (RaftMetadataStore) |
| ~12 `asyncio.Semaphore` | scattered | — | Ad-hoc concurrency bounding |

**Resolved** (by PR #2732, #2733, #2734):
1. ~~VFSLockManager not wired into syscalls~~ → wired into sys_read/sys_write/sys_rename/sys_unlink
2. ~~PassthroughBackend.lock() duplicates kernel logic~~ → deleted, replaced by LocalLockManager
3. ~~No local advisory lock manager~~ → LocalLockManager wraps MetastoreABC
4. ~~No local kernel semaphore~~ → VFSSemaphore (Rust + Python)

### 1.1 POSIX Mapping

| Nexus | POSIX Equivalent |
|-------|-----------------|
| VFSLockManager | `i_rwsem` (inode RW semaphore) |
| VFSSemaphore | `sem_t` (named counting semaphore + TTL) |
| AdvisoryLockManager | `flock(2)` advisory lock ABC |
| LocalLockManager | Local `flock` via VFSSemaphore |
| RaftLockManager | Distributed `flock` via Raft |

---

## 2. Kernel Primitives

### 2.1 VFSLockManager — I/O serialization

`core/lock_fast.py`. Rust-accelerated (PyO3), Python fallback. ~200ns.
Wired into every mutating syscall:

```python
# sys_write (actual code pattern)
handle = self._vfs_acquire(path, "write")  # raises LockTimeout on failure
try:
    content_hash = backend.write_content(content, context=context).content_hash
    self.metadata.put(metadata)
finally:
    self._vfs_lock_manager.release(handle)
# Event emission AFTER lock release (like Linux inotify after i_rwsem)
self._dispatch.notify(FileEvent(type=FILE_WRITE, ...))
```

| Syscall | Lock Mode | Failure |
|---------|----------|---------|
| `sys_read` | shared (read) | Timeout → LockTimeout (HTTP 423) |
| `sys_write` | exclusive (write) | Timeout → LockTimeout |
| `sys_rename` | exclusive on both old + new (sorted order, deadlock-free) | Timeout → LockTimeout |
| `sys_unlink` | exclusive (write) | Timeout → LockTimeout |

Properties: synchronous, hierarchical (`write("/a/b")` blocks `read("/a/b/c")`),
no TTL (held for syscall duration only), not user-visible (like `i_rwsem`).

### 2.2 VFSSemaphore — holder-tracked counting semaphore

`lib/semaphore.py`. Rust (PyO3) + Python fallback. Kernel-authored standard library.

Holder-tracked: each `acquire` returns unique `holder_id`, `release` requires it.
Standard for distributed semaphores (Consul sessions, ZK ephemeral nodes).
Matches `RaftLockManager.acquire(max_holders=N)` semantics.

```python
class VFSSemaphore:
    def acquire(name, max_holders, timeout_ms=30000, ttl_ms=30000) -> str | None
    def release(name, holder_id) -> bool
    def extend(name, holder_id, ttl_ms=30000) -> bool
    def info(name) -> SemaphoreInfo | None
    def force_release(name) -> bool
```

---

## 3. Two-Lock Architecture

Through architecture review, the original LockRouter plan was rejected.
Key insight: advisory locks and I/O locks are **fundamentally different concerns**.

### 3.1 Why No Router

1. **Writes converge**: In all deployment modes (standalone, REMOTE, federation),
   writes to the same path converge to a single process. VFSLockManager
   (in-memory, ~200ns) is sufficient for I/O serialization.
2. **Advisory locks ARE metadata**: Like HDFS leases in the NameNode's
   FSImage+EditLog, advisory locks should live in the metastore — visible,
   queryable, Raft-replicated in federation, persistent with TTL cleanup.
3. **Factory DI suffices**: `factory.py` injects `LocalLockManager` (standalone)
   or `RaftLockManager` (federation). Both implement `LockManagerBase`.
   No runtime routing needed.

### 3.2 Two Locks

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

### 3.3 Kernel Ownership Model

```python
# NexusFS.__init__ creates LocalLockManager (kernel owns)
from nexus.lib.distributed_lock import LocalLockManager
from nexus.lib.semaphore import create_vfs_semaphore

self._lock_manager = LocalLockManager(create_vfs_semaphore(), zone_id=ROOT_ZONE_ID)

# Federation: RaftLockManager upgrade at link time (kernel knows)
_raft_lm = RaftLockManager(nx.metadata, zone_id=zone_id)
nx._upgrade_lock_manager(_raft_lm)
```

Same pattern as FileWatcher: kernel-owned local + kernel-knows remote.
Exposed via kernel syscalls: `sys_lock`, `sys_unlock`, `lock()` (Tier 2 blocking wait),
`locked()` (Tier 2 async context manager).

| Profile | Metastore | lock_manager → |
|---------|-----------|----------------|
| minimal / embedded | redb | LocalLockManager |
| lite / full | redb | LocalLockManager |
| cloud / federation | redb + Raft | RaftLockManager |
| remote | RemoteMetastore | None (server-side) |

Callers see only `AdvisoryLockManager`. Same async API regardless of backend.

---

## 4. Summary

| Primitive | Location | Latency | Visibility | TTL | Scope |
|-----------|----------|---------|------------|-----|-------|
| VFSLockManager | `core/lock_fast.py` | ~200ns | Kernel-internal | No | Local |
| VFSSemaphore | `lib/semaphore.py` | ~200ns | Kernel-authored stdlib | Yes | Local |
| LocalLockManager | `lib/distributed_lock.py` | ~500ns–1μs | Internal | Yes | Local (standalone) |
| RaftLockManager | `raft/lock_manager.py` | ~5-10ms | Internal | Yes | Distributed (zone) |

---

## 5. Design Decisions

**D1: Two locks, not one** — I/O lock (VFSLockManager, kernel-internal, ~200ns) and
advisory lock (user-facing, TTL-based) are fundamentally different.
Like Linux `i_rwsem` vs `flock(2)`.

**D2: Advisory locks are metadata** — stored in redb `sm_locks` table (separate from
FileMetadata), queryable, Raft-replicated in federation. Like HDFS leases in NameNode.

**D3: Kernel-owned, not service-owned** — NexusFS.__init__ constructs LocalLockManager.
Federation upgrades to RaftLockManager via `_upgrade_lock_manager()` at link time.
Same pattern as FileWatcher (kernel-owned local + kernel-knows remote).
Exposed via kernel syscalls: `sys_lock`/`sys_unlock` (Tier 1), `lock()`/`locked()` (Tier 2).

**D4: PassthroughBackend.lock() deleted** — duplicated kernel lock logic.
`_StripeLock` also deleted — CAS metadata RMW now uses `VFSSemaphore` directly.

**D5: asyncio.Semaphore stays as-is** — internal concurrency limiters (not advisory
locks). No names, TTL, or cross-node semantics needed.

**D6: Kernel lock mandatory, advisory lock cooperative** — sys_read/sys_write always
acquire VFSLockManager. Advisory locks are cooperative like `flock(2)`.

**D7: Advisory lock supports shared/exclusive modes** — RW gate pattern via two
VFSSemaphore instances per path (one for shared, one for exclusive). Matches
`flock(2)` LOCK_SH/LOCK_EX semantics.

---

## 6. Lock Ordering (Issue #3392)

**Motivation:** DFUSE (arXiv:2503.18191) §4.2 — deadlock from reversed lock
ordering in distributed filesystem I/O. Document and enforce Nexus's lock
hierarchy before a similar bug manifests.

### 6.1 Global Ordering Rule

Nexus has four lock layers. **A task that holds a higher-numbered lock must
NEVER acquire a lower-numbered lock.**

```
L1 (VFS I/O)  →  L2 (Advisory/Raft)  →  L3 (asyncio)  →  L4 (threading)
```

| Layer | Lock | Location | Typical Latency |
|-------|------|----------|-----------------|
| L1 | VFS I/O locks | `core/lock_fast.py` | ~200ns (Rust) / ~500ns (Python) |
| L2 | Advisory/Raft locks | `lib/distributed_lock.py`, `raft/lock_manager.py` | ~5μs (local) / ~5-10ms (Raft) |
| L3 | asyncio primitives | pipes, streams, asyncio.Semaphore | ~1μs |
| L4 | threading locks | `file_watcher.py` `_waiters_lock`, `semaphore.py` `_mu` | ~1μs |

### 6.2 Permitted Acquisition Orders

- **VFS → Metadata (L1 → metastore):** Standard write path — VFS lock protects
  both backend write and metadata put.
- **VFS → VFS (L1 → L1):** Rename acquires two VFS locks in **sorted path order**
  to prevent circular wait.
- **Observer → threading.Lock (L3 → L4):** Observer dispatch runs after VFS lock
  release. Safe to acquire threading locks.

### 6.3 Forbidden Patterns

- **Advisory Lock → VFS Lock (L2 → L1) ❌** — Exact DFUSE deadlock pattern.
- **Observer → VFS/Advisory Lock (L3/L4 → L1) ❌** — Observers run post-release; re-acquiring creates cycle.
- **Threading Lock → VFS Lock (L4 → L1) ❌** — Short-lived internal locks must not block on I/O.

### 6.4 Safety Mechanism: Phase Separation

VFS lock is **always** released before event dispatch (same pattern as Linux
`i_rwsem` release before `fsnotify()`). This prevents DFUSE-style deadlocks
without runtime ordering checks.

### 6.5 Debug Assertions

`NEXUS_DEBUG_LOCK_ORDER=1` enables per-task lock acquisition tracking at runtime:
- Acquiring L1 while holding L2 raises `LockOrderError`
- Acquiring L1/L2 from observer context raises `LockOrderError`

See `lib/lock_order.py` for implementation.

### 6.6 DFUSE Lesson

DFUSE found that normal I/O acquires `inode lock → lease lock`, but lease
revocations acquire `lease lock → inode lock`. Nexus equivalent:
`inode lock` ≈ VFSLockManager (L1), `lease lock` ≈ RaftLockManager (L2).

**References:** `core/lock_fast.py`, `core/nexus_fs.py` (write path lock scope),
`core/kernel_dispatch.py` (observer dispatch), `lib/lock_order.py` (assertions),
DFUSE paper: https://arxiv.org/abs/2503.18191
