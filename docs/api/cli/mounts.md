# CLI: Backend Mounts

← [CLI Reference](index.md) | [API Documentation](../README.md)

This document describes CLI commands for managing backend mounts and their Python API equivalents.

Backend mounts allow you to attach different storage backends (local, GCS, etc.) to different paths in your Nexus filesystem.

## mounts list - List mounts

Show all configured mounts.

**CLI:**
```bash
# Show all mounts
nexus mounts list
```

**Python API:**
```python
# List mounts
mounts = nx.list_mounts()
for mount in mounts:
    print(f"{mount['path']}: {mount['backend_type']}")
    print(f"  Priority: {mount['priority']}")
    print(f"  Config: {mount['config']}")
```

**See Also:**
- [Python API: list_mounts()](../mounts.md#list_mounts)

---

## mounts add - Add mount

Add a new backend mount at a specific path.

**CLI:**
```bash
# Add GCS mount
nexus mounts add /personal/alice gcs '{"bucket":"alice-bucket"}' --priority 10

# Add local mount
nexus mounts add /shared local '{"path":"/shared-data"}' --priority 5
```

**Python API:**
```python
# Add GCS mount
nx.add_mount(
    path="/personal/alice",
    backend_type="gcs",
    config={"bucket": "alice-bucket"},
    priority=10
)

# Add local mount
nx.add_mount(
    path="/shared",
    backend_type="local",
    config={"path": "/shared-data"},
    priority=5
)
```

**Options:**
- `--priority NUM`: Mount priority (higher = higher priority)

**See Also:**
- [Python API: add_mount()](../mounts.md#add_mount)

---

## mounts info - Show mount info

Get detailed information about a specific mount.

**CLI:**
```bash
# Get mount details
nexus mounts info /personal/alice
```

**Python API:**
```python
# Get mount info
info = nx.get_mount_info("/personal/alice")
print(f"Backend: {info['backend_type']}")
print(f"Config: {info['config']}")
print(f"Priority: {info['priority']}")
print(f"Created: {info['created_at']}")
```

**See Also:**
- [Python API: get_mount_info()](../mounts.md#get_mount_info)

---

## mounts remove - Remove mount

Remove a mount point.

**CLI:**
```bash
# Remove mount
nexus mounts remove /personal/alice
```

**Python API:**
```python
# Remove mount
nx.remove_mount("/personal/alice")
```

**See Also:**
- [Python API: remove_mount()](../mounts.md#remove_mount)

---

## Common Workflows

### Multi-backend setup
```bash
# Add personal GCS storage for each user
nexus mounts add /personal/alice gcs '{"bucket":"alice-data"}' --priority 10
nexus mounts add /personal/bob gcs '{"bucket":"bob-data"}' --priority 10

# Add shared local storage
nexus mounts add /shared local '{"path":"/mnt/shared"}' --priority 5

# List all mounts
nexus mounts list

# Files in /personal/alice/* use GCS backend
# Files in /shared/* use local backend
```

### Python equivalent
```python
# Add personal GCS storage for each user
users = ["alice", "bob"]
for user in users:
    nx.add_mount(
        path=f"/personal/{user}",
        backend_type="gcs",
        config={"bucket": f"{user}-data"},
        priority=10
    )

# Add shared local storage
nx.add_mount(
    path="/shared",
    backend_type="local",
    config={"path": "/mnt/shared"},
    priority=5
)

# List all mounts
mounts = nx.list_mounts()
for mount in mounts:
    print(f"{mount['path']}: {mount['backend_type']} (priority: {mount['priority']})")
```

---

## See Also

- [CLI Reference Overview](index.md)
- [Python API: Mounts](../mounts.md)
- [Configuration](../configuration.md)
