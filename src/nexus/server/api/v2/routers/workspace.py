"""Workspace registry REST API endpoints (Issue #2987).

Provides 5 endpoints for workspace directory registration CRUD:

- GET    /api/v2/registry/workspaces              — List caller's workspaces
- GET    /api/v2/registry/workspaces/{path:path}   — Get workspace by path
- POST   /api/v2/registry/workspaces              — Register a workspace
- PATCH  /api/v2/registry/workspaces/{path:path}   — Update workspace
- DELETE /api/v2/registry/workspaces/{path:path}   — Unregister workspace

All endpoints require authentication and are scoped to the caller's
user_id (derived from OperationContext). Uses sync `def` endpoints
because WorkspaceRegistry uses synchronous SQLAlchemy sessions —
FastAPI auto-runs sync endpoints in a threadpool.
"""

import json
import logging
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from nexus.server.api.v2.dependencies import (
    _get_operation_context,
    _get_require_auth,
    get_workspace_registry,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class WorkspaceRegisterRequest(BaseModel):
    """Request body for registering a workspace."""

    path: str = Field(..., min_length=1, description="Absolute path to workspace directory")
    name: str | None = Field(None, max_length=255, description="Optional friendly name")
    description: str = Field("", max_length=2000, description="Human-readable description")
    metadata: dict[str, Any] = Field(default_factory=dict, description="User-defined metadata")
    session_id: str | None = Field(
        None, max_length=36, description="Session ID for session-scoped workspace"
    )
    ttl_seconds: int | None = Field(
        None, ge=60, le=604800, description="TTL in seconds (60s to 7d)"
    )
    overlay: bool = Field(False, description="Enable overlay for this workspace")
    base_snapshot_hash: str | None = Field(None, description="Base manifest CAS hash for overlay")

    @field_validator("path")
    @classmethod
    def _path_must_be_absolute(cls, v: str) -> str:
        if not v.startswith("/"):
            raise ValueError("path must be absolute (start with '/')")
        return v


class ResourceUpdateRequest(BaseModel):
    """Request body for updating a workspace."""

    name: str | None = Field(None, max_length=255, description="New friendly name")
    description: str | None = Field(None, max_length=2000, description="New description")
    metadata: dict[str, Any] | None = Field(None, description="New metadata (replaces existing)")


class WorkspaceResponse(BaseModel):
    """Response model for a single workspace."""

    path: str
    name: str | None
    description: str
    created_at: str | None
    created_by: str | None
    user_id: str | None
    agent_id: str | None
    scope: str
    session_id: str | None
    expires_at: str | None
    metadata: dict[str, Any]


class WorkspaceListResponse(BaseModel):
    """Response model for listing workspaces."""

    items: list[WorkspaceResponse]
    count: int


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

workspace_router = APIRouter(prefix="/api/v2/registry/workspaces", tags=["registry"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalize_path(path: str) -> str:
    """Ensure path has a leading slash (FastAPI strips it from {path:path})."""
    return path if path.startswith("/") else "/" + path


def _datetime_to_str(dt: Any) -> str | None:
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return dt.isoformat()
    return str(dt)


def _get_caller_user_id(auth_result: dict[str, Any]) -> str | None:
    """Extract user_id from auth result for ownership scoping."""
    context = _get_operation_context(auth_result)
    return getattr(context, "user_id", None)


def _build_workspace_response(db_model: Any) -> WorkspaceResponse:
    """Build a WorkspaceResponse from a DB model (WorkspaceConfigModel)."""
    metadata_dict = json.loads(db_model.extra_metadata) if db_model.extra_metadata else {}
    return WorkspaceResponse(
        path=db_model.path,
        name=db_model.name,
        description=db_model.description or "",
        created_at=_datetime_to_str(db_model.created_at),
        created_by=db_model.created_by,
        user_id=db_model.user_id,
        agent_id=db_model.agent_id,
        scope=db_model.scope or "persistent",
        session_id=db_model.session_id,
        expires_at=_datetime_to_str(db_model.expires_at),
        metadata=metadata_dict,
    )


def _get_workspace_db_model(registry: Any, path: str, *, user_id: str | None = None) -> Any:
    """Fetch WorkspaceConfigModel from DB by path, optionally filtered by user_id."""
    from sqlalchemy import select

    from nexus.storage.models import WorkspaceConfigModel

    with registry.metadata_session_factory() as session:
        stmt = select(WorkspaceConfigModel).filter_by(path=path)
        if user_id is not None:
            stmt = stmt.filter(
                (WorkspaceConfigModel.user_id == user_id)
                | (WorkspaceConfigModel.user_id.is_(None))
            )
        return session.execute(stmt).scalars().first()


def _list_workspace_db_models(registry: Any, *, user_id: str | None = None) -> list[Any]:
    """Fetch WorkspaceConfigModel rows from DB, filtered by user_id."""
    from sqlalchemy import select

    from nexus.storage.models import WorkspaceConfigModel

    with registry.metadata_session_factory() as session:
        stmt = select(WorkspaceConfigModel)
        if user_id is not None:
            stmt = stmt.filter(
                (WorkspaceConfigModel.user_id == user_id)
                | (WorkspaceConfigModel.user_id.is_(None))
            )
        return list(session.execute(stmt).scalars().all())


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@workspace_router.get("", response_model=WorkspaceListResponse)
def list_workspaces(
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
    registry: Any = Depends(get_workspace_registry),
) -> WorkspaceListResponse:
    """List workspaces visible to the authenticated caller."""
    try:
        user_id = _get_caller_user_id(auth_result)
        db_models = _list_workspace_db_models(registry, user_id=user_id)
        items = [_build_workspace_response(m) for m in db_models]
        return WorkspaceListResponse(items=items, count=len(items))
    except Exception as e:
        logger.error("Failed to list workspaces: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to list workspaces") from e


@workspace_router.post("", response_model=WorkspaceResponse, status_code=status.HTTP_201_CREATED)
def register_workspace(
    request: WorkspaceRegisterRequest,
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
    registry: Any = Depends(get_workspace_registry),
) -> WorkspaceResponse:
    """Register a directory as a workspace."""
    try:
        context = _get_operation_context(auth_result)
        ttl = timedelta(seconds=request.ttl_seconds) if request.ttl_seconds else None

        registry.register_workspace(
            path=request.path,
            name=request.name,
            description=request.description,
            metadata=request.metadata,
            context=context,
            session_id=request.session_id,
            ttl=ttl,
            overlay=request.overlay,
            base_snapshot_hash=request.base_snapshot_hash,
        )

        db_model = _get_workspace_db_model(registry, request.path)
        if db_model is None:
            raise HTTPException(status_code=500, detail="Workspace registered but not found in DB")
        return _build_workspace_response(db_model)

    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to register workspace: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to register workspace") from e


@workspace_router.get("/{path:path}", response_model=WorkspaceResponse)
def get_workspace(
    path: str,
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
    registry: Any = Depends(get_workspace_registry),
) -> WorkspaceResponse:
    """Get workspace configuration by path (ownership-scoped)."""
    path = _normalize_path(path)
    try:
        user_id = _get_caller_user_id(auth_result)
        db_model = _get_workspace_db_model(registry, path, user_id=user_id)
        if db_model is None:
            raise HTTPException(status_code=404, detail=f"Workspace not found: {path}")
        return _build_workspace_response(db_model)

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to get workspace %s: %s", path, e, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to get workspace") from e


@workspace_router.patch("/{path:path}", response_model=WorkspaceResponse)
def update_workspace(
    path: str,
    request: ResourceUpdateRequest,
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
    registry: Any = Depends(get_workspace_registry),
) -> WorkspaceResponse:
    """Update workspace name, description, or metadata (ownership-scoped)."""
    path = _normalize_path(path)
    try:
        user_id = _get_caller_user_id(auth_result)
        db_model = _get_workspace_db_model(registry, path, user_id=user_id)
        if db_model is None:
            raise HTTPException(status_code=404, detail=f"Workspace not found: {path}")

        registry.update_workspace(
            path=path,
            name=request.name,
            description=request.description,
            metadata=request.metadata,
        )

        db_model = _get_workspace_db_model(registry, path)
        if db_model is None:
            raise HTTPException(status_code=500, detail="Workspace updated but not found in DB")
        return _build_workspace_response(db_model)

    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to update workspace %s: %s", path, e, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to update workspace") from e


@workspace_router.delete("/{path:path}")
def unregister_workspace(
    path: str,
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
    registry: Any = Depends(get_workspace_registry),
) -> dict[str, Any]:
    """Unregister a workspace (ownership-scoped, does NOT delete files)."""
    path = _normalize_path(path)
    try:
        user_id = _get_caller_user_id(auth_result)
        db_model = _get_workspace_db_model(registry, path, user_id=user_id)
        if db_model is None:
            raise HTTPException(status_code=404, detail=f"Workspace not found: {path}")

        deleted = registry.unregister_workspace(path)
        if not deleted:
            raise HTTPException(status_code=404, detail=f"Workspace not found: {path}")
        return {"unregistered": True, "path": path}

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to unregister workspace %s: %s", path, e, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to unregister workspace") from e
