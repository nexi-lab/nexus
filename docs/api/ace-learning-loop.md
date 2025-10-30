# ACE Learning Loop API

**Status:** v0.5.0+
**Module:** `nexus.core.ace.learning_loop`

## Overview

The ACE Learning Loop provides automatic learning capabilities for AI agents through trajectory tracking, reflection, and playbook curation. It implements the core concepts from the [Agentic Context Engineering paper](https://arxiv.org/abs/2510.04618).

**Key Features:**

- 🔄 Automatic trajectory tracking during task execution
- 🧠 LLM-powered reflection on outcomes
- 📚 Self-updating playbooks with learned strategies
- 🔁 Background re-learning from delayed feedback
- 🎯 Evidence-based strategy confidence scoring

## Architecture

```
┌──────────────────────────────────────────────────┐
│         Agent Execution Layer                    │
│  (Your Task Function)                            │
└────────────────┬─────────────────────────────────┘
                 ↓
┌──────────────────────────────────────────────────┐
│         ACE Learning Loop                        │
│                                                  │
│  1. Track → 2. Execute → 3. Reflect → 4. Curate │
└────────────────┬─────────────────────────────────┘
                 ↓
┌──────────────────────────────────────────────────┐
│         Nexus Memory System                      │
│                                                  │
│  • Trajectories  • Playbooks  • Feedback        │
└──────────────────────────────────────────────────┘
```

## API Reference

### `LearningLoop`

Main class for executing tasks with automatic learning.

#### Initialization

```python
from nexus.core.ace.learning_loop import LearningLoop

learning_loop = LearningLoop(
    session=db_session,
    backend=storage_backend,
    llm_provider=llm_provider,
    user_id="user_123",
    agent_id="agent_456",
    tenant_id="tenant_789",
    context=operation_context  # Optional
)
```

**Parameters:**

- `session` (Session): Database session for persistence
- `backend` (Any): Storage backend for content-addressable storage
- `llm_provider` (LLMProvider): LLM provider for reflection and curation
- `user_id` (str): User ID for ownership and permissions
- `agent_id` (str, optional): Agent ID for scoping
- `tenant_id` (str, optional): Tenant ID for multi-tenancy
- `context` (OperationContext, optional): Permission context

---

### `execute_with_learning()`

Execute a task with automatic learning loop.

```python
result = learning_loop.execute_with_learning(
    task_description="Deploy caching strategy",
    task_fn=deploy_cache,
    task_type="deployment",
    playbook_id="playbook_123",
    enable_reflection=True,
    enable_curation=True,
    **task_kwargs
)
```

**Parameters:**

- `task_description` (str): Human-readable description of the task
- `task_fn` (Callable): Function to execute (sync or async)
- `task_type` (str, optional): Task category (e.g., 'api_call', 'data_processing')
- `playbook_id` (str, optional): Playbook to update with learnings
- `enable_reflection` (bool): Whether to reflect on outcome (default: True)
- `enable_curation` (bool): Whether to update playbook (default: True)
- `**task_kwargs`: Arguments to pass to `task_fn`

**Returns:**

Dictionary with execution results:

```python
{
    "result": Any,              # Task function result
    "trajectory_id": str,       # Trajectory ID for tracking
    "success": bool,            # Whether task succeeded
    "error": str | None,        # Error message if failed
    "reflection_id": str | None,  # Reflection memory ID
    "duration_ms": int          # Execution duration
}
```

**Example:**

```python
def deploy_cache():
    """Deploy caching with learned strategies."""
    # Load playbook strategies
    playbook = nx.memory.get_playbook()

    # Apply helpful patterns
    for strategy in playbook['strategies']['helpful']:
        if 'cache' in strategy['pattern'].lower():
            print(f"Applying: {strategy['pattern']}")

    # Execute deployment
    cache.deploy(ttl=300)
    return {"deployed": True, "ttl": 300}

# Execute with learning
result = learning_loop.execute_with_learning(
    task_description="Deploy caching with 5min TTL",
    task_fn=deploy_cache,
    task_type="deployment",
    playbook_id="ops_playbook",
    enable_reflection=True,
    enable_curation=True
)

print(f"Success: {result['success']}")
print(f"Trajectory: {result['trajectory_id']}")
print(f"Reflection: {result['reflection_id']}")
```

---

### `execute_with_learning_async()`

Async version of `execute_with_learning()`.

```python
result = await learning_loop.execute_with_learning_async(
    task_description="Process data batch",
    task_fn=async_process_batch,
    playbook_id="data_playbook"
)
```

**Parameters:** Same as `execute_with_learning()`

**Returns:** Same as `execute_with_learning()`

---

### `process_relearning_queue()`

Process trajectories flagged for re-learning based on new feedback.

```python
results = learning_loop.process_relearning_queue(limit=10)
```

**Parameters:**

- `limit` (int): Maximum trajectories to process (default: 10)

**Returns:**

List of re-learning results:

```python
[
    {
        "trajectory_id": str,
        "success": bool,
        "reflection_id": str | None,
        "error": str | None
    },
    ...
]
```

**Example:**

```python
# Process high-priority re-learning items
results = learning_loop.process_relearning_queue(limit=5)

for result in results:
    if result['success']:
        print(f"✓ Re-learned from {result['trajectory_id']}")
    else:
        print(f"✗ Failed: {result['error']}")
```

---

### `get_playbook_strategies()`

Get relevant strategies from a playbook for a specific task.

```python
strategies = learning_loop.get_playbook_strategies(
    playbook_id="api_playbook",
    task_description="Call external API with retry logic",
    strategy_type="helpful"  # or "harmful" or "neutral"
)
```

**Parameters:**

- `playbook_id` (str): Playbook ID
- `task_description` (str): Task description for relevance matching
- `strategy_type` (Literal["helpful", "harmful", "neutral"], optional): Filter by type

**Returns:**

List of relevant strategies:

```python
[
    {
        "pattern": "Use exponential backoff for retries",
        "context": "API calls with rate limiting",
        "confidence": 0.95,
        "evidence_count": 12,
        "category": "helpful"
    },
    ...
]
```

---

### `consolidate_memories()`

Consolidate low-importance memories to prevent context collapse.

```python
results = learning_loop.consolidate_memories(
    memory_type="experience",
    importance_max=0.5,
    batch_size=10
)
```

**Parameters:**

- `memory_type` (str, optional): Filter by memory type
- `importance_max` (float): Maximum importance threshold (default: 0.5)
- `batch_size` (int): Memories per consolidation batch (default: 10)

**Returns:**

List of consolidation results:

```python
[
    {
        "consolidated_memory_id": str,
        "source_memory_ids": List[str],
        "memories_merged": int,
        "content_preview": str
    },
    ...
]
```

---

## Complete Example

Here's a complete example showing the learning loop in action:

```python
import nexus
from nexus.core.ace.learning_loop import LearningLoop

# Connect to Nexus
nx = nexus.connect()

# Access the learning loop
learning_loop = nx.ace.learning_loop  # type: LearningLoop

# Define your agent task
def process_customer_data(customer_id: str):
    """Process customer data with learned strategies."""

    # 1. Get relevant strategies from playbook
    strategies = learning_loop.get_playbook_strategies(
        playbook_id="data_processing_playbook",
        task_description=f"Process customer {customer_id}",
        strategy_type="helpful"
    )

    # 2. Apply high-confidence strategies
    for strategy in strategies:
        if strategy['confidence'] > 0.8:
            print(f"Applying: {strategy['pattern']}")

    # 3. Execute the actual task
    data = fetch_customer_data(customer_id)
    processed = validate_and_transform(data)
    store_results(processed)

    return {"customer_id": customer_id, "records": len(processed)}

# Execute with automatic learning
result = learning_loop.execute_with_learning(
    task_description="Process customer data with validation",
    task_fn=process_customer_data,
    task_type="data_processing",
    playbook_id="data_processing_playbook",
    enable_reflection=True,
    enable_curation=True,
    customer_id="cust_123"  # Passed to task_fn
)

# Check results
if result['success']:
    print(f"✓ Task completed: {result['result']}")
    print(f"✓ Trajectory tracked: {result['trajectory_id']}")
    print(f"✓ Reflection generated: {result['reflection_id']}")
    print(f"⏱ Duration: {result['duration_ms']}ms")
else:
    print(f"✗ Task failed: {result['error']}")

# Later: Process delayed feedback (e.g., from monitoring)
# This happens automatically in the background, or manually:
results = learning_loop.process_relearning_queue(limit=10)
print(f"Re-learned from {len(results)} trajectories")
```

---

## Integration with Nexus Memory API

The Learning Loop integrates seamlessly with the Nexus Memory API:

```python
# Start trajectory manually
traj_id = nx.memory.start_trajectory(
    task_description="Deploy API changes",
    task_type="deployment"
)

# Log steps during execution
nx.memory.log_step(traj_id, "decision", "Chose blue-green deployment")
nx.memory.log_step(traj_id, "action", "Deployed to staging")
nx.memory.log_step(traj_id, "observation", "All health checks passed")

# Complete trajectory
nx.memory.complete_trajectory(
    traj_id,
    status="success",
    success_score=0.95,
    metrics={"duration_ms": 5400, "rollback_count": 0}
)

# Reflect and curate
reflection = nx.memory.reflect(traj_id)
nx.memory.curate_playbook([reflection['memory_id']], playbook_name="ops_playbook")
```

---

## Best Practices

### 1. Task Description Quality

Good task descriptions improve reflection quality:

```python
# ✗ Bad: Too vague
execute_with_learning("do stuff", task_fn)

# ✓ Good: Specific and actionable
execute_with_learning(
    "Process invoice PDF with OCR and extract line items",
    task_fn
)
```

### 2. Playbook Organization

Organize playbooks by domain:

```python
# Separate playbooks for different task types
execute_with_learning("API call", api_task, playbook_id="api_playbook")
execute_with_learning("Data validation", validate_task, playbook_id="data_playbook")
execute_with_learning("ML inference", infer_task, playbook_id="ml_playbook")
```

### 3. Selective Learning

Disable learning for trivial tasks:

```python
# Enable learning for complex tasks
execute_with_learning(
    "Complex multi-step workflow",
    complex_task,
    enable_reflection=True,
    enable_curation=True
)

# Disable for simple tasks
execute_with_learning(
    "Simple file read",
    simple_task,
    enable_reflection=False,
    enable_curation=False
)
```

### 4. Background Re-learning

Schedule periodic re-learning:

```python
import schedule

def relearn_job():
    """Background job to process re-learning queue."""
    results = learning_loop.process_relearning_queue(limit=10)
    print(f"Re-learned from {len(results)} trajectories")

# Run every 5 minutes
schedule.every(5).minutes.do(relearn_job)
```

---

## Related Documentation

- [Trajectory Tracking API](trajectory-tracking.md)
- [Playbook Management API](playbook-management.md)
- [Memory Consolidation API](memory-consolidation.md)
- [Dynamic Feedback System](../design/ACE_INTEGRATION.md#critical-gap-dynamic-feedback-support)
- [ACE Examples](../examples/ace.md)

---

## See Also

- [ACE Paper](https://arxiv.org/abs/2510.04618) - Original research paper
- [Design Document](../design/ACE_INTEGRATION.md) - Nexus ACE integration design
- [Memory Management API](memory-management.md) - Core memory API
