# Nexus Examples

This directory contains example code demonstrating Nexus embedded mode functionality.

## Available Example

### **integrated_demo.py** - Complete Embedded Mode Demo ⭐

Comprehensive demonstration of Nexus embedded filesystem with metadata store integration.

**Features:**
- 📁 High-level Embedded filesystem API (user-facing)
- 💾 Low-level SQLAlchemy Metadata Store (internal)
- 🔗 Shows the connection between both layers
- 🏷️ Custom metadata capabilities (tags, author, etc.)
- 💪 Persistence testing
- 📊 Architecture diagram

**What you'll see:**
1. **Part 1: User View** - Write/read files using simple API
2. **Part 2: Internal View** - Inspect the metadata database directly
3. **Part 3: Persistence** - Verify data survives restarts
4. **Summary** - Architecture diagram showing how it all connects

## Quick Start

### Prerequisites

```bash
# Install Nexus in development mode
pip install -e .

# Or just install dependencies
pip install sqlalchemy alembic
```

### Running the Demo

```bash
PYTHONPATH=src python examples/integrated_demo.py
```

### Expected Output

```
======================================================================
Nexus Integrated Demo: Embedded Mode + Metadata Store
======================================================================

PART 1: High-Level Embedded API (User View)
- Writing files via Embedded API...
- Reading files...
- Listing files...

PART 2: Low-Level Metadata Store (Internal View)
- Inspecting file metadata...
- Adding custom metadata...

PART 3: Persistence Test
- Re-opening filesystem...
- Verifying data persisted...

✓ Demo completed successfully!
```

## What This Demo Shows

### 1. Simple File Operations
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

# Close
nx.close()
```

### 2. Automatic Metadata Tracking

Every file operation automatically updates the metadata database:

- ✅ **Write** → Creates metadata entry with size, ETag, timestamps
- ✅ **Read** → Looks up physical path from metadata
- ✅ **Delete** → Removes metadata entry (soft delete)
- ✅ **List** → Queries metadata database

### 3. Low-Level Access (Advanced)

You can also access the metadata store directly:

```python
from nexus.storage.metadata_store import SQLAlchemyMetadataStore

# Open the same database
store = SQLAlchemyMetadataStore("./nexus-data/metadata.db")

# Inspect file metadata
metadata = store.get("/documents/report.pdf")
print(f"Size: {metadata.size} bytes")
print(f"ETag: {metadata.etag}")
print(f"Created: {metadata.created_at}")

# Add custom metadata
store.set_file_metadata("/documents/report.pdf", "author", "John Doe")
store.set_file_metadata("/documents/report.pdf", "tags", ["quarterly", "financial"])

# Retrieve custom metadata
author = store.get_file_metadata("/documents/report.pdf", "author")
```

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                   USER APPLICATION                      │
│                 (your Python code)                      │
└────────────────────┬────────────────────────────────────┘
                     │ Simple API
                     │ (read, write, delete, list)
                     ▼
┌─────────────────────────────────────────────────────────┐
│              Embedded Filesystem Class                  │
│              (nexus.core.embedded)                      │
├─────────────────────────────────────────────────────────┤
│  • Path validation                                      │
│  • ETag computation                                     │
│  • Automatic metadata tracking                          │
└─────┬──────────────────────────────────┬────────────────┘
      │                                  │
      │ Store metadata                   │ Read/write data
      ▼                                  ▼
┌──────────────────────┐      ┌──────────────────────────┐
│ SQLAlchemy Metadata  │      │   Storage Backend        │
│      Store           │      │   (LocalBackend)         │
├──────────────────────┤      ├──────────────────────────┤
│ • FilePathModel      │      │ • Physical file I/O      │
│ • FileMetadataModel  │      │ • Local filesystem       │
│ • ContentChunkModel  │      │   operations             │
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

### ✅ Embedded Mode (v0.1.0 - Current)
- SQLite metadata store
- SQLAlchemy ORM models
- Alembic migrations
- Soft delete support
- Custom metadata
- Local file backend
- Automatic metadata tracking

### 🚧 Coming in v0.2.0+
- PostgreSQL support
- Multi-tenancy
- Multiple backends (S3, GCS, Azure)
- Content deduplication
- Version tracking
- Distributed locking

## Database Migrations

The metadata store uses Alembic for schema migrations:

```bash
# View current migration
alembic current

# Upgrade to latest
alembic upgrade head

# Create new migration (after model changes)
alembic revision --autogenerate -m "description"

# Rollback
alembic downgrade -1
```

See `alembic/README_DATABASES.md` for detailed migration guide.

## Database Compatibility

The implementation works with both **SQLite** (default) and **PostgreSQL**:

### SQLite (Default - Embedded Mode)
```python
# Uses SQLite by default
store = SQLAlchemyMetadataStore("./nexus.db")
```

**Best for:**
- Desktop applications
- Single-user deployments
- Development/testing
- Embedded systems

### PostgreSQL (Production Mode)
```python
# Connect to PostgreSQL
store = SQLAlchemyMetadataStore(
    "postgresql://user:password@localhost:5432/nexus"
)
```

**Best for:**
- Multi-tenant SaaS
- High-concurrency applications
- Large-scale data (>10GB)
- Production distributed systems

See `docs/DATABASE_COMPATIBILITY.md` for detailed comparison.

## Testing

Run the tests to verify everything works:

```bash
# All tests
PYTHONPATH=src python -m pytest tests/ -v

# Just storage tests
PYTHONPATH=src python -m pytest tests/unit/storage/ -v

# Just embedded tests
PYTHONPATH=src python -m pytest tests/unit/core/test_embedded.py -v
```

## Project Structure

```
examples/
└── integrated_demo.py         # This demo

src/nexus/
├── core/
│   ├── embedded.py           # High-level API
│   ├── backend.py            # Storage backend interface
│   └── backends/
│       └── local.py          # Local filesystem backend
└── storage/
    ├── models.py             # SQLAlchemy models
    └── metadata_store.py     # Metadata store implementation

alembic/
├── versions/                 # Migration files
├── env.py                   # Alembic environment
└── README_DATABASES.md      # Migration guide

tests/
├── unit/
│   ├── core/
│   │   └── test_embedded.py
│   └── storage/
│       ├── test_models.py
│       └── test_metadata_store.py
```

## Quick Reference

### The Correct Way to Use Nexus

```python
import nexus

# ✅ Recommended: Use nexus.connect()
nx = nexus.connect(config={"data_dir": "./nexus-data"})

# ❌ Not recommended: Direct class instantiation
# from nexus.core.embedded import Embedded
# nx = Embedded(data_dir="./nexus-data")
```

**Why use `nexus.connect()`?**
- ✅ Auto-detects deployment mode (embedded/monolithic/distributed)
- ✅ Config-based and future-proof
- ✅ Works across all modes
- ✅ Simpler and cleaner

## Next Steps

1. **Run the demo**: `PYTHONPATH=src python examples/integrated_demo.py`
2. **Check database compatibility**: `docs/DATABASE_COMPATIBILITY.md`
3. **Learn about migrations**: `alembic/README_DATABASES.md`
4. **Explore the codebase**: `src/nexus/`

## Contributing

See `CONTRIBUTING.md` for development guidelines.

## License

Apache License 2.0 - See `LICENSE` for details.
