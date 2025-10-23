# Nexus Architecture

## Overview

Nexus is an AI-native distributed filesystem that provides a unified API across multiple storage backends with advanced features for AI agent workflows.

**Version:** 0.4.0
**Last Updated:** 2025-10-23

## System Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     Nexus Filesystem                         │
├─────────────────────────────────────────────────────────────┤
│  CLI Layer          │  Python SDK        │  MCP Server      │
├─────────────────────────────────────────────────────────────┤
│                    Core Components Layer                     │
│  ┌──────────┬──────────┬──────────┬──────────┬──────────┐  │
│  │ Metadata │  Plugin  │ Workflow │   Job    │  Skills  │  │
│  │  System  │  System  │  Engine  │  System  │  System  │  │
│  └──────────┴──────────┴──────────┴──────────┴──────────┘  │
├─────────────────────────────────────────────────────────────┤
│                  Storage Abstraction Layer                   │
│  ┌──────────────────────────────────────────────────────┐  │
│  │  CAS Layer  │  Ops Log  │  Version Control  │  Cache │  │
│  └──────────────────────────────────────────────────────┘  │
├─────────────────────────────────────────────────────────────┤
│                      Backend Adapters                        │
│  ┌────────┬─────────┬──────────┬──────────┬───────────┐   │
│  │ Local  │   S3    │  GDrive  │   GCS    │ Workspace │   │
│  │   FS   │         │          │          │  Backend  │   │
│  └────────┴─────────┴──────────┴──────────┴───────────┘   │
└─────────────────────────────────────────────────────────────┘
```

## Core Components

### 1. NexusFS Core
The central filesystem abstraction providing unified file operations across all backends.

**Location:** `src/nexus/core/nexus_fs.py`

**Key Features:**
- Async-first API
- Multi-backend routing
- Permission enforcement (ReBAC)
- Operation logging
- Content-addressable storage

### 2. Content-Addressable Storage (CAS)
Automatic deduplication using SHA-256 hashing.

**Location:** `src/nexus/storage/cas.py`

**Benefits:**
- 30-50% storage savings
- Immutable content for caching
- Lineage tracking
- Efficient time-travel

### 3. Operation Log & Time-Travel
Complete audit trail with undo capability.

**Location:** `src/nexus/storage/operations.py`

**Features:**
- All filesystem operations logged
- Undo capability for reversible operations
- Time-travel: read files at historical points
- Content diffing between versions

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

### 4. Plugin System
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

### 5. Workflow Engine (v0.4.0)
Event-driven automation for document processing.

**Location:** `src/nexus/workflows/` (planned)

**Components:**
- Trigger System (file events, schedules, webhooks)
- Action Registry (built-in + plugin actions)
- YAML DSL parser
- Execution engine

**Workflow Storage:** `.nexus/workflows/*.yaml`

### 6. Skills System
Vendor-neutral skill management with three-tier hierarchy.

**Location:** `src/nexus/skills/`

**Hierarchy:**
```
/system/skills/          # System-wide, read-only
/shared/skills/          # Tenant-wide, shared
/workspace/.nexus/skills/ # Agent-specific
```

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
