# File Operations

← [API Documentation](README.md)

This document describes all file manipulation operations in Nexus.

## write()

Write content to a file. Creates parent directories automatically. Overwrites if file exists.

```python
def write(
    path: str,
    content: bytes,
    context: OperationContext | EnhancedOperationContext | None = None,
    if_match: str | None = None,
    if_none_match: bool = False,
    force: bool = False,
) -> dict[str, Any]
```

**Parameters:**
- `path` (str): Virtual path (must start with `/`)
- `content` (bytes): File content as bytes
- `context` (OperationContext | EnhancedOperationContext, optional): Operation context for permission checks
- `if_match` (str, optional): ETag for optimistic concurrency control (v0.3.9)
- `if_none_match` (bool): If True, create-only mode (fails if file exists)
- `force` (bool): If True, skip version check

**Returns:**
- `dict`: Metadata dict with keys: `etag`, `version`, `modified_at`, `size`

**Raises:**
- `InvalidPathError`: If path is invalid
- `BackendError`: If write operation fails
- `AccessDeniedError`: If access is denied
- `PermissionError`: If path is read-only
- `ConflictError`: If if_match doesn't match current etag

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

## read()

Read file content as bytes.

```python
def read(
    path: str,
    context: OperationContext | EnhancedOperationContext | None = None,
    return_metadata: bool = False
) -> bytes | dict[str, Any]
```

**Parameters:**
- `path` (str): Virtual path to read
- `context` (OperationContext | EnhancedOperationContext, optional): Operation context for permission checks
- `return_metadata` (bool): If True, return dict with content and metadata

**Returns:**
- `bytes`: File content (if return_metadata=False)
- `dict`: Dict with content and metadata (if return_metadata=True)

**Raises:**
- `NexusFileNotFoundError`: If file doesn't exist
- `InvalidPathError`: If path is invalid
- `BackendError`: If read operation fails
- `AccessDeniedError`: If access is denied

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

## delete()

Delete a file (soft delete - metadata preserved).

```python
def delete(
    path: str,
    context: OperationContext | EnhancedOperationContext | None = None
) -> None
```

**Parameters:**
- `path` (str): Virtual path to delete
- `context` (OperationContext | EnhancedOperationContext, optional): Operation context for permission checks (uses default if None)

**Raises:**
- `NexusFileNotFoundError`: If file doesn't exist
- `InvalidPathError`: If path is invalid
- `BackendError`: If delete operation fails
- `AccessDeniedError`: If access is denied
- `PermissionError`: If path is read-only or user doesn't have WRITE permission

**Examples:**

```python
# Delete a file
nx.delete("/documents/old.txt")

# Check if deleted
assert not nx.exists("/documents/old.txt")

# Delete with specific user context
from nexus.core.permissions import OperationContext
ctx = OperationContext(user="alice", groups=["team-engineering"])
nx.delete("/workspace/alice/temp.txt", context=ctx)
```

**Note:** This is a soft delete. The metadata entry is marked as deleted but preserved in the database. Physical file is removed from storage.

---

## list() - Simple List (Deprecated)

List all files with optional path prefix filtering.

**Note:** This is the old simplified API. Use the enhanced `list()` method in [File Discovery](file-discovery.md) for more features.

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

## exists()

Check if a file exists.

```python
def exists(
    path: str,
    context: OperationContext | EnhancedOperationContext | None = None
) -> bool
```

**Parameters:**
- `path` (str): Virtual path to check
- `context` (OperationContext | EnhancedOperationContext, optional): Operation context for permission checks (uses default if None)

**Returns:**
- `bool`: `True` if file exists, `False` otherwise (returns False if user lacks READ permission)

**Examples:**

```python
if nx.exists("/documents/report.pdf"):
    content = nx.read("/documents/report.pdf")
else:
    print("File not found")

# Use in conditional
if not nx.exists("/cache/data.json"):
    nx.write("/cache/data.json", b"{}")

# Check with specific user context
from nexus.core.permissions import OperationContext
ctx = OperationContext(user="charlie", groups=["project-beta"])
if nx.exists("/workspace/alice/secret.txt", context=ctx):
    print("Charlie can see this file")
else:
    print("File doesn't exist or Charlie lacks permission")
```

---

## close()

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

## write_batch()

Write multiple files in a single transaction for improved performance (4x faster than individual writes).

```python
def write_batch(
    files: list[tuple[str, bytes]],
    context: OperationContext | EnhancedOperationContext | None = None
) -> list[dict[str, Any]]
```

**Parameters:**
- `files` (list): List of (path, content) tuples to write
- `context` (OperationContext | EnhancedOperationContext, optional): Operation context for permission checks

**Returns:**
- `list[dict]`: List of metadata dicts for each file

**Raises:**
- `InvalidPathError`: If any path is invalid
- `BackendError`: If write operation fails
- `AccessDeniedError`: If access is denied
- `PermissionError`: If any path is read-only

**Examples:**

```python
# Write 100 files in one transaction (4x faster!)
files = [(f"/logs/file_{i}.txt", b"log data") for i in range(100)]
results = nx.write_batch(files)
print(f"Wrote {len(results)} files")

# Atomic batch write - all or nothing
files = [
    ("/config/setting1.json", b'{"enabled": true}'),
    ("/config/setting2.json", b'{"timeout": 30}'),
]
nx.write_batch(files)
```

---

## rename()

Rename/move a file (metadata-only operation, no content copying).

```python
def rename(
    old_path: str,
    new_path: str,
    context: OperationContext | EnhancedOperationContext | None = None
) -> None
```

**Parameters:**
- `old_path` (str): Current virtual path
- `new_path` (str): New virtual path
- `context` (OperationContext | EnhancedOperationContext, optional): Operation context for permission checks (uses default if None)

**Raises:**
- `NexusFileNotFoundError`: If source file doesn't exist
- `FileExistsError`: If destination already exists
- `InvalidPathError`: If either path is invalid
- `AccessDeniedError`: If access is denied
- `PermissionError`: If either path is read-only or user doesn't have WRITE permission

**Examples:**

```python
# Rename a file
nx.rename("/documents/old.txt", "/documents/new.txt")

# Move to different directory
nx.rename("/temp/data.csv", "/archive/data.csv")

# Rename with specific user context
from nexus.core.permissions import OperationContext
ctx = OperationContext(user="alice", groups=["team-engineering"])
nx.rename("/workspace/alice/draft.txt", "/workspace/alice/final.txt", context=ctx)
```

## See Also

- [File Discovery](file-discovery.md) - Find and search files
- [Directory Operations](directory-operations.md) - Working with directories
- [Versioning](versioning.md) - Version control and snapshots
- [Permissions](permissions.md) - Access control
- [Error Handling](error-handling.md) - Exception handling

## Next Steps

1. Learn about [file discovery operations](file-discovery.md) (list, glob, grep)
2. Explore [versioning](versioning.md) for tracking file history
3. Set up [permissions](permissions.md) for access control
