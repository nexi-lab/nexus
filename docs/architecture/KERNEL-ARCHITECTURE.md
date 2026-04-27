# Nexus Kernel Architecture

Kernel architecture SSOT. Keep small and precise — prefer inplace edits over
additions. Delegate details to `federation-memo.md` and `data-storage-matrix.md`.

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

| Category | Direction | Audience | Kernel relationship | API tier |
|----------|-----------|----------|---------------------|----------|
| **User Contract** (§2) | ↑ upward | Users, AI, agents, services | Kernel **implements** | Tier 1: Syscalls (`sys_*`) |
| **HAL — Driver Contract** (§3) | ↓ downward | Driver implementors | Kernel **requires** | Tier 2: 3 pillar ABCs |
| **Kernel Primitive** (§4) | internal | Kernel-internal only | Kernel **owns** | Tier 3: Kernel Module API (`create_from_backend`, `register_resolver`) |
| **Kernel-Authored Standard** (§5) | sideways | Services | Kernel **defines** but doesn't own | — (service standards, not kernel API) |

Tier 1 is the only user-facing interface. Tier 3 is for trusted kernel modules
(federation resolvers, ACP) — analogous to Linux `EXPORT_SYMBOL`.

### Swap Tiers

Follows Linux's monolithic kernel model, not microkernel:

| Tier | Swap time | Nexus | Syscall | Linux analogue |
|------|-----------|-------|---------|----------------|
| Static kernel | Never | MetastoreABC, VFS `route()`, syscall dispatch | — | vmlinuz core (scheduler, mm, VFS) |
| Drivers | Runtime mount/unmount | redb, S3, PostgreSQL, Dragonfly, SearchBrick | `sys_setattr(DT_MOUNT)` / `rmdir` | `mount`/`umount` |
| Services | Runtime register/swap/unregister | 40+ protocols (ReBAC, Mount, Auth, Agents, Search, Skills, ...) | `sys_setattr("/__sys__/services/X")` / `sys_unlink` | `insmod`/`rmmod` |

**Invariant:** Services depend on kernel interfaces, never the reverse.
The kernel operates with zero services loaded. Kernel code (`core/nexus_fs.py`)
has zero reads of service containers — all service wiring flows through
`ServiceRegistry` (`nx.service("name")`), factory-injected closures
(`functools.partial`), or KernelDispatch hooks. Services flow through `sys_setattr("/__sys__/services/X")` — factory
uses the same syscall API as runtime callers (factory = first user).

**Drivers** are mounted at runtime via `sys_setattr(entry_type=DT_MOUNT, backend=...)`,
unmounted via `rmdir`. MetastoreABC is the only startup-time driver (sole
kernel init param). Other drivers are mounted post-init by factory or at runtime.

### Service Lifecycle

`factory/` acts as the init system (like systemd): creates selected services
and injects them via DI. `DeploymentProfile` gates which bricks are constructed
(see §7).

Factory boot sequence:

1. **`create_nexus_services()`** — `_boot_pre_kernel_services()` + `_boot_independent_bricks()` + `_boot_dependent_bricks()`
2. **`NexusFS()` constructor** — Instantiate kernel primitives (no I/O, `router` passed directly)
3. **`_wire_services()`** — Wire topology, boot post-kernel services, enlist into ServiceRegistry
4. **`_initialize_services()`** — Register VFS hooks, IPC adapter bind

See `factory/orchestrator.py` for implementation.

#### Service Lifecycle Protocols

One-dimension model: the only user-facing lifecycle dimension is
**background vs on-demand** (`BackgroundService` protocol). Hook management
uses duck-typed `hook_spec()` — the kernel auto-captures hooks via
`hasattr(instance, 'hook_spec')` at `enlist()` time.

| Mechanism | Methods | Kernel auto-manages |
|-----------|---------|---------------------|
| `BackgroundService` protocol | `start()`, `stop()` | `start()` on bootstrap (dependency order); `stop()` on shutdown (reverse order) |
| Duck-typed `hook_spec()` | `hook_spec()` → `HookSpec` | Hook registration into KernelDispatch at `enlist()` time; unregister at shutdown |

One-click contract: implement protocol / `hook_spec()` →
`ServiceRegistry.enlist()` → kernel handles the rest. `ServiceRegistry`
(kernel-owned, lifecycle integrated) scans the registry and auto-calls
the appropriate methods during `NexusFS.bootstrap()` / `NexusFS.close()`.
Rust `ServiceRegistry` calls `start()/stop()` via `asyncio.run()` (Python
stdlib only, zero nexus bridge imports).

`swap_service()` supports all services. Unified path: refcount drain → unhook
old → replace → rehook new.

**AgentRegistry** (`nexus.services.agents.agent_registry`): service-tier
agent lifecycle manager. Mounts under `nx.service("agent_registry")`.
State (pid → AgentState + condvar wakeup) lives in the Rust `services::agent_table::AgentTable`
SSOT (`rust/services/src/agent_table.rs`); the Python service is a thin
shim that adds OS behavior — PID allocation, parent/child tree, signal
semantics, transition validation, IPC provisioning — and dual-writes
every state mutation into the Rust table so the kernel-side
`AgentStatusResolver` (procfs view at `/{zone}/proc/{pid}/status`) and
any blocking `kernel.agent_wait` callers stay synchronized. Profiles
without agent workloads (REMOTE) skip construction; the kernel boots
the same way either path.

**Kernel DI patterns** (two mechanisms; the kernel reaches services only via
`ServiceRegistry` lookups or factory-injected closures):

| Pattern | Kernel `__init__` | Factory `_do_link()` | Example |
|---------|-------------------|---------------------|---------|
| **Kernel owns** | Creates instance | — | LockManager (I/O + advisory), KernelDispatch, PipeManager, StreamManager, FileWatcher, ServiceRegistry, DriverLifecycleCoordinator |
| **Kernel knows** (sentinel) | `self._x = None` | Injects real value; `None` = graceful degrade | `_token_manager`, `_sandbox_manager`, `_coordination_client`, `_event_client` |

"Kernel knows" follows the Linux LSM pattern: kernel declares a default
(`None`), factory overrides at link-time. Kernel modules import only from
`contracts/`, `lib/`, and other kernel-tier packages.

Permission enforcement is fully delegated to KernelDispatch INTERCEPT hooks
(PermissionCheckHook). No hook registered = no check = zero overhead.

**Zone identity:** `self._zone_id = ROOT_ZONE_ID` — kernel namespace partition
(analogous to Linux `sb->s_dev`). VFSRouter (Rust kernel primitive) canonicalizes
all paths to `/{zone_id}/{path}` for zone-aware LPM routing. Standalone: always
`"root"`. Federation: set at link time. All primitives (LockManager, FileEvent)
receive canonical paths — zone handling is VFSRouter's responsibility, not theirs.

**Source of truth:** `contracts/protocols/service_lifecycle.py`

### Entry Point: `connect()`

`connect(config=...)` is the **mode-dispatcher factory function** — the single
entry point for all Nexus users. It auto-detects deployment mode
(standalone/remote/federation), bootstraps the appropriate stack, and returns
`NexusFilesystem`.

```python
from nexus.sdk import connect
nx = connect()                    # auto-detect from env/config
nx = connect(config={"profile": "remote", "url": "http://..."})
```

Linux analogue: the boot sequence that selects rootfs and mounts it
(`mount_root()` in `init/do_mounts.c`). After `connect()` returns, you have a
usable filesystem. All three modes return the same `NexusFilesystem` contract
— clients never need to know which mode is running.

Not DI — it's the user-facing entry point. The factory/DI machinery is internal.

---

## 2. User Contract — Syscall Interface

**Category:** User Contract (↑) | **Audience:** Users, AI, agents | **Package:** `contracts.filesystem`, `core.nexus_fs`

### 2.1 NexusFilesystem — Published Contract

The published user-facing contract is `NexusFilesystem` (Protocol, in `contracts/filesystem/`):

| Tier | Content | Caller responsibility |
|------|---------|----------------------|
| **Tier 1 (abstract)** | `sys_*` kernel syscalls | Implementors MUST override |
| **Tier 2 (concrete)** | Convenience methods composing Tier 1 (`mkdir`, `rmdir`, `read`, `write`, …) | Inherit — no override needed |

Relationship: POSIX spec (contract) vs Linux kernel (implementation) — clients
program against the contract, kernel implements it.

### 2.2 Kernel Syscalls — POSIX-Aligned, Path-Addressed

`NexusFS` is the kernel implementation of `NexusFilesystem`. It wires
primitives (§4) into user-facing operations. NexusFS contains **no service
business logic**.

All kernel methods are synchronous (`def`, not `async def`). Blocking
waits (advisory locks, stream reads) use Rust Condvar with GIL release.
Exception: `sys_watch` uses asyncio futures to wait for file events.
Async exists only at the transport layer (gRPC, HTTP).

Kernel syscalls, all POSIX-aligned, all path-addressed:

| Plane | Syscalls |
|-------|----------|
| **Metadata** | `sys_stat`, `sys_setattr`, `sys_rename`, `sys_unlink`, `sys_readdir` |
| **Content** | `sys_read` (pread), `sys_write` (pwrite), `sys_copy` |
| **Locking** | `sys_lock` (acquire + extend), `sys_unlock` (release + force) |
| **Watch** | `sys_watch` (inotify) |

`sys_setattr` is the universal creation/management syscall:
`mkdir` = `sys_setattr(entry_type=DT_DIR)`, `mount` = `sys_setattr(entry_type=DT_MOUNT, backend=...)`,
`umount` = `rmdir` on DT_MOUNT path.

Lock operations are consolidated into two syscalls (POSIX `fcntl(F_SETLK)` pattern):
- `sys_lock(path, lock_id=None)` — acquire (lock_id=None) or extend TTL (lock_id=existing)
- `sys_unlock(path, lock_id=None, force=False)` — release by lock_id or force-release all holders
- Lock state: `sys_stat(path, include_lock=True)` — zero cost when False (default)
- Lock listing: `sys_readdir("/__sys__/locks/")` — virtual namespace (like `/proc/locks`)
`/__sys__/` paths are kernel management operations (not filesystem metadata):
`sys_setattr("/__sys__/services/X", service=inst)` registers,
`sys_unlink("/__sys__/services/X")` unregisters.

**Primitive usage pattern:**

- **Mutating syscalls** (write, unlink, rename, rmdir): full pipeline — VFSRouter →
  VFSLock → KernelDispatch (3-phase) → Metastore → FileEvent
- **DT_PIPE / DT_STREAM I/O**: Rust dcache detects entry_type early in sys_read/sys_write
  and dispatches to PipeManager/StreamManager inline — no VFS lock, no metastore update,
  no observer dispatch (matching Linux `write(2)` on a pipe not triggering inotify)
- **Read**: same pipeline minus FileEvent (reads are not mutations)
- **Read-only metadata** (stat, access, readdir, is_directory): direct Metastore
  lookup only — no routing, locking, or dispatch
- **setattr**: Metastore-only (Tier 2 `mkdir` adds routing + hooks)

See `syscall-design.md` for the full per-syscall primitive matrix.

### 2.3 Tier 2 Convenience Methods

Tier 2 methods compose Tier 1 syscalls — concrete implementations in `NexusFilesystem`:

| Half | Examples | Addressing |
|------|----------|-----------|
| **VFS half** (POSIX-aligned) | `mkdir()`, `rmdir()`, `read()`, `write()`, `append()`, `edit()`, `write_batch()`, `access()`, `is_directory()`, `lock()`, `locked()`, `glob()`, `grep()`, `service()` | Path-addressed, delegates to `sys_*` |
| **HDFS half** (driver-level) | `read_content()`, `write_content()`, `stream()`, `stream_range()`, `write_stream()` | Hash-addressed (etag/CAS), direct to ObjectStoreABC |

The HDFS half bypasses path resolution and metadata lookup — CAS is a driver
detail. Like HDFS separates ClientProtocol (NameNode, path-based) from
DataTransferProtocol (DataNode, block-based). The metadata layer above ensures
etag ownership and zone isolation.

**Kernel-managed metadata side effects** (POSIX ``generic_write_end`` pattern):
kernel updates mtime, size, version, etag in VFS lock after
``backend.write_content()``. Drivers only manage content.
Consistency is zone-level (configured at metastore layer), not per-write.

### 2.4 VFS Dispatch (KernelDispatch)

The kernel provides callback-based dispatch at 6 VFS operation points (read,
write, delete, rename, mkdir, rmdir) plus driver lifecycle events (mount,
unmount). These are kernel-owned callback lists (implemented by
`KernelDispatch`, §4) that any authorized caller populates.

**Three-phase dispatch per VFS operation:**

| Phase | Semantics | Short-circuit? | Linux Analogue |
|-------|-----------|----------------|----------------|
| **PRE-DISPATCH** | First-match short-circuit | Yes (skips pipeline) | VFS `file->f_op` dispatch (procfs, sysfs) |
| **INTERCEPT** | Synchronous, ordered (pre + post) | Yes (abort/policy) | LSM security hooks |
| **OBSERVE** | Fire-and-forget | No | `fsnotify()` / `notifier_call_chain()` |

**Driver lifecycle hooks:**

| Phase | Semantics | Short-circuit? | Linux Analogue |
|-------|-----------|----------------|----------------|
| **MOUNT** | Fire-and-forget on backend mount | No | `file_system_type.mount()` |
| **UNMOUNT** | Fire-and-forget on backend unmount | No | `kill_sb()` |

Mount/unmount hooks are dispatched by `DriverLifecycleCoordinator` (§4) via
KernelDispatch. Backends declare mount hooks via `hook_spec()` (same pattern
as VFS hooks). CASAddressingEngine uses `on_mount` for mount-time logging.

**PRE-DISPATCH**: `VFSPathResolver` instances checked in order; first match
handles entire operation. Each resolver owns its own permission semantics.

**INTERCEPT**: Per-operation `VFS*Hook` protocols. Hooks receive a typed context
dataclass, can modify context or abort. POST hooks support sync and async
(classified by Rust `HookRegistry`). Audit is a factory-registered interceptor,
not a kernel built-in.

**OBSERVE**: `VFSObserver` instances receive frozen `FileEvent` (§4.3) on all
mutations. Strictly fire-and-forget — failures never abort the syscall.
Observers needing causal ordering belong in INTERCEPT post-hooks, not OBSERVE.

Hook protocols and context dataclasses are defined in `contracts/vfs_hooks.py`
(tier-neutral). Concrete implementations live in `services/hooks/`.

**Registration API:** Each phase has a symmetric `register_*()` /
`unregister_*()` pair — runtime-callable by any authorized caller.

#### 2.4.1 The 4 Dispatch Contracts

Each dispatch phase is a formal contract between the kernel and its callers.
These contracts define ordering, error semantics, and performance guarantees.

| # | Contract | Phase | Trait / Protocol | Dispatch semantics | Error handling |
|---|----------|-------|-----------------|-------------------|----------------|
| 1 | **RESOLVE** (PRE-DISPATCH) | Before pipeline | `VFSPathResolver` (Rust `PathResolver` trait) | PathTrie O(depth) lookup, then fallback linear scan. First resolver whose `try_*(path)` returns non-None handles the entire operation — normal VFS pipeline is skipped. | Resolver exceptions propagate to caller (resolver owns error semantics). |
| 2 | **INTERCEPT PRE** | Before HAL I/O | `InterceptHook.on_pre_*` (Rust trait) | Serial, ordered. All registered pre-hooks run in registration order. | Any hook may abort by returning `Err` / raising — exception propagates to caller, operation is cancelled. |
| 3 | **INTERCEPT POST** | After HAL I/O | `InterceptHook.on_post_*` (Rust trait) | Serial, fire-and-forget via Rust `dispatch_post_hooks()`. | Failures are logged and swallowed — never affect the caller or the operation result. |
| 4 | **OBSERVE** | After lock release | `VFSObserver.on_mutation` (Python protocol) | Inline observers: synchronous on caller thread. Deferred observers: submitted to kernel observer ThreadPoolExecutor (4 threads, `observe` prefix). Event mask bitmask filtering at registration time. | Failures are caught and logged — never abort the syscall. Observers needing causal ordering belong in INTERCEPT POST, not OBSERVE. |

**Ordering guarantee:** RESOLVE > INTERCEPT PRE > HAL I/O > INTERCEPT POST > OBSERVE.
OBSERVE always fires after VFS lock release (like Linux inotify after `i_rwsem`).

**Zero-overhead invariant:** Empty callback list = no-op dispatch = zero overhead
when no services are registered.

**Rust/Python boundary crossing budget:**

| Path | Crossings | Notes |
|------|-----------|-------|
| Pillar calls (Metastore, ObjectStore, DCache) | 0 | Pure Rust trait dispatch |
| Hook dispatch (read/write/unlink/rename/copy/mkdir/rmdir) | 2+N | Context build + per-hook call, GIL held pre-detach |
| Service lifecycle (enlist auto-start, start_all, stop_all) | 4/service | isinstance + call_method0 + asyncio.wait_for + asyncio.run (stdlib only). Not on syscall hot path |
| Zero-crossing syscalls | 0 | sys_lock, sys_unlock, sys_watch, sys_stat (chrono), sys_setattr, sys_readdir, sys_write IPC (DT_PIPE/DT_STREAM) |

### 2.5 Mediation Principle

Users access HAL only through syscalls. For mutating syscalls the pipeline is:
PRE-DISPATCH → route → INTERCEPT pre → lock → HAL I/O → unlock → INTERCEPT
post → OBSERVE. See `syscall-design.md` for the full per-syscall flow.

**Exception:** Tier 2 hash-addressed operations (see §2.3 HDFS half) access
ObjectStoreABC directly by etag, bypassing path resolution and metadata lookup.

---

## 3. HAL — Storage Driver Contracts

**Category:** HAL — Driver Contract (↓) | **Audience:** Driver implementors

NexusFS abstracts storage by **Capability** (access pattern + consistency guarantee),
not by domain or implementation.

| Pillar | ABC (Python) | Trait (Rust) | Capability | Kernel Role | Package |
|--------|-----|------|------------|-------------|---------|
| **Metastore** | `MetastoreABC` | `MetaStore` | Ordered KV, CAS, prefix scan, optional Raft SC | **Required** — sole kernel init param | `core.metastore` / `kernel/src/abc/metastore.rs` |
| **ObjectStore** | `ObjectStoreABC` (= `Backend`) | `ObjectStore` | Streaming I/O, immutable blobs, petabyte scale | **Interface only** — instances mounted via `nx.mount()` | `core.object_store` / `kernel/src/abc/object_store.rs` |
| **CacheStore** | `CacheStoreABC` | `CacheStore` | Ephemeral KV, Pub/Sub, TTL | **Optional** — defaults to `NullCacheStore` | `contracts.cache_store` / `kernel/src/abc/cache_store.rs` |

**Rust naming note:** the Rust trait `MetaStore` (two-word PascalCase)
matches `ObjectStore` / `CacheStore` for visual symmetry across the
three ABC pillars.  Phase 0.5 of
`refactor/rust-workspace-parallel-layers` renamed the Rust trait from
`Metastore` (one word) to `MetaStore` (two words); the Python ABC
stays `MetastoreABC` because the Python tier is on a sunset path and
not worth ripple-renaming.  The cross-language asymmetry is anchored
at exactly one PyO3 boundary
(`raft/src/pyo3_bindings.rs`'s `#[pyclass(name = "Metastore")]`),
which disappears wholesale when the Rust-ification of every
Metastore caller (Phase J / `kernel.sys_*` syscalls) retires the
last Python `MetastoreABC` reference.

**Rust-side strict layout:** `kernel/src/abc/` contains **exactly the
3 §3 ABC pillar trait files**, period — no extension interfaces, no
helper traits.  Kernel-defined extension interfaces that aren't §3
pillars (LSM-style hooks like `LlmStreamingBackend`,
`PeerBlobClient`) live in `kernel/src/hal/`.  Kernel primitives (§4)
live in `kernel/src/core/` and never declare traits.  The
3-way split is enforced by directory layout — anything inside
`abc/` is a §3 pillar, anything else isn't.

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
`zone_id` is a **kernel namespace partition identifier** (analogous to Linux
`sb->s_dev`). Federation extends zones with Raft consensus groups, but the
kernel owns the concept. `owner_id` is the kernel's posix_uid — used by
`PermissionEnforcerProtocol.check_owner()` for O(1) DAC before service-layer
hooks run. Audit trail (who created a file) is a service concern tracked by
VersionRecorder, not a kernel inode field.

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

### 3.5 Transport × Addressing Composition

**Linux analogue:** Block device driver (Transport) × filesystem (Addressing)

ObjectStoreABC backends decompose into two orthogonal axes: **Transport** (WHERE —
raw key→bytes I/O) and **Addressing Engine** (HOW — CAS or Path). Every backend,
including external API connectors, is a Transport composed with an addressing
engine. REST APIs are filesystems: `GET` = `fetch`, `PUT` = `store`, `DELETE` = `remove`.

**DT_EXTERNAL_STORAGE** (`entry_type=5`): Mount-time detection via
`ConnectorRegistry.category` for OAuth APIs and CLI tools.

See `backend-architecture.md` §2 for the full composition matrix and Transport
protocol. See `connector-transport-matrix.md` for per-connector details.

---

## 4. Kernel Primitives

**Category:** Kernel Primitive (internal) | **Audience:** Kernel-internal | **Package:** `core.*`

Primitives mediate between user-facing syscalls and HAL drivers. Users interact
with them indirectly through syscalls. See §2.2 for per-syscall usage.

| Primitive | Package | Linux Analogue | Role |
|-----------|---------|---------------|------|
| **VFSRouter** | `core.router` + `rust/kernel/src/mount_table.rs` | VFS `lookup_slow()` | `route(path, zone_id)` → `RouteResult`. Zone-canonical LPM (~30ns Rust). In-memory mount table keyed by `/{zone_id}/{mount_point}` |
| **LockManager** | `rust/kernel/src/lock_manager.rs` | `i_rwsem` + `flock(2)` | I/O lock + advisory lock in one Rust struct. I/O: per-path condvar-based RW lock (§4.1). Advisory: `sys_lock`/`sys_unlock` with TTL (§4.4). Local: VFSSemaphore. Federation: auto-upgrade via `upgrade_to_distributed()` at mount time |
| **Dispatch (Rust Kernel + DispatchMixin)** | `core.nexus_fs_dispatch` + `rust/kernel/src/dispatch.rs` | `security_hook_heads` + `fsnotify` | Three-phase VFS dispatch (§2.4) + driver lifecycle hooks (MOUNT/UNMOUNT). Rust Kernel owns PathTrie + HookRegistry + ObserverRegistry (pure Rust, zero Py\<PyAny\>). DispatchMixin provides Python-side registration API. Empty = zero overhead |
| **PipeManager + StreamManager** | `rust/kernel/src/pipe_manager.rs` + `rust/kernel/src/stream_manager.rs` | `pipe(2)` + append-only log | VFS named IPC. DT_PIPE: destructive FIFO (MemoryPipeBackend / SharedMemoryPipeBackend). DT_STREAM: non-destructive offset reads. Details in §4.2 |
| **FileWatcher + FileEvent** | `core.file_watcher` + `core.file_events` | `inotify(7)` + `fsnotify_event` | File change notification + immutable mutation records. Local OBSERVE waiters + optional RemoteWatchProtocol. Details in §4.3 |
| **ServiceRegistry** | `core.service_registry` | `init/main.c` + `module.c` | Kernel-owned symbol table + lifecycle orchestration (enlist/swap/shutdown). BackgroundService + duck-typed hook_spec() |
| **DriverLifecycleCoordinator** | `rust/kernel/src/dlc.rs` + `core.driver_lifecycle_coordinator` | `register_filesystem` + `kern_mount` | Rust DLC: routing table + metastore + dcache + lock manager upgrade + **federation dcache-coherence callback** (installs a per-mount invalidator on the zone's state machine so committed metadata mutations evict stale dcache entries on every voter). Python DLC: backend refs (`_PyMountInfo`) + event dispatch |

### 4.1 Unified LockManager — I/O Lock + Advisory Lock

Rust `LockManager` (`rust/kernel/src/lock_manager.rs`) unifies both lock
concerns in one struct. I/O lock (condvar-based, per-path RW) and advisory
lock (TTL-based, user-facing) share one code path.

| Property | I/O Lock | Advisory Lock |
|----------|----------|---------------|
| Modes | `read` (shared) / `write` (exclusive) | exclusive (mutex), TTL-based |
| Latency | ~200ns (Rust condvar) | ~5μs local / ~5-10ms Raft |
| Scope | Process-scoped, crash → released | TTL-based, expire → released |
| Visibility | Kernel-internal (sys_read/write) | User-facing (sys_lock/sys_unlock) |
| Storage | In-memory only | redb `sm_locks` table (metastore) |

See `lock-architecture.md` for full design. See `federation-memo.md` for
distributed lock upgrade path.

### 4.2 IPC Primitives — Named Pipes & Streams

Two-layer architecture for both: VFS metadata (inode) in MetastoreABC, data
(bytes) in process heap buffer (like Linux `kmalloc`'d pipe buffer).

| Primitive  | Linux Analogue    | Buffer         | Read          |
|------------|-------------------|----------------|---------------|
| DT_PIPE    | `kfifo` ring      | MemoryPipeBackend     | Destructive   |
| DT_STREAM  | append-only log   | MemoryStreamBackend   | Non-destructive (offset-based) |

**DT_PIPE (PipeManager + MemoryPipeBackend):**

- **PipeManager (mkpipe)** — VFS named pipe lifecycle (created via `sys_setattr`
  upsert, read/write via `sys_read`/`sys_write`, destroyed via `sys_unlink`),
  per-pipe lock for MPMC safety. Reads are destructive (consumed on read).
- **MemoryPipeBackend (kpipe)** — Lock-free **SPSC** kernel primitive (`kfifo` analogue),
  no internal synchronization. Kernel manages pipe lifecycle directly.
  Direct MemoryPipeBackend access is kernel-internal only.

**DT_STREAM (StreamManager + pluggable StreamBackend):**

- **StreamManager (mkstream)** — VFS named stream lifecycle (same syscall
  surface as mkpipe). Per-stream lock for concurrent writers. Reads are
  non-destructive — multiple readers maintain independent byte offsets (fan-out).
- **StreamBackend protocol** — pluggable backing store for DT_STREAM data.
  ``io_profile`` determines which backend is used at creation time.
  Implementations: ``MemoryStreamBackend`` (in-memory, default),
  ``SharedMemoryStreamBackend`` (mmap shared memory, cross-process, ~1-5μs),
  ``WalStreamCore`` (Raft-replicated WAL, durable + distributed).

**io_profile — Backend Selection via sys_setattr:**

``sys_setattr(path, entry_type=DT_PIPE|DT_STREAM, io_profile=...)`` selects the
backend implementation at creation time. ``io_profile`` defaults to ``"memory"``
(in-process ring buffer); ``"shared_memory"`` creates mmap-based cross-process
IPC; ``"wal"`` creates a Raft-replicated WAL stream (requires federation).
Rust kernel creates the backend, registers it in PipeManager/StreamManager,
and returns SHM metadata (``shm_path``, ``data_rd_fd``, ``space_rd_fd``) to
Python for asyncio integration. sys_read/sys_write go through Rust PipeManager
regardless of io_profile — zero Python state.

See `federation-memo.md` §7j for design rationale.

### 4.3 FileWatcher + FileEvent — File Change Notification

| Property | Value |
|----------|-------|
| Event types | `FILE_WRITE`, `FILE_DELETE`, `FILE_RENAME`, `METADATA_CHANGE`, `DIR_CREATE`, `DIR_DELETE`, `SYNC_*`, `CONFLICT_*` |
| FileEvent | Frozen dataclass: path, etag, size, version, zone_id, agent_id, user_id, vector_clock |
| FileWatcher (kernel-owned) | Local OBSERVE waiters — `on_mutation()` resolves in-memory futures (~0µs) |
| FileWatcher (kernel-knows) | Optional `RemoteWatchProtocol` for distributed watch, set via `set_remote_watcher()` |
| Emission point | Always AFTER lock release |

### 4.4 LockManager — Advisory Lock

| Property | Value |
|----------|-------|
| Linux analogue | `flock(2)` / `fcntl(F_SETLK)` |
| Package | `rust/kernel/src/lock_manager.rs` |
| Storage | `sm_locks` redb table (separate from FileMetadata) |
| Lifecycle | Kernel-owned: Rust `LockManager` constructed in `Kernel::new()`; federation upgrades via `upgrade_to_distributed()` at DLC mount time |

- **Local**: `VFSSemaphore` (Rust) — exclusive (mutex), shared (RW), counting (semaphore)
- **Distributed**: `upgrade_to_distributed(ZoneConsensus, Handle)` — advisory locks replicated via Raft
- **Syscalls**: `sys_lock` (try-acquire, Tier 1), `sys_unlock` (release, Tier 1), `lock()` (blocking wait, Tier 2)

---

## 5. Kernel-Authored Standards

**Category:** Kernel-Authored Standard (service-tier contract) | **Audience:** Services

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

**Package:** `storage.record_store` | **Service-tier interface (consumed by services, defined by kernel)**

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

**Package:** `contracts.protocols` | **Service-tier standards (defined by kernel, implemented by services)**

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
| Consumers | Advisory lock layer (`LocalLockManager`), CAS metadata RMW |

Advisory lock layer uses two semaphores per path for RW gate pattern
(shared/exclusive). See `lock-architecture.md` §3.

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

### Python ↔ Rust Crate Mapping

Both tier-neutral packages have a Rust mirror.  Names match so a reader
jumping between the two trees finds the same module in the same place.

| Tier-neutral package | Python                | Rust crate         |
|----------------------|-----------------------|--------------------|
| `contracts`          | `src/nexus/contracts` | `rust/contracts/`  |
| `lib`                | `src/nexus/lib`       | `rust/lib/`        |

`rust/lib/` builds against `wasm32-unknown-unknown` with default features.
PyO3 wrappers for the algorithms (rebac, search, trigram, glob, io,
prefix, simd, path_utils, bitmap, bloom, hash) live behind the optional
`python` feature in `rust/lib/src/python/*.rs`.  `rust/nexus-cdylib`
enables that feature so the wheel registers them through a single
`lib::python::register(m)` call.

### 6.1 Workspace composition

The Rust workspace splits into three roles:

| Role            | Cargo type   | Purpose                                                                  |
|-----------------|--------------|--------------------------------------------------------------------------|
| Library crates  | `rlib`       | Compose into Python wheel + standalone binaries.                         |
| Wheel artifact  | `cdylib`     | `rust/nexus-cdylib/` — produces `nexus_kernel.so` / `.pyd` for Python.   |
| Profile binary  | binary       | `rust/profiles/<name>/` — standalone deployment binaries (see §7.1).     |

The Linux analogue is `make bzImage`: rlibs compile into one of two
final artifacts (Python wheel or deployment binary) the same way
`fs/built-in.a` and `kernel/built-in.a` link into `vmlinuz`.

#### Wheel composition

`rust/nexus-cdylib/src/lib.rs` is the sole `#[pymodule] fn
nexus_kernel`; it aggregates each peer's PyO3 surface through that
peer's `python::register` entry:

```rust
#[pymodule]
fn nexus_kernel(m: &Bound<PyModule>) -> PyResult<()> {
    lib::python::register(m)?;
    kernel::python::register(m)?;
    nexus_raft::pyo3_bindings::register_python_classes(m)?;
    services::python::register(m)?;
    backends::python::register(m)?;
    transport::python::register(m)?;
    Ok(())
}
```

This split lets each peer crate depend on `kernel` (for trait
declarations: `abc::ObjectStore`, `hal::peer::PeerBlobClient`, …)
while the wheel-side dependency `nexus-cdylib → {kernel, peers}`
flows in only one direction.

#### Dependency direction

```text
              contracts                       (zero deps)
                  ↑
                 lib                          (depends on contracts)
                  ↑
        transport-primitives                  (low-level TLS / pool /
                  ↑                            addressing; depends on
                  │                            contracts)
               kernel                         (depends on contracts + lib +
                  ↑                            transport-primitives + raft)
          ↑    ↑    ↑    ↑
          │    │    │    │
  backends services transport raft            (peers — depend on kernel +
          ↑    ↑    ↑    ↑                    transport-primitives)
          │    │    │    │
          └────┴────┴────┴──── nexus-cdylib   (Python wheel sink)

                                  raft         (used by profile binaries)
                                   ↑
                          rust/profiles/cluster (deployment binary sink)
```

Edge invariants:

| Edge                                | Direction                                  |
|-------------------------------------|--------------------------------------------|
| `services` / `backends` / `transport` / `raft` | siblings; no cross-edges               |
| `kernel ↔ lib`                      | one-way: `kernel → lib`                    |
| `raft ↔ transport`                  | one-way: `raft → transport-primitives`     |
| `nexus-cdylib`                      | sink (Python wheel)                        |
| `rust/profiles/<name>`              | sink (deployment binary)                   |

The first edge keeps lib WASM-clean.  The second is the cycle-break
that lets transport own kernel-bound code while raft keeps a
kernel-free dependency footprint.

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

| Profile | Target | Metastore | Linux Analogue |
|---------|--------|-----------|----------------|
| **slim** | Bare minimum runnable | redb (embedded) | initramfs |
| **cluster** | Minimal multi-node (IPC + federation, no auth) | redb (Raft) | CoreOS |
| **embedded** | MCU, WASM (<1 MB) | redb (embedded) | BusyBox |
| **lite** | Pi, Jetson, mobile | redb (embedded) | Alpine |
| **full** | Desktop, laptop | redb (embedded) | Ubuntu Desktop |
| **cloud** | k8s, serverless | redb (Raft) | Ubuntu Server |
| **remote** | Client-side proxy (zero local bricks) | RemoteMetastore | NFS client |

Profile hierarchy: `slim ⊂ cluster ⊂ embedded ⊂ lite ⊂ full ⊆ cloud`.
REMOTE is orthogonal — stateless proxy, all operations via gRPC to server.

Same kernel binary, different driver injection. See §1 `connect()`.
**Source of truth:** `src/nexus/contracts/deployment_profile.py`.

### 7.1 Profile binaries (`rust/profiles/`)

A profile that runs as its own OS process lives under `rust/profiles/<name>/`
and produces a standalone deployment binary `nexusd-<name>`:

| Profile  | Crate                       | Binary             |
|----------|-----------------------------|--------------------|
| cluster  | `rust/profiles/cluster/`    | `nexusd-cluster`   |

The crate composes the rlibs needed for that profile (e.g. `cluster`
links `raft + contracts` only — no kernel, no Python interpreter), so
each binary lands at the size floor for the features it ships.

`rust/nexus-cdylib/` lives at workspace top level rather than under
`profiles/` because the Python wheel is a different artifact category:
it loads into an external Python process, where profile binaries each
run as their own process.

---

## 8. Communication

Kernel-adjacent services built on kernel primitives (§4.2 IPC, §4.3
FileEvent). Not kernel-owned, but bottom-layer infrastructure.

| Tier | Nexus | Built on | Topology |
|------|-------|----------|----------|
| **Kernel** | DT_PIPE (§4.2) | MemoryPipeBackend — destructive FIFO | Local or distributed (transparent) |
| **Kernel** | DT_STREAM (§4.2) | MemoryStreamBackend — append-only log | Local or distributed (transparent) |
| **System** | gRPC + IPC | PipeManager/StreamManager, consensus proto | Point-to-point |
| **User Space** | EventBus | CacheStoreABC pub/sub + FileEvent (§4.3) | Fan-out (1:N) |

See `federation-memo.md` §2–§5 for gRPC/consensus details.

---

## 9. Cross-References

| Topic | Document |
|-------|----------|
| Data type → pillar mapping | `data-storage-matrix.md` |
| Ops ABC × scenario affinity | `ops-scenario-matrix.md` |
| Syscall table and design rationale | `syscall-design.md` |
| VFS lock design + advisory locks | `lock-architecture.md` §4 |
| Zone model, DT_MOUNT, federation | `federation-memo.md` §5–§6 |
| Raft, gRPC, write flows | `federation-memo.md` §2–§5 |
| Pipe + Stream design rationale | `federation-memo.md` §7j |
| Backend storage composition (CAS × Backend) | `backend-architecture.md` |
| CLI nexus/nexusd split | `cli-design.md` |
