# Data-to-Storage Properties Matrix

**Date:** 2026-02-12 (updated: sled → redb throughout)
**Status:** Steps 1-3 COMPLETE — All Data-Storage Affinity Decisions Resolved
**Purpose:** Catalog ALL data types in Nexus and determine optimal storage for each

**Companion docs.** This matrix maps the **data side**: what data types exist
and which storage capability fits each. The **HAL side** — how the kernel
abstracts those storage capabilities into pluggable driver contracts — lives
in `KERNEL-ARCHITECTURE.md` §3.A (Storage HAL: ABC pillars) and §3.B
(Control-Plane HAL: `DistributedCoordinator`, `ObjectStoreProvider`).

---

## Methodology

Three-step **Data-Storage Affinity** analysis:

### Step 1: Data Layer
Eliminate or merge redundant data types based on **properties** and **use cases**.
- For each data type ask: "why does this exist?" and "is it redundant with another type?"
- Merge types that share the same properties and lifecycle (e.g. tenant→zone, FilePathModel→FileMetadata)

### Step 2: Storage Layer
Verify storage medium **orthogonality** — no two stores should serve the same role.
- Each storage medium must have a unique capability profile
- Identify and deprecate redundant stores (e.g. Redis/Dragonfly post-Raft)

### Step 3: Affinity Matching
Map **data requiring properties** ↔ **storage providing properties**.
- Match each surviving data type to the storage medium whose properties best fit
- Result: each data type has exactly one canonical storage home

---

## Property Dimensions

| Property | Values | Meaning |
|----------|--------|---------|
| **Read Perf** | Low / Medium / High / Critical | Read query frequency & latency requirements |
| **Write Perf** | Low / Medium / High / Critical | Write frequency & latency requirements |
| **Consistency** | EC / SC / Strict SC | Eventual / Strong / Strict Strong Consistency |
| **Query Pattern** | KV / Relational / Vector / Blob | Access pattern (key-value, JOIN, similarity, large binary) |
| **Data Size** | Tiny / Small / Medium / Large / Huge | Typical size per record |
| **Cardinality** | Low / Medium / High / Very High | Number of records |
| **Durability** | Ephemeral / Session / Persistent / Archive | How long data must survive |
| **Scope** | System / Zone / User / Session | Isolation boundary |
| **Why Exists** | Brief rationale | First-principles justification |

---

## PART 1: CORE FILESYSTEM DATA

### 1.1 File Metadata (Primary)

| Data Type | Read | Write | Consistency | Query | Size | Card | Dur | Scope | Why Exists | Current Storage | Optimal Storage | Action |
|-----------|------|-------|-------------|-------|------|------|-----|-------|------------|----------------|-----------------|--------|
| **FilePathModel** | High | Med | SC (multi) / Local (single) | Relational (JOIN on zone_id, FK lookups) | Small | Very High | Persistent | Zone | Map virtual path → backend location; support multi-backend | SQLAlchemy | ~~Keep SQLAlchemy~~ → **MERGE into FileMetadata (redb)** | ✅ DECIDED: MERGE + DEPRECATE |
| **FileMetadata** (proto) | High | Med | SC (multi) / Local (single) | KV (by path) | Small | Very High | Persistent | Zone | Core file attributes (size, etag, timestamps) | Generated proto → Python dataclass | **redb via Raft** (KV-friendly, SC via Raft) | ✅ MIGRATE |
| **CompactFileMetadata** | Critical | Med | SC | KV | Tiny | Very High | Session | Zone | Memory-optimized metadata for L1 cache | In-memory (string interning) | **In-memory only** (cache layer) | ✅ KEEP |

**Analysis (Step 1 DECIDED):**
- **FilePathModel → FileMetadata**: ✅ **MERGE CONFIRMED**. Deprecate relational model long-term.
  - FilePathModel has 17 columns but only 2 JOINs in codebase (cache invalidation + Tiger predicate pushdown)
  - Both JOINs replaceable: cache invalidation → redb prefix scan, Tiger → direct redb query
  - FK to FileMetadataModel (custom KV) → redb prefix-keyed entries (`meta:{path_id}:{key}`)
  - No irreplaceable relational query exists on FilePathModel
- **CompactFileMetadata**: ✅ **KEEP** — Same 13 fields as FileMetadata but all strings interned to int IDs (cache-tier projection, ~64-100 bytes vs ~200-300 bytes at 1M+ scale). Already auto-generated from proto via `gen_metadata.py`.
- **FileMetadataModel (custom KV)**: ✅ **KEEP SEPARATE** — Arbitrary `{path_id, key, value}` pairs, fundamentally different from FileMetadata's fixed schema. Should NOT inherit from FileMetadata.

### 1.2 Directory Indexing

| Data Type | Read | Write | Consistency | Query | Size | Card | Dur | Scope | Why Exists | Current Storage | Optimal Storage | Action |
|-----------|------|-------|-------------|-------|------|------|-----|-------|------------|----------------|-----------------|--------|
| **DirectoryEntryModel** | High | Low | SC | KV (by parent_path) | Small | High | Persistent | Zone | Sparse directory index for O(1) non-recursive `ls` | SQLAlchemy | ~~Separate redb entry~~ → **MERGE into FileMetadata** | ✅ DECIDED: MERGE |

**Analysis (Step 1+3 DECIDED):**
- Currently uses SQLAlchemy but access pattern is pure KV (lookup by parent_path)
- No JOINs needed → Metastore (redb)
- **Step 3 merge decision**: In redb's ordered KV, directory listing = prefix scan on FileMetadata keys under `{parent_path}/`. One less data type. redb at ~14μs/op handles 1000-entry dirs in ~14ms.
- If future profiling shows large-directory bottleneck, re-introduce sparse index as Metastore-internal optimization (not a separate data type)

### 1.3 Custom Metadata

| Data Type | Read | Write | Consistency | Query | Size | Card | Dur | Scope | Why Exists | Current Storage | Optimal Storage | Action |
|-----------|------|-------|-------------|-------|------|-----|-------|------------|----------------|-----------------|--------|
| **FileMetadataModel** (KV) | Med | Low | EC | KV (by path_id + key) | Small | Med | Persistent | Zone | Arbitrary user-defined metadata (tags, custom fields) | SQLAlchemy | **redb via Raft** (KV) | ✅ MIGRATE |

**Analysis:**
- Pure KV access (lookup by path_id + key)
- No relational queries
- **Action**: Migrate to redb

---

## PART 2: CONTENT & DEDUPLICATION

| Data Type | Read | Write | Consistency | Query | Size | Card | Dur | Scope | Why Exists | Current Storage | Optimal Storage | Action |
|-----------|------|-------|-------------|-------|------|------|-----|-------|------------|----------------|-----------------|--------|
| **ContentChunkModel** | Med | Low | EC | KV (by content_hash) | Small | High | Persistent | System | CAS (Content-Addressed Storage) for deduplication; track refcount | SQLAlchemy | **redb** (KV by hash, no Raft needed for CAS) | ✅ MIGRATE |
| **File Content (blobs)** | Med | Low | EC | Blob (by path) | Huge | Very High | Persistent | Zone | Actual file data | Disk / S3 / GCS | **Keep Disk/S3** (blob storage) | ✅ KEEP |
| **ContentCacheModel** | Med | Low | EC | KV (by path_id) | Large | High | Session | Zone | Parsed content cache (avoid re-parsing) | SQLAlchemy + Disk | **Disk only** (binary cache, no DB metadata needed) | ✅ DECIDED: ELIMINATE DB |

**Analysis (Step 1 DECIDED):**
- **ContentChunkModel**: ✅ Pure CAS, immutable → move to redb (no Raft, just local KV)
- **ContentCacheModel**: ✅ **ELIMINATE DB metadata**, simplify to pure disk cache with TTL. No DB model needed.

---

## PART 3: VERSIONING

| Data Type | Read | Write | Consistency | Query | Size | Card | Dur | Scope | Why Exists | Current Storage | Optimal Storage | Action |
|-----------|------|-------|-------------|-------|------|------|-----|-------|------------|----------------|-----------------|--------|
| **VersionHistoryModel** | Low | Low | EC | Relational (parent_version_id FK) | Small | High | Archive | Zone | Track file/memory/skill version history | SQLAlchemy with BRIN index | **Keep SQLAlchemy** (needs parent FK, BRIN for time-series) | ✅ KEEP |
| **WorkspaceSnapshotModel** | Low | Low | EC | Relational (FK to snapshot files) | Small | Low | Archive | Zone | Point-in-time workspace captures (zero-copy via CAS) | SQLAlchemy | **Keep SQLAlchemy** (relational queries for snapshot browsing) | ✅ KEEP |

**Analysis:**
- Both have relational queries (parent FK, time-series)
- Low frequency → PostgreSQL BRIN indexes work well
- **Action**: Keep in SQLAlchemy

---

## PART 4: MEMORY SYSTEM (ACE)

| Data Type | Read | Write | Consistency | Query | Size | Card | Dur | Scope | Why Exists | Current Storage | Optimal Storage | Action |
|-----------|------|-------|-------------|-------|------|------|-----|-------|------------|----------------|-----------------|--------|
| **MemoryModel** | Med | Med | EC | Relational + Vector (embedding search, entity/relationship queries, decay tracking) | Medium | Very High | Persistent | User/Agent | AI agent memory with identity-based 3-layer permissions; supports semantic search, entity extraction, temporal refs, ACE consolidation | SQLAlchemy with BRIN + vector index (pgvector/sqlite-vec) | **Keep SQLAlchemy** (complex relational + vector queries) | ✅ KEEP |
| **MemoryConfig** | Low | Low | EC | KV (by path) | Tiny | Low | Persistent | Zone | Memory directory configuration | In-memory + SQLAlchemy | ~~redb~~ → **Keep RecordStore** (co-existence with MemoryModel) | ✅ DECIDED: STAY RecordStore |
| **TrajectoryModel** | Low | Med | EC | Relational (FK to agent, task) | Small | High | Persistent | Agent | Task execution traces for ACE learning | Inferred (implicit in memory system) | **Keep SQLAlchemy** (relational) | ✅ KEEP |
| **TrajectoryFeedbackModel** | Low | Low | EC | Relational (FK to trajectory) | Small | Med | Persistent | Agent | Feedback on trajectories | SQLAlchemy | **Keep SQLAlchemy** (FK to trajectory) | ✅ KEEP |
| **PlaybookModel** | Low | Low | EC | Relational (FK to strategies) | Medium | Med | Persistent | Agent | Strategy playbooks | Inferred (API models) | **Keep SQLAlchemy** (relational) | ✅ KEEP |

**Analysis (Step 1 DECIDED):**
- **MemoryModel**: ✅ KEEP RecordStore — complex relational + vector queries (pgvector)
- **MemoryConfig**: ✅ **KEEP RecordStore** (was: migrate to redb). **Cross-pillar co-existence principle**: MemoryConfig is meaningless without MemoryModel. If RecordStore is not injected, orphaned MemoryConfig entries in Metastore would point to non-functional memory. Configs that only serve RecordStore data belong in RecordStore.
- Trajectory/Playbook: ✅ KEEP RecordStore — relational FK
- **No merges needed** within this part — all serve distinct purposes

---

## PART 4b: KNOWLEDGE GRAPH (GraphRAG)

| Data Type | Read | Write | Consistency | Query | Size | Card | Dur | Scope | Why Exists | Current Storage | Optimal Storage | Action |
|-----------|------|-------|-------------|-------|------|------|-----|-------|------------|----------------|-----------------|--------|
| **EntityModel** | Med | Med | EC | Relational + Vector (embedding similarity, name lookup, batch fetch) | Medium | High | Persistent | Zone | Knowledge graph entities with embeddings for entity resolution (pgvector HNSW) | SQLAlchemy with vector index | **Keep RecordStore** (relational + vector) | ✅ KEEP |
| **RelationshipModel** | Med | Med | EC | Relational (FK to entities, recursive CTE for N-hop traversal) | Small | Very High | Persistent | Zone | Directed typed relationships between entities for graph traversal | SQLAlchemy with composite indexes | **Keep RecordStore** (relational FK + recursive CTE) | ✅ KEEP |
| **EntityMentionModel** | Low | Med | EC | Relational (FK to entity + memory, JOIN for provenance) | Tiny | High | Persistent | Zone | Links entities to source memories for provenance tracking | SQLAlchemy | **Keep RecordStore** (relational FK) | ✅ KEEP |

**Analysis:**
- **EntityModel**: ✅ KEEP RecordStore — uses pgvector for embedding similarity search (same pattern as MemoryModel). Composite indexes on `(zone_id, canonical_name)`.
- **RelationshipModel**: ✅ KEEP RecordStore — recursive CTEs for N-hop neighbor traversal require SQL. FK to EntityModel.
- **EntityMentionModel**: ✅ KEEP RecordStore — FK to both EntityModel and MemoryModel for provenance tracking.
- **GraphStore** is a RecordStore consumer (receives `RecordStoreABC` + session). Dialect selection is config-time via `RecordStoreABC._is_postgresql`.

---

## PART 5: ACCESS CONTROL (ReBAC)

| Data Type | Read | Write | Consistency | Query | Size | Card | Dur | Scope | Why Exists | Current Storage | Optimal Storage | Action |
|-----------|------|-------|-------------|-------|------|------|-----|-------|------------|----------------|-----------------|--------|
| **ReBACTupleModel** | Critical | Low | SC | Relational (composite index on subject/relation/object) | Tiny | Very High | Persistent | Zone | Zanzibar-style relationship tuples (user:alice#member@group:eng) | SQLAlchemy with composite indexes | **RecordStore** (SSOT) + **CacheStore** (hot path) | ✅ DECIDED |
| **ReBACNamespaceModel** | Med | Low | EC | KV (by namespace_id) | Small | Low | Persistent | System | Permission expansion rules (namespace config) | SQLAlchemy | **redb** (KV, low cardinality) | ✅ MIGRATE |
| **ReBACGroupClosureModel** | Critical | Low | SC | Relational (composite index on member/group) | Tiny | Very High | Persistent | Zone | Leopard-style transitive closure for O(1) group membership | SQLAlchemy with composite indexes | **Keep SQLAlchemy** (critical path, materialized view) | ✅ KEEP |
| **ReBACChangelogModel** | Low | Med | EC | Relational (BRIN index on created_at) | Small | High | Archive | Zone | Audit log for tuple modifications | SQLAlchemy with BRIN | **Keep SQLAlchemy** (append-only, BRIN optimized) | ✅ KEEP |

**Analysis (Step 1+3 DECIDED):**
- **Layering**: ReBAC is a **service** (user management), NOT kernel.
- **No merges needed** — Zanzibar-correct: TupleModel (SSOT), GroupClosureModel (derived), ChangelogModel (audit), NamespaceModel (config)
- **ReBACTupleModel affinity (Step 3)**:
  - Required: composite index (6-field), SC, persistent, critical read path
  - Ordered KV (Metastore): ✅ fast (~14μs), ✅ SC (Raft), but ❌ composite indexes must be hand-encoded as prefix keys + secondary index key patterns — reimplements what SQL gives for free
  - Relational ACID (RecordStore): ✅ composite indexes native, ✅ SC (ACID), ✅ persistent, but ⚠️ ~1ms latency
  - **Decision**: **RecordStore** (SSOT) — composite indexes are the dominant requirement. Hot-path latency solved by CacheStore (TigerCache + PermissionCache already exist as caching layer).
  - ⚠️ **Architecture risk**: Permission hot path depends on CacheStore. If CacheStore unavailable, falls back to ~1ms SQL. Acceptable — CacheStore is optional optimization, not correctness requirement.

---

## PART 6: USERS & AUTHENTICATION

| Data Type | Read | Write | Consistency | Query | Size | Card | Dur | Scope | Why Exists | Current Storage | Optimal Storage | Action |
|-----------|------|-------|-------------|-------|------|------|-----|-------|------------|----------------|-----------------|--------|
| **UserModel** | Med | Low | SC | Relational (JOIN on zone_id, email lookup) | Small | Med | Persistent | System | Core user accounts with soft delete | SQLAlchemy with soft delete | **Keep SQLAlchemy** (relational queries) | ✅ KEEP |
| **UserOAuthAccountModel** | Med | Low | SC | Relational (FK to user_id, unique constraint on provider+provider_user_id) | Small | Med | Persistent | System | OAuth provider accounts for SSO login | SQLAlchemy | **Keep SQLAlchemy** (FK, unique constraints) | ✅ KEEP |
| **OAuthCredentialModel** | Med | Low | SC | Relational (FK to user_id, zone_id, encrypted tokens) | Small | Med | Persistent | Zone | OAuth tokens for backend integrations (Google Drive, OneDrive) | SQLAlchemy with encryption | **Keep SQLAlchemy** (FK, encryption) | ✅ KEEP |
| **UserSessionModel** | High | Med | EC | KV (by session_id) | Tiny | High | Session | System | Active user sessions | SQLAlchemy | **CacheStore** (Dragonfly / In-Memory) | ✅ DECIDED: CacheStore |

**Analysis (Step 1+3 DECIDED):**
- **No merges or abstractions needed** — well-designed, minimal redundancy:
  - **UserOAuthAccountModel** vs **OAuthCredentialModel**: Intentionally separate — *login auth* (ID token only) vs *backend integration* (access/refresh tokens). Different security flows.
  - User/OAuth models: ✅ KEEP RecordStore — relational queries, FK, encryption
- **UserSessionModel affinity (Step 3)**:
  - Required: KV by session_id, TTL expiry, high read freq, EC sufficient
  - Relational ACID (RecordStore): ✅ works, but ❌ no native TTL, ❌ overkill (no JOINs/FK needed)
  - Ephemeral KV (CacheStore): ✅ KV native, ✅ TTL native, ✅ high read perf, ✅ EC
  - **Decision**: **CacheStore** — pure KV with TTL, no relational features needed
  - Admin queries ("all sessions for user X") use CacheStore scan (rare, acceptable latency)

---

## PART 7: ZONES & ISOLATION

| Data Type | Read | Write | Consistency | Query | Size | Card | Dur | Scope | Why Exists | Current Storage | Optimal Storage | Action |
|-----------|------|-------|-------------|-------|------|------|-----|-------|------------|----------------|-----------------|--------|
| **ZoneModel** | Med | Low | SC | Relational (unique constraint on domain) | Small | Low | Persistent | System | Zone/organization metadata with soft delete | SQLAlchemy with soft delete | **Keep SQLAlchemy** (unique constraint, soft delete) | ✅ KEEP |
| **EntityRegistryModel** | Med | Low | SC | Relational (parent_type/parent_id FK) | Tiny | Med | Persistent | System | Identity hierarchy (zone→user→agent) | SQLAlchemy | **Keep SQLAlchemy** (hierarchical FK) | ✅ KEEP |
| **ExternalUserServiceModel** | Low | Low | EC | Relational (encrypted config) | Small | Low | Persistent | System | External user management config | SQLAlchemy with encryption | **Keep SQLAlchemy** (encryption) | ✅ KEEP |

**Analysis:**
- All need relational features (unique constraints, FK, encryption)
- **Action**: Keep SQLAlchemy

---

## PART 8: EVENTS & SUBSCRIPTIONS

| Data Type | Read | Write | Consistency | Query | Size | Card | Dur | Scope | Why Exists | Current Storage | Optimal Storage | Action |
|-----------|------|-------|-------------|-------|------|------|-----|-------|------------|----------------|-----------------|--------|
| **FileEvent** | N/A | High | EC | Pub/Sub | Tiny | N/A | Ephemeral | Zone | File change notifications (write, delete, rename) | In-memory → Dragonfly pub/sub | **CacheStore** (pub/sub) | ✅ DECIDED: CacheStore |
| **SubscriptionCreate/Update** | Med | Low | EC | Relational (FK to zone, query by event_types) | Small | Low | Persistent | Zone | Webhook subscription config | Pydantic (API only, no DB model found) | **Need SQLAlchemy model?** | ❓ MISSING |
| **WebhookDelivery** | Low | Med | EC | Relational (BRIN on created_at) | Small | High | Archive | Zone | Webhook delivery attempt history | Pydantic (API only) | **Need SQLAlchemy model?** | ❓ MISSING |

**Analysis (Step 1+3 DECIDED):**
- **No merges** — different lifecycles (ephemeral / persistent config / audit log), pipeline: Subscription + FileEvent match → WebhookDelivery
- **Co-location**: Subscription and WebhookDelivery → RecordStore (both persistent, relational)
- **FileEvent affinity (Step 3)**:
  - Required: pub/sub (publish to channel, subscribers receive), ephemeral, high write freq, EC
  - Ordered KV (Metastore): ❌ no pub/sub — would need polling, defeats purpose of event-driven
  - Ephemeral KV + Pub/Sub (CacheStore): ✅ pub/sub native, ✅ ephemeral, ✅ high throughput, ✅ EC
  - **Decision**: **CacheStore** — pub/sub is the dominant requirement. Events are fire-and-forget notifications; missed events can be recovered from SSOT (Metastore).
  - ⚠️ **Gap**: EventBusProtocol currently has NO in-memory impl. Need `InMemoryEventBus` for kernel-only/dev mode.
- **Subscription/Delivery** DB models: ❓ STILL MISSING — need RecordStore models (Task #12)

---

## PART 9: WORKFLOWS

| Data Type | Read | Write | Consistency | Query | Size | Card | Dur | Scope | Why Exists | Current Storage | Optimal Storage | Action |
|-----------|------|-------|-------------|-------|------|------|-----|-------|------------|----------------|-----------------|--------|
| **WorkflowModel** | Med | Low | EC | Relational (FK to zone, version tracking) | Medium | Low | Persistent | Zone | Workflow definitions (YAML) | SQLAlchemy | **Keep SQLAlchemy** (version tracking, FK) | ✅ KEEP |
| **WorkflowExecutionModel** | Med | Med | EC | Relational (FK to workflow, BRIN on started_at) | Small | High | Archive | Zone | Workflow execution history | SQLAlchemy with BRIN | **Keep SQLAlchemy** (append-only, BRIN) | ✅ KEEP |

**Analysis:**
- Relational queries needed
- **Action**: Keep SQLAlchemy

---

## PART 10: SEMANTIC SEARCH

| Data Type | Read | Write | Consistency | Query | Size | Card | Dur | Scope | Why Exists | Current Storage | Optimal Storage | Action |
|-----------|------|-------|-------------|-------|------|------|-----|-------|------------|----------------|-----------------|--------|
| **DocumentChunkModel** | Med | Med | EC | Vector (embedding similarity search) | Medium | Very High | Persistent | Zone | Document chunks with embeddings for semantic search | SQLAlchemy with pgvector/sqlite-vec | **Keep SQLAlchemy** (vector indexes) | ✅ KEEP |

**Analysis:**
- Requires vector index (pgvector for PostgreSQL, sqlite-vec for SQLite)
- **Action**: Keep SQLAlchemy

---

## PART 11: AUDIT & LOGGING

| Data Type | Read | Write | Consistency | Query | Size | Card | Dur | Scope | Why Exists | Current Storage | Optimal Storage | Action |
|-----------|------|-------|-------------|-------|------|------|-----|-------|------------|----------------|-----------------|--------|
| **OperationLogModel** | Low | High | EC | Relational (BRIN on created_at) | Small | Very High | Archive | Zone | Filesystem operation audit trail | SQLAlchemy with BRIN | **Keep SQLAlchemy** (append-only, BRIN optimized) | ✅ KEEP |

**Analysis:**
- Append-only log with time-series queries
- **Action**: Keep SQLAlchemy with BRIN

---

## PART 12: SANDBOXES

| Data Type | Read | Write | Consistency | Query | Size | Card | Dur | Scope | Why Exists | Current Storage | Optimal Storage | Action |
|-----------|------|-------|-------------|-------|------|------|-----|-------|------------|----------------|-----------------|--------|
| **SandboxMetadataModel** | Med | Med | EC | Relational (FK to user/agent/zone, status queries) | Small | Med | Persistent | Zone | Managed sandbox instances (E2B, Docker, Modal) | SQLAlchemy | **Keep SQLAlchemy** (relational queries) | ✅ KEEP |

---

## PART 13: SYSTEM CONFIGURATION

| Data Type | Read | Write | Consistency | Query | Size | Card | Dur | Scope | Why Exists | Current Storage | Optimal Storage | Action |
|-----------|------|-------|-------------|-------|------|------|-----|-------|------------|----------------|-----------------|--------|
| **SystemSettingsModel** | Med | Low | SC | KV (by key) | Small | Low | Persistent | System | System-wide settings (OAuth encryption key, feature flags) | SQLAlchemy | **redb** (KV, low cardinality) | ✅ MIGRATE |
| ~~**Cluster Topology**~~ | ~~Med~~ | ~~Low~~ | ~~SC~~ | ~~???~~ | ~~Small~~ | ~~Low~~ | ~~Persistent~~ | ~~System~~ | ~~Raft cluster membership, node addresses~~ | ~~???~~ | N/A | ✅ DECIDED: ELIMINATE |

**Analysis (Step 1 DECIDED):**
- **SystemSettingsModel**: ✅ Pure KV → keep in Metastore (redb). No merge needed.
- **Cluster Topology**: ✅ **ELIMINATED** as standalone data type. Raft node membership is inherent in the Raft consensus layer's own log (redb). If no Raft service → doesn't exist. Not application-level data.

---

## PART 14: CACHE LAYERS

| Data Type | Read | Write | Consistency | Query | Size | Card | Dur | Scope | Why Exists | Current Storage | Optimal Storage | Action |
|-----------|------|-------|-------------|-------|------|------|-----|-------|------------|----------------|-----------------|--------|
| **PermissionCacheProtocol** | Critical | Med | EC | KV (by cache key) | Tiny | Very High | Session | Zone | Permission check result cache (avoid ReBAC recomputation) | Dragonfly/PostgreSQL/In-memory | **CacheStore** (in-memory, TTL) | ✅ KEEP |
| **TigerCacheProtocol** | Critical | Low | EC | KV (by object_id → bitmap) | Small | High | Session | Zone | Pre-materialized permission bitmaps for O(1) filtering | Dragonfly/PostgreSQL | **CacheStore** (in-memory, fast bitmap ops) | ✅ KEEP |

**Analysis:**
- Both are performance caches, not SSOT
- **Action**: Keep Dragonfly (in-memory cache)

---

## PART 15: WORKSPACE & MEMORY CONFIG

| Data Type | Read | Write | Consistency | Query | Size | Card | Dur | Scope | Why Exists | Current Storage | Optimal Storage | Action |
|-----------|------|-------|-------------|-------|------|------|-----|-------|------------|----------------|-----------------|--------|
| **WorkspaceConfig** | Low | Low | EC | KV (by path) | Small | Low | Persistent | Zone | Workspace directory registration | In-memory + SQLAlchemy | **MERGE → PathRegistrationModel** | ✅ DECIDED: MERGE |
| **MemoryConfigModel** (DB) | Low | Low | EC | KV (by path) | Small | Low | Persistent | Zone | Memory directory configuration (DB storage) | SQLAlchemy | **MERGE → PathRegistrationModel** | ✅ DECIDED: MERGE |

**Analysis (Step 1 DECIDED):**
- ✅ **MERGE into single `PathRegistrationModel`** with `type` discriminator ("workspace" | "memory")
- Schemas are structurally identical: path, name, description, created_at, created_by, metadata (+ DB extras: user_id, agent_id, scope, session_id, expires_at)
- **Storage**: RecordStore (not Metastore) — same co-existence principle as MemoryConfig: WorkspaceConfig is meaningless without WorkspaceSnapshotModel in RecordStore

---

## SUMMARY: STORAGE LAYER DECISIONS

All data-storage affinity decisions (Steps 1-3) are resolved. See per-Part analyses above for
individual rationale, and the Quartet section below for complete data type → pillar mapping tables.

### ⚠️ **Architecture Risk**

**CacheStore dependency for permissions**: ReBAC hot path (TigerCache, PermissionCache) depends on CacheStore. If CacheStore unavailable, falls back to ~1ms SQL (RecordStore). Acceptable — optimization, not correctness. See Quartet "CacheStore Implementation Status" for InMemory impl gaps.

### ❓ **REMAINING GAPS**

1. **Subscription/Delivery DB models**: Pydantic models exist, need RecordStore models (Task #12)
2. **CacheStoreABC + InMemoryCacheStore**: Need to implement for kernel-only/dev fallback (Task #22)

---

## REDUNDANCY ANALYSIS (Step 1 Complete)

### ✅ Confirmed MERGES:

1. **FilePathModel + FileMetadata** → ✅ **MERGE into FileMetadata (redb)**
   - FilePathModel 17 columns, only 2 JOINs (both replaceable with redb prefix scan)
   - Deprecate relational model long-term

2. **WorkspaceConfig + WorkspaceConfigModel + MemoryConfig + MemoryConfigModel** → ✅ **MERGE into PathRegistrationModel (RecordStore)**
   - All 4 types have identical schemas (path, name, description, created_at, created_by, metadata)
   - Single model with `type` discriminator ("workspace" | "memory")
   - Lives in RecordStore (co-existence principle: meaningless without SnapshotModel/MemoryModel)

3. **Cluster Topology** → ✅ **ELIMINATED**
   - Not application-level data; inherent in Raft consensus layer
   - If no Raft → doesn't exist

4. **ContentCacheModel** → ✅ **ELIMINATE DB metadata**
   - Simplify to pure disk cache with TTL, no SQLAlchemy model needed

### ✅ Confirmed NO-MERGE (architecture is correct):

5. **ReBAC 4 types** — Zanzibar-correct: SSOT (Tuple, Namespace), Derived (GroupClosure), Audit (Changelog)
6. **User/Auth 4 types** — Clean separation: identity (User), login auth (OAuthAccount), backend integration (OAuthCredential), sessions (UserSession)
7. **Events 3 types** — Different lifecycles: ephemeral (FileEvent), persistent config (Subscription), audit (Delivery)
8. **CompactFileMetadata** — Cache-tier projection of FileMetadata (auto-generated from proto)
9. **FileMetadataModel (custom KV)** — Arbitrary user-defined pairs, fundamentally different from fixed-schema FileMetadata

### 🆕 New Principle: Cross-Pillar Co-existence

> **If a config type only exists to serve data in another pillar, it belongs in that pillar.**
>
> MemoryConfig is meaningless without MemoryModel (RecordStore). WorkspaceConfig is meaningless without WorkspaceSnapshotModel (RecordStore). Therefore both belong in RecordStore, not Metastore, despite their KV access pattern.

---

## STORAGE MEDIUM ORTHOGONALITY ANALYSIS (Step 2 — DECIDED)

### Core Insight: Storage Mediums = Pillars, not Implementations

Orthogonality analysis operates at the **storage medium** level, not the driver/implementation level.
Drivers within the same pillar are interchangeable (deployment-time config via ABC), not architectural choices.

> **Principle**: If two "storage mediums" serve the same query pattern and are abstracted behind the same ABC,
> they are **drivers** of one medium, not separate mediums.

This collapses 9 listed implementations → **4 storage mediums** (1:1 with Four Pillars):

| Pillar | Storage Medium | Unique Properties | Drivers (interchangeable via ABC) |
|--------|---------------|-------------------|-----------------------------------|
| **Metastore** | Ordered KV | Persistent, ordered prefix scan, optional Raft SC, ~14μs ops | redb (local PyO3), redb (gRPC Raft) |
| **RecordStore** | Relational ACID | JOINs, FK, unique constraints, vector search, BRIN indexes | PostgreSQL (networked, multi-writer), SQLite (embedded, single-writer) |
| **ObjectStore** | Blob | Streaming I/O, petabyte scale, content-addressed | S3, GCS, Azure Blob (cloud), Local Disk (embedded) |
| **CacheStore** | Ephemeral KV + Pub/Sub | TTL, pub/sub, no persistence guarantee | Dragonfly (networked), In-Memory (process-local: Python dict / DashMap) |

### Kernel Self-Inclusiveness Check

With kernel-only (Metastore required + ObjectStore required, no services):

| Kernel need | Provided by | Storage property used |
|-------------|------------|----------------------|
| File metadata (inode) | Metastore (redb) | KV by path |
| Directory index (dentry) | Metastore (redb) | Ordered prefix scan |
| Zone revision tracking | Metastore (redb) | `/__sys__/` KV entries |
| System settings | Metastore (redb) | KV by key |
| File content (bytes) | ObjectStore (Backend) | Blob by path |

Kernel does NOT need: JOINs, FK, vector search, BRIN, TTL, pub/sub, composite indexes.
Those are all service-layer concerns (RecordStore/CacheStore).

**Verdict**: ✅ Kernel is **self-inclusive** with 2 storage mediums (Ordered KV + Blob). Zero unnecessary properties.

CompactFileMetadata (DashMap L1 cache) is process-internal optimization, not a storage medium — like a CPU cache.

### Orthogonality Between Pillars (4 mediums)

#### ✅ Ordered KV (Metastore) vs Relational ACID (RecordStore)
- **Metastore**: Pure KV, ordered prefix scan, ~14μs, no JOINs, no FK
- **RecordStore**: JOINs, FK, unique constraints, vector search, BRIN indexes, ~1ms
- **Verdict**: **Orthogonal** — fundamentally different query patterns (KV vs relational)

#### ✅ Ordered KV (Metastore) vs Ephemeral KV (CacheStore)
- **Metastore**: Persistent SSOT, linearizable (Raft), embedded
- **CacheStore**: Ephemeral cache, eventual consistency, TTL eviction, pub/sub
- **Verdict**: **Orthogonal** — different durability (persistent vs ephemeral) and consistency guarantees

#### ✅ Relational ACID (RecordStore) vs Blob (ObjectStore)
- **RecordStore**: Structured data, small records, complex queries
- **ObjectStore**: Unstructured bytes, huge objects, no queries (by-key only)
- **Verdict**: **Orthogonal** — different data shape (structured vs unstructured)

#### ✅ Ephemeral KV (CacheStore) vs Blob (ObjectStore)
- **CacheStore**: Tiny KV entries, TTL, pub/sub, in-memory
- **ObjectStore**: Huge blobs, persistent, streaming I/O
- **Verdict**: **Orthogonal** — different size profile and durability

### Driver Merges Within Pillars (Step 2 decisions)

#### ❌ **DEPRECATE Redis** → merge into Dragonfly (CacheStore driver)
- Same storage medium (Ephemeral KV + Pub/Sub), same protocol
- Dragonfly: 25x memory efficiency, multi-threaded, drop-in replacement
- **Migration**: Change connection string only, zero code changes

#### ✅ **MERGE In-Memory Python dict + DashMap** → single "In-Memory" driver (CacheStore)
- Same storage medium: process-local ephemeral KV, no persistence, no TTL
- DashMap is a faster engine (~100ns vs ~1μs), not a different medium
- Under CacheStoreABC: `InMemoryCacheStore(engine="dict")` vs `InMemoryCacheStore(engine="dashmap")`

#### ✅ **PostgreSQL + SQLite** are drivers, not separate mediums (RecordStore)
- Same query patterns (SQL, JOINs, FK, ACID), same ABC (RecordStoreABC via SQLAlchemy)
- Difference is operational (networked multi-writer vs embedded single-writer), not architectural
- Driver selection is deployment-time configuration, not storage architecture

#### ✅ **S3/GCS/Azure + Local Disk** are drivers, not separate mediums (ObjectStore)
- Same access pattern (blob by key, streaming I/O), same ABC (ObjectStoreABC = Backend)
- Difference is operational (cloud managed vs local embedded)

### Storage Medium Properties Matrix (4 mediums)

| Medium | Read Perf | Write Perf | Consistency | Query Patterns | Durability | Unique Capability |
|--------|-----------|------------|-------------|----------------|------------|-------------------|
| **Ordered KV** | Critical (~14μs) | Critical (~14μs) | Linearizable (Raft) / Local | Ordered KV, prefix scan, range queries | Persistent (B+ tree) | **Ordered iteration** for user root localization (first key = `/` in chroot) |
| **Relational ACID** | Med (~1ms) | Med (~1ms) | Serializable (ACID) | JOIN, FK, vector (pgvector), BRIN | Persistent (WAL) | **Complex queries** — JOINs, referential integrity, vector similarity search |
| **Blob** | Med (variable) | Med (variable) | Eventual / Local | By-key only, streaming I/O | Persistent (11-nines) | **Unbounded size** — petabyte-scale object storage |
| **Ephemeral KV** | Critical (<1μs) | Critical (<1μs) | Eventual / Local | KV + pub/sub + TTL | Ephemeral (lost on restart) | **TTL + pub/sub** — cache invalidation, event bus, session management |

### Deployment Mode → Driver Selection

| Deployment Mode | RecordStore driver | Metastore driver | ObjectStore driver | CacheStore driver |
|-----------------|-------------------|------------------|-------------------|-------------------|
| **Dev (single-node)** | SQLite | redb (local) | Local Disk | In-Memory (dict/DashMap) |
| **Production (single-node)** | PostgreSQL | redb (local) | S3 / Local | Dragonfly |
| **Production (multi-node)** | PostgreSQL | redb (Raft) | S3 | Dragonfly |

### Key Insights

1. **4 storage mediums, 1:1 with Four Pillars**: Orthogonality is between pillars (different query patterns), not between drivers within a pillar (same pattern, different operational profiles).

2. **Kernel needs exactly 2 mediums**: Ordered KV (Metastore) + Blob (ObjectStore). Services optionally add Relational ACID (RecordStore) and/or Ephemeral KV (CacheStore). Kernel is self-inclusive.

3. **Drivers are deployment-time config**: PostgreSQL vs SQLite, S3 vs Local Disk, Dragonfly vs In-Memory — all selected by deployment context, abstracted behind ABCs.

4. **3 driver merges**: Redis → Dragonfly (redundant), In-Memory dict + DashMap → single driver with engine selection, PostgreSQL + SQLite → conceptually one medium.

### Action Items

1. ✅ **Step 2 COMPLETE**: 4 orthogonal storage mediums verified (1:1 with Pillars)
2. ⚠️ **Deprecate Redis** (P2): Merge into Dragonfly driver (change connection string only)
3. ✅ **Kernel self-inclusiveness verified**: 2 mediums sufficient (Ordered KV + Blob)
4. ✅ **New principle**: Orthogonality = between pillars; drivers = within pillars

---

## THE NEXUS QUARTET: FOUR STORAGE PILLARS (Task #14)

**Design Decision**: NexusFS (nexus-core) abstracts storage by **Capability** (Access Pattern & Consistency Guarantee),
not by domain (`UserStore`) or implementation (`PostgresStore`).
Inspired by Linux Kernel's `BlockDevice`/`CharDevice`/`FileSystem` model.
Names explain the **"What"** and **"Why"**, not the **"How"**.

### The Four Pillars

| Pillar | ABC | Role | Backing Drivers | Kernel Status |
|--------|-----|------|-----------------|---------------|
| **Metastore** | `MetastoreABC` | "The Structure" — inodes, dentries, config, topology | redb (local PyO3 / gRPC Raft) | **Required** init param |
| **RecordStore** | `RecordStoreABC` | "The Truth" — entities, relationships, logs, vectors | PostgreSQL (prod), SQLite (dev) | **Optional** — injected for Services |
| **ObjectStore** | `ObjectStoreABC` (= current `Backend`) | "The Content" — raw file bytes, immutable objects | S3, GCS, Local Disk | **Mounted** dynamically (like Linux `mount`) |
| **CacheStore** | `CacheStoreABC` (in `contracts/cache_store.py`) | "The Reflexes" — sessions, signals, ephemeral data | Dragonfly (prod), In-Memory (dev) | **Optional** — ABC in contracts/ (like `include/linux/fscache.h`), kernel accepts via DI, services consume |

**Naming Note**: The Metastore pillar class is `MetastoreABC` (in `core/metastore.py`), providing typed FileMetadata CRUD.
Data classes (`FileMetadata`, `PaginatedResult`) live in `contracts/metadata.py`. Issue #1525 completed the rename.

### Complete Data Type → Pillar Mapping

**Metastore** (Ordered KV — redb) — 5 types:
| Data Type | Current Storage | From Part | Rationale |
|-----------|----------------|-----------|-----------|
| **FileMetadata** (+ merged FilePathModel, DirectoryEntryModel) | Generated dataclass / SQLAlchemy | Part 1 | Core file attributes, KV by path. Dir listing = prefix scan. |
| FileMetadataModel (custom KV) | SQLAlchemy | Part 1 | Arbitrary user metadata, KV by path_id + key |
| ContentChunkModel | SQLAlchemy | Part 2 | CAS dedup index, KV by content_hash (immutable, local only) |
| ReBACNamespaceModel | SQLAlchemy | Part 5 | Permission config, KV by namespace_id |
| SystemSettingsModel | SQLAlchemy | Part 13 | System config, KV by key |

**RecordStore** (Relational — PostgreSQL/SQLite) — 49 types:
| Category | Data Types | From Part | Rationale |
|----------|-----------|-----------|-----------|
| **Users & Auth** | UserModel, UserOAuthAccountModel, OAuthCredentialModel | Part 6 | FK, unique constraints, encryption |
| **ReBAC** | ReBACTupleModel, ReBACGroupClosureModel, ReBACChangelogModel | Part 5 | Composite indexes (SSOT), materialized view, append-only BRIN |
| **Memory System** | MemoryModel, **MemoryConfig**, TrajectoryModel, TrajectoryFeedbackModel, PlaybookModel | Part 4 | Vector search (pgvector), relational FK; MemoryConfig co-exists with MemoryModel |
| **Versioning** | VersionHistoryModel, WorkspaceSnapshotModel | Part 3 | Parent FK, BRIN time-series |
| **Semantic Search** | DocumentChunkModel | Part 10 | Vector index (pgvector/sqlite-vec) |
| **Workflows** | WorkflowModel, WorkflowExecutionModel | Part 9 | Version tracking, FK, BRIN |
| **Zones** | ZoneModel, EntityRegistryModel, ExternalUserServiceModel | Part 7 | Unique constraints, hierarchical FK, encryption |
| **Audit** | OperationLogModel | Part 11 | Append-only BRIN |
| **Sandboxes** | SandboxMetadataModel | Part 12 | Relational queries |
| **Path Registration** | **PathRegistrationModel** (NEW: WorkspaceConfig + MemoryConfig merged) | Part 15 | Co-exists with SnapshotModel/MemoryModel (cross-pillar principle) |
| **Governance** | AnomalyAlert, AgentBaseline, FraudRing, FraudScore, GovernanceNode, GovernanceEdge, SuspensionRecord, ThrottleConfig | Part 7 (Gov) | Composite PKs, zone-scoped filtering, time-range queries |
| **IPC** | IPCMessageModel | Part 16 | Binary payload + transactional upsert + unique constraint |
| **Agent Economy** | SpendingPolicyModel, SpendingLedgerModel, SpendingApprovalModel | Part 17 | ACID counters, workflow state machine, unique constraints |
| **Sharing** | ShareLinkModel, ShareLinkAccessLogModel | Part 18 | Partial index, FK cascade, mutable counters, audit |
| **Disputes** | DisputeModel | Part 19 | State machine, unique constraint, evidence hash |
| **Reputation** | ReputationEventModel, ReputationScoreModel | Part 19 | Immutable tamper-detected events, materialized leaderboard queries |
| **Token Security** | RefreshTokenHistoryModel, SecretsAuditLogModel | Part 20 | Security-critical ACID, tamper detection, replay prevention |
| **Sync** | SyncJobModel, SyncBacklogModel, BackendChangeLogModel, ConflictLogModel | Part 21 | Outbox pattern, delta sync state, conflict audit |
| **Upload** | UploadSessionModel | Part 22 | Atomic offset updates, restart-survivable session state |
| **Agent Lifecycle** | AgentRecordModel, AgentEventModel, DelegationRecordModel | Part 23 | Optimistic locking, append-only events, hierarchical delegation chain |

**ObjectStore** (= existing `Backend` ABC — S3/Local Disk) — 1 type:
| Data Type | From Part | Rationale |
|-----------|-----------|-----------|
| File Content (blobs) | Part 2 | Actual file bytes, petabyte scale, streaming I/O |

**CacheStore** (Ephemeral KV + Pub/Sub — Dragonfly / In-Memory) — 4 types:
| Data Type | Current Storage | From Part | Rationale |
|-----------|----------------|-----------|-----------|
| UserSessionModel | SQLAlchemy | Part 6 | Session tokens, pure KV with TTL (Step 3 decided) |
| PermissionCacheProtocol | Dragonfly/PostgreSQL/In-memory | Part 14 | Permission check cache, TTL |
| TigerCacheProtocol | Dragonfly/PostgreSQL | Part 14 | Pre-materialized bitmaps, TTL |
| FileEvent (pub/sub) | Dragonfly pub/sub | Part 8 | Ephemeral change notifications, pub/sub (Step 3 decided) |

### CacheStore Implementation Status

⚠️ **GAP**: Existing impls are scattered and lack in-memory fallbacks for kernel-only/dev mode:
- **EventBus**: `EventBusProtocol` (ABC), `RedisEventBus` (Dragonfly impl) — ❌ NO in-memory impl
- **PermissionCache**: `PermissionCacheProtocol` (ABC), `DragonflyPermissionCache`, `PostgresPermissionCache` — ❌ NO in-memory impl
- **TigerCache**: `TigerCacheProtocol` (ABC), `DragonflyTigerCache`, `PostgresTigerCache` — ❌ NO in-memory impl
- **UserSession**: Currently in SQLAlchemy — needs CacheStore migration + in-memory fallback

**Action (Task #22)**: Unify into `CacheStoreABC` with `InMemoryCacheStore` fallback for all 4 data types.

---

## PART 7: GOVERNANCE DATA (Issue #1359)

### 7.1 Anomaly Detection

| Data Type | Read | Write | Consistency | Query | Size | Card | Dur | Scope | Why Exists | Storage |
|-----------|------|-------|-------------|-------|------|------|-----|-------|------------|---------|
| **AnomalyAlert** | Med | Med | SC | Relational (zone+time range, severity filter) | Small | High | Persistent | Zone | Detect and record agent transaction anomalies (Z-score/IQR) | **RecordStore** ✅ |
| **AgentBaseline** | High | Low | SC | Relational (zone+agent composite PK) | Tiny | Med | Persistent | Zone | Store per-agent statistical baselines for anomaly detection | **RecordStore** ✅ |

### 7.2 Collusion & Fraud

| Data Type | Read | Write | Consistency | Query | Size | Card | Dur | Scope | Why Exists | Storage |
|-----------|------|-------|-------------|-------|------|------|-----|-------|------------|---------|
| **FraudRing** | Low | Low | SC | Relational (zone-scoped, detected_at range) | Med | Low | Persistent | Zone | Record detected fraud ring structures (cycle detection via Johnson's alg) | **RecordStore** ✅ |
| **FraudScore** | Med | Low | SC | Relational (zone+agent composite PK, score filter) | Tiny | Med | Persistent | Zone | Per-agent EigenTrust-derived fraud scores for Sybil detection | **RecordStore** ✅ |

### 7.3 Governance Graph

| Data Type | Read | Write | Consistency | Query | Size | Card | Dur | Scope | Why Exists | Storage |
|-----------|------|-------|-------------|-------|------|------|-----|-------|------------|---------|
| **GovernanceNode** | High | Low | SC | Relational (unique constraint on zone+node_id) | Tiny | Med | Persistent | Zone | Represent agents/resources as graph nodes for constraint checking | **RecordStore** ✅ |
| **GovernanceEdge** | High | Low | SC | Relational (composite index zone+from+to, constraint type filter) | Tiny | High | Persistent | Zone | Directed edges with constraint types (ALLOW/DENY/RATE_LIMIT) between nodes | **RecordStore** ✅ |

### 7.4 Response Actions

| Data Type | Read | Write | Consistency | Query | Size | Card | Dur | Scope | Why Exists | Storage |
|-----------|------|-------|-------------|-------|------|------|-----|-------|------------|---------|
| **SuspensionRecord** | Med | Low | SC | Relational (zone+agent, active filter, appeal status) | Small | Low | Persistent | Zone | Track agent suspensions with appeal workflow (7-day expiry) | **RecordStore** ✅ |
| **ThrottleConfig** | High | Low | SC | Relational (zone+agent lookup) | Tiny | Low | Persistent | Zone | Per-agent rate limiting configs (requests/window, triggered by anomaly threshold) | **RecordStore** ✅ |

**Affinity rationale:** All governance data types require relational queries (composite PKs, zone-scoped filtering, time-range queries, JOINs between alerts↔baselines). RecordStore (PostgreSQL) is the correct pillar. Process-local TTL caches in `GovernanceGraphService` and `AnomalyService` are optimization caches, not storage-tier — same pattern as `CompactFileMetadata`.

---

## PART 16: IPC DATA

| Data Type | Read | Write | Consistency | Query | Size | Card | Dur | Scope | Why Exists | Current Storage | Optimal Storage | Action |
|-----------|------|-------|-------------|-------|------|------|-----|-------|------------|----------------|-----------------|--------|
| **IPCMessageModel** | Med | Med | SC | KV-like (UPSERT by zone_id + path, dir listing by dir_path) | Medium (LargeBinary payload) | High | Persistent | Zone | IPC message persistence with VFS-style path semantics | SQLAlchemy | **Keep RecordStore** (binary payload + transactional upsert + unique constraint) | ✅ KEEP |

**Analysis:**
- Path-keyed with zone_id isolation, LargeBinary `data` column, UNIQUE constraint on `(zone_id, path)`
- Access patterns: upsert by zone+path, list/count by zone+dir_path
- Despite KV-like access, the binary payload and transactional upsert semantics justify RecordStore
- If payloads grow large, consider splitting: Metastore (path index) + ObjectStore (payload)

---

## PART 17: AGENT ECONOMY

| Data Type | Read | Write | Consistency | Query | Size | Card | Dur | Scope | Why Exists | Current Storage | Optimal Storage | Action |
|-----------|------|-------|-------------|-------|------|------|-----|-------|------------|----------------|-----------------|--------|
| **SpendingPolicyModel** | Med | Low | SC | Relational (UNIQUE agent_id+zone_id, priority index) | Small | Low | Persistent | Zone | Agent spending limits and policy rules (daily/weekly/monthly) | SQLAlchemy | **Keep RecordStore** (unique constraint, priority ordering) | ✅ KEEP |
| **SpendingLedgerModel** | Med | High | SC | Relational (UPSERT by agent+zone+period+start, atomic counter) | Tiny | Med | Persistent | Zone | Period-based spending counters, atomically updated on each transfer | SQLAlchemy | **Keep RecordStore** (ACID required for accurate spend tracking) | ✅ KEEP |
| **SpendingApprovalModel** | Med | Med | SC | Relational (status filter, agent+zone index) | Small | Med | Persistent | Zone | Approval workflow state machine (pending→approved/rejected/expired) | SQLAlchemy | **Keep RecordStore** (workflow state machine, mutable status) | ✅ KEEP |

**Analysis:**
- All three form a cohesive agent economy subsystem: Policy (config) → Ledger (counters) → Approval (workflow)
- SpendingLedgerModel is the most critical: atomic UPSERT prevents double-counting credits
- **Action**: Keep all in RecordStore — ACID transactions required for financial correctness

---

## PART 18: SHARING & CAPABILITY LINKS

| Data Type | Read | Write | Consistency | Query | Size | Card | Dur | Scope | Why Exists | Current Storage | Optimal Storage | Action |
|-----------|------|-------|-------------|-------|------|------|-----|-------|------------|----------------|-----------------|--------|
| **ShareLinkModel** | Med | Low | SC | Relational (resource index, partial index on active links, Argon2 password hash) | Small | Med | Persistent | Zone | Capability URL share links with optional password, TTL, and access limits | SQLAlchemy | **Keep RecordStore** (partial index, mutable counters, revocation state) | ✅ KEEP |
| **ShareLinkAccessLogModel** | Low | Med | EC | Relational (FK to share_links CASCADE, time-series by link_id+accessed_at) | Tiny | High | Archive | Zone | Append-only access audit log for share links | SQLAlchemy | **Keep RecordStore** (FK cascade, append-only audit) | ✅ KEEP |

**Analysis:**
- ShareLinkModel has mutable state (access_count, revoked_at) and partial index for active links
- AccessLog is append-only audit with FK cascade — deletion of parent link cascades to access records
- **Action**: Keep both in RecordStore

---

## PART 19: DISPUTES & REPUTATION

### 19.1 Disputes

| Data Type | Read | Write | Consistency | Query | Size | Card | Dur | Scope | Why Exists | Current Storage | Optimal Storage | Action |
|-----------|------|-------|-------------|-------|------|------|-----|-------|------------|----------------|-----------------|--------|
| **DisputeModel** | Med | Low | SC | Relational (UNIQUE exchange_id, status+zone index, parties index) | Small | Low | Persistent | Zone | Agent dispute resolution workflow (filed→auto_mediating→resolved/dismissed) | SQLAlchemy | **Keep RecordStore** (state machine, unique constraint, evidence hash) | ✅ KEEP |

### 19.2 Reputation

| Data Type | Read | Write | Consistency | Query | Size | Card | Dur | Scope | Why Exists | Current Storage | Optimal Storage | Action |
|-----------|------|-------|-------------|-------|------|------|-----|-------|------------|----------------|-----------------|--------|
| **ReputationEventModel** | Low | Med | EC | Relational (composite PK for partitioning, BRIN on zone+created_at, UNIQUE exchange+rater) | Small | Very High | Archive | Zone | Immutable reputation events with tamper-detection hash (SHA-256 record_hash) | SQLAlchemy | **Keep RecordStore** (immutable append-only, tamper-detected, partition-ready composite PK) | ✅ KEEP |
| **ReputationScoreModel** | High | Med | EC | Relational (composite PK agent+context+window, leaderboard index on zone+context+score) | Tiny | Med | Persistent | Zone | Materialized aggregate of reputation events (Bayesian beta distribution scores) | SQLAlchemy | **Keep RecordStore** (leaderboard composite queries, zone-scoped ordered reads) | ✅ KEEP |

**Analysis:**
- **DisputeModel**: State machine with SHA-256 evidence hash — requires durable, transactional storage
- **ReputationEventModel**: Immutable, append-only with tamper detection — partition-ready composite PK `(id, created_at)`
- **ReputationScoreModel**: While derived from events (could theoretically be CacheStore), it has complex composite PK, zone-scoped leaderboard queries requiring composite indexes, and Bayesian alpha/beta parameters that are incrementally updated. RecordStore is correct — the leaderboard query pattern is relational, not KV
- **No merges needed** — different lifecycles (events are immutable, scores are mutable materialized views)

---

## PART 20: TOKEN SECURITY & AUDIT

| Data Type | Read | Write | Consistency | Query | Size | Card | Dur | Scope | Why Exists | Current Storage | Optimal Storage | Action |
|-----------|------|-------|-------------|-------|------|------|-----|-------|------------|----------------|-----------------|--------|
| **RefreshTokenHistoryModel** | Med | Med | SC | Relational (family+hash lookup, family+rotated_at for pruning) | Tiny | High | Persistent | Zone | Retired refresh token hashes for replay detection per RFC 9700 | SQLAlchemy | **Keep RecordStore** (ACID required for security-critical replay detection) | ✅ KEEP |
| **SecretsAuditLogModel** | Low | Med | SC | Relational (composite PK for partitioning, BRIN-like time queries, tamper-detection hash) | Small | Very High | Archive | Zone | Immutable secrets access audit trail with SHA-256 tamper detection | SQLAlchemy | **Keep RecordStore** (immutable, tamper-detected, ORM rejects UPDATE/DELETE) | ✅ KEEP |

**Analysis:**
- Both are security-critical with ACID requirements
- **RefreshTokenHistoryModel**: Used for token theft detection — if a retired hash is reused, the entire token family is revoked. Rows are pruned by age (not TTL auto-eviction). ACID required.
- **SecretsAuditLogModel**: Partition-ready composite PK `(id, created_at)`, SHA-256 `record_hash` for tamper detection, ORM-level guards reject UPDATE/DELETE. Same pattern as ReputationEventModel.
- **Action**: Keep both in RecordStore — security guarantees require ACID persistence

---

## PART 21: SYNC & CHANGE TRACKING

| Data Type | Read | Write | Consistency | Query | Size | Card | Dur | Scope | Why Exists | Current Storage | Optimal Storage | Action |
|-----------|------|-------|-------------|-------|------|------|-----|-------|------------|----------------|-----------------|--------|
| **SyncJobModel** | Med | Med | EC | Relational (status index, mount_point index, BRIN on created_at) | Small | Med | Persistent | Zone | Long-running sync job state tracking with progress percentage | SQLAlchemy | **Keep RecordStore** (mutable job state, progress updates) | ✅ KEEP |
| **SyncBacklogModel** | Med | High | SC | Relational (status+created_at index, UNIQUE path+backend+zone+status, BRIN) | Small | High | Persistent | Zone | Work queue / outbox for write-back sync operations with retry logic | SQLAlchemy | **Keep RecordStore** (outbox pattern, retry+status state machine, ACID required) | ✅ KEEP |
| **BackendChangeLogModel** | Med | Med | EC | Relational (UNIQUE path+backend+zone, BRIN on synced_at) | Small | High | Persistent | Zone | Delta sync state: one row per (path, backend, zone), upserted on each sync | SQLAlchemy | **Keep RecordStore** (upsert state tracking, composite unique constraint) | ✅ KEEP |
| **ConflictLogModel** | Low | Med | EC | Relational (status+created_at index, BRIN on created_at) | Small | Med | Archive | Zone | Append-only conflict resolution audit log | SQLAlchemy | **Keep RecordStore** (append-only audit, BRIN optimized) | ✅ KEEP |

**Analysis:**
- Cohesive sync subsystem: Job (orchestration) → Backlog (outbox) → ChangeLog (state) → ConflictLog (audit)
- **SyncBacklogModel** is essentially a transactional outbox/work-queue: ACID required to prevent duplicate processing
- All use BRIN indexes for append-biased time-series queries
- **Action**: Keep all in RecordStore

---

## PART 22: UPLOAD SESSIONS

| Data Type | Read | Write | Consistency | Query | Size | Card | Dur | Scope | Why Exists | Current Storage | Optimal Storage | Action |
|-----------|------|-------|-------------|-------|------|------|-----|-------|------------|----------------|-----------------|--------|
| **UploadSessionModel** | Med | Med | SC | Relational (status index, expires_at index, zone+user index) | Small | Med | Session | Zone | Chunked upload session state (tus protocol) with atomic offset tracking | SQLAlchemy | **Keep RecordStore** (ACID required for atomic offset updates, restart-survivable) | ✅ KEEP |

**Analysis:**
- Has `expires_at` (TTL-like) and is session-scoped — superficially CacheStore-like
- However: must survive server restarts (upload can resume after crash), requires atomic `upload_offset` updates to prevent data corruption, and tracks multi-part state
- GC of expired sessions is handled by periodic cleanup, not TTL auto-eviction
- **Action**: Keep in RecordStore — restart survival + atomic offset updates outweigh TTL convenience

---

## PART 23: AGENT LIFECYCLE

| Data Type | Read | Write | Consistency | Query | Size | Card | Dur | Scope | Why Exists | Current Storage | Optimal Storage | Action |
|-----------|------|-------|-------------|-------|------|------|-----|-------|------------|----------------|-----------------|--------|
| **AgentRecordModel** | High | Med | SC | Relational (zone+state index, state+heartbeat index, owner index) | Small | Med | Persistent | Zone | Agent lifecycle state with optimistic locking (generation counter) and heartbeat | SQLAlchemy | **Keep RecordStore** (optimistic locking, mutable lifecycle state, heartbeat) | ✅ KEEP |
| **AgentEventModel** | Low | High | EC | Relational (agent+created_at index, event_type index) | Tiny | Very High | Archive | Zone | Append-only agent lifecycle event log | SQLAlchemy | **Keep RecordStore** (append-only event log, agent traceability) | ✅ KEEP |
| **DelegationRecordModel** | Med | Low | SC | Relational (agent/parent indexes, status index, parent_delegation chain, lease TTL) | Small | Med | Persistent | Zone | Hierarchical delegation chain with lease TTL and sub-delegation tracking | SQLAlchemy | **Keep RecordStore** (hierarchical chain, mutable status, lease management) | ✅ KEEP |

**Analysis:**
- **AgentRecordModel**: Natural string PK (`agent_id`), optimistic locking via `generation` counter, frequent heartbeat updates. RecordStore for transactional state machine.
- **AgentEventModel**: High-volume append-only log. Could potentially move to a dedicated event store at scale, but RecordStore with BRIN-style indexes is adequate.
- **DelegationRecordModel**: Hierarchical chain (`parent_delegation_id`, `depth`, `can_sub_delegate`). Has TTL semantics (`lease_expires_at`) but requires transactional chain integrity — not CacheStore.
- **No merges needed** — distinct lifecycles (state vs events vs delegations)

---

## NEXT STEPS

Completed items removed. See SUMMARY "REMAINING GAPS" for open tasks, REDUNDANCY ANALYSIS for merge decisions.

1. ✅ **DONE**: Renamed `FileMetadataProtocol` → `MetastoreABC`, extracted to `core/metastore.py` (Issue #1525)
2. ❓ **DECISION**: Version history (VersionHistoryGC, TimeTravelReader) — kernel or services? (Related: Task #3, #11)

---

**END OF DATA-STORAGE-MATRIX.MD**
