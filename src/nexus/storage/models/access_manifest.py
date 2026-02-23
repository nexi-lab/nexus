"""SQLAlchemy model for access manifests (Issue #1754).

Stores declarative tool access manifests that scope MCP tool access per agent.
Manifests reference optional VC credentials and track ReBAC tuple IDs
for clean revocation.
"""

from datetime import UTC, datetime

from sqlalchemy import DateTime, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.storage.models._base import Base, _generate_uuid


class AccessManifestModel(Base):
    """MCP tool access manifest for an agent (Issue #1754).

    Each manifest declares ordered tool access rules (allow/deny with glob
    patterns) for an agent within a zone. ReBAC tuple IDs are stored for
    clean revocation of generated grants.
    """

    __tablename__ = "access_manifests"

    manifest_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=_generate_uuid,
    )

    agent_id: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True,
    )

    zone_id: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        default=ROOT_ZONE_ID,
    )

    name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    entries_json: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="active",
    )

    valid_from: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
    )

    valid_until: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
    )

    credential_id: Mapped[str | None] = mapped_column(
        String(36),
        nullable=True,
    )

    created_by: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(UTC),
    )

    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
    )

    tuple_ids_json: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    __table_args__ = (
        Index("idx_access_manifests_agent_zone_status", "agent_id", "zone_id", "status"),
    )

    def __repr__(self) -> str:
        return (
            f"<AccessManifestModel(manifest_id={self.manifest_id}, "
            f"agent_id={self.agent_id}, status={self.status})>"
        )
