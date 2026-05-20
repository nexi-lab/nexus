"""Path context model (Issue #3773).

The path_contexts table is primarily accessed through raw SQL in
``nexus.bricks.search.path_context``. Keeping a model here ensures
``Base.metadata.create_all`` creates the table for SQLite/dev/test databases,
matching the Alembic-managed production schema.
"""

from datetime import UTC, datetime

from sqlalchemy import DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.storage.models._base import Base


class PathContextModel(Base):
    """Admin-configured per-zone path prefix description."""

    __tablename__ = "path_contexts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    zone_id: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        default=ROOT_ZONE_ID,
        server_default=ROOT_ZONE_ID,
    )
    path_prefix: Mapped[str] = mapped_column(String(1024), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(),
        nullable=False,
        default=lambda: datetime.now(UTC).replace(tzinfo=None),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(),
        nullable=False,
        default=lambda: datetime.now(UTC).replace(tzinfo=None),
        onupdate=lambda: datetime.now(UTC).replace(tzinfo=None),
    )

    __table_args__ = (
        UniqueConstraint("zone_id", "path_prefix", name="uq_path_contexts_zone_prefix"),
        Index("ix_path_contexts_zone_updated", "zone_id", "updated_at"),
    )
