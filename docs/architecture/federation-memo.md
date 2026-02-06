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
- **Current**: Flat structure, zones don't nest
- **Intra-zone**: Raft consensus guarantees linearizable reads/writes
- **Cross-zone**: Two approaches planned:
  - **Plan A** (Issue #1181): Nexus-to-nexus mount — one zone mounts another's namespace
  - **Plan B** (future): Spanner-like 2PC for cross-zone transactions

---

## 5. Open Questions & Future Design Work

### 5a. Zone Nesting & Overlaps

**Status**: No design yet. Need to decide.

**Questions**:
- Can a zone contain sub-zones? e.g., `/zone:org/zone:team/file`
- Can a single path belong to multiple zones?
- If zones can nest, how does Raft consensus scope work? (One Raft group per leaf zone? Per top-level zone?)
- What's the permission model for nested zones? (Inherit from parent? Independent?)

**Current assumption**: Flat zones only. `/zone:{id}/...` where `id` is a single-level identifier.

**TODO**: Design document needed before implementing any nesting.

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

**Status**: Generation script was never committed. Only .pyc of generated files exist.

**What existed**:
- `make proto-gen` target generated `_metadata_generated.py` and `_compact_generated.py` from `metadata.proto`
- Docstring: `"Auto-generated from proto/nexus/core/metadata.proto - DO NOT EDIT."`
- `_metadata_generated.py`: Clean `FileMetadata` dataclass + `PaginatedResult` + `validate()` + `to_compact()`/`from_compact()`
- `_compact_generated.py`: `CompactFileMetadata` with simplified string interning (`_intern()`/`_resolve()` pattern, global dict)
- Already used `zone_id` (not `tenant_id`)

**What we have now**: Hand-written `metadata.py` and `compact_metadata.py` (restored from pre-deletion). These work but are not proto-driven.

**TODO**: Recreate the `proto-gen` script/Makefile target that reads `metadata.proto` and generates both Python files. This ensures proto remains true SSOT for FileMetadata across Python and Rust.

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
2. Rebuild proto SSOT files (in progress)
3. Complete global tenant→zone rename (3866 remaining occurrences)
4. Write this memo ✅

### P1: Short-term
5. Add maturin (PyO3) build to CI
6. Re-integrate RaftMetadataStore into NexusFS (behind feature flag or config)
7. Get gRPC transport compiling and tested (proto → tonic codegen)

### P2: Medium-term
8. Implement nexus-to-nexus mount (Issue #1181) for cross-zone reads
9. Multi-node integration tests with Docker Compose
10. Production deployment guide

### P3: Future
11. Cross-zone distributed transactions (Spanner-like 2PC)
12. Zone nesting/overlaps design
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