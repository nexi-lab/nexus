"""Pydantic models for API v2 endpoints.

All request/response models for the Memory & ACE REST APIs.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

# =============================================================================
# Memory Models
# =============================================================================


class MemoryStoreRequest(BaseModel):
    """Request model for POST /api/v2/memories."""

    content: str | dict[str, Any] = Field(..., description="Memory content (text or JSON)")
    scope: Literal["agent", "user", "zone", "global", "session"] = Field(
        "user", description="Memory scope"
    )
    memory_type: str | None = Field(
        None,
        description="Memory type (fact, preference, experience, strategy, etc.)",
    )
    importance: float | None = Field(None, ge=0.0, le=1.0, description="Importance score")
    namespace: str | None = Field(None, description="Hierarchical namespace")
    path_key: str | None = Field(None, description="Unique key within namespace for upsert")
    state: Literal["active", "inactive"] = Field("active", description="Memory state")
    extract_entities: bool = Field(True, description="Extract named entities")
    extract_temporal: bool = Field(True, description="Extract temporal references")
    extract_relationships: bool = Field(False, description="Extract relationships")
    store_to_graph: bool = Field(False, description="Store entities to knowledge graph")
    valid_at: str | None = Field(None, description="When fact became valid (ISO-8601)")  # #1183
    metadata: dict[str, Any] | None = Field(None, description="Additional metadata")


class MemoryUpdateRequest(BaseModel):
    """Request model for PUT /api/v2/memories/{id}."""

    content: str | dict[str, Any] | None = Field(None, description="Updated content")
    importance: float | None = Field(None, ge=0.0, le=1.0, description="Updated importance")
    state: Literal["active", "inactive"] | None = Field(None, description="Updated state")
    namespace: str | None = Field(None, description="Updated namespace")
    metadata: dict[str, Any] | None = Field(None, description="Updated metadata")


class MemorySearchRequest(BaseModel):
    """Request model for POST /api/v2/memories/search."""

    query: str = Field(..., description="Search query")
    scope: str | None = Field(None, description="Filter by scope")
    memory_type: str | None = Field(None, description="Filter by memory type")
    limit: int = Field(10, ge=1, le=100, description="Maximum results")
    search_mode: Literal["semantic", "keyword", "hybrid"] = Field(
        "hybrid", description="Search mode"
    )
    after: str | None = Field(None, description="Filter by created after (ISO-8601)")
    before: str | None = Field(None, description="Filter by created before (ISO-8601)")
    during: str | None = Field(None, description="Filter by time period (e.g., 'last week')")
    entity_type: str | None = Field(None, description="Filter by entity type")
    person: str | None = Field(None, description="Filter by person name")


class MemoryQueryRequest(BaseModel):
    """Request model for POST /api/v2/memories/query (#1185 point-in-time queries)."""

    scope: str | None = Field(None, description="Filter by scope")
    memory_type: str | None = Field(None, description="Filter by memory type")
    namespace: str | None = Field(None, description="Filter by exact namespace")
    namespace_prefix: str | None = Field(None, description="Filter by namespace prefix")
    state: str | None = Field("active", description="Filter by state (active, inactive, all)")
    limit: int | None = Field(None, ge=1, le=1000, description="Maximum results")
    # Temporal filters
    after: str | None = Field(None, description="Filter by created after (ISO-8601)")
    before: str | None = Field(None, description="Filter by created before (ISO-8601)")
    during: str | None = Field(None, description="Filter by time period")
    # Entity filters
    entity_type: str | None = Field(None, description="Filter by entity type")
    person: str | None = Field(None, description="Filter by person name")
    # Event date filters (#1028)
    event_after: str | None = Field(None, description="Filter by event date >= (ISO-8601)")
    event_before: str | None = Field(None, description="Filter by event date <= (ISO-8601)")
    # Bi-temporal filters (#1185)
    include_invalid: bool = Field(False, description="Include invalidated memories")
    as_of_event: str | None = Field(
        None, description="What was TRUE at time X? (ISO-8601, filters by valid_at/invalid_at)"
    )
    as_of_system: str | None = Field(
        None, description="What did SYSTEM KNOW at time X? (ISO-8601, filters by created_at)"
    )
    # Append-only filters (#1188)
    include_superseded: bool = Field(False, description="Include superseded (old version) memories")


class MemoryBatchStoreRequest(BaseModel):
    """Request model for POST /api/v2/memories/batch."""

    memories: list[MemoryStoreRequest] = Field(..., description="List of memories to store")


class MemoryResponse(BaseModel):
    """Response model for memory objects."""

    memory_id: str
    content: str | dict[str, Any]
    content_hash: str | None = None
    scope: str
    memory_type: str | None = None
    importance: float | None = None
    importance_effective: float | None = None
    state: str
    namespace: str | None = None
    path_key: str | None = None
    access_count: int = 0
    entities: list[dict[str, Any]] | None = None
    temporal_refs: list[dict[str, Any]] | None = None
    created_at: str | None = None
    updated_at: str | None = None


class MemoryStoreResponse(BaseModel):
    """Response for POST /api/v2/memories."""

    memory_id: str
    status: str = "created"


class MemoryBatchStoreResponse(BaseModel):
    """Response for POST /api/v2/memories/batch."""

    stored: int
    failed: int
    memory_ids: list[str]
    errors: list[dict[str, Any]] | None = None


class MemoryVersionHistoryResponse(BaseModel):
    """Response for GET /api/v2/memories/{id}/history."""

    memory_id: str
    current_version: int
    versions: list[dict[str, Any]]


# =============================================================================
# Trajectory Models
# =============================================================================


class TrajectoryStartRequest(BaseModel):
    """Request for POST /api/v2/trajectories."""

    task_description: str = Field(..., description="Description of the task")
    task_type: str | None = Field(None, description="Type of task")
    parent_trajectory_id: str | None = Field(None, description="Parent trajectory for nesting")
    metadata: dict[str, Any] | None = Field(None, description="Additional metadata")
    path: str | None = Field(None, description="File path context")


class TrajectoryStepRequest(BaseModel):
    """Request for POST /api/v2/trajectories/{id}/steps."""

    step_type: Literal["action", "decision", "observation", "tool_call", "error"] = Field(
        ..., description="Type of step"
    )
    description: str = Field(..., description="Step description")
    result: Any | None = Field(None, description="Step result/output")
    metadata: dict[str, Any] | None = Field(None, description="Step metadata")


class TrajectoryCompleteRequest(BaseModel):
    """Request for POST /api/v2/trajectories/{id}/complete."""

    status: Literal["success", "failure", "partial", "cancelled"] = Field(
        ..., description="Completion status"
    )
    success_score: float | None = Field(None, ge=0.0, le=1.0, description="Success score")
    error_message: str | None = Field(None, description="Error message if failed")
    metrics: dict[str, Any] | None = Field(None, description="Completion metrics")


class TrajectoryQueryParams(BaseModel):
    """Query parameters for GET /api/v2/trajectories."""

    agent_id: str | None = Field(None, description="Filter by agent ID")
    task_type: str | None = Field(None, description="Filter by task type")
    status: str | None = Field(None, description="Filter by status")
    limit: int = Field(50, ge=1, le=100, description="Maximum results")
    path: str | None = Field(None, description="Filter by path")


class TrajectoryResponse(BaseModel):
    """Response model for trajectory objects."""

    trajectory_id: str
    task_description: str
    task_type: str | None = None
    status: str
    success_score: float | None = None
    duration_ms: int | None = None
    step_count: int = 0
    started_at: str | None = None
    completed_at: str | None = None
    trace: list[dict[str, Any]] | None = None


class TrajectoryStartResponse(BaseModel):
    """Response for POST /api/v2/trajectories."""

    trajectory_id: str
    status: str = "in_progress"


# =============================================================================
# Feedback Models
# =============================================================================


class FeedbackAddRequest(BaseModel):
    """Request for POST /api/v2/feedback."""

    trajectory_id: str = Field(..., description="Trajectory to add feedback to")
    feedback_type: Literal["human", "monitoring", "ab_test", "production"] = Field(
        ..., description="Type of feedback"
    )
    score: float | None = Field(None, ge=0.0, le=1.0, description="Feedback score")
    source: str | None = Field(None, description="Feedback source identifier")
    message: str | None = Field(None, description="Feedback message")
    metrics: dict[str, Any] | None = Field(None, description="Feedback metrics")


class FeedbackScoreRequest(BaseModel):
    """Request for POST /api/v2/feedback/score."""

    trajectory_id: str = Field(..., description="Trajectory to score")
    strategy: Literal["latest", "average", "weighted"] = Field(
        "latest", description="Scoring strategy"
    )


class FeedbackRelearnRequest(BaseModel):
    """Request for POST /api/v2/feedback/relearn."""

    trajectory_id: str = Field(..., description="Trajectory to mark for relearning")
    reason: str = Field(..., description="Reason for relearning")
    priority: int = Field(5, ge=1, le=10, description="Relearning priority")


class FeedbackResponse(BaseModel):
    """Response model for feedback objects."""

    feedback_id: str
    trajectory_id: str
    feedback_type: str
    score: float | None = None
    source: str | None = None
    message: str | None = None
    created_at: str | None = None


class FeedbackAddResponse(BaseModel):
    """Response for POST /api/v2/feedback."""

    feedback_id: str
    status: str = "created"


class FeedbackScoreResponse(BaseModel):
    """Response for POST /api/v2/feedback/score."""

    trajectory_id: str
    effective_score: float
    strategy: str


# =============================================================================
# Playbook Models
# =============================================================================


class PlaybookCreateRequest(BaseModel):
    """Request for POST /api/v2/playbooks."""

    name: str = Field(..., description="Playbook name")
    description: str | None = Field(None, description="Playbook description")
    scope: Literal["agent", "user", "zone", "global"] = Field("agent", description="Playbook scope")
    visibility: Literal["private", "shared", "public"] = Field(
        "private", description="Playbook visibility"
    )
    initial_strategies: list[dict[str, Any]] | None = Field(None, description="Initial strategies")


class PlaybookUpdateRequest(BaseModel):
    """Request for PUT /api/v2/playbooks/{id}."""

    strategies: list[dict[str, Any]] | None = Field(None, description="Updated strategies")
    metadata: dict[str, Any] | None = Field(None, description="Updated metadata")
    increment_version: bool = Field(True, description="Increment version number")


class PlaybookUsageRequest(BaseModel):
    """Request for POST /api/v2/playbooks/{id}/usage."""

    success: bool = Field(..., description="Whether the usage was successful")
    improvement_score: float | None = Field(None, ge=0.0, le=1.0, description="Improvement score")


class PlaybookResponse(BaseModel):
    """Response model for playbook objects."""

    playbook_id: str
    name: str
    description: str | None = None
    version: int = 1
    scope: str
    visibility: str
    usage_count: int = 0
    success_rate: float | None = None
    strategies: list[dict[str, Any]] | None = None
    created_at: str | None = None
    updated_at: str | None = None


class PlaybookCreateResponse(BaseModel):
    """Response for POST /api/v2/playbooks."""

    playbook_id: str
    status: str = "created"


# =============================================================================
# Reflection & Curation Models
# =============================================================================


class ReflectRequest(BaseModel):
    """Request for POST /api/v2/reflect."""

    trajectory_id: str = Field(..., description="Trajectory to reflect on")
    context: str | None = Field(None, description="Additional context")
    reflection_prompt: str | None = Field(None, description="Custom reflection prompt")


class CurateRequest(BaseModel):
    """Request for POST /api/v2/curate."""

    playbook_id: str = Field(..., description="Target playbook")
    reflection_memory_ids: list[str] = Field(..., description="Reflection memories to curate")
    merge_threshold: float = Field(0.7, ge=0.0, le=1.0, description="Strategy merge threshold")


class CurateBulkRequest(BaseModel):
    """Request for POST /api/v2/curate/bulk."""

    playbook_id: str = Field(..., description="Target playbook")
    trajectory_ids: list[str] = Field(..., description="Trajectories to curate from")


class ReflectionResponse(BaseModel):
    """Response model for reflection results."""

    memory_id: str
    trajectory_id: str
    helpful_strategies: list[dict[str, Any]]
    harmful_patterns: list[dict[str, Any]]
    observations: list[dict[str, Any]]
    confidence: float


class CurationResponse(BaseModel):
    """Response for curation operations."""

    playbook_id: str
    strategies_added: int
    strategies_merged: int
    strategies_total: int


# =============================================================================
# Consolidation Models
# =============================================================================


class ConsolidateRequest(BaseModel):
    """Request for POST /api/v2/consolidate."""

    memory_ids: list[str] | None = Field(None, description="Specific memories to consolidate")
    beta: float = Field(0.7, ge=0.0, le=1.0, description="Semantic weight (SimpleMem)")
    lambda_decay: float = Field(0.1, description="Temporal decay rate")
    affinity_threshold: float = Field(0.85, ge=0.0, le=1.0, description="Clustering threshold")
    importance_max: float = Field(0.5, ge=0.0, le=1.0, description="Max importance for candidates")
    memory_type: str | None = Field(None, description="Filter by memory type")
    namespace: str | None = Field(None, description="Filter by namespace")
    limit: int = Field(100, ge=1, le=1000, description="Max memories to process")


class HierarchyBuildRequest(BaseModel):
    """Request for POST /api/v2/consolidate/hierarchy."""

    memory_ids: list[str] | None = Field(None, description="Specific memories")
    max_levels: int = Field(3, ge=1, le=10, description="Maximum hierarchy levels")
    cluster_threshold: float = Field(0.6, ge=0.0, le=1.0, description="Clustering threshold")
    beta: float = Field(0.7, ge=0.0, le=1.0, description="Semantic weight")
    lambda_decay: float = Field(0.1, description="Temporal decay rate")
    time_unit_hours: float = Field(24.0, description="Time unit for decay calculation")


class DecayRequest(BaseModel):
    """Request for POST /api/v2/consolidate/decay."""

    decay_factor: float = Field(0.95, ge=0.0, le=1.0, description="Decay factor per period")
    min_importance: float = Field(0.1, ge=0.0, le=1.0, description="Minimum importance floor")
    batch_size: int = Field(1000, ge=1, le=10000, description="Batch size for processing")


class ConsolidationResponse(BaseModel):
    """Response for consolidation operations."""

    clusters_formed: int
    total_consolidated: int
    archived_count: int = 0
    results: list[dict[str, Any]]


class HierarchyResponse(BaseModel):
    """Response for hierarchy operations."""

    total_memories: int
    total_abstracts_created: int
    max_level_reached: int
    levels: dict[str, Any]
    statistics: dict[str, Any] | None = None


class DecayResponse(BaseModel):
    """Response for decay operations."""

    success: bool
    updated: int
    skipped: int
    processed: int
    error: str | None = None


# =============================================================================
# Gateway Models
# =============================================================================


class GatewayMessageRequest(BaseModel):
    """Request to send a message through the gateway.

    All conversations are treated as "boardrooms" - same model for DMs and groups.
    """

    text: str = Field(..., description="Message content")
    user: str = Field(..., description="Sender ID (human ID or agent ID)")
    role: Literal["human", "agent"] = Field(..., description="Who is sending this message")
    session_id: str = Field(..., description="Boardroom key (channel:account_id:chat_id)")
    channel: str = Field(..., description="Platform (discord, slack, telegram)")

    id: str | None = Field(None, description="Channel's native message ID (for sync)")
    ts: str | None = Field(None, description="Original timestamp (ISO8601, for sync)")
    parent_id: str | None = Field(None, description="Reply-to message ID for threading")
    target: str | None = Field(None, description="@mention hint (not enforced)")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Extensible context")


class GatewayMessageResponse(BaseModel):
    """Response after processing a message."""

    message_id: str = Field(..., description="Unique message ID")
    status: Literal["created", "duplicate"] = Field(..., description="Processing result")
    ts: str = Field(..., description="ISO8601 timestamp")


class GatewaySyncRequest(BaseModel):
    """Request to sync conversation history from a channel.

    Priority for determining the sync starting point:
    1. `history_message_id` + `history_ts` (explicit cursor from caller)
    2. `after_id` (explicit parameter)
    3. Stored cursor from session metadata (incremental sync)
    """

    session_id: str = Field(..., description="Boardroom key (channel:account_id:chat_id)")
    channel: str = Field(..., description="Platform (discord, slack, telegram)")
    limit: int = Field(100, ge=1, le=1000, description="Maximum messages to fetch")
    before_id: str | None = Field(None, description="Fetch messages before this ID")
    after_id: str | None = Field(None, description="Fetch messages after this ID")
    history_message_id: str | None = Field(
        None,
        description="Channel's native message ID to start sync from (takes precedence over after_id)",
    )
    history_ts: str | None = Field(
        None,
        description="Timestamp of history_message_id (ISO8601, for channels that use time-based cursors)",
    )


class GatewaySyncResponse(BaseModel):
    """Response after syncing conversation history."""

    session_id: str = Field(..., description="Session that was synced")
    added: int = Field(..., description="Number of new messages added")
    skipped: int = Field(..., description="Number of duplicate messages skipped")
    total_fetched: int = Field(..., description="Total messages fetched from channel")
    last_synced_id: str | None = Field(None, description="ID of the last synced message (cursor)")
    last_synced_ts: str | None = Field(None, description="Timestamp of the last synced message")
