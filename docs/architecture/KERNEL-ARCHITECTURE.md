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
State (pid → AgentState + condvar wakeup) lives in the Rust
`kernel::core::agents::registry::AgentRegistry` SSOT
(`rust/kernel/src/core/agents/registry.rs`); the Python service is a
thin shim that adds OS behavior — PID allocation, parent/child tree,
signal semantics, transition validation, IPC provisioning — and
dual-writes every state mutation into the Rust registry so the
kernel-side `AgentStatusResolver` (procfs view at
`/{zone}/proc/{pid}/status`) and any blocking `kernel.agent_wait`
callers stay synchronized. Profiles without agent workloads (REMOTE)
skip construction; the kernel boots the same way either path.

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
`umount` = `rmdir` on DT_MOUNT path, `symlink` = `sys_setattr(entry_type=DT_LINK, link_target=...)`.

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
- **DT_LINK**: route() follows the link target one hop with self-loop rejection (§4.5);
  hooks fire on the resolved target path so audit and access checks behave identically
  to a direct write
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

## 3. HAL — Storage HAL & Control-Plane HAL

**Category:** HAL — Driver Contract (↓) | **Audience:** Driver implementors

The kernel exposes two HAL flavors:

- **§3.A Storage HAL** — persistent-data driver contracts. The 3 ABC pillars
  (Metastore, ObjectStore, CacheStore) plus the Transport × Addressing
  composition that decomposes ObjectStore.
- **§3.B Control-Plane HAL** — runtime DI surfaces. Capabilities the kernel
  needs but does not own: distributed namespace topology
  (`DistributedCoordinator`) and backend instantiation (`ObjectStoreProvider`).

Both flavors live under `rust/kernel/src/`: `abc/` for the §3.A pillars,
`hal/` for §3.B.

### 3.A Storage HAL — ABC pillars

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

**Rust-side strict layout:** `kernel/src/abc/` contains exactly the
3 §3.A ABC pillar trait files. `kernel/src/hal/` contains the §3.B
Control-Plane HAL trait files (`DistributedCoordinator`,
`ObjectStoreProvider`). Kernel primitives (§4) live in `kernel/src/core/`
as concrete types. Connector-backend protocol extensions (e.g.
`LlmStreamingBackend`) live in `rust/backends/`; transport-layer
abstractions (`PeerBlobClient`, TOFU trust store) live in the
tier-neutral `rust/lib/` crate's `transport_primitives` module.
Directory layout enforces the three-way split: `abc/` is for §3.A
pillars, `hal/` is for §3.B DI surfaces, `core/` is for primitives.

**Orthogonality:** Between pillars = different query patterns. Within pillars =
interchangeable drivers (deployment-time config). See `data-storage-matrix.md`.

**Kernel self-inclusiveness:** Kernel boots with **1 pillar** (Metastore).
ObjectStore mounted post-init. Kernel does NOT need: JOINs, FK, vector search,
TTL, pub/sub (all service-layer). Like Linux: kernel defines VFS + block device
interface but doesn't ship a filesystem.

#### 3.A.1 MetastoreABC — Inode Layer

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

#### 3.A.2 ObjectStoreABC (= Backend) — Blob I/O

**Linux analogue:** `struct file_operations`

CAS-addressed blob storage: read/write/delete by etag (content hash), plus
streaming variants. Directory ops (mkdir/rmdir/list_dir) for backends that
support them. Rename is optional (capability-dependent).

#### 3.A.3 CacheStoreABC — Ephemeral KV + Pub/Sub (Optional)

**Linux analogue:** `/dev/shm` + message bus

The only **optional** HAL pillar. Kernel defines the ABC (ephemeral KV + pub/sub);
services consume it for caching, event fan-out, and session storage.
Drivers: Dragonfly/Redis (production), `InMemoryCacheStore` (dev).

**Graceful degradation:** `NullCacheStore` (no-op) is the default. Without a real
CacheStore, EventBus disables, permission/tiger caches fall back to RecordStore,
and sessions stay in RecordStore. No kernel functionality is lost.

#### 3.A.4 Dual-Axis ABC Architecture

Two independent ABC axes, composed via DI:

- **Data ABCs** (this section): WHERE is data stored? → 3 kernel pillars by storage capability
- **Ops ABCs** (§5.3): WHAT can users/agents DO? → 40+ scenario domains by ops affinity

A concrete class sits at the intersection: e.g. `ReBACManager` implements
`PermissionProtocol` (Ops) and internally uses `RecordStoreABC` (Data).
See `ops-scenario-matrix.md` for full proof.

#### 3.A.5 Transport × Addressing Composition

**Linux analogue:** Block device driver (Transport) × filesystem (Addressing)

ObjectStoreABC backends decompose into two orthogonal axes: **Transport** (WHERE —
raw key→bytes I/O) and **Addressing Engine** (HOW — CAS or Path). Every backend,
including external API connectors, is a Transport composed with an addressing
engine. REST APIs are filesystems: `GET` = `fetch`, `PUT` = `store`, `DELETE` = `remove`.

**DT_EXTERNAL_STORAGE** (`entry_type=5`): Mount-time detection via
`ConnectorRegistry.category` for OAuth APIs and CLI tools.

See `backend-architecture.md` §2 for the full composition matrix and Transport
protocol. See `connector-transport-matrix.md` for per-connector details.

### 3.B Control-Plane HAL — Runtime DI Surfaces

Storage HAL (§3.A) is the persistent-data flavor of HAL; Control-Plane HAL is
the in-memory coordination flavor. The kernel calls a trait method, an
external crate's impl handles the actual work. Same DI shape on both sides:
trait declared in `kernel/src/hal/`, concrete impl in the owner crate, an
`Arc<dyn Trait>` slot the cdylib boots before any syscall fires.

| Trait | Capability | Default Impl | Reference Impl |
|-------|------------|--------------|----------------|
| `DistributedCoordinator` | Per-node distributed namespace topology — zones, mounts, share registry, leader/voter introspection | `NoopDistributedCoordinator` (errors out) | `RaftDistributedCoordinator` in `rust/raft/` |
| `ObjectStoreProvider` | Construct `Arc<dyn ObjectStore>` for a given backend type + args | `OnceLock` slot installed at boot | `DefaultObjectStoreProvider` in `rust/backends/` |

#### 3.B.1 `DistributedCoordinator`

**Linux analogue:** `struct super_operations` — the abstraction the VFS layer
talks through to reach any concrete filesystem driver without naming the
driver type. `DistributedCoordinator` plays the same role for distributed
namespace topology: kernel-side syscalls dispatch through
`kernel.distributed_coordinator()` instead of naming `nexus_raft::*` types
directly.

11 methods, four families:

- **Introspection (2):** `list_zones`, `cluster_info`. `ClusterInfo` carries
  `leader_id`, `term`, `voter_count`, `witness_count`, `links_count`,
  `commit_index`, applied index — typed Rust struct, native Rust field access
  on the caller side.
- **Zone lifecycle (3):** `create_zone`, `remove_zone` (cascade-unmounts cross-zone
  references first; `force=true` honors the POSIX-style `unlink while i_links > 0`
  bypass), `join_zone` (`as_learner=true` for non-voter membership).
- **Mount wiring (2):** `wire_mount` / `unwire_mount` — leader-side fast-path.
  The apply-cb on the state machine is the correctness guarantee, this pair is
  the optimization.
- **Share registry (2):** `share_zone` (atomic create-zone + copy-subtree +
  register-share), `lookup_share` returns a `ShareInfo` (zone_id +
  remote-path metadata).
- **Per-zone dispatch (2):** `metastore_for_zone` returns
  `Arc<dyn MetaStore>` backed by Raft state machine; `locks_for_zone` returns
  `Arc<dyn Locks>` that replicates lock acquisition via
  `Command::AcquireLock`.

Boot-time setup is a module-level `install()` function — a once-per-process
hook that wires the slot and folds in DI plumbing (blob-fetcher slot stash)
that lives outside the runtime surface. Same shape as
`transport::python::install_transport_wiring`.

Naming convention follows the §3.A pillars (`MetaStore`, `ObjectStore`,
`CacheStore`): the trait name describes the capability — distributed-namespace
coordination — rather than the implementation (Raft) or a GoF role (Provider /
Manager).

#### 3.B.2 `ObjectStoreProvider`

Single method: `construct(args: ObjectStoreProviderArgs) -> Arc<dyn ObjectStore>`.

`Kernel::sys_setattr("backend", …)` and the mount path use this to instantiate
backends through trait dispatch. Cycle break is identical to the §3.A pattern:
kernel declares the trait, backends crate provides the impl, cdylib boot wires
the slot.

The trait name describes the capability ("provides ObjectStore instances"), in
symmetry with `DistributedCoordinator` and the §3.A pillars.

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

### 4.5 DT_LINK — Path-Internal Symlink

| Property | Value |
|----------|-------|
| Linux analogue | `symlink(2)` |
| Entry type | `DT_LINK = 6` (`proto/nexus/core/metadata.proto`) |
| Storage | `FileMetadata.link_target` — absolute or workspace-relative VFS path |
| Resolution | Kernel `route()` follows the link before reaching the backend; one hop only, with self-loop rejection |

A DT_LINK is a metadata-only entry whose `link_target` field carries the path it
points at. Path resolution treats it as a redirect: every `sys_*` call against a
DT_LINK path resolves to the equivalent operation on the link target, with hooks
firing on the resolved target path. `sys_unlink` removes the link without touching
the target; `sys_stat` reports the entry as a link with its `link_target` filled in.

Cycle handling is bounded by the one-hop rule — if `target` is itself a DT_LINK,
the resolver returns `ELOOP` rather than chaining. Self-loops (`link → itself`) are
rejected at `sys_setattr` time.

**Use cases:**

- `/proc/{pid}/agent` → `/agents/{name}/` (runtime back-reference to image; mirrors Linux `/proc/{pid}/exe`)
- `/proc/{pid}/workspace/chat-with-me` → `/proc/{pid}/chat-with-me` (workspace-anchored mailbox shortcut so agents addressing each other don't have to walk the registry)

See the sudowork integration design doc (`sudowork/docs/tech/nexus-integration-architecture.md`) for the A2A messaging conventions that consume DT_LINK.

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

`rust/lib/` builds against `wasm32-unknown-unknown` with default
features. PyO3 wrappers for the algorithms (rebac, search, trigram,
glob, io, prefix, simd, path_utils, bitmap, bloom, hash) live behind
the optional `python` feature in `rust/lib/src/python/*.rs`.
`rust/nexus-cdylib` enables that feature so the wheel registers them
through a single `lib::python::register(m)` call.

`rust/lib/` also carries the `transport_primitives` module — TLS
config, peer addressing, connection pooling, channel creation, the
TOFU trust store, and the `PeerBlobClient` trait. The module sits
behind the optional `transport` feature so WASM / pure-algo callers
skip the tonic + tokio dep stack. Every peer crate that speaks raft
or VFS gRPC (raft, transport, kernel through the peer-client slot)
enables `lib`'s `transport` feature.

### 6.1 Workspace composition

The Rust workspace splits into three Cargo artifact roles:

| Cargo role      | Cargo type   | Purpose                                                                  |
|-----------------|--------------|--------------------------------------------------------------------------|
| Library crates  | `rlib`       | Compose into Python wheel + standalone binaries.                         |
| Wheel artifact  | `cdylib`     | `rust/nexus-cdylib/` — produces `nexus_runtime.so` / `.pyd` for Python.   |
| Profile binary  | binary       | `rust/profiles/<name>/` — standalone deployment binaries (see §7.1).     |

The Linux analogue is `make bzImage`: rlibs compile into one of two
final artifacts (Python wheel or deployment binary) the same way
`fs/built-in.a` and `kernel/built-in.a` link into `vmlinuz`.

#### Crate role taxonomy

The library crates split into 5 architectural roles. Every peer crate
maps to exactly one role — that is the invariant that lets the dep
graph stay acyclic.

| Role | Crates | Linux analogue | Charter |
|------|--------|----------------|---------|
| **OS proper** | `kernel/`, `contracts/` | `kernel/` (vmlinux core) | VFS, syscalls, namespace primitives, HAL trait declarations. Depends on `contracts` and `lib`. |
| **Driver layer (kernel-internal)** | `backends/`, `raft/` | `drivers/` | Implement HAL traits; consume kernel's runtime API. `backends` = local storage drivers (ObjectStore impl). `raft` = distributed storage driver (MetaStore impl + DistributedCoordinator impl). |
| **Network surface (kernel-external)** | `transport/` | `net/` | VFS gRPC server + IPC envelope helpers (in-bound) plus VFS / peer-blob / federation clients (driver-outgoing). One crate covers both directions like Linux's `net/` covers both server sockets and outgoing connections. Depends on `kernel`, `lib`, and `raft` (proto stubs for the federation client). |
| **Post-syscall services (kernel-internal hooks)** | `services/` | LSM hooks (`security/`) | Audit, agents, permission, tasks. Fired on syscall paths through registered hooks; depends on `kernel`. |
| **Tier-neutral lib (§6)** | `lib/` | `lib/` (libc, libm) | Pure utilities depending on `contracts` only. Algorithms (bitmap, bloom, glob, hash, simd, …) plus the `transport_primitives` module (TLS, pool, addressing, TOFU trust store, `PeerBlobClient` trait). The §6 mirror of `src/nexus/lib`. |

The role split makes the orthogonality invariants
**`services ⊥ backends ⊥ raft`** (services and backends reach raft
state through `kernel.sys_*` syscalls, never via Cargo dep) and
**`kernel ⊥ raft`** (kernel reaches raft only through trait dispatch)
read directly off the table.

#### Kernel crate composition

`rust/kernel/src/kernel/` hosts the `Kernel` struct and its
syscall implementations across per-family submodules:

| File                | Owns                                                                           |
|---------------------|--------------------------------------------------------------------------------|
| `kernel/mod.rs`     | `Kernel` struct, constructor, wiring, MetaStore + DCache + Router proxies.    |
| `kernel/io.rs`      | `sys_read` / `sys_write` / `sys_stat` / `sys_unlink` / `sys_rename` / `sys_copy` / `sys_mkdir` / `sys_rmdir`. |
| `kernel/ipc.rs`     | Pipe + stream registries (`create_pipe`, `pipe_write_nowait`, `stream_read_at`, …). |
| `kernel/locks.rs`   | Advisory-lock syscalls (`sys_lock`, `sys_unlock`, `metastore_list_locks`, `install_federation_locks`). |
| `kernel/dispatch.rs`| Native INTERCEPT hook dispatch (`dispatch_native_pre`, `dispatch_native_post`, `register_native_hook`). |
| `kernel/observability.rs` | Observer registry, file-watch registry, `sys_watch`, `dispatch_mutation` shared helper. |
| `kernel/mount.rs`   | Mount-table primitives (`add_mount`, `remove_mount`, `install_mount_metastore`, `route`, …). |
| `kernel/federation.rs` | `DistributedCoordinator` slot accessors, `/__sys__/zones/` procfs synthesisers, blob-fetcher slot plumbing. |

Every submodule writes its methods as `impl Kernel { … }` blocks —
Rust treats each block as a member set of the same `Kernel` type, so
`self.method_in_io()` from a submodule reaches `self.method_in_mod()`
without intermediate trait dispatch.

The split between `kernel/` (syscalls) and `core/` (primitives) follows
the data type: §4 primitives — concrete data structures like
`DCache`, `VFSRouter`, `AgentRegistry`, `LockManager` — live in `core/`;
the syscall families that operate on them live in `kernel/`.

#### Control-Plane HAL DI surface

The `Kernel.distributed_coordinator` slot holds an
`Arc<dyn DistributedCoordinator>` that drives every federation-aware
syscall (§3.B.1). Trait surface lives in `kernel::hal::distributed_coordinator`;
concrete impl (`RaftDistributedCoordinator`) lives in the raft crate at
`nexus_raft::distributed_coordinator`. The kernel ↔ raft Cargo edge is
`raft → kernel` — kernel reaches distributed state
(`ZoneManager`, `ZoneRaftRegistry`, `tokio::runtime::Handle`,
`cross_zone_mounts` reverse index) through the trait dispatch, with the
coordinator owning that state.

Boot wiring:

| Step | Caller                                                           | Effect                                                                    |
|------|------------------------------------------------------------------|---------------------------------------------------------------------------|
| 1    | `Kernel::new`                                                    | Slot defaults to `NoopDistributedCoordinator`                             |
| 2    | `nexus_raft::distributed_coordinator::install(kernel)`           | Slot is replaced with `RaftDistributedCoordinator`; `init_from_env` stashes the blob-fetcher slot, then `blob_fetcher_handler::install` drains it and wires the kernel-backed `KernelBlobFetcher` |
| 3    | Federation syscalls (`init_federation_from_env`, `create_zone`, …) | Dispatch through `kernel.distributed_coordinator().<method>(kernel, …)`   |

Coordinator methods all take `kernel: &Kernel` so the unit-struct impl
forwards into kernel-side primitives without holding back-references.
The §3.B.2 `ObjectStoreProvider` slot uses the same pattern: trait in
`kernel::hal::object_store_provider`, impl in `backends::python::factory`,
boot hook in cdylib.

#### Wheel composition

`rust/nexus-cdylib/src/lib.rs` is the sole `#[pymodule] fn
nexus_runtime`; it aggregates each peer's PyO3 surface through that
peer's `python::register` entry:

```rust
#[pymodule]
fn nexus_runtime(m: &Bound<PyModule>) -> PyResult<()> {
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
declarations: `abc::ObjectStore`, `hal::distributed_coordinator::DistributedCoordinator`,
…) while the wheel-side dependency `nexus-cdylib → {kernel, peers}`
flows in only one direction. `PeerBlobClient` lives in
`lib::transport_primitives` so both raft (server-side handler) and
transport (client-side fetch) can depend on it without depending on
each other.

#### Dependency direction

```text
                       contracts              (zero deps)
                          ↑
                         lib                  (depends on contracts;
                          ↑                    algorithms + transport_primitives
                          │                    behind opt-in features)
                       kernel                 (depends on contracts + lib;
                          ↑                    declares HAL traits)
              ↑    ↑    ↑    ↑
              │    │    │    │
       backends raft transport services       (peer crates — depend on
              ↑    ↑    ↑    ↑                kernel + lib; transport
              │    │    │    │                additionally depends on raft
              │    │    │    │                for federation proto stubs)
              └────┴────┴────┴── nexus-cdylib  (Python wheel sink)

                              raft         (used by profile binaries)
                               ↑
                       rust/profiles/cluster  (deployment binary sink)
```

Edge invariants:

| Edge                                          | Direction                                      |
|-----------------------------------------------|------------------------------------------------|
| `services` / `backends` / `raft`              | role peers — orthogonal; reach each other via `kernel.sys_*` syscalls |
| `kernel ↔ lib`                                | one-way: `kernel → lib`                        |
| `raft ↔ transport`                            | one-way: `transport → raft` for federation client proto stubs (Postgres-client-references-libpq shape) |
| `kernel → raft`                               | trait-only: kernel reaches raft through `DistributedCoordinator` dispatch |
| `nexus-cdylib`                                | sink (Python wheel)                            |
| `rust/profiles/<name>`                        | sink (deployment binary)                       |

`lib` (default features) keeps a zero peer-crate footprint so it builds
against `wasm32-unknown-unknown`. The `transport_primitives` module
under lib's `transport` feature houses TLS / pool / addressing / TOFU
trust store / `PeerBlobClient` trait — both raft (server-side handler)
and transport (client-side fetch) consume it without depending on
each other.

#### RPC: client side vs server side

The remote-RPC stack lives on the network surface tier `transport/`,
plus raft for the federation server fabric.

| Side   | Crate                       | Module                         | Role                                                                                  |
|--------|-----------------------------|--------------------------------|---------------------------------------------------------------------------------------|
| Server | `transport`                 | `grpc` / `ipc`                 | VFS gRPC server (port 2028) + IPC envelope helpers                                    |
| Server | `raft`                      | `blob_fetcher_handler` / `pyo3_bindings` | Federation peer mesh + per-zone routers + blob-fetcher server handler         |
| Client | `transport`                 | `vfs` / `peer_blob` / `federation` | Driver-outgoing clients: VFS gRPC for `RemoteBackend`, peer-blob fetch, federation peer client |
| Shared | `lib::transport_primitives` | (whole module)                 | TLS, connection pool, addressing, TOFU trust store, `PeerBlobClient` trait — consumed by both sides |

`transport/` covers both directions of the network surface (Linux
`net/` analogue: same crate hosts server sockets and outgoing
connection helpers). The `RpcTransport` type sits in the kernel crate
(kernel-internal `RemoteMetaStore` / `RemotePipeBackend` /
`RemoteStreamBackend` wrappers also wrap it directly); `transport::vfs`
re-exports it so out-bound callers name a single canonical path.

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

The crate composes the rlibs needed for that profile.  `cluster` links
`raft + contracts + kernel + backends` (the last two with their
slimmest feature sets — no connectors, no Python interpreter).  The
binary mounts host-fs at `/` via `PathLocalBackend` at boot
(`--root-path`) and exposes runtime `mount` / `unmount` subcommands
that drive the same DLC syscalls.

`rust/nexus-cdylib/` lives at workspace top level rather than under
`profiles/` because the Python wheel is a different artifact category:
it loads into an external Python process, where profile binaries each
run as their own process.

### 7.2 Compile-time features vs runtime driver gate

Driver selection is gated at two layers — pick which layer is doing
the work for any given deployment:

| Layer | Mechanism | Decided | Cost paid by | Linux analogue |
|-------|-----------|---------|--------------|----------------|
| **Compile-time** | `backends`/`services` Cargo features (`driver-path-local`, `service-audit`, …) | `cargo build` | binary size on disk | `CONFIG_FOO=y` in `.config` |
| **Runtime** | `kernel::hal::object_store_provider::set_enabled_drivers` (Python `nx_set_enabled_drivers`) | Boot, before first `sys_setattr(DT_MOUNT)` | runtime error if a profile asks for a missing driver | `/sys/module/<name>/parameters` |

`nexus-cdylib` (Python wheel) compiles every driver in (`features =
["python", "connectors", "driver-*"]`) and uses the runtime gate to
limit what an active `DeploymentProfile` is allowed to mount.  The
runtime gate is the SSOT — every dispatch goes through
`is_driver_enabled`, no implicit local-default skip-branch.

`nexusd-cluster` (slim Rust binary) compiles only the drivers it needs
(`features = ["driver-path-local"]`) and skips the runtime gate
entirely — the compile-time gate is sufficient because the dispatch
arms for missing drivers don't exist.  Attempting to mount a
non-compiled driver returns `driver `X` not compiled into this
binary` straight from the factory.

A driver name that appears in
`src/nexus/contracts/deployment_profile.py::ALL_DRIVER_NAMES` is the
canonical name in both layers — Python aliases like the historical
`"cas"` → `"cas-local"` mapping live in
`src/nexus/core/nexus_fs_metadata.py`, never in Rust.

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

### 8.1 NexusVFSService.Call — RPC dispatch order

The tonic `Call(method, payload)` handler resolves the method through
two dispatch paths in order:

1. **Rust services** — `Kernel::dispatch_rust_call(service, method, payload)`
   routes to a `RustService::dispatch` impl when the method maps to a
   Rust-flavoured entry in `ServiceRegistry`. Method names follow one
   of two shapes:
   - Dotted: `service.method` (canonical) — split on the first `.`,
     dispatch the bare method on that service.
   - Flat backward-compat: methods with the prefix `acp_` or
     `managed_agent_` route to that service with the full method name
     preserved (matches Python `@rpc_expose` naming).
2. **Python `@rpc_expose`** — fallback path when the Rust dispatch
   returns `None` (no Rust service for that name) or `NotFound`
   (service exists but doesn't expose the method). The handler hands
   the original method string to `bridge.dispatch_call`, which runs
   the existing async `dispatch_method` on the FastAPI loop.

Auth is resolved before either dispatch path so admin-only checks
apply uniformly. `RustCallError::InvalidArgument` and `Internal`
short-circuit straight to the wire encoder; no fallback in those
cases.

### 8.2 Registered Rust services

| Service name | Source | Methods |
|--------------|--------|---------|
| `managed_agent` | `rust/kernel/src/managed_agent/` | `start_session_v1`, `cancel_v1`, `get_session_v1` — owns the chat-with-me + workspace-boundary hooks plus the session lifecycle for `AgentKind::Managed`. State writes go to `kernel::core::agents::registry::AgentRegistry` directly (no PyO3). |
| `acp` | `rust/kernel/src/acp/` | `acp_call`, `acp_kill`, `acp_list_agents`, `acp_list_processes`, `acp_set_system_prompt`, `acp_get_system_prompt`, `acp_set_enabled_skills`, `acp_get_enabled_skills`, `acp_history` — stateless coding-agent CLI caller via ACP JSON-RPC. `call_agent` orchestrates `AcpSubprocess` (tokio Command + DT_PIPE) + `AcpConnection` + `AcpSubservice` lifecycle. AgentRegistry stays Python; reached through the `PyAgentRegistry` trait bridge wired by `nx_acp_set_agent_registry`. |

In-process Python callers reach any Rust service through the generic
`nexus_runtime.nx_kernel_dispatch_rust_call(kernel, service, method,
payload)` (releases the GIL during the call). One primitive — no
per-service `nx_<svc>_dispatch` shortcuts — so audit / permission
hooks added to the dispatch path land in one place. External callers
come in over the tonic `Call` handler and follow the §8.1 dispatch
order.

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
