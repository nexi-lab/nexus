# Examples

Explore Nexus through hands-on examples that demonstrate key features and real-world use cases.

## 🚀 Quick Start Examples

<div class="grid cards" markdown>

-   :material-file-document:{ .lg .middle } __File Operations__

    ---

    Learn how to read, write, copy, move, and delete files with optimistic concurrency control.

    [:octicons-arrow-right-24: View Example](file-operations.md)

-   :material-folder:{ .lg .middle } __Directory Operations__

    ---

    Master directory management with hierarchical permissions and automatic inheritance.

    [:octicons-arrow-right-24: View Example](directory-operations.md)

-   :material-brain:{ .lg .middle } __Agentic Context Engineering (ACE)__

    ---

    Enable AI agents to learn from experience through self-reflection and memory.

    [:octicons-arrow-right-24: View Example](ace.md)

-   :material-shield-lock:{ .lg .middle } __Permission Management__

    ---

    Set up fine-grained access control with Google Zanzibar-style ReBAC permissions.

    [:octicons-arrow-right-24: View Example](permissions.md)

-   :material-application:{ .lg .middle } __Workspace & Sessions__

    ---

    Build multi-tenant applications with workspace isolation and session tracking.

    [:octicons-arrow-right-24: View Example](workspace-session.md)

</div>

## 💡 Use Case Examples

=== "AI Agent Memory"

    Store and query agent context with semantic search.

    ```python
    import nexus
    import json

    # Connect in embedded mode (no auth needed)
    nx = nexus.connect(config={"data_dir": "./nexus-data"})

    # Store conversation
    conversation = {
        "user": "What are your pricing tiers?",
        "assistant": "We offer Basic ($10/mo), Pro ($50/mo), Enterprise (custom)",
        "timestamp": "2024-01-15T10:30:00Z"
    }

    nx.write(
        "/agent/memory/conversation.json",
        json.dumps(conversation).encode(),
        metadata={"agent_id": "gpt-4", "session": "abc123"}
    )

    # Query semantic memory
    results = nx.search("/agent/memory", query="pricing preferences")
    print(f"Found {len(results)} relevant conversations")
    ```

=== "Multi-Tenant SaaS"

    Complete tenant isolation with automatic permissions.

    ```python
    import nexus

    # Admin creates tenant workspace
    nx = nexus.connect(remote_url="https://nexus.example.com", api_key="admin-key")

    nx.workspace.create(
        "/tenant/acme-corp",
        tenant_id="acme-123",
        metadata={"company": "Acme Corp", "plan": "enterprise"}
    )

    # Grant tenant admin permissions
    nx.rebac_create("user", "admin@acme.com", "owner", "file", "/tenant/acme-corp")

    # User writes to their tenant workspace
    user_nx = nexus.connect(remote_url="https://nexus.example.com", api_key="user-key")
    user_nx.write("/tenant/acme-corp/data.json", b'{"records": 1000}')
    ```

=== "Version Control"

    Track every change with built-in versioning.

    ```python
    import nexus

    nx = nexus.connect(config={"data_dir": "./nexus-data"})

    # Write initial version
    nx.write("/model/weights.pkl", b"version 1 data")
    nx.write("/model/weights.pkl", b"version 2 data")
    nx.write("/model/weights.pkl", b"version 3 data")

    # View history
    versions = nx.versions.history("/model/weights.pkl")
    for v in versions:
        print(f"Version {v.version_number} at {v.timestamp}")

    # Roll back
    nx.versions.rollback("/model/weights.pkl", version=2)

    # Create workspace snapshot
    snapshot = nx.workspace.snapshot("/project", name="before-refactor")
    ```

=== "Semantic Search"

    Find files by meaning, not just name.

    ```python
    import nexus

    nx = nexus.connect(config={"data_dir": "./nexus-data"})

    # Store documents (automatically indexed)
    docs = [
        "Machine learning improves model accuracy",
        "Deep neural networks for image classification",
        "Natural language processing with transformers"
    ]

    for i, doc in enumerate(docs):
        nx.write(f"/docs/doc{i}.txt", doc.encode())

    # Semantic search
    results = nx.search("/docs", query="AI and computer vision", limit=5)

    for result in results:
        print(f"{result.path}: {result.score}")
    ```

## 📚 Interactive Demos

All examples include runnable shell scripts that demonstrate the full workflow:

| Example | Script | What It Demonstrates |
|---------|--------|---------------------|
| File Operations | `examples/cli/file_operations_demo.sh` | Write, read, copy, move, delete with metadata |
| Directory Operations | `examples/cli/directory_operations_demo.sh` | Create directories with permission inheritance |
| Permissions | `examples/cli/permissions_demo_enhanced.sh` | ReBAC permissions, groups, and inheritance |
| Workspace & Sessions | `examples/cli/workspace_session_demo.sh` | Multi-tenant isolation and session tracking |
| Advanced Usage | `examples/cli/advanced_usage_demo.sh` | Mounts, versioning, and advanced features |
| ACE Learning Agent | `examples/ace/demo_3_data_validator.py` | Agent learns validation rules from experience |

## 🎯 What's Next?

- **[Quick Start Guide](../getting-started/quickstart.md)** - Get up and running in 30 seconds
- **[API Reference](../api/index.md)** - Complete API documentation
- **[Deployment Guide](../deployment/index.md)** - Production deployment patterns
