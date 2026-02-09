# Data-to-Storage Properties Matrix

**Date:** 2026-02-09
**Status:** Step 1 — Data Layer Review (Data-Storage Affinity Analysis)
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
| **FilePathModel** | High | Med | SC (multi) / Local (single) | Relational (JOIN on zone_id, FK lookups) | Small | Very High | Persistent | Zone | Map virtual path → backend location; support multi-backend | SQLAlchemy | **Keep SQLAlchemy** (relational queries needed) OR **Migrate to sled via Raft** (if we can flatten JOINs) | 🤔 DECISION NEEDED |
| **FileMetadata** (proto) | High | Med | SC (multi) / Local (single) | KV (by path) | Small | Very High | Persistent | Zone | Core file attributes (size, etag, timestamps) | Generated proto → Python dataclass | **sled via Raft** (KV-friendly, SC via Raft) | ✅ MIGRATE |
| **CompactFileMetadata** | Critical | Med | SC | KV | Tiny | Very High | Session | Zone | Memory-optimized metadata for L1 cache | In-memory (string interning) | **In-memory only** (cache layer) | ✅ KEEP |

**Analysis:**
- **FilePathModel vs FileMetadata**: REDUNDANT! Both store file metadata.
  - FilePathModel has FK to zone, relational structure
  - FileMetadata is proto-generated, KV-style
  - **Merge decision**: Migrate FilePathModel → FileMetadata in sled, deprecate relational model
- **CompactFileMetadata**: Pure cache optimization, not persistent storage

### 1.2 Directory Indexing

| Data Type | Read | Write | Consistency | Query | Size | Card | Dur | Scope | Why Exists | Current Storage | Optimal Storage | Action |
|-----------|------|-------|-------------|-------|------|------|-----|-------|------------|----------------|-----------------|--------|
| **DirectoryEntryModel** | High | Low | SC | KV (by parent_path) | Small | High | Persistent | Zone | Sparse directory index for O(1) non-recursive `ls` | SQLAlchemy | **sled via Raft** (KV access pattern) | ✅ MIGRATE |

**Analysis:**
- Currently uses SQLAlchemy but access pattern is pure KV (lookup by parent_path)
- No JOINs needed → ideal for sled
- **Action**: Migrate to sled

### 1.3 Custom Metadata

| Data Type | Read | Write | Consistency | Query | Size | Card | Dur | Scope | Why Exists | Current Storage | Optimal Storage | Action |
|-----------|------|-------|-------------|-------|------|-----|-------|------------|----------------|-----------------|--------|
| **FileMetadataModel** (KV) | Med | Low | EC | KV (by path_id + key) | Small | Med | Persistent | Zone | Arbitrary user-defined metadata (tags, custom fields) | SQLAlchemy | **sled via Raft** (KV) | ✅ MIGRATE |

**Analysis:**
- Pure KV access (lookup by path_id + key)
- No relational queries
- **Action**: Migrate to sled

---

## PART 2: CONTENT & DEDUPLICATION

| Data Type | Read | Write | Consistency | Query | Size | Card | Dur | Scope | Why Exists | Current Storage | Optimal Storage | Action |
|-----------|------|-------|-------------|-------|------|------|-----|-------|------------|----------------|-----------------|--------|
| **ContentChunkModel** | Med | Low | EC | KV (by content_hash) | Small | High | Persistent | System | CAS (Content-Addressed Storage) for deduplication; track refcount | SQLAlchemy | **sled** (KV by hash, no Raft needed for CAS) | ✅ MIGRATE |
| **File Content (blobs)** | Med | Low | EC | Blob (by path) | Huge | Very High | Persistent | Zone | Actual file data | Disk / S3 / GCS | **Keep Disk/S3** (blob storage) | ✅ KEEP |
| **ContentCacheModel** | Med | Low | EC | KV (by path_id) | Large | High | Session | Zone | Parsed content cache (avoid re-parsing) | SQLAlchemy + Disk | **Disk only** (binary cache, no DB metadata needed) | 🤔 SIMPLIFY |

**Analysis:**
- **ContentChunkModel**: Pure CAS, no strong consistency needed (content-addressed is immutable)
  - **Action**: Move to sled (no Raft, just local KV)
- **ContentCacheModel**: Can we eliminate DB metadata and just use disk cache with TTL?
  - **Action**: Simplify to pure disk cache

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
| **MemoryConfig** | Low | Low | EC | KV (by path) | Tiny | Low | Persistent | Zone | Memory directory configuration | In-memory + SQLAlchemy | **sled** (simple KV) | ✅ MIGRATE |
| **TrajectoryModel** | Low | Med | EC | Relational (FK to agent, task) | Small | High | Persistent | Agent | Task execution traces for ACE learning | Inferred (implicit in memory system) | **Keep SQLAlchemy** (relational) | ✅ KEEP |
| **TrajectoryFeedbackModel** | Low | Low | EC | Relational (FK to trajectory) | Small | Med | Persistent | Agent | Feedback on trajectories | SQLAlchemy | **Keep SQLAlchemy** (FK to trajectory) | ✅ KEEP |
| **PlaybookModel** | Low | Low | EC | Relational (FK to strategies) | Medium | Med | Persistent | Agent | Strategy playbooks | Inferred (API models) | **Keep SQLAlchemy** (relational) | ✅ KEEP |

**Analysis:**
- **MemoryModel**: Extremely complex with vector search, entity extraction, relational queries → MUST stay in PostgreSQL (pgvector)
- **MemoryConfig**: Simple KV → migrate to sled
- Trajectory/Playbook: Relational → keep SQLAlchemy

---

## PART 5: ACCESS CONTROL (ReBAC)

| Data Type | Read | Write | Consistency | Query | Size | Card | Dur | Scope | Why Exists | Current Storage | Optimal Storage | Action |
|-----------|------|-------|-------------|-------|------|------|-----|-------|------------|----------------|-----------------|--------|
| **ReBACTupleModel** | Critical | Low | SC | Relational (composite index on subject/relation/object) | Tiny | Very High | Persistent | Zone | Zanzibar-style relationship tuples (user:alice#member@group:eng) | SQLAlchemy with composite indexes | **Keep SQLAlchemy** (critical path, needs composite indexes) OR **Migrate to sled with custom indexes** | 🤔 DECISION NEEDED |
| **ReBACNamespaceModel** | Med | Low | EC | KV (by namespace_id) | Small | Low | Persistent | System | Permission expansion rules (namespace config) | SQLAlchemy | **sled** (KV, low cardinality) | ✅ MIGRATE |
| **ReBACGroupClosureModel** | Critical | Low | SC | Relational (composite index on member/group) | Tiny | Very High | Persistent | Zone | Leopard-style transitive closure for O(1) group membership | SQLAlchemy with composite indexes | **Keep SQLAlchemy** (critical path, materialized view) | ✅ KEEP |
| **ReBACChangelogModel** | Low | Med | EC | Relational (BRIN index on created_at) | Small | High | Archive | Zone | Audit log for tuple modifications | SQLAlchemy with BRIN | **Keep SQLAlchemy** (append-only, BRIN optimized) | ✅ KEEP |

**Analysis:**
- **ReBACTupleModel**: Critical read path, needs composite indexes
  - **Question**: Can sled support composite indexes efficiently?
  - **Decision**: If yes, migrate; if no, keep SQLAlchemy
- **ReBACGroupClosureModel**: Materialized view for O(1) lookups → keep SQLAlchemy
- **ReBACChangelogModel**: Append-only audit log with BRIN → keep SQLAlchemy

---

## PART 6: USERS & AUTHENTICATION

| Data Type | Read | Write | Consistency | Query | Size | Card | Dur | Scope | Why Exists | Current Storage | Optimal Storage | Action |
|-----------|------|-------|-------------|-------|------|------|-----|-------|------------|----------------|-----------------|--------|
| **UserModel** | Med | Low | SC | Relational (JOIN on zone_id, email lookup) | Small | Med | Persistent | System | Core user accounts with soft delete | SQLAlchemy with soft delete | **Keep SQLAlchemy** (relational queries) | ✅ KEEP |
| **UserOAuthAccountModel** | Med | Low | SC | Relational (FK to user_id, unique constraint on provider+provider_user_id) | Small | Med | Persistent | System | OAuth provider accounts for SSO login | SQLAlchemy | **Keep SQLAlchemy** (FK, unique constraints) | ✅ KEEP |
| **OAuthCredentialModel** | Med | Low | SC | Relational (FK to user_id, zone_id, encrypted tokens) | Small | Med | Persistent | Zone | OAuth tokens for backend integrations (Google Drive, OneDrive) | SQLAlchemy with encryption | **Keep SQLAlchemy** (FK, encryption) | ✅ KEEP |
| **UserSessionModel** | High | Med | EC | KV (by session_id) | Tiny | High | Session | System | Active user sessions | SQLAlchemy | **Redis/Dragonfly** (session cache, TTL) | 🔄 MIGRATE |

**Analysis:**
- User/OAuth models need relational queries → keep SQLAlchemy
- **UserSessionModel**: Pure KV with TTL → migrate to Redis/Dragonfly (session cache)

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
| **FileEvent** | N/A | High | EC | Pub/Sub | Tiny | N/A | Ephemeral | Zone | File change notifications (write, delete, rename) | In-memory → Dragonfly pub/sub | **Raft event log?** OR **Keep Dragonfly pub/sub** | 🤔 DECISION NEEDED |
| **SubscriptionCreate/Update** | Med | Low | EC | Relational (FK to zone, query by event_types) | Small | Low | Persistent | Zone | Webhook subscription config | Pydantic (API only, no DB model found) | **Need SQLAlchemy model?** | ❓ MISSING |
| **WebhookDelivery** | Low | Med | EC | Relational (BRIN on created_at) | Small | High | Archive | Zone | Webhook delivery attempt history | Pydantic (API only) | **Need SQLAlchemy model?** | ❓ MISSING |

**Analysis:**
- **FileEvent**: Currently Dragonfly pub/sub
  - **Question**: After Raft migration, should events go through Raft event log (SC) or keep Dragonfly (EC)?
  - **Issue**: User mentioned Dragonfly might be broken after Raft migration
  - **Decision**: Need to clarify event bus architecture
- **Subscription/Delivery models**: Pydantic models exist but no DB storage found
  - **Action**: Need to implement SQLAlchemy models if webhooks are persistent

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
| **SystemSettingsModel** | Med | Low | SC | KV (by key) | Small | Low | Persistent | System | System-wide settings (OAuth encryption key, feature flags) | SQLAlchemy | **sled** (KV, low cardinality) | ✅ MIGRATE |
| **Cluster Topology** | Med | Low | SC | ??? | Small | Low | Persistent | System | Raft cluster membership, node addresses | ??? | **sled via Raft** (bootstrap info) | ❓ MISSING |

**Analysis:**
- **SystemSettingsModel**: Pure KV → migrate to sled
- **Cluster Topology**: MISSING! Should be stored somewhere for Raft bootstrap
  - **Question**: Should cluster topology be part of file metadata or separate?
  - **User's suggestion**: "cluster topology 可能不用单独存在应该和metadata是在一起的"
  - **Action**: Merge into Raft metadata (sled)

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
| **WorkspaceConfig** | Low | Low | EC | KV (by path) | Small | Low | Persistent | Zone | Workspace directory registration | In-memory + SQLAlchemy | **sled** (KV) | ✅ MIGRATE |
| **MemoryConfigModel** (DB) | Low | Low | EC | KV (by path) | Small | Low | Persistent | Zone | Memory directory configuration (DB storage) | SQLAlchemy | **sled** (KV) | ✅ MIGRATE |

**Analysis:**
- Both are simple KV configs
- **Action**: Migrate to sled

---

## SUMMARY: STORAGE LAYER DECISIONS

### ✅ **Keep SQLAlchemy (PostgreSQL/SQLite)** - 20 types
Relational queries, FK, unique constraints, vector search, encryption, BRIN indexes

| Category | Data Types | Rationale |
|----------|-----------|-----------|
| **Users & Auth** | UserModel, UserOAuthAccountModel, OAuthCredentialModel | Relational queries, FK, unique constraints, encryption |
| **ReBAC (Partial)** | ReBACGroupClosureModel, ReBACChangelogModel | Materialized view, append-only BRIN |
| **Memory System** | MemoryModel, TrajectoryModel, TrajectoryFeedbackModel, PlaybookModel | Complex relational + vector search (pgvector) |
| **Versioning** | VersionHistoryModel, WorkspaceSnapshotModel | Parent FK, BRIN time-series |
| **Semantic Search** | DocumentChunkModel | Vector index (pgvector/sqlite-vec) |
| **Workflows** | WorkflowModel, WorkflowExecutionModel | Version tracking, FK, BRIN |
| **Zones** | ZoneModel, EntityRegistryModel, ExternalUserServiceModel | Unique constraints, hierarchical FK, encryption |
| **Audit** | OperationLogModel | Append-only BRIN |
| **Sandboxes** | SandboxMetadataModel | Relational queries |

### ✅ **Migrate to sled via Raft** - 8 types
KV access pattern, strong consistency needed (multi-node)

| Data Type | Current | Reason |
|-----------|---------|--------|
| FileMetadata (proto) | Generated dataclass | Core metadata, KV by path, SC via Raft |
| DirectoryEntryModel | SQLAlchemy | KV by parent_path, no JOINs |
| FileMetadataModel (KV) | SQLAlchemy | Arbitrary KV metadata |
| ReBACNamespaceModel | SQLAlchemy | KV by namespace_id, low cardinality |
| SystemSettingsModel | SQLAlchemy | KV by key, low cardinality |
| WorkspaceConfig | In-memory + SQLAlchemy | KV by path |
| MemoryConfig | In-memory + SQLAlchemy | KV by path |
| **Cluster Topology (NEW)** | MISSING | Raft bootstrap info, merge with metadata |

### ✅ **Migrate to sled (local, no Raft)** - 1 type
CAS (content-addressed), immutable

| Data Type | Current | Reason |
|-----------|---------|--------|
| ContentChunkModel | SQLAlchemy | KV by content_hash, immutable (no SC needed) |

### ✅ **Keep Dragonfly (in-memory cache)** - 3 types
Performance cache, TTL, pub/sub

| Data Type | Current | Reason |
|-----------|---------|--------|
| PermissionCacheProtocol | Dragonfly/PostgreSQL/In-memory | Permission check cache, TTL |
| TigerCacheProtocol | Dragonfly/PostgreSQL | Pre-materialized bitmaps, TTL |
| **FileEvent (pub/sub)** | Dragonfly pub/sub | ??? (NEEDS DECISION) |

### 🤔 **DECISIONS NEEDED**

1. **FilePathModel vs FileMetadata**: MERGE? (Redundant file metadata)
2. **ReBACTupleModel**: Keep SQLAlchemy OR migrate to sled with custom indexes?
3. **FileEvent**: Raft event log (SC) OR keep Dragonfly pub/sub (EC)?
4. **UserSessionModel**: Migrate to Redis/Dragonfly (session cache)?
5. **ContentCacheModel**: Simplify to pure disk cache (remove DB metadata)?

### ❓ **MISSING / INCOMPLETE**

1. **Cluster Topology**: No storage found, should merge with Raft metadata
2. **Subscription/Delivery DB models**: Pydantic models exist, but no SQLAlchemy storage found
3. **Dragonfly status after Raft**: User mentioned it might be broken post-Raft migration

---

## REDUNDANCY ANALYSIS

### Candidate for MERGE (like tenant→zone):

1. **FilePathModel + FileMetadata**
   - Both store file metadata
   - FilePathModel: Relational (SQLAlchemy), FK to zone
   - FileMetadata: Proto-generated, KV-style
   - **Recommendation**: Merge into single FileMetadata in sled, deprecate FilePathModel

2. **WorkspaceConfig + WorkspaceConfigModel**
   - WorkspaceConfig: In-memory dataclass
   - WorkspaceConfigModel: SQLAlchemy DB storage
   - **Recommendation**: Keep only WorkspaceConfigModel in sled (no in-memory duplication)

3. **MemoryConfig + MemoryConfigModel**
   - Same as above
   - **Recommendation**: Keep only MemoryConfigModel in sled

4. **Cluster Topology (standalone) → Merge into FileMetadata**
   - As user suggested, cluster topology doesn't need separate existence
   - **Recommendation**: Store as special metadata entries in sled (e.g., `/system/cluster/node-{id}`)

---

## STORAGE MEDIUM ORTHOGONALITY ANALYSIS (Task #2)

### Purpose
Every storage medium must justify its existence. No overlaps, no redundancy.
Use same Property Dimensions as data types to ensure orthogonality.

### Storage Medium Properties Matrix

| Storage Medium | Read Perf | Write Perf | Consistency | Query Patterns | Data Size Limit | Durability | Deployment | Cost | Justification |
|----------------|-----------|------------|-------------|----------------|-----------------|------------|------------|------|---------------|
| **PostgreSQL** | Med | Med | Serializable (ACID) | Relational (JOIN, FK, indexes, vector via pgvector) | ~1TB (practical) | Persistent (WAL) | Complex (server process) | High (CPU+RAM) | **Relational queries, ACID transactions, vector search** — No substitute for complex JOINs and referential integrity |
| **SQLite** | High (local) | Med (single writer) | Serializable (ACID) | Relational (JOIN, FK, limited indexes, vector via sqlite-vec) | ~281TB (theoretical), ~100GB (practical) | Persistent (file) | Simple (embedded) | Low (single file) | **Embedded relational DB for dev/testing** — Same SQL interface as PostgreSQL, zero-config |
| **sled** | Critical (~14μs) | Critical (~14μs) | Linearizable (via Raft) OR Local (single-node) | **Ordered** KV (prefix scan, range queries, append-only LSM-tree) | Multi-TB (LSM-tree) | Persistent (log-structured) | Simple (embedded) | Low (single process) | **High-performance ordered KV with optional Raft consensus** — Orders of magnitude faster than SQL for pure KV workloads, embedded like SQLite but KV-focused. **Ordered property critical for user root localization** (each user's first key = their `/` root in chroot model) |
| **Dragonfly** | Critical (in-memory) | Critical (in-memory) | Eventual (async replication) | KV + pub/sub + Lua scripts | RAM-limited (~100GB typical) | Ephemeral (optional AOF) | Med (server process) | High (RAM) | **In-memory cache + pub/sub** — Needed for permission cache, event bus, TTL-based expiration; Redis protocol compatible |
| **Redis** | Critical (in-memory) | Critical (in-memory) | Eventual (async replication) | KV + pub/sub + Lua scripts | RAM-limited (~100GB typical) | Ephemeral (optional AOF) | Med (server process) | High (RAM) | ⚠️ **REDUNDANT with Dragonfly** — Same use case, Dragonfly is drop-in replacement with better performance |
| **S3 / GCS / Azure Blob** | Med (network latency) | Med (network latency) | Eventual (object versioning) | Blob only (by key, no queries) | Unlimited (petabytes) | Persistent (99.999999999% durability) | Simple (managed service) | Low (pay-per-GB) | **Cloud blob storage for large files** — No substitute for petabyte-scale object storage with geo-replication |
| **Local Disk** | High (SSD) | High (SSD) | Local (filesystem) | Blob only (by path) | Multi-TB per disk | Persistent (filesystem) | Simple (OS filesystem) | Med (hardware cost) | **Local blob storage for single-node deployments** — Zero network latency, good for dev/edge nodes |
| **In-Memory (Python dict)** | Critical (<1μs) | Critical (<1μs) | Local (process-only) | KV only (hashmap) | RAM-limited (~10GB typical for Python process) | Ephemeral (lost on restart) | Simple (no setup) | Low (process RAM) | **L1 cache for hot data** — No persistence, fastest possible access, process-local only |
| **In-Memory (DashMap, Rust)** | Critical (<100ns) | Critical (<100ns) | Local (process-only) | KV only (lock-free hashmap) | RAM-limited (~10GB typical) | Ephemeral (lost on restart) | Simple (PyO3 FFI) | Low (process RAM) | **L1 cache for CompactFileMetadata** — Lock-free concurrent access, string interning, faster than Python dict |

### Orthogonality Analysis

#### ✅ **NO OVERLAP**: PostgreSQL vs SQLite
- **PostgreSQL**: Multi-user, networked, high concurrency, production relational workloads
- **SQLite**: Single-user, embedded, dev/testing, zero-config relational workloads
- **Verdict**: **Keep both** — Different deployment models, same query interface (SQL)

#### ✅ **NO OVERLAP**: PostgreSQL/SQLite vs sled
- **PostgreSQL/SQLite**: Relational queries (JOIN, FK, transactions across tables)
- **sled**: Pure KV (no relations), extreme performance (100x faster for simple get/set)
- **Verdict**: **Keep both** — Orthogonal query patterns (Relational vs KV)

#### ✅ **NO OVERLAP**: sled vs Dragonfly
- **sled**: Persistent KV with optional Raft consensus, embedded (no network)
- **Dragonfly**: In-memory cache with TTL, networked (shared across nodes), pub/sub
- **Verdict**: **Keep both** — Different durability (persistent vs ephemeral) and use case (SSOT vs cache)

#### ❌ **OVERLAP DETECTED**: Redis vs Dragonfly
- **Same use case**: In-memory cache, pub/sub, TTL-based eviction
- **Same protocol**: Redis wire protocol
- **Dragonfly advantages**:
  - 25x better memory efficiency (jemalloc + custom allocator)
  - Multi-threaded (Redis is single-threaded)
  - Drop-in replacement (no code changes)
- **Redis advantages**: None in our context
- **Verdict**: ⚠️ **DEPRECATE Redis, standardize on Dragonfly**
  - Migration: Change connection string, zero code changes
  - Timeline: P2 (after Raft migration)

#### ✅ **NO OVERLAP**: Cloud Blob (S3/GCS/Azure) vs Local Disk
- **Cloud Blob**: Geo-replicated, unlimited scale, managed service, higher latency
- **Local Disk**: Single-node, limited scale, self-managed, zero network latency
- **Verdict**: **Keep both** — Different deployment contexts (cloud vs edge/dev)

#### ✅ **NO OVERLAP**: Dragonfly vs In-Memory (Python dict / DashMap)
- **Dragonfly**: Networked (shared across processes/nodes), durable with AOF, TTL management
- **In-Memory**: Process-local only, no persistence, no TTL (manual eviction)
- **Verdict**: **Keep both** — Different scope (multi-node vs single-process)

#### ✅ **NO OVERLAP**: In-Memory Python dict vs DashMap
- **Python dict**: Simple, no FFI overhead, good for non-critical paths
- **DashMap**: Lock-free, 10x faster, string interning, critical path (CompactFileMetadata L1 cache)
- **Verdict**: **Keep both** — Different performance tiers (DashMap for critical hot paths only)

### Storage Medium Justification Summary

| Storage Medium | Status | Justification |
|----------------|--------|---------------|
| **PostgreSQL** | ✅ **KEEP** | **Relational SSOT**: Users, ReBAC, Memory (vector), Workflows, Versioning. No substitute for ACID + JOINs + vector search. |
| **SQLite** | ✅ **KEEP** | **Dev/Test relational**: Same SQL interface as PostgreSQL, zero-config, embedded. Essential for local development. |
| **sled** | ✅ **KEEP** | **Ordered KV SSOT with Raft**: File metadata, directory index, custom metadata, system settings. 100x faster than SQL for pure KV, Raft consensus for multi-node SC. **Ordered property enables user root localization** (first key = user `/` in chroot). |
| **Dragonfly** | ✅ **KEEP** | **In-memory cache + pub/sub**: Permission cache, Tiger cache, FileEvent pub/sub. TTL management, shared across nodes, Redis protocol. |
| **Redis** | ❌ **DEPRECATE (P2)** | **REDUNDANT**: Same use case as Dragonfly, inferior performance. Migration: change connection string only. |
| **S3/GCS/Azure** | ✅ **KEEP** | **Cloud blob storage**: Unlimited scale, geo-replication, managed service. No substitute for petabyte-scale object storage. |
| **Local Disk** | ✅ **KEEP** | **Local blob storage**: Zero network latency, good for dev/edge nodes. Essential for single-node deployments. |
| **In-Memory (Python dict)** | ✅ **KEEP** | **L1 cache (non-critical)**: Simple process-local cache, no FFI overhead. Used sparingly. |
| **In-Memory (DashMap)** | ✅ **KEEP** | **L1 cache (critical)**: Lock-free, string interning, 10x faster than Python dict. CompactFileMetadata hot path. |

### Deployment Mode → Storage Mapping

| Deployment Mode | Relational | KV (metadata) | KV (CAS) | Cache | Pub/Sub | Blob |
|-----------------|------------|---------------|----------|-------|---------|------|
| **Dev (single-node)** | SQLite | sled (local) | sled (local) | In-Memory | In-Memory | Local Disk |
| **Production (single-node)** | PostgreSQL | sled (local) | sled (local) | Dragonfly | Dragonfly | S3/Local |
| **Production (multi-node, Raft SC)** | PostgreSQL | sled (Raft) | sled (local) | Dragonfly | Dragonfly | S3 |
| **Production (multi-node, Raft EC)** | PostgreSQL | sled (async) | sled (local) | Dragonfly | Dragonfly | S3 |

### Key Insights

1. **Relational vs KV**: PostgreSQL/SQLite handle relational data (20 types), sled handles KV data (8 types). No overlap due to orthogonal query patterns.

2. **Persistent vs Ephemeral**: sled is persistent SSOT, Dragonfly is ephemeral cache. No overlap due to orthogonal durability requirements.

3. **Networked vs Embedded**: Dragonfly is networked (shared across nodes), sled is embedded (PyO3 FFI, same process). No overlap due to orthogonal deployment contexts.

4. **Redis redundancy**: Dragonfly is a strict superset of Redis (same protocol, better performance). Redis should be deprecated.

5. **Cloud vs Local blob storage**: S3 for cloud deployments (unlimited scale), Local Disk for edge/dev (zero latency). Both needed for different contexts.

6. **L1 cache tiers**: DashMap for critical hot paths (CompactFileMetadata), Python dict for non-critical. Different performance tiers, both justified.

### Action Items

1. ✅ **No storage medium merges needed** (except Redis → Dragonfly migration)
2. ⚠️ **Deprecate Redis** (P2): Migrate all Redis usage to Dragonfly (change connection string, zero code changes)
3. ✅ **Orthogonality verified**: All remaining storage mediums have distinct, non-overlapping responsibilities

---

## THE NEXUS QUARTET: FOUR STORAGE PILLARS (Task #14)

**Design Decision**: NexusFS (nexus-core) abstracts storage by **Capability** (Access Pattern & Consistency Guarantee),
not by domain (`UserStore`) or implementation (`PostgresStore`).
Inspired by Linux Kernel's `BlockDevice`/`CharDevice`/`FileSystem` model.
Names explain the **"What"** and **"Why"**, not the **"How"**.

### The Four Pillars

| Pillar | ABC | Role | Backing Drivers | Kernel Status |
|--------|-----|------|-----------------|---------------|
| **Metastore** | `MetastoreABC` | "The Structure" — inodes, dentries, config, topology | sled (local PyO3 / gRPC Raft) | **Required** init param |
| **RecordStore** | `RecordStoreABC` | "The Truth" — entities, relationships, logs, vectors | PostgreSQL (prod), SQLite (dev) | **Optional** — injected for Services |
| **ObjectStore** | `ObjectStoreABC` (= current `Backend`) | "The Content" — raw file bytes, immutable objects | S3, GCS, Local Disk | **Mounted** dynamically (like Linux `mount`) |
| **CacheStore** | `CacheStoreABC` (future) | "The Reflexes" — sessions, signals, ephemeral data | Dragonfly (prod), In-Memory (dev) | **Future** — optional |

**Naming Note**: The existing proto-generated `MetadataStore` (specific to `FileMetadata` typed operations)
will be renamed to `FileMetadataProtocol` to avoid confusion with `MetastoreABC` (the underlying ordered KV primitive).
`MetastoreABC` is the lower-level KV store; `FileMetadataProtocol` is a typed wrapper that sits on top of it.

### Complete Data Type → Pillar Mapping

**Metastore** (Ordered KV — sled):
| Data Type | From Part | Rationale |
|-----------|-----------|-----------|
| FileMetadata (proto) | Part 1 | Core file attributes, KV by path |
| DirectoryEntryModel | Part 1 | Sparse directory index, KV by parent_path |
| FileMetadataModel (custom KV) | Part 1 | Arbitrary user metadata, KV by path_id + key |
| ContentChunkModel | Part 2 | CAS dedup index, KV by content_hash (immutable) |
| ReBACNamespaceModel | Part 5 | Permission config, KV by namespace_id |
| SystemSettingsModel | Part 13 | System config, KV by key |
| WorkspaceConfig | Part 15 | Workspace config, KV by path |
| MemoryConfig | Part 15 | Memory config, KV by path |
| Cluster Topology | Part 13 | Raft bootstrap, merged with metadata |

**RecordStore** (Relational — PostgreSQL/SQLite):
| Data Type | From Part | Rationale |
|-----------|-----------|-----------|
| UserModel, UserOAuthAccountModel, OAuthCredentialModel | Part 6 | FK, unique constraints, encryption |
| ReBACTupleModel, ReBACGroupClosureModel, ReBACChangelogModel | Part 5 | Composite indexes, materialized view, BRIN |
| MemoryModel, TrajectoryModel, TrajectoryFeedbackModel, PlaybookModel | Part 4 | Vector search (pgvector), relational FK |
| VersionHistoryModel, WorkspaceSnapshotModel | Part 3 | Parent FK, BRIN time-series |
| DocumentChunkModel | Part 10 | Vector index (pgvector/sqlite-vec) |
| WorkflowModel, WorkflowExecutionModel | Part 9 | Version tracking, FK, BRIN |
| ZoneModel, EntityRegistryModel, ExternalUserServiceModel | Part 7 | Unique constraints, hierarchical FK |
| OperationLogModel | Part 11 | Append-only BRIN |
| SandboxMetadataModel | Part 12 | Relational queries |

**ObjectStore** (= existing `Backend` ABC — S3/Local Disk):
| Data Type | From Part | Rationale |
|-----------|-----------|-----------|
| File Content (blobs) | Part 2 | Actual file bytes, petabyte scale, streaming I/O |

**CacheStore** (future — Dragonfly / In-Memory):
| Data Type | From Part | Rationale |
|-----------|-----------|-----------|
| UserSessionModel | Part 6 | Session tokens, TTL |
| PermissionCacheProtocol | Part 14 | Permission check cache, TTL |
| TigerCacheProtocol | Part 14 | Pre-materialized bitmaps, TTL |
| FileEvent (pub/sub) | Part 8 | Change notifications |

### CacheStore Implementation Status

Nexus already has individual implementations scattered across the codebase:
- **EventBus**: `EventBusProtocol` (ABC), `RedisEventBus` (Dragonfly impl) — NO in-memory impl
- **PermissionCache**: `PermissionCacheProtocol` (ABC), `DragonflyPermissionCache`, `PostgresPermissionCache` — NO in-memory impl
- **TigerCache**: `TigerCacheProtocol` (ABC), `DragonflyTigerCache`, `PostgresTigerCache` — NO in-memory impl

**Future work**: Unify these into a single `CacheStoreABC` with `InMemoryCacheStore` fallback.

---

## NEXT STEPS

1. ✅ Review this matrix with user
2. ❓ Resolve 5 decision points
3. ❓ Identify missing Subscription/Delivery storage
4. ❓ Clarify Dragonfly status post-Raft
5. ✅ Merge redundant data types (FilePathModel → FileMetadata, WorkspaceConfig → DB only)
6. ✅ Rewrite federation-memo.md with this data architecture
7. ✅ Storage medium orthogonality analysis complete — Redis deprecation identified (P2)
8. ✅ **NEW**: "Nexus Quartet" — Four Pillars abstraction design decided (Metastore, RecordStore, ObjectStore, CacheStore)
9. ✅ **COMPLETE**: Task #14 — MetastoreABC + RecordStoreABC in NexusFS constructor (Four Pillars DI)
10. 📋 **PLANNED**: Rename proto-generated `MetadataStore` → `FileMetadataProtocol` (avoid confusion with MetastoreABC)
11. ✅ **COMPLETE**: CI PyO3 build for nexus_raft (#1234) — `test.yml` builds `nexus_raft` with `--features python`
12. ❓ **DECISION**: Version history (VersionHistoryGC, TimeTravelReader) — kernel or services? Currently lives inside `SQLAlchemyMetadataStore.put()`. With RaftMetadataStore as default, FilePathModel/VersionHistoryModel are not populated. Need to decide: (a) kernel: MetastoreABC natively supports versioning, or (b) services: version tracking as separate observer/hook on RecordStore. (Related: Task #3, #11)

---

**END OF DATA-STORAGE-MATRIX.MD**