# Nexus Permission & Authorization System

**Version:** 2.0 (Reflects Actual Implementation)

---

## Table of Contents

1. [Overview](#overview)
2. [Core Concepts](#core-concepts)
3. [How ReBAC Works](#how-rebac-works)
4. [Embedded vs Server Mode](#embedded-vs-server-mode)
5. [Usage Examples](#usage-examples)
6. [Configuration](#configuration)
7. [Multi-Tenant Support](#multi-tenant-support)
8. [CLI Commands](#cli-commands)
9. [Security & Best Practices](#security--best-practices)
10. [Migration Guide](#migration-guide)
11. [Performance](#performance)
12. [Production Features (P0 Enhancements)](#production-features-p0-enhancements)
13. [Troubleshooting](#troubleshooting)

---

## Overview

Nexus uses a **Relationship-Based Access Control (ReBAC)** system inspired by Google's Zanzibar. Unlike traditional UNIX permissions or ACLs, ReBAC models permissions as relationships between subjects and objects.

### ⚠️ Important: Permissions Work in Both Modes

**Unlike authentication (server-only), permissions work in BOTH embedded and server modes:**

- **Embedded Mode**: Direct ReBAC operations via SDK
  ```python
  from nexus.sdk import connect
  nx = connect()

  # Create permissions
  nx.rebac_create(
      subject=("user", "alice"),
      relation="direct_owner",
      object=("file", "/workspace/file.txt")
  )

  # Check permissions
  can_write = nx.rebac_check(
      subject=("user", "alice"),
      permission="write",
      object=("file", "/workspace/file.txt")
  )
  ```

- **Server Mode**: ReBAC via HTTP RPC
  ```python
  from nexus.remote import RemoteNexusFS
  nx = RemoteNexusFS(server_url="...", api_key="...")

  # Same API, works over HTTP
  nx.rebac_create(...)
  nx.rebac_check(...)
  ```

**Key Differences from Authentication:**
- Authentication: Server-only (identifies who you are)
- Authorization/Permissions: Both modes (determines what you can do)

### Why ReBAC?

Traditional permission systems don't scale well for modern applications:

| System | Use Case | Limitations |
|--------|----------|-------------|
| **UNIX (chmod)** | Single-user files | No groups, no hierarchies, no expiry |
| **ACLs (setfacl)** | File-level access | No relationships, hard to audit |
| **RBAC** | Static roles | Doesn't model relationships |
| **ReBAC (Nexus)** | Complex organizations | Scales to billions of relationships |

**ReBAC Features:**
- ✅ Hierarchical permissions (folders inherit to files)
- ✅ Group membership with transitive access
- ✅ Temporary permissions with expiry
- ✅ Relationship graphs (teams, departments, orgs)
- ✅ Multi-tenant isolation
- ✅ Audit trails for compliance
- ✅ Proven at scale (Google, Airbnb, Auth0)

---

## Core Concepts

### 1. Entities

Entities are identified by **(type, id)** tuples:

```python
# Subject types (who is requesting access)
("user", "alice")
("agent", "claude_001")
("service", "backup_bot")
("session", "session_abc123")

# Object types (what is being accessed)
("file", "/workspace/document.txt")
("directory", "/workspace/")
("memory", "user_preferences")
("resource", "billing_dashboard")
```

### 2. Relationships (Tuples)

A relationship tuple represents: **Subject has Relation with Object**

```python
# Alice directly owns a file
(
    subject=("user", "alice"),
    relation="direct_owner",
    object=("file", "/workspace/doc.txt")
)

# Bob is a viewer of the file
(
    subject=("user", "bob"),
    relation="direct_viewer",
    object=("file", "/workspace/doc.txt")
)

# Alice is a member of the engineering group
(
    subject=("user", "alice"),
    relation="member",
    object=("group", "engineering")
)
```

### 3. Relations vs Permissions

**Relations** are the edges in the relationship graph:
- `direct_owner`, `direct_viewer`, `direct_editor`
- `member`, `parent`, `child`
- Custom relations for your domain

**Permissions** are computed from relations:
- `read` - Can view content
- `write` - Can modify content
- `execute` - Can run/execute

**Mapping (configured in namespace):**
```python
{
    "relations": {
        "owner": {"can": ["read", "write", "execute"]},
        "editor": {"can": ["read", "write"]},
        "viewer": {"can": ["read"]}
    }
}
```

### 4. Namespace Configuration

Namespaces define how permissions are computed for different object types:

```python
# File namespace
{
    "relations": {
        # Owner = direct owner OR inherited from parent
        "owner": {
            "union": ["direct_owner", "parent_owner"]
        },

        # Direct owner (explicit grant)
        "direct_owner": {},

        # Parent owner (inherited from folder)
        "parent_owner": {
            "tupleToUserset": {
                "tupleset": "parent",        # Find parent folder
                "computedUserset": "owner"   # Get owner of parent
            }
        },

        # Viewer = owner OR direct viewer
        "viewer": {
            "union": ["owner", "direct_viewer"]
        },

        # Editor = owner OR direct editor
        "editor": {
            "union": ["owner", "direct_editor"]
        }
    }
}
```

---

## How ReBAC Works

### Permission Check Flow

**EnhancedReBACManager adds consistency levels and graph limits:**

```
1. Request: Can alice WRITE to /workspace/doc.txt?
   ↓
2. Extract subject: ("user", "alice")
   ↓
3. Check admin bypass: alice.is_admin? → No
   ↓
4. Determine consistency level:
   - EVENTUAL: Use cache (default)
   - BOUNDED: Max 1s staleness
   - STRONG: Bypass cache
   ↓
5. Map permission to relations:
   WRITE → ["editor", "owner"]
   ↓
6. Check cache (if not STRONG consistency): cached result? → No
   ↓
7. Graph Traversal (with P0-5 limits):
   ├─ Start traversal timer (100ms timeout)
   ├─ Initialize counters (max 10k nodes, 100 queries)
   ├─ Check editor relation
   │  ├─ Direct editor? → No
   │  └─ Union: owner? → Check owner
   │     ├─ Direct owner? → No
   │     └─ Parent owner?
   │        ├─ Find parent: /workspace/
   │        └─ alice owner of /workspace/? → YES ✓
   │
   └─ Result: GRANTED
   ↓
8. Generate version token (monotonic counter)
   ↓
9. Cache result (5 min TTL) with version
   ↓
10. Return: CheckResult(
      allowed=True,
      consistency_token="v123",
      decision_time_ms=45.2,
      cached=False,
      traversal_stats=TraversalStats(...)
    )
```

**Graph Limits (P0-5):**

| Limit | Default | Purpose |
|-------|---------|---------|
| `MAX_DEPTH` | 10 hops | Prevent infinite recursion |
| `MAX_FAN_OUT` | 1000 edges | Limit union expansion |
| `MAX_EXECUTION_TIME_MS` | 100ms | Hard timeout per check |
| `MAX_VISITED_NODES` | 10,000 nodes | Memory bound |
| `MAX_TUPLE_QUERIES` | 100 queries | Database query limit |

### Graph Traversal Algorithm

**Depth-First Search with Cycle Detection:**

```python
def check_relation(subject, relation, object, depth=0):
    # 1. Depth limit (prevent infinite loops)
    if depth > MAX_DEPTH:
        return False

    # 2. Cycle detection
    if (subject, relation, object) in visited:
        return False
    visited.add((subject, relation, object))

    # 3. Direct relationship exists?
    if direct_tuple_exists(subject, relation, object):
        return True

    # 4. Get namespace config for object type
    namespace = get_namespace(object.type)
    relation_config = namespace.relations[relation]

    # 5. Union (OR) expansion
    if "union" in relation_config:
        for sub_relation in relation_config["union"]:
            if check_relation(subject, sub_relation, object, depth+1):
                return True

    # 6. TupleToUserset (parent) expansion
    if "tupleToUserset" in relation_config:
        tts = relation_config["tupleToUserset"]
        # Find parent relationships
        parents = find_tuples(object, tts["tupleset"], ANY)
        for parent_tuple in parents:
            parent_object = parent_tuple.subject
            # Check permission on parent
            if check_relation(subject, tts["computedUserset"], parent_object, depth+1):
                return True

    return False
```

### Example: Hierarchical Permissions

```
File Structure:
/workspace/               (folder, owned by alice)
  ├─ sales/               (folder, owned by sales-team)
  │  └─ report.txt        (file)
  └─ eng/                 (folder, owned by eng-team)
     └─ code.py           (file)

Relationships:
1. alice --direct_owner--> /workspace/
2. sales-team --direct_owner--> /workspace/sales/
3. /workspace/sales/ --parent--> /workspace/sales/report.txt
4. bob --member--> sales-team

Check: Can bob READ /workspace/sales/report.txt?

Graph Traversal:
1. bob viewer of report.txt?
   ├─ bob owner of report.txt?
   │  ├─ bob direct_owner? → No
   │  └─ bob parent_owner?
   │     ├─ Parent: /workspace/sales/
   │     └─ bob owner of /workspace/sales/?
   │        ├─ bob direct_owner? → No
   │        └─ bob parent_owner?
   │           ├─ Parent: /workspace/
   │           └─ bob owner of /workspace/? → No
   │
   └─ bob direct_viewer? → No

DENIED (bob has no direct access)

But wait! Bob is member of sales-team:
1. sales-team --direct_owner--> /workspace/sales/
2. bob --member--> sales-team
3. Check: bob has sales-team's permissions?
   └─ sales-team owner of /workspace/sales/? → YES
   └─ /workspace/sales/ parent of report.txt? → YES
   └─ Result: GRANTED ✓
```

---

## Embedded vs Server Mode

### Embedded Mode

**Direct Database Access:**

```python
from nexus.sdk import connect

nx = connect({"data_dir": "./nexus-data"})

# All ReBAC operations are direct database calls
tuple_id = nx.rebac_create(
    subject=("user", "alice"),
    relation="direct_owner",
    object=("file", "/workspace/doc.txt")
)

# Permission check = SQL query + graph traversal
can_write = nx.rebac_check(
    subject=("user", "alice"),
    permission="write",
    object=("file", "/workspace/doc.txt")
)

# Fastest performance (no network overhead)
```

**Internal Flow:**
```
Python SDK
    ↓
NexusFS.rebac_create()
    ↓
ReBACManager._create_tuple()
    ↓
SQLAlchemy Session
    ↓
Database (SQLite/PostgreSQL)
```

### Server Mode

**HTTP RPC Protocol:**

```python
from nexus.remote import RemoteNexusFS

nx = RemoteNexusFS(
    server_url="http://localhost:8080",
    api_key="sk-alice-xxx"
)

# Same API, but over HTTP
tuple_id = nx.rebac_create(
    subject=("user", "alice"),
    relation="direct_owner",
    object=("file", "/workspace/doc.txt")
)

# RPC call to server
can_write = nx.rebac_check(
    subject=("user", "alice"),
    permission="write",
    object=("file", "/workspace/doc.txt")
)
```

**Internal Flow:**
```
RemoteNexusFS
    ↓
HTTP POST /api/nfs/rebac_create
    ↓
JSON-RPC Request
    ↓
RPCServer.handle_request()
    ↓
Authenticate API Key
    ↓
NexusFS.rebac_create()
    ↓
ReBACManager._create_tuple()
    ↓
Database
    ↓
JSON-RPC Response
    ↓
HTTP 200 OK
    ↓
RemoteNexusFS returns result
```

### Performance Comparison

| Operation | Embedded | Server | Overhead |
|-----------|----------|--------|----------|
| `rebac_create()` | ~5ms | ~15ms | HTTP + JSON |
| `rebac_check()` (cache hit) | ~1ms | ~10ms | HTTP + JSON |
| `rebac_check()` (cache miss) | ~50ms | ~60ms | Graph traversal |
| `rebac_expand()` | ~100ms | ~110ms | Recursive expansion |

**When to Use:**
- **Embedded**: Single-user apps, scripts, notebooks, CLI tools
- **Server**: Multi-user apps, production deployments, centralized permissions

---

## Usage Examples

### Example 1: Basic File Permissions

```python
from nexus.sdk import connect

nx = connect()

# Grant Alice ownership
tuple_id = nx.rebac_create(
    subject=("user", "alice"),
    relation="direct_owner",
    object=("file", "/workspace/document.txt")
)

# Grant Bob viewer access
nx.rebac_create(
    subject=("user", "bob"),
    relation="direct_viewer",
    object=("file", "/workspace/document.txt")
)

# Alice can write (owner has write permission)
alice_can_write = nx.rebac_check(
    subject=("user", "alice"),
    permission="write",
    object=("file", "/workspace/document.txt")
)
# → True

# Bob can only read (viewer has read permission)
bob_can_write = nx.rebac_check(
    subject=("user", "bob"),
    permission="write",
    object=("file", "/workspace/document.txt")
)
# → False

bob_can_read = nx.rebac_check(
    subject=("user", "bob"),
    permission="read",
    object=("file", "/workspace/document.txt")
)
# → True

# Use strong consistency for critical checks (P0-1)
from nexus.core.rebac_manager_enhanced import ConsistencyLevel

result = nx._rebac_manager.rebac_check(
    subject=("user", "alice"),
    permission="write",
    object=("file", "/workspace/document.txt"),
    consistency=ConsistencyLevel.STRONG  # Bypass cache
)
# → CheckResult(allowed=True, consistency_token="v123", ...)
```

### Example 2: Group Permissions

```python
# Create engineering group
nx.rebac_create(
    subject=("user", "alice"),
    relation="member",
    object=("group", "engineering")
)

nx.rebac_create(
    subject=("user", "bob"),
    relation="member",
    object=("group", "engineering")
)

# Grant group access to folder
nx.rebac_create(
    subject=("group", "engineering"),
    relation="direct_owner",
    object=("directory", "/workspace/eng/")
)

# Both Alice and Bob inherit permissions
alice_can_access = nx.rebac_check(
    subject=("user", "alice"),
    permission="write",
    object=("directory", "/workspace/eng/")
)
# → True (via engineering group membership)

bob_can_access = nx.rebac_check(
    subject=("user", "bob"),
    permission="write",
    object=("directory", "/workspace/eng/")
)
# → True (via engineering group membership)
```

### Example 3: Hierarchical Permissions

```python
# Folder structure
nx.mkdir("/workspace/projects/")
nx.mkdir("/workspace/projects/ai-app/")

# Grant Alice ownership of parent folder
nx.rebac_create(
    subject=("user", "alice"),
    relation="direct_owner",
    object=("directory", "/workspace/projects/")
)

# Create file in subfolder
nx.write("/workspace/projects/ai-app/code.py", b"print('hello')")

# Create parent relationship (auto-created by default)
nx.rebac_create(
    subject=("directory", "/workspace/projects/"),
    relation="parent",
    object=("directory", "/workspace/projects/ai-app/")
)

nx.rebac_create(
    subject=("directory", "/workspace/projects/ai-app/"),
    relation="parent",
    object=("file", "/workspace/projects/ai-app/code.py")
)

# Alice inherits permissions through parent chain
alice_can_edit = nx.rebac_check(
    subject=("user", "alice"),
    permission="write",
    object=("file", "/workspace/projects/ai-app/code.py")
)
# → True (via parent_owner relationship)
```

### Example 4: Temporary Access

```python
from datetime import datetime, timedelta, UTC

# Grant contractor access for 24 hours
tuple_id = nx.rebac_create(
    subject=("user", "contractor_john"),
    relation="direct_viewer",
    object=("file", "/workspace/sensitive.txt"),
    expires_at=datetime.now(UTC) + timedelta(hours=24)
)

# Access works immediately
can_read = nx.rebac_check(
    subject=("user", "contractor_john"),
    permission="read",
    object=("file", "/workspace/sensitive.txt")
)
# → True

# After 24 hours, permission automatically expires
# (cleanup_expired_tuples() removes expired relationships)
```

### Example 5: Find All Users with Access

```python
# Grant permissions to multiple users
nx.rebac_create(("user", "alice"), "direct_owner", ("file", "/workspace/doc.txt"))
nx.rebac_create(("user", "bob"), "direct_editor", ("file", "/workspace/doc.txt"))
nx.rebac_create(("user", "charlie"), "direct_viewer", ("file", "/workspace/doc.txt"))

# Find all users who can write
writers = nx.rebac_expand(
    permission="write",
    object=("file", "/workspace/doc.txt")
)
# → [("user", "alice"), ("user", "bob")]

# Find all users who can read
readers = nx.rebac_expand(
    permission="read",
    object=("file", "/workspace/doc.txt")
)
# → [("user", "alice"), ("user", "bob"), ("user", "charlie")]
```

### Example 6: Organization Hierarchy

```python
# Build org structure
nx.rebac_create(("user", "alice"), "member", ("team", "backend"))
nx.rebac_create(("team", "backend"), "part_of", ("department", "engineering"))
nx.rebac_create(("department", "engineering"), "part_of", ("organization", "acme"))

# Grant access at org level
nx.rebac_create(
    ("organization", "acme"),
    "direct_owner",
    ("resource", "company_wiki")
)

# Alice inherits through 3-level chain
alice_can_access = nx.rebac_check(
    subject=("user", "alice"),
    permission="write",
    object=("resource", "company_wiki")
)
# → True (via backend → engineering → acme)
```

### Example 7: File Operations with Permission Enforcement

```python
# Enable permission enforcement
nx = connect({"enforce_permissions": True})

# Write requires permission
try:
    nx.write(
        "/workspace/protected.txt",
        b"content",
        subject=("user", "alice"),
        tenant_id="acme"
    )
except PermissionError:
    print("Access denied - no write permission")

# Grant permission
nx.rebac_create(
    subject=("user", "alice"),
    relation="direct_editor",
    object=("file", "/workspace/protected.txt")
)

# Now write succeeds
nx.write(
    "/workspace/protected.txt",
    b"content",
    subject=("user", "alice"),
    tenant_id="acme"
)
# → Success!
```

---

## Configuration

### NexusFS Configuration

```python
from nexus.sdk import connect

nx = connect({
    "data_dir": "./nexus-data",
    "enforce_permissions": True,      # Enable permission checks (default: False in v0.5.0)
    "inherit_permissions": True,      # Auto-create parent relationships
    "is_admin": False,                # Admin bypass flag
    "cache_ttl_seconds": 300,         # Cache TTL (5 minutes)
})
```

### YAML Configuration

```yaml
# nexus.yaml
data_dir: ./nexus-data
enforce_permissions: true
inherit_permissions: true

# ReBAC automatically configured with default namespaces
```

### Environment Variables

```bash
# Subject identity (for CLI operations)
export NEXUS_SUBJECT_TYPE=user
export NEXUS_SUBJECT_ID=alice
export NEXUS_TENANT_ID=acme

# Data directory
export NEXUS_DATA_DIR=./nexus-data

# Admin mode (bypass all permissions)
export NEXUS_IS_ADMIN=false
```

### EnhancedReBACManager Configuration

**Note:** Nexus uses `EnhancedReBACManager` which includes all production-ready features (P0 fixes for GA).

```python
from nexus.core.rebac_manager_enhanced import EnhancedReBACManager

rebac = EnhancedReBACManager(
    engine=db_engine,
    cache_ttl_seconds=300,              # 5 minute cache
    max_depth=10,                       # Graph traversal depth limit
    enforce_tenant_isolation=True,      # Multi-tenant support (P0-2)
    enable_graph_limits=True,           # DoS protection (P0-5)
)
```

**EnhancedReBACManager Features:**

| Feature | Description | P0 Fix |
|---------|-------------|--------|
| **Consistency Levels** | Control cache behavior (EVENTUAL, BOUNDED, STRONG) | P0-1 |
| **Version Tokens** | Monotonic version counter for consistency tracking | P0-1 |
| **Tenant Isolation** | Enforces same-tenant relationships | P0-2 |
| **Graph Limits** | Prevents DoS with timeouts and resource limits | P0-5 |
| **Traversal Stats** | Monitoring metrics for graph operations | P0-5 |

**Class Hierarchy:**
```
EnhancedReBACManager
    ↓ extends
TenantAwareReBACManager (P0-2: Tenant Isolation)
    ↓ extends
ReBACManager (Core Zanzibar implementation)
```

### EnhancedPermissionEnforcer Configuration

**Note:** Nexus also uses `EnhancedPermissionEnforcer` for scoped admin bypass (P0-4).

```python
from nexus.core.permissions_enhanced import EnhancedPermissionEnforcer, AuditStore

# Create audit store
audit_store = AuditStore(engine=db_engine)

# Create enforcer with P0-4 features
enforcer = EnhancedPermissionEnforcer(
    metadata_store=metadata,
    rebac_manager=rebac,
    allow_admin_bypass=True,      # Kill-switch for admin bypass
    allow_system_bypass=True,     # Kill-switch for system bypass
    audit_store=audit_store        # Immutable audit logging
)
```

**EnhancedPermissionEnforcer Features:**

| Feature | Description | P0 Fix |
|---------|-------------|--------|
| **Admin Capabilities** | Scoped admin access (not blanket bypass) | P0-4 |
| **Audit Logging** | Immutable log of all bypass attempts | P0-4 |
| **Kill-Switch** | Disable admin/system bypass globally | P0-4 |
| **Path-Scoped Bypass** | Limit admin access to specific paths | P0-4 |

**Full Stack:**
```
NexusFS
    ↓
EnhancedPermissionEnforcer (P0-4: Scoped admin bypass)
    ↓
EnhancedReBACManager (P0-1, P0-2, P0-5)
    ↓
Database + Cache
```

### Namespace Configuration

**Default File Namespace:**
```python
FILE_NAMESPACE = {
    "name": "file",
    "relations": {
        "owner": {
            "union": ["direct_owner", "parent_owner"]
        },
        "direct_owner": {},
        "parent_owner": {
            "tupleToUserset": {
                "tupleset": "parent",
                "computedUserset": "owner"
            }
        },
        "viewer": {
            "union": ["owner", "direct_viewer"]
        },
        "direct_viewer": {},
        "editor": {
            "union": ["owner", "direct_editor"]
        },
        "direct_editor": {}
    }
}
```

**Custom Namespace:**
```python
# Create custom namespace for your domain
BILLING_NAMESPACE = {
    "name": "billing",
    "relations": {
        "admin": {},
        "accountant": {
            "union": ["admin", "direct_accountant"]
        },
        "viewer": {
            "union": ["accountant", "direct_viewer"]
        }
    }
}

# Register namespace
nx._rebac_manager.create_namespace(BILLING_NAMESPACE)
```

---

## Multi-Tenant Support

### Tenant Isolation

**Every permission operation is tenant-scoped:**

```python
# Tenant A - Acme Corp
nx.rebac_create(
    subject=("user", "alice"),
    relation="direct_owner",
    object=("file", "/workspace/doc.txt"),
    tenant_id="acme"
)

# Tenant B - TechCorp
nx.rebac_create(
    subject=("user", "alice"),  # Same user, different tenant!
    relation="direct_owner",
    object=("file", "/workspace/doc.txt"),  # Same path!
    tenant_id="techcorp"
)

# Queries are tenant-scoped
acme_access = nx.rebac_check(
    subject=("user", "alice"),
    permission="write",
    object=("file", "/workspace/doc.txt"),
    tenant_id="acme"
)
# → True

techcorp_access = nx.rebac_check(
    subject=("user", "alice"),
    permission="write",
    object=("file", "/workspace/doc.txt"),
    tenant_id="techcorp"
)
# → True (but different file!)
```

### Cross-Tenant Protection

**Attempts to create cross-tenant relationships are rejected:**

```python
# This will fail - subject and object from different tenants
try:
    nx.rebac_create(
        subject=("user", "alice"),  # From tenant "acme"
        relation="viewer",
        object=("file", "/workspace/doc.txt"),  # From tenant "techcorp"
        tenant_id="acme"
    )
except ValidationError:
    print("Cross-tenant relationship not allowed")
```

### Tenant-Scoped Caching

**Cache entries are isolated by tenant:**

```python
# Cache key includes tenant_id
cache_key = (
    subject_type, subject_id,
    permission,
    object_type, object_id,
    tenant_id  # ← Tenant isolation
)

# Cache hits only for same tenant
```

---

## CLI Commands

### ReBAC Commands

```bash
# Create relationship
nexus rebac create \
    --subject user:alice \
    --relation direct_owner \
    --object file:/workspace/doc.txt \
    --tenant acme

# Alternative positional syntax
nexus rebac create user alice direct_owner file /workspace/doc.txt

# Check permission
nexus rebac check \
    --subject user:alice \
    --permission write \
    --object file:/workspace/doc.txt \
    --tenant acme

# Output: ✅ GRANTED or ❌ DENIED

# Expand (find all subjects with permission)
nexus rebac expand \
    --permission write \
    --object file:/workspace/doc.txt \
    --tenant acme

# Output: Table with subjects who have write access

# Delete relationship
nexus rebac delete <tuple-id>
```

### File Operations with Subject

```bash
# Set default subject via environment
export NEXUS_SUBJECT_TYPE=user
export NEXUS_SUBJECT_ID=alice
export NEXUS_TENANT_ID=acme

# File operations respect permissions
nexus read /workspace/doc.txt
nexus write /workspace/doc.txt --content "Hello World"
nexus delete /workspace/doc.txt

# Override subject for specific operation
nexus read /workspace/doc.txt \
    --subject-type user \
    --subject-id bob
```

### Admin Operations

```bash
# Bypass permission checks (admin mode)
export NEXUS_IS_ADMIN=true

# Or per-command
nexus write /workspace/doc.txt --content "..." --is-admin
```

---

## Security & Best Practices

### 1. Principle of Least Privilege

```python
# ❌ Bad: Grant owner when viewer is enough
nx.rebac_create(("user", "contractor"), "direct_owner", ("file", "/sensitive.txt"))

# ✅ Good: Grant minimum required permission
nx.rebac_create(("user", "contractor"), "direct_viewer", ("file", "/sensitive.txt"))
```

### 2. Use Groups for Team Access

```python
# ❌ Bad: Grant individual access to each team member
nx.rebac_create(("user", "alice"), "direct_editor", ("file", "/team-doc.txt"))
nx.rebac_create(("user", "bob"), "direct_editor", ("file", "/team-doc.txt"))
nx.rebac_create(("user", "charlie"), "direct_editor", ("file", "/team-doc.txt"))

# ✅ Good: Use groups
nx.rebac_create(("user", "alice"), "member", ("group", "backend-team"))
nx.rebac_create(("user", "bob"), "member", ("group", "backend-team"))
nx.rebac_create(("user", "charlie"), "member", ("group", "backend-team"))
nx.rebac_create(("group", "backend-team"), "direct_editor", ("file", "/team-doc.txt"))
```

### 3. Temporary Access with Expiry

```python
from datetime import datetime, timedelta, UTC

# ✅ Good: Time-limited access for contractors
nx.rebac_create(
    subject=("user", "contractor"),
    relation="direct_viewer",
    object=("file", "/project-docs/"),
    expires_at=datetime.now(UTC) + timedelta(days=30)
)

# Cleanup expired tuples periodically
nx._rebac_manager.cleanup_expired_tuples()
```

### 4. Audit Trail Monitoring

```python
# Query permission changes
from nexus.storage.models import ReBACChangelogModel

with session_factory() as session:
    changes = session.query(ReBACChangelogModel).filter(
        ReBACChangelogModel.object_id == "/sensitive/file.txt"
    ).order_by(ReBACChangelogModel.created_at.desc()).all()

    for change in changes:
        print(f"{change.created_at}: {change.change_type} - "
              f"{change.subject_id} → {change.object_id}")
```

### 5. Tenant Isolation

```python
# ✅ Always specify tenant_id
nx.rebac_create(
    subject=("user", "alice"),
    relation="owner",
    object=("file", "/doc.txt"),
    tenant_id="acme"  # Required!
)

# ❌ Never mix tenants
# This will fail with ValidationError
nx.rebac_create(
    subject=("user", "alice"),  # tenant_id="acme"
    relation="viewer",
    object=("file", "/doc.txt"),  # tenant_id="techcorp"
    tenant_id="acme"
)
```

### 6. Subject Validation

```python
# ✅ Extract subject from authenticated token
auth_result = await auth_provider.authenticate(token)
subject = (auth_result.subject_type, auth_result.subject_id)
tenant_id = auth_result.tenant_id

# Use validated subject
nx.rebac_check(
    subject=subject,
    permission="write",
    object=("file", "/doc.txt"),
    tenant_id=tenant_id
)

# ❌ Never trust client-provided subjects without authentication
```

### 7. Admin Operations

```python
# Use dedicated service account for admin operations
bootstrap_subject = ("service", "bootstrap")

# Initial setup with admin bypass
nx.write(
    "/workspace/system/config.yaml",
    config_content,
    subject=bootstrap_subject,
    is_admin=True  # Bypass permissions
)

# Then grant specific access
nx.rebac_create(
    subject=("user", "admin"),
    relation="direct_owner",
    object=("file", "/workspace/system/config.yaml")
)
```

### 8. Keep Graphs Shallow

```python
# ❌ Bad: Deep nesting (slow traversal)
nx.rebac_create(("user", "alice"), "member", ("team", "backend"))
nx.rebac_create(("team", "backend"), "part_of", ("dept", "engineering"))
nx.rebac_create(("dept", "engineering"), "part_of", ("division", "tech"))
nx.rebac_create(("division", "tech"), "part_of", ("company", "acme"))
nx.rebac_create(("company", "acme"), "owner", ("file", "/doc.txt"))
# 5-level traversal required!

# ✅ Good: Flatten when possible
nx.rebac_create(("team", "backend"), "owner", ("file", "/doc.txt"))
nx.rebac_create(("user", "alice"), "member", ("team", "backend"))
# 2-level traversal
```

---

## Migration Guide

### From v0.5.x (UNIX Permissions) to v0.6.0 (ReBAC)

**Breaking Changes:**

| Feature | v0.5.x | v0.6.0 | Status |
|---------|--------|--------|--------|
| UNIX Permissions | `chmod()`, `chown()` | Removed | ❌ |
| ACLs | `setfacl()`, `getfacl()` | Removed | ❌ |
| Permission Model | UNIX mode bits (0o755) | ReBAC relationships | ✅ |
| Subject Parameter | Optional | Required for all ops | ✅ |

**Migration Steps:**

#### 1. Extract Existing Permissions

```python
# v0.5.x: Get UNIX permissions
metadata = nx.metadata("/workspace/doc.txt")
owner = metadata["owner"]
group = metadata["group"]
mode = metadata["mode"]

print(f"Owner: {owner}")
print(f"Group: {group}")
print(f"Mode: {oct(mode)}")  # e.g., 0o644
```

#### 2. Convert to ReBAC Relationships

```python
# v0.6.0: Create equivalent ReBAC tuples

# Owner gets full access
nx.rebac_create(
    subject=("user", owner),
    relation="direct_owner",
    object=("file", "/workspace/doc.txt")
)

# If group had read permission (mode & 0o040)
if mode & 0o040:
    nx.rebac_create(
        subject=("group", group),
        relation="direct_viewer",
        object=("file", "/workspace/doc.txt")
    )

# If group had write permission (mode & 0o020)
if mode & 0o020:
    nx.rebac_create(
        subject=("group", group),
        relation="direct_editor",
        object=("file", "/workspace/doc.txt")
    )
```

#### 3. Update File Operations

```python
# v0.5.x: Implicit subject from config
nx = connect({"agent_id": "alice", "tenant_id": "acme"})
nx.write("/workspace/doc.txt", b"content")

# v0.6.0: Explicit subject per operation
nx = connect()
nx.write(
    "/workspace/doc.txt",
    b"content",
    subject=("user", "alice"),
    tenant_id="acme"
)
```

#### 4. Update Permission Checks

```python
# v0.5.x: UNIX mode checks
if metadata["mode"] & 0o400:  # Owner can read
    print("Owner has read access")

# v0.6.0: ReBAC checks
if nx.rebac_check(
    subject=("user", owner),
    permission="read",
    object=("file", "/workspace/doc.txt")
):
    print("Owner has read access")
```

#### 5. Migration Script

```python
def migrate_unix_to_rebac(nx):
    """Migrate all UNIX permissions to ReBAC."""

    # 1. Get all files
    files = nx.glob("/workspace/**/*")

    for file_path in files:
        # 2. Get UNIX metadata
        metadata = nx.metadata(file_path)
        owner = metadata.get("owner")
        group = metadata.get("group")
        mode = metadata.get("mode", 0)

        if not owner:
            continue

        # 3. Create owner relationship
        nx.rebac_create(
            subject=("user", owner),
            relation="direct_owner",
            object=("file", file_path)
        )

        # 4. Create group relationships
        if group:
            # Group read
            if mode & 0o040:
                nx.rebac_create(
                    subject=("group", group),
                    relation="direct_viewer",
                    object=("file", file_path)
                )
            # Group write
            if mode & 0o020:
                nx.rebac_create(
                    subject=("group", group),
                    relation="direct_editor",
                    object=("file", file_path)
                )

    print("Migration complete!")
```

---

## Performance

### Time Complexity

| Operation | Best Case | Average Case | Worst Case | Notes |
|-----------|-----------|--------------|-----------|-------|
| `rebac_create()` | O(1) | O(1) | O(1) | Direct DB insert |
| `rebac_check()` (cached) | O(1) | O(1) | O(1) | Cache lookup |
| `rebac_check()` (uncached) | O(d) | O(n*d) | O(n^d) | n=relations, d=depth |
| `rebac_delete()` | O(1) | O(1) | O(1) | Direct DB delete |
| `rebac_expand()` | O(n) | O(n*d) | O(n^d) | Recursive expansion |

### Caching Impact

**Cache Hit Rates:**
- Typical workload: 80-90% cache hits
- Cache TTL: 5 minutes (configurable)
- Invalidation: On write/delete to related tuples

**Performance Example:**
```
Without cache:
- 100 permission checks
- 100 graph traversals × 50ms each
- Total: 5000ms (5 seconds)

With cache (90% hit rate):
- 90 cache hits × 1ms = 90ms
- 10 cache misses × 50ms = 500ms
- Total: 590ms (0.6 seconds)

Speed-up: 8.5x faster!
```

### Optimization Strategies

#### 1. Increase Cache TTL for Stable Permissions

```python
nx = connect({
    "cache_ttl_seconds": 3600  # 1 hour for rarely-changing permissions
})
```

#### 2. Use Direct Relations

```python
# ✅ Fast: Direct relationship
nx.rebac_create(("user", "alice"), "direct_owner", ("file", "/doc.txt"))

# ❌ Slow: Multiple hops
nx.rebac_create(("user", "alice"), "member", ("team", "backend"))
nx.rebac_create(("team", "backend"), "part_of", ("dept", "eng"))
nx.rebac_create(("dept", "eng"), "owner", ("file", "/doc.txt"))
```

#### 3. Batch Permission Checks

```python
# ❌ Slow: Individual checks
for file in files:
    if nx.rebac_check(("user", "alice"), "read", ("file", file)):
        process(file)

# ✅ Fast: Batch expand
allowed_files = nx.rebac_expand("read", ("user", "alice"))
for file in files:
    if ("file", file) in allowed_files:
        process(file)
```

#### 4. Cleanup Expired Tuples

```python
# Run periodically (e.g., daily cron job)
nx._rebac_manager.cleanup_expired_tuples()

# Reduces graph size → faster traversal
```

#### 5. Monitor Graph Depth

```python
# Set reasonable depth limit
nx._rebac_manager.max_depth = 10  # Default

# Log when depth limit hit
# Indicates overly complex permission graph
```

### Benchmark Results

**Test Setup:**
- SQLite database
- 1000 files
- 100 users
- 10 groups
- 50 relationships per user

**Results:**

| Operation | Time (avg) | Throughput |
|-----------|------------|------------|
| `rebac_create()` | 5ms | 200 ops/sec |
| `rebac_check()` (cached) | 1ms | 1000 ops/sec |
| `rebac_check()` (uncached, depth=1) | 10ms | 100 ops/sec |
| `rebac_check()` (uncached, depth=3) | 50ms | 20 ops/sec |
| `rebac_expand()` (10 subjects) | 100ms | 10 ops/sec |
| `rebac_delete()` | 5ms | 200 ops/sec |

**PostgreSQL is 2-3x faster for large datasets (>100k tuples).**

---

## Production Features (P0 Enhancements)

Nexus uses `EnhancedReBACManager` which includes critical production features for GA (General Availability).

### P0-1: Consistency Levels

**Problem:** Cached permission checks may be stale (up to 5 minutes).

**Solution:** Three consistency levels for different use cases.

```python
from nexus.core.rebac_manager_enhanced import ConsistencyLevel, CheckResult

# EVENTUAL: Use cache (default, fastest)
result = nx._rebac_manager.rebac_check(
    subject=("user", "alice"),
    permission="read",
    object=("file", "/doc.txt"),
    consistency=ConsistencyLevel.EVENTUAL  # Up to 5min staleness
)

# BOUNDED: Max 1 second staleness
result = nx._rebac_manager.rebac_check(
    subject=("user", "alice"),
    permission="read",
    object=("file", "/doc.txt"),
    consistency=ConsistencyLevel.BOUNDED  # Fresh within 1s
)

# STRONG: Always fresh (bypass cache)
result = nx._rebac_manager.rebac_check(
    subject=("user", "alice"),
    permission="write",
    object=("file", "/sensitive.txt"),
    consistency=ConsistencyLevel.STRONG  # No cache, slowest
)

# Result includes metadata
print(f"Allowed: {result.allowed}")
print(f"Version: {result.consistency_token}")  # e.g., "v123"
print(f"Decision time: {result.decision_time_ms}ms")
print(f"From cache: {result.cached}")
if result.cached:
    print(f"Cache age: {result.cache_age_ms}ms")
```

**When to Use:**

| Level | Use Case | Performance | Staleness |
|-------|----------|-------------|-----------|
| **EVENTUAL** | Normal operations | Fastest (1ms) | Up to 5min |
| **BOUNDED** | Financial transactions | Medium (10ms) | Max 1s |
| **STRONG** | Security-critical | Slowest (50ms) | 0s (fresh) |

### P0-4: Scoped Admin Bypass

**Problem:** Blanket admin bypass is too broad and creates security risks.

**Solution:** Admin capabilities with granular scoping and audit logging.

```python
from nexus.core.permissions_enhanced import (
    EnhancedPermissionEnforcer,
    EnhancedOperationContext,
    AdminCapability
)

# Create context with specific capabilities
context = EnhancedOperationContext(
    user="admin",
    groups=["administrators"],
    is_admin=True,
    admin_capabilities={
        AdminCapability.READ_SYSTEM,   # Can read /system/* only
        AdminCapability.WRITE_SYSTEM,  # Can write /system/* only
    }
)

# Admin can access /system paths
enforcer = EnhancedPermissionEnforcer(
    metadata_store=metadata,
    rebac_manager=rebac,
    allow_admin_bypass=True,  # Kill-switch enabled
    audit_store=audit_store
)

# Allowed: /system path with matching capability
can_read_system = enforcer.check(
    "/system/config.yaml",
    Permission.READ,
    context
)
# → True (has READ_SYSTEM capability)

# Denied: non-system path without wildcard
can_read_user_file = enforcer.check(
    "/workspace/user-data.txt",
    Permission.READ,
    context
)
# → False (no READ_ALL capability)
```

**Available Admin Capabilities:**

| Capability | Scope | Risk Level |
|-----------|-------|------------|
| `BOOTSTRAP` | One-time initial setup | Critical |
| `READ_SYSTEM` | Read /system/* paths only | Medium |
| `READ_ALL` | Read any file | High |
| `WRITE_SYSTEM` | Write /system/* paths only | High |
| `WRITE_ALL` | Write any file | Critical |
| `DELETE_SYSTEM` | Delete /system/* paths | High |
| `DELETE_ANY` | Delete any file | Critical |
| `MANAGE_REBAC` | Manage permissions | Critical |
| `MANAGE_TENANTS` | Manage tenant isolation | Critical |

**Audit Logging:**

Every admin/system bypass is logged to an immutable audit table:

```python
# All bypass attempts are automatically logged
# Query audit trail
from datetime import UTC, datetime, timedelta

audit_entries = audit_store.query_bypasses(
    user="admin",
    start_time=datetime.now(UTC) - timedelta(days=7),
    limit=100
)

for entry in audit_entries:
    print(f"{entry['timestamp']}: {entry['user']} "
          f"{entry['bypass_type']} {entry['permission']} "
          f"{entry['path']} → {entry['allowed']}")
```

**Audit Log Entry:**
```python
{
    "timestamp": "2025-10-24T10:30:00Z",
    "request_id": "req-abc-123",
    "user_id": "admin",
    "tenant_id": "org_acme",
    "path": "/system/config.yaml",
    "permission": "read",
    "bypass_type": "admin",
    "allowed": True,
    "capabilities": ["admin:read:/system/*"],
    "denial_reason": None
}
```

**Kill-Switch:**

```python
# Disable admin bypass globally (emergency)
enforcer = EnhancedPermissionEnforcer(
    allow_admin_bypass=False,  # ← No admin bypass allowed
    allow_system_bypass=False  # ← No system bypass allowed
)

# Even admins must have explicit permissions
context = EnhancedOperationContext(
    user="admin",
    is_admin=True,
    admin_capabilities={AdminCapability.WRITE_ALL}
)

can_write = enforcer.check(
    "/workspace/file.txt",
    Permission.WRITE,
    context
)
# → False (kill-switch disabled bypass)
```

**Best Practices:**

1. **Principle of Least Privilege**: Grant minimum required capabilities
2. **Scope to Paths**: Use `_SYSTEM` over `_ALL` when possible
3. **Audit Regularly**: Monitor `admin_bypass_audit` table
4. **Rotate Capabilities**: Change admin capabilities periodically
5. **Use Bootstrap Sparingly**: Only for initial system setup

### P0-2: Tenant Isolation

**Problem:** Permission graphs could leak across tenants.

**Solution:** Enforced tenant scoping at all levels.

```python
# Create relationships - enforces same tenant
nx.rebac_create(
    subject=("user", "alice"),        # tenant_id="acme"
    relation="owner",
    object=("file", "/doc.txt"),      # tenant_id="acme"
    tenant_id="acme"
)

# Cross-tenant relationships rejected
try:
    nx.rebac_create(
        subject=("user", "alice"),    # tenant_id="acme"
        relation="viewer",
        object=("file", "/doc.txt"),  # tenant_id="techcorp"
        tenant_id="acme"
    )
except ValidationError as e:
    print(f"Rejected: {e}")
    # → "Cross-tenant relationship not allowed"

# Permission checks are tenant-scoped
acme_result = nx.rebac_check(
    subject=("user", "alice"),
    permission="read",
    object=("file", "/doc.txt"),
    tenant_id="acme"
)

techcorp_result = nx.rebac_check(
    subject=("user", "alice"),
    permission="read",
    object=("file", "/doc.txt"),
    tenant_id="techcorp"
)
# → Different results for different tenants!
```

**Protection Mechanisms:**

1. **Write-time validation**: Rejects cross-tenant tuples
2. **Query-time filtering**: All SQL queries include `WHERE tenant_id = ?`
3. **Cache isolation**: Cache keys include tenant_id
4. **Graph traversal**: Cannot follow edges across tenants

### P0-5: Graph Limits (DoS Protection)

**Problem:** Malicious or complex graphs could cause timeouts or memory exhaustion.

**Solution:** Hard limits on graph traversal.

```python
from nexus.core.rebac_manager_enhanced import GraphLimits, GraphLimitExceeded

# Limits are automatically enforced
try:
    result = nx.rebac_check(
        subject=("user", "attacker"),
        permission="read",
        object=("file", "/doc.txt")
    )
except GraphLimitExceeded as e:
    print(f"Limit exceeded: {e.limit_type}")
    print(f"Limit: {e.limit_value}")
    print(f"Actual: {e.actual_value}")

    # Convert to HTTP error
    error = e.to_http_error()
    # → {"code": 429, "message": "Graph depth limit exceeded", ...}
```

**Default Limits:**

| Limit | Default | Purpose |
|-------|---------|---------|
| `MAX_DEPTH` | 10 hops | Prevent infinite recursion |
| `MAX_FAN_OUT` | 1,000 edges | Limit union expansion |
| `MAX_EXECUTION_TIME_MS` | 100ms | Hard timeout |
| `MAX_VISITED_NODES` | 10,000 nodes | Memory bound |
| `MAX_TUPLE_QUERIES` | 100 queries | Database query limit |

**Traversal Statistics:**

```python
result = nx._rebac_manager.rebac_check(
    subject=("user", "alice"),
    permission="read",
    object=("file", "/doc.txt")
)

# Inspect traversal metrics
stats = result.traversal_stats
print(f"Queries: {stats.queries}")              # DB queries made
print(f"Nodes visited: {stats.nodes_visited}")  # Graph nodes traversed
print(f"Max depth: {stats.max_depth_reached}")  # Deepest path
print(f"Cache hits: {stats.cache_hits}")        # Cache utilization
print(f"Duration: {stats.duration_ms}ms")       # Total time
```

**Monitoring Alerts:**

```python
# Monitor for graph complexity
if result.traversal_stats.queries > 50:
    logging.warning(
        f"Complex permission check: {result.traversal_stats.queries} queries"
    )

# Alert on timeout approaches
if result.decision_time_ms > 80:  # Close to 100ms limit
    logging.warning(
        f"Slow permission check: {result.decision_time_ms}ms"
    )
```

### Version Tokens

**Problem:** Difficult to track consistency across distributed systems.

**Solution:** Monotonic version tokens for each check.

```python
# First check
result1 = nx._rebac_manager.rebac_check(...)
token1 = result1.consistency_token  # "v123"

# Permission modified
nx.rebac_create(...)

# Second check
result2 = nx._rebac_manager.rebac_check(...)
token2 = result2.consistency_token  # "v124"

# Token increased → permissions changed
assert int(token2[1:]) > int(token1[1:])
```

**Use Cases:**
- Compare permission versions across services
- Detect stale caches in distributed systems
- Audit trail with version tracking
- Optimistic concurrency control

---

## Troubleshooting

### Common Issues

#### 1. Permission Denied Despite Ownership

**Problem:**
```python
nx.rebac_create(("user", "alice"), "direct_owner", ("file", "/doc.txt"))
can_write = nx.rebac_check(("user", "alice"), "write", ("file", "/doc.txt"))
# → False (expected True)
```

**Solution:**
Check namespace configuration. `owner` must include `write` permission:
```python
namespace = nx._rebac_manager.get_namespace("file")
# Ensure: "owner": {"can": ["read", "write", "execute"]}
```

#### 2. Slow Permission Checks

**Problem:**
```python
# Takes 5+ seconds
can_access = nx.rebac_check(("user", "alice"), "read", ("file", "/doc.txt"))
```

**Solutions:**
- Check graph depth: `max_depth=10` exceeded?
- Enable caching: `cache_ttl_seconds=300`
- Simplify relationship graph
- Add database indexes on `rebac_tuples` table

#### 3. Cross-Tenant Access

**Problem:**
```python
# Works in tenant A, fails in tenant B
nx.rebac_check(..., tenant_id="tenant_a")  # → True
nx.rebac_check(..., tenant_id="tenant_b")  # → False
```

**Solution:**
Permissions are tenant-scoped. Create separate relationships for each tenant.

#### 4. Cache Staleness

**Problem:**
```python
nx.rebac_delete(tuple_id)  # Delete permission
can_access = nx.rebac_check(...)  # Still returns True (cached)
```

**Solution:**
Cache is invalidated on write/delete. If still stale:
- Reduce `cache_ttl_seconds`
- Manual invalidation: `nx._rebac_manager._cache.clear()`

---

## Further Reading

**Core Implementations:**
- `src/nexus/core/rebac_manager.py` - Base Zanzibar implementation
- `src/nexus/core/rebac_manager_tenant_aware.py` - P0-2 tenant isolation
- `src/nexus/core/rebac_manager_enhanced.py` - P0-1, P0-5 (production-ready)
- `src/nexus/core/permissions.py` - Base permission enforcer
- `src/nexus/core/permissions_enhanced.py` - P0-4 scoped admin bypass

**Examples:**
- `examples/py_demo/rebac_demo.py` - Comprehensive ReBAC examples
- `examples/py_demo/multi_tenant_rebac_demo.py` - Multi-tenant usage
- `examples/script_demo/rebac_demo.sh` - CLI examples

**Documentation:**
- `docs/design/auth-system.md` - Authentication (who you are)
- `docs/design/permission-system.md` - Authorization (what you can do)
- `P0_IMPLEMENTATION_STATUS.md` - Production readiness tracking

**Research:**
- Google Zanzibar Paper: https://research.google/pubs/pub48190/

---

## Summary

**Key Takeaways:**

1. ✅ **ReBAC = Relationships**: Permissions are relationships in a graph
2. ✅ **Works in Both Modes**: Embedded (direct) and Server (RPC)
3. ✅ **Subject-Based**: Identity per operation, not per connection
4. ✅ **Multi-Tenant**: Complete isolation via tenant_id (P0-2)
5. ✅ **Scalable**: Proven to billions of relationships (Google Zanzibar)
6. ✅ **Flexible**: Model any permission structure (groups, hierarchies, expiry)
7. ✅ **Auditable**: Every change logged to changelog
8. ✅ **Cached**: 80-90% cache hit rate for performance
9. ✅ **Production-Ready**: Enhanced components with P0 fixes
   - **EnhancedReBACManager**: Consistency levels, graph limits, tenant isolation
   - **EnhancedPermissionEnforcer**: Scoped admin bypass, audit logging

**When to Use ReBAC:**
- ✅ Multi-user applications
- ✅ Complex organizational structures
- ✅ Dynamic permissions (team changes, temporary access)
- ✅ Audit requirements
- ✅ Multi-tenant SaaS

**When NOT to Use:**
- ❌ Single-user applications (just use admin mode)
- ❌ Public data (no access control needed)
- ❌ Simple read-only systems

---

**Version History:**
- v2.0 (2025-10-24): Comprehensive implementation guide
- v1.0 (2025-01-24): Initial ReBAC design (Zanzibar-based)
