# CLI: Workspace Management

← [CLI Reference](index.md) | [API Documentation](../README.md)

This document describes CLI commands for workspace management and their Python API equivalents.

Workspaces allow you to snapshot and restore entire directory trees, track changes over time, and manage project states.

## workspace register - Register workspace

Register a directory as a workspace for snapshot tracking.

**CLI:**
```bash
# Register workspace
nexus workspace register /my-workspace --name main --description "My workspace"

# With metadata
nexus workspace register /my-workspace --name main --created-by alice
```

**Python API:**
```python
# Register workspace
nx.register_workspace("/my-workspace", name="main", description="My workspace")

# With metadata
nx.register_workspace(
    "/my-workspace",
    name="main",
    description="My workspace",
    metadata={"created_by": "alice", "project": "app"}
)
```

**Options:**
- `--name TEXT`: Workspace name (required)
- `--description TEXT`: Description of the workspace
- `--created-by TEXT`: Creator name
- `--remote-url URL`: Connect to remote server
- `--remote-api-key KEY`: API key for authentication

**See Also:**
- [Python API: register_workspace()](../workspace-management.md#register_workspace)

---

## workspace list - List workspaces

List all registered workspaces.

**CLI:**
```bash
# List all registered workspaces
nexus workspace list
```

**Python API:**
```python
# List workspaces
workspaces = nx.list_workspaces()
for ws in workspaces:
    print(f"{ws['path']} - {ws['name']}: {ws['description']}")
```

**See Also:**
- [Python API: list_workspaces()](../workspace-management.md#list_workspaces)

---

## workspace info - Show workspace info

Get detailed information about a workspace.

**CLI:**
```bash
# Get workspace details
nexus workspace info /my-workspace
```

**Python API:**
```python
# Get workspace info
info = nx.get_workspace_info("/my-workspace")
print(f"Name: {info['name']}")
print(f"Description: {info['description']}")
print(f"Snapshots: {info['snapshot_count']}")
print(f"Created: {info['created_at']}")
```

**See Also:**
- [Python API: get_workspace_info()](../workspace-management.md#get_workspace_info)

---

## workspace snapshot - Create snapshot

Create a snapshot of the current workspace state.

**CLI:**
```bash
# Create snapshot
nexus workspace snapshot /my-workspace --description "Before refactor"

# With tags
nexus workspace snapshot /my-workspace --description "Stable" --tag stable --tag v1.0
```

**Python API:**
```python
# Create snapshot
snapshot_id = nx.create_workspace_snapshot(
    "/my-workspace",
    description="Before refactor"
)
print(f"Created snapshot: {snapshot_id}")

# With tags and metadata
snapshot_id = nx.create_workspace_snapshot(
    "/my-workspace",
    description="Stable release",
    tags=["stable", "v1.0"],
    metadata={"release": "1.0.0", "author": "alice"}
)
```

**Options:**
- `--description TEXT`: Snapshot description (required)
- `--tag TEXT`: Add tag (can be used multiple times)
- `--remote-url URL`: Connect to remote server
- `--remote-api-key KEY`: API key for authentication

**See Also:**
- [Python API: create_workspace_snapshot()](../workspace-management.md#create_workspace_snapshot)

---

## workspace log - Show snapshot history

Display the snapshot history of a workspace.

**CLI:**
```bash
# Show all snapshots
nexus workspace log /my-workspace

# Limit results
nexus workspace log /my-workspace --limit 10
```

**Python API:**
```python
# Get snapshot history
snapshots = nx.list_workspace_snapshots("/my-workspace")
for snapshot in snapshots:
    print(f"Snapshot {snapshot['snapshot_id']}: {snapshot['description']}")
    print(f"  Created: {snapshot['created_at']}")
    print(f"  Tags: {', '.join(snapshot.get('tags', []))}")

# Limit results
recent_snapshots = nx.list_workspace_snapshots("/my-workspace", limit=10)
```

**Options:**
- `--limit NUM`: Limit number of snapshots returned
- `--remote-url URL`: Connect to remote server
- `--remote-api-key KEY`: API key for authentication

**See Also:**
- [Python API: list_workspace_snapshots()](../workspace-management.md#list_workspace_snapshots)

---

## workspace restore - Restore snapshot

Restore workspace to a previous snapshot.

**CLI:**
```bash
# Restore to snapshot 5
nexus workspace restore /my-workspace --snapshot 5
```

**Python API:**
```python
# Restore to snapshot
nx.restore_workspace_snapshot("/my-workspace", snapshot_id=5)

# Restore and verify
nx.restore_workspace_snapshot("/my-workspace", snapshot_id=5)
info = nx.get_workspace_info("/my-workspace")
print(f"Restored to snapshot {info['current_snapshot']}")
```

**Options:**
- `--snapshot NUM`: Snapshot ID to restore to
- `--remote-url URL`: Connect to remote server
- `--remote-api-key KEY`: API key for authentication

**See Also:**
- [Python API: restore_workspace_snapshot()](../workspace-management.md#restore_workspace_snapshot)

---

## workspace diff - Compare snapshots

Compare two workspace snapshots.

**CLI:**
```bash
# Compare snapshots
nexus workspace diff /my-workspace --snapshot-1 5 --snapshot-2 7
```

**Python API:**
```python
# Compare snapshots
diff = nx.diff_workspace_snapshots("/my-workspace", snapshot1=5, snapshot2=7)
print(f"Files added: {len(diff['added'])}")
print(f"Files modified: {len(diff['modified'])}")
print(f"Files deleted: {len(diff['deleted'])}")

for file in diff['added']:
    print(f"  + {file}")
for file in diff['modified']:
    print(f"  M {file}")
for file in diff['deleted']:
    print(f"  - {file}")
```

**Options:**
- `--snapshot-1 NUM`: First snapshot ID
- `--snapshot-2 NUM`: Second snapshot ID
- `--remote-url URL`: Connect to remote server
- `--remote-api-key KEY`: API key for authentication

**See Also:**
- [Python API: diff_workspace_snapshots()](../workspace-management.md#diff_workspace_snapshots)

---

## workspace unregister - Unregister workspace

Unregister a workspace (doesn't delete files).

**CLI:**
```bash
# Unregister (doesn't delete files)
nexus workspace unregister /my-workspace
```

**Python API:**
```python
# Unregister workspace
nx.unregister_workspace("/my-workspace")
# Note: Files in /my-workspace are not deleted, only workspace tracking is removed
```

**See Also:**
- [Python API: unregister_workspace()](../workspace-management.md#unregister_workspace)

---

## Common Workflows

### Basic workspace management
```bash
# Register a project workspace
nexus workspace register /projects/myapp --name myapp --description "My application"

# Make some changes
nexus write /projects/myapp/src/main.py "print('hello')"
nexus write /projects/myapp/README.md "# My App"

# Create checkpoint before refactoring
nexus workspace snapshot /projects/myapp --description "Before refactor" --tag pre-refactor

# Make more changes
nexus write /projects/myapp/src/main.py "print('hello world')"
nexus mkdir /projects/myapp/tests --parents

# Create another snapshot
nexus workspace snapshot /projects/myapp --description "After refactor" --tag post-refactor

# View history
nexus workspace log /projects/myapp

# Compare snapshots
nexus workspace diff /projects/myapp --snapshot-1 1 --snapshot-2 2

# Restore if needed
nexus workspace restore /projects/myapp --snapshot 1
```

### Python equivalent
```python
import nexus

# Initialize
nx = nexus.Nexus(data_dir="./nexus-data")

# Register a project workspace
nx.register_workspace("/projects/myapp", name="myapp", description="My application")

# Make some changes
nx.write("/projects/myapp/src/main.py", b"print('hello')")
nx.write("/projects/myapp/README.md", b"# My App")

# Create checkpoint before refactoring
snapshot1 = nx.create_workspace_snapshot(
    "/projects/myapp",
    description="Before refactor",
    tags=["pre-refactor"]
)
print(f"Created snapshot: {snapshot1}")

# Make more changes
nx.write("/projects/myapp/src/main.py", b"print('hello world')")
nx.mkdir("/projects/myapp/tests", parents=True)

# Create another snapshot
snapshot2 = nx.create_workspace_snapshot(
    "/projects/myapp",
    description="After refactor",
    tags=["post-refactor"]
)

# View history
snapshots = nx.list_workspace_snapshots("/projects/myapp")
for snap in snapshots:
    print(f"Snapshot {snap['snapshot_id']}: {snap['description']}")
    print(f"  Tags: {snap.get('tags', [])}")

# Compare snapshots
diff = nx.diff_workspace_snapshots("/projects/myapp", snapshot1=1, snapshot2=2)
print(f"\nChanges:")
print(f"  Added: {len(diff['added'])} files")
print(f"  Modified: {len(diff['modified'])} files")
print(f"  Deleted: {len(diff['deleted'])} files")

# Restore if needed
nx.restore_workspace_snapshot("/projects/myapp", snapshot_id=1)
print("Restored to snapshot 1")
```

---

## See Also

- [CLI Reference Overview](index.md)
- [Python API: Workspace Management](../workspace-management.md)
- [Versioning](versioning.md)
- [Memory Management](memory.md)
