# Global Lock Ordering

**Issue**: #3392
**Motivation**: DFUSE (arXiv:2503.18191) §4.2 — deadlock from reversed lock
ordering in distributed filesystem I/O. Document and enforce Nexus's lock
hierarchy before a similar bug manifests.

---

## 1. Lock Layers

Nexus has four lock layers. The **global ordering rule** is:

```
L1 (VFS I/O)  →  L2 (Advisory/Raft)  →  L3 (asyncio)  →  L4 (threading)
```

**A task that holds a higher-numbered lock must NEVER acquire a lower-numbered lock.**

| Layer | Lock | Location | Typical Latency |
|-------|------|----------|-----------------|
| L1 | VFS I/O locks | `core/lock_fast.py` | ~200ns (Rust) / ~500ns (Python) |
| L2 | Advisory/Raft locks | `lib/distributed_lock.py`, `raft/lock_manager.py` | ~5μs (local) / ~5-10ms (Raft) |
| L3 | asyncio primitives | pipes, streams, asyncio.Semaphore | ~1μs |
| L4 | threading locks | `file_watcher.py` `_waiters_lock`, `semaphore.py` `_mu` | ~1μs |

---

## 2. Permitted Acquisition Orders

### 2.1 VFS → Metadata (L1 → metastore)

The standard write path. VFS I/O lock protects both backend write and
metadata put:

```python
with self._vfs_locked(path, "write"):       # L1
    backend.write_content(...)               # I/O
    metadata.put(...)                        # metastore
# Lock released — event dispatch below (L3/L4)
await dispatch.notify(event)
```

### 2.2 VFS → VFS (L1 → L1, sorted order)

Rename acquires two VFS locks. **Sorted path order prevents circular wait**:

```python
_first, _second = sorted([old_path, new_path])
_h1 = self._vfs_acquire(_first, "write")    # L1 (first)
_h2 = self._vfs_acquire(_second, "write")   # L1 (second)
```

### 2.3 Observer → threading.Lock (L3 → L4)

Observer dispatch runs after VFS lock release. Observers may acquire
threading locks (e.g. FileWatcher `_waiters_lock`):

```python
# KernelDispatch.notify() — runs AFTER VFS lock released
async def on_mutation(event):
    with self._waiters_lock:                 # L4 — safe
        for w in self._waiters:
            ...
```

---

## 3. Forbidden Patterns

### 3.1 Advisory Lock → VFS Lock (L2 → L1) ❌

**NEVER** acquire a VFS I/O lock while holding an advisory lock.
This is the exact deadlock pattern DFUSE identified (lease lock → inode lock
while normal I/O does inode lock → lease lock).

```python
# FORBIDDEN — will trigger debug assertion
lock_id = await advisory_lock_manager.acquire(path)  # L2
with self._vfs_locked(path, "write"):                 # L1 ← DEADLOCK RISK
    ...
```

### 3.2 Observer Holding Lock → Dispatch (L3/L4 → L1) ❌

**NEVER** acquire a VFS lock or advisory lock from within an observer
callback (`on_mutation`). Observers run in the OBSERVE phase, after
VFS locks are released. Re-acquiring would create a cycle:

```
sys_write: VFS lock → release → notify → observer → VFS lock (cycle!)
```

### 3.3 Threading Lock → VFS Lock (L4 → L1) ❌

**NEVER** acquire a VFS lock while holding `_waiters_lock` or
`VFSSemaphore._mu`. These are short-lived internal locks that must
not block on I/O-latency operations.

---

## 4. Lock Scoping Rules

### 4.1 VFS Locks — Path-Scoped, Hierarchical

- Write to `/a/b` blocks read/write to `/a/b/c` (descendants)
- Write to `/a/b` blocks write to `/a` (ancestors)
- Multiple readers allowed; exclusive with writers
- No TTL — held for syscall duration only (try/finally)
- Process-scoped — crash releases all locks

### 4.2 Advisory Locks — Zone-Scoped

- `zone_id` bound at construction (`LocalLockManager`, `RaftLockManager`)
- Lock keys are `{zone_id}:{path}` — never cross zones
- TTL-based (default 30s) — prevents orphan locks on crash
- `RaftLockManager` must NOT be called from observer context

### 4.3 Threading Locks — Micro-Scoped

- `FileWatcher._waiters_lock`: protects waiter list only (append/iterate/remove)
- `VFSSemaphore._mu`: protects holder map only
- Critical sections must be sub-microsecond — no I/O, no awaits

---

## 5. Phase Separation (Current Safety Mechanism)

Nexus avoids DFUSE-style deadlocks through **phase separation**:

```
ACQUIRE VFS LOCK (L1)
    │
    ├── Backend I/O (write_content)
    ├── Metadata update (metadata.put)
    │
RELEASE VFS LOCK
    │
    ├── OBSERVE phase: KernelDispatch.notify()
    │   ├── FileWatcher.on_mutation() — resolves futures (L4)
    │   ├── RevisionTrackingObserver.on_mutation() — metadata (no locks)
    │   └── EventBusObserver.on_mutation() — publish (deferred, no locks)
    │
    └── INTERCEPT post-phase: intercept_post_write()
```

The VFS lock is **always** released before event dispatch. This is the
same pattern as Linux `i_rwsem` release before `fsnotify()`.

---

## 6. Debug Assertions

When `NEXUS_DEBUG_LOCK_ORDER=1` is set, Nexus tracks lock acquisition per-task
and asserts ordering constraints at runtime:

- **Layer ordering**: acquiring L1 while holding L2 raises `LockOrderError`
- **Observer context**: acquiring L1 or L2 from a task tagged as
  `_nexus_observer=True` raises `LockOrderError`

See: `lib/lock_order.py` for implementation.

Enable in development/CI:

```bash
NEXUS_DEBUG_LOCK_ORDER=1 pytest tests/
```

---

## 7. DFUSE Lesson

DFUSE (arXiv:2503.18191) found that normal I/O acquires `inode lock → lease lock`,
but lease revocations acquire `lease lock → inode lock`. This reversed ordering
causes deadlock under concurrent I/O + revocation. DFUSE solved it by embedding
both locks in the kernel with a consistent acquisition order.

Nexus's equivalent mapping:
- `inode lock` ≈ VFSLockManager (L1)
- `lease lock` ≈ RaftLockManager (L2)

Current safety: phase separation (VFS released before observers).
Future safety: debug assertions enforce ordering if phase separation is
accidentally broken by a code change.

---

## 8. References

- `core/lock_fast.py` — VFS lock manager
- `core/nexus_fs.py` — write path lock scope, rename sorted-order
- `core/kernel_dispatch.py` — observer dispatch (OBSERVE phase)
- `lib/lock_order.py` — debug-mode ordering assertions
- `raft/lock_manager.py` — RaftLockManager
- `lib/distributed_lock.py` — advisory lock ABC + LocalLockManager
- `core/file_watcher.py` — FileWatcher (threading.Lock for waiters)
- `docs/architecture/lock-architecture.md` — two-lock architecture overview
- DFUSE paper: https://arxiv.org/abs/2503.18191
