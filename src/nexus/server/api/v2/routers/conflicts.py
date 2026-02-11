"""Conflict management REST API endpoints (Issue #1130).

Provides 3 endpoints for conflict audit and manual resolution:
- GET  /api/v2/sync/conflicts          - List conflicts (paginated)
- GET  /api/v2/sync/conflicts/{id}     - Get conflict by ID
- POST /api/v2/sync/conflicts/{id}/resolve - Manually resolve conflict
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from nexus.server.api.v2.dependencies import (
    _get_require_auth,
    get_conflict_log_store,
)
from nexus.server.api.v2.models import (
    ConflictDetailResponse,
    ConflictListResponse,
    ConflictResolveRequest,
    ConflictResolveResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/sync/conflicts", tags=["sync-conflicts"])


def _record_to_response(record: Any) -> ConflictDetailResponse:
    """Convert a ConflictRecord to API response model."""
    return ConflictDetailResponse(
        conflict_id=record.id,
        path=record.path,
        backend_name=record.backend_name,
        zone_id=record.zone_id,
        strategy=str(record.strategy),
        outcome=str(record.outcome),
        nexus_content_hash=record.nexus_content_hash,
        nexus_mtime=record.nexus_mtime.isoformat() if record.nexus_mtime else None,
        nexus_size=record.nexus_size,
        backend_content_hash=record.backend_content_hash,
        backend_mtime=record.backend_mtime.isoformat() if record.backend_mtime else None,
        backend_size=record.backend_size,
        conflict_copy_path=record.conflict_copy_path,
        status=record.status,
        resolved_at=record.resolved_at.isoformat() if record.resolved_at else None,
    )


@router.get("", response_model=ConflictListResponse)
async def list_conflicts(
    status: str | None = Query(None, description="Filter by status"),
    backend_name: str | None = Query(None, description="Filter by backend name"),
    zone_id: str | None = Query(None, description="Filter by zone ID"),
    limit: int = Query(50, ge=1, le=200, description="Max results"),
    offset: int = Query(0, ge=0, description="Number to skip"),
    _auth_result: dict[str, Any] = Depends(_get_require_auth()),
    store: Any = Depends(get_conflict_log_store),
) -> ConflictListResponse:
    """List conflict records with optional filtering and pagination."""
    try:
        records = store.list_conflicts(
            status=status,
            backend_name=backend_name,
            zone_id=zone_id,
            limit=limit,
            offset=offset,
        )
        return ConflictListResponse(
            conflicts=[_record_to_response(r) for r in records],
            total=len(records),
        )
    except Exception as e:
        logger.error(f"List conflicts error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to list conflicts") from e


@router.get("/{conflict_id}", response_model=ConflictDetailResponse)
async def get_conflict(
    conflict_id: str,
    _auth_result: dict[str, Any] = Depends(_get_require_auth()),
    store: Any = Depends(get_conflict_log_store),
) -> ConflictDetailResponse:
    """Get a conflict record by ID."""
    try:
        record = store.get_conflict(conflict_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"Conflict {conflict_id} not found")
        return _record_to_response(record)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Get conflict error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to get conflict") from e


@router.post("/{conflict_id}/resolve", response_model=ConflictResolveResponse)
async def resolve_conflict(
    conflict_id: str,
    request: ConflictResolveRequest,
    _auth_result: dict[str, Any] = Depends(_get_require_auth()),
    store: Any = Depends(get_conflict_log_store),
) -> ConflictResolveResponse:
    """Manually resolve a pending conflict."""
    from nexus.services.conflict_resolution import ResolutionOutcome

    try:
        outcome = ResolutionOutcome(request.outcome)
    except ValueError as err:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid outcome: {request.outcome}. Must be 'nexus_wins' or 'backend_wins'",
        ) from err

    try:
        updated = store.resolve_conflict_manually(conflict_id, outcome)
        if not updated:
            raise HTTPException(
                status_code=404,
                detail=f"Conflict {conflict_id} not found or not in manual_pending status",
            )
        return ConflictResolveResponse(
            conflict_id=conflict_id,
            status="manually_resolved",
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Resolve conflict error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to resolve conflict") from e
