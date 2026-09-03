"""Operation replay and reindex REST API endpoints (Issue #2930).

Provides endpoints for MCL replay and index rebuilding:
- GET /api/v2/ops/replay -- Cursor-based MCL replay
- POST /api/v2/admin/reindex -- Trigger index rebuild
- POST /api/v2/admin/reconcile-projections -- Repair file_paths / version_history
  / operation_log from the kernel (#4738)
"""

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.exceptions import NexusFileNotFoundError
from nexus.server.api.v2.dependencies import get_auth_result, get_operation_logger
from nexus.server.api.v2.models.aspects import (
    ReconcileProjectionsRequest,
    ReconcileProjectionsResponse,
    ReindexRequest,
    ReindexResponse,
    ReplayResponse,
)
from nexus.server.api.v2.routers._index_on_write import (
    ReindexSearchOutcome,
    index_zone_for,
    reindex_paths,
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
        # Fetch limit+1 to detect has_more via true lookahead
        records: list[dict[str, Any]] = []

        for row in op_logger.replay_changes(
            from_sequence=from_sequence,
            zone_id=zone_id,
            batch_size=limit + 2,  # over-fetch for lookahead after filtering
        ):
            if entity_urn and getattr(row, "entity_urn", "") != entity_urn:
                continue
            if aspect_name and getattr(row, "aspect_name", "") != aspect_name:
                continue

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

            if len(records) > limit:
                break  # Got one extra — proves there's more

        has_more = len(records) > limit
        if has_more:
            records = records[:limit]

        last_seq = records[-1]["sequence_number"] if records else from_sequence
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
    request: Request,
    body: ReindexRequest,
    logger_and_zone: tuple[Any, str] = Depends(get_operation_logger),
    auth_result: dict[str, Any] = Depends(get_auth_result),
) -> ReindexResponse:
    """Trigger an index rebuild from MCL records.

    Replays operation_log MCL entries to rebuild aspect store state, and
    — for ``target`` in ``{"all", "search"}`` — indexes every processed
    path into the search plugin SYNCHRONOUSLY (#4241 / #4736): deleted
    paths are evicted, the rest are read through the VFS and indexed in
    bounded batches (files over 2 MiB or non-UTF-8 are reported
    ``skipped``).  The response says per path what happened —
    ``search_paths_indexed`` / ``_deleted`` / ``_skipped`` /
    ``search_index_errors`` — and carries the plugin's ``search_index_seq``
    so ``/search/stats`` ``last_index_seq`` can confirm it is served.
    Nothing is "enqueued": there is no queue.

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
        # Issue #4241: track distinct paths so the search plugin can be
        # driven after the aspect-store rebuild. Preserve change_type so
        # deletes propagate as evictions (not re-indexes).
        paths_seen: dict[str, str] = {}

        for row in op_logger.replay_changes(
            from_sequence=body.from_sequence or 0,
            zone_id=effective_zone,
            batch_size=body.batch_size,
        ):
            try:
                processor.process(row)
                processed += 1
                last_sequence = row.sequence_number
                row_path = getattr(row, "path", None)
                # Only VFS paths are search documents.  Aspect rows
                # (schema_metadata / lineage / governance.*) carry the
                # entity URN in ``path`` — aspect-store work, nothing to
                # index — so they must not count as search failures.
                if isinstance(row_path, str) and row_path.startswith("/"):
                    row_change = getattr(row, "change_type", "") or ""
                    paths_seen[row_path] = "delete" if row_change == "delete" else "update"
            except Exception as e:
                errors += 1
                logger.warning("Reindex error at seq %d: %s", row.sequence_number, e)

        session.commit()

        # #4241 / #4736: post-P12 the plugin indexes synchronously and
        # NotifyFileChange for create / update is a no-op ack, so the old
        # "enqueue a refresh" step never indexed anything while reporting
        # queue counters.  Drive the plugin directly and report per path.
        search = ReindexSearchOutcome()
        if body.target in ("all", "search") and paths_seen:
            search_daemon = getattr(request.app.state, "search_daemon", None)
            nexus_fs = getattr(request.app.state, "nexus_fs", None)
            if search_daemon is None or nexus_fs is None:
                missing = "search_daemon" if search_daemon is None else "nexus_fs"
                logger.warning(
                    "Search reindex requested but no %s on app.state — %d path(s) NOT indexed.",
                    missing,
                    len(paths_seen),
                )
                search = ReindexSearchOutcome.unavailable(
                    paths_seen,
                    f"search unavailable: no {missing} on app.state",
                )
            else:
                from nexus.server.dependencies import get_operation_context

                search = await reindex_paths(
                    search_daemon,
                    nexus_fs,
                    get_operation_context(auth_result),
                    paths_seen,
                    # Same zone /search/query reads and every other
                    # indexing surface writes.
                    zone_id=index_zone_for(auth_result),
                )

        return ReindexResponse(
            target=body.target,
            total=total,
            processed=processed,
            errors=errors,
            last_sequence=last_sequence,
            search_paths_indexed=search.indexed,
            search_paths_deleted=search.deleted,
            search_paths_skipped=search.skipped,
            search_skip_reasons=search.skip_reasons,
            search_skipped_paths=search.skipped_paths,
            search_index_errors=search.errors,
            search_index_failed_paths=search.failed_paths,
            search_index_error=search.first_error,
            search_index_seq=search.index_seq,
            search_indexed_at=search.completed_at,
        )

    except Exception as e:
        logger.error("reindex error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to run reindex") from e


@router.post("/api/v2/admin/reconcile-projections", response_model=ReconcileProjectionsResponse)
async def reconcile_projections(
    request: Request,
    body: ReconcileProjectionsRequest,
    auth_result: dict[str, Any] = Depends(get_auth_result),
) -> ReconcileProjectionsResponse:
    """Repair the RecordStore projection from the kernel (#4738).

    The write observer commits ``file_paths`` / ``version_history`` /
    ``operation_log`` before a write returns, but the kernel commit lands
    first: a crash between the two, a RecordStore outage that outlived the
    observer's retries, or a dropped queue entry
    (``nexus_projection_events_dropped_total``) leaves the kernel ahead of
    the projection.  This walks the kernel under ``prefix`` and, per file,
    creates the missing rows or appends a version for content the
    projection does not have; paths whose projected kernel ``gen`` is
    already newer than this node's kernel view are reported
    ``stale_kernel`` and left alone.  Rows are keyed by the caller's zone
    (the zone writes in that token's context were recorded under).
    ``dry_run`` reports without writing.  Requires admin privileges.
    """
    if not auth_result.get("is_admin"):
        raise HTTPException(status_code=403, detail="Admin access required for reconcile")

    nexus_fs = getattr(request.app.state, "nexus_fs", None)
    if nexus_fs is None:
        raise HTTPException(status_code=503, detail="NexusFS not initialized")
    record_store = getattr(nexus_fs, "record_store", None)
    session_factory = (
        record_store.session_factory
        if record_store is not None
        else getattr(nexus_fs, "SessionLocal", None)
    )
    if session_factory is None:
        raise HTTPException(status_code=503, detail="RecordStore not initialized")

    from nexus.server.dependencies import get_operation_context
    from nexus.storage.projection_reconcile import reconcile_projection

    context = get_operation_context(auth_result)
    zone_id = context.zone_id or ROOT_ZONE_ID
    try:
        report = await asyncio.to_thread(
            reconcile_projection,
            nexus_fs=nexus_fs,
            session_factory=session_factory,
            prefix=body.prefix,
            zone_id=zone_id,
            context=context,
            dry_run=body.dry_run,
            retire_missing=body.retire_missing,
            max_entries=body.max_entries,
        )
    except NexusFileNotFoundError as e:
        raise HTTPException(
            status_code=404, detail=f"prefix not found in the kernel: {body.prefix} ({e})"
        ) from e
    except Exception as e:
        logger.error("reconcile-projections error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to reconcile projections") from e
    return ReconcileProjectionsResponse(**report.to_dict())
