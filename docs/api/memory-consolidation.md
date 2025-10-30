# Memory Consolidation API

**Status:** v0.5.0+
**Module:** `nexus.core.ace.consolidation`

## Overview

The Memory Consolidation API prevents context collapse by intelligently merging similar low-importance memories while preserving critical high-importance knowledge. Based on importance-based preservation from the ACE paper, it ensures agents maintain essential details as their memory grows.

**Key Features:**

- 🧠 Importance-based preservation
- 🔄 Semantic similarity detection
- 📊 Configurable consolidation strategies
- 🔒 High-importance memory protection
- 📈 Batch processing for efficiency
- 🎯 LLM-powered semantic merging
- 🔍 Consolidation lineage tracking

## The Context Collapse Problem

As agents accumulate memories, context windows become overloaded:

```
Day 1:   [Memory 1] [Memory 2] [Memory 3]
Day 30:  [Memory 1] [Memory 2] ... [Memory 500]  ← Context overflow!
Day 60:  [Memory 1] [Memory 2] ... [Memory 1000] ← Critical details lost
```

**Without consolidation:**
- ❌ Context windows exceeded
- ❌ Critical details buried
- ❌ Query performance degraded
- ❌ Irrelevant old memories persist

**With consolidation:**
- ✅ Context stays manageable
- ✅ Important details preserved
- ✅ Related memories merged
- ✅ Query performance maintained

---

## API Reference

### `ConsolidationEngine`

Main class for memory consolidation.

#### Initialization

```python
from nexus.core.ace.consolidation import ConsolidationEngine

consolidation_engine = ConsolidationEngine(
    session=db_session,
    backend=storage_backend,
    llm_provider=llm_provider,
    user_id="user_123",
    agent_id="agent_456",
    tenant_id="tenant_789"
)
```

**Parameters:**

- `session` (Session): Database session
- `backend` (Any): Storage backend for CAS content
- `llm_provider` (LLMProvider): LLM provider for semantic merging
- `user_id` (str): User ID for ownership
- `agent_id` (str, optional): Agent ID for scoping
- `tenant_id` (str, optional): Tenant ID for multi-tenancy

---

### `consolidate_by_criteria()`

Consolidate memories matching specific criteria.

```python
results = consolidation_engine.consolidate_by_criteria(
    memory_type="experience",
    importance_max=0.5,
    batch_size=10,
    min_age_days=7,
    scope="agent"
)
```

**Parameters:**

- `memory_type` (str, optional): Filter by memory type
  - `"fact"`, `"preference"`, `"experience"`, `"observation"`, etc.
- `importance_max` (float): Maximum importance threshold (default: 0.5)
  - Only consolidate memories with importance ≤ this value
- `batch_size` (int): Memories per consolidation batch (default: 10)
- `min_age_days` (int): Minimum age in days (default: 0)
  - Only consolidate memories older than this
- `scope` (str, optional): Filter by scope
  - `"agent"`, `"user"`, `"tenant"`, `"global"`

**Returns:** `List[dict]` - Consolidation results

```python
[
    {
        "consolidated_memory_id": str,
        "source_memory_ids": List[str],
        "memories_merged": int,
        "content_preview": str,
        "importance": float,
        "consolidation_strategy": str
    },
    ...
]
```

**Example:**

```python
# Consolidate old low-importance experiences
results = consolidation_engine.consolidate_by_criteria(
    memory_type="experience",
    importance_max=0.5,  # Only importance ≤ 0.5
    batch_size=20,
    min_age_days=7,  # Only memories > 7 days old
    scope="agent"
)

for result in results:
    print(f"Merged {result['memories_merged']} memories")
    print(f"New memory: {result['consolidated_memory_id']}")
    print(f"Preview: {result['content_preview'][:100]}...")
```

---

### `consolidate_similar_memories()`

Consolidate semantically similar memories.

```python
result = consolidation_engine.consolidate_similar_memories(
    memory_ids=["mem_1", "mem_2", "mem_3"],
    preserve_importance=True,
    strategy="semantic_merge"
)
```

**Parameters:**

- `memory_ids` (List[str]): Memory IDs to consolidate
- `preserve_importance` (bool): Keep highest importance (default: True)
- `strategy` (str): Consolidation strategy
  - `"semantic_merge"` - LLM merges content semantically (default)
  - `"concatenate"` - Simple concatenation
  - `"summarize"` - LLM generates summary

**Returns:** `dict` - Consolidation result

```python
{
    "consolidated_memory_id": str,
    "source_memory_ids": List[str],
    "content": str,
    "importance": float,
    "metadata": dict
}
```

**Example:**

```python
# Find similar memories
similar_groups = consolidation_engine.find_similar_memory_groups(
    scope="agent",
    similarity_threshold=0.85
)

# Consolidate each group
for group in similar_groups:
    result = consolidation_engine.consolidate_similar_memories(
        memory_ids=group,
        preserve_importance=True,
        strategy="semantic_merge"
    )
    print(f"Consolidated {len(group)} → {result['consolidated_memory_id']}")
```

---

### `find_similar_memory_groups()`

Identify groups of similar memories for consolidation.

```python
groups = consolidation_engine.find_similar_memory_groups(
    scope="agent",
    memory_type="experience",
    similarity_threshold=0.85,
    min_group_size=2,
    max_groups=10
)
```

**Parameters:**

- `scope` (str, optional): Filter by scope
- `memory_type` (str, optional): Filter by type
- `similarity_threshold` (float): Semantic similarity threshold (default: 0.85)
- `min_group_size` (int): Minimum memories per group (default: 2)
- `max_groups` (int): Maximum groups to return (default: 10)

**Returns:** `List[List[str]]` - Groups of similar memory IDs

```python
[
    ["mem_1", "mem_2", "mem_3"],      # Group 1: Similar memories
    ["mem_10", "mem_11"],             # Group 2: Similar memories
    ...
]
```

**Example:**

```python
# Find similar memory groups
groups = consolidation_engine.find_similar_memory_groups(
    scope="agent",
    memory_type="experience",
    similarity_threshold=0.9  # High similarity required
)

print(f"Found {len(groups)} groups of similar memories")
for i, group in enumerate(groups):
    print(f"Group {i+1}: {len(group)} memories")
```

---

### `get_consolidation_candidates()`

Get memories eligible for consolidation.

```python
candidates = consolidation_engine.get_consolidation_candidates(
    importance_max=0.5,
    min_age_days=7,
    memory_type="experience",
    limit=100
)
```

**Parameters:**

- `importance_max` (float): Maximum importance threshold
- `min_age_days` (int): Minimum age in days
- `memory_type` (str, optional): Filter by type
- `limit` (int): Maximum candidates (default: 100)

**Returns:** `List[dict]` - Candidate memories

**Example:**

```python
# Preview consolidation candidates
candidates = consolidation_engine.get_consolidation_candidates(
    importance_max=0.5,
    min_age_days=30,
    limit=50
)

print(f"Found {len(candidates)} consolidation candidates")
for c in candidates[:5]:
    print(f"  {c['memory_id']}: importance={c['importance']}")
```

---

### `rollback_consolidation()`

Rollback a consolidation by restoring source memories.

```python
success = consolidation_engine.rollback_consolidation(
    consolidated_memory_id="mem_consolidated_123"
)
```

**Parameters:**

- `consolidated_memory_id` (str): Consolidated memory ID

**Returns:** `bool` - True if rollback successful

**Example:**

```python
# Rollback if consolidation was incorrect
if consolidation_engine.rollback_consolidation(consolidated_id):
    print("Consolidation rolled back, source memories restored")
else:
    print("Rollback failed or no lineage found")
```

---

## Consolidation Strategies

### 1. Importance-Based Preservation

**Never consolidate high-importance memories:**

```python
# Safe consolidation
consolidation_engine.consolidate_by_criteria(
    importance_max=0.5,  # Only low-importance
    batch_size=10
)

# High-importance memories (>0.5) are protected!
```

### 2. Semantic Merging

**LLM intelligently merges related content:**

```python
# Before consolidation:
# mem_1: "User prefers dark mode in IDE"
# mem_2: "User likes dark themes"
# mem_3: "User uses dark color scheme"

result = consolidation_engine.consolidate_similar_memories(
    memory_ids=["mem_1", "mem_2", "mem_3"],
    strategy="semantic_merge"
)

# After consolidation:
# "User consistently prefers dark themes across IDE and applications"
```

### 3. Time-Based Consolidation

**Consolidate old memories, keep recent ones fresh:**

```python
# Only consolidate memories >30 days old
results = consolidation_engine.consolidate_by_criteria(
    min_age_days=30,
    importance_max=0.6
)
```

---

## Usage Examples

### Basic Consolidation

```python
import nexus

nx = nexus.connect()

# Run consolidation
results = nx.ace.consolidation_engine.consolidate_by_criteria(
    memory_type="experience",
    importance_max=0.5,
    batch_size=20,
    min_age_days=7
)

print(f"Consolidated {len(results)} memory batches")
for result in results:
    print(f"  Merged {result['memories_merged']} memories")
    print(f"  New importance: {result['importance']}")
```

---

### Scheduled Consolidation

```python
import schedule
import time

def consolidation_job():
    """Background job for memory consolidation."""
    results = nx.ace.consolidation_engine.consolidate_by_criteria(
        importance_max=0.5,
        batch_size=10,
        min_age_days=7
    )

    if results:
        print(f"Consolidated {len(results)} batches")
    else:
        print("No consolidation needed")

# Run daily at 2 AM
schedule.every().day.at("02:00").do(consolidation_job)

while True:
    schedule.run_pending()
    time.sleep(3600)  # Check hourly
```

---

### Dry Run Preview

```python
# Preview what would be consolidated
candidates = nx.ace.consolidation_engine.get_consolidation_candidates(
    importance_max=0.5,
    min_age_days=7,
    limit=100
)

print(f"Would consolidate {len(candidates)} memories:")
for c in candidates[:10]:
    print(f"  {c['memory_id']}: {c['content'][:50]}...")
    print(f"    Importance: {c['importance']}, Age: {c['age_days']} days")

# Proceed if acceptable
if input("Consolidate? (y/n): ").lower() == 'y':
    results = nx.ace.consolidation_engine.consolidate_by_criteria(
        importance_max=0.5,
        min_age_days=7
    )
    print(f"Consolidated {len(results)} batches")
```

---

### Smart Similarity-Based Consolidation

```python
# Find similar memory groups
groups = nx.ace.consolidation_engine.find_similar_memory_groups(
    scope="agent",
    similarity_threshold=0.85,
    min_group_size=3  # At least 3 similar memories
)

print(f"Found {len(groups)} groups of similar memories")

# Consolidate each group
for i, group in enumerate(groups):
    print(f"Consolidating group {i+1} ({len(group)} memories)...")

    result = nx.ace.consolidation_engine.consolidate_similar_memories(
        memory_ids=group,
        preserve_importance=True,
        strategy="semantic_merge"
    )

    print(f"  ✓ Created: {result['consolidated_memory_id']}")
    print(f"  Preview: {result['content'][:100]}...")
```

---

### Integration with Learning Loop

```python
from nexus.core.ace.learning_loop import LearningLoop

learning_loop = nx.ace.learning_loop

# After executing many tasks, consolidate old memories
def cleanup_old_memories():
    """Periodic memory cleanup."""

    # Consolidate low-importance memories
    results = learning_loop.consolidate_memories(
        memory_type="experience",
        importance_max=0.5,
        batch_size=10
    )

    print(f"Consolidated {len(results)} memory batches")

    # Also consolidate old reflections
    results = learning_loop.consolidate_memories(
        memory_type="reflection",
        importance_max=0.6,
        batch_size=5
    )

    print(f"Consolidated {len(results)} reflection batches")

# Run cleanup
cleanup_old_memories()
```

---

### Rollback Incorrect Consolidation

```python
# Consolidate memories
result = nx.ace.consolidation_engine.consolidate_similar_memories(
    memory_ids=["mem_1", "mem_2", "mem_3"]
)

consolidated_id = result['consolidated_memory_id']

# Later: User reports loss of important detail
# Rollback the consolidation
if nx.ace.consolidation_engine.rollback_consolidation(consolidated_id):
    print("✓ Rollback successful, original memories restored")
else:
    print("✗ Rollback failed")
```

---

## CLI Commands

### Consolidate Memories

```bash
# Basic consolidation
nexus memory consolidate \
  --type experience \
  --threshold 0.5 \
  --json

# Dry run to preview
nexus memory consolidate \
  --type experience \
  --threshold 0.5 \
  --dry-run

# With filters
nexus memory consolidate \
  --type observation \
  --threshold 0.6 \
  --json
```

---

## Best Practices

### 1. Conservative Importance Thresholds

Start with low thresholds and increase gradually:

```python
# ✓ Good: Conservative start
consolidate_by_criteria(importance_max=0.3)  # Very low importance only

# ⚠️ Risky: Too aggressive
consolidate_by_criteria(importance_max=0.8)  # May lose important details
```

### 2. Age-Based Protection

Protect recent memories:

```python
# Only consolidate memories >30 days old
consolidate_by_criteria(
    importance_max=0.5,
    min_age_days=30  # Keep recent memories fresh
)
```

### 3. Batch Size Tuning

Balance between efficiency and quality:

```python
# Small batches: Better semantic coherence
consolidate_by_criteria(batch_size=5)

# Large batches: More efficient but less coherent
consolidate_by_criteria(batch_size=50)

# Recommended: 10-20
consolidate_by_criteria(batch_size=15)
```

### 4. Regular Monitoring

Track consolidation metrics:

```python
def consolidate_with_metrics():
    """Consolidation with monitoring."""
    before_count = count_memories(scope="agent")

    results = consolidation_engine.consolidate_by_criteria(
        importance_max=0.5,
        batch_size=10
    )

    after_count = count_memories(scope="agent")

    reduction = before_count - after_count
    print(f"Memories: {before_count} → {after_count} (-{reduction})")
    print(f"Reduction: {reduction / before_count:.1%}")

consolidate_with_metrics()
```

### 5. Type-Specific Strategies

Different consolidation for different types:

```python
# Aggressive for observations (transient)
consolidate_by_criteria(
    memory_type="observation",
    importance_max=0.6,
    batch_size=20
)

# Conservative for facts (persistent)
consolidate_by_criteria(
    memory_type="fact",
    importance_max=0.3,
    batch_size=5
)

# Never consolidate preferences
# (Don't run consolidation on preference type)
```

---

## Performance Considerations

### Batch Processing

Process in batches for efficiency:

```python
# Process in chunks
total_candidates = get_consolidation_candidates(
    importance_max=0.5,
    limit=1000
)

for i in range(0, len(total_candidates), 10):
    batch = total_candidates[i:i+10]
    consolidate_similar_memories([m['memory_id'] for m in batch])
    time.sleep(0.1)  # Rate limiting
```

### Async Processing

Use async for background consolidation:

```python
import asyncio

async def async_consolidate():
    """Async consolidation job."""
    results = await consolidation_engine.consolidate_async(
        importance_max=0.5,
        batch_size=10
    )
    return results

# Run in background
asyncio.create_task(async_consolidate())
```

### Caching

Cache similarity calculations:

```python
# Embeddings are cached automatically in CAS
# No need to recompute for same content
```

---

## Consolidation Lineage

Track consolidation history:

```python
# After consolidation
result = consolidate_similar_memories(
    memory_ids=["mem_1", "mem_2", "mem_3"]
)

# Lineage tracked automatically
consolidated_memory = nx.memory.get(result['consolidated_memory_id'])

print(f"Consolidated from: {consolidated_memory['consolidated_from']}")
# Output: ["mem_1", "mem_2", "mem_3"]

# Can rollback anytime
rollback_consolidation(result['consolidated_memory_id'])
```

---

## Related Documentation

- [ACE Learning Loop API](ace-learning-loop.md)
- [Trajectory Tracking API](trajectory-tracking.md)
- [Playbook Management API](playbook-management.md)
- [Design Document](../design/ACE_INTEGRATION.md)

---

## See Also

- [ACE Paper](https://arxiv.org/abs/2510.04618) - Importance-based preservation
- [Memory Management API](memory-management.md) - Core memory operations
- [ACE Examples](../examples/ace.md) - Working consolidation examples
