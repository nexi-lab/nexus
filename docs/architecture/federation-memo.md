# Federation Architecture Memo

**Date:** 2026-03-01 (Last updated)
**Status:** Design SSOT (Single Source of Truth)

> **Contributing**: Living design document. Prefer **in-place edits** over appending.
> Keep it concise — rationale > code. No task tracking here.

---

## 1. Architecture Components

### Raft Consensus Core (Rust)
- `ZoneConsensus` wrapping tikv/raft-rs `RawNode` with async propose API
- `RaftStorage` backed by redb (persistent log, hard state, snapshots, compaction)
- `FullStateMachine` (metadata + locks) and `WitnessStateMachine` (vote-only)

### PyO3 FFI Bindings
`Metastore` class for same-box Python→Rust redb access (~5us/op):
metadata ops, lock ops (mutex + semaphore), snapshot/restore.

### RaftMetadataStore (Python)
Local mode (PyO3) and remote mode (gRPC). Same interface as SQLAlchemyMetadataStore.

### Distributed Locks
`RaftLockManager` — locks in Metastore (redb), replicated via Raft (SC). Cross-zone locks route via gRPC. RedisLockManager deprecated for Raft-enabled deployments.

### gRPC Transport
Inter-node Raft replication via `ZoneTransportService` + `ZoneApiService`.

---

## 2. Target Architecture (Production Federation)

```
              Zone: us-west-1
  ┌─────────────────────────────────────────────┐
  │ Node A (Leader)    Node B (Follower)  Node C│
  │ ┌──────────┐       ┌──────────┐     (Witness)
  │ │ NexusFS  │ gRPC  │ NexusFS  │  ┌────────┐│
  │ │ + Raft   │◄─────►│ + Raft   │──│Vote-only│
  │ │ + redb   │       │ + redb   │  │ redb(log)│
  │ └──────────┘       └──────────┘  └────────┘│
  └─────────────────────────────────────────────┘
                        │ (nexus-to-nexus mount)
              Zone: eu-central-1 (same structure)
```

**Node composition** (single process): NexusFS + FastAPI + gRPC + ZoneConsensus + redb + SQLAlchemy. Leader/Follower run same binary; role by Raft election. Witness: vote-only, no state machine, minimal footprint (`RaftConfig::witness(id, peers)`).

---

## 3. Data Architecture

### 3.1 Design Principles
Every data type justified across 8 property dimensions (R/W perf, consistency, query pattern, size, cardinality, durability, scope). Storage mediums must not overlap in purpose.

### 3.2 Storage Layer Decisions

#### **SQLAlchemy (PostgreSQL/SQLite) = RecordStore** — 22 types
Relational queries, FK, unique constraints, vector search, encryption, BRIN indexes.

| Category | Data Types |
|----------|-----------|
| Users & Auth | UserModel, UserOAuthAccountModel, OAuthCredentialModel |
| ReBAC | ReBACTupleModel, ReBACGroupClosureModel, ReBACChangelogModel |
| Memory | MemoryModel, MemoryConfig, TrajectoryModel, PlaybookModel |
| Versioning | VersionHistoryModel, WorkspaceSnapshotModel |
| Semantic Search | DocumentChunkModel (pgvector/sqlite-vec) |
| Workflows | WorkflowModel, WorkflowExecutionModel |
| Zones | ZoneModel, EntityRegistryModel, ExternalUserServiceModel |
| Audit | OperationLogModel |
| Other | SandboxMetadataModel, PathRegistrationModel |

#### **Metastore (Ordered KV — redb)** — 5 types
| Data Type | Rationale |
|-----------|-----------|
| FileMetadata (merged FilePathModel, DirectoryEntryModel) | Core metadata, KV by path, SC via Raft |
| FileMetadataModel (custom KV) | Arbitrary user-defined KV metadata |
| ReBACNamespaceModel | KV by namespace_id, low cardinality |
| SystemSettingsModel | KV by key, low cardinality |
| ContentChunkModel | CAS dedup index, KV by content_hash, immutable (local only) |

#### **CacheStore (Ephemeral KV + Pub/Sub)** — 4 types
PermissionCacheProtocol, TigerCacheProtocol (TTL), UserSessionModel (TTL), FileEvent (pub/sub).

**Full analysis**: `docs/architecture/data-storage-matrix.md`

---

## 4. Kernel Architecture

See **`docs/architecture/KERNEL-ARCHITECTURE.md`** (SSOT) for the OS-inspired layered architecture.

### 4.1 Raft Dual Mode: Strong vs Eventual Consistency

| Mode | Writes | Reads | Latency | Use case |
|------|--------|-------|---------|----------|
| **SC** (default) | Raft consensus (majority ACK) | Linearizable | ~5-10ms intra-DC | Financial, compliance |
| **EC** (opt-in) | Local + async replicate | Eventual | ~5us (local redb) | Media, high-throughput |

Per-operation parameter (`consistency="sc"` or `"ec"`), not per-zone. SC uses Raft consensus core; EC uses async ReplicationLog + LWW conflict resolution.

---

## 5. Write Flow

```
5.1 Single-Node:        Client → NexusFS.write() → SQLAlchemyMetadataStore → Backend.write()
5.2 Raft Local:         Client → NexusFS.write() → RaftMetadataStore (PyO3 ~5us) → redb → Backend
5.3 Raft Distributed:   Client → NexusFS.write() → ZoneConsensus.propose() → gRPC replicate
                                                  → Majority ACK → StateMachine.apply() on all → redb
                                                  → per-voter dcache.evict(key)  ← cache coherence
```

raft-rs only handles consensus (log replication, election). Transport (gRPC) is our responsibility.

Cache coherence: every voter's `StateMachine.apply` fires the invalidation callback the kernel DLC installed at mount time (see KERNEL-ARCHITECTURE §4 DLC row), so a leader-forwarded follower write — or any replicated mutation — evicts stale dcache entries on nodes that didn't originate the write. Without this step, `sys_stat` / `sys_read` on non-writer voters would keep returning the pre-write `etag` from local dcache even after raft applied the new state.

---

## 6. Zone Model

### 6.1 Core Decision: Zone = Consensus Domain

A Zone is both a **logical namespace** and a **consensus boundary**:
- Each Zone has its own **independent Raft group** with its own redb database
- Zones do NOT share metadata — separate, non-replicated redb stores
- Cross-zone access requires gRPC (DT_MOUNT resolution)

**Why not replicate all metadata to all nodes?**
Security (GDPR data sovereignty), space (millions of users), latency (cross-continent Raft).

**Spanner comparison**: Spanner Universe → Federation, Spanner Zone → Zone, Paxos Group → Raft group. Key difference: Spanner's Paxos Group and Zone are orthogonal; in NexusFS, Zone and Raft group are 1:1 (Multi-Raft sharding within a zone possible later).

### 6.2 Mount = Create New Zone, All Voters

**NFS-style UX:**
```bash
nexus mount /my-project bob:/my-project
```

Creates a **new independent zone**. All participants are **equal Voters** (not Learners). Permissions (read-only vs read-write) via ReBAC, not Raft roles.

| Aspect | Behavior |
|--------|----------|
| Read latency | ~5us (local redb) — always local |
| Write latency | Raft propose → commit |
| Consistency | Linearizable (no cache invalidation needed) |
| Data locality | Full metadata replica in local redb |

**Why not redirect + cache?** Redirect = gRPC every read (~200ms). Client cache = re-inventing weaker Raft. Raft already solves consistent multi-party views.

**Implicit share**: `nexus share /path --with user` auto-creates zone + DT_MOUNT + invite + Raft join. **Explicit**: `nexus zone create`, `nexus mount` for admin operations. Decision logic: contributes new metadata → create zone; only consumes → join existing zone.

### 6.3 Peer Discovery: No Custom DNS

Standard OS DNS + bootstrap + Raft membership exchange covers all scenarios.

| Layer | Mechanism | When |
|-------|-----------|------|
| Bootstrap | `NEXUS_BOOTSTRAP_NEW=1` (founder) or JoinZone RPC against `NEXUS_PEERS` (joiner) | First cluster formation |
| First contact | OS DNS (hostname → IP) | `join_zone(peers=["2@bob:2126"])` |
| After join | Leader snapshot installs authoritative `ConfState` | After AddNode commits |
| Ongoing | Raft `ConfChange` | Automatic membership propagation |

Path resolution across zones is **all local** (~5us per hop) because mounting = Voter = full local replica. No network hops on the read path.

#### 6.3.1 Bootstrap

Etcd / TiKV-style opaque IDs + leader-driven `AddNode`.

- **Identity** — `node_id` is an opaque random `u64` minted at first daemon boot, persisted as 8 bytes BE u64 to `<NEXUS_DATA_DIR>/.node_id`.  Decoupling identity from hostname lets a wiped follower rejoin under a fresh ID; the leader's `Progress[new_id]` is created with `matched=0` by `AddNode`, so the first heartbeat carries `m.commit=0` — within `RaftLog::commit_to`'s safe range on a fresh follower (`last_index=0`).  Pinned by [`test_handle_heartbeat_on_empty_follower_with_stale_commit_panics`](../../rust/raft/src/raft/storage.rs).
- **Address book** — `NEXUS_PEERS` is a hostname → endpoint mapping for OTHER nodes only that seeds the transport peer map.  Self joins the cluster through `create_zone(self)` (founder) or `AddNode(self)` on the leader (joiner) — never through the address book.  Boot fails loud (`peer list contains self ...`) when `NEXUS_PEERS` includes the local node so the joiner-loop self-RPC stall surfaces at parse time, not after `Zone 'root' registered`.  `ConfState` lives in raft storage and is mutated only by `ConfChange` (AddNode / RemoveNode) driven by JoinZone.
- **Bootstrap mode** — operator declares intent up front via `NEXUS_BOOTSTRAP_MODE` (or `--bootstrap-mode` for `nexusd-cluster`).  The validator runs once at boot and rejects any state × flag combination that does not match the declared mode, so misconfiguration surfaces before the gRPC server starts rather than as a silent stall later.  See [`BootstrapMode`](../../rust/raft/src/distributed_coordinator.rs).

  | Mode | Required state | Required flags | Forbidden flags | Bootstrap dispatch |
  |------|---------------|----------------|-----------------|---------------------|
  | `static` | Empty data dir | `NEXUS_BOOTSTRAP_NEW=1` (founder) **or** `NEXUS_PEERS` non-empty (joiner) | — | Founder: `create_zone("root")` 1-voter.  Joiner: loop on JoinZone RPC against `NEXUS_PEERS`, indefinite |
  | `dynamic` | Empty data dir | — | `NEXUS_BOOTSTRAP_NEW`, `NEXUS_PEERS` | Daemon comes up rootless; runtime API (`nexusd-cluster share`/`join`, Python `federation_create_zone`) drives zone formation |
  | `restart` | Data dir holds `<dir>/root/raft/` | — | `NEXUS_BOOTSTRAP_NEW`, `NEXUS_PEERS` | Resume from persisted ConfState — state on disk is the SSOT, env flags would be ambiguous |

- **Wipe-rejoin** — wiping `<NEXUS_DATA_DIR>` mints a fresh `node_id` on the next boot; the daemon JoinZones, the leader commits `AddNode(new_id)`.

##### Witness

The standalone witness binary derives `node_id = hostname_to_node_id(hostname)` (SHA-256 of hostname).  Witnesses live at well-known addresses, so binding identity to hostname is sufficient for them.

#### 6.3.2 Federation Control-Plane API Surface

The federation control plane has two layers; they are NOT shortcuts for each other and live at different trust boundaries.

| Operation | Syscall path (preferred) | RPC path (legacy / pending migration) | Notes |
|-----------|--------------------------|----------------------------------------|-------|
| **Create zone (mount-tied)** | `sys_setattr(path, DT_MOUNT, zone_id, source=None)` | `federation_create_zone(zone_id)` + `federation_mount(parent, path, zone)` | Syscall is the architectural target — service tier should always go through syscall. The two-step RPC pattern remains for legacy callers. |
| **Join zone (mount-tied)** | `sys_setattr(path, DT_MOUNT, zone_id, source="http://leader:2126")` | `federation_join(peer_addr, remote_path, local_path)` (share-registry-based) | Syscall covers raw cluster join via leader address; the RPC covers subtree share/join via the raft-replicated share registry — two distinct workflows. |
| **Unmount zone** | `sys_unlink(mount_path)` | `federation_unmount(parent_zone, path)` | Equivalent surfaces. |
| **Remove zone (standalone)** | (no syscall — zone removal without a path has no filesystem analog) | `federation_remove_zone(zone_id, force=false)` | Cluster-control plane only. Cascade-unmount happens inside the impl. |
| **Read replicated state** | `sys_read(path)` / `sys_stat(path)` / `sys_readdir(path)` | — | Filesystem syscalls reach federated zones via the mount table; no special federation API needed. |

The `_nr.federation_create_zone` / `federation_remove_zone` / `federation_join_zone` PyO3 bindings are direct service-tier shortcuts to the `DistributedCoordinator` HAL trait. They predate the syscall-only contract and are scheduled for removal once all callers migrate. **Do not add new callers** — go through `sys_setattr` / `sys_unlink` instead.

Architectural principle: service tier (`@rpc_expose` methods in `nexus.server.rpc.services.*`) interacts with the kernel **only** through syscalls — same trust boundary as any external user. Direct PyO3 trait shortcuts collapse the boundary and make permission / audit / hook injection harder to reason about.

### 6.4 DT_MOUNT Entry Structure

```python
class DT_MOUNT:
    name: str               # Mount point name in parent directory
    entry_type: "DT_MOUNT"  # Alongside DT_DIR, DT_REG
    target_zone_id: str     # Target zone UUID (no address: Voter has local replica)
```

Mount shadows existing DT_DIR (NFS-compliant). DT_REG conflict rejected.
Zone lifecycle uses hard-link model with `i_links_count` (shared_ptr semantics).
Orphaned zones → `/nexus/trash/`, explicit `nexus zone destroy` to delete.

### 6.5 Inter-Zone Architecture

Zones are physically flat and isolated. The global namespace tree is an illusion of DT_MOUNT entries:

```
Physical (what Raft sees):              Logical (what users see):
  Zone_A: /, docs/, hr/                  /company/
  Zone_B: /, code/, design/                ├── engineering/ → [Zone_B]
  Zone_C: /, photos/                       └── ceo_wife/    → [Zone_C]
```

Mixed consistency: Zone A (EC), Zone B (SC) — each Raft group independent.

**Permissions**: Parent zone controls mount point visibility; target zone controls entry (ReBAC at boundary). **User-centric root**: Each user's `/` determined by zone registry scan — no complex ACL to hide upper directories.

### 6.6 Federation as Optional DI Subsystem

Federation is **NOT kernel**. NexusFS without federation degrades to remote mode (`nexus.connect()`) or standalone.

```
NexusFS (kernel)           Federation (optional subsystem)
NexusFilesystem (ABC)      — (inherently asymmetric)
NexusFS                    NexusFederation (orchestration)
MetastoreABC               ZoneManager (wraps PyO3)
RaftMetadataStore          PyZoneManager (Rust/redb/Raft)
```

**API Privilege Levels**: File I/O (agents/users) → Federation ops (`share/join`) → Zone lifecycle (admin). Agents do NOT get mount/unmount APIs.

---

## 7. Extended Design Topics

### 7a. Write Performance (~30ms/op)

redb is ~0.014ms/op; 99.95% overhead is Python/NexusFS (CAS hash, cache invalidation, SQLAlchemy, permission checks, directory indexing). Future: batch API, async checks, redb-native metadata.

### 7b. Multi-Node Deployment

Full Node Docker image: single container runs NexusFS + FastAPI + gRPC + ZoneConsensus + redb + SQLAlchemy. Same image for dev (`docker-compose.cross-platform-test.yml`) and production.

### 7c. Dragonfly Role Post-Raft

Redis deprecated → Dragonfly only. Distributed locks → Raft. Permission/Tiger caches, FileEvent pub/sub, UserSession → CacheStore (Dragonfly prod / In-Memory dev). Dragonfly is optional (NullCacheStore fallback).

### 7d. Cross-Zone 2PC (Plan B)

If atomic cross-zone writes needed: coordinator runs 2PC across zone leaders (prepare → commit). Plan A (nexus-to-nexus mount) covers most cases.

### 7e. Future Design Topics

Documented in `document-ai/notes/` discussions; brief summary for reference:

- **Microkernel extraction**: Kernel = local RPC router (VFS + IPC + Raft + Permission Gate). Storage/Timer/HTTP/Auth = user-mode drivers.
- **Memory/Cache tiering**: L0 kernel (redb ~50ns), L1 Dragonfly (~1ms), L2 PostgreSQL (~5ms). L0 stays in kernel; L1/L2 hot-pluggable.
- **Identity: PCB-based binding**: Immutable identity at process spawn. Progressive isolation: Host Process → Docker → Wasm.
- **Auth: Verify/Sign split**: Kernel = `verify_token()` ~50ns. Driver = `login()` ~50-500ms (DB + OAuth).
- **Container I/O monopoly**: `--network none`, single mount `/mnt/nexus`, `--read-only`.
- **Runtime hot-swapping**: Linux `modprobe`/`rmmod` semantics for drivers. Phases: Constructor DI → DriverRegistry → state migration.

### 7f. Federation Content CRUD: Implementation & Caveats

#### Architecture Alignment: HDFS/GFS, Not UNIX ext4

Nexus's metadata/content separation (Metastore + ObjectStore) aligns with distributed filesystem
best practices, not traditional single-machine OS design:

| System | Metadata Plane | Content Plane | Separation |
|--------|---------------|---------------|------------|
| **HDFS** | NameNode (ClientProtocol) | DataNode (DataTransferProtocol) | Two independent RPC protocols |
| **GFS** | Master | ChunkServer | Two independent services |
| **Nexus** | Metastore (redb/Raft) | ObjectStore (CAS/S3/GCS) | Two independent pillar ABCs |
| Linux ext4 | inode | data blocks | Same driver (single machine) |

HDFS exposes metadata-only and content-only interfaces as **separate first-class protocols** at
the kernel primitive level — not just a convenience layer. Our Metastore + ObjectStore split
follows this same pattern. Consequences:
- `sys_write` orchestrates both planes (like HDFS DFSClient), but the planes are independent
- Cross-plane coordination (orphan cleanup) is async, not synchronous (see Caveat 4)
- Content never flows through the metadata plane (like HDFS: "user data never flows through NameNode")

Federation has two I/O planes with different routing strategies:

| Plane | Pattern | Mechanism |
|-------|---------|-----------|
| **Metadata** | Transparent DI proxy | `FederatedMetadataProxy` wraps MetastoreABC, zone-routes all ops |
| **Content** | PRE-DISPATCH resolver | `FederationContentResolver` intercepts read/delete before kernel |

**Zone-aware path routing:** PathRouter canonicalizes all paths to
`/{zone_id}/{path}` and does zone-canonical LPM. For local-zone paths,
FederationContentResolver fast-exits without metadata lookup (~0 cost).
Cross-zone paths still require metadata lookup to determine content locality
(CAS blobs are node-specific). See `KERNEL-ARCHITECTURE.md` §4.

#### Content CRUD Status

| Operation | Mechanism | Routing |
|-----------|-----------|---------|
| **Read** | `FederationContentResolver.try_read()` | Local zone: fast-exit (no metadata lookup). Remote: gRPC Read/StreamRead RPC |
| **Write** | Always local (by design) | `FederatedMetadataProxy` enriches `backend_name` with node address (`local@host:port`) |
| **Delete** | `FederationContentResolver.try_delete()` | Local zone: fast-exit. Remote: gRPC Delete RPC delegates `sys_unlink` to origin peer |
| **Rename** | Metadata-only (CAS content stays at same hash) | Cross-zone rename blocked by `FederatedMetadataProxy` |

Streaming reads: `FederationContentResolver.try_read()` uses a size threshold —
< 1MB: unary gRPC `Read` RPC; >= 1MB: `StreamRead` RPC (chunked, CAS-aware for
CDC files). No local persistence on read — content stays on the origin node only.

#### CAS Semantics in Federation

CAS stores each file as **one immutable blob keyed by SHA-256 hash**. "Modifying" a file (including `append()`) creates a **new blob with a new hash**. Properties: no partial reads, safe remote read (hash-verified), conflicts only at metadata level.

#### Caveat 1: Concurrent Multi-Node Write (Last-Writer-Wins)

Two nodes writing to the same path: Raft totally orders the two metadata proposals. Last committed write wins; losing node's CAS blob becomes orphaned (see Caveat 4).

**Mitigation**: `sys_write(if_match=etag)` provides OCC. Because metadata is Raft-replicated (all nodes see same etag), `if_match` correctly detects conflicts.

#### Caveat 2: Cross-Node Append = Full Read-Modify-Write

`append()` = `sys_read()` + concatenate + `sys_write()`. In federation, appending 1 byte to a 100MB file on another node transfers the entire file over the network, creates a new complete blob, and orphans the old blob.

Acceptable for v1: most federation is read-heavy; frequent cross-node appends are rare.

#### Caveat 3: Content Availability on Writer Node Failure

Content exists only on writer's CAS until another node reads it. Writer failure before any read → `NexusFileNotFoundError`. Future: eager replication, CacheStore L2, WAL read-repair.

#### Caveat 4: CAS Orphan Accumulation (Standard Pattern — Needs GC)

`sys_write` does NOT release old blobs on overwrite. This is **not a bug** — it follows the
HDFS/GFS standard pattern where metadata changes are synchronous and content cleanup is
asynchronous via background GC.

**HDFS/GFS precedent**:
- GFS (paper §4.4): delete renames file to hidden name; background scan removes metadata after 3 days;
  ChunkServer heartbeat reports chunks; Master identifies orphans and instructs deletion.
- HDFS: NameNode adds blocks to `invalidateBlocks` queue; DataNode heartbeat picks up delete commands;
  BlockManager periodically reconciles blocks against namespace references.

Both systems explicitly accept temporary orphans as a design choice. Synchronous cross-plane
cleanup (releasing content during metadata write) is NOT how distributed filesystems work.

**Nexus behavior**:
```
write("Hello")  → store(hash_A) on ObjectStore, metadata.put(etag=hash_A) on Metastore
write("World")  → store(hash_B) on ObjectStore, metadata.put(etag=hash_B) on Metastore
                   hash_A: no metadata reference, still in ObjectStore → orphan (temporary)
```

**Federation amplifies**: cross-node writes leave orphans on the original writer's ObjectStore.
The writing node's Raft follower receives metadata updates but does not trigger ObjectStore cleanup.

**Resolution: ContentGarbageCollector** (like HDFS BlockManager):
```
referenced_hashes = metastore.all_etags()          # metadata plane
existing_hashes   = objectstore.all_content_hashes() # content plane
orphans           = existing - referenced
for hash in orphans: objectstore.delete_content(hash) # async cleanup
```

Single-node GC is straightforward (scan local ObjectStore vs local Metastore).
Federation GC requires node-level reconciliation: each node scans its local ObjectStore
against the Raft-replicated Metastore to find locally-held orphans.

### 7j. DT_PIPE / DT_STREAM Federation Design

Both IPC primitives have Raft-replicated metadata but in-process heap data
(MemoryPipeBackend for DT_PIPE, StreamBuffer for DT_STREAM). Federation extends
IPC I/O transparently via origin-aware routing. DT_STREAM uses the same
`stream@host:port` pattern as DT_PIPE's `pipe@host:port`.

#### Metadata: `backend_name` Encoding

PipeManager embeds the creator node's advertise address in `backend_name`:

| Mode | `backend_name` | Meaning |
|------|---------------|---------|
| Single-node | `pipe` / `stream` | No origin, always local |
| Federated | `pipe@host:port` / `stream@host:port` | Origin node address for remote proxy |

#### Read/Write Routing

`BackendAddress.parse(backend_name)` extracts the origin. NexusFS dispatches:

- **Local** (`origin == self` or no origin): Direct MemoryPipeBackend via PipeManager (~0.5us)
- **Remote** (`origin != self`): gRPC `Call` RPC to origin node, which executes
  `sys_read`/`sys_write` locally and returns the result

The remote path reuses existing gRPC auth/zone/error infrastructure — no new proto RPCs.

#### sys_write: Always Local (Design Decision)

`sys_write` is always local by design. The writer node becomes the content origin:
- Regular files: `FederatedMetadataProxy` enriches `backend_name` with writer's address
- Pipes: PipeManager embeds `self_address` in `backend_name` at creation time

Remote nodes read from the origin. There is no write-forwarding or write-proxying.
This is consistent with HDFS/GFS where writes go to a local DataNode/ChunkServer.

---

## 8. Key Files Reference

| Component | File |
|-----------|------|
| Raft node | `rust/nexus_raft/src/raft/node.rs` |
| Raft storage | `rust/nexus_raft/src/raft/storage.rs` |
| State machine | `rust/nexus_raft/src/raft/state_machine.rs` |
| PyO3 bindings | `rust/nexus_raft/src/pyo3_bindings.rs` |
| Raft proto | `rust/nexus_raft/proto/raft.proto` |
| RaftMetadataStore | `src/nexus/storage/raft_metadata_store.py` |
| SQLAlchemyMetadataStore | `src/nexus/storage/sqlalchemy_metadata_store.py` |
| Docker Compose | `dockerfiles/docker-compose.cross-platform-test.yml` |
| FederatedMetadataProxy | `src/nexus/raft/federated_metadata_proxy.py` |
| FederationContentResolver | `src/nexus/raft/federation_content_resolver.py` |
| ZonePathResolver | `src/nexus/raft/zone_path_resolver.py` |
| BackendAddress | `src/nexus/contracts/backend_address.py` |
| ChannelFactory | `src/nexus/grpc/channel_factory.py` |
| PipeManager | `src/nexus/core/pipe_manager.py` |
| VFS gRPC proto | `proto/nexus/grpc/vfs/vfs.proto` |
| VFS gRPC servicer | `src/nexus/grpc/servicer.py` |
| Data architecture | `docs/architecture/data-storage-matrix.md` |
