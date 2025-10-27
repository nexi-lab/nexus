# Memory Registry Management

← [API Documentation](README.md)

This document describes memory registration and management for long-term knowledge storage.

Similar to workspaces, you can register directories as memories for long-term knowledge storage. Memories are persistent data stores that can be shared across agents and sessions.

### register_memory()

Register a directory as a memory for persistent knowledge storage.

```python
def register_memory(
    path: str,
    name: str | None = None,
    description: str = "",
    created_by: str | None = None,
    metadata: dict | None = None
) -> dict
```

**Parameters:**
- `path` (str): Absolute path to memory directory (e.g., "/my-memory")
- `name` (str, optional): Friendly name for the memory
- `description` (str): Human-readable description (default: "")
- `created_by` (str, optional): User/agent who created it (for audit)
- `metadata` (dict, optional): Additional user-defined metadata

**Returns:**
- `dict`: Memory configuration dict with keys:
  - `path`: Memory path
  - `name`: Memory name
  - `description`: Description
  - `created_by`: Creator
  - `created_at`: Creation timestamp
  - `metadata`: Additional metadata

**Raises:**
- `ValueError`: If path already registered as memory

**Examples:**

```python
# Register a memory for knowledge base
config = nx.register_memory(
    "/knowledge-base",
    name="kb",
    description="Shared knowledge base"
)
print(f"Registered memory: {config['path']}")

# Register with metadata
config = nx.register_memory(
    "/project-docs",
    name="docs",
    description="Project documentation",
    created_by="alice",
    metadata={"project_id": "12345", "team": "engineering"}
)
```

---

### unregister_memory()

Unregister a memory (does NOT delete files, only removes registration).

```python
def unregister_memory(
    path: str
) -> bool
```

**Parameters:**
- `path` (str): Memory path to unregister

**Returns:**
- `bool`: True if unregistered, False if not found

**Examples:**

```python
# Unregister a memory
success = nx.unregister_memory("/my-memory")
if success:
    print("Memory unregistered")
```

---

### list_memories()

List all registered memories.

```python
def list_memories() -> list[dict]
```

**Returns:**
- `list[dict]`: List of memory configuration dicts

**Examples:**

```python
# List all memories
memories = nx.list_memories()
for mem in memories:
    print(f"{mem['path']}: {mem['name']}")
```

---

### get_memory_info()

Get information about a registered memory.

```python
def get_memory_info(
    path: str
) -> dict | None
```

**Parameters:**
- `path` (str): Memory path

**Returns:**
- `dict | None`: Memory configuration dict or None if not found

**Examples:**

```python
# Get memory info
info = nx.get_memory_info("/knowledge-base")
if info:
    print(f"Memory: {info['name']}")
    print(f"Created by: {info['created_by']}")
    print(f"Description: {info['description']}")
else:
    print("Memory not found")
```

---

## See Also

- [Workspace Management](workspace-management.md) - Similar registry for workspaces
- [Semantic Search](semantic-search.md) - Search across memories
- [Configuration](configuration.md) - YAML configuration

## Next Steps

1. Set up [semantic search](semantic-search.md) for memory queries
2. Configure [workspace registries](workspace-management.md)
