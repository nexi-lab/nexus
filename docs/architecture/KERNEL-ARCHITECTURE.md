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
│  Minimal compilable unit. VFS, FileMetadataProtocol,         │
│  MetastoreABC, ObjectStoreABC interface definitions.         │
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
| Static kernel | Never | MetastoreABC, VFS, FileMetadataProtocol, syscall dispatch | vmlinuz core (scheduler, mm, VFS) |
| Drivers | Config-time (DI at startup) | redb, S3, PostgreSQL, Dragonfly | compiled-in drivers (`=y`) |
| Services | Runtime (load/unload) | ReBAC, Auth, Agents, EventBus, Skills | user-space daemons (systemd units) |

**Services** depend on kernel interfaces, never the reverse. The kernel operates
without any services loaded.

**Distros:** Once kernel is minimal, different distributions (nexus-server, nexus-embedded,
nexus-cloud) compose kernel + selected drivers + selected services. Planned, not yet designed.

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

---

## 3. Kernel vs Services Boundary

### Kernel Interfaces (`nexus.core`)

| Interface | Linux Analogue | Purpose |
|-----------|---------------|---------|
| `MetastoreABC` | block device | Ordered KV primitive |
| `FileMetadataProtocol` | `struct inode_operations` | Typed FileMetadata CRUD over MetastoreABC |
| `VFSRouterProtocol` | VFS mount table | Path resolution + mount routing |
| `ObjectStoreABC` (= `Backend`) | `struct file_operations` | Blob I/O interface (read/write/delete/list) |
| `CacheStoreABC` | (no direct analogue) | Ephemeral KV + Pub/Sub primitives |

`FileMetadataProtocol` is kernel because it IS the inode layer — the typed contract
between VFS and Metastore. Without it, the kernel cannot describe files.

### NexusFS — Syscall Dispatch Layer

`NexusFS` is the kernel entry point, analogous to Linux's syscall layer (`sys_open`,
`sys_read`). It wires VFSRouter + FileMetadataProtocol + ObjectStoreABC into
user-facing operations (read, write, list, mkdir, mount).

**Current state:** NexusFS is a mixin-based god object that also contains service
code (ReBAC, OAuth, Skills, MCP, Events, Tasks). Mixins are compile-time composition —
cannot add/remove at runtime, cannot compose into different distros.

**Target state:** Composition + ServiceRegistry pattern. Kernel provides
`load_service()` / `unload_service()` for the Services tier (runtime swap).
Drivers remain config-time DI (static kernel + drivers never change at runtime,
following Linux's monolithic model). This enables:
- Distro composition: `nexus-server` loads all services, `nexus-embedded` loads none
- Clean kernel boundary: NexusFS shrinks to pure syscall dispatch

Migration is incremental: current "move X out of core" tasks extract mixins into
standalone service classes (Step 1), then a ServiceRegistry replaces inheritance (Step 2).

### Service Protocols (`nexus.services.protocols`)

| Protocol | Storage Affinity | Purpose |
|----------|-----------------|---------|
| `AgentRegistryProtocol` | RecordStore | Agent identity management |
| `NamespaceManagerProtocol` | RecordStore + CacheStore | ReBAC namespace views |
| `EventLogProtocol` | RecordStore | Append-only audit log |
| `HookEngineProtocol` | CacheStore | Pre/post operation hooks |
| `SchedulerProtocol` | CacheStore or RecordStore | Work queue |
| `ContextManifestProtocol` | (service models) | Deterministic context pre-execution |

All use `typing.Protocol` with `@runtime_checkable`.

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

## 6. gRPC Services

| Proto Service | Scope | Purpose |
|---------------|-------|---------|
| `ZoneTransportService` | Internal | Node-to-node Raft messages (StepMessage, ReplicateEntries) |
| `ZoneApiService` | Internal | Client-facing zone ops (Propose, Query, JoinZone, InviteZone) |

Named `Zone*` to match `ZoneConsensus` (Rust). Neither is an external API.

---

## 7. RecordStoreABC Pattern

Services consume `RecordStoreABC.session_factory` + SQLAlchemy ORM.
Direct SQL or raw driver access is an abstraction break.
This ensures driver interchangeability (PostgreSQL ↔ SQLite) without code changes.

---

## Cross-References

| Topic | Document |
|-------|----------|
| Data type → pillar mapping (50+ types) | `data-storage-matrix.md` |
| Storage orthogonality proof | `data-storage-matrix.md` §ORTHOGONALITY |
| Raft, gRPC, write flows | `federation-memo.md` §2–§5 |
| Zone model, DT_MOUNT | `federation-memo.md` §5–§6 |
| SC vs EC consistency | `federation-memo.md` §4.1 |
| API privilege levels (agents vs ops vs admin) | `federation-memo.md` §6.10 |
