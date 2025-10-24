# Nexus Architecture

**Version:** 0.4.0 | **Last Updated:** 2025-10-23

> **Purpose:** High-level architecture overview of Nexus, an AI-native distributed filesystem with advanced features for AI agent workflows.

## Table of Contents

- [Overview](#overview)
- [System Architecture](#system-architecture)
- [Core Components](#core-components)
  - [NexusFS Core](#1-nexusfs-core)
  - [LLM Provider](#2-llm-provider-abstraction-v040)
  - [Plugin System](#3-plugin-system)
  - [Work Queue](#4-work-queue-system)
  - [Workflow Engine](#5-workflow-engine-v040)
  - [Skills System](#6-skills-system)
  - [Permission System](#7-3-tier-permission-system-v040)
  - [Memory System](#8-identity-based-memory-system-v040)
- [Storage Layer](#storage-layer)
- [Namespace System](#namespace-system)
- [Data Flow](#data-flow)
- [Key Design Decisions](#key-design-decisions)
- [Performance](#performance-characteristics)
- [Security](#security)
- [Deployment](#deployment-modes)

---

## Overview

Nexus is an AI-native distributed filesystem providing a unified API across multiple storage backends with advanced features for AI agent workflows:

- **Unified Interface**: Single API for local, GCS, S3, and cloud storage
- **Content-Addressable Storage**: Automatic deduplication (30-50% savings)
- **3-Tier Permissions**: UNIX + ACL + ReBAC for flexible access control
- **Identity-Based Memory**: Order-neutral paths for multi-agent collaboration
- **Time-Travel**: Full operation history with undo capability
- **AI-Native Features**: Semantic search, LLM integration, workflow automation

## System Architecture

```
┌─────────────────────────────────────────────────────┐
│              User-Facing APIs                       │
│   CLI  │  Python SDK  │  MCP Server  │  HTTP API   │
├─────────────────────────────────────────────────────┤
│              Core Components                        │
│   NexusFS  │  Plugins  │  Workflows  │  LLM        │
│   Permissions (UNIX/ACL/ReBAC)  │  Memory System   │
├─────────────────────────────────────────────────────┤
│              Storage Layer                          │
│   Metadata Store  │  CAS  │  Cache  │  Op Log      │
├─────────────────────────────────────────────────────┤
│              Backend Adapters                       │
│   Local  │  GCS  │  S3  │  GDrive  │  Workspace   │
└─────────────────────────────────────────────────────┘
```

## Core Components

### 1. NexusFS Core

**Purpose:** Central filesystem abstraction providing unified file operations across all backends.

**Location:** `src/nexus/core/nexus_fs.py`

**Key Capabilities:**
- **Multi-Backend Routing**: Automatic path routing to appropriate storage backend
- **Permission Enforcement**: Integrated 3-tier permission system (UNIX/ACL/ReBAC)
- **Operation Logging**: Complete audit trail for time-travel and undo
- **CAS Integration**: Automatic content deduplication via SHA-256 hashing
- **Batch Operations**: 4x faster bulk writes via `write_batch()`
- **Async-First Design**: Non-blocking I/O for scalability

**Implementation:** Mixin-based architecture separating concerns:
- `NexusFSCoreMixin`: Core read/write/delete operations
- `NexusFSPermissionsMixin`: UNIX/ACL permission operations
- `NexusFSReBACMixin`: Relationship-based access control
- `NexusFSSearchMixin`: Semantic and keyword search
- `NexusFSVersionsMixin`: Workspace snapshots and versioning

### 2. LLM Provider Abstraction (v0.4.0)

**Purpose:** Unified interface for multiple LLM providers with automatic KV cache management.

**Location:** `src/nexus/llm/`

**Key Features:**
- Multi-provider support via LiteLLM (Anthropic, OpenAI, Google, Ollama)
- Automatic KV cache management (50-90% cost savings on repeated queries)
- Token counting and cost tracking
- Streaming response support

**Example:** See `examples/py_demo/llm_provider_demo.py`

### 3. Plugin System

**Purpose:** Extensible architecture for vendor integrations without forking core.

**Location:** `src/nexus/plugins/`

**Key Components:**
- Plugin registry with auto-discovery
- Lifecycle hooks (before/after read, write, delete, mkdir, copy)
- CLI command integration
- Configuration management

**Plugin Interface:** Base class `NexusPlugin` with metadata, commands, hooks, and lifecycle methods.

**Available Plugins:**
- `nexus-plugin-anthropic`: Claude Skills API integration
- `nexus-plugin-skill-seekers`: Generate skills from documentation
- `nexus-plugin-firecrawl`: Web scraping and content extraction

**Development Guide:** See `docs/development/PLUGIN_DEVELOPMENT.md`

### 4. Work Queue System

**Purpose:** File-based job queue with SQL views for efficient querying.

**Location:** `src/nexus/storage/views.py`

**Core Concept:** Jobs are regular files with metadata - no separate job system needed ("Everything as a File" principle).

**Status States:** `ready`, `pending`, `blocked`, `in_progress`, `completed`, `failed`

**Key Features:**
- Priority-based scheduling
- Dependency resolution (blocked jobs wait on dependencies)
- Worker assignment tracking
- SQL views for O(1) queue queries

**CLI:** `nexus work ready`, `nexus work status`, `nexus work blocked`

**Note:** Provides job state management. Users implement execution logic.

### 5. Workflow Engine (v0.4.0)

**Purpose:** Event-driven automation for document processing and multi-step operations.

**Location:** `src/nexus/workflows/`

**Components:**
- **Triggers**: File events, schedules, manual invocation
- **Actions**: Built-in + plugin actions (parse, LLM query, file ops)
- **Engine**: DAG execution with dependency resolution
- **Storage**: Workflow definitions stored as YAML files in `.nexus/workflows/`

**Workflow Format:** YAML with triggers, actions, and config

**Example:** See `examples/workflows/invoice_processing.yaml`

### 6. Skills System

**Purpose:** Vendor-neutral skill management with three-tier hierarchy and governance.

**Location:** `src/nexus/skills/`

**Hierarchy:**
- `/system/skills/`: System-wide, read-only
- `/shared/skills/`: Tenant-wide, shared
- `/workspace/.nexus/skills/`: Agent-specific

**Key Features:**
- Dependency resolution with cycle detection
- Skill versioning and lineage tracking
- Approval governance for shared skills
- Export/import workflows

**Format:** SKILL.md files with YAML frontmatter (name, version, dependencies, tier)

### 8. 3-Tier Permission System (v0.4.0+)

**Location:**
- `src/nexus/core/permissions.py` - Base permission enforcement
- `src/nexus/core/nexus_fs_permissions.py` - ACL Python API
- `src/nexus/core/nexus_fs_rebac.py` - ReBAC Python API
- `src/nexus/core/rebac_manager.py` - ReBAC graph engine

**Three Permission Layers:**
1. **UNIX** - Owner/group/mode (chmod, chown, chgrp)
2. **ACL** - Per-user/group granular control (setfacl, grant_user)
3. **ReBAC** - Graph-based dynamic inheritance (rebac_create, rebac_check)

**Permission Types:**
- `read`: View file content
- `write`: Modify files
- `execute`: Execute files
- `owner-of`: Full control
- Custom relations: `member-of`, `viewer-of`, `editor-of`, `parent-of`

**Features:**
- Complete CLI + Python SDK for all layers
- Zanzibar-style graph traversal (ReBAC)
- Explicit deny rules (ACL)
- Automatic permission inheritance (ReBAC)
- Time-limited access with expiration
- Multi-level organization hierarchies

**Quick Examples:**
```python
# ACL - Per-user granular control
nx.grant_user("/file.txt", user="alice", permissions="rw-")
nx.deny_user("/secret.txt", user="intern")

# ReBAC - Dynamic graph-based permissions
nx.rebac_create(
    subject=("agent", "alice"),
    relation="member-of",
    object=("group", "developers")
)
can_access = nx.rebac_check(
    subject=("agent", "alice"),
    permission="owner-of",
    object=("file", "/project.txt")
)
```

See **Permission System Deep Dive** section below for comprehensive documentation.

### 9. Identity-Based Memory System (v0.4.0)
Order-neutral virtual paths with identity-based storage for AI agent memory.

**Location:** `src/nexus/core/entity_registry.py`, `src/nexus/core/memory_router.py`

**Core Concept:**
Separates identity from location - canonical storage by ID with multiple virtual path views. Memory location ≠ identity; relationships determine access, paths determine browsing.

**Key Features:**
- Order-neutral paths: `/workspace/alice/agent1` and `/workspace/agent1/alice` resolve to same memory
- No data duplication for memory sharing across agents
- Identity relationships enable advanced permission checks
- Multi-view capability: browse by user, agent, or tenant

**Entity Registry:**
```sql
CREATE TABLE entity_registry (
    entity_type TEXT NOT NULL,  -- 'tenant', 'user', 'agent'
    entity_id TEXT NOT NULL,
    parent_type TEXT,
    parent_id TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (entity_type, entity_id)
);
```

**Memory Schema:**
```sql
CREATE TABLE memories (
    memory_id TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,

    -- Identity relationships
    tenant_id TEXT,
    user_id TEXT,        -- Real user ownership
    agent_id TEXT,       -- Created by agent

    -- Scope and visibility
    scope TEXT,          -- 'agent', 'user', 'tenant', 'global'
    visibility TEXT,     -- 'private', 'shared', 'public'

    -- UNIX permissions
    group TEXT,
    mode INTEGER DEFAULT 420,

    -- Metadata
    memory_type TEXT,
    importance REAL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

**Virtual Path Router:**
```python
# Multiple paths resolve to same memory_id
/workspace/acme/alice/agent1/memory/mem.json
/workspace/alice/agent1/acme/memory/mem.json
/workspace/agent1/alice/memory/mem.json
/memory/by-user/alice/agent1/mem.json
/objs/memory/mem_123  # Canonical storage

# Router: path → extract IDs → query by relationships → memory_id
```

**3-Layer Permission Integration:**
```python
class MemoryPermissionEnforcer(PermissionEnforcer):
    """
    Layer 1: ReBAC - Identity relationships
      - Direct creator access
      - User ownership inheritance (agents → user)
      - Tenant-scoped sharing

    Layer 2: ACL - Canonical path access control
      - Works on order-neutral paths

    Layer 3: UNIX - Proper user ownership
      - Uses user_id as owner (not agent_id)
    """
```

**Example: Multi-Agent Memory Sharing**
```python
# Alice has 2 agents
agent1 = Agent(agent_id='agent1', owner_user_id='alice')
agent2 = Agent(agent_id='agent2', owner_user_id='alice')

# agent1 creates user-scoped memory
memory = Memory(
    memory_id='mem_123',
    user_id='alice',
    agent_id='agent1',
    scope='user'  # Shared across Alice's agents
)

# agent2 can access via user ownership relationship
ctx = OperationContext(user='agent2')
can_read = enforcer.check('mem_123', Permission.READ, ctx)
# ✅ True - both agents owned by alice
```

**Benefits:**
- No file duplication for memory sharing
- Flexible hierarchy views without data movement
- Complete ReBAC layer with meaningful identity relationships
- Order-neutral paths enable reorganization without file moves
- Foundation for advanced memory features (consolidation, search)

#### Phase 2 Integration: File API Support (v0.4.0)

Memory virtual paths are now fully integrated with the File API, allowing users to access memories via standard file operations.

**Location:** `src/nexus/core/nexus_fs_core.py:110-213`, `src/nexus/core/nexus_fs_search.py:89-90`

**Core Concept:**
Users can choose between two equivalent interfaces for memory access:
1. **Memory API**: `nx.memory.store()` / `get()` / `query()` (specialized interface)
2. **File API**: `nx.read()` / `write()` / `delete()` / `list()` (familiar file operations)

**Path Interception:**
```python
def read(self, path: str) -> bytes:
    """Read file or memory content."""
    # Intercept memory paths
    if MemoryViewRouter.is_memory_path(path):
        return self._read_memory_path(path)
    # Normal file operations...
```

**Memory Path Patterns:**
```python
# All these patterns are detected and routed to memory system:
/objs/memory/{id}                          # Canonical path
/workspace/{...}/memory/{...}              # Workspace view (order-neutral)
/memory/by-user/{user}/...                 # User-centric view
/memory/by-agent/{agent}/...               # Agent-centric view
/memory/by-tenant/{tenant}/...             # Tenant-centric view
```

**Example: Dual-API Access**
```python
# Method 1: Memory API (specialized)
mem_id = nx.memory.store("Python best practices", scope="user")
mem = nx.memory.get(mem_id)

# Method 2: File API (familiar)
nx.write("/workspace/alice/agent1/memory/facts", b"Python is great!")
content = nx.read("/workspace/alice/agent1/memory/facts")

# Order-neutral: All these paths access the SAME memory!
content1 = nx.read("/workspace/alice/agent1/memory/facts")
content2 = nx.read("/workspace/agent1/alice/memory/facts")  # Same!
content3 = nx.read("/memory/by-user/alice/facts")           # Same!
```

**CLI Support:**
```bash
# Store memory via file operations
nexus write /workspace/alice/agent1/memory/facts "Python is great!"

# Read via any equivalent path
nexus cat /workspace/agent1/alice/memory/facts
nexus cat /memory/by-user/alice/facts

# List memories
nexus ls /workspace/alice/agent1/memory

# Delete memory
nexus rm /objs/memory/{id}
```

**Implementation Details:**
- **Path Detection**: `MemoryViewRouter.is_memory_path()` checks for memory path patterns
- **Resolution**: Extracts entity IDs from path → queries by relationships → returns most recent matching memory
- **Multiple Results**: When multiple memories match (e.g., multiple memories for alice+agent1), returns most recent by `created_at DESC`
- **Directory Listing**: `_list_memory_path()` queries memories and returns virtual paths based on filter
- **Write Behavior**: Creates new memory each time (memories are immutable references)

**Forward Compatibility:**
This implementation is forward-compatible with Issue #121 (Agent Workspace Structure):
- When #121 adds `.nexus/` subdirectory: just add path aliases
- Core virtual path routing already supports flexible patterns
- Minimal changes needed for full #121 integration

**Benefits:**
- **Familiar Interface**: Use standard file operations for memory access
- **Tool Integration**: Memory works with all CLI commands (`cat`, `write`, `ls`, `rm`)
- **Order Agnostic**: Path component order doesn't matter
- **Two APIs, One System**: Memory API and File API access same underlying storage
- **No Breaking Changes**: Existing Memory API code continues to work unchanged

**Demo:**
See `examples/py_demo/memory_file_api_demo.py` and `examples/script_demo/memory_file_api_demo.sh` for comprehensive examples.

## Storage Layer

### Content-Addressable Storage (CAS)
Automatic deduplication using SHA-256 hashing.

**Location:** `src/nexus/backends/local.py`, `src/nexus/storage/`

**Benefits:**
- 30-50% storage savings via deduplication
- Immutable content enables efficient caching
- Lineage tracking across file copies
- Efficient time-travel without full copies

**How It Works:**
```python
# Writing content
content = b"Hello World"
content_hash = hashlib.sha256(content).hexdigest()
cas_path = f"cas/{content_hash[:2]}/{content_hash}"
# Store once, reference many times
```

### Operation Log & Time-Travel
Complete audit trail with undo capability.

**Location:** `src/nexus/storage/operations.py`

**Features:**
- All filesystem operations logged
- Undo capability for reversible operations
- Time-travel: read files at historical points
- Content diffing between versions
- Multi-agent safe with per-agent tracking

**Database Schema:**
```sql
CREATE TABLE operations (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    operation_type VARCHAR(50) NOT NULL,
    file_path TEXT NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,
    details JSONB NOT NULL,
    undo_state JSONB,
    undone BOOLEAN DEFAULT FALSE
);
```

### Caching System (v0.4.0)
Multi-tier caching for performance optimization.

**Location:** `src/nexus/storage/cache.py`, `src/nexus/storage/content_cache.py`

**Cache Types:**
- **Metadata Cache**: File metadata, path lookups, existence checks
- **Content Cache**: LRU cache for file content (256MB default)
- **Permission Cache**: Permission check results

**Performance Impact:**
- Cached reads: **10-50x faster**
- Metadata operations: **5x faster**
- Configurable cache sizes and TTLs

## Namespace System

Nexus organizes files into five built-in namespaces with different access control and visibility rules.

**Location:** `src/nexus/core/router.py`

### Built-in Namespaces

| Namespace | Purpose | Readonly | Admin-Only | Requires Tenant |
|-----------|---------|----------|------------|-----------------|
| `/workspace` | Agent-specific workspace | No | No | Yes |
| `/shared` | Tenant-wide shared files | No | No | Yes |
| `/archives` | Long-term storage | Yes | No | Yes |
| `/external` | External integrations | No | No | No |
| `/system` | System configuration | Yes | **Yes** | No |

### Namespace Visibility Rules

Namespaces are automatically filtered based on user context:

```python
# tenant_id=None (no tenant)
visible = ["/external"]  # Only external accessible

# tenant_id="default" (single tenant)
visible = ["/workspace", "/shared", "/archives", "/external"]

# is_admin=True
visible = ["/workspace", "/shared", "/archives", "/external", "/system"]
```

### FUSE Mount Integration

When mounting via FUSE, namespace directories appear at root level:

```bash
$ ls /mnt/nexus/
archives/  external/  shared/  workspace/  .raw/
```

The filesystem dynamically shows only accessible namespaces based on the user's tenant and admin status.

## Data Flow

### Read Flow
```
User API → NexusFS → Metadata Lookup → CAS Fetch → Return Content
                ↓
          Cache Check (if hit, return cached)
```

### Write Flow
```
User API → Hooks (before_write) → Hash Content → CAS Store →
Metadata Update → Operation Log → Hooks (after_write)
```

### Undo Flow
```
User Undo → Load Operation → Extract Undo State →
Reverse Operation → Log Undo → Return Success
```

## Backend Adapters

### Interface
```python
class BackendAdapter(ABC):
    async def read(self, path: str) -> bytes: ...
    async def write(self, path: str, data: bytes) -> None: ...
    async def delete(self, path: str) -> None: ...
    async def list(self, path: str) -> list[str]: ...
    async def exists(self, path: str) -> bool: ...
    async def stat(self, path: str) -> FileStat: ...
```

### Implementations
- **LocalFSBackend**: Local filesystem (`src/nexus/backends/local.py`)
- **S3Backend**: AWS S3 (`src/nexus/backends/s3.py`)
- **GCSBackend**: Google Cloud Storage (`src/nexus/backends/gcs.py`)
- **GDriveBackend**: Google Drive (partial)
- **WorkspaceBackend**: Agent workspace abstraction

## Database Schema

### Core Tables

**file_metadata:**
```sql
CREATE TABLE file_metadata (
    tenant_id UUID,
    file_path TEXT,
    content_hash VARCHAR(64),  -- SHA-256
    size_bytes BIGINT,
    created_at TIMESTAMPTZ,
    modified_at TIMESTAMPTZ,
    PRIMARY KEY (tenant_id, file_path)
);
```

**operations:**
```sql
CREATE TABLE operations (
    id UUID PRIMARY KEY,
    tenant_id UUID,
    operation_type VARCHAR(50),
    file_path TEXT,
    timestamp TIMESTAMPTZ,
    details JSONB,
    undo_state JSONB,
    undone BOOLEAN
);
```

**tags:**
```sql
CREATE TABLE tags (
    tenant_id UUID,
    file_path TEXT,
    tag_key VARCHAR(255),
    tag_value TEXT,
    PRIMARY KEY (tenant_id, file_path, tag_key)
);
```

## Key Design Decisions

### Why Content-Addressable Storage?
- **Automatic deduplication** (30-50% savings)
- **Immutable content** enables efficient caching
- **Lineage tracking** across copies
- **Time-travel** without storing full file copies

**Tradeoff:** Additional hash computation, metadata overhead

### Why SQLite for Local Mode?
- **Zero-deployment** (single file database)
- **ACID guarantees** for undo operations
- **Efficient queries** for time-travel
- **Easy backup**

**Tradeoff:** Single-writer limitation (solved by PostgreSQL in hosted mode)

### Why Plugin System?
- **Vendor neutrality** (core stays cloud-agnostic)
- **Extensibility** without forking
- **Community contributions**
- **Unix philosophy** (composable tools)

### Why YAML for Workflows?
- **Human-readable** and editable
- **Version control friendly** (Git-compatible)
- **Standard format** (no custom DSL)
- **Everything-as-a-file** principle

## Performance Characteristics

### Latency Targets (Local Mode)
- Read: < 5ms (cache hit), < 50ms (cache miss)
- Write: < 100ms (including hash + CAS + metadata)
- List: < 50ms for 1000 files
- Undo: < 200ms

### Throughput Targets
- Sequential reads: 100+ MB/s
- Sequential writes: 50+ MB/s
- Batch writes: 4x faster (write_batch API)
- Concurrent operations: 100+ ops/sec

### Scaling Limits (Local Mode)
- Files: 1M+ per tenant
- Storage: 10GB - 1TB typical
- Operations log: 10M+ operations

## Security

### Multi-Tenancy
- Tenant isolation at database level
- Path namespace isolation
- Per-tenant operation logs
- Per-tenant metadata

### Permission Model (ReBAC)
- Relationship-Based Access Control
- Permissions: read, write, delete, admin
- Directory → file inheritance

### Data Security
- SHA-256 content hashing
- Optional encryption at rest (backend-dependent)
- Append-only operation log
- Audit trail for compliance

## Deployment Modes

### Local Mode
```
Python Process
  ├── NexusFS Core
  ├── SQLite Database
  └── Local Filesystem (./nexus-data/)
```

### Hosted Mode (Auto-Scaling)
```
API Layer (FastAPI) → NexusFS Core → PostgreSQL (Managed)
                            ↓
                    Cloud Storage (GCS/S3)
```

## Future Enhancements

### Planned (v0.5+)
- Distributed CAS for multi-node deployments
- Event streaming (Kafka/Pub/Sub)
- Advanced query language (beyond glob/grep)
- Built-in vector search
- Multi-region replication

## References

- [Core Tenets](../CORE_TENETS.md)
- [Plugin Development](../development/PLUGIN_DEVELOPMENT.md)
- [Database Compatibility](../DATABASE_COMPATIBILITY.md)
- [Deployment Guide](../deployment/DEPLOYMENT.md)

---

**Document Status:** Living document, updated with each major release
**Next Review:** v0.5.0 release

---

## Permission System Deep Dive (v0.4.0+)

### Overview

Nexus implements a complete 3-tier permission system supporting both CLI and Python SDK:

1. **UNIX Permissions**: Traditional owner/group/mode (0644, etc.)
2. **ACL (Access Control Lists)**: Per-user and per-group granular permissions
3. **ReBAC (Relationship-Based)**: Zanzibar-style graph-based dynamic permissions

### Layer 1: UNIX Permissions

**Basic file access control using owner/group/mode bits.**

**CLI:**
```bash
nexus chmod 0o644 /workspace/file.txt
nexus chown alice /workspace/file.txt
nexus chgrp developers /workspace/file.txt
```

**Python SDK:**
```python
nx.chmod("/workspace/file.txt", 0o644)
nx.chown("/workspace/file.txt", "alice")
nx.chgrp("/workspace/file.txt", "developers")
```

### Layer 2: ACL (Access Control Lists)

**Fine-grained per-user and per-group permissions.**

**CLI:**
```bash
# Grant user permissions
nexus setfacl user:alice:rw- /workspace/file.txt

# Grant group permissions
nexus setfacl group:developers:r-x /workspace/code/

# Deny user access (explicit deny)
nexus setfacl deny:user:intern:--- /workspace/secret.txt

# View ACL
nexus getfacl /workspace/file.txt

# Remove ACL entry
nexus setfacl user:alice:rw- /workspace/file.txt --remove
```

**Python SDK (NEW in v0.4.0):**
```python
# Grant user permissions
nx.grant_user("/workspace/file.txt", user="alice", permissions="rw-")

# Grant group permissions
nx.grant_group("/workspace/file.txt", group="developers", permissions="r--")

# Explicit deny (takes precedence)
nx.deny_user("/workspace/secret.txt", user="intern")

# Get ACL entries
acl = nx.get_acl("/workspace/file.txt")
# Returns: [{'entry_type': 'user', 'identifier': 'alice', 'permissions': 'rw-', 'deny': False}]

# Revoke permissions
nx.revoke_acl("/workspace/file.txt", entry_type="user", identifier="alice")
```

**Use Cases:**
- Share file with specific users without changing ownership
- Temporarily grant contractor access
- Block specific user while allowing group
- Mix different permissions for different users

### Layer 3: ReBAC (Relationship-Based Access Control)

**Dynamic graph-based permissions inspired by Google Zanzibar.**

**CLI:**
```bash
# Create relationships
nexus rebac create agent alice member-of group developers
nexus rebac create group developers owner-of file /workspace/project.txt

# Check permission (with graph traversal)
nexus rebac check agent alice owner-of file /workspace/project.txt

# Find all who can access
nexus rebac expand owner-of file /workspace/project.txt

# Delete relationship
nexus rebac delete <tuple-id>
```

**Python SDK (NEW in v0.4.0):**
```python
# Create relationship tuple
tuple_id = nx.rebac_create(
    subject=("agent", "alice"),
    relation="member-of",
    object=("group", "developers")
)

# Create ownership relationship
nx.rebac_create(
    subject=("group", "developers"),
    relation="owner-of",
    object=("file", "/workspace/project.txt")
)

# Check permission (automatic graph traversal)
can_access = nx.rebac_check(
    subject=("agent", "alice"),
    permission="owner-of",
    object=("file", "/workspace/project.txt")
)
# Returns: True (alice → member-of → developers → owner-of → file)

# Find all subjects with permission
subjects = nx.rebac_expand(
    permission="owner-of",
    object=("file", "/workspace/project.txt")
)
# Returns: [("agent", "alice"), ("agent", "bob"), ("group", "developers")]

# List relationships
tuples = nx.rebac_list_tuples(subject=("agent", "alice"))

# Delete relationship
deleted = nx.rebac_delete(tuple_id)

# Temporary access (expires automatically)
from datetime import UTC, datetime, timedelta
expires = datetime.now(UTC) + timedelta(hours=1)
nx.rebac_create(
    subject=("agent", "contractor"),
    relation="viewer-of",
    object=("file", "/workspace/doc.txt"),
    expires_at=expires
)
```

**Relationship Types:**
- `member-of`: Group membership
- `owner-of`: Resource ownership
- `viewer-of`: Read access
- `editor-of`: Write access
- `parent-of`: Hierarchical relationship (folder → file)
- `part-of`: Organization hierarchy (team → department)

**Use Cases:**
- Team-based access (add to group = auto access all group resources)
- Hierarchical permissions (folder ownership → file ownership)
- Organization structures (teams within departments)
- Dynamic sharing (relationship changes = permission changes)
- Temporary access with auto-expiration

### Permission Check Order

When checking access, Nexus evaluates permissions in this order:

```
1. Admin/System Bypass
   ↓ (if not admin)
2. ReBAC Check (relationship graph traversal)
   ↓ (if no ReBAC match)
3. ACL Check (explicit allow/deny entries)
   ↓ (if no ACL match)
4. UNIX Check (owner/group/mode bits)
   ↓ (if no UNIX match)
5. Deny (default)
```

**Key Principles:**
- **ReBAC grants** allow access (dynamic inheritance)
- **ACL deny** blocks access (explicit deny takes precedence)
- **ACL allow** grants access (explicit permission)
- **UNIX** provides baseline (traditional permissions)

### Database Tables

**ACL Tables:**
```sql
acl_entries (
    path_id, entry_type, identifier,
    permissions, deny, is_default, created_at
)
```

**ReBAC Tables (NEW in v0.4.0):**
```sql
rebac_tuples (
    tuple_id, subject_type, subject_id, relation,
    object_type, object_id, created_at, expires_at, conditions
)

rebac_namespaces (
    namespace_id, object_type, config,
    created_at, updated_at
)

rebac_changelog (
    change_id, change_type, tuple_id,
    subject_type, subject_id, relation,
    object_type, object_id, created_at
)

rebac_check_cache (
    cache_id, subject_type, subject_id, permission,
    object_type, object_id, result, created_at, expires_at
)
```

### Performance Optimizations

**ACL:**
- Indexed by path_id for fast lookups
- Cached at metadata store level

**ReBAC:**
- Check result caching with TTL (default 5 minutes)
- Graph traversal depth limit (default 10 hops)
- Cycle detection to prevent infinite loops
- Automatic cleanup of expired relationships

### Examples

See comprehensive examples in:
- **Python API**: `examples/py_demo/acl_demo.py`, `examples/py_demo/rebac_demo.py`
- **CLI + Python**: `examples/script_demo/acl_demo.sh`, `examples/script_demo/rebac_demo.sh`
