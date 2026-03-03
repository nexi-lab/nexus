# Unified Kernel Lock Architecture

**Issues**: #909, #906, #908, #910, #805
**Prerequisite**: #1323 (OCC + lock extraction from kernel write path)
**Status**: Implemented (PR #2732, #2733, #2734).

---

## 1. Lock Inventory

| What | Where | Latency | Scope |
|------|-------|---------|-------|
| **VFSLockManager** | `core/lock_fast.py` | ~200ns Rust / ~500ns Python | Local, path-level RW, hierarchical |
| **VFSSemaphore** | `core/semaphore.py` | ~200ns Rust / Python | Local, holder-tracked counting semaphore |
| **LocalLockManager** | `lib/distributed_lock.py` | ~5μs | Standalone advisory locks via MetastoreABC |
| **RaftLockManager** | `raft/lock_manager.py` | ~5-10ms | Distributed advisory locks, zone-scoped |
| **_StripeLock** | `backends/cas_blob_store.py` | ~1us | Local, CAS hash-stripe threading.Lock |
| **LockManagerBase** | `lib/distributed_lock.py` | — | ABC: async advisory lock API (zone_id bound at construction) |
| **LockStoreProtocol** | `lib/distributed_lock.py` | — | Low-level store interface (MetastoreABC lock methods) |
| ~12 `asyncio.Semaphore` | scattered | — | Ad-hoc concurrency bounding |

**Resolved** (by PR #2732, #2733, #2734):
1. ~~VFSLockManager not wired into syscalls~~ → wired into sys_read/sys_write/sys_rename/sys_unlink
2. ~~PassthroughBackend.lock() duplicates kernel logic~~ → deleted, replaced by LocalLockManager
3. ~~No local advisory lock manager~~ → LocalLockManager wraps MetastoreABC
4. ~~No local kernel semaphore~~ → VFSSemaphore (Rust + Python)

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

`core/semaphore.py`. Rust (PyO3) + Python fallback. Local kernel primitive.

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

### 3.3 DI Model

```python
# factory/_bricks.py (actual pattern)
from nexus.lib.distributed_lock import LockStoreProtocol, LocalLockManager

if isinstance(metadata_store, LockStoreProtocol):
    if dist and dist.enable_locks:
        lock_manager = RaftLockManager(metadata_store)   # federation
    else:
        lock_manager = LocalLockManager(metadata_store)  # standalone
```

Capability detection via `isinstance(store, LockStoreProtocol)` — no `supports_locks`
property needed. `LockStoreProtocol` is `@runtime_checkable`.

| Profile | Metastore | lock_manager → |
|---------|-----------|----------------|
| minimal / embedded | redb (satisfies LockStoreProtocol) | LocalLockManager |
| lite / full | redb (satisfies LockStoreProtocol) | LocalLockManager |
| cloud / federation | redb + Raft (satisfies LockStoreProtocol) | RaftLockManager |
| remote | RemoteMetastore (no lock methods) | None (server-side) |

Callers see only `LockManagerBase`. Same async API regardless of backend.

---

## 4. Summary

| Primitive | Location | Latency | Visibility | TTL | Scope |
|-----------|----------|---------|------------|-----|-------|
| VFSLockManager | `core/lock_fast.py` | ~200ns | Kernel-internal | No | Local |
| VFSSemaphore | `core/semaphore.py` | ~200ns | Kernel-internal | Yes | Local |
| LocalLockManager | `lib/distributed_lock.py` | ~5μs | Internal | Yes | Local (standalone) |
| RaftLockManager | `raft/lock_manager.py` | ~5-10ms | Internal | Yes | Distributed (zone) |
| _StripeLock | `backends/cas_blob_store.py` | ~1us | Backend-internal | No | Local (per-hash) |

---

## 5. Design Decisions

**D1: Two locks, not one** — I/O lock (VFSLockManager, kernel-internal, ~200ns) and
advisory lock (`LockStoreProtocol`, user-facing, TTL-based) are fundamentally different.
Like Linux `i_rwsem` vs `flock(2)`.

**D2: Advisory locks are metadata** — stored via `LockStoreProtocol` (redb `TREE_LOCKS`),
queryable, Raft-replicated in federation. Like HDFS leases in NameNode FSImage+EditLog.
`LockStoreProtocol` is a capability protocol — MetastoreABC does NOT own lock methods.

**D3: Factory DI, not runtime routing** — `LocalLockManager` or `RaftLockManager`
injected at boot. No `_LockRouter`, no runtime auto-detect. Simpler, testable.

**D4: PassthroughBackend.lock() deleted** — duplicated kernel lock logic.
EventsService now uses `LockManagerBase` exclusively (LocalLockManager or RaftLockManager).

**D5: asyncio.Semaphore stays as-is** — internal concurrency limiters (not advisory
locks). No names, TTL, or cross-node semantics needed.

**D6: Kernel lock mandatory, advisory lock cooperative** — sys_read/sys_write always
acquire VFSLockManager. Advisory locks are cooperative like `flock(2)`.
