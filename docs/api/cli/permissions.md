# CLI: Permissions (ReBAC)

← [CLI Reference](index.md) | [API Documentation](../README.md)

This document describes CLI commands for ReBAC (Relationship-Based Access Control) permissions and their Python API equivalents.

## rebac create - Create relationship

Create a relationship tuple between subject and object.

**CLI:**
```bash
# Make alice a member of eng-team
nexus rebac create agent alice member-of group eng-team

# Give alice viewer access to file
nexus rebac create agent alice direct_viewer file file123

# With tenant isolation (via flag)
nexus rebac create agent alice member-of group eng-team --tenant-id org_acme

# With tenant isolation (via environment variable)
export NEXUS_TENANT_ID=org_acme
nexus rebac create agent alice member-of group eng-team

# With expiration
nexus rebac create agent bob direct_viewer file secret --expires 2025-12-31T23:59:59
```

**Python API:**
```python
# Create relationship
nx.create_relationship(
    subject=("agent", "alice"),
    relation="member-of",
    object=("group", "eng-team")
)

# Give viewer access
nx.create_relationship(
    subject=("agent", "alice"),
    relation="viewer",
    object=("file", "file123")
)

# With tenant isolation
nx.create_relationship(
    subject=("agent", "alice"),
    relation="member-of",
    object=("group", "eng-team"),
    tenant_id="org_acme"
)

# With expiration
from datetime import datetime
nx.create_relationship(
    subject=("agent", "bob"),
    relation="viewer",
    object=("file", "secret"),
    expires_at=datetime(2025, 12, 31, 23, 59, 59)
)
```

**See Also:**
- [Python API: create_relationship()](../permissions.md#create_relationship)

---

## rebac check - Check permission

Check if a subject has permission on an object.

**CLI:**
```bash
# Check if alice can read file
nexus rebac check agent alice read file file123
```

**Python API:**
```python
# Check permission
has_permission = nx.check_permission(
    subject=("agent", "alice"),
    permission="read",
    object=("file", "file123")
)
print(f"Has permission: {has_permission}")
```

**See Also:**
- [Python API: check_permission()](../permissions.md#check_permission)

---

## rebac explain - Explain permission check

Explain why a permission check succeeds or fails.

**CLI:**
```bash
# Explain why alice has read permission on file
nexus rebac explain agent alice read file file123

# Show detailed path information
nexus rebac explain agent alice read file file123 --verbose

# Explain why permission is denied
nexus rebac explain agent bob write workspace main
```

**Python API:**
```python
# Explain permission
explanation = nx.explain_permission(
    subject=("agent", "alice"),
    permission="read",
    object=("file", "file123")
)
print(f"Granted: {explanation['granted']}")
print(f"Reason: {explanation['reason']}")
if explanation.get('path'):
    print(f"Path: {explanation['path']}")
```

**Options:**
- `--verbose`: Show detailed explanation with full path

**See Also:**
- [Python API: explain_permission()](../permissions.md#explain_permission)

---

## rebac expand - Find all subjects with permission

Find all subjects that have a specific permission on an object.

**CLI:**
```bash
# Find everyone who can read file123
nexus rebac expand read file file123
```

**Python API:**
```python
# Expand permission
subjects = nx.expand_permission(
    permission="read",
    object=("file", "file123")
)
for subject in subjects:
    print(f"{subject[0]}:{subject[1]}")
```

**See Also:**
- [Python API: expand_permission()](../permissions.md#expand_permission)

---

## rebac delete - Delete relationship

Delete a relationship tuple.

**CLI:**
```bash
# Delete relationship tuple
nexus rebac delete <tuple-id>
```

**Python API:**
```python
# Delete relationship
nx.delete_relationship(tuple_id="123")

# Or delete by components
nx.delete_relationship(
    subject=("agent", "alice"),
    relation="viewer",
    object=("file", "file123")
)
```

**See Also:**
- [Python API: delete_relationship()](../permissions.md#delete_relationship)

---

## rebac namespace-create - Create custom namespace

Create a custom namespace with relations and permissions.

**CLI:**
```bash
# Create from config file (JSON/YAML)
nexus rebac namespace-create document --config-file document.json

# Create inline
nexus rebac namespace-create project \
  --relations owner \
  --relations maintainer \
  --relations contributor \
  --relations viewer:union:maintainer,contributor,owner \
  --permission read:viewer \
  --permission write:maintainer,owner \
  --permission admin:owner
```

**Config file format (JSON):**
```json
{
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
}
```

**Python API:**
```python
# Create namespace from dict
nx.create_namespace(
    object_type="document",
    config={
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
    }
)
```

**See Also:**
- [Python API: create_namespace()](../permissions.md#create_namespace)

---

## rebac namespace-list - List namespaces

List all defined namespaces.

**CLI:**
```bash
# List as table
nexus rebac namespace-list

# JSON output
nexus rebac namespace-list --format json
```

**Python API:**
```python
# List namespaces
namespaces = nx.list_namespaces()
for ns in namespaces:
    print(f"{ns['object_type']}: {ns['relations']}")
```

**See Also:**
- [Python API: list_namespaces()](../permissions.md#list_namespaces)

---

## rebac namespace-get - View namespace config

View the configuration of a specific namespace.

**CLI:**
```bash
# View file namespace (YAML)
nexus rebac namespace-get file

# JSON output
nexus rebac namespace-get memory --format json
```

**Python API:**
```python
# Get namespace config
config = nx.get_namespace("file")
print(f"Relations: {config['relations']}")
print(f"Permissions: {config['permissions']}")
```

**See Also:**
- [Python API: get_namespace()](../permissions.md#get_namespace)

---

## rebac namespace-delete - Delete namespace

Delete a namespace definition.

**CLI:**
```bash
# Delete with confirmation
nexus rebac namespace-delete document

# Skip confirmation
nexus rebac namespace-delete document --yes
```

**Python API:**
```python
# Delete namespace
nx.delete_namespace("document")
```

**Note:** This does not delete existing tuples for this object type.

**See Also:**
- [Python API: delete_namespace()](../permissions.md#delete_namespace)

---

## Common Workflows

### Set up team permissions
```bash
# Create team groups
nexus rebac create agent alice member-of group eng-team
nexus rebac create agent bob member-of group eng-team
nexus rebac create agent charlie member-of group product-team

# Give team access to workspace
nexus rebac create group eng-team direct_editor workspace eng-workspace
nexus rebac create group product-team direct_viewer workspace product-workspace

# Check access
nexus rebac check agent alice write workspace eng-workspace
nexus rebac explain agent alice write workspace eng-workspace
```

### Python equivalent
```python
# Create team groups
teams = [
    (("agent", "alice"), "member-of", ("group", "eng-team")),
    (("agent", "bob"), "member-of", ("group", "eng-team")),
    (("agent", "charlie"), "member-of", ("group", "product-team")),
]

for subject, relation, obj in teams:
    nx.create_relationship(subject, relation, obj)

# Give team access to workspaces
nx.create_relationship(
    ("group", "eng-team"),
    "editor",
    ("workspace", "eng-workspace")
)
nx.create_relationship(
    ("group", "product-team"),
    "viewer",
    ("workspace", "product-workspace")
)

# Check access
can_write = nx.check_permission(
    ("agent", "alice"),
    "write",
    ("workspace", "eng-workspace")
)
print(f"Alice can write: {can_write}")

# Explain access
explanation = nx.explain_permission(
    ("agent", "alice"),
    "write",
    ("workspace", "eng-workspace")
)
print(f"Reason: {explanation['reason']}")
```

---

## See Also

- [CLI Reference Overview](index.md)
- [Python API: Permissions](../permissions.md)
- [Multi-Tenant Documentation](../../MULTI_TENANT.md)
- [ReBAC Architecture](../../architecture/rebac.md)
