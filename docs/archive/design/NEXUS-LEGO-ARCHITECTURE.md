# NEXUS LEGO ARCHITECTURE: Feature Bricks on a Microkernel Baseplate

### A Design Document for the Modular Agent OS

*Companion to: [KERNEL-ARCHITECTURE.md](./KERNEL-ARCHITECTURE.md) (SSOT for kernel/driver/pillar details) | [AGENT-OS-DEEP-RESEARCH.md](./AGENT-OS-DEEP-RESEARCH.md)*

*Issue tracking: [WORKSTREAMS.md](../../WORKSTREAMS.md) (13 parallel streams, ~111 active issues)*

---

## TABLE OF CONTENTS

1. [Vision: Nexus as a Lego System](#part-1-vision-nexus-as-a-lego-system)
2. [The Four-Tier Architecture](#part-2-the-four-tier-architecture)
3. [Feature Bricks Catalog](#part-3-feature-bricks-catalog)
4. [Brick Composition Patterns](#part-4-brick-composition--kiss--recursive)
5. [Design Decisions — Trade-Offs](#part-5-design-decisions--trade-offs)
6. [Technology Strategy](#part-6-technology-strategy)
7. [Linux Kernel Lessons](#part-7-linux-kernel-lessons)
8. [Agent Communication — Filesystem as IPC](#part-8-agent-communication--filesystem-as-ipc)
9. [Mount I/O Profiles](#part-9-mount-io-profiles)
10. [Edge Deployment](#part-10-edge-deployment)
11. [Code Cross-Reference](#part-11-code-cross-reference)
12. [References](#part-12-references)

---

## PART 1: VISION — NEXUS AS A LEGO SYSTEM

### 1.1 The Metaphor

Lego works because of **a universal baseplate** and **bricks that snap together through a standard interface**. Nexus follows the same principle:

```
┌───────────────────────────────────────────────────────────┐
│                      AGENT WORKSPACE                      │
│                                                           │
│  ┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐  │
│  │ Pay  │ │Search│ │Memory│ │ReBAC │ │Skills│ │ Auth │  │
│  │Brick │ │Brick │ │Brick │ │Brick │ │Brick │ │Brick │  │
│  └──┬───┘ └──┬───┘ └──┬───┘ └──┬───┘ └──┬───┘ └──┬───┘  │
│  ═══╪════════╪════════╪════════╪════════╪════════╪═════  │
│     │     BRICK PROTOCOLS (the studs)    │        │       │
│  ═══╪════════╪════════╪════════╪════════╪════════╪═════  │
│  ┌──┴────────┴────────┴────────┴────────┴────────┴────┐  │
│  │          SYSTEM SERVICES (always-on)               │  │
│  │  AgentRegistry · Namespace · EventLog · Hook · Sched│  │
│  └────────────────────────┬───────────────────────────┘  │
│  ═════════════════════════╪═══════════════════════════    │
│                    KERNEL PROTOCOLS                       │
│  ═════════════════════════╪═══════════════════════════    │
│  ┌────────────────────────┴───────────────────────────┐  │
│  │              KERNEL (the baseplate)                 │  │
│  │            VFS Router · File Metadata               │  │
│  └──────────────┬─────────────────┬───────────────────┘  │
│  ═══════════════╪═ CONNECTOR ═════╪═══════════════════   │
│  ═══════════════╪═ PROTOCOL  ═════╪═══════════════════   │
│                 │  (plug shape)   │                       │
│  ┌──────────────┴─────────────────┴───────────────────┐  │
│  │           STORAGE PILLARS (pluggable)              │  │
│  │  Metastore · ObjectStore · RecordStore · CacheStore│  │
│  └────────────────────────────────────────────────────┘  │
└───────────────────────────────────────────────────────────┘
```

ConnectorProtocol sits **between** the kernel and storage pillars as a boundary contract — the "plug shape" that both sides agree on. It's defined in `core/protocols/connector.py` because the kernel references it, but it's not a kernel mechanism itself.

### 1.2 Design Principles

| # | Principle | Lego Analogy | Equivalent |
|---|-----------|-------------|------------|
| 1 | **Minimal kernel, maximal bricks** | Small baseplate, unlimited bricks | Microkernel: 2 kernel protocols + 5 system services |
| 2 | **Standard interface** | Studs are always 8mm apart | `Protocol` classes define every boundary |
| 3 | **Bricks don't know about each other** | Red brick doesn't know blue | No import between feature modules |
| 4 | **Hot-swappable** | Pull off, put another on | `register_connector()` / plugin registry |
| 5 | **Composition over inheritance** | Stack bricks, don't mold shapes | `file_operations` vtable, not class trees |
| 6 | **Namespace is security** | Each builder gets own baseplate view | Plan 9: unmounted = invisible |
| 7 | **Few primitives, standard ops** | Same stud spacing everywhere | Unix: read/write/open/close compose everything |

### 1.3 Current vs. Target State

**Today (develop branch, Feb 2026):**
- `NexusFS` still a large god object in `core/nexus_fs.py` (9 mixins extracted on develop, 6 Protocols created)
- `factory.py` (~1,016 LOC on develop) acts as "reincarnation server" — 4-tier boot sequence formalized
- **52+ runtime_checkable Protocols, 6 ABCs, 25+ Dataclasses** across 4 tiers:
  - `core/protocols/`: 2 kernel (VFSRouter, FileMetadata) + 8 boundary contracts (ConnectorProtocol × 7 sub-protocols + CachingConnectorContract)
  - `services/protocols/`: 35+ Protocols across 20+ domain files
  - `workflows/protocol.py`: 5 Protocols | `ipc/protocols.py`: 3 Protocols | `governance/protocols.py`: 4 Protocols
- `pay/` module (2,635 LOC) = exemplary brick with zero core imports
- Zones deeply integrated across 20+ core files

**Target:**
- 4-tier separation: Storage Pillars → Kernel (~1K LOC) → System Services (~1K LOC) → Bricks
- Brick registry: `register_brick("search", SearchBrick)` — generalizing `ParserRegistry`
- Agent sees only mounted bricks — unmounted = invisible, not forbidden
- `factory.py` stays as explicit Composition Root (Seemann pattern)

---

## PART 2: THE FOUR-TIER ARCHITECTURE

Research-backed capability-tiered model from seL4, MINIX 3, Fuchsia/Zircon. Key insight from [Liedtke (1995)](https://dl.acm.org/doi/10.1145/224056.224075): *"A concept is tolerated inside the micro-kernel only if moving it outside would prevent the system's required functionality."*

```
┌─────────────────────────────────────────────────────────┐
│  BRICKS (optional, removable, limited capability)        │
│  ReBAC, Auth, Search, Skills, Pay, Parsers, etc.         │
│  Can fail independently. System continues without them.  │
└─────────────────────────────────────────────────────────┘
                    ↓ service/brick protocols
┌─────────────────────────────────────────────────────────┐
│  SYSTEM SERVICES (always-started, critical, non-optional)│
│  AgentRegistry, NamespaceManager, EventLog,              │
│  HookEngine, Scheduler                                   │
│  Cannot be unloaded. System fails if these fail.         │
│  Code: services/protocols/ (validated by OS research)    │
└─────────────────────────────────────────────────────────┘
                    ↓ kernel protocols
┌─────────────────────────────────────────────────────────┐
│  KERNEL (mechanisms, always present)                     │
│  VFSRouterProtocol, MetastoreABC                         │
│  Code: core/protocols/                                   │
│  Boots with MetastoreABC only.                           │
└─────────────────────────────────────────────────────────┘
         ↓ ConnectorProtocol (boundary contract, 7 sub-protocols)
                    ↓ storage abstractions (DI)
┌─────────────────────────────────────────────────────────┐
│  STORAGE PILLARS (pluggable substrate)                   │
│  MetastoreABC (required), ObjectStoreABC (mount),        │
│  RecordStoreABC (services), CacheStoreABC (optional)     │
└─────────────────────────────────────────────────────────┘
```

### 2.1 Why 4 Tiers?

| Property | Kernel | System Services | Bricks |
|----------|--------|----------------|--------|
| **Boot guarantee** | Always present | Always started by factory.py | Loaded on demand |
| **Failure impact** | System crashes | Agents can't function | Feature degrades gracefully |
| **Capability scope** | Provides mechanisms | Broad access to kernel + pillars | Limited to own domain |
| **Storage dependency** | MetastoreABC only | RecordStore + CacheStore | Own storage or none |
| **Can be unloaded?** | Never | Never (in production) | Yes |
| **OS analogy** | seL4 kernel (~9K LOC) | MINIX TCB servers (~16K LOC) | MINIX replaceable servers |

### 2.2 Kernel Protocols (2)

Mechanisms requiring maximum privilege (Liedtke's test: "cannot work if moved to user space"):

| Protocol | Removes if absent | Code location |
|----------|------------------|---------------|
| `VFSRouterProtocol` | No file ops → no filesystem | `core/protocols/vfs_router.py` |
| `MetastoreABC` | No inodes → can't describe files | `core/metastore.py` |

**Validation:** The "Everything is Context" VFS paper (Dec 2025) validates VFS + metadata as THE correct kernel boundary. Agent-Kernel (ZJU, Dec 2025) uses exactly this pattern — minimal core mechanisms, everything else pluggable.

### 2.3 ConnectorProtocol — Boundary Contract (not kernel)

`ConnectorProtocol` is the **plug shape** between kernel and storage — like Linux's `struct file_operations`. The kernel *defines* it so backends can plug in, but it's not a kernel mechanism itself. Lives in `core/protocols/connector.py` because the kernel references it.

Decomposes into 7 sub-protocols:

| Sub-Protocol | Purpose |
|---|---|
| `ReadableConnector` | `read_file()`, `get_metadata()` |
| `WritableConnector` | `write_file()`, `delete_file()` |
| `ListableConnector` | `list_directory()` |
| `SearchableConnector` | `search()` (content/metadata search) |
| `StreamableConnector` | `read_stream()`, `write_stream()` |
| `BatchConnector` | `batch_read()`, `batch_write()` |
| `CachingConnectorContract` | Cache invalidation hooks |

### 2.4 System Services (5)

Critical, always-started, but CAN run outside the kernel (no hardware privilege needed):

| Service | Purpose | Code location |
|---------|---------|---------------|
| `AgentRegistryProtocol` | Who exists, API keys, capabilities | `services/protocols/agent_registry.py` |
| `NamespaceManagerProtocol` | Who sees what, mount table | `services/protocols/namespace_manager.py` |
| `EventLogProtocol` | What happened, append-only audit | `services/event_subsystem/log/protocol.py` |
| `HookEngineProtocol` | Intercept & transform, pre/post | `services/protocols/hook_engine.py` |
| `SchedulerProtocol` | Who goes next, fair-share | `services/protocols/scheduler.py` |

**35+ additional service-level Protocols** exist in `services/protocols/` for domain bricks: LLM, Memory, Trajectory, Reputation, Payment, ReBAC, OAuth, Delegation, Parse, MCP, Skills, ContextManifest, Version, APIKeyCreator, Search, Events, Mount, ShareLink, and more. These are brick interfaces, not kernel code.

Everything else — search, memory, payments, connectors, ReBAC — is a **brick**.

### 2.5 Storage Pillars

All 4 pillars are implemented on develop. Conceptual names differ from code names — table shows both:

| Pillar (Conceptual) | Actual Class | Location | Type | Implementations |
|---|---|---|---|---|
| **MetastoreABC** | `MetastoreABC` | `core/metastore.py` | Required at boot | RaftMetadataStore, FederatedMetadataProxy |
| **ObjectStoreABC** | `Backend` (ABC) | `backends/backend.py` | Mount post-init | 22+ storage backends |
| **RecordStoreABC** | `RecordStoreABC` | `storage/record_store.py` | Services-only | SQLAlchemyRecordStore (55+ usages) |
| **CacheStoreABC** | `CacheStoreABC` | `core/cache_store.py` | Optional | DragonflyCacheStore, InMemoryCacheStore, NullCacheStore |

### 2.6 Zone Model

A Zone is the fundamental isolation and consensus unit. 1 Zone = 1 Raft group = 1 independent redb database. Zones are deeply integrated across 20+ core files. See [KERNEL-ARCHITECTURE.md §4](./KERNEL-ARCHITECTURE.md).

---

## PART 3: FEATURE BRICKS CATALOG

### 3.1 Brick Catalog

Brick readiness = **core imports**. Zero core imports = brick-ready (like `pay/`).

| Brick | Protocol | LOC | Core Imports | Ready? |
|-------|----------|-----|-------------|--------|
| **NexusPay** | `PaymentProtocol` | 2,635 | **0** | **YES — exemplary** |
| **Scheduler** | `SchedulerProtocol` | 925 | 1 (TYPE_CHECKING) | **YES** |
| **Discovery** | `DiscoveryProtocol` | 424 | 0 | **YES** |
| **A2A** | `A2AProtocol` | 1,777 | Minimal | **YES** |
| **Workflows** | `WorkflowProtocol` | 2,179 | Moderate | **CLOSE** |
| **Sandbox** | `SandboxProtocol` | 3,256 | 1 | **CLOSE** |
| **Portability** | `PortabilityProtocol` | 2,599 | 3 | Needs interface |
| **Parsers** | `ParserProtocol` | 3,000 | Moderate | Needs interface |
| **LLM** | `LLMProtocol` | 2,500 | Moderate | Needs extraction |
| **Cache** | `CacheProtocol` | 2,000 | Moderate | Needs extraction |
| **Skills** | `SkillsProtocol` | 9,391 | Heavy | Needs extraction |
| **ReBAC** | `PermissionProtocol` | ~15,000 | Heavy | Needs extraction |
| **Auth/OAuth** | `AuthProtocol` | 8,813 | Heavy | Needs extraction |
| **Search** | `SearchProtocol` | 13,227 | Heavy | Needs extraction |
| **Storage** (connectors) | `ConnectorProtocol` | ~8,000 | Heavy | Needs Protocol boundary |

### 3.2 Brick Lifecycle

```
1. REGISTER   →  brick declares its Protocol implementation
2. MOUNT      →  namespace manager makes brick visible to agent
3. USE        →  VFS router dispatches to brick
4. HOOK       →  hook engine intercepts brick I/O
5. LOG        →  event log records brick activity
6. UNMOUNT    →  namespace manager removes brick from agent view
7. UNREGISTER →  brick removed from registry (hot-swap)
```

### 3.3 Brick Rules

A brick **MUST**:
- Implement exactly one Protocol (or small set of related Protocols)
- Have zero imports from other bricks
- Declare dependencies in constructor (DI, not config)
- Be testable in isolation (no kernel needed for unit tests)

A brick **MUST NOT**:
- Import from kernel internals (only from protocol interfaces)
- Hold global state outside its own scope
- Communicate with other bricks directly (use EventLog or VFS)

### 3.4 How to Add a New Brick

Follow the `pay/` module pattern:

```
nexus/bricks/<name>/
├── __init__.py              # Public API: exports Protocol implementation
├── service.py               # Protocol implementation (zero core imports)
├── providers/               # Backend-specific implementations
│   ├── provider_a.py
│   └── provider_b.py
└── tests/
    └── test_service.py      # Unit tests (no kernel needed)
```

**Registration in factory.py:**

```python
# Lazy import, config-gated
if config.get("enable_<name>", False):
    from nexus.bricks.<name> import <Name>Brick
    brick = <Name>Brick(provider=config.get("<name>_provider"))
```

**Checklist:**
- [ ] Zero imports from `nexus.core` or other bricks
- [ ] Testable in isolation
- [ ] No global state
- [ ] Lazy imports for providers
- [ ] `zone_id` passed through (not ignored)
- [ ] Exposed via REST API, not direct import

### 3.5 Kernel Dispatch Flow

```
Agent: POST /api/v2/files/search?q=quarterly+report&zone_id=acme

  1. AUTHENTICATE  → AgentRegistryProtocol.authenticate()
  2. RESOLVE       → NamespaceManagerProtocol.resolve()
  3. SCHEDULE      → SchedulerProtocol.submit()
  4. PRE-HOOK      → HookEngineProtocol.fire("pre_search", ctx)
  5. DISPATCH      → SearchProtocol.search() ◄── PROTOCOL BOUNDARY
  6. POST-HOOK     → HookEngineProtocol.fire("post_search", ctx)
  7. LOG           → EventLogProtocol.append()

  The kernel does NOT know the concrete implementation.
  Swap FAISS → Qdrant = change one line in factory.py.
```

---

## PART 4: BRICK COMPOSITION — KISS + RECURSIVE

### 4.1 Two Composition Mechanisms

Not five. Not one. Two — because they solve fundamentally different problems.

```
┌───────────────────────────────────────────────────────┐
│  Mechanism 1: DI                Mechanism 2: Wrapping  │
│  (cross-Protocol)               (same-Protocol)        │
│                                                         │
│  RAGBrick(                      Cached(                 │
│    search: SearchProtocol,        Encrypted(            │
│    llm: LLMProtocol                 S3()                │
│  )                                )                     │
│                                 )                       │
│  factory.py wires them.         factory.py assembles.   │
└───────────────────────────────────────────────────────┘
```

### 4.2 Mechanism 1: Constructor Injection (Cross-Protocol)

A brick takes Protocol dependencies in `__init__`. `factory.py` wires them.

```python
class RAGBrick:
    def __init__(self, search: SearchProtocol, llm: LLMProtocol):
        self._search = search
        self._llm = llm

    async def query(self, question: str, *, zone_id: str) -> str:
        docs = await self._search.search(question, zone_id=zone_id, limit=10)
        context = "\n".join(doc.content for doc in docs[:5])
        return await self._llm.generate(prompt=f"Context:\n{context}\n\nQ: {question}", zone_id=zone_id)

# factory.py
rag = RAGBrick(search=search_brick, llm=llm_brick)
```

Why not a DI container? `factory.py` already does this. Python's type system IS the manifest — `TypeError` on missing arg IS the dependency check.

### 4.3 Mechanism 2: Recursive Wrapping (Same-Protocol)

A brick wraps another brick implementing the **same Protocol**. Recursive — no depth limit.

```python
class CachingStorage:
    """Wraps any StorageProtocol. IS a StorageProtocol."""
    def __init__(self, inner: StorageProtocol, cache: CacheProtocol):
        self._inner = inner
        self._cache = cache

    async def read(self, path: str, *, zone_id: str) -> bytes:
        cached = await self._cache.get(f"storage:{zone_id}:{path}")
        if cached is not None:
            return cached
        data = await self._inner.read(path, zone_id=zone_id)
        await self._cache.set(f"storage:{zone_id}:{path}", data, ttl=300)
        return data

# factory.py — recursive chain
storage = CachingStorage(
    inner=EncryptedStorage(inner=CompressedStorage(inner=S3Storage(bucket="data")), key=key),
    cache=redis_cache,
)
# storage.describe() → "cache → encrypt → compress → s3"
```

**Wrapping rules:**
1. Wrapper implements **same Protocol** as its `inner`
2. Wrapper delegates unknown ops to `inner`
3. Wrapper implements `describe()` for debuggability
4. Chain assembly in `factory.py` (config-time, never runtime)
5. Each wrapper independently testable with mock `inner`

### 4.4 Events Are Infrastructure, Not Composition

Events (pub/sub via EventLog) are the kernel doing its job — not a third composition mechanism. Bricks receive `EventLogProtocol` via DI (Mechanism 1).

---

## PART 5: DESIGN DECISIONS — TRADE-OFFS

### 5.1 Mixin → Service Delegation

| | Mixins (Legacy) | Service Delegation (Target) |
|---|---|---|
| **Pros** | All methods in one `nx.` namespace | Each service testable in isolation |
| | No message passing overhead | Clear ownership boundaries, can swap impls |
| **Cons** | MRO fragility, can't test one without all | More indirection, migration risk |

**Decision:** Migrate to delegation. Phase 1: define Protocols first.

### 5.2 ReBAC as Kernel vs. Brick

**Decision:** Hybrid. `PermissionProtocol` in kernel (10 lines). ReBAC implementation (11 files) moves to a brick. Kernel calls `permission_protocol.check()` without knowing it's ReBAC.

### 5.3 Protocol vs. ABC

**Decision:** Protocol for brick interfaces (duck typing, third-party classes auto-satisfy). ABC for internal implementations (`Backend`, `RecordStoreABC`).

### 5.4 Brick Isolation Level

| Level | Mechanism | Cost |
|---|---|---|
| **L1: Module** | Separate `nexus/bricks/<name>/` | Zero runtime cost |
| **L2: Package** | Separate pip package | Build complexity |
| **L3: Process** | Sub-interpreters | Serialization overhead |

**Decision:** L1 now, L2 for mature bricks (`pay/`), L3 for untrusted third-party.

### 5.5 FUSE is a Presentation Layer

FUSE is NOT a brick — it's the kernel's syscall interface. Same category as `server/` (REST), `cli/`, `remote/` (RPC).

### 5.6 factory.py: Keep Simple

Constructor injection IS the DI container. `factory.py` grows as bricks are extracted — fine up to ~30 bricks. When it exceeds ~800 LOC, THEN add auto-discovery. Not before.

> Eclipse OSGi proved plugin machinery more complex than plugins is a death sentence. VS Code proved starting small and growing on demand works.

---

## PART 6: TECHNOLOGY STRATEGY

### 6.1 Python 3.14 Impact

| Feature | Impact on Nexus | Priority |
|---------|----------------|----------|
| **Free-Threaded (PEP 703)** | True parallel threads: ReBAC checks, search index, hooks can run concurrently. ~9% single-thread tax acceptable for I/O-bound workload. | Evaluate (#1388) |
| **Sub-Interpreters (PEP 734)** | Perfect brick isolation boundary. Only primitives cross — enforces separation. | Future (untrusted bricks) |
| **Deferred Annotations (PEP 649)** | Eliminates `from __future__ import annotations`. Cleaner Protocols. | Adopt immediately |
| **Template Strings (PEP 750)** | Safer SQL/path interpolation. | Adopt for new code |
| `compression.zstd` | Native Zstandard for bundle export | Medium |
| `uuid4()` 30% faster | Agent ID generation | Free |
| JIT (experimental) | Not ready for production | Wait |

**Python performance ceiling is real.** Faster CPython (Microsoft) canceled. JIT "often slower than interpreter." This strengthens Rust acceleration.

### 6.2 Rust Acceleration — Decision Matrix

```
Is the operation on the hot path (>1000 calls/sec)?
├── NO  → Python. Don't over-optimize.
├── YES → Is it CPU-bound (not waiting on I/O)?
│         ├── NO  → Python async. Rust won't help with I/O waits.
│         └── YES → Can you batch to amortize FFI cost?
│                   ├── NO  → Profile first. Maybe free-threading is enough.
│                   └── YES → Rust via PyO3. Design for bulk operations.
```

### 6.3 Rust Accelerated Components

| Component | Speedup | Status |
|-----------|---------|--------|
| ReBAC evaluation | 85x (~23μs vs ~2ms) | **Implemented** |
| Roaring Bitmap ops | 3,344x | **Implemented** |
| Content grep | 50-100x | **Implemented** |
| BLAKE3 hashing | 3x vs SHA-256 | **Implemented** |
| Event Log WAL | 10-40x vs Redis | **Implemented** |
| SIMD vector similarity | Hardware SIMD | **Implemented** |

### 6.4 Where Rust is NOT Worth It

| Operation | Why NOT Rust |
|-----------|-------------|
| HTTP routing / FastAPI | I/O-bound, not CPU-bound |
| Database queries | Driver already in C, bottleneck is DB |
| Config parsing | Runs once at startup |
| Test code | Developer velocity matters more |

---

## PART 7: LINUX KERNEL LESSONS

Patterns stolen from the Linux kernel:

| Linux Pattern | Nexus Equivalent | Application |
|--------------|-----------------|-------------|
| `struct file_operations` (vtable) | `Protocol` class | `ConnectorProtocol`, `SearchProtocol` |
| `register_filesystem()` | `registry.register()` | Unified BrickRegistry |
| io_uring (batch submission) | `/batch` HTTP endpoint (#1242) | Agent batch operations |
| eBPF / BPF LSM | Hook Engine (#1257) | Dynamic pre/post hooks |
| sched_ext (pluggable scheduler) | Scheduler brick (#1274) | Fair-share, priority, cost-aware |
| Kernel modules (insmod/rmmod) | Dynamic import + register | Hot-swap bricks at runtime |
| `/proc` virtual filesystem | Virtual FS drivers (#1245) | `/proc/agents`, `/proc/metrics` |
| `container_of()` macro | Composition via dataclass | Brick contains config + state |

### 7.1 VFS → Protocol-Based Plugin

```python
# Linux: struct file_operations { read, write, open, ... }
# Nexus: Protocol classes ARE vtables
class ConnectorProtocol(Protocol):
    async def read_file(self, path: str) -> bytes: ...
    async def write_file(self, path: str, data: bytes) -> WriteResult: ...
    async def list_directory(self, path: str) -> list[FileInfo]: ...
```

### 7.2 io_uring → Batch API

```python
POST /api/batch
{
  "operations": [
    {"op": "read", "path": "/data/model.bin"},
    {"op": "write", "path": "/data/output.json", "content": "..."},
    {"op": "search", "query": "embedding similarity", "top_k": 5}
  ]
}
# One HTTP roundtrip → all results
```

---

## PART 8: AGENT COMMUNICATION — FILESYSTEM AS IPC

### 8.1 The Fundamental Insight

Most agent frameworks treat storage and communication as separate concerns (3 systems). Nexus unifies them:

```
TRADITIONAL (other frameworks):
  Agent A ──► gRPC/A2A      ──► Agent B     ← separate protocol
  Agent A ──► Database/S3   ──► persist     ← separate storage
  Agent A ──► Redis Pub/Sub ──► notify      ← separate event bus

NEXUS (filesystem-as-protocol):
  Agent A ──► write("/agents/B/inbox/task.json") ──► Agent B reads

  That single write gives you:
    ✅ Communication  (B receives the message)
    ✅ Persistence    (durably stored)
    ✅ Notification   (EventBus fires FILE_WRITE)
    ✅ Authorization  (ReBAC checks A can write to B's inbox)
    ✅ Audit trail    (EventLog records the operation)
    ✅ Caching        (L2 cache warms for B's next read)
    ✅ Replication    (Raft replicates to other nodes)
```

### 8.2 Agent Communication Flow

```
Agent A (Analyst)              Nexus VFS              Agent B (Reviewer)

1. SEND: nx.write("/agents/reviewer/inbox/task_42.json", envelope)
   → VFS Router resolves path
   → ReBAC checks: analyst→editor→inbox:reviewer ✓
   → EventLog records operation
   → EventBus fires FILE_WRITE

2. RECEIVE (push, NOT poll):
   → EventBus subscription: /agents/reviewer/inbox/*
   → Agent B reads task_42.json, processes, writes response

3. RESPONSE: write to /agents/analyst/inbox/resp_42.json
   → Same flow in reverse
```

### 8.3 Message Envelope

```json
{
    "nexus_message": "1.0",
    "id": "msg_7f3a9b2c",
    "from": "agent:analyst",
    "to": "agent:reviewer",
    "type": "task",
    "correlation_id": "task_42",
    "timestamp": "2026-02-12T10:00:00Z",
    "ttl_seconds": 3600,
    "payload": { "action": "review_document", "document": "/workspace/draft.md" }
}
```

### 8.4 Protocol Stack (2026)

| Layer | Protocol | Latency | Throughput |
|-------|----------|---------|-----------|
| 3. Orchestration | LangGraph, CrewAI, AutoGen, Temporal | — | — |
| 2. Agent-to-Agent | Google A2A, Nexus Agent Mesh | 50-100ms | ~1K msg/s |
| 1. Tool/Context | Anthropic MCP, OpenAI Function Calling | 10-30ms | ~5K msg/s |
| Internal | gRPC streaming | 1-5ms | ~50K msg/s |
| Hot path | NATS/JetStream | 0.5-2ms | ~100K msg/s |

### 8.5 Tiered Hot/Cold Delivery (#1747)

For million-agent scale: NATS hot path (<1ms, 1-7M msg/s) handles real-time delivery; filesystem cold path handles durability/audit/replay asynchronously. Pattern proven by Discord (5M concurrent), WhatsApp (70M msg/s), Slack (5M WebSockets).

### 8.6 Competitive Position

**Nexus has two unique advantages:**

1. **Filesystem IS the communication protocol.** A file write simultaneously communicates, persists, authorizes, notifies, audits, caches, and replicates. Other frameworks need 3-5 separate systems.

2. **Tiered hot/cold delivery unlocks million-agent scale** without abandoning the filesystem model.

**Already implemented (11):** OTel, Prometheus, Sentry, 3 circuit breakers, gRPC, DID identity, mTLS, federation, rate limiting, backpressure.

---

## PART 9: MOUNT I/O PROFILES

### 9.1 The Problem

Today every mount shares the same caching/buffer configuration. A highway needs different rules than a parking lot.

### 9.2 I/O Profiles

| Profile | Readahead | Write Buffer | L2 Cache | Use Case |
|---------|-----------|-------------|----------|----------|
| **FAST_READ** | 64MB, 8 workers | Small (100ms/50) | High, LFU | Model weights, shared knowledge |
| **FAST_WRITE** | Disabled | Large (50ms/500), async | Low, FIFO | Conversation logs, telemetry |
| **EDIT** | Small (256KB, 2 workers) | Medium, sync mode | Medium, LRU | Code files, documents |
| **APPEND_ONLY** | Disabled | Bulk (500ms/1000), WAL | Minimal | Embeddings, time-series |
| **BALANCED** | Adaptive (512KB→32MB) | Default (100ms/100) | Medium, CLOCK | General purpose |
| **ARCHIVE** | Disabled | N/A (read-only) | Minimal | Cold storage |

### 9.3 How Profiles Wire In

```python
@dataclass
class MountConfig:
    mount_point: str
    backend: "Backend"
    priority: int = 0
    readonly: bool = False
    io_profile: IOProfile = IOProfile.BALANCED  # ← NEW

class IOProfile(StrEnum):
    FAST_READ = "fast_read"
    FAST_WRITE = "fast_write"
    EDIT = "edit"
    APPEND_ONLY = "append_only"
    BALANCED = "balanced"
    ARCHIVE = "archive"
```

**Building blocks already exist:** PathRouter, MountConfig, ReadaheadManager, WriteBuffer, LocalDiskCache, ContentCache, FeaturesConfig. **Gap:** `MountConfig` lacks `io_profile` field; caching/buffer stack is global not per-mount.

---

## PART 10: EDGE DEPLOYMENT

### 10.1 Kernel Runs Anywhere

The microkernel enables "kernel runs anywhere" — only the bricks loaded change.

```
┌──────────┐  ┌──────────────┐  ┌─────────────┐  ┌──────────┐
│   MCU    │  │   Edge Node  │  │   Desktop   │  │  Cloud   │
│  256KB   │  │  Pi/Jetson   │  │   macOS     │  │  k8s     │
├──────────┤  ├──────────────┤  ├─────────────┤  ├──────────┤
│ Kernel   │  │ Kernel       │  │ Kernel      │  │ Kernel   │
│ (Rust)   │  │ (Python)     │  │ (Python)    │  │ (Python) │
├──────────┤  ├──────────────┤  ├─────────────┤  ├──────────┤
│ 2 bricks │  │ 5 bricks     │  │ 10 bricks   │  │ ALL      │
│ Storage  │  │ + Search     │  │ + LLM       │  │          │
│ Events   │  │ + Auth/Cache │  │ + Skills    │  │          │
└──────────┘  └──────────────┘  └─────────────┘  └──────────┘
  Feature flag: NEXUS_MODE=embedded|lite|full|cloud (#1389)
```

### 10.2 Deployment Profiles

| Profile | Target | RAM | Bricks |
|---------|--------|-----|--------|
| **embedded** | MCU / WASM | <1 MB | Storage + EventLog only (Rust kernel) |
| **lite** | Pi, Jetson, mobile | 512 MB-4 GB | 5 core (no LLM, no Pay) |
| **full** | Desktop, laptop | 4-32 GB | All bricks, local inference |
| **cloud** | k8s, serverless | Unlimited | All + federation + multi-tenant |

### 10.3 Python Language Reality

Python is only a problem at MCU/WASM tier (CPython = 30-50 MB vs 256 KB budget). Fine everywhere else. The kernel's Protocols are language-agnostic — a Rust kernel and Python kernel implement the same interfaces.

### 10.4 Edge-Cloud Hybrid

Edge kernels can **proxy** to cloud bricks via `ProxyBrick` — implements Protocol locally but forwards to remote kernel over gRPC/HTTP. Offline queue buffers when cloud unreachable.

---

## PART 11: CODE CROSS-REFERENCE

### 11.1 Module Size Map

```
nexus/
├── core/          ~77K LOC  87 files  ← NEEDS DECOMPOSITION
│   ├── nexus_fs.py     ~10K LOC  ← God object (develop: mixins extracted)
│   ├── rebac_*         ~15K LOC  ← Brick candidate
│   └── protocols/       2K LOC  ← Kernel + service protocols ✓
├── server/        ~26K LOC  55 files  ← Presentation layer
├── backends/      ~20K LOC  23 files  ← Storage bricks
├── search/        ~13K LOC  18 files  ← Intelligence brick
├── services/      ~12K LOC  18 files  ← Phase 2 extractions ✓
├── storage/       ~12K LOC  24 files  ← Persistence layer
├── skills/         ~9K LOC  17 files  ← Intelligence brick
├── pay/            ~3K LOC   6 files  ← EXEMPLARY BRICK ✓
├── sandbox/        ~3K LOC   6 files  ← Runtime brick
├── portability/    ~3K LOC   5 files  ← Being extracted
├── llm/            ~3K LOC  10 files  ← Intelligence brick
├── workflows/      ~2K LOC   8 files  ← Workflow brick
├── a2a/            ~2K LOC   7 files  ← Communication brick
├── raft/           ~2K LOC   6 files  ← Consistency brick
├── scheduler/      ~1K LOC   7 files  ← System service
├── discovery/       424 LOC   3 files  ← Agent discovery
└── factory.py     ~1K LOC   1 file   ← "systemd" ✓
```

### 11.2 Brick Readiness

| Module | Core Imports | Brick Ready? |
|--------|-------------|-------------|
| **pay/** | **0** | **YES — exemplary** |
| **scheduler/** | 1 (TYPE_CHECKING) | **YES** |
| **discovery/** | 0 | **YES** |
| **a2a/** | Minimal | **YES** |
| **workflows/** | Moderate | **CLOSE** |
| **sandbox/** | 1 | **CLOSE** |
| **search/** | 1 (TYPE_CHECKING) | **CLOSE** |
| **portability/** | 6 | NO — needs contracts/ |
| **fuse/** | 8+ (deep coupling) | **NEVER** — presentation layer |

### 11.3 Proven Patterns to Copy

**`pay/` module** — the exemplary brick:
- Clean `__init__.py` exports (`CreditsService`, `NexusPay`, `X402Client`)
- Zero imports from `core/`, `server/`, or other modules
- Independent database (TigerBeetle)
- REST-only integration via `server/api/v2/routers/pay.py`

**`ParserRegistry`** (193 LOC) — the gold standard registry:
- `register()`, `get_parser()`, `get_supported_formats()`, `discover_parsers()`
- Priority-based selection, auto-discovery. Target: generalize into `BrickRegistry`.

---

## PART 12: REFERENCES

### Python 3.14

1. [What's New in Python 3.14](https://docs.python.org/3/whatsnew/3.14.html)
2. [PEP 703 — Making the GIL Optional](https://peps.python.org/pep-0703/)
3. [PEP 734 — Multiple Interpreters in the Stdlib](https://peps.python.org/pep-0734/)
4. [PEP 750 — Template String Literals](https://peps.python.org/pep-0750/)
5. [PEP 649/749 — Deferred Evaluation of Annotations](https://peps.python.org/pep-0649/)

### OS Kernel Research

6. [Liedtke (1995) "On Micro-Kernel Construction"](https://dl.acm.org/doi/10.1145/224056.224075)
7. [seL4 Whitepaper](https://sel4.systems/About/seL4-whitepaper.pdf) — formally verified microkernel
8. [MINIX 3 (Tanenbaum)](https://www.usenix.org/system/files/login/articles/61781-tanenbaum.pdf)
9. [Fuchsia/Zircon Kernel](https://fuchsia.dev/fuchsia-src/concepts/kernel)
10. [Linux VFS Overview](https://docs.kernel.org/filesystems/vfs.html)
11. [FUSE-over-io_uring](https://docs.kernel.org/next/filesystems/fuse-io-uring.html)

### Rust + Python

12. [PyO3 User Guide](https://pyo3.rs/)
13. [Maturin User Guide](https://www.maturin.rs/)
14. [Pydantic v2 Architecture](https://docs.pydantic.dev/) — 17x faster via Rust core
15. [HuggingFace Xet Protocol](https://huggingface.co/docs/xet/index) — Rust CAS for 20 PB

### Agent Ecosystem

16. [Agent-OS Blueprint](https://www.techrxiv.org/doi/full/10.36227/techrxiv.175736224.43024590) (Sept 2025)
17. [AIOS](https://arxiv.org/abs/2403.16971) (COLM 2025) — validates microkernel approach
18. [Unix Philosophy for Agentic AI](https://arxiv.org/abs/2601.11672) — few primitives, standard ops
19. [Seemann: Composition Root](https://blog.ploeh.dk/2011/07/28/CompositionRoot/) — validates factory.py

### Agent OS Research (2025-2026) — Validates Nexus Design

20. **Agent-Kernel** (ZJU, Dec 2025) — Closest microkernel parallel: 5 core modules, everything else pluggable. Validates our kernel size.
21. **"Everything is Context" VFS** (Dec 2025) — Namespace hierarchy + mount-based tool integration = closest academic design to Nexus's VFS approach.
22. **AgenticOS Workshop** (ASPLOS 2026) — First hardware/systems venue to treat agent OS as real systems problem. Validates that agent OS is not just middleware.
23. **Composable OS Kernel** (Aug 2025) — Protocol-based composition, component-level reconfiguration without recompilation. Validates our Protocol pattern.
24. **AgentFS** (Turso, 2025) — Filesystem + metadata as unified agent interface. Validates our VFS-as-IPC approach.
25. **Google A2A v0.3** (2025) — Agent Card + Task lifecycle, 150+ organizations, Linux Foundation. Standard for agent-to-agent communication.
26. **Anthropic MCP** (2025-2026) — Donated to Agentic AI Foundation (Linux Foundation), 10,000+ servers, 97M+ monthly SDK downloads. Standard for tool/context integration.
27. **OpenAI Agents SDK** (2025) — Handoffs pattern, Guardrails, tool_choice enforcement. Alternative composition approach.

**Key validation from research:**
- Nexus's kernel is the **right size** — not too thin (AgentFS), not too thick (AIOS). Closest to Agent-Kernel's approach.
- 4-pillar storage model is **best-in-class** — no other system has as rigorous storage orthogonality.
- VFS + FileMetadata as kernel protocols validated by "Everything is Context" paper and AgentFS.
- Zone-based isolation is **ahead of most systems** — only Fuchsia/Zircon has comparable namespace isolation.
- **Potential additions to consider:** LockProtocol at kernel level (distributed locking), ContextPipeline protocol (agent context management), A2A Agent Card support (interop with A2A ecosystem).

### Nexus Internal

28. [KERNEL-ARCHITECTURE.md](./KERNEL-ARCHITECTURE.md) — SSOT for kernel/driver/pillar details
29. [AGENT-OS-DEEP-RESEARCH.md](./AGENT-OS-DEEP-RESEARCH.md) — 79 academic references
30. [WORKSTREAMS.md](../../WORKSTREAMS.md) — 13 parallel work streams, issue tracking
