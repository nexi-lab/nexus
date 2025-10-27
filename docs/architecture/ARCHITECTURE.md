# Nexus Architecture

**Version:** 0.6.0 | **Last Updated:** 2025-10-26

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
  - [Permission System](#7-rebac-permission-system-v060)
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
- **ReBAC Permissions**: Pure Zanzibar-style relationship-based access control
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
│   Permissions (ReBAC)  │  Memory System             │
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
- **Permission Enforcement**: Integrated ReBAC permission system
- **Operation Logging**: Complete audit trail for time-travel and undo
- **CAS Integration**: Automatic content deduplication via SHA-256 hashing
- **Batch Operations**: 4x faster bulk writes via `write_batch()`
- **Async-First Design**: Non-blocking I/O for scalability

**Implementation:** Mixin-based architecture separating concerns:
- `NexusFSCoreMixin`: Core read/write/delete operations
- `NexusFSReBACMixin`: Relationship-based access control (fully remote-capable via RPC)
- `NexusFSSearchMixin`: Semantic and keyword search
- `NexusFSVersionsMixin`: Workspace snapshots and versioning
- `NexusFSMountsMixin`: Mount management for virtual filesystem views

**RPC Exposure:** All public methods use `@rpc_expose` decorator for automatic remote access via HTTP/RPC protocol. RPC parity is automatically enforced in CI to prevent local-only methods.

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

### 7. ReBAC Permission System (v0.6.0+)

**Purpose:** Pure relationship-based access control using Google Zanzibar principles for scalable, flexible authorization.

**Location:** `src/nexus/core/permissions.py`, `nexus_fs_rebac.py`, `rebac_manager.py`

**Architecture:** Pure ReBAC (Relationship-Based Access Control) - all UNIX-style permissions and ACLs removed in v0.6.0.

**Permission Model:**

- **Subject-Based Identity**: Identity specified per-operation, not per-instance
  - Types: `user`, `agent`, `service`, `group`, custom entity types
  - Examples: `("user", "alice")`, `("agent", "claude_001")`, `("service", "bootstrap")`

- **Relationship Tuples**: All permissions expressed as `(subject, relation, object)` tuples
  - Direct Relations: `direct_owner`, `direct_editor`, `direct_viewer`
  - Computed Relations: `owner`, `editor`, `viewer` (unions of direct + inherited)
  - Permissions: `read`, `write`, `execute` (map to relations via namespace config)

- **Object Types**: `file`, `memory`, `workspace`, custom resource types
  - Examples: `("file", "/workspace/doc.txt")`, `("memory", "mem_123")`

**Key Capabilities:**
- Complete CLI + Python SDK for ReBAC operations (`nexus rebac create/check/list/delete`)
- **Full Remote Support**: All permission operations work via RPC (local/remote parity)
- Automatic permission inheritance via parent relationships
- Time-limited access with expiration timestamps
- Multi-level organization hierarchies (tenant → workspace → user → agent)
- Multi-tenant isolation with tenant-aware permission checks
- Centralized permission management in client-server deployments
- Graph-based permission checking with caching for performance

**Permission Check Order:** Admin bypass → ReBAC relation check → Deny (default)

**Permission Hierarchy:**
```
owner (full access)
  └── write (includes read)
       └── read (view only)

Relations:
- owner = direct_owner ∪ parent_owner
- editor = direct_editor ∪ owner
- viewer = direct_viewer ∪ editor
```

**Examples:** See `examples/py_demo/rebac_demo.py`, `rebac_comprehensive_demo.py`, `rebac_advanced_demo.py`

**Detailed Documentation:** See [PERMISSIONS.md](../PERMISSIONS.md) for comprehensive guide

### 8. Identity-Based Memory System (v0.4.0)

**Purpose:** Order-neutral virtual paths with identity-based storage for AI agent memory.

**Location:** `src/nexus/core/entity_registry.py`, `src/nexus/core/memory_router.py`, `src/nexus/core/memory_api.py`

**Core Concept:** Separates identity from location. Canonical storage by ID with multiple virtual path views. Memory location ≠ identity; relationships determine access, paths determine browsing.

**Key Features:**
- **Order-Neutral Paths**: `/workspace/alice/agent1` and `/workspace/agent1/alice` resolve to same memory
- **Zero Duplication**: Memory sharing across agents without file copies
- **Dual API Access**: Use Memory API (`nx.memory.*`) or File API (`nx.read/write`) interchangeably
- **Multi-View Browsing**: Access by user, agent, or tenant perspective
- **Permission Integration**: Full ReBAC permission system support

**Storage Structure:**
- **Entity Registry**: Tracks tenant/user/agent relationships and hierarchies
- **Memories Table**: Stores memory content with identity metadata (tenant_id, user_id, agent_id, scope, visibility)
- **Virtual Router**: Maps flexible paths to canonical memory IDs

**Memory Path Patterns (all equivalent):**
```
/objs/memory/{id}                     # Canonical storage
/workspace/alice/agent1/memory/...    # Workspace view (order-neutral)
/memory/by-user/alice/...             # User-centric view
/memory/by-agent/agent1/...           # Agent-centric view
```

**Example Use Case:**
Alice's two agents share user-scoped memories. Agent1 creates memory → Agent2 can access via user ownership relationship → no file duplication required.

**Examples:** See `examples/py_demo/memory_file_api_demo.py`

### 9. RPC Parity Enforcement System (v0.4.0+)

**Purpose:** Automated verification that all NexusFS methods work identically in local and remote modes.

**Location:** `src/nexus/core/rpc_decorator.py`, `tests/unit/test_rpc_parity.py`

**Problem Solved:** Previously, adding methods to NexusFS without exposing them via RPC created inconsistencies between local and remote modes. This led to features that only worked locally.

**Solution:** Automated enforcement at two levels:

1. **@rpc_expose Decorator**: All public NexusFS methods must be decorated to auto-register with RPC server
2. **CI Enforcement**: Automated test blocks PRs if new public methods lack `@rpc_expose` or explicit exclusion

**Key Features:**
- **Automatic Registration**: Decorated methods auto-register with RPC protocol
- **Zero Manual Dispatch**: Server automatically routes RPC calls to decorated methods
- **CI Blocking**: PRs fail if parity is broken
- **Clear Error Messages**: Test output shows exactly which methods need attention

**Method Exposure Options:**

1. **Expose via RPC** (default): Add `@rpc_expose` decorator + implement in `RemoteNexusFS`
2. **Mark Internal-Only** (rare): Add to `INTERNAL_ONLY_METHODS` exclusion list with justification

**Example:**
```python
from nexus.core.rpc_decorator import rpc_expose

@rpc_expose(description="Create ReBAC relationship")
def rebac_create(self, subject, relation, object, tenant_id=None) -> bool:
    """Create a ReBAC relationship tuple."""
    # Implementation
```

**CI Integration:** Separate `rpc-parity` job runs before main tests, ensuring all methods are properly exposed.

**Benefits:**
- ✅ **Guaranteed Parity**: Local and remote modes always have same capabilities
- ✅ **No Manual Tracking**: Automated detection of missing RPC exposure
- ✅ **Early Detection**: Catches issues at PR time, not in production
- ✅ **Documentation**: `@rpc_expose` serves as self-documenting API contract

**Detailed Guide:** See `docs/RPC_PARITY_GUIDE.md`

## Storage Layer

### Content-Addressable Storage (CAS)

**Purpose:** Automatic deduplication using SHA-256 content hashing.

**Location:** `src/nexus/backends/local.py`, `src/nexus/storage/`

**How It Works:** Content is stored by hash (e.g., `cas/ab/abcd123...`). Identical content stored once, referenced many times.

**Benefits:**
- 30-50% storage savings via deduplication
- Immutable content enables efficient caching
- Lineage tracking across file copies
- Efficient time-travel without storing full copies

### Operation Log & Time-Travel

**Purpose:** Complete audit trail with undo capability.

**Location:** `src/nexus/storage/operations.py`

**Key Features:**
- All filesystem operations logged to database
- Undo capability for reversible operations (write, delete, move, copy)
- Time-travel: read files at any historical point
- Content diffing between versions
- Multi-agent safe with per-agent tracking

**CLI:** `nexus ops log`, `nexus ops undo`, `nexus time-travel`

### Caching System (v0.4.0)

**Purpose:** Multi-tier caching for performance optimization.

**Location:** `src/nexus/storage/cache.py`, `src/nexus/storage/content_cache.py`

**Cache Tiers:**
1. **Metadata Cache**: File metadata, path lookups, existence checks
2. **Content Cache**: LRU cache for file content (256MB default)
3. **Permission Cache**: Permission check results with TTL

**Performance Impact:**
- Cached reads: 10-50x faster
- Metadata operations: 5x faster
- Configurable sizes and TTLs

## Namespace System

**Purpose:** Organize files into namespaces with different access control and visibility rules.

**Location:** `src/nexus/core/router.py`

### Built-in Namespaces

| Namespace | Purpose | Readonly | Admin-Only | Tenant Required |
|-----------|---------|----------|------------|-----------------|
| `/workspace` | Agent-specific workspace | No | No | Yes |
| `/shared` | Tenant-wide shared files | No | No | Yes |
| `/archives` | Long-term storage | Yes | No | Yes |
| `/external` | External integrations | No | No | No |
| `/system` | System configuration | Yes | Yes | No |

**Visibility:** Namespaces are automatically filtered based on user context (tenant_id, is_admin).

**FUSE Integration:** When mounting via FUSE, namespace directories appear at root level dynamically based on access rights.

## Data Flow

### Read Flow
```
User API → NexusFS → Cache Check → (if miss) → Metadata Lookup → CAS Fetch → Return Content
```

### Write Flow
```
User API → Hooks (before_write) → Hash Content → CAS Store →
Metadata Update → Operation Log → Hooks (after_write) → Cache Invalidation
```

### Undo Flow
```
User Undo → Load Operation → Extract Undo State → Reverse Operation → Log Undo
```

## Backend Adapters

**Purpose:** Abstract storage backends behind unified interface.

**Interface:** `Backend` base class with read, write, delete, list, exists, stat methods.

**Implementations:**
- `LocalFSBackend`: Local filesystem with CAS support
- `GCSBackend`: Google Cloud Storage
- `S3Backend`: AWS S3 (partial)
- `GDriveBackend`: Google Drive (partial)
- `WorkspaceBackend`: Agent workspace abstraction

**Location:** `src/nexus/backends/`

## Key Design Decisions

### Why Content-Addressable Storage?
**Benefits:** 30-50% storage savings, immutable content enables caching, lineage tracking, time-travel without full copies
**Tradeoff:** Hash computation overhead

### Why SQLite for Local Mode?
**Benefits:** Zero-deployment, ACID guarantees, easy backup
**Tradeoff:** Single-writer limitation (solved by PostgreSQL in hosted mode)

### Why Plugin System?
**Benefits:** Vendor neutrality, extensibility without forking, community contributions, composable tools
**Philosophy:** Unix philosophy of composable tools

### Why YAML for Workflows?
**Benefits:** Human-readable, Git-friendly, standard format (no custom DSL), everything-as-a-file principle

## Performance Characteristics

### Latency Targets (Local Mode)
- **Read**: < 5ms (cached), < 50ms (uncached)
- **Write**: < 100ms (including hash + CAS + metadata)
- **List**: < 50ms for 1000 files
- **Undo**: < 200ms

### Throughput Targets
- **Sequential reads**: 100+ MB/s
- **Sequential writes**: 50+ MB/s
- **Batch writes**: 4x faster than individual writes
- **Concurrent operations**: 100+ ops/sec

### Scaling Limits (Local Mode)
- **Files**: 1M+ per tenant
- **Storage**: 10GB - 1TB typical
- **Operations log**: 10M+ operations

## Security

### Multi-Tenancy
- Tenant isolation at database level
- Path namespace isolation
- Per-tenant operation logs and metadata

### Permission Model
- **Pure ReBAC**: Zanzibar-style relationship-based access control
- **Permissions**: read, write, execute (mapped from relations)
- **Relations**: owner, editor, viewer (with direct_ variants)
- **Inheritance**: Directory → file inheritance via parent relationships
- **Multi-tenant**: Complete tenant isolation in permission checks

### Data Security
- SHA-256 content hashing for integrity
- Optional encryption at rest (backend-dependent)
- Append-only operation log
- Complete audit trail for compliance

## Deployment Modes

### Local Mode
Single Python process with SQLite and local filesystem. Ideal for development and CLI tools.

### Hosted Mode (Auto-Scaling)
API layer (FastAPI) → NexusFS Core → PostgreSQL + Cloud Storage (GCS/S3). Auto-scales based on usage.

**See:** [Deployment Guide](../deployment/DEPLOYMENT.md)

## References

- [Core Tenets](../CORE_TENETS.md) - Design principles and philosophy
- [Plugin Development](../development/PLUGIN_DEVELOPMENT.md) - Building extensions
- [Permission System](./PERMISSIONS.md) - Comprehensive permission guide
- [Database Compatibility](../DATABASE_COMPATIBILITY.md) - SQLite vs PostgreSQL
- [Deployment Guide](../deployment/DEPLOYMENT.md) - Production deployment

---

**Document Status:** Living document, updated with each major release
**Next Review:** v0.5.0 release
