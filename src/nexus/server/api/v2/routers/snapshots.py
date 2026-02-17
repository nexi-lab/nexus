"""Transactional Snapshot REST API (Issue #1752).

Endpoints:
    POST /api/v2/snapshots/begin          — Begin a transaction
    POST /api/v2/snapshots/{id}/commit    — Commit (discard snapshot)
    POST /api/v2/snapshots/{id}/rollback  — Rollback to snapshot state
    GET  /api/v2/snapshots/active         — List active transactions
    POST /api/v2/snapshots/cleanup        — Expire stale transactions
    GET  /api/v2/snapshots/{id}           — Get transaction details

Note: Static paths (/active, /cleanup) must be registered before
the wildcard /{snapshot_id} to avoid route shadowing.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from nexus.server.api.v2.models.snapshots import (
    ActiveSnapshotsResponse,
    BeginSnapshotRequest,
    BeginSnapshotResponse,
    CleanupResponse,
    ConflictInfoResponse,
    RollbackResultResponse,
    TransactionInfoResponse,
)
from nexus.services.protocols.transactional_snapshot import (
    InvalidTransactionStateError,
    OverlappingTransactionError,
    SnapshotId,
    TransactionNotFoundError,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/snapshots", tags=["snapshots"])


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


async def _get_snapshot_service(request: Request) -> Any:
    """Get TransactionalSnapshotService from app.state."""
    svc = getattr(request.app.state, "transactional_snapshot_service", None)
    if svc is None:
        raise HTTPException(status_code=503, detail="Snapshot service not initialized")
    return svc


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/begin", response_model=BeginSnapshotResponse)
async def begin_snapshot(
    body: BeginSnapshotRequest,
    svc: Any = Depends(_get_snapshot_service),
) -> BeginSnapshotResponse:
    """Begin a transactional snapshot for the specified paths."""
    try:
        sid = await svc.begin(
            agent_id=body.agent_id,
            paths=body.paths,
            zone_id=body.zone_id,
        )
        return BeginSnapshotResponse(snapshot_id=sid.id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except OverlappingTransactionError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except Exception as e:
        logger.error("Snapshot begin failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to begin snapshot") from e


@router.post("/{snapshot_id}/commit", status_code=204)
async def commit_snapshot(
    snapshot_id: str,
    svc: Any = Depends(_get_snapshot_service),
) -> None:
    """Commit a transaction — changes become permanent."""
    try:
        await svc.commit(SnapshotId(id=snapshot_id))
    except TransactionNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except InvalidTransactionStateError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except Exception as e:
        logger.error("Snapshot commit failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to commit snapshot") from e


@router.post("/{snapshot_id}/rollback", response_model=RollbackResultResponse)
async def rollback_snapshot(
    snapshot_id: str,
    svc: Any = Depends(_get_snapshot_service),
) -> RollbackResultResponse:
    """Rollback a transaction — restore all paths to snapshot state."""
    try:
        result = await svc.rollback(SnapshotId(id=snapshot_id))
        return RollbackResultResponse(
            snapshot_id=result.snapshot_id,
            reverted=result.reverted,
            conflicts=[
                ConflictInfoResponse(
                    path=c.path,
                    snapshot_hash=c.snapshot_hash,
                    current_hash=c.current_hash,
                    reason=c.reason,
                )
                for c in result.conflicts
            ],
            deleted=result.deleted,
            stats=result.stats,
        )
    except TransactionNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except InvalidTransactionStateError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except Exception as e:
        logger.error("Snapshot rollback failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to rollback snapshot") from e


@router.get("/active", response_model=ActiveSnapshotsResponse)
async def list_active_snapshots(
    agent_id: str = Query(..., description="Agent to list active transactions for"),
    zone_id: str = Query("root", description="Zone ID"),
    svc: Any = Depends(_get_snapshot_service),
) -> ActiveSnapshotsResponse:
    """List active transactions for an agent."""
    try:
        txns = await svc.list_active(agent_id, zone_id=zone_id)
        return ActiveSnapshotsResponse(
            transactions=[
                TransactionInfoResponse(
                    snapshot_id=t.snapshot_id,
                    agent_id=t.agent_id,
                    zone_id=t.zone_id,
                    status=t.status,
                    paths=t.paths,
                    created_at=t.created_at,
                    expires_at=t.expires_at,
                    committed_at=t.committed_at,
                    rolled_back_at=t.rolled_back_at,
                )
                for t in txns
            ],
            count=len(txns),
        )
    except Exception as e:
        logger.error("Snapshot list_active failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to list active snapshots") from e


@router.post("/cleanup", response_model=CleanupResponse)
async def cleanup_expired(
    svc: Any = Depends(_get_snapshot_service),
) -> CleanupResponse:
    """Expire stale ACTIVE transactions past their TTL."""
    try:
        count = await svc.cleanup_expired()
        return CleanupResponse(expired_count=count)
    except Exception as e:
        logger.error("Snapshot cleanup failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to cleanup snapshots") from e


@router.get("/{snapshot_id}", response_model=TransactionInfoResponse)
async def get_snapshot(
    snapshot_id: str,
    svc: Any = Depends(_get_snapshot_service),
) -> TransactionInfoResponse:
    """Get transaction details by snapshot ID."""
    try:
        info = await svc.get_transaction(SnapshotId(id=snapshot_id))
        return TransactionInfoResponse(
            snapshot_id=info.snapshot_id,
            agent_id=info.agent_id,
            zone_id=info.zone_id,
            status=info.status,
            paths=info.paths,
            created_at=info.created_at,
            expires_at=info.expires_at,
            committed_at=info.committed_at,
            rolled_back_at=info.rolled_back_at,
        )
    except TransactionNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        logger.error("Snapshot get failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to get snapshot") from e
