# Team Collaboration

**Build multi-user applications with shared workspaces and fine-grained permissions**

⏱️ **Time:** 20 minutes | 💡 **Difficulty:** Intermediate

## What You'll Learn

- Set up multi-user Nexus server
- Create and manage user accounts
- Share files and workspaces between users
- Configure fine-grained permissions with ReBAC
- Use session-scoped resources for isolation
- Build collaborative workflows
- Manage team access control

## Prerequisites

✅ Python 3.8+ installed
✅ Nexus installed (`pip install nexus-ai-fs`)
✅ Basic understanding of file operations ([Simple File Storage](simple-file-storage.md))
✅ Familiarity with permissions concepts (helpful but not required)

## Overview

Nexus enables **secure multi-user collaboration** through its built-in authentication, authorization, and permission system. Multiple users can work on shared files while maintaining proper access control and data isolation.

**Use Cases:**
- 👥 Team document collaboration
- 🤖 Multi-agent systems with separate contexts
- 🏢 Multi-tenant SaaS applications
- 📊 Shared data analysis projects
- 🔒 Secure file sharing with permissions
- 🎯 Session-based user isolation

**Architecture:**

```
┌─────────────────────────────────────────────────────────┐
│                 Team Members                            │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐             │
│  │  Alice   │  │   Bob    │  │  Carol   │             │
│  │(API Key) │  │(API Key) │  │(API Key) │             │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘             │
└───────┼─────────────┼─────────────┼──────────────────┘
        │             │             │ HTTP + Auth
        └─────────────┼─────────────┘
                      ↓
┌─────────────────────────────────────────────────────────┐
│           Nexus Server (Central Authority)              │
│  ┌───────────────────────────────────────────────────┐ │
│  │         Authentication & Authorization            │ │
│  │  ┌────────────┐  ┌────────────┐  ┌────────────┐ │ │
│  │  │   User     │  │   ReBAC    │  │  Session   │ │ │
│  │  │  Manager   │  │  Engine    │  │  Manager   │ │ │
│  │  └────────────┘  └────────────┘  └────────────┘ │ │
│  └────────────────────┬──────────────────────────────┘ │
│         ↓             ↓              ↓                  │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐ │
│  │   Shared     │  │    User      │  │   Session    │ │
│  │  Workspace   │  │  Workspaces  │  │   Storage    │ │
│  └──────────────┘  └──────────────┘  └──────────────┘ │
└─────────────────────────────────────────────────────────┘
```

---

## Step 1: Start Nexus Server with Authentication

First, start a Nexus server that supports multiple users:

```bash
# Start server with authentication enabled
nexus serve --host 0.0.0.0 --port 8080 --data-dir ./nexus-collab-data &

# Wait for server to start
sleep 2

# Verify server is running
curl http://localhost:8080/health
```

**Expected output:**
```json
{"status":"ok","version":"0.5.0"}
```

---

## Step 2: Create Team Users

Set up multiple users for your team:

```bash
# Create admin user
nexus admin create-user admin \
  --name "Admin User" \
  --email "admin@example.com"

# Create team members
nexus admin create-user alice \
  --name "Alice Smith" \
  --email "alice@example.com"

nexus admin create-user bob \
  --name "Bob Jones" \
  --email "bob@example.com"

nexus admin create-user carol \
  --name "Carol White" \
  --email "carol@example.com"

# List all users
nexus admin list-users
```

**Expected output:**
```
Users:
  - admin (Admin User) - admin@example.com
  - alice (Alice Smith) - alice@example.com
  - bob (Bob Jones) - bob@example.com
  - carol (Carol White) - carol@example.com
```

---

## Step 3: Generate API Keys for Each User

Create API keys for authentication:

```bash
# Admin key
ADMIN_KEY=$(nexus admin create-user-key admin --description "Admin key" | grep "API Key" | awk '{print $4}')

# Team member keys
ALICE_KEY=$(nexus admin create-user-key alice --description "Alice's key" | grep "API Key" | awk '{print $4}')
BOB_KEY=$(nexus admin create-user-key bob --description "Bob's key" | grep "API Key" | awk '{print $4}')
CAROL_KEY=$(nexus admin create-user-key carol --description "Carol's key" | grep "API Key" | awk '{print $4}')

# Export keys
export NEXUS_URL=http://localhost:8080
export ADMIN_KEY
export ALICE_KEY
export BOB_KEY
export CAROL_KEY

echo "✅ API keys created for all users"
```

---

## Step 4: Create Shared Workspace

Alice creates a shared workspace for the team:

```python
# alice_setup.py
import nexus

# Alice connects to the server
alice = nexus.connect(config={
    "url": "http://localhost:8080",
    "api_key": "alice_key_here"  # Replace with actual key
})

# Alice creates a shared project workspace
alice.mkdir("/workspace/team-project")
alice.write(
    "/workspace/team-project/README.md",
    b"""# Team Project

Welcome to our collaborative project!

## Team Members
- Alice (Project Lead)
- Bob (Developer)
- Carol (Designer)

## Guidelines
- Use descriptive file names
- Add comments to your work
- Coordinate on shared files
"""
)

print("✅ Alice created shared workspace")

# Alice creates a document
alice.write(
    "/workspace/team-project/design-doc.md",
    b"""# Design Document

## Overview
This is our project design document.

## Architecture
- Frontend: React
- Backend: Python
- Database: PostgreSQL
- Storage: Nexus

Created by: Alice
"""
)

print("✅ Alice created design document")
```

---

## Step 5: Grant Team Permissions

Alice grants permissions to team members:

```python
# alice_permissions.py
import nexus

alice = nexus.connect(config={
    "url": "http://localhost:8080",
    "api_key": "alice_key_here"
})

# Grant Bob read and write access to the project
alice.grant_permission(
    subject_type="user",
    subject_id="bob",
    object_type="file",
    object_id="/workspace/team-project",
    relation="can_write"
)

print("✅ Bob granted write access to team-project")

# Grant Carol read access (she can view but not edit)
alice.grant_permission(
    subject_type="user",
    subject_id="carol",
    object_type="file",
    object_id="/workspace/team-project",
    relation="can_read"
)

print("✅ Carol granted read access to team-project")

# Verify permissions
can_bob_write = alice.check_permission(
    subject_type="user",
    subject_id="bob",
    object_type="file",
    object_id="/workspace/team-project/design-doc.md",
    relation="can_write"
)

can_carol_write = alice.check_permission(
    subject_type="user",
    subject_id="carol",
    object_type="file",
    object_id="/workspace/team-project/design-doc.md",
    relation="can_write"
)

print(f"Can Bob write? {can_bob_write}")      # True
print(f"Can Carol write? {can_carol_write}")  # False
```

---

## Step 6: Team Members Collaborate

Bob adds his contribution:

```python
# bob_contributes.py
import nexus

bob = nexus.connect(config={
    "url": "http://localhost:8080",
    "api_key": "bob_key_here"
})

# Bob reads the design doc
design_doc = bob.read("/workspace/team-project/design-doc.md").decode()
print("📄 Bob read design document")

# Bob adds implementation details
implementation = b"""# Implementation Plan

## Backend Development (Bob)

### Phase 1: API Development
- [ ] Set up FastAPI server
- [ ] Create database models
- [ ] Implement authentication
- [ ] Add Nexus integration

### Phase 2: Testing
- [ ] Unit tests
- [ ] Integration tests
- [ ] Load testing

Created by: Bob
"""

bob.write("/workspace/team-project/implementation.md", implementation)
print("✅ Bob created implementation plan")

# Bob creates a code file
bob.write(
    "/workspace/team-project/api.py",
    b"""\"\"\"
Team Project API
Author: Bob
\"\"\"

from fastapi import FastAPI
import nexus

app = FastAPI()
nx = nexus.connect()

@app.get("/files")
async def list_files():
    \"\"\"List all project files\"\"\"
    files = nx.list_files("/workspace/team-project")
    return {"files": files}

@app.post("/files/{path}")
async def create_file(path: str, content: str):
    \"\"\"Create a new file\"\"\"
    nx.write(f"/workspace/team-project/{path}", content.encode())
    return {"status": "created", "path": path}
"""
)

print("✅ Bob created API code")
```

Carol views the project (read-only):

```python
# carol_views.py
import nexus

carol = nexus.connect(config={
    "url": "http://localhost:8080",
    "api_key": "carol_key_here"
})

# Carol lists project files
files = carol.list_files("/workspace/team-project", recursive=True)
print("📁 Project files:")
for file in files:
    print(f"  - {file['path']}")

# Carol reads the design doc
design_doc = carol.read("/workspace/team-project/design-doc.md").decode()
print("\n📄 Design document:")
print(design_doc)

# Carol tries to write (will fail - she only has read access)
try:
    carol.write(
        "/workspace/team-project/designs.md",
        b"# UI Designs\n\nDesign concepts..."
    )
except nexus.NexusPermissionError as e:
    print(f"\n❌ Carol cannot write: {e}")
    print("   (She only has read access)")
```

---

## Step 7: Session-Scoped Resources

Use sessions to isolate user workspaces:

```python
# session_isolation.py
import nexus

# Alice creates a session for her work
alice = nexus.connect(config={
    "url": "http://localhost:8080",
    "api_key": "alice_key_here"
})

# Create session-scoped workspace
alice_session = alice.create_session(
    name="alice-research",
    description="Alice's research workspace"
)

print(f"✅ Created session: {alice_session['id']}")

# Alice's session-scoped work (isolated from team)
alice.write(
    f"/sessions/{alice_session['id']}/notes.md",
    b"""# Private Research Notes

These are my personal notes, not shared with the team.

## Ideas
- New feature concept
- Performance optimization ideas
- Architecture improvements
"""
)

print("✅ Alice created private session notes")

# Bob creates his own session
bob = nexus.connect(config={
    "url": "http://localhost:8080",
    "api_key": "bob_key_here"
})

bob_session = bob.create_session(
    name="bob-experiments",
    description="Bob's experimental code"
)

bob.write(
    f"/sessions/{bob_session['id']}/experiment.py",
    b"""# Experimental Code
# This is Bob's private testing area
import nexus

def test_feature():
    # Testing new ideas...
    pass
"""
)

print(f"✅ Bob created session: {bob_session['id']}")

# Sessions are isolated - Alice cannot see Bob's session files
try:
    alice.read(f"/sessions/{bob_session['id']}/experiment.py")
except nexus.NexusPermissionError:
    print("✅ Session isolation working - Alice cannot access Bob's session")

# List Alice's sessions
alice_sessions = alice.list_sessions()
print(f"\n📋 Alice's sessions: {len(alice_sessions)}")
for session in alice_sessions:
    print(f"  - {session['name']}: {session['description']}")
```

---

## Step 8: Advanced Permission Management

Set up complex permission hierarchies:

```python
# advanced_permissions.py
import nexus

alice = nexus.connect(config={
    "url": "http://localhost:8080",
    "api_key": "alice_key_here"
})

# Create a hierarchy of workspaces
alice.mkdir("/workspace/team-project/public")
alice.mkdir("/workspace/team-project/internal")
alice.mkdir("/workspace/team-project/confidential")

# Public: Everyone can read
alice.grant_permission(
    subject_type="user",
    subject_id="bob",
    object_type="file",
    object_id="/workspace/team-project/public",
    relation="can_read"
)

alice.grant_permission(
    subject_type="user",
    subject_id="carol",
    object_type="file",
    object_id="/workspace/team-project/public",
    relation="can_read"
)

# Internal: Bob can write, Carol can read
alice.grant_permission(
    subject_type="user",
    subject_id="bob",
    object_type="file",
    object_id="/workspace/team-project/internal",
    relation="can_write"
)

alice.grant_permission(
    subject_type="user",
    subject_id="carol",
    object_type="file",
    object_id="/workspace/team-project/internal",
    relation="can_read"
)

# Confidential: Only Alice (owner) has access
# No additional grants needed - owner has all permissions

print("✅ Permission hierarchy configured:")
print("  - Public: Bob (read), Carol (read)")
print("  - Internal: Bob (write), Carol (read)")
print("  - Confidential: Alice only")

# Create sample files
alice.write("/workspace/team-project/public/README.md", b"Public information")
alice.write("/workspace/team-project/internal/specs.md", b"Internal specifications")
alice.write("/workspace/team-project/confidential/strategy.md", b"Confidential strategy")

# Test permissions
bob = nexus.connect(config={"url": "http://localhost:8080", "api_key": "bob_key_here"})

# Bob can read public
public_content = bob.read("/workspace/team-project/public/README.md")
print("✅ Bob read public file")

# Bob cannot read confidential
try:
    bob.read("/workspace/team-project/confidential/strategy.md")
except nexus.NexusPermissionError:
    print("✅ Bob cannot read confidential file (expected)")
```

---

## Step 9: Group-Based Permissions

Create user groups for easier permission management:

```python
# groups.py
import nexus

admin = nexus.connect(config={
    "url": "http://localhost:8080",
    "api_key": "admin_key_here"
})

# Create a "developers" group
admin.create_group("developers", description="Development team")

# Add Bob to developers
admin.add_user_to_group("bob", "developers")

print("✅ Created 'developers' group with Bob")

# Grant group permissions
admin.grant_permission(
    subject_type="group",
    subject_id="developers",
    object_type="file",
    object_id="/workspace/team-project/internal",
    relation="can_write"
)

print("✅ Granted developers write access to internal/")

# Now all developers (including Bob) have access
# If we add more users to the group, they automatically get access

# Create a "designers" group
admin.create_group("designers", description="Design team")
admin.add_user_to_group("carol", "designers")

admin.grant_permission(
    subject_type="group",
    subject_id="designers",
    object_type="file",
    object_id="/workspace/team-project/public",
    relation="can_write"
)

print("✅ Created 'designers' group with Carol")
print("✅ Granted designers write access to public/")
```

---

## Step 10: Real-Time Collaboration Patterns

Implement collaborative workflows:

```python
# collaboration_workflow.py
import nexus
import json
from datetime import datetime

class CollaborationWorkflow:
    def __init__(self, user_name, api_key):
        self.user_name = user_name
        self.nx = nexus.connect(config={
            "url": "http://localhost:8080",
            "api_key": api_key
        })

    def claim_file(self, file_path):
        """Claim a file for editing (simple locking)"""
        lock_path = f"{file_path}.lock"

        # Check if file is locked
        if self.nx.exists(lock_path):
            lock_info = json.loads(self.nx.read(lock_path).decode())
            print(f"⚠️  File locked by {lock_info['user']} at {lock_info['time']}")
            return False

        # Create lock
        lock_data = {
            "user": self.user_name,
            "time": datetime.now().isoformat()
        }
        self.nx.write(lock_path, json.dumps(lock_data).encode())
        print(f"🔒 {self.user_name} claimed {file_path}")
        return True

    def release_file(self, file_path):
        """Release file lock"""
        lock_path = f"{file_path}.lock"
        if self.nx.exists(lock_path):
            self.nx.delete(lock_path)
            print(f"🔓 {self.user_name} released {file_path}")

    def add_comment(self, file_path, comment):
        """Add a comment to a file"""
        comments_path = f"{file_path}.comments.json"

        # Load existing comments
        if self.nx.exists(comments_path):
            comments = json.loads(self.nx.read(comments_path).decode())
        else:
            comments = []

        # Add new comment
        comments.append({
            "user": self.user_name,
            "time": datetime.now().isoformat(),
            "comment": comment
        })

        self.nx.write(comments_path, json.dumps(comments, indent=2).encode())
        print(f"💬 {self.user_name} commented on {file_path}")

    def get_comments(self, file_path):
        """Get all comments for a file"""
        comments_path = f"{file_path}.comments.json"

        if not self.nx.exists(comments_path):
            return []

        return json.loads(self.nx.read(comments_path).decode())

# Usage example
alice_flow = CollaborationWorkflow("alice", "alice_key_here")
bob_flow = CollaborationWorkflow("bob", "bob_key_here")

# Alice claims and edits a file
file_path = "/workspace/team-project/design-doc.md"

if alice_flow.claim_file(file_path):
    # Alice edits the file
    content = alice_flow.nx.read(file_path)
    updated = content + b"\n\n## Updated by Alice\nNew section added."
    alice_flow.nx.write(file_path, updated)

    # Alice adds a comment
    alice_flow.add_comment(file_path, "Updated the architecture section")

    # Alice releases the file
    alice_flow.release_file(file_path)

# Bob tries to claim the same file
if bob_flow.claim_file(file_path):
    # Bob can now edit
    bob_flow.add_comment(file_path, "Looks good! I'll add the API details.")
    bob_flow.release_file(file_path)

# View all comments
comments = alice_flow.get_comments(file_path)
print(f"\n💬 Comments on {file_path}:")
for comment in comments:
    print(f"  [{comment['user']}] {comment['comment']}")
```

---

## Complete Working Example

Here's a production-ready team collaboration system:

```python
#!/usr/bin/env python3
"""
Team Collaboration System with Nexus
Demonstrates: multi-user, permissions, sessions, locking
"""
import nexus
import json
from datetime import datetime
from typing import List, Dict, Optional

class TeamCollaboration:
    """Manage team collaboration with Nexus"""

    def __init__(self, server_url: str):
        self.server_url = server_url
        self.users = {}

    def add_user(self, username: str, api_key: str):
        """Register a user"""
        self.users[username] = nexus.connect(config={
            "url": self.server_url,
            "api_key": api_key
        })
        print(f"✅ Registered user: {username}")

    def create_shared_workspace(self, owner: str, workspace_path: str):
        """Create a shared workspace"""
        nx = self.users[owner]
        nx.mkdir(workspace_path)

        # Create metadata
        metadata = {
            "owner": owner,
            "created": datetime.now().isoformat(),
            "type": "shared_workspace"
        }
        nx.write(
            f"{workspace_path}/.metadata.json",
            json.dumps(metadata, indent=2).encode()
        )

        print(f"✅ {owner} created workspace: {workspace_path}")

    def share_with_user(
        self,
        owner: str,
        workspace_path: str,
        user: str,
        permission: str = "can_read"
    ):
        """Share workspace with another user"""
        nx = self.users[owner]

        nx.grant_permission(
            subject_type="user",
            subject_id=user,
            object_type="file",
            object_id=workspace_path,
            relation=permission
        )

        print(f"✅ {owner} granted {user} '{permission}' on {workspace_path}")

    def create_document(
        self,
        username: str,
        file_path: str,
        content: str,
        notify_users: Optional[List[str]] = None
    ):
        """Create a document and optionally notify team members"""
        nx = self.users[username]

        # Write document
        nx.write(file_path, content.encode())
        print(f"✅ {username} created: {file_path}")

        # Add metadata
        metadata = {
            "author": username,
            "created": datetime.now().isoformat(),
            "notified": notify_users or []
        }
        nx.write(
            f"{file_path}.meta.json",
            json.dumps(metadata, indent=2).encode()
        )

        # Notify team members (by creating notification files)
        if notify_users:
            for user in notify_users:
                notification = {
                    "from": username,
                    "message": f"New document created: {file_path}",
                    "time": datetime.now().isoformat(),
                    "action": "view",
                    "target": file_path
                }

                notif_path = f"/workspace/.notifications/{user}/{datetime.now().timestamp()}.json"
                nx.write(notif_path, json.dumps(notification).encode())

            print(f"   Notified: {', '.join(notify_users)}")

    def get_activity_log(self, workspace_path: str) -> List[Dict]:
        """Get activity log for a workspace"""
        # In production, this would query version history or event logs
        # For demo, we'll return a sample
        return [
            {"user": "alice", "action": "created", "file": "design-doc.md"},
            {"user": "bob", "action": "updated", "file": "implementation.md"},
            {"user": "carol", "action": "viewed", "file": "design-doc.md"}
        ]

    def list_team_files(self, username: str, workspace_path: str) -> List[Dict]:
        """List files user has access to"""
        nx = self.users[username]

        try:
            files = nx.list_files(workspace_path, recursive=True)
            return files
        except nexus.NexusPermissionError:
            print(f"❌ {username} does not have access to {workspace_path}")
            return []

# Demo usage
def main():
    # Server configuration
    SERVER_URL = "http://localhost:8080"

    # API keys (replace with actual keys)
    ALICE_KEY = "alice_key_here"
    BOB_KEY = "bob_key_here"
    CAROL_KEY = "carol_key_here"

    # Initialize collaboration system
    collab = TeamCollaboration(SERVER_URL)

    # Register users
    collab.add_user("alice", ALICE_KEY)
    collab.add_user("bob", BOB_KEY)
    collab.add_user("carol", CAROL_KEY)

    # Alice creates shared workspace
    workspace = "/workspace/team-project"
    collab.create_shared_workspace("alice", workspace)

    # Alice shares with team
    collab.share_with_user("alice", workspace, "bob", "can_write")
    collab.share_with_user("alice", workspace, "carol", "can_read")

    # Alice creates a document and notifies team
    collab.create_document(
        "alice",
        f"{workspace}/project-brief.md",
        """# Project Brief

## Overview
This is our new collaborative project.

## Timeline
- Week 1: Planning
- Week 2: Development
- Week 3: Testing
- Week 4: Launch
""",
        notify_users=["bob", "carol"]
    )

    # Bob contributes
    collab.create_document(
        "bob",
        f"{workspace}/technical-spec.md",
        """# Technical Specification

## Architecture
- Frontend: React
- Backend: FastAPI
- Database: PostgreSQL
- Storage: Nexus
""",
        notify_users=["alice"]
    )

    # List files each user can see
    print("\n📁 Files accessible to each user:")
    for user in ["alice", "bob", "carol"]:
        files = collab.list_team_files(user, workspace)
        print(f"\n{user}:")
        for file in files:
            print(f"  - {file['path']}")

if __name__ == "__main__":
    main()
```

---

## Using CLI for Team Collaboration

Manage users and permissions via CLI:

```bash
# Create users
nexus admin create-user alice --name "Alice" --email "alice@example.com"
nexus admin create-user bob --name "Bob" --email "bob@example.com"

# Create API keys
nexus admin create-user-key alice --description "Alice's key"
nexus admin create-user-key bob --description "Bob's key"

# Grant permissions (using rebac commands)
nexus rebac grant user alice file /workspace/shared --relation owner
nexus rebac grant user bob file /workspace/shared --relation can_write

# Check permissions
nexus rebac check user bob file /workspace/shared --relation can_write

# List all permissions
nexus rebac list --subject-type user --subject-id alice

# Create groups
nexus admin create-group developers
nexus admin add-user-to-group bob developers

# Grant group permissions
nexus rebac grant group developers file /workspace/dev --relation can_write
```

---

## Troubleshooting

### Issue: Permission Denied

**Error:** `NexusPermissionError: User does not have permission`

**Solution:**
```python
# Check current permissions
permissions = nx.list_permissions(
    object_type="file",
    object_id="/workspace/shared"
)

for perm in permissions:
    print(f"{perm['subject_id']} has {perm['relation']}")

# Grant missing permission
nx.grant_permission(
    subject_type="user",
    subject_id="bob",
    object_type="file",
    object_id="/workspace/shared",
    relation="can_write"
)
```

---

### Issue: Session Isolation Not Working

**Problem:** Users can see each other's session files

**Solution:**
```python
# Ensure using session-specific paths
session = nx.create_session(name="my-session")
session_id = session['id']

# Use session prefix in all paths
nx.write(f"/sessions/{session_id}/private.txt", b"data")

# NOT: nx.write("/workspace/private.txt", b"data")
```

---

### Issue: File Locked by Another User

**Problem:** Cannot edit file claimed by another user

**Solution:**
```python
# Check lock status
lock_path = f"{file_path}.lock"

if nx.exists(lock_path):
    lock_info = json.loads(nx.read(lock_path).decode())
    print(f"Locked by: {lock_info['user']}")
    print(f"Since: {lock_info['time']}")

    # Contact user or wait for release
    # Or implement lock timeout mechanism
```

---

## Key Concepts

### Permission Model

Nexus uses **Relationship-Based Access Control (ReBAC)**:

| Relation | Description | Actions Allowed |
|----------|-------------|-----------------|
| `owner` | File owner | All operations |
| `can_write` | Write access | Read, write, delete |
| `can_read` | Read access | Read only |
| `can_execute` | Execute access | Read, execute |

### Permission Inheritance

Permissions can be inherited:

```python
# Grant on directory
nx.grant_permission(
    subject_type="user",
    subject_id="bob",
    object_type="file",
    object_id="/workspace/team-project",
    relation="can_write"
)

# Bob automatically has access to all files in the directory
# /workspace/team-project/file1.txt ✅
# /workspace/team-project/subdir/file2.txt ✅
```

### Session Scopes

Sessions provide isolation:

- **User sessions:** Private workspace per user
- **Agent sessions:** Isolated context per agent instance
- **Temporary sessions:** Auto-cleanup after expiry

```python
# Create temporary session (auto-expires)
session = nx.create_session(
    name="temp-work",
    ttl=3600  # 1 hour
)
```

---

## Best Practices

### 1. Use Groups for Team Permissions

```python
# ✅ Good: Use groups
nx.create_group("engineering")
nx.add_user_to_group("alice", "engineering")
nx.add_user_to_group("bob", "engineering")

nx.grant_permission(
    subject_type="group",
    subject_id="engineering",
    object_type="file",
    object_id="/workspace/eng",
    relation="can_write"
)

# ❌ Bad: Grant individually (hard to maintain)
nx.grant_permission(subject_type="user", subject_id="alice", ...)
nx.grant_permission(subject_type="user", subject_id="bob", ...)
```

### 2. Implement Optimistic Locking

```python
# ✅ Good: Check version before updating
def safe_update(file_path, update_fn):
    # Read current version
    current = nx.read_with_metadata(file_path)
    version = current['metadata']['version']

    # Apply update
    updated_content = update_fn(current['content'])

    # Write only if version unchanged
    try:
        nx.write(
            file_path,
            updated_content,
            if_version=version
        )
        return True
    except nexus.NexusConflictError:
        # File was modified by someone else
        return False
```

### 3. Use Session Cleanup

```python
# ✅ Good: Clean up old sessions
def cleanup_old_sessions():
    sessions = nx.list_sessions()
    now = datetime.now()

    for session in sessions:
        created = datetime.fromisoformat(session['created'])
        age_days = (now - created).days

        if age_days > 7:  # Older than 7 days
            nx.delete_session(session['id'])
            print(f"Cleaned up session: {session['name']}")
```

### 4. Audit Trail

```python
# ✅ Good: Maintain audit log
def log_access(user, action, file_path):
    log_entry = {
        "timestamp": datetime.now().isoformat(),
        "user": user,
        "action": action,
        "file": file_path
    }

    nx.append(
        "/workspace/.audit/access.log",
        (json.dumps(log_entry) + '\n').encode()
    )
```

---

## What's Next?

Now that you've mastered team collaboration, explore more advanced topics:

### 🔍 Recommended Next Steps

1. **[Multi-Tenant SaaS](multi-tenant-saas.md)** (30 min)
   Scale to multiple organizations with tenant isolation

2. **[Workflow Automation](workflow-automation.md)** (15 min)
   Automate team workflows with event triggers

3. **[Agent Framework Integration](agent-framework-integration.md)** (20 min)
   Build multi-agent systems with Nexus

### 📚 Related Concepts

- [ReBAC Explained](../concepts/rebac-explained.md) - Deep dive into permissions
- [Multi-Tenancy](../concepts/multi-tenancy.md) - Tenant isolation architecture
- [Agent Permissions](../concepts/agent-permissions.md) - Permission patterns for agents

### 🔧 Advanced Topics

- [Administration & Operations](administration-operations.md) - User management
- [Production Deployment](../production/deployment-patterns.md) - Scale your server
- [Security Checklist](../production/security-checklist.md) - Security best practices

---

## Summary

🎉 **You've completed the Team Collaboration tutorial!**

**What you learned:**
- ✅ Set up multi-user Nexus server with authentication
- ✅ Create and manage user accounts and API keys
- ✅ Share workspaces and grant fine-grained permissions
- ✅ Use session-scoped resources for isolation
- ✅ Implement collaborative workflows with locking
- ✅ Manage groups and permission hierarchies
- ✅ Build production-ready collaboration systems

**Key Takeaways:**
- ReBAC provides flexible, fine-grained access control
- Sessions enable user and agent isolation
- Groups simplify permission management
- Always implement proper locking for concurrent edits

---

**Next:** [Multi-Tenant SaaS →](multi-tenant-saas.md)

**Questions?** Check our [Permissions Guide](../concepts/rebac-explained.md) or [GitHub Discussions](https://github.com/nexi-lab/nexus/discussions)
