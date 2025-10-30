# Autonomous Agent Task Workflow

This demo showcases how to build an autonomous task management agent using **only** Nexus memory system primitives. The agent manages tasks through pure CRUD operations without requiring a dedicated task management system.

**Key principle:** Pure memory operations - no external dependencies, no LLM calls, just Nexus primitives.

## What This Demo Shows

1. **Flexible data storage** - Task tracking without a dedicated task system
2. **Simple API** - Just `store()`, `query()`, `get()`, and `delete()` operations
3. **Agent identity** - Proper permission handling with agent IDs
4. **Task discovery** - Dynamic workflow generation through task relationships
5. **Multi-agent ready** - Multiple agents can coordinate on shared task pools
6. **Zero dependencies** - No external services, databases, or frameworks needed

## How It Works

The agent follows an autonomous workflow loop:

```
1. Find ready work  → Query for tasks with no blocking dependencies
2. Claim task       → Mark task as in_progress with agent identity
3. Execute work     → Simulate work (sleep 1 second)
4. Discover issues  → Randomly find related work (50% chance)
5. Link discoveries → Store discovered-from relationship
6. Complete task    → Mark as completed and repeat
```

### Task Storage

Tasks are stored as JSON-serialized structured data in the Nexus memory system:

```python
{
    "task_id": "task_001",
    "title": "Implement authentication",
    "status": "pending",      # pending | in_progress | completed
    "priority": 1,            # 1=highest
    "blocked_by": [],         # List of task_ids that block this
    "discovered_from": None,  # Parent task_id for discovered tasks
    "agent_id": None,
    "created_at": "2025-10-28T10:30:00",
    "completed_at": None,
}
```

Stored with:
```python
nx.memory.store(json.dumps(task_data), scope="agent", memory_type="task")
```

Retrieved with:
```python
memories = nx.memory.query(scope="agent", memory_type="task")
tasks = [json.loads(m['content']) for m in memories]
```

## Running the Demo

### Setup

```bash
cd examples/task_workflow

# Create initial tasks
python setup_demo_tasks.py
```

### Run the Agent

```bash
python agent.py
```

**Expected output:**

```
🚀 Nexus Autonomous Task Agent
Agent ID: agent_demo

=== Iteration 1 ===
Found: Implement authentication
  Working on: Implement authentication
  → Discovered: task_5273
✓ Completed!
Stats: 4 pending | 0 in progress | 1 completed

=== Iteration 2 ===
Found: Design database schema
  Working on: Design database schema
✓ Completed!
Stats: 3 pending | 0 in progress | 2 completed

...

==================================================
Agent workflow completed!

Final Task Summary:
--------------------------------------------------

✅ Completed (9):
  - Implement authentication
  - Design database schema
  - Setup CI/CD pipeline
  - Test: Implement authentication (discovered from task_001)
  - Test: Design database schema (discovered from task_002)
  - ...
```

**Zero external dependencies. Zero setup. Just run.**

## Implementation Details

### Finding Ready Work

```python
def find_ready_work(nx, limit=1):
    """Find tasks with no blockers."""
    all_memories = nx.memory.query(scope="agent", memory_type="task")
    all_tasks = [parse_task(m) for m in all_memories]

    # Filter to pending tasks with no blockers
    ready = [
        t for t in all_tasks
        if t['data']['status'] == 'pending'
        and not t['data']['blocked_by']
    ]

    # Sort by priority (1=highest)
    ready.sort(key=lambda t: t['data']['priority'])
    return ready[:limit]
```

### Updating Tasks

Since memories are immutable (content-addressed storage), updates are done via delete + recreate:

```python
def update_task(nx, memory_id, task_data):
    """Update a task by deleting and recreating."""
    # Delete old version
    nx.memory.delete(memory_id)

    # Create new version with updated data
    new_memory_id = nx.memory.store(
        json.dumps(task_data),
        scope="agent",
        memory_type="task"
    )

    return new_memory_id
```

### Task Discovery

```python
def execute_task(nx, task):
    """Execute task and maybe discover new work."""
    print(f"  Working on: {task['data']['title']}")
    time.sleep(1)  # Simulate work

    discovered = []
    if random.random() < 0.5:  # 50% chance of discovery
        new_id = create_discovered_task(nx, task)
        discovered.append(new_id)
        print(f"  → Discovered: {new_id}")

    return discovered
```

## Nexus Features Demonstrated

### 1. Memory System Flexibility

Store any structured data without schema definitions:

```python
nx.memory.store(json.dumps(data), scope="agent", memory_type="task")
```

No migrations, no schema changes - just store your data.

### 2. Agent Identity & Permissions

Each agent connects with its own identity:

```python
nx = nexus.connect(config={
    "data_dir": "./nexus-task-demo",
    "agent_id": "agent_demo",  # Agent identity for permissions
})
```

Nexus automatically:
- Tracks which agent created each memory
- Enforces read/write permissions based on scope and visibility
- Enables multi-agent coordination

### 3. Scoped Memory

Organize memories by scope:

```python
# Agent-specific memories
nx.memory.store(data, scope="agent", memory_type="task")

# User-level memories
nx.memory.store(data, scope="user", memory_type="preference")

# Tenant-wide memories
nx.memory.store(data, scope="tenant", memory_type="policy")
```

### 4. Content-Addressed Storage (CAS)

Memories are stored using content-addressed storage:
- Automatic deduplication
- Immutable content (updates = new versions)
- Efficient storage and retrieval

### 5. Embedded Mode

No server required - runs locally like SQLite:

```python
nx = nexus.connect(config={"data_dir": "./nexus-task-demo"})
```

Perfect for:
- Development and testing
- Single-user applications
- Edge deployments

## Advanced Features

### Multi-Agent Coordination

Multiple agents can work on the same task pool:

```python
# Terminal 1
agent_a = TaskAgent(agent_id="agent_a")
agent_a.run()

# Terminal 2
agent_b = TaskAgent(agent_id="agent_b")
agent_b.run()
```

Each agent:
- Sees the same task pool
- Claims tasks independently
- Cannot interfere with tasks claimed by other agents

### Task Dependencies

Create tasks that block others:

```python
nx.memory.store(json.dumps({
    "task_id": "task_005",
    "title": "Deploy to production",
    "blocked_by": ["task_001", "task_002"],  # Must wait for these
    "status": "pending",
    ...
}), scope="agent", memory_type="task")
```

The `find_ready_work()` function automatically filters out blocked tasks.

### Workspace Isolation

Isolate tasks by workspace:

```python
nx = nexus.connect(config={
    "data_dir": "./nexus-task-demo",
    "workspace": "/project-alpha"  # Isolated workspace
})
```

Different workspaces maintain separate task pools.

### Remote Server Mode

Scale to multiple machines with Nexus server:

```python
nx = nexus.connect(
    remote_url="http://localhost:8080",
    api_key="your-key"
)
```

Multiple agents across different machines coordinate through the server.

### ReBAC Permissions

Fine-grained access control:

```bash
# Grant agent permission to specific workspaces
nexus rebac create agent agent_a direct_worker workspace /project-alpha
```

Control:
- Which agents can access which tasks
- Task visibility across teams
- Hierarchical permissions

## Architecture

```
┌─────────────────────────────────────────┐
│         Task Agent (agent.py)           │
│                                         │
│  1. Find ready work                     │
│  2. Claim task                          │
│  3. Execute + Discover                  │
│  4. Complete                            │
│  5. Repeat                              │
└─────────────────┬───────────────────────┘
                  │
                  │ store() / query() / delete()
                  │
┌─────────────────▼───────────────────────┐
│     Nexus Memory System                 │
│                                         │
│  - SQLite database (metadata)           │
│  - Content-addressed storage (CAS)      │
│  - Agent identity & permissions         │
│  - Scopes: agent, user, tenant          │
│  - Memory types: task, fact, etc.       │
└─────────────────────────────────────────┘
```

### Data Flow

1. **Initialization**: `setup_demo_tasks.py` creates initial tasks
2. **Agent loop**: Agent queries memory for ready tasks
3. **Claim**: Task status updated to `in_progress` with agent ID
4. **Execute**: Simulated work, possible discovery of new tasks
5. **Complete**: Task status updated to `completed`
6. **Repeat**: Loop continues until no ready work remains

## Extending the Demo

### Add Task Priority Queue

Implement more sophisticated priority:

```python
def find_ready_work(nx, limit=1):
    all_memories = nx.memory.query(scope="agent", memory_type="task")
    all_tasks = [parse_task(m) for m in all_memories]
    ready = [t for t in all_tasks if t['data']['status'] == 'pending'
             and not t['data']['blocked_by']]

    # Sort by priority, then by creation time
    ready.sort(key=lambda t: (
        t['data']['priority'],
        t['data']['created_at']
    ))
    return ready[:limit]
```

### Add Task Failure Handling

Track failed tasks and retry logic:

```python
task_data['status'] = 'failed'
task_data['failure_count'] = task_data.get('failure_count', 0) + 1
task_data['last_error'] = str(error)
update_task(nx, memory_id, task_data)
```

### Add Execution Metrics

Store execution traces for analysis:

```python
nx.memory.store(json.dumps({
    "task_id": task_id,
    "execution_time": elapsed_time,
    "success": True,
    "agent_id": agent_id,
    "context": {...},
}), scope="agent", memory_type="execution_trace")
```

### Integration with Nexus LLM

Use Nexus LLM context for intelligent task handling:

```python
from nexus.llm import build_context

# Build context from related tasks
context = build_context(nx, query=f"Tasks related to {task_title}")

# Use context for LLM-powered task execution
result = llm.complete(
    prompt=f"Execute task: {task_title}",
    context=context
)
```

## Files

- **`agent.py`** (~300 lines) - Main agent implementation
- **`setup_demo_tasks.py`** (~65 lines) - Initialize demo tasks
- **`README.md`** - This file

## Key Takeaways

✅ **No dedicated infrastructure** - Just Nexus memory primitives
✅ **Simple API** - `store()`, `query()`, `get()`, `delete()`
✅ **Agent coordination** - Built-in identity and permissions
✅ **Extensible** - Foundation for complex workflows
✅ **Production-ready** - Scale from embedded to distributed

## Next Steps

1. **Try it yourself**: Run the demo and modify the code
2. **Add features**: Implement task dependencies, failure handling
3. **Multi-agent**: Launch multiple agents on the same task pool
4. **Connect to server**: Try remote mode with Nexus server
5. **Add permissions**: Experiment with ReBAC for task access control
6. **Build something new**: Use as foundation for your own agent workflow

## Learn More

- [Nexus Memory System Documentation](../../docs/api/memory.md)
- [Agent Identity & Permissions](../../docs/permissions.md)
- [ReBAC Access Control](../../docs/rebac.md)
- [Nexus Server Mode](../../docs/server.md)

---

**The goal**: Show what you can build with Nexus primitives in ~300 lines!
