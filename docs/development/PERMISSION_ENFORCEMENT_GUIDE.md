# Permission Enforcement in Nexus

## Overview

Nexus now supports multi-layer permission enforcement for file operations. This guide explains how to use the new permission system in your applications.

## Quick Start

### Python SDK

```python
from nexus import connect
from nexus.core.permissions import OperationContext

# Option 1: Enable permissions globally (recommended for production)
nx = connect(
    backend_type="local",
    agent_id="alice",
    is_admin=False,
    enforce_permissions=True,  # Enable permission enforcement
)

# Now all operations automatically check permissions
nx.write("/workspace/file.txt", b"content")  # Checks if alice can write
content = nx.read("/workspace/file.txt")     # Checks if alice can read

# Option 2: Override context per-operation
admin_context = OperationContext(user="admin", groups=["admins"], is_admin=True)
nx.write("/system/config.yml", b"data", context=admin_context)  # Bypass checks as admin
```

### CLI

The CLI automatically creates an `OperationContext` based on the current system user:

```bash
# Standard usage - uses current user context
nexus write /workspace/file.txt --content "hello"
nexus read /workspace/file.txt

# Override user context for testing
nexus write /workspace/file.txt --content "hello" --as-user alice
nexus read /workspace/file.txt --as-user bob  # May fail if bob can't read

# Admin operations
nexus --admin chmod 755 /workspace/script.sh
```

## Architecture

### Three-Layer Permission Model

Nexus uses a hierarchical permission check system:

1. **ReBAC (Relationship-Based Access Control)** - Check graph relationships
   - Example: Alice is member of "engineering" team which owns the file
   - Status: Stub implementation (Phase 2)

2. **ACL (Access Control Lists)** - Check explicit allow/deny entries
   - Example: `user:bob:r--` grants Bob read access
   - Status: Stub implementation (Phase 2)

3. **UNIX Permissions** - Check owner/group/other mode bits
   - Example: `rw-r--r--` (0o644) - owner can write, others can read
   - Status: ✅ Fully implemented

The enforcer short-circuits on first match:
- If ReBAC grants permission → allow
- If ACL denies explicitly → deny
- If ACL allows explicitly → allow
- Fall back to UNIX permissions
- Default deny if no permissions set

### Components

#### OperationContext

Carries user/agent authentication context through all filesystem operations:

```python
from nexus.core.permissions import OperationContext

# Regular user
ctx = OperationContext(
    user="alice",
    groups=["developers", "team-alpha"],
    is_admin=False,
    is_system=False
)

# Admin user (bypasses all checks)
admin_ctx = OperationContext(
    user="admin",
    groups=["admins"],
    is_admin=True,
    is_system=False
)

# System context (bypasses all checks)
system_ctx = OperationContext(
    user="system",
    groups=[],
    is_admin=False,
    is_system=True
)
```

#### PermissionEnforcer

Multi-layer permission enforcement engine:

```python
from nexus.core.permissions import PermissionEnforcer, Permission

enforcer = PermissionEnforcer(
    metadata_store=metadata,
    acl_store=None,      # TODO: Phase 2
    rebac_manager=None,  # TODO: Phase 2
)

# Check if user can read file
can_read = enforcer.check("/workspace/file.txt", Permission.READ, ctx)

# Filter list by permissions
readable_files = enforcer.filter_list(all_files, ctx)
```

## Usage Patterns

### Pattern 1: Global Enforcement (Recommended for Production)

Enable permission enforcement when creating the filesystem:

```python
from nexus import connect

# Production configuration
nx = connect(
    backend_type="local",
    agent_id="alice",
    tenant_id="acme-corp",
    is_admin=False,
    enforce_permissions=True,  # ✅ Enable globally
)

# All operations now enforce permissions
try:
    nx.write("/shared/readonly.txt", b"data")
except PermissionError as e:
    print(f"Access denied: {e}")
```

### Pattern 2: Selective Enforcement

Enable enforcement only for specific operations:

```python
from nexus import connect
from nexus.core.permissions import OperationContext

# Default: no enforcement
nx = connect(backend_type="local", agent_id="alice")

# Enable for specific operation
nx._enforce_permissions = True
ctx = OperationContext(user="alice", groups=["developers"])

try:
    nx._check_permission("/sensitive/data.txt", Permission.WRITE, ctx)
    nx.write("/sensitive/data.txt", b"secret data")
except PermissionError:
    print("Access denied!")
```

### Pattern 3: Context Override

Override context per-operation (useful for service accounts):

```python
from nexus import connect
from nexus.core.permissions import OperationContext

nx = connect(backend_type="local", enforce_permissions=True)

# User operation
user_ctx = OperationContext(user="alice", groups=["users"])
nx.write("/workspace/alice/file.txt", b"data", context=user_ctx)

# Admin operation
admin_ctx = OperationContext(user="admin", groups=["admins"], is_admin=True)
nx.write("/system/config.yml", b"admin data", context=admin_ctx)

# System operation (bypasses all checks)
system_ctx = OperationContext(user="system", groups=[], is_system=True)
nx.write("/system/internal.db", b"system data", context=system_ctx)
```

### Pattern 4: Multi-Tenant Isolation

Use tenant_id for multi-tenant isolation:

```python
# Tenant A
nx_tenant_a = connect(
    backend_type="local",
    tenant_id="tenant-a",
    agent_id="alice",
    enforce_permissions=True,
)

# Tenant B
nx_tenant_b = connect(
    backend_type="local",
    tenant_id="tenant-b",
    agent_id="bob",
    enforce_permissions=True,
)

# Each tenant can only access their own files
nx_tenant_a.write("/workspace/file.txt", b"tenant a data")
nx_tenant_b.write("/workspace/file.txt", b"tenant b data")  # Different file!
```

## Permission Modes

### UNIX-Style Permissions

Files have owner, group, and mode (like Linux):

```python
from nexus import connect
from nexus.cli import chmod, chown, chgrp

nx = connect(backend_type="local", enforce_permissions=True)

# Create file (gets default permissions from policy)
nx.write("/workspace/file.txt", b"data")

# Change permissions
chmod(nx, "644", "/workspace/file.txt")    # rw-r--r--
chown(nx, "alice", "/workspace/file.txt")
chgrp(nx, "developers", "/workspace/file.txt")

# Check permissions
meta = nx.metadata.get("/workspace/file.txt")
print(f"Owner: {meta.owner}")
print(f"Group: {meta.group}")
print(f"Mode: {oct(meta.mode)}")  # 0o644
```

### Permission Inheritance

New files automatically inherit permissions from their parent directory:

```python
# Create parent directory with specific permissions
nx.mkdir("/workspace/project", parents=True)
chmod(nx, "755", "/workspace/project")
chown(nx, "alice", "/workspace/project")
chgrp(nx, "developers", "/workspace/project")

# New file inherits from parent
nx.write("/workspace/project/file.txt", b"data")

# File gets: owner=alice, group=developers, mode=0o644 (x bits cleared)
meta = nx.metadata.get("/workspace/project/file.txt")
assert meta.owner == "alice"
assert meta.group == "developers"
assert meta.mode == 0o644  # rwx becomes rw-
```

## Testing

### Unit Tests

```python
import pytest
from nexus import connect
from nexus.core.permissions import OperationContext

def test_permission_enforcement():
    nx = connect(backend_type="local", enforce_permissions=True)

    # Setup test file
    admin_ctx = OperationContext(user="root", groups=["root"], is_admin=True)
    nx.write("/test/file.txt", b"data", context=admin_ctx)
    nx.chmod("/test/file.txt", "600")  # Owner only

    # Test read access
    owner_ctx = OperationContext(user="root", groups=["root"])
    content = nx.read("/test/file.txt", context=owner_ctx)  # ✅ Success

    other_ctx = OperationContext(user="bob", groups=["users"])
    with pytest.raises(PermissionError):
        nx.read("/test/file.txt", context=other_ctx)  # ❌ Denied
```

### Integration Tests

```bash
# Test file permissions
nexus write /test/file.txt --content "secret"
nexus chmod 600 /test/file.txt
nexus chown alice /test/file.txt

# Verify access control
nexus read /test/file.txt --as-user alice  # ✅ Success
nexus read /test/file.txt --as-user bob    # ❌ Should fail
```

## Migration Guide


Permission enforcement is **opt-in** for backward compatibility:

```python
# Old code (still works - no enforcement)
nx = connect(backend_type="local", agent_id="alice")
nx.write("/file.txt", b"data")  # ✅ No permission check

# New code (opt-in enforcement)
nx = connect(
    backend_type="local",
    agent_id="alice",
    enforce_permissions=True,  # ✅ Enable checks
)
nx.write("/file.txt", b"data")  # ✅ Checks permissions
```

### Setting Default Permissions

For existing files without permissions, set defaults:

```python
from nexus import connect
from nexus.core.permissions import FilePermissions, FileMode

nx = connect(backend_type="local")

# Migrate existing files
for path in nx.list("/", recursive=True):
    meta = nx.metadata.get(path)
    if meta and not meta.owner:
        # Set default permissions
        meta.owner = "root"
        meta.group = "root"
        meta.mode = 0o644  # rw-r--r--
        nx.metadata.put(meta)
```

## Configuration

### Via Code

```python
nx = connect(
    backend_type="local",
    agent_id="alice",                    # Used for default context
    tenant_id="acme-corp",               # Used for isolation
    is_admin=False,                      # Admin bypass
    enforce_permissions=True,             # Enable enforcement
)
```

### Via Config File

```yaml
# nexus.toml
[nexus]
backend = "local"
agent_id = "alice"
tenant_id = "acme-corp"
enforce_permissions = true

[nexus.permissions]
default_owner = "root"
default_group = "root"
default_file_mode = "0o644"
default_dir_mode = "0o755"
```

## Roadmap

### Phase 1: UNIX Permissions ✅
- ✅ OperationContext dataclass
- ✅ PermissionEnforcer class
- ✅ UNIX permission checking
- ✅ Permission inheritance
- ✅ Comprehensive tests

### Phase 2: ACL & ReBAC
- ⏳ ACL entry checking
- ⏳ ReBAC graph traversal
- ⏳ Integration with operations
- ⏳ Audit logging

### Phase 3: Advanced Features
- ⏳ Permission policies
- ⏳ Role-based access control (RBAC)
- ⏳ Time-based permissions
- ⏳ Conditional access

## Troubleshooting

### Permission Denied Errors

```python
# Enable debug logging
import logging
logging.basicConfig(level=logging.DEBUG)

# Check file permissions
meta = nx.metadata.get("/file.txt")
print(f"Owner: {meta.owner}, Group: {meta.group}, Mode: {oct(meta.mode)}")

# Check user context
print(f"User: {nx._default_context.user}")
print(f"Groups: {nx._default_context.groups}")
print(f"Is Admin: {nx._default_context.is_admin}")

# Bypass with admin context
admin_ctx = OperationContext(user="admin", groups=[], is_admin=True)
nx.read("/file.txt", context=admin_ctx)  # Bypasses checks
```

### Backward Compatibility

If you see unexpected permission errors after upgrading:

1. **Disable enforcement temporarily**:
   ```python
   nx = connect(backend_type="local", enforce_permissions=False)
   ```

2. **Set default permissions**:
   ```bash
   nexus setfacl user:$(whoami):rwx /workspace
   nexus chmod -R 755 /workspace
   ```

3. **Use admin context for migration**:
   ```python
   admin_ctx = OperationContext(user="admin", groups=[], is_admin=True)
   nx.write("/file.txt", b"data", context=admin_ctx)
   ```

## See Also

- [UNIX Permissions Implementation](./PERMISSIONS_IMPLEMENTATION.md)
- [ACL Guide](./ACL_GUIDE.md) (coming soon)
- [ReBAC Guide](./REBAC_GUIDE.md) (coming soon)
- [Security Best Practices](./SECURITY.md)

## Contributing

To add permission checks to a new operation:

```python
def my_operation(self, path: str, context: OperationContext | None = None) -> Any:
    """My operation with permission checking.

    Args:
        path: Virtual file path
        context: Optional operation context (defaults to self._default_context)
    """
    path = self._validate_path(path)

    # Check permission
    self._check_permission(path, Permission.WRITE, context)

    # Perform operation
    # ...
```

## License

Apache 2.0 - See LICENSE file for details.
