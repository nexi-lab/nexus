# Unified Kernel Lock Architecture

**Status:** Active — lock architecture SSOT

---

## 1. Lock Inventory

| What | Where | Latency | Scope |
|------|-------|---------|-------|
| **LockManager** (I/O + advisory) | `rust/kernel/src/lock_manager.rs` | ~200ns (I/O) / ~5μs (advisory local) / ~5-10ms (advisory Raft) | I/O: local condvar. Advisory: local or Raft-distributed |
| **VFSSemaphore** | `rust/kernel/src/semaphore.rs` | ~200ns Rust | Local, holder-tracked counting semaphore |
| ~12 `asyncio.Semaphore` | scattered | — | Ad-hoc concurrency bounding |

### 1.1 POSIX Mapping

| Nexus | POSIX Equivalent |
|-------|-----------------|
| LockManager (I/O mode) | `i_rwsem` (inode RW semaphore) |
| LockManager (advisory mode) | `flock(2)` advisory lock |
| VFSSemaphore | `sem_t` (named counting semaphore + TTL) |

---

## 2. Kernel Primitives

### 2.1 LockManager — I/O serialization

`rust/kernel/src/lock_manager.rs`. Pure Rust, condvar-based. ~200ns.
Wired into every mutating syscall via `Kernel::sys_write`/`sys_read`:

```
// sys_write (Rust kernel, simplified)
lock_manager.io_lock(path, LockMode::Write)?;  // condvar wait
backend.write_content(data)?;
metastore.put(metadata)?;
lock_manager.io_unlock(path);
// Event emission AFTER lock release (like Linux inotify after i_rwsem)
dispatch_observers(FileEvent { ... });
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

Advisory locks and I/O locks are **fundamentally different concerns**.

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
│  I/O Lock (Rust LockManager)         │  Advisory Lock (Rust LockManager)    │
├──────────────────────────────────────┼──────────────────────────────────────┤
│  Condvar-based per-path RW lock      │  VFSSemaphore / Raft — redb storage  │
│  ~200ns, sync                        │  ~5μs standalone / ~ms Raft          │
│  Process-scoped (crash → released)   │  TTL-based (expire → released)       │
│  Kernel-internal (sys_read/write)    │  User/service-facing (coordination)  │
│  Metadata-invisible                  │  Metadata-visible, queryable         │
└──────────────────────────────────────┴──────────────────────────────────────┘
```

**Fingerprint**: Advisory locks require `ttl > 0` (mandatory, prevents orphans).
I/O locks have no TTL (kernel manages lifecycle in try/finally).

**Restart behavior**: Advisory locks survive in redb. Dead holders stop renewing →
TTL expires → auto-released. No orphans.

### 3.3 Kernel Ownership Model

```
// Rust Kernel::new() creates LockManager (kernel owns)
lock_manager: Arc<LockManager>,  // local VFSSemaphore by default

// Federation: auto-upgrade at DLC mount time when Raft handle provided
// Kernel::add_mount() with raft_backend → lock_manager.upgrade_to_distributed()
```

Rust `LockManager` owns both I/O and advisory lock state. Federation upgrade
happens automatically when `sys_setattr(DT_MOUNT)` provides a `ZoneConsensus`
handle — no Python factory wiring needed.

Exposed via kernel syscalls:
- `sys_lock(path, lock_id=None)` — acquire (lock_id=None) or extend TTL (lock_id=existing)
- `sys_unlock(path, lock_id=None, force=False)` — release by lock_id or force-release all holders
- `sys_stat(path, include_lock=True)` — lock state query (zero cost when False)
- `sys_readdir("/__sys__/locks/")` — list active locks (virtual namespace)
- `lock_acquire(path, ...)` — Tier 2 dict wrapper for gRPC Call RPC
- `lock()`, `locked()` — Tier 2 blocking wait / async context manager

| Profile | Metastore | LockManager mode |
|---------|-----------|-----------------|
| minimal / embedded | redb | local (VFSSemaphore) |
| lite / full | redb | local (VFSSemaphore) |
| cloud / federation | redb + Raft | distributed (auto-upgrade at mount) |
| remote | RemoteMetastore | None (server-side) |

---

## 4. Summary

| Primitive | Location | Latency | Visibility | TTL | Scope |
|-----------|----------|---------|------------|-----|-------|
| LockManager (I/O) | `rust/kernel/src/lock_manager.rs` | ~200ns | Kernel-internal | No | Local (condvar) |
| LockManager (advisory) | `rust/kernel/src/lock_manager.rs` | ~5μs / ~5-10ms | User-facing (sys_lock) | Yes | Local or Raft-distributed |
| VFSSemaphore | `rust/kernel/src/semaphore.rs` | ~200ns | Kernel-authored stdlib | Yes | Local |

---

## 5. Design Decisions

**D1: Two locks, not one** — I/O lock (VFSLockManager, kernel-internal, ~200ns) and
advisory lock (user-facing, TTL-based) are fundamentally different.
Like Linux `i_rwsem` vs `flock(2)`.

**D2: Advisory locks are metadata** — stored in redb `sm_locks` table (separate from
FileMetadata), queryable, Raft-replicated in federation. Like HDFS leases in NameNode.

**D3: Kernel-owned, not service-owned** — Rust `Kernel::new()` constructs `LockManager`.
Federation auto-upgrades via `upgrade_to_distributed()` at DLC mount time when
Raft handle is provided — no Python factory wiring needed.
Exposed via kernel syscalls: `sys_lock`/`sys_unlock` (Tier 1), `lock_acquire`/`lock()`/`locked()` (Tier 2).
Lock info via `sys_stat(include_lock=True)`, lock listing via `sys_readdir("/__sys__/locks/")`.

**D4: No backend-level locking** — CAS metadata RMW uses `VFSSemaphore` directly.

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
| L1 | VFS I/O locks | `rust/kernel/src/lock_manager.rs` | ~200ns (Rust condvar) |
| L2 | Advisory/Raft locks | `rust/kernel/src/lock_manager.rs` | ~5μs (local) / ~5-10ms (Raft) |
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

**References:** `rust/kernel/src/lock_manager.rs`, `rust/kernel/src/kernel.rs` (write path lock scope),
`rust/kernel/src/dispatch.rs` (observer dispatch), `lib/lock_order.py` (assertions),
DFUSE paper: https://arxiv.org/abs/2503.18191
