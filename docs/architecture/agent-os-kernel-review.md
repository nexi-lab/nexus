# Nexus Kernel Architecture Review: Is This an Agent OS?

**Purpose:** Handover document for deep-dive architectural discussion. We need constructive feedback on whether the current kernel design is the right foundation for an "agent OS," or if it's a filesystem with agent services bolted on.

**Context:** This is an early-stage design review, not a code review. We're questioning foundational assumptions.

---

## 1. What is Nexus?

Nexus is an **AI-native virtual filesystem** for cognitive agents. It unifies files, databases, APIs, and SaaS tools into a single path-based API with built-in permissions, memory, semantic search, and skills management.

- **Language:** Python (kernel + services), Rust (Raft consensus + redb metastore via PyO3)
- **Stage:** Beta (v0.7.x), open source
- **Tagline:** "The AI-native filesystem for cognitive agents"

Agents interact with Nexus like a filesystem: `read`, `write`, `list`, `mkdir`, `mount`. Nexus handles storage, permissions, search, and tool integration behind a unified path API.

---

## 2. Current Kernel Architecture

The design follows an **OS-inspired layered architecture** with three layers:

```
┌──────────────────────────────────────────────────────────────┐
│  SERVICES (user space)                                        │
│  Installable/removable. ReBAC, Auth, Agents, Scheduler, etc. │
└──────────────────────────────────────────────────────────────┘
                          ↓ protocol interface
┌──────────────────────────────────────────────────────────────┐
│  KERNEL                                                       │
│  Minimal compilable unit. VFS, FileMetadataProtocol,          │
│  MetastoreABC, ObjectStoreABC interface definitions.          │
└──────────────────────────────────────────────────────────────┘
                          ↓ dependency injection
┌──────────────────────────────────────────────────────────────┐
│  DRIVERS                                                      │
│  Pluggable at startup. redb, S3, LocalDisk, gRPC, etc.        │
└──────────────────────────────────────────────────────────────┘
```

### 2.1 Three Layers

| Layer | Swap time | What it contains | Linux analogue |
|-------|-----------|-----------------|----------------|
| **Kernel** | Never | Interface definitions (ABCs, Protocols), VFS routing, syscall dispatch | vmlinuz core (scheduler, mm, VFS) |
| **Drivers** | Config-time (DI at startup) | Storage implementations: redb, S3, PostgreSQL, Dragonfly | Compiled-in drivers (`=y`) |
| **Services** | Runtime (load/unload) — target state | 22 domain protocols (ReBAC, Mount, Auth, Agents, Search, Skills, ...) | User-space daemons (systemd units) |

Services depend on kernel interfaces, never the reverse. The kernel operates without any services loaded.

### 2.2 Four Storage Pillars (Data ABCs)

Storage is abstracted by **access pattern**, not domain:

| Pillar | ABC | Capability | Kernel Role |
|--------|-----|------------|-------------|
| **Metastore** | `MetastoreABC` | Ordered KV, CAS, prefix scan, optional Raft SC | **Required** — sole kernel init param |
| **ObjectStore** | `ObjectStoreABC` | Streaming blob I/O, immutable blobs, petabyte scale | **Interface only** — mounted dynamically |
| **RecordStore** | `RecordStoreABC` | Relational ACID, JOINs, FK, vector search | **Services only** — optional |
| **CacheStore** | `CacheStoreABC` | Ephemeral KV, Pub/Sub, TTL | **Optional** — degrades to `NullCacheStore` |

The kernel boots with **only the Metastore**. Everything else layers on top.

### 2.3 Dual-Axis ABC Architecture

Two independent axes of abstraction, composed via dependency injection:

- **Data ABCs** (the 4 pillars): WHERE is data stored?
- **Ops ABCs** (28 scenario domains): WHAT can users/agents DO?

A concrete class sits at the intersection:

```
ReBACManager (Services layer)
  ├── implements PermissionProtocol   ← Ops axis
  └── consumes RecordStoreABC         ← Data axis
```

The Protocol itself has no storage opinion. Data and Ops axes are orthogonal.

### 2.4 Kernel Interfaces

| Interface | Linux Analogue | Purpose |
|-----------|---------------|---------|
| `MetastoreABC` | block device | Ordered KV primitive |
| `FileMetadataProtocol` | `struct inode_operations` | Typed FileMetadata CRUD over MetastoreABC |
| `VFSRouterProtocol` | VFS `lookup_slow()` | Path resolution |
| `ObjectStoreABC` (= `Backend`) | `struct file_operations` | Blob I/O interface |
| `CacheStoreABC` | (no direct analogue) | Ephemeral KV + Pub/Sub primitives |

### 2.5 Service Protocols (22 exist, 9 gaps)

| Category | Protocols |
|----------|-----------|
| Permission & Visibility | PermissionProtocol, NamespaceManagerProtocol |
| Search & Content | SearchProtocol, SearchBrickProtocol, LLMProtocol |
| Mount & Storage | MountProtocol, ShareLinkProtocol, OAuthProtocol |
| Agent Infra | AgentRegistryProtocol, SchedulerProtocol |
| Events & Hooks | EventLogProtocol, HookEngineProtocol, EventsProtocol |
| Domain Services | SkillsProtocol, PaymentProtocol |
| **Missing (9 gaps)** | Version, Memory, Trajectory, Delegation, Governance, Reputation, OperationLog, Plugin, Workflow |

### 2.6 Zone (Isolation Unit)

A Zone is the fundamental isolation and consensus unit:
- **Data isolation:** Each zone has its own redb database
- **Consensus boundary:** 1 Zone = 1 Raft group
- **Horizontal scaling:** Zones scale independently
- Zones do NOT control permissions (that's ReBAC) or file content location (that's ObjectStore)

### 2.7 Current Problem

`NexusFS` (the kernel entry point) is currently a **mixin-based god object** that contains kernel code AND all service code (ReBAC, OAuth, Skills, MCP, Events, Tasks). Mixins are compile-time composition — cannot add/remove at runtime, cannot compose different "distros."

Target: Extract mixins → standalone services → ServiceRegistry with `load_service()` / `unload_service()`.

---

## 3. The Core Question: Is This an Agent OS?

Through internal design review, we arrived at a fundamental tension:

**The kernel is an excellent filesystem kernel. But Nexus calls itself an "agent OS." Agents have zero representation in the kernel — they are purely service-layer.**

### 3.1 The Linux Analogy Exposes the Gap

Linux is a process OS. Its kernel has multiple peer subsystems:

```
Linux Kernel
├── VFS / Filesystem    ← manages files
├── Process scheduler   ← manages processes
├── Memory management   ← manages memory
├── IPC                 ← inter-process communication
├── Security            ← capabilities, namespaces
└── ...
```

Processes aren't built on top of files. They're a **peer concept** — `task_struct` is a kernel data structure alongside inodes. Remove the process subsystem and you have a filesystem, not an OS.

Nexus today:

```
Nexus Kernel
├── File subsystem (VFS, FileMetadata, Metastore)
└── That's it
```

Agents live entirely in services. The kernel treats every caller as an anonymous file operation — it has no concept of "agent A is doing this read."

### 3.2 Why Not Just Add Agent Primitives to the Kernel?

We explored this and hit a key insight:

**Linux puts processes in the kernel because:**
- Context switches happen millions of times per second — performance demands it
- The kernel IS the executor — it controls the CPU
- Only the kernel can enforce memory isolation between processes

**Agent runs operate at seconds-to-minutes per step:**
- Service-layer overhead is negligible at this timescale
- The kernel is NOT the executor — the LLM provider is
- Isolation can be enforced through file-level capabilities

So the performance/execution argument that justifies kernel-level process management **doesn't apply** to agents.

### 3.3 But Agent State Should Be Persistent

One argument that DID emerge for kernel-level agents:

Linux keeps `task_struct` in memory because process operations are nanosecond-scale. Ephemeral is fine — if the machine crashes, processes restart cheaply.

But agent runs take seconds to hours per step. Losing an hour of agent work to a crash is expensive. Agent state should be **persistent by default** — checkpointable and resumable.

Counter-argument: persistence is just file writes. The kernel already provides that. An agent service can write checkpoints to Metastore using regular file operations. No special kernel support needed.

### 3.4 Two Possible Definitions of Agent

We identified that "agent" is vague and needs a concrete definition:

**Agent (the entity):** Like a program binary on disk. A definition/manifest — what tools it has, what model it uses, what it can do. Exists whether running or not. This is **just a file** — no special kernel concept needed.

**AgentRun (one execution):** Like a Linux process — one instance of an agent executing a task. Has state, step log, resource consumption. The candidate for kernel promotion.

An AgentRun progresses through discrete steps:
```
Step = observe (read state) → reason (LLM call) → act (tool use, file write)
```

Repeated until completion or failure. Each step is a checkpoint opportunity.

### 3.5 Where We Landed

Two possible architectural directions:

**Direction A: Kernel = filesystem, agents = core service**
```
Nexus (agent OS)
├── Agent service       ← manages agent runs (core, but still a service)
├── Other services      ← permissions, search, etc.
├── Nexus kernel        ← manages files only
└── Drivers             ← storage backends
```
Like Android: it's a "mobile OS" but mobile-specific logic is in the framework layer, not in the Linux kernel underneath. The kernel provides primitives, services build agent management on top.

**Direction B: Kernel = filesystem + agent subsystem (peers)**
```
Nexus Kernel
├── File subsystem      ← manages files (exists)
├── Agent subsystem     ← manages agent runs (new, peer to files)
├── Channel subsystem   ← agent-to-agent communication (new)
├── Capability subsystem ← access control at syscall level (new)
└── Drivers
```
Like Linux: processes and files are both kernel concepts. The kernel actively manages both.

---

## 4. IPC: The First Concrete Answer

While the kernel question remains open, the IPC subsystem has been designed and implemented — and it decisively chose **Direction A** (filesystem-as-IPC, service-layer, not kernel). This is the strongest data point we have for or against the "everything is a file" thesis.

### 4.1 Design: Mailbox Directories

Agent communication maps onto the VFS as a directory hierarchy:

```
/agents/
├── agent:alice/
│   ├── AGENT.json          # Agent card (name, skills, status)
│   ├── inbox/              # Incoming messages (write: sender, read: recipient)
│   │   └── 20260212T100000_msg_7f3a9b2c.json
│   ├── outbox/             # Audit trail of sent messages
│   ├── processed/          # Successfully handled messages
│   └── dead_letter/        # Expired / failed / malformed
├── agent:bob/
│   └── [same structure]
```

**Message lifecycle:** `inbox/` → parse → dedup → TTL check → handler → `processed/` (or `dead_letter/` on failure).

### 4.2 Message Envelope

A `MessageEnvelope` (Pydantic, JSON-serialized) with four types:

| Type | Purpose |
|------|---------|
| `TASK` | Request from one agent to another |
| `RESPONSE` | Reply (linked via `correlation_id`) |
| `EVENT` | Notification broadcast |
| `CANCEL` | Cancel an in-flight task |

Fields: `id`, `from`, `to`, `type`, `correlation_id`, `ttl_seconds`, `payload`, `timestamp`, protocol version.

### 4.3 Two-Layer Delivery

1. **Push (low-latency):** `MessageSender.send()` writes to recipient's inbox, copies to sender's outbox, publishes EventBus notification on `ipc.inbox.{agent_id}`
2. **Poll (guaranteed):** `MessageProcessor.process_inbox()` scans inbox files, deduplicates (bounded OrderedDict, 10k FIFO), checks TTL, invokes handler, moves to `processed/` or `dead_letter/`

This gives **best-effort push with guaranteed-eventual-delivery** semantics. If push fails, polling catches it.

### 4.4 Supporting Components

| Component | File | Purpose |
|-----------|------|---------|
| **AgentProvisioner** | `ipc/provisioning.py` | Auto-creates mailbox directories on `AGENT_REGISTERED` events |
| **AgentDiscovery** | `ipc/discovery.py` | Discovers peers by listing `/agents/` — no central registry query |
| **TTLSweeper** | `ipc/sweep.py` | Background sweep (60s) moves expired messages to `dead_letter/` |
| **IPCVFSDriver** | `ipc/driver.py` | Mounts IPC storage at `/agents/` in the VFS |

### 4.5 Pluggable Storage

Three backends behind an `IPCStorageDriver` protocol:

| Driver | Use case |
|--------|----------|
| **VFS driver** | Delegates to kernel VFS — gets ReBAC, EventLog for free |
| **PostgreSQL driver** | `ipc_messages` table, zone + path indexing, Alembic-managed |
| **In-memory** | Unit tests (zero I/O) |

### 4.6 Backpressure & Safety

- Inbox capped at **1000 messages** (configurable)
- Payload capped at **1 MB**
- Bounded in-memory dedup (10k entries, FIFO eviction)
- Zone-scoped for multi-tenant isolation
- Exception hierarchy: `IPCError` → `EnvelopeValidationError`, `InboxFullError`, `InboxNotFoundError`, `MessageExpiredError`

### 4.7 What This Proves (and Doesn't)

**Validates the thesis:**
- Agent discovery via `ls /agents/` — works, no special kernel support needed
- Message passing via `write(inbox/msg.json)` — works, reuses VFS permissions
- Observability for free — can inspect any agent's mailbox with standard file operations
- Pluggable storage — same message semantics on VFS, PostgreSQL, or in-memory
- Zero new kernel primitives were required

**Leaves open:**
- **Latency:** File-based polling has higher latency than kernel-level channels. Phase 2 plans `DT_PIPE` inodes (ring buffers) to address this — but that's a **new kernel primitive**, which contradicts "kernel = filesystem only"
- **Ordering guarantees:** Filename-based chronological ordering is fragile at scale (clock skew, high throughput)
- **Backpressure signaling:** Inbox-full is detected at write time, not proactively communicated to senders
- **No kernel awareness of message flow:** The kernel doesn't know "agent A sent agent B a task." It sees file writes. This means the kernel can't enforce communication policies, rate-limit senders, or prioritize messages

The IPC brick is the strongest evidence for Direction A. But Phase 2's pipe inodes hint that pure filesystem semantics hit a ceiling for real-time agent collaboration.

---

## 5. Open Questions

1. **Is a filesystem kernel sufficient for an agent OS?** Can "agent OS" mean "a system where the whole stack (kernel + services) serves agents" — or does the kernel itself need agent awareness?

2. **What should the kernel boundary be?** The minimality principle says "only put things in the kernel that the kernel can't function without." The kernel functions fine without agents. But does that principle still hold when agents are the primary entity the system exists to serve?

3. **Are there agent-specific primitives that demand kernel-level treatment?** We identified candidates:
   - Agent identity (caller ID on every syscall, like UID)
   - Agent lifecycle (spawn, checkpoint, resume, kill)
   - Inter-agent communication (channels/message passing)
   - Capability enforcement at syscall level

   IPC has been implemented as a service on file primitives and it works. But Phase 2 already needs `DT_PIPE` (a kernel-level inode type). Is this the thin end of the wedge — will each subsystem eventually need "just one more kernel primitive" until we have Direction B anyway?

4. **Is the Linux analogy even the right one?** Maybe agent OSes are fundamentally different from process OSes and need a different architectural model entirely. What other OS or runtime models might be more appropriate?

5. **The "everything is a file" tension:** Nexus's design principle is "everything is a file." The IPC brick validates this — agent communication IS file reads/writes, discovery IS `ls /agents/`. But it also shows the limits: polling latency, no kernel-level message flow awareness, no proactive backpressure. Is the ceiling acceptable, or does real-time agent collaboration require fundamentally different primitives?

6. **What about the 9 missing protocol gaps?** The gaps are: Version, Memory, Trajectory, Delegation, Governance, Reputation, OperationLog, Plugin, Workflow. Many are agent-centric (Trajectory, Delegation, Memory). Should any of these be kernel-level rather than service-level?

---

## 6. Summary

| Concept | Current state | Question |
|---------|--------------|----------|
| Kernel | Filesystem only (VFS + Metastore + ObjectStore) | Should agents be a peer subsystem? |
| Agent | Purely service-layer (AgentRegistryProtocol, SchedulerProtocol) | Should any agent primitive be kernel? |
| Agent definition | Not explicitly modeled | Just a file? Or a kernel entity? |
| Agent run | Not explicitly modeled | Service on top of files? Or kernel `task_struct` equivalent? |
| Agent communication | **Designed** — filesystem-as-IPC brick (`src/nexus/ipc/`) | Validates "everything is a file" — but is file-based IPC enough? |
| Capability enforcement | Service-layer ReBAC only | Should basic caps be checked at kernel syscall level? |
| "Agent OS" claim | Marketing, not architecture | How do we make it real? |

We're looking for: architectural patterns, trade-offs we're missing, comparable systems (Erlang/BEAM, Plan 9, microkernel vs monolithic, container runtimes, etc.), and a clear recommendation on where to draw the kernel boundary for an agent OS.
