# CLI: File Operations

← [CLI Reference](index.md) | [API Documentation](../README.md)

This document describes CLI commands for file manipulation operations and their Python API equivalents.

## write - Write file content

Write content to a file. Creates parent directories automatically.

**CLI:**
```bash
# Basic write
nexus write /workspace/file.txt "Hello World"

# Write from stdin
echo "Hello World" | nexus write /workspace/file.txt --input -

# Write from file
nexus write /workspace/file.txt --input local_file.txt

# Optimistic concurrency control
nexus write /doc.txt "Updated" --if-match abc123

# Create-only mode
nexus write /new.txt "Initial" --if-none-match

# Show metadata
nexus write /doc.txt "Content" --show-metadata
```

**Python API:**
```python
# Basic write
nx.write("/workspace/file.txt", b"Hello World")

# Write with metadata return
metadata = nx.write("/workspace/file.txt", b"Hello World")
print(f"ETag: {metadata['etag']}, Version: {metadata['version']}")

# Optimistic concurrency control
nx.write("/doc.txt", b"Updated", if_match="abc123")

# Create-only mode
nx.write("/new.txt", b"Initial", if_none_match=True)

# Write JSON
import json
data = {"key": "value"}
nx.write("/data/config.json", json.dumps(data).encode())

# Write binary
with open("image.jpg", "rb") as f:
    nx.write("/images/photo.jpg", f.read())
```

**Options:**
- `--input PATH`: Read content from file or stdin (`-`)
- `--if-match ETAG`: Only write if current ETag matches (optimistic locking)
- `--if-none-match`: Only write if file doesn't exist (create-only)
- `--show-metadata`: Display file metadata after write
- `--remote-url URL`: Connect to remote server
- `--remote-api-key KEY`: API key for authentication

**See Also:**
- [Python API: write()](../file-operations.md#write)
- [Optimistic Concurrency Control](../advanced-usage.md#optimistic-concurrency)

---

## cat - Display file contents

Read and display file content.

**CLI:**
```bash
# Read file
nexus cat /workspace/file.txt

# Show metadata (etag, version)
nexus cat /workspace/file.txt --metadata

# Time-travel: Read at historical operation point
nexus cat /workspace/file.txt --at-operation op_abc123
```

**Python API:**
```python
# Read file
content = nx.read("/workspace/file.txt")
text = content.decode("utf-8")

# Read with metadata
result = nx.read("/workspace/file.txt", return_metadata=True)
content = result["content"]
etag = result["etag"]
version = result["version"]

# Read JSON
import json
content = nx.read("/data/config.json")
data = json.loads(content.decode())

# Time-travel read
from nexus.core.permissions import OperationContext
ctx = OperationContext(at_operation="op_abc123")
content = nx.read("/workspace/file.txt", context=ctx)
```

**Options:**
- `--metadata`: Show file metadata (etag, version, size, modified time)
- `--at-operation OP_ID`: Read file at specific operation point (time-travel)
- `--remote-url URL`: Connect to remote server
- `--remote-api-key KEY`: API key for authentication

**See Also:**
- [Python API: read()](../file-operations.md#read)
- [Time Travel](../versioning.md#time-travel)

---

## rm - Delete file

Delete a file from Nexus.

**CLI:**
```bash
# Delete with confirmation
nexus rm /workspace/file.txt

# Force delete (no confirmation)
nexus rm /workspace/file.txt --force
```

**Python API:**
```python
# Delete file
nx.delete("/workspace/file.txt")

# Delete with specific user context
from nexus.core.permissions import OperationContext
ctx = OperationContext(user="alice", groups=["eng-team"])
nx.delete("/workspace/file.txt", context=ctx)
```

**Options:**
- `--force`: Skip confirmation prompt
- `--remote-url URL`: Connect to remote server
- `--remote-api-key KEY`: API key for authentication

**See Also:**
- [Python API: delete()](../file-operations.md#delete)

---

## cp/copy - Copy files

Copy a file to a new location.

**CLI:**
```bash
# Simple copy
nexus cp /source.txt /dest.txt

# Smart copy with deduplication
nexus copy /source.txt /dest.txt
```

**Python API:**
```python
# Copy file
nx.copy("/source.txt", "/dest.txt")

# The copy operation is content-addressed and deduplicated automatically
```

**Options:**
- `--remote-url URL`: Connect to remote server
- `--remote-api-key KEY`: API key for authentication

**See Also:**
- [Python API: copy()](../file-operations.md#copy)

---

## move - Move files

Move or rename a file.

**CLI:**
```bash
# Move file
nexus move /old/path.txt /new/path.txt
```

**Python API:**
```python
# Move/rename file
nx.move("/old/path.txt", "/new/path.txt")

# Move with specific user context
from nexus.core.permissions import OperationContext
ctx = OperationContext(user="alice", groups=["eng-team"])
nx.move("/old/path.txt", "/new/path.txt", context=ctx)
```

**Options:**
- `--remote-url URL`: Connect to remote server
- `--remote-api-key KEY`: API key for authentication

**See Also:**
- [Python API: move()](../file-operations.md#move)

---

## Common Workflows

### Basic file management
```bash
# Write a file
nexus write /docs/README.md "# My Project"

# Read it back
nexus cat /docs/README.md

# Copy it
nexus cp /docs/README.md /docs/README.backup.md

# Move it
nexus move /docs/README.backup.md /backups/README.md

# Delete it
nexus rm /backups/README.md --force
```

### Python equivalent
```python
import nexus

# Initialize
nx = nexus.Nexus(data_dir="./nexus-data")

# Write a file
nx.write("/docs/README.md", b"# My Project")

# Read it back
content = nx.read("/docs/README.md")
print(content.decode())

# Copy it
nx.copy("/docs/README.md", "/docs/README.backup.md")

# Move it
nx.move("/docs/README.backup.md", "/backups/README.md")

# Delete it
nx.delete("/backups/README.md")
```

---

## See Also

- [CLI Reference Overview](index.md)
- [Python API: File Operations](../file-operations.md)
- [Directory Operations](directory-operations.md)
- [Search Operations](search.md)
