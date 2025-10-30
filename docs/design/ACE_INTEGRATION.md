# ACE (Agentic Context Engineering) Integration with Nexus Memory System

## Executive Summary

This document details the deep integration of ACE (Agentic Context Engineering) concepts into Nexus's memory system, creating a native learning loop for AI agents that accumulates, refines, and organizes strategies over time.

**Paper Reference:** ["Agentic Context Engineering: Evolving Contexts for Self-Improving Language Models" (2024)](https://arxiv.org/abs/2510.04618)

---

## 1. Architecture Overview

### 1.1 Current Nexus Memory System

```python
# Current capabilities
nx.memory.store(content, scope="user", memory_type="fact", importance=0.8)
nx.memory.query(scope="user", memory_type="preference")
nx.memory.search(query="Python best practices")
```

**Existing Infrastructure:**
- ✅ Content-addressable storage (SHA-256 deduplication)
- ✅ Identity-based access (tenant_id, user_id, agent_id)
- ✅ ReBAC permission enforcement
- ✅ Importance scoring (0.0-1.0)
- ✅ Memory types: fact, preference, experience
- ✅ Scopes: agent, user, tenant, global
- ✅ Workflow engine for automation

**Missing for ACE:**
- ❌ Playbook storage and retrieval
- ❌ Trajectory/execution tracking
- ❌ Reflection and analysis
- ❌ Strategy curation
- ❌ Consolidation to prevent context collapse
- ❌ Brevity bias prevention

### 1.2 Proposed ACE-Enhanced Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Agent Execution Layer                    │
│  (LangGraph, CrewAI, AutoGen, Custom Agents)               │
└─────────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────────┐
│              ACE Learning Loop (NEW)                        │
│                                                             │
│  Generator → Executor → Reflector → Curator                │
│  (use playbook) (track) (analyze)  (update)                │
└─────────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────────┐
│         Nexus Memory System (Enhanced)                      │
│                                                             │
│  ┌──────────────┬──────────────┬──────────────────────┐   │
│  │ Memories     │ Trajectories │ Playbooks            │   │
│  │ (existing)   │ (new)        │ (new)                │   │
│  │              │              │                      │   │
│  │ • Facts      │ • Steps      │ • Strategies         │   │
│  │ • Prefs      │ • Decisions  │ • Patterns           │   │
│  │ • Experience │ • Outcomes   │ • Anti-patterns      │   │
│  └──────────────┴──────────────┴──────────────────────┘   │
│                                                             │
│  Consolidation Engine (NEW) - Prevent brevity bias         │
│  Importance Preservation (NEW) - Critical knowledge         │
└─────────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────────┐
│  Storage Layer (CAS + PostgreSQL/SQLite)                    │
└─────────────────────────────────────────────────────────────┘
```

---

## 2. Core Components Design

### 2.1 Extended Memory Types

Add new memory types to support ACE:

```python
class MemoryType:
    # Existing
    FACT = "fact"               # Factual knowledge
    PREFERENCE = "preference"   # User/agent preferences
    EXPERIENCE = "experience"   # Past experiences

    # NEW: ACE-specific types
    STRATEGY = "strategy"       # Successful approaches (✓ helpful)
    ANTI_PATTERN = "anti_pattern"  # Failed patterns (✗ harmful)
    OBSERVATION = "observation"    # Neutral observations (○)
    TRAJECTORY = "trajectory"      # Execution trace
    REFLECTION = "reflection"      # Analysis of outcomes
    CONSOLIDATED = "consolidated"  # Merged memories
```

### 2.2 Database Schema Extensions

**New Table: `trajectories`**
```sql
CREATE TABLE trajectories (
    trajectory_id VARCHAR(36) PRIMARY KEY,
    agent_id VARCHAR(255) NOT NULL,
    user_id VARCHAR(255),
    tenant_id VARCHAR(255),

    -- Task info
    task_description TEXT NOT NULL,
    task_type VARCHAR(50),  -- 'api_call', 'data_processing', 'reasoning'

    -- Execution trace (stored as CAS content)
    trace_hash VARCHAR(64) NOT NULL,  -- JSON with steps/decisions/outcomes

    -- Outcome
    status VARCHAR(20) NOT NULL,  -- 'success', 'failure', 'partial'
    success_score FLOAT,  -- 0.0-1.0
    error_message TEXT,

    -- Metadata
    duration_ms INTEGER,
    tokens_used INTEGER,
    cost_usd FLOAT,

    -- Relations
    parent_trajectory_id VARCHAR(36),  -- For multi-step workflows

    -- Timestamps
    started_at TIMESTAMP NOT NULL,
    completed_at TIMESTAMP,

    -- Indexes
    INDEX idx_traj_agent (agent_id),
    INDEX idx_traj_status (status),
    INDEX idx_traj_task_type (task_type),
    INDEX idx_traj_completed (completed_at)
);
```

**New Table: `playbooks`**
```sql
CREATE TABLE playbooks (
    playbook_id VARCHAR(36) PRIMARY KEY,
    agent_id VARCHAR(255) NOT NULL,
    user_id VARCHAR(255),
    tenant_id VARCHAR(255),

    -- Playbook info
    name VARCHAR(255) NOT NULL,
    description TEXT,
    version INTEGER DEFAULT 1,

    -- Content (stored as CAS)
    content_hash VARCHAR(64) NOT NULL,  -- Structured playbook data

    -- Effectiveness metrics
    usage_count INTEGER DEFAULT 0,
    success_rate FLOAT DEFAULT 0.0,
    avg_improvement FLOAT DEFAULT 0.0,

    -- Scope
    scope VARCHAR(50) DEFAULT 'agent',  -- 'agent', 'user', 'tenant', 'global'
    visibility VARCHAR(50) DEFAULT 'private',

    -- Timestamps
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    last_used_at TIMESTAMP,

    -- Indexes
    INDEX idx_playbook_agent (agent_id),
    INDEX idx_playbook_name (name),
    INDEX idx_playbook_scope (scope),

    UNIQUE (agent_id, name, version)
);
```

**Extended `memories` Table:**
```sql
-- Add new columns to existing memories table
ALTER TABLE memories ADD COLUMN trajectory_id VARCHAR(36);
ALTER TABLE memories ADD COLUMN playbook_id VARCHAR(36);
ALTER TABLE memories ADD COLUMN consolidated_from TEXT;  -- JSON array of source memory_ids
ALTER TABLE memories ADD COLUMN consolidation_version INTEGER DEFAULT 0;

-- Add foreign keys
ALTER TABLE memories ADD CONSTRAINT fk_memory_trajectory
    FOREIGN KEY (trajectory_id) REFERENCES trajectories(trajectory_id);
ALTER TABLE memories ADD CONSTRAINT fk_memory_playbook
    FOREIGN KEY (playbook_id) REFERENCES playbooks(playbook_id);
```

### 2.3 Playbook Structure

Playbooks are stored as structured JSON in content-addressable storage:

```python
from dataclasses import dataclass
from typing import Literal

@dataclass
class StrategyEntry:
    """Single strategy in playbook."""
    category: Literal["helpful", "harmful", "neutral"]  # ✓, ✗, ○
    pattern: str  # Description of the pattern
    context: str  # When to use/avoid this
    confidence: float  # 0.0-1.0
    evidence_count: int  # Number of trajectories supporting this
    created_at: str
    updated_at: str
    examples: list[str]  # Example trajectories

@dataclass
class PlaybookContent:
    """Structured playbook content."""
    version: int
    strategies: list[StrategyEntry]

    # Categorized for quick access
    helpful: list[StrategyEntry]  # ✓ Do these
    harmful: list[StrategyEntry]  # ✗ Avoid these
    neutral: list[StrategyEntry]  # ○ Context-dependent

    # Meta
    last_consolidated: str | None  # ISO timestamp
    consolidation_count: int
    total_trajectories_analyzed: int

# Example playbook
playbook = PlaybookContent(
    version=5,
    strategies=[
        StrategyEntry(
            category="helpful",
            pattern="When processing API errors, always check rate limits first",
            context="API integration tasks with 429 errors",
            confidence=0.95,
            evidence_count=12,
            created_at="2025-10-15T10:00:00Z",
            updated_at="2025-10-28T14:30:00Z",
            examples=["traj_abc123", "traj_def456"]
        ),
        StrategyEntry(
            category="harmful",
            pattern="Retrying immediately without backoff causes cascading failures",
            context="Error handling in API calls",
            confidence=0.88,
            evidence_count=7,
            created_at="2025-10-20T11:00:00Z",
            updated_at="2025-10-28T14:30:00Z",
            examples=["traj_xyz789"]
        )
    ],
    helpful=[...],
    harmful=[...],
    neutral=[...],
    last_consolidated="2025-10-28T14:30:00Z",
    consolidation_count=3,
    total_trajectories_analyzed=45
)
```

---

## 3. Enhanced Memory API

### 3.1 New Methods

```python
class Memory:
    """Enhanced Memory API with ACE capabilities."""

    # ========== Existing methods (unchanged) ==========
    def store(self, content, scope="user", memory_type="fact", importance=0.8) -> str: ...
    def query(self, user_id=None, scope=None, memory_type=None) -> list[dict]: ...
    def search(self, query, scope=None, limit=10) -> list[dict]: ...

    # ========== NEW: Trajectory Management ==========

    def start_trajectory(
        self,
        task_description: str,
        task_type: str | None = None,
        parent_trajectory_id: str | None = None
    ) -> str:
        """Start tracking an execution trajectory.

        Args:
            task_description: Description of the task
            task_type: Type of task ('api_call', 'reasoning', etc.)
            parent_trajectory_id: Parent trajectory for multi-step workflows

        Returns:
            trajectory_id: ID for tracking this execution

        Example:
            >>> traj_id = nx.memory.start_trajectory(
            ...     task_description="Fetch user data from API",
            ...     task_type="api_call"
            ... )
        """

    def log_step(
        self,
        trajectory_id: str,
        step_type: str,
        description: str,
        metadata: dict | None = None
    ) -> None:
        """Log a step in the trajectory.

        Args:
            trajectory_id: Trajectory ID from start_trajectory()
            step_type: Type of step ('decision', 'action', 'observation')
            description: What happened
            metadata: Additional context (tool calls, LLM responses, etc.)

        Example:
            >>> nx.memory.log_step(
            ...     traj_id,
            ...     step_type="decision",
            ...     description="Decided to use exponential backoff",
            ...     metadata={"retry_count": 3, "delay_ms": 2000}
            ... )
        """

    def complete_trajectory(
        self,
        trajectory_id: str,
        status: Literal["success", "failure", "partial"],
        success_score: float | None = None,
        error_message: str | None = None,
        metrics: dict | None = None
    ) -> None:
        """Mark trajectory as complete with outcome.

        Args:
            trajectory_id: Trajectory ID
            status: Final status
            success_score: 0.0-1.0 success rating
            error_message: Error if failed
            metrics: Performance metrics (tokens, cost, duration)

        Example:
            >>> nx.memory.complete_trajectory(
            ...     traj_id,
            ...     status="success",
            ...     success_score=0.95,
            ...     metrics={"tokens": 1500, "cost_usd": 0.03, "duration_ms": 2340}
            ... )
        """

    # ========== NEW: Reflection & Analysis ==========

    def reflect(
        self,
        trajectory_id: str,
        llm_client: Any | None = None
    ) -> dict[str, Any]:
        """Analyze a trajectory to extract insights.

        Uses LLM to analyze what worked, what failed, and what patterns emerged.
        Stores reflection as a memory with type='reflection'.

        Args:
            trajectory_id: Trajectory to analyze
            llm_client: LLM client (uses default if None)

        Returns:
            Reflection analysis with extracted insights

        Example:
            >>> reflection = nx.memory.reflect(traj_id)
            >>> print(reflection['insights'])
            {
                'successful_patterns': [
                    'Used exponential backoff for retries',
                    'Validated input before API call'
                ],
                'failure_patterns': [
                    'Did not check rate limit headers'
                ],
                'recommendations': [
                    'Add rate limit monitoring',
                    'Cache API responses for 5 minutes'
                ]
            }
        """

    def batch_reflect(
        self,
        agent_id: str | None = None,
        since: str | None = None,
        task_type: str | None = None,
        min_trajectories: int = 5
    ) -> list[dict[str, Any]]:
        """Reflect on multiple trajectories to find patterns.

        Args:
            agent_id: Filter by agent
            since: ISO timestamp - only trajectories after this
            task_type: Filter by task type
            min_trajectories: Minimum trajectories needed for pattern detection

        Returns:
            List of pattern insights across trajectories

        Example:
            >>> patterns = nx.memory.batch_reflect(
            ...     agent_id="agent_123",
            ...     since="2025-10-01T00:00:00Z",
            ...     min_trajectories=10
            ... )
        """

    # ========== NEW: Playbook Management ==========

    def get_playbook(
        self,
        name: str = "default",
        agent_id: str | None = None
    ) -> dict[str, Any] | None:
        """Get playbook for agent.

        Args:
            name: Playbook name
            agent_id: Agent ID (uses current if None)

        Returns:
            Playbook content with strategies

        Example:
            >>> playbook = nx.memory.get_playbook("default")
            >>> for strategy in playbook['strategies']['helpful']:
            ...     print(f"✓ {strategy['pattern']}")
        """

    def update_playbook(
        self,
        strategies: list[dict],
        name: str = "default",
        merge: bool = True
    ) -> str:
        """Update playbook with new strategies.

        Args:
            strategies: New strategies to add
            name: Playbook name
            merge: If True, merge with existing; if False, replace

        Returns:
            playbook_id: Updated playbook ID

        Example:
            >>> nx.memory.update_playbook([
            ...     {
            ...         'category': 'helpful',
            ...         'pattern': 'Always validate inputs before API calls',
            ...         'context': 'API integration tasks',
            ...         'confidence': 0.9,
            ...         'evidence_count': 5
            ...     }
            ... ])
        """

    def curate_playbook(
        self,
        reflections: list[str],  # reflection memory_ids
        playbook_name: str = "default",
        llm_client: Any | None = None
    ) -> dict[str, Any]:
        """Automatically curate playbook from reflections.

        Uses LLM to extract strategies from reflection memories and
        update the playbook, removing duplicates and merging similar patterns.

        Args:
            reflections: List of reflection memory IDs to analyze
            playbook_name: Target playbook
            llm_client: LLM client

        Returns:
            Curation result with diff of what changed

        Example:
            >>> result = nx.memory.curate_playbook(
            ...     reflections=["mem_123", "mem_456", "mem_789"]
            ... )
            >>> print(f"Added: {len(result['added'])}")
            >>> print(f"Updated: {len(result['updated'])}")
            >>> print(f"Removed: {len(result['removed'])}")
        """

    # ========== NEW: Consolidation ==========

    def consolidate(
        self,
        memory_type: str | None = None,
        scope: str | None = None,
        min_importance: float = 0.5,
        preserve_high_importance: bool = True,
        importance_threshold: float = 0.8,
        llm_client: Any | None = None
    ) -> dict[str, Any]:
        """Consolidate memories to prevent context collapse.

        Implements importance-based preservation from ACE paper.
        Merges similar low-importance memories while preserving
        high-importance details.

        Args:
            memory_type: Filter by type
            scope: Filter by scope
            min_importance: Only consolidate memories below this
            preserve_high_importance: Never consolidate high-importance memories
            importance_threshold: Threshold for "high importance"
            llm_client: LLM for semantic merging

        Returns:
            Consolidation report

        Example:
            >>> report = nx.memory.consolidate(
            ...     memory_type="experience",
            ...     scope="agent",
            ...     min_importance=0.5,
            ...     importance_threshold=0.8
            ... )
            >>> print(f"Consolidated {report['merged_count']} memories")
            >>> print(f"Preserved {report['preserved_count']} high-importance")
        """

    # ========== NEW: ACE Learning Loop ==========

    def execute_with_learning(
        self,
        task_fn: callable,
        task_description: str,
        task_type: str | None = None,
        auto_reflect: bool = True,
        auto_curate: bool = True,
        playbook_name: str = "default"
    ) -> tuple[Any, str]:
        """Execute a task with automatic learning loop.

        This is the main ACE integration method that wraps task execution
        with trajectory tracking, reflection, and playbook curation.

        Args:
            task_fn: Function to execute (should return result and status)
            task_description: Description of task
            task_type: Type of task
            auto_reflect: Automatically reflect after completion
            auto_curate: Automatically update playbook from reflection
            playbook_name: Playbook to use/update

        Returns:
            (result, trajectory_id): Task result and trajectory ID

        Example:
            >>> def my_task():
            ...     # Load playbook strategies
            ...     playbook = nx.memory.get_playbook()
            ...     strategies = playbook['strategies']['helpful']
            ...
            ...     # Execute with learned strategies
            ...     result = call_api_with_strategies(strategies)
            ...     return result, "success"
            ...
            >>> result, traj_id = nx.memory.execute_with_learning(
            ...     my_task,
            ...     task_description="Call external API",
            ...     task_type="api_call"
            ... )
        """
```

---

## 4. Implementation Plan

### Phase 1: Database Schema & Models (Week 1)

**Files to create:**
```
src/nexus/storage/models.py          # Add TrajectoryModel, PlaybookModel
src/nexus/migrations/                 # Alembic migrations
  versions/
    add_trajectories_table.py
    add_playbooks_table.py
    extend_memories_for_ace.py
```

**Database migrations:**
1. Create `trajectories` table
2. Create `playbooks` table
3. Add `trajectory_id`, `playbook_id`, `consolidated_from` to `memories`
4. Add indexes for performance

### Phase 2: Core ACE Components (Week 2-3)

**Files to create:**
```
src/nexus/core/ace/
  __init__.py
  trajectory.py        # TrajectoryManager - track executions
  reflection.py        # Reflector - analyze outcomes
  curation.py          # Curator - update playbooks
  playbook.py          # PlaybookManager - CRUD operations
  consolidation.py     # ConsolidationEngine - merge memories
  learning_loop.py     # execute_with_learning() implementation
```

**Key classes:**

```python
# trajectory.py
class TrajectoryManager:
    def start(self, task_description, task_type=None) -> str: ...
    def log_step(self, trajectory_id, step_type, description, metadata=None): ...
    def complete(self, trajectory_id, status, success_score, error_message=None): ...
    def get_trajectory(self, trajectory_id) -> dict: ...
    def query_trajectories(self, filters) -> list[dict]: ...

# reflection.py
class Reflector:
    def reflect(self, trajectory_id, llm_client=None) -> dict: ...
    def batch_reflect(self, trajectories, llm_client=None) -> list[dict]: ...
    def extract_patterns(self, reflections) -> dict: ...

# curation.py
class Curator:
    def curate(self, reflections, playbook_name) -> dict: ...
    def merge_strategies(self, existing, new) -> list: ...
    def remove_duplicates(self, strategies) -> list: ...
    def calculate_confidence(self, strategy, evidence) -> float: ...

# playbook.py
class PlaybookManager:
    def get(self, name, agent_id=None) -> dict: ...
    def create(self, name, content, scope="agent") -> str: ...
    def update(self, playbook_id, content, increment_version=True) -> str: ...
    def delete(self, playbook_id) -> bool: ...
    def list_versions(self, name, agent_id=None) -> list: ...

# consolidation.py
class ConsolidationEngine:
    def consolidate(
        self,
        memory_type=None,
        scope=None,
        strategy="importance_based"
    ) -> dict: ...
    def identify_similar_memories(self, memories) -> list[list]: ...
    def merge_memories(self, memory_ids, llm_client=None) -> str: ...
    def preserve_high_importance(self, memories, threshold=0.8) -> list: ...
```

### Phase 3: Memory API Integration (Week 4)

**Update:**
```
src/nexus/core/memory_api.py
  - Add trajectory methods
  - Add reflection methods
  - Add playbook methods
  - Add consolidation methods
  - Add execute_with_learning()
```

### Phase 4: CLI Commands (Week 5)

**Update:**
```
src/nexus/cli/commands/memory.py
  - nexus memory trajectory start
  - nexus memory trajectory log
  - nexus memory trajectory complete
  - nexus memory trajectory list
  - nexus memory reflect <trajectory_id>
  - nexus memory reflect --batch
  - nexus memory playbook get
  - nexus memory playbook update
  - nexus memory playbook curate
  - nexus memory consolidate
```

### Phase 5: LLM Integration (Week 6)

**Create:**
```
src/nexus/llm/
  __init__.py
  client.py           # LLM client abstraction
  prompts.py          # Reflection and curation prompts
  providers/
    openai.py
    anthropic.py
    litellm.py        # For multi-provider support
```

**Reflection prompt example:**
```python
REFLECTION_PROMPT = """
Analyze the following task execution trajectory and extract insights:

Task: {task_description}
Status: {status}
Success Score: {success_score}

Execution Steps:
{steps}

Please identify:
1. What strategies were successful? (✓ Helpful patterns)
2. What approaches failed? (✗ Harmful patterns)
3. What observations are context-dependent? (○ Neutral)

Format your response as:
{
  "successful_patterns": [...],
  "failure_patterns": [...],
  "neutral_observations": [...],
  "recommendations": [...]
}
"""
```

### Phase 6: Testing & Documentation (Week 7-8)

**Tests:**
```
tests/unit/ace/
  test_trajectory_manager.py
  test_reflector.py
  test_curator.py
  test_playbook_manager.py
  test_consolidation.py
  test_learning_loop.py

tests/integration/
  test_ace_end_to_end.py
```

**Documentation:**
```
docs/api/
  ace-learning-loop.md
  trajectory-tracking.md
  playbook-management.md
  memory-consolidation.md

docs/guides/
  ace-quickstart.md
  ace-best-practices.md
```

---

## 5. Usage Examples

### 5.1 Basic Learning Loop

```python
import nexus

nx = nexus.connect()

# Define your agent's task
def api_task():
    # 1. Load playbook to get learned strategies
    playbook = nx.memory.get_playbook("api_client")
    strategies = playbook['strategies']['helpful']

    # 2. Apply strategies
    for strategy in strategies:
        print(f"Applying: {strategy['pattern']}")

    # 3. Execute task
    response = call_external_api()

    # 4. Return result and status
    return response, "success"

# Execute with automatic learning
result, traj_id = nx.memory.execute_with_learning(
    api_task,
    task_description="Call external API for user data",
    task_type="api_call",
    auto_reflect=True,
    auto_curate=True
)

# Playbook automatically updated with insights!
```

### 5.2 Manual Learning Loop

```python
# 1. Start trajectory
traj_id = nx.memory.start_trajectory(
    task_description="Process invoice PDFs",
    task_type="document_processing"
)

# 2. Execute task with logging
try:
    nx.memory.log_step(traj_id, "decision", "Checking PDF format")
    pdf_format = detect_format(pdf_file)

    nx.memory.log_step(traj_id, "action", f"Parsing as {pdf_format}")
    data = parse_pdf(pdf_file, format=pdf_format)

    nx.memory.log_step(traj_id, "observation", f"Extracted {len(data)} fields")

    # Task succeeded
    nx.memory.complete_trajectory(
        traj_id,
        status="success",
        success_score=0.95,
        metrics={"fields_extracted": len(data), "duration_ms": 1500}
    )

except Exception as e:
    nx.memory.log_step(traj_id, "observation", f"Error: {str(e)}")
    nx.memory.complete_trajectory(
        traj_id,
        status="failure",
        success_score=0.0,
        error_message=str(e)
    )

# 3. Reflect on what happened
reflection = nx.memory.reflect(traj_id)
print(reflection['insights'])

# 4. Update playbook
nx.memory.curate_playbook([reflection['memory_id']])
```

### 5.3 Batch Reflection & Curation

```python
# After running 50 API calls, reflect on patterns
patterns = nx.memory.batch_reflect(
    agent_id="api_agent",
    since="2025-10-01T00:00:00Z",
    task_type="api_call",
    min_trajectories=10
)

# Get all reflection memory IDs
reflection_ids = [p['memory_id'] for p in patterns]

# Curate playbook from patterns
result = nx.memory.curate_playbook(
    reflections=reflection_ids,
    playbook_name="api_client"
)

print(f"✓ Added {len(result['added'])} new strategies")
print(f"↻ Updated {len(result['updated'])} existing strategies")
print(f"✗ Removed {len(result['removed'])} outdated strategies")
```

### 5.4 Memory Consolidation

```python
# Prevent context collapse by consolidating old memories
report = nx.memory.consolidate(
    memory_type="experience",
    scope="agent",
    min_importance=0.5,
    preserve_high_importance=True,
    importance_threshold=0.8
)

print(f"Consolidated {report['merged_count']} memories")
print(f"Preserved {report['preserved_count']} high-importance memories")
print(f"Saved {report['space_saved_bytes']} bytes")
```

### 5.5 Multi-Agent Learning

```python
# Agent A learns from task execution
agent_a = nexus.connect(config={"agent_id": "agent_a"})
result, traj_id = agent_a.memory.execute_with_learning(
    task_fn=process_documents,
    task_description="Process PDF invoices"
)

# Agent B can access Agent A's playbook (with ReBAC permissions)
agent_b = nexus.connect(config={"agent_id": "agent_b"})

# Grant permission for Agent B to read Agent A's playbook
agent_a.rebac.create(
    subject=("agent", "agent_b"),
    relation="viewer",
    object=("playbook", f"agent_a/default")
)

# Agent B uses Agent A's learned strategies
playbook = agent_b.memory.get_playbook(
    name="default",
    agent_id="agent_a"  # Read from Agent A
)

# Apply learned strategies
for strategy in playbook['strategies']['helpful']:
    print(f"Learned from Agent A: {strategy['pattern']}")
```

---

## 6. Key Innovations

### 6.1 Importance-Based Preservation

**Problem from paper:** "Brevity bias" causes consolidation to lose critical details.

**Solution:**
```python
def consolidate(self, preserve_high_importance=True, importance_threshold=0.8):
    """
    Never consolidate memories above importance threshold.
    For consolidation candidates:
    - Track information loss
    - Preserve named entities, numbers, specific examples
    - Store consolidation lineage for rollback
    """
```

### 6.2 Evidence-Based Confidence

**Innovation:** Strategy confidence based on trajectory evidence.

```python
def calculate_confidence(strategy, evidence_trajectories):
    """
    Confidence = f(
        success_rate across trajectories,
        number of supporting examples,
        recency of evidence,
        consistency of results
    )
    """
    success_count = sum(1 for t in evidence if t.status == "success")
    total = len(evidence_trajectories)
    base_confidence = success_count / total

    # Boost for more evidence
    evidence_boost = min(0.2, len(evidence_trajectories) * 0.01)

    # Decay for old evidence
    recency_factor = calculate_recency_decay(evidence_trajectories)

    return min(1.0, base_confidence + evidence_boost) * recency_factor
```

### 6.3 Automatic Deduplication

**Problem:** Agents may learn the same pattern multiple times.

**Solution:** Semantic similarity detection during curation.

```python
def remove_duplicates(strategies, similarity_threshold=0.85):
    """
    Use semantic embeddings to detect duplicate strategies.
    Merge similar strategies and combine evidence counts.
    """
    embeddings = compute_embeddings([s.pattern for s in strategies])

    # Find similar pairs
    similar_pairs = find_similar_above_threshold(
        embeddings,
        threshold=similarity_threshold
    )

    # Merge strategies
    for s1, s2 in similar_pairs:
        merged = merge_strategies(s1, s2)
        strategies.append(merged)
        strategies.remove(s1)
        strategies.remove(s2)
```

### 6.4 Multi-Tenant Learning with Privacy

**Innovation:** Use Nexus ReBAC for playbook sharing.

```python
# Create private playbook (default)
playbook_id = nx.memory.create_playbook(
    name="api_strategies",
    scope="agent",
    visibility="private"
)

# Share playbook with team
nx.rebac.create(
    subject=("user", "alice"),
    relation="viewer",
    object=("playbook", playbook_id)
)

# Create tenant-wide playbook
team_playbook = nx.memory.create_playbook(
    name="team_best_practices",
    scope="tenant",
    visibility="shared"
)
```

---

## 7. Performance Considerations

### 7.1 Trajectory Storage Optimization

**Challenge:** Storing detailed traces for every execution is expensive.

**Solutions:**
1. **Sampling:** Only store detailed traces for failures + random sample of successes
2. **Compression:** Use content-addressable storage to deduplicate common steps
3. **Retention:** Auto-delete old trajectories after reflection (configurable)

```python
# Configuration
TRAJECTORY_RETENTION_DAYS = 30
TRAJECTORY_SAMPLE_RATE = 0.1  # 10% of successes
TRAJECTORY_ALWAYS_STORE_FAILURES = True
```

### 7.2 Consolidation Strategy

**Challenge:** Consolidating large memory stores is slow.

**Solutions:**
1. **Incremental:** Consolidate in batches of 100-1000 memories
2. **Background:** Run consolidation as async task
3. **Triggers:** Only consolidate when memory count exceeds threshold

```python
# Auto-consolidation triggers
AUTO_CONSOLIDATE_THRESHOLD = 10000  # memories
CONSOLIDATION_BATCH_SIZE = 1000
RUN_CONSOLIDATION_ASYNC = True
```

### 7.3 Playbook Versioning

**Challenge:** Frequent updates cause version bloat.

**Solution:** Semantic versioning with automatic cleanup.

```python
# Keep only:
# - Latest 5 versions
# - All major versions
# - Versions from last 90 days
PLAYBOOK_VERSION_RETENTION = {
    "keep_latest": 5,
    "keep_all_major": True,
    "keep_days": 90
}
```

---

## 9. Next Steps

### Immediate (This PR)
1. ✅ Design review (this document)
2. Create GitHub issues for each phase (#296-#302)
3. Get community feedback on API design

### Phase 1 (Week 1)
4. Database migrations
5. SQLAlchemy models

### Phase 2-3 (Week 2-4)
6. Core ACE components
7. Memory API integration

### Phase 4-6 (Week 5-8)
8. CLI commands
9. LLM integration
10. Tests & documentation

### Future Enhancements
- Workflow integration (trigger learning on workflow completion)
- Real-time playbook updates (WebSocket streaming)
- Playbook templates (starter playbooks for common tasks)
- Visualization dashboard (trajectory graphs, strategy evolution)
- A/B testing (compare playbook versions)

---

## 10. References

- **ACE Paper:** https://arxiv.org/abs/2510.04618
- **Google Zanzibar (ReBAC):** https://research.google/pubs/pub48190/
- **Nexus Issues:**
  - #152 - Importance-Based Memory Preservation
  - #150 - Automated Strategy Extraction
  - #126 - Strategy/Playbook Organization
  - #125 - Memory Reflection Phase
  - #124 - Memory Consolidation System

---

**Document Version:** 1.0
**Last Updated:** 2025-10-28
**Status:** Design Review
**Next Review:** After community feedback
