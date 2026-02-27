# CLI: Memory Management

← [CLI Reference](index.md) | [API Documentation](../README.md)

This document describes CLI commands for memory management and their Python API equivalents.

Memory in Nexus provides a structured way to store and retrieve contextual information, knowledge, and agent experiences.

---

## Table of Contents

### Memory Operations (v0.4.0+)
- [memory store](#memory-store---store-memory) - Store a memory
- [memory query](#memory-query---query-memories) - Query by filters
- [memory search](#memory-search---semantic-search) - Semantic search
- [memory list](#memory-list---list-memories) - List memories
- [memory get](#memory-get---get-memory) - Get specific memory
- [memory delete](#memory-delete---delete-memory) - Delete memory

### Memory Registry (v0.7.0+)
- [memory register](#memory-register---register-memory-directory) - Register memory directory
- [memory list-registered](#memory-list-registered---list-registered-memories) - List registered memories
- [memory info](#memory-info---show-memory-info) - Show memory info
- [memory unregister](#memory-unregister---unregister-memory) - Unregister memory

---

## Memory Operations (v0.4.0+)

### memory store - Store memory

Store a new memory entry.

**CLI:**
```bash
# Store user preference
nexus memory store "User prefers Python" --scope user --type preference

# Store agent knowledge with importance
nexus memory store "API key for prod: abc123" --scope agent --importance 0.9

# Store experience
nexus memory store "Deployment failed on 2024-01-15" --scope user --type experience
```

**Python API:**
```python
# Store memory
memory_id = nx.memory.store(
    content="User prefers Python",
    scope="user",
    memory_type="preference"
)

# Store with importance
memory_id = nx.memory.store(
    content="API key for prod: abc123",
    scope="agent",
    importance=0.9
)

# Store experience
memory_id = nx.memory.store(
    content="Deployment failed on 2024-01-15",
    scope="user",
    memory_type="experience"
)
```

**Options:**
- `--scope TEXT`: Memory scope (agent/user/tenant/global) - default: "user"
- `--type TEXT`: Memory type (fact/preference/experience) - optional
- `--importance FLOAT`: Importance score (0.0-1.0) - optional

**Memory Scopes:**
- `agent`: Private to this agent instance
- `user`: Shared across user's agents
- `tenant`: Shared within organization
- `global`: Shared globally (requires permissions)

**Memory Types:**
- `fact`: Factual knowledge ("Python uses indentation")
- `preference`: User/agent preferences ("Prefers concise code")
- `experience`: Past experiences ("Failed deployment at 3pm")

---

### memory query - Query memories

Query memories using filters.

**CLI:**
```bash
# Query user preferences
nexus memory query --scope user --type preference

# Query agent memories
nexus memory query --agent-id agent1 --limit 10

# Query with JSON output
nexus memory query --json
```

**Python API:**
```python
# Query user preferences
results = nx.memory.query(scope="user", memory_type="preference")

# Query by agent
results = nx.memory.query(agent_id="agent1", limit=10)

# Query with multiple filters
results = nx.memory.query(
    user_id="alice",
    scope="user",
    memory_type="fact",
    limit=50
)
```

**Options:**
- `--user-id TEXT`: Filter by user ID
- `--agent-id TEXT`: Filter by agent ID
- `--scope TEXT`: Filter by scope
- `--type TEXT`: Filter by memory type
- `--limit INT`: Maximum results (default: 100)
- `--json`: Output as JSON

---

### memory search - Semantic search

Search memories using semantic search (vector similarity).

**CLI:**
```bash
# Semantic search
nexus memory search "Python programming best practices"

# Search with filters
nexus memory search "user preferences" --scope user --limit 5

# Search with JSON output
nexus memory search "API keys" --json
```

**Python API:**
```python
# Semantic search
results = nx.memory.search(query="Python programming best practices")

# Search with filters
results = nx.memory.search(
    query="user preferences",
    scope="user",
    limit=5
)

# Process results
for result in results:
    print(f"Score: {result['score']:.2f}")
    print(f"Content: {result['content']}")
    print(f"Type: {result.get('memory_type', 'N/A')}")
```

**Options:**
- `--scope TEXT`: Filter by scope
- `--type TEXT`: Filter by memory type
- `--limit INT`: Maximum results (default: 10)
- `--json`: Output as JSON

**Note:** Semantic search requires embeddings to be enabled on the server.

---

### memory list - List memories

List memories for current user/agent.

**CLI:**
```bash
# List all memories
nexus memory list

# List with filters
nexus memory list --scope user --type preference

# List with JSON output
nexus memory list --json
```

**Python API:**
```python
# List all memories
results = nx.memory.list()

# List with filters
results = nx.memory.list(scope="user", memory_type="preference")

# Iterate through results
for mem in results:
    print(f"{mem['memory_id']}: {mem['content'][:50]}...")
```

**Options:**
- `--scope TEXT`: Filter by scope
- `--type TEXT`: Filter by memory type
- `--limit INT`: Maximum results (default: 100)
- `--json`: Output as JSON

---

### memory get - Get memory

Get a specific memory by ID.

**CLI:**
```bash
# Get memory by ID
nexus memory get mem_123

# Get with JSON output
nexus memory get mem_123 --json
```

**Python API:**
```python
# Get memory
memory = nx.memory.get("mem_123")

if memory:
    print(f"Content: {memory['content']}")
    print(f"Scope: {memory['scope']}")
    print(f"Type: {memory['memory_type']}")
    print(f"Importance: {memory['importance']}")
```

**Options:**
- `--json`: Output as JSON

---

### memory delete - Delete memory

Delete a memory by ID.

**CLI:**
```bash
# Delete memory
nexus memory delete mem_123
```

**Python API:**
```python
# Delete memory
deleted = nx.memory.delete("mem_123")

if deleted:
    print("Memory deleted successfully")
else:
    print("Memory not found or no permission")
```

---

## Memory Registry (v0.7.0+)

### memory register - Register memory directory

Register a directory as a memory for persistent knowledge storage.

**CLI:**
```bash
# Register persistent memory
nexus memory register /knowledge-base --name kb --description "Knowledge base"

# Register with creator metadata
nexus memory register /kb --name kb --created-by alice

# Register temporary session-scoped memory (v0.5.0)
nexus memory register /tmp/agent-context --session-id abc123 --ttl 2h
```

**Python API:**
```python
# Register persistent memory
config = nx.register_memory(
    path="/knowledge-base",
    name="kb",
    description="Knowledge base"
)

# Register with metadata
config = nx.register_memory(
    path="/kb",
    name="kb",
    description="Knowledge base",
    created_by="alice"
)

# Register session-scoped (v0.5.0)
from datetime import timedelta
config = nx.register_memory(
    path="/tmp/agent-context",
    name="temp-kb",
    session_id="abc123",
    ttl=timedelta(hours=2)
)
```

**Options:**
- `--name, -n TEXT`: Friendly name (optional)
- `--description, -d TEXT`: Description (optional)
- `--created-by TEXT`: Creator name (optional)
- `--session-id TEXT`: Session ID for temporary memory (v0.5.0)
- `--ttl TEXT`: Time-to-live (e.g., '8h', '2d', '30m') (v0.5.0)

**TTL Format Examples:**
- `8h` - 8 hours
- `2d` - 2 days
- `30m` - 30 minutes
- `1w` - 1 week
- `90s` - 90 seconds

---

### memory list-registered - List registered memories

List all registered memory directories.

**CLI:**
```bash
# List all registered memories
nexus memory list-registered
```

**Python API:**
```python
# List memories
memories = nx.list_memories()
for mem in memories:
    print(f"{mem['path']} - {mem['name']}: {mem['description']}")
```

---

### memory info - Show memory info

Get detailed information about a registered memory.

**CLI:**
```bash
# Get memory details
nexus memory info /knowledge-base
```

**Python API:**
```python
# Get memory info
info = nx.get_memory_info("/knowledge-base")
if info:
    print(f"Name: {info['name']}")
    print(f"Description: {info['description']}")
    print(f"Created: {info['created_at']}")
    print(f"Created by: {info['created_by']}")
```

---

### memory unregister - Unregister memory

Unregister a memory (doesn't delete files).

**CLI:**
```bash
# Unregister (with confirmation)
nexus memory unregister /knowledge-base

# Unregister (skip confirmation)
nexus memory unregister /knowledge-base --yes
```

**Python API:**
```python
# Unregister memory
success = nx.unregister_memory("/knowledge-base")
# Note: Files are not deleted, only memory tracking is removed
```

**Options:**
- `--yes, -y`: Skip confirmation prompt

---

## Common Workflows

### Basic memory management
```bash
# Store and query memories
nexus memory store "Python uses indentation" --scope user --type fact
nexus memory store "User prefers concise explanations" --scope user --type preference
nexus memory store "Last deployment failed at 3pm" --scope agent --type experience

# Query memories
nexus memory query --scope user --type preference
nexus memory search "Python syntax"

# List and get memories
nexus memory list --scope user
nexus memory get mem_123
```

### Knowledge base workflow
```bash
# Register knowledge base
nexus memory register /docs/kb --name company-kb --description "Company knowledge"

# Store documentation
nexus memory store "Deployment: Run tests, build Docker, push, deploy k8s" \
  --scope user --type fact
nexus memory store "Code review: 2 approvals, tests passing" \
  --scope user --type fact

# Query the knowledge base
nexus memory search "deployment process"
nexus memory search "code review requirements"

# List registered memories
nexus memory list-registered

# Get memory info
nexus memory info /docs/kb
```

---

## Python Workflow Examples

### Store and query memories
```python
import nexus

nx = nexus.connect()

# Store various memory types
fact_id = nx.memory.store(
    "Python uses indentation for blocks",
    scope="user",
    memory_type="fact"
)

pref_id = nx.memory.store(
    "User prefers concise explanations",
    scope="user",
    memory_type="preference",
    importance=0.8
)

exp_id = nx.memory.store(
    "Deployment failed at 3pm on 2024-01-15",
    scope="agent",
    memory_type="experience"
)

# Query memories
preferences = nx.memory.query(scope="user", memory_type="preference")
for pref in preferences:
    print(f"Preference: {pref['content']}")

# Semantic search
results = nx.memory.search("Python syntax rules")
for result in results:
    print(f"[{result['score']:.2f}] {result['content']}")
```

### Knowledge base management
```python
# Register knowledge base
config = nx.register_memory(
    path="/docs/kb",
    name="company-kb",
    description="Company knowledge base",
    created_by="admin"
)

# Store documentation
kb_entries = [
    ("Deployment process: Run tests, build Docker image, push to registry, deploy to k8s",
     "fact"),
    ("Code review guidelines: At least 2 approvals, all tests passing",
     "fact"),
    ("On-call rotation: Week-long shifts, escalate after 30min",
     "fact"),
]

for content, memory_type in kb_entries:
    nx.memory.store(content=content, scope="user", memory_type=memory_type)

# Query the knowledge base
results = nx.memory.search("deployment process")
for result in results:
    print(f"[{result['score']:.2f}] {result['content']}")

# List registered memories
memories = nx.list_memories()
for mem in memories:
    print(f"{mem['name']}: {mem['description']}")

# Get memory info
info = nx.get_memory_info("/docs/kb")
print(f"Created by: {info['created_by']}")
print(f"Created at: {info['created_at']}")
```

### Session-scoped memory (v0.5.0)
```python
from datetime import timedelta

# Create temporary memory for notebook session
config = nx.register_memory(
    path="/tmp/notebook-memory",
    name="notebook-context",
    description="Temporary notebook context",
    session_id="session_abc123",
    ttl=timedelta(hours=2)  # Auto-expires after 2 hours
)

# Store session-specific memories
nx.memory.store(
    "Current analysis focuses on Q4 revenue",
    scope="agent",
    memory_type="fact"
)

# Memory and registration auto-delete after 2 hours
```

---

## See Also

- [CLI Reference Overview](index.md)
- [Python API: Memory Management](../memory-management.md)
- [Semantic Search](semantic-search.md)
- [Workspace Management](workspace.md)
