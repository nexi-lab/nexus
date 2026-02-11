"""Operations REST API endpoint (Event Replay).

Provides a single endpoint for querying filesystem operation history:
- GET /api/v2/operations — List operations with offset or cursor pagination

Supports filtering by agent_id, operation_type, path_pattern, status,
and time range (since/until). All results are scoped to the authenticated
user's zone_id.

Issue #1197: Add Event Replay API for Agent Mesh support.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from nexus.server.api.v2.dependencies import get_operation_logger
from nexus.server.api.v2.models import (
    OperationListResponse,
    OperationResponse,
)
from nexus.storage.operation_logger import OperationLogger

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/operations", tags=["operations"])


def _to_operation_response(op: Any) -> OperationResponse:
    """Convert an OperationLogModel to an OperationResponse."""
    metadata = None
    if op.metadata_snapshot:
        try:
            metadata = json.loads(op.metadata_snapshot)
        except (json.JSONDecodeError, TypeError):
            metadata = None

    return OperationResponse(
        id=op.operation_id,
        agent_id=op.agent_id,
        operation_type=op.operation_type,
        path=op.path,
        new_path=op.new_path,
        status=op.status,
        timestamp=op.created_at.isoformat() if op.created_at else "",
        metadata=metadata,
    )


@router.get("")
async def list_operations(
    since: datetime | None = Query(None, description="Operations after this time (ISO-8601)"),
    until: datetime | None = Query(None, description="Operations before this time (ISO-8601)"),
    agent_id: str | None = Query(None, description="Filter by agent ID"),
    operation_type: str | None = Query(None, description="Filter by operation type"),
    path_pattern: str | None = Query(None, description="Wildcard path filter (* supported)"),
    status: str | None = Query(None, description="Filter by status (success/failure)"),
    limit: int = Query(100, ge=1, le=1000, description="Maximum results"),
    offset: int = Query(0, ge=0, description="Offset for pagination (offset mode)"),
    cursor: str | None = Query(None, description="Cursor from previous response (cursor mode)"),
    include_total: bool = Query(
        False, description="Include exact total count (adds a COUNT query; off by default)"
    ),
    logger_and_zone: tuple[OperationLogger, str] = Depends(get_operation_logger),
) -> OperationListResponse:
    """List operations with optional filters.

    Supports two pagination modes:
    - **Offset mode** (default): uses LIMIT+1 to detect has_more. Pass
      include_total=true for an exact COUNT (slower on large datasets).
    - **Cursor mode**: pass cursor param from previous response's next_cursor.
    """
    op_logger, zone_id = logger_and_zone

    filter_kwargs: dict[str, Any] = {
        "zone_id": zone_id,
        "agent_id": agent_id,
        "operation_type": operation_type,
        "status": status,
        "since": since,
        "until": until,
        "path_pattern": path_pattern,
    }

    try:
        if cursor is not None:
            # Cursor mode (already uses LIMIT+1 internally)
            operations, next_cursor = op_logger.list_operations_cursor(
                **filter_kwargs,
                limit=limit,
                cursor=cursor,
            )
            return OperationListResponse(
                operations=[_to_operation_response(op) for op in operations],
                limit=limit,
                has_more=next_cursor is not None,
                next_cursor=next_cursor,
            )

        # Offset mode — fetch limit+1 to detect has_more without COUNT
        operations = op_logger.list_operations(
            **filter_kwargs,
            limit=limit + 1,
            offset=offset,
        )

        has_more = len(operations) > limit
        if has_more:
            operations = operations[:limit]

        # Only run COUNT when explicitly requested
        total = op_logger.count_operations(**filter_kwargs) if include_total else None

        return OperationListResponse(
            operations=[_to_operation_response(op) for op in operations],
            offset=offset,
            limit=limit,
            has_more=has_more,
            total=total,
        )

    except Exception as e:
        logger.error("Operations query error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to query operations") from e
