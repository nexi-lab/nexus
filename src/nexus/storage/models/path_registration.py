"""Path registration model (workspace + memory directory configs).

Issue #189: Merge WorkspaceConfigModel + MemoryConfigModel into single
PathRegistrationModel with type discriminator, per DATA-STORAGE-MATRIX.md Part 15.

Lives in RecordStore (co-existence principle: meaningless without
WorkspaceSnapshotModel / MemoryModel which are also in RecordStore).
"""

from datetime import UTC, datetime

from sqlalchemy import DateTime, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from nexus.storage.models._base import Base


class PathRegistrationModel(Base):
    """Unified path registration for workspaces and memory directories.

    Replaces the separate WorkspaceConfigModel and (planned) MemoryConfigModel
    with a single model using a ``type`` discriminator column.

    Type values:
        "workspace" - Workspace directories (snapshots, versioning, rollback)
        "memory"    - Memory directories (AI agent memory storage)
    """

    __tablename__ = "path_registrations"

    path: Mapped[str] = mapped_column(Text, primary_key=True)
    type: Mapped[str] = mapped_column(String(20), nullable=False, default="workspace")

    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    user_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    agent_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    scope: Mapped[str] = mapped_column(String(20), nullable=False, default="persistent")
    session_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    extra_metadata: Mapped[str | None] = mapped_column("metadata", Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )

    __table_args__ = (
        Index("idx_path_reg_type", "type"),
        Index("idx_path_reg_created_at", "created_at"),
        Index("idx_path_reg_user", "user_id"),
        Index("idx_path_reg_agent", "agent_id"),
        Index("idx_path_reg_session", "session_id"),
        Index("idx_path_reg_expires", "expires_at"),
    )

    def __repr__(self) -> str:
        return f"<PathRegistrationModel(path={self.path}, type={self.type}, name={self.name})>"
