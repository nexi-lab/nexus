# Permissions & Access Control

**Complete guide to Nexus permissions using ReBAC (Relationship-Based Access Control)**

---

## Table of Contents

1. [Overview](#overview)
2. [Quick Start](#quick-start)
3. [Core Concepts](#core-concepts)
4. [Permission Model](#permission-model)
5. [API Patterns](#api-patterns)
6. [Namespace Design](#namespace-design)
7. [Multi-Tenant Isolation](#multi-tenant-isolation)
8. [Remote Server Usage](#remote-server-usage)
9. [Common Patterns](#common-patterns)
10. [Best Practices](#best-practices)
11. [Troubleshooting](#troubleshooting)

---

## Overview


Nexus uses a **pure ReBAC** (Relationship-Based Access Control) permission system based on Google's Zanzibar design. This provides fine-grained, flexible, and scalable authorization for multi-tenant AI file systems.

### Key Features

- ✅ **Pure ReBAC**: All permissions defined as relationships between subjects and objects
- ✅ **Subject-Based**: Identity specified per-operation, not per-instance
- ✅ **Multi-Tenant**: Complete isolation between tenants
- ✅ **Flexible**: Supports users, agents, groups, services, and custom entity types
- ✅ **Scalable**: Graph-based permission checking with caching
- ✅ **Standard**: Industry-proven Zanzibar model

All permissions are **explicitly managed** through ReBAC relationships.

---


## Quick Start


## Core Concepts


### Core Concepts

#### Subject (Who)
The entity requesting access:
- **Type**: `user`, `agent`, `service`, `group`, etc.
- **ID**: Unique identifier

```python
subject = ("user", "alice")
subject = ("agent", "claude_001")
subject = ("service", "bootstrap")
```

#### Object (What)
The resource being accessed:
- **Type**: `file`, `memory`, `workspace`, etc.
- **ID**: Path or unique identifier

```python
object = ("file", "/workspace/doc.txt")
object = ("memory", "mem_12345")
object = ("workspace", "ws_sales")
```

#### Relation (How)
The relationship type between subject and object:
- **Direct Relations**: `direct_owner`, `direct_editor`, `direct_viewer`
- **Computed Relations**: `owner`, `editor`, `viewer` (unions of direct + indirect)
- **Permissions**: `read`, `write`, `execute` (maps to relations via namespace config)

```python
relation = "direct_editor"  # Alice is a direct editor
relation = "editor"         # Includes direct + inherited via groups
permission = "write"        # Maps to "editor" or "owner" via namespace
```

### Permission Hierarchy

```
owner (full access)
  └── write (includes read)
       └── read (view only)

Relations:
- owner = direct_owner ∪ parent_owner
- editor = direct_editor ∪ owner
- viewer = direct_viewer ∪ editor
```

### Permission Mapping

| Permission | ReBAC Relations | Capabilities |
|------------|----------------|--------------|
| `READ` | `viewer`, `editor`, `owner` | Read file content, list directories |
| `WRITE` | `editor`, `owner` | Create, update, delete files |
| `EXECUTE` | `owner` | Execute files (currently owner-only) |

---


## ReBAC (Relationship-Based Access Control)

### Zanzibar Model

Nexus implements Google Zanzibar's authorization model:

1. **Tuples**: (subject, relation, object) relationships
2. **Namespaces**: Permission expansion rules per object type
3. **Check API**: Fast permission verification with graph traversal
4. **Expand API**: Find all subjects with a permission

### Namespace Configuration

Defines how permissions expand for an object type:

```python
DEFAULT_FILE_NAMESPACE = {
    "object_type": "file",
    "relations": {
        # Structural relation: parent directory
        "parent": {},

        # Direct relations (granted explicitly)
        "direct_owner": {},
        "direct_editor": {},
        "direct_viewer": {},

        # Parent inheritance via tupleToUserset
        "parent_owner": {
            "tupleToUserset": {
                "tupleset": "parent",
                "computedUserset": "owner"
            }
        },
        "parent_editor": {
            "tupleToUserset": {
                "tupleset": "parent",
                "computedUserset": "editor"
            }
        },
        "parent_viewer": {
            "tupleToUserset": {
                "tupleset": "parent",
                "computedUserset": "viewer"
            }
        },

        # Computed relations (union of direct + parent inheritance)
        "owner": {"union": ["direct_owner", "parent_owner"]},
        "editor": {"union": ["direct_editor", "parent_editor", "owner"]},
        "viewer": {"union": ["direct_viewer", "parent_viewer"]},
    },

    # Explicit permission-to-userset mapping (Zanzibar-style)
    # Prevents ambiguous check("write") bugs by defining exact semantics
    "permissions": {
        "read": ["viewer", "editor", "owner"],    # Read = viewer OR editor OR owner
        "write": ["editor", "owner"],              # Write = editor OR owner (NOT viewer)
        "execute": ["owner"],                      # Execute = owner only
    }
}
```

**Key Insight:** The `permissions` mapping explicitly defines which relations grant which permissions. This eliminates ambiguity and follows Google Zanzibar's userset semantics.

### Relationship Types

#### 1. Direct Relationships
Explicitly created tuples:

```python
# Alice owns /workspace/doc.txt
nx.rebac_create(
    subject=("user", "alice"),
    relation="direct_owner",
    object=("file", "/workspace/doc.txt"),
)
```

#### 2. Union Relationships
Logical OR of multiple relations:

```python
# "editor" = "owner" OR "direct_editor"
# If alice is owner OR direct_editor, she has editor permission
```

#### 3. Indirect Relationships (tupleToUserset)
Permission via another object:

```python
# "parent_owner" = "if you're owner of the parent, you're owner of this"
# If alice owns /workspace, she owns /workspace/doc.txt
```

### Graph Traversal

Permission checking traverses the relationship graph:

```
Check: Can alice WRITE to /workspace/sales/doc.txt?

1. Direct check: alice --write--> /workspace/sales/doc.txt? NO
2. Union expansion: write = editor ∪ owner
3. Check editor:
   a. alice --direct_editor--> file? NO
   b. alice --owner--> file? → expand owner
4. Check owner:
   a. alice --direct_owner--> file? NO
   b. alice --parent_owner--> file?
      i. Find parent: file --parent--> /workspace/sales
      ii. alice --owner--> /workspace/sales? YES ✓
5. Result: ALLOW
```

---


## API Patterns

### Basic Operations

## Namespace Design


### Real-World Examples

### Example 1: GitHub-style Repository Permissions

```python
repo_namespace = NamespaceConfig(
    namespace_id="repo-001",
    object_type="repository",
    config={
        "relations": {
            # Direct relations
            "direct_admin": {},
            "direct_write": {},
            "direct_read": {},
            "direct_triage": {},

            # Admin (full control)
            "admin": {"union": ["direct_admin"]},

            # Write (push access + admin)
            "write": {"union": ["direct_write", "admin"]},

            # Triage (manage issues + write)
            "triage": {"union": ["direct_triage", "write"]},

            # Read (view + all above)
            "read": {"union": ["direct_read", "triage"]}
        },
        "permissions": {
            "view": ["read"],
            "comment": ["read"],
            "create_issue": ["read"],
            "push": ["write"],
            "manage_issues": ["triage"],
            "manage_settings": ["admin"],
            "delete": ["admin"]
        }
    }
)
```

### Example 2: Google Drive-style Sharing

```python
drive_namespace = NamespaceConfig(
    namespace_id="drive-001",
    object_type="drive-file",
    config={
        "relations": {
            # Structural
            "parent": {},

            # Direct
            "direct_owner": {},
            "direct_editor": {},
            "direct_commenter": {},
            "direct_viewer": {},

            # Inherited from parent
            "parent_owner": {
                "tupleToUserset": {
                    "tupleset": "parent",
                    "computedUserset": "owner"
                }
            },
            "parent_editor": {
                "tupleToUserset": {
                    "tupleset": "parent",
                    "computedUserset": "editor"
                }
            },
            "parent_viewer": {
                "tupleToUserset": {
                    "tupleset": "parent",
                    "computedUserset": "viewer"
                }
            },

            # Computed
            "owner": {"union": ["direct_owner", "parent_owner"]},
            "editor": {"union": ["direct_editor", "parent_editor", "owner"]},
            "commenter": {"union": ["direct_commenter", "editor"]},
            "viewer": {"union": ["direct_viewer", "parent_viewer", "commenter"]}
        },
        "permissions": {
            "view": ["viewer"],
            "comment": ["commenter"],
            "edit": ["editor"],
            "share": ["owner"],
            "delete": ["owner"]
        }
    }
)
```

### Example 3: Slack-style Channel Permissions

```python
channel_namespace = NamespaceConfig(
    namespace_id="channel-001",
    object_type="channel",
    config={
        "relations": {
            "workspace_member": {},  # Member of workspace
            "channel_member": {},    # Explicitly in channel
            "channel_admin": {},     # Channel admin
            "workspace_admin": {},   # Workspace admin

            # Admins (channel or workspace)
            "admin": {"union": ["channel_admin", "workspace_admin"]},

            # Members (must be in channel AND workspace)
            "member": {
                "intersection": ["channel_member", "workspace_member"]
            },

            # Can post (members or admins)
            "poster": {"union": ["member", "admin"]}
        },
        "permissions": {
            "read": ["member"],
            "post": ["poster"],
            "manage": ["admin"],
            "archive": ["admin"]
        }
    }
)
```

## Permission Lattice Pattern

A common pattern is to create a **permission lattice** where higher privileges include lower ones:

```
        owner (full control)
          ⇓
        editor (read + write)
          ⇓
        viewer (read only)
```

**Implementation:**

```python
"viewer": {"union": ["direct_viewer"]},
"editor": {"union": ["direct_editor", "viewer"]},  # editor includes viewer
"owner": {"union": ["direct_owner", "editor"]}      # owner includes editor
```

**Result:**
- Owner can do everything (owner + editor + viewer)
- Editor can edit and view (editor + viewer)
- Viewer can only view (viewer)


## Multi-Tenant Isolation

### Tenant ID

Every operation includes a **tenant_id** for data isolation:

```python
# Alice in org_acme
nx.read("/workspace/doc.txt", subject=("user", "alice"), tenant_id="org_acme")

# Bob in org_techcorp (different tenant)
nx.read("/workspace/doc.txt", subject=("user", "bob"), tenant_id="org_techcorp")
```

### Isolation Mechanisms

#### 1. Database-Level
Metadata filtered by `tenant_id`:

```sql
SELECT * FROM file_paths
WHERE tenant_id = 'org_acme' AND path = '/workspace/doc.txt'
```

#### 2. Permission-Level (ReBAC)
ReBAC tuples include tenant_id for isolation:

```python
# Tenant ID validation at write-time
# Create permission tuple with tenant isolation
nx.rebac_create(
    subject=("user", "alice"),
    relation="direct_editor",
    object=("file", "/workspace/doc.txt"),
    # Tenant ID is now stored in the tuple
)

# Cross-tenant relationships are REJECTED at write-time
try:
    # This will FAIL - alice from org_acme cannot access org_techcorp files
    nx.rebac_create(
        subject=("user", "alice"),  # org_acme user
        relation="viewer",
        object=("file", "/workspace/doc.txt"),  # org_techcorp file
        # Cross-tenant tuple rejected!
    )
except ValueError as e:
    print(f"❌ {e}")  # "Cross-tenant relationship not allowed"
```

**Security:** Write-time validation prevents cross-tenant permission leaks (P0 fix).

#### 3. Storage-Level
Physical storage organized by tenant:

```
/data/
├── org_acme/
│   └── workspace/
│       └── doc.txt
└── org_techcorp/
    └── workspace/
        └── doc.txt  # Different file!
```

### Cross-Tenant Access

By default, **cross-tenant access is prohibited**:

```python
# Alice (org_acme) tries to access org_techcorp file
try:
    nx.read(
        "/workspace/doc.txt",
        subject=("user", "alice"),
        tenant_id="org_techcorp"  # Different tenant!
    )
except PermissionError:
    print("❌ Cross-tenant access denied")
```

---


## Remote Server Usage


## Common Patterns

### Pattern 1: Organization Setup

```python
# 1. Create organization structure
org_id = "org_acme"
nx.mkdir(f"/orgs/{org_id}", subject=("service", "setup"), is_admin=True)
nx.mkdir(f"/orgs/{org_id}/workspaces", subject=("service", "setup"), is_admin=True)

# 2. Create workspace
workspace_id = "sales"
workspace_path = f"/orgs/{org_id}/workspaces/{workspace_id}"
nx.mkdir(workspace_path, subject=("service", "setup"), is_admin=True)

# 3. Grant admin access
nx.rebac_create(
    subject=("user", "org_admin"),
    relation="direct_owner",
    object=("file", workspace_path),
)

# 4. Grant team access
nx.rebac_create(
    subject=("group", "sales_team"),
    relation="direct_editor",
    object=("file", workspace_path),
)
```

### Pattern 2: Workspace Sharing

```python
# Share workspace with another user
def share_workspace(nx, workspace_path, user_id, role="viewer"):
    """Share workspace with user (viewer or editor)."""
    # Grant all files in workspace
    for file_path in nx.glob(f"{workspace_path}/**/*"):
        nx.rebac_create(
            subject=("user", user_id),
            relation=f"direct_{role}",  # direct_viewer or direct_editor
            object=("file", file_path),
        )

    print(f"✅ Shared {workspace_path} with {user_id} as {role}")

# Usage
share_workspace(nx, "/orgs/acme/workspaces/sales", "bob", role="editor")
```

### Pattern 3: Hierarchical Permissions

```python
# Parent-child permission inheritance (requires parent tuples)
def setup_directory_permissions(nx, parent_dir, child_file):
    """Set up parent-child relationship for permission inheritance."""
    # Create parent relationship tuple
    nx.rebac_create(
        subject=("file", child_file),
        relation="parent",
        object=("file", parent_dir),
    )

    # Now if alice owns parent_dir, she automatically owns child_file
    # (via parent_owner relation in namespace config)
```

### Pattern 4: Role-Based Access

```python
# Define roles with permission templates
ROLES = {
    "owner": "direct_owner",
    "admin": "direct_owner",
    "editor": "direct_editor",
    "viewer": "direct_viewer",
    "guest": "direct_viewer",
}

def grant_role(nx, user_id, resource_path, role):
    """Grant role-based access to resource."""
    relation = ROLES.get(role)
    if not relation:
        raise ValueError(f"Unknown role: {role}")

    nx.rebac_create(
        subject=("user", user_id),
        relation=relation,
        object=("file", resource_path),
    )
```

### Pattern 5: Audit Trail

```python
# Track permission changes via changelog
from datetime import datetime, UTC

def audit_permissions(nx, object_path):
    """Get permission change history for a file."""
    # Query rebac_changelog table
    conn = nx.rebac_manager._get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT change_type, subject_type, subject_id, relation, created_at
        FROM rebac_changelog
        WHERE object_type = 'file' AND object_id = ?
        ORDER BY created_at DESC
    """, (object_path,))

    changes = cursor.fetchall()
    for change in changes:
        print(f"{change['created_at']}: {change['change_type']} - "
              f"{change['subject_type']}:{change['subject_id']} "
              f"{change['relation']}")
```

---


## Security Best Practices

### 1. Principle of Least Privilege

Grant minimum necessary permissions:

```python
# ✅ Good: Read-only access
nx.rebac_create(
    subject=("user", "viewer"),
    relation="direct_viewer",  # Not editor or owner
    object=("file", "/data/report.pdf"),
)

# ❌ Bad: Unnecessary owner access
nx.rebac_create(
    subject=("user", "viewer"),
    relation="direct_owner",  # Too much!
    object=("file", "/data/report.pdf"),
)
```

### 2. Use Groups for Team Access

Avoid per-user grants for large teams:

```python
# ✅ Good: Use groups
nx.rebac_create(("group", "sales_team"), "direct_editor", ("file", "/sales/data"))
nx.rebac_create(("user", "alice"), "member", ("group", "sales_team"))
nx.rebac_create(("user", "bob"), "member", ("group", "sales_team"))

# ❌ Bad: Individual grants (hard to manage at scale)
nx.rebac_create(("user", "alice"), "direct_editor", ("file", "/sales/data"))
nx.rebac_create(("user", "bob"), "direct_editor", ("file", "/sales/data"))
# ... 50 more users
```

### 3. Temporary Access with Expiry

Use `expires_at` for short-term access:

```python
from datetime import datetime, timedelta, UTC

# Guest access for 24 hours
nx.rebac_create(
    subject=("user", "guest"),
    relation="direct_viewer",
    object=("file", "/shared/demo.pdf"),
    expires_at=datetime.now(UTC) + timedelta(hours=24),
)
```

### 4. Audit Permission Changes

Monitor the changelog:

```python
# Regular audit of permission changes
conn = nx.rebac_manager._get_connection()
cursor = conn.cursor()

cursor.execute("""
    SELECT * FROM rebac_changelog
    WHERE created_at > NOW() - INTERVAL '1 day'
    ORDER BY created_at DESC
""")

for change in cursor.fetchall():
    print(f"⚠️  {change['change_type']}: {change['subject_id']} "
          f"{change['relation']} {change['object_id']}")
```

### 5. Separate Admin Operations

Use dedicated admin subjects for setup:

```python
# ✅ Good: Separate bootstrap subject
BOOTSTRAP_SUBJECT = ("service", "bootstrap")
nx.write("/system/config", data, subject=BOOTSTRAP_SUBJECT, is_admin=True)

# Then grant specific access
nx.rebac_create(("user", "admin"), "owner", ("file", "/system/config"))

# ❌ Bad: Using user as admin everywhere
nx.write("/file.txt", data, subject=("user", "alice"), is_admin=True)
```

### 6. Validate Subject Sources

Always verify subject authenticity:

```python
# ✅ Good: Server extracts from authenticated token
auth_result = await auth.authenticate(api_key)
subject = (auth_result.subject_type, auth_result.subject_id)

# ❌ Bad: Trust client-provided subject without verification
subject = request.json["subject"]  # NEVER DO THIS!
```

### 7. Tenant Isolation

Always specify tenant_id for multi-tenant operations:

```python
# ✅ Good: Explicit tenant isolation
nx.read("/data/file.txt", subject=("user", "alice"), tenant_id="org_acme")

# ❌ Bad: Missing tenant_id (may leak across tenants)
nx.read("/data/file.txt", subject=("user", "alice"))
```

---



### Problem: "PermissionError: Access denied"

**Cause**: Subject doesn't have required ReBAC relationship.

**Solution**:
```python
# 1. Check who has access
subjects = nx.rebac_expand(permission="read", object=("file", "/path"))
print(f"Current access: {subjects}")

# 2. Grant access
nx.rebac_create(
    subject=("user", "alice"),
    relation="direct_viewer",  # For read access
    object=("file", "/path"),
)

# 3. Verify
has_access = nx.rebac_check(("user", "alice"), "read", ("file", "/path"))
print(f"Alice can now read: {has_access}")
```

### Problem: "Directory permissions don't work"

**Cause**: Directory-level permissions with child inheritance not yet implemented.

**Solution**: Grant per-file or use helper:
```python
def grant_directory_access(nx, user, directory, role):
    for path in nx.glob(f"{directory}/**/*"):
        nx.rebac_create(
            subject=("user", user),
            relation=f"direct_{role}",
            object=("file", path),
        )

grant_directory_access(nx, "alice", "/workspace/sales", "editor")
```

### Problem: "Group permissions not working"

**Cause**: User not added to group, or namespace doesn't support groups.

**Solution**:
```python
# 1. Add user to group
nx.rebac_create(
    subject=("user", "alice"),
    relation="member",
    object=("group", "engineering"),
)

# 2. Grant group access
nx.rebac_create(
    subject=("group", "engineering"),
    relation="direct_editor",
    object=("file", "/projects/feature.md"),
)

# 3. Verify
has_access = nx.rebac_check(("user", "alice"), "write", ("file", "/projects/feature.md"))
```

### Problem: "Permission check is slow"

**Cause**: Deep graph traversal without caching.

**Solution**:
```python
# 1. Check cache TTL
rebac_manager = nx.rebac_manager
print(f"Cache TTL: {rebac_manager.cache_ttl_seconds}s")

# 2. Increase cache TTL (default: 5 minutes)
rebac_manager.cache_ttl_seconds = 600  # 10 minutes

# 3. Clean up expired cache
rebac_manager.cleanup_expired_cache()

# 4. Simplify relationship graph (avoid deep nesting)
```

### Problem: "Cross-tenant access leak"

**Cause**: Missing or incorrect tenant_id in operations.

**Solution**:
```python
# ✅ Always specify tenant_id
nx.read("/file.txt", subject=("user", "alice"), tenant_id="org_acme")

# Verify isolation
alice_acme_access = nx.rebac_check(
    ("user", "alice"), "read", ("file", "/file.txt")
)  # tenant_id=org_acme

alice_techcorp_access = nx.rebac_check(
    ("user", "alice"), "read", ("file", "/file.txt")
)  # tenant_id=org_techcorp (should be False)
```

---



---

## Summary

Nexus uses **pure ReBAC** for all permissions:
- ✅ Explicit relationships define access
- ✅ Subject-based operations
- ✅ Multi-tenant isolation
- ✅ Flexible and scalable
- ✅ Industry-standard Zanzibar model

**See Also:**
- [Authentication Guide](authentication.md)
- [Multi-Tenant Guide](MULTI_TENANT.md)
- [API Reference](api/permissions.md)
