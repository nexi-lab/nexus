# Examples

See Nexus in action with these real-world use cases.

## AI Agent Memory

Store and query agent context with semantic search. Agents learn from past interactions and improve over time.

```python
import nexus
import json

# Connect to Nexus
nx = nexus.connect(config={"data_dir": "./nexus-data", "enforce_permissions": False})

# Store conversation with rich metadata
conversation_data = {
    "user": "What are your pricing tiers?",
    "assistant": "We offer Basic ($10/mo), Pro ($50/mo), and Enterprise (custom)",
    "timestamp": "2024-01-15T10:30:00Z"
}

nx.write(
    "/agent/memory/conversation.json",
    json.dumps(conversation_data).encode(),
    metadata={"agent_id": "gpt-4", "session": "abc123", "topic": "pricing"}
)

# Later, query semantic memory across all sessions
results = nx.search(
    "/agent/memory",
    query="user preferences about pricing"
)

print(f"Found {len(results)} relevant conversations")
```

## Multi-Tenant SaaS

Complete tenant isolation with automatic permission enforcement. Perfect for SaaS applications.

```python
import nexus

# Admin connection
nx = nexus.connect(remote_url="https://nexus.example.com", api_key="admin-key")

# Create isolated workspace for tenant
nx.workspace.create(
    "/tenant/acme-corp",
    tenant_id="acme-123",
    metadata={"company": "Acme Corp", "plan": "enterprise"}
)

# Grant permissions to tenant admin
nx.rebac_create(
    subject_type="user",
    subject_id="admin@acme.com",
    relation="owner",
    object_type="file",
    object_id="/tenant/acme-corp"
)

# User connection (permissions checked automatically)
user_nx = nexus.connect(
    remote_url="https://nexus.example.com",
    api_key="user-key"
)

# Write to tenant workspace
user_nx.write(
    "/tenant/acme-corp/data.json",
    b'{"records": 1000}',
    context={"user_id": "user-456"}
)
```

## Distributed Teams

Same API works everywhere - local development, staging, production. Zero code changes needed.

```python
import nexus

# Local development with SQLite
local_nx = nexus.connect(
    config={
        "data_dir": "./local-dev",
        "enforce_permissions": False
    }
)

# Write locally
local_nx.write("/project/config.yaml", b"env: development")

# Production with cloud storage and PostgreSQL
prod_nx = nexus.connect(
    remote_url="https://nexus.example.com",
    api_key="prod-key"
)

# Same API, different backend
prod_nx.write("/project/config.yaml", b"env: production")

# List files (same API everywhere)
local_files = local_nx.list("/project")
prod_files = prod_nx.list("/project")
```

## Versioning & Time Travel

Track every change with built-in versioning. Roll back to any point in time instantly.

```python
import nexus

nx = nexus.connect(config={"data_dir": "./nexus-data", "enforce_permissions": False})

# Write initial version
nx.write("/model/weights.pkl", b"version 1 data")

# Make changes
nx.write("/model/weights.pkl", b"version 2 data")
nx.write("/model/weights.pkl", b"version 3 data")

# View version history
versions = nx.versions.history("/model/weights.pkl")
for v in versions:
    print(f"Version {v.version_number} at {v.timestamp}")

# Get specific version
v2_data = nx.versions.get("/model/weights.pkl", version=2)

# Roll back to previous version
nx.versions.rollback("/model/weights.pkl", version=2)

# Create snapshot of entire workspace
snapshot_id = nx.workspace.snapshot("/project", name="before-refactor")

# Restore entire workspace
nx.workspace.restore("/project", snapshot_id)
```

## Semantic Search

Find files by meaning, not just name. Built-in vector search for AI applications.

```python
import nexus

nx = nexus.connect(config={"data_dir": "./nexus-data", "enforce_permissions": False})

# Store documents with automatic indexing
docs = [
    "Machine learning improves model accuracy",
    "Deep neural networks for image classification",
    "Natural language processing with transformers"
]

for i, doc in enumerate(docs):
    nx.write(f"/docs/doc{i}.txt", doc.encode())

# Semantic search across documents
results = nx.search(
    "/docs",
    query="AI and computer vision",
    limit=5
)

for result in results:
    print(f"Match: {result.path} (score: {result.score})")
```

## Permission Management

Fine-grained access control with Google Zanzibar-style ReBAC.

```python
import nexus

nx = nexus.connect(remote_url="https://nexus.example.com", api_key="admin-key")

# Create group
nx.rebac_create("user", "alice", "member", "group", "engineers")
nx.rebac_create("user", "bob", "member", "group", "engineers")

# Grant group permissions on workspace
nx.rebac_create("group", "engineers", "write", "file", "/workspace/project")

# Check permissions (returns True)
can_write = nx.rebac_check("user", "alice", "write", "file", "/workspace/project/code.py")

# Explain why permission is granted
explanation = nx.rebac_explain("user", "alice", "write", "file", "/workspace/project/code.py")
print(f"Permission granted because: {explanation}")

# Find all users with write access
users = nx.rebac_expand("write", "file", "/workspace/project")
print(f"Users with write access: {users}")
```

---

## Next Steps

- [Read the Full Documentation](api/index.md)
- [View Quick Start Guide](index.md#quick-start-in-30-seconds)
- [Explore GitHub Repository](https://github.com/nexi-lab/nexus)
