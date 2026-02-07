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

### 4.3 Driver Selection (Config-Driven, Startup-Time)

**Current Implementation (P1)**: Config-driven driver selection at initialization.

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

**Deployment-Time Driver Selection** (no user space recompile):
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

Same binary, different drivers loaded at **startup**.

**Limitations**:
- ❌ **Not true runtime hot-swapping**: Drivers selected at startup, cannot change without restart
- ❌ **Single driver per type**: Only one MetadataStore active at a time (no zone-specific drivers)
- ❌ **No graceful driver removal**: Cannot unload a driver while system is running

**Future**: True runtime hot-swapping (see Section 7o) will enable Linux-like `modprobe`/`rmmod` operations.

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
4. Blocked by: Data architecture decisions (Tasks #7-#11) + NexusFS constructor refactor (7h)

### 7h. NexusFS Constructor Pattern Refactor (Hot-Swapping Support)

**Status**: ⚠️ **PENDING DISCUSSION** — Architecture improvement proposal, not yet confirmed.

**Current Problem** (Line 215 in `nexus_fs.py`):
```python
# ❌ Hard-coded dependency on SQLAlchemyMetadataStore
self.metadata = SQLAlchemyMetadataStore(
    db_path=db_path,
    enable_cache=enable_metadata_cache,
    ...
)
```

**Issues**:
1. ❌ **Tight coupling**: NexusFS directly depends on concrete `SQLAlchemyMetadataStore` class
2. ❌ **No hot-swapping**: Cannot switch to `RaftMetadataStore` at runtime
3. ❌ **Violates Open-Closed Principle**: Adding new store requires modifying NexusFS code
4. ❌ **Test inflexibility**: Fixtures cannot inject mock stores easily

**Proposed Solution: Constructor Injection + Factory Pattern**

```python
class NexusFS(...):
    def __init__(
        self,
        backend: Backend,
        metadata_store: MetadataStore | None = None,  # Priority 1: Direct injection
        config: dict | None = None,  # Priority 2: Config-driven factory
        db_path: str | Path | None = None,  # Priority 3: Backward compatibility
        # ... other parameters
    ):
        # Priority 1: User directly injects store
        if metadata_store is not None:
            self.metadata = metadata_store
        # Priority 2: Create via config (using MetadataStoreFactory)
        elif config is not None:
            self.metadata = MetadataStoreFactory.create(config)
        # Priority 3: Backward compatibility - default to SQLAlchemy
        else:
            if db_path is None:
                db_path = Path("./nexus-metadata.db")
            self.metadata = SQLAlchemyMetadataStore(db_path=db_path, ...)
```

**Benefits**:
- ✅ **Flexibility**: Three usage modes (injection / config / default)
- ✅ **Backward compatible**: Existing code requires no changes
- ✅ **Test-friendly**: Fixtures can inject mocks
- ✅ **Hot-swappable**: Config-driven switching between stores
- ✅ **Clear dependencies**: Explicit, no global state

**Alternative Patterns Considered**:
- ❌ **Decorator Pattern**: Not applicable (we're not "adding features", we're "switching implementations")
- ❌ **Service Locator**: Introduces global state, harder to test
- ✅ **Factory Pattern**: Good as auxiliary (combined with constructor injection)

**Usage Examples**:
```python
# Production: Raft mode (direct injection)
raft_store = RaftMetadataStore(peers=["node1", "node2", "node3"])
nexusfs = NexusFS(backend=backend, metadata_store=raft_store)

# Production: Config-driven (environment variables)
config = {"NEXUS_METADATA_STORE": "raft", "RAFT_PEERS": "node1,node2,node3"}
nexusfs = NexusFS(backend=backend, config=config)

# Production: Backward compatible (existing code)
nexusfs = NexusFS(backend=backend, db_path="nexus.db")  # Auto-creates SQLAlchemy

# Testing: Mock store
nexusfs = NexusFS(backend=backend, metadata_store=mock_store)
```

**Implementation Priority**: P1 (as dependency for P1#7 - RaftMetadataStore re-integration)

**TODO**:
1. 📋 **Discuss and confirm** approach with team before implementation
2. 🛠️ Implement `MetadataStoreFactory` with config parsing
3. 🔧 Refactor `NexusFS.__init__()` to support injection + factory + backward compatibility
4. ✅ Update all tests to use new pattern (optional, backward compatible)
5. 📚 Document new usage patterns in README

**Related**: Section 4.3 (Driver Registration & Hot-Swapping), P1#7 (RaftMetadataStore re-integration)

### 7i. Microkernel Refactoring: True Kernel Extraction

**Status**: Design direction from Gemini discussions. Implementation P2-P3.

**Source**: `document-ai/notes/Nexus Microkernel Refactoring Cache Separation and Design Philosophy (msg143-146).md`

#### Philosophy: Protocol vs Implementation

Current Nexus architecture (Section 4) separates User/Kernel/Driver layers. The Microkernel refactor **deepens this separation** by moving ALL features out of the kernel, leaving only the "switchboard" (routing + consistency).

**Key Principle**: For a true microkernel where `nexus-core` is "decent small" (CI < 2 minutes), we must identify what the kernel truly needs vs what can be external drivers.

#### 3-Layer Architecture (Finer-Grained)

```
┌─────────────────────────────────────────────────────────┐
│ Layer 3: USER SPACE (Agents)                           │
│   - Copilot, Worker, Boardroom Chairman                │
│   - Application logic only                              │
└─────────────────────────────────────────────────────────┘
                         ↓ IPC (Everything is a File)
┌─────────────────────────────────────────────────────────┐
│ Layer 2: SYSTEM SERVERS (User-Mode Drivers)            │
│   - fs-driver-s3: Real S3 read/write                   │
│   - fs-driver-local: Real disk I/O                     │
│   - sys-driver-timer: /sys/timer implementation        │
│   - sys-driver-net: /sys/net/http implementation       │
│   - sys-driver-auth: Token signing (not verify)        │
│   - mem-driver-dragonfly: /dev/mem/hot cache           │
│   - mem-driver-pg: /dev/mem/vector storage             │
└─────────────────────────────────────────────────────────┘
                         ↓ Mount Table + Message Passing
┌─────────────────────────────────────────────────────────┐
│ Layer 1: MICROKERNEL (nexus-core)                      │
│   - VFS Interface: Defines read/write/list/stat        │
│   - Message Passing (IPC): Routes Agent→Driver         │
│   - Consistency Engine: Raft + Directory Tree topology │
│   - Permission Gate: ACL checks                         │
│   - Wait Queue: Block/unblock for I/O completion       │
│                                                         │
│   Code Size: Extremely small, CI < 2 minutes           │
└─────────────────────────────────────────────────────────┘
```

**Analogy for Hackers**: Nexus Microkernel is a "Local RPC Router". It doesn't do storage, networking, or timers — it only routes messages and maintains the tree structure.

#### The Interrupt Model: Timer Example

**Bad (Monolithic)**: Kernel contains timer loop logic
```rust
// ❌ In Kernel
loop {
   if now() > target_time { notify_agent(); }
}
```
This bloats the kernel with business logic (timer features, HTTP client, etc.).

**Good (Microkernel)**: Kernel only routes and unblocks

1. **Agent**: `write("/sys/timer/sleep", "5000")` → blocks
2. **Kernel**: Forwards request to `sys-driver-timer` (doesn't know what timer is)
3. **Driver**: Calls host OS `setTimeout`, waits 5 seconds
4. **Driver**: Sends "Done" to Kernel
5. **Kernel**: Unblocks Agent (interrupt handler behavior)

**Result**: Kernel has zero timer code, only forwarding + wait queue management.

#### Decoupling Candidates (What to Extract)

Following the Timer model, extract these features from `nexus-core`:

| Feature | Current Location | Target Driver | Rationale |
|---------|-----------------|---------------|-----------|
| **Storage I/O** | Direct S3/Local imports | `fs-driver-s3`, `fs-driver-local` | Kernel only stores Inode tree, not file contents |
| **HTTP Client** | `reqwest` imports | `sys-driver-net` | Agent writes to `/sys/net/http/request`, driver fetches |
| **Timer/Scheduler** | APScheduler logic | `sys-driver-timer` | See Interrupt Model above |
| **Auth Signing** | JWT generation | `sys-driver-auth` | See 7l (Kernel only verifies, driver signs) |
| **Boardroom Logic** | Vote/Proposal code | User Space Agent | Kernel provides atomic `mkdir`/`mv`, flow is user logic |

**Implementation Strategy**: Start with Timer and HTTP Client (easiest to identify), then progressively extract others.

**Trade-off Discussion**:
- **Gemini's view**: Everything extractable should be extracted for purity
- **Our consideration**: Some "drivers" like Metadata (sled) may need to stay in-kernel for performance (see 7j for distinction)
- **Decision pending**: Need profiling data to decide on a case-by-case basis

**TODO** (P2):
1. Audit `nexus-core` imports: Flag any `import aws_sdk`, `reqwest`, `chrono` (timers), `jsonwebtoken` (signing)
2. Create `nexus-drivers/` directory with initial drivers (timer, http, auth)
3. Define `/sys/*` and `/dev/*` mount point protocols
4. Refactor Kernel to pure VFS + IPC + Permission Gate

### 7j. Memory/Cache Tiering: Implementation Pattern Distinction

**Status**: Design direction from Gemini discussions. Implementation P2-P3.

**Source**: `document-ai/notes/Nexus Microkernel Refactoring Cache Separation and Design Philosophy (msg143-146).md`

#### Conceptual Alignment: Cache = RAM in OS

Since Nexus is an OS, we treat cache as "Memory (RAM)". However, **cache management has two distinct patterns**:

1. **Kernel Internal Cache (L1)**: Like CPU L1/L2 cache — stays in kernel, uses **decorator pattern**
2. **External Cache (L2/L3)**: Like RAM DIMMs — hot-pluggable, uses **ABC pattern** (HAL)

**Critical Distinction**: This refines Section 4.2.6 (Memory) and 4.2.7 (Cache).

#### 3-Layer Memory Tiering

**Layer 0: Metadata Store (Kernel Internal)**
- **Technology**: Sled (embedded KV store)
- **Location**: Inside `nexus-core` (NOT extracted as driver)
- **Content**: Inode Table, Mount Points, Raft State, Active Locks
- **Pattern**: **Decorator** (not ABC) — internal optimization, not hot-pluggable
- **Rationale**: Sub-microsecond latency required, cannot tolerate RPC overhead
- **Implementation**:
  ```rust
  // Rust: Use macros like #[cached] for transparent caching
  #[cached(size = 1000, time = 60)]
  fn get_inode(path: &str) -> Option<Inode> {
      sled_db.get(path.as_bytes())
  }
  ```
- **Visibility**: Kernel exposes cache stats via `/sys/kernel/cache/stats` (read-only)

**Layer 1: Hot Cache (User-Mode Driver)**
- **Technology**: Dragonfly (Redis-compatible)
- **Location**: `mem-driver-dragonfly` (external process)
- **Content**: Boardroom Pub/Sub, Agent short-term memory, File write-back buffers
- **Pattern**: **ABC** (HAL) — hot-pluggable, replaceable with Redis/Valkey
- **Mount Point**: `/dev/mem/hot` or `/dev/mem/fast`
- **Interface**:
  ```python
  class MemoryDriverProtocol(ABC):
      @abstractmethod
      async def read_page(key: str) -> bytes
      async def write_page(key: str, data: bytes, ttl: int)
      async def evict_page(key: str)
  ```
- **Characteristics**: Volatile, high throughput, TTL eviction

**Layer 2: Warm/Vector Store (User-Mode Driver)**
- **Technology**: PostgreSQL (pgvector)
- **Location**: `mem-driver-pg` (external process)
- **Content**: GraphRAG index, historical audit logs, semantic search cache
- **Pattern**: **ABC** (HAL) — hot-pluggable
- **Mount Point**: `/dev/mem/vector` or `/dev/mem/stable`
- **Characteristics**: Persistent, supports complex queries

#### Why HAL for L1/L2? (Benefits)

1. **Testing**: CI doesn't need real Dragonfly/PG containers — mock driver in memory
2. **Swappable Backend**:
   - Tech Founder: Use `mem-driver-sqlite` (single file)
   - Enterprise: Use `mem-driver-dragonfly-cluster`
   - Kernel code unchanged
3. **Decoupled Dependencies**: `nexus-core` has zero `redis-rs` or `sqlx` imports

#### Implementation Patterns: Decorator vs ABC

**Decorator Pattern (Layer 0 - Kernel Internal)**:
```rust
// ❌ DON'T extract as HAL driver (too slow)
// ✅ DO use decorator/macro for transparent optimization
#[cached]
fn check_permission(user: &str, path: &str) -> bool {
    // Cache hit: ~50ns
    // Cache miss: queries Raft state machine
}
```

**ABC Pattern (Layer 1/2 - External Drivers)**:
```python
# ✅ Hot-pluggable via ABC
class DragonflyMemoryDriver(MemoryDriverProtocol):
    async def write_page(self, key, data, ttl):
        await self.redis.setex(key, ttl, data)

class MockMemoryDriver(MemoryDriverProtocol):
    async def write_page(self, key, data, ttl):
        self.store[key] = (data, time.time() + ttl)  # In-memory dict
```

**Trade-off Discussion**:
- **Gemini's view**: Unify all cache under HAL for conceptual purity
- **Our view**: Kernel-internal cache (like Inode cache in Linux) cannot tolerate RPC latency
- **Resolution**: Layer 0 stays in kernel with decorator, Layer 1/2 use HAL

**Comparison with Current Design** (Section 4.2.7):
- Current design has `CacheProtocol(ABC)` with Dragonfly/L1Cache/PostgreSQL drivers
- This design **splits** into:
  - Layer 0 (internal): Not in ABC, uses decorators (like `@cache_mixin.cached`)
  - Layer 1/2 (external): Keep as ABC drivers
- **Refinement needed**: Update Section 4.2.7 to distinguish these two patterns

**TODO** (P2):
1. Audit current cache usage: Which are kernel-critical (keep) vs hot-pluggable (extract)?
2. Implement `/dev/mem/*` mount protocol
3. Create `mem-driver-dragonfly` and `mem-driver-pg` stub implementations
4. Remove `redis-rs`/`sqlx` from `nexus-core` dependencies (move to drivers)
5. Update Section 4.2.7 with this Layer 0/1/2 distinction

### 7k. Identity System: PCB-Based Binding

**Status**: Design direction from Gemini discussions. Implementation P2-P3.

**Source**: `document-ai/notes/Nexus Identity Auth Separation Verify-Sign Split and PCB Design (msg147-150).md`

#### Core Principle: Identity Lockdown at Spawn Time

**Problem**: Current systems allow identity to be claimed per-request (Agent can lie: "I'm Gemini Worker").

**Solution**: Bind identity at process spawn (like Linux PID) — immutable for process lifetime.

#### PCB (Process Control Block) Model

In traditional OS, PCB stores: PID, PPID, UID, GID, open file descriptors.

In Nexus OS, PCB adds:
```rust
struct NexusTaskStruct {
    pid: u32,                    // Host OS process ID
    identity: String,            // "agent-gemini-worker-01" (immutable)
    zone_id: ZoneID,             // Access boundary
    spawn_time: Timestamp,
    caps: Capabilities,          // Permission capabilities
}
```

**Key Property**: `identity` is set once at spawn, cannot be changed. Every subsequent request auto-tags with this identity.

#### Progressive Isolation: 3 Levels

Agent sandboxing has different isolation strengths depending on deployment mode:

| Level | Mode | Identity Binding | I/O Monopoly | Mechanism | Use Case |
|-------|------|------------------|--------------|-----------|----------|
| **Level 0** | Host Process | Weak | ❌ No | SO_PEERCRED | Dev/Hacker mode |
| **Level 1** | Docker | Strong | ✅ Yes | Mount point | Prod/Company |
| **Level 2** | Wasm | Perfect | ✅ Yes | Memory isolation | Future/Lightweight |

**Level 0: SO_PEERCRED (Unix Domain Socket)**

No container overhead. Nexus uses Unix socket `getsockopt(SO_PEERCRED)` to get PID:
```rust
Agent (PID 12345) → Unix Socket → Nexus Kernel
Kernel: getsockopt(SO_PEERCRED) → OS returns PID 12345
Nexus: Lookup in-memory table: PID 12345 = Identity "Gemini-Worker"
```

**Advantages**:
- No Admin/root privileges required
- ~50ns overhead
- Kernel trusts OS (cannot fake PID)

**Limitation**: Agent can still bypass Nexus and directly access host OS (no I/O monopoly).

**Level 1: Docker Mode (Production)**

Full isolation, Agent cannot escape container:
```bash
docker run -d \
  --network none \              # Block all network except Nexus
  -v /nexus/agent/01:/mnt/nexus \  # Only mount point to Nexus
  -e NEXUS_ID="agent-01" \
  my-agent-image
```

Container has zero access to host OS. All file/network I/O must go through `/mnt/nexus` (Nexus FS).

**Level 2: Wasm Mode (Future)**

Agent compiled to WebAssembly, runs in sandboxed runtime (Wasmtime). Memory isolation enforced by Wasm VM.

#### Identity Auto-Tagging

Once identity is bound, every request is auto-tagged:
```python
# Agent code (no identity parameter needed)
nexusfs.write("/data/report.txt", content)

# Kernel automatically injects identity
def write(path, data):
    identity = current_task().identity  # From PCB
    if not check_permission(identity, path, "write"):
        raise PermissionDenied
    ...
```

Agent **cannot** claim a different identity — it's locked in PCB.

**Trade-off Discussion**:
- **Gemini's view**: PCB + Progressive Isolation is essential for security
- **Our consideration**: Level 0 (SO_PEERCRED) is pragmatic for dev/hacker workflows
- **Consensus**: Support all 3 levels, default to Level 0 for ease of use, recommend Level 1+ for production

**Comparison with Current Design**:
- Current: Token-based auth (Agent passes JWT per request)
- Proposed: PCB-based (identity set once at spawn, no per-request auth)
- **Migration**: Can coexist — external clients use JWT, internal agents use PCB

**TODO** (P2):
1. Define `NexusTaskStruct` (in Rust or Python)
2. Implement SO_PEERCRED binding for Level 0
3. Implement Docker mount isolation for Level 1
4. Add `NEXUS_ISOLATION_LEVEL=0|1|2` config
5. Refactor permission checks to use `current_task().identity` instead of JWT parsing

### 7l. Auth Separation: Verify/Sign Split

**Status**: Design direction from Gemini discussions. Implementation P2.

**Source**: `document-ai/notes/Nexus Identity Auth Separation Verify-Sign Split and PCB Design (msg147-150).md`

#### Core Principle: Fast Path vs Slow Path

Authentication has two operations with vastly different performance:

1. **Verify** (Fast Path): Check if a token/signature is valid
   - Latency: ~50ns (ed25519 signature verify)
   - Frequency: Every single request
   - **Must be in Kernel** (hot path optimization)

2. **Sign** (Slow Path): Generate a new token
   - Latency: ~50-500ms (DB lookup, OAuth flow, token generation)
   - Frequency: Once per session (login, refresh)
   - **Should be in Driver** (not kernel-critical)

**Current Problem** (Section 4.2.9): `AuthenticationProtocol` mixes both operations.

**Proposed Split**:

```
┌─────────────────────────────────────────────────────────┐
│ KERNEL SPACE (nexus-core)                               │
│                                                         │
│  class AuthVerifyProtocol(ABC):                        │
│    @abstractmethod                                      │
│    def verify_token(token: str) -> Optional[Identity]  │  ← Fast, in kernel
│    def verify_signature(data: bytes, sig: bytes) → bool│
│                                                         │
│  Implementation: ed25519_verify() — ~50ns               │
└─────────────────────────────────────────────────────────┘
                         ↓
┌─────────────────────────────────────────────────────────┐
│ DRIVER LAYER (sys-driver-auth)                         │
│                                                         │
│  class AuthSignProtocol(ABC):                          │
│    @abstractmethod                                      │
│    async def login(credentials) → Token                │  ← Slow, in driver
│    async def refresh_token(old_token) → Token          │
│    async def sign_data(data: bytes) → Signature        │
│                                                         │
│  Implementation:                                        │
│  - DB lookup (50ms)                                    │
│  - OAuth flow (200-500ms)                              │
│  - JWT generation (5ms)                                │
└─────────────────────────────────────────────────────────┘
```

**Data Flow Examples**:

**Fast Path (Every Request)**:
```python
# In Kernel (hot path, zero I/O)
@require_permission("file:read")
async def read_file(request):
    identity = kernel.auth_verify.verify_token(request.headers["Authorization"])
    if identity is None:
        raise Unauthorized
    # Proceed with permission check (50ns verify + 50ns permission lookup)
```

**Slow Path (Login)**:
```python
# In Driver (cold path, DB + OAuth)
@app.post("/api/v1/auth/login")
async def login_api(username, password):
    user = await db.get_user(username)  # 50ms DB query
    if not verify_password(password, user.password_hash):  # 100ms bcrypt
        raise InvalidCredentials
    token = auth_driver.sign_token(user.id, ttl=86400)  # 5ms JWT generation
    return {"token": token}
```

**Benefits**:
1. **Kernel stays fast**: No DB/OAuth dependencies in hot path
2. **Driver flexibility**: Swap OAuth providers (Google → GitHub) without kernel recompile
3. **Testing**: Kernel tests don't need OAuth mock servers

**Privacy Computing Extension** (from Gemini):
- Sensitive operations (Sign) can run in TEE (Trusted Execution Environment)
- Mount point: `/dev/tee/enclave_01/sign`
- Kernel forwards Sign requests to TEE driver, receives signed result
- Verify stays in kernel (public key verification doesn't need secrecy)

**Trade-off Discussion**:
- **Gemini's view**: Strict separation, kernel never does slow I/O
- **Our consideration**: Some hybrid cases (e.g., cached OAuth tokens) might blur the line
- **Decision**: Keep strict separation, use cache (Layer 0) for hybrid cases

**TODO** (P2):
1. Split `AuthenticationProtocol` into `AuthVerifyProtocol` (kernel) and `AuthSignProtocol` (driver)
2. Move JWT signing logic to `sys-driver-auth`
3. Keep only `ed25519_verify` and public key cache in kernel
4. Update permission checks to use kernel-side `verify_token()` only
5. (Optional) Implement TEE driver for Sign operations

### 7m. Nexus Native IPC: Pipe Implementation

**Status**: Design direction from Gemini discussions. Implementation P2-P3.

**Source**: `document-ai/notes/Nexus Container Deployment Strategy and Agent Permission Model (msg151-160).md`

#### Motivation: Why Not Linux Pipes?

Linux native pipes (`mkfifo`, `pipe()`) have limitations for Nexus use cases:

| Property | Linux Pipe | Nexus Native Pipe |
|----------|-----------|-------------------|
| **Observable** | ❌ No (opaque kernel buffer) | ✅ Yes (file-based) |
| **Persistent** | ❌ No (lost on process exit) | ✅ Yes (survives restart) |
| **Network Transparent** | ❌ No (local only) | ✅ Yes (Nexus FS replication) |
| **Seekable** | ❌ No (FIFO only) | ✅ Yes (ring buffer file) |
| **Access Control** | ❌ Limited (file permissions) | ✅ Yes (Nexus ReBAC) |

**Use Case**: Boardroom agents need observable, persistent event streams that survive container restarts and work across network boundaries.

#### Design: Ring Buffer File

**Location**: `/nexus/pipes/{pipe_name}` (special inode type: `DT_PIPE`)

**Structure**:
```rust
struct NexusPipe {
    buffer: RingBuffer<u8>,    // Fixed-size circular buffer (e.g., 1MB)
    read_ptr: AtomicU64,       // Current read position
    write_ptr: AtomicU64,      // Current write position
    metadata: FileMetadata,    // Standard Nexus metadata (ACL, timestamps)
}
```

**Operations**:
```python
# Writer (Producer)
pipe = nexusfs.open("/nexus/pipes/boardroom-events", mode="w")
pipe.write(b"event: new_proposal\ndata: {...}\n\n")  # Non-blocking (if space)

# Reader (Consumer)
pipe = nexusfs.open("/nexus/pipes/boardroom-events", mode="r")
for event in pipe:  # Blocking iterator, yields when data available
    handle_event(event)
```

**Blocking Semantics** (similar to Interrupt Model in 7i):
1. Reader issues `read()` on empty pipe → Kernel blocks reader (adds to wait queue)
2. Writer issues `write()` → Kernel writes to ring buffer
3. Kernel unblocks reader (interrupt-like wakeup)
4. Reader receives data

#### Advantages over External Message Queue (RabbitMQ/Kafka)

1. **Observable**: Admin can `cat /nexus/pipes/events` to inspect current data
2. **Persistent**: Pipe survives process restart (data persisted in Nexus FS)
3. **Network Transparent**: If pipe is in federated zone, replication is automatic (Raft sync)
4. **Unified ACL**: Use Nexus ReBAC (no separate queue auth system)
5. **Everything is a File**: No external dependencies, fits Nexus philosophy

#### Comparison with Dragonfly Pub/Sub (Section 3.4, Task #7)

**Alternative Approaches**:

| Approach | Pros | Cons | Use Case |
|----------|------|------|----------|
| **Raft Event Log** | Strong consistency, integrated | Higher latency (~5-10ms) | Critical events (consensus required) |
| **Dragonfly Pub/Sub** | Low latency (~1ms), external scale | External dependency, EC only | High-throughput notifications |
| **Nexus Native Pipe** | Observable, persistent, file-based | Ring buffer size limit | Medium-throughput, needs observability |

**Decision Pending**: Can coexist — use Raft Event Log for consistency-critical, Nexus Pipe for observable streams, Dragonfly for high-throughput ephemeral.

**Trade-off Discussion**:
- **Gemini's view**: Nexus Native Pipe is superior for observability and persistence
- **Our consideration**: Ring buffer size limits may not suit high-volume streams (billions of events/day)
- **Hybrid approach**: Use Pipe as primary interface, backed by Dragonfly or Raft depending on consistency needs

**TODO** (P2):
1. Define `DT_PIPE` inode type in FileMetadata
2. Implement ring buffer backend (in Rust for performance)
3. Implement blocking read/write operations (wait queue in kernel)
4. Add `/nexus/pipes/` namespace with special ACL handling
5. Document pipe API and migration guide from Dragonfly pub/sub

### 7n. Container Strategy: I/O Monopoly Requirement

**Status**: Design direction from Gemini discussions. Implementation P2.

**Source**: `document-ai/notes/Nexus Container Deployment Strategy and Agent Permission Model (msg151-160).md`

#### Core Requirement: Agent Must Go Through Nexus

**Problem**: If Agent can bypass Nexus and directly access Host OS (file system, network), then:
- Identity binding (PCB) is meaningless (Agent can forge files)
- Permission checks are bypassed
- Audit trail is incomplete

**Solution**: Enforce **I/O Monopoly** — Agent's only I/O channel is Nexus.

#### Implementation: "接管 Docker 的 I/O，不替代 Docker"

**Philosophy**: Use Docker for isolation, but control its I/O configuration to enforce monopoly.

**Docker Command Example**:
```bash
docker run -d \
  --network none \                         # ← Block all network access
  -v /nexus/agent/01:/mnt/nexus:ro \      # ← Only one mount (read-only Nexus socket)
  --read-only \                            # ← Root filesystem read-only
  --tmpfs /tmp:size=100M,noexec \         # ← Temporary writes in tmpfs (no persistence)
  -e NEXUS_ID="agent-01" \
  --security-opt=no-new-privileges \
  my-agent-image
```

**Key Restrictions**:
1. `--network none`: Agent cannot access internet directly (must use `/sys/net/http` via Nexus)
2. Single mount point: `/mnt/nexus` → Unix socket to Nexus kernel
3. `--read-only`: Agent cannot modify container filesystem (no side channels)
4. `/tmp` in tmpfs: Temporary files evaporate on restart (no hidden persistence)

**Agent Code (Inside Container)**:
```python
import nexus_sdk  # SDK that talks to /mnt/nexus socket

# All I/O goes through Nexus
nexus = nexus_sdk.connect("/mnt/nexus")
nexus.write("/data/report.txt", content)          # File I/O
response = nexus.http_get("https://api.com/data") # Network I/O (via /sys/net/http)
```

#### Compatibility with Level 0 (Host Process Mode)

**Level 0 (Dev/Hacker)**: No Docker, Agent runs as host process
- Uses SO_PEERCRED (see 7k)
- **No I/O monopoly** (Agent can access host FS directly)
- **Trade-off**: Convenience vs security

**Level 1 (Docker)**: Agent in container
- Enforced I/O monopoly
- **Production recommended**

**Configuration**: `NEXUS_ISOLATION_LEVEL=0|1|2` (see 7k Progressive Isolation table)

#### Alternative: Wasm-Based Isolation (Level 2, Future)

Agent compiled to Wasm, runs in Wasmtime:
- Memory isolation by default (Wasm VM sandbox)
- All I/O via WASI (WebAssembly System Interface) → can be intercepted by Nexus
- Lightweight (<10MB memory overhead vs Docker's ~100MB)

**TODO** (P2):
1. Create Docker image template with I/O monopoly restrictions
2. Implement Nexus SDK for container-side communication (Unix socket client)
3. Add validation: Kernel rejects connections from non-isolated agents (unless Level 0 mode)
4. Document deployment guide with Docker Compose examples
5. (P3) Prototype Wasm agent runtime

### 7o. True Runtime Hot-Swapping (Future Design)

**Status**: ⚠️ **Conceptual design, not implemented**. Current system (Section 4.3) only supports startup-time driver selection.

**Priority**: P2-P3 (after Microkernel foundation in place)

**Source**: Discussion on driver hot-swapping requirements (2026-02-08)

#### Goal: Linux Kernel Module Semantics

Users should be able to load/unload drivers at runtime without restarting Nexus, analogous to Linux kernel modules.

**Linux kernel module management**:
```bash
sudo modprobe e1000e              # Load network driver
sudo rmmod e1000e                 # Unload driver
lsmod | grep e1000e               # List loaded modules
sudo modprobe e1000e debug=1      # Load with parameters
# System continues running when driver loaded/unloaded
```

**Nexus equivalent** (aspirational):
```bash
nexus load-driver raft_metadata_store --zone=A
nexus unload-driver raft_metadata_store --zone=A
nexus list-drivers
nexus load-driver raft_metadata_store --zone=B --mode=eventual
```

#### Core Requirements

1. **Runtime Loading/Unloading**: Load/unload drivers while Nexus is running (no restart required)
2. **Multiple Drivers Per Type**: Multiple MetadataStore instances active (zone-specific assignment)
3. **Graceful Degradation**: System continues if one driver fails or is removed
4. **Consistency Mode Switching**: Runtime switch between SC and EC modes

#### Design Principle: Follow Linux Kernel Analogy

**Critical**: Use kernel module terminology, NOT filesystem mount terminology.

| ✅ Correct (Kernel Module) | ❌ Incorrect (Confusing with DT_MOUNT) |
|---------------------------|----------------------------------------|
| Load/unload driver        | Mount/unmount MetadataStore           |
| Driver registry           | Mount table                            |
| `modprobe` / `rmmod`      | `mount` / `umount`                     |

**Why**: DT_MOUNT (Section 7a) is for **cross-zone filesystem mounting** (user-facing). Driver management is **kernel module loading** (infra-facing). These are completely different concepts and must not share terminology.

**Linux kernel analogy** (familiar to hackers):

| Linux Kernel | Nexus Kernel | Purpose |
|--------------|--------------|---------|
| `modprobe e1000e` | `load_driver("raft_metadata_store")` | Load driver module |
| `rmmod e1000e` | `unload_driver("raft_metadata_store")` | Unload driver |
| `lsmod` | `list_drivers()` | List active drivers |
| `/proc/modules` | `/sys/kernel/drivers` | Inspect loaded drivers |
| Module parameters | Driver config | Initialization params |
| Hot-pluggable NIC | Zone-specific store | Multiple instances |

#### High-Level Architecture (Sketch)

**Current** (Section 4.3): Single driver per type, selected at startup
```python
self.metadata = SQLAlchemyMetadataStore(...)  # Hardcoded, startup-only
```

**Future**: Zone-aware driver resolution (conceptual)
```python
class NexusFS:
    def __init__(self):
        self.driver_registry = DriverRegistry()  # Manages active drivers

    async def get_metadata(self, path: str):
        zone_id = self._extract_zone(path)  # "/zone:A/..." → "A"
        driver = self.driver_registry.get_driver_for_zone(
            driver_type=MetadataStore,
            zone_id=zone_id
        )
        return await driver.get_metadata(path)
```

#### Open Questions (Need Consensus Before Implementation)

1. **Driver identification**: How to uniquely identify driver instances? (by zone? by name? by type+zone?)
2. **State migration**: When switching drivers, how to handle existing metadata?
   - Forbid switch if data exists? (safe, restrictive)
   - Automatic migration? (risky)
   - Manual export/import? (safest)
3. **Fallback strategy**: When primary driver fails, what happens?
   - Read-only mode with cache?
   - Fail fast with error?
   - Automatic failover?
4. **SC/EC mode switching**: Within-driver mode change vs full driver replacement?
   - Prefer: `driver.set_consistency_mode(EVENTUAL)` (avoids state migration)
   - Over: Unload SC driver, load EC driver (requires migration)
   - **Related**: Issue #1180 proposes `migrate_consistency_mode(path, target_mode)` API
     - Challenge: LOCAL mode uses SQLite/Dragonfly, STRONG_HA mode uses Raft/sled
     - Need metadata export/import between storage backends
     - Requires distributed lock during migration to prevent writes
5. **Zone-specific vs global**: Should there be a global default driver (catch-all for unconfigured zones)?
6. **Concurrency control**: How to prevent races during driver switch? (pause? per-zone locks? read-only transition?)

#### Implementation Phases

**Phase 1 (P1, Current)**: Foundation
- Task #14: NexusFS constructor injection (enable driver-agnostic design)
- P1#7: Re-integrate RaftMetadataStore with config-driven selection
- **Limitation**: Startup-time only, single driver, no runtime switching

**Phase 2 (P2)**: Basic Runtime Hot-Swapping
- Implement `DriverRegistry` with load/unload/list operations
- Zone-aware driver resolution
- Graceful driver unload (drain + flush)
- CLI: `nexus load-driver`, `nexus unload-driver`, `nexus list-drivers`
- **Limitation**: No state migration, no automatic fallback

**Phase 3 (P3)**: Advanced Features
- State migration between drivers
- Fallback driver support (graceful degradation)
- SC/EC mode runtime switching (within same driver)
- `/sys/kernel/drivers` monitoring interface

#### Relationship to Other Designs

- **DT_MOUNT (Section 7a)**: DIFFERENT concept, no code reuse
  - DT_MOUNT: Cross-zone filesystem mounting (user-facing)
  - Driver hot-swap: Kernel module management (infra-facing)

- **Microkernel (Section 7i)**: RELATED
  - Microkernel extracts features to drivers
  - Hot-swap enables runtime driver management

- **Progressive Isolation (Section 7k)**: INDEPENDENT
  - Isolation: Agent sandboxing
  - Hot-swap: Operational flexibility

**Related Issues**:
- Issue #1180: Runtime consistency mode migration (LOCAL ↔ STRONG_HA)
  - Proposes `migrate_consistency_mode(path, target_mode)` API
  - Hot-swapping can enable this feature (driver replacement + state migration)

**TODO** (P2-P3):
1. Team consensus on open questions (especially state migration and fallback strategies)
2. Design `DriverRegistry` interface in detail (Task #21)
3. Prototype zone-aware driver resolution
4. Implement graceful unload with drain + flush
5. Add CLI commands and monitoring interface
6. Coordinate with Issue #1180 on state migration strategy

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
16. **Microkernel Refactoring** (Section 7i):
    - Audit `nexus-core` imports for extraction candidates (Timer, HTTP, Storage I/O)
    - Create `nexus-drivers/` directory structure
    - Extract Timer (sys-driver-timer) and HTTP (sys-driver-net) as proof-of-concept
    - Define `/sys/*` and `/dev/*` mount protocols
17. **Memory HAL Implementation** (Section 7j):
    - Implement `/dev/mem/hot` and `/dev/mem/vector` mount points
    - Create `mem-driver-dragonfly` and `mem-driver-pg` drivers
    - Remove `redis-rs`/`sqlx` from `nexus-core` dependencies
    - Refine Section 4.2.7 with Layer 0/1/2 distinction
18. **Identity System: PCB Binding** (Section 7k):
    - Define `NexusTaskStruct` structure
    - Implement SO_PEERCRED binding for Level 0 (Host Process Mode)
    - Implement Docker mount isolation for Level 1
    - Add `NEXUS_ISOLATION_LEVEL=0|1|2` configuration
19. **Auth Verify/Sign Split** (Section 7l):
    - Split `AuthenticationProtocol` into `AuthVerifyProtocol` (kernel) + `AuthSignProtocol` (driver)
    - Move JWT signing to `sys-driver-auth`
    - Keep only `ed25519_verify` in kernel
20. **Nexus Native Pipe** (Section 7m):
    - Define `DT_PIPE` inode type
    - Implement ring buffer backend (Rust)
    - Implement blocking read/write with wait queue
    - Add `/nexus/pipes/` namespace
21. **Container I/O Monopoly** (Section 7n):
    - Create Docker image template with I/O restrictions
    - Implement Nexus SDK for container communication (Unix socket)
    - Add isolation level validation in kernel
    - Document deployment guide
22. **Runtime Hot-Swapping Foundation** (Section 7o, Phase 2):
    - Consensus on open questions (driver ID, state migration, fallback)
    - Design `DriverRegistry` interface with load/unload/list operations
    - Implement zone-aware driver resolution
    - Implement graceful driver unload (drain + flush)
    - CLI: `nexus load-driver`, `nexus unload-driver`, `nexus list-drivers`

### P3: Future
23. Cross-zone distributed transactions (Spanner-like 2PC)
24. ✅ ~~Zone nesting/overlaps design~~ Design decided (Section 7a: Mount Points + Hard Link lifecycle)
25. Write performance optimization (NexusFS.write() ~30ms/op)
26. Raft Eventual Consistency (EC) mode (Issue #1180)
27. Wasm-based agent runtime (Level 2 isolation, Section 7k/7n)
28. TEE driver for privacy computing (Sign operations in enclave, Section 7l)
29. **Runtime Hot-Swapping Advanced** (Section 7o, Phase 3):
    - Automatic state migration between drivers
    - Fallback driver support (graceful degradation)
    - SC/EC mode runtime switching within driver
    - `/sys/kernel/drivers` monitoring interface

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