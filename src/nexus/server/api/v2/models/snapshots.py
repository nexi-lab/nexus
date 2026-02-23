"""Pydantic models for Transactional Snapshot API (Issue #1752)."""

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.server.api.v2.models.base import ApiModel


class BeginSnapshotRequest(ApiModel):
    """Request to begin a transactional snapshot."""

    agent_id: str
    paths: list[str]
    zone_id: str = ROOT_ZONE_ID


class BeginSnapshotResponse(ApiModel):
    """Response from begin — contains the snapshot ID."""

    snapshot_id: str


class TransactionInfoResponse(ApiModel):
    """Read-only view of a transaction."""

    snapshot_id: str
    agent_id: str
    zone_id: str
    status: str
    paths: list[str]
    created_at: str
    expires_at: str
    committed_at: str | None = None
    rolled_back_at: str | None = None


class RollbackResultResponse(ApiModel):
    """Result of a rollback operation."""

    snapshot_id: str
    reverted: list[str]
    conflicts: "list[ConflictInfoResponse]"
    deleted: list[str]
    stats: dict[str, int]


class ConflictInfoResponse(ApiModel):
    """Describes a rollback conflict on a single path."""

    path: str
    snapshot_hash: str | None
    current_hash: str | None
    reason: str


class ActiveSnapshotsResponse(ApiModel):
    """List of active transactions for an agent."""

    transactions: list[TransactionInfoResponse]
    count: int


class CleanupResponse(ApiModel):
    """Result of cleanup operation."""

    expired_count: int
