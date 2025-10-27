# FUSE Permission Enforcement with ReBAC

## Overview

As of this update, **FUSE mounts now properly enforce ReBAC permissions** on all file operations. This means that commands like `grep`, `cat`, `ls` running through FUSE will respect the permission tuples defined in your Nexus instance.

## What Changed

### Before
FUSE operations used the **default context** of the NexusFS instance, meaning:
- All users accessing the FUSE mount had the same permissions
- Individual user permissions from ReBAC tuples were ignored
- Permission checks happened, but always against the wrong user

### After
FUSE operations now support **two permission modes**:

1. **Per-Mount Context** (Default): All operations use a single user identity
2. **UID Mapping** (Advanced): Maps OS user IDs to different Nexus identities

## How It Works

### 1. Per-Mount Context (Simple)

Each FUSE mount is associated with a specific Nexus user identity. All file operations performed through that mount are subject to that user's permissions.

```python
from nexus import connect
from nexus.fuse import mount_nexus
from nexus.core.permissions import OperationContext

# Connect to Nexus
nx = connect(config={"data_dir": "./nexus-data"})

# Create context for user "alice" in tenant "org_acme"
alice_ctx = OperationContext(
    subject_type="user",
    subject_id="alice",
    groups=["developers"],
    tenant_id="org_acme"
)

# Mount with Alice's context - all operations will be as Alice
fuse = mount_nexus(
    nx,
    "/mnt/nexus-alice",
    default_context=alice_ctx,
    foreground=False
)

# Now:
# cat /mnt/nexus-alice/workspace/file.txt  → checks Alice's READ permission
# echo "test" > /mnt/nexus-alice/data.txt  → checks Alice's WRITE permission
```

**Use this when:**
- Single user needs FUSE access
- You want to mount different directories with different permissions
- Simple security model is sufficient

### 2. UID Mapping (Multi-User)

For true multi-user FUSE filesystems, you can map OS user IDs to Nexus identities.

```python
from nexus import connect
from nexus.fuse import mount_nexus
from nexus.core.permissions import OperationContext

# Connect to Nexus
nx = connect(config={"data_dir": "./nexus-data"})

# Create mapping: OS UID → Nexus OperationContext
uid_mapping = {
    1000: OperationContext(  # Map UID 1000 (alice on the system)
        subject_type="user",
        subject_id="alice",
        groups=["developers"],
        tenant_id="org_acme"
    ),
    1001: OperationContext(  # Map UID 1001 (bob on the system)
        subject_type="user",
        subject_id="bob",
        groups=["qa"],
        tenant_id="org_acme"
    ),
}

# Mount with UID mapping
fuse = mount_nexus(
    nx,
    "/mnt/nexus-shared",
    uid_mapping=uid_mapping,
    foreground=False
)

# Now:
# When Alice (UID 1000) runs: cat /mnt/nexus-shared/file.txt
#   → Checks permissions for user:alice
#
# When Bob (UID 1001) runs: cat /mnt/nexus-shared/file.txt
#   → Checks permissions for user:bob
```

**Use this when:**
- Multiple OS users need access to the same mount
- Different users should have different permissions
- You want true multi-user filesystem behavior

**To find your UID:**
```bash
id -u        # Your UID
id -u alice  # Alice's UID
```

## Permission Enforcement Details

### Operations That Are Checked

**ALL file operations now enforce ReBAC permissions!**

| FUSE Operation | Shell Commands | Permission Required | Enforced |
|----------------|----------------|-------------------|----------|
| `read()` | cat, grep, less, head, tail | READ | ✅ |
| `write()` | echo >, vim, nano, sed -i | WRITE | ✅ |
| `create()` | touch, > newfile | WRITE | ✅ |
| `truncate()` | > existing_file | WRITE | ✅ |
| `unlink()` | rm, rm -f | WRITE | ✅ |
| `delete()` | (internal) | WRITE | ✅ |
| `mkdir()` | mkdir, mkdir -p | WRITE on parent | ✅ |
| `rmdir()` | rmdir, rm -r | WRITE on parent | ✅ |
| `rename()` | mv, mv -f | WRITE on both paths | ✅ |
| `chmod()` | chmod | WRITE | ✅ |
| `chown()` | chown | WRITE | ✅ |

**How it works:**
- Every operation retrieves the calling user's context via `_get_operation_context()`
- Context is passed to the corresponding NexusFS method
- NexusFS checks ReBAC tuples for the user/file combination
- If permission denied, FUSE returns `EACCES` error

**Directory listing filtering:**
- `readdir()` (ls) checks READ permission for each file
- Files without permission are completely hidden from the listing
- Each user sees a different filtered view of the same directory

**Metadata access control:**
- `getattr()` (stat) checks READ permission before returning file attributes
- File size, timestamps, and other metadata are protected
- Prevents information leakage about files you can't read

### Error Handling

When permission is denied:
- FUSE returns `EACCES` (Permission denied) error
- The operation fails with standard POSIX error message
- Error is logged with details about the denial

Example:
```bash
$ cat /mnt/nexus/private/secret.txt
cat: /mnt/nexus/private/secret.txt: Permission denied
```

## Migration Guide

### If you're using FUSE already:

**No changes required for basic usage**, but permissions are now enforced!

**Before:**
```python
# This worked regardless of permissions
fuse = mount_nexus(nx, "/mnt/nexus")
```

**After:**
```python
# Still works, but now uses nexus_fs.default_context
# If you want explicit control:
fuse = mount_nexus(
    nx,
    "/mnt/nexus",
    default_context=OperationContext(
        subject_type="user",
        subject_id="admin",
        is_admin=True  # Admin bypass if needed
    )
)
```

### Setting Up Multi-Tenant FUSE

```python
# Tenant-isolated mount for organization "acme"
acme_ctx = OperationContext(
    subject_type="user",
    subject_id="acme_admin",
    tenant_id="org_acme"
)

fuse = mount_nexus(
    nx,
    "/mnt/nexus-acme",
    default_context=acme_ctx
)

# Users will only see files in tenant "org_acme"
# Cross-tenant access is blocked
```

## Security Considerations

### 1. Mount Point Security

The FUSE mount point permissions matter:
```bash
# Create mount point with restricted access
sudo mkdir /mnt/nexus-alice
sudo chown alice:alice /mnt/nexus-alice
sudo chmod 700 /mnt/nexus-alice  # Only alice can access
```

### 2. Admin Bypass

By default, admin bypass is **disabled** for security. If needed:
```python
nx = connect(
    config={"data_dir": "./data"},
    context=OperationContext(
        subject_type="user",
        subject_id="admin",
        is_admin=True
    ),
    allow_admin_bypass=True  # Enable admin bypass
)
```

### 3. System Operations

Operations with `is_system=True` bypass all permission checks:
```python
# Use with extreme caution!
system_ctx = OperationContext(
    subject_type="service",
    subject_id="backup_service",
    is_system=True  # Bypasses ALL permission checks
)
```

## Advanced Examples

### Example 1: Development Team Setup

```python
# Development team mount with different user permissions
dev_mapping = {
    1000: OperationContext(
        subject_type="user",
        subject_id="senior_dev",
        groups=["developers", "reviewers"],
        tenant_id="org_dev"
    ),
    1001: OperationContext(
        subject_type="user",
        subject_id="junior_dev",
        groups=["developers"],
        tenant_id="org_dev"
    ),
}

fuse = mount_nexus(
    nx,
    "/mnt/team-workspace",
    uid_mapping=dev_mapping,
    allow_other=True  # Allow all users to access (if permitted)
)

# Then set up permissions:
# Give developers READ on /workspace/*
nx.rebac.create_tuple(
    object_type="file",
    object_id="/workspace/*",
    relation="reader",
    subject_type="group",
    subject_id="developers"
)

# Give reviewers WRITE on /reviews/*
nx.rebac.create_tuple(
    object_type="file",
    object_id="/reviews/*",
    relation="writer",
    subject_type="group",
    subject_id="reviewers"
)
```

### Example 2: AI Agent Workspace

```python
# Mount for an AI agent with limited permissions
agent_ctx = OperationContext(
    subject_type="agent",
    subject_id="claude_001",
    groups=["ai_agents"],
    tenant_id="user_alice"
)

fuse = mount_nexus(
    nx,
    "/mnt/agent-workspace",
    default_context=agent_ctx
)

# Restrict agent to workspace namespace only
nx.rebac.create_tuple(
    object_type="file",
    object_id="/workspace/*",
    relation="writer",
    subject_type="agent",
    subject_id="claude_001"
)
```

## Debugging Permission Issues

### Enable Debug Logging

```python
import logging
logging.basicConfig(level=logging.DEBUG)

# FUSE operations will log permission checks
fuse = mount_nexus(nx, "/mnt/nexus", debug=True)
```

### Check Effective Context

```python
# In the FUSE operations code, you can check what context is being used:
# Look for log messages like:
# DEBUG:nexus.fuse.operations:Using mapped context for UID 1000
# WARNING:nexus.fuse.operations:No mapping found for UID 1002, using default context
```

### Verify ReBAC Tuples

```python
# Check what permissions a user has
tuples = nx.rebac.list_tuples(
    subject_type="user",
    subject_id="alice"
)
for t in tuples:
    print(f"{t['object_id']} - {t['relation']}")
```

## Current Status: FULLY ENFORCED ✅

All permission checks are now implemented:

1. ✅ **Directory listing is filtered by permissions**
   - `ls /mnt/nexus` only shows files you can read
   - Files without READ permission are completely hidden
   - Each user sees a personalized view

2. ✅ **Metadata access checks permissions**
   - `stat /mnt/nexus/file.txt` requires READ permission
   - File sizes and timestamps are protected
   - Prevents information leakage

3. ⚠️ **Minor limitation: Inheritance visualization**
   - Parent directory permissions are enforced but not shown via `ls -l`
   - This is cosmetic only - enforcement is correct

**No known security gaps in FUSE permission enforcement!**

## Related Documentation

- [Permission System Overview](PERMISSION_SYSTEM.md)
- [ReBAC Integration](REBAC_INTEGRATION_COMPLETE.md)
- [Multi-Tenant Architecture](multi-tenant-architecture.md)
- [P0 Security Implementation](P0_SECURITY_IMPLEMENTATION_COMPLETE.md)
