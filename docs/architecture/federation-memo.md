# Federation Architecture Memo

**Date:** 2026-02-09 (Last updated)
**Status:** Working memo (not a design doc)
**Author:** Engineering notes from federation recovery work

---

## 1. Current State Summary

### What Works
- **Raft consensus core** (Rust): 100% complete
  - `ZoneConsensus` wrapping tikv/raft-rs `RawNode` with async propose API
  - `RaftStorage` backed by sled (persistent log, hard state, snapshots, compaction)
  - `FullStateMachine` (metadata + locks) and `WitnessStateMachine` (vote-only)
  - `WitnessStateMachineInMemory` for testing
  - All tests pass, clippy clean with `--all-features`
- **PyO3 FFI bindings**: `Metastore` class for same-box Python→Rust sled access (~5μs/op) ✅ **CI complete (#1234)**
  - Metadata ops: set/get/delete/list
  - Lock ops: acquire/release/extend (mutex + semaphore)
  - Snapshot/restore
  - **Now builds in CI**: `.github/workflows/test.yml` includes `maturin develop --features python`
- **RaftMetadataStore** (Python): Full implementation with local (PyO3) and remote (gRPC) modes
  - Same interface as SQLAlchemyMetadataStore
  - Reverted from NexusFS integration for CI reasons (commit 46e7884b)
  - **Ready for re-integration** (P1#7 below)
- **Distributed locks**: RedisLockManager (Dragonfly-backed) for cross-platform coordination
  - ⚠️ **Status uncertain post-Raft migration** (see Section 7d)
- **CI**: 5 workflows all green (lint, test, docker, code quality, rpc-parity)

### What's Broken / Missing
- **gRPC transport**: Code exists in Python (`src/nexus/raft/`) but proto compilation is missing in CI (Task #34)
- **NexusFS integration**: RaftMetadataStore was integrated then reverted; currently using SQLAlchemy (Task #33, **NOW UNBLOCKED**)

---

## 2. Target Architecture (Production Federation)

```
                         ┌─────────────────────────────────────┐
                         │           Zone: us-west-1            │
                         │                                      │
  ┌──────────────────────┼──────────────────────────────────┐  │
  │ Node A (Leader)      │  Node B (Follower)    Node C     │  │
  │ ┌──────────────┐     │  ┌──────────────┐    (Witness)   │  │
  │ │   NexusFS    │     │  │   NexusFS    │  ┌──────────┐  │  │
  │ │  + RPC Srv   │     │  │  + RPC Srv   │  │ Vote-only│  │  │
  │ │  + ZoneConsensus  │◄────┼──┤  + ZoneConsensus  │──┤ ZoneConsensus │  │  │
  │ │              │  gRPC│  │              │  │          │  │  │
  │ │ StateMachine │     │  │ StateMachine │  │ (no SM)  │  │  │
  │ │  ├─ meta     │     │  │  ├─ meta     │  │          │  │  │
  │ │  └─ locks    │     │  │  └─ locks    │  │          │  │  │
  │ │              │     │  │              │  │          │  │  │
  │ │ sled (data)  │     │  │ sled (data)  │  │ sled(log)│  │  │
  │ └──────────────┘     │  └──────────────┘  └──────────┘  │  │
  └──────────────────────┼──────────────────────────────────┘  │
                         │                                      │
                         └─────────────────────────────────────┘
                                        │
                               Issue #1181
                           (nexus-to-nexus mount)
                                        │
                         ┌─────────────────────────────────────┐
                         │           Zone: eu-central-1         │
                         │        (same structure)              │
                         └─────────────────────────────────────┘
```

### Node Composition (same process)
Every non-witness node runs in a single process:
1. **NexusFS** — filesystem operations, backend connectors, caching
2. **RPC Server** — FastAPI (HTTP) + gRPC (Raft transport)
3. **ZoneConsensus** — consensus participant (Leader or Follower, code-wise identical)
4. **StateMachine** — metadata + locks, persisted in sled
5. **SQLAlchemy** — relational data (users, permissions, ReBAC)

Leader and Follower run the same binary. Role is determined by Raft election.

### Witness Node
- Participates in voting only (breaks ties for 2-node deployments)
- No state machine, no metadata storage
- Minimal resource footprint
- Config: `RaftConfig::witness(id, peers)`

---

## 3. Data Architecture

### 3.1 Design Principles

**First Principles Analysis**: Every data type must justify its existence. Redundancies are eliminated (like tenant→zone merge).

**Storage Medium Mapping**: 50+ data types analyzed across 8 property dimensions:
- Read/Write Performance (Low/Med/High/Critical)
- Consistency (EC/SC/Strict SC)
- Query Pattern (KV/Relational/Vector/Blob)
- Data Size, Cardinality, Durability, Scope

**Orthogonality Requirement**: Storage mediums must not overlap in purpose (Task #2).

### 3.2 Storage Layer Decisions

#### ✅ **Keep SQLAlchemy (PostgreSQL/SQLite)** - 20 types
**Use Cases**: Relational queries, FK, unique constraints, vector search, encryption, BRIN indexes

| Category | Data Types | Rationale |
|----------|-----------|-----------|
| **Users & Auth** | UserModel, UserOAuthAccountModel, OAuthCredentialModel | Relational queries, FK, unique constraints, encryption |
| **ReBAC (Partial)** | ReBACGroupClosureModel, ReBACChangelogModel | Materialized view (Leopard transitive closure), append-only BRIN |
| **Memory System** | MemoryModel, TrajectoryModel, TrajectoryFeedbackModel, PlaybookModel | Complex relational + vector search (pgvector for embeddings) |
| **Versioning** | VersionHistoryModel, WorkspaceSnapshotModel | Parent FK, BRIN time-series, zero-copy via CAS |
| **Semantic Search** | DocumentChunkModel | Vector index (pgvector/sqlite-vec) for embeddings |
| **Workflows** | WorkflowModel, WorkflowExecutionModel | Version tracking, FK, BRIN |
| **Zones** | ZoneModel, EntityRegistryModel, ExternalUserServiceModel | Unique constraints, hierarchical FK, encryption |
| **Audit** | OperationLogModel | Append-only BRIN |
| **Sandboxes** | SandboxMetadataModel | Relational queries |

#### ✅ **Migrate to sled via Raft (Strong Consistency)** - 8 types
**Use Cases**: KV access pattern, linearizable reads/writes in multi-node deployments

| Data Type | Current Storage | Migration Rationale |
|-----------|----------------|---------------------|
| **FileMetadata** (proto) | Generated dataclass | Core metadata, KV by path, SC via Raft |
| **DirectoryEntryModel** | SQLAlchemy | KV by parent_path, no JOINs needed |
| **FileMetadataModel** (KV) | SQLAlchemy | Arbitrary user-defined KV metadata |
| **ReBACNamespaceModel** | SQLAlchemy | KV by namespace_id, low cardinality |
| **SystemSettingsModel** | SQLAlchemy | KV by key, low cardinality |
| **WorkspaceConfig** | In-memory + SQLAlchemy | KV by path (merge duplicates, Task #4) |
| **MemoryConfig** | In-memory + SQLAlchemy | KV by path (merge duplicates, Task #5) |
| **Cluster Topology** | MISSING | Raft bootstrap info (merge with metadata, Task #6, #13) |

#### ✅ **Migrate to sled (Local, no Raft)** - 1 type
**Use Cases**: Content-addressed storage (CAS), immutable data

| Data Type | Current Storage | Rationale |
|-----------|----------------|-----------|
| **ContentChunkModel** | SQLAlchemy | KV by content_hash, immutable (no SC needed) |

#### ✅ **Keep Dragonfly (In-Memory Cache)** - 3 types
**Use Cases**: Performance cache with TTL, pub/sub

| Data Type | Current Storage | Rationale |
|-----------|----------------|-----------|
| **PermissionCacheProtocol** | Dragonfly/PostgreSQL/In-memory | Permission check cache, TTL |
| **TigerCacheProtocol** | Dragonfly/PostgreSQL | Pre-materialized bitmaps, TTL |
| **FileEvent** (pub/sub) | Dragonfly pub/sub | ⚠️ **NEEDS DECISION** (Task #7): Raft event log OR keep Dragonfly? |

### 3.3 Resolved Items (formerly open)

All data architecture decisions completed:
- ✅ Tasks #3-#6: Redundancies merged (FilePathModel, WorkspaceConfig, MemoryConfig, Cluster Topology)
- ✅ Tasks #7-#11: Storage affinity decided (FileEvent→CacheStore, ReBAC→RecordStore, UserSession→CacheStore, ContentCache→simplified, FilePathModel→merged)
- ✅ Task #13: Cluster Topology derived from Raft metadata
- ✅ Task #22: CacheStoreABC defined as Fourth Pillar
- ✅ Task #27: EmbeddingCache + ResourceMapCache abstracted to CacheStoreABC

### 3.4 Remaining Gap

- **Subscription/Delivery DB models** (Task #12): Pydantic models exist, but no SQLAlchemy storage found — needs investigation

**Full analysis**: See `docs/architecture/data-storage-matrix.md` (374 lines, 50+ types cataloged)

---

## 4. Decoupling Strategy: OS-Inspired Layered Architecture

### 4.1 Design Philosophy

**Mega-Decoupling**: Nexus architecture follows operating system design with strict separation:

```
┌─────────────────────────────────────────────────────────────┐
│                      USER SPACE                              │  ← Application Logic
│  (NexusFS API, FastAPI Routes, CLI Commands, Client SDKs)   │
└─────────────────────────────────────────────────────────────┘
                           ↓ syscall-like interface
┌─────────────────────────────────────────────────────────────┐
│                      KERNEL SPACE                            │  ← Interface Contracts
│  (ABCs: MetadataStore, TransportProtocol, StorageDriver,    │
│   CacheProtocol, TimerScheduler, MemoryAllocator, etc.)     │
└─────────────────────────────────────────────────────────────┘
                           ↓ driver registration
┌─────────────────────────────────────────────────────────────┐
│                    DRIVER / HAL LAYER                        │  ← Hot-Pluggable Implementations
│  (SQLAlchemyMetadataStore, RaftMetadataStore, gRPCTransport,│
│   S3StorageDriver, LocalStorageDriver, DragonflyCache, etc.)│
└─────────────────────────────────────────────────────────────┘
```

**Key Principle**: User space NEVER directly imports driver implementations. All access goes through kernel ABCs.

**Hot-Pluggable Modules**: Like Linux kernel modules, drivers are interchangeable at runtime/config time without recompiling user space.

### 4.2 Subsystem Layering

#### 4.2.1 Filesystem Layer

```
┌─────────────────────────────────────────────────────────┐
│ USER SPACE                                              │
│                                                         │
│  NexusFS.write(path, data)                             │
│  NexusFS.read(path) → bytes                            │
│  NexusFS.list(dir) → Iterator[FileMetadata]           │
│  NexusFS.stat(path) → FileMetadata                     │
└─────────────────────────────────────────────────────────┘
                        ↓
┌─────────────────────────────────────────────────────────┐
│ KERNEL SPACE                                            │
│                                                         │
│  class MetadataStore(ABC):                             │
│    @abstractmethod                                      │
│    async def get_metadata(path) → FileMetadata         │
│    async def set_metadata(path, metadata)              │
│    async def delete_metadata(path)                     │
│    async def list_metadata(prefix, limit) → Iterator   │
└─────────────────────────────────────────────────────────┘
                        ↓
┌─────────────────────────────────────────────────────────┐
│ DRIVER LAYER                                            │
│                                                         │
│  SQLAlchemyMetadataStore (PostgreSQL/SQLite)           │
│    - Relational queries, FK, BRIN indexes              │
│    - Serializable isolation                            │
│                                                         │
│  RaftMetadataStore (sled + Raft)                       │
│    - Local mode: PyO3 FFI (~5μs latency)               │
│    - Remote mode: gRPC to leader                       │
│    - Linearizable reads/writes                         │
└─────────────────────────────────────────────────────────┘
```

**Config-Based Selection**:
```bash
NEXUS_METADATA_STORE=raft        # Use Raft (SC mode)
NEXUS_METADATA_STORE=sqlalchemy  # Use PostgreSQL (current)
```

#### 4.2.2 Network Layer (Transport)

```
┌─────────────────────────────────────────────────────────┐
│ USER SPACE                                              │
│                                                         │
│  ZoneConsensus.send_message(peer_id, msg)                   │
│  EventBus.publish(event)                               │
│  RPC.call_remote_method(method, args)                  │
└─────────────────────────────────────────────────────────┘
                        ↓
┌─────────────────────────────────────────────────────────┐
│ KERNEL SPACE                                            │
│                                                         │
│  class TransportProtocol(ABC):                         │
│    @abstractmethod                                      │
│    async def send(peer, message) → Response            │
│    async def receive() → AsyncIterator[Message]        │
│    async def connect(address) → Connection             │
│                                                         │
│  class PubSubProtocol(ABC):                            │
│    @abstractmethod                                      │
│    async def publish(channel, data)                    │
│    async def subscribe(channel) → AsyncIterator        │
└─────────────────────────────────────────────────────────┘
                        ↓
┌─────────────────────────────────────────────────────────┐
│ DRIVER LAYER                                            │
│                                                         │
│  gRPCTransport (inter-node Raft replication)           │
│    - Tonic-based, Protobuf serialization               │
│    - Streaming for log transfer                        │
│                                                         │
│  HTTPTransport (client RPC)                            │
│    - JSON-RPC over HTTP/2                              │
│    - FastAPI backend                                   │
│                                                         │
│  DragonflyPubSub (event bus)                           │
│    - Redis protocol compatible                         │
│    - Channel-based broadcast                           │
└─────────────────────────────────────────────────────────┘
```

#### 4.2.3–4.2.9: Other Subsystems (Same User→Kernel→Driver Pattern)

All subsystems follow the same 3-layer pattern shown in 4.2.1–4.2.2. Summary:

| Subsystem | Kernel ABC | Drivers |
|-----------|-----------|---------|
| **4.2.3 Blob Storage** | `StorageDriver(ABC)` | Local, S3, GCS, Azure |
| **4.2.4 RPC Service** | `RPCServiceProtocol(ABC)` | FastAPI (HTTP), gRPC |
| **4.2.5 Timer/Scheduler** | `TimerScheduler(ABC)` | AsyncIO, APScheduler |
| **4.2.6 Memory** | `MemoryAllocator(ABC)` | System, HugePage, NUMA, SharedMem |
| **4.2.7 Cache** | `CacheProtocol(ABC)` | Dragonfly, L1Cache (DashMap), PostgreSQL |
| **4.2.8 Consensus** | `ConsensusProtocol(ABC)` + `DistributedLock(ABC)` | Raft, RedisLock |
| **4.2.9 Security** | `AuthenticationProtocol(ABC)` + `AuthorizationProtocol(ABC)` | ZanzibarReBAC, OAuth |

**Key distinctions**: Memory (4.2.6) = RAM allocation; Cache (4.2.7) = computed result storage.
Consensus (4.2.8) RedisLockManager potentially deprecated post-Raft.

### 4.3 The Nexus Quartet: Four Storage Pillars (Task #14)

**Design Philosophy**: Abstraction by **Capability** (Access Pattern & Consistency Guarantee),
not by business domain (`UserStore`) or implementation (`PostgresStore`).
Linux Kernel defines `BlockDevice`, `CharDevice`, `FileSystem` — Nexus defines four orthogonal storage primitives.
Names explain the **"What"** and **"Why"**, not the **"How"**.

**The Four Pillars**:

| Pillar | Role | Capability | Backing Driver | Kernel Status |
|--------|------|------------|----------------|---------------|
| **MetastoreABC** | "The Structure" (Brain/Skeleton) | Ordered KV, SC (Raft), CAS, Range Scan | sled (embedded/Raft) | **Required** init param |
| **RecordStoreABC** | "The Truth" (Memory/Library) | Relational (JOINs), ACID, Vector Search | PostgreSQL (prod) / SQLite (dev) | **Optional** — injected for Services (ReBAC, Auth, Audit, etc.) |
| **ObjectStoreABC** | "The Content" (Flesh/Warehouse) | Streaming I/O, Immutable Objects, Petabyte Scale | S3 / GCS / Local Disk | **Mounted** dynamically (= current `Backend` ABC) |
| **CacheStoreABC** | "The Reflexes" (Nerves/Signals) | Ephemeral KV, Pub/Sub, TTL | Dragonfly (prod) / In-Memory (dev) | ✅ **Implemented** (optional, graceful degrade) |

**Naming Rationale** (see Gemini design review for full analysis):
- **Metastore**: Industry standard for metadata engines (HDFS NameNode, Colossus Curator). Covers inodes + config + topology — not just FileMetadata.
- **RecordStore**: "System of Record" (SoR). Covers entities, relationships, logs, vectors — broader than "Registry".
- **ObjectStore**: Aligns with S3/GCS "Object Storage" terminology. Not a DB "Blob" field — an independent storage entity.
- **CacheStore**: Honest about ephemerality. Avoids "State" confusion with Raft state machine.

**Naming Clarification**: The existing proto-generated `MetadataStore` (from `metadata.proto`, specific to `FileMetadata` typed operations) will be renamed to `FileMetadataProtocol` to avoid confusion with `MetastoreABC` (the underlying ordered KV primitive).

**Data Type → Pillar Mapping** (see `data-storage-matrix.md` for full details):

- **Metastore**: FileMetadata (inodes), DirectoryEntry (dentries), CustomMetadata, SystemSettings, WorkspaceConfig, ContentChunkModel (CAS), ClusterTopology
- **RecordStore**: Users, ReBAC (Graph), Memory (Vector), Audit, Workflows, Versioning, Zones, Sandboxes, Search
- **ObjectStore**: File content (actual bytes on disk/cloud) — mounted via `router.add_mount()`
- **CacheStore**: UserSession, PermissionCache, TigerCache, FileEvent (pub/sub)

**CacheStore Note**: ✅ **Implemented** (Task #22, #27). `CacheStoreABC` unifies all cache access
behind a single ABC with drivers (Dragonfly, InMemory, Null). Domain caches (PermissionCache,
TigerCache, ResourceMapCache, EmbeddingCache) are driver-agnostic implementations in `nexus/cache/domain.py`,
built on CacheStoreABC primitives. NullCacheStore provides graceful degradation when no cache is available.

**Kernel Init** (dependency injection):

```python
class NexusFS:
    def __init__(
        self,
        # Required: Kernel core
        metastore: MetastoreABC,                    # sled: inodes, dentries, config, topology

        # Optional: Services layer (not kernel core)
        record_store: RecordStoreABC | None = None, # PG/SQLite: users, ReBAC, audit, vectors

        # ObjectStore (= Backend) is NOT an init param — mounted dynamically:
        #   nx.mount("/", LocalBackend(...))        # like: mount /dev/sda1 /
        #   nx.mount("/cloud", S3Backend(...))      # like: mount nfs://... /cloud

        # CacheStore: optional, NullCacheStore fallback when omitted (Task #22 done)
    ):
        self.vfs = VFS(metastore=metastore)
        # Services only initialized when record_store is provided
        if record_store:
            self.identity = IdentityService(record_store)
            self.memory = SemanticMemory(record_store)     # Uses vector search
```

> **Why optional?** Pure kernel only needs Metastore for inode CRUD (read/write/mkdir/ls).
> RecordStore is consumed by **Services** (ReBAC, Auth, Audit, Search, Workflows) that
> currently live inside NexusFS but conceptually belong in User Space — like how Linux's
> `/etc/passwd` is a file managed by user-space tools, not a kernel data structure.
> Tests exercising pure file operations need not provide a RecordStore.

**Deployment-Time Driver Selection** (config-driven, no recompile):
```bash
# Production: sled + PostgreSQL + S3 + Dragonfly
NEXUS_METASTORE=raft
NEXUS_RECORD_STORE=postgresql://...
NEXUS_OBJECT_STORE=s3://my-bucket

# Development: sled + SQLite + Local Disk (no Dragonfly)
NEXUS_METASTORE=local
NEXUS_RECORD_STORE=sqlite:///dev.db
NEXUS_OBJECT_STORE=local:./nexus-data
```

Same binary, different drivers loaded at **startup**.

**Limitations**:
- ❌ **Not true runtime hot-swapping**: Drivers selected at startup, cannot change without restart
- ❌ **Single driver per type**: Only one Metastore active at a time (no zone-specific drivers)
- ❌ **No graceful driver removal**: Cannot unload a driver while system is running

**Future**: True runtime hot-swapping (see Section 7o) will enable Linux-like `modprobe`/`rmmod` operations.

### 4.4 Deployment Modes

| Mode | Metastore | RecordStore | ObjectStore (mounted) | CacheStore | Use Case |
|------|-----------|-------------|----------------------|------------|----------|
| **Single-Node (Dev)** | sled (local PyO3) | SQLite | LocalBackend | In-Memory (future) | Development, testing |
| **Single-Node (Prod)** | sled (local PyO3) | PostgreSQL | LocalBackend / S3 | Dragonfly (future) | Small-scale production |
| **Multi-Node (Raft SC)** | sled (gRPC, Raft consensus) | PostgreSQL | S3 | Dragonfly (future) | HA, strong consistency |
| **Multi-Node (Raft EC)** | sled (async replication) | PostgreSQL | S3 | Dragonfly (future) | High throughput, geo-distributed (#1180) |

### 4.5 Raft Dual Mode: Strong vs Eventual Consistency (Issue #1180)

**Strong Consistency (SC) Mode** (default):
- All writes go through Raft consensus (majority ACK)
- Linearizable reads (Leader Read or Read Index)
- Latency: ~5-10ms (intra-DC), ~50-100ms (cross-region)
- Use case: Financial, legal, compliance workloads

**Eventual Consistency (EC) Mode** (opt-in):
- Writes replicate asynchronously (Leader ACK only)
- Reads may observe stale data (bounded staleness)
- Latency: ~1-2ms (local sled read)
- Use case: Media, content delivery, high-throughput ingestion

**Configuration**: Per-zone setting in `ZoneModel.consistency_mode` (SC/EC).

**Trade-offs**:
- SC: Lower throughput (~1K writes/sec), stronger guarantees
- EC: Higher throughput (~30K writes/sec), risk of data loss on leader crash

**Implementation Status**: SC mode complete (Raft core), EC mode planned (P3).

### 4.6 Migration Strategy

**No backward compatibility required** (project in early stage):
- Breaking schema changes acceptable
- Existing deployments can stay on SQLAlchemy indefinitely
- New deployments can choose Raft from day 1

**Gradual rollout**:
1. P1#7: Re-integrate RaftMetadataStore behind config (`NEXUS_METADATA_STORE=raft|sqlalchemy`)
2. P1#8-9: Complete data type merges and storage decisions (Tasks #3-#11)
3. P2: Production deployment guide with both modes documented
4. P3: Deprecate SQLAlchemy for metadata (keep for relational data)

---

## 5. Write Flow

### 5.1 Single-Node (Current Production Path)
```
Client → NexusFS.write() → SQLAlchemyMetadataStore → SQLite/PostgreSQL
                         → Backend.write() → local/S3/GCS/...
```

### 5.2 Single-Node with Raft (Local, Future)
```
Client → NexusFS.write() → RaftMetadataStore (local mode)
                              → PyO3 FFI (~5μs)
                              → FullStateMachine.apply()
                              → sled persist
                         → Backend.write() → local/S3/GCS/...
```

### 5.3 Multi-Node with Raft (Distributed, Future)
```
Client → NexusFS.write() → RaftMetadataStore (remote mode)
                              → ZoneConsensus.propose()
                              → gRPC replicate to followers
                              → Majority ACK (2/3 or 2/2+witness)
                              → StateMachine.apply() on all nodes
                              → sled persist on all nodes
                         → Backend.write() → local/S3/GCS/...
```

**Key insight**: raft-rs only handles the consensus algorithm (log replication, leader election, state transitions). Transport (gRPC) is our responsibility — raft-rs outputs `Message` structs that we must deliver via our gRPC `RaftService`.

---

## 6. Zone Model

### 6.1 Core Decision: Zone = Consensus Domain (DECIDED 2026-02-10)

A Zone is both a **logical namespace** and a **consensus boundary**:
- Each Zone has its own **independent Raft group** with its own sled database
- Zones do NOT share metadata — different zones have **separate, non-replicated** sled stores
- Cross-zone access requires **gRPC** calls (DT_MOUNT resolution)
- Visibility is enforced at the zone boundary, not by application-layer filtering

**Why not replicate all metadata to all nodes?**
1. **Security**: CN nodes should not have EU user metadata (GDPR, data sovereignty)
2. **Space**: Millions of users × thousands of files = sled cannot hold global metadata
3. **Latency**: Cross-continent Raft consensus adds 200ms+ per write

**Comparison with Google Spanner**:

| Spanner | NexusFS |
|---------|---------|
| Universe | **Federation** (全网唯一) |
| Zone (datacenter) | **Zone** (consensus domain, own Raft group) |
| Paxos Group (data shard) | Raft group (1:1 with zone for MVP; sharding later if needed) |
| Placement Driver | Manual zone placement (future: automatic) |
| Directory (placement unit) | Zone's entire metadata set |

**Key difference from Spanner**: Spanner's Paxos Group and Zone are orthogonal (a Paxos Group
spans multiple zones for HA, a zone hosts multiple Paxos Groups for sharding). In NexusFS,
Zone and Raft group are 1:1 for simplicity. If a single zone's metadata grows too large,
we can introduce Multi-Raft sharding within a zone (like TiKV), but this is not needed for MVP.

**Zone nesting via DT_MOUNT** (not Paxos Group nesting): Zones compose hierarchically through
mount points. A parent zone's DT_MOUNT entry points to a child zone's UUID + address.
This is a namespace-level concept, not a storage-level shard.

### 6.2 Mount = Create New Zone, All Voters (DECIDED 2026-02-11)

**Core decision**: Mounting creates a **new independent zone** with shared data.
All participating nodes are **equal Voters** in the new zone's Raft group.

**NFS-style UX** (familiar to everyone):
```bash
nexus mount /my-project bob:/my-project
```
- `bob` resolves via DNS-style zone discovery (§6.5) → finds Bob's node address
- `bob:/my-project` specifies the remote path to mount
- Initially `bob:/` if you don't know Bob's file structure (like NFS)
- Under the hood: system creates Zone_C, migrates `/my-project` data, both nodes join as Voters

**Why all-Voters in a new zone, not Learners in an existing zone?**
- Mount creates a **new independent zone** — all participants are equal
- Permissions (read-only vs read-write) handled by **ReBAC**, not Raft roles
- A Voter with read-only ReBAC can replicate the Raft log but gets rejected on writes
- Simpler model: no asymmetric Raft roles to manage for the common case
- Future optimization: Learner role for massive fan-out (1000+ readers of a public dataset)

**Why not redirect + cache (NFS-style remote reads)?**
- Redirect requires gRPC for every read (~200ms) — unacceptable for filesystem workloads
- Client-side cache requires cache invalidation — effectively re-inventing a weaker Raft
- Raft already solves "multiple parties see consistent view" — use it directly

**Mount semantics**:

| Aspect | Behavior |
|--------|----------|
| Mount = | Create new zone + all participants join as **Voters** |
| Read latency | ~5μs (local sled, page cache) — **always local** |
| Write latency | Raft propose → commit (~ms same region, ~200ms cross-continent) |
| Consistency | Linearizable (Raft guarantees) — **no cache invalidation needed** |
| Data locality | Full metadata replica in local sled (Voter has complete copy) |
| Unmount = | Node leaves Raft group, local sled data can be cleaned up |

**Authentication**: Reuse existing mechanisms — gRPC mutual TLS or SSH-style key exchange
at mount time. Same as NFS mount authentication. No new auth system needed.

**No redirect-only mode**: Every participant has a local sled replica through Raft.
If a zone owner doesn't want you to access their data, they don't grant mount permission.
"贡献 storage 的一方有绝对主动权" — the zone owner controls who can join.

### 6.3 Multi-Party Sharing (Example)

```
Scenario: Alice, Bob, and Charlie want to collaborate.

1. Alice shares with NFS-style mount:
   alice$ nexus mount /my-project bob:/my-project
   bob$   nexus mount /collab/alice bob:/my-project    # Bob chooses his local mount point

   Or implicit share (creates zone + invites in one step):
     nexus share /my-project --with bob,charlie

   Explicit equivalent:
     nexus zone create my-project-zone
     nexus mount /my-project my-project-zone:/
     nexus zone invite my-project-zone bob charlie

2. Zone_MyProject Raft group membership:
   Voters: [alice-node, bob-node, charlie-node]   ← all equal participants

3. All three have local sled replicas of Zone_MyProject:
   Alice:   /my-project → DT_MOUNT → Zone_MyProject      (local sled, ~5μs reads)
   Bob:     /collab/alice → DT_MOUNT → Zone_MyProject     (local sled, ~5μs reads)
   Charlie: /shared/project → DT_MOUNT → Zone_MyProject   (local sled, ~5μs reads)

4. Alice writes /my-project/new.txt:
   → Raft propose → committed by majority (2/3) → replicated to all
   → Bob and Charlie see new.txt immediately (no cache invalidation needed)
```

**Permissions**: ReBAC controls who can read vs write. A Voter with read-only
ReBAC permission can replicate the Raft log but gets rejected when proposing writes.

### 6.4 Implicit vs Explicit Zone Management (DECIDED 2026-02-11)

**Default: Implicit** (zone is an implementation detail, not a user concept).
**Available: Explicit** (for advanced users and admin operations).

| Mode | User sees | System does |
|------|-----------|-------------|
| **Implicit** | `nexus share /path --with user` | Auto zone create + DT_MOUNT + invite + Raft join |
| **Explicit** | `nexus zone create`, `nexus mount` | Manual zone lifecycle, Raft config, placement |

**Implicit share (`nexus share /path`)** internally:
1. Creates a new zone for the subtree
2. Migrates metadata from parent zone's sled → new zone's sled (like `git subtree split`)
3. Replaces original path with DT_MOUNT in parent zone
4. Creator's node becomes Voter in new zone's Raft group
5. Invited users' nodes join as **Voters** (All-Voter model — no Learner asymmetry)
6. Creates DT_MOUNT in each invited user's zone

**Implicit zone creation vs join** (decision logic):
- If the mount **contributes new metadata** (expands visibility of local data) → **create new zone**
- If the mount **only consumes** existing shared metadata (joining to view) → **join existing zone**

Example: node1 shares /folderA with node2 → creates zone-X (contributes metadata).
node3 mounts node2:/folderA → discovers zone-X already exists → joins as Voter (no new zone).

**When explicit is needed**:
- Zone migration to different region (`nexus zone migrate`)
- Raft configuration (voter/learner ratio, quorum size)
- Storage quota management
- Admin/ops operations

### 6.5 Peer Discovery: No Custom DNS Needed (DECIDED 2026-02-13)

**Source**: Architecture review, validated against current implementation.

**Decision**: Standard OS DNS + static bootstrap + Raft membership exchange covers all
peer discovery scenarios. **No custom NexusFS-level discovery mechanism needed.**

**Why no custom DNS?** Mounting = joining a Raft group as Voter. By the time a DT_MOUNT
entry exists, the node has already joined the target zone (via `JoinZone` RPC) and has
a local sled replica. Path resolution is always local (~5μs). There is no scenario where
path resolution encounters an "unknown" zone — if you can see the DT_MOUNT, you're
already a Voter in that zone.

**Three-layer discovery (all already implemented):**

| Layer | Mechanism | When | Status |
|-------|-----------|------|--------|
| **Bootstrap** | `NEXUS_PEERS` env var | First cluster formation (like etcd `--initial-cluster`) | ✅ Implemented |
| **First contact** | OS DNS (hostname → IP) | `join_zone(peers=["2@bob-laptop:2126"])` — tonic/tokio delegates to OS resolver | ✅ Automatic |
| **After join** | `JoinZoneResponse.ClusterConfig` | Returns all voter `NodeInfo{id, address}` | ✅ Implemented |
| **Ongoing** | Raft `ConfChange` | Membership changes propagated automatically | ✅ Implemented |

**OS DNS covers all deployment modes transparently:**
- Docker Compose: container name = hostname (Docker DNS)
- Kubernetes: service name resolves via kube-dns/CoreDNS
- LAN: mDNS/Bonjour (`.local` hostnames resolve automatically)
- Public: standard DNS

**Path resolution across zones** (all local, no network hops):
```
Path: /company/engineering/team1/file.txt

  Step 1: Root Zone (local sled) → /company is DT_MOUNT → Zone_Company
  Step 2: Zone_Company (local sled) → /engineering is DT_MOUNT → Zone_Eng
  Step 3: Zone_Eng (local sled) → /team1/file.txt → return FileMetadata

  All reads are local (~5μs) because mounting = Voter = full local replica.
  No "DNS resolution" on the read path. Ever.
```

**Node bootstrap flow:**
```
nexus start (first time)
  → bootstrap(root_zone_id="root", peers=None)
  → create_zone("root", peers=[])       # single-node Raft group
  → put("/", DT_DIR, i_links_count=1)   # POSIX root self-reference

nexus start (multi-node static bootstrap)
  → NEXUS_PEERS="2@node2:2126,3@node3:2126"
  → bootstrap(root_zone_id="root", peers=[...])
  → create_zone("root", peers=[...])    # N-node Raft group
  → put("/", DT_DIR, i_links_count=1)
```

**Previous design note**: An earlier draft (§6.5 pre-2026-02-13) proposed DNS-style
hierarchical zone discovery with a Root Zone acting like DNS root servers, client-side
PathResolver caches, and optional global search via Bloom Filter/DHT. This is
**superseded** — all of those mechanisms are unnecessary because (a) Voters have local
replicas so path resolution never hits the network, and (b) OS DNS already handles
hostname→IP for the `JoinZone` RPC. The only "discovery" needed is knowing at least one
peer's hostname to send the initial `JoinZone` request, which is a standard networking
problem solved by DNS, not a filesystem problem.

### 6.6 DT_MOUNT Entry Structure

```python
class DT_MOUNT:
    name: str               # Mount point name in parent directory
    entry_type: "DT_MOUNT"  # Alongside DT_DIR, DT_REG
    target_zone_id: str     # Target zone UUID
    # No target_address needed: mounting node is a Raft Learner,
    # so it has local sled replica. Address is resolved at mount time
    # and stored in Raft group membership, not in the DT_MOUNT entry.
```

**Mount conflict**: Reject if path already exists as DT_DIR or DT_REG (NFS-style,
no shadow). See Issue #1326 for shadow discussion.

**Reference counting**: `i_links_count` tracks DT_MOUNT refs to a zone.
New zone starts at 1 (owner reference). Each DT_MOUNT adds 1. Drop to 0 is impossible
(owner ref is implicit). `nexus zone destroy` required for actual deletion.

### 6.7 Metastore as Cache Backing Store

Since mounting = joining Raft group (All-Voter model), every mounted zone has a
**local sled replica**. The existing `Metastore` serves dual purpose:

| Use case | sled instance | Raft? | Data |
|----------|--------------|-------|------|
| **Own zone** | RaftConsensus (SC/EC) | Yes (Voter) | This zone's metadata |
| **Mounted zone** | RaftConsensus | Yes (Voter) | Shared zone's metadata |

Both use sled's built-in page cache (~5μs for hot data). **No separate cache layer needed.**
Raft log replication IS the cache invalidation mechanism.

**Note (2026-02-11)**: An earlier design discussed a separate `cache_sled` (local-only,
no Raft) for caching remote zone metadata. This is **superseded** by the All-Voter model —
since every participant has a full local replica via Raft, the authoritative sled IS the cache.
No `cache_sled` or Dragonfly needed for metadata caching.

### 6.8 Implications for Current Code

| Component | Single-Zone (now) | Multi-Zone (federation) |
|-----------|-------------------|------------------------|
| `_create_metadata_store()` | 1 RaftMetadataStore | 1 per zone the node participates in |
| Mount operation | N/A | Create new zone, all participants join as Voters |
| Read path | Local sled | Local sled (always, Voter has full replica) |
| Write path | Raft propose | Raft propose to target zone's leader |
| Node discovery | `NEXUS_PEERS` (static) | OS DNS + `NEXUS_PEERS` + `JoinZoneResponse.ClusterConfig` |
| 3-node compose | 1 zone, 3 replicas | Still 1 zone; multi-zone needs multiple Raft groups |
| Multi-Raft sharding | N/A | Future: Issue #1327 (when single zone too large) |

---

## 7. Open Questions & Future Design Work

### 7a. Inter-Zone Architecture: Mount Points & Zone Lifecycle

**Status**: Design decided. Implementation pending (P2).

**Source**: Discussion in `document-ai/notes/Nexus Federation inter-zones 架构设计重新决策 (消息131-142).md`

#### Core Principle: Flattened Storage + Hierarchical Mounts

Zones are physically flat and isolated. The global namespace tree is an illusion
constructed by **mount point entries** (`DT_MOUNT`) in parent zones.

```
Physical Reality (what Raft sees):         Logical View (what users see):

  Zone_A (Company):  /, docs/, hr/           /company/
  Zone_B (Eng):      /, code/, design/         ├── docs/
  Zone_C (Wife):     /, photos/                ├── hr/
                                               ├── engineering/  → [Zone_B]
                                               └── ceo_wife/     → [Zone_C]
```

Zone A stores `engineering` as `DT_MOUNT → Zone_B_UUID` — it knows nothing about
Zone B's contents. Zone A and Zone B **never sync metadata**.

#### Directory Entry: DT_MOUNT

A new entry type alongside `DT_DIR` and `DT_REG`:

| Field | Value |
|-------|-------|
| `name` | `engineering` |
| `entry_type` | `DT_MOUNT` |
| `target_zone_id` | `Zone_B_UUID` |

#### Cross-Zone Read Path (All-Voter Model)

Since mounting = creating a new zone where all participants are Voters, cross-zone reads
are **always local**:

1. Node has local sled replicas for ALL zones it participates in (Voter)
2. `ls -R /company/` → Zone A sled (local) → sees DT_MOUNT → Zone B sled (also local)
3. All reads are ~5μs — no gRPC needed for data access
4. gRPC is only used for Raft log replication (background, async)

**Mixed consistency**: Zone A can be EC, Zone B can be SC. Each zone's Raft group
operates independently. The node participates as **Voter** in both.

#### Unified Mount Logic (DRY)

Creating a child zone and manually mounting a cross-zone path are the **same operation**:

```python
# System topology (automatic):
nexus zone create /company/engineering  →  link_zone("/company", "engineering", Zone_B_UUID)

# User mount (manual):
nexus mount /home/wife /company/ceo_wife  →  link_zone("/company", "ceo_wife", Zone_C_UUID)
```

One mechanism for all zone relationships.

#### Zone Lifecycle: Hard Link Model (shared_ptr semantics)

Mount points are **Hard Links** to zones with reference counting (`i_links_count`):

| Action | Operation | RefCnt | Data Fate |
|--------|-----------|--------|-----------|
| `nexus zone create` | `new Zone()` | 0 → 1 | Created |
| `nexus mount /a/b` | `link(Zone, "/a/b")` | 1 → 2 | Accessible |
| `rm /a/b` | `unlink("/a/b")` | 2 → 1 | Hidden (safe) |
| `nexus zone destroy` | `delete Zone` | 1 → 0 | Destroyed |

**Safety net**: Every Zone has an implicit system-level link from its Owner.
Even if all mount points are removed, `i_links_count ≥ 1` (the Owner reference).
Orphaned zones appear in `/nexus/trash/` — explicit `nexus zone destroy` required
to truly delete data.

#### Permissions: Gatekeeper at the Door

- **Parent zone** controls: "Can you see this mount point exists?"
- **Target zone** controls: "Can you enter this zone?" (RBAC check at zone boundary)

Example: Wife can see `ceo` directory name in `/home`, but Zone Private denies her entry.

#### User-Centric Root (Chroot by Default)

Each user's root is determined by their zone registry (ordered KV scan):
- CEO's first key: `/zones/001/company/` → mounts as `/`
- Eng's first key: `/zones/002/engineering/` → mounts as `/`

Users don't see parent zones they don't have access to. No complex ACL needed to
hide upper directories — the namespace boundary is the zone boundary.

#### Edge Cases

- **Orphan zones**: GC agent scans for unreferenced zones → moves to lost+found, notifies admin
- **Cycle detection**: Check at mount time (hierarchy is shallow), or set max recursion depth
- **Zone down**: Parent still shows mount point name, but entering returns Transport Error

### 7b. Write Performance (NexusFS.write() ~30ms/op)

**Status**: Known bottleneck, not yet addressed.

**Symptoms**: Writing 1000 files takes ~30 seconds (30ms per write). sled itself is ~0.014ms/op, so 99.95% of time is in Python/NexusFS overhead.

**Suspected bottleneck breakdown**:
- CAS (content-addressable storage) hash computation
- `cache_mixin` cache invalidation
- `auto_parse` thread spawning
- SQLAlchemy session commit overhead
- Permission checks per write
- Hierarchy/directory index updates

**TODO**: Profile `NexusFS.write()` to identify exact bottleneck distribution. Consider:
- Batch write API (single transaction for N files)
- Async permission checks
- Deferred directory index updates
- sled-native metadata (bypass SQLAlchemy entirely when using Raft)

### 7c. Multi-Node Deployment & Testing

**Status**: Docker Compose template exists, not yet tested end-to-end.

**What exists**:
- `dockerfiles/docker-compose.cross-platform-test.yml` (3-node: 2 full + 1 witness + Dragonfly)
- All Raft core logic is tested in unit tests

**What's missing**:
- Actual multi-node integration test that starts 3 containers and verifies consensus
- Network partition testing (kill a node, verify failover)
- Leader re-election timing measurements
- Snapshot transfer between nodes

**Full Node Docker Image Goal**: Each container should be a complete Nexus node capable of acting as both a federation participant and a client-server backend:
- NexusFS (filesystem ops, backend connectors, caching)
- FastAPI (HTTP API)
- RPC Server (client-facing RPC)
- ZoneConsensus + sled (consensus + embedded storage)
- gRPC transport (inter-node Raft replication)
- SQLAlchemy (users, permissions, ReBAC)

This "full node" image will serve as the unit for `docker-compose.cross-platform-test.yml` (dev/test) and eventually the production `Dockerfile`. The test compose environment (`docker-compose.cross-platform-test.yml`) evolves from single-node → distributed as components land; main `Dockerfile` updated only when production-ready.

**TODO**: After gRPC transport is functional, create a proper multi-node test suite.

### 7d. Dragonfly Status Post-Raft Migration

**Status**: ⚠️ **POTENTIALLY BROKEN** (needs clarification)

**Current usage**:
- RedisLockManager (distributed locks via Dragonfly)
- PermissionCacheProtocol (permission check cache)
- TigerCacheProtocol (pre-materialized permission bitmaps)
- FileEvent pub/sub (file change notifications)

**Post-Raft considerations**:
- **Distributed locks**: Raft now provides consensus-based locks (mutex, semaphore) via `FullStateMachine`
  - **Question**: Should RedisLockManager be deprecated? Or keep for cross-platform (non-Raft) scenarios?
- **Permission caches**: Can stay in Dragonfly (performance cache, not SSOT)
- **FileEvent pub/sub**: See Task #7 (Raft event log vs Dragonfly pub/sub decision)

**Action needed**: Clarify if Dragonfly becomes optional or remains required for caching.

### 7e. Cross-Zone Federation (Plan B: Spanner-like 2PC)

**Status**: Not started. Plan A (Issue #1181, nexus-to-nexus mount) comes first.

**When to consider Plan B**: If we need atomic writes that span multiple zones (e.g., move a file from zone A to zone B atomically).

**Rough approach**:
- Each zone has its own Raft group
- A coordinator (TBD: which node?) runs 2PC across zone leaders
- Phase 1: Prepare (all zones lock resources, write to WAL)
- Phase 2: Commit (all zones apply, release locks)
- Requires distributed deadlock detection if zones can cross-reference

**TODO**: Evaluate if Plan A (mount) is sufficient for 90%+ of cross-zone use cases before investing in 2PC.

### ~~7f. Proto-to-Python Code Generation~~ ✅ COMPLETE

Implemented in commit 5da0bf1c. See `scripts/gen_metadata.py`.

### 7g. NexusFS Raft Re-integration → Task #33

**Status**: **UNBLOCKED** — all blockers resolved.

- ✅ CI PyO3 build (#1234)
- ✅ Data architecture decisions (Tasks #7-#11)
- ✅ NexusFS constructor refactor (Task #14 — Four Pillars DI)

**Next**: Implement `NEXUS_METADATA_STORE=raft|sqlalchemy` config, re-integrate RaftMetadataStore.

### ~~7h. NexusFS Constructor Pattern Refactor~~ ✅ COMPLETE (Task #14)

Implemented as Four Pillars DI: `NexusFS(metastore=..., record_store=..., backend=...)`.
Constructor injection + backward compatibility via `db_path` fallback. See Task #14.

### 7i. Microkernel Refactoring: True Kernel Extraction (Task #15)

**Status**: Design decided. Implementation P2. **Source**: `document-ai/notes/...msg143-146.md`

**Goal**: Kernel = "Local RPC Router" (VFS + IPC + Raft + Permission Gate). Everything else = user-mode driver.

**3-Layer Architecture**: User Space (Agents) → System Servers (Drivers) → Microkernel (nexus-core)

**Extraction targets**: Storage I/O → `fs-driver-*`, Timer → `sys-driver-timer`, HTTP → `sys-driver-net`, Auth Signing → `sys-driver-auth`, Boardroom Logic → User Space Agent.

**Interrupt model**: Agent writes to `/sys/timer/sleep` → Kernel forwards to driver → Driver completes → Kernel unblocks Agent. Kernel has zero timer/HTTP/signing code.

### 7j. Memory/Cache Tiering (Task #16)

**Status**: Design decided. Implementation P2. **Source**: `document-ai/notes/...msg143-146.md`

**Key Decision**: Two distinct cache patterns:

| Layer | Location | Pattern | Technology | Latency |
|-------|----------|---------|------------|---------|
| **L0** | Kernel internal | Decorator (`#[cached]`) | sled | ~50ns |
| **L1** | User-mode driver | ABC (HAL) | Dragonfly `/dev/mem/hot` | ~1ms |
| **L2** | User-mode driver | ABC (HAL) | PostgreSQL `/dev/mem/vector` | ~5ms |

**L0 stays in kernel** (cannot tolerate RPC). L1/L2 are hot-pluggable via `MemoryDriverProtocol(ABC)`.

### 7k. Identity System: PCB-Based Binding (Task #17)

**Status**: Design decided. Implementation P2. **Source**: `document-ai/notes/...msg147-150.md`

**Core idea**: Bind identity at process spawn (like Linux PID) — immutable for lifetime.

```rust
struct NexusTaskStruct { pid: u32, identity: String, zone_id: ZoneID, caps: Capabilities }
```

**Progressive Isolation**:

| Level | Mode | Identity Binding | I/O Monopoly | Mechanism |
|-------|------|------------------|--------------|-----------|
| **0** | Host Process | Weak | No | SO_PEERCRED |
| **1** | Docker | Strong | Yes | Mount point |
| **2** | Wasm (future) | Perfect | Yes | Memory isolation |

Migration: JWT (external clients) + PCB (internal agents) coexist.

### 7l. Auth Separation: Verify/Sign Split (Task #18)

**Status**: Design decided. Implementation P2. **Source**: `document-ai/notes/...msg147-150.md`

**Split `AuthenticationProtocol`** into:
- **Kernel**: `AuthVerifyProtocol` — `verify_token()` ~50ns (ed25519), every request
- **Driver** (`sys-driver-auth`): `AuthSignProtocol` — `login()` ~50-500ms (DB + OAuth), once per session

Kernel stays fast (zero DB/OAuth deps). TEE extension possible for Sign operations.

### 7m. Nexus Native IPC: Pipe Implementation (Task #19)

**Status**: Design decided. Implementation P2. **Source**: `document-ai/notes/...msg151-160.md`

**Design**: `DT_PIPE` inode type, ring buffer file at `/nexus/pipes/{name}`.

Advantages over Linux pipes: Observable, persistent, network-transparent (Raft), ReBAC-controlled.

Can coexist with Raft Event Log (SC) and Dragonfly Pub/Sub (high-throughput).

### 7n. Container Strategy: I/O Monopoly (Task #20)

**Status**: Design decided. Implementation P2. **Source**: `document-ai/notes/...msg151-160.md`

**Core**: Agent's only I/O channel is Nexus. Docker with `--network none`, single mount `/mnt/nexus`, `--read-only`. Config: `NEXUS_ISOLATION_LEVEL=0|1|2`.

### 7o. True Runtime Hot-Swapping (Task #21)

**Status**: Conceptual design. Implementation P2-P3.

**Goal**: Linux kernel module semantics (`modprobe`/`rmmod`) for Nexus drivers.

**Key distinction**: DT_MOUNT = cross-zone filesystem mounting (user-facing). Hot-swap = kernel module loading (infra-facing). Different concepts, different terminology.

**Open questions**: Driver identification, state migration strategy, fallback strategy, SC/EC mode switching, concurrency control during switch. Need consensus before implementation.

**Phases**: P1 ✅ (constructor DI), P2 (DriverRegistry + zone-aware routing), P3 (state migration + fallback).

**Related**: Issue #1180 (consistency mode migration).

---

## 8. What's Needed to Reach Production Federation

### ✅ P0 + P1 Foundation — ALL COMPLETE

- ✅ File recovery, proto SSOT, tenant→zone rename, memo, data-storage matrix
- ✅ CI PyO3 build (#1234)
- ✅ Data type merges (Tasks #3-#6) + storage decisions (Tasks #7-#11)
- ✅ NexusFS Four Pillars constructor refactor (Task #14)
- ✅ CacheStoreABC + domain caches (Tasks #22, #27, #28)
- ✅ Services extraction + FileMetadataProtocol rename (Tasks #23, #24)

### P1: Remaining (Critical Path)

| # | Task | Ref | Status |
|---|------|-----|--------|
| **#33** | Re-integrate RaftMetadataStore (`NEXUS_METADATA_STORE=raft`) | §7g | **UNBLOCKED** |
| **#34** | gRPC transport compile + test (inter-node Raft) | §5.3 | Blocked by #33 |

### P2: Medium-term

| # | Task | Ref | Blocked By |
|---|------|-----|------------|
| #35 | Cross-zone DT_MOUNT (#1181) | §7a | #34 |
| #36 | Multi-node Docker integration tests | §7c | #34 |
| #37 | Production deployment guide | §4.4 | #36 |
| #12 | Investigate Subscription/Delivery storage | §3.4 | — |
| #15 | Microkernel: extract Timer/HTTP drivers | §7i | — |
| #16 | Memory HAL with /dev/mem drivers | §7j | — |
| #17 | PCB-based Identity (SO_PEERCRED) | §7k | — |
| #18 | Auth Verify/Sign split | §7l | — |
| #19 | Nexus Native Pipe (ring buffer) | §7m | — |
| #20 | Docker I/O Monopoly | §7n | — |
| #21 | Runtime hot-swapping (DriverRegistry) | §7o | — |
| #29 | Clean up core/ (move non-kernel code) | — | — |

### P3: Future

| # | Task | Ref |
|---|------|-----|
| #38 | Raft EC mode (#1180) | §4.5 |
| #39 | Cross-zone 2PC / Spanner-like (#1233) | §7e |
| — | Write perf optimization (~30ms/op) | §7b |
| — | Wasm agent runtime (Level 2) | §7k/7n |
| — | TEE driver (privacy computing) | §7l |
| — | Advanced hot-swapping (state migration, fallback) | §7o |

---

## 9. Key Files Reference

| Component | File | Notes |
|-----------|------|-------|
| Raft node | `rust/nexus_raft/src/raft/node.rs` | RawNode wrapper, propose API |
| Raft storage | `rust/nexus_raft/src/raft/storage.rs` | sled-backed Storage trait impl |
| State machine | `rust/nexus_raft/src/raft/state_machine.rs` | Full + Witness + InMemory |
| PyO3 bindings | `rust/nexus_raft/src/pyo3_bindings.rs` | Metastore + RaftConsensus Python classes |
| Raft proto | `rust/nexus_raft/proto/raft.proto` | gRPC transport definitions |
| Proto build | `rust/nexus_raft/build.rs` | tonic-build, expects `../../proto/` |
| RaftMetadataStore | `src/nexus/storage/raft_metadata_store.py` | Python Raft client (local+remote) |
| SQLAlchemyMetadataStore | `src/nexus/storage/sqlalchemy_metadata_store.py` | Current production store |
| Docker Compose | `dockerfiles/docker-compose.cross-platform-test.yml` | 3-node cluster template |
| gRPC stubs | `src/nexus/raft/*_pb2*.py` | Generated from proto (committed) |
| Data architecture | `docs/architecture/data-storage-matrix.md` | 50+ types, storage mapping, decisions |

---

## 10. Dependency Graph & Task Cross-Reference

See `.claude/tasks/` for full task list. See Section 8 for per-task details. Updated 2026-02-09.

### Dependency Graph

```
═══════════════════════════════════════════════════════════════════════════
 FEDERATION CRITICAL PATH (sequential chain)
═══════════════════════════════════════════════════════════════════════════

    ┌─────────────────────────────────────────────────────────────────┐
    │  FOUNDATION (ALL DONE)                                          │
    │  #2-#14, #22-#28: Data arch, Four Pillars DI, CacheStoreABC,  │
    │  Proto SSOT, Services extraction, domain caches                │
    └───────────────────────────┬─────────────────────────────────────┘
                                │
                ╔═══════════════╧═══════════════╗
                ║  #33 Raft Re-integration  [P1] ║ ← NEXT STEP (unblocked!)
                ║  NEXUS_METADATA_STORE=raft     ║
                ╚═══════════════╤═══════════════╝
                                │
                ╔═══════════════╧═══════════════╗
                ║  #34 gRPC Transport       [P1] ║
                ║  Inter-node Raft replication   ║
                ╚═══════╤═══════════════╤═══════╝
                        │               │
           ┌────────────┘               └────────────┐
           │                                         │
  ╔════════╧══════════╗                  ╔═══════════╧═══════════╗
  ║  #35 DT_MOUNT [P2]║                  ║ #36 Multi-node    [P2]║
  ║  Cross-zone mount  ║                  ║ Docker tests         ║
  ║  Issue #1181       ║                  ╚═══╤═══════════╤═════╝
  ╚════════╤══════════╝                       │           │
           │                       ╔══════════╧═══╗ ╔════╧════════════╗
  ╔════════╧══════════╗            ║#37 Deploy [P2]║ ║ #38 Raft EC [P3]║
  ║ #39 2PC       [P3]║            ║ Guide         ║ ║ Issue #1180     ║
  ║ Issue #1233        ║            ╚══════════════╝ ╚════════════════╝
  ╚═══════════════════╝

═══════════════════════════════════════════════════════════════════════════
 ARCHITECTURAL IMPROVEMENTS (parallel, independent of critical path)
═══════════════════════════════════════════════════════════════════════════

  ┌─ Microkernel Track ────────────────────────────────────────────────┐
  │                                                                    │
  │  #15 Extract Timer/HTTP ──→ #16 Memory HAL (/dev/mem drivers)     │
  │       (sys-driver-*)              (mem-driver-dragonfly/pg)        │
  │                                                                    │
  │  #29 Clean up core/ (move non-kernel code to proper modules)      │
  │                                                                    │
  └────────────────────────────────────────────────────────────────────┘

  ┌─ Security Track ───────────────────────────────────────────────────┐
  │                                                                    │
  │  #17 PCB Identity ────→ #20 Docker I/O Monopoly                   │
  │      (SO_PEERCRED)          (container isolation)                  │
  │                                                                    │
  │  #18 Auth Verify/Sign Split (kernel verify vs driver sign)        │
  │                                                                    │
  └────────────────────────────────────────────────────────────────────┘

  ┌─ IPC Track ────────────────────────────────────────────────────────┐
  │                                                                    │
  │  #19 Nexus Native Pipe (DT_PIPE, ring buffer, blocking I/O)      │
  │                                                                    │
  └────────────────────────────────────────────────────────────────────┘

  ┌─ Infrastructure Track ─────────────────────────────────────────────┐
  │                                                                    │
  │  #21 Runtime Hot-Swapping (DriverRegistry, zone-aware routing)    │
  │  #12 Investigate Subscription/Delivery storage (data gap)         │
  │                                                                    │
  └────────────────────────────────────────────────────────────────────┘

═══════════════════════════════════════════════════════════════════════════
 P3 FUTURE (no task yet)
═══════════════════════════════════════════════════════════════════════════

  Write perf (~30ms/op)  │  Wasm runtime (Level 2)  │  TEE privacy
  Advanced hot-swap      │  (state migration, fallback, /sys/kernel/drivers)
```

### Next Step

**Task #33 is the critical path bottleneck.** All P0/P1 data architecture work is complete.
Re-integrating RaftMetadataStore (behind config flag) unlocks the entire federation chain.
