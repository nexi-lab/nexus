# Workspace Registry Management

← [API Documentation](README.md)

This document describes workspace registration and management APIs.

Before using workspace snapshots, you must register directories as workspaces. These functions manage workspace registration.

### register_workspace()

Register a directory as a workspace to enable snapshot functionality.

```python
def register_workspace(
    path: str,
    name: str | None = None,
    description: str = "",
    created_by: str | None = None,
    metadata: dict | None = None
) -> dict
```

**Parameters:**
- `path` (str): Absolute path to workspace directory (e.g., "/my-workspace")
- `name` (str, optional): Friendly name for the workspace
- `description` (str): Human-readable description (default: "")
- `created_by` (str, optional): User/agent who created it (for audit)
- `metadata` (dict, optional): Additional user-defined metadata

**Returns:**
- `dict`: Workspace configuration dict with keys:
  - `path`: Workspace path
  - `name`: Workspace name
  - `description`: Description
  - `created_by`: Creator
  - `created_at`: Creation timestamp
  - `metadata`: Additional metadata

**Raises:**
- `ValueError`: If path already registered as workspace

**Examples:**

```python
# Register a workspace
config = nx.register_workspace(
    "/my-workspace",
    name="main",
    description="My main workspace"
)
print(f"Registered workspace: {config['path']}")

# Register with metadata
config = nx.register_workspace(
    "/project-workspace",
    name="project-1",
    description="Project workspace",
    created_by="alice",
    metadata={"project_id": "12345", "team": "engineering"}
)
```

---

### unregister_workspace()

Unregister a workspace (does NOT delete files, only removes registration).

```python
def unregister_workspace(
    path: str
) -> bool
```

**Parameters:**
- `path` (str): Workspace path to unregister

**Returns:**
- `bool`: True if unregistered, False if not found

**Examples:**

```python
# Unregister a workspace
success = nx.unregister_workspace("/my-workspace")
if success:
    print("Workspace unregistered")
```

---

### list_workspaces()

List all registered workspaces.

```python
def list_workspaces() -> list[dict]
```

**Returns:**
- `list[dict]`: List of workspace configuration dicts

**Examples:**

```python
# List all workspaces
workspaces = nx.list_workspaces()
for ws in workspaces:
    print(f"{ws['path']}: {ws['name']}")
```

---

### get_workspace_info()

Get information about a registered workspace.

```python
def get_workspace_info(
    path: str
) -> dict | None
```

**Parameters:**
- `path` (str): Workspace path

**Returns:**
- `dict | None`: Workspace configuration dict or None if not found

**Examples:**

```python
# Get workspace info
info = nx.get_workspace_info("/my-workspace")
if info:
    print(f"Workspace: {info['name']}")
    print(f"Created by: {info['created_by']}")
    print(f"Description: {info['description']}")
else:
    print("Workspace not found")
```

---

### load_workspace_memory_config()

Load workspaces and memories from configuration (YAML or dict).

```python
def load_workspace_memory_config(
    workspaces: list[dict] | None = None,
    memories: list[dict] | None = None
) -> dict[str, Any]
```

**Parameters:**
- `workspaces` (list[dict], optional): List of workspace config dicts with keys:
  - `path` (required): Workspace path
  - `name` (optional): Friendly name
  - `description` (optional): Description
  - `created_by` (optional): Creator
  - `metadata` (optional): Additional metadata dict
- `memories` (list[dict], optional): List of memory config dicts (same format)

**Returns:**
- `dict`: Registration results with keys:
  - `workspaces_registered`: Number of workspaces registered
  - `workspaces_skipped`: Number already registered
  - `memories_registered`: Number of memories registered
  - `memories_skipped`: Number already registered

**Examples:**

```python
# Load from YAML config
import yaml

with open("nexus.yaml") as f:
    config = yaml.safe_load(f)

results = nx.load_workspace_memory_config(
    workspaces=config.get("workspaces"),
    memories=config.get("memories")
)
print(f"Registered {results['workspaces_registered']} workspaces")
print(f"Registered {results['memories_registered']} memories")

# Load from dict
config = {
    "workspaces": [
        {"path": "/my-workspace", "name": "main"},
        {"path": "/team/project", "name": "team-project"}
    ],
    "memories": [
        {"path": "/my-memory", "name": "kb"}
    ]
}
results = nx.load_workspace_memory_config(**config)
```

**Example YAML Configuration:**

```yaml
workspaces:
  - path: /my-workspace
    name: main
    description: My main workspace
    created_by: alice

  - path: /team/project
    name: team-project
    description: Team collaboration workspace
    metadata:
      project_id: "12345"

memories:
  - path: /my-memory
    name: knowledge-base
    description: Personal knowledge base
```

---

## See Also

- [Versioning](versioning.md) - Workspace snapshots
- [Memory Management](memory-management.md) - Similar registry for memories
- [Configuration](configuration.md) - YAML configuration

## Next Steps

1. Create [workspace snapshots](versioning.md) for version control
2. Set up [memory registries](memory-management.md) for knowledge storage
