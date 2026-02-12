# Nexus Agent OS Overhaul — Unified Implementation Plan

*Merged: Engineering Quality (16 issues from codebase review) + Agent OS Architecture (7 phases from AGENT-OS-DEEP-RESEARCH.md) + Best Practices (79 papers, 6 OS projects, 15+ agent frameworks)*

---

## Overview

Two tracks running in parallel:

- **Track A: Engineering Foundation** — Code quality, tests, performance (my 16 issues)
- **Track B: Agent OS Architecture** — Namespaces, registry, scheduler, IPC (research 7 phases)

Track A enables Track B. You can't safely build per-agent namespaces on a god object with zero tests.

---

## Decisions Summary

### Track A: Engineering (16 issues)

| # | Issue | Decision | Effort | Phase |
|---|-------|----------|--------|-------|
| 1 | NexusFS God Object (7,608 LOC) | Facade + Domain Services | 8-12w | 2 |
| 2 | Monolithic models.py (4,609 LOC) | Domain-split + shared mixins | 2-3w | 1 |
| 3 | FastAPI Server Monolith (5,463 LOC) | Router-based decomposition | 3-4w | 2 |
| 4 | No OS Kernel Abstraction | Subsystem Registry + Lifecycle → merge with 6-component Microkernel | 4-6w | 2 |
| 5 | Remote Client Mirror (6,404 LOC) | Protocol + RPC Proxy | 4-5w | 4 |
| 6 | 567 type:ignore comments | Security-first triage | 6-8w | 1-4 |
| 7 | Circular Imports (22 files) | Dependency Inversion via Protocols | 3-4w | 2 |
| 8 | Inconsistent Config Access | Strict injection + lint ban | 2-3w | 2 |
| 9 | ReBACService 0 tests (1,378 LOC) | Comprehensive + adversarial + Hypothesis | 3-4w | 1 |
| 10 | OAuth Crypto 0 tests | Full crypto test suite | 1w | 1 |
| 11 | Migration System 0 tests | Harness + CI gate | 2-3w | 1 |
| 12 | Coverage 50% -> 80% | Phased ramp + CI gates | 14-16w | 1-5 |
| 13 | N+1 Query Risks | Profile + batch top 5 paths | 3-4w | 4 |
| 14 | PostgreSQL SPOF | Managed DB (Cloud SQL/Supabase) | 2-3w | 4 |
| 15 | Async/Sync ThreadPool mixing | Audit + fix anti-pattern bridges | 1-2w | 4 |
| 16 | No Slow Query Monitoring | Query observability layer | 2-3w | 3 |

### Track B: Agent OS Architecture (from Deep Research)

| # | Feature | Source | Effort | Phase |
|---|---------|--------|--------|-------|
| B1 | Per-Agent Namespaces (AgentNamespace + dcache) | Research Phase 0, Plan 9, 5 papers validate | 3w | 1 |
| B2 | Agent Registry + Session Lifecycle | Research Phase 1, Hubris generation counters | 3-4w | 2 |
| B3 | Transactional Event Log (DBOS pattern) | Research Phase 2, DBOS Transact v2.11 | 2-3w | 3 |
| B4 | Request Scheduler + Batch API | Research Phase 3, AIOS 2.1x, Astraea 25.5% | 3w | 3 |
| B5 | Virtual Filesystems (/proc, /sys, /dev) | Research Phase 4, Linux procfs | 2w | 4 |
| B6 | MemGPT-style Memory Paging | Research Phase 5, H-MEM, A-MEM, MemOS | 3w | 5 |
| B7 | VFS-based IPC + CRDT + Federation | Research Phase 6, CodeCRDT, Plan 9 | 4w | 5 |
| B8 | Kernel Extraction (~2K LOC NexusKernel) | Research Phase 7, seL4, Asterinas | 3-4w | 6 |

---

## Unified Dependency Graph

```
Phase 1 (Foundation — Weeks 1-8)
  TRACK A (Engineering):
  ├── #2   models.py split ──────────────────────┐
  ├── #9   ReBAC tests (safety net) ─────────────┤
  ├── #10  Crypto tests ─────────────────────────┤
  ├── #11  Migration test harness ───────────────┤
  ├── #6a  type:ignore (security modules) ───────┤
  └── #12a CI gate raise to 55% ─────────────────┘
  TRACK B (Agent OS):                            │
  └── B1   Per-Agent Namespaces ◄──── can start  │
           in parallel with #9 (uses existing     │
           PathRouter + ReBAC, no god object      │
           refactor needed yet)                   │
                                                  │
Phase 2 (Service Extraction — Weeks 5-16)        ▼
  TRACK A:
  ├── #1   NexusFS Facade + Services ◄── needs #9 safety net
  ├── #4   Subsystem Registry ◄── merge with 6-component Microkernel
  ├── #3   FastAPI Router decomp
  ├── #7   Protocol interfaces (alongside #1)
  ├── #8   Config injection (alongside #1)
  ├── #6b  type:ignore (core modules)
  └── #12b CI gate raise to 65%
  TRACK B:
  └── B2   Agent Registry + Sessions ◄── needs #4 (kernel exists)

Phase 3 (Observability + Scheduling — Weeks 13-20)
  TRACK A:
  └── #16  Query observability layer
  TRACK B:
  ├── B3   Transactional Event Log ◄── needs #1 (services extracted)
  └── B4   Request Scheduler + Batch API ◄── needs #4 (kernel exists)

Phase 4 (Performance + Virtual FS — Weeks 17-24)
  TRACK A:
  ├── #5   Protocol + RPC Proxy ◄── needs #7 (protocols exist)
  ├── #13  N+1 query profiling ◄── needs #16 (observability)
  ├── #14  Managed DB migration
  ├── #15  Async bridge audit
  ├── #6c  type:ignore (remaining)
  └── #12c CI gate raise to 75%
  TRACK B:
  └── B5   Virtual Filesystems (/proc, /sys, /dev)

Phase 5 (Memory + IPC — Weeks 22-30)
  TRACK A:
  └── #12d CI gate raise to 80%
  TRACK B:
  ├── B6   MemGPT-style Memory Paging
  └── B7   VFS-based IPC + CRDT + Federation

Phase 6 (Kernel Extraction — Weeks 28-32)
  TRACK B:
  └── B8   Extract NexusKernel (~2K LOC)
           All 6 components isolated, hot-swappable modules
```

---

## Phase 1: Foundation (Weeks 1-8)

### 1.1 Split models.py (#2)
**Goal:** 4,609-line monolith → 8-10 domain files + shared mixins

**Steps:**
1. Create `src/nexus/storage/models/` package:
   - `base.py` — `TimestampMixin`, `ZoneIsolationMixin`, `SoftDeleteMixin`, `UUIDMixin`
   - `filesystem.py` — FilePathModel, DirectoryEntryModel, FileVersionModel, ChunkModel
   - `permissions.py` — ReBACTupleModel, TigerCacheModel, PermissionCheckLogModel
   - `memory.py` — MemoryModel, TrajectoryModel, EntityModel, ReflectionModel
   - `auth.py` — UserModel, APIKeyModel, OAuthCredentialModel, SessionModel
   - `workflows.py` — WorkflowModel, WorkflowExecutionModel
   - `payments.py` — SubscriptionModel, PaymentTransactionMeta
   - `sharing.py` — ShareLinkModel, ShareLinkAccessLogModel
   - `infrastructure.py` — SandboxMetadataModel, MountConfigModel, WebhookModel
   - `__init__.py` — re-export all models for backward compatibility
2. Extract shared mixins into `base.py` (eliminates 100+ DRY violations):
   ```python
   class TimestampMixin:
       created_at: Mapped[datetime] = mapped_column(default=func.now())
       updated_at: Mapped[datetime] = mapped_column(default=func.now(), onupdate=func.now())

   class ZoneIsolationMixin:
       zone_id: Mapped[str] = mapped_column(String(64), index=True)

   class SoftDeleteMixin:
       deleted_at: Mapped[Optional[datetime]] = mapped_column(default=None)
   ```
3. Update all imports (codemod script or `replace_all` edits)
4. Verify: `alembic check`, `ruff check`, `mypy`, `pytest`

**Risk mitigation:** Keep `models/__init__.py` re-exporting everything for backward compatibility.

### 1.2 ReBAC Comprehensive Tests (#9)
**Goal:** 0 → 80-100 unit tests for ReBACService, including adversarial cases

**Steps:**
1. Create `tests/unit/services/test_rebac_service.py`
2. Test categories:
   - **Happy path (20 tests):** grant, revoke, check for all relation types
   - **Edge cases (20 tests):** wildcard subjects, cross-zone, nested groups, empty tuples
   - **Adversarial (20 tests):** permission escalation, zone boundary violations, tuple injection, race conditions
   - **Cache interaction (15 tests):** permission change → cache invalidation → correct re-check
   - **Property-based (10 tests):** Hypothesis strategies:
     - "Revoked access is never re-granted without explicit grant"
     - "Cross-zone access requires explicit cross-zone relation"
     - "Permission check is idempotent"
3. Add `hypothesis` to dev dependencies

**Verification:** 90%+ coverage on `rebac_service.py`. Hypothesis finds no counterexamples.

### 1.3 OAuth Crypto Tests (#10)
**Goal:** 0 → 20-30 tests for `oauth_crypto.py`

- Round-trip: encrypt → decrypt → matches original
- Key rotation: old key decrypts, new key encrypts
- Tampered tokens: modified ciphertext → clean error
- Wrong key: different Fernet key → clean error
- Edge cases: None, empty, very large, concurrent

**Verification:** 95%+ coverage on `oauth_crypto.py`.

### 1.4 Migration Test Harness (#11)
**Goal:** Test harness for migration system + CI gate

- Test `alembic upgrade head` / `downgrade base` / round-trip
- Test data migrators with sample data
- CI gate: new migrations must have tests

### 1.5 Type Safety — Security Modules (#6, Phase 1)
**Goal:** Zero `type: ignore` in security-critical code

**Target files:** `rebac_manager*.py`, `tiger_cache.py`, `server/auth/*.py`, `permissions.py`

### 1.6 CI Coverage Gate (#12a)
**Goal:** 50% → 55%

### 1.7 Per-Agent Namespaces (B1) ← NEW FROM RESEARCH
**Goal:** Plan 9-style per-agent mount tables. Namespace = security model.

**Why Phase 1:** The research identifies this as "the biggest architectural gap" and makes it Phase 0. It can start in parallel with testing work because it builds on existing `PathRouter` + `ScopedFilesystem` + `EnhancedReBACManager` — no god object refactor needed yet.

**Validated by:** Progent [66] (41.2%→2.2% attack reduction), MiniScope [67] (mechanical enforcement > prompting), AgentBound [68], Google Security Foundations [69], Agent Forking [70]

**Steps:**
1. Create `src/nexus/core/namespace.py` (~400 LOC):
   ```python
   class AgentNamespace:
       """Per-agent mount table. The namespace IS the security model."""

       def bind(self, source: str, target: str, flags: BindFlags) -> None: ...
       def mount(self, backend: Backend, target: str, readonly: bool = False) -> None: ...
       def unmount(self, target: str) -> None: ...
       def fork(self, mode: Literal["shared", "copy", "clean"]) -> AgentNamespace: ...
       def resolve(self, path: str) -> RouteResult: ...  # Delegates to internal PathRouter
   ```
2. Namespace construction from ReBAC grants:
   ```python
   async def construct_namespace(self, agent_id: str, grants: list[Grant]) -> AgentNamespace:
       """ReBAC grants → mount table. Unmounted paths are invisible, not denied."""
       ns = AgentNamespace()
       for grant in grants:
           backend = self.router.resolve_backend(grant.resource_path)
           ns.mount(backend, grant.resource_path, readonly=(grant.relation == "viewer"))
       return ns
   ```
3. Namespace resolution cache (dcache pattern from Linux):
   ```python
   # Cache (agent_id, path) → (backend, resolved_path)
   # Invalidate on namespace mutation (rare — only on grant change)
   ```
4. Support namespace fork semantics (Plan 9 `rfork`):
   - `shared` — child sees same mounts as parent
   - `copy` — child gets a copy, mutations don't affect parent
   - `clean` — child starts with empty namespace
5. Extend namespace to individual MCP tools (from AgentBound [68]):
   - `/tools/search` visible but `/tools/code_exec` not
6. Persistent namespace views (from Twizzler):
   - Cache constructed namespace in PostgreSQL
   - On reconnect with same `(agent_id, generation)`, restore instantly

**Verification:** Test: agent A cannot access paths not in its namespace. Namespace fork semantics work. dcache hit rate > 90%.

---

## Phase 2: Service Extraction (Weeks 5-16)

### 2.1 NexusFS Facade + Domain Services (#1)
**Goal:** 7,608-line god object → ~800-1000 line facade + domain services

Extract one mixin at a time, with tests passing after each:
- `nexus_fs_search.py` (2,803 LOC) → `SearchSubsystem`
- `nexus_fs_rebac.py` (2,585 LOC) → `PermissionSubsystem` (safety net from #9)
- `nexus_fs_skills.py` → `SkillSubsystem`
- `nexus_fs_mounts.py` → `MountSubsystem`
- `nexus_fs_oauth.py` → `OAuthSubsystem`
- `nexus_fs_share_links.py` → `ShareSubsystem`
- `nexus_fs_events.py` → `EventSubsystem`
- `nexus_fs_llm.py` → `LLMSubsystem`

NexusFS becomes a thin Facade delegating to subsystems.

### 2.2 Subsystem Registry + 6-Component Microkernel (#4 merged with Research)
**Goal:** Clean OS kernel with the 6 components from deep research

The `Kernel` class manages both module lifecycle AND the 6 microkernel components:

```python
# src/nexus/core/kernel.py
class NexusKernel:
    """6-component microkernel (from AGENT-OS-DEEP-RESEARCH.md §11.1)"""

    # Microkernel components
    agent_registry: AgentRegistry      # API key → identity → namespace
    namespace_manager: NamespaceManager # ReBAC grants → per-agent mount tables
    vfs_router: VFSRouter              # Path → backend dispatch
    event_log: EventLog                # Immutable, transactional audit trail
    hook_engine: HookEngine            # User-installable pre/post hooks
    scheduler: Scheduler               # Request admission + priority + fair-share

    # Module lifecycle
    async def register(self, subsystem: Subsystem) -> None: ...
    async def startup(self) -> None:     # dependency-ordered
    async def shutdown(self) -> None:    # reverse-order
    async def health(self) -> dict[str, SubsystemHealth]: ...
```

### 2.3 FastAPI Router Decomposition (#3)
Split `fastapi_server.py` → `app.py` (~200 LOC) + domain routers.
Fix CORS wildcard → configurable via env var.

### 2.4 Protocol Interfaces — Circular Import Fix (#7)
Create `src/nexus/contracts/` with Protocol definitions per domain.

### 2.5 Config Injection (#8)
Ban `os.environ` outside `config.py`. Inject config into services.

### 2.6 Agent Registry + Session Lifecycle (B2) ← NEW FROM RESEARCH
**Goal:** Proper agent session tracking with generation counters

**Validated by:** Hubris OS (generation counters), AIOS (agent lifecycle), AgentFS (agent identity)

**Steps:**
1. Create `src/nexus/core/registry.py` (~300 LOC):
   ```python
   class AgentRecord:
       agent_id: str
       zone_id: str                     # Billing label (NexusPay)
       api_key: str
       grants: list[Grant]              # From ReBAC → namespace mounts
       last_seen: datetime
       session_generation: int          # Increments on reconnect (Hubris pattern)
       event_count: int
       status: Literal["unknown", "connected", "idle", "suspended"]

   class AgentRegistry:
       async def authenticate(self, api_key: str) -> AgentRecord: ...
       async def construct_namespace(self, record: AgentRecord) -> AgentNamespace: ...
       async def record_event(self, agent_id: str, event: AgentEvent) -> None: ...
   ```
2. Session state machine: `UNKNOWN → CONNECTED → IDLE → SUSPENDED`
3. Generation counter: stale sessions fail cleanly
4. Lease-based handles: file handles + WebSocket subscriptions expire on timeout
5. Wire to existing `session_cleanup_task` and `sandbox_cleanup_task`

### 2.7 Type Safety + Coverage (#6b, #12b)
Raise CI gate to 65%.

---

## Phase 3: Observability + Scheduling (Weeks 13-20)

### 3.1 Query Observability Layer (#16)
SQLAlchemy event listeners for slow queries + per-request query counter + OTel metrics.

### 3.2 Transactional Event Log (B3) ← NEW FROM RESEARCH
**Goal:** VFS op + event log in same PostgreSQL transaction (DBOS pattern)

**Validated by:** DBOS Transact v2.11 (production Python), DBOS VLDB 2024

**Steps:**
1. Wrap `OperationLogModel` insert in same transaction as VFS write:
   ```python
   async def write(self, path: str, content: bytes, ctx: OperationContext) -> None:
       async with self.session.begin():  # Single transaction
           await self._do_write(path, content)
           await self._log_event(AgentEvent(
               agent_id=ctx.agent_id,
               operation="write",
               path=path,
               timestamp=utcnow(),
           ))
       # If either fails, both roll back
   ```
2. Time-travel queries: reconstruct state at any past timestamp
3. Cost attribution: sum events by agent → exact cost (feeds NexusPay)

**Verification:** Write + log entry are atomic. Simulated crash mid-write → neither persists.

### 3.3 Request Scheduler + Batch API (B4) ← NEW FROM RESEARCH
**Goal:** Priority scheduling (2.1x speedup per AIOS) + batch API (io_uring-inspired)

**Validated by:** AIOS [59] (2.1x measured speedup), Astraea [63] (25.5% JCT reduction), sched_ext (Meta 1M+ machines)

**Steps:**
1. Create `src/nexus/core/scheduler.py` (~300 LOC):
   ```python
   class PriorityClass(Enum):
       INTERACTIVE = 0   # User-facing agents — lowest latency
       BATCH = 1         # Background processing — fair share
       BACKGROUND = 2    # Indexing, maintenance — best effort

   class Scheduler:
       async def admit(self, agent_id: str, op: VFSOp) -> bool: ...
       async def dequeue(self) -> tuple[str, VFSOp]: ...
       async def set_priority(self, agent_id: str, cls: PriorityClass) -> None: ...
   ```
2. Build on existing `views.py` SQL views (8 views: ready, pending, blocked, by_priority)
3. Fair-share round robin within each priority class
4. Anti-starvation: max wait time guarantee
5. Astraea-style state classification: `io_wait`, `compute`, `tool_call`
6. Batch API endpoint:
   ```python
   # POST /batch — io_uring-inspired
   batch = [
       {"op": "read", "path": "/data/model.bin"},
       {"op": "write", "path": "/data/output.json", "content": result},
       {"op": "search", "query": "embedding"},
   ]
   results = await kernel.batch_execute(batch)  # One HTTP call, all results
   ```
7. Expose `/proc/scheduler/queues` as virtual file (queue depths per priority)
8. Pluggable scheduling policies via Hook Engine

**Verification:** 10 concurrent agents → fair scheduling. Batch API 2-4x faster than N individual calls.

---

## Phase 4: Performance + Virtual FS (Weeks 17-24)

### 4.1 Protocol + RPC Proxy (#5)
Replace 6,404-line client with subsystem-aware RPC proxy.

### 4.2 N+1 Query Profiling + Batch (#13)
Profile with observability layer (#16), batch-optimize top 5 paths.

### 4.3 Managed Database Migration (#14)
Move PostgreSQL to Cloud SQL/Supabase. Add read/write routing.

### 4.4 Async Bridge Audit (#15)
Fix `ThreadPoolExecutor + asyncio.run` anti-patterns. Keep legitimate CPU offloading.

### 4.5 Virtual Filesystems (B5) ← NEW FROM RESEARCH
**Goal:** /proc, /sys, /dev backends exposing runtime state as files

**Validated by:** Linux procfs, Plan 9, Agent-OS (POSIX-inspired VFS)

**Steps:**
1. Create virtual backend drivers:
   ```python
   # /proc/agents/<id>/ — agent status, capabilities, event history
   # /proc/agents/<id>/pressure — PSI-style pressure metrics (from Gap 2)
   # /proc/scheduler/queues — queue depths per priority
   # /sys/kernel/config — system configuration as files
   # /sys/zones/<id>/health — zone health metrics
   # /dev/auth — credential proxy (Plan 9 Factotum pattern)
   # /dev/llm — LLM provider as a device
   ```
2. Register as backends in VFS Router
3. Agents access via standard `read()` calls — no special API needed
4. Pressure metrics per agent (from Linux PSI, Gap 2):
   ```json
   {"tokens": {"some": 0.15, "full": 0.02},
    "api_calls": {"some": 0.0, "full": 0.0},
    "credits": {"some": 0.45, "full": 0.10}}
   ```

### 4.6 Type Safety + Coverage (#6c, #12c)
Raise CI gate to 75%.

---

## Phase 5: Memory + IPC (Weeks 22-30)

### 5.1 MemGPT-style Memory Paging (B6) ← NEW FROM RESEARCH
**Goal:** Automatic context paging with tiered memory hierarchy

**Validated by:** MemGPT/Letta (ICLR 2024), H-MEM [74] (4-layer hierarchy), A-MEM [73] (85-93% token reduction), MemOS [75] (ranks #1 vs Mem0/Zep)

**Steps:**
1. Extend existing `Memory` class with paging engine:
   - Tier 0 (context "RAM") → Tier 1 (agent memory "disk"): auto-summarize evicted context
   - Tier 1 → Tier 0: semantic search retrieval on demand
2. H-MEM hierarchical indexing within Tier 1:
   - Domain → Category → Memory Trace → Episode
   - O(log n) retrieval instead of flat semantic search
3. A-MEM inter-memory links (Zettelkasten-style backlinks)
4. CoW shared prefixes: shared system prompts across agents, copy on diverge (PagedAttention)
5. MGLRU-inspired eviction: multi-generation aging for memory cache (Research §1.5)
6. Extend `hotspot_prefetch_task` with MemOS-style Next-Scene Prediction

### 5.2 VFS-based IPC + CRDT + Federation (B7) ← NEW FROM RESEARCH
**Goal:** Agent-to-agent communication via VFS paths

**Validated by:** Plan 9 (everything-is-a-file IPC), CodeCRDT [76] (600 trials, <200ms convergence, zero data loss), Federation of Agents [77] (13x improvement)

**Steps:**
1. IPC drivers (VFS paths, not new protocol):
   ```
   write("/ipc/agent-b/inbox", message)     # Point-to-point
   read("/ipc/own/inbox")                    # Receive
   subscribe("/ipc/own/*")                   # Push via SSE/WebSocket
   write("/ipc/team-x/broadcast", msg)       # Team broadcast
   ```
2. `/shared/*/state` CRDT driver:
   - Yjs-compatible CRDTs (from CodeCRDT [76])
   - Formal TODO-claim protocol for multi-agent task coordination
   - CAS-backed zero-copy sharing (existing CAS infrastructure)
3. `RemoteBackend` driver (federation):
   - Nexus-to-Nexus via HTTPS (mutual TLS)
   - Agent calls `read("/partner/report.pdf")` — doesn't know it's remote
   - Network transparency (Plan 9 pattern)
4. Namespace-controlled visibility: `/ipc/agent-b/` only mounted if allowed by ReBAC
5. Versioned Capability Vectors for federation agent discovery (from [77])

### 5.3 Coverage Gate (#12d)
Raise CI gate to 80%.

---

## Phase 6: Kernel Extraction (Weeks 28-32)

### 6.1 Extract NexusKernel (B8)
**Goal:** ~2K LOC kernel with clear trusted/untrusted boundary

**Validated by:** seL4 (~10K LOC, formally verified), Asterinas framekernel, Theseus OS (hot-swap)

**Steps:**
1. Extract 6 components from NexusFS mixins into `NexusKernel`:
   - Agent Registry, Namespace Manager, VFS Router, Event Log, Hook Engine, Scheduler
2. Module interface for hot-swappable subsystems:
   ```python
   class BackendModule:
       version: str
       depends_on: list[str]
       ref_count: int  # Active operations using this module

       async def init(self) -> None: ...
       async def cleanup(self) -> None: ...
       # Hot-swap: load v2 → redirect new requests → drain v1 → unload v1
   ```
3. Boot DAG: dependency graph for subsystem init (systemd pattern)
4. Per-agent hook scoping (today hooks are global)
5. Programmable hooks: pre/post VFS operations via Hook Engine

**Verification:** Kernel < 2K LOC. All modules hot-swappable. Boot DAG correctly orders init.

---

## Key Files Affected

| File | Operation | Description |
|------|-----------|-------------|
| `src/nexus/storage/models.py` (4,609 LOC) | Split | → `models/` package (8-10 files + base.py) |
| `src/nexus/core/nexus_fs.py` (7,608 LOC) | Refactor | → ~800 LOC facade, delegates to subsystems |
| `src/nexus/core/nexus_fs_*.py` (8 mixins) | Extract | → subsystem services |
| `src/nexus/server/fastapi_server.py` (5,463 LOC) | Split | → `app.py` (~200 LOC) + `routers/` (8 files) |
| `src/nexus/remote/client.py` (6,404 LOC) | Replace | → `proxy.py` (~300 LOC) + Protocol contracts |
| `src/nexus/core/kernel.py` | Create | 6-component microkernel + subsystem lifecycle |
| `src/nexus/core/namespace.py` | Create | Per-agent mount tables (~400 LOC) |
| `src/nexus/core/registry.py` | Create | Agent session registry (~300 LOC) |
| `src/nexus/core/scheduler.py` | Create | Request admission controller (~300 LOC) |
| `src/nexus/core/event_log.py` | Create | Transactional audit trail |
| `src/nexus/contracts/*.py` | Create | Protocol interfaces per domain |
| `src/nexus/backends/proc_backend.py` | Create | Virtual /proc filesystem |
| `src/nexus/backends/ipc_backend.py` | Create | VFS-based IPC driver |
| `src/nexus/backends/remote_backend.py` | Create | Nexus-to-Nexus federation driver |
| `tests/unit/services/test_rebac_service.py` | Create | 80-100 tests |
| `tests/unit/server/auth/test_oauth_crypto.py` | Create | 20-30 tests |
| `tests/unit/migrations/*.py` | Create | Migration test harness |
| `.github/workflows/test.yml` | Modify | Coverage gates, type:ignore CI checks |

---

## Risks and Mitigation

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Service extraction breaks permissions | Medium | Critical | #9 ReBAC tests as safety net BEFORE extraction |
| Per-agent namespaces break backward compat | Medium | High | Fallback to global namespace if agent has no grants |
| Alembic migration breaks after models split | Low | High | `alembic check` after split, keep `__init__.py` re-exports |
| Scheduler adds latency to fast paths | Medium | Medium | Skip scheduler for single-agent embedded mode |
| Remote client proxy loses edge cases | Medium | Medium | Parity test in CI, gradual migration |
| Managed DB migration causes downtime | Low | High | Blue-green: run both DBs in parallel, switch DNS |
| Phase 2 takes longer than estimated | High | Medium | Each mixin extraction is independent — partial completion still valuable |

---

## Success Metrics (from Research §12)

| Metric | Current | After Phase 3 | After Phase 6 |
|--------|---------|---------------|---------------|
| Architecture model | HTTP request/response | Async entity + batch + scheduler | Full Agent OS |
| Microkernel components | 0 | 4 (registry, namespace, VFS, events) | 6 (+ hooks, scheduler) |
| Agent session states | 1 (registered) | 4 (unknown/connected/idle/suspended) | 4 + event audit |
| Security model | ReBAC check per op | Namespace-based (structural) | Namespace (single mechanism) |
| Scheduling | FIFO (HTTP) | Priority + fair-share + pluggable | + custom policies via hooks |
| Agent communication | None | — | Filesystem IPC + CRDT + pub/sub |
| Federation | None | — | RemoteBackend (Nexus-to-Nexus) |
| Batch ops per call | 1 | N (batch API) | N |
| Virtual filesystems | 0 | 0 | 3 (/proc, /sys, /dev) + IPC |
| Test coverage | ~50% | 65% | 80%+ |
| NexusFS LOC | 7,608 | ~2,000 | ~800 (facade) |
| Agent OS Readiness | 47% | ~65% | ~90% |

---

## Estimated Timeline

| Phase | Weeks | Parallel Tracks |
|-------|-------|-----------------|
| Phase 1: Foundation | 1-8 | Models split + Tests + Namespaces (all parallel) |
| Phase 2: Service Extraction | 5-16 | NexusFS refactor + Agent Registry (overlap Phase 1) |
| Phase 3: Observability + Scheduling | 13-20 | Event Log + Scheduler + Query observability |
| Phase 4: Performance + Virtual FS | 17-24 | DB migration + /proc backends (overlap Phase 3) |
| Phase 5: Memory + IPC | 22-30 | MemGPT paging + VFS IPC + Federation |
| Phase 6: Kernel Extraction | 28-32 | Extract NexusKernel, hot-swap modules |

**Total: ~32 weeks (8 months)** with significant parallelism between tracks.
**Quick wins (first 4 weeks):** models.py split, ReBAC tests, crypto tests, namespace prototype.

---

## Competitive Position After Completion

| Capability | Nexus (current) | Nexus (after) | AIOS | Agent-OS | LangGraph | CrewAI |
|------------|----------------|---------------|------|----------|-----------|--------|
| VFS with mountable backends | Yes (production) | Yes + per-agent namespaces | No | Partial | No | No |
| Multi-tenant permissions | ReBAC (production) | Namespace-as-capability | Basic | Policy engine | No | No |
| Agent scheduling | None | Priority + fair-share + pluggable | FIFO/RR/Priority | None | None | Sequential |
| Batch API | `write_batch` only | Full io_uring-style batch | None | None | None | None |
| Agent memory | 3-tier (production) | MemGPT-style paging + H-MEM indexing | Basic | Episodic | None | None |
| Agent IPC | Event bus (infra) | VFS-based point-to-point + CRDT | None | Message bus | Graph edges | Task delegation |
| Federation | None | RemoteBackend (network transparent) | None | None | None | None |
| Payment system | TigerBeetle + USDC | + event-based cost attribution | None | None | None | None |
| Rust acceleration | ReBAC 85x, grep 50x | Same | None | None | None | None |

---

## Sources

### Codebase Review
- [God Object Anti-Pattern in Python](https://softwarepatternslexicon.com/patterns-python/11/2/4/)
- [FastAPI Best Practices](https://github.com/zhanymkanov/fastapi-best-practices)
- [FastAPI Bigger Applications](https://fastapi.tiangolo.com/tutorial/bigger-applications/)
- [SQLAlchemy Project Structure](https://github.com/sqlalchemy/sqlalchemy/discussions/9283)
- [Async Python Best Practices](https://betterstack.com/community/guides/scaling-python/python-async-programming/)
- [FastAPI in Production](https://dev.to/mrchike/fastapi-in-production-build-scale-deploy-series-a-codebase-design-ao3)
- [Refactoring at Scale](https://understandlegacycode.com/blog/key-points-of-refactoring-at-scale/)

### Agent OS Research (from AGENT-OS-DEEP-RESEARCH.md)
- AIOS: LLM Agent Operating System (COLM 2025) — 2.1x scheduling speedup
- Astraea: State-Aware Scheduling (arXiv 2512.14142) — 25.5% JCT reduction
- Progent: Programmable Privilege Control (arXiv 2504.11703) — validates namespace-as-capability
- MiniScope: Least Privilege Framework (arXiv 2512.11147) — mechanical > prompt-based enforcement
- CodeCRDT: Multi-agent CRDTs (arXiv 2510.18893) — zero data loss, <200ms convergence
- Agent Contracts (arXiv 2601.08815) — validates budget hierarchy
- DBOS Transact v2.11 — production transactional event logging
- AgenticOS Workshop (ASPLOS 2026) — validates 5/6 microkernel components

### Open-Source Agent Frameworks
- [AIOS](https://github.com/agiresearch/AIOS) — LLM Agent OS with scheduling
- [Agent-OS](https://github.com/imran-siddique/agent-os) — POSIX-inspired safety kernel
- [LangGraph 1.0](https://github.com/langchain-ai/langgraph) — Graph-based workflows (LinkedIn, Uber)
- [CrewAI](https://github.com/crewAIInc/crewAI) — Role-based teams (60% Fortune 500)
- [Microsoft Agent Framework](https://github.com/microsoft/semantic-kernel) — Semantic Kernel + AutoGen
- [10 Open-Source Agent Frameworks 2026](https://medium.com/@techlatest.net/10-open-source-agent-frameworks-for-building-custom-agents-in-2026-4fead61fdc7c)
