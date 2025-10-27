# CLI: Directory Operations

← [CLI Reference](index.md) | [API Documentation](../README.md)

This document describes CLI commands for directory management and their Python API equivalents.

## mkdir - Create directory

Create a new directory.

**CLI:**
```bash
# Create directory
nexus mkdir /workspace/data

# Create with parents
nexus mkdir /workspace/deep/nested/dir --parents
```

**Python API:**
```python
# Create directory
nx.mkdir("/workspace/data")

# Create directory with parents
nx.mkdir("/workspace/deep/nested/dir", parents=True)

# Don't error if exists
nx.mkdir("/workspace/data", exist_ok=True)

# Create with specific user context
from nexus.core.permissions import OperationContext
ctx = OperationContext(user="alice", groups=["team-engineering"])
nx.mkdir("/workspace/alice/projects", parents=True, context=ctx)
```

**Options:**
- `--parents, -p`: Create parent directories as needed (like `mkdir -p`)
- `--remote-url URL`: Connect to remote server
- `--remote-api-key KEY`: API key for authentication

**See Also:**
- [Python API: mkdir()](../directory-operations.md#mkdir)

---

## rmdir - Remove directory

Remove a directory.

**CLI:**
```bash
# Remove empty directory
nexus rmdir /workspace/data

# Remove recursively
nexus rmdir /workspace/data --recursive --force
```

**Python API:**
```python
# Remove empty directory
nx.rmdir("/temp/empty")

# Remove directory and all contents
nx.rmdir("/temp/cache", recursive=True)

# Remove with specific user context
from nexus.core.permissions import OperationContext
ctx = OperationContext(user="alice", groups=["team-engineering"])
nx.rmdir("/workspace/alice/temp", recursive=True, context=ctx)
```

**Options:**
- `--recursive, -r`: Remove directory and all contents (like `rm -rf`)
- `--force, -f`: Skip confirmation prompt
- `--remote-url URL`: Connect to remote server
- `--remote-api-key KEY`: API key for authentication

**See Also:**
- [Python API: rmdir()](../directory-operations.md#rmdir)

---

## ls - List files

List directory contents.

**CLI:**
```bash
# List directory
nexus ls /workspace

# Recursive listing
nexus ls /workspace --recursive

# Detailed listing
nexus ls /workspace --long

# Time-travel: List at historical point
nexus ls /workspace --at-operation op_abc123
```

**Python API:**
```python
# List directory
entries = nx.list("/workspace")
for entry in entries:
    print(f"{entry['name']} - {entry['type']}")

# List with details
entries = nx.list("/workspace")
for entry in entries:
    print(f"{entry['name']}: {entry['size']} bytes, modified {entry['modified_at']}")

# Recursive list
entries = nx.list("/workspace", recursive=True)

# Time-travel list
from nexus.core.permissions import OperationContext
ctx = OperationContext(at_operation="op_abc123")
entries = nx.list("/workspace", context=ctx)
```

**Options:**
- `--recursive, -r`: List recursively
- `--long, -l`: Detailed output with size, modified time, etc.
- `--at-operation OP_ID`: List at specific operation point (time-travel)
- `--remote-url URL`: Connect to remote server
- `--remote-api-key KEY`: API key for authentication

**See Also:**
- [Python API: list()](../directory-operations.md#list)
- [Time Travel](../versioning.md#time-travel)

---

## tree - Display directory tree

Display directory structure as a tree.

**CLI:**
```bash
# Show tree structure
nexus tree /workspace
```

**Python API:**
```python
# Tree view is CLI-only, but you can build it from list()
def print_tree(nx, path="/", prefix="", recursive=True):
    """Print directory tree"""
    entries = nx.list(path)
    for i, entry in enumerate(entries):
        is_last = i == len(entries) - 1
        connector = "└── " if is_last else "├── "
        print(f"{prefix}{connector}{entry['name']}")

        if entry['type'] == 'directory' and recursive:
            extension = "    " if is_last else "│   "
            child_path = f"{path.rstrip('/')}/{entry['name']}"
            print_tree(nx, child_path, prefix + extension)

print_tree(nx, "/workspace")
```

**Options:**
- `--remote-url URL`: Connect to remote server
- `--remote-api-key KEY`: API key for authentication

---

## Common Workflows

### Basic directory management
```bash
# Create a directory structure
nexus mkdir /projects/myapp/src --parents
nexus mkdir /projects/myapp/docs --parents
nexus mkdir /projects/myapp/tests --parents

# List the structure
nexus tree /projects

# List with details
nexus ls /projects/myapp --long

# Remove a directory
nexus rmdir /projects/myapp/tests --recursive --force
```

### Python equivalent
```python
import nexus

# Initialize
nx = nexus.Nexus(data_dir="./nexus-data")

# Create directory structure
nx.mkdir("/projects/myapp/src", parents=True)
nx.mkdir("/projects/myapp/docs", parents=True)
nx.mkdir("/projects/myapp/tests", parents=True)

# List the structure
entries = nx.list("/projects/myapp")
for entry in entries:
    print(f"{entry['name']} ({entry['type']})")

# List recursively with details
def list_recursive(path, indent=0):
    entries = nx.list(path)
    for entry in entries:
        prefix = "  " * indent
        print(f"{prefix}{entry['name']} - {entry['size']} bytes")
        if entry['type'] == 'directory':
            child_path = f"{path.rstrip('/')}/{entry['name']}"
            list_recursive(child_path, indent + 1)

list_recursive("/projects/myapp")

# Remove directory
nx.rmdir("/projects/myapp/tests", recursive=True)
```

---

## See Also

- [CLI Reference Overview](index.md)
- [Python API: Directory Operations](../directory-operations.md)
- [File Operations](file-operations.md)
- [Search Operations](search.md)
