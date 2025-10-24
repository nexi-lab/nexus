# Nexus Architecture

## Overview

Nexus is an AI-native distributed filesystem that provides a unified API across multiple storage backends with advanced features for AI agent workflows.

**Version:** 0.4.0
**Last Updated:** 2025-10-23

## System Architecture

```
┌───────────────────────────────────────────────────────────────────┐
│                        Nexus Filesystem                            │
├───────────────────────────────────────────────────────────────────┤
│           Interface Layer (User-Facing APIs)                       │
│  ┌──────────────┬──────────────────┬─────────────────────┐       │
│  │ CLI Commands │   Python SDK     │    MCP Server       │       │
│  │ (nexus.cli)  │ (nexus.connect()) │ (Model Context)     │       │
│  └──────────────┴──────────────────┴─────────────────────┘       │
├───────────────────────────────────────────────────────────────────┤
│                   Core Components Layer                            │
│  ┌──────────┬──────────┬──────────┬──────────┬──────────────┐   │
│  │ NexusFS  │  Plugin  │   Work   │ Workflow │   Skills     │   │
│  │   Core   │  System  │  Queue   │  Engine  │   System     │   │
│  ├──────────┼──────────┴──────────┴──────────┴──────────────┤   │
│  │   LLM    │          ReBAC Permissions System              │   │
│  │ Provider │        (Relationship-Based Access Control)     │   │
│  └──────────┴────────────────────────────────────────────────┘   │
├───────────────────────────────────────────────────────────────────┤
│                      Storage Layer                                 │
│  ┌────────────┬──────────────┬─────────────┬─────────────┐       │
│  │  Metadata  │ Content-Addr │  Operation  │   Caching   │       │
│  │   Store    │   Storage    │     Log     │   System    │       │
│  │ (SQLite/   │    (CAS)     │ (Time-      │ (Content +  │       │
│  │ Postgres)  │ (SHA-256)    │  Travel)    │  Metadata)  │       │
│  └────────────┴──────────────┴─────────────┴─────────────┘       │
├───────────────────────────────────────────────────────────────────┤
│                      Backend Adapters                              │
│  ┌──────────┬──────────┬──────────┬──────────┬──────────────┐   │
│  │  Local   │   GCS    │   S3*    │ GDrive*  │  Workspace   │   │
│  │   FS     │ (Google) │  (AWS)   │ (Google) │   Backend    │   │
│  └──────────┴──────────┴──────────┴──────────┴──────────────┘   │
│                      * = Partial/Planned                          │
└───────────────────────────────────────────────────────────────────┘
```

## Core Components

### 1. NexusFS Core
The central filesystem abstraction providing unified file operations across all backends.

**Location:** `src/nexus/core/nexus_fs.py`

**Key Features:**
- Async-first API
- Multi-backend routing via PathRouter
- Permission enforcement (ReBAC)
- Operation logging for time-travel
- Content-addressable storage integration
- Batch write operations (4x faster)

### 2. LLM Provider Abstraction (v0.4.0)
Unified interface for multiple LLM providers with KV cache management.

**Location:** `src/nexus/llm/`

**Key Features:**
- Multi-provider support (Anthropic, OpenAI, Google, Ollama, etc.)
- Automatic KV cache management (50-90% cost savings)
- Token counting and cost tracking
- Streaming response support
- Provider-agnostic API via LiteLLM

**Example:**
```python
from nexus.llm import get_provider

provider = get_provider("anthropic")
response = await provider.complete(
    prompt="Summarize this document",
    model="claude-sonnet-4"
)
```

### 3. Plugin System
Extensible architecture for vendor integrations.

**Location:** `src/nexus/plugins/`

**Components:**
- `base.py`: NexusPlugin base class
- `registry.py`: Plugin discovery and management
- `hooks.py`: Lifecycle hooks system
- `cli.py`: CLI integration

**Plugin Interface:**
```python
class NexusPlugin(ABC):
    def metadata(self) -> PluginMetadata: ...
    def commands(self) -> dict[str, Callable]: ...
    def hooks(self) -> dict[str, Callable]: ...
    def initialize(self, config: dict): ...
    def shutdown(self): ...
```

**Lifecycle Hooks:**
- `before_write`, `after_write`
- `before_read`, `after_read`
- `before_delete`, `after_delete`
- `before_mkdir`, `after_mkdir`
- `before_copy`, `after_copy`

**Example Plugins:**
- `nexus-plugin-anthropic`: Claude Skills API integration
- `nexus-plugin-skill-seekers`: Generate skills from documentation

### 5. Work Queue System
File-based job queue with SQL views for efficient querying.

**Location:** `src/nexus/storage/views.py`

**How It Works:**
Jobs are regular files with metadata. No separate job system needed - follows "Everything as a File" principle.

**Job Metadata Schema:**
```python
# Create a job (just a file)
nx.write("/jobs/task1.json", b'{"action": "process"}')

# Add work metadata
nx.metadata.set_file_metadata("/jobs/task1.json", "status", "ready")
nx.metadata.set_file_metadata("/jobs/task1.json", "priority", 1)
nx.metadata.set_file_metadata("/jobs/task1.json", "tags", ["urgent"])
```

**Metadata Fields:**
- `status`: `ready` | `pending` | `blocked` | `in_progress` | `completed` | `failed`
- `priority`: Integer (lower = higher priority)
- `depends_on`: Path ID of dependency (creates blocking relationship)
- `worker_id`: ID of processing worker
- `started_at`: ISO timestamp

**SQL Views (O(n) performance):**
- `ready_work_items`: Jobs ready to process (status='ready', no blockers)
- `pending_work_items`: Jobs in backlog (status='pending')
- `blocked_work_items`: Jobs waiting on dependencies
- `in_progress_work`: Jobs currently running
- `work_by_priority`: All jobs sorted by priority

**CLI Commands:**
```bash
nexus work ready --limit 10    # Get ready jobs
nexus work status              # Queue statistics
nexus work blocked             # Find bottlenecks
```

**Python API:**
```python
ready_jobs = nx.metadata.get_ready_work(limit=10)
pending = nx.metadata.get_pending_work()
blocked = nx.metadata.get_blocked_work()
```

**Note:** This provides job state management infrastructure. Users implement their own execution logic.

### 6. Workflow Engine (v0.4.0)
Event-driven automation for document processing and multi-step operations.

**Location:** `src/nexus/workflows/`

**Components:**
- **Triggers** (`triggers.py`): File events, schedules, manual invocation
- **Actions** (`actions.py`): Built-in + plugin actions
- **Engine** (`engine.py`): Workflow execution with DAG resolution
- **Storage** (`storage.py`): Persistent workflow state
- **Loader** (`loader.py`): YAML DSL parser

**Workflow Storage:** `.nexus/workflows/*.yaml`

**Example Workflow:**
```yaml
name: process-invoices
triggers:
  - type: file
    pattern: /invoices/*.pdf
    event: create
actions:
  - name: parse-invoice
    type: parse_document
  - name: extract-data
    type: llm_query
    config:
      prompt: "Extract invoice details"
  - name: save-result
    type: write_file
    config:
      path: /processed/{filename}.json
```

### 7. Skills System
Vendor-neutral skill management with three-tier hierarchy and governance.

**Location:** `src/nexus/skills/`

**Hierarchy:**
```
/system/skills/          # System-wide, read-only
/shared/skills/          # Tenant-wide, shared
/workspace/.nexus/skills/ # Agent-specific
```

**Features:**
- Dependency resolution with DAG and cycle detection
- Skill versioning and lineage tracking
- Export/import workflows
- Approval governance for org-wide skills
- Analytics for skill effectiveness

**SKILL.md Format:**
```markdown
---
name: skill-name
version: 1.0.0
description: Skill description
tier: agent|tenant|system
requires: [dependency-skill]
---
# Skill Content
```

### 8. ReBAC Permissions System
Relationship-Based Access Control for fine-grained security.

**Location:** `src/nexus/core/permissions.py`, `src/nexus/core/permission_policy.py`

**Permission Types:**
- `read`: View file content
- `write`: Modify files
- `delete`: Remove files
- `admin`: Full control + permission grants

**Features:**
- Directory → file permission inheritance
- Policy-based access control
- Tenant isolation
- Namespace-level readonly enforcement
- Admin-only namespaces (`/system`)

**Example:**
```python
# Grant read permission
nx.permissions.grant("/shared/docs", user_id, Permission.READ)

# Check permission
has_access = nx.permissions.check("/shared/docs/file.txt", Permission.WRITE)
```

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
