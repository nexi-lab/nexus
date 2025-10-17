# Nexus Examples

This directory contains example code demonstrating Nexus embedded mode functionality.

## Available Examples

### **embedded_demo.py** - Complete Embedded Mode Demo ⭐

Comprehensive demonstration of Nexus embedded filesystem with all v0.1.0 features.

**Features:**
- 📁 High-level Embedded filesystem API (user-facing)
- 💾 SQLAlchemy Metadata Store integration
- 🏗️ Path routing and directory operations
- 🔒 Multi-tenant and agent isolation
- 🎯 Custom namespace support
- 📦 Content-addressable storage (CAS)
- 🔄 Automatic content deduplication
- 🏷️ Custom metadata capabilities

**What you'll see:**
1. **Part 1-3**: Basic file operations, metadata, persistence
2. **Part 4**: Path routing and directory operations
3. **Part 5**: Multi-mount configuration (educational)
4. **Part 6**: Namespace & tenant isolation (educational + user-facing)
5. **Part 7**: End-to-end tenant isolation (recommended approach) ✅
6. **Part 8**: Content-addressable storage (CAS) with deduplication

### **config_usage_demo.py** - Configuration Examples 🎛️

Shows different ways to configure Nexus with custom namespaces and multi-tenant settings.

**Examples:**
1. Dict config (programmatic)
2. YAML config files (declarative)
3. Multi-tenant isolation
4. Admin access override

### **config-basic.yaml** - Basic Configuration Template

Simple starter template for Nexus configuration.

### **config-multi-tenant.yaml** - Multi-Tenant Configuration

Complete example showing:
- Tenant isolation setup
- Custom namespace definitions
- Real-world SaaS application structure

## Quick Start

### Prerequisites

```bash
# Install Nexus in development mode
pip install -e .

# Or just install dependencies
pip install sqlalchemy alembic pydantic pyyaml
```

### Running the Examples

```bash
# Main comprehensive demo
PYTHONPATH=src python examples/embedded_demo.py

# Configuration examples
PYTHONPATH=src python examples/config_usage_demo.py
```

## What These Examples Show

### 1. Basic File Operations

```python
import nexus

# Initialize - auto-detects mode from config
nx = nexus.connect(config={"data_dir": "./nexus-data"})

# Write files
nx.write("/documents/report.pdf", b"PDF content...")
nx.write("/images/photo.jpg", b"JPEG data...")

# Read files
content = nx.read("/documents/report.pdf")

# List files
files = nx.list()  # All files
data_files = nx.list(prefix="/data")  # Filter by prefix

# Delete files
nx.delete("/images/photo.jpg")

# Directory operations
nx.mkdir("/workspace/agent1/data", parents=True)
nx.rmdir("/workspace/agent1/data", recursive=True)
nx.is_directory("/workspace/agent1")

# Close
nx.close()
```

### 2. Multi-Tenant Isolation (NEW in v0.1.0!)

```python
import nexus

# Tenant ACME
nx_acme = nexus.connect(config={
    "data_dir": "./nexus-data",
    "tenant_id": "acme",
    "agent_id": "agent1"
})

# Tenant TechInc
nx_tech = nexus.connect(config={
    "data_dir": "./nexus-data",
    "tenant_id": "techinc",
    "agent_id": "agent1"
})

# ACME writes to their workspace
nx_acme.write("/workspace/acme/agent1/secret.txt", b"ACME data")

# TechInc CANNOT read ACME's data (automatic isolation)
try:
    nx_tech.read("/workspace/acme/agent1/secret.txt")
except AccessDeniedError:
    print("✓ Tenant isolation enforced!")

# Agents in same tenant can share via /shared
nx_acme.write("/shared/acme/team-data.json", b'{"project": "collaboration"}')
nx_agent2 = nexus.connect(config={
    "data_dir": "./nexus-data",
    "tenant_id": "acme",
    "agent_id": "agent2"
})
data = nx_agent2.read("/shared/acme/team-data.json")  # ✓ Works!
```

### 3. Custom Namespaces (NEW in v0.1.0!)

**Option A: Dict Config**

```python
import nexus

nx = nexus.connect(config={
    "data_dir": "./nexus-data",
    "tenant_id": "acme",
    "namespaces": [
        {
            "name": "analytics",
            "readonly": False,
            "admin_only": False,
            "requires_tenant": True
        },
        {
            "name": "audit",
            "readonly": False,
            "admin_only": True,
            "requires_tenant": False
        }
    ]
})

# Use custom namespace
nx.write("/analytics/acme/daily_report.json", b'{"revenue": 50000}')

# Admin-only namespace (will fail for non-admin)
nx.write("/audit/access.log", b"log entry")  # ❌ AccessDeniedError
```

**Option B: YAML Config**

Create `nexus.yaml`:

```yaml
mode: embedded
data_dir: ./nexus-data
tenant_id: acme
agent_id: agent1

namespaces:
  - name: analytics
    readonly: false
    admin_only: false
    requires_tenant: true

  - name: audit
    readonly: false
    admin_only: true
    requires_tenant: false
```

```python
import nexus

# Auto-discovers nexus.yaml
nx = nexus.connect()

# Or explicitly specify
nx = nexus.connect(config="path/to/nexus.yaml")
```

### 4. Content-Addressable Storage (CAS) (NEW in v0.1.0!)

**Automatic Deduplication:**

```python
import nexus

nx = nexus.connect(config={"data_dir": "./nexus-data"})

# Write same content to different paths
content = b"This is important data"
nx.write("/documents/data.txt", content)
nx.write("/reports/summary.txt", content)  # Same content!

# Only stored ONCE in CAS - automatic deduplication
# Reference count: 2
# Physical copies: 1
# Space saved automatically!

# Delete one file
nx.delete("/documents/data.txt")
# Content still exists (ref_count=1)

# Delete second file
nx.delete("/reports/summary.txt")
# Content automatically removed (ref_count=0)
```

### 5. Metadata Export/Import (NEW in v0.1.0!)

**Backup and Migration:**

Nexus supports exporting and importing metadata in JSONL format for easy backup and migration.

```python
import nexus

nx = nexus.connect(config={"data_dir": "./nexus-data"})

# Export all metadata to JSONL file
count = nx.export_metadata("backup.jsonl")
print(f"Exported {count} files")

# Export only specific prefix
count = nx.export_metadata("workspace-backup.jsonl", prefix="/workspace")

# Import metadata from JSONL file
imported, skipped = nx.import_metadata("backup.jsonl")
print(f"Imported {imported} files, skipped {skipped}")

# Import with overwrite
imported, skipped = nx.import_metadata("backup.jsonl", overwrite=True)

nx.close()
```

**CLI Commands:**

```bash
# Export all metadata
nexus export metadata-backup.jsonl

# Export with prefix filter
nexus export workspace-backup.jsonl --prefix /workspace

# Import metadata
nexus import metadata-backup.jsonl

# Import with overwrite
nexus import metadata-backup.jsonl --overwrite
```

**JSONL Format:**

Each line in the export file is a JSON object containing:
- `path`: Virtual file path
- `size`: File size in bytes
- `etag`: Content hash (SHA-256)
- `backend_name`: Backend identifier
- `physical_path`: Physical storage path (content hash in CAS)
- `mime_type`: MIME type (optional)
- `created_at`: Creation timestamp (ISO format)
- `modified_at`: Modification timestamp (ISO format)
- `version`: Version number
- `custom_metadata`: Custom key-value metadata (optional)

**Important Notes:**
- Export/import only handles metadata, not file content
- Content must already exist in CAS storage (matched by content hash)
- Useful for disaster recovery, migration, and auditing
- Human-readable JSON format for easy inspection

### 6. Default Namespaces

Nexus provides 5 default namespaces out of the box:

| Namespace | Path Format | Description | Access Control |
|-----------|-------------|-------------|----------------|
| **workspace** | `/workspace/{tenant}/{agent}/...` | Agent scratch space | Tenant + Agent isolation |
| **shared** | `/shared/{tenant}/...` | Tenant-wide shared data | Tenant isolation |
| **external** | `/external/...` | Pass-through backends | No isolation |
| **system** | `/system/...` | System metadata | Admin-only, read-only |
| **archives** | `/archives/{tenant}/...` | Cold storage | Tenant isolation, read-only |

### 6. Admin Access Override

```python
import nexus

# Regular user
nx_user = nexus.connect(config={
    "tenant_id": "acme",
    "is_admin": False
})

# Admin user
nx_admin = nexus.connect(config={
    "tenant_id": "admin",
    "is_admin": True
})

# User writes to their workspace
nx_user.write("/workspace/acme/agent1/secret.txt", b"confidential")

# Admin can access ANY tenant's resources (bypass isolation)
data = nx_admin.read("/workspace/acme/agent1/secret.txt")  # ✓ Works!
```

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                   USER APPLICATION                      │
│                 (your Python code)                      │
└────────────────────┬────────────────────────────────────┘
                     │
                     │ import nexus
                     │ nx = nexus.connect(config={...})
                     ▼
┌─────────────────────────────────────────────────────────┐
│              nexus.connect()                            │
│              (auto-detects mode)                        │
└────────────────────┬────────────────────────────────────┘
                     │ Returns Embedded instance
                     ▼
┌─────────────────────────────────────────────────────────┐
│              Embedded Filesystem Class                  │
│              (nexus.core.embedded)                      │
├─────────────────────────────────────────────────────────┤
│  • Path validation & security                           │
│  • PathRouter (namespace-aware)                         │
│  • Tenant/Agent isolation enforcement                   │
│  • Automatic metadata tracking                          │
└─────┬──────────────────────────────────┬────────────────┘
      │                                  │
      │ Store metadata                   │ Read/write data
      ▼                                  ▼
┌──────────────────────┐      ┌──────────────────────────┐
│ SQLAlchemy Metadata  │      │   Storage Backend        │
│      Store           │      │   (LocalBackend)         │
├──────────────────────┤      ├──────────────────────────┤
│ • FilePathModel      │      │ • CAS (content hash)     │
│ • FileMetadataModel  │      │ • Reference counting     │
│ • Custom metadata    │      │ • Deduplication          │
└──────┬───────────────┘      └──────────────────────────┘
       │
       ▼
┌──────────────────────┐
│   SQLite Database    │
│   (metadata.db)      │
├──────────────────────┤
│ • Virtual paths      │
│ • File metadata      │
│ • Custom attributes  │
└──────────────────────┘
```

## Key Features

### ✅ Implemented in v0.1.0

**Core Filesystem:**
- ✅ Simple file operations (read, write, delete, list)
- ✅ Directory operations (mkdir, rmdir, is_directory)
- ✅ Automatic metadata tracking
- ✅ SQLite metadata store
- ✅ SQLAlchemy ORM models
- ✅ Alembic migrations
- ✅ Custom metadata (key-value)
- ✅ Local file backend

**Advanced Features:**
- ✅ **Multi-tenant isolation** (tenant_id)
- ✅ **Agent-level isolation** (agent_id in /workspace)
- ✅ **Custom namespaces** (define your own)
- ✅ **Path routing** (virtual paths → backends)
- ✅ **Content-addressable storage (CAS)**
- ✅ **Automatic deduplication** (same content stored once)
- ✅ **Reference counting** (safe deletion)
- ✅ **Admin access override** (bypass isolation)
- ✅ **Configuration system** (dict, YAML, env vars)
- ✅ **Namespace access control** (readonly, admin-only)
- ✅ **Metadata export/import** (JSONL format for backup/migration)

### 🚧 Coming in v0.2.0+

- PostgreSQL support (multi-tenant production)
- Multiple backends (S3, GCS, Azure)
- Version tracking per file
- Distributed locking
- Vector search integration
- LLM cache integration
- Monolithic mode (single server)
- Distributed mode (Kubernetes-ready)

## Configuration

### Config Sources (Priority Order)

1. **Explicit config parameter** (highest priority)
2. **Environment variables** (`NEXUS_*`)
3. **Config files** (`./nexus.yaml`, `~/.nexus/config.yaml`)
4. **Defaults** (embedded mode with `./nexus-data`)

### Configuration Options

```yaml
# Deployment mode
mode: embedded  # embedded | monolithic | distributed

# Storage
data_dir: ./nexus-data
db_path: ./nexus-data/metadata.db  # optional, auto-generated

# Multi-tenant isolation
tenant_id: acme       # Tenant identifier (optional)
agent_id: agent1      # Agent identifier (optional)
is_admin: false       # Admin privileges (optional)

# Performance
cache_size_mb: 100
enable_vector_search: true
enable_llm_cache: true

# Custom namespaces (optional)
namespaces:
  - name: analytics
    readonly: false
    admin_only: false
    requires_tenant: true

  - name: audit
    readonly: false
    admin_only: true
    requires_tenant: false
```

### Environment Variables

```bash
export NEXUS_MODE=embedded
export NEXUS_DATA_DIR=./nexus-data
export NEXUS_TENANT_ID=acme
export NEXUS_AGENT_ID=agent1
export NEXUS_IS_ADMIN=false
```

## Testing

Run the tests to verify everything works:

```bash
# All tests
PYTHONPATH=src python -m pytest tests/ -v

# Core tests (includes tenant isolation, namespaces, routing)
PYTHONPATH=src python -m pytest tests/unit/core/ -v

# Specific feature tests
PYTHONPATH=src python -m pytest tests/unit/core/test_embedded_namespaces.py -v
PYTHONPATH=src python -m pytest tests/unit/core/test_embedded_tenant_isolation.py -v
PYTHONPATH=src python -m pytest tests/unit/core/test_router.py -v
PYTHONPATH=src python -m pytest tests/unit/core/test_embedded_cas.py -v
```

**Current Test Status:**
- ✅ 124 tests passing
- ✅ 75% overall coverage
- ✅ 96% router.py coverage
- ✅ 92% embedded.py coverage

## Project Structure

```
examples/
├── README.md                     # This file
├── embedded_demo.py              # Comprehensive demo (8 parts)
├── config_usage_demo.py          # Configuration examples (4 demos)
├── config-basic.yaml             # Basic config template
└── config-multi-tenant.yaml      # Multi-tenant config example

src/nexus/
├── __init__.py                   # nexus.connect() entry point
├── config.py                     # Configuration system (NEW!)
├── core/
│   ├── embedded.py               # High-level API
│   ├── router.py                 # Path routing (NEW!)
│   ├── exceptions.py             # Exception classes
│   └── metadata.py               # Metadata models
├── backends/
│   ├── backend.py                # Backend interface
│   └── local.py                  # LocalBackend with CAS (NEW!)
└── storage/
    ├── models.py                 # SQLAlchemy models
    └── metadata_store.py         # Metadata store implementation

tests/
├── unit/
│   ├── core/
│   │   ├── test_embedded.py              # Basic embedded tests
│   │   ├── test_embedded_cas.py          # CAS tests (NEW!)
│   │   ├── test_embedded_namespaces.py   # Namespace tests (NEW!)
│   │   ├── test_embedded_tenant_isolation.py  # Isolation tests (NEW!)
│   │   └── test_router.py                # Router tests (NEW!)
│   └── storage/
│       ├── test_models.py
│       └── test_metadata_store.py
```

## Quick Reference

### The Correct Way to Use Nexus

```python
import nexus

# ✅ Recommended: Use nexus.connect() with config
nx = nexus.connect(config={
    "data_dir": "./nexus-data",
    "tenant_id": "acme",
    "agent_id": "agent1"
})

# ✅ Also recommended: YAML config file
nx = nexus.connect(config="nexus.yaml")

# ✅ Also recommended: Auto-discovery (uses nexus.yaml if exists)
nx = nexus.connect()

# ❌ Not recommended: Direct class instantiation
# from nexus.core.embedded import Embedded
# nx = Embedded(data_dir="./nexus-data")
```

**Why use `nexus.connect()`?**
- ✅ Auto-detects deployment mode (embedded/monolithic/distributed)
- ✅ Config-based and future-proof
- ✅ Works across all modes
- ✅ Supports multi-tenant configuration
- ✅ Supports custom namespaces
- ✅ Cleaner and simpler API

## Common Use Cases

### Use Case 1: Single-Tenant Application

```python
# Simple - no tenant isolation needed
nx = nexus.connect(config={"data_dir": "./data"})
nx.write("/workspace/data.txt", b"content")
```

### Use Case 2: Multi-Tenant SaaS

```yaml
# nexus.yaml
mode: embedded
data_dir: ./nexus-data
tenant_id: ${TENANT_ID}  # From environment
agent_id: ${AGENT_ID}
namespaces:
  - name: analytics
    requires_tenant: true
```

### Use Case 3: Agent Framework with Workspace Isolation

```python
# Each agent gets isolated workspace
for agent_id in ["agent1", "agent2", "agent3"]:
    nx = nexus.connect(config={
        "tenant_id": "acme",
        "agent_id": agent_id
    })
    nx.write(f"/workspace/acme/{agent_id}/state.json", b'{"status": "ready"}')
    # Agents can collaborate via /shared/acme/
```

### Use Case 4: Custom Application-Specific Namespaces

```python
nx = nexus.connect(config={
    "tenant_id": "acme",
    "namespaces": [
        {"name": "ml_models", "requires_tenant": True},
        {"name": "datasets", "requires_tenant": True},
        {"name": "experiments", "requires_tenant": True}
    ]
})

nx.write("/ml_models/acme/classifier-v1.pkl", model_bytes)
nx.write("/datasets/acme/training/data.csv", data_bytes)
nx.write("/experiments/acme/exp-001/results.json", results_bytes)
```

## 6. Work Detection with SQL Views (Issue #69)

Nexus provides efficient SQL views for detecting and querying "ready work" - files that represent tasks or jobs in a work queue system. This enables building distributed task processing systems using the "Everything as a File" paradigm.

### What is "Work" in Nexus?

In Nexus, **work items** are files that represent tasks, jobs, or processing units. Each file has metadata that describes its state and dependencies:

- `status`: One of `ready`, `pending`, `blocked`, `in_progress`, `completed`, `failed`
- `priority`: Integer (lower = higher priority)
- `depends_on`: Path ID of a file this work depends on (creates dependency chain)
- `worker_id`: ID of worker processing this item
- `started_at`, `completed_at`: Timestamps

### SQL Views for Work Detection

Five optimized SQL views provide O(n) performance for complex work queries:

1. **ready_work_items** - Files ready for processing (status='ready', no blockers)
2. **pending_work_items** - Files waiting to start (status='pending')
3. **blocked_work_items** - Files blocked by dependencies
4. **in_progress_work** - Files currently being processed
5. **work_by_priority** - All work items ordered by priority

### Python API

```python
import nexus
from datetime import datetime

nx = nexus.connect(config={"data_dir": "./nexus-data"})

# Create work item files
nx.write("/jobs/task1.json", b'{"task": "process_data"}')
nx.write("/jobs/task2.json", b'{"task": "train_model"}')

# Set work metadata
nx.metadata.set_file_metadata("/jobs/task1.json", "status", "ready")
nx.metadata.set_file_metadata("/jobs/task1.json", "priority", 1)  # High priority

nx.metadata.set_file_metadata("/jobs/task2.json", "status", "in_progress")
nx.metadata.set_file_metadata("/jobs/task2.json", "worker_id", "worker-001")
nx.metadata.set_file_metadata("/jobs/task2.json", "started_at", datetime.utcnow().isoformat())

# Query ready work (sorted by priority)
ready_work = nx.metadata.get_ready_work(limit=10)
for item in ready_work:
    print(f"Ready: {item['virtual_path']}, Priority: {item['priority']}")

# Query in-progress work
in_progress = nx.metadata.get_in_progress_work()
for item in in_progress:
    print(f"Processing: {item['virtual_path']}, Worker: {item['worker_id']}")

# Query blocked work
blocked = nx.metadata.get_blocked_work()
for item in blocked:
    print(f"Blocked: {item['virtual_path']}, Blockers: {item['blocker_count']}")

nx.close()
```

### CLI Usage

```bash
# Query ready work items
nexus work ready --limit 10

# Query work by status
nexus work pending
nexus work blocked
nexus work in-progress

# View aggregate statistics
nexus work status

# Output as JSON
nexus work ready --json
nexus work status --json
```

### Example: Work Queue System

```python
# Worker loop
while True:
    # Get highest priority ready work
    ready = store.get_ready_work(limit=1)
    if ready:
        item = ready[0]
        path = item['virtual_path']

        # Mark as in-progress
        store.set_file_metadata(path, "status", "in_progress")
        store.set_file_metadata(path, "worker_id", worker_id)
        store.set_file_metadata(path, "started_at", datetime.utcnow().isoformat())

        # Process work
        process_work_item(item)

        # Mark as completed
        store.set_file_metadata(path, "status", "completed")
        store.set_file_metadata(path, "completed_at", datetime.utcnow().isoformat())
    else:
        time.sleep(1)
```

### Example: Dependency Resolution

```python
# Create dependent work items
nx.write("/jobs/step1.json", b'{"task": "data_prep"}')
nx.write("/jobs/step2.json", b'{"task": "training"}')

# Step 1 is ready
nx.metadata.set_file_metadata("/jobs/step1.json", "status", "ready")
nx.metadata.set_file_metadata("/jobs/step1.json", "priority", 1)

# Step 2 depends on Step 1
step1_meta = nx.metadata.get("/jobs/step1.json")
nx.metadata.set_file_metadata("/jobs/step2.json", "status", "blocked")
nx.metadata.set_file_metadata("/jobs/step2.json", "depends_on", step1_meta.path_id)

# Only step1 will appear in ready_work
ready = nx.metadata.get_ready_work()  # Returns [step1]

# After completing step1, step2 may become ready
nx.metadata.set_file_metadata("/jobs/step1.json", "status", "completed")
nx.metadata.set_file_metadata("/jobs/step2.json", "status", "ready")

ready = nx.metadata.get_ready_work()  # Now returns [step2]
```

### Performance

- O(n) query performance using indexed SQL views
- Efficient dependency checking with EXISTS subqueries
- < 100ms query time for 10,000+ work items
- Automatic query optimization by SQLite

### Use Cases

- **Work Queue Systems**: Distributed task processing (Celery, RQ alternative)
- **DAG Execution**: Dependency resolution (Airflow-style workflows)
- **Priority Scheduling**: High-priority tasks first
- **Monitoring Dashboards**: Real-time work queue status
- **Load Balancing**: Worker assignment and tracking

See `docs/SQL_VIEWS_FOR_WORK_DETECTION.md` for full documentation and examples.

## Next Steps

1. **Run the comprehensive demo**: `PYTHONPATH=src python examples/embedded_demo.py`
2. **Try config examples**: `PYTHONPATH=src python examples/config_usage_demo.py`
3. **Copy config templates**: `cp examples/config-*.yaml ./nexus.yaml`
4. **Explore test files**: See `tests/unit/core/test_embedded_*.py` for more examples
5. **Read the docs**: `docs/` directory

## Contributing

See `CONTRIBUTING.md` for development guidelines.

## License

Apache License 2.0 - See `LICENSE` for details.
