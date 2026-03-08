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
        │  │  PathValidator, ZoneAccessGuard, VFSRouter, │ │
        │  │  VFSLockManager, KernelDispatch,            │ │
        │  │  PipeManager, FileEvent                     │ │
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
| Services | Init-time DI (Phase 1); runtime hot-swap planned (Phase 2) | 40+ protocols (ReBAC, Mount, Auth, Agents, Search, Skills, ...) | loadable kernel modules (`insmod`/`rmmod`) |

**Invariant:** Services depend on kernel interfaces, never the reverse.
The kernel operates with zero services loaded.

**Drivers** use constructor DI at startup — same binary, different config
(`NEXUS_METASTORE=redb`, `NEXUS_RECORD_STORE=postgresql`). Immutable after init.

### Service Lifecycle

**Phase 1 — Init-time DI (current).** `factory/` acts as the init system
(like systemd): creates selected services and injects them via DI.
Different distros select different service sets at startup — `nexus-server`
loads all 22+, `nexus-embedded` loads zero.

Factory boot sequence (3 tiers, strictly ordered):

| Tier | Name | When | What gets built | Depends on |
|------|------|------|-----------------|------------|
| 0 | KERNEL | First | `NexusFS` + kernel primitives | MetastoreABC (sole required param) |
| 1 | SYSTEM | After kernel | Critical services (ReBAC, Audit, Permissions) | Kernel + storage pillars |
| 2 | BRICK | After system | Auto-discovered bricks (`nexus/bricks/*/brick_factory.py`) | Kernel + system services |

Services needing kernel syscalls declare `KERNEL_DEPS` in `brick_factory.py`;
`ServiceRegistry` resolves via kernel symbol table (`EXPORT_SYMBOL()` pattern).
`DeploymentProfile` gates which bricks are constructed (see §7).

**Phase 2 — Runtime hot-swap (planned).** `ServiceRegistry` will manage services
following the Loadable Kernel Module pattern: lifecycle protocol
(`init`→`start`→`stop`→`cleanup`), dependency graph enforcement, reference
counting, and hook auto-registration/unregistration at load/unload time.

### Entry Point: `connect()`

`connect(config=...)` is the **mode-dispatcher factory function** — the single
entry point for all Nexus users. It auto-detects deployment mode
(standalone/remote/federation), bootstraps the appropriate stack, and returns
`NexusFilesystemABC`.

```python
from nexus.sdk import connect
nx = connect()                    # auto-detect from env/config
nx = connect(config={"mode": "remote", "url": "http://..."})
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
Hooks receive a typed context dataclass, can modify context or abort. Audit is a
factory-registered interceptor, not a kernel built-in.

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
| **KernelDispatch** | `core.kernel_dispatch` | `security_hook_heads` + `fsnotify` | Three-phase callback mechanism implementing §2.4. Per-op callback lists; empty = zero overhead. Hook contracts (§2.4) and registration API (§2.5) are User Contract; this is the plumbing |
| **PipeManager + RingBuffer** | `system_services` + `core.pipe` | `pipe(2)` + `fs/pipe.c` | VFS named pipes — inode in MetastoreABC, data in heap ring buffer. Details in §4.2 |
| **PathValidator** | `core.nexus_fs` (to extract) | `fs/namei.c` path validation | Path format validation on every syscall entry. Rejects malformed paths before routing or HAL access |
| **ZoneAccessGuard** | `core.nexus_fs` (to extract) | `fs/namespace.c` mount readonly | Zone write permission check on every mutating syscall. Rejects writes to read-only zones before routing |
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

### 4.2 PipeManager + RingBuffer — Named Pipes

Two-layer architecture: VFS metadata (inode) in MetastoreABC, data (bytes) in
process heap ring buffer (like Linux `kmalloc`'d pipe buffer).

- **PipeManager** — VFS named pipe lifecycle (`mkpipe` / `destroy` / `pipe_read`),
  per-pipe lock for MPMC safety
- **RingBuffer** — Lock-free SPSC kernel primitive (`kfifo` analogue), GIL-atomic.
  PipeManager wraps with `asyncio.Lock` for MPMC

See `federation-memo.md` §7j for design rationale.

### 4.3 FileEvent / FileEventType — Immutable Mutation Records

| Property | Value |
|----------|-------|
| Event types | `FILE_WRITE`, `FILE_DELETE`, `FILE_RENAME`, `METADATA_CHANGE`, `DIR_CREATE`, `DIR_DELETE`, `SYNC_*`, `CONFLICT_*` |
| Structure | Frozen dataclass: path, etag, size, version, zone_id, agent_id, user_id, vector_clock |
| Consumer paths | KernelDispatch OBSERVE (local), EventBus (distributed), Layer 1 inotify/FSEvents |
| Emission point | Always AFTER lock release |


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

Kernel-adjacent services built on kernel primitives (PipeManager §4.2,
FileEvent §4.3). Not kernel-owned, but bottom-layer infrastructure.

| Tier | Nexus | Built on | Topology |
|------|-------|----------|----------|
| **Kernel** | Native Pipe (§4.2) | RingBuffer (kernel primitive) | Intra-process |
| **System** | gRPC + IPC | PipeManager, consensus proto | Point-to-point |
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
| Pipe design rationale | `federation-memo.md` §7j |
