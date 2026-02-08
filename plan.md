# Plan: #1256 — Implement Google A2A Protocol Endpoint

## Architecture Decisions (Approved)

| # | Decision | Choice |
|---|----------|--------|
| 1 | Task state storage | Dedicated `a2a_tasks` PostgreSQL table |
| 2 | Agent Card generation | Dynamic from NexusConfig + Skills registry |
| 3 | SSE streaming | `StreamingResponse` + `asyncio.Queue` per-task |
| 4 | Module boundary | Top-level `src/nexus/a2a/` module |
| 5 | JSON-RPC models | Independent Pydantic A2A models (no shared base with `protocol.py`) |
| 6 | Authentication | Reuse `get_auth_result` via lazy imports |
| 7 | Error handling | A2A-specific exceptions in `a2a/exceptions.py` |
| 8 | Skill→AgentCard mapping | Agent Card builder in `a2a/agent_card.py` |
| 9 | Agent Card caching | Build once at startup, cache pre-serialized bytes |
| 10 | SSE lifecycle | Heartbeat + timeout + explicit cleanup on disconnect |
| 11 | DB indexes | Targeted: `(zone_id, agent_id, state)`, `context_id`, `created_at` |
| 12 | Active stream tracking | In-memory `dict[str, asyncio.Queue]` |

## File Structure

```
src/nexus/a2a/
├── __init__.py              # Public API: create_a2a_router()
├── models.py                # Pydantic models: AgentCard, Task, Message, Part types,
│                            #   TaskState enum, A2ARequest, A2AResponse,
│                            #   PushNotificationConfig, StreamResponse
├── exceptions.py            # A2AError base, TaskNotFoundError, TaskNotCancelableError,
│                            #   UnsupportedOperationError, etc.
├── agent_card.py            # build_agent_card(config, skills) → AgentCard
│                            #   Imports SkillMetadata from nexus.skills.models
├── task_manager.py          # TaskManager class:
│                            #   - CRUD: create_task, get_task, list_tasks, cancel_task
│                            #   - State machine: transition_state() with validation
│                            #   - Active stream tracking: dict[task_id, asyncio.Queue]
│                            #   - DB operations via SQLAlchemy async
├── router.py                # FastAPI router with A2A JSON-RPC endpoints:
│                            #   GET  /.well-known/agent.json
│                            #   POST /a2a (JSON-RPC dispatch for all A2A methods)
│                            #   - a2a.tasks.send
│                            #   - a2a.tasks.get
│                            #   - a2a.tasks.cancel
│                            #   - a2a.tasks.list
│                            #   - a2a.tasks.sendStreamingMessage (SSE)
│                            #   - a2a.tasks.subscribeToTask (SSE)
│                            #   - a2a.agent.getExtendedAgentCard
│                            #   - a2a.tasks.createPushNotificationConfig
│                            #   - a2a.tasks.getPushNotificationConfig
│                            #   - a2a.tasks.deletePushNotificationConfig
│                            #   Auth: reuse get_auth_result via lazy import
└── db.py                    # SQLAlchemy model for a2a_tasks table + indexes
```

## Implementation Steps

### Step 1: Pydantic Models (`a2a/models.py`)

Define all A2A protocol types as Pydantic BaseModel classes:

- **TaskState** (str enum): `submitted`, `working`, `input_required`, `completed`, `failed`, `canceled`, `rejected`
- **TextPart**: `type="text"`, `text: str`, `metadata: dict | None`
- **FilePart**: `type="file"`, `file: FileContent` (with url/bytes/name/mimeType)
- **DataPart**: `type="data"`, `data: dict`, `metadata: dict | None`
- **Part**: Union discriminated by `type` field
- **Message**: `role: Literal["user", "agent"]`, `parts: list[Part]`, `metadata: dict | None`
- **Artifact**: `artifactId: str`, `parts: list[Part]`, `metadata: dict | None`
- **Task**: `id: str`, `contextId: str | None`, `status: TaskStatus`, `artifacts: list[Artifact]`, `metadata: dict | None`
- **TaskStatus**: `state: TaskState`, `message: Message | None`, `timestamp: datetime`
- **TaskStatusUpdateEvent**: `taskId: str`, `status: TaskStatus`, `final: bool`
- **TaskArtifactUpdateEvent**: `taskId: str`, `artifact: Artifact`
- **StreamResponse**: Union of task/message/statusUpdate/artifactUpdate
- **AgentSkill**: `id: str`, `name: str`, `description: str`, `tags: list[str]`, `examples: list[str] | None`
- **AgentCapabilities**: `streaming: bool`, `pushNotifications: bool`
- **AuthScheme**: `type: str` (apiKey, httpBearer, oauth2), additional fields per type
- **AgentCard**: `name: str`, `description: str`, `url: str`, `version: str`, `capabilities: AgentCapabilities`, `skills: list[AgentSkill]`, `authentication: list[AuthScheme]`, `defaultInputModes: list[str]`, `defaultOutputModes: list[str]`
- **A2ARequest**: `jsonrpc: str = "2.0"`, `method: str`, `params: dict`, `id: str | int`
- **A2AResponse**: `jsonrpc: str = "2.0"`, `result: Any | None`, `error: A2AError | None`, `id: str | int`
- **SendParams**: `message: Message`, `configuration: dict | None`
- **GetParams**: `taskId: str`
- **CancelParams**: `taskId: str`
- **PushNotificationConfig**: `url: str`, `authentication: dict | None`

### Step 2: Exceptions (`a2a/exceptions.py`)

- **A2AError(Exception)**: base with `code: int`, `message: str`, `data: dict | None`
- **TaskNotFoundError(A2AError)**: code -32001
- **TaskNotCancelableError(A2AError)**: code -32002
- **UnsupportedOperationError(A2AError)**: code -32003
- **ContentTypeNotSupportedError(A2AError)**: code -32004
- **InvalidRequestError(A2AError)**: code -32600
- **MethodNotFoundError(A2AError)**: code -32601
- **InternalError(A2AError)**: code -32603

### Step 3: Database Model (`a2a/db.py`)

SQLAlchemy model for `a2a_tasks` table:

```python
class A2ATask(Base):
    __tablename__ = "a2a_tasks"

    id = Column(String, primary_key=True)           # UUID
    context_id = Column(String, nullable=True, index=True)
    zone_id = Column(String, nullable=False, default="default")
    agent_id = Column(String, nullable=True)
    state = Column(String, nullable=False, default="submitted")
    messages = Column(JSON, nullable=False, default=list)  # list[Message]
    artifacts = Column(JSON, nullable=False, default=list) # list[Artifact]
    metadata_ = Column("metadata", JSON, nullable=True)
    push_notification_configs = Column(JSON, nullable=False, default=list)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("ix_a2a_tasks_zone_agent_state", "zone_id", "agent_id", "state"),
        Index("ix_a2a_tasks_created_at", "created_at"),
    )
```

### Step 4: Task Manager (`a2a/task_manager.py`)

Core business logic:

- **State transition validation**: Matrix of valid (from, to) pairs
  - submitted → working, canceled, rejected
  - working → completed, failed, canceled, input_required
  - input_required → working, canceled, failed
  - completed/failed/canceled/rejected → (terminal, no transitions)
- **create_task(message, config, zone_id, agent_id) → Task**: Create task in DB, return Task
- **get_task(task_id, zone_id) → Task**: Fetch from DB with zone isolation
- **list_tasks(zone_id, agent_id, state, limit, offset) → list[Task]**: Filtered query
- **cancel_task(task_id, zone_id) → Task**: Validate cancellable state, transition to canceled
- **update_task_state(task_id, new_state, message) → Task**: Validate transition, update DB, push to active stream
- **add_artifact(task_id, artifact) → Task**: Append artifact, push to active stream
- **Active stream tracking**:
  - `register_stream(task_id) → asyncio.Queue`
  - `unregister_stream(task_id)`
  - `push_event(task_id, event: StreamResponse)`

### Step 5: Agent Card Builder (`a2a/agent_card.py`)

- **build_agent_card(config: NexusConfig, skills: list[SkillMetadata], base_url: str) → AgentCard**
  - Map each SkillMetadata → AgentSkill (name, description, tags)
  - Determine auth schemes from active auth provider config
  - Set capabilities: `streaming=True`, `pushNotifications=False` (Phase 1)
  - Return complete AgentCard

- **Caching**: Module-level `_cached_card: bytes | None` — built once, returned for every request

### Step 6: Router (`a2a/router.py`)

FastAPI router wired into `create_app()`:

```python
router = APIRouter(tags=["a2a"])

@router.get("/.well-known/agent.json")
async def get_agent_card() -> Response:
    """Public endpoint — no auth required."""
    return Response(content=_cached_card_bytes, media_type="application/json")

@router.post("/a2a")
async def a2a_jsonrpc(request: A2ARequest, auth=Depends(require_auth)):
    """JSON-RPC dispatch for all A2A methods."""
    # Dispatch based on request.method:
    #   "a2a.tasks.send" → handle_send()
    #   "a2a.tasks.get" → handle_get()
    #   "a2a.tasks.cancel" → handle_cancel()
    #   "a2a.tasks.list" → handle_list()
    #   "a2a.tasks.sendStreamingMessage" → handle_send_streaming() → SSE
    #   "a2a.tasks.subscribeToTask" → handle_subscribe() → SSE
    #   "a2a.agent.getExtendedAgentCard" → handle_extended_card()
    #   etc.

@router.post("/a2a/stream")
async def a2a_stream(request: A2ARequest, auth=Depends(require_auth)):
    """SSE endpoint for streaming methods."""
    # Returns StreamingResponse with text/event-stream
    # Uses asyncio.Queue for event delivery
    # Heartbeat every 15 seconds
    # Max lifetime 30 minutes (configurable)
    # Cleanup on disconnect
```

### Step 7: Wire into FastAPI Server

In `fastapi_server.py` `create_app()`:

```python
# A2A Protocol Endpoint (Issue #1256)
try:
    from nexus.a2a import create_a2a_router
    a2a_router = create_a2a_router(nexus_fs=nexus_fs, config=config)
    app.include_router(a2a_router)
    logger.info("A2A protocol endpoint registered")
except ImportError as e:
    logger.warning(f"Failed to import A2A router: {e}")
```

Add feature flag to `FeaturesConfig`:
```python
a2a_endpoint: bool = Field(default=True, description="Enable A2A protocol endpoint")
```

### Step 8: Tests

```
tests/unit/a2a/
├── __init__.py
├── test_models.py           # ~40-60 tests: model validation, serialization,
│                            #   Part type discrimination, TaskState enum
├── test_task_manager.py     # ~50-60 tests: exhaustive state transition matrix,
│                            #   CRUD operations, concurrent access, edge cases
├── test_agent_card.py       # ~10 tests: spec conformance, skill mapping,
│                            #   auth scheme detection, caching behavior
└── test_exceptions.py       # ~10 tests: error codes, serialization

tests/integration/a2a/
├── __init__.py
├── test_a2a_endpoints.py    # ~20 tests: JSON-RPC dispatch, auth, error handling,
│                            #   end-to-end task lifecycle via HTTP
└── test_a2a_streaming.py    # ~15-20 tests: SSE event delivery, ordering,
│                            #   disconnect handling, cancel mid-stream, timeout
```

## Implementation Order

1. `a2a/models.py` + `a2a/exceptions.py` — Foundation types (no external deps)
2. `tests/unit/a2a/test_models.py` + `test_exceptions.py` — Validate types
3. `a2a/db.py` — Database model
4. `a2a/task_manager.py` — Business logic
5. `tests/unit/a2a/test_task_manager.py` — State machine tests
6. `a2a/agent_card.py` — Agent Card builder
7. `tests/unit/a2a/test_agent_card.py` — Spec conformance
8. `a2a/router.py` + `a2a/__init__.py` — HTTP layer
9. Wire into `fastapi_server.py` + add feature flag to `config.py`
10. `tests/integration/a2a/test_a2a_endpoints.py` — End-to-end HTTP tests
11. `tests/integration/a2a/test_a2a_streaming.py` — SSE streaming tests

## Key Design Constraints (from Agent OS Deep Research doc)

- A2A is a **protocol surface**, not a kernel component (line 1184)
- A2A tasks bridge to VFS IPC paths **internally** — but for Phase 1, DB-only is sufficient
- Agent Cards populated from Agent Registry + Templar manifests (line 212) — for Phase 1, populated from NexusConfig + Skills
- Nexus provides what A2A lacks: event backbone, shared memory, ReBAC, payments (line 212)
- Three protocol surfaces: VFS (native), MCP (agent↔tools), A2A (agent↔agent) (line 1079)
