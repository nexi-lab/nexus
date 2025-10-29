# Workspace & Session Management Example

Build multi-tenant applications with workspace isolation and session tracking in Nexus.

## 🎯 What You'll Learn

- Create and manage workspaces
- Implement multi-tenant isolation
- Track agent sessions
- Use workspace snapshots for versioning
- Restore workspace state

## 🚀 Quick Start

=== "Python SDK"

    ```python
    import nexus

    nx = nexus.connect(remote_url="http://localhost:8080", api_key="admin-key")

    # Create workspace
    nx.workspace.create(
        "/workspace/acme-corp",
        metadata={"tenant_id": "acme-123", "plan": "enterprise"}
    )

    # Write to workspace
    nx.write("/workspace/acme-corp/data.json", b'{"records": 1000}')

    # Create snapshot
    snapshot_id = nx.workspace.snapshot("/workspace/acme-corp", name="daily-backup")

    # Later: restore snapshot
    nx.workspace.restore("/workspace/acme-corp", snapshot_id)
    ```

=== "CLI"

    ```bash
    # Create workspace
    nexus workspace create /workspace/acme-corp

    # List workspaces
    nexus workspace list

    # Create snapshot
    nexus workspace snapshot /workspace/acme-corp --name daily-backup

    # Restore snapshot
    nexus workspace restore /workspace/acme-corp <snapshot-id>
    ```

## 🏢 Workspace Management

=== "Create Workspace"

    ```python
    import nexus

    nx = nexus.connect(remote_url="http://localhost:8080", api_key="admin-key")

    # Create workspace with metadata
    nx.workspace.create(
        "/workspace/project-alpha",
        metadata={
            "project": "Alpha Initiative",
            "team": "backend",
            "created_by": "admin",
            "budget": 50000
        }
    )
    ```

=== "List Workspaces"

    ```python
    import nexus

    nx = nexus.connect(remote_url="http://localhost:8080", api_key="admin-key")

    # Get all workspaces
    workspaces = nx.workspace.list()

    for ws in workspaces:
        print(f"""
        Path: {ws['path']}
        Created: {ws['created_at']}
        Metadata: {ws.get('metadata', {})}
        """)
    ```

=== "Delete Workspace"

    ```python
    import nexus

    nx = nexus.connect(remote_url="http://localhost:8080", api_key="admin-key")

    # Delete workspace and all contents
    nx.workspace.delete("/workspace/old-project")
    ```

## 👥 Multi-Tenant Isolation

=== "Basic Multi-Tenancy"

    ```python
    import nexus

    nx = nexus.connect(remote_url="http://localhost:8080", api_key="admin-key")

    # Create tenant workspaces
    tenants = [
        {"id": "acme-123", "name": "Acme Corp", "plan": "enterprise"},
        {"id": "beta-456", "name": "Beta Inc", "plan": "professional"},
        {"id": "gamma-789", "name": "Gamma Ltd", "plan": "starter"}
    ]

    for tenant in tenants:
        # Create isolated workspace
        workspace_path = f"/tenants/{tenant['id']}"
        nx.workspace.create(workspace_path, metadata=tenant)

        # Grant tenant admin full access
        nx.rebac_create(
            "user", f"admin@tenant-{tenant['id']}.com",
            "owner",
            "file", workspace_path
        )

        # Create standard subdirectories
        nx.mkdir(f"{workspace_path}/data", parents=True)
        nx.mkdir(f"{workspace_path}/models", parents=True)
        nx.mkdir(f"{workspace_path}/exports", parents=True)

    print(f"Created {len(tenants)} isolated tenant workspaces")
    ```

=== "Tenant Connection"

    ```python
    import nexus

    # Tenant user connects with their API key
    tenant_nx = nexus.connect(
        remote_url="http://localhost:8080",
        api_key="tenant-specific-key"
    )

    # Write to tenant workspace
    tenant_nx.write(
        "/tenants/acme-123/data/records.json",
        b'{"records": [...]}'
    )

    # Tenant CANNOT access other tenants' data
    try:
        tenant_nx.read("/tenants/beta-456/data/records.json")
    except nexus.PermissionError:
        print("Access denied - tenant isolation working!")
    ```

=== "Admin Cross-Tenant Access"

    ```python
    import nexus

    nx = nexus.connect(remote_url="http://localhost:8080", api_key="admin-key")

    # Admin can access all tenants
    all_tenants = ["/tenants/acme-123", "/tenants/beta-456", "/tenants/gamma-789"]

    for tenant_path in all_tenants:
        files = nx.list(tenant_path, recursive=True)
        print(f"{tenant_path}: {len(files)} files")
    ```

## 📸 Workspace Snapshots

=== "Create Snapshot"

    ```python
    import nexus

    nx = nexus.connect(config={"data_dir": "./nexus-data"})

    # Create snapshot of entire workspace
    snapshot_id = nx.workspace.snapshot(
        "/workspace/project",
        name="before-refactor",
        metadata={"reason": "Major refactoring planned"}
    )

    print(f"Created snapshot: {snapshot_id}")
    ```

=== "List Snapshots"

    ```python
    import nexus

    nx = nexus.connect(config={"data_dir": "./nexus-data"})

    # List all snapshots for workspace
    snapshots = nx.workspace.list_snapshots("/workspace/project")

    for snapshot in snapshots:
        print(f"""
        ID: {snapshot['snapshot_id']}
        Name: {snapshot['name']}
        Created: {snapshot['created_at']}
        Files: {snapshot['file_count']}
        """)
    ```

=== "Restore Snapshot"

    ```python
    import nexus

    nx = nexus.connect(config={"data_dir": "./nexus-data"})

    # Restore workspace to previous state
    nx.workspace.restore(
        "/workspace/project",
        snapshot_id="snap_abc123"
    )

    print("Workspace restored to snapshot state")
    ```

## 🤖 Agent Session Tracking

=== "Create Session"

    ```python
    import nexus
    from datetime import datetime

    nx = nexus.connect(config={"data_dir": "./nexus-data"})

    # Start agent session
    session = nx.session.create(
        agent_id="gpt-4-agent",
        metadata={
            "user_id": "user_123",
            "conversation_id": "conv_456",
            "started_at": datetime.now().isoformat()
        }
    )

    session_id = session['session_id']
    print(f"Session ID: {session_id}")
    ```

=== "Use Session Context"

    ```python
    import nexus

    nx = nexus.connect(config={"data_dir": "./nexus-data"})

    # Operations within session context
    with nx.session.context(session_id="sess_abc123") as session:
        # Write files associated with this session
        nx.write(
            "/agent/memory/conversation.json",
            b'{"messages": [...]}',
            context={"session_id": session.id}
        )

        # Session metadata automatically tracked
        files_in_session = nx.list(
            "/agent/memory",
            filter={"session_id": session.id}
        )
    ```

=== "List Sessions"

    ```python
    import nexus

    nx = nexus.connect(config={"data_dir": "./nexus-data"})

    # List all sessions for an agent
    sessions = nx.session.list(agent_id="gpt-4-agent")

    for session in sessions:
        print(f"""
        Session: {session['session_id']}
        Agent: {session['agent_id']}
        Started: {session['created_at']}
        Files: {session.get('file_count', 0)}
        """)
    ```

## 🎬 Complete Workflow Example

=== "SaaS Application"

    ```python
    import nexus
    from datetime import datetime

    nx = nexus.connect(remote_url="http://localhost:8080", api_key="admin-key")

    class TenantManager:
        def __init__(self, nx):
            self.nx = nx

        def onboard_tenant(self, tenant_id, company_name, plan):
            """Complete tenant onboarding"""
            workspace_path = f"/tenants/{tenant_id}"

            # 1. Create workspace
            self.nx.workspace.create(
                workspace_path,
                metadata={
                    "tenant_id": tenant_id,
                    "company": company_name,
                    "plan": plan,
                    "onboarded_at": datetime.now().isoformat()
                }
            )

            # 2. Create directory structure
            subdirs = ["data", "models", "exports", "logs"]
            for subdir in subdirs:
                self.nx.mkdir(f"{workspace_path}/{subdir}", parents=True)

            # 3. Set up permissions
            admin_email = f"admin@{tenant_id}.com"
            self.nx.rebac_create("user", admin_email, "owner", "file", workspace_path)

            # 4. Create initial snapshot
            self.nx.workspace.snapshot(
                workspace_path,
                name="initial-setup",
                metadata={"milestone": "onboarding complete"}
            )

            print(f"✅ Tenant {company_name} onboarded successfully")
            return workspace_path

        def daily_backup(self, tenant_id):
            """Create daily backup snapshot"""
            workspace_path = f"/tenants/{tenant_id}"

            snapshot_name = f"daily-{datetime.now().strftime('%Y%m%d')}"
            snapshot_id = self.nx.workspace.snapshot(
                workspace_path,
                name=snapshot_name
            )

            print(f"Created backup: {snapshot_name}")
            return snapshot_id

        def restore_tenant(self, tenant_id, snapshot_id):
            """Restore tenant to previous state"""
            workspace_path = f"/tenants/{tenant_id}"
            self.nx.workspace.restore(workspace_path, snapshot_id)
            print(f"✅ Restored tenant {tenant_id}")

    # Usage
    manager = TenantManager(nx)

    # Onboard new tenant
    manager.onboard_tenant("acme-123", "Acme Corp", "enterprise")

    # Daily backups
    snapshot_id = manager.daily_backup("acme-123")

    # Restore if needed
    manager.restore_tenant("acme-123", snapshot_id)
    ```

=== "AI Agent Memory Management"

    ```python
    import nexus
    import json
    from datetime import datetime

    nx = nexus.connect(config={"data_dir": "./nexus-data"})

    class AgentMemoryManager:
        def __init__(self, nx, agent_id):
            self.nx = nx
            self.agent_id = agent_id
            self.workspace_path = f"/agents/{agent_id}"

            # Create agent workspace
            nx.mkdir(self.workspace_path, parents=True)

        def start_session(self, user_id):
            """Start new conversation session"""
            session = self.nx.session.create(
                agent_id=self.agent_id,
                metadata={
                    "user_id": user_id,
                    "started_at": datetime.now().isoformat()
                }
            )

            # Create session memory directory
            session_path = f"{self.workspace_path}/sessions/{session['session_id']}"
            self.nx.mkdir(session_path, parents=True)

            return session

        def save_conversation(self, session_id, messages):
            """Save conversation to session"""
            session_path = f"{self.workspace_path}/sessions/{session_id}"

            conversation_file = f"{session_path}/conversation.json"
            self.nx.write(
                conversation_file,
                json.dumps({
                    "messages": messages,
                    "timestamp": datetime.now().isoformat()
                }).encode()
            )

        def snapshot_session(self, session_id):
            """Create snapshot of session state"""
            session_path = f"{self.workspace_path}/sessions/{session_id}"

            snapshot_id = self.nx.workspace.snapshot(
                session_path,
                name=f"session-{session_id}-checkpoint"
            )

            return snapshot_id

        def recall_session(self, session_id):
            """Load session conversation history"""
            session_path = f"{self.workspace_path}/sessions/{session_id}"
            conversation_file = f"{session_path}/conversation.json"

            content = self.nx.read(conversation_file)
            return json.loads(content.decode())

    # Usage
    agent = AgentMemoryManager(nx, agent_id="gpt-4-assistant")

    # Start session
    session = agent.start_session(user_id="user_123")

    # Save conversation
    messages = [
        {"role": "user", "content": "What is Nexus?"},
        {"role": "assistant", "content": "Nexus is an AI-native filesystem..."}
    ]
    agent.save_conversation(session['session_id'], messages)

    # Create checkpoint
    snapshot_id = agent.snapshot_session(session['session_id'])

    # Later: recall conversation
    history = agent.recall_session(session['session_id'])
    print(f"Loaded {len(history['messages'])} messages")
    ```

## 💡 Best Practices

=== "Workspace Naming"

    ```python
    # ✅ Good: Use hierarchical, descriptive names
    "/tenants/acme-corp/production"
    "/projects/alpha/backend"
    "/agents/gpt-4/user-123"

    # ❌ Bad: Flat, non-descriptive names
    "/workspace1"
    "/data"
    "/temp"
    ```

=== "Regular Snapshots"

    ```python
    import nexus
    from datetime import datetime

    nx = nexus.connect(config={"data_dir": "./nexus-data"})

    # ✅ Good: Regular automated snapshots
    def daily_snapshot_job():
        workspaces = nx.workspace.list()
        for ws in workspaces:
            snapshot_name = f"daily-{datetime.now().strftime('%Y%m%d')}"
            nx.workspace.snapshot(ws['path'], name=snapshot_name)

    # Schedule daily_snapshot_job() to run every day
    ```

## 🏃 Run the Full Demo

Try the workspace and session demo:

```bash
# Start server
./scripts/init-nexus-with-auth.sh

# In another terminal
source .nexus-admin-env
./examples/cli/workspace_session_demo.sh
```

## 📚 What's Next?

- **[File Operations](file-operations.md)** - Master file manipulation
- **[Permissions](permissions.md)** - Fine-grained access control
- **[Multi-Tenancy Guide](../MULTI_TENANT.md)** - Architecture deep dive
