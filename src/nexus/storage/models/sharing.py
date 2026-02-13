"""Share link models for anonymous/external file access.

Issue #1286: Extracted from monolithic __init__.py.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from nexus.storage.models._base import Base, uuid_pk


class ShareLinkModel(Base):
    """Capability URL-based share links for anonymous/external file access.

    Implements W3C TAG Capability URL best practices:
    - Unguessable tokens (UUID v4 = 122 bits entropy)
    - Time-limited access via expires_at
    - Optional password protection (Argon2id hashed)
    - Download limits
    - Revocable
    """

    __tablename__ = "share_links"

    link_id: Mapped[str] = uuid_pk()

    resource_type: Mapped[str] = mapped_column(String(50), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(255), nullable=False)

    permission_level: Mapped[str] = mapped_column(String(20), nullable=False, default="viewer")

    zone_id: Mapped[str] = mapped_column(String(255), nullable=False, default="default", index=True)

    created_by: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)

    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    max_access_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    access_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked_by: Mapped[str | None] = mapped_column(String(255), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )
    last_accessed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    extra_data: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("idx_share_links_resource", "resource_type", "resource_id"),
        Index(
            "idx_share_links_active",
            "link_id",
            postgresql_where=text("revoked_at IS NULL"),
        ),
        Index("idx_share_links_created_by", "zone_id", "created_by"),
    )

    def __repr__(self) -> str:
        return f"<ShareLinkModel(link_id={self.link_id}, resource={self.resource_type}:{self.resource_id}, permission={self.permission_level})>"

    def is_valid(self) -> bool:
        """Check if the share link is currently valid for access."""
        now = datetime.now(UTC)

        if self.revoked_at is not None:
            return False

        if self.expires_at is not None and self.expires_at < now:
            return False

        return not (
            self.max_access_count is not None and self.access_count >= self.max_access_count
        )


class ShareLinkAccessLogModel(Base):
    """Access log for share link usage tracking."""

    __tablename__ = "share_link_access_log"

    log_id: Mapped[str] = uuid_pk()

    link_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("share_links.link_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    accessed_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)

    success: Mapped[int] = mapped_column(Integer, nullable=False)
    failure_reason: Mapped[str | None] = mapped_column(String(100), nullable=True)

    accessed_by_user_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    accessed_by_zone_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    __table_args__ = (
        Index("idx_share_link_access_log_time", "link_id", "accessed_at"),
        Index("idx_share_link_access_log_ip", "ip_address"),
    )

    def __repr__(self) -> str:
        status = "success" if self.success else f"failed:{self.failure_reason}"
        return f"<ShareLinkAccessLogModel(log_id={self.log_id}, link_id={self.link_id}, status={status})>"
