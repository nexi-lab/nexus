"""Context branch models for workspace branching (Issue #1315).

Provides git-like named branches on top of existing workspace snapshots.
Branches are metadata-only pointers — no data duplication.
Uses optimistic concurrency via pointer_version counter.
"""

from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.storage.models._base import Base, uuid_pk


class ContextBranchModel(Base):
    """Named branch pointer for workspace context versioning.

    Each branch tracks a head_snapshot_id (pointer to the latest snapshot on that branch)
    and metadata about its lifecycle (fork point, merge target, status).

    Concurrency: Uses optimistic locking via pointer_version. All pointer updates must
    use compare-and-swap: UPDATE ... WHERE id = :id AND pointer_version = :expected.

    Lifecycle:
        active → merged (via merge operation)
        active → discarded (via delete/discard operation)
    """

    __tablename__ = "context_branches"

    id: Mapped[str] = uuid_pk()

    zone_id: Mapped[str] = mapped_column(String(255), nullable=False, default=ROOT_ZONE_ID)
    workspace_path: Mapped[str] = mapped_column(Text, nullable=False)
    branch_name: Mapped[str] = mapped_column(String(255), nullable=False)

    # Pointer to current branch HEAD (latest snapshot on this branch)
    head_snapshot_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    # Fork metadata: which branch this was forked from and at what snapshot
    parent_branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
    fork_point_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    # Lifecycle status: 'active', 'merged', 'discarded'
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")

    # True if this is the currently checked-out branch for this workspace
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Optimistic concurrency control (A3-B): increment on every pointer update
    pointer_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Merge audit: where this branch was merged and the resulting snapshot
    merged_into_branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
    merge_snapshot_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    __table_args__ = (
        UniqueConstraint("zone_id", "workspace_path", "branch_name", name="uq_context_branch"),
        Index("ix_ctx_branch_zone_ws", "zone_id", "workspace_path"),
        Index("ix_ctx_branch_status", "status"),
    )

    def __repr__(self) -> str:
        return (
            f"<ContextBranchModel(id={self.id}, branch={self.branch_name}, "
            f"workspace={self.workspace_path}, status={self.status}, "
            f"pointer_version={self.pointer_version})>"
        )
