# Context and Permissions

← [API Documentation](README.md)

This document describes operation contexts and ReBAC (Relationship-Based Access Control) permissions.

Nexus uses ReBAC methods for all permission management: `nx.rebac_create()`, `nx.rebac_check()`, `nx.rebac_expand()`, etc. For advanced features, you can also access them via `nx.rebac_manager`.

Nexus supports two types of operation contexts for permission checks and tenant isolation.

### OperationContext (Basic)

The simple context for basic permission checks:

```python
from nexus.core.permissions import OperationContext

context = OperationContext(
    user: str,                    # User ID
    groups: list[str] = [],       # List of group IDs
    is_admin: bool = False,       # Admin bypass flag
    is_system: bool = False       # System operation bypass flag
)
```

**Examples:**

```python
from nexus.core.permissions import OperationContext

# Create context for a user
ctx = OperationContext(
    user="alice",
    groups=["team-engineering", "project-alpha"],
    is_admin=False
)

# Write file with context
nx.write("/workspace/alice/notes.txt", b"Hello", context=ctx)

# Read file with context
content = nx.read("/workspace/alice/notes.txt", context=ctx)
```

### EnhancedOperationContext (Recommended for Production)

Enhanced context with P0-4 security features (scoped admin, audit trail):

```python
from nexus.core.permissions_enhanced import EnhancedOperationContext, AdminCapability

context = EnhancedOperationContext(
    user: str,                              # User ID (legacy - use subject_id)
    groups: list[str] = [],                 # List of group IDs
    tenant_id: str | None = None,           # Tenant/organization ID
    is_admin: bool = False,                 # Admin privileges flag
    is_system: bool = False,                # System operation flag
    admin_capabilities: set[str] = set(),   # Scoped admin capabilities (P0-4)
    request_id: str = auto_generated,       # Audit trail correlation ID
    subject_type: str = "user",             # Subject type (user, agent, service, session)
    subject_id: str | None = None           # Unique subject identifier
)
```

**Examples:**

```python
from nexus.core.permissions_enhanced import EnhancedOperationContext, AdminCapability

# Regular user context
user_ctx = EnhancedOperationContext(
    user="alice",
    groups=["team-engineering"],
    tenant_id="acme-corp",
    is_admin=False
)

# Admin with scoped capabilities (P0-4)
admin_ctx = EnhancedOperationContext(
    user="admin",
    groups=["admins"],
    is_admin=True,
    admin_capabilities={
        AdminCapability.READ_ALL,       # Can read any file
        AdminCapability.MANAGE_REBAC    # Can manage permissions
    }
)

# System operation with audit trail (P0-4)
system_ctx = EnhancedOperationContext(
    user="backup-service",
    is_system=True,  # Bypasses checks but logs to audit trail
    subject_type="service",
    subject_id="backup-prod"
)

# All operations work with both context types
nx.write("/workspace/data.txt", b"content", context=user_ctx)
nx.read("/system/config.json", context=admin_ctx)  # Uses admin capability
```

**Note:** All Nexus APIs accept both `OperationContext` and `EnhancedOperationContext`. Use `EnhancedOperationContext` for production deployments to get:
- Scoped admin access (prevents privilege escalation)
- Immutable audit logging (compliance)
- Subject-based identity (user, agent, service, session)
- Request correlation (debugging)

### Permission Management (ReBAC)

Nexus uses ReBAC (Relationship-Based Access Control) for fine-grained permissions.

#### rebac_create()

Create a relationship tuple in the ReBAC system.

```python
def rebac_create(
    subject: tuple[str, str],
    relation: str,
    object: tuple[str, str],
    expires_at: datetime | None = None,
    tenant_id: str | None = None,
) -> str
```

**Parameters:**
- `subject`: (subject_type, subject_id) tuple (e.g., `("agent", "alice")`)
- `relation`: Relation type (e.g., `"member-of"`, `"owner-of"`, `"viewer-of"`)
- `object`: (object_type, object_id) tuple (e.g., `("group", "developers")`)
- `expires_at` (optional): Expiration datetime for temporary relationships
- `tenant_id` (optional): Tenant ID for multi-tenant isolation

**Returns:** Tuple ID (string)

**Example:**
```python
# Alice is member of developers group
tuple_id = nx.rebac_create(
    subject=("agent", "alice"),
    relation="member-of",
    object=("group", "developers")
)

# Developers group owns file
nx.rebac_create(
    subject=("group", "developers"),
    relation="owner-of",
    object=("file", "/workspace/project.txt")
)

# Temporary viewer access (expires in 1 hour)
from datetime import datetime, timedelta, UTC
nx.rebac_create(
    subject=("agent", "bob"),
    relation="viewer-of",
    object=("file", "/workspace/secret.txt"),
    expires_at=datetime.now(UTC) + timedelta(hours=1)
)
```

#### rebac_check()

Check if a subject has permission on an object via ReBAC. Supports ABAC (Attribute-Based Access Control) via contextual conditions.

```python
def rebac_check(
    subject: tuple[str, str],
    permission: str,
    object: tuple[str, str],
    context: dict[str, Any] | None = None,
    tenant_id: str | None = None,
) -> bool
```

**Parameters:**
- `subject`: (subject_type, subject_id) tuple
- `permission`: Permission to check (e.g., `"read"`, `"write"`, `"owner"`)
- `object`: (object_type, object_id) tuple
- `context` (optional): ABAC context for condition evaluation (time, IP, device, custom attributes)
- `tenant_id` (optional): Tenant ID for multi-tenant isolation

**Returns:** `True` if permission is granted, `False` otherwise

**Example:**
```python
# Basic permission check
has_access = nx.rebac_check(
    subject=("agent", "alice"),
    permission="read",
    object=("file", "/workspace/doc.txt"),
    tenant_id="org_acme"
)

# ABAC check with time window
from datetime import datetime, UTC
has_access = nx.rebac_check(
    subject=("agent", "contractor"),
    permission="read",
    object=("file", "/sensitive.txt"),
    context={
        "time": datetime.now(UTC).isoformat(),  # ISO8601 format
        "ip": "10.0.1.5"
    }
)

# Check if group owns workspace
is_owner = nx.rebac_check(
    subject=("group", "developers"),
    permission="owner",
    object=("workspace", "/workspace")
)
```

#### rebac_explain()

Explain why a subject has or doesn't have permission on an object.

```python
def rebac_explain(
    subject: tuple[str, str],
    permission: str,
    object: tuple[str, str],
    tenant_id: str | None = None,
) -> dict
```

**Parameters:**
- `subject`: (subject_type, subject_id) tuple
- `permission`: Permission to check (e.g., `"read"`, `"write"`, `"owner"`)
- `object`: (object_type, object_id) tuple
- `tenant_id` (optional): Tenant ID for multi-tenant isolation

**Returns:** Dictionary with:
- `result` (bool): Whether permission is granted
- `cached` (bool): Whether result came from cache
- `reason` (str): Human-readable explanation
- `paths` (list[dict]): All checked paths through the graph
- `successful_path` (dict | None): The path that granted access (if any)

**Example:**
```python
# Why does alice have read permission?
explanation = nx.rebac_explain(
    subject=("agent", "alice"),
    permission="read",
    object=("file", "/workspace/doc.txt"),
    tenant_id="org_acme"
)

print(explanation["result"])  # True
print(explanation["reason"])  # "agent:alice has 'read' on file:/workspace/doc.txt (expanded to relations: viewer) via parent inheritance"

# Inspect the permission path
if explanation["successful_path"]:
    print("Access granted via:", explanation["successful_path"])
else:
    print("Access denied - no valid path found")

# View all paths checked (for debugging)
for path in explanation["paths"]:
    print(f"Checked {path['permission']} at depth {path['depth']}: granted={path['granted']}")
```

#### rebac_expand()

Find all subjects that have a given permission on an object.

```python
def rebac_expand(
    permission: str,
    object: tuple[str, str],
) -> list[tuple[str, str]]
```

**Parameters:**
- `permission`: Permission to check (e.g., `"read"`, `"write"`, `"owner"`)
- `object`: (object_type, object_id) tuple

**Returns:** List of (subject_type, subject_id) tuples that have the permission

**Example:**
```python
# Who can read this file?
subjects = nx.rebac_expand(
    permission="read",
    object=("file", "/workspace/doc.txt")
)
# Returns: [('agent', 'alice'), ('agent', 'bob'), ('group', 'developers')]

# Who owns this workspace?
owners = nx.rebac_expand(
    permission="owner",
    object=("workspace", "/workspace")
)
# Returns: [('group', 'admins')]
```

#### rebac_delete()

Delete a relationship tuple by ID.

```python
def rebac_delete(tuple_id: str) -> bool
```

**Parameters:**
- `tuple_id`: ID of the tuple to delete (returned from `rebac_create()`)

**Returns:** `True` if tuple was deleted, `False` if not found

**Example:**
```python
# Create a relationship
tuple_id = nx.rebac_create(
    subject=("agent", "alice"),
    relation="viewer-of",
    object=("file", "/workspace/doc.txt")
)

# Delete it later
deleted = nx.rebac_delete(tuple_id)
print(deleted)  # True
```

#### grant_consent()

Grant consent for one subject to discover another (privacy/consent management).

```python
def grant_consent(
    from_subject: tuple[str, str],
    to_subject: tuple[str, str],
    expires_at: datetime | None = None,
    tenant_id: str | None = None
) -> str
```

**Parameters:**
- `from_subject`: Who is granting consent (e.g., `("profile", "alice")`, `("file", "/doc.txt")`)
- `to_subject`: Who can now discover (e.g., `("user", "bob")`)
- `expires_at` (optional): Expiration datetime for temporary consent
- `tenant_id` (optional): Tenant ID for multi-tenant isolation

**Returns:** Tuple ID of the consent relationship

**Example:**
```python
from datetime import datetime, timedelta, UTC

# Alice grants Bob permanent consent to discover her profile
consent_id = nx.grant_consent(
    from_subject=("profile", "alice"),
    to_subject=("user", "bob")
)

# Grant temporary consent (expires in 30 days)
temp_consent = nx.grant_consent(
    from_subject=("file", "/doc.txt"),
    to_subject=("user", "charlie"),
    expires_at=datetime.now(UTC) + timedelta(days=30)
)

# Check if Bob can now discover
can_discover = nx.rebac_check(
    subject=("user", "bob"),
    permission="discover",
    object=("profile", "alice")
)  # True
```

#### revoke_consent()

Revoke previously granted consent.

```python
def revoke_consent(
    from_subject: tuple[str, str],
    to_subject: tuple[str, str]
) -> bool
```

**Parameters:**
- `from_subject`: Who is revoking consent
- `to_subject`: Who loses discovery access

**Returns:** `True` if consent was revoked, `False` if no consent existed

**Example:**
```python
# Revoke Bob's consent to see Alice's profile
revoked = nx.revoke_consent(
    from_subject=("profile", "alice"),
    to_subject=("user", "bob")
)

# Bob can no longer discover
can_discover = nx.rebac_check(
    subject=("user", "bob"),
    permission="discover",
    object=("profile", "alice")
)  # False
```

#### make_public()

Make a resource publicly discoverable (anyone can discover it without consent).

```python
def make_public(
    resource: tuple[str, str],
    tenant_id: str | None = None
) -> str
```

**Parameters:**
- `resource`: Resource to make public (e.g., `("profile", "alice")`, `("file", "/doc.txt")`)
- `tenant_id` (optional): Tenant ID for multi-tenant isolation

**Returns:** Tuple ID of the public relationship

**Example:**
```python
# Make Alice's profile publicly discoverable
public_id = nx.make_public(("profile", "alice"))

# Anyone can now discover (no consent needed)
can_discover = nx.rebac_check(
    subject=("user", "anyone"),
    permission="discover",
    object=("profile", "alice")
)  # True
```

#### make_private()

Remove public discoverability from a resource.

```python
def make_private(
    resource: tuple[str, str]
) -> bool
```

**Parameters:**
- `resource`: Resource to make private

**Returns:** `True` if public access was removed, `False` if resource wasn't public

**Example:**
```python
# Make profile private again
made_private = nx.make_private(("profile", "alice"))

# Public access removed (individual consent still works)
can_discover_public = nx.rebac_check(
    subject=("user", "anyone"),
    permission="discover",
    object=("profile", "alice")
)  # False
```

#### rebac_expand_with_privacy()

Privacy-aware expand that filters results based on consent.

```python
def rebac_expand_with_privacy(
    permission: str,
    object: tuple[str, str],
    respect_consent: bool = True,
    requester: tuple[str, str] | None = None
) -> list[tuple[str, str]]
```

**Parameters:**
- `permission`: Permission to expand (e.g., `"view"`, `"edit"`)
- `object`: Object to expand from
- `respect_consent`: Whether to filter by consent (default: `True`)
- `requester`: Who is requesting (filters results they can discover)

**Returns:** List of subjects that have the permission AND are discoverable by requester

**Example:**
```python
# Find all viewers of a workspace (privacy-aware)
discoverable_viewers = nx.rebac_expand_with_privacy(
    permission="view",
    object=("workspace", "/project"),
    respect_consent=True,
    requester=("user", "alice")
)
# Returns: Only viewers that Alice has consent to discover

# Standard expand (no privacy filtering)
all_viewers = nx.rebac_expand_with_privacy(
    permission="view",
    object=("workspace", "/project"),
    respect_consent=False
)
# Returns: All viewers regardless of consent
```

**Use Cases:**
- User directory (only show profiles user has consent to see)
- Team member lists (respect privacy settings)
- Search results (filter by discoverability)
- Collaboration features (honor consent preferences)

---

## Namespace Management

Namespaces define the permission model for different object types. Each namespace specifies:
- **Relations**: How subjects relate to objects (e.g., `owner`, `editor`, `viewer`)
- **Permissions**: Which relations grant which permissions (e.g., `read`, `write`)

### Default Namespaces

Nexus comes with pre-configured namespaces:

- **`file`**: Supports parent directory inheritance, owner/editor/viewer roles
- **`group`**: Member and admin relations
- **`memory`**: Owner/editor/viewer for AI agent memories
- **`profile`**: With consent and privacy relations

### namespace_create()

Create or update a custom namespace configuration.

```python
def namespace_create(
    object_type: str,
    config: dict[str, Any]
) -> None
```

**Parameters:**
- `object_type`: Type of objects this namespace applies to (e.g., `"document"`, `"project"`)
- `config`: Namespace configuration with `"relations"` and `"permissions"` keys

**Example:**
```python
# Create a custom document namespace
nx.namespace_create("document", {
    "relations": {
        "owner": {},
        "editor": {},
        "viewer": {"union": ["editor", "owner"]}
    },
    "permissions": {
        "read": ["viewer", "editor", "owner"],
        "write": ["editor", "owner"],
        "delete": ["owner"]
    }
})

# Now you can use it with ReBAC
nx.rebac_create(
    subject=("user", "alice"),
    relation="owner",
    object=("document", "doc123")
)

# Check permissions
can_write = nx.rebac_check(
    subject=("user", "alice"),
    permission="write",
    object=("document", "doc123")
)  # True
```

### namespace_list()

List all registered namespace configurations.

```python
def namespace_list() -> list[dict[str, Any]]
```

**Returns:** List of namespace dictionaries with metadata and config

**Example:**
```python
# List all namespaces
namespaces = nx.namespace_list()

for ns in namespaces:
    print(f"{ns['object_type']}: {list(ns['config']['relations'].keys())}")
# Output:
# file: ['parent', 'owner', 'editor', 'viewer', ...]
# group: ['member', 'admin']
# memory: ['owner', 'editor', 'viewer']
# document: ['owner', 'editor', 'viewer']
```

### namespace_get()

Get namespace configuration for a specific object type.

```python
def namespace_get(object_type: str) -> dict[str, Any] | None
```

**Parameters:**
- `object_type`: Type of objects (e.g., `"file"`, `"group"`)

**Returns:** Namespace configuration dict or `None` if not found

**Example:**
```python
# Get file namespace
file_ns = nx.namespace_get("file")

if file_ns:
    print(f"Relations: {list(file_ns['config']['relations'].keys())}")
    print(f"Permissions: {list(file_ns['config']['permissions'].keys())}")
```

### namespace_delete()

Delete a namespace configuration.

```python
def namespace_delete(object_type: str) -> bool
```

**Parameters:**
- `object_type`: Type of objects to remove namespace for

**Returns:** `True` if namespace was deleted, `False` if not found

**Warning:** This does not delete existing tuples for this object type.

**Example:**
```python
# Delete custom namespace
deleted = nx.namespace_delete("document")
print(deleted)  # True
```

### Advanced Namespace Patterns

#### Union Relations
Combine multiple relations for inheritance:

```python
nx.namespace_create("project", {
    "relations": {
        "owner": {},
        "maintainer": {},
        "contributor": {},
        # Viewer = anyone who is maintainer, contributor, or owner
        "viewer": {"union": ["maintainer", "contributor", "owner"]}
    },
    "permissions": {
        "read": ["viewer"],
        "write": ["maintainer", "owner"],
        "admin": ["owner"]
    }
})
```

#### Computed Usersets (tupleToUserset)
Delegate permissions through relationships:

```python
nx.namespace_create("document", {
    "relations": {
        "parent_folder": {},  # Link to parent folder
        # Inherit owner from parent
        "parent_owner": {
            "tupleToUserset": {
                "tupleset": "parent_folder",
                "computedUserset": "owner"
            }
        },
        "direct_owner": {},
        # Owner = direct OR inherited from parent
        "owner": {"union": ["direct_owner", "parent_owner"]}
    },
    "permissions": {
        "write": ["owner"]
    }
})
```

---

## See Also

- [File Operations](file-operations.md) - Using contexts with file operations
- [Configuration](configuration.md) - Permission configuration
- [Advanced Usage](advanced-usage.md) - Permission patterns

## Next Steps

1. Create operation contexts for users
2. Set up ReBAC relationships via nx.rebac_manager
3. Use contexts in file operations
