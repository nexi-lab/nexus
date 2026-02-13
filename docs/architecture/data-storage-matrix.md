# Data-to-Storage Properties Matrix

**Date:** 2026-02-12 (updated: sled → redb throughout)
**Status:** Steps 1-3 COMPLETE — All Data-Storage Affinity Decisions Resolved
**Purpose:** Catalog ALL data types in Nexus and determine optimal storage for each

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
| **PermissionCacheProtocol** | Critical | Med | EC | KV (by cache key) | Tiny | Very High | Session | Zone | Permission check result cache (avoid ReBAC recomputation) | Dragonfly/PostgreSQL/In-memory | **Dragonfly** (in-memory, TTL) | ✅ KEEP |
| **TigerCacheProtocol** | Critical | Low | EC | KV (by object_id → bitmap) | Small | High | Session | Zone | Pre-materialized permission bitmaps for O(1) filtering | Dragonfly/PostgreSQL | **Dragonfly** (in-memory, fast bitmap ops) | ✅ KEEP |

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

### ✅ **Keep SQLAlchemy (PostgreSQL/SQLite) = RecordStore** - 22 types (was 20, +2 from co-existence moves)
Relational queries, FK, unique constraints, vector search, encryption, BRIN indexes

| Category | Data Types | Rationale |
|----------|-----------|-----------|
| **Users & Auth** | UserModel, UserOAuthAccountModel, OAuthCredentialModel | Relational queries, FK, unique constraints, encryption |
| **ReBAC (Partial)** | ReBACGroupClosureModel, ReBACChangelogModel | Materialized view, append-only BRIN |
| **Memory System** | MemoryModel, **MemoryConfig**, TrajectoryModel, TrajectoryFeedbackModel, PlaybookModel | Complex relational + vector search; MemoryConfig co-exists with MemoryModel |
| **Versioning** | VersionHistoryModel, WorkspaceSnapshotModel | Parent FK, BRIN time-series |
| **Semantic Search** | DocumentChunkModel | Vector index (pgvector/sqlite-vec) |
| **Workflows** | WorkflowModel, WorkflowExecutionModel | Version tracking, FK, BRIN |
| **Zones** | ZoneModel, EntityRegistryModel, ExternalUserServiceModel | Unique constraints, hierarchical FK, encryption |
| **Audit** | OperationLogModel | Append-only BRIN |
| **Sandboxes** | SandboxMetadataModel | Relational queries |
| **Path Registration** | **PathRegistrationModel** (NEW: WorkspaceConfig + MemoryConfig merged) | Co-exists with SnapshotModel/MemoryModel |

### ✅ **Metastore (Ordered KV — redb via Raft)** — 4 surviving types
KV access pattern, strong consistency needed (multi-node)

| Data Type | Current | Reason |
|-----------|---------|--------|
| **FileMetadata** (proto) + ~~FilePathModel~~ + ~~DirectoryEntryModel~~ | Generated dataclass / SQLAlchemy | Core metadata KV by path; FilePathModel + DirectoryEntry merged in; dir listing = prefix scan |
| FileMetadataModel (custom KV) | SQLAlchemy | Arbitrary KV metadata by path_id + key |
| ReBACNamespaceModel | SQLAlchemy | KV by namespace_id, low cardinality |
| SystemSettingsModel | SQLAlchemy | KV by key, low cardinality |

### ✅ **Migrate to redb (local, no Raft)** - 1 type
CAS (content-addressed), immutable

| Data Type | Current | Reason |
|-----------|---------|--------|
| ContentChunkModel | SQLAlchemy | KV by content_hash, immutable (no SC needed) |

### ✅ **CacheStore (Ephemeral KV — Dragonfly / In-Memory)** — 4 types
Performance cache, TTL, pub/sub

| Data Type | Current | Reason |
|-----------|---------|--------|
| PermissionCacheProtocol | Dragonfly/PostgreSQL/In-memory | Permission check cache, TTL |
| TigerCacheProtocol | Dragonfly/PostgreSQL | Pre-materialized bitmaps, TTL |
| **FileEvent** (pub/sub) | Dragonfly pub/sub | Ephemeral change notifications, pub/sub native |
| **UserSessionModel** | SQLAlchemy | Pure KV with TTL, no relational features needed |

### ✅ **Step 1 DECISIONS RESOLVED**

1. ✅ **FilePathModel → FileMetadata**: MERGE confirmed, deprecate relational model
2. ✅ **ContentCacheModel**: ELIMINATE DB metadata, pure disk cache
3. ✅ **Cluster Topology**: ELIMINATED as standalone type (inherent in Raft layer)
4. ✅ **WorkspaceConfig + MemoryConfig**: MERGE into PathRegistrationModel (RecordStore)
5. ✅ **MemoryConfig pillar**: STAY RecordStore (cross-pillar co-existence principle)
6. ✅ **ReBAC 4 types**: No merges needed (Zanzibar-correct)
7. ✅ **User/Auth types**: No merges needed
8. ✅ **Events/Subscriptions**: No merges, co-locate Subscription+Delivery in RecordStore

### ✅ **Step 3 DECISIONS RESOLVED (Affinity Matching)**

1. ✅ **ReBACTupleModel → RecordStore** (SSOT) + CacheStore (hot path). Composite indexes are dominant requirement; hot-path latency covered by TigerCache/PermissionCache.
2. ✅ **FileEvent → CacheStore** (pub/sub). Ephemeral, fire-and-forget; missed events recoverable from Metastore SSOT.
3. ✅ **UserSessionModel → CacheStore**. Pure KV with TTL, no relational features needed.
4. ✅ **DirectoryEntryModel → MERGE into FileMetadata** (Metastore). Prefix scan replaces sparse index; one less data type.

### ⚠️ **Architecture Risks Identified (Step 3)**

1. **CacheStore dependency for permissions**: ReBAC hot path (TigerCache, PermissionCache) depends on CacheStore. If CacheStore unavailable, falls back to ~1ms SQL (RecordStore). Acceptable — optimization, not correctness.
2. **Missing InMemoryEventBus**: EventBusProtocol has Dragonfly impl but NO in-memory impl. Kernel-only/dev mode has no event bus. Need `InMemoryEventBus` for CacheStoreABC.
3. **Missing InMemory impls**: PermissionCache and TigerCache also lack in-memory impls. Same CacheStoreABC gap.

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
| **CacheStore** | `CacheStoreABC` (future) | "The Reflexes" — sessions, signals, ephemeral data | Dragonfly (prod), In-Memory (dev) | **Future** — optional |

**Naming Note**: The existing proto-generated `MetadataStore` (specific to `FileMetadata` typed operations)
will be renamed to `FileMetadataProtocol` to avoid confusion with `MetastoreABC` (the underlying ordered KV primitive).
`MetastoreABC` is the lower-level KV store; `FileMetadataProtocol` is a typed wrapper that sits on top of it.

### Complete Data Type → Pillar Mapping

**Metastore** (Ordered KV — redb):
| Data Type | From Part | Rationale |
|-----------|-----------|-----------|
| **FileMetadata** (+ merged FilePathModel, DirectoryEntryModel) | Part 1 | Core file attributes, KV by path. Dir listing = prefix scan. |
| FileMetadataModel (custom KV) | Part 1 | Arbitrary user metadata, KV by path_id + key |
| ContentChunkModel | Part 2 | CAS dedup index, KV by content_hash (immutable, local only) |
| ReBACNamespaceModel | Part 5 | Permission config, KV by namespace_id |
| SystemSettingsModel | Part 13 | System config, KV by key |

**RecordStore** (Relational — PostgreSQL/SQLite):
| Data Type | From Part | Rationale |
|-----------|-----------|-----------|
| UserModel, UserOAuthAccountModel, OAuthCredentialModel | Part 6 | FK, unique constraints, encryption |
| ReBACTupleModel, ReBACGroupClosureModel, ReBACChangelogModel | Part 5 | Composite indexes, materialized view, BRIN |
| MemoryModel, MemoryConfig, TrajectoryModel, TrajectoryFeedbackModel, PlaybookModel | Part 4 | Vector search (pgvector), relational FK; MemoryConfig co-exists with MemoryModel |
| VersionHistoryModel, WorkspaceSnapshotModel | Part 3 | Parent FK, BRIN time-series |
| DocumentChunkModel | Part 10 | Vector index (pgvector/sqlite-vec) |
| WorkflowModel, WorkflowExecutionModel | Part 9 | Version tracking, FK, BRIN |
| ZoneModel, EntityRegistryModel, ExternalUserServiceModel | Part 7 | Unique constraints, hierarchical FK |
| OperationLogModel | Part 11 | Append-only BRIN |
| SandboxMetadataModel | Part 12 | Relational queries |
| **PathRegistrationModel** (NEW: merged WorkspaceConfig + MemoryConfig) | Part 15 | Co-exists with SnapshotModel/MemoryModel (cross-pillar principle) |

**ObjectStore** (= existing `Backend` ABC — S3/Local Disk):
| Data Type | From Part | Rationale |
|-----------|-----------|-----------|
| File Content (blobs) | Part 2 | Actual file bytes, petabyte scale, streaming I/O |

**CacheStore** (Ephemeral KV + Pub/Sub — Dragonfly / In-Memory):
| Data Type | From Part | Rationale |
|-----------|-----------|-----------|
| UserSessionModel | Part 6 | Session tokens, pure KV with TTL (Step 3 decided) |
| PermissionCacheProtocol | Part 14 | Permission check cache, TTL |
| TigerCacheProtocol | Part 14 | Pre-materialized bitmaps, TTL |
| FileEvent (pub/sub) | Part 8 | Ephemeral change notifications, pub/sub (Step 3 decided) |

### CacheStore Implementation Status

⚠️ **GAP**: Existing impls are scattered and lack in-memory fallbacks for kernel-only/dev mode:
- **EventBus**: `EventBusProtocol` (ABC), `RedisEventBus` (Dragonfly impl) — ❌ NO in-memory impl
- **PermissionCache**: `PermissionCacheProtocol` (ABC), `DragonflyPermissionCache`, `PostgresPermissionCache` — ❌ NO in-memory impl
- **TigerCache**: `TigerCacheProtocol` (ABC), `DragonflyTigerCache`, `PostgresTigerCache` — ❌ NO in-memory impl
- **UserSession**: Currently in SQLAlchemy — needs CacheStore migration + in-memory fallback

**Action (Task #22)**: Unify into `CacheStoreABC` with `InMemoryCacheStore` fallback for all 4 data types.

**Future work**: Unify these into a single `CacheStoreABC` with `InMemoryCacheStore` fallback.

---

## NEXT STEPS

1. ✅ Review this matrix with user
2. ✅ **Step 1+2+3 COMPLETE**: All data-storage affinity decisions resolved
3. ❓ Identify missing Subscription/Delivery storage (Task #12)
4. ❓ Clarify Dragonfly status post-Raft
5. ✅ Merge redundant data types (FilePathModel → FileMetadata, WorkspaceConfig + MemoryConfig → PathRegistrationModel)
6. ✅ Rewrite federation-memo.md with this data architecture
7. ✅ Storage medium orthogonality analysis complete — Redis deprecation identified (P2)
8. ✅ "Nexus Quartet" — Four Pillars abstraction design decided (Metastore, RecordStore, ObjectStore, CacheStore)
9. ✅ **COMPLETE**: Task #14 — MetastoreABC + RecordStoreABC in NexusFS constructor (Four Pillars DI)
10. 📋 **PLANNED**: Rename proto-generated `MetadataStore` → `FileMetadataProtocol` (avoid confusion with MetastoreABC)
11. ✅ **COMPLETE**: CI PyO3 build for nexus_raft (#1234)
12. ❓ **DECISION**: Version history (VersionHistoryGC, TimeTravelReader) — kernel or services? (Related: Task #3, #11)
13. 🆕 **PRINCIPLE**: Cross-pillar co-existence — if a config only serves data in another pillar, it belongs in that pillar
14. ✅ **Step 2 COMPLETE**: 4 orthogonal storage mediums = 4 Pillars. Redis deprecated. In-Memory merged.
15. ✅ **Step 3 COMPLETE**: ReBACTuple→RecordStore, FileEvent→CacheStore, UserSession→CacheStore, DirectoryEntry→merged into FileMetadata
16. ⚠️ **GAP**: CacheStoreABC needs InMemory impls (EventBus, PermissionCache, TigerCache) for kernel-only/dev mode

---

**END OF DATA-STORAGE-MATRIX.MD**
