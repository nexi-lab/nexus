# ReBAC Roles and Relations in Nexus

**Date:** 2025-10-24
**Status:** Complete Documentation

---

## Overview

Nexus uses a **pure ReBAC** (Relationship-Based Access Control) system based on Google Zanzibar. Instead of traditional "roles," we use **relations** between subjects and objects to define permissions.

---

## Core Concepts

### Relations vs Roles

In traditional systems:
- **Role**: User has role "Editor" globally
- **Permission**: Role grants certain permissions everywhere

In Nexus ReBAC:
- **Relation**: User has "editor" relation to specific file
- **Permission**: Computed from relation graph per request
- **Flexible**: Same user can be editor of one file, viewer of another

---

## File Relations (for file/workspace objects)

### 1. **Owner** (`owner`)

**What it means:**
- Full control over the file/workspace
- Can read, write, delete, and share
- Can grant permissions to others
- Highest level of access

**How it's computed:**
```python
owner = direct_owner ∪ parent_owner
```

**Breakdown:**
- `direct_owner`: Explicitly granted owner relation
- `parent_owner`: Inherited from parent directory owner (via tupleToUserset)

**Example:**
```python
# Direct ownership
rebac_write(
    subject=("user", "alice"),
    relation="direct_owner",
    object=("file", "/workspace/alice/project.txt")
)

# Parent ownership (alice owns /workspace/alice, inherits to all files)
rebac_write(
    subject=("user", "alice"),
    relation="direct_owner",
    object=("workspace", "/workspace/alice")
)
rebac_write(
    subject=("workspace", "/workspace/alice"),
    relation="parent",
    object=("file", "/workspace/alice/file.txt")
)
# Result: alice is "owner" of /workspace/alice/file.txt via parent_owner
```

**Capabilities:**
- ✅ Read files
- ✅ Write/update files
- ✅ Delete files
- ✅ Grant permissions to others
- ✅ All operations

---

### 2. **Editor** (`editor`)

**What it means:**
- Can read and modify files
- Cannot delete or change ownership
- Cannot grant permissions to others
- Read-write access

**How it's computed:**
```python
editor = owner ∪ direct_editor
```

**Breakdown:**
- Includes all `owner` permissions
- Plus `direct_editor` (explicitly granted editor relation)

**Example:**
```python
# Grant editor permission
rebac_write(
    subject=("user", "bob"),
    relation="direct_editor",
    object=("file", "/workspace/shared/doc.txt")
)

# Bob can now:
# - Read the file ✅
# - Write/update the file ✅
# - Delete the file ❌ (owner only)
```

**Capabilities:**
- ✅ Read files
- ✅ Write/update files
- ❌ Delete files (owner only in current config)
- ❌ Grant permissions (owner only)

---

### 3. **Viewer** (`viewer`)

**What it means:**
- Read-only access
- Cannot modify, delete, or share
- Safest sharing option

**How it's computed:**
```python
viewer = owner ∪ editor ∪ direct_viewer
```

**Breakdown:**
- Includes all `owner` and `editor` permissions
- Plus `direct_viewer` (explicitly granted viewer relation)

**Example:**
```python
# Grant viewer permission
rebac_write(
    subject=("user", "charlie"),
    relation="direct_viewer",
    object=("file", "/workspace/public/readme.md")
)

# Charlie can now:
# - Read the file ✅
# - Write/update the file ❌
# - Delete the file ❌
```

**Capabilities:**
- ✅ Read files
- ❌ Write/update files
- ❌ Delete files
- ❌ Grant permissions

---

## Group Relations (for group objects)

### 4. **Member** (`member`)

**What it means:**
- Basic group membership
- Can see group members
- Part of the group for permission checks

**Example:**
```python
# Add alice to engineering group
rebac_write(
    subject=("user", "alice"),
    relation="member",
    object=("group", "engineering")
)

# Grant group access to file
rebac_write(
    subject=("group", "engineering"),
    relation="direct_editor",
    object=("file", "/projects/backend.py")
)

# Result: alice can edit /projects/backend.py via group membership
```

**How it works:**
1. User is member of group
2. Group has relation to object
3. User inherits permissions via group

---

### 5. **Admin** (group admin, not system admin)

**What it means:**
- Can manage group membership
- Can add/remove members
- Can see all group permissions

**Example:**
```python
# Make bob admin of engineering group
rebac_write(
    subject=("user", "bob"),
    relation="admin",
    object=("group", "engineering")
)

# Bob can now manage the engineering group
```

**Note:** This is different from system admin (`is_admin` flag)

---

## Hierarchical Relations

### 6. **Parent** (`parent`)

**What it means:**
- Defines parent-child hierarchy
- Enables permission inheritance
- Used for folders → files

**Example:**
```python
# Define folder as parent of file
rebac_write(
    subject=("workspace", "/workspace/projects"),
    relation="parent",
    object=("file", "/workspace/projects/file.txt")
)

# Grant owner of folder
rebac_write(
    subject=("user", "alice"),
    relation="direct_owner",
    object=("workspace", "/workspace/projects")
)

# Result: alice is "owner" of /workspace/projects/file.txt via parent_owner
```

**Inheritance Chain:**
```
User alice → direct_owner → /workspace/projects (workspace)
                             ↓ parent relation
                           /workspace/projects/file.txt (file)
                             ↓ parent_owner computation
User alice → owner → /workspace/projects/file.txt ✅
```

---

## Permission Mapping

### How Permissions Map to Relations

| Permission | Allowed Relations | Use Case |
|------------|------------------|----------|
| `READ` | `viewer`, `editor`, `owner` | Read file content, list directories |
| `WRITE` | `editor`, `owner` | Create, update files |
| `DELETE` | `owner` | Delete files, remove directories |
| `EXECUTE` | `owner` | Execute files (future use) |

### Permission Check Flow

```python
# When checking: Can alice read /file.txt?
permission_check(
    subject=("user", "alice"),
    permission="read",
    object=("file", "/file.txt")
)

# ReBAC checks (in order):
# 1. Is alice "owner" of /file.txt? (direct_owner OR parent_owner)
# 2. Is alice "editor" of /file.txt? (direct_editor)
# 3. Is alice "viewer" of /file.txt? (direct_viewer)
# 4. If ANY match → Allow, else → Deny
```

---

## Relation Hierarchy Visualization

```
┌─────────────────────────────────────────────┐
│                   OWNER                     │
│  (Full Access: Read + Write + Delete)      │
│                                             │
│  • direct_owner (explicit)                 │
│  • parent_owner (inherited from folder)    │
└─────────────────┬───────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────┐
│                  EDITOR                     │
│         (Read + Write Access)               │
│                                             │
│  • Includes all OWNER perms                │
│  • direct_editor (explicit)                │
└─────────────────┬───────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────┐
│                 VIEWER                      │
│             (Read-Only Access)              │
│                                             │
│  • Includes all EDITOR perms               │
│  • direct_viewer (explicit)                │
└─────────────────────────────────────────────┘
```

**Key Point:** This is a **union**, not a hierarchy!
- Being a viewer doesn't "downgrade" an owner
- If you're owner, you also match editor and viewer checks
- Permission checks look for ANY matching relation

---

## Direct vs Computed Relations

### Direct Relations (Explicit)
Relations you explicitly grant:
- `direct_owner` - Explicit ownership
- `direct_editor` - Explicit edit permission
- `direct_viewer` - Explicit view permission
- `member` - Explicit group membership

### Computed Relations (Derived)
Relations computed from direct relations:
- `owner` = `direct_owner` ∪ `parent_owner`
- `editor` = `owner` ∪ `direct_editor`
- `viewer` = `owner` ∪ `editor` ∪ `direct_viewer`
- `parent_owner` = computed via tupleToUserset from parent

**When to use each:**

Use **direct relations** when:
- Granting permissions via API/CLI
- Creating new relationships
- Need explicit control

Use **computed relations** when:
- Checking permissions
- Querying who has access
- Want to include inherited permissions

---

## Common Use Cases

### Use Case 1: Personal Workspace

```python
# Alice owns her workspace
rebac_write(
    subject=("user", "alice"),
    relation="direct_owner",
    object=("workspace", "/workspace/alice")
)

# All files in /workspace/alice/* automatically inherit owner permission
# via parent_owner relation (if parent tuples are created)
```

### Use Case 2: Shared Document

```python
# Alice owns the document
rebac_write(
    subject=("user", "alice"),
    relation="direct_owner",
    object=("file", "/shared/roadmap.md")
)

# Bob can edit
rebac_write(
    subject=("user", "bob"),
    relation="direct_editor",
    object=("file", "/shared/roadmap.md")
)

# Charlie can view
rebac_write(
    subject=("user", "charlie"),
    relation="direct_viewer",
    object=("file", "/shared/roadmap.md")
)
```

### Use Case 3: Team Access via Groups

```python
# Create engineering team
rebac_write(
    subject=("user", "alice"),
    relation="member",
    object=("group", "engineering")
)
rebac_write(
    subject=("user", "bob"),
    relation="member",
    object=("group", "engineering")
)

# Grant team access to project folder
rebac_write(
    subject=("group", "engineering"),
    relation="direct_editor",
    object=("workspace", "/projects/backend")
)

# Result: Both alice and bob can edit files in /projects/backend
# via group membership
```

### Use Case 4: Temporary Access

```python
# Grant contractor temporary viewer access (expires in 7 days)
rebac_write(
    subject=("user", "contractor_123"),
    relation="direct_viewer",
    object=("file", "/docs/spec.pdf"),
    expires_at=datetime.now(UTC) + timedelta(days=7)
)

# After 7 days, permission automatically expires
```

---

## Differences Summary Table

| Relation | Read | Write | Delete | Share | Inherit | Grant Via |
|----------|------|-------|--------|-------|---------|-----------|
| **owner** | ✅ | ✅ | ✅ | ✅ | ✅ | `direct_owner` or parent |
| **editor** | ✅ | ✅ | ❌ | ❌ | ✅ | `direct_editor` |
| **viewer** | ✅ | ❌ | ❌ | ❌ | ✅ | `direct_viewer` |
| **member** | N/A | N/A | N/A | N/A | N/A | For groups only |
| **admin** | N/A | N/A | N/A | N/A | N/A | For groups only |
| **parent** | N/A | N/A | N/A | N/A | ✅ | For hierarchy |

**Legend:**
- ✅ = Allowed
- ❌ = Not allowed
- N/A = Not applicable (different object type)

---

## CLI Examples

### Grant Ownership
```bash
# Make alice owner of her workspace
nexus rebac create user alice direct_owner file /workspace/alice
```

### Grant Editor Permission
```bash
# Let bob edit the shared folder
nexus rebac create user bob direct_editor file /workspace/shared
```

### Grant Viewer Permission
```bash
# Let charlie view the public docs
nexus rebac create user charlie direct_viewer file /workspace/public
```

### Create Group and Add Members
```bash
# Create relationship: alice is member of engineering
nexus rebac create user alice member group engineering

# Grant group access
nexus rebac create group engineering direct_editor file /projects/backend
```

### List Who Has Access
```bash
# Find all subjects who can read a file
nexus rebac expand read file /workspace/secret.txt

# Result shows all users/groups/services with read permission
```

### Check Specific Permission
```bash
# Can alice read this file?
nexus rebac check user alice read file /workspace/file.txt

# Returns: true or false
```

---

## Python API Examples

### Grant Permissions
```python
from nexus import NexusFS

nx = NexusFS(backend)

# Grant owner
nx.rebac_create(
    subject=("user", "alice"),
    relation="direct_owner",
    object=("file", "/workspace/alice/project.txt")
)

# Grant editor
nx.rebac_create(
    subject=("user", "bob"),
    relation="direct_editor",
    object=("file", "/workspace/shared/doc.md")
)

# Grant viewer
nx.rebac_create(
    subject=("user", "charlie"),
    relation="direct_viewer",
    object=("file", "/workspace/public/readme.md")
)
```

### Check Permissions
```python
# Check if alice can read the file
can_read = nx.rebac_check(
    subject=("user", "alice"),
    permission="read",
    object=("file", "/workspace/secret.txt")
)

if can_read:
    content = nx.read("/workspace/secret.txt", subject=("user", "alice"))
```

### Find Who Has Access
```python
# Get all subjects who can write to a file
writers = nx.rebac_expand(
    permission="write",
    object=("file", "/workspace/shared/doc.txt")
)

for subject_type, subject_id in writers:
    print(f"{subject_type}:{subject_id} can write")
```

---

## Best Practices

### 1. Use Direct Relations for Granting
✅ **Do:**
```python
rebac_create(subject, "direct_owner", object)
rebac_create(subject, "direct_editor", object)
rebac_create(subject, "direct_viewer", object)
```

❌ **Don't:**
```python
rebac_create(subject, "owner", object)  # Use direct_owner
rebac_create(subject, "editor", object)  # Use direct_editor
```

### 2. Use Computed Relations for Checking
✅ **Do:**
```python
rebac_check(subject, "read", object)  # Maps to viewer/editor/owner
```

### 3. Use Groups for Team Access
✅ **Do:**
```python
# Add users to group
rebac_create(user, "member", group)

# Grant group access to resources
rebac_create(group, "direct_editor", file)
```

### 4. Use Hierarchy for Folders
✅ **Do:**
```python
# Define parent-child
rebac_create(folder, "parent", file)

# Grant on parent
rebac_create(user, "direct_owner", folder)

# User automatically gets owner on file via parent_owner
```

---

## Migration from Old Roles

If you used the old system with hardcoded roles:

| Old System | New ReBAC |
|------------|-----------|
| `is_admin=True` | System admin flag (unchanged) |
| `tenant_id="acme"` | Use groups: `member` of `("tenant", "acme")` |
| File owner | `direct_owner` relation |
| Shared with user | `direct_editor` or `direct_viewer` |
| Public file | Grant `direct_viewer` to special `("group", "public")` |

---

## Troubleshooting

### "Permission denied" but user should have access

1. **Check direct relations:**
```bash
nexus rebac list --subject user:alice
```

2. **Check computed relations (with expansion):**
```bash
nexus rebac check user alice read file /path/to/file
```

3. **Verify parent hierarchy:**
```bash
# Check if parent relations exist
nexus rebac list --object file:/path/to/file
```

### User can't see files they should access

- Permission checks happen on READ operations
- Files without any relations are denied by default
- Grant at least `direct_viewer` for read access

### Group access not working

1. Verify user is member: `nexus rebac list --subject user:alice`
2. Verify group has permission: `nexus rebac list --subject group:engineering`
3. Check both relationships exist

---

## Summary

**Current Relations in Nexus:**

1. **owner** - Full control (read + write + delete + share)
2. **editor** - Read-write access (no delete/share)
3. **viewer** - Read-only access
4. **member** - Group membership
5. **admin** - Group administration
6. **parent** - Hierarchical inheritance

**Key Differences:**
- **owner** has all permissions, can grant access
- **editor** can read and write, but not delete or share
- **viewer** can only read, no modifications
- All three are **unions** (inclusive), not exclusive roles

**Remember:**
- Use `direct_*` relations when granting
- Use computed relations when checking
- Relations are per-object, not global
- Same user can have different relations to different files

---
