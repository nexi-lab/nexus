# Versioning and Workspace Snapshots

← [API Documentation](README.md)

This document describes version tracking and workspace snapshot capabilities in Nexus.


⚠️ **NOTE:** In workspace_diff(), the `agent_id` parameter is REQUIRED (not optional as currently documented)

Nexus provides automatic version tracking for all file writes using content-addressable storage (CAS).

### get_version()

Retrieve a specific version of a file.

```python
def get_version(
    path: str,
    version: int,
    context: OperationContext | EnhancedOperationContext | None = None
) -> bytes
```

**Parameters:**
- `path` (str): Virtual file path
- `version` (int): Version number to retrieve
- `context` (OperationContext | EnhancedOperationContext, optional): Operation context for permission checks (uses default if None)

**Returns:**
- `bytes`: File content for the specified version

**Raises:**
- `NexusFileNotFoundError`: If file or version doesn't exist
- `InvalidPathError`: If path is invalid
- `PermissionError`: If user doesn't have READ permission

**Examples:**

```python
# Get version 3 of a file
content_v3 = nx.get_version("/documents/report.txt", 3)

# Get version with specific context
from nexus.core.permissions import OperationContext
ctx = OperationContext(user="alice", groups=["engineering"])
content = nx.get_version("/workspace/file.txt", 5, context=ctx)
```

---

### list_versions()

List all versions of a file.

```python
def list_versions(
    path: str,
    context: OperationContext | EnhancedOperationContext | None = None
) -> list[dict[str, Any]]
```

**Parameters:**
- `path` (str): Virtual file path
- `context` (OperationContext | EnhancedOperationContext, optional): Operation context for permission checks (uses default if None)

**Returns:**
- `list[dict]`: List of version info dicts (newest first) with keys:
  - `version`: Version number
  - `size`: File size in bytes
  - `etag`: Content hash
  - `created_at`: Creation timestamp
  - `is_rollback`: Whether this was a rollback operation

**Raises:**
- `PermissionError`: If user doesn't have READ permission

**Examples:**

```python
# List all versions
versions = nx.list_versions("/documents/report.txt")
for v in versions:
    print(f"v{v['version']}: {v['size']} bytes at {v['created_at']}")

# List versions with specific context
from nexus.core.permissions import OperationContext
ctx = OperationContext(user="alice", groups=["engineering"])
versions = nx.list_versions("/workspace/file.txt", context=ctx)
```

---

### rollback()

Rollback file to a previous version.

```python
def rollback(
    path: str,
    version: int,
    context: OperationContext | EnhancedOperationContext | None = None
) -> None
```

**Parameters:**
- `path` (str): Virtual file path
- `version` (int): Version number to rollback to
- `context` (OperationContext | EnhancedOperationContext, optional): Operation context for permission checks

**Raises:**
- `NexusFileNotFoundError`: If file or version doesn't exist
- `InvalidPathError`: If path is invalid
- `PermissionError`: If user doesn't have write permission

**Examples:**

```python
# Rollback to version 5
nx.rollback("/documents/report.txt", 5)
```

---

### diff_versions()

Compare two versions of a file.

```python
def diff_versions(
    path: str,
    v1: int,
    v2: int,
    mode: str = "metadata",
    context: OperationContext | EnhancedOperationContext | None = None
) -> dict[str, Any] | str
```

**Parameters:**
- `path` (str): Virtual file path
- `v1` (int): First version number
- `v2` (int): Second version number
- `mode` (str): Diff mode - "metadata" (default) or "content"
- `context` (OperationContext | EnhancedOperationContext, optional): Operation context for permission checks (uses default if None)

**Returns:**
- `dict`: Metadata differences (if mode="metadata")
- `str`: Unified diff string (if mode="content")

**Raises:**
- `NexusFileNotFoundError`: If file or version doesn't exist
- `InvalidPathError`: If path is invalid
- `ValueError`: If mode is invalid
- `PermissionError`: If user doesn't have READ permission

**Examples:**

```python
# Compare metadata
diff = nx.diff_versions("/documents/report.txt", 5, 7, mode="metadata")
print(f"Size changed from {diff['v1']['size']} to {diff['v2']['size']}")

# Compare content
diff = nx.diff_versions("/documents/report.txt", 5, 7, mode="content")
print(diff)  # Unified diff output

# Compare with specific context
from nexus.core.permissions import OperationContext
ctx = OperationContext(user="alice", groups=["engineering"])
diff = nx.diff_versions("/workspace/file.txt", 1, 3, context=ctx)
```

---

## Workspace Snapshots

Workspace snapshots allow you to capture and restore the entire state of an agent's workspace.

### workspace_snapshot()

Create a snapshot of a registered workspace.

```python
def workspace_snapshot(
    workspace_path: str | None = None,
    agent_id: str | None = None,  # DEPRECATED: Use workspace_path instead
    description: str | None = None,
    tags: list[str] | None = None,
    created_by: str | None = None
) -> dict[str, Any]
```

**Parameters:**
- `workspace_path` (str, optional): Path to registered workspace (e.g., "/my-workspace")
- `agent_id` (str, optional): **DEPRECATED** - Use workspace_path instead
- `description` (str, optional): Human-readable description of snapshot
- `tags` (list[str], optional): List of tags for categorization
- `created_by` (str, optional): User/agent who created the snapshot

**Returns:**
- `dict`: Snapshot metadata with keys:
  - `snapshot_number`: Snapshot version number
  - `created_at`: Creation timestamp
  - `description`: Snapshot description
  - `tags`: List of tags
  - `file_count`: Number of files in snapshot

**Raises:**
- `ValueError`: If workspace not registered or workspace_path not provided
- `BackendError`: If snapshot cannot be created

**Examples:**

```python
# First, register a workspace
nx.register_workspace("/my-workspace", name="dev", description="Development workspace")

# Create a snapshot
snapshot = nx.workspace_snapshot(
    workspace_path="/my-workspace",
    description="Before major refactoring",
    tags=["pre-refactor", "stable"]
)
print(f"Created snapshot {snapshot['snapshot_number']}")

# Create snapshot with created_by tracking
snapshot = nx.workspace_snapshot(
    workspace_path="/my-workspace",
    description="Checkpoint",
    created_by="alice"
)
```

---

### workspace_restore()

Restore workspace to a previous snapshot.

```python
def workspace_restore(
    snapshot_number: int,
    workspace_path: str | None = None,
    agent_id: str | None = None  # DEPRECATED: Use workspace_path instead
) -> dict[str, Any]
```

**Parameters:**
- `snapshot_number` (int): Snapshot version number to restore
- `workspace_path` (str, optional): Path to registered workspace
- `agent_id` (str, optional): **DEPRECATED** - Use workspace_path instead

**Returns:**
- `dict`: Restore operation result with keys:
  - `files_restored`: Number of files restored
  - `workspace_path`: Path to the workspace
  - `snapshot_number`: Snapshot that was restored

**Raises:**
- `ValueError`: If workspace not registered or workspace_path not provided
- `NexusFileNotFoundError`: If snapshot not found

**Examples:**

```python
# Restore to snapshot 5
result = nx.workspace_restore(5, workspace_path="/my-workspace")
print(f"Restored {result['files_restored']} files")
```

---

### workspace_log()

List snapshot history for workspace.

```python
def workspace_log(
    workspace_path: str | None = None,
    agent_id: str | None = None,  # DEPRECATED: Use workspace_path instead
    limit: int = 100
) -> list[dict[str, Any]]
```

**Parameters:**
- `workspace_path` (str, optional): Path to registered workspace
- `agent_id` (str, optional): **DEPRECATED** - Use workspace_path instead
- `limit` (int): Maximum number of snapshots to return (default: 100)

**Returns:**
- `list[dict]`: List of snapshot metadata dicts (most recent first) with keys:
  - `snapshot_number`: Snapshot version number
  - `created_at`: Creation timestamp
  - `description`: Snapshot description
  - `tags`: List of tags
  - `created_by`: Who created the snapshot

**Raises:**
- `ValueError`: If workspace not registered or workspace_path not provided

**Examples:**

```python
# List recent snapshots
snapshots = nx.workspace_log(workspace_path="/my-workspace", limit=10)
for s in snapshots:
    print(f"Snapshot {s['snapshot_number']}: {s['description']}")
```

---

### workspace_diff()

Compare two workspace snapshots.

```python
def workspace_diff(
    snapshot_1: int,
    snapshot_2: int,
    agent_id: str | None = None
) -> dict[str, Any]
```

**Parameters:**
- `snapshot_1` (int): First snapshot number
- `snapshot_2` (int): Second snapshot number
- `agent_id` (str, optional): Agent identifier (uses default if not provided)

**Returns:**
- `dict`: Diff dict with keys:
  - `added`: List of files added
  - `removed`: List of files removed
  - `modified`: List of files modified

**Raises:**
- `ValueError`: If agent_id not provided and no default set
- `NexusFileNotFoundError`: If either snapshot not found

**Examples:**

```python
# Compare two snapshots
diff = nx.workspace_diff(5, 7, agent_id="agent-123")
print(f"Added: {len(diff['added'])} files")
print(f"Removed: {len(diff['removed'])} files")
print(f"Modified: {len(diff['modified'])} files")
```

---

## See Also

- [Workspace Management](workspace-management.md) - Workspace registry
- [File Operations](file-operations.md) - Basic file operations
- [CLI Reference](cli-reference.md) - Version control commands

## Next Steps

1. Register workspaces with [workspace management](workspace-management.md)
2. Learn about [file operations](file-operations.md) with version control
