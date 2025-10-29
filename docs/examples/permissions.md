# Permission Management Example

Learn fine-grained access control with Google Zanzibar-style ReBAC (Relationship-Based Access Control) in Nexus.

## 🎯 What You'll Learn

- Create user and group relationships
- Grant and revoke permissions
- Check permissions programmatically
- Understand permission inheritance
- Debug permission chains with explain
- Find all users with specific permissions

## 🚀 Quick Start

=== "Python SDK"

    ```python
    import nexus

    nx = nexus.connect(remote_url="http://localhost:8080", api_key="admin-key")

    # Create relationships
    nx.rebac_create("user", "alice", "member", "group", "engineers")
    nx.rebac_create("group", "engineers", "write", "file", "/workspace/code")

    # Check permission
    can_write = nx.rebac_check("user", "alice", "write", "file", "/workspace/code/main.py")
    print(f"Alice can write: {can_write}")  # True (via group membership)

    # Explain why
    explanation = nx.rebac_explain("user", "alice", "write", "file", "/workspace/code/main.py")
    print(explanation)
    ```

=== "CLI"

    ```bash
    # Create relationships
    nexus rebac create user alice member group engineers
    nexus rebac create group engineers write file /workspace/code

    # Check permission
    nexus rebac check user alice write file /workspace/code/main.py

    # Explain permission chain
    nexus rebac explain user alice write file /workspace/code/main.py

    # List all relationships
    nexus rebac list user alice
    ```

## 👥 User and Group Management

=== "Create Groups"

    ```python
    import nexus

    nx = nexus.connect(remote_url="http://localhost:8080", api_key="admin-key")

    # Add users to groups
    nx.rebac_create("user", "alice", "member", "group", "engineers")
    nx.rebac_create("user", "bob", "member", "group", "engineers")
    nx.rebac_create("user", "charlie", "member", "group", "managers")

    # Create nested groups
    nx.rebac_create("group", "engineers", "member", "group", "all-staff")
    nx.rebac_create("group", "managers", "member", "group", "all-staff")
    ```

=== "Grant Permissions"

    ```python
    import nexus

    nx = nexus.connect(remote_url="http://localhost:8080", api_key="admin-key")

    # Grant group permissions
    nx.rebac_create("group", "engineers", "write", "file", "/workspace/code")
    nx.rebac_create("group", "managers", "read", "file", "/workspace/reports")

    # Grant individual permissions (overrides)
    nx.rebac_create("user", "alice", "owner", "file", "/workspace/code/special.py")
    ```

## 🔐 Permission Types

=== "Basic Permissions"

    ```python
    import nexus

    nx = nexus.connect(remote_url="http://localhost:8080", api_key="admin-key")

    # Viewer: read-only access
    nx.rebac_create("user", "alice", "direct_viewer", "file", "/workspace/docs")

    # Editor: read + write (no delete)
    nx.rebac_create("user", "bob", "direct_editor", "file", "/workspace/data")

    # Owner: full control (read, write, delete, manage permissions)
    nx.rebac_create("user", "charlie", "direct_owner", "file", "/workspace/project")
    ```

=== "Custom Relations"

    ```python
    import nexus

    nx = nexus.connect(remote_url="http://localhost:8080", api_key="admin-key")

    # Custom relationships for your domain
    nx.rebac_create("user", "alice", "can_approve", "workflow", "deploy-prod")
    nx.rebac_create("user", "bob", "can_review", "document", "security-policy")

    # Check custom permissions
    can_approve = nx.rebac_check("user", "alice", "can_approve", "workflow", "deploy-prod")
    ```

## 🔍 Permission Checking

=== "Basic Check"

    ```python
    import nexus

    nx = nexus.connect(remote_url="http://localhost:8080", api_key="admin-key")

    # Check if user has permission
    can_write = nx.rebac_check(
        "user", "alice",
        "write",
        "file", "/workspace/code/main.py"
    )

    if can_write:
        # Proceed with operation
        nx.write("/workspace/code/main.py", b"# Updated code")
    else:
        print("Permission denied")
    ```

=== "Explain Permission Chain"

    ```python
    import nexus

    nx = nexus.connect(remote_url="http://localhost:8080", api_key="admin-key")

    # Get detailed explanation
    explanation = nx.rebac_explain(
        "user", "alice",
        "write",
        "file", "/workspace/code/main.py"
    )

    # Shows the permission chain:
    # alice -> member of engineers -> engineers has write on /workspace/code
    # -> permission inherited to /workspace/code/main.py
    print(explanation)
    ```

=== "Batch Permission Checks"

    ```python
    import nexus

    nx = nexus.connect(remote_url="http://localhost:8080", api_key="admin-key")

    files = ["/workspace/file1.txt", "/workspace/file2.txt", "/workspace/file3.txt"]

    for file in files:
        can_write = nx.rebac_check("user", "alice", "write", "file", file)
        print(f"{file}: {'✓' if can_write else '✗'}")
    ```

## 🌳 Permission Inheritance

=== "Directory Inheritance"

    ```python
    import nexus

    nx = nexus.connect(remote_url="http://localhost:8080", api_key="admin-key")

    # Grant permission on parent directory
    nx.rebac_create(
        "group", "engineers",
        "write",
        "file", "/workspace/project"
    )

    # Permission automatically inherited to all subdirectories and files
    can_write_file = nx.rebac_check(
        "user", "alice",  # member of engineers
        "write",
        "file", "/workspace/project/src/main.py"
    )
    print(can_write_file)  # True (inherited from /workspace/project)
    ```

=== "Group Inheritance"

    ```python
    import nexus

    nx = nexus.connect(remote_url="http://localhost:8080", api_key="admin-key")

    # Nested group structure
    nx.rebac_create("user", "alice", "member", "group", "backend-team")
    nx.rebac_create("group", "backend-team", "member", "group", "engineers")
    nx.rebac_create("group", "engineers", "member", "group", "all-staff")

    # Grant permission to top-level group
    nx.rebac_create("group", "all-staff", "read", "file", "/workspace")

    # Alice inherits permission through group chain
    can_read = nx.rebac_check("user", "alice", "read", "file", "/workspace/file.txt")
    print(can_read)  # True (via backend-team -> engineers -> all-staff)
    ```

## 🔎 Finding Users with Permissions

=== "Expand Permissions"

    ```python
    import nexus

    nx = nexus.connect(remote_url="http://localhost:8080", api_key="admin-key")

    # Find all users with write access
    users_with_write = nx.rebac_expand(
        "write",
        "file", "/workspace/project"
    )

    print("Users with write access:")
    for user in users_with_write:
        print(f"  - {user}")
    ```

=== "List User's Permissions"

    ```python
    import nexus

    nx = nexus.connect(remote_url="http://localhost:8080", api_key="admin-key")

    # List all relationships for a user
    relationships = nx.rebac_list("user", "alice")

    print("Alice's permissions:")
    for rel in relationships:
        print(f"  {rel['relation']} on {rel['object_type']}:{rel['object_id']}")
    ```

## 🚫 Revoking Permissions

=== "Remove Direct Permission"

    ```python
    import nexus

    nx = nexus.connect(remote_url="http://localhost:8080", api_key="admin-key")

    # Remove user's permission
    nx.rebac_delete(
        "user", "alice",
        "write",
        "file", "/workspace/code"
    )
    ```

=== "Remove from Group"

    ```python
    import nexus

    nx = nexus.connect(remote_url="http://localhost:8080", api_key="admin-key")

    # Remove user from group
    nx.rebac_delete(
        "user", "alice",
        "member",
        "group", "engineers"
    )

    # Alice loses all permissions granted via engineers group
    ```

## 🎬 Complete Workflow Example

=== "Team Collaboration Setup"

    ```python
    import nexus

    nx = nexus.connect(remote_url="http://localhost:8080", api_key="admin-key")

    # 1. Create project workspace
    nx.mkdir("/workspace/project-alpha", parents=True)
    nx.mkdir("/workspace/project-alpha/src", parents=True)
    nx.mkdir("/workspace/project-alpha/docs", parents=True)

    # 2. Create teams
    team_members = {
        "backend-team": ["alice", "bob"],
        "frontend-team": ["charlie", "diana"],
        "docs-team": ["eve"]
    }

    for team, members in team_members.items():
        for member in members:
            nx.rebac_create("user", member, "member", "group", team)

    # 3. Grant team permissions
    nx.rebac_create(
        "group", "backend-team",
        "write",
        "file", "/workspace/project-alpha/src"
    )

    nx.rebac_create(
        "group", "frontend-team",
        "write",
        "file", "/workspace/project-alpha/src"
    )

    nx.rebac_create(
        "group", "docs-team",
        "write",
        "file", "/workspace/project-alpha/docs"
    )

    # 4. Grant read access to everyone
    all_teams = ["backend-team", "frontend-team", "docs-team"]
    for team in all_teams:
        nx.rebac_create("group", team, "member", "group", "project-alpha-all")

    nx.rebac_create(
        "group", "project-alpha-all",
        "read",
        "file", "/workspace/project-alpha"
    )

    # 5. Verify permissions
    # Alice (backend) can write to src
    assert nx.rebac_check("user", "alice", "write", "file", "/workspace/project-alpha/src/api.py")

    # Alice can read docs (via project-alpha-all)
    assert nx.rebac_check("user", "alice", "read", "file", "/workspace/project-alpha/docs/README.md")

    # Alice cannot write to docs (not in docs-team)
    assert not nx.rebac_check("user", "alice", "write", "file", "/workspace/project-alpha/docs/README.md")

    print("✅ Team collaboration setup complete!")
    ```

=== "Multi-Tenant Isolation"

    ```python
    import nexus

    nx = nexus.connect(remote_url="http://localhost:8080", api_key="admin-key")

    # Create isolated tenant workspaces
    tenants = ["acme-corp", "beta-inc"]

    for tenant in tenants:
        # Create workspace
        workspace_path = f"/tenants/{tenant}"
        nx.mkdir(workspace_path, parents=True)

        # Grant tenant admin full ownership
        nx.rebac_create(
            "user", f"admin@{tenant}.com",
            "owner",
            "file", workspace_path
        )

        # Add tenant users to tenant group
        tenant_users = [f"user1@{tenant}.com", f"user2@{tenant}.com"]
        for user in tenant_users:
            nx.rebac_create("user", user, "member", "group", tenant)

        # Grant group access to tenant workspace
        nx.rebac_create(
            "group", tenant,
            "write",
            "file", workspace_path
        )

    # Verify isolation
    # ACME user can access ACME workspace
    acme_can_read = nx.rebac_check(
        "user", "user1@acme-corp.com",
        "read",
        "file", "/tenants/acme-corp/data.json"
    )
    assert acme_can_read  # True

    # ACME user CANNOT access Beta workspace
    acme_to_beta = nx.rebac_check(
        "user", "user1@acme-corp.com",
        "read",
        "file", "/tenants/beta-inc/data.json"
    )
    assert not acme_to_beta  # False - isolated!

    print("✅ Multi-tenant isolation verified!")
    ```

## 💡 Best Practices

=== "Use Groups Over Individual Permissions"

    ```python
    # ✅ Good: Use groups for scalability
    nx.rebac_create("user", "alice", "member", "group", "engineers")
    nx.rebac_create("group", "engineers", "write", "file", "/workspace/code")

    # ❌ Bad: Individual permissions don't scale
    # nx.rebac_create("user", "alice", "write", "file", "/workspace/code")
    # nx.rebac_create("user", "bob", "write", "file", "/workspace/code")
    # ... tedious when you have 100+ users!
    ```

=== "Grant Permissions High in the Hierarchy"

    ```python
    # ✅ Good: Grant at top level, inherit down
    nx.rebac_create("group", "team", "write", "file", "/workspace/project")
    # All subdirectories automatically included

    # ❌ Bad: Grant permissions file-by-file
    # nx.rebac_create("group", "team", "write", "file", "/workspace/project/file1.txt")
    # nx.rebac_create("group", "team", "write", "file", "/workspace/project/file2.txt")
    # ... doesn't scale, error-prone
    ```

## 🏃 Run the Full Demo

Try the comprehensive permissions demo:

```bash
# Start server
./scripts/init-nexus-with-auth.sh

# In another terminal
source .nexus-admin-env
./examples/cli/permissions_demo_enhanced.sh
```

The demo covers:
- ✅ User and group creation
- ✅ Direct and inherited permissions
- ✅ Permission checks and explanations
- ✅ Hierarchical inheritance
- ✅ Multi-level permission chains
- ✅ Permission revocation

## 📚 What's Next?

- **[Workspace & Sessions](workspace-session.md)** - Multi-tenant isolation
- **[API Reference](../api/permissions.md)** - Complete permissions API
- **[ReBAC Architecture](../PERMISSIONS.md)** - Deep dive into ReBAC design
