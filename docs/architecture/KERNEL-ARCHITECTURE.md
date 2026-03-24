# Nexus Kernel Architecture

**Status:** Active — kernel architecture SSOT
**Rule:** Keep this file small and precise. Prefer inplace edits over additions.
Delegate details to `federation-memo.md` and `data-storage-matrix.md`.

---

## 1. Design Philosophy

NexusFS follows an **OS-inspired layered architecture**.

```
┌──────────────────────────────────────────────────────────────┐
│  SERVICES (user space)                                       │
│  Installable/removable. ReBAC, Auth, Agents, Scheduler, etc. │
└──────────────────────────────────────────────────────────────┘
                          ↓ protocol interface
┌──────────────────────────────────────────────────────────────┐
│  KERNEL                                                      │
│  Minimal compilable unit. VFS, MetastoreABC,                 │
│  ObjectStoreABC interface definitions.                       │
└──────────────────────────────────────────────────────────────┘
                          ↓ dependency injection
┌──────────────────────────────────────────────────────────────┐
│  DRIVERS                                                     │
│  Pluggable at startup. redb, S3, LocalDisk, gRPC, etc.       │
└──────────────────────────────────────────────────────────────┘
```

### Interface Taxonomy

Every kernel interface belongs to exactly one of four categories:

```
        ┌──────────────────────────────────────────────────┐
        │               Users / AI / Agents                │
        └──────────────┬───────────────────────────────────┘
                       │  ↑ USER CONTRACT (§2)
                       │    NexusFilesystemABC, 11 sys_*,
                       │    Tier 2 convenience, Hook Reg API
        ┌──────────────┴───────────────────────────────────┐
        │               KERNEL                             │
        │  ┌─────────────────────────────────────────────┐ │
        │  │  PRIMITIVES — internal (§4)                 │ │
        │  │  VFSRouter, VFSLockManager,                │ │
        │  │  KernelDispatch, PipeManager, StreamManager,│ │
        │  │  FileEvent, ServiceRegistry                │ │
        │  └─────────────────────────────────────────────┘ │
        └──────────────┬───────────────────────────────────┘
                       │  ↓ HAL — DRIVER CONTRACT (§3)
                       │    MetastoreABC, ObjectStoreABC,
                       │    CacheStoreABC
        ┌──────────────┴───────────────────────────────────┐
        │               DRIVERS                            │
        │  redb, S3, LocalDisk, Dragonfly, PostgreSQL      │
        └──────────────────────────────────────────────────┘

        ── Kernel-Authored Standards (§5) ──────────────────
           RecordStoreABC, 40+ Service Protocols
           Defined by kernel, NOT owned by kernel.
```

| Category | Direction | Audience | Kernel relationship |
|----------|-----------|----------|---------------------|
| **User Contract** (§2) | ↑ upward | Users, AI, agents, services extending syscalls | Kernel **implements** |
| **HAL — Driver Contract** (§3) | ↓ downward | Driver implementors | Kernel **requires** |
| **Kernel Primitive** (§4) | internal | Kernel-internal only | Kernel **owns** |
| **Kernel-Authored Standard** (§5) | sideways | Services | Kernel **defines** but doesn't own |

### Swap Tiers

Follows Linux's monolithic kernel model, not microkernel:

| Tier | Swap time | Nexus | Linux analogue |
|------|-----------|-------|----------------|
| Static kernel | Never | MetastoreABC, VFS `route()`, syscall dispatch | vmlinuz core (scheduler, mm, VFS) |
| Drivers | Config-time (DI at startup) | redb, S3, PostgreSQL, Dragonfly, SearchBrick | compiled-in drivers (`=y`) |
| Services | Init-time DI + runtime hot-swap | 40+ protocols (ReBAC, Mount, Auth, Agents, Search, Skills, ...) | loadable kernel modules (`insmod`/`rmmod`) |

**Invariant:** Services depend on kernel interfaces, never the reverse.
The kernel operates with zero services loaded. Kernel code (`core/nexus_fs.py`)
has **zero reads** of `_system_services` attributes — all service wiring flows
through factory-injected closures (`functools.partial`) or KernelDispatch hooks.

**Drivers** use constructor DI at startup — same binary, different config
(`NEXUS_METASTORE=redb`, `NEXUS_RECORD_STORE=postgresql`). Immutable after init.

### Service Lifecycle

`factory/` acts as the init system (like systemd): creates selected services
and injects them via DI. Different distros select different service sets at
startup — `nexus-server` loads all 22+, MINIMAL profile loads zero.

Factory boot sequence (6 phases, strictly ordered):

| Phase | Name | Side effects | Key actions |
|-------|------|-------------|-------------|
| 1 | `create_nexus_services()` | None | Build 3-tier service containers (Kernel/System/Brick) |
| 2 | `NexusFS()` constructor | None | Kernel primitives only (MetastoreABC, VFSRouter, KernelDispatch, PipeManager, StreamManager, AgentRegistry, ServiceRegistry) + `init_cred` (kernel process identity, like Linux `init_task.cred`) |
| 3 | `link()` | None (memory only) | Wire service topology via `_do_link()`. `functools.partial` bakes `system_services` into closures — kernel never stores the reference for reads |
| 4 | `initialize()` | None | Register VFS hooks (INTERCEPT + OBSERVE), IPC adapter bind |
| 5 | `bootstrap()` | Yes (I/O, threads) | `mark_bootstrapped()` → auto-start PersistentServices, activate HotSwappable hooks |
| 6 | Runtime | Yes | Syscalls live, hooks fire, observers emit |

Services needing kernel syscalls declare `KERNEL_DEPS` in `brick_factory.py`;
`ServiceRegistry` resolves via kernel symbol table (`EXPORT_SYMBOL()` pattern).
`DeploymentProfile` gates which bricks are constructed (see §7).

#### Service Lifecycle Protocols

Two `@runtime_checkable` protocols classify services into a 2×2 matrix.
Services satisfy the contract by implementing the methods — no inheritance
required (structural typing).

```
                      On-demand                Persistent-required
                 ┌─────────────────────┬─────────────────────────┐
  Restart-req.   │ Q1: register only   │ Q3: auto start()/stop() │
                 │ (SearchService)     │ (EventDeliveryWorker)   │
                 ├─────────────────────┼─────────────────────────┤
  HotSwappable   │ Q2: auto hooks +   │ Q4: hooks + activate +  │
                 │     activate()      │     start()/stop()      │
                 │ (ReBACService)      │ (future)                │
                 └─────────────────────┴─────────────────────────┘
```

| Protocol | Methods | Kernel auto-manages |
|----------|---------|---------------------|
| `HotSwappable` | `hook_spec()`, `drain()`, `activate()` | Hook registration into KernelDispatch + activate on bootstrap; drain + unregister on shutdown |
| `PersistentService` | `start()`, `stop()` | `start()` on bootstrap (dependency order); `stop()` on shutdown (reverse order) |

One-click contract: implement protocol → `ServiceRegistry.enlist()` →
kernel handles the rest. `ServiceRegistry` (kernel-owned, lifecycle integrated)
scans the registry and auto-calls the appropriate methods during
`NexusFS.bootstrap()` / `NexusFS.close()`.

**Service → Kernel wiring pattern:** Factory captures service references in
`functools.partial` closures (same pattern as `_brick_on`, `_parse_fn`).
The kernel receives injected callables/sentinels — never reads service
containers directly. Example: `flush_write_observer` uses
`_flush_write_observer_fn` (closure over `write_observer.flush()`), not
`_system_services.write_observer`.

**Source of truth:** `contracts/protocols/service_lifecycle.py`

### Entry Point: `connect()`

`connect(config=...)` is the **mode-dispatcher factory function** — the single
entry point for all Nexus users. It auto-detects deployment mode
(standalone/remote/federation), bootstraps the appropriate stack, and returns
`NexusFilesystemABC`.

```python
from nexus.sdk import connect
nx = connect()                    # auto-detect from env/config
nx = connect(config={"profile": "remote", "url": "http://..."})
```

Linux analogue: the boot sequence that selects rootfs and mounts it
(`mount_root()` in `init/do_mounts.c`). After `connect()` returns, you have a
usable filesystem. All three modes return the same `NexusFilesystemABC` contract
— clients never need to know which mode is running.

Not DI — it's the user-facing entry point. The factory/DI machinery is internal.

---

## 2. User Contract — Syscall Interface

**Category:** User Contract (↑) | **Audience:** Users, AI, agents | **Package:** `contracts.filesystem`, `core.nexus_fs`

### 2.1 NexusFilesystemABC — Published Contract

The published user-facing contract is `NexusFilesystemABC` (in `contracts/filesystem/`):

| Tier | Content | Caller responsibility |
|------|---------|----------------------|
| **Tier 1 (abstract)** | 11 `sys_*` kernel syscalls | Implementors MUST override |
| **Tier 2 (concrete)** | Convenience methods composing Tier 1 | Inherit — no override needed |

Relationship: POSIX spec (contract) vs Linux kernel (implementation) — clients
program against the contract, kernel implements it.

### 2.2 Kernel Syscalls — POSIX-Aligned, Path-Addressed

`NexusFS` is the kernel implementation of `NexusFilesystemABC`. It wires
primitives (§4) into user-facing operations. NexusFS contains **no service
business logic**.

**11 kernel syscalls**, all POSIX-aligned, all path-addressed:

| Plane | Syscalls |
|-------|----------|
| **Metadata** (9) | `sys_stat`, `sys_setattr`, `sys_mkdir`, `sys_rmdir`, `sys_readdir`, `sys_access`, `sys_rename`, `sys_unlink`, `sys_is_directory` |
| **Content** (2) | `sys_read` (pread), `sys_write` (pwrite) |

**Syscall × Primitive usage matrix:**

| Syscall | VFSRouter | VFSLock | KernelDispatch | Metastore | FileEvent |
|---------|-----------|---------|----------------|-----------|-----------|
| `sys_mkdir` | Yes | — | Yes (3-phase) | Yes | Yes |
| `sys_rmdir` | Yes | — | Yes (3-phase) | Yes | Yes |
| `sys_read` | Yes | Yes (shared) | Yes (3-phase) | Yes | —* |
| `sys_write` | Yes | Yes (exclusive) | Yes (3-phase) | Yes | Yes |
| `sys_unlink` | Yes | Yes (exclusive) | Yes (3-phase) | Yes | Yes |
| `sys_rename` | Yes | Yes (both, sorted) | Yes (2-phase) | Yes | Yes |
| `sys_stat` | — | — | — | Yes | — |
| `sys_access` | — | — | — | Yes | — |
| `sys_setattr` | Yes | Yes (exclusive) | — | Yes | Yes |
| `sys_readdir` | — | — | — | Yes | — |
| `sys_is_directory` | — | — | — | Yes | — |

*`sys_read` does not emit `FileEvent` (reads are not mutations).

**Bypass paths (intentional):**
- `sys_stat`, `sys_access`, `sys_is_directory`, `sys_readdir` — read-only metadata
  queries. Direct metastore lookup, no routing/locking/dispatch. Fast-path: ~5μs.
- Dynamic connectors in `sys_read` — `user_scoped=True` backends bypass VFSLock
  (external data source, no local inode to lock).

See `syscall-design.md` for full syscall table and design rationale.

### 2.3 Tier 2 Convenience Methods

Tier 2 methods compose Tier 1 syscalls — concrete implementations in `NexusFilesystemABC`:

| Half | Examples | Addressing |
|------|----------|-----------|
| **VFS half** (POSIX-aligned) | `read()`, `write()`, `stat()`, `append()`, `edit()`, `read_bulk()`, `write_batch()` | Path-addressed, delegates to `sys_*` |
| **HDFS half** (driver-level) | `read_content()`, `write_content()`, `stream()`, `stream_range()`, `write_stream()` | Hash-addressed (etag/CAS), direct to ObjectStoreABC |

The HDFS half bypasses path resolution and metadata lookup — CAS is a driver
detail. Like HDFS separates ClientProtocol (NameNode, path-based) from
DataTransferProtocol (DataNode, block-based). The metadata layer above ensures
etag ownership and zone isolation.

### 2.4 Syscall Extension Model (VFS Dispatch)

The kernel provides callback-based dispatch at 6 VFS operation points (read,
write, delete, rename, mkdir, rmdir). These are kernel-owned callback lists
(implemented by `KernelDispatch`, §4) that any authorized caller populates.

**Three-phase dispatch per VFS operation:**

| Phase | Semantics | Short-circuit? | Linux Analogue |
|-------|-----------|----------------|----------------|
| **PRE-DISPATCH** | First-match short-circuit | Yes (skips pipeline) | VFS `file->f_op` dispatch (procfs, sysfs) |
| **INTERCEPT** | Synchronous, ordered (pre + post) | Yes (abort/policy) | LSM security hooks |
| **OBSERVE** | Fire-and-forget | No | `fsnotify()` / `notifier_call_chain()` |

**PRE-DISPATCH** (Issue #889): `VFSPathResolver` instances checked in order;
first match handles entire operation. Each resolver owns its own permission
semantics.

**INTERCEPT**: Per-operation hook lists (`VFS*Hook` protocols, one per syscall).
Hooks receive a typed context dataclass, can modify context or abort. PRE hooks
are synchronous. POST hooks support both sync (serial, fault-isolated) and async
(parallel with timeout) — classified at registration by Rust `HookRegistry`.
Audit is a factory-registered interceptor, not a kernel built-in.

**OBSERVE**: `VFSObserver` instances receive frozen `FileEvent` (§4.3) on all
mutations. Used for cache invalidation, workflow triggers, telemetry.
Failures logged, never abort.

All 9 hook protocols + 7 context dataclasses defined in `contracts/vfs_hooks.py`
(tier-neutral). Concrete implementations live in `services/hooks/` (policy,
like SELinux/AppArmor).

### 2.5 Hook Registration API

User Contract for extending syscall behavior at runtime. Each of the three
dispatch phases has a symmetric `register_*()` / `unregister_*()` pair:

| Phase | Pattern | Count |
|-------|---------|-------|
| PRE-DISPATCH | `register_resolver()` / `unregister_resolver()` | 1 pair |
| INTERCEPT | `register_intercept_{op}()` / `unregister_intercept_{op}()` | 7 pairs (one per hookable syscall) |
| OBSERVE | `register_observe()` / `unregister_observe()` | 1 pair |

Like Linux's `register_kprobe()` / `security_add_hooks()`, these are
**runtime-callable** — any authorized caller (factory, service, user, agent)
can register and unregister hooks dynamically.

### 2.6 Mediation Principle

Users access HAL only through syscalls. Primitives (§4) mediate all
user→HAL interaction:

```
User call                Kernel Primitives               HAL Driver
─────────                ─────────────────               ──────────
nx.sys_write(path, buf)
  │
  ├─→ KernelDispatch.resolve_write()   [PRE-DISPATCH: short-circuit?]
  ├─→ VFSRouter.route(path)            [path → backend + backend_path]
  ├─→ KernelDispatch.intercept_pre_*() [permission, policy hooks]
  ├─→ VFSLockManager.acquire(write)    [exclusive lock]
  │     │
  │     ├─→ Backend.write_content(buf)  ← HAL call
  │     ├─→ MetastoreABC.put(metadata)  ← HAL call
  │     │
  ├─→ VFSLockManager.release()         [lock released]
  ├─→ KernelDispatch.intercept_post_*() [audit, cache update]
  └─→ KernelDispatch.notify(FileEvent) [OBSERVE: fire-and-forget]
```

**Exception:** Tier 2 hash-addressed operations (see §2.3 HDFS half) access
ObjectStoreABC directly by etag, bypassing path resolution and metadata lookup.

---

## 3. HAL — Storage Driver Contracts

**Category:** HAL — Driver Contract (↓) | **Audience:** Driver implementors

NexusFS abstracts storage by **Capability** (access pattern + consistency guarantee),
not by domain or implementation.

| Pillar | ABC | Capability | Kernel Role | Package |
|--------|-----|------------|-------------|---------|
| **Metastore** | `MetastoreABC` | Ordered KV, CAS, prefix scan, optional Raft SC | **Required** — sole kernel init param | `core.metastore` |
| **ObjectStore** | `ObjectStoreABC` (= `Backend`) | Streaming I/O, immutable blobs, petabyte scale | **Interface only** — instances mounted via `nx.mount()` | `core.object_store` |
| **CacheStore** | `CacheStoreABC` | Ephemeral KV, Pub/Sub, TTL | **Optional** — defaults to `NullCacheStore` | `contracts.cache_store` |

**Orthogonality:** Between pillars = different query patterns. Within pillars =
interchangeable drivers (deployment-time config). See `data-storage-matrix.md`.

**Kernel self-inclusiveness:** Kernel boots with **1 pillar** (Metastore).
ObjectStore mounted post-init. Kernel does NOT need: JOINs, FK, vector search,
TTL, pub/sub (all service-layer). Like Linux: kernel defines VFS + block device
interface but doesn't ship a filesystem.

### 3.1 MetastoreABC — Inode Layer

**Linux analogue:** `struct inode_operations`

The typed contract between VFS and storage. Without it, the kernel cannot
describe files. Operations: O(1) KV (get/put/delete), ordered prefix scan
(list), batch ops, implicit directory detection. System config stored under
`/__sys__/` prefix.

Data type: `FileMetadata` — path, backend_name, etag, size, version, zone_id,
owner_id, timestamps, mime_type. Always tagged with `zone_id` (P0 invariant).

### 3.2 ObjectStoreABC (= Backend) — Blob I/O

**Linux analogue:** `struct file_operations`

CAS-addressed blob storage: read/write/delete by etag (content hash), plus
streaming variants. Directory ops (mkdir/rmdir/list_dir) for backends that
support them. Rename is optional (capability-dependent).

### 3.3 CacheStoreABC — Ephemeral KV + Pub/Sub (Optional)

**Linux analogue:** `/dev/shm` + message bus

The only **optional** HAL pillar. Kernel defines the ABC (ephemeral KV + pub/sub);
services consume it for caching, event fan-out, and session storage.
Drivers: Dragonfly/Redis (production), `InMemoryCacheStore` (dev).

**Graceful degradation:** `NullCacheStore` (no-op) is the default. Without a real
CacheStore, EventBus disables, permission/tiger caches fall back to RecordStore,
and sessions stay in RecordStore. No kernel functionality is lost.

### 3.4 Dual-Axis ABC Architecture

Two independent ABC axes, composed via DI:

- **Data ABCs** (this section): WHERE is data stored? → 3 kernel pillars by storage capability
- **Ops ABCs** (§5.3): WHAT can users/agents DO? → 40+ scenario domains by ops affinity

A concrete class sits at the intersection: e.g. `ReBACManager` implements
`PermissionProtocol` (Ops) and internally uses `RecordStoreABC` (Data).
See `ops-scenario-matrix.md` for full proof.

---

## 4. Kernel Primitives

**Category:** Kernel Primitive (internal) | **Audience:** Kernel-internal | **Package:** `core.*`

Primitives mediate between user-facing syscalls and HAL drivers. Users interact
with them indirectly through syscalls. See §2.2 matrix for per-syscall usage.

| Primitive | Package | Linux Analogue | Role |
|-----------|---------|---------------|------|
| **VFSRouter** | `core.protocols.vfs_router` | VFS `lookup_slow()` | `route(path)` → `ResolvedPath` (backend, backend_path, mount_point). ~5μs redb lookup. Resolution only — mount CRUD is `MountProtocol` (service) |
| **VFSLockManager** | `core.lock_fast` | per-inode `i_rwsem` | Per-path read/write lock with hierarchy-aware conflict detection. Details in §4.1 |
| **KernelDispatch** | `core.kernel_dispatch` | `security_hook_heads` + `fsnotify` | Three-phase callback mechanism implementing §2.4. Rust `PathTrie` (O(depth) resolver routing) + Rust `HookRegistry` (cached sync/async classification). Per-op callback lists; empty = zero overhead |
| **PipeManager + RingBuffer** | `system_services` + `core.pipe` | `pipe(2)` + `fs/pipe.c` | VFS named pipes — inode in MetastoreABC, data in heap ring buffer. Details in §4.2 |
| **StreamManager + StreamBuffer** | `system_services` + `core.stream` | append-only log | VFS named streams — inode in MetastoreABC, data in heap linear buffer. Non-destructive offset-based reads, multi-reader fan-out. Details in §4.2 |
| **ServiceRegistry** | `core.service_registry` | `init/main.c` + `module.c` | Kernel-owned symbol table + lifecycle orchestration (enlist/swap/shutdown). Manages all 4 service quadrants — subsumes former ServiceLifecycleCoordinator |
| **DriverLifecycleCoordinator** | `core.driver_lifecycle_coordinator` | `register_filesystem` + `kern_mount` | Manages driver mount lifecycle: routing table + VFS hook registration + mount/unmount KernelDispatch notification. Orthogonal to ServiceRegistry lifecycle (drivers vs services) |
| **AgentRegistry** | `core.agent_registry` | `task_struct` list | In-memory agent process table. Kernel-owned, created at `__init__`. Details in §4.4 |
| **FileEvent** | `core.file_events` | `fsnotify_event` | Immutable mutation records. Details in §4.3 |

### 4.1 VFSLockManager — Per-Path RW Lock

| Property | Value |
|----------|-------|
| Modes | `"read"` (shared) / `"write"` (exclusive) |
| Hierarchy awareness | Ancestor/descendant conflict detection |
| Latency | ~200ns (Rust PyO3) / ~500ns–1μs (Python fallback) |
| Scope | In-memory, process-scoped (crash → released), metadata-invisible |
| Lock release timing | Released BEFORE observers (like Linux inotify after i_rwsem) |

**Advisory locks** are a separate concern — see `lock-architecture.md` §4.

### 4.2 IPC Primitives — Named Pipes & Streams

Two-layer architecture for both: VFS metadata (inode) in MetastoreABC, data
(bytes) in process heap buffer (like Linux `kmalloc`'d pipe buffer).

| Primitive  | Linux Analogue    | Buffer         | Read          |
|------------|-------------------|----------------|---------------|
| DT_PIPE    | `kfifo` ring      | RingBuffer     | Destructive   |
| DT_STREAM  | append-only log   | StreamBuffer   | Non-destructive (offset-based) |

**DT_PIPE (PipeManager + RingBuffer):**

- **PipeManager (mkpipe)** — VFS named pipe lifecycle (created via `sys_setattr`
  upsert, read/write via `sys_read`/`sys_write`, destroyed via `sys_unlink`),
  per-pipe lock for MPMC safety. Reads are destructive (consumed on read).
- **RingBuffer (kpipe)** — Lock-free **SPSC** kernel primitive (`kfifo` analogue),
  no internal synchronization. PipeManager wraps with per-pipe `asyncio.Lock`
  for **MPMC** safety. Direct RingBuffer access is kernel-internal only.

**DT_STREAM (StreamManager + StreamBuffer):**

- **StreamManager (mkstream)** — VFS named stream lifecycle (same syscall
  surface as mkpipe). Per-stream lock for concurrent writers. Reads are
  non-destructive — multiple readers maintain independent byte offsets (fan-out).
- **StreamBuffer (kstream)** — Linear append-only buffer. Monotonic tail, no
  wrap-around. Primary use case: LLM streaming I/O (realtime first consumer +
  replay for later consumers).

See `federation-memo.md` §7j for design rationale.

### 4.3 FileEvent / FileEventType — Immutable Mutation Records

| Property | Value |
|----------|-------|
| Event types | `FILE_WRITE`, `FILE_DELETE`, `FILE_RENAME`, `METADATA_CHANGE`, `DIR_CREATE`, `DIR_DELETE`, `SYNC_*`, `CONFLICT_*` |
| Structure | Frozen dataclass: path, etag, size, version, zone_id, agent_id, user_id, vector_clock |
| Consumer paths | KernelDispatch OBSERVE (local), EventBus (distributed) |
| Emission point | Always AFTER lock release |

### 4.4 AgentRegistry — Kernel Process Table

| Property | Value |
|----------|-------|
| Linux analogue | `task_struct` list (`for_each_process()`) |
| Package | `core.agent_registry` |
| Storage | In-memory dict (process heap) — no persistence |
| Lifecycle | Created in `NexusFS.__init__()`, closed via factory close callback |

The AgentRegistry is the kernel's process table — an in-memory registry of all
active agent descriptors (spawn, status, close). Like Linux's `task_struct`,
it is kernel-owned infrastructure that services consume but never create.

**Why kernel-owned (Issue #1792):** AgentRegistry was previously created in the
system-services boot tier and injected via `SystemServices.agent_registry`.
This caused a layering violation: the kernel needed to read `_system_services`
to access its own process table. Moving it to `NexusFS.__init__()` (alongside
PipeManager and StreamManager) makes it a true kernel primitive — available
before any service boots, with no upward dependency.

**Consumers:** EvictionManager, AcpService, AgentStatusResolver (all service-layer).
These are created at factory link-time (`_do_link()`) where `nx._agent_registry`
is already available.

---

## 5. Kernel-Authored Standards

**Category:** Kernel-Authored Standard (≠ kernel interface) | **Audience:** Services

### 5.1 The "Standard Plug" Principle

The kernel defines contracts it doesn't own — so kernel infrastructure works
automatically with any service that conforms.

**Linux analogies:**

| Linux pattern | What kernel defines | What modules provide | Kernel benefit |
|---------------|--------------------|--------------------|----------------|
| `file_operations` | Struct with read/write/ioctl pointers | Each filesystem fills the struct | VFS calls any filesystem uniformly |
| `security_operations` | Struct with 200+ LSM hook pointers | SELinux, AppArmor fill hooks | Security framework calls any LSM |

**Nexus equivalent:**

| Nexus pattern | What kernel defines | What services provide | Infrastructure benefit |
|---------------|--------------------|--------------------|----------------------|
| `RecordStoreABC` | Session factory + read replica interface | PostgreSQL, SQLite drivers | Services get pooling, error translation, replica routing |
| `VFS*Hook` protocols | Hook shapes (context dataclasses) | Service-layer hook implementations | KernelDispatch calls any conforming hook uniformly |
| `VFSSemaphoreProtocol` | Named counting semaphore interface | `lib.semaphore` implementation | Advisory locks + CAS coordination use uniform semaphore API |
| Service Protocols | `@runtime_checkable` typed interfaces | Concrete service implementations | Typed contracts for service implementors |

**Integration mechanisms:** Factory auto-discovers bricks via `brick_factory.py`
convention (`RESULT_KEY` + `PROTOCOL` + `create()`), validates protocol
conformance at registration, and resolves kernel dependencies via
`EXPORT_SYMBOL()` pattern (see §1 Service Lifecycle).

### 5.2 RecordStoreABC — Relational Storage Standard

**Package:** `storage.record_store` | **NOT a kernel interface — service-only**

| Property | Value |
|----------|-------|
| Kernel role | Kernel **defines** the ABC; kernel does NOT consume it |
| Consumers | Services only (ReBAC, Auth, Agents, Scheduler, etc.) |
| Interface | `session_factory` + `read_session_factory` (SQLAlchemy ORM) |
| Drivers | PostgreSQL, SQLite (interchangeable without code changes) |
| Rule | Direct SQL or raw driver access is an abstraction break |

The kernel is the standards body — it defines the interface shape that forces
driver implementors to provide pooling, error translation, read replica routing,
WAL mode, async lazy init. Both sides (drivers and services) conform to the
same interface; neither needs to know the other. The value comes from
bilateral interface conformance, not from kernel providing these features directly.

### 5.3 Service Protocols — 40+ Scenario Domains

**Package:** `contracts.protocols` | **NOT kernel interfaces — service standards**

40+ `typing.Protocol` classes with `@runtime_checkable`, organized by domain
(Permission, Search, Mount, Agent, Events, Memory, Domain, Audit, Cross-Cutting).

See `ops-scenario-matrix.md` §2–§3 for full enumeration and affinity matching.

### 5.4 VFSSemaphore — Named Counting Semaphore

**Package:** `lib.semaphore` | **Protocol:** `contracts.protocols.semaphore.VFSSemaphoreProtocol`

| Property | Value |
|----------|-------|
| POSIX analogue | `sem_t` (named semaphore, extended with TTL + holder tracking) |
| Kernel role | Kernel **defines** the protocol and provides the implementation in `lib/`; kernel does NOT own it as a primitive |
| Modes | Counting (N holders), mutex (max_holders=1) |
| Latency | ~200ns (Rust PyO3) / ~500ns-1us (Python fallback) |
| Scope | In-memory, process-scoped, TTL-based lazy expiry |
| Consumers | Advisory lock layer (`SemaphoreAdvisoryLockManager`), CAS metadata RMW |

Replaced `_StripeLock` (ad-hoc 64-stripe mutex) for CAS metadata coordination.
Advisory lock layer uses two semaphores per path for RW gate pattern
(shared/exclusive). See `lock-architecture.md` §3.

Previously lived in `core.semaphore` as a kernel primitive. Moved to `lib/`
(PR #3298) because it has no kernel dependencies and is consumed by
service-layer components — making it a kernel-authored standard, not a
kernel-owned primitive.

---

## 6. Tier-Neutral Infrastructure (`contracts/`, `lib/`)

Two packages sit **outside** the Kernel → Services → Drivers stack.
Any layer may import from them; they must **not** import from `nexus.core`,
`nexus.services`, `nexus.fuse`, `nexus.bricks`, or any other tier-specific package.

| Package | Contains | Linux Analogue | Rule |
|---------|----------|----------------|------|
| **`contracts/`** | Types, enums, exceptions, constants | `include/linux/` (header files) | Declarations only — no implementation logic, no I/O |
| **`lib/`** | Reusable helper functions, pure utilities | `lib/` (libc, libm) | Implementation allowed, but zero kernel deps |

**Core distinction:** `contracts/` = **what** (shapes of data). `lib/` = **how** (behavior).

### Placement Decision Tree

```
Is it used by a SINGLE layer?
  → Yes: stays in that layer (e.g. fuse/filters.py)
  → No (multi-layer):
       Is it a type / ABC / exception / enum / constant?
         → Yes: contracts/
         → No (function / helper / I/O logic): lib/
```

### Import Rules

`contracts/` and `lib/` may import from: each other, stdlib, third-party packages.
They must **never** import from: `nexus.core`, `nexus.services`, `nexus.server`,
`nexus.cli`, `nexus.fuse`, `nexus.bricks`, `nexus.rebac`.


---

## 7. Deployment Profiles

The kernel's layered design (§1) and DI contracts (§3) enable a range of
deployment profiles. Not kernel-owned, but kernel-enabled.

Like Linux distros select packages from the same kernel, Nexus profiles select
which bricks to enable and which drivers to inject.

| Profile | Target | Bricks | Metastore | Linux Analogue |
|---------|--------|--------|-----------|----------------|
| **minimal** | Bare minimum runnable | 1 (storage only) | redb (embedded) | initramfs |
| **embedded** | MCU, WASM (<1 MB) | 2 (storage + eventlog) | redb (embedded) | BusyBox |
| **lite** | Pi, Jetson, mobile | 8 (+namespace, agent, permissions, ...) | redb (embedded) | Alpine |
| **full** | Desktop, laptop | 21 (all except federation) | redb (embedded) | Ubuntu Desktop |
| **cloud** | k8s, serverless | 22 (all, incl. federation) | redb (Raft) | Ubuntu Server |
| **remote** | Client-side proxy | 0 (zero local bricks) | RemoteMetastore | NFS client |

Profile hierarchy: `minimal ⊂ embedded ⊂ lite ⊂ full ⊆ cloud`.
REMOTE is orthogonal — stateless proxy, all operations via gRPC to server.

Same kernel binary, different driver injection. See §1 `connect()`.
**Source of truth:** `src/nexus/contracts/deployment_profile.py`.

---

## 8. Communication

Kernel-adjacent services built on kernel primitives (§4.2 IPC, §4.3
FileEvent). Not kernel-owned, but bottom-layer infrastructure.

| Tier | Nexus | Built on | Topology |
|------|-------|----------|----------|
| **Kernel** | DT_PIPE (§4.2) | RingBuffer — destructive FIFO | Local or distributed (transparent) |
| **Kernel** | DT_STREAM (§4.2) | StreamBuffer — append-only log | Local or distributed (transparent) |
| **System** | gRPC + IPC | PipeManager/StreamManager, consensus proto | Point-to-point |
| **User Space** | EventBus | CacheStoreABC pub/sub + FileEvent (§4.3) | Fan-out (1:N) |

See `federation-memo.md` §2–§5 for gRPC/consensus details.

---

## 9. Cross-References

| Topic | Document |
|-------|----------|
| Data type → pillar mapping (50+ types) | `data-storage-matrix.md` |
| Ops ABC × scenario affinity (40+ domains) | `ops-scenario-matrix.md` |
| Syscall table and design rationale | `syscall-design.md` |
| VFS lock design + advisory locks | `lock-architecture.md` §4 |
| Zone model, DT_MOUNT, federation | `federation-memo.md` §5–§6 |
| Raft, gRPC, write flows | `federation-memo.md` §2–§5 |
| Pipe + Stream design rationale | `federation-memo.md` §7j |
