"""Transactional snapshot REST API endpoints (Issue #1752).

Provides endpoints for managing transactional filesystem snapshots:
- POST   /api/v2/snapshots              — Begin a new transaction
- GET    /api/v2/snapshots              — List transactions (zone-scoped)
- GET    /api/v2/snapshots/{txn_id}     — Get transaction details
- POST   /api/v2/snapshots/{txn_id}/commit   — Commit transaction
- POST   /api/v2/snapshots/{txn_id}/rollback — Rollback transaction
- GET    /api/v2/snapshots/{txn_id}/entries  — List snapshot entries

All endpoints require authentication and are scoped to the user's zone_id.
"""

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from pydantic import BaseModel, Field

from nexus.contracts.constants import ROOT_ZONE_ID

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/snapshots", tags=["snapshots"])

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class BeginTransactionRequest(BaseModel):
    """Request body for beginning a new transaction."""

    description: str | None = Field(None, max_length=500)
    ttl_seconds: int = Field(3600, ge=60, le=86400)


class TransactionResponse(BaseModel):
    """Response model for a single transaction."""

    transaction_id: str
    zone_id: str
    agent_id: str | None
    status: str
    description: str | None
    created_at: str
    expires_at: str
    entry_count: int


class SnapshotEntryResponse(BaseModel):
    """Response model for a single snapshot entry."""

    entry_id: str
    transaction_id: str
    path: str
    operation: str
    original_hash: str | None
    new_hash: str | None
    created_at: str


class TransactionListResponse(BaseModel):
    """Response model for listing transactions."""

    transactions: list[TransactionResponse]
    count: int


class ConflictResponse(BaseModel):
    """Response model for conflict details."""

    path: str
    expected_hash: str | None
    current_hash: str | None
    reason: str


class CommitErrorResponse(BaseModel):
    """Response model for commit conflict errors."""

    detail: str
    conflicts: list[ConflictResponse]


# ---------------------------------------------------------------------------
# Dependency
# ---------------------------------------------------------------------------


async def get_snapshot_context(request: Request) -> tuple[Any, str, str | None]:
    """FastAPI dependency: get snapshot service + auth context from request."""
    nexus_fs = getattr(request.app.state, "nexus_fs", None)
    if nexus_fs is None:
        raise HTTPException(status_code=503, detail="NexusFS not initialized")

    snapshot_service = getattr(nexus_fs, "_snapshot_service", None)
    if snapshot_service is None:
        raise HTTPException(status_code=503, detail="Snapshot service not available")

    zone_id = ROOT_ZONE_ID
    agent_id = None

    # Lazy import to avoid circular dependencies
    from nexus.server.api.v2.dependencies import _get_require_auth

    require_auth = _get_require_auth()
    if require_auth:
        auth_result = require_auth()
        if auth_result:
            zone_id = auth_result.get("zone_id", ROOT_ZONE_ID)
            agent_id = auth_result.get("agent_id")

    return snapshot_service, zone_id, agent_id


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _to_response(info: Any) -> TransactionResponse:
    """Convert TransactionInfo to response model."""
    return TransactionResponse(
        transaction_id=info.transaction_id,
        zone_id=info.zone_id,
        agent_id=info.agent_id,
        status=info.status,
        description=info.description,
        created_at=info.created_at.isoformat()
        if isinstance(info.created_at, datetime)
        else str(info.created_at),
        expires_at=info.expires_at.isoformat()
        if isinstance(info.expires_at, datetime)
        else str(info.expires_at),
        entry_count=info.entry_count,
    )


def _entry_to_response(entry: Any) -> SnapshotEntryResponse:
    """Convert SnapshotEntry to response model."""
    return SnapshotEntryResponse(
        entry_id=entry.entry_id,
        transaction_id=entry.transaction_id,
        path=entry.path,
        operation=entry.operation,
        original_hash=entry.original_hash,
        new_hash=entry.new_hash,
        created_at=entry.created_at.isoformat()
        if isinstance(entry.created_at, datetime)
        else str(entry.created_at),
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("", status_code=201, response_model=TransactionResponse)
async def begin_transaction(
    body: BeginTransactionRequest,
    ctx: tuple[Any, str, str | None] = Depends(get_snapshot_context),
) -> TransactionResponse:
    """Begin a new transactional snapshot."""
    snapshot_service, zone_id, agent_id = ctx
    try:
        info = await snapshot_service.begin(
            zone_id=zone_id,
            agent_id=agent_id,
            description=body.description,
            ttl_seconds=body.ttl_seconds,
        )
        return _to_response(info)
    except Exception as e:
        logger.error("Failed to begin transaction: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to begin transaction") from e


@router.get("", response_model=TransactionListResponse)
async def list_transactions(
    status: str | None = Query(None, description="Filter by status"),
    limit: int = Query(100, ge=1, le=1000),
    ctx: tuple[Any, str, str | None] = Depends(get_snapshot_context),
) -> TransactionListResponse:
    """List transactions scoped to the user's zone."""
    snapshot_service, zone_id, _ = ctx
    try:
        transactions = await snapshot_service.list_transactions(
            zone_id=zone_id,
            status=status,
            limit=limit,
        )
        return TransactionListResponse(
            transactions=[_to_response(t) for t in transactions],
            count=len(transactions),
        )
    except Exception as e:
        logger.error("Failed to list transactions: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to list transactions") from e


async def _verify_zone_ownership(snapshot_service: Any, txn_id: str, zone_id: str) -> Any:
    """Verify transaction exists and belongs to the caller's zone.

    Returns the TransactionInfo if valid, raises 404 otherwise.
    Prevents cross-zone transaction access (CRITICAL-1 fix).
    """
    info = await snapshot_service.get_transaction(txn_id)
    if info is None or info.zone_id != zone_id:
        raise HTTPException(status_code=404, detail=f"Transaction not found: {txn_id}")
    return info


@router.get("/{txn_id}", response_model=TransactionResponse)
async def get_transaction(
    txn_id: str = Path(..., min_length=1, max_length=36),
    ctx: tuple[Any, str, str | None] = Depends(get_snapshot_context),
) -> TransactionResponse:
    """Get details of a specific transaction."""
    snapshot_service, zone_id, _ = ctx
    try:
        info = await _verify_zone_ownership(snapshot_service, txn_id, zone_id)
        return _to_response(info)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to get transaction %s: %s", txn_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to get transaction") from e


@router.post("/{txn_id}/commit", response_model=TransactionResponse)
async def commit_transaction(
    txn_id: str = Path(..., min_length=1, max_length=36),
    ctx: tuple[Any, str, str | None] = Depends(get_snapshot_context),
) -> TransactionResponse:
    """Commit a transaction (with conflict detection)."""
    from nexus.bricks.snapshot.errors import (
        TransactionConflictError,
        TransactionNotActiveError,
        TransactionNotFoundError,
    )

    snapshot_service, zone_id, _ = ctx
    await _verify_zone_ownership(snapshot_service, txn_id, zone_id)
    try:
        info = await snapshot_service.commit(txn_id)
        return _to_response(info)
    except TransactionNotFoundError as e:
        raise HTTPException(status_code=404, detail=f"Transaction not found: {txn_id}") from e
    except TransactionNotActiveError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except TransactionConflictError as e:
        raise HTTPException(
            status_code=409,
            detail={
                "message": str(e),
                "conflicts": [
                    {
                        "path": c.path,
                        "expected_hash": c.expected_hash,
                        "current_hash": c.current_hash,
                        "reason": c.reason,
                    }
                    for c in e.conflicts
                ],
            },
        ) from e
    except Exception as e:
        logger.error("Failed to commit transaction %s: %s", txn_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to commit transaction") from e


@router.post("/{txn_id}/rollback", response_model=TransactionResponse)
async def rollback_transaction(
    txn_id: str = Path(..., min_length=1, max_length=36),
    ctx: tuple[Any, str, str | None] = Depends(get_snapshot_context),
) -> TransactionResponse:
    """Rollback a transaction to pre-transaction state."""
    from nexus.bricks.snapshot.errors import (
        TransactionNotActiveError,
        TransactionNotFoundError,
    )

    snapshot_service, zone_id, _ = ctx
    await _verify_zone_ownership(snapshot_service, txn_id, zone_id)
    try:
        info = await snapshot_service.rollback(txn_id)
        return _to_response(info)
    except TransactionNotFoundError as e:
        raise HTTPException(status_code=404, detail=f"Transaction not found: {txn_id}") from e
    except TransactionNotActiveError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except Exception as e:
        logger.error("Failed to rollback transaction %s: %s", txn_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to rollback transaction") from e


@router.get("/{txn_id}/entries", response_model=list[SnapshotEntryResponse])
async def list_entries(
    txn_id: str = Path(..., min_length=1, max_length=36),
    ctx: tuple[Any, str, str | None] = Depends(get_snapshot_context),
) -> list[SnapshotEntryResponse]:
    """List all entries for a transaction."""
    snapshot_service, zone_id, _ = ctx
    await _verify_zone_ownership(snapshot_service, txn_id, zone_id)
    try:
        entries = await snapshot_service.list_entries(txn_id)
        return [_entry_to_response(e) for e in entries]
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to list entries for %s: %s", txn_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to list entries") from e
