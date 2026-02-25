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

**Kernel minimality:** The kernel is the minimal compilable unit — it cannot run alone
(like Linux's vmlinuz needs bootloader + init). It defines interfaces; drivers provide
implementations via DI at startup.

**Three swap tiers** (follows Linux's monolithic kernel model, not microkernel):

| Tier | Swap time | Nexus | Linux analogue |
|------|-----------|-------|----------------|
| Static kernel | Never | MetastoreABC, VFS `route()`, syscall dispatch | vmlinuz core (scheduler, mm, VFS) |
| Drivers | Config-time (DI at startup) | redb, S3, PostgreSQL, Dragonfly, SearchBrick | compiled-in drivers (`=y`) |
| Services | Runtime (load/unload) | 23 protocols (ReBAC, Mount, Auth, Agents, Search, Skills, ...) | loadable kernel modules (`insmod`/`rmmod`) |

**Invariant:** Services depend on kernel interfaces, never the reverse.
The kernel operates with zero services loaded.

**Drivers** use constructor DI at startup — same binary, different config
(`NEXUS_METASTORE=redb`, `NEXUS_RECORD_STORE=postgresql`). Immutable after init.

**Services** have two maturity phases, both preserving the invariant above:

**Phase 1 — Init-time DI (distro composition).** `factory.py` acts as the init
system (like systemd): creates selected services and injects them via
`KernelServices` dataclass. Different distros select different service sets at
startup — `nexus-server` loads all 22+, `nexus-embedded` loads zero.

> *Resolved (Issue #643):* `factory.py` gates all services via `DeploymentProfile` +
> `enabled_bricks` frozenset (see §5.1). `_wire_services()` migrated to
> `factory._boot_wired_services()` — NexusFS constructor no longer imports or creates
> services. Two-phase init: `NexusFS(...)` → `_boot_wired_services(nx, ...)` →
> `nx._bind_wired_services(dict)`.

**Phase 2 — Runtime hot-swap (Linux LKM model).** A `ServiceRegistry` manages
in-process service modules following the Loadable Kernel Module pattern:

- **Lifecycle protocol**: `service_init()` → `service_start()` → `service_stop()` →
  `service_cleanup()`, plus `service_name` and `service_dependencies` declarations
- **Capability registration**: services register the Protocols they implement
  (like LKMs call `register_filesystem()` or `register_chrdev()`)
- **Dependency graph**: `load_service()` rejects when dependencies missing;
  `unload_service()` rejects when dependents still loaded
- **Reference counting**: prevents unloading while callers hold references

Why LKM, not systemd? Nexus services are **in-process** components (shared memory,
zero IPC overhead), not separate daemon processes. LKMs have the same property —
in-kernel modules that register capabilities with subsystems.

> *Gap:* No `ServiceRegistry`, no lifecycle protocol, no `load_service()`/`unload_service()`.
> Path: extract remaining mixins → standalone service classes (in progress) →
> introduce `ServiceRegistry` with LKM lifecycle.

---

## 2. The Four Storage Pillars

NexusFS abstracts storage by **Capability** (access pattern + consistency guarantee),
not by domain or implementation.

| Pillar | ABC | Capability | Kernel Role |
|--------|-----|------------|-------------|
| **Metastore** | `MetastoreABC` | Ordered KV, CAS, prefix scan, optional Raft SC | **Required** — sole kernel init param |
| **ObjectStore** | `ObjectStoreABC` (= `Backend`) | Streaming I/O, immutable blobs, petabyte scale | **Interface only** — instances mounted dynamically |
| **RecordStore** | `RecordStoreABC` | Relational ACID, JOINs, FK, vector search | **Services only** — optional, injected for ReBAC/Auth/etc. |
| **CacheStore** | `CacheStoreABC` | Ephemeral KV, Pub/Sub, TTL | **Optional** — kernel defines ABC, services consume; defaults to `NullCacheStore` |

**Orthogonality:** Between pillars = different query patterns. Within pillars = interchangeable
drivers (deployment-time config). See `data-storage-matrix.md` for full proof.

### Kernel Self-Inclusiveness

Kernel compiles and inits with **1 pillar** (Metastore). ObjectStore is mounted post-init.
Like Linux: kernel defines VFS + block device interface but doesn't ship a filesystem.

| Kernel need | Source |
|-------------|--------|
| File metadata (inode) | MetastoreABC — KV by path |
| Directory index (dentry) | MetastoreABC — ordered prefix scan |
| System settings, zone tracking | MetastoreABC — `/__sys__/` KV entries |
| File content (bytes) | ObjectStoreABC — mounted via `nx.mount()`, not init param |

Kernel does NOT need: JOINs, FK, vector search, TTL, pub/sub (all service-layer).

### CacheStore Graceful Degradation

No CacheStore → EventBus disabled, PermissionCache falls back to RecordStore,
TigerCache O(n), UserSession stays in RecordStore. `NullCacheStore` provides no-op impl.

### RecordStoreABC Usage Pattern

Services consume `RecordStoreABC.session_factory` + SQLAlchemy ORM.
Direct SQL or raw driver access is an abstraction break.
This ensures driver interchangeability (PostgreSQL ↔ SQLite) without code changes.

### Dual-Axis ABC Architecture

Two independent ABC axes, composed via DI:

- **Data ABCs** (this section): WHERE is data stored? → 4 pillars by storage capability
- **Ops ABCs** (§3): WHAT can users/agents DO? → 29 scenario domains by ops affinity

A concrete class sits at the intersection: e.g. `ReBACManager` implements `PermissionProtocol`
(Ops) and internally uses `RecordStoreABC` (Data). The Protocol itself has no storage opinion.
See `ops-scenario-matrix.md` for full Ops-Scenario affinity proof.

---

## 3. Kernel Interfaces & Primitives

### Kernel Interfaces (`nexus.core`)

| Interface | Linux Analogue | Purpose |
|-----------|---------------|---------|
| `MetastoreABC` | `struct inode_operations` | Typed FileMetadata CRUD (the inode layer) |
| `VFSRouterProtocol` | VFS `lookup_slow()` | Path resolution only — mount CRUD lives in Service `MountProtocol` |
| `ObjectStoreABC` (= `Backend`) | `struct file_operations` | Blob I/O interface (read/write/delete/list) |
| `CacheStoreABC` | (no direct analogue) | Ephemeral KV + Pub/Sub primitives |
| `VFSLockManagerProtocol` | per-inode `i_rwsem` | Path-level RW locking with hierarchy awareness |
| `PipeManagerProtocol` | `pipe(2)` + `fs/pipe.c` | Named pipe lifecycle + MPMC data path (see §6 Kernel Tier) |
| Notification dispatch | `security_hook_heads` + `fsnotify` | Two-phase callback lists at VFS operation points (see §3 Notification Dispatch) |

`MetastoreABC` is kernel because it IS the inode layer — the typed contract
between VFS and storage. Without it, the kernel cannot describe files.

`VFSLockManager` (`core/lock_fast.py`) provides rwsem semantics with hierarchical
ancestor/descendant conflict detection. Rust-accelerated (PyO3), Python fallback.
Distinct from service-layer advisory locking (LockProtocol / `ops-scenario-matrix.md` S9).

> **Gap:** VFSLockManager is created in `NexusFS.__init__` but not yet wired into the
> write path. Intent: local coroutine concurrency lock, complementing the distributed
> RaftLockManager — like Linux `i_rwsem` (local) coexisting with `flock(2)` (distributed).

### NexusFS — Syscall Dispatch Layer

`NexusFS` is the kernel entry point, analogous to Linux's syscall layer (`sys_open`,
`sys_read`). It wires VFSRouter + MetastoreABC + ObjectStoreABC into
user-facing operations (read, write, list, mkdir, mount). NexusFS contains
**no service business logic** — services are accessed through `ServiceRegistry`
(Phase 2) or thin delegation stubs (Phase 1).

`NexusFSCoreMixin` contains the VFS operation implementations (like `vfs_read`,
`vfs_write` in Linux), inherited by NexusFS. This is an implementation detail —
a Python mixin used to split the large NexusFS class. As services continue
extracting, the mixin should shrink to pure VFS ops and eventually evolve from
mixin (inheritance) to composition (standalone `VFSCore` class).

`factory.py` is the init system (analogous to systemd): constructs drivers
+ services and wires them together. NexusFS creates its own kernel
infrastructure (dispatch, locks, pipes) with empty callback lists, and
receives external dependencies (drivers, services) via constructor DI.
Factory registers callbacks into kernel-owned infrastructure at boot —
like Linux `security_init()` creates empty `security_hook_heads`, then
LSM modules call `security_add_hooks()` to populate them.

> *Resolved:* Event mixins fully extracted — `NexusFSEventsMixin` removed (#573),
> `FileWatcher` moved to `services/watch/` (#706), orphaned kernel attrs cleaned (#656).
> `_wire_services()` deleted — all service creation moved to `factory._boot_wired_services()` (#643).
> Remaining: replace `KernelServices` dataclass with `ServiceRegistry`.

### Kernel Notification Dispatch

The kernel provides callback-based notification at VFS operation points (read,
write, delete, rename, mkdir, rmdir). Like Linux's `security_hook_heads` and
`fsnotify_group`, these are kernel-internal callback lists.

**Decision (Issue #625):** Kernel **owns** dispatch infrastructure — creates
`KernelDispatch` with empty callback lists at init. Factory **registers**
callbacks into kernel-owned dispatch at boot. Empty lists = no-op dispatch
= kernel operates with zero services.

Two-phase dispatch per VFS operation:

| Phase | Semantics | Abort? | Modify? | Linux Analogue | Mechanism |
|-------|-----------|--------|---------|----------------|-----------|
| **INTERCEPT** | Synchronous, ordered | Yes (hook policy) | Yes (hooks) | LSM `call_void_hook()` | `KernelDispatch.intercept_post_*()` |
| **OBSERVE** | Fire-and-forget | No | No | `fsnotify()` / `notifier_call_chain()` | `KernelDispatch.notify()` |

**Implementation** (`core/kernel_dispatch.py`, Issue #900):

`KernelDispatch` is a single class that owns both phases. Each VFS operation
(read/write/delete/rename/mkdir/rmdir) calls `intercept_post_*()` then `notify()`.

INTERCEPT phase dispatches registered interceptor hooks — per-operation
hook lists (`register_intercept_read/write/delete/rename/mkdir/rmdir`).
Hooks can modify context (e.g. filter CSV columns, update cache bitmaps).
The audit write observer is a factory-registered interceptor, not a kernel
built-in; its error policy (abort vs log-and-continue) is observer-level
config, not dispatch-level.

OBSERVE phase broadcasts a frozen `FileEvent` to all registered
`VFSObserver` instances. `FileEvent` is the single kernel-defined I/O
event type — used by both OBSERVE (local, fire-and-forget) and EventBus
(distributed delivery). Analogous to Linux `fsnotify_event`.
Used for cache invalidation, workflow triggers, telemetry.
Failures logged, never abort.

**Contracts:**
- `FileEvent`/`FileEventType` in `core/file_events.py` (kernel-defined data type).
- Hook protocols (`VFSReadHook`, `VFSWriteHook`, etc.), context dataclasses
  (`ReadHookContext`, `WriteHookContext`, etc.), `VFSObserver` — in
  `contracts/vfs_hooks.py` (tier-neutral, like `include/linux/notifier.h`).
- Concrete implementations in `services/hooks/` (policy, like SELinux/AppArmor).

**Registration API** (factory registers at boot):
- `dispatch.register_intercept_read(hook)` — INTERCEPT hooks (per-operation)
- `dispatch.register_observe(observer)` — OBSERVE observers (all mutations)

**Distinction from HookEngineProtocol (S15/P17):** The kernel notification
dispatch is an internal mechanism — always-on infrastructure that dispatches
at operation points. `HookEngineProtocol` is the service-layer API for
plugin/user hook registration (like netfilter userspace config) — an optional
service brick that sits above kernel dispatch.

### Service Protocols (`nexus.services.protocols`)

29 scenario domains mapped to Ops ABCs. 23 Protocols exist, 9 gaps remain.

| Category | Protocols | Count |
|----------|-----------|-------|
| **Permission & Visibility** | PermissionProtocol, NamespaceManagerProtocol | 2 |
| **Search & Content** | SearchProtocol, SearchBrickProtocol (driver), LLMProtocol | 3 |
| **Mount & Storage** | MountProtocol, ShareLinkProtocol, OAuthProtocol | 3 |
| **Agent Infra** | AgentRegistryProtocol, SchedulerProtocol | 2 |
| **Events & Hooks** | EventLogProtocol, HookEngineProtocol, WatchProtocol, LockProtocol | 4 |
| **Domain Services** | SkillsProtocol, PaymentProtocol | 2 |
| **Missing (9 gaps)** | Version, Memory, Trajectory, Delegation, Governance, Reputation, OperationLog, Plugin, Workflow | 9 |

All use `typing.Protocol` with `@runtime_checkable`.
See `ops-scenario-matrix.md` §2–§3 for full enumeration and affinity matching.

---

## 3.1. Tier-Neutral Layers (`contracts/`, `lib/`)

Two packages sit **outside** the Kernel → Services → Drivers stack.
Any layer may import from them; they must **not** import from `nexus.core`,
`nexus.services`, `nexus.fuse`, `nexus.bricks`, or any other tier-specific package.

| Package | Contains | Linux Analogue | Rule |
|---------|----------|----------------|------|
| **`contracts/`** | Types, enums, exceptions, constants | `include/linux/` (header files) | Declarations only — no implementation logic, no I/O |
| **`lib/`** | Reusable helper functions, pure utilities | `lib/` (libc, libm) | Implementation allowed, but zero kernel deps |

**Core distinction:** `contracts/` = **what** (shapes of data). `lib/` = **how** (behavior).
When you see `from nexus.contracts import X` you know X is a lightweight type/exception
with near-zero deps. `from nexus.lib import Y` means Y is a function that *does* something.

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

### What Goes Where — Examples

| Module | Destination | Reason |
|--------|-------------|--------|
| `OperationContext`, `Permission` (type defs) | `contracts/types.py` | Type declarations |
| `NexusError`, `BackendError` (exceptions) | `contracts/exceptions.py` | Exception hierarchy |
| `Base`, `TimestampMixin` (ORM base/mixins) | `lib/db_base.py` | Schema helpers with implementation (uuid gen, server_default) |
| `EmailList`, `ISODateTimeStr` (Pydantic Annotated) | `lib/validators.py` | Annotated types with validation logic |
| `get_database_url()` (env var resolution) | `lib/env.py` | Implementation helper |
| `path_matches_pattern()` (glob matching) | `lib/path_utils.py` | Pure utility function |
| `PathInterner`, `SegmentedPathInterner` (string interning) | `lib/path_interner.py` | Generic utility (like `lib/string.c` in Linux) |
| `is_os_metadata_file()` (OS file filter) | `fuse/filters.py` | Single-layer (FUSE only) |

---

## 4. Zone

A Zone is the **fundamental isolation and consensus unit** in NexusFS.

**What a Zone determines:**
- **Data isolation:** Each zone has its own independent redb database (no shared metadata)
- **Consensus boundary:** 1 Zone = 1 Raft group (consistency guarantees scope)
- **Visibility:** Only nodes participating in a zone can see its metadata
- **Scalability unit:** Zones scale horizontally; adding zones adds capacity without coordination

**What a Zone does NOT determine:**
- **Permissions:** Read/write access controlled by ReBAC (service layer), not zone membership
- **User identity:** Authentication and user management are services, not zone concerns
- **File content location:** ObjectStore (S3, local disk) is independent of zone topology

**Operations:**
- Mount = create new zone, all participants are equal Voters (no Learner asymmetry)
- `DT_MOUNT` entries in Metastore compose zones into a namespace tree (NFS-style)
- DNS-style hierarchical discovery — each zone only knows direct children, no global registry

See `federation-memo.md` §5–§6 for implementation details.

---

## 5. Deployment Modes

### 5.1 Deployment Profiles (Distro)

Like Linux distros (Ubuntu, Alpine, BusyBox) select which packages to include from
the same kernel, Nexus **deployment profiles** select which bricks to enable from
the same codebase. Two orthogonal axes:

- **Mode** = network topology (standalone, client-server, federation)
- **Profile** = feature set (which bricks are enabled)

| Profile | Target | Bricks | Linux Analogue |
|---------|--------|--------|----------------|
| **minimal** | Bare minimum runnable (Issue #2194) | 1 (storage only) | initramfs |
| **embedded** | MCU, WASM (<1 MB) | 2 (storage + eventlog) | BusyBox |
| **lite** | Pi, Jetson, mobile (512 MB-4 GB) | 8 (+namespace, agent, permissions, cache, ipc, scheduler) | Alpine |
| **full** | Desktop, laptop (4-32 GB) | 21 (all except federation) | Ubuntu Desktop |
| **cloud** | k8s, serverless (unlimited) | 22 (all) | Ubuntu Server |
| **remote** | Client-side proxy (Issue #844) | 0 (zero local bricks) | NFS client |

Profile hierarchy: `minimal ⊂ embedded ⊂ lite ⊂ full ⊆ cloud`

REMOTE is orthogonal — not in the hierarchy. It has zero local bricks because all
operations proxy to a remote server via `RemoteBackend`. The client runs the same
NexusFS kernel with `RemoteMetastore` (stateless proxy to server SSOT) + `PathRouter`
(local path resolution) + `RemoteBackend` mounted at `/`. Same class, different components.

**Mechanism:** `factory.py` (the init system) resolves the active profile via
`NEXUS_PROFILE` env var -> `DeploymentProfile` enum -> `resolve_enabled_bricks()`
-> `frozenset[str]`. Each service in the 3-tier boot (`_boot_kernel_services`,
`_boot_system_services`, `_boot_brick_services`) checks brick membership before
construction. Individual brick overrides via `FeaturesConfig` YAML always win over
profile defaults.

**Source of truth:** `src/nexus/contracts/deployment_profile.py` (22 canonical brick names,
6 profile-to-brick mappings, `resolve_enabled_bricks()` merge function).

### 5.2 Network Modes

| Mode | Description | Metastore | Services |
|------|-------------|-----------|----------|
| **Standalone** | Single process, local storage | redb (local) | Optional |
| **Remote** | NexusFS(profile=REMOTE) with RemoteBackend | RemoteMetastore (stateless proxy) | Zero (server-side) |
| **Federation** | Multiple nodes sharing zones via Raft | redb (Raft) | Per-node |

Remote mode uses the same NexusFS class as standalone — not a separate `RemoteNexusFS`.
`RemoteMetastore` is a stateless proxy — all metadata queries go directly to the server
(SSOT), no local cache or invalidation. `PathRouter` resolves mount paths locally,
actual I/O goes to the server via `RemoteBackend`.
This is the NFS-client model: same VFS kernel, remote storage backend.

Driver selection is config-time: same binary, different `NEXUS_METASTORE`, `NEXUS_RECORD_STORE`, etc.

---

## 6. Communication

### Messaging Tiers

Three tiers, mirroring Linux's kernel → system → user space communication:

| Tier | Linux Analogue | Nexus | Latency | Topology |
|------|---------------|-------|---------|----------|
| **Kernel** | `kfifo` ring buffer | Nexus Native Pipe (`DT_PIPE`, MetastoreABC) | ~5μs | Intra-process |
| **System** | `sendmsg()` / Unix sockets / POSIX MQ | gRPC (consensus) + IPC (agent messaging) | ~0.5–1ms | Point-to-point (1:1) |
| **User Space** | `dbus-daemon` / Netlink | EventBus (CacheStoreABC pub/sub) | ~1–5ms | Fan-out (1:N) |

**Selection rule:** Consensus write path → System (gRPC, 1:1). Agent-to-agent messaging → System (IPC, 1:1 queue). Notification read path → User Space (EventBus, 1:N fan-out to 100s of observers). Internal signaling → Kernel (Pipe, zero-copy).

### Kernel Tier: Native Pipes

Two-layer pipe architecture (matches Linux `kfifo` + `fs/pipe.c`):

```
┌─────────────────────────────────────────┐
│ PipeManager (core/pipe_manager.py)      │  ← fs/pipe.c: VFS named pipe
│   mkpipe() / destroy() / pipe_read()   │     lifecycle, per-pipe lock (MPMC)
│   DT_PIPE inode in MetastoreABC        │
├─────────────────────────────────────────┤
│ RingBuffer (core/pipe.py)              │  ← kfifo: kernel-internal SPSC
│   write_nowait() / read_nowait()       │     deque + asyncio.Event pair
│   Process heap memory (no pillar)      │
└─────────────────────────────────────────┘
```

- **Inode** (DT_PIPE FileMetadata) in MetastoreABC — VFS path visibility, ReBAC, observability
- **Data** (bytes in ring buffer) in process heap — like Linux `kmalloc`'d pipe buffer
- **SPSC → MPMC**: RingBuffer is lock-free SPSC (GIL-atomic). PipeManager wraps with
  per-pipe `asyncio.Lock` for MPMC safety using lock→try→unlock→wait→retry (deadlock-free).

Services depend on `PipeManagerProtocol` (defined in `core/pipe_manager.py`,
matching `VFSLockManagerProtocol` pattern). Kernel creates the concrete `PipeManager`.

See `federation-memo.md` §7j for Pipe design rationale.

### System Tier

gRPC for consensus (Raft node-to-node, zone API) and Exchange (agent-to-agent value exchange).
IPC for agent messaging — 1:1 queue semantics using VFS as transport.

> **SSOT:** Proto files in `proto/` define all RPC services. See `federation-memo.md` §2–§5.
> IPC details in `ops-scenario-matrix.md` S29.

### User Space Tier: EventBus

`EventBusProtocol` (service protocol in `nexus.services.event_bus.protocol`) provides
pub/sub for file system change notifications. Kernel defines only the event data types
(`FileEvent`, `FileEventType` in `nexus.core.file_events`).

Linux analogue: `dbus-daemon` (1:N broadcast). Consumed by `WatchProtocol` (S8) and
`EventLogProtocol` (S17). Backends: Redis/Dragonfly (current default), NATS JetStream
(preferred long-term). All should route through `CacheStoreABC` pub/sub.
Target: EventBus delivery registered as `VFSObserver` on `KernelDispatch`, replacing
`_publish_file_event()` direct calls (#969).

**Federation gap:** EventBus is currently zone-local. Cross-zone event propagation not yet designed.

---

## Cross-References

| Topic | Document |
|-------|----------|
| Data type → pillar mapping (50+ types) | `data-storage-matrix.md` |
| Storage orthogonality proof | `data-storage-matrix.md` §ORTHOGONALITY |
| Ops ABC × scenario affinity (29 domains, 23 protocols) | `ops-scenario-matrix.md` |
| Ops ABC orthogonality + gap analysis | `ops-scenario-matrix.md` §2–§3 |
| Raft, gRPC, write flows | `federation-memo.md` §2–§5 |
| Zone model, DT_MOUNT | `federation-memo.md` §5–§6 |
| SC vs EC consistency | `federation-memo.md` §4.1 |
| API privilege levels (agents vs ops vs admin) | `federation-memo.md` §6.10 |
