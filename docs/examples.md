# Examples

See Nexus in action with these real-world use cases.

## AI Agent Memory

Store and query agent context with semantic search. Agents learn from past interactions and improve over time.

```python
import nexus
import json

# Connect to Nexus (embedded mode - no auth needed)
nx = nexus.connect(config={"data_dir": "./nexus-data"})

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

# Local development (embedded mode - no auth needed)
local_nx = nexus.connect(config={"data_dir": "./local-dev"})

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

nx = nexus.connect(config={"data_dir": "./nexus-data"})

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

nx = nexus.connect(config={"data_dir": "./nexus-data"})

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

## Autonomous Agent Task Workflow

Build autonomous task management agents using only Nexus memory primitives. No dedicated task system required.

This example demonstrates:
- Flexible data storage with memory system
- Agent identity and permissions
- Task discovery and dynamic workflow generation
- Multi-agent coordination capabilities

```python
import nexus
import json
import time
import random
from datetime import datetime

# Connect with agent identity
nx = nexus.connect(config={
    "data_dir": "./nexus-task-demo",
    "agent_id": "agent_demo",
})

# Store task as structured memory
task_data = {
    "task_id": "task_001",
    "title": "Implement authentication",
    "status": "pending",      # pending | in_progress | completed
    "priority": 1,            # 1=highest
    "blocked_by": [],         # List of task_ids that block this
    "discovered_from": None,  # Parent task_id for discovered tasks
    "agent_id": None,
    "created_at": datetime.now().isoformat(),
    "completed_at": None,
}

# Store task in memory
memory_id = nx.memory.store(
    json.dumps(task_data),
    scope="agent",
    memory_type="task"
)

# Query ready tasks (no blockers)
def find_ready_work(nx):
    memories = nx.memory.query(scope="agent", memory_type="task")
    tasks = [json.loads(m['content']) for m in memories]

    # Filter to pending tasks with no blockers
    ready = [
        t for t in tasks
        if t['status'] == 'pending' and not t['blocked_by']
    ]

    # Sort by priority
    ready.sort(key=lambda t: t['priority'])
    return ready

# Autonomous agent loop
ready_tasks = find_ready_work(nx)
for task in ready_tasks:
    print(f"Working on: {task['title']}")
    time.sleep(1)  # Simulate work

    # Discover new tasks (50% chance)
    if random.random() < 0.5:
        new_task = {
            "task_id": f"task_{random.randint(1000, 9999)}",
            "title": f"Test: {task['title']}",
            "status": "pending",
            "priority": 2,
            "discovered_from": task['task_id'],
            "created_at": datetime.now().isoformat(),
        }
        nx.memory.store(json.dumps(new_task), scope="agent", memory_type="task")
        print(f"  → Discovered: {new_task['task_id']}")
```

**Full working demo:** See [`examples/task_workflow/`](../examples/task_workflow/) for a complete autonomous agent implementation.

Features demonstrated:
- **Zero dependencies** - Pure memory operations, no external services
- **Agent identity** - Automatic permission handling
- **Multi-agent coordination** - Multiple agents work on shared task pool
- **Task discovery** - Dynamic workflow generation
- **Embedded mode** - Runs locally with zero deployment

## DeepAgents Integration

Build autonomous research agents with event-driven workflows that automatically process outputs and consolidate knowledge - zero prompt engineering required.

This example demonstrates:
- DeepAgents filesystem backend using Nexus
- Event-driven workflows triggered on file writes
- Automatic memory consolidation from agent outputs
- Multi-agent coordination via shared context

```python
import nexus
from deepagents import create_deep_agent
from nexus_backend import NexusBackend
from nexus.workflows import WorkflowAPI

# Connect to Nexus with workflows enabled
nx = nexus.connect(config={"enable_workflows": True})

# Create research agent with Nexus backend
agent = create_deep_agent(
    model="anthropic:claude-sonnet-4",
    backend=NexusBackend(nx, base_path="/workspace"),
    tools=[internet_search]
)

# Define workflow to auto-process agent outputs
workflow = {
    "name": "agent-processor",
    "triggers": [{"type": "file_write", "pattern": "/workspace/*.md"}],
    "actions": [{
        "type": "python",
        "code": """
nx = nexus.connect()
content = nx.read(context.file_path).decode()

# Extract insights and store in memory
insight = f"Agent researched: {context.file_path}"
nx.memory.store(insight, scope="user", memory_type="research")
nx.memory.session.commit()
        """
    }]
}

# Register workflow (runs automatically on file writes)
WorkflowAPI().load(workflow, enabled=True)

# Agent writes files → workflow triggers → memory updates
agent.invoke({
    "messages": [{"role": "user", "content": "Research transformers"}]
})

# Query consolidated memories
memories = nx.memory.search("transformers", scope="user")
```

**Three-tier architecture:**
- **Tier 1**: Files stored in Nexus with versioning
- **Tier 2**: Workflows process outputs automatically
- **Tier 3**: Memories consolidated for semantic search

**Full working demo:** See [`examples/deepagents/`](examples/deepagents.md) for complete implementation with workflow automation, multi-agent coordination, and production patterns.

Key features:
- **Zero prompt engineering** - Workflows handle memory storage automatically
- **Event-driven processing** - Triggers on filesystem events (no polling)
- **Reliable execution** - Workflows run independent of LLM behavior
- **Production ready** - Versioning, permissions, multi-tenancy built-in

## Agentic Context Engineering (ACE)

Enable AI agents to learn from experience and continuously improve performance through automated reflection and strategy curation.

This example demonstrates:
- Trajectory tracking for agent actions
- Automated reflection on successes and failures
- Dynamic playbook updates with learned strategies
- Measurable performance improvement over time

```python
import nexus

# Connect to Nexus
nx = nexus.connect(config={"data_dir": "./nexus-data"})

# Start tracking an agent task
traj_id = nx.memory.start_trajectory(
    task_description="Validate customer data records",
    playbook="data_validator"
)

# Load learned strategies from previous runs
playbook = nx.memory.get_playbook("data_validator")
strategies = playbook.get("strategies", [])

# Execute task using learned strategies
accuracy = validate_data(records, strategies=strategies)

# Complete trajectory with performance metric
nx.memory.complete_trajectory(
    traj_id,
    outcome="success",
    success_score=accuracy
)

# Agent automatically reflects on what worked
reflection = nx.memory.reflect(traj_id)

# Curate playbook with high-performing strategies
nx.memory.curate_playbook(
    reflection_ids=[reflection.id],
    playbook_name="data_validator"
)

# Next run will use improved strategies
# Repeat over epochs → continuous improvement
```

**Real results from the ACE demo:**
- **Epoch 0**: 58% accuracy (no learned rules)
- **Epoch 10**: 95% accuracy (+37% improvement)
- Agent automatically discovered 15+ validation rules
- Zero manual rule engineering required

**Full working demo:** See [`examples/ace/`](../examples/ace/) for complete implementation with Titanic dataset, learning curves, and mermaid diagrams showing the auto-improvement loop.

Key features:
- **Automated learning** - Agent improves without human intervention
- **Trajectory tracking** - Full observability of agent behavior
- **Reflection engine** - Extracts insights from successes and failures
- **Playbook curation** - Stores and evolves proven strategies
- **Measurable ROI** - Quantified performance improvements

## LangGraph + Nexus Integration

Build powerful ReAct (Reasoning + Acting) agents with LangGraph that interact with Nexus filesystem. Perfect for code analysis, documentation generation, and autonomous file operations.

This example demonstrates:
- ReAct agent architecture for multi-step reasoning
- Familiar file operation tools (grep, glob, cat/less, write)
- Remote Nexus filesystem for persistent storage
- Multi-LLM support (Claude, GPT-4, OpenRouter)
- Multi-tenancy for team collaboration

```python
import nexus
from langgraph_react_demo import create_react_agent

# Connect to remote Nexus server
nx = nexus.connect(
    remote_url="http://nexus-server:8080",
    config={"tenant_id": "team-dev", "agent_id": "code-analyzer"}
)

# Create ReAct agent with file operation tools
agent = create_react_agent(nx)

# Agent autonomously searches, reads, and writes files
result = agent.invoke({
    "messages": [{
        "role": "user",
        "content": "Find all async/await patterns, analyze them, and create a summary report"
    }]
})

# Agent automatically:
# 1. grep_files("async def /workspace") - Search for patterns
# 2. read_file("cat /workspace/api.py") - Read relevant files
# 3. write_file("/reports/analysis.md", "...") - Save analysis
```

**Real-world use cases:**
- **Code Analysis** - Find patterns, security issues, performance bottlenecks
- **Documentation** - Auto-generate API docs, onboarding guides
- **Code Review** - Analyze PRs, suggest improvements
- **Migration** - Find and update deprecated patterns

**Full working demo:** See [`examples/langgraph/`](../examples/langgraph/) for complete implementation with sample tasks, multi-LLM support, and ReAct loop visualization.

Key features:
- **ReAct Architecture** - Agents reason, act, and observe in a loop
- **File Operations** - grep, glob, read, write with Nexus
- **Remote Execution** - Work with shared Nexus servers
- **Multi-Tenancy** - Isolated workspaces for teams
- **Framework Integration** - Shows Nexus as infrastructure for LangGraph

## OpenAI Agents SDK + Nexus Integration

Build production-ready ReAct agents using OpenAI Agents SDK with Nexus filesystem. Same capabilities as LangGraph but with 70% less code and automatic ReAct loop handling.

This example demonstrates:
- Built-in ReAct pattern (no manual state management)
- Function tool decorators for Nexus operations
- Persistent memory across agent sessions
- Agent handoffs for multi-agent collaboration
- Guardrails for input/output validation

```python
import nexus
from agents import Agent, function_tool

# Connect to Nexus
nx = nexus.connect(
    remote_url="http://nexus-server:8080",
    config={"tenant_id": "team-dev", "agent_id": "file-agent"}
)

# Define tools with @function_tool decorator
@function_tool
async def grep_files(pattern: str, path: str = "/") -> str:
    """Search file content using grep patterns."""
    results = nx.grep(pattern, path)
    return format_results(results)

@function_tool
async def write_file(path: str, content: str) -> str:
    """Write content to Nexus filesystem."""
    nx.write(path, content.encode('utf-8'))
    return f"Successfully wrote to {path}"

# Create agent (ReAct loop is automatic!)
agent = Agent(
    name="FileAgent",
    instructions="You are a file analysis assistant with Nexus filesystem access.",
    tools=[grep_files, read_file, write_file],
    model="gpt-4o"
)

# Run agent - automatic Think→Act→Observe loop
result = agent.run(
    "Find all async/await patterns and create a summary report"
)
```

**With persistent memory:**

```python
@function_tool
async def store_memory(content: str, memory_type: str = "fact") -> str:
    """Store information in persistent memory."""
    nx.memory.store(content, scope="agent", memory_type=memory_type)
    return f"Stored {memory_type}: {content}"

@function_tool
async def recall_memory(query: str) -> str:
    """Query stored memories using semantic search."""
    results = nx.memory.query(query, scope="agent")
    return format_memories(results)

# Agent remembers across sessions!
agent = Agent(
    name="MemoryAgent",
    instructions="Store important information and recall when relevant.",
    tools=[store_memory, recall_memory]
)
```

**Real-world comparison:**

| Metric | LangGraph + Nexus | OpenAI SDK + Nexus |
|--------|-------------------|-------------------|
| Lines of code | ~370 | ~300 (-19%) |
| ReAct loop setup | Manual (30 lines) | Automatic (0 lines) |
| Tool definition | `@tool` decorator | `@function_tool` decorator |
| Execution | `agent.invoke()` | `agent.run()` |
| State management | Manual TypedDict | Automatic |
| Best for | Complex workflows | Production agents |

**Full working demos:** See [`examples/openai_agents/`](../examples/openai_agents/) for:
- File operations demo (port of LangGraph ReAct demo)
- Memory agent with persistent context
- Side-by-side framework comparison

Key features:
- **Automatic ReAct** - Built-in Think→Act→Observe loop
- **70% Less Code** - No manual state management needed
- **Persistent Memory** - Nexus Memory API integration
- **Agent Handoffs** - Native multi-agent collaboration
- **Production Ready** - Guardrails, validation, error handling

---

## Next Steps

- [Read the Full Documentation](api/index.md)
- [View Quick Start Guide](index.md#quick-start-in-30-seconds)
- [Explore GitHub Repository](https://github.com/nexi-lab/nexus)
