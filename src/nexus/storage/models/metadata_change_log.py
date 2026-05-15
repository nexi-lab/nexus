"""MetadataChangeLogModel — replayable metadata change stream (Issue #2929).

Separate from OperationLogModel (audit trail). MCL records metadata-level
changes (aspect upserts/deletes) for index rebuild and reactive processing.

Design decisions (Architecture Review #2):
    - Separate table from operation_log (different cardinality, different purpose)
    - Append-only, immutable records
    - Sequence-numbered for ordered replay
    - Full aspect value in each record (not delta) for idempotent replay
"""

from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import BigInteger, DateTime, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from nexus.storage.models._base import Base, _generate_uuid, _get_uuid_server_default


class MCLChangeType(StrEnum):
    """Types of metadata changes recorded in the MCL."""

    UPSERT = "upsert"
    DELETE = "delete"
    # Path aspect changed (rename/move, URN stable)
    PATH_CHANGED = "path_changed"


class MetadataChangeLogModel(Base):
    """Append-only log of metadata changes for replay and reactive indexing.

    Each record contains the full aspect value (not a delta) so replay
    is idempotent: ``replay(events) == replay(replay(events))``.
    """

    __tablename__ = "metadata_change_log"

    # Primary key
    mcl_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=_generate_uuid,
        server_default=_get_uuid_server_default(),
    )

    # Monotonic sequence for ordered replay.
    # PostgreSQL uses a SEQUENCE (mcl_sequence_number_seq) via migration.
    # SQLite uses MCLRecorder's fallback allocator (Issue #3062).
    sequence_number: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True)

    # Entity and aspect identification
    entity_urn: Mapped[str] = mapped_column(String(512), nullable=False)
    aspect_name: Mapped[str] = mapped_column(String(128), nullable=False)

    # Change type
    change_type: Mapped[str] = mapped_column(String(20), nullable=False)

    # Full aspect value (JSON) for idempotent replay
    aspect_value: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Previous value for diff/audit (optional)
    previous_value: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Context
    zone_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    changed_by: Mapped[str] = mapped_column(String(255), nullable=False, default="system")

    # Timestamp
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )

    __table_args__ = (
        # Primary replay cursor: ORDER BY sequence_number
        Index("idx_mcl_sequence", "sequence_number"),
        # Filter by entity for per-entity replay
        Index("idx_mcl_entity_urn", "entity_urn"),
        # Filter by aspect for targeted reindex
        Index("idx_mcl_aspect_name", "aspect_name"),
        # Zone-scoped replay
        Index("idx_mcl_zone_sequence", "zone_id", "sequence_number"),
        # BRIN for efficient range scans on sequence_number
        Index(
            "idx_mcl_sequence_brin",
            "sequence_number",
            postgresql_using="brin",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<MCL(seq={self.sequence_number}, "
            f"urn={self.entity_urn}, "
            f"aspect={self.aspect_name}, "
            f"type={self.change_type})>"
        )
