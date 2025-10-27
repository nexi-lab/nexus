# Multi-Tenant Architecture

**Complete guide to implementing multi-tenancy in Nexus**

---

## Table of Contents

1. [Overview](#overview)
2. [Approach 1: Simple Permissions-Based](#approach-1-simple-permissions-based)
3. [Approach 2: Canonical Architecture](#approach-2-canonical-architecture)
4. [Choosing an Approach](#choosing-an-approach)
5. [Implementation Guide](#implementation-guide)
6. [Security & Isolation](#security--isolation)
7. [Best Practices](#best-practices)

---

## Overview

Nexus supports multi-tenancy through two complementary approaches:

1. **Simple Permissions-Based** - Flat structure with ReBAC permissions
2. **Canonical Architecture** - Hierarchical structure with workspace isolation

Both approaches provide complete tenant isolation using Nexus's ReBAC system.

---

## Approach 1: Simple Permissions-Based

Flat directory structure with permission-based access control.

## Core Principles

1. **Simple directory structure** - Organizations → Workspaces → Resources/Memory
2. **Permissions control access** - Use ReBAC instead of reference files
3. **Config-driven setup** - Define structure in config files
4. **Dynamic user management** - Add users via API while server is running

## Directory Structure

```
/orgs/
├── acme/
│   ├── org.json                   # Org metadata and member list
│   ├── sales/                     # Workspace (just a folder)
│   │   ├── context.json          # Workspace memory
│   │   ├── decisions.json        # Team decisions
│   │   ├── documents/            # Static resources
│   │   └── conversations/        # Session data
│   ├── engineering/
│   │   ├── context.json
│   │   └── code/
│   └── shared/                    # Org-wide resources
│       ├── knowledge-base/
│       └── templates/
└── techcorp/
    └── ...
```

**That's it!** No `/users/`, `/agents/`, or reference files needed.

## Configuration-Based Setup

### 1. Nexus Config File (`nexus.yaml`)

Define your multi-tenant structure in config:

```yaml
# nexus.yaml
data_dir: /var/lib/nexus
backend: local

# Multi-tenant configuration
organizations:
  - id: acme
    name: Acme Corporation
    workspaces:
      - id: sales
        name: Sales Team
      - id: engineering
        name: Engineering Team

    # Initial members (can add more via API later)
    members:
      - user_id: alice@acme.com
        role: admin
        permissions:
          - orgs/acme:rwx
          - orgs/acme/sales:rwx
          - orgs/acme/engineering:rwx

      - user_id: bob@acme.com
        role: member
        permissions:
          - orgs/acme/sales:rw-

      - user_id: agent_sales_001
        type: agent
        role: assistant
        permissions:
          - orgs/acme/sales:rw-

  - id: techcorp
    name: Tech Corp Inc.
    workspaces:
      - id: product
        name: Product Team
    members:
      - user_id: john@techcorp.com
        role: admin
        permissions:
          - orgs/techcorp:rwx

# Authentication (optional - integrate with your auth system)
auth:
  type: jwt  # or api_key, oauth, etc.
  jwt_secret: ${JWT_SECRET}
  jwt_algorithm: HS256
```

### 2. Initialize from Config

```bash
# One-time setup: creates directory structure and sets permissions
nexus init --config nexus.yaml

# This creates:
# - /orgs/acme/ with workspaces
# - /orgs/techcorp/ with workspaces
# - Sets all ACLs based on config
```

### 3. Start Server with Config

```bash
nexus serve --config nexus.yaml --host 0.0.0.0 --port 8080
```

## Dynamic User Management

### Adding Users While Server is Running

#### Option 1: Via Python API

```python
from nexus import RemoteNexusClient

# Connect to running server
client = RemoteNexusClient(
    host="localhost",
    port=8080,
    api_key="admin-api-key"  # Admin credentials
)

# Add new user to organization
def add_user_to_org(user_email, org_id, role="member", workspaces=None):
    """Add a user to an organization dynamically"""

    # 1. Update org.json to include new member
    org_config = client.read(f"/orgs/{org_id}/org.json")
    org_data = json.loads(org_config)

    org_data["members"].append({
        "user_id": user_email,
        "role": role,
        "joined_at": datetime.utcnow().isoformat() + "Z",
        "status": "active"
    })

    client.write(f"/orgs/{org_id}/org.json", json.dumps(org_data, indent=2))

    # 2. Set permissions based on role
    if role == "admin":
        # Admin gets full access to org
        client.setfacl(f"/orgs/{org_id}", f"user:{user_email}:rwx")
    else:
        # Members get workspace-specific access
        for workspace in (workspaces or []):
            client.setfacl(
                f"/orgs/{org_id}/{workspace}",
                f"user:{user_email}:rw-"
            )

    return {"success": True, "user": user_email, "org": org_id}

# Usage
add_user_to_org(
    user_email="charlie@acme.com",
    org_id="acme",
    role="member",
    workspaces=["sales"]
)
```

#### Option 2: Via REST API

If you expose a management endpoint:

```bash
# POST /api/orgs/{org_id}/members
curl -X POST http://localhost:8080/api/orgs/acme/members \
  -H "Authorization: Bearer admin-token" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "charlie@acme.com",
    "role": "member",
    "workspaces": ["sales"]
  }'
```

#### Option 3: Via CLI (Server Running)

```bash
# Using remote client
NEXUS_HOST=localhost NEXUS_PORT=8080 nexus admin add-user \
  --org acme \
  --user charlie@acme.com \
  --role member \
  --workspaces sales
```

### Removing Users

```python
def remove_user_from_org(user_email, org_id):
    """Remove user from organization"""

    # 1. Update org.json
    org_config = client.read(f"/orgs/{org_id}/org.json")
    org_data = json.loads(org_config)

    org_data["members"] = [
        m for m in org_data["members"]
        if m["user_id"] != user_email
    ]

    client.write(f"/orgs/{org_id}/org.json", json.dumps(org_data, indent=2))

    # 2. Remove all permissions
    client.setfacl(f"/orgs/{org_id}", f"user:{user_email}:---")

    # Also remove from all workspaces
    workspaces = client.ls(f"/orgs/{org_id}")
    for ws in workspaces:
        if ws.is_dir:
            client.setfacl(f"/orgs/{org_id}/{ws.name}", f"user:{user_email}:---")
```

## Permission-Based Access

### Using ACLs (Access Control Lists)

```bash
# Grant org-level access
nexus setfacl /orgs/acme user:alice@acme.com:rwx

# Grant workspace access
nexus setfacl /orgs/acme/sales user:bob@acme.com:rw-

# Grant agent access
nexus setfacl /orgs/acme/sales agent:sales_bot_001:rw-

# View permissions
nexus getfacl /orgs/acme/sales
```

### Using ReBAC (Relationship-Based Access Control)

For more complex permission models:

```bash
# Define relationships
nexus rebac create user:alice@acme.com admin /orgs/acme
nexus rebac create user:bob@acme.com member /orgs/acme/sales
nexus rebac create agent:sales_bot_001 assistant /orgs/acme/sales

# Check permissions
nexus rebac check user:bob@acme.com write /orgs/acme/sales/documents/
# Returns: true

nexus rebac check user:bob@acme.com write /orgs/acme/engineering/
# Returns: false
```

## Data Schemas

### Organization Config (`org.json`)

```json
{
  "org_id": "acme",
  "name": "Acme Corporation",
  "created_at": "2025-01-15T10:00:00Z",
  "settings": {
    "retention_policy": {
      "conversations": "90d",
      "sessions": "30d"
    }
  },
  "members": [
    {
      "user_id": "alice@acme.com",
      "role": "admin",
      "joined_at": "2025-01-15T10:00:00Z",
      "status": "active"
    },
    {
      "user_id": "bob@acme.com",
      "role": "member",
      "joined_at": "2025-01-16T11:00:00Z",
      "status": "active"
    }
  ]
}
```

### Workspace Context (`context.json`)

```json
{
  "workspace_id": "sales",
  "org_id": "acme",
  "last_updated": "2025-01-20T14:30:00Z",
  "active_topics": ["Q1 planning", "customer onboarding"],
  "current_context": {
    "focus_areas": ["pipeline review", "pricing strategy"],
    "blockers": []
  }
}
```

### Agent Config

Agents don't need separate directories. Just store config in workspace:

```json
// /orgs/acme/sales/agents.json
{
  "agents": [
    {
      "agent_id": "sales_bot_001",
      "agent_type": "claude-sonnet-4.5",
      "config": {
        "temperature": 0.7,
        "max_tokens": 4096
      },
      "created_at": "2025-01-15T14:00:00Z",
      "created_by": "alice@acme.com"
    }
  ]
}
```

## Access Patterns

### User Access

```python
# User authenticates (JWT, OAuth, etc.)
user = authenticate(token)  # Returns: {user_id: "alice@acme.com", ...}

# User lists their accessible orgs
# Nexus automatically filters based on ACLs
orgs = client.ls("/orgs/")  # Returns only orgs user has access to

# User accesses workspace
context = client.read("/orgs/acme/sales/context.json")

# User writes document (permission check automatic)
client.write(
    "/orgs/acme/sales/documents/proposal.md",
    "# Q1 Sales Proposal..."
)
```

### Agent Access

```python
# Agent loads its config
agent_config = client.read("/orgs/acme/sales/agents.json")
my_config = [a for a in agent_config["agents"] if a["agent_id"] == agent_id][0]

# Agent reads workspace context
context = client.read("/orgs/acme/sales/context.json")

# Agent stores conversation (in workspace, not tied to agent instance)
client.write(
    "/orgs/acme/sales/conversations/session_123.json",
    {
        "session_id": "session_123",
        "agent_id": "sales_bot_001",
        "messages": [...],
        "created_at": "2025-01-20T15:00:00Z"
    }
)
```

## Management API Example

Here's a simple admin API you can add to Nexus server:

```python
from fastapi import FastAPI, HTTPException, Depends
from nexus import NexusClient

app = FastAPI()
nexus = NexusClient()

def verify_admin(token: str):
    """Verify user is admin"""
    user = verify_jwt(token)
    # Check if user is admin of any org
    if not is_admin(user["user_id"]):
        raise HTTPException(403, "Admin access required")
    return user

@app.post("/api/orgs/{org_id}/members")
async def add_member(
    org_id: str,
    user_id: str,
    role: str = "member",
    workspaces: list[str] = [],
    admin: dict = Depends(verify_admin)
):
    """Add a new member to an organization"""

    # 1. Update org.json
    org_config = nexus.read(f"/orgs/{org_id}/org.json")
    org_data = json.loads(org_config)

    # Check if user already exists
    if any(m["user_id"] == user_id for m in org_data["members"]):
        raise HTTPException(400, "User already exists in org")

    # Add member
    org_data["members"].append({
        "user_id": user_id,
        "role": role,
        "joined_at": datetime.utcnow().isoformat() + "Z",
        "status": "active",
        "added_by": admin["user_id"]
    })

    nexus.write(f"/orgs/{org_id}/org.json", json.dumps(org_data, indent=2))

    # 2. Set permissions
    if role == "admin":
        nexus.setfacl(f"/orgs/{org_id}", f"user:{user_id}:rwx")
    else:
        for workspace in workspaces:
            nexus.setfacl(f"/orgs/{org_id}/{workspace}", f"user:{user_id}:rw-")

    return {
        "success": True,
        "org_id": org_id,
        "user_id": user_id,
        "role": role,
        "workspaces": workspaces
    }

@app.delete("/api/orgs/{org_id}/members/{user_id}")
async def remove_member(
    org_id: str,
    user_id: str,
    admin: dict = Depends(verify_admin)
):
    """Remove a member from organization"""

    # Update org.json
    org_config = nexus.read(f"/orgs/{org_id}/org.json")
    org_data = json.loads(org_config)

    org_data["members"] = [
        m for m in org_data["members"] if m["user_id"] != user_id
    ]

    nexus.write(f"/orgs/{org_id}/org.json", json.dumps(org_data, indent=2))

    # Remove permissions
    nexus.setfacl(f"/orgs/{org_id}", f"user:{user_id}:---")

    # Remove from all workspaces
    for workspace in nexus.ls(f"/orgs/{org_id}"):
        if workspace.is_dir:
            nexus.setfacl(f"/orgs/{org_id}/{workspace.name}", f"user:{user_id}:---")

    return {"success": True, "removed": user_id}

@app.get("/api/orgs/{org_id}/members")
async def list_members(org_id: str, user: dict = Depends(verify_user)):
    """List organization members"""

    # Check user has access to org
    if not can_access(user["user_id"], f"/orgs/{org_id}"):
        raise HTTPException(403, "Access denied")

    org_config = nexus.read(f"/orgs/{org_id}/org.json")
    org_data = json.loads(org_config)

    return {
        "org_id": org_id,
        "members": org_data["members"]
    }
```

## Initialization Script

Create initial structure from config:

```python
# init_from_config.py
import yaml
from nexus import NexusClient

def init_multi_tenant(config_path="nexus.yaml"):
    """Initialize multi-tenant structure from config"""

    with open(config_path) as f:
        config = yaml.safe_load(f)

    nexus = NexusClient(data_dir=config["data_dir"])

    # Create /orgs parent directory
    nexus.mkdir("/orgs")

    for org in config.get("organizations", []):
        org_id = org["id"]

        # Create org directory
        nexus.mkdir(f"/orgs/{org_id}")

        # Create org.json
        nexus.write(
            f"/orgs/{org_id}/org.json",
            json.dumps({
                "org_id": org_id,
                "name": org["name"],
                "created_at": datetime.utcnow().isoformat() + "Z",
                "settings": org.get("settings", {}),
                "members": []
            }, indent=2)
        )

        # Create workspaces
        for workspace in org.get("workspaces", []):
            ws_id = workspace["id"]
            nexus.mkdir(f"/orgs/{org_id}/{ws_id}")
            nexus.mkdir(f"/orgs/{org_id}/{ws_id}/documents")
            nexus.mkdir(f"/orgs/{org_id}/{ws_id}/conversations")

            # Initialize workspace context
            nexus.write(
                f"/orgs/{org_id}/{ws_id}/context.json",
                json.dumps({
                    "workspace_id": ws_id,
                    "org_id": org_id,
                    "name": workspace["name"],
                    "created_at": datetime.utcnow().isoformat() + "Z",
                    "active_topics": [],
                    "current_context": {}
                }, indent=2)
            )

        # Create shared directory
        nexus.mkdir(f"/orgs/{org_id}/shared")
        nexus.mkdir(f"/orgs/{org_id}/shared/knowledge-base")
        nexus.mkdir(f"/orgs/{org_id}/shared/templates")

        # Add initial members and set permissions
        org_config = json.loads(nexus.read(f"/orgs/{org_id}/org.json"))

        for member in org.get("members", []):
            user_id = member["user_id"]

            # Add to org.json
            org_config["members"].append({
                "user_id": user_id,
                "role": member["role"],
                "type": member.get("type", "human"),
                "joined_at": datetime.utcnow().isoformat() + "Z",
                "status": "active"
            })

            # Set permissions
            for perm in member.get("permissions", []):
                path, mode = perm.split(":")
                nexus.setfacl(f"/{path}", f"user:{user_id}:{mode}")

        # Write updated org.json
        nexus.write(
            f"/orgs/{org_id}/org.json",
            json.dumps(org_config, indent=2)
        )

    print(f"✓ Initialized {len(config['organizations'])} organizations")

if __name__ == "__main__":
    init_multi_tenant()
```

## Comparison: Complex vs Simple

### Old (Overcomplicated)
```
/orgs/acme/members/humans/alice.json  ← Canonical membership
/users/alice/orgs/acme.json           ← Reference file pointing to above
/users/alice/workspaces/acme/sales.json ← Another reference

# User wants to access sales workspace:
1. Read /users/alice/workspaces/acme/sales.json
2. Parse canonical_path
3. Read actual workspace data
```

### New (Simple)
```
/orgs/acme/org.json         ← Membership in members array
/orgs/acme/sales/           ← Workspace (direct access)

# User wants to access sales workspace:
1. Read /orgs/acme/sales/context.json (permission check automatic)
```

## Benefits

1. **Simpler**: No reference files, no complex IDs
2. **Config-driven**: Define structure once in YAML
3. **Dynamic**: Add/remove users via API while running
4. **Standard**: Uses Nexus's built-in ReBAC
5. **Efficient**: Direct access, no indirection
6. **Scalable**: Permissions enforced at filesystem level


## Example: Complete Workflow

```yaml
# 1. Define in nexus.yaml
organizations:
  - id: acme
    name: Acme Corp
    workspaces:
      - id: sales
    members:
      - user_id: alice@acme.com
        role: admin

# 2. Initialize
nexus init --config nexus.yaml

# 3. Start server
nexus serve --config nexus.yaml

# 4. Add user dynamically (server running)
curl -X POST http://localhost:8080/api/orgs/acme/members \
  -H "Authorization: Bearer admin-token" \
  -d '{"user_id": "bob@acme.com", "role": "member", "workspaces": ["sales"]}'

# 5. User accesses data (permissions checked automatically)
curl http://localhost:8080/api/orgs/acme/sales/context.json \
  -H "Authorization: Bearer bob-token"
```


## Approach 2: Canonical Architecture

Hierarchical structure with workspace-based isolation.



## Choosing an Approach

| Feature | Simple | Canonical |
|---------|--------|-----------|
| **Structure** | Flat (`/orgs/tenant/`) | Hierarchical (`/orgs/tenant/workspaces/`) |
| **Complexity** | Lower | Higher |
| **Setup** | Config-driven | API + permissions |
| **Use Case** | Small teams, simple needs | Enterprise, complex hierarchies |
| **Permissions** | ReBAC on files | ReBAC on workspaces + files |
| **Flexibility** | Medium | High |

**Recommendation:**
- Start with **Simple** for faster setup
- Migrate to **Canonical** if you need workspace-level features


## Implementation Guide


## Security & Isolation


### Tenant Isolation

Nexus provides multi-layer tenant isolation:

1. **Database Level** - `tenant_id` filtering in all queries
2. **Permission Level** - ReBAC tuples scoped to tenant
3. **Storage Level** - Physical separation in backend storage

### Cross-Tenant Protection

**Write-time validation** prevents cross-tenant access:

```python
# ✅ Same tenant - allowed
nx.rebac_create(
    subject=("user", "alice"),
    relation="editor",
    object=("file", "/doc.txt"),
    tenant_id="org_acme"
)

# ❌ Cross-tenant - REJECTED
try:
    nx.rebac_create(
        subject=("user", "alice"),  # org_acme
        object=("file", "/doc.txt"),  # org_other
        # Cross-tenant tuple rejected!
    )
except ValueError:
    print("Cross-tenant access denied")
```


## Best Practices


1. **Simpler**: No reference files, no complex IDs
2. **Config-driven**: Define structure once in YAML
3. **Dynamic**: Add/remove users via API while running
4. **Standard**: Uses Nexus's built-in ReBAC
5. **Efficient**: Direct access, no indirection
6. **Scalable**: Permissions enforced at filesystem level

## Example: Complete Workflow

```yaml
# 1. Define in nexus.yaml
organizations:
  - id: acme
    name: Acme Corp
    workspaces:
      - id: sales
    members:
      - user_id: alice@acme.com
        role: admin

---

## Summary

**Both approaches work well - choose based on your needs:**

- **Simple**: Fast setup, flat structure, config-driven
- **Canonical**: Enterprise-ready, hierarchical, workspace features

**See Also:**
- [Permission Guide](PERMISSIONS.md)
- [Authentication Guide](authentication.md)
- [API Reference](api/workspace-management.md)
