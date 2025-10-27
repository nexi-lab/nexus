# Directory Operations

← [API Documentation](README.md)

This document describes directory management operations in Nexus.

### mkdir()

Create a directory.

```python
def mkdir(
    path: str,
    parents: bool = False,
    exist_ok: bool = False,
    context: OperationContext | EnhancedOperationContext | None = None
) -> None
```

**Parameters:**
- `path` (str): Virtual path to directory
- `parents` (bool): Create parent directories if needed (like `mkdir -p`)
- `exist_ok` (bool): Don't raise error if directory exists
- `context` (OperationContext | EnhancedOperationContext, optional): Operation context for permission checks (uses default if None)

**Raises:**
- `FileExistsError`: If directory exists and exist_ok=False
- `FileNotFoundError`: If parent doesn't exist and parents=False
- `InvalidPathError`: If path is invalid
- `AccessDeniedError`: If access is denied
- `PermissionError`: If path is read-only or user doesn't have WRITE permission on parent

**Examples:**

```python
# Create a directory
nx.mkdir("/documents/reports")

# Create directory with parents
nx.mkdir("/documents/2025/Q1/reports", parents=True)

# Don't error if exists
nx.mkdir("/documents", exist_ok=True)

# Create with specific user context
from nexus.core.permissions import OperationContext
ctx = OperationContext(user="alice", groups=["team-engineering"])
nx.mkdir("/workspace/alice/projects", parents=True, context=ctx)
```

---

### rmdir()

Remove a directory.

```python
def rmdir(
    path: str,
    recursive: bool = False,
    subject: tuple[str, str] | None = None,
    context: OperationContext | EnhancedOperationContext | None = None,
    tenant_id: str | None = None,
    agent_id: str | None = None,
    is_admin: bool | None = None
) -> None
```

**Parameters:**
- `path` (str): Virtual path to directory
- `recursive` (bool): Remove non-empty directory (like `rm -rf`)
- `subject` (tuple, optional): Subject performing the operation as (type, id) tuple
- `context` (OperationContext | EnhancedOperationContext, optional): Operation context for permission checks (DEPRECATED, use subject instead)
- `tenant_id` (str, optional): Legacy tenant ID (DEPRECATED)
- `agent_id` (str, optional): Legacy agent ID (DEPRECATED)
- `is_admin` (bool, optional): Admin override flag

**Raises:**
- `OSError`: If directory not empty and recursive=False
- `NexusFileNotFoundError`: If directory doesn't exist
- `InvalidPathError`: If path is invalid
- `AccessDeniedError`: If access is denied
- `PermissionError`: If path is read-only or user doesn't have WRITE permission

**Examples:**

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

---

### is_directory()

Check if path is a directory.

```python
def is_directory(
    path: str,
    context: OperationContext | EnhancedOperationContext | None = None
) -> bool
```

**Parameters:**
- `path` (str): Virtual path to check
- `context` (OperationContext | EnhancedOperationContext, optional): Operation context for permission checks (uses default if None)

**Returns:**
- `bool`: True if path is a directory, False otherwise (returns False if user lacks READ permission)

**Examples:**

```python
if nx.is_directory("/documents"):
    files = nx.list("/documents")
else:
    print("Not a directory")

# Check with specific user context
from nexus.core.permissions import OperationContext
ctx = OperationContext(user="bob", groups=["project-alpha"])
if nx.is_directory("/workspace/alice", context=ctx):
    print("Bob can see this directory")
else:
    print("Directory doesn't exist or Bob lacks permission")
```

---

## See Also

- [File Operations](file-operations.md) - Basic file operations
- [File Discovery](file-discovery.md) - Finding and listing files
- [Permissions](permissions.md) - Access control

## Next Steps

1. Learn about [permissions](permissions.md) for directory access control
2. Explore [file operations](file-operations.md) within directories
