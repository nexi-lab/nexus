# CLI: Memory Management

← [CLI Reference](index.md) | [API Documentation](../README.md)

This document describes CLI commands for memory management and their Python API equivalents.

Memory in Nexus provides a structured way to store and retrieve contextual information, knowledge, and notes.

## memory register - Register memory

Register a directory as a memory for tracking.

**CLI:**
```bash
# Register memory
nexus memory register /knowledge-base --name kb --description "Knowledge base"

# With metadata
nexus memory register /kb --name kb --created-by alice
```

**Python API:**
```python
# Register memory
nx.register_memory("/knowledge-base", name="kb", description="Knowledge base")

# With metadata
nx.register_memory(
    "/kb",
    name="kb",
    description="Knowledge base",
    metadata={"created_by": "alice", "category": "technical"}
)
```

**Options:**
- `--name TEXT`: Memory name (required)
- `--description TEXT`: Description of the memory
- `--created-by TEXT`: Creator name
- `--remote-url URL`: Connect to remote server (use NEXUS_URL env var)
- `--remote-api-key KEY`: API key (use NEXUS_API_KEY env var)

**See Also:**
- [Python API: register_memory()](../memory-management.md#register_memory)

---

## memory list-registered - List memories

List all registered memories.

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

**See Also:**
- [Python API: list_memories()](../memory-management.md#list_memories)

---

## memory info - Show memory info

Get detailed information about a memory.

**CLI:**
```bash
# Get memory details
nexus memory info /knowledge-base
```

**Python API:**
```python
# Get memory info
info = nx.get_memory_info("/knowledge-base")
print(f"Name: {info['name']}")
print(f"Description: {info['description']}")
print(f"Entry count: {info['entry_count']}")
print(f"Created: {info['created_at']}")
```

**See Also:**
- [Python API: get_memory_info()](../memory-management.md#get_memory_info)

---

## memory unregister - Unregister memory

Unregister a memory (doesn't delete files).

**CLI:**
```bash
# Unregister (doesn't delete files)
nexus memory unregister /knowledge-base
```

**Python API:**
```python
# Unregister memory
nx.unregister_memory("/knowledge-base")
# Note: Files are not deleted, only memory tracking is removed
```

**See Also:**
- [Python API: unregister_memory()](../memory-management.md#unregister_memory)

---

## memory store - Store memory

Store a memory entry.

**CLI:**
```bash
# Store a memory entry
nexus memory store --content "Important fact" --tags learning,important

# Note: Memory commands require NEXUS_URL environment variable
export NEXUS_URL=http://localhost:8765
export NEXUS_API_KEY=your-api-key
nexus memory store --content "Authentication uses JWT tokens" --tags security,auth
```

**Python API:**
```python
# Store memory
memory_id = nx.store_memory(
    content="Important fact",
    tags=["learning", "important"]
)
print(f"Stored memory: {memory_id}")

# Store with metadata
memory_id = nx.store_memory(
    content="Authentication uses JWT tokens",
    tags=["security", "auth"],
    metadata={"source": "docs", "author": "alice"}
)
```

**Options:**
- `--content TEXT`: Memory content (required)
- `--tags TEXT`: Comma-separated tags

**Environment:**
- Requires `NEXUS_URL` environment variable
- Requires `NEXUS_API_KEY` if server uses authentication

**See Also:**
- [Python API: store_memory()](../memory-management.md#store_memory)

---

## memory search - Semantic search memories

Search memories using semantic search.

**CLI:**
```bash
# Search memories
nexus memory search "authentication flow"

# Note: Requires NEXUS_URL environment variable
export NEXUS_URL=http://localhost:8765
export NEXUS_API_KEY=your-api-key
nexus memory search "how to authenticate users"
```

**Python API:**
```python
# Search memories (async)
import asyncio

async def search_memories():
    results = await nx.search_memories("authentication flow")
    for result in results:
        print(f"Score: {result['score']}")
        print(f"Content: {result['content']}")
        print(f"Tags: {result.get('tags', [])}")

asyncio.run(search_memories())

# Search with limit
async def search_top_results():
    results = await nx.search_memories("authentication flow", limit=5)
    return results
```

**Options:**
- `--limit NUM`: Maximum number of results

**Environment:**
- Requires `NEXUS_URL` environment variable
- Requires `NEXUS_API_KEY` if server uses authentication

**See Also:**
- [Python API: search_memories()](../memory-management.md#search_memories)
- [Semantic Search](semantic-search.md)

---

## Common Workflows

### Basic memory management
```bash
# Set up remote connection
export NEXUS_URL=http://localhost:8765
export NEXUS_API_KEY=your-api-key

# Register a memory
nexus memory register /knowledge --name kb --description "Team knowledge base"

# Store some memories
nexus memory store --content "Use bcrypt for password hashing" --tags security,best-practice
nexus memory store --content "Database migrations use Alembic" --tags database,tools
nexus memory store --content "API rate limit is 100 req/min" --tags api,limits

# Search memories
nexus memory search "password security"
nexus memory search "database tools"

# List all registered memories
nexus memory list-registered

# Get memory info
nexus memory info /knowledge
```

### Python equivalent
```python
import nexus
import asyncio

# Initialize with remote server
nx = nexus.Nexus(remote_url="http://localhost:8765", api_key="your-api-key")

# Register a memory
nx.register_memory("/knowledge", name="kb", description="Team knowledge base")

# Store some memories
memories = [
    ("Use bcrypt for password hashing", ["security", "best-practice"]),
    ("Database migrations use Alembic", ["database", "tools"]),
    ("API rate limit is 100 req/min", ["api", "limits"]),
]

for content, tags in memories:
    memory_id = nx.store_memory(content=content, tags=tags)
    print(f"Stored: {memory_id}")

# Search memories
async def search():
    results = await nx.search_memories("password security")
    print("\nPassword security results:")
    for result in results:
        print(f"  - {result['content']} (score: {result['score']})")

    results = await nx.search_memories("database tools")
    print("\nDatabase tools results:")
    for result in results:
        print(f"  - {result['content']} (score: {result['score']})")

asyncio.run(search())

# List all registered memories
memories = nx.list_memories()
for mem in memories:
    print(f"{mem['name']}: {mem['description']}")

# Get memory info
info = nx.get_memory_info("/knowledge")
print(f"\nMemory: {info['name']}")
print(f"Entries: {info['entry_count']}")
```

### Knowledge base workflow
```bash
export NEXUS_URL=http://localhost:8765
export NEXUS_API_KEY=your-api-key

# Create knowledge base
nexus memory register /docs/kb --name company-kb --description "Company knowledge"

# Store documentation
nexus memory store --content "Deployment process: Run tests, build Docker image, push to registry, deploy to k8s" --tags deployment,process
nexus memory store --content "Code review guidelines: At least 2 approvals, all tests passing, no merge conflicts" --tags process,quality
nexus memory store --content "On-call rotation: Week-long shifts, escalate after 30min, document incidents" --tags oncall,process

# Query the knowledge base
nexus memory search "how to deploy"
nexus memory search "code review requirements"
nexus memory search "on-call procedures"
```

### Python equivalent
```python
import asyncio

nx = nexus.Nexus(remote_url="http://localhost:8765", api_key="your-api-key")

# Create knowledge base
nx.register_memory("/docs/kb", name="company-kb", description="Company knowledge")

# Store documentation
kb_entries = [
    ("Deployment process: Run tests, build Docker image, push to registry, deploy to k8s",
     ["deployment", "process"]),
    ("Code review guidelines: At least 2 approvals, all tests passing, no merge conflicts",
     ["process", "quality"]),
    ("On-call rotation: Week-long shifts, escalate after 30min, document incidents",
     ["oncall", "process"]),
]

for content, tags in kb_entries:
    nx.store_memory(content=content, tags=tags)

# Query the knowledge base
async def query_kb():
    queries = [
        "how to deploy",
        "code review requirements",
        "on-call procedures"
    ]

    for query in queries:
        print(f"\nQuery: {query}")
        results = await nx.search_memories(query, limit=3)
        for result in results:
            print(f"  [{result['score']:.2f}] {result['content']}")

asyncio.run(query_kb())
```

---

## See Also

- [CLI Reference Overview](index.md)
- [Python API: Memory Management](../memory-management.md)
- [Semantic Search](semantic-search.md)
- [Workspace Management](workspace.md)
