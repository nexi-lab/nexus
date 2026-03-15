"""Task Manager REST API endpoints.

Provides endpoints for mission and task management:
- POST   /api/v2/missions              - Create mission
- GET    /api/v2/missions              - List missions
- GET    /api/v2/missions/{id}         - Mission detail + tasks
- PATCH  /api/v2/missions/{id}         - Update mission
- POST   /api/v2/tasks                 - Create task
- GET    /api/v2/tasks                 - List dispatchable tasks
- GET    /api/v2/tasks/{id}            - Task detail + comments + artifacts
- PATCH  /api/v2/tasks/{id}            - Update task status
- GET    /api/v2/tasks/events           - SSE stream of task mutations
- POST   /api/v2/tasks/{id}/audit      - Create audit entry
- GET    /api/v2/tasks/{id}/history    - Unified timeline (audit + comments)
- POST   /api/v2/comments              - Create comment
- GET    /api/v2/comments              - List comments by task_id
- POST   /api/v2/artifacts             - Create artifact
"""

import asyncio
import json
import logging
from collections.abc import AsyncGenerator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from starlette.responses import StreamingResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["task_manager"])

# =============================================================================
# Pydantic Models
# =============================================================================


# -- Mission --


class CreateMissionRequest(BaseModel):
    title: str = Field(..., min_length=1, description="Mission title")
    context_summary: str | None = Field(default=None, description="Optional context")


class UpdateMissionRequest(BaseModel):
    title: str | None = Field(default=None, description="New title")
    status: str | None = Field(
        default=None, description="running|partial_complete|completed|cancelled"
    )
    conclusion: str | None = Field(default=None, description="Conclusion text")
    archived: bool | None = Field(default=None, description="Archive flag")
    context_summary: str | None = Field(default=None, description="Updated context")


class MissionResponse(BaseModel):
    id: str
    title: str
    status: str
    context_summary: str | None = None
    conclusion: str | None = None
    archived: bool = False
    created_at: str
    updated_at: str
    tasks: list[dict[str, Any]] | None = None


class MissionListResponse(BaseModel):
    items: list[MissionResponse]
    total: int
    page: int
    limit: int


# -- Task --


class CreateTaskRequest(BaseModel):
    mission_id: str = Field(..., description="Parent mission ID")
    instruction: str = Field(..., min_length=1, description="Task instruction")
    worker_type: str | None = Field(default=None, description="Worker type")
    input_refs: list[str] | None = Field(default=None, description="Input artifact IDs")
    blocked_by: list[str] | None = Field(default=None, description="Blocking task IDs")
    deadline: str | None = Field(default=None, description="ISO 8601 deadline")
    estimated_duration: int | None = Field(default=None, description="Estimated seconds")
    label: str | None = Field(default=None, description="Optional label")


class UpdateTaskRequest(BaseModel):
    status: str | None = Field(default=None, description="New status")
    output_refs: list[str] | None = Field(default=None, description="Output artifact IDs")


class TaskResponse(BaseModel):
    id: str
    mission_id: str
    instruction: str
    status: str
    worker_type: str | None = None
    input_refs: list[str] = Field(default_factory=list)
    output_refs: list[str] = Field(default_factory=list)
    blocked_by: list[str] = Field(default_factory=list)
    label: str | None = None
    deadline: str | None = None
    estimated_duration: int | None = None
    created_at: str
    started_at: str | None = None
    completed_at: str | None = None
    comments: list[dict[str, Any]] | None = None
    artifacts: list[dict[str, Any]] | None = None
    history: list[dict[str, Any]] | None = None


# -- Comment --


class CreateCommentRequest(BaseModel):
    task_id: str = Field(..., description="Parent task ID")
    author: str = Field(..., description="copilot|worker")
    content: str = Field(..., min_length=1, description="Comment text")
    artifact_refs: list[str] | None = Field(default=None, description="Artifact IDs")


class CommentResponse(BaseModel):
    id: str
    task_id: str
    author: str
    content: str
    artifact_refs: list[str] = Field(default_factory=list)
    created_at: str


# -- Artifact --


class CreateArtifactRequest(BaseModel):
    type: str = Field(
        ..., description="document|code|folder|pr|image|data|spreadsheet|presentation|other"
    )
    uri: str = Field(..., description="Resource URI")
    title: str = Field(..., min_length=1, description="Artifact title")
    mime_type: str | None = Field(default=None, description="MIME type")
    size_bytes: int | None = Field(default=None, description="File size")


class ArtifactResponse(BaseModel):
    id: str
    type: str
    uri: str
    title: str
    mime_type: str | None = None
    size_bytes: int | None = None
    created_at: str


# -- Audit --


class CreateAuditEntryRequest(BaseModel):
    action: str = Field(..., min_length=1, description="Audit action (e.g. task_created)")
    actor: str | None = Field(default=None, description="Actor (e.g. worker, copilot)")
    detail: str | None = Field(default=None, description="Additional detail")


class AuditEntryResponse(BaseModel):
    id: str
    task_id: str
    action: str
    actor: str | None = None
    detail: str | None = None
    created_at: str


# -- History --


class HistoryEntryResponse(BaseModel):
    type: str = Field(..., description="audit|comment")
    id: str
    task_id: str
    created_at: str
    # Audit fields
    action: str | None = None
    actor: str | None = None
    detail: str | None = None
    # Comment fields
    author: str | None = None
    content: str | None = None
    artifact_refs: list[str] = Field(default_factory=list)


# =============================================================================
# Dependencies
# =============================================================================


def _get_require_auth() -> Any:
    """Lazy import to avoid circular imports."""
    from nexus.server.dependencies import require_auth

    return require_auth


def get_task_manager_service(request: Request) -> Any:
    """Get TaskManagerService from app state."""
    service = getattr(request.app.state, "task_manager_service", None)
    if not service:
        raise HTTPException(status_code=503, detail="Task manager service not available")
    return service


# =============================================================================
# SSE via DT_STREAM (task change notifications)
# =============================================================================

_TASK_SSE_STREAM_PATH = "/nexus/streams/task-events"


def _get_stream_manager(request: Request) -> Any:
    """Get StreamManager from app state (optional — SSE degrades gracefully)."""
    return getattr(request.app.state, "task_stream_manager", None)


def _notify_stream(request: Request, event: str, task_id: str) -> None:
    """Write a task event notification to the DT_STREAM (best-effort)."""
    sm = _get_stream_manager(request)
    if sm is None:
        return
    try:
        data = json.dumps({"event": event, "task_id": task_id}).encode()
        sm.stream_write_nowait(_TASK_SSE_STREAM_PATH, data)
    except Exception:
        logger.debug("[TASK-SSE] stream write failed (non-fatal)")


@router.get("/api/v2/tasks/events")
async def task_events(request: Request) -> StreamingResponse:
    """SSE stream of task mutation notifications via DT_STREAM.

    Each SSE client maintains its own byte offset into the stream,
    providing true fan-out without destructive reads (unlike DT_PIPE).
    """
    sm = _get_stream_manager(request)

    async def _stream() -> AsyncGenerator[str, None]:
        if sm is None:
            yield ": stream manager not available\n\n"
            return

        offset = 0
        while True:
            if await request.is_disconnected():
                break
            try:
                data, next_offset = sm.stream_read_at(_TASK_SSE_STREAM_PATH, offset)
                offset = next_offset
                yield f"data: {data.decode()}\n\n"
            except Exception:
                # StreamEmptyError or StreamNotFoundError — wait and send keepalive
                await asyncio.sleep(25)
                yield ": keepalive\n\n"

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# =============================================================================
# Mission endpoints
# =============================================================================


@router.post("/api/v2/missions", response_model=MissionResponse, status_code=201)
async def create_mission(
    request: CreateMissionRequest,
    auth_result: dict[str, Any] = Depends(_get_require_auth()),  # noqa: ARG001
    svc: Any = Depends(get_task_manager_service),
) -> MissionResponse:
    """Create a new mission."""
    doc = svc.create_mission(
        title=request.title,
        context_summary=request.context_summary,
    )
    return MissionResponse(**doc)


@router.get("/api/v2/missions", response_model=MissionListResponse)
async def list_missions(
    status: str | None = Query(default=None),
    archived: bool = Query(default=False),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=20, ge=1, le=100),
    auth_result: dict[str, Any] = Depends(_get_require_auth()),  # noqa: ARG001
    svc: Any = Depends(get_task_manager_service),
) -> MissionListResponse:
    """List missions with optional filters."""
    result = svc.list_missions(archived=archived, status=status, page=page, limit=limit)
    return MissionListResponse(**result)


@router.get("/api/v2/missions/{mission_id}", response_model=MissionResponse)
async def get_mission(
    mission_id: str,
    auth_result: dict[str, Any] = Depends(_get_require_auth()),  # noqa: ARG001
    svc: Any = Depends(get_task_manager_service),
) -> MissionResponse:
    """Get mission detail with task list."""
    from nexus.bricks.task_manager.service import NotFoundError

    try:
        doc = svc.get_mission(mission_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return MissionResponse(**doc)


@router.patch("/api/v2/missions/{mission_id}", response_model=MissionResponse)
async def update_mission(
    mission_id: str,
    request: UpdateMissionRequest,
    auth_result: dict[str, Any] = Depends(_get_require_auth()),  # noqa: ARG001
    svc: Any = Depends(get_task_manager_service),
) -> MissionResponse:
    """Update mission fields."""
    from nexus.bricks.task_manager.service import NotFoundError, ValidationError

    fields = request.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")

    try:
        doc = svc.update_mission(mission_id, **fields)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return MissionResponse(**doc)


# =============================================================================
# Task endpoints
# =============================================================================


@router.post("/api/v2/tasks", response_model=TaskResponse, status_code=201)
async def create_task(
    raw_request: Request,
    body: CreateTaskRequest,
    auth_result: dict[str, Any] = Depends(_get_require_auth()),  # noqa: ARG001
    svc: Any = Depends(get_task_manager_service),
) -> TaskResponse:
    """Create a new task within a mission."""
    from nexus.bricks.task_manager.service import NotFoundError

    try:
        doc = svc.create_task(
            mission_id=body.mission_id,
            instruction=body.instruction,
            worker_type=body.worker_type,
            input_refs=body.input_refs,
            blocked_by=body.blocked_by,
            deadline=body.deadline,
            estimated_duration=body.estimated_duration,
            label=body.label,
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    _notify_stream(raw_request, "task_created", doc["id"])
    return TaskResponse(**doc)


@router.get("/api/v2/tasks", response_model=list[TaskResponse])
async def list_tasks(
    worker_type: str | None = Query(default=None),
    auth_result: dict[str, Any] = Depends(_get_require_auth()),  # noqa: ARG001
    svc: Any = Depends(get_task_manager_service),
) -> list[TaskResponse]:
    """List dispatchable tasks (status=created, unblocked)."""
    tasks = svc.list_dispatchable_tasks(worker_type=worker_type)
    return [TaskResponse(**t) for t in tasks]


@router.get("/api/v2/tasks/{task_id}", response_model=TaskResponse)
async def get_task_detail(
    task_id: str,
    auth_result: dict[str, Any] = Depends(_get_require_auth()),  # noqa: ARG001
    svc: Any = Depends(get_task_manager_service),
) -> TaskResponse:
    """Get task detail with comments and artifacts."""
    from nexus.bricks.task_manager.service import NotFoundError

    try:
        doc = svc.get_task_detail(task_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return TaskResponse(**doc)


@router.patch("/api/v2/tasks/{task_id}", response_model=TaskResponse)
async def update_task(
    raw_request: Request,
    task_id: str,
    body: UpdateTaskRequest,
    auth_result: dict[str, Any] = Depends(_get_require_auth()),  # noqa: ARG001
    svc: Any = Depends(get_task_manager_service),
) -> TaskResponse:
    """Update task status and/or output_refs."""
    from nexus.bricks.task_manager.service import NotFoundError, ValidationError

    fields: dict[str, Any] = {}
    if body.status is not None:
        fields["status"] = body.status
    if body.output_refs is not None:
        fields["output_refs"] = body.output_refs
    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")

    try:
        doc = svc.update_task(task_id, **fields)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _notify_stream(raw_request, "task_updated", task_id)
    return TaskResponse(**doc)


# =============================================================================
# Audit / History endpoints
# =============================================================================


@router.post("/api/v2/tasks/{task_id}/audit", response_model=AuditEntryResponse, status_code=201)
async def create_audit_entry(
    raw_request: Request,
    task_id: str,
    body: CreateAuditEntryRequest,
    auth_result: dict[str, Any] = Depends(_get_require_auth()),  # noqa: ARG001
    svc: Any = Depends(get_task_manager_service),
) -> AuditEntryResponse:
    """Create an audit trail entry for a task."""
    from nexus.bricks.task_manager.service import NotFoundError

    try:
        doc = svc.create_audit_entry(
            task_id,
            body.action,
            actor=body.actor,
            detail=body.detail,
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    _notify_stream(raw_request, "audit_created", task_id)
    return AuditEntryResponse(**doc)


@router.get("/api/v2/tasks/{task_id}/history", response_model=list[HistoryEntryResponse])
async def get_task_history(
    task_id: str,
    auth_result: dict[str, Any] = Depends(_get_require_auth()),  # noqa: ARG001
    svc: Any = Depends(get_task_manager_service),
) -> list[HistoryEntryResponse]:
    """Get unified timeline of audit entries and comments for a task."""
    from nexus.bricks.task_manager.service import NotFoundError

    try:
        history = svc.get_task_history(task_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return [HistoryEntryResponse(**h) for h in history]


# =============================================================================
# Comment endpoints
# =============================================================================


@router.post("/api/v2/comments", response_model=CommentResponse, status_code=201)
async def create_comment(
    raw_request: Request,
    body: CreateCommentRequest,
    auth_result: dict[str, Any] = Depends(_get_require_auth()),  # noqa: ARG001
    svc: Any = Depends(get_task_manager_service),
) -> CommentResponse:
    """Create a comment on a task."""
    from nexus.bricks.task_manager.service import NotFoundError, ValidationError

    try:
        doc = svc.create_comment(
            task_id=body.task_id,
            author=body.author,
            content=body.content,
            artifact_refs=body.artifact_refs,
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _notify_stream(raw_request, "comment_created", body.task_id)
    return CommentResponse(**doc)


@router.get("/api/v2/comments", response_model=list[CommentResponse])
async def list_comments(
    task_id: str = Query(..., description="Task ID to list comments for"),
    auth_result: dict[str, Any] = Depends(_get_require_auth()),  # noqa: ARG001
    svc: Any = Depends(get_task_manager_service),
) -> list[CommentResponse]:
    """List comments for a task."""
    comments = svc.get_comments(task_id)
    return [CommentResponse(**c) for c in comments]


# =============================================================================
# Artifact endpoints
# =============================================================================


@router.post("/api/v2/artifacts", response_model=ArtifactResponse, status_code=201)
async def create_artifact(
    request: CreateArtifactRequest,
    auth_result: dict[str, Any] = Depends(_get_require_auth()),  # noqa: ARG001
    svc: Any = Depends(get_task_manager_service),
) -> ArtifactResponse:
    """Create an artifact reference."""
    from nexus.bricks.task_manager.service import ValidationError

    try:
        doc = svc.create_artifact(
            type=request.type,
            uri=request.uri,
            title=request.title,
            mime_type=request.mime_type,
            size_bytes=request.size_bytes,
        )
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ArtifactResponse(**doc)
