"""EntityAspectModel — side-store for extensible entity metadata (Issue #2929).

Stores aspects keyed by (entity_urn, aspect_name, version) using the
DataHub "version 0" pattern:
    - Version 0 is always the current value (O(1) reads)
    - Older versions are numbered 1, 2, 3... (bounded by max_versions)
    - On update: copy current to version N+1, overwrite version 0

Design decisions:
    - Side-store (not on FileMetadata hot path) — keeps Metastore at 64-100 bytes
    - JSON payload for simplicity (Avro/Protobuf deferred)
    - Composite PK (entity_urn, aspect_name, version) for efficient lookups
    - Soft-delete via deleted_at for aspect lifecycle tied to entity lifecycle
"""

from datetime import UTC, datetime

from sqlalchemy import BigInteger, DateTime, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from nexus.storage.models._base import Base, _generate_uuid, _get_uuid_server_default


class EntityAspectModel(Base):
    """Stores extensible metadata aspects for entities.

    Uses composite primary key (entity_urn, aspect_name, version) following
    the DataHub version-0 pattern for O(1) current-state reads.
    """

    __tablename__ = "entity_aspects"

    # Surrogate key for simpler FK references if needed
    aspect_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=_generate_uuid,
        server_default=_get_uuid_server_default(),
    )

    # Entity identification (URN string)
    entity_urn: Mapped[str] = mapped_column(String(512), nullable=False)

    # Aspect identification
    aspect_name: Mapped[str] = mapped_column(String(128), nullable=False)

    # Version: 0 = current, 1+ = history (DataHub pattern)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Payload (JSON blob)
    payload: Mapped[str] = mapped_column(Text, nullable=False)

    # Audit fields
    created_by: Mapped[str] = mapped_column(String(255), nullable=False, default="system")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )

    # Soft-delete for entity lifecycle
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Optimistic locking version for concurrent write safety
    lock_version: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )

    __table_args__ = (
        # Fast current-state lookup: WHERE entity_urn=? AND aspect_name=? AND version=0
        Index(
            "idx_entity_aspects_urn_name_version",
            "entity_urn",
            "aspect_name",
            "version",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        # List all aspects for an entity
        Index(
            "idx_entity_aspects_urn",
            "entity_urn",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        # Batch loading: WHERE entity_urn IN (...) AND aspect_name=? AND version=0
        Index(
            "idx_entity_aspects_name_version",
            "aspect_name",
            "version",
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<EntityAspectModel("
            f"urn={self.entity_urn}, "
            f"aspect={self.aspect_name}, "
            f"v={self.version})>"
        )

    def validate(self) -> None:
        """Validate aspect model before database operations."""
        from nexus.contracts.exceptions import ValidationError

        if not self.entity_urn:
            raise ValidationError("entity_urn is required")
        if not self.aspect_name:
            raise ValidationError("aspect_name is required")
        if self.version < 0:
            raise ValidationError(f"version must be >= 0, got {self.version}")
        if not self.payload:
            raise ValidationError("payload is required")
