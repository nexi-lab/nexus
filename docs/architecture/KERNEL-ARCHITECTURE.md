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
| Services | Runtime (load/unload) | 22 protocols (ReBAC, Mount, Auth, Agents, Search, Skills, ...) | loadable kernel modules (`insmod`/`rmmod`) |

**Invariant:** Services depend on kernel interfaces, never the reverse.
The kernel operates with zero services loaded.

**Drivers** use constructor DI at startup — same binary, different config
(`NEXUS_METASTORE=redb`, `NEXUS_RECORD_STORE=postgresql`). Immutable after init.

**Services** have two maturity phases, both preserving the invariant above:

**Phase 1 — Init-time DI (distro composition).** `factory.py` acts as the init
system (like systemd): creates selected services and injects them via
`KernelServices` dataclass. Different distros select different service sets at
startup — `nexus-server` loads all 22+, `nexus-embedded` loads zero.

> *Gap:* `factory.py` hardcodes all service creation; `_wire_services()` loads
> everything unconditionally. No selective loading per distro yet.

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
- **Ops ABCs** (§3): WHAT can users/agents DO? → 28 scenario domains by ops affinity

A concrete class sits at the intersection: e.g. `ReBACManager` implements `PermissionProtocol`
(Ops) and internally uses `RecordStoreABC` (Data). The Protocol itself has no storage opinion.
See `ops-scenario-matrix.md` for full Ops-Scenario affinity proof.

---

## 3. Kernel vs Services Boundary

### Kernel Interfaces (`nexus.core`)

| Interface | Linux Analogue | Purpose |
|-----------|---------------|---------|
| `MetastoreABC` | `struct inode_operations` | Typed FileMetadata CRUD (the inode layer) |
| `VFSRouterProtocol` | VFS `lookup_slow()` | Path resolution only — mount CRUD lives in Service `MountProtocol` |
| `ObjectStoreABC` (= `Backend`) | `struct file_operations` | Blob I/O interface (read/write/delete/list) |
| `CacheStoreABC` | (no direct analogue) | Ephemeral KV + Pub/Sub primitives |

`MetastoreABC` is kernel because it IS the inode layer — the typed contract
between VFS and storage. Without it, the kernel cannot describe files.

### NexusFS — Syscall Dispatch Layer

`NexusFS` is the kernel entry point, analogous to Linux's syscall layer (`sys_open`,
<<<<<<< HEAD
`sys_read`). It wires VFSRouter + FileMetadataProtocol + ObjectStoreABC into
=======
`sys_read`). It wires VFSRouter + MetastoreABC + ObjectStoreABC into
>>>>>>> origin/develop
user-facing operations (read, write, list, mkdir, mount). NexusFS contains
**no service business logic** — services are accessed through `ServiceRegistry`
(Phase 2) or thin delegation stubs (Phase 1).

`factory.py` is the init system (analogous to systemd): constructs kernel + drivers
+ services and wires them together. NexusFS receives pre-built dependencies via its
constructor and never auto-creates services.

<<<<<<< HEAD
> *Gap:* NexusFS still contains 2 event-related mixins and ~40 lazy service imports
> in `_wire_services()`. Migration: extract remaining mixins → standalone service
> classes, then replace `KernelServices` dataclass with `ServiceRegistry`.
=======
> *Resolved:* Event mixins fully extracted — `NexusFSEventsMixin` removed (#573),
> `FileWatcher` moved to `services/watch/` (#706), orphaned kernel attrs cleaned (#656).
> Remaining: ~40 lazy service imports in `_wire_services()` (#194), replace
> `KernelServices` dataclass with `ServiceRegistry`.
>>>>>>> origin/develop

### Service Protocols (`nexus.services.protocols`)

28 scenario domains mapped to Ops ABCs. 22 Protocols exist, 9 gaps remain.

| Category | Protocols | Count |
|----------|-----------|-------|
| **Permission & Visibility** | PermissionProtocol, NamespaceManagerProtocol | 2 |
| **Search & Content** | SearchProtocol, SearchBrickProtocol (driver), LLMProtocol | 3 |
| **Mount & Storage** | MountProtocol, ShareLinkProtocol, OAuthProtocol | 3 |
| **Agent Infra** | AgentRegistryProtocol, SchedulerProtocol | 2 |
| **Events & Hooks** | EventLogProtocol, HookEngineProtocol, WatchProtocol, LockProtocol (split from EventsProtocol, #546) | 4 |
| **Domain Services** | SkillsProtocol, PaymentProtocol | 2 |
| **Missing (9 gaps)** | Version, Memory, Trajectory, Delegation, Governance, Reputation, OperationLog, Plugin, Workflow | 9 |

All use `typing.Protocol` with `@runtime_checkable`.
See `ops-scenario-matrix.md` §2–§3 for full enumeration and affinity matching.

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

| Mode | Description | Metastore | Services |
|------|-------------|-----------|----------|
| **Standalone** | Single process, local storage | redb (local) | Optional |
| **Client-Server** | RemoteNexusFS connects to a NexusFS server | redb (local) on server | On server |
| **Federation** | Multiple nodes sharing zones via Raft | redb (Raft) | Per-node |
| **Embedded** | Minimal kernel on constrained devices | redb (local) | None (planned) |

Driver selection is config-time: same binary, different `NEXUS_METASTORE`, `NEXUS_RECORD_STORE`, etc.

---

## 6. Communication

### Messaging Tiers

Three tiers, mirroring Linux's kernel → system → user space communication:

| Tier | Linux Analogue | Nexus | Latency | Topology |
|------|---------------|-------|---------|----------|
| **Kernel** | `kfifo` ring buffer | Nexus Native Pipe (`DT_PIPE`, MetastoreABC) | ~5μs | Intra-process |
| **System** | `sendmsg()` / Unix sockets | gRPC (Zone Transport/API) | ~0.5–1ms | Point-to-point (1:1) |
| **User Space** | POSIX `mq_open` / multi-queue | EventBus (CacheStoreABC pub/sub) | ~1–5ms | Fan-out (1:N) |

**Selection rule:** Consensus write path → System (gRPC, 1:1). Notification read path → User Space (EventBus, 1:N fan-out to 100s of observers). Internal signaling → Kernel (Pipe, zero-copy).

See `federation-memo.md` §7j for Pipe design.

### System Tier: gRPC Services

> **SSOT:** Proto files in `proto/` are the source of truth for RPC definitions.

| Proto Service | Proto File | Scope | Purpose |
|---------------|-----------|-------|---------|
| `ZoneTransportService` | `proto/nexus/raft/transport.proto` | Internal | Node-to-node Raft messages (StepMessage, ReplicateEntries) |
| `ZoneApiService` | `proto/nexus/raft/transport.proto` | Internal | Client-facing zone ops (Propose, Query, GetClusterInfo, JoinZone, InviteZone) |
| `ExchangeService` | `proto/nexus/exchange/v1/exchange.proto` | External | Agent Exchange API — identity (4 RPCs), payment (8 RPCs), audit (5 RPCs) |

Named `Zone*` to match `ZoneConsensus` (Rust). `ExchangeService` is the external-facing API for agent-to-agent value exchange.

---

## Cross-References

| Topic | Document |
|-------|----------|
| Data type → pillar mapping (50+ types) | `data-storage-matrix.md` |
| Storage orthogonality proof | `data-storage-matrix.md` §ORTHOGONALITY |
| Ops ABC × scenario affinity (28 domains, 22 protocols) | `ops-scenario-matrix.md` |
| Ops ABC orthogonality + gap analysis | `ops-scenario-matrix.md` §2–§3 |
| Raft, gRPC, write flows | `federation-memo.md` §2–§5 |
| Zone model, DT_MOUNT | `federation-memo.md` §5–§6 |
| SC vs EC consistency | `federation-memo.md` §4.1 |
| API privilege levels (agents vs ops vs admin) | `federation-memo.md` §6.10 |
