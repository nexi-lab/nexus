# ADR: Raft/sled Strategy — Commit, Defer, or Replace

**Issue**: [#1247](https://github.com/nexi-lab/nexus/issues/1247)
**Status**: Accepted
**Date**: 2026-02-10

## Summary

Strategic decision on the sled + Raft consensus layer: replace sled with redb, defer Raft consensus behind feature flags, and establish a tiered storage architecture (redb = embedded cache, PostgreSQL = source of truth).

## Background

The codebase includes a Raft consensus layer using sled (embedded Rust DB via PyO3). Current state:

- **sled 0.34** is in perpetual beta (since 2018, never reached 1.0), with known bugs and excessive disk usage
- **Raft consensus** is implemented (~2,500 lines) but **not used in production** — `PyLocalRaft` applies commands directly to the state machine, bypassing leader election and log replication
- **PostgreSQL** is already the primary metadata store
- **Single-node** deployments don't need Raft

### What We Actually Use Today

```
Python (raft_metadata_store.py)
  → PyO3 FFI (~5μs)
    → FullStateMachine (Rust)
      → sled (embedded KV)
```

No Raft consensus. No leader election. No log replication. The "Raft" in the name is aspirational.

## Decisions

### Decision 1: Feature-flagged storage backend (sled default, redb opt-in)

**Choice**: Both sled 0.34 and redb 2.x are available; sled is the default, redb is opt-in via `--features storage-redb`

**Rationale**:
- redb is 1.0 stable (since June 2023), actively maintained
- Pure Rust (same as sled) — no impact on PyO3 build pipeline
- Similar KV API — both export identical types (`SledStore`, `SledTree`, `SledBatch`, etc.)
- ACID with copy-on-write B-trees (LMDB-inspired) — predictable disk usage
- sled remains default until redb is proven in production
- Feature flag allows zero-risk rollback: just remove `--features storage-redb`

**How to switch**:
```bash
# Default build uses sled
cargo build

# Opt-in to redb
cargo build --features storage-redb

# Verify both backends pass tests
cargo test --lib                        # sled (default)
cargo test --lib --features storage-redb  # redb
```

**Alternatives considered**:
- **Hard swap to redb**: Simpler but no rollback path if redb has issues
- **RocksDB**: Battle-tested but adds C++ dependency, different API (column families)
- **SQLite**: Universal but different paradigm (SQL vs KV)
- **Keep sled only**: Zero effort but carries ongoing risk of data corruption

### Decision 2: Feature Flag Raft (Default Off)

**Choice**: Keep Raft code behind `consensus` feature flag, default disabled

**Rationale**:
- Production path (`PyLocalRaft`) doesn't use Raft consensus
- The `consensus` feature flag already exists in `Cargo.toml`
- Preserves ~2,500 lines of working Raft code for future multi-node use
- Default build is smaller and faster to compile

**Alternatives considered**:
- **Delete Raft code**: Cleaner, but rebuilds from scratch if multi-node needed
- **Wire up Raft for real**: 2-4 weeks effort, premature — multi-node isn't a current priority

### Decision 3: Tiered Storage Architecture

**Choice**: redb = embedded read cache + lock store (~5μs), PostgreSQL = durable source of truth

**Rationale**:
- Best of both: redb gives ~5μs reads, PG gives durability + queryability
- Clear contract: writes go to both (write-through), reads go to redb first
- PG handles complex queries (joins, full-text search, migrations)
- redb handles hot-path metadata lookups and distributed locks

**Alternatives considered**:
- **PostgreSQL only**: Simpler single source of truth, but ~1-5ms reads (200-1000x slower)
- **redb only**: Simpler but loses PG's query power and ACID guarantees across restarts

### Decision 4: Defer Raft, Use PostgreSQL Advisory Locks

**Choice**: No Raft consensus for now. Use PG advisory locks for distributed coordination.

**Rationale**:
- Single-node deployment doesn't need consensus
- PG advisory locks are proven for leader election and distributed locking
- When multi-node becomes a real requirement, evaluate **openraft** (not tikv/raft-rs):
  - openraft is async-native (Tokio), better docs, joint consensus support
  - Used in production by Databend (70K-1M writes/sec)
  - tikv/raft-rs feels dated ("2018 codebase") with minimal documentation

**Alternatives considered**:
- **openraft now**: Best Rust Raft library but premature — multi-node isn't needed
- **etcd/Consul**: Battle-tested external consensus, but adds operational infrastructure
- **PG logical replication**: Leverages existing PG but complex to operate

## Implementation Plan

### Phase 1 — Foundation (2-3 days)
1. This ADR (document decisions)
2. Implement redb backend behind `storage-redb` feature flag (sled remains default)
3. Add batch + range methods to PyO3 bindings
4. Formalize feature flags, document transport as experimental

### Phase 2 — Hardening (1-2 days)
5. Fix 3 error swallowing locations
6. Add 4 Rust state machine edge case tests
7. Add Python serialization + pagination tests

### Phase 3 — Integration (1 day)
8. Add PyO3 bridge integration tests

## Future: When Multi-Node Is Needed

When multi-node becomes a priority:
1. Evaluate **openraft** as replacement for tikv/raft-rs 0.7
2. Wire `consensus` feature flag to activate Raft log replication
3. Use gRPC transport layer (already built, behind `grpc` flag)
4. Add integration tests for transport layer
5. Deploy with 3-node cluster (2 voters + 1 witness)

## References

- [sled GitHub](https://github.com/spacejam/sled) — maintenance status
- [redb 1.0 release](https://www.redb.org/post/2023/06/16/1-0-stable-release/)
- [openraft](https://github.com/databendlabs/openraft) — modern Rust Raft
- [PG advisory locks for leader election](https://jeremydmiller.com/2020/05/05/using-postgresql-advisory-locks-for-leader-election/)
- [tikv/raft-rs](https://github.com/tikv/raft-rs) — current implementation
