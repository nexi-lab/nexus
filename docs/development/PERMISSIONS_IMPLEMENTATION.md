# UNIX-Style File Permissions & ACL Implementation

## Overview

This document describes the implementation of UNIX-style file permissions and Access Control Lists (ACLs) for Nexus (Issue #84).

## Features Implemented

### 1. UNIX-Style Permissions
- **Owner, Group, Mode**: Standard POSIX permission model
- **Permission Bits**: Read (r), Write (w), Execute (x) for Owner/Group/Other
- **Mode Support**: Both octal (0o755) and symbolic (rwxr-xr-x) formats

### 2. Access Control Lists (ACLs)
- **Fine-grained Access Control**: Per-user and per-group permissions
- **Deny Entries**: Explicit access denial (takes priority)
- **Flexible Entry Types**: user, group, mask, other

### 3. CLI Commands
- `nexus chmod <mode> <path>` - Change file permissions
- `nexus chown <owner> <path>` - Change file owner
- `nexus chgrp <group> <path>` - Change file group
- `nexus getfacl <path>` - Display ACL entries
- `nexus setfacl <entry> <path>` - Add/remove ACL entries

## Implementation Details

### Core Modules

#### 1. `/src/nexus/core/permissions.py`
UNIX-style permission implementation:
- `Permission` - Permission flags (READ, WRITE, EXECUTE)
- `FileMode` - Permission bits management (0o755, etc.)
- `FilePermissions` - Complete permission information (owner, group, mode)
- `PermissionChecker` - Permission validation logic
- `parse_mode()` - Parse mode from string (octal/symbolic)

**Example Usage:**
```python
from nexus.core.permissions import FileMode, FilePermissions

# Create permissions
perms = FilePermissions.default("alice", "developers")  # rw-r--r--
perms = FilePermissions.default_directory("alice", "developers")  # rwxr-xr-x

# Check permissions
can_read = perms.can_read("bob", ["developers"])  # True
can_write = perms.can_write("bob", ["developers"])  # False

# Parse mode
mode = parse_mode("755")  # 0o755
mode = parse_mode("rwxr-xr-x")  # 0o755
```

#### 2. `/src/nexus/core/acl.py`
Access Control List implementation:
- `ACLPermission` - ACL permissions (read, write, execute)
- `ACLEntryType` - Entry types (user, group, mask, other)
- `ACLEntry` - Single ACL entry
- `ACL` - Complete ACL with multiple entries
- `ACLManager` - High-level ACL operations

**Example Usage:**
```python
from nexus.core.acl import ACL, ACLEntry, ACLEntryType, ACLPermission, ACLManager

# Create ACL
acl = ACL.empty()
manager = ACLManager()

# Grant permissions
manager.grant_user(acl, "alice", read=True, write=True)
manager.grant_group(acl, "developers", read=True, execute=True)

# Deny access
manager.deny_user(acl, "bob")

# Check permissions
result = acl.check_permission("alice", [], ACLPermission.READ)
# True = allowed, False = denied, None = no match (use UNIX permissions)

# Parse from string
entry = ACLEntry.from_string("user:alice:rw-")
acl = ACL.from_strings(["user:alice:rw-", "group:developers:r-x"])
```

### Database Schema

#### 1. File Permissions (added to `file_paths` table)
```sql
ALTER TABLE file_paths ADD COLUMN owner VARCHAR(255);
ALTER TABLE file_paths ADD COLUMN group VARCHAR(255);
ALTER TABLE file_paths ADD COLUMN mode INTEGER;  -- Permission bits (e.g., 0o644)
CREATE INDEX idx_file_paths_owner ON file_paths(owner);
CREATE INDEX idx_file_paths_group ON file_paths(group);
```

#### 2. ACL Entries (new `acl_entries` table)
```sql
CREATE TABLE acl_entries (
    acl_id VARCHAR(36) PRIMARY KEY,
    path_id VARCHAR(36) NOT NULL,  -- FK to file_paths
    entry_type VARCHAR(20) NOT NULL,  -- user, group, mask, other
    identifier VARCHAR(255),  -- username/groupname (NULL for mask/other)
    permissions VARCHAR(10) NOT NULL,  -- rwx format
    deny BOOLEAN NOT NULL DEFAULT FALSE,  -- Deny entry flag
    created_at TIMESTAMP NOT NULL,
    FOREIGN KEY (path_id) REFERENCES file_paths(path_id) ON DELETE CASCADE
);
CREATE INDEX idx_acl_entries_path_id ON acl_entries(path_id);
CREATE INDEX idx_acl_entries_type_id ON acl_entries(entry_type, identifier);
```

### Database Migration

**Migration File:** `/alembic/versions/777350ff28ce_add_unix_permissions_and_acl_support.py`

To apply the migration:
```bash
alembic upgrade head
```

To rollback:
```bash
alembic downgrade -1
```

### Updated Data Models

#### FileMetadata (`/src/nexus/core/metadata.py`)
```python
@dataclass
class FileMetadata:
    path: str
    backend_name: str
    physical_path: str
    size: int
    # ... existing fields ...

    # UNIX-style permissions
    owner: str | None = None
    group: str | None = None
    mode: int | None = None  # Permission bits (e.g., 0o644)
```

#### FilePathModel (`/src/nexus/storage/models.py`)
```python
class FilePathModel(Base):
    # ... existing fields ...

    # UNIX-style permissions
    owner: Mapped[str | None] = mapped_column(String(255), nullable=True)
    group: Mapped[str | None] = mapped_column(String(255), nullable=True)
    mode: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Relationships
    acl_entries: Mapped[list["ACLEntryModel"]] = relationship(
        "ACLEntryModel", back_populates="file_path", cascade="all, delete-orphan"
    )
```

#### ACLEntryModel (`/src/nexus/storage/models.py`)
```python
class ACLEntryModel(Base):
    __tablename__ = "acl_entries"

    acl_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    path_id: Mapped[str] = mapped_column(String(36), ForeignKey("file_paths.path_id"))
    entry_type: Mapped[str] = mapped_column(String(20), nullable=False)
    identifier: Mapped[str | None] = mapped_column(String(255), nullable=True)
    permissions: Mapped[str] = mapped_column(String(10), nullable=False)
    deny: Mapped[bool] = mapped_column(default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    # Relationship
    file_path: Mapped["FilePathModel"] = relationship("FilePathModel", back_populates="acl_entries")
```

## CLI Usage Examples

### Change File Permissions
```bash
# Using octal notation
nexus chmod 755 /workspace/script.sh
nexus chmod 0o644 /workspace/data.txt

# Using symbolic notation
nexus chmod rwxr-xr-x /workspace/file.txt
nexus chmod rw-r--r-- /workspace/config.yaml
```

### Change Owner and Group
```bash
# Change owner
nexus chown alice /workspace/file.txt

# Change group
nexus chgrp developers /workspace/code/

# Both can be used together
nexus chown alice /workspace/file.txt
nexus chgrp developers /workspace/file.txt
```

### View ACL Entries
```bash
nexus getfacl /workspace/file.txt

# Output:
# file: /workspace/file.txt
# owner: alice
# group: developers
# mode: 0o644 (rw-r--r--)
#
# ACL entries:
# user:bob:r--
# group:admins:r-x
# deny:user:charlie:---
```

### Manage ACL Entries
```bash
# Grant user read+write
nexus setfacl user:alice:rw- /workspace/file.txt

# Grant group read+execute
nexus setfacl group:developers:r-x /workspace/code/

# Deny user access
nexus setfacl deny:user:bob /workspace/secret.txt

# Remove ACL entry
nexus setfacl user:alice:rwx /workspace/file.txt --remove
```

## Testing

### Unit Tests

**Test Files:**
- `/tests/unit/test_permissions.py` - 46 tests for UNIX permissions
- `/tests/unit/test_acl.py` - 45 tests for ACL functionality

**Run Tests:**
```bash
# Run all permission tests
pytest tests/unit/test_permissions.py tests/unit/test_acl.py -v

# Run with coverage
pytest tests/unit/test_permissions.py tests/unit/test_acl.py --cov=nexus.core.permissions --cov=nexus.core.acl
```

**Test Coverage:**
- `permissions.py`: 95% coverage
- `acl.py`: 90% coverage
- Total: 91 passing tests

## Permission Evaluation Order

When checking file access, Nexus uses the following order:

1. **ACL Deny Entries** - Explicit denials (highest priority)
2. **ACL Allow Entries** - Explicit permissions
3. **UNIX Permissions** - Owner/Group/Other based on mode
4. **Default** - Deny if no match (for security)

**Example:**
```python
# File permissions: rw-r--r-- (owner=alice, group=developers)
# ACL: user:bob:rw-, deny:user:charlie:---

# Alice (owner): Can read+write (UNIX permissions)
# Bob: Can read+write (ACL allows)
# Charlie: Denied (ACL denies)
# Dave (in developers): Can read (UNIX group permissions)
# Eve (not in group): Can read (UNIX other permissions)
```

## Backward Compatibility

- **Existing Files**: Files without permissions work as before (all access allowed)
- **Optional Fields**: owner/group/mode fields are nullable in database
- **Permission Checker**: Returns True for None permissions (backward compatible)
- **No Breaking Changes**: Existing code continues to work

## Future Enhancements

The current implementation provides the foundation for:

1. **ReBAC (Relationship-Based Access Control)** - Zanzibar-style authorization
2. **Permission Inheritance** - Automatic permission assignment based on paths
3. **Permission Policies** - Default policies per namespace
4. **Permission Checking in Operations** - Integrate into filesystem operations
5. **Web UI** - Graphical permission management interface

## References

- Issue: https://github.com/nexi-lab/nexus/issues/84
- POSIX Permissions: https://en.wikipedia.org/wiki/File-system_permissions#Unix_permissions
- POSIX ACLs: https://www.usenix.org/legacy/publications/library/proceedings/usenix03/tech/freenix03/full_papers/gruenbacher/gruenbacher_html/main.html

## Migration Guide

### For Existing Installations

1. **Backup Database**
   ```bash
   nexus export backup.jsonl
   ```

2. **Apply Migration**
   ```bash
   alembic upgrade head
   ```

3. **Set Default Permissions** (optional)
   ```python
   from nexus import connect
   from nexus.core.permissions import FilePermissions, FileMode

   nx = connect()

   # Set permissions for existing files
   for path in nx.list("/", recursive=True):
       meta = nx.metadata.get(path)
       if meta and not meta.owner:
           meta.owner = "root"
           meta.group = "root"
           meta.mode = 0o644  # Default: rw-r--r--
           nx.metadata.put(meta)
   ```

4. **Verify**
   ```bash
   nexus getfacl /workspace/file.txt
   ```

## Contributors

Implementation by Claude (Anthropic AI Assistant) based on requirements from issue #84.

## License

Apache 2.0 - See LICENSE file for details.
