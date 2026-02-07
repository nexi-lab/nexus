# Federation Architecture Memo

**Date:** 2026-02-07 (Last updated)
**Status:** Working memo (not a design doc)
**Author:** Engineering notes from federation recovery work

---

## 1. Current State Summary

### What Works
- **Raft consensus core** (Rust): 100% complete
  - `RaftNode` wrapping tikv/raft-rs `RawNode` with async propose API
  - `RaftStorage` backed by sled (persistent log, hard state, snapshots, compaction)
  - `FullStateMachine` (metadata + locks) and `WitnessStateMachine` (vote-only)
  - `WitnessStateMachineInMemory` for testing
  - All tests pass, clippy clean with `--all-features`
- **PyO3 FFI bindings**: `LocalRaft` class for same-box Python→Rust access (~5μs/op) ✅ **CI complete (#1234)**
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
- **Proto files**: `commands.proto` and `transport.proto` were never committed (being rebuilt from _pb2.py stubs)
- **gRPC transport**: Code exists in Python (`src/nexus/raft/`) but proto compilation is missing in CI
- **NexusFS integration**: RaftMetadataStore was integrated then reverted; currently using SQLAlchemy
- **Global tenant→zone rename**: ✅ Complete except `tenant.py` module filename + Azure OAuth references

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
  │ │  + RaftNode  │◄────┼──┤  + RaftNode  │──┤ RaftNode │  │  │
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
3. **RaftNode** — consensus participant (Leader or Follower, code-wise identical)
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

### 3.3 Identified Redundancies (Merge Candidates)

Following the **tenant→zone merge pattern**, 4 redundancies were identified:

1. **FilePathModel + FileMetadata** (Task #3, #11)
   - Both store file metadata
   - FilePathModel: Relational (SQLAlchemy), FK to zone
   - FileMetadata: Proto-generated, KV-style
   - **Recommendation**: Merge into single FileMetadata in sled, deprecate FilePathModel

2. **WorkspaceConfig + WorkspaceConfigModel** (Task #4)
   - WorkspaceConfig: In-memory dataclass
   - WorkspaceConfigModel: SQLAlchemy DB storage
   - **Recommendation**: Keep only WorkspaceConfigModel in sled (no in-memory duplication)

3. **MemoryConfig + MemoryConfigModel** (Task #5)
   - Same as above
   - **Recommendation**: Keep only MemoryConfigModel in sled

4. **Cluster Topology (standalone) → Merge into FileMetadata** (Task #6, #13)
   - Cluster topology doesn't need separate existence
   - **Recommendation**: Store as special metadata entries in sled (e.g., `/system/cluster/node-{id}`)

### 3.4 Pending Decisions (5 open)

1. **ReBACTupleModel storage** (Task #8): Keep SQLAlchemy (composite indexes) OR migrate to sled with custom indexes?
2. **FileEvent architecture** (Task #7): Raft event log (SC) OR keep Dragonfly pub/sub (EC)?
3. **UserSessionModel migration** (Task #9): Move to Redis/Dragonfly (session cache with TTL)?
4. **ContentCacheModel simplification** (Task #10): Remove DB metadata, use pure disk cache?
5. **FilePathModel merge strategy** (Task #11): Dual-mode support OR hard cutover?

### 3.5 Missing / Incomplete

- **Subscription/Delivery DB models** (Task #12): Pydantic models exist, but no SQLAlchemy storage found
- **Cluster Topology storage** (Task #13): No current implementation, needs clarification if it's derived from Raft metadata

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
│  RaftNode.send_message(peer_id, msg)                   │
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

#### 4.2.3 Storage Layer (Blob Storage)

```
┌─────────────────────────────────────────────────────────┐
│ USER SPACE                                              │
│                                                         │
│  Backend.put(path, stream)                             │
│  Backend.get(path) → AsyncIterator[bytes]             │
│  Backend.delete(path)                                  │
│  Backend.list(prefix) → AsyncIterator[str]            │
└─────────────────────────────────────────────────────────┘
                        ↓
┌─────────────────────────────────────────────────────────┐
│ KERNEL SPACE                                            │
│                                                         │
│  class StorageDriver(ABC):                             │
│    @abstractmethod                                      │
│    async def write_blob(key, data) → ETag              │
│    async def read_blob(key) → AsyncIterator[bytes]    │
│    async def delete_blob(key)                          │
│    async def list_blobs(prefix) → AsyncIterator[str]  │
└─────────────────────────────────────────────────────────┘
                        ↓
┌─────────────────────────────────────────────────────────┐
│ DRIVER LAYER                                            │
│                                                         │
│  LocalStorageDriver (local disk)                       │
│    - Zero-copy via mmap                                │
│    - XFS/ext4 backend                                  │
│                                                         │
│  S3StorageDriver (AWS S3)                              │
│    - Multipart upload, chunked download                │
│    - Boto3 SDK                                         │
│                                                         │
│  GCSStorageDriver (Google Cloud Storage)               │
│    - Same interface, google-cloud-storage SDK          │
│                                                         │
│  AzureBlobDriver (Azure Blob Storage)                  │
│    - azure-storage-blob SDK                            │
└─────────────────────────────────────────────────────────┘
```

#### 4.2.4 Client-Server Mode (RPC Service)

```
┌─────────────────────────────────────────────────────────┐
│ USER SPACE                                              │
│                                                         │
│  @app.post("/api/v1/files/write")                      │
│  async def write_file_rpc(req: WriteRequest):          │
│      return await nexusfs.write(req.path, req.data)    │
│                                                         │
│  @app.get("/api/v1/files/read")                        │
│  async def read_file_rpc(path: str):                   │
│      return await nexusfs.read(path)                   │
└─────────────────────────────────────────────────────────┘
                        ↓
┌─────────────────────────────────────────────────────────┐
│ KERNEL SPACE                                            │
│                                                         │
│  class RPCServiceProtocol(ABC):                        │
│    @abstractmethod                                      │
│    async def register_handler(method, handler)         │
│    async def serve(host, port)                         │
│    async def shutdown()                                │
└─────────────────────────────────────────────────────────┘
                        ↓
┌─────────────────────────────────────────────────────────┐
│ DRIVER LAYER                                            │
│                                                         │
│  FastAPIRPCService (HTTP/JSON)                         │
│    - REST-style API                                    │
│    - Pydantic validation                               │
│    - OpenAPI docs                                      │
│                                                         │
│  gRPCService (Protobuf/HTTP2)                          │
│    - Strongly-typed contracts                          │
│    - Streaming support                                 │
│    - Code generation from .proto                       │
└─────────────────────────────────────────────────────────┘
```

#### 4.2.5 Timer / Scheduler

```
┌─────────────────────────────────────────────────────────┐
│ USER SPACE                                              │
│                                                         │
│  scheduler.schedule_task(delay=60, task=gc_orphans)    │
│  scheduler.schedule_cron("0 0 * * *", backup)          │
│  scheduler.cancel_task(task_id)                        │
└─────────────────────────────────────────────────────────┘
                        ↓
┌─────────────────────────────────────────────────────────┐
│ KERNEL SPACE                                            │
│                                                         │
│  class TimerScheduler(ABC):                            │
│    @abstractmethod                                      │
│    async def schedule(delay, callback) → TaskID        │
│    async def schedule_interval(period, callback)       │
│    async def cancel(task_id)                           │
└─────────────────────────────────────────────────────────┘
                        ↓
┌─────────────────────────────────────────────────────────┐
│ DRIVER LAYER                                            │
│                                                         │
│  AsyncIOScheduler (single-node)                        │
│    - asyncio.call_later, loop-based                    │
│    - In-memory task queue                              │
│                                                         │
│  APSchedulerDriver (distributed)                       │
│    - Persistent job store (PostgreSQL)                 │
│    - Multi-node coordination                           │
│    - Cron/interval/date triggers                       │
└─────────────────────────────────────────────────────────┘
```

#### 4.2.6 Memory Management (Hot-Pluggable RAM, NOT Cache)

```
┌─────────────────────────────────────────────────────────┐
│ USER SPACE                                              │
│                                                         │
│  buffer = memory_pool.allocate(size=1MB)               │
│  memory_pool.deallocate(buffer)                        │
│  stats = memory_pool.get_usage_stats()                 │
└─────────────────────────────────────────────────────────┘
                        ↓
┌─────────────────────────────────────────────────────────┐
│ KERNEL SPACE                                            │
│                                                         │
│  class MemoryAllocator(ABC):                           │
│    @abstractmethod                                      │
│    def allocate(size) → Buffer                         │
│    def deallocate(buffer)                              │
│    def get_stats() → MemoryStats                       │
└─────────────────────────────────────────────────────────┘
                        ↓
┌─────────────────────────────────────────────────────────┐
│ DRIVER LAYER                                            │
│                                                         │
│  SystemMemoryAllocator (OS malloc)                     │
│    - Default Python heap                               │
│    - GC-managed                                        │
│                                                         │
│  HugePageAllocator (THP / 2MB pages)                   │
│    - Low TLB miss for large datasets                   │
│    - Manual madvise(MADV_HUGEPAGE)                     │
│                                                         │
│  NUMAAllocator (NUMA-aware)                            │
│    - Pin memory to CPU socket                          │
│    - Reduce cross-socket latency                       │
│                                                         │
│  SharedMemoryAllocator (IPC)                           │
│    - multiprocessing.shared_memory                     │
│    - Zero-copy between processes                       │
└─────────────────────────────────────────────────────────┘
```

**Note**: This is NOT cache (see 4.2.7). Memory management is about RAM allocation (hot-pluggable DIMMs on server hardware).

#### 4.2.7 Cache (CPU-Level, Distinct from Memory)

```
┌─────────────────────────────────────────────────────────┐
│ USER SPACE                                              │
│                                                         │
│  @cache_mixin.cached(ttl=60)                           │
│  async def get_user_permissions(user_id):              │
│      return await rebac.check(user_id)                 │
│                                                         │
│  tiger_cache.materialize(user_id, object_ids)          │
└─────────────────────────────────────────────────────────┘
                        ↓
┌─────────────────────────────────────────────────────────┐
│ KERNEL SPACE                                            │
│                                                         │
│  class CacheProtocol(ABC):                             │
│    @abstractmethod                                      │
│    async def get(key) → Optional[Value]                │
│    async def set(key, value, ttl)                      │
│    async def delete(key)                               │
│    async def invalidate_pattern(pattern)               │
└─────────────────────────────────────────────────────────┘
                        ↓
┌─────────────────────────────────────────────────────────┐
│ DRIVER LAYER                                            │
│                                                         │
│  DragonflyCache (in-memory, networked)                 │
│    - Redis protocol compatible                         │
│    - TTL support, LRU eviction                         │
│    - Shared across nodes                               │
│                                                         │
│  L1Cache (CPU cache emulation, process-local)          │
│    - DashMap (lock-free concurrent hashmap)            │
│    - Zero serialization overhead                       │
│    - Sub-microsecond latency                           │
│                                                         │
│  PostgreSQLCache (persistent fallback)                 │
│    - Materialize expensive queries                     │
│    - Survives process restart                          │
└─────────────────────────────────────────────────────────┘
```

**Distinction**:
- **Memory (4.2.6)**: Physical RAM allocation, like hot-pluggable DIMMs on motherboard
- **Cache (4.2.7)**: Fast temporary storage for computed results, like CPU L1/L2/L3 cache

#### 4.2.8 Consensus / Coordination

```
┌─────────────────────────────────────────────────────────┐
│ USER SPACE                                              │
│                                                         │
│  async with distributed_lock("file:/path/to/file"):    │
│      await nexusfs.write(path, data)                   │
│                                                         │
│  leader_id = consensus.get_leader()                    │
│  await consensus.propose(command)                      │
└─────────────────────────────────────────────────────────┘
                        ↓
┌─────────────────────────────────────────────────────────┐
│ KERNEL SPACE                                            │
│                                                         │
│  class ConsensusProtocol(ABC):                         │
│    @abstractmethod                                      │
│    async def propose(command) → ApplyResult            │
│    async def get_leader() → NodeID                     │
│    async def add_node(node_id, address)                │
│    async def remove_node(node_id)                      │
│                                                         │
│  class DistributedLock(ABC):                           │
│    @abstractmethod                                      │
│    async def acquire(key, ttl) → bool                  │
│    async def release(key)                              │
│    async def extend(key, ttl)                          │
└─────────────────────────────────────────────────────────┘
                        ↓
┌─────────────────────────────────────────────────────────┐
│ DRIVER LAYER                                            │
│                                                         │
│  RaftConsensus (Strong Consistency)                    │
│    - Linearizable reads/writes                         │
│    - Majority quorum required                          │
│    - Leader election via tikv/raft-rs                  │
│                                                         │
│  RedisLockManager (Dragonfly-backed)                   │
│    - Redlock algorithm                                 │
│    - Cross-platform coordination                       │
│    - ⚠️ Potentially deprecated post-Raft migration     │
└─────────────────────────────────────────────────────────┘
```

#### 4.2.9 Security / Authentication

```
┌─────────────────────────────────────────────────────────┐
│ USER SPACE                                              │
│                                                         │
│  @require_permission("file:read", path)                │
│  async def read_file_api(path: str):                   │
│      return await nexusfs.read(path)                   │
│                                                         │
│  user = await auth.authenticate(token)                 │
│  allowed = await auth.check_permission(user, action)   │
└─────────────────────────────────────────────────────────┘
                        ↓
┌─────────────────────────────────────────────────────────┐
│ KERNEL SPACE                                            │
│                                                         │
│  class AuthenticationProtocol(ABC):                    │
│    @abstractmethod                                      │
│    async def authenticate(credentials) → User          │
│    async def create_session(user) → Token             │
│    async def revoke_session(token)                     │
│                                                         │
│  class AuthorizationProtocol(ABC):                     │
│    @abstractmethod                                      │
│    async def check_permission(user, resource, action)  │
│    async def grant_permission(subject, object, role)   │
└─────────────────────────────────────────────────────────┘
                        ↓
┌─────────────────────────────────────────────────────────┐
│ DRIVER LAYER                                            │
│                                                         │
│  ZanzibarReBAC (Relationship-Based Access Control)     │
│    - Tuple-based (user:alice#member@group:eng)         │
│    - Leopard transitive closure (O(1) check)           │
│    - SQLAlchemy storage (composite indexes)            │
│                                                         │
│  OAuthProvider (Federated Authentication)              │
│    - Google, GitHub, Azure AD                          │
│    - JWT token validation                              │
│    - SQLAlchemy storage (encrypted tokens)             │
└─────────────────────────────────────────────────────────┘
```

### 4.3 Driver Registration & Hot-Swapping

```python
# System initialization (kernel boot)
from nexus.kernel import DriverRegistry

# Register drivers (config-driven)
registry = DriverRegistry()

if config.METADATA_STORE == "raft":
    registry.register(MetadataStore, RaftMetadataStore(config.raft_peers))
elif config.METADATA_STORE == "sqlalchemy":
    registry.register(MetadataStore, SQLAlchemyMetadataStore(config.database_url))

if config.STORAGE_BACKEND == "s3":
    registry.register(StorageDriver, S3StorageDriver(config.s3_bucket))
elif config.STORAGE_BACKEND == "local":
    registry.register(StorageDriver, LocalStorageDriver(config.data_dir))

# User space retrieves drivers from registry (dependency injection)
metadata_store = registry.get(MetadataStore)  # Returns configured driver
storage_driver = registry.get(StorageDriver)

# NexusFS is driver-agnostic
nexusfs = NexusFS(
    metadata_store=metadata_store,
    storage_driver=storage_driver,
)
```

**Hot-Swap Example** (no user space recompile):
```bash
# Production deployment A: PostgreSQL + S3
NEXUS_METADATA_STORE=sqlalchemy
NEXUS_STORAGE_BACKEND=s3

# Production deployment B: Raft + Local (edge node)
NEXUS_METADATA_STORE=raft
NEXUS_STORAGE_BACKEND=local

# Development: SQLite + Local
NEXUS_METADATA_STORE=sqlalchemy
NEXUS_DATABASE_URL=sqlite:///dev.db
NEXUS_STORAGE_BACKEND=local
```

Same binary, different drivers loaded at runtime.

### 4.4 Deployment Modes

| Mode | Metadata Store | Storage Driver | Transport | Consensus | Use Case |
|------|---------------|----------------|-----------|-----------|----------|
| **Single-Node (Dev)** | SQLAlchemyMetadataStore (SQLite) | LocalStorageDriver | HTTPTransport | N/A | Development, testing |
| **Single-Node (Raft)** | RaftMetadataStore (local PyO3) | LocalStorageDriver | HTTPTransport | RaftConsensus (1-node) | Performance testing |
| **Multi-Node (PostgreSQL)** | SQLAlchemyMetadataStore (PostgreSQL) | S3StorageDriver | HTTPTransport | N/A (DB serializable) | Current production |
| **Multi-Node (Raft SC)** | RaftMetadataStore (gRPC remote) | S3StorageDriver | gRPCTransport | RaftConsensus (3-node) | High availability, strong consistency |
| **Multi-Node (Raft EC)** | RaftMetadataStore (async replication) | S3StorageDriver | gRPCTransport | RaftConsensus (async) | High throughput, geo-distributed (Issue #1180) |

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
                              → RaftNode.propose()
                              → gRPC replicate to followers
                              → Majority ACK (2/3 or 2/2+witness)
                              → StateMachine.apply() on all nodes
                              → sled persist on all nodes
                         → Backend.write() → local/S3/GCS/...
```

**Key insight**: raft-rs only handles the consensus algorithm (log replication, leader election, state transitions). Transport (gRPC) is our responsibility — raft-rs outputs `Message` structs that we must deliver via our gRPC `RaftService`.

---

## 6. Zone Model

- **Path format**: `/zone:{zone_id}/path/to/file`
- **Physical**: Zones are flat and independent — each Zone is a Raft Group with its own root `/`
- **Logical**: Hierarchical namespace is composed through **Mount Points** (see 7a)
- **Intra-zone**: Raft consensus guarantees linearizable reads/writes
- **Cross-zone reads**: Client-side VFS traversal across mount points (no cross-zone metadata sync)
- **Cross-zone writes**: Two approaches planned:
  - **Plan A** (Issue #1181): Nexus-to-nexus mount — client traverses zone boundaries on read
  - **Plan B** (future): Spanner-like 2PC for cross-zone atomic transactions

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

#### Client-Side Traversal (ls -R /company/)

1. Client → Zone A: `list /` → returns `docs/, hr/, engineering(DT_MOUNT→Zone_B)`
2. Client sees DT_MOUNT, pauses recursion
3. Client resolves Zone_B address, connects
4. Client → Zone B: `list /` → returns `code/, design/`
5. Client merges Zone B results under `engineering/` name
6. Final result presented to user as unified tree

**Mixed consistency**: Zone A can be eventual, Zone B can be strong.
Client handles the boundary transparently.

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
- RaftNode + sled (consensus + embedded storage)
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

### 7f. Proto-to-Python Code Generation (SSOT Pattern)

**Status**: ✅ Complete. Implemented in commit 5da0bf1c.

- `scripts/gen_metadata.py` reads `proto/nexus/core/metadata.proto` and generates:
  - `src/nexus/core/_metadata_generated.py` — FileMetadata + PaginatedResult + MetadataStore ABC
  - `src/nexus/core/_compact_generated.py` — CompactFileMetadata with dict interning
- Old `metadata.py` and `compact_metadata.py` deleted
- All imports updated (20+ files), idempotent generation verified
- `_resolve_required()` for type-safe required fields (no mypy suppressions needed)

### 7g. NexusFS Raft Re-integration

**Status**: Was done (commit 9295b82e), reverted for CI (commit 46e7884b). **CI PyO3 now complete (#1234)**.

**Plan** (P1#7):
1. ✅ Add maturin build to CI (DONE: #1234)
2. Re-integrate RaftMetadataStore behind config flag (`NEXUS_METADATA_STORE=raft|sqlalchemy`)
3. Ensure all existing tests pass with both store backends
4. Blocked by: Data architecture decisions (Tasks #7-#11)

---

## 8. What's Needed to Reach Production Federation

### P0: Immediate (completed)
1. ✅ Restore accidentally deleted files
2. ✅ Rebuild proto SSOT files (`scripts/gen_metadata.py` + generated files)
3. ✅ Complete global tenant→zone rename (except `tenant.py` module filename + Azure OAuth)
4. ✅ Write this memo
5. ✅ Data-to-storage mapping analysis (`docs/architecture/data-storage-matrix.md`)

### P1: Short-term
6. ✅ Add maturin (PyO3) build to CI (#1234) — `test.yml` builds `nexus_raft` with `--features python`
   - Main `Dockerfile` deferred until production-ready (currently only nexus_fast)
   - gRPC feature (`--features python,grpc`) deferred until transport is ready for true federation
7. **Re-integrate RaftMetadataStore into NexusFS** (behind config flag)
   - Implement `NEXUS_METADATA_STORE=raft|sqlalchemy` config
   - Ensure all tests pass with both backends
   - **Blocked by**: Data architecture decisions (Tasks #7-#11)
8. **Complete data type merges** (Tasks #3-#6):
   - Merge FilePathModel + FileMetadata
   - Merge WorkspaceConfig duplicates
   - Merge MemoryConfig duplicates
   - Merge Cluster Topology into metadata
9. **Resolve storage decisions** (Tasks #7-#11):
   - FileEvent architecture (Raft vs Dragonfly)
   - ReBACTupleModel storage
   - UserSessionModel migration
   - ContentCacheModel simplification
   - FilePathModel merge strategy
10. Get gRPC transport compiling and tested (proto → tonic codegen)

### P2: Medium-term
11. Implement nexus-to-nexus mount (Issue #1181) for cross-zone reads
12. Multi-node integration tests with Docker Compose
13. Production deployment guide (both SQLAlchemy and Raft modes documented)
14. Investigate missing Subscription/Delivery DB storage (Task #12)
15. Clarify Dragonfly status (Task #7d)

### P3: Future
16. Cross-zone distributed transactions (Spanner-like 2PC)
17. ✅ ~~Zone nesting/overlaps design~~ Design decided (Section 7a: Mount Points + Hard Link lifecycle)
18. Write performance optimization (NexusFS.write() ~30ms/op)
19. Raft Eventual Consistency (EC) mode (Issue #1180)

---

## 9. Key Files Reference

| Component | File | Notes |
|-----------|------|-------|
| Raft node | `rust/nexus_raft/src/raft/node.rs` | RawNode wrapper, propose API |
| Raft storage | `rust/nexus_raft/src/raft/storage.rs` | sled-backed Storage trait impl |
| State machine | `rust/nexus_raft/src/raft/state_machine.rs` | Full + Witness + InMemory |
| PyO3 bindings | `rust/nexus_raft/src/pyo3_bindings.rs` | LocalRaft Python class |
| Raft proto | `rust/nexus_raft/proto/raft.proto` | gRPC transport definitions |
| Proto build | `rust/nexus_raft/build.rs` | tonic-build, expects `../../proto/` |
| RaftMetadataStore | `src/nexus/storage/raft_metadata_store.py` | Python Raft client (local+remote) |
| SQLAlchemyMetadataStore | `src/nexus/storage/sqlalchemy_metadata_store.py` | Current production store |
| Docker Compose | `dockerfiles/docker-compose.cross-platform-test.yml` | 3-node cluster template |
| gRPC stubs | `src/nexus/raft/*_pb2*.py` | Generated from proto (committed) |
| Data architecture | `docs/architecture/data-storage-matrix.md` | 50+ types, storage mapping, decisions |

---

## 10. Pending Tasks (Cross-Reference)

See `.claude/tasks/` for full task list. Key tasks related to this memo:

- Task #2: Verify storage medium orthogonality
- Task #3-6: Merge redundant data types (FilePathModel, WorkspaceConfig, MemoryConfig, Cluster Topology)
- Task #7-11: Storage architecture decisions (FileEvent, ReBAC, UserSession, ContentCache, FilePathModel)
- Task #12-13: Investigate missing storage (Subscription/Delivery, Cluster Topology derivation)

**Next Steps**: Resolve P1#7-9 (data architecture completion) before re-integrating RaftMetadataStore.