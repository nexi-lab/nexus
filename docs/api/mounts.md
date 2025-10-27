# Mount Management

← [API Documentation](README.md)

This document describes backend mount management in Nexus.

Nexus supports both **static** (configured via YAML) and **dynamic** (added at runtime) backend mounts, allowing you to attach different storage backends to different paths.

## Static vs Dynamic Mounts

### Static Mounts (YAML Configuration)

Static mounts are defined in your `nexus.yaml` configuration file and are loaded when the Nexus instance starts. These are ideal for permanent infrastructure like archive storage, shared resources, or multi-cloud setups.

```yaml
# nexus.yaml
mode: embedded
backend: local
data_dir: ./nexus-local

backends:
  - name: archives
    type: gcs
    mount_point: /archives
    bucket_name: archive-bucket
    readonly: true
    priority: 10
```

**Use static mounts when:**
- The mount configuration is permanent
- All users/applications need access
- Configuration is managed through infrastructure-as-code
- Server-side setup for remote mode

See [Configuration](configuration.md#multi-backend-support) for full YAML configuration details.

### Dynamic Mounts (Runtime API)

Dynamic mounts are added programmatically at runtime using the mount management API. These are ideal for user-specific workspaces, temporary storage, or application-specific backends.

> **⚠️ NOTE:** Dynamic mount methods exist in MountManager/PathRouter classes but are not directly exposed as NexusFS methods. Access via `nx.mount_manager` or `nx.router`.

**Use dynamic mounts when:**
- Mounts are user-specific or session-based
- Applications need to add/remove backends on-the-fly
- Multi-tenant scenarios with isolated storage per tenant
- Temporary mounts that should not persist in configuration

---

## Dynamic Mount API

### add_mount()

Add a dynamic backend mount to the filesystem.

```python
def add_mount(
    mount_point: str,
    backend_type: str,
    backend_config: dict,
    priority: int = 0,
    readonly: bool = False
) -> str
```

**Parameters:**
- `mount_point` (str): Virtual path where backend is mounted (e.g., "/personal/alice")
- `backend_type` (str): Backend type - "local", "gcs", "google_drive", etc.
- `backend_config` (dict): Backend-specific configuration
- `priority` (int): Mount priority - higher values take precedence (default: 0)
- `readonly` (bool): Whether mount is read-only (default: False)

**Returns:**
- `str`: Mount ID (unique identifier)

**Raises:**
- `ValueError`: If mount_point already exists or configuration is invalid

**Examples:**

```python
# Add personal GCS mount for user
mount_id = nx.add_mount(
    mount_point="/personal/alice",
    backend_type="gcs",
    backend_config={
        "bucket": "alice-personal-bucket",
        "project_id": "my-project"
    },
    priority=10
)

# Add local shared mount
mount_id = nx.add_mount(
    mount_point="/shared/team",
    backend_type="local",
    backend_config={
        "data_dir": "/mnt/shared/team-data"
    },
    priority=5
)

# Add read-only archive mount
mount_id = nx.add_mount(
    mount_point="/archive/2024",
    backend_type="gcs",
    backend_config={
        "bucket": "archive-bucket",
        "prefix": "2024/"
    },
    priority=1,
    readonly=True
)
```

---

### remove_mount()

Remove a backend mount.

```python
def remove_mount(
    mount_point: str
) -> bool
```

**Parameters:**
- `mount_point` (str): Virtual path of mount to remove

**Returns:**
- `bool`: True if mount was removed, False if not found

**Examples:**

```python
# Remove a mount
success = nx.remove_mount("/personal/alice")
if success:
    print("Mount removed")
else:
    print("Mount not found")
```

---

### list_mounts()

List all active backend mounts.

```python
def list_mounts() -> list[MountConfig]
```

**Returns:**
- `list[MountConfig]`: List of mount configuration objects with fields:
  - `mount_point`: Virtual path
  - `backend_type`: Backend type
  - `backend_config`: Backend configuration (dict)
  - `priority`: Mount priority
  - `readonly`: Whether read-only
  - `mount_id`: Unique mount identifier
  - `created_at`: Creation timestamp
  - `owner_user_id`: Owner user ID (optional)
  - `tenant_id`: Tenant ID (optional)

**Examples:**

```python
# List all mounts
mounts = nx.list_mounts()
for mount in mounts:
    print(f"{mount.mount_point}: {mount.backend_type} (priority={mount.priority})")

# Find user-specific mounts
user_mounts = [m for m in nx.list_mounts() if m.mount_point.startswith("/personal/alice")]
```

---

### get_mount_info()

Get information about a specific mount.

```python
def get_mount_info(
    mount_point: str
) -> MountConfig | None
```

**Parameters:**
- `mount_point` (str): Virtual path of mount

**Returns:**
- `MountConfig | None`: Mount configuration or None if not found

**Examples:**

```python
# Get mount info
mount = nx.get_mount_info("/personal/alice")
if mount:
    print(f"Backend: {mount.backend_type}")
    print(f"Priority: {mount.priority}")
    print(f"Read-only: {mount.readonly}")
else:
    print("Mount not found")
```

---

## See Also

- [Configuration](configuration.md#multi-backend-support) - Static YAML mount configuration
- [Advanced Usage](advanced-usage.md#multi-backend-patterns) - Programmatic mount examples
- [CLI Reference](cli-reference.md) - Mount commands

## Next Steps

1. Configure [backends](configuration.md#multi-backend-support)
2. Add dynamic mounts via nx.mount_manager
3. Use [CLI tools](cli-reference.md) for mount management
