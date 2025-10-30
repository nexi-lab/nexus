# Playbook Management API

**Status:** v0.5.0+
**Module:** `nexus.core.ace.playbook`

## Overview

The Playbook Management API provides storage and retrieval of learned strategies for AI agents. Playbooks organize knowledge into helpful, harmful, and neutral patterns, with evidence-based confidence scoring and automatic curation from trajectory reflections.

**Key Features:**

- 📚 Structured strategy storage (helpful ✓, harmful ✗, neutral ○)
- 🎯 Evidence-based confidence scoring
- 🔄 Automatic curation from reflections
- 🔍 Semantic search for relevant strategies
- 📊 Usage tracking and effectiveness metrics
- 🔒 Multi-tenant with ReBAC permissions
- 🗂️ Version control for playbooks

## Core Concepts

### What is a Playbook?

A **playbook** is a collection of learned strategies organized by category:

- **Helpful (✓):** Proven patterns that lead to success
- **Harmful (✗):** Anti-patterns to avoid
- **Neutral (○):** Context-dependent observations

### Playbook Structure

```json
{
  "playbook_id": "playbook_123",
  "name": "api_client_playbook",
  "version": 5,
  "strategies": [
    {
      "category": "helpful",
      "pattern": "Use exponential backoff for API retries",
      "context": "Rate-limited API calls",
      "confidence": 0.95,
      "evidence_count": 12,
      "examples": ["traj_abc", "traj_def"]
    },
    {
      "category": "harmful",
      "pattern": "Retry immediately without delay",
      "context": "API error handling",
      "confidence": 0.88,
      "evidence_count": 7,
      "examples": ["traj_xyz"]
    }
  ],
  "usage_count": 47,
  "success_rate": 0.89
}
```

---

## API Reference

### `PlaybookManager`

Main class for managing playbooks.

#### Initialization

```python
from nexus.core.ace.playbook import PlaybookManager

playbook_mgr = PlaybookManager(
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

### `create_playbook()`

Create a new playbook.

```python
playbook_id = playbook_mgr.create_playbook(
    name="api_client_playbook",
    description="Strategies for external API calls",
    scope="agent",
    visibility="private"
)
```

**Parameters:**

- `name` (str): Playbook name (unique per agent/user)
- `description` (str, optional): Human-readable description
- `scope` (str): Playbook scope
  - `"agent"` - Agent-specific (default)
  - `"user"` - User-specific
  - `"tenant"` - Tenant-wide
  - `"global"` - Global (admin only)
- `visibility` (str): Access control
  - `"private"` - Owner only (default)
  - `"shared"` - Accessible via ReBAC

**Returns:** `str` - Playbook ID

**Example:**

```python
# Create agent-specific playbook
playbook_id = playbook_mgr.create_playbook(
    name="data_processing_playbook",
    description="Learned patterns for data validation",
    scope="agent",
    visibility="private"
)

# Create tenant-wide playbook
team_playbook_id = playbook_mgr.create_playbook(
    name="team_best_practices",
    description="Shared team strategies",
    scope="tenant",
    visibility="shared"
)
```

---

### `get_playbook()`

Retrieve a playbook by name or ID.

```python
playbook = playbook_mgr.get_playbook(
    name="api_client_playbook",
    playbook_id=None
)
```

**Parameters:**

- `name` (str, optional): Playbook name
- `playbook_id` (str, optional): Playbook ID
- One of `name` or `playbook_id` must be provided

**Returns:** `dict | None` - Playbook data or None if not found

**Playbook Structure:**

```python
{
    "playbook_id": str,
    "name": str,
    "description": str,
    "version": int,
    "scope": str,
    "visibility": str,
    "strategies": {
        "helpful": [
            {
                "pattern": str,
                "context": str,
                "confidence": float,
                "evidence_count": int,
                "created_at": str,
                "updated_at": str,
                "examples": List[str]
            },
            ...
        ],
        "harmful": [...],
        "neutral": [...]
    },
    "usage_count": int,
    "success_rate": float,
    "created_at": str,
    "updated_at": str
}
```

**Example:**

```python
playbook = playbook_mgr.get_playbook(name="api_client_playbook")

if playbook:
    print(f"Playbook: {playbook['name']} (v{playbook['version']})")
    print(f"Helpful strategies: {len(playbook['strategies']['helpful'])}")
    print(f"Success rate: {playbook['success_rate']:.1%}")
```

---

### `update_playbook()`

Update playbook with new strategies.

```python
playbook_mgr.update_playbook(
    playbook_id="playbook_123",
    strategies=[
        {
            "category": "helpful",
            "pattern": "Cache API responses for 5 minutes",
            "context": "Frequently accessed data",
            "confidence": 0.9,
            "evidence_count": 5
        }
    ],
    merge=True,  # Merge with existing strategies
    increment_version=True
)
```

**Parameters:**

- `playbook_id` (str): Playbook ID
- `strategies` (List[dict]): New strategies to add
- `merge` (bool): If True, merge with existing; if False, replace (default: True)
- `increment_version` (bool): Increment version number (default: True)

**Returns:** `str` - Updated playbook ID

**Example:**

```python
# Add new helpful strategy
playbook_mgr.update_playbook(
    playbook_id,
    strategies=[
        {
            "category": "helpful",
            "pattern": "Validate input before API calls",
            "context": "Data submission endpoints",
            "confidence": 0.85,
            "evidence_count": 3
        }
    ],
    merge=True
)

# Replace all strategies (use with caution!)
playbook_mgr.update_playbook(
    playbook_id,
    strategies=new_strategies,
    merge=False,
    increment_version=True
)
```

---

### `get_relevant_strategies()`

Get strategies relevant to a specific task.

```python
strategies = playbook_mgr.get_relevant_strategies(
    playbook_id="playbook_123",
    task_description="Call external API with pagination",
    strategy_type="helpful",
    min_confidence=0.8,
    limit=5
)
```

**Parameters:**

- `playbook_id` (str): Playbook ID
- `task_description` (str): Task description for semantic matching
- `strategy_type` (Literal["helpful", "harmful", "neutral"], optional): Filter by type
- `min_confidence` (float): Minimum confidence threshold (default: 0.0)
- `limit` (int): Maximum strategies to return (default: 10)

**Returns:** `List[dict]` - Relevant strategies sorted by relevance

**Example:**

```python
# Get helpful strategies for API calls
strategies = playbook_mgr.get_relevant_strategies(
    playbook_id,
    task_description="Call paginated API endpoint",
    strategy_type="helpful",
    min_confidence=0.7
)

for strategy in strategies:
    print(f"✓ {strategy['pattern']}")
    print(f"  Confidence: {strategy['confidence']:.1%}")
    print(f"  Evidence: {strategy['evidence_count']} trajectories")

# Get anti-patterns to avoid
harmful = playbook_mgr.get_relevant_strategies(
    playbook_id,
    task_description="Handle API errors",
    strategy_type="harmful"
)

for pattern in harmful:
    print(f"✗ AVOID: {pattern['pattern']}")
```

---

### `list_playbooks()`

List all accessible playbooks.

```python
playbooks = playbook_mgr.list_playbooks(
    scope="agent",
    limit=50
)
```

**Parameters:**

- `scope` (str, optional): Filter by scope
- `limit` (int): Maximum results (default: 50)

**Returns:** `List[dict]` - Playbook summaries

**Example:**

```python
# List agent playbooks
agent_playbooks = playbook_mgr.list_playbooks(scope="agent")

for pb in agent_playbooks:
    print(f"{pb['name']} (v{pb['version']})")
    print(f"  Strategies: {pb['strategy_count']}")
    print(f"  Success rate: {pb['success_rate']:.1%}")
```

---

### `delete_playbook()`

Delete a playbook.

```python
success = playbook_mgr.delete_playbook(playbook_id="playbook_123")
```

**Parameters:**

- `playbook_id` (str): Playbook ID

**Returns:** `bool` - True if deleted, False if not found/no permission

**Example:**

```python
if playbook_mgr.delete_playbook(playbook_id):
    print("Playbook deleted")
else:
    print("Playbook not found or no permission")
```

---

## Curation API

### `Curator`

Automatically curate playbooks from trajectory reflections.

```python
from nexus.core.ace.curation import Curator

curator = Curator(
    session=db_session,
    backend=storage_backend,
    playbook_manager=playbook_mgr
)
```

---

### `curate_playbook()`

Automatically extract and merge strategies from reflections.

```python
result = curator.curate_playbook(
    playbook_id="playbook_123",
    reflection_memory_ids=["mem_abc", "mem_def", "mem_xyz"],
    llm_provider=llm_provider
)
```

**Parameters:**

- `playbook_id` (str): Target playbook ID
- `reflection_memory_ids` (List[str]): Reflection memory IDs to analyze
- `llm_provider` (LLMProvider, optional): LLM for extraction

**Returns:** `dict` - Curation result

```python
{
    "added": List[dict],      # New strategies added
    "updated": List[dict],    # Existing strategies updated
    "removed": List[dict],    # Outdated strategies removed
    "merged": List[dict],     # Duplicate strategies merged
    "playbook_version": int   # New version number
}
```

**Example:**

```python
# Collect reflections
reflections = []
for traj_id in recent_trajectories:
    reflection = nx.memory.reflect(traj_id)
    reflections.append(reflection['memory_id'])

# Curate playbook
result = curator.curate_playbook(
    playbook_id,
    reflection_memory_ids=reflections
)

print(f"Added: {len(result['added'])} strategies")
print(f"Updated: {len(result['updated'])} strategies")
print(f"Removed: {len(result['removed'])} outdated strategies")
print(f"New version: {result['playbook_version']}")
```

---

## Usage Examples

### Basic Playbook Usage

```python
import nexus

nx = nexus.connect()

# Create playbook
playbook_id = nx.ace.playbook_manager.create_playbook(
    name="deployment_playbook",
    description="Strategies for safe deployments"
)

# Add strategies manually
nx.ace.playbook_manager.update_playbook(
    playbook_id,
    strategies=[
        {
            "category": "helpful",
            "pattern": "Use blue-green deployment for zero downtime",
            "context": "Production deployments",
            "confidence": 0.95,
            "evidence_count": 10
        },
        {
            "category": "harmful",
            "pattern": "Deploy during peak hours",
            "context": "Production deployments",
            "confidence": 0.9,
            "evidence_count": 5
        }
    ]
)

# Retrieve and use playbook
playbook = nx.ace.playbook_manager.get_playbook(playbook_id=playbook_id)

# Apply strategies
for strategy in playbook['strategies']['helpful']:
    if strategy['confidence'] > 0.8:
        print(f"✓ Apply: {strategy['pattern']}")

for pattern in playbook['strategies']['harmful']:
    print(f"✗ Avoid: {pattern['pattern']}")
```

---

### Automatic Curation from Trajectories

```python
# Execute tasks with learning
for task in tasks:
    result = nx.ace.learning_loop.execute_with_learning(
        task_description=f"Process {task}",
        task_fn=lambda: process_task(task),
        playbook_id=playbook_id,
        enable_reflection=True,
        enable_curation=True  # Auto-curate!
    )

# Playbook automatically updated with learned strategies!
playbook = nx.ace.playbook_manager.get_playbook(playbook_id=playbook_id)
print(f"Playbook now has {len(playbook['strategies']['helpful'])} helpful strategies")
```

---

### Multi-Agent Playbook Sharing

```python
# Agent A creates playbook
agent_a = nexus.connect(config={"agent_id": "agent_a"})
playbook_id = agent_a.ace.playbook_manager.create_playbook(
    name="shared_strategies",
    scope="agent",
    visibility="shared"
)

# Grant access to Agent B
agent_a.rebac.create(
    subject=("agent", "agent_b"),
    relation="viewer",
    object=("playbook", playbook_id)
)

# Agent B can now read the playbook
agent_b = nexus.connect(config={"agent_id": "agent_b"})
playbook = agent_b.ace.playbook_manager.get_playbook(playbook_id=playbook_id)
print(f"Agent B accessing Agent A's playbook: {playbook['name']}")
```

---

### Task-Specific Strategy Selection

```python
def execute_with_playbook_guidance(task_description: str):
    """Execute task with relevant strategies."""

    # Get relevant strategies
    strategies = nx.ace.playbook_manager.get_relevant_strategies(
        playbook_id,
        task_description=task_description,
        strategy_type="helpful",
        min_confidence=0.7
    )

    print(f"Applying {len(strategies)} relevant strategies:")
    for s in strategies:
        print(f"  • {s['pattern']} ({s['confidence']:.0%} confidence)")

    # Execute with strategies in context
    result = execute_task(strategies=strategies)
    return result

# Use it
execute_with_playbook_guidance("Call paginated REST API with retry logic")
```

---

## CLI Commands

### Create Playbook

```bash
# Via update command with empty file
echo '{"strategies": []}' > empty.json
nexus memory playbook update my_playbook --strategies empty.json
```

### Get Playbook

```bash
nexus memory playbook get api_playbook --json
```

### Update Playbook

```bash
# Prepare strategies file
cat > strategies.json << EOF
[
  {
    "category": "helpful",
    "pattern": "Use connection pooling for database queries",
    "context": "High-throughput applications",
    "confidence": 0.9,
    "evidence_count": 8
  }
]
EOF

nexus memory playbook update my_playbook --strategies strategies.json
```

### Curate Playbook

```bash
# Auto-curate from reflections
nexus memory playbook curate \
  --reflections mem_abc,mem_def,mem_xyz \
  --name my_playbook \
  --json
```

### List Playbooks

```bash
nexus memory playbook list --scope agent --json
```

---

## Best Practices

### 1. Organize by Domain

Create separate playbooks for different domains:

```python
# API client strategies
api_playbook = create_playbook("api_client_playbook")

# Data processing strategies
data_playbook = create_playbook("data_processing_playbook")

# Deployment strategies
deploy_playbook = create_playbook("deployment_playbook")
```

### 2. High-Quality Strategy Patterns

Write clear, actionable patterns:

```python
# ✗ Bad: Too vague
{"pattern": "Use caching"}

# ✓ Good: Specific and actionable
{
    "pattern": "Cache GET requests for 5 minutes using Redis",
    "context": "Frequently accessed read-only data",
    "confidence": 0.9,
    "evidence_count": 12
}
```

### 3. Confidence Thresholds

Filter strategies by confidence:

```python
# Only apply high-confidence strategies
strategies = get_relevant_strategies(
    playbook_id,
    task_description,
    min_confidence=0.8  # 80%+
)
```

### 4. Regular Curation

Schedule periodic curation:

```python
import schedule

def curate_job():
    """Periodic curation from recent trajectories."""
    # Get recent reflections
    trajectories = nx.memory.query_trajectories(
        status="success",
        limit=20
    )

    reflections = []
    for traj in trajectories:
        reflection = nx.memory.reflect(traj['trajectory_id'])
        reflections.append(reflection['memory_id'])

    # Curate
    result = curator.curate_playbook(playbook_id, reflections)
    print(f"Curated: +{len(result['added'])} strategies")

# Run daily
schedule.every().day.at("02:00").do(curate_job)
```

### 5. Version Control

Track playbook evolution:

```python
# Get version history
versions = playbook_mgr.list_versions(
    name="api_playbook",
    limit=10
)

for v in versions:
    print(f"v{v['version']}: {v['strategy_count']} strategies")
    print(f"  Updated: {v['updated_at']}")
```

---

## Strategy Confidence Scoring

Confidence is calculated based on:

1. **Success Rate:** Trajectories using this pattern
2. **Evidence Count:** Number of supporting examples
3. **Recency:** Recent evidence weighted higher
4. **Consistency:** How consistent the outcomes are

```python
# Confidence calculation (conceptual)
base_confidence = success_count / total_trajectories
evidence_boost = min(0.2, evidence_count * 0.01)
recency_factor = calculate_recency_decay(evidence)

confidence = min(1.0, base_confidence + evidence_boost) * recency_factor
```

---

## Related Documentation

- [ACE Learning Loop API](ace-learning-loop.md)
- [Trajectory Tracking API](trajectory-tracking.md)
- [Memory Consolidation API](memory-consolidation.md)
- [Design Document](../design/ACE_INTEGRATION.md)

---

## See Also

- [ACE Paper](https://arxiv.org/abs/2510.04618) - Original research
- [ACE Examples](../examples/ace.md) - Working examples
- [Memory Management API](memory-management.md) - Core memory API
