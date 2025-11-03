# AI Agent Memory

**Give your AI agents persistent memory that survives across sessions**

⏱️ **Time:** 15 minutes | 💡 **Difficulty:** Medium

## What You'll Learn

- Store facts, preferences, and experiences
- Query and search agent memories
- Use memory scopes (agent, user, tenant, global)
- Build agents with persistent memory
- Retrieve memory across sessions
- Use both Python and CLI for memory

## Prerequisites

✅ Completed [Simple File Storage](simple-file-storage.md) tutorial
✅ Nexus server running
✅ Basic understanding of AI agents

## Overview

Agent memory in Nexus allows AI agents to remember information across conversations and sessions. Instead of starting fresh each time, agents can:

- **Remember facts**: "The user's timezone is UTC-5"
- **Store preferences**: "User prefers Python over JavaScript"
- **Learn from experiences**: "Deployment failed on Jan 15 at 3pm"
- **Share knowledge**: Memories can be scoped to agents, users, or teams

**Architecture:**

```
┌─────────────────┐
│   AI Agent      │  ← Your agent application
│   Session 1     │
└────────┬────────┘
         │ store_memory("User prefers Python")
         ↓
┌─────────────────┐
│  Nexus Server   │  ← Persistent memory storage
│  Memory API     │
└────────┬────────┘
         │
         ↓
┌─────────────────┐
│   Database      │  ← Memories persist here
└─────────────────┘

... Later ...

┌─────────────────┐
│   AI Agent      │  ← New session, same agent
│   Session 2     │
└────────┬────────┘
         │ query_memory("programming language")
         ↓
┌─────────────────┐
│  Nexus Server   │  ← Retrieves stored memory
│  Memory API     │
└────────┬────────┘
         │
         ↓
"User prefers Python"  ← Memory from Session 1!
```

---

## Step 1: Start Nexus Server

Start Nexus server with database authentication:

```bash
# First-time setup: Initialize server with admin user
nexus serve --auth-type database --init --port 8765

# Output:
# ✓ Admin user created: admin
# ✓ API key: nxk_abc123def456...
# Save this API key - you'll need it for client connections!
```

**Save the API key** - this is the only time it will be displayed!

For subsequent starts (after initialization):

```bash
# Restart server (already initialized)
nexus serve --auth-type database --port 8765
```

**Verify server is running:**
```bash
curl http://localhost:8765/health
```

**Expected output:**
```json
{"status":"ok","version":"0.5.2"}
```

---

## Step 2: Store Your First Memory (CLI)

Let's store some information that an agent should remember:

```bash
# Store a user preference
nexus memory store "User prefers Python over JavaScript" \
  --scope user \
  --type preference

# Store a fact
nexus memory store "The staging API is at https://api-staging.example.com" \
  --scope user \
  --type fact

# Store an experience
nexus memory store "Deployment to production succeeded on 2025-01-15" \
  --scope user \
  --type experience
```

**Expected output:**
```
✓ Memory stored: mem_abc123...
✓ Memory stored: mem_def456...
✓ Memory stored: mem_ghi789...
```

🎉 **Success!** You've stored your first memories in Nexus.

---

## Step 3: Query Memories

Now let's retrieve the memories we just stored:

```bash
# List all user memories
nexus memory query --scope user

# Filter by type
nexus memory query --scope user --type preference

# Limit results
nexus memory query --scope user --limit 5
```

**Output:**
```
Found 3 memories:

Memory ID: mem_abc123...
  Content: User prefers Python over JavaScript
  Scope: user
  Type: preference
  Created: 2025-01-15T10:30:00Z

Memory ID: mem_def456...
  Content: The staging API is at https://api-staging.example.com
  Scope: user
  Type: fact
  Created: 2025-01-15T10:30:15Z

Memory ID: mem_ghi789...
  Content: Deployment to production succeeded on 2025-01-15
  Scope: user
  Type: experience
  Created: 2025-01-15T10:30:30Z
```

---

## Step 4: Search Memories Semantically

Use semantic search to find relevant memories by meaning, not just keywords:

```bash
# Search for programming-related memories
nexus memory search "programming language" --limit 3

# Search for deployment info
nexus memory search "deployment" --limit 3

# Search for API information
nexus memory search "API endpoint" --limit 3
```

**Output:**
```
Found 1 matching memory:

Content: User prefers Python over JavaScript
  Type: preference
  Score: 0.89
  Memory ID: mem_abc123...
```

The semantic search found the relevant memory even though we searched for "programming language" and the memory says "Python over JavaScript"!

---

## Step 5: Memory Quality Control (Approve/Deactivate)

Nexus includes a memory approval workflow for quality control. **New memories start as "inactive" by default** and must be approved before they're used by queries.

### Why Approval Matters

- **Quality Control**: Review memories before they affect agent behavior
- **Privacy**: Ensure sensitive information is approved before use
- **Memory Hygiene**: Temporarily disable outdated memories without deletion

### List Inactive Memories

```bash
# View memories pending approval (state=inactive)
nexus memory list --state inactive

# View approved memories (state=active)
nexus memory list --state active

# View ALL memories (both inactive and active)
nexus memory list --state all
```

**Output:**
```
Inactive Memories (3):

Memory ID: mem_abc123...
  Content: User prefers Python over JavaScript
  State: inactive  ← Pending approval
  Type: preference
  Created: 2025-01-15T10:30:00Z
```

### Approve Individual Memories

Approve a memory to make it "active" and usable:

```bash
# Approve a specific memory
nexus memory approve mem_abc123...
```

**Python:**
```python
# Approve a memory
success = nx.memory.approve("mem_abc123...")
if success:
    print("✓ Memory approved and now active")
```

### Approve Multiple Memories (Batch)

Approve multiple memories at once:

```bash
# Approve multiple memories
nexus memory approve-batch mem_abc123... mem_def456... mem_ghi789...
```

**Python:**
```python
# Batch approve
result = nx.memory.approve_batch([
    "mem_abc123...",
    "mem_def456...",
    "mem_ghi789..."
])

print(f"Approved: {result['approved']}")
print(f"Failed: {result['failed']}")
```

### Deactivate Memories

Temporarily disable a memory without deleting it:

```bash
# Deactivate a memory (makes it inactive)
nexus memory deactivate mem_abc123...
```

**Python:**
```python
# Deactivate a memory
nx.memory.deactivate("mem_abc123...")
```

### Default Behavior: Active Memories Only

**Important:** By default, queries and searches only return **active** memories:

```bash
# Returns only ACTIVE memories (default)
nexus memory query --scope user

# Explicitly get active memories
nexus memory query --scope user --state active

# Get inactive memories (pending approval)
nexus memory query --scope user --state inactive

# Get ALL memories (both active and inactive)
nexus memory query --scope user --state all
```

**Python:**
```python
# Default: returns only active memories
active_memories = nx.memory.query(scope="user")

# Explicitly query inactive (pending approval)
inactive_memories = nx.memory.query(scope="user", state="inactive")

# Get all memories regardless of state
all_memories = nx.memory.query(scope="user", state="all")
```

### Complete Approval Workflow Example

```python
#!/usr/bin/env python3
import asyncio
from nexus import connect

async def main():
    async with connect() as nx:
        print("=== Memory Approval Workflow ===\n")

        # 1. Store a memory (starts as inactive)
        print("1️⃣ Storing new memory (will be inactive)...")
        mem_id = nx.memory.store(
            content="User prefers dark theme",
            scope="user",
            memory_type="preference"
        )
        print(f"   ✓ Stored: {mem_id}\n")

        # 2. Check inactive memories
        print("2️⃣ Checking inactive memories...")
        inactive = nx.memory.query(scope="user", state="inactive")
        print(f"   Found {len(inactive)} inactive memories\n")

        # 3. Default query returns only active (won't include new memory)
        print("3️⃣ Default query (active only)...")
        active = nx.memory.query(scope="user")  # state="active" by default
        print(f"   Active memories: {len(active)}")
        print(f"   (New memory NOT included - still inactive)\n")

        # 4. Approve the memory
        print("4️⃣ Approving memory...")
        success = nx.memory.approve(mem_id)
        print(f"   {'✓' if success else '✗'} Approved\n")

        # 5. Now it appears in default queries
        print("5️⃣ Query again (active only)...")
        active = nx.memory.query(scope="user")
        print(f"   Active memories: {len(active)}")
        print(f"   (New memory NOW included!)\n")

        print("✨ Approval workflow complete!")

if __name__ == "__main__":
    asyncio.run(main())
```

**Output:**
```
=== Memory Approval Workflow ===

1️⃣ Storing new memory (will be inactive)...
   ✓ Stored: mem_xyz789...

2️⃣ Checking inactive memories...
   Found 1 inactive memories

3️⃣ Default query (active only)...
   Active memories: 0
   (New memory NOT included - still inactive)

4️⃣ Approving memory...
   ✓ Approved

5️⃣ Query again (active only)...
   Active memories: 1
   (New memory NOW included!)

✨ Approval workflow complete!
```

---

## Step 6: Python SDK - Build a Memory-Enabled Agent

Now let's build a simple agent with persistent memory using Python.

### Connection Setup

First, configure your connection to the Nexus server:

```python
import nexus

# Option 1: Use environment variables (recommended)
# export NEXUS_URL=http://localhost:8765
# export NEXUS_API_KEY=nxk_abc123...
nx = nexus.connect()

# Option 2: Explicit configuration
nx = nexus.connect(config={
    "url": "http://localhost:8765",
    "api_key": "nxk_abc123..."
})
```

### Complete Example

```python
# memory_agent_demo.py
import asyncio
from nexus import connect

async def main():
    # Connect to Nexus server (reads NEXUS_URL and NEXUS_API_KEY from environment)
    async with connect() as nx:
        print("=== AI Agent with Memory ===\n")

        # Store some memories
        print("1️⃣ Storing memories...")

        nx.memory.store(
            content="User's name is Alice",
            scope="user",
            memory_type="fact"
        )

        nx.memory.store(
            content="User prefers concise code examples",
            scope="user",
            memory_type="preference"
        )

        nx.memory.store(
            content="User asked about file operations yesterday",
            scope="user",
            memory_type="experience"
        )

        print("   ✓ Stored 3 memories\n")

        # Query all user memories
        print("2️⃣ Querying memories...")

        memories = nx.memory.query(scope="user")
        print(f"   Found {len(memories)} memories:")
        for mem in memories:
            print(f"   - {mem['content'][:50]}...")
        print()

        # Search for specific information
        print("3️⃣ Searching for user's name...")

        results = nx.memory.search(query="user name", limit=1)
        if results:
            print(f"   📝 {results[0]['content']}")
        print()

        # Search for preferences
        print("4️⃣ Searching for preferences...")

        results = nx.memory.search(query="code style preference", limit=1)
        if results:
            print(f"   ⭐ {results[0]['content']}")
        print()

        print("✨ Agent successfully stored and retrieved memories!")

if __name__ == "__main__":
    asyncio.run(main())
```

**Run it:**

```bash
# Make sure NEXUS_URL and NEXUS_API_KEY are set
python memory_agent_demo.py
```

**Expected output:**
```
=== AI Agent with Memory ===

1️⃣ Storing memories...
   ✓ Stored 3 memories

2️⃣ Querying memories...
   Found 3 memories:
   - User's name is Alice...
   - User prefers concise code examples...
   - User asked about file operations yesterday...

3️⃣ Searching for user's name...
   📝 User's name is Alice

4️⃣ Searching for preferences...
   ⭐ User prefers concise code examples

✨ Agent successfully stored and retrieved memories!
```

---

## Step 7: Memory Scopes

Nexus supports different memory scopes for different sharing needs:

### Agent Scope (Private)

Memories private to a specific agent instance:

```python
# Only this agent can see this memory
nx.memory.store(
    content="My internal state: processing request 12345",
    scope="agent",
    memory_type="fact"
)
```

```bash
nexus memory store "Agent internal state" --scope agent
```

### User Scope (Shared Across User's Agents)

Memories shared across all of a user's agents:

```python
# All user's agents can access this
nx.memory.store(
    content="User prefers dark mode",
    scope="user",
    memory_type="preference"
)
```

```bash
nexus memory store "User prefers dark mode" --scope user --type preference
```

### Tenant Scope (Team/Organization)

Memories shared within an organization:

```python
# All agents in the organization can access
nx.memory.store(
    content="Company policy: Code reviews required",
    scope="tenant",
    memory_type="fact"
)
```

```bash
nexus memory store "Company policy" --scope tenant
```

### Global Scope

Globally shared knowledge (requires permissions):

```python
# Shared across all tenants (admin only)
nx.memory.store(
    content="Python 3.12 released in 2024",
    scope="global",
    memory_type="fact"
)
```

---

## Step 8: Memory Types

Different types of information require different memory types:

### Facts

Objective, verifiable information:

```python
nx.memory.store(
    content="The database is PostgreSQL 15",
    scope="user",
    memory_type="fact"
)
```

Examples:
- "The staging server is at 192.168.1.100"
- "User's timezone is UTC-5"
- "The API uses JWT authentication"

### Preferences

User or agent preferences:

```python
nx.memory.store(
    content="User prefers TypeScript over JavaScript",
    scope="user",
    memory_type="preference"
)
```

Examples:
- "User likes detailed explanations"
- "Agent should use concise responses"
- "User prefers dark mode"

### Experiences

Past events and learnings:

```python
nx.memory.store(
    content="Deployment failed due to missing env var on 2025-01-10",
    scope="user",
    memory_type="experience"
)
```

Examples:
- "Bug fix deployed successfully yesterday"
- "User reported issue with login form"
- "Performance improved after database optimization"

---

## Step 9: Build a Conversational Agent with Memory

Here's a complete example of an agent that remembers conversations:

```python
#!/usr/bin/env python3
"""
Conversational Agent with Persistent Memory
"""
import asyncio
from nexus import connect

class MemoryAgent:
    def __init__(self, nx):
        self.nx = nx
        self.agent_id = "conversation-agent"

    async def remember(self, fact: str, memory_type: str = "fact"):
        """Store information in memory."""
        self.nx.memory.store(
            content=fact,
            scope="agent",
            memory_type=memory_type
        )
        print(f"   ✓ Remembered: {fact}")

    async def recall(self, query: str, limit: int = 3):
        """Search for relevant memories."""
        results = self.nx.memory.search(
            query=query,
            limit=limit
        )
        return results

    async def process_message(self, user_message: str):
        """Process a user message with memory context."""
        print(f"\n👤 User: {user_message}")

        # Search for relevant context from past conversations
        context = await self.recall(user_message, limit=2)

        if context:
            print(f"   🧠 Recalled {len(context)} relevant memories:")
            for mem in context:
                print(f"      - {mem['content'][:60]}...")

        # Here you would integrate with an LLM using the context
        # For this demo, we'll just acknowledge
        print(f"   🤖 Agent: I understand. I have {len(context)} relevant memories about this.")

        return context

async def main():
    async with connect() as nx:
        agent = MemoryAgent(nx)

        print("=== Conversational Agent with Memory ===\n")

        # Session 1: Learn about the user
        print("📅 Session 1 - Initial Conversation")

        await agent.remember("User's name is Bob", "fact")
        await agent.remember("User is learning Python", "fact")
        await agent.remember("User prefers step-by-step explanations", "preference")

        # Session 2: Agent recalls previous conversation
        print("\n📅 Session 2 - Continuation (Later)")

        await agent.process_message("What's my name again?")
        await agent.process_message("Tell me about Python")

        print("\n✨ Agent successfully maintained memory across 'sessions'!")

if __name__ == "__main__":
    asyncio.run(main())
```

**Run it:**

```bash
python conversational_agent.py
```

**Output:**
```
=== Conversational Agent with Memory ===

📅 Session 1 - Initial Conversation
   ✓ Remembered: User's name is Bob
   ✓ Remembered: User is learning Python
   ✓ Remembered: User prefers step-by-step explanations

📅 Session 2 - Continuation (Later)

👤 User: What's my name again?
   🧠 Recalled 1 relevant memories:
      - User's name is Bob
   🤖 Agent: I understand. I have 1 relevant memories about this.

👤 User: Tell me about Python
   🧠 Recalled 2 relevant memories:
      - User is learning Python
      - User prefers step-by-step explanations
   🤖 Agent: I understand. I have 2 relevant memories about this.

✨ Agent successfully maintained memory across 'sessions'!
```

---

## Step 10: Delete Memories

When information becomes outdated, you can delete memories:

### CLI:
```bash
# Get memory ID first
nexus memory query --scope user

# Delete specific memory
nexus memory delete mem_abc123...
```

### Python:
```python
# Delete by ID
nx.memory.delete(memory_id="mem_abc123...")

# Or delete all of a certain type
memories = nx.memory.query(scope="user", memory_type="experience")
for mem in memories:
    nx.memory.delete(mem['memory_id'])
```

---

## Advanced: Memory with Importance Scores

You can assign importance scores to prioritize certain memories:

```python
# High importance - critical information
nx.memory.store(
    content="Production database credentials rotated",
    scope="user",
    importance=0.95  # 0.0 to 1.0
)

# Medium importance
nx.memory.store(
    content="User mentioned liking cats",
    scope="user",
    importance=0.5
)

# Low importance
nx.memory.store(
    content="Weather was nice today",
    scope="user",
    importance=0.2
)
```

```bash
# CLI with importance
nexus memory store "Critical info" --importance 0.9
```

When querying, high-importance memories can be weighted more heavily.

---

## Troubleshooting

### Issue: Memories not persisting

**Problem:** Memories disappear after restarting

**Solution:**
```bash
# Verify server is running (not embedded mode)
echo $NEXUS_URL
# Should show: http://localhost:8080

# Verify API key is set
echo $NEXUS_API_KEY
# Should show your key

# Test connection
curl $NEXUS_URL/health
```

Embedded mode doesn't persist across sessions - use server mode!

---

### Issue: Search returns no results

**Problem:** `nexus memory search` returns empty

**Solution:**
```bash
# Check if memories exist
nexus memory query --scope user

# Verify search works
nexus memory search "your search term" --limit 10

# Try broader search
nexus memory search "any keyword"
```

Semantic search requires content similarity - try different search terms.

---

### Issue: Permission denied

**Error:** `403 Forbidden` when accessing memories

**Solution:**
```bash
# Check your permissions
nexus rebac check user your-username memory /memories --relation can_read

# Admin can grant permissions
nexus rebac grant user your-username memory /memories --relation owner
```

---

## Complete Working Example

Here's the complete script combining all concepts:

```python
#!/usr/bin/env python3
"""
Complete AI Agent Memory Demo
Prerequisites: Nexus server running with NEXUS_URL and NEXUS_API_KEY set
"""
import asyncio
from nexus import connect

async def main():
    async with connect() as nx:
        print("=== AI Agent Memory Demo ===\n")

        # 1. Store different types of memories
        print("1️⃣ Storing Memories")

        # Facts
        nx.memory.store("User's timezone is UTC-5", scope="user", memory_type="fact")
        nx.memory.store("API uses JWT authentication", scope="user", memory_type="fact")

        # Preferences
        nx.memory.store("User prefers Python", scope="user", memory_type="preference")
        nx.memory.store("User likes concise responses", scope="user", memory_type="preference")

        # Experiences
        nx.memory.store("Deployment succeeded at 2pm", scope="user", memory_type="experience")

        print("   ✓ Stored 5 memories\n")

        # 2. Query by type
        print("2️⃣ Query by Type")

        facts = nx.memory.query(scope="user", memory_type="fact")
        prefs = nx.memory.query(scope="user", memory_type="preference")

        print(f"   Facts: {len(facts)}")
        print(f"   Preferences: {len(prefs)}\n")

        # 3. Semantic search
        print("3️⃣ Semantic Search")

        results = nx.memory.search("what time is it", limit=2)
        if results:
            print(f"   Found: {results[0]['content']}")
        print()

        # 4. Search for preferences
        print("4️⃣ Search User Preferences")

        results = nx.memory.search("coding language preference", limit=1)
        if results:
            print(f"   Preference: {results[0]['content']}")
        print()

        # 5. List all memories
        print("5️⃣ List All Memories")

        all_memories = nx.memory.query(scope="user")
        print(f"   Total: {len(all_memories)} memories")
        for i, mem in enumerate(all_memories, 1):
            print(f"   {i}. {mem['content'][:50]}...")
        print()

        print("✨ Demo complete!")

if __name__ == "__main__":
    asyncio.run(main())
```

---

## Key Concepts

### Memory vs. Files

- **Files**: For documents, data, code
- **Memory**: For facts, preferences, experiences that should influence agent behavior

### Memory Scopes

| Scope | Shared With | Use Case |
|-------|-------------|----------|
| `agent` | This agent only | Private internal state |
| `user` | All user's agents | Personal preferences |
| `tenant` | Organization | Team knowledge |
| `global` | Everyone | Universal facts |

### Memory Types

- **fact**: Objective information
- **preference**: User/agent preferences
- **experience**: Past events and learnings

### When to Use Memory

✅ **Good uses:**
- User preferences: "Prefers dark mode"
- Learned facts: "User's name is Alice"
- Past experiences: "Deployment failed on Jan 15"
- Configuration: "Staging server is at x.x.x.x"

❌ **Bad uses:**
- Large documents (use files instead)
- Frequently changing data (use database)
- Temporary session state (use in-memory)

---

## What's Next?

Now that your agents have memory, explore more advanced features:

### 🔍 Recommended Next Steps

1. **[Workflow Automation](workflow-automation.md)** (15 min)
   Trigger actions based on events

2. **[Team Collaboration](team-collaboration.md)** (20 min)
   Share memories across team members

3. **[Semantic Search](../api/semantic-search.md)**
   Advanced memory retrieval with embeddings

### 📚 Related Concepts

- [Memory API](../api/memory-management.md) - Complete API reference
- [Memory CLI](../api/cli/memory.md) - All CLI commands
- [Agent Permissions](../concepts/agent-permissions.md) - Control memory access

### 🔧 Advanced Topics

- **Memory Consolidation**: Merge similar memories
- **Memory Decay**: Automatically forget old information
- **Importance Weighting**: Prioritize critical memories
- **Cross-Session Learning**: Build knowledge over time

---

## Summary

🎉 **You've completed the AI Agent Memory tutorial!**

**What you learned:**
- ✅ Store facts, preferences, and experiences
- ✅ Query memories with filters
- ✅ Search memories semantically
- ✅ Approve/deactivate memories (quality control)
- ✅ Work with active/inactive memory states
- ✅ Use different memory scopes (agent/user/tenant/global)
- ✅ Build agents with persistent memory
- ✅ Use both Python SDK and CLI

**Your agents can now:**
- Remember information across sessions
- Learn from experiences
- Personalize behavior based on preferences
- Share knowledge with other agents

---

**Next:** [Workflow Automation →](workflow-automation.md)

**Questions?** Check the [Memory API](../api/memory-management.md) or [GitHub Discussions](https://github.com/nexi-lab/nexus/discussions)
