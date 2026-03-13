"""Operation replay and reindex REST API endpoints (Issue #2930).

Provides endpoints for MCL replay and index rebuilding:
- GET /api/v2/ops/replay -- Cursor-based MCL replay
- POST /api/v2/admin/reindex -- Trigger index rebuild
"""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from nexus.server.api.v2.dependencies import get_auth_result, get_operation_logger
from nexus.server.api.v2.models.aspects import (
    ReindexRequest,
    ReindexResponse,
    ReplayResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["operations"])


@router.get("/api/v2/ops/replay")
async def replay_changes(
    from_sequence: int = Query(0, ge=0, description="Start from sequence number (inclusive)"),
    limit: int = Query(50, ge=1, le=500, description="Max records to return"),
    entity_urn: str | None = Query(None, description="Filter by entity URN"),
    aspect_name: str | None = Query(None, description="Filter by aspect name"),
    logger_and_zone: tuple[Any, str] = Depends(get_operation_logger),
) -> ReplayResponse:
    """Replay MCL records with cursor-based pagination.

    Returns operation_log rows that carry MCL semantics (entity_urn IS NOT NULL),
    ordered by sequence_number ascending.
    """
    op_logger, zone_id = logger_and_zone
    try:
        records: list[dict[str, Any]] = []
        count = 0
        last_seq = from_sequence

        for row in op_logger.replay_changes(
            from_sequence=from_sequence,
            zone_id=zone_id,
            batch_size=limit + 1,
        ):
            # Apply optional filters
            if entity_urn and getattr(row, "entity_urn", "") != entity_urn:
                continue
            if aspect_name and getattr(row, "aspect_name", "") != aspect_name:
                continue

            if count >= limit:
                break

            records.append(
                {
                    "sequence_number": getattr(row, "sequence_number", 0),
                    "entity_urn": getattr(row, "entity_urn", ""),
                    "aspect_name": getattr(row, "aspect_name", ""),
                    "change_type": getattr(row, "change_type", ""),
                    "timestamp": row.created_at.isoformat() if row.created_at else "",
                    "operation_type": row.operation_type,
                }
            )
            count += 1
            last_seq = getattr(row, "sequence_number", 0)

        has_more = count >= limit
        return ReplayResponse(
            records=records,
            next_cursor=last_seq + 1 if has_more else None,
            has_more=has_more,
        )

    except Exception as e:
        logger.error("replay_changes error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to replay changes") from e


@router.post("/api/v2/admin/reindex")
async def trigger_reindex(
    body: ReindexRequest,
    logger_and_zone: tuple[Any, str] = Depends(get_operation_logger),
    auth_result: dict[str, Any] = Depends(get_auth_result),
) -> ReindexResponse:
    """Trigger an index rebuild from MCL records.

    Replays operation_log MCL entries to rebuild aspect store state.
    Use dry_run=true to see what would be processed without making changes.
    Requires admin privileges.
    """
    if not auth_result.get("is_admin"):
        raise HTTPException(status_code=403, detail="Admin access required for reindex")

    op_logger, zone_id = logger_and_zone
    effective_zone = zone_id  # Always use authenticated user's zone — no cross-zone escalation

    # Semantic reindex requires local filesystem walk — not available via REST API
    if body.target == "semantic":
        raise HTTPException(
            status_code=501,
            detail="Semantic reindex requires local filesystem access. "
            "Use 'nexus reindex --target semantic' from the CLI with a local RecordStore.",
        )
    # For "all" via REST, _MCLProcessor runs search+versions; semantic
    # requires local filesystem walk and is not available remotely.

    try:
        from sqlalchemy import func, select

        from nexus.storage.models.operation_log import OperationLogModel

        # Count MCL records
        session = op_logger.session
        count_stmt = (
            select(func.count())
            .select_from(OperationLogModel)
            .where(OperationLogModel.entity_urn.isnot(None))
        )
        if body.from_sequence is not None:
            count_stmt = count_stmt.where(OperationLogModel.sequence_number >= body.from_sequence)
        if effective_zone:
            count_stmt = count_stmt.where(OperationLogModel.zone_id == effective_zone)

        total = session.execute(count_stmt).scalar_one()

        if body.dry_run:
            return ReindexResponse(
                target=body.target,
                total=total,
                dry_run=True,
            )

        # Run reindex
        from nexus.cli.commands.reindex import _MCLProcessor

        processor = _MCLProcessor(session, body.target)
        processed = 0
        errors = 0
        last_sequence = body.from_sequence or 0

        for row in op_logger.replay_changes(
            from_sequence=body.from_sequence or 0,
            zone_id=effective_zone,
            batch_size=body.batch_size,
        ):
            try:
                processor.process(row)
                processed += 1
                last_sequence = row.sequence_number
            except Exception as e:
                errors += 1
                logger.warning("Reindex error at seq %d: %s", row.sequence_number, e)

        session.commit()

        return ReindexResponse(
            target=body.target,
            total=total,
            processed=processed,
            errors=errors,
            last_sequence=last_sequence,
        )

    except Exception as e:
        logger.error("reindex error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to run reindex") from e
