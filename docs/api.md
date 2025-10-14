# Nexus Python API Documentation

This document describes the Python API for Nexus embedded mode (v0.1.0).

## Table of Contents

- [Getting Started](#getting-started)
- [Core API](#core-api)
  - [nexus.connect()](#nexusconnect)
  - [File Operations](#file-operations)
- [Configuration](#configuration)
- [Metadata Store](#metadata-store)
- [Error Handling](#error-handling)
- [Advanced Usage](#advanced-usage)

---

## Getting Started

### Installation

```bash
# Install from source
pip install -e .

# Or install dependencies only
pip install sqlalchemy alembic
```

### Quick Start

```python
import nexus

# Connect to Nexus (auto-creates database)
nx = nexus.connect(config={"data_dir": "./nexus-data"})

# Write a file
nx.write("/documents/hello.txt", b"Hello, Nexus!")

# Read a file
content = nx.read("/documents/hello.txt")
print(content.decode())  # "Hello, Nexus!"

# List files
files = nx.list()
print(files)  # ['/documents/hello.txt']

# Delete a file
nx.delete("/documents/hello.txt")

# Close connection
nx.close()
```

### Context Manager

```python
import nexus

with nexus.connect(config={"data_dir": "./nexus-data"}) as nx:
    nx.write("/file.txt", b"content")
    content = nx.read("/file.txt")
# Automatically closed
```

---

## Core API

### nexus.connect()

The main entry point for using Nexus. Auto-detects deployment mode and returns the appropriate client.

```python
def connect(
    config: str | Path | dict | NexusConfig | None = None
) -> Embedded
```

**Parameters:**
- `config` (optional): Configuration source
  - `None`: Auto-discover from environment/files (default)
  - `str | Path`: Path to config file (`.yaml` or `.json`)
  - `dict`: Configuration dictionary
  - `NexusConfig`: Pre-loaded configuration object

**Returns:**
- `Embedded`: Nexus client instance (v0.1.0 only supports embedded mode)

**Raises:**
- `ValueError`: If configuration is invalid
- `NotImplementedError`: If mode is not yet implemented (monolithic/distributed)

**Examples:**

```python
# Auto-detect (uses defaults)
nx = nexus.connect()

# With inline config
nx = nexus.connect(config={"data_dir": "./my-data"})

# From config file
nx = nexus.connect(config="./config.yaml")

# From environment
# Set NEXUS_DATA_DIR=/path/to/data
nx = nexus.connect()
```

---

## File Operations

### write()

Write content to a file. Creates parent directories automatically. Overwrites if file exists.

```python
def write(path: str, content: bytes) -> None
```

**Parameters:**
- `path` (str): Virtual path (must start with `/`)
- `content` (bytes): File content as bytes

**Raises:**
- `InvalidPathError`: If path is invalid
- `BackendError`: If write operation fails

**Examples:**

```python
# Write text file
nx.write("/documents/readme.txt", b"Hello World")

# Write JSON
import json
data = {"key": "value"}
nx.write("/data/config.json", json.dumps(data).encode())

# Write binary
with open("image.jpg", "rb") as f:
    nx.write("/images/photo.jpg", f.read())
```

**Automatic Metadata:**
- Virtual path → physical path mapping
- File size
- ETag (MD5 hash)
- Created/modified timestamps
- Backend information

---

### read()

Read file content as bytes.

```python
def read(path: str) -> bytes
```

**Parameters:**
- `path` (str): Virtual path to read

**Returns:**
- `bytes`: File content

**Raises:**
- `NexusFileNotFoundError`: If file doesn't exist
- `InvalidPathError`: If path is invalid
- `BackendError`: If read operation fails

**Examples:**

```python
# Read text file
content = nx.read("/documents/readme.txt")
text = content.decode("utf-8")

# Read JSON
import json
content = nx.read("/data/config.json")
data = json.loads(content)

# Read binary
content = nx.read("/images/photo.jpg")
with open("output.jpg", "wb") as f:
    f.write(content)
```

---

### delete()

Delete a file (soft delete - metadata preserved).

```python
def delete(path: str) -> None
```

**Parameters:**
- `path` (str): Virtual path to delete

**Raises:**
- `NexusFileNotFoundError`: If file doesn't exist
- `InvalidPathError`: If path is invalid
- `BackendError`: If delete operation fails

**Examples:**

```python
# Delete a file
nx.delete("/documents/old.txt")

# Check if deleted
assert not nx.exists("/documents/old.txt")
```

**Note:** This is a soft delete. The metadata entry is marked as deleted but preserved in the database. Physical file is removed from storage.

---

### list()

List all files with optional path prefix filtering.

```python
def list(prefix: str = "") -> list[str]
```

**Parameters:**
- `prefix` (str, optional): Path prefix to filter by (default: empty = all files)

**Returns:**
- `list[str]`: List of virtual paths, sorted alphabetically

**Examples:**

```python
# List all files
all_files = nx.list()
# ['/data/config.json', '/documents/report.pdf', '/images/photo.jpg']

# List files in /documents
docs = nx.list(prefix="/documents")
# ['/documents/report.pdf', '/documents/readme.txt']

# List files with specific pattern
logs = nx.list(prefix="/logs/2025")
# ['/logs/2025-01-01.log', '/logs/2025-01-02.log']
```

---

### exists()

Check if a file exists.

```python
def exists(path: str) -> bool
```

**Parameters:**
- `path` (str): Virtual path to check

**Returns:**
- `bool`: `True` if file exists, `False` otherwise

**Examples:**

```python
if nx.exists("/documents/report.pdf"):
    content = nx.read("/documents/report.pdf")
else:
    print("File not found")

# Use in conditional
if not nx.exists("/cache/data.json"):
    nx.write("/cache/data.json", b"{}")
```

---

### close()

Close the connection and release resources.

```python
def close() -> None
```

**Examples:**

```python
nx = nexus.connect()
try:
    nx.write("/file.txt", b"content")
finally:
    nx.close()

# Or use context manager (recommended)
with nexus.connect() as nx:
    nx.write("/file.txt", b"content")
```

---

## Configuration

### Config Dictionary

```python
config = {
    "mode": "embedded",           # Deployment mode (only "embedded" in v0.1.0)
    "data_dir": "./nexus-data",   # Root directory for data and database
    "db_path": None,              # Optional: custom database path
}

nx = nexus.connect(config=config)
```

### Config File (YAML)

```yaml
# config.yaml
mode: embedded
data_dir: ./nexus-data
```

```python
nx = nexus.connect(config="config.yaml")
```

### Environment Variables

```bash
export NEXUS_MODE=embedded
export NEXUS_DATA_DIR=/var/nexus-data
```

```python
# Auto-detects from environment
nx = nexus.connect()
```

### Configuration Object

```python
from nexus import NexusConfig

config = NexusConfig(
    mode="embedded",
    data_dir="./nexus-data"
)

nx = nexus.connect(config=config)
```

---

## Metadata Store

### Direct Access (Advanced)

For advanced use cases, you can access the metadata store directly:

```python
from nexus.storage.metadata_store import SQLAlchemyMetadataStore

# Open the metadata database
store = SQLAlchemyMetadataStore("./nexus-data/metadata.db")

# Get file metadata
metadata = store.get("/documents/report.pdf")
print(f"Size: {metadata.size} bytes")
print(f"ETag: {metadata.etag}")
print(f"Created: {metadata.created_at}")

# Add custom metadata
store.set_file_metadata("/documents/report.pdf", "author", "John Doe")
store.set_file_metadata("/documents/report.pdf", "tags", ["quarterly", "financial"])
store.set_file_metadata("/documents/report.pdf", "version", 3)

# Retrieve custom metadata
author = store.get_file_metadata("/documents/report.pdf", "author")
tags = store.get_file_metadata("/documents/report.pdf", "tags")

store.close()
```

### FileMetadata Object

```python
@dataclass
class FileMetadata:
    path: str                      # Virtual path
    backend_name: str              # Backend identifier
    physical_path: str             # Physical storage path
    size: int                      # File size in bytes
    etag: str | None               # ETag (MD5 hash)
    mime_type: str | None          # MIME type
    created_at: datetime | None    # Creation timestamp
    modified_at: datetime | None   # Last modification timestamp
    version: int                   # Version number (always 1 in v0.1.0)
```

---

## Error Handling

### Exception Hierarchy

```
NexusError (base)
├── NexusFileNotFoundError
├── NexusPermissionError
├── BackendError
├── InvalidPathError
└── MetadataError
```

### Exception Details

#### NexusFileNotFoundError

```python
try:
    content = nx.read("/nonexistent.txt")
except NexusFileNotFoundError as e:
    print(f"File not found: {e.path}")
```

#### InvalidPathError

```python
try:
    nx.write("no-leading-slash.txt", b"content")  # Invalid
except InvalidPathError as e:
    print(f"Invalid path: {e.path}")
```

#### BackendError

```python
try:
    nx.write("/file.txt", b"content")
except BackendError as e:
    print(f"Backend error: {e.message}")
```

### Best Practices

```python
import nexus
from nexus import NexusFileNotFoundError, InvalidPathError

nx = nexus.connect()

try:
    # Check before reading
    if nx.exists("/file.txt"):
        content = nx.read("/file.txt")

    # Handle specific errors
    nx.write("/documents/report.pdf", b"content")

except NexusFileNotFoundError as e:
    print(f"File not found: {e.path}")
except InvalidPathError as e:
    print(f"Invalid path: {e.path}")
except Exception as e:
    print(f"Unexpected error: {e}")
finally:
    nx.close()
```

---

## Advanced Usage

### Batch Operations

```python
import nexus

nx = nexus.connect()

# Batch write
files = {
    "/data/file1.txt": b"content1",
    "/data/file2.txt": b"content2",
    "/data/file3.txt": b"content3",
}

for path, content in files.items():
    nx.write(path, content)

# Batch read
contents = {}
for path in nx.list(prefix="/data"):
    contents[path] = nx.read(path)

nx.close()
```

### Working with JSON

```python
import json
import nexus

nx = nexus.connect()

# Write JSON
data = {"users": [{"id": 1, "name": "Alice"}]}
nx.write("/data/users.json", json.dumps(data, indent=2).encode())

# Read JSON
content = nx.read("/data/users.json")
data = json.loads(content)
print(data["users"])

nx.close()
```

### Path Patterns

```python
# Valid paths
nx.write("/file.txt", b"content")              # ✅
nx.write("/path/to/nested/file.txt", b"data")  # ✅
nx.write("/documents/2025/report.pdf", b"pdf") # ✅

# Invalid paths
nx.write("no-slash.txt", b"content")           # ❌ No leading slash
nx.write("/path/../file.txt", b"content")      # ❌ Contains '..'
nx.write("/path\nfile.txt", b"content")        # ❌ Contains newline
```

### File Size Limits

```python
# No explicit file size limits in v0.1.0
# Limited by:
# - Available disk space
# - SQLite database limits (2TB max)
# - Python memory for in-memory operations

# For large files, read in chunks (future enhancement)
```

### Performance Tips

```python
# 1. Use batch operations instead of individual calls
files_to_write = [...]
for path, content in files_to_write:
    nx.write(path, content)  # Each call is a transaction

# 2. Use list() with prefix for filtering
docs = nx.list(prefix="/documents")  # Fast, uses index
all_files = nx.list()
docs = [f for f in all_files if f.startswith("/documents")]  # Slower

# 3. Check existence before read
if nx.exists(path):
    content = nx.read(path)
# Instead of try/except for normal flow

# 4. Use context manager for automatic cleanup
with nexus.connect() as nx:
    # Operations
    pass
# Automatically closed
```

---

## Version Compatibility

### v0.1.0 (Current)

**Supported:**
- ✅ Embedded mode
- ✅ SQLite backend
- ✅ Local filesystem storage
- ✅ Automatic metadata tracking
- ✅ Soft delete
- ✅ Custom metadata (via MetadataStore)

**Not Yet Implemented:**
- ⏳ Monolithic mode (v0.2.0+)
- ⏳ Distributed mode (v0.2.0+)
- ⏳ PostgreSQL backend (v0.2.0+)
- ⏳ S3/GCS/Azure backends (v0.2.0+)
- ⏳ Version tracking (v0.2.0+)
- ⏳ Content deduplication (v0.2.0+)

### API Stability

The API is **stable** for v0.1.0 embedded mode. Breaking changes will be avoided in minor versions.

---

## Examples

### Complete Application

```python
import nexus
import json
from datetime import datetime

def main():
    # Initialize
    nx = nexus.connect(config={"data_dir": "./app-data"})

    try:
        # Store application config
        config = {
            "app_name": "MyApp",
            "version": "1.0.0",
            "created_at": datetime.now().isoformat()
        }
        nx.write("/config/app.json", json.dumps(config).encode())

        # Store user data
        nx.write("/users/alice.txt", b"Alice's data")
        nx.write("/users/bob.txt", b"Bob's data")

        # List all users
        users = nx.list(prefix="/users")
        print(f"Users: {users}")

        # Read config
        config_data = json.loads(nx.read("/config/app.json"))
        print(f"App: {config_data['app_name']} v{config_data['version']}")

        # Cleanup old data
        if nx.exists("/temp/cache.dat"):
            nx.delete("/temp/cache.dat")

    finally:
        nx.close()

if __name__ == "__main__":
    main()
```

---

## Further Reading

- **Database Compatibility**: `docs/DATABASE_COMPATIBILITY.md`
- **Development Guide**: `docs/development.md`
- **Examples**: `examples/integrated_demo.py`

---

## Support

- **GitHub Issues**: https://github.com/nexi-lab/nexus/issues
- **Documentation**: `docs/`
- **Examples**: `examples/`
