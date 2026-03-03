# Ops-to-Scenario Properties Matrix

**Date:** 2026-02-16
**Status:** COMPLETE — Steps 1-3 done (28 scenarios, 22 existing Protocols, 9 gaps identified)
**Purpose:** Catalog ALL usage scenarios in Nexus and determine the optimal Ops ABC for each

**Companion document:** `data-storage-matrix.md` (Data × Storage affinity)
**This document:** Ops ABC × Scenario affinity (the second axis of the dual-axis architecture)

> **Contributing Rule — State-of-the-Art Only**
>
> This document is a **living SSOT**, not a changelog. All edits must be **in-place**:
> update the existing row/section, don't append a new one. If a decision changes,
> overwrite the old decision — the git history is the audit trail.
> Goal: the document always reads as if written today, from scratch.

---

## Dual-Axis Architecture

Two independent ABC axes, each with its own affinity analysis:

```
                        Data ABCs (4 pillars)
                        ← SOT: storage affinity
                        MetastoreABC | RecordStoreABC | ObjectStoreABC | CacheStoreABC
                              ↑
                              | (DI composition)
                              |
Ops ABCs  ────────────────────┼──────────────────────────
← SOT: scenario affinity      |
MetastoreABC                   |     Concrete class =
SearchProtocol                 |     Data ABC × Ops ABC
PermissionProtocol             |     (composed via DI)
...                            |
```

These axes are **parallel and independent**:
- Data ABCs answer: "WHERE is data stored?" (storage properties → pillar affinity)
- Ops ABCs answer: "WHAT can users/agents DO?" (scenario properties → ops affinity)

A concrete class sits at the intersection: e.g., `ReBACManager` implements `PermissionProtocol`
(Ops) and internally uses `RecordStoreABC` (Data). But the Protocol itself has no storage opinion.

---

## Methodology

Three-step **Ops-Scenario Affinity** analysis (mirrors data-storage-matrix):

### Step 1: Scenario Layer
Enumerate ALL usage scenarios. Assign **properties**. Dedup/merge redundant scenarios.
- For each scenario ask: "why does this exist?" and "is it redundant with another scenario?"
- Merge scenarios that share the same properties and lifecycle

### Step 2: Ops ABC Layer
Enumerate ALL existing Ops ABCs (Protocols). Assign **properties**. Verify **orthogonality**.
- Each Ops ABC must have a unique capability profile
- No two Ops ABCs should serve the same scenario role
- Identify missing Ops ABCs (scenarios with no ABC) and redundant ones (merge/eliminate)

### Step 3: Affinity Matching
Map **scenario requiring properties** ↔ **Ops ABC providing properties**.
- Match each surviving scenario to the Ops ABC whose properties best fit
- Result: each scenario has exactly one canonical Ops ABC home
- Also annotate kernel-tier vs service-tier for each Ops ABC

---

## Scenario Property Dimensions

| Property | Values | Meaning |
|----------|--------|---------|
| **Caller** | User / Agent / System / Any | Who initiates this scenario |
| **Frequency** | Rare / Occasional / Frequent / Hot-path | How often called in production |
| **Latency** | Best-effort / Normal / Low / Ultra-low | Response time budget |
| **Mutation** | Read / Write / Mixed | Does it change persistent state |
| **Scope** | Path / Subtree / Zone / Cross-zone / System | Blast radius of the operation |
| **Auth Model** | None / Identity / Permission / Capability | Required authorization level |
| **Statefulness** | Stateless / Session / Persistent | State management needs |
| **Federation** | Local / Zone-aware / Cross-zone | Federation impact |
| **Why Exists** | Brief rationale | First-principles justification |

## Ops ABC Property Dimensions

| Property | Values | Meaning |
|----------|--------|---------|
| **Tier** | Kernel / Service | Static kernel (never swap) vs runtime load/unload |
| **Sync Model** | Sync / Async / Mixed | Interface calling convention |
| **Method Count** | Micro (1-3) / Small (4-6) / Medium (7-15) / Large (16+) | Interface surface |
| **Coupling** | Standalone / Composed | Depends on other Ops ABCs to function |
| **Linux Analogue** | Brief | Closest Linux kernel/userspace concept |
| **Why Exists** | Brief rationale | First-principles justification |

---

## STEP 1: SCENARIO ENUMERATION

**Source of truth:** Code audit of all user-facing entry points:
- 90+ API routes (`server/api/v1/` + `v2/`)
- 180+ CLI commands (`cli/commands/`)
- 120+ NexusFS `@rpc_expose` methods (`core/nexus_fs.py`)
- 14 FUSE operations (`fuse/`)
- 35+ MCP tools (`mcp/`)
- 30+ SDK exports (`sdk/`)

### 1.1 File I/O

**Code surface:** `read`, `write`, `delete`, `rename`, `copy`, `append`, `exists`, `get_metadata`, `get_etag`, `batch_get_content_ids`, `export_metadata`, `import_metadata`, `mkdir`, `rmdir`, `is_directory`, `list`, `tree`, `write-batch`, `batch-read`, `stream`, `truncate`, `create`, `open`, `release`, `unlink`, `chmod`, `chown`, `utimens`, `getattr`, TUS `create/resume/cancel`

| Scenario | Caller | Freq | Latency | Mutation | Scope | Auth | State | Fed | Why Exists |
|----------|--------|------|---------|----------|-------|------|-------|-----|------------|
| **File Read** | Any | Hot-path | Ultra-low | Read | Path | Permission | Stateless | Zone-aware | Fundamental — retrieve file content by path |
| **File Write** | Any | Frequent | Low | Write | Path | Permission | Stateless | Zone-aware | Fundamental — store file content at path |
| **File Delete** | Any | Occasional | Normal | Write | Path | Permission | Stateless | Zone-aware | Remove file and free storage |
| **File Rename/Move** | Any | Occasional | Low | Write | Path | Permission | Stateless | Zone-aware | Atomic metadata update (no content copy) |
| **File Copy** | Any | Occasional | Normal | Write | Subtree | Permission | Stateless | Zone-aware | Duplicate content + new metadata entry |
| **File Exists** | Any | Hot-path | Ultra-low | Read | Path | Permission | Stateless | Zone-aware | Existence check without content read |
| **Get Metadata** | Any | Hot-path | Ultra-low | Read | Path | Permission | Stateless | Zone-aware | Size, etag, timestamps, entry_type |

**Step 1 Analysis:** All 7 scenarios share identical property profiles: same caller, same auth model, same scope, same federation impact. They form a **single cohesive domain** — basic path-addressed file operations. This is the inode layer.

**Current Ops ABC:** `MetastoreABC` (kernel) — covers get/put/delete/exists/list metadata. BUT: content read/write goes through VFS → ObjectStore separately. The scenario is split across two ABCs today.

**Decision:** File I/O is ONE scenario domain. Whether metadata and content operations live in one Protocol or two is a Step 3 question (affinity to Metastore vs ObjectStore).

### 1.2 Directory Operations

| Scenario | Caller | Freq | Latency | Mutation | Scope | Auth | State | Fed | Why Exists |
|----------|--------|------|---------|----------|-------|------|-------|-----|------------|
| **mkdir** | Any | Occasional | Low | Write | Path | Permission | Stateless | Zone-aware | Create directory entry |
| **rmdir** | Any | Rare | Normal | Write | Subtree | Permission | Stateless | Zone-aware | Remove directory + children |
| **List directory** | Any | Hot-path | Low | Read | Subtree | Permission | Stateless | Zone-aware | Enumerate children (non-recursive ls) |

**Step 1 Analysis:** Directory ops share the same properties as File I/O (§1.1). In Linux, `mkdir`/`rmdir` are `inode_operations` and `readdir` is `file_operations` — but they're on the same VFS objects.

**Decision: MERGE into File I/O domain.** Directory operations are file operations on entry_type=DT_DIR. Separate domain not justified.

### 1.3 Content Discovery

| Scenario | Caller | Freq | Latency | Mutation | Scope | Auth | State | Fed | Why Exists |
|----------|--------|------|---------|----------|-------|------|-------|-----|------------|
| **Glob** | Any | Frequent | Low | Read | Subtree | Permission | Stateless | Zone-aware | Pattern matching on paths |
| **Grep** | Any | Frequent | Normal | Read | Subtree | Permission | Stateless | Zone-aware | Content search within files |
| **Semantic Search** | Any | Occasional | Normal | Read | Zone | Permission | Stateless | Zone-aware | Natural language vector queries |
| **Search Index** | System | Rare | Best-effort | Write | Zone | None | Persistent | Zone-aware | Build/update search index |

**Step 1 Analysis:**
- Glob: path-pattern matching — can be served purely from metadata (prefix scan).
- Grep: content search — needs file content access + text matching.
- Semantic search: vector similarity — needs embedding index + vector store.
- Search index: write-side maintenance of search data structures.

Glob/Grep share properties (frequent, low-latency, read-only, subtree-scope). Semantic search differs (needs vector index, occasional, normal latency). Index management is write-side, rare.

**Decision: ONE domain — Content Discovery.** Despite implementation differences (metadata scan vs content grep vs vector), the user scenario is the same: "find files matching X." The Ops ABC should unify the interface. Implementation strategy (prefix scan vs parallel grep vs vector similarity) is hidden behind the ABC.

### 1.4 Version Control

| Scenario | Caller | Freq | Latency | Mutation | Scope | Auth | State | Fed | Why Exists |
|----------|--------|------|---------|----------|-------|------|-------|-----|------------|
| **Version History** | User | Occasional | Normal | Read | Path | Permission | Persistent | Zone-aware | View change history of a file |
| **Version Rollback** | User | Rare | Normal | Write | Path | Permission | Persistent | Zone-aware | Restore previous version |
| **Version Diff** | User | Occasional | Normal | Read | Path | Permission | Persistent | Zone-aware | Compare two versions |

**Step 1 Analysis:** All three share the same profile: user-initiated, occasional, path-scoped, requires version history data. This is a distinct domain from File I/O — file I/O operates on current state, versioning operates on historical state.

**Decision: Keep as separate domain — Version Control.**

### 1.5 Workspace Management

| Scenario | Caller | Freq | Latency | Mutation | Scope | Auth | State | Fed | Why Exists |
|----------|--------|------|---------|----------|-------|------|-------|-----|------------|
| **Register Workspace** | User | Rare | Normal | Write | Subtree | Identity | Persistent | Zone-aware | Declare a subtree as a tracked workspace |
| **Snapshot** | User/Agent | Occasional | Best-effort | Write | Subtree | Permission | Persistent | Zone-aware | Point-in-time capture (zero-copy via CAS) |
| **Restore** | User | Rare | Best-effort | Write | Subtree | Permission | Persistent | Zone-aware | Restore workspace to snapshot |
| **Workspace Diff** | User | Occasional | Normal | Read | Subtree | Permission | Persistent | Zone-aware | Compare current state vs snapshot |

**Step 1 Analysis:** Properties closely resemble Version Control (§1.4) — same mutation/read patterns, same scope, same persistence. But workspace operates on subtrees while versioning operates on individual paths.

**Question: Merge Workspace + Version Control?**
- Version Control: per-file history (git-like `log`, `diff`, `checkout`)
- Workspace: per-subtree snapshot (git-like `stash`, `branch`, `tag`)
- Both need historical state tracking with CAS deduplication

**Decision: MERGE into one domain — History & Snapshots.** The underlying primitive is the same: point-in-time state capture and comparison. Per-file vs per-subtree is a scope parameter, not a different scenario.

### 1.6 Agent Memory

| Scenario | Caller | Freq | Latency | Mutation | Scope | Auth | State | Fed | Why Exists |
|----------|--------|------|---------|----------|-------|------|-------|-----|------------|
| **Register Memory** | Agent | Rare | Normal | Write | Subtree | Identity | Persistent | Zone-aware | Declare a subtree as agent memory |
| **Append** | Agent | Frequent | Low | Write | Path | Identity | Persistent | Zone-aware | Add memory entry (observation, learning) |
| **Edit** | Agent | Occasional | Normal | Write | Path | Identity | Persistent | Zone-aware | Update existing memory entry |
| **Recall** | Agent | Frequent | Low | Read | Subtree | Identity | Persistent | Zone-aware | Search/retrieve relevant memories |

**Step 1 Analysis:** Memory registration is like workspace registration. Append/edit are file write operations. Recall is a search operation.

**Question: Is Agent Memory a distinct scenario or is it File I/O + Search?**

Code audit finding: **30+ dedicated operations, own service layer**, not just thin wrappers.
- `services/memory/` — 5 modules: `memory_api.py`, `memory_router.py`, `memory_with_paging.py`, `evolution_detector.py`, `memory_paging/` (pager, recall_store, archival_store)
- 15 API routes: CRUD + invalidate/revalidate + search/query + batch + history/versions/rollback/diff/lineage
- Own storage models: `storage/models/memory.py`
- Own CLI: `cli/commands/memory.py`
- Own remote domain: `remote/domain/memory.py`
- Unique capabilities: memory invalidation lifecycle, multi-version lineage, evolution detection, memory paging (hot/warm/cold archival)

Memory operations have distinct properties from File I/O:
- **Invalidation/revalidation** — files don't have validity lifecycle
- **Lineage tracking** — provenance graph across memory entries
- **Evolution detection** — detects concept drift in memory over time
- **Paging (archival)** — hot→warm→cold tiering of memories

**Decision: KEEP as separate domain — Agent Memory.** Not reducible to File I/O — it has its own state machines (valid→invalid→revalidated), lineage graphs, and archival lifecycle. Closer to a key-value store with temporal semantics than a filesystem.

### 1.7 Mount Management

| Scenario | Caller | Freq | Latency | Mutation | Scope | Auth | State | Fed | Why Exists |
|----------|--------|------|---------|----------|-------|------|-------|-----|------------|
| **Add Mount** | User/System | Rare | Normal | Write | System | Identity | Persistent | Cross-zone | Attach storage backend at virtual path |
| **Remove Mount** | User/System | Rare | Normal | Write | System | Identity | Persistent | Cross-zone | Detach storage backend |
| **List Mounts** | Any | Occasional | Low | Read | System | Identity | Stateless | Zone-aware | Enumerate active mounts |
| **Get Mount** | Any | Occasional | Low | Read | Path | Identity | Stateless | Zone-aware | Get mount config for a path |
| **Sync Mount** | User/System | Rare | Best-effort | Write | Subtree | Identity | Persistent | Cross-zone | Synchronize remote → local metadata |
| **Save/Load Config** | User | Rare | Normal | Write | System | Identity | Persistent | Zone-aware | Persist mount configuration for reboot |
| **Connector Delete** | User | Rare | Normal | Write | System | Identity | Persistent | Cross-zone | Full cleanup: unmount + revoke OAuth + delete config |

**Step 1 Analysis:** All mount scenarios share: rare frequency, system/cross-zone scope, identity auth, persistent state. Sync is an outlier (best-effort latency, subtree scope) but still mount-domain.

**Decision: Keep as one domain — Mount Management.** Note: current `MountProtocol` has 16 methods across 4 sub-domains. We may want to split in Step 2, but the scenario properties are cohesive.

### 1.8 Content Sharing

| Scenario | Caller | Freq | Latency | Mutation | Scope | Auth | State | Fed | Why Exists |
|----------|--------|------|---------|----------|-------|------|-------|-----|------------|
| **Create Share Link** | User | Occasional | Normal | Write | Path | Permission | Persistent | Zone-aware | Generate capability URL for file/dir |
| **Access Share Link** | Any | Frequent | Low | Read | Path | Capability | Session | Zone-aware | Resolve capability URL → content |
| **Revoke Share Link** | User | Rare | Normal | Write | Path | Permission | Persistent | Zone-aware | Invalidate capability URL |
| **List Share Links** | User | Occasional | Normal | Read | Zone | Permission | Stateless | Zone-aware | Browse active share links |
| **Access Logs** | User | Rare | Normal | Read | Path | Permission | Persistent | Zone-aware | Audit who accessed a share |

**Step 1 Analysis:** Distinct auth model (Capability URLs vs Permission), distinct scope pattern (the link itself is a separate entity from the file). This is NOT just "file read with a different auth" — it introduces new state (link records, access logs, expiry).

**Decision: Keep as separate domain — Content Sharing.**

### 1.9 OAuth & Credential Management

| Scenario | Caller | Freq | Latency | Mutation | Scope | Auth | State | Fed | Why Exists |
|----------|--------|------|---------|----------|-------|------|-------|-----|------------|
| **Get Auth URL** | User | Rare | Normal | Read | System | Identity | Session | Local | Start OAuth flow (PKCE) |
| **Exchange Code** | User | Rare | Normal | Write | System | Identity | Persistent | Local | Complete OAuth flow → store tokens |
| **List Providers** | User | Rare | Normal | Read | System | None | Stateless | Local | Discovery — what OAuth providers exist |
| **List Credentials** | User | Occasional | Normal | Read | System | Identity | Persistent | Local | View stored OAuth tokens |
| **Revoke Credential** | User | Rare | Normal | Write | System | Identity | Persistent | Local | Delete stored OAuth token |
| **Test Credential** | User | Rare | Normal | Read | System | Identity | Persistent | Local | Verify token still valid |
| **MCP Connect** | User | Rare | Normal | Write | System | Identity | Session | Local | Connect to MCP provider via Klavis |

**Step 1 Analysis:** Unique properties: exclusively user-initiated, rare frequency, system scope, local-only (no federation), session+persistent state. This is fundamentally about managing external service credentials — orthogonal to file operations.

**Decision: Keep as separate domain — Credential Management.**

### 1.10 Permission (ReBAC)

| Scenario | Caller | Freq | Latency | Mutation | Scope | Auth | State | Fed | Why Exists |
|----------|--------|------|---------|----------|-------|------|-------|-----|------------|
| **Check** | System | Hot-path | Ultra-low | Read | Path | None (IS the auth) | Stateless | Zone-aware | Authorization decision: can subject do X on object? |
| **Check Bulk** | System | Frequent | Low | Read | Zone | None | Stateless | Zone-aware | Batch permission checks |
| **Write Tuple** | User/System | Occasional | Normal | Write | Zone | Permission | Persistent | Zone-aware | Grant: create subject→relation→object |
| **Delete Tuple** | User/System | Rare | Normal | Write | Zone | Permission | Persistent | Zone-aware | Revoke: delete specific tuple |
| **Expand** | System | Occasional | Normal | Read | Zone | None | Stateless | Zone-aware | Find all subjects with permission on object |
| **List Objects** | System | Occasional | Normal | Read | Zone | None | Stateless | Zone-aware | Find all objects subject can access |

**Step 1 Analysis:** Permission checks are hot-path (every file operation triggers a check). Write/delete are administrative. The unique property is that this domain IS the authorization system — it doesn't need auth itself for checks (it provides auth to others). Zanzibar-style graph traversal is fundamentally different from file operations.

**Decision: Keep as separate domain — Permission (ReBAC).**

### 1.11 File Watching

| Scenario | Caller | Freq | Latency | Mutation | Scope | Auth | State | Fed | Why Exists |
|----------|--------|------|---------|----------|-------|------|-------|-----|------------|
| **Watch (long-poll)** | Agent/User | Frequent | Normal | Read | Path/Subtree | Permission | Session | Zone-aware | Observe filesystem changes in real-time |

**Step 1 Analysis:** Currently bundled in `EventsProtocol` with advisory locking. But watching and locking have fundamentally different properties:
- Watching: read-only, session-scoped, pub/sub pattern
- Locking: write (acquire/release), session-scoped, mutual exclusion pattern

**Decision: SPLIT from Advisory Locking (§1.12).**

### 1.12 Advisory Locking

| Scenario | Caller | Freq | Latency | Mutation | Scope | Auth | State | Fed | Why Exists |
|----------|--------|------|---------|----------|-------|------|-------|-----|------------|
| **Lock** | Agent/User | Occasional | Low | Write | Path | Permission | Session | Zone-aware | Acquire advisory lock on path |
| **Extend Lock** | Agent/User | Frequent | Low | Write | Path | Permission | Session | Zone-aware | Renew TTL on held lock |
| **Unlock** | Agent/User | Occasional | Low | Write | Path | Permission | Session | Zone-aware | Release advisory lock |

**Step 1 Analysis:** Distinct from file watching — mutation pattern (write), mutual exclusion semantics, TTL-based lifecycle. In Linux, `flock()` / `fcntl()` locking is a separate subsystem from `inotify`/`fanotify`.

**Decision: Keep as separate domain — Advisory Locking.**

### 1.13 Agent Lifecycle

| Scenario | Caller | Freq | Latency | Mutation | Scope | Auth | State | Fed | Why Exists |
|----------|--------|------|---------|----------|-------|------|-------|-----|------------|
| **Register** | Agent | Rare | Normal | Write | Zone | Identity | Persistent | Zone-aware | Create agent identity |
| **Get** | System | Frequent | Low | Read | Zone | None | Stateless | Zone-aware | Lookup agent by ID |
| **Heartbeat** | Agent | Frequent | Low | Write | Zone | Identity | Session | Zone-aware | Keep agent alive (TTL renewal) |
| **Transition** | Agent/System | Occasional | Normal | Write | Zone | Identity | Persistent | Zone-aware | State machine: IDLE→BUSY→IDLE |
| **List by Zone** | System | Occasional | Normal | Read | Zone | None | Stateless | Zone-aware | Enumerate agents in a zone |
| **Unregister** | Agent/System | Rare | Normal | Write | Zone | Identity | Persistent | Zone-aware | Remove agent identity |

**Step 1 Analysis:** Distinct domain — agent identity and lifecycle management. Not related to file operations. The heartbeat pattern (frequent, low-latency write) is unique.

**Decision: Keep as separate domain — Agent Lifecycle.**

### 1.14 Agent Scheduling

| Scenario | Caller | Freq | Latency | Mutation | Scope | Auth | State | Fed | Why Exists |
|----------|--------|------|---------|----------|-------|------|-------|-----|------------|
| **Submit** | User/System | Occasional | Normal | Write | Zone | Identity | Session | Zone-aware | Queue work request for agent |
| **Next** | Agent | Frequent | Low | Write | Zone | Identity | Session | Zone-aware | Dequeue next work item (priority) |
| **Pending Count** | System | Occasional | Normal | Read | Zone | None | Stateless | Zone-aware | Queue depth monitoring |
| **Cancel** | User/System | Rare | Normal | Write | Zone | Identity | Session | Zone-aware | Cancel queued work |

**Step 1 Analysis:** Queue semantics (submit/dequeue/cancel). Ephemeral state (no archive). Similar to Agent Lifecycle (§1.13) in scope/auth/federation.

**Question: Merge with Agent Lifecycle?**
- Agent Lifecycle: identity + state machine (persistent)
- Scheduling: work queue (ephemeral, session-scoped)
- Different statefulness: persistent vs session

**Decision: Keep separate — Agent Scheduling.** Different state model (ephemeral queue vs persistent identity).

### 1.15 Skill Management

| Scenario | Caller | Freq | Latency | Mutation | Scope | Auth | State | Fed | Why Exists |
|----------|--------|------|---------|----------|-------|------|-------|-----|------------|
| **Share** | User | Rare | Normal | Write | Zone | Permission | Persistent | Zone-aware | Distribute skill to user/group |
| **Unshare** | User | Rare | Normal | Write | Zone | Permission | Persistent | Zone-aware | Revoke skill distribution |
| **Discover** | User/Agent | Occasional | Normal | Read | Zone | Permission | Stateless | Zone-aware | Browse available skills |
| **Subscribe** | User/Agent | Rare | Normal | Write | Zone | Permission | Persistent | Zone-aware | Opt-in to a shared skill |
| **Unsubscribe** | User/Agent | Rare | Normal | Write | Zone | Permission | Persistent | Zone-aware | Opt-out from skill |
| **Load/Run** | Agent | Frequent | Low | Read | Path | Permission | Session | Zone-aware | Load skill content for execution |
| **Export/Import** | User | Rare | Best-effort | Mixed | Path | Permission | Persistent | Local | Package skill as .zip |

**Step 1 Analysis:** Skill management is a distribution + subscription + package system. It has its own state (subscription records, share grants). Load/Run is frequent and low-latency (agent execution).

**Question: Is this just "file operations on skill files"?**
- Share/unshare: ReBAC tuple operations (not file ops)
- Subscribe: user preference record (not file ops)
- Load: file read (IS file ops)
- Export/import: file packaging (IS file ops)

**Decision: Keep as separate domain — Skill Management.** The distribution/subscription lifecycle is unique. Load is a file read, but discovering and subscribing is not.

### 1.16 LLM Document Reading

| Scenario | Caller | Freq | Latency | Mutation | Scope | Auth | State | Fed | Why Exists |
|----------|--------|------|---------|----------|-------|------|-------|-----|------------|
| **LLM Read** | User/Agent | Occasional | Normal | Read | Path | Permission | Stateless | Zone-aware | AI-powered document understanding |
| **LLM Stream** | User/Agent | Occasional | Normal | Read | Path | Permission | Session | Zone-aware | Streaming AI response |
| **Create Reader** | User/Agent | Rare | Normal | Read | System | Identity | Session | Local | Configure LLM reader instance |

**Step 1 Analysis:** LLM reading is "file read + AI inference." The unique property is external API dependency (LLM provider) and streaming response pattern.

**Question: Is this a distinct scenario or just "file read with a post-processor"?**
- It uses file content (File I/O dependency)
- It uses search results (Content Discovery dependency)
- It adds AI inference (unique capability)

**Decision: Keep as separate domain — LLM Reading.** The external API dependency and streaming pattern are distinct properties that don't fit in File I/O or Content Discovery.

### 1.17 Sandbox Execution

| Scenario | Caller | Freq | Latency | Mutation | Scope | Auth | State | Fed | Why Exists |
|----------|--------|------|---------|----------|-------|------|-------|-----|------------|
| **Create** | Agent | Occasional | Normal | Write | System | Permission | Persistent | Local | Create execution environment (Docker/E2B) |
| **Run** | Agent | Frequent | Normal | Write | Path | Permission | Session | Local | Execute code in sandbox |
| **Lifecycle** | Agent | Occasional | Normal | Write | System | Permission | Session | Local | Pause, resume, stop sandbox |
| **Connect/Disconnect** | Agent | Occasional | Normal | Write | System | Identity | Session | Local | Attach/detach from sandbox |

**Step 1 Analysis:** Unique domain — external compute environment management. Local-only (sandboxes run on the same machine or via E2B API). No federation impact.

**Decision: Keep as separate domain — Sandbox Execution.**

### 1.18 Lifecycle Hooks

| Scenario | Caller | Freq | Latency | Mutation | Scope | Auth | State | Fed | Why Exists |
|----------|--------|------|---------|----------|-------|------|-------|-----|------------|
| **Register Hook** | System/Plugin | Rare | Normal | Write | System | None | Session | Local | Register callback for pre/post operation |
| **Fire Hooks** | System | Hot-path | Ultra-low | Mixed | Path | None | Session | Local | Execute registered hooks for an operation |
| **Unregister Hook** | System/Plugin | Rare | Normal | Write | System | None | Session | Local | Remove a registered hook |

**Step 1 Analysis:** Infrastructure scenario — not user-initiated. Hot-path on fire (called for every file operation). Ephemeral state (hooks registered in memory).

**Decision: Keep as separate domain — Lifecycle Hooks.**

### 1.19 Namespace Visibility

| Scenario | Caller | Freq | Latency | Mutation | Scope | Auth | State | Fed | Why Exists |
|----------|--------|------|---------|----------|-------|------|-------|-----|------------|
| **Is Visible** | System | Hot-path | Ultra-low | Read | Path | None | Stateless | Zone-aware | Can this subject see this path? |
| **Get Mount Table** | System | Occasional | Normal | Read | Zone | None | Stateless | Zone-aware | Get subject's visible mount points |
| **Invalidate** | System | Occasional | Normal | Write | Zone | None | Session | Zone-aware | Clear cached visibility data |

**Step 1 Analysis:** Infrastructure scenario — called by the system for every path operation. Hot-path on visibility check. Derived from ReBAC data.

**Question: Merge with Permission (§1.10)?**
- Namespace visibility is derived from ReBAC grants
- But it's a different question: "can subject see path?" vs "does subject have permission X on object?"
- Visibility is binary and path-scoped; permissions are relation-specific and may involve graph traversal

**Decision: Keep separate — Namespace Visibility.** While data-derived from ReBAC, the operational properties (hot-path visibility check vs occasional permission evaluation) are distinct. In Linux, this is like dcache (directory entry cache) vs inode permissions — related but separate subsystems.

### 1.20 Event Log (Audit)

| Scenario | Caller | Freq | Latency | Mutation | Scope | Auth | State | Fed | Why Exists |
|----------|--------|------|---------|----------|-------|------|-------|-----|------------|
| **Append** | System | Frequent | Low | Write | Zone | None | Persistent | Zone-aware | Record operation for audit trail |
| **Read** | User/System | Occasional | Normal | Read | Zone | Permission | Persistent | Zone-aware | Query audit history |

**Step 1 Analysis:** Append-only log with monotonic sequence numbers. Write-heavy (every operation generates events). Distinct from file watching (real-time notifications) and from event bus (ephemeral pub/sub).

**Decision: Keep as separate domain — Event Log (Audit).**

### 1.21 Context Manifest

| Scenario | Caller | Freq | Latency | Mutation | Scope | Auth | State | Fed | Why Exists |
|----------|--------|------|---------|----------|-------|------|-------|-----|------------|
| **Resolve** | Agent | Occasional | Best-effort | Read | System | Identity | Session | Local | Pre-execute all context sources before agent reasoning |

**Step 1 Analysis:** Single method, agent-initiated, pre-execution phase. Orchestrates multiple file reads + search + API calls in parallel.

**Question: Is this a distinct scenario or orchestration?**
- It's a composite: reads files, runs searches, calls APIs
- The "resolve" is orchestration, not a new primitive

**Decision: ELIMINATE as separate domain.** Context manifest resolution is orchestration of existing primitives (File I/O + Content Discovery). It doesn't define new capabilities — it composes existing ones. The Protocol can remain as a convenience interface but is not an Ops ABC in the architectural sense.

### 1.22 Payment

| Scenario | Caller | Freq | Latency | Mutation | Scope | Auth | State | Fed | Why Exists |
|----------|--------|------|---------|----------|-------|------|-------|-----|------------|
| **Transfer** | System | Occasional | Normal | Write | Zone | Identity | Persistent | Zone-aware | Move credits between accounts |
| **Spending Policy** | User | Rare | Normal | Write | Zone | Identity | Persistent | Zone-aware | Set/check agent spending limits |

**Step 1 Analysis:** Financial operations. Persistent state. Distinct from all other scenarios.

**Decision: Keep as separate domain — Payment.**

### 1.23 Anomaly Detection

| Scenario | Caller | Freq | Latency | Mutation | Scope | Auth | State | Fed | Why Exists |
|----------|--------|------|---------|----------|-------|------|-------|-----|------------|
| **Detect** | System | Frequent | Normal | Read | Zone | None | Stateless | Zone-aware | Check for unusual spending/behavior patterns |

**Step 1 Analysis:** Single-method statistical analysis. System-initiated. Read-only.

**Code audit finding:** Anomaly detection is part of a larger Governance subsystem (`services/governance/`) with 10+ modules: `anomaly_service.py`, `anomaly_math.py`, `collusion_service.py`, `trust_math.py`, `governance_graph_service.py`, `response_service.py`, `approval/workflow.py`, `governance_wrapper.py`, plus 13 API routes covering alerts, fraud scores, collusion rings, constraints, suspensions, and appeals. This is NOT just spending anomaly detection — it's a full security governance system.

**Decision: DO NOT MERGE into Payment.** Instead, see §1.29 (Governance) and §1.30 (Reputation) — the PM decision is to keep security governance and agent economy reputation as separate domains.

### 1.24 VFS Routing

| Scenario | Caller | Freq | Latency | Mutation | Scope | Auth | State | Fed | Why Exists |
|----------|--------|------|---------|----------|-------|------|-------|-----|------------|
| **Route** | System | Hot-path | Ultra-low | Read | Path | None | Stateless | Zone-aware | Resolve virtual path → backend + physical path |
| ~~Add/Remove Mount~~ | ~~System~~ | ~~Rare~~ | ~~Normal~~ | ~~Write~~ | ~~System~~ | ~~None~~ | ~~Persistent~~ | ~~Zone-aware~~ | → Moved to Mount Management §1.7 (P10 MountProtocol) |
| ~~List Mounts~~ | ~~System~~ | ~~Occasional~~ | ~~Low~~ | ~~Read~~ | ~~System~~ | ~~None~~ | ~~Stateless~~ | ~~Zone-aware~~ | → Moved to Mount Management §1.7 (P10 MountProtocol) |

**Step 1 Analysis:** Internal kernel primitive — path resolution. System-initiated (user mounts go through Mount Management §1.7, which delegates to VFS Router). Hot-path routing on every file operation.

In Linux, `vfsmount` (kernel) has two layers: `lookup_slow()` is the hot-path resolution (kernel, called on every syscall), while `mount(2)`/`umount(2)` are rare administrative ops handled by userspace tools. Only `lookup_slow()` must be in the kernel — mount table CRUD can live in userspace.

**Decision: Split VFS Routing.** Only `route()` is kernel-native (hot-path, every operation). `add_mount()`/`remove_mount()`/`list_mounts()` move to P10 MountProtocol (Service tier) — they're admin ops, not data-plane primitives. VFSRouterProtocol shrinks from 4 methods to 1.

### 1.25 Storage Connector

| Scenario | Caller | Freq | Latency | Mutation | Scope | Auth | State | Fed | Why Exists |
|----------|--------|------|---------|----------|-------|------|-------|-----|------------|
| **Read Content** | System | Frequent | Low | Read | Path | None | Stateless | Local | Read blob from storage backend |
| **Write Content** | System | Frequent | Low | Write | Path | None | Stateless | Local | Write blob to storage backend |
| **Connect/Disconnect** | System | Rare | Normal | Write | System | None | Session | Local | Backend connection lifecycle |

**Step 1 Analysis:** Low-level storage I/O interface. System-only (users don't call connector directly — they go through VFS Router + File I/O). Local scope (each connector talks to one backend).

**Decision: Keep as separate domain — Storage Connector.** This is the block device driver interface in Linux terms.

### 1.26 ACE (Agentic Continuous Evaluation)

**Code surface:** `services/ace/` (9 modules: `trajectory.py`, `playbook.py`, `reflection.py`, `curation.py`, `feedback.py`, `learning_loop.py`, `consolidation.py`, `affinity.py`, `memory_hierarchy.py`); API routers: `trajectories.py` (5 routes), `playbooks.py`, `feedback.py` (5 routes), `reflect.py`, `curate.py`; NexusFS `@rpc_expose`: `trajectory_start`, `trajectory_log_step`, `trajectory_complete`, `trajectory_add_feedback`, `trajectory_get_feedback`, `trajectory_get_score`, `trajectory_mark_relearn`, `playbook_create`, `playbook_get`, `playbook_query`; models: `storage/models/ace.py`, `server/api/v2/models/trajectories.py`, `reflection.py`, `playbooks.py`, `feedback.py`; remote: `remote/domain/ace.py`

| Scenario | Caller | Freq | Latency | Mutation | Scope | Auth | State | Fed | Why Exists |
|----------|--------|------|---------|----------|-------|------|-------|-----|------------|
| **Start Trajectory** | Agent | Frequent | Low | Write | Zone | Identity | Persistent | Zone-aware | Begin execution trace for evaluation |
| **Log Step** | Agent | Hot-path | Ultra-low | Write | Zone | Identity | Persistent | Zone-aware | Record individual action within trajectory |
| **Complete Trajectory** | Agent | Frequent | Low | Write | Zone | Identity | Persistent | Zone-aware | Finalize trajectory with outcome |
| **Add Feedback** | User/Agent | Occasional | Normal | Write | Zone | Identity | Persistent | Zone-aware | Score/annotate trajectory quality |
| **Create Playbook** | User/Agent | Rare | Normal | Write | Zone | Permission | Persistent | Zone-aware | Codify a proven trajectory as reusable template |
| **Reflect** | Agent | Occasional | Best-effort | Write | Zone | Identity | Persistent | Zone-aware | Self-evaluation across trajectory history |
| **Curate** | User | Rare | Normal | Write | Zone | Permission | Persistent | Zone-aware | Human-in-the-loop quality control of trajectories |

**Step 1 Analysis:** ACE is fundamentally different from Agent Memory (§1.6):
- Memory = persistent knowledge store (what the agent knows)
- ACE = execution trace + evaluation (how the agent performed)
- Different data models: memories are KV entries; trajectories are step-ordered sequences with scores
- Different lifecycle: memories persist indefinitely with invalidation; trajectories are evaluated, fedback, then potentially consolidated into playbooks
- No cross-imports between `services/memory/` and `services/ace/`

**Decision: Keep as separate domain — ACE.**

### 1.27 Agent Delegation

**Code surface:** `services/delegation/` (5 modules: `service.py`, `models.py`, `errors.py`, `derivation.py`, `__init__.py`); API router: `delegation.py` (3 routes: POST delegate, DELETE revoke, GET list); models: `DelegateRequest`, `DelegateResponse`, `DelegationRecord`, `DelegationListResponse`; CLI: `cli/commands/agent.py` (delegate subcommands)

| Scenario | Caller | Freq | Latency | Mutation | Scope | Auth | State | Fed | Why Exists |
|----------|--------|------|---------|----------|-------|------|-------|-----|------------|
| **Delegate** | Agent | Occasional | Normal | Write | Zone | Permission | Persistent | Zone-aware | Parent agent grants narrowed capabilities to child agent |
| **Revoke** | Agent | Rare | Normal | Write | Zone | Permission | Persistent | Zone-aware | Parent agent revokes delegation |
| **List Delegations** | Agent | Occasional | Normal | Read | Zone | Identity | Stateless | Zone-aware | View active delegations |

**Step 1 Analysis:** Distinct from Agent Lifecycle (§1.13):
- Agent Lifecycle: identity + state machine (register, heartbeat, transition)
- Agent Delegation: capability narrowing via namespace derivation + ReBAC tuple injection
- Delegation creates child agent with narrowed permissions (coordinator→worker pattern)
- Uses `derivation.py` for namespace path narrowing, not lifecycle management

**Decision: Keep as separate domain — Agent Delegation.** It's the A2A coordination model (peer-to-peer with narrowed permissions), complementary to lifecycle management.

### 1.28 Operations Undo

**Code surface:** `storage/operation_logger.py` (`OperationLogger`: `log_operation`, `list_operations`, `list_operations_cursor`); `storage/models/operation_log.py` (`OperationLogModel`); API router: `server/api/v2/routers/operations.py` (2 routes: GET list, GET agent activity); CLI: `cli/commands/operations.py`

| Scenario | Caller | Freq | Latency | Mutation | Scope | Auth | State | Fed | Why Exists |
|----------|--------|------|---------|----------|-------|------|-------|-----|------------|
| **Log Operation** | System | Frequent | Low | Write | Zone | None | Persistent | Zone-aware | Record reversible operation for undo |
| **List Operations** | User/Agent | Occasional | Normal | Read | Zone | Identity | Stateless | Zone-aware | View operation history with cursor pagination |
| **Undo Operation** | User | Rare | Normal | Write | Zone | Permission | Persistent | Zone-aware | Reverse a logged operation |

**Step 1 Analysis:** Distinct from Event Log (§1.20) and History & Snapshots (§1.4):
- Event Log: immutable audit trail (append-only, forensic)
- History & Snapshots: file-level versioning (content-centric)
- Operations Undo: reversible command log (operation-centric, supports undo)
- Different data model: `OperationLogModel` stores operation type, target path, reverse data

**Decision: Keep as separate domain — Operations Undo.** Independent `OperationLogger` system with its own storage model.

### 1.29 Governance (Security)

**Code surface:** `services/governance/` (10+ modules: `anomaly_service.py`, `anomaly_math.py`, `collusion_service.py`, `trust_math.py`, `governance_graph_service.py`, `response_service.py`, `governance_wrapper.py`, `approval/workflow.py`, `protocols.py`, `models.py`, `db_models.py`); API router: `server/api/v2/routers/governance.py` (13 routes: alerts, fraud scores, collusion rings, constraints, suspension, appeals); `skills/governance.py`

| Scenario | Caller | Freq | Latency | Mutation | Scope | Auth | State | Fed | Why Exists |
|----------|--------|------|---------|----------|-------|------|-------|-----|------------|
| **Anomaly Detection** | System | Frequent | Normal | Read | Zone | None | Stateless | Zone-aware | Statistical detection of unusual agent behavior |
| **Collusion Detection** | System | Occasional | Best-effort | Read | Zone | None | Persistent | Zone-aware | Graph analysis for coordinated fraud rings |
| **Trust Scoring** | System | Frequent | Low | Read | Zone | None | Persistent | Zone-aware | Compute trust score for agent interactions |
| **Suspension/Appeal** | User/System | Rare | Normal | Write | Zone | Permission | Persistent | Zone-aware | Suspend bad actors, handle appeals |
| **Constraint Graph** | User | Rare | Normal | Write | Zone | Permission | Persistent | Zone-aware | Define interaction constraints between agents |
| **Approval Workflow** | User | Occasional | Normal | Write | Zone | Permission | Persistent | Zone-aware | Multi-step governance approval for risky actions |

**Step 1 Analysis:** Security-focused governance — distinct from both Payment (§1.22) and Reputation (§1.30):
- Payment: financial transfers and spending limits
- Governance: security enforcement (anomaly detection, collusion prevention, suspensions)
- Reputation: agent economy quality scores (see §1.30)

**Decision: Keep as separate domain — Governance (Security).** PM decision: security governance ≠ agent economy reputation.

### 1.30 Reputation (Agent Economy)

**Code surface:** `services/reputation/` (4 modules: `reputation_service.py`, `reputation_records.py`, `reputation_math.py`, `dispute_service.py`); API router: `server/api/v2/routers/reputation.py` (7 routes: agent reputation, leaderboard, exchange feedback, disputes); models: `storage/models/reputation_score.py`, `storage/models/reputation_event.py`

| Scenario | Caller | Freq | Latency | Mutation | Scope | Auth | State | Fed | Why Exists |
|----------|--------|------|---------|----------|-------|------|-------|-----|------------|
| **Get Reputation** | Any | Frequent | Low | Read | Zone | None | Stateless | Zone-aware | Look up agent's reputation score |
| **Leaderboard** | User | Occasional | Normal | Read | Zone | None | Stateless | Zone-aware | Rank agents by reputation |
| **Exchange Feedback** | User/Agent | Occasional | Normal | Write | Zone | Identity | Persistent | Zone-aware | Rate agent after task exchange |
| **Dispute** | User/Agent | Rare | Normal | Write | Zone | Identity | Persistent | Zone-aware | Challenge unfair feedback |
| **Resolve Dispute** | User | Rare | Normal | Write | Zone | Permission | Persistent | Zone-aware | Adjudicate dispute outcome |

**Step 1 Analysis:** Agent economy domain — complements Governance but serves a different purpose:
- Governance: punitive (detect bad actors, suspend them)
- Reputation: evaluative (score agent quality, enable marketplace trust)
- Different math: governance uses anomaly statistics; reputation uses ELO-style scoring

**Decision: Keep as separate domain — Reputation (Agent Economy).** PM decision: separate from governance.

### 1.31 Plugins

**Code surface:** `plugins/` (7 modules: `base.py`, `registry.py`, `hooks.py`, `async_hooks.py`, `scaffold.py`, `cli.py`, `__init__.py`); CLI: `cli/commands/plugins.py` (list, install, uninstall, scaffold)

| Scenario | Caller | Freq | Latency | Mutation | Scope | Auth | State | Fed | Why Exists |
|----------|--------|------|---------|----------|-------|------|-------|-----|------------|
| **Install Plugin** | User | Rare | Best-effort | Write | System | Identity | Persistent | Local | pip install + register entry point |
| **Uninstall Plugin** | User | Rare | Normal | Write | System | Identity | Persistent | Local | pip uninstall + deregister |
| **List Plugins** | User | Occasional | Low | Read | System | None | Stateless | Local | Enumerate installed plugins |
| **Scaffold Plugin** | User | Rare | Normal | Write | System | None | Session | Local | Generate plugin template |
| **Register Hook** | Plugin | Rare | Normal | Write | System | None | Session | Local | Plugin registers pre/post operation hooks |

**Step 1 Analysis:** Package management (pip entry-point packages). Local-only, no federation. Plugin hooks overlap with Lifecycle Hooks (§1.18), but the scenario here is the distribution lifecycle (install/uninstall/scaffold), not the hook execution itself.

**Decision: Keep as separate domain — Plugins.** Package management lifecycle is distinct from hook execution.

### 1.32 Workflows

**Code surface:** `workflows/` (8 modules: `engine.py`, `api.py`, `loader.py`, `storage.py`, `types.py`, `triggers.py`, `actions.py`, `__init__.py`); CLI: `cli/commands/workflows.py`; models: `storage/models/workflows.py`

| Scenario | Caller | Freq | Latency | Mutation | Scope | Auth | State | Fed | Why Exists |
|----------|--------|------|---------|----------|-------|------|-------|-----|------------|
| **Load Workflow** | System | Rare | Normal | Write | System | None | Persistent | Local | Parse YAML definition into engine |
| **Execute Workflow** | User/Agent | Occasional | Best-effort | Write | Zone | Permission | Session | Zone-aware | Run multi-step automation pipeline |
| **List Workflows** | User | Occasional | Normal | Read | System | None | Stateless | Local | Enumerate available workflows |
| **Trigger Workflow** | System | Frequent | Low | Write | Zone | None | Session | Zone-aware | Auto-trigger workflow on event/schedule |

**Step 1 Analysis:** YAML-based automation pipelines (Dify/n8n-style). Distinct from Plugins (§1.31):
- Plugins: pip packages that extend system capabilities (install/uninstall)
- Workflows: YAML definitions that orchestrate actions (load/trigger/execute)
- Different lifecycle: plugins are installed persistently; workflows are loaded from YAML and triggered by events

**Decision: Keep as separate domain — Workflows.**

---

## STEP 1 SUMMARY: Scenario Dedup Results

### Eliminated (merged into other domains)
| Original | Merged Into | Reason |
|----------|------------|--------|
| Directory Operations (§1.2) | File I/O (§1.1) | Same properties, same objects (entry_type=DT_DIR) |
| Workspace Management (§1.5) | History & Snapshots (§1.4→renamed) | Same primitive: point-in-time state capture |
| Context Manifest (§1.21) | (orchestration of existing primitives) | Composes File I/O + Search, no new primitive |

### Restored (previously eliminated, reinstated after code audit)
| Domain | Reason for Reinstatement |
|--------|--------------------------|
| Agent Memory (§1.6) | 30+ dedicated ops, own service layer, unique invalidation/lineage/paging lifecycle |

### Split
| Original | Split Into | Reason |
|----------|-----------|--------|
| EventsProtocol scenarios | File Watching (§1.11) + Advisory Locking (§1.12) | Different properties: read/pub-sub vs write/mutex |
| "Anomaly Detection" (§1.23) | Governance (§1.29) + Reputation (§1.30) | PM decision: security ≠ agent economy |

### Surviving Scenario Domains (27)

| # | Domain | Tier (preliminary) | Linux Analogue |
|---|--------|--------------------|----------------|
| S1 | **File I/O** (+ dirs) | Kernel | `inode_operations` + `file_operations` |
| S2 | **Content Discovery** (glob/grep/semantic) | Service | `readdir` + custom search |
| S3 | **History & Snapshots** (versions + workspaces) | Service | `btrfs snapshot` / git |
| S4 | **Mount Management** | Service | `mount(2)` / `systemd-mount` |
| S5 | **Content Sharing** (capability URLs) | Service | (no direct analogue) |
| S6 | **Credential Management** (OAuth) | Service | PAM / keyring |
| S7 | **Permission (ReBAC)** | Service | SELinux / AppArmor |
| S8 | **File Watching** | Service | `inotify` / `fanotify` |
| S9 | **Advisory Locking** | Kernel or Service | `flock(2)` / `fcntl` locking |
| S10 | **Agent Lifecycle** | Service | process management (`fork`, `wait`) |
| S11 | **Agent Scheduling** | Service | `nice(2)` / CFS scheduler |
| S12 | **Skill Management** | Service | package manager (`apt`, `npm`) |
| S13 | **LLM Reading** | Service | (no analogue — AI-native) |
| S14 | **Sandbox Execution** | Service | `cgroups` / `namespaces` |
| S15 | **Lifecycle Hooks** | Kernel or Service | `netfilter` hooks |
| S16 | **Namespace Visibility** | Service | dcache / mount namespace |
| S17 | **Event Log (Audit)** | Service | `auditd` / `syslog` |
| S18 | **Payment** | Service | (no analogue — billing) |
| S19 | **VFS Routing** | Kernel | VFS `lookup_slow()` (route only; mount CRUD → P10 Service) |
| S20 | **Storage Connector** | Kernel (driver) | block device drivers |
| S21 | **Agent Memory** | Service | `/proc` with temporal semantics |
| S22 | **ACE** (trajectories/playbooks/feedback) | Service | (no analogue — AI-native) |
| S23 | **Agent Delegation** | Service | `setuid` / capability delegation |
| S24 | **Operations Undo** | Service | `fsck` journal / undo log |
| S25 | **Governance** (security) | Service | kernel security modules (LSM) |
| S26 | **Reputation** (agent economy) | Service | (no analogue — marketplace) |
| S27 | **Plugins** | Service | kernel modules (`insmod`) |
| S28 | **Workflows** | Service | `systemd` units / cron |

---

## STEP 2: OPS ABC ENUMERATION

**Source of truth:** Code audit of all Protocol/ABC classes:
- 15 service-layer protocols (`services/protocols/`)
- 5 core-layer protocols (`core/protocols/` + `core/metastore.py`)
- 3 IPC protocols (`ipc/protocols.py`)
- 1 governance protocol (`services/governance/protocols.py`)
- 1 event log protocol (`services/event_log/protocol.py`)
- 1 payment ABC (`pay/protocol.py`)
- 1 skills facade (`skills/protocols.py`)

### 2.1 Ops ABC Properties Table

| # | Protocol | File | Tier | Sync | Methods | Coupling | Linux Analogue | Why Exists |
|---|----------|------|------|------|---------|----------|----------------|------------|
| P1 | **MetastoreABC** | `core/metastore.py` | Kernel | Sync | 14 (Large) | Standalone | VFS inode cache | Metadata CRUD for virtual→physical path mapping |
| P2 | **ContentStoreProtocol** | `core/protocols/connector.py` | Kernel | Sync | 6 (Small) | Standalone | block device read/write | Content-addressable blob storage |
| P3 | **DirectoryOpsProtocol** | `core/protocols/connector.py` | Kernel | Sync | 3 (Micro) | Standalone | `inode_operations` mkdir/rmdir | Directory entry management |
| P4 | **ConnectorProtocol** | `core/protocols/connector.py` | Kernel | Sync | 13 (Medium) | Composed (P2+P3) | storage driver | Full storage backend interface (ContentStore + DirectoryOps + lifecycle) |
| P5 | **VFSRouterProtocol** | `core/protocols/vfs_router.py` | Kernel | Async | 1 (Micro) | Standalone | VFS `lookup_slow()` | Route virtual path → backend + physical path. Mount CRUD moved to P10 MountProtocol. |
| P6 | **SearchProtocol** | `services/protocols/search.py` | Service | Mixed | 6 (Small) | Standalone | `readdir` + custom | Unified find interface (glob/grep/semantic) |
| P7 | **SearchBrickProtocol** | `services/protocols/search.py` | Service | Mixed | 5 (Small) | Standalone | search daemon | Per-brick search with indexing lifecycle |
| P8 | **PermissionProtocol** | `services/protocols/permission.py` | Service | Sync | 6 (Small) | Standalone | SELinux / Zanzibar | ReBAC check/write/delete/expand |
| P9a | **WatchProtocol** | `services/protocols/watch.py` | Service | Async | 2 (Micro) | Standalone | `inotify` | File change long-poll (split from EventsProtocol) |
| P9b | **LockProtocol** | `services/protocols/lock.py` | Service | Async | 3 (Micro) | Standalone | `flock` | Advisory lock lifecycle (split from EventsProtocol) |
| P10 | **MountProtocol** | `services/protocols/mount.py` | Service | Async | 15 (Large) | Standalone | `mount(2)` | Mount lifecycle: add/remove/sync/save/load |
| P11 | **ShareLinkProtocol** | `services/protocols/share_link.py` | Service | Async | 6 (Small) | Standalone | capability URLs | Create/revoke/access capability URLs |
| P12 | **OAuthProtocol** | `services/protocols/oauth.py` | Service | Async | 7 (Medium) | Standalone | PAM / keyring | OAuth flow + credential management |
| P13 | **LLMProtocol** | `services/protocols/llm.py` | Service | Mixed | 4 (Small) | Standalone | (AI-native) | AI-powered document reading |
| P14 | **AgentRegistryProtocol** | `services/protocols/agent_registry.py` | Service | Async | 6 (Small) | Standalone | process table | Agent identity + lifecycle |
| P15 | **SchedulerProtocol** | `services/protocols/scheduler.py` | Service | Async | 4 (Small) | Standalone | CFS scheduler | Work queue: submit/next/cancel |
| P16 | **SkillsProtocol** | `services/protocols/skills.py` | Service | Sync | 9 (Medium) | Standalone | `apt` / `npm` | Skill distribution + subscription + package |
| P17 | **HookEngineProtocol** | `services/protocols/hook_engine.py` | Service | Async | 3 (Micro) | Standalone | `netfilter` hooks | Pre/post operation hook registration + firing |
| P18 | **NamespaceManagerProtocol** | `services/protocols/namespace_manager.py` | Service | Async | 3 (Micro) | Standalone | dcache / mount ns | Path visibility check + mount table |
| P19 | **EventLogProtocol** (service) | `services/event_log/protocol.py` | Service | Mixed | 8 (Medium) | Standalone | WAL / `auditd` | Durable append-only event log with WAL |
| P20 | **AnomalyDetectorProtocol** | `services/governance/protocols.py` | Service | Sync | 1 (Micro) | Standalone | LSM hook | Statistical anomaly detection |
| P21 | **PaymentProtocol** | `pay/protocol.py` | Service | Mixed | 3 (Micro) | Standalone | (billing) | Extensible payment dispatch (X402/credits) |
| P22 | **ContextManifestProtocol** | `services/protocols/context_manifest.py` | Service | Async | 1 (Micro) | Composed | (orchestration) | Pre-execute context sources for agent reasoning |

**IPC Protocols (brick subsets, not primary ABCs):**

| # | Protocol | File | Tier | Sync | Methods | Notes |
|---|----------|------|------|------|---------|-------|
| I1 | **VFSOperations** | `ipc/protocols.py` | IPC | Async | 8 | Subset of P1+P5 for IPC brick inbox/outbox |
| I2 | **EventPublisher** | `ipc/protocols.py` | IPC | Async | 1 | Minimal publish for IPC isolation |
| I3 | **EventSubscriber** | `ipc/protocols.py` | IPC | Async | 1 | Minimal subscribe for IPC isolation |

**Composite Facade (not a primary ABC):**

| # | Protocol | File | Tier | Sync | Methods | Notes |
|---|----------|------|------|------|---------|-------|
| F1 | **NexusFilesystem** | `skills/protocols.py` | Skills | Mixed | 60+ | Composite of P1-P22 for skill runtime; must match `core.filesystem.NexusFilesystem` |

### 2.2 Orthogonality Analysis

**Each Ops ABC should provide a unique capability profile. Check for overlaps:**

#### 2.2.1 No Overlap (Clean)

| Protocol Pair | Why They're Distinct |
|---------------|---------------------|
| P1 (FileMetadata) ↔ P2 (ContentStore) | Metadata paths vs blob content — different data planes |
| P5 (VFSRouter) ↔ P10 (Mount) | Kernel path resolution (1 method: `route()`) vs service-tier mount lifecycle (add/remove/sync/save/load + mount CRUD absorbed from former P5) |
| P6 (Search) ↔ P13 (LLM) | Pattern matching vs AI inference — different compute models |
| P8 (Permission) ↔ P18 (NamespaceManager) | Graph traversal authorization vs binary visibility check |
| P14 (AgentRegistry) ↔ P15 (Scheduler) | Identity/state machine vs work queue — different state models |
| P16 (Skills) ↔ P13 (LLM) | Package distribution vs AI reading — different concerns |
| P11 (ShareLink) ↔ P12 (OAuth) | Capability URLs vs OAuth flows — different auth patterns |

#### 2.2.2 Overlaps / Bundling Issues

| Issue | Protocols | Problem | Recommendation |
|-------|-----------|---------|----------------|
| ~~**BUNDLE**~~ | ~~P9 (Events)~~ | ~~Bundles file watching + advisory locking~~ | **DONE (#546)** — split into WatchProtocol + LockProtocol |
| **TWO LEVELS** | P6 (Search) + P7 (SearchBrick) | Both cover Content Discovery; P7 is per-brick, P6 is aggregate | Keep both — P7 is driver-level, P6 is service-level |
| **COMPOSITION** | P2 (ContentStore) + P3 (DirOps) → P4 (Connector) | P4 is the union of P2 + P3 + lifecycle | Keep all three — ISP compliance (callers needing only read don't depend on mkdir) |
| **OVERLAP** | P19 (EventLog service) ↔ P17 (HookEngine) | Both fire on operations; hooks are sync pre/post, event log is async durable append | Keep both — different timing (inline vs async) and durability (ephemeral vs durable) |
| **TINY** | P20 (AnomalyDetector) | 1 method — too small for an ABC? | Keep as-is — strategy pattern for swappable detection algorithms |
| **TINY** | P22 (ContextManifest) | 1 method — scenario was eliminated as "orchestration" | **CANDIDATE FOR REMOVAL** as Ops ABC (keep as convenience interface) |

#### 2.2.3 Gaps (Scenarios WITHOUT an Ops ABC)

| Scenario | Code Exists? | Gap Analysis |
|----------|-------------|--------------|
| S3 History & Snapshots | Yes — `workspace_snapshot/restore/log/diff`, `get_version/list_versions/rollback/diff_versions` in NexusFS | **MISSING PROTOCOL** — Version and workspace operations are directly on NexusFS, no Protocol extraction yet |
| S21 Agent Memory | Yes — `services/memory/` (5 modules, 15+ API routes) | **MISSING PROTOCOL** — `MemoryAPI` exists as concrete class, no Protocol ABC |
| S22 ACE | Yes — `services/ace/` (9 modules, 10+ RPC methods) | **MISSING PROTOCOL** — ACE operations go through NexusFS directly |
| S23 Agent Delegation | Yes — `services/delegation/` (5 modules) | **MISSING PROTOCOL** — `DelegationService` is concrete, no Protocol ABC |
| S24 Operations Undo | Yes — `storage/operation_logger.py`, `services/operation_undo_service.py` | `OperationLogProtocol` exists (`services/protocols/operation_log.py`). `OperationUndoService` extracted from CLI — handles undo orchestration via router + kernel primitives. |
| S25 Governance | Partial — `AnomalyDetectorProtocol` (1 method) covers anomaly only | **INCOMPLETE** — Collusion, trust, suspension, approval workflow have no protocol |
| S26 Reputation | Yes — `services/reputation/` (4 modules) | **MISSING PROTOCOL** — `ReputationService` is concrete, no Protocol ABC |
| S27 Plugins | Yes — `plugins/` (7 modules) | **MISSING PROTOCOL** — `PluginRegistry` is concrete, no Protocol ABC |
| S28 Workflows | Yes — `workflows/` (8 modules) | **MISSING PROTOCOL** — `WorkflowEngine` is concrete, no Protocol ABC |

**Summary: 9 out of 28 scenarios have no Ops ABC Protocol.** These are all newer service-layer features that were implemented as concrete classes without Protocol extraction.

### 2.3 Step 2 Summary

#### Existing Ops ABCs: 22 Protocols

| Category | Count | Status |
|----------|-------|--------|
| **Clean, well-defined** | 17 | P1, P2, P3, P4, P5, P6, P7, P8, P10, P11, P12, P13, P14, P15, P16, P19, P21 |
| **Needs split** | 1 | P9 (Events → Watch + Lock) |
| **Too small / candidate for merge** | 2 | P20 (Anomaly, 1 method), P22 (ContextManifest, 1 method) |
| **Infrastructure (not primary ABC)** | 2 | P17 (Hooks), P18 (NamespaceManager) |

#### Missing Ops ABCs: 9 scenarios without Protocol

| Priority | Scenario | Recommended Protocol Name |
|----------|----------|--------------------------|
| HIGH | S3 History & Snapshots | `VersionProtocol` |
| HIGH | S21 Agent Memory | `MemoryProtocol` |
| HIGH | S22 ACE | `TrajectoryProtocol` |
| MED | S23 Agent Delegation | `DelegationProtocol` |
| MED | S25 Governance (full) | `GovernanceProtocol` (extend beyond AnomalyDetector) |
| MED | S26 Reputation | `ReputationProtocol` |
| ~~LOW~~ | ~~S24 Operations Undo~~ | ~~`OperationLogProtocol`~~ ✓ Done — `OperationLogProtocol` + `OperationUndoService` |
| LOW | S27 Plugins | `PluginProtocol` |
| LOW | S28 Workflows | `WorkflowProtocol` |

#### Recommended Changes

1. ~~**SPLIT P9**~~ ✓ Done — `WatchProtocol` (`services/protocols/watch.py`) + `LockProtocol` (`services/protocols/lock.py`)
2. **DEPRECATE P22** (ContextManifestProtocol) — scenario was eliminated; protocol is orchestration, not a true Ops ABC
3. **EXTEND P20** (AnomalyDetectorProtocol) → `GovernanceProtocol` (add collusion, trust, suspension methods)
4. **CREATE 8 new Protocols** for missing scenarios (S3, S21-S28)

## STEP 3: AFFINITY MATCHING

Map each surviving scenario (S1-S28) to its canonical Ops ABC (existing or proposed).

### 3.1 Affinity Matrix

| Scenario | Ops ABC | Status | Tier | Match Quality | Notes |
|----------|---------|--------|------|---------------|-------|
| **S1** File I/O | **P1** MetastoreABC + **P2** ContentStoreProtocol | EXISTS | Kernel | EXACT | Metadata (P1) + content (P2) = complete file I/O |
| **S2** Content Discovery | **P6** SearchProtocol | EXISTS | Service | EXACT | glob/grep/semantic unified behind one Protocol |
| **S3** History & Snapshots | *(new)* **VersionProtocol** | MISSING | Service | — | Needs: `get_version`, `list_versions`, `rollback`, `diff`, `snapshot`, `restore`, `log` |
| **S4** Mount Management | **P10** MountProtocol | EXISTS | Service | EXACT | 15 methods covering full mount lifecycle |
| **S5** Content Sharing | **P11** ShareLinkProtocol | EXISTS | Service | EXACT | Capability URL CRUD + access logs |
| **S6** Credential Management | **P12** OAuthProtocol | EXISTS | Service | EXACT | OAuth flow + credential CRUD + MCP connect |
| **S7** Permission (ReBAC) | **P8** PermissionProtocol | EXISTS | Service | EXACT | 6 core Zanzibar APIs |
| **S8** File Watching | **WatchProtocol** (split from P9) | EXISTS | Service | EXACT | `services/protocols/watch.py` — inotify-style long-poll |
| **S9** Advisory Locking | **LockProtocol** (split from P9) | EXISTS | Service | EXACT | `services/protocols/lock.py` — flock-style advisory locks |
| **S10** Agent Lifecycle | **P14** AgentRegistryProtocol | EXISTS | Service | EXACT | 6 methods: register/get/transition/heartbeat/list/unregister |
| **S11** Agent Scheduling | **P15** SchedulerProtocol | EXISTS | Service | EXACT | 4 methods: submit/next/cancel/pending_count |
| **S12** Skill Management | **P16** SkillsProtocol | EXISTS | Service | EXACT | 9 methods: share/discover/subscribe/load/export |
| **S13** LLM Reading | **P13** LLMProtocol | EXISTS | Service | EXACT | 4 methods: read/detailed/stream/create_reader |
| **S14** Sandbox Execution | *(implicit)* in NexusFilesystem | MISSING | Service | — | Sandbox ops are on NexusFS directly; no Protocol extraction yet |
| **S15** Lifecycle Hooks | **P17** HookEngineProtocol | EXISTS | Service | EXACT | 3 methods: register/unregister/fire |
| **S16** Namespace Visibility | **P18** NamespaceManagerProtocol | EXISTS | Service | EXACT | 3 methods: is_visible/get_mount_table/invalidate |
| **S17** Event Log (Audit) | **P19** EventLogProtocol (service) | EXISTS | Service | EXACT | 8 methods: append/batch/read/truncate/sync/close |
| **S18** Payment | **P21** PaymentProtocol | EXISTS | Service | EXACT | Strategy pattern: protocol_name/can_handle/transfer |
| **S19** VFS Routing | **P5** VFSRouterProtocol | EXISTS | Kernel | EXACT | 1 method: `route()` only. Mount CRUD moved to P10 MountProtocol (Service). |
| **S20** Storage Connector | **P4** ConnectorProtocol | EXISTS | Kernel (driver) | EXACT | 13 methods: content + directory + lifecycle |
| **S21** Agent Memory | *(new)* **MemoryProtocol** | MISSING | Service | — | Needs: store/get/edit/delete/search/invalidate/revalidate/history/lineage |
| **S22** ACE | *(new)* **TrajectoryProtocol** | MISSING | Service | — | Needs: start/log_step/complete/feedback/score/playbook CRUD/reflect/curate |
| **S23** Agent Delegation | *(new)* **DelegationProtocol** | MISSING | Service | — | Needs: delegate/revoke/list |
| **S24** Operations Undo | **OperationLogProtocol** + **OperationUndoService** | EXISTS | Service | EXACT | `OperationLogProtocol` (`services/protocols/operation_log.py`) covers log/list/query. `OperationUndoService` (`services/operation_undo_service.py`) covers undo orchestration. |
| **S25** Governance | *(extend P20)* **GovernanceProtocol** | INCOMPLETE | Service | — | Extend AnomalyDetector → add collusion/trust/suspension/approval |
| **S26** Reputation | *(new)* **ReputationProtocol** | MISSING | Service | — | Needs: get_score/leaderboard/feedback/dispute/resolve |
| **S27** Plugins | *(new)* **PluginProtocol** | MISSING | Service | — | Needs: install/uninstall/list/scaffold |
| **S28** Workflows | *(new)* **WorkflowProtocol** | MISSING | Service | — | Needs: load/execute/list/trigger |

### 3.2 Coverage Summary

```
                    Existing Protocol?
                    YES (exact)     NEEDS WORK      MISSING
Kernel tier:       S1,S19,S20      —               —
Service tier:      S2,S4-S7,       S8,S9 (split)   S3,S14,S21-S23,
                   S10-S13,        S25 (extend)    S26-S28
                   S15-S18,S24
```

| Status | Count | Scenarios |
|--------|-------|-----------|
| **EXACT match** | 18 | S1, S2, S4, S5, S6, S7, S10, S11, S12, S13, S15, S16, S17, S18, S19, S20, S24 |
| **Needs split** | 2 | S8 (Watch), S9 (Lock) — from P9 EventsProtocol |
| **Needs extension** | 1 | S25 (Governance) — extend P20 AnomalyDetector |
| **Missing Protocol** | 7 | S3, S14, S21, S22, S23, S26, S27, S28 |
| **TOTAL** | 28 | |

### 3.3 Tier Assignment

Tier assignment per KERNEL-ARCHITECTURE.md (three swap tiers):

| Tier | Swap | Protocols | Scenarios |
|------|------|-----------|-----------|
| **Static Kernel** (never swap) | Compile-time | P1 (FileMetadata), P5 (VFSRouter) | S1 (File I/O metadata), S19 (VFS Routing) |
| **Drivers** (config-time DI) | Restart required | P2 (ContentStore), P3 (DirOps), P4 (Connector), P7 (SearchBrick) | S20 (Storage Connector), S2 (Search per-brick) |
| **Services** (runtime load/unload) | Hot-swap via ServiceRegistry | All others (P6, P8-P22, new Protocols) | S2-S18, S21-S28 |

### 3.4 Findings and Recommendations

**Architecture Health:**
- 17 of 28 scenarios (61%) have an exact Protocol match — the oliverfeng Protocol extraction effort has good coverage
- All 3 kernel scenarios (S1, S19, S20) are fully covered with Protocols
- The 9 missing Protocols are all newer service-layer features (Memory, ACE, Delegation, etc.)

**Priority Actions:**

1. ~~**Split EventsProtocol (P9)**~~ → WatchProtocol + LockProtocol — **DONE (#546)**

2. **Create VersionProtocol** for S3 History & Snapshots
   - Rationale: HIGH value scenario with ~10 operations currently inlined in NexusFS
   - Priority: HIGH — needed for workspace versioning feature

3. **Create MemoryProtocol** for S21 Agent Memory
   - Rationale: 30+ operations, full service layer, unique lifecycle (invalidation/lineage/paging)
   - Priority: HIGH — core agent infrastructure

4. **Create TrajectoryProtocol** for S22 ACE
   - Rationale: 10+ operations with dedicated service layer and distinct data model
   - Priority: HIGH — evaluation/feedback loop for agent quality

5. **Extend AnomalyDetectorProtocol → GovernanceProtocol** for S25
   - Rationale: 1-method Protocol is insufficient for 13 API routes spanning 6 sub-domains
   - Priority: MED

6. **Create remaining Protocols** (S23 Delegation, S26 Reputation, S27 Plugins, S28 Workflows)
   - Priority: LOW-MED — these can be extracted incrementally as concrete classes stabilize
   - S24 Undo: ✓ Done — `OperationLogProtocol` + `OperationUndoService` (`services/operation_undo_service.py`)

**Non-Actions:**
- P22 (ContextManifestProtocol): keep as convenience interface but do NOT count as Ops ABC — it's orchestration
- IPC Protocols (I1-I3): keep as brick-local subsets — not primary Ops ABCs
- NexusFilesystem (F1): keep as composite facade for skills runtime — verified by protocol compat test
- P2 + P3 + P4 composition: correct ISP pattern — keep all three levels

---

## APPENDIX A: Transport & Infrastructure Details

### A.1 gRPC Proto Services (System Tier)

> SSOT: Proto files in `proto/` define all RPC services.

| Proto Service | Proto File | Scope | Purpose |
|---------------|-----------|-------|---------|
| `ZoneTransportService` | `proto/nexus/raft/transport.proto` | Internal | Node-to-node Raft messages (StepMessage, ReplicateEntries) |
| `ZoneApiService` | `proto/nexus/raft/transport.proto` | Internal | Client-facing zone ops (Propose, Query, GetClusterInfo, JoinZone, JoinCluster) |
| `ExchangeService` | `proto/nexus/exchange/v1/exchange.proto` | External | Agent Exchange API — identity (4 RPCs), payment (8 RPCs), audit (5 RPCs). REST-only Phase 1; Connect-RPC Phase 2/3. |

Named `Zone*` to match `ZoneConsensus` (Rust). IPC Agent Messaging (`src/nexus/ipc/`) uses VFS as transport — see S29.

### A.2 EventBus Backends (User Space Tier)

| Backend | Module | Durability | Notes |
|---------|--------|-----------|-------|
| `RedisEventBus` | `services/event_bus/redis.py` | Best-effort (PG operation_log is SSOT) | Current default; Dragonfly/Redis pub/sub |
| `NatsEventBus` | `services/event_bus/nats.py` | Durable (JetStream ack/nack) | Preferred long-term; 7-day retention |
| `InMemoryEventBus` | *(not yet implemented)* | Ephemeral | Needed for kernel-only/embedded mode |

All should route through `CacheStoreABC` pub/sub rather than direct client access.
Federation gap: EventBus is currently zone-local — cross-zone propagation not yet designed.
