# Federation Architecture Memo

**Date:** 2026-02-06
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
- **PyO3 FFI bindings**: `LocalRaft` class for same-box Python→Rust access (~5μs/op)
  - Metadata ops: set/get/delete/list
  - Lock ops: acquire/release/extend (mutex + semaphore)
  - Snapshot/restore
- **RaftMetadataStore** (Python): Full implementation with local (PyO3) and remote (gRPC) modes
  - Same interface as SQLAlchemyMetadataStore
  - Reverted from NexusFS integration for CI reasons (commit 46e7884b)
- **Distributed locks**: RedisLockManager (Dragonfly-backed) for cross-platform coordination
- **CI**: 4 workflows all green (lint, test, docker, code quality)

### What's Broken / Missing
- **Proto files**: `commands.proto` and `transport.proto` were never committed (being rebuilt from _pb2.py stubs)
- **gRPC transport**: Code exists in Python (`src/nexus/raft/`) but proto compilation is missing in CI
- **NexusFS integration**: RaftMetadataStore was integrated then reverted; currently using SQLAlchemy
- **CI PyO3**: No maturin build in CI, so RaftMetadataStore can't be tested there
- **Global tenant→zone rename**: 3866 remaining `zone_id` references across 161 files still using old `tenant_id` naming

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

## 3. Write Flow

### Single-Node (Current Production Path)
```
Client → NexusFS.write() → SQLAlchemyMetadataStore → SQLite/PostgreSQL
                         → Backend.write() → local/S3/GCS/...
```

### Single-Node with Raft (Local, Future)
```
Client → NexusFS.write() → RaftMetadataStore (local mode)
                              → PyO3 FFI (~5μs)
                              → FullStateMachine.apply()
                              → sled persist
                         → Backend.write() → local/S3/GCS/...
```

### Multi-Node with Raft (Distributed, Future)
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

## 4. Zone Model

- **Path format**: `/zone:{zone_id}/path/to/file`
- **Physical**: Zones are flat and independent — each Zone is a Raft Group with its own root `/`
- **Logical**: Hierarchical namespace is composed through **Mount Points** (see 5a)
- **Intra-zone**: Raft consensus guarantees linearizable reads/writes
- **Cross-zone reads**: Client-side VFS traversal across mount points (no cross-zone metadata sync)
- **Cross-zone writes**: Two approaches planned:
  - **Plan A** (Issue #1181): Nexus-to-nexus mount — client traverses zone boundaries on read
  - **Plan B** (future): Spanner-like 2PC for cross-zone atomic transactions

---

## 5. Open Questions & Future Design Work

### 5a. Inter-Zone Architecture: Mount Points & Zone Lifecycle

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

### 5b. Write Performance (NexusFS.write() ~30ms/op)

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

### 5c. Multi-Node Deployment & Testing

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

### 5d. Cross-Zone Federation (Plan B: Spanner-like 2PC)

**Status**: Not started. Plan A (Issue #1181, nexus-to-nexus mount) comes first.

**When to consider Plan B**: If we need atomic writes that span multiple zones (e.g., move a file from zone A to zone B atomically).

**Rough approach**:
- Each zone has its own Raft group
- A coordinator (TBD: which node?) runs 2PC across zone leaders
- Phase 1: Prepare (all zones lock resources, write to WAL)
- Phase 2: Commit (all zones apply, release locks)
- Requires distributed deadlock detection if zones can cross-reference

**TODO**: Evaluate if Plan A (mount) is sufficient for 90%+ of cross-zone use cases before investing in 2PC.

### 5e. Proto-to-Python Code Generation (SSOT Pattern)

**Status**: ✅ Complete. Implemented in commit 5da0bf1c.

- `scripts/gen_metadata.py` reads `proto/nexus/core/metadata.proto` and generates:
  - `src/nexus/core/_metadata_generated.py` — FileMetadata + PaginatedResult + MetadataStore ABC
  - `src/nexus/core/_compact_generated.py` — CompactFileMetadata with dict interning
- Old `metadata.py` and `compact_metadata.py` deleted
- All imports updated (20+ files), idempotent generation verified
- `_resolve_required()` for type-safe required fields (no mypy suppressions needed)

### 5f. NexusFS Raft Re-integration

**Status**: Was done (commit 9295b82e), reverted for CI (commit 46e7884b). Blocked on CI PyO3 support.

**Plan**:
1. Add maturin build to CI (P1)
2. Re-integrate RaftMetadataStore behind config flag (`NEXUS_METADATA_STORE=raft|sqlalchemy`)
3. Ensure all existing tests pass with both store backends
4. Gradual migration: new deployments use Raft, existing can stay on SQLAlchemy

---

## 6. What's Needed to Reach Production Federation

### P0: Immediate (this sprint)
1. ~~Restore accidentally deleted files~~ ✅
2. ~~Rebuild proto SSOT files~~ ✅ (`scripts/gen_metadata.py` + generated files)
3. Complete global tenant→zone rename (3866 remaining occurrences)
4. Write this memo ✅

### P1: Short-term
5. Add maturin (PyO3) build to CI — ✅ `test.yml` builds `nexus_raft` with `--features python` (#1234)
   - Main `Dockerfile` deferred until production-ready (currently only nexus_fast)
   - gRPC feature (`--features python,grpc`) deferred until transport is ready for true federation
6. Re-integrate RaftMetadataStore into NexusFS (behind feature flag or config)
7. Get gRPC transport compiling and tested (proto → tonic codegen)

### P2: Medium-term
8. Implement nexus-to-nexus mount (Issue #1181) for cross-zone reads
9. Multi-node integration tests with Docker Compose
10. Production deployment guide

### P3: Future
11. Cross-zone distributed transactions (Spanner-like 2PC)
12. ~~Zone nesting/overlaps design~~ ✅ Design decided (Section 5a: Mount Points + Hard Link lifecycle)
13. Write performance optimization (NexusFS.write() ~30ms/op)

---

## 7. Key Files Reference

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