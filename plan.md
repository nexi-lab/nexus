# Plan: #1307 — Sandbox Authentication through Agent Registry

## Approved Decisions

| # | Section | Decision | Choice |
|---|---------|----------|--------|
| 1 | Architecture | Auth boundary | **1A**: New `SandboxAuthService` orchestration layer |
| 2 | Architecture | Dependency wiring | **2A**: Add `AgentRegistry` to existing factory |
| 3 | Architecture | Namespace scope | **3A**: Construct namespace, defer FUSE filtering to #1305 |
| 4 | Architecture | Event recording | **4A**: Events emitted from `SandboxAuthService` |
| 5 | Code Quality | DRY retry pattern | **5A**: Extract `_execute_with_retry()` helper |
| 6 | Code Quality | Orphan containers | **6A**: Add container cleanup on DB failure |
| 7 | Code Quality | Event storage | **7B**: New `agent_events` audit table |
| 8 | Code Quality | agent_id typing | **8A**: Type-safe split (service=required, manager=optional) |
| 9 | Tests | SandboxManager tests | **9A**: Add unit tests before modifying |
| 10 | Tests | Auth service tests | **10A**: Unit + integration tests |
| 11 | Tests | Edge cases | **11A**: All 7 failure modes tested |
| 12 | Tests | E2E | **12A**: Add E2E test for full auth pipeline |
| 13 | Performance | Sync/Async | **13A**: `asyncio.to_thread()` wrapper |
| 14 | Performance | Namespace latency | **14A**: Rely on existing cache (do nothing) |
| 15 | Performance | Budget enforcement | **15A**: Feature flag (`budget_enforcement=False` default) |
| 16 | Performance | Event writes | **16C**: Synchronous inserts (simple) |

## File Structure

### New Files

```
src/nexus/sandbox/
    auth_service.py             # SandboxAuthService — orchestration layer (~200 lines)
    events.py                   # AgentEventLog — audit table writer (~80 lines)

alembic/versions/
    add_agent_events_table.py   # Migration for agent_events table (~40 lines)

tests/unit/sandbox/
    __init__.py
    test_sandbox_manager.py     # Unit tests for SandboxManager (~250 lines)
    test_auth_service.py        # Unit tests for SandboxAuthService (~300 lines)

tests/integration/sandbox/
    __init__.py
    test_sandbox_auth_integration.py  # Integration test with real DB (~150 lines)
```

### Modified Files

```
src/nexus/sandbox/sandbox_manager.py  # Extract _execute_with_retry(), add cleanup on DB failure
src/nexus/sandbox/__init__.py         # Export SandboxAuthService
src/nexus/storage/models.py           # Add AgentEventModel
src/nexus/factory.py                  # Create AgentRegistry, return in services dict
src/nexus/server/fastapi_server.py    # Wire SandboxAuthService, use for sandbox endpoints
tests/e2e/test_agent_registry_e2e.py  # Add sandbox auth E2E test class
```

## Data Model: `agent_events` Table

```python
class AgentEventModel(Base):
    __tablename__ = "agent_events"

    id = Column(String, primary_key=True)          # UUID
    agent_id = Column(String, nullable=False, index=True)
    event_type = Column(String, nullable=False)     # "sandbox.created", "sandbox.stopped", etc.
    zone_id = Column(String, nullable=True)
    payload = Column(JSON, nullable=True)           # Event-specific data
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    __table_args__ = (
        Index("ix_agent_events_agent_created", "agent_id", "created_at"),
        Index("ix_agent_events_type", "event_type"),
    )
```

## SandboxAuthService API

```python
class SandboxAuthService:
    """Orchestrates sandbox creation through Agent Registry.

    Pipeline: validate agent -> construct namespace -> check budget ->
              create sandbox -> record events.

    This is a platform service that USES kernel primitives (Agent Registry,
    Namespace Manager) — the sandbox doesn't bypass the kernel.
    """

    def __init__(
        self,
        agent_registry: AgentRegistry,
        sandbox_manager: SandboxManager,
        namespace_manager: NamespaceManager | None = None,
        nexus_pay: NexusPay | None = None,
        event_log: AgentEventLog | None = None,
        session_factory: sessionmaker[Session] | None = None,
        budget_enforcement: bool = False,       # Feature flag (#15A)
    ) -> None: ...

    async def create_sandbox(
        self,
        agent_id: str,                          # Required (not optional) (#8A)
        owner_id: str,
        zone_id: str,
        name: str,
        ttl_minutes: int = 10,
        provider: str | None = None,
        template_id: str | None = None,
    ) -> SandboxAuthResult: ...

    async def stop_sandbox(
        self,
        sandbox_id: str,
        agent_id: str,
    ) -> dict[str, Any]: ...

    async def connect_sandbox(
        self,
        sandbox_id: str,
        agent_id: str,
        mount_path: str = "/mnt/nexus",
        nexus_url: str | None = None,
        nexus_api_key: str | None = None,
    ) -> dict[str, Any]: ...
```

## SandboxAuthResult

```python
@dataclass(frozen=True)
class SandboxAuthResult:
    """Immutable result of authenticated sandbox creation."""
    sandbox: dict[str, Any]          # Sandbox metadata dict
    agent_record: AgentRecord        # Validated agent snapshot
    mount_table: list[MountEntry]    # Constructed namespace (may be empty)
    budget_checked: bool             # Whether budget was enforced
```

## Implementation Steps (TDD Order)

### Phase 1: Safety Net (Tests for Existing Code)

**Step 1**: `tests/unit/sandbox/test_sandbox_manager.py`
- ~20-25 tests for existing SandboxManager
- Cover: create, pause, resume, stop, list, cleanup, name uniqueness, DB errors
- Fixtures: in-memory SQLite + mocked providers
- Run RED (they should pass since testing existing behavior)

### Phase 2: Code Quality Fixes

**Step 2**: Extract `_execute_with_retry()` in `sandbox_manager.py` (#5A)
- Private method: `_execute_with_retry(self, stmt) -> Any`
- Replace 5 duplicate try/except blocks
- Run Step 1 tests: GREEN

**Step 3**: Add orphan container cleanup in `create_sandbox()` (#6A)
- If DB commit fails after container creation, call `provider_obj.destroy(sandbox_id)`
- Add test in Step 1 file for this specific edge case
- Run tests: GREEN

### Phase 3: Database Schema

**Step 4**: Add `AgentEventModel` to `storage/models.py` (#7B)
- Append-only audit table
- Indexes: (agent_id, created_at), (event_type)

**Step 5**: Alembic migration `add_agent_events_table.py`
- Creates `agent_events` table

### Phase 4: Event Log

**Step 6**: `src/nexus/sandbox/events.py`
- `AgentEventLog` class with `record(agent_id, event_type, zone_id, payload)` method
- Uses session-per-operation pattern (same as AgentRegistry)
- Synchronous inserts (#16C)
- ~80 lines

**Step 7**: Unit tests for `AgentEventLog`
- Test: record event, query events, event with payload
- ~30 lines

### Phase 5: SandboxAuthService (Core)

**Step 8**: `src/nexus/sandbox/auth_service.py` — write tests FIRST
- `tests/unit/sandbox/test_auth_service.py`
- Tests for each step of the pipeline:
  1. Agent not found → error before container creation
  2. Agent in wrong state → error with state info
  3. Namespace construction (mock NamespaceManager)
  4. Budget insufficient → error before container creation
  5. Successful creation → agent transitions to CONNECTED
  6. Partial failure → container cleaned up, agent not left in bad state
  7. Concurrent creation → one wins, other fails gracefully
  8. Ownership mismatch → error
  9. Stop sandbox → agent transitions to IDLE, event recorded
  10. Connect sandbox → namespace passed as metadata

**Step 9**: Implement `SandboxAuthService`
- Pipeline: validate agent → check ownership → transition CONNECTED → construct namespace
           → check budget → delegate to SandboxManager → record event
- Wrapped in `asyncio.to_thread()` for sync operations (#13A)
- Budget check gated by feature flag (#15A)
- Events recorded synchronously (#16C)
- ~200 lines
- Run Step 8 tests: GREEN

### Phase 6: Factory & Server Wiring

**Step 10**: Update `factory.py`
- Add `AgentRegistry` creation in `create_nexus_services()`
- Accept `session_factory` parameter
- Return `agent_registry` in services dict

**Step 11**: Update `sandbox/__init__.py`
- Export `SandboxAuthService`, `SandboxAuthResult`, `AgentEventLog`

**Step 12**: Update `fastapi_server.py`
- Create `SandboxAuthService` with wired dependencies
- Route sandbox creation endpoints through `SandboxAuthService`
- Keep backward-compatible endpoints that don't require agent auth

### Phase 7: Integration & E2E Tests

**Step 13**: `tests/integration/sandbox/test_sandbox_auth_integration.py`
- Real in-memory SQLite with AgentRegistry + SandboxManager + EventLog
- Mock only sandbox providers
- Test full pipeline: register agent → create sandbox → verify events
- ~150 lines

**Step 14**: Extend `tests/e2e/test_agent_registry_e2e.py`
- New test class: `TestSandboxAuthE2E`
- Real PostgreSQL + mocked Docker provider
- Test: agent registration → sandbox creation → namespace construction → event recording
- ~100 lines

### Phase 8: Cleanup & Verification

**Step 15**: Run full test suite
- `uv run pytest tests/unit/sandbox/ tests/unit/core/test_agent_registry.py -v`
- `uv run pytest tests/integration/sandbox/ -v`
- Verify no regressions in existing tests

**Step 16**: Update `sandbox/__init__.py` exports and verify imports

## Key Design Principles

1. **Sandbox is a platform service, not a kernel component**
   - `SandboxAuthService` uses kernel primitives (AgentRegistry, NamespaceManager)
   - `SandboxManager` stays as infrastructure-layer lifecycle manager
   - Clean layering: API → SandboxAuthService → SandboxManager → Provider

2. **Immutability**
   - `SandboxAuthResult` is a frozen dataclass
   - `AgentRecord` is already frozen
   - `MountEntry` is already frozen

3. **Explicit over clever**
   - Type-safe split: service requires `agent_id: str`, manager keeps `agent_id: str | None`
   - Feature flag for budget enforcement (explicit opt-in)
   - Synchronous event inserts (no hidden buffering)

4. **DRY**
   - `_execute_with_retry()` replaces 5 duplicate try/except blocks
   - Single auth path through `SandboxAuthService`

5. **Edge cases handled**
   - Orphan container cleanup on DB failure
   - All 7 failure modes explicitly tested
   - Concurrent creation handled via AgentRegistry's optimistic locking

## Deferred to Follow-up Issues

- **#1305**: FUSE filtering (namespace mount table passed but not enforced in FUSE)
- **EventBus integration**: Generic event pub/sub (currently file-events only)
- **Async conversion**: Full async AgentRegistry (sync + to_thread is sufficient for now)
- **Foreign key**: `sandbox_metadata.agent_id → agent_records.agent_id` (deferred to avoid migration complexity)

## Estimated Scope

| Component | Lines (approx) | Tests (approx) |
|-----------|----------------|-----------------|
| SandboxAuthService | ~200 | ~30 unit + 7 edge cases |
| AgentEventLog | ~80 | ~10 |
| SandboxManager fixes | ~30 net (remove ~85, add ~30) | ~25 |
| Models + Migration | ~40 | — |
| Factory + Server wiring | ~40 | — |
| Integration tests | — | ~15 |
| E2E tests | — | ~5 |
| **Total** | ~390 new/modified | ~92 tests |
