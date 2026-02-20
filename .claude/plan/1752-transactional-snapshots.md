# Issue #1752: Transactional Filesystem Snapshots for Agent Rollback

## Implementation Plan

### Decisions Summary (16 decisions, all approved)

| # | Topic | Decision |
|---|-------|----------|
| 1 | Tier placement | System Service (Protocol + impl in `services/`) |
| 2 | Relationship | New standalone TransactionalSnapshot service |
| 3 | COW mechanism | CAS-metadata snapshot + PostgreSQL savepoint for atomicity |
| 4 | Concurrency | Optimistic concurrency with conflict reporting |
| 5 | Permissions | AsyncPermissionEnforcer pattern (VersionService style) |
| 6 | Sync/Async | Fully async |
| 7 | Audit trail | Full EventLog + OperationLog in TransactionalSnapshot |
| 8 | Edge cases | Strict state machine (ACTIVE -> COMMITTED/ROLLED_BACK/EXPIRED) |
| 9 | Test strategy | Full TDD pyramid (unit + integration + E2E + Hypothesis) |
| 10 | Concurrency tests | Deterministic barriers + Hypothesis stateful |
| 11 | Edge case tests | All 10 edge cases |
| 12 | Performance tests | pytest-benchmark + OTel spans |
| 13 | N+1 queries | Batch APIs from day one |
| 14 | Sessions | Materialize-then-process (short sessions) |
| 15 | Indexes | Upfront composite index design |
| 16 | OTel | Per-operation + per-batch spans (5-6 max) |

---

### Phase 1: Protocol + Model (TDD: tests first)

**Files to create:**

1. **`src/nexus/services/protocols/transactional_snapshot.py`** (~80 LOC)
   - `TransactionalSnapshotProtocol` (runtime_checkable Protocol)
   - Methods: `begin()`, `commit()`, `rollback()`, `get_transaction()`, `list_active()`
   - Dataclasses: `SnapshotId`, `TransactionState` (enum: ACTIVE, COMMITTED, ROLLED_BACK, EXPIRED)
   - `TransactionResult` (for rollback: reverted paths, conflicts, metadata)
   - `TransactionConfig` (ttl_seconds, max_paths, auto_snapshot_on_destructive)

2. **`src/nexus/storage/models/transactional_snapshot.py`** (~80 LOC)
   - `TransactionSnapshotModel` (SQLAlchemy model)
   - Fields: snapshot_id (PK), agent_id, zone_id, status, paths_json, metadata_snapshot_json, created_at, committed_at, rolled_back_at, expires_at
   - Indexes: `(agent_id, status)`, `(status, created_at)` partial on ACTIVE, `(zone_id, agent_id)`
   - `validate()` method

3. **Tests first:**
   - `tests/unit/storage/test_transaction_snapshot_model.py` (~100 LOC) — Model validation, index presence
   - `tests/unit/services/test_transactional_snapshot_protocol.py` (~50 LOC) — Protocol shape compliance

**Commit:** `feat(#1752): Add TransactionalSnapshot protocol and model`

---

### Phase 2: Service Implementation (TDD: tests first)

**Files to create:**

4. **`src/nexus/services/transactional_snapshot.py`** (~350 LOC)
   - `TransactionalSnapshotService` class implementing `TransactionalSnapshotProtocol`
   - Constructor DI: `metadata_store`, `session_factory`, `permission_enforcer`, `event_log_protocol`, `config`
   - `async begin(agent_id, paths, zone_id, context)`:
     1. Validate paths non-empty, all exist or mark as absent
     2. Short session: `get_batch(paths)` -> materialize `{path: (content_hash, metadata_json)}`
     3. Short session: Create `TransactionSnapshotModel(status=ACTIVE, expires_at=now+ttl)`
     4. EventLog: append `SNAPSHOT_BEGIN` event
     5. Return `SnapshotId`
   - `async commit(snapshot_id, context)`:
     1. Short session: Load snapshot, verify status=ACTIVE
     2. Transition to COMMITTED, set committed_at
     3. EventLog: append `SNAPSHOT_COMMITTED` event
   - `async rollback(snapshot_id, context)`:
     1. Short session: Load snapshot + current metadata for paths
     2. Optimistic conflict detection: compare current_hash vs expected_hash
     3. `session.begin_nested()`: Batch restore metadata (put_batch + delete_batch)
     4. Transition to ROLLED_BACK, set rolled_back_at
     5. EventLog: append `SNAPSHOT_ROLLED_BACK` event with conflicts list
     6. Return `TransactionResult(reverted=[], conflicts=[], stats={})`
   - `async get_transaction(snapshot_id)`: Read-only lookup
   - `async list_active(agent_id, zone_id)`: List ACTIVE transactions
   - `async cleanup_expired()`: Background TTL cleanup

5. **Tests first (unit):**
   - `tests/unit/services/test_transactional_snapshot.py` (~600 LOC)
     - TestBegin: happy path, empty paths error, non-existent paths, permission denied
     - TestCommit: happy path, already committed, already rolled back, expired
     - TestRollback: happy path, conflict detection, absent path restoration, already committed
     - TestLifecycle: full begin->commit, begin->rollback, begin->expire
     - TestEdgeCases: all 10 edge cases from Issue #11
     - TestCleanupExpired: TTL expiry, batch cleanup

**Commit:** `feat(#1752): Implement TransactionalSnapshotService with state machine`

---

### Phase 3: Integration Tests + Factory Wiring

**Files to create/modify:**

6. **`tests/integration/services/test_transactional_snapshot_integration.py`** (~400 LOC)
   - Real SQLite + real CAS (in-memory)
   - TestAtomicRollback: multi-file rollback atomicity
   - TestConcurrentAgents: deterministic barrier tests (asyncio.Event)
   - TestConflictDetection: Agent A snapshots, Agent B writes, Agent A rollbacks -> conflicts
   - TestNonExistentPaths: snapshot absent paths, rollback deletes them
   - TestLargeTransactions: 100+ files, verify batch performance

7. **`tests/unit/services/test_transactional_snapshot_hypothesis.py`** (~200 LOC)
   - Hypothesis stateful testing: random begin/write/commit/rollback sequences
   - Properties: no data loss, all conflicts detected, state machine invariants

8. **Modify `src/nexus/factory.py`** (~10 LOC added)
   - Wire TransactionalSnapshotService with DI
   - Config-gated: `config.get("enable_transactional_snapshots", True)`

9. **Modify `src/nexus/storage/models/__init__.py`** (~2 LOC)
   - Re-export `TransactionSnapshotModel`

10. **Modify `tests/unit/storage/test_model_imports.py`** (~1 LOC)
    - Add `"TransactionSnapshotModel"` to EXPECTED_MODELS

**Commit:** `feat(#1752): Integration tests and factory wiring`

---

### Phase 4: REST API + Hook Integration

**Files to create/modify:**

11. **`src/nexus/server/api/v2/routers/snapshots.py`** (~150 LOC)
    - `POST /api/v2/snapshots/begin` — begin transaction
    - `POST /api/v2/snapshots/{snapshot_id}/commit` — commit
    - `POST /api/v2/snapshots/{snapshot_id}/rollback` — rollback
    - `GET /api/v2/snapshots/{snapshot_id}` — get transaction details
    - `GET /api/v2/snapshots/active` — list active transactions for agent
    - Pydantic request/response models

12. **Hook integration** (~30 LOC in existing hook wiring)
    - Register pre-hook on `PRE_WRITE`, `PRE_DELETE` phases
    - Hook logic: if agent has config `auto_snapshot_on_destructive=True` and no active transaction, auto-begin
    - Config-gated per agent via AgentRegistry

13. **Modify `src/nexus/server/api/v2/__init__.py`** (~2 LOC)
    - Register snapshots router

**Commit:** `feat(#1752): REST API endpoints and hook integration`

---

### Phase 5: E2E Tests + Performance Benchmarks + OTel

**Files to create:**

14. **`tests/e2e/server/test_transactional_snapshot_e2e.py`** (~300 LOC)
    - Spawn `nexus serve` subprocess
    - TestFullLifecycle: begin -> write files -> rollback -> verify restored
    - TestPermissionEnforcement: non-admin agent, permission denied
    - TestConcurrentAgents: two agents, overlapping paths, conflict detection
    - TestAutoSnapshot: hook-triggered auto-snapshot on destructive ops
    - TestAPIResponses: verify JSON response shapes

15. **`tests/benchmark/test_snapshot_performance.py`** (~150 LOC)
    - pytest-benchmark: snapshot overhead vs baseline write
    - Targets: <20% overhead for 100 files, <20% for 1000 files
    - Benchmark: begin(), commit(), rollback() individually

16. **OTel spans** (~30 LOC across service methods)
    - `transactional_snapshot.begin` span
    - `transactional_snapshot.commit` span
    - `transactional_snapshot.rollback` span
    - Child spans: `metadata.get_batch`, `metadata.put_batch`, `db.create_snapshot`, `eventlog.append`

**Commit:** `feat(#1752): E2E tests, benchmarks, and OTel instrumentation`

---

### Phase 6: NexusFS Integration + Alembic Migration

**Files to modify:**

17. **`src/nexus/core/nexus_fs.py`** (~30 LOC)
    - Add `snapshot_begin()`, `snapshot_commit()`, `snapshot_rollback()` RPC methods
    - Delegate to `self._transactional_snapshot_service`

18. **Alembic migration** (~40 LOC)
    - Create `transaction_snapshots` table
    - Add indexes
    - Both PostgreSQL and SQLite support

19. **`src/nexus/remote/client.py`** (~20 LOC)
    - Add client-side methods for snapshot RPC calls

**Commit:** `feat(#1752): NexusFS integration and migration`

---

### File Summary

| Type | Files | Est. LOC |
|------|-------|---------|
| Protocol | 1 | ~80 |
| Model | 1 | ~80 |
| Service | 1 | ~350 |
| REST Router | 1 | ~150 |
| Factory wiring | 1 (modify) | ~10 |
| NexusFS integration | 1 (modify) | ~30 |
| Client | 1 (modify) | ~20 |
| Migration | 1 | ~40 |
| Unit tests | 4 | ~950 |
| Integration tests | 1 | ~400 |
| E2E tests | 1 | ~300 |
| Benchmark | 1 | ~150 |
| OTel | inline | ~30 |
| **Total** | **~14 files** | **~2,590** |

---

### Acceptance Criteria Mapping

- [x] `TransactionalSnapshot` class in `src/nexus/services/` (System Service per LEGO)
- [x] `begin()` / `commit()` / `rollback()` API (strict state machine)
- [x] REST endpoint `/api/v2/snapshots` (5 endpoints)
- [x] Hook integration: auto-snapshot before destructive VFS ops (PRE_WRITE, PRE_DELETE)
- [x] <20% overhead on snapshot creation (pytest-benchmark + batch APIs)
- [x] Tests with concurrent agent writes during snapshot (deterministic barriers + Hypothesis)
