# Nexus Lego Architecture

**Date:** 2026-02-14
**Status:** Active design document
**References:**
- `docs/architecture/federation-memo.md` — Federation details, Raft implementation, Zone model
- `docs/architecture/data-storage-matrix.md` — Complete 50+ data type catalog, storage affinity analysis

---

## 1. Design Philosophy

NexusFS follows an **OS-inspired layered architecture** with strict separation between
application logic, interface contracts, and pluggable implementations.

```
┌──────────────────────────────────────────────────────────────┐
│                       USER SPACE                             │
│  NexusFS API, FastAPI Routes, CLI Commands, Client SDKs     │
└──────────────────────────────────────────────────────────────┘
                          ↓ protocol interface
┌──────────────────────────────────────────────────────────────┐
│                      KERNEL SPACE                            │
│  Protocols: VFSRouterProtocol, FileMetadataProtocol          │
│  ABCs: MetastoreABC, ObjectStoreABC (= Backend)             │
└──────────────────────────────────────────────────────────────┘
                          ↓ driver registration
┌──────────────────────────────────────────────────────────────┐
│                    DRIVER / HAL LAYER                        │
│  redb (local/Raft), S3, GCS, LocalDisk, gRPC transport      │
└──────────────────────────────────────────────────────────────┘
```

**Key rules:**
- User space never imports driver implementations directly — all access through ABCs/Protocols
- Drivers are interchangeable at startup via config (same binary, different drivers)
- Adding something to kernel requires strong justification (OS best practice)

---

## 2. The Four Storage Pillars

NexusFS abstracts storage by **Capability** (access pattern + consistency guarantee),
not by domain (`UserStore`) or implementation (`PostgresStore`).
Inspired by Linux's `BlockDevice` / `CharDevice` / `FileSystem` model.

| Pillar | ABC | Capability | Drivers | Kernel Status |
|--------|-----|------------|---------|---------------|
| **Metastore** | `MetastoreABC` | Ordered KV, SC (Raft), CAS, prefix scan | redb (local PyO3 / gRPC Raft) | **Required** init param |
| **RecordStore** | `RecordStoreABC` | Relational ACID, JOINs, FK, vector search | PostgreSQL (prod), SQLite (dev) | **Optional** — for Services |
| **ObjectStore** | `ObjectStoreABC` (= `Backend`) | Streaming I/O, immutable objects, petabyte scale | S3, GCS, Local Disk | **Mounted** dynamically |
| **CacheStore** | `CacheStoreABC` | Ephemeral KV, Pub/Sub, TTL | Dragonfly (prod), In-Memory (dev) | **Optional** — graceful degrade |

### Naming Rationale

- **Metastore**: Industry standard for metadata engines (HDFS NameNode, Colossus Curator). Covers inodes + config + topology.
- **RecordStore**: "System of Record" (SoR). Covers entities, relationships, logs, vectors.
- **ObjectStore**: Aligns with S3/GCS "Object Storage" terminology — an independent storage entity.
- **CacheStore**: Honest about ephemerality. Avoids "State" confusion with Raft state machine.

### Orthogonality

Storage mediums are orthogonal **between pillars** (different query patterns).
Drivers are interchangeable **within pillars** (same pattern, different operational profiles).

| Pair | Why Orthogonal |
|------|---------------|
| Ordered KV vs Relational | KV prefix scan vs JOINs/FK — fundamentally different query patterns |
| Ordered KV vs Ephemeral KV | Persistent + linearizable (Raft) vs ephemeral + TTL eviction |
| Relational vs Blob | Structured small records vs unstructured huge objects |
| Ephemeral KV vs Blob | Tiny TTL entries + pub/sub vs petabyte streaming I/O |

See `data-storage-matrix.md` §STORAGE MEDIUM ORTHOGONALITY ANALYSIS for full proof.

### Kernel Self-Inclusiveness

Kernel requires exactly **2 storage mediums**: Ordered KV (Metastore) + Blob (ObjectStore).

| Kernel need | Pillar | Storage property |
|-------------|--------|-----------------|
| File metadata (inode) | Metastore | KV by path |
| Directory index (dentry) | Metastore | Ordered prefix scan |
| Zone revision tracking | Metastore | `/__sys__/` KV entries |
| System settings | Metastore | KV by key |
| File content (bytes) | ObjectStore | Blob by path |

Kernel does NOT need: JOINs, FK, vector search, TTL, pub/sub.
Those are service-layer concerns (RecordStore / CacheStore).

### CacheStore Graceful Degradation

CacheStore is **optional with graceful degrade** (not "fallback"):
- No CacheStore → EventBus disabled
- No CacheStore → PermissionCache direct-queries RecordStore
- No CacheStore → TigerCache O(n) scan
- No CacheStore → UserSession stays in RecordStore

`NullCacheStore` implements `CacheStoreABC` as no-op for kernel-only / dev mode.

---

## 3. Kernel vs Services Boundary

### Kernel Protocols (`nexus.core.protocols`)

Only **VFSRouterProtocol** lives in kernel — it is a fundamental filesystem concern
(mount table + virtual path routing).

```python
# nexus/core/protocols/vfs_router.py
@runtime_checkable
class VFSRouterProtocol(Protocol):
    def add_mount(self, mount_point: str, backend: Backend, ...) -> None: ...
    def resolve(self, path: str) -> ResolvedPath: ...
    def list_mounts(self) -> list[MountInfo]: ...
    def remove_mount(self, mount_point: str) -> None: ...
```

### Service Protocols (`nexus.services.protocols`)

Six protocols define service-layer contracts. Their implementations depend on one or more
of the Four Pillars (not kernel primitives themselves).

| Protocol | Storage Affinity | Purpose |
|----------|-----------------|---------|
| `AgentRegistryProtocol` | RecordStore | Register, lookup, authenticate agents |
| `NamespaceManagerProtocol` | RecordStore + CacheStore | Mount, unmount, resolve ReBAC namespace views |
| `EventLogProtocol` | RecordStore | Append-only BRIN audit log |
| `HookEngineProtocol` | CacheStore | Ephemeral hook registration, pre/post fire |
| `SchedulerProtocol` | CacheStore or RecordStore | Work queue: submit, next |
| `ContextManifestProtocol` | (service models) | Deterministic pre-execution of context sources |

All use `typing.Protocol` with `@runtime_checkable` for structural subtyping.

### Pillar ABCs (Kernel-Level Contracts)

| ABC | Location | Purpose |
|-----|----------|---------|
| `MetastoreABC` | `nexus.core` | Ordered KV primitive (get/set/delete/list/prefix_scan) |
| `FileMetadataProtocol` | `nexus.core` | Typed wrapper over MetastoreABC for FileMetadata operations |
| `ObjectStoreABC` (= `Backend`) | `nexus.backends` | Blob storage primitive (read/write/delete/list) |
| `RecordStoreABC` | `nexus.storage` | Relational ACID access (session_factory + SQLAlchemy ORM) |
| `CacheStoreABC` | `nexus.core` | Ephemeral KV + Pub/Sub primitives (get/set/delete/publish/subscribe) |

**Naming clarification**: `MetastoreABC` is the low-level ordered KV store.
`FileMetadataProtocol` is a typed wrapper that sits on top (specific to `FileMetadata` operations).
These are distinct — like a block device vs a filesystem.

---

## 4. Deployment Modes

Same binary, different drivers loaded at startup:

| Mode | Metastore | RecordStore | ObjectStore | CacheStore |
|------|-----------|-------------|-------------|------------|
| **Dev (single-node)** | redb (local) | SQLite | Local Disk | In-Memory |
| **Prod (single-node)** | redb (local) | PostgreSQL | S3 / Local | Dragonfly |
| **Prod (multi-node SC)** | redb (Raft) | PostgreSQL | S3 | Dragonfly |
| **Prod (multi-node EC)** | redb (async repl) | PostgreSQL | S3 | Dragonfly |

```bash
# Production
NEXUS_METASTORE=raft
NEXUS_RECORD_STORE=postgresql://...
NEXUS_OBJECT_STORE=s3://my-bucket

# Development
NEXUS_METASTORE=local
NEXUS_RECORD_STORE=sqlite:///dev.db
NEXUS_OBJECT_STORE=local:./nexus-data
```

---

## 5. Zone = Consensus Domain

A Zone is both a **logical namespace** and a **consensus boundary**:

- Each Zone has its own **independent Raft group** with its own redb database
- Zones do NOT share metadata — different zones have separate, non-replicated stores
- Cross-zone access requires gRPC calls (DT_MOUNT resolution)
- 1 Zone = 1 Raft group (MVP; Multi-Raft sharding within zone is future)

### Mount = Create New Zone, All Voters

```bash
nexus mount /path bob:/path    # NFS-style UX
```

- Creates NEW independent zone — all participants are **equal Voters** (no Learner asymmetry)
- Permissions (read/write) via ReBAC, not Raft roles
- Auth: gRPC mutual TLS or SSH-style (reuse existing mechanisms)
- EC mode writes = local apply + async replicate (~5μs), equivalent to local write

### DT_MOUNT: Cross-Zone Namespace Composition

- `DT_MOUNT` entries in Metastore link zones into a namespace tree (NFS-style)
- Reject on path conflict — no shadow mounts
- DNS-style hierarchical zone discovery — no global registry on critical path
- Each zone only knows its direct children (stored in DT_MOUNT entries)

See `federation-memo.md` §5–§6 for implementation details.

---

## 6. API Privilege Levels

Federation operations are **not** agent-facing. Like Linux's `mount(2)` requiring `CAP_SYS_ADMIN`,
zone lifecycle is an ops/admin concern — agents only see paths.

| Level | Who | API | Examples |
|-------|-----|-----|----------|
| **File I/O** | Agents, users | `nx.read/write/list/mkdir/delete` | Transparent — VFS + DT_MOUNT routes automatically |
| **Federation orchestration** | Ops scripts | `NexusFederation.share/join` | Programmatic zone sharing |
| **Zone lifecycle** | Admin | `nexus zone create/mount/unmount` (CLI) | Cluster management |

Agents do NOT get mount/unmount APIs. If an agent needs access to a remote path,
an admin pre-mounts it. This matches the OS model: processes don't mount filesystems.

---

## 7. Kernel Init (Dependency Injection)

```python
class NexusFS:
    def __init__(
        self,
        metastore: MetastoreABC,                     # Required: kernel core
        record_store: RecordStoreABC | None = None,   # Optional: services
        # ObjectStore mounted dynamically: nx.mount("/", LocalBackend(...))
        # CacheStore: optional, NullCacheStore when omitted
    ):
        self.vfs = VFS(metastore=metastore)
        if record_store:
            self.identity = IdentityService(record_store)
            self.memory = SemanticMemory(record_store)  # vector search
```

Pure kernel only needs Metastore for inode CRUD.
RecordStore is consumed by **Services** (ReBAC, Auth, Audit, Search, Workflows).
Tests exercising pure file operations need not provide a RecordStore.

---

## 7. gRPC Service Naming

| Proto Service | Purpose | Consumers |
|---------------|---------|-----------|
| `ZoneTransportService` | Node-to-node Raft message forwarding (StepMessage, ReplicateEntries) | Internal only (ZoneConsensus) |
| `ZoneApiService` | Client-facing zone operations (Propose, Query, JoinZone, InviteZone) | RaftClient, RaftMetadataStore |

Previously named `RaftService` / `RaftClientService` — renamed to `Zone*` for consistency
with `ZoneConsensus` (Rust) and to clarify the internal vs API distinction.

---

## 8. RecordStoreABC Consumption Pattern

Services consume `RecordStoreABC` through its `session_factory` + SQLAlchemy ORM.
Direct SQL or raw asyncpg access is an abstraction break.

```python
# Correct: use session_factory + ORM
class MyService:
    def __init__(self, record_store: RecordStoreABC):
        self._session_factory = record_store.session_factory

    async def get_item(self, item_id: str) -> Item:
        async with asyncio.to_thread(self._session_factory) as session:
            return session.query(ItemModel).get(item_id)
```

This ensures driver interchangeability (PostgreSQL ↔ SQLite) without code changes.

---

## Cross-References

| Topic | Document |
|-------|----------|
| Complete data type → pillar mapping (50+ types) | `data-storage-matrix.md` |
| Storage medium orthogonality proof | `data-storage-matrix.md` §ORTHOGONALITY |
| Raft implementation, gRPC transport | `federation-memo.md` §2–§4 |
| Zone model, DT_MOUNT, federation | `federation-memo.md` §5–§6 |
| Write flow (single-node, multi-node) | `federation-memo.md` §5 |
| SC vs EC consistency modes | `federation-memo.md` §4.5 |
