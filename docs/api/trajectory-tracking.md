# Trajectory Tracking API

**Status:** v0.5.0+
**Module:** `nexus.core.ace.trajectory`

## Overview

The Trajectory Tracking API enables agents to record detailed execution traces for learning and analysis. Trajectories capture the sequence of decisions, actions, and observations during task execution, forming the foundation for reflection and continuous improvement.

**Key Features:**

- 📝 Step-by-step execution logging
- 🔍 Content-addressable storage for deduplication
- 🔒 ReBAC permission enforcement
- 📊 Performance metrics tracking
- 🔗 Hierarchical trajectory relationships (parent/child)
- 🎯 Integration with dynamic feedback system

## Core Concepts

### What is a Trajectory?

A **trajectory** is a complete record of an agent's execution path, including:

- **Task Description:** What the agent was trying to accomplish
- **Steps:** Sequence of actions, decisions, and observations
- **Outcome:** Success/failure status and score
- **Metrics:** Duration, tokens used, cost, etc.
- **Context:** User, agent, tenant for multi-tenant scenarios

### Trajectory Lifecycle

```
START → LOG STEPS → COMPLETE
  ↓         ↓            ↓
Create   Record      Finalize
  ID     trace       outcome
         data        + metrics
```

---

## API Reference

### `TrajectoryManager`

Main class for managing execution trajectories.

#### Initialization

```python
from nexus.core.ace.trajectory import TrajectoryManager

trajectory_mgr = TrajectoryManager(
    session=db_session,
    backend=storage_backend,
    user_id="user_123",
    agent_id="agent_456",
    tenant_id="tenant_789",
    context=operation_context  # Optional
)
```

**Parameters:**

- `session` (Session): Database session
- `backend` (Any): Storage backend for CAS content
- `user_id` (str): User ID for ownership
- `agent_id` (str, optional): Agent ID for scoping
- `tenant_id` (str, optional): Tenant ID for multi-tenancy
- `context` (OperationContext, optional): Permission context

---

### `start_trajectory()`

Start tracking a new execution trajectory.

```python
trajectory_id = trajectory_mgr.start_trajectory(
    task_description="Process customer invoice",
    task_type="data_processing",
    parent_trajectory_id=None,
    metadata={"invoice_id": "inv_123"},
    path="/project-a/invoices"
)
```

**Parameters:**

- `task_description` (str): Human-readable description
- `task_type` (str, optional): Task category
- `parent_trajectory_id` (str, optional): Parent for subtasks
- `metadata` (dict, optional): Additional context
- `path` (str, optional): Path context for trajectory

**Returns:** `str` - Unique trajectory ID

**Example:**

```python
# Start tracking API call
traj_id = trajectory_mgr.start_trajectory(
    task_description="Fetch user data from API",
    task_type="api_call",
    metadata={
        "endpoint": "/api/v1/users",
        "method": "GET"
    }
)
print(f"Started trajectory: {traj_id}")
```

---

### `log_step()`

Log a step in the trajectory execution.

```python
trajectory_mgr.log_step(
    trajectory_id=traj_id,
    step_type="action",
    description="Sent GET request to /api/v1/users",
    result={"status_code": 200, "users": 10},
    metadata={"retry_count": 0}
)
```

**Parameters:**

- `trajectory_id` (str): Trajectory ID from `start_trajectory()`
- `step_type` (str): Type of step
  - `"action"` - Something the agent did
  - `"decision"` - A choice made by the agent
  - `"observation"` - Something the agent noticed
  - `"tool_call"` - External tool invocation
- `description` (str): Human-readable description
- `result` (Any, optional): Step result data
- `metadata` (dict, optional): Additional context

**Example:**

```python
# Log decision
trajectory_mgr.log_step(
    traj_id,
    step_type="decision",
    description="Chose exponential backoff strategy",
    metadata={"max_retries": 3, "base_delay": 1.0}
)

# Log action
trajectory_mgr.log_step(
    traj_id,
    step_type="action",
    description="Retrying request with 2s delay",
    result={"attempt": 2, "delay_ms": 2000}
)

# Log observation
trajectory_mgr.log_step(
    traj_id,
    step_type="observation",
    description="Request succeeded on retry",
    result={"status_code": 200, "latency_ms": 340}
)
```

---

### `complete_trajectory()`

Mark trajectory as complete with final outcome.

```python
trajectory_id = trajectory_mgr.complete_trajectory(
    trajectory_id=traj_id,
    status="success",
    success_score=0.95,
    error_message=None,
    metrics={
        "duration_ms": 2340,
        "tokens_used": 567,
        "cost_usd": 0.03
    }
)
```

**Parameters:**

- `trajectory_id` (str): Trajectory ID
- `status` (str): Final status
  - `"success"` - Task completed successfully
  - `"failure"` - Task failed
  - `"partial"` - Partial success
- `success_score` (float, optional): Success rating (0.0-1.0)
- `error_message` (str, optional): Error description if failed
- `metrics` (dict, optional): Performance metrics

**Returns:** `str` - The trajectory ID (for chaining)

**Example:**

```python
# Success case
trajectory_mgr.complete_trajectory(
    traj_id,
    status="success",
    success_score=0.95,
    metrics={
        "duration_ms": 1234,
        "records_processed": 100,
        "errors": 0
    }
)

# Failure case
trajectory_mgr.complete_trajectory(
    traj_id,
    status="failure",
    success_score=0.0,
    error_message="API rate limit exceeded",
    metrics={
        "duration_ms": 567,
        "retry_count": 3
    }
)
```

---

### `get_trajectory()`

Retrieve a trajectory by ID with full trace content.

```python
trajectory = trajectory_mgr.get_trajectory(trajectory_id=traj_id)
```

**Parameters:**

- `trajectory_id` (str): Trajectory ID

**Returns:** `dict | None` - Trajectory data or None if not found/no permission

**Trajectory Structure:**

```python
{
    "trajectory_id": "traj_abc123",
    "user_id": "user_123",
    "agent_id": "agent_456",
    "task_description": "Process invoice",
    "task_type": "data_processing",
    "status": "success",
    "success_score": 0.95,
    "error_message": None,
    "duration_ms": 2340,
    "tokens_used": 567,
    "cost_usd": 0.03,
    "started_at": "2025-10-29T10:00:00Z",
    "completed_at": "2025-10-29T10:00:02Z",
    "trace": {
        "steps": [
            {
                "timestamp": "2025-10-29T10:00:00Z",
                "step_type": "action",
                "description": "Parsed invoice PDF",
                "result": {"pages": 3},
                "metadata": {}
            },
            ...
        ],
        "decisions": [...],
        "observations": [...],
        "metadata": {...}
    }
}
```

**Example:**

```python
trajectory = trajectory_mgr.get_trajectory(traj_id)

if trajectory:
    print(f"Task: {trajectory['task_description']}")
    print(f"Status: {trajectory['status']}")
    print(f"Steps: {len(trajectory['trace']['steps'])}")
    print(f"Duration: {trajectory['duration_ms']}ms")
```

---

### `query_trajectories()`

Query trajectories by filters with permission checks.

```python
trajectories = trajectory_mgr.query_trajectories(
    agent_id="agent_456",
    task_type="api_call",
    status="success",
    path="/project-a/",
    limit=50
)
```

**Parameters:**

- `agent_id` (str, optional): Filter by agent ID
- `task_type` (str, optional): Filter by task type
- `status` (str, optional): Filter by status
- `path` (str, optional): Filter by path context
- `limit` (int): Maximum results (default: 50)

**Returns:** `List[dict]` - List of trajectory summaries (without full trace)

**Example:**

```python
# Get recent successful API calls
successful_calls = trajectory_mgr.query_trajectories(
    agent_id="agent_456",
    task_type="api_call",
    status="success",
    limit=10
)

for traj in successful_calls:
    print(f"{traj['task_description']}: {traj['success_score']}")
```

---

## Usage Examples

### Basic Trajectory Tracking

```python
import nexus

nx = nexus.connect()

# Start trajectory
traj_id = nx.memory.start_trajectory(
    task_description="Deploy microservice update",
    task_type="deployment"
)

try:
    # Log deployment steps
    nx.memory.log_step(
        traj_id,
        step_type="decision",
        description="Chose blue-green deployment strategy"
    )

    nx.memory.log_step(
        traj_id,
        step_type="action",
        description="Deployed to staging environment",
        result={"environment": "staging", "version": "v2.3.0"}
    )

    nx.memory.log_step(
        traj_id,
        step_type="observation",
        description="Health checks passed",
        result={"healthy_instances": 5, "total_instances": 5}
    )

    nx.memory.log_step(
        traj_id,
        step_type="action",
        description="Promoted to production",
        result={"environment": "production"}
    )

    # Success!
    nx.memory.complete_trajectory(
        traj_id,
        status="success",
        success_score=1.0,
        metrics={"duration_ms": 45000, "downtime_ms": 0}
    )

except Exception as e:
    # Log failure
    nx.memory.log_step(
        traj_id,
        step_type="observation",
        description=f"Deployment failed: {str(e)}",
        result={"error": str(e)}
    )

    nx.memory.complete_trajectory(
        traj_id,
        status="failure",
        success_score=0.0,
        error_message=str(e)
    )
```

---

### Hierarchical Trajectories

Track parent-child relationships for complex workflows:

```python
# Parent trajectory
parent_id = nx.memory.start_trajectory(
    task_description="Process batch of 100 invoices",
    task_type="batch_processing"
)

# Child trajectories
for invoice in invoices[:100]:
    child_id = nx.memory.start_trajectory(
        task_description=f"Process invoice {invoice.id}",
        task_type="data_processing",
        parent_trajectory_id=parent_id  # Link to parent
    )

    # Process invoice...
    process_invoice(invoice)

    nx.memory.complete_trajectory(
        child_id,
        status="success",
        success_score=0.9
    )

# Complete parent
nx.memory.complete_trajectory(
    parent_id,
    status="success",
    success_score=0.95,
    metrics={"invoices_processed": 100}
)
```

---

### Integration with Feedback System

Trajectories can receive delayed feedback:

```python
# Initial trajectory
traj_id = nx.memory.start_trajectory(
    task_description="Deploy caching strategy"
)

# ... execute and complete ...
nx.memory.complete_trajectory(
    traj_id,
    status="success",
    success_score=0.95  # Initially looks good
)

# 2 hours later: Monitoring detects issues
nx.memory.add_feedback(
    traj_id,
    feedback_type="monitoring_alert",
    score=0.3,  # Revised down!
    source="datadog_monitor",
    message="15% stale data rate detected",
    metrics={"stale_rate": 0.15, "user_complaints": 47}
)

# Auto-flagged for re-learning
nx.memory.mark_for_relearning(
    traj_id,
    reason="production_failure",
    priority=9
)
```

---

## Permission Model

Trajectories respect ReBAC permissions:

### Access Control

```python
# User can access:
# 1. Trajectories they created (same user_id)
# 2. Trajectories from their agents (same agent_id)
# 3. Tenant-scoped trajectories (same tenant_id)
# 4. Admin/system users bypass checks

# Example: Query only returns accessible trajectories
trajectories = trajectory_mgr.query_trajectories(
    task_type="api_call"
)  # Automatically filtered by permissions
```

### Sharing Trajectories

```python
# Share trajectory with another user via ReBAC
nx.rebac.create(
    subject=("user", "alice"),
    relation="viewer",
    object=("trajectory", traj_id)
)
```

---

## Best Practices

### 1. Meaningful Descriptions

Write clear, specific task descriptions:

```python
# ✗ Bad: Too vague
start_trajectory("process data")

# ✓ Good: Specific and actionable
start_trajectory(
    "Process customer invoice PDF with OCR and line item extraction"
)
```

### 2. Structured Logging

Use consistent step types:

```python
# Decision: Why did you choose this approach?
log_step(traj_id, "decision", "Chose REST over GraphQL due to simplicity")

# Action: What did you do?
log_step(traj_id, "action", "Sent POST request to /api/v1/users")

# Observation: What did you notice?
log_step(traj_id, "observation", "Response time exceeded SLA (2.3s)")
```

### 3. Rich Metadata

Include relevant context:

```python
trajectory_mgr.log_step(
    traj_id,
    step_type="action",
    description="Validated input data",
    result={
        "total_records": 1000,
        "valid_records": 987,
        "invalid_records": 13
    },
    metadata={
        "validation_rules": ["email", "phone", "address"],
        "validation_duration_ms": 234
    }
)
```

### 4. Error Handling

Always complete trajectories, even on failure:

```python
try:
    # Task execution
    result = execute_task()
    trajectory_mgr.complete_trajectory(
        traj_id,
        status="success",
        success_score=1.0
    )
except Exception as e:
    # Still complete, but as failure
    trajectory_mgr.complete_trajectory(
        traj_id,
        status="failure",
        success_score=0.0,
        error_message=str(e)
    )
    raise  # Re-raise if needed
```

---

## CLI Commands

### Start Trajectory

```bash
nexus memory trajectory start "Deploy API changes" --type deployment --json
```

### Log Step

```bash
nexus memory trajectory log traj_abc123 "Deployed to staging" --type action
```

### Complete Trajectory

```bash
nexus memory trajectory complete traj_abc123 --status success --score 0.95
```

### List Trajectories

```bash
nexus memory trajectory list --agent-id agent_456 --status success --limit 10 --json
```

---

## Performance Considerations

### Content-Addressable Storage

Trajectories use CAS for efficient storage:

- ✅ Duplicate steps are deduplicated automatically
- ✅ Common patterns share storage
- ✅ Efficient for large-scale trajectory logging

### Sampling Strategy

For high-volume scenarios, consider sampling:

```python
import random

# Only track 10% of successful trajectories
if status == "success" and random.random() < 0.10:
    trajectory_mgr.start_trajectory(...)
elif status == "failure":
    # Always track failures
    trajectory_mgr.start_trajectory(...)
```

### Retention Policy

Configure automatic cleanup:

```python
# Configuration (example)
TRAJECTORY_RETENTION_DAYS = 30
TRAJECTORY_ALWAYS_KEEP_FAILURES = True
TRAJECTORY_SAMPLE_RATE = 0.1  # 10% of successes
```

---

## Related Documentation

- [ACE Learning Loop API](ace-learning-loop.md)
- [Dynamic Feedback System](../design/ACE_INTEGRATION.md#critical-gap-dynamic-feedback-support)
- [Playbook Management API](playbook-management.md)
- [Memory Management API](memory-management.md)

---

## See Also

- [Design Document](../design/ACE_INTEGRATION.md) - Full ACE integration design
- [ACE Examples](../examples/ace.md) - Working examples and demos
