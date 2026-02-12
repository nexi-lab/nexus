"""SQLAlchemy model for persistent namespace views (Issue #1265).

Stores pre-built namespace views for instant agent reconnection.
One row per (subject_type, subject_id, zone_id) — upsert semantics.

Part of the L3 cache layer:
    dcache L1 (O(1)) → mount table L2 (O(log m)) → L3 persistent (1-3ms)
    → ReBAC rebuild (5-50ms)
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from nexus.storage.models._base import Base, _generate_uuid


class PersistentNamespaceViewModel(Base):
    """Persistent namespace view for instant agent reconnection.

    Keyed on (subject_type, subject_id, zone_id). Upsert via DELETE + INSERT.
    mount_paths_json stores a JSON array of sorted mount prefix strings.

    Attributes:
        id: UUID primary key
        subject_type: Subject type (e.g., "user", "agent")
        subject_id: Subject identifier
        zone_id: Zone for multi-tenant isolation (default: "default")
        mount_paths_json: JSON array of sorted mount path strings
        grants_hash: 16-char SHA-256 hex digest of sorted grants
        revision_bucket: Zone revision bucket when view was built
        created_at: When this view was first created
        updated_at: When this view was last updated (upsert)
    """

    __tablename__ = "persistent_namespace_views"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_generate_uuid)
    subject_type: Mapped[str] = mapped_column(String(50), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(255), nullable=False)
    zone_id: Mapped[str] = mapped_column(String(255), nullable=False, default="default")
    mount_paths_json: Mapped[str] = mapped_column(Text, nullable=False)
    grants_hash: Mapped[str] = mapped_column(String(16), nullable=False)
    revision_bucket: Mapped[int] = mapped_column(Integer, nullable=False)
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
        UniqueConstraint(
            "subject_type",
            "subject_id",
            "zone_id",
            name="uq_persistent_ns_view_subject",
        ),
        Index("idx_persistent_ns_view_zone", "zone_id"),
    )
