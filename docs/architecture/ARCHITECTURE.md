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
