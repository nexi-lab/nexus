"""Refresh token rotation history for reuse detection.

Issue #997: Tracks retired refresh tokens so that replay of an
already-rotated token triggers family-wide invalidation (RFC 9700).

Each row stores only the SHA-256 hash of the retired refresh token,
never the plaintext.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from nexus.storage.models._base import Base, _generate_uuid, _get_uuid_server_default


class RefreshTokenHistoryModel(Base):
    """Immutable record of a retired refresh token.

    When a refresh token is rotated (provider returns a new one), the
    SHA-256 hash of the old token is stored here.  If the same hash
    appears in a subsequent refresh request, it means the old token was
    replayed — indicating theft — and the entire token family is
    invalidated.
    """

    __tablename__ = "refresh_token_history"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=_generate_uuid,
        server_default=_get_uuid_server_default(),
    )

    token_family_id: Mapped[str] = mapped_column(String(36), nullable=False)
    credential_id: Mapped[str] = mapped_column(String(36), nullable=False)
    refresh_token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    rotation_counter: Mapped[int] = mapped_column(Integer, nullable=False)
    zone_id: Mapped[str] = mapped_column(String(255), nullable=False, default="default")

    rotated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )

    __table_args__ = (
        # Primary lookup: "has this refresh token hash been used in this family?"
        Index(
            "idx_rth_family_hash",
            "token_family_id",
            "refresh_token_hash",
        ),
        # Efficient pruning: DELETE WHERE token_family_id=? AND rotated_at < ?
        Index(
            "idx_rth_family_rotated_at",
            "token_family_id",
            "rotated_at",
        ),
        Index("idx_rth_credential", "credential_id"),
        Index("idx_rth_rotated_at", "rotated_at"),
        Index("idx_rth_zone", "zone_id"),
    )

    def __repr__(self) -> str:
        return (
            f"<RefreshTokenHistory(id={self.id}, "
            f"family={self.token_family_id}, counter={self.rotation_counter})>"
        )
