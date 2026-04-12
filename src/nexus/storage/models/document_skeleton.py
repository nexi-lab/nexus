"""DocumentSkeletonModel — global path+title index for file location (Issue #3725).

A lightweight, globally-indexed record for every file in every zone.
No embeddings, no LLM — BM25 over path tokens and title only.
Complementary to document_chunks (L2); this is the L0/L1 discovery layer.

Design decisions:
    - path_tokens column dropped (Issue #3725 review, 6A): virtual_path and title
      are fed as separate BM25S fields so the daemon can apply column weights.
      Pre-tokenized text would be redundant and create a rename-sync hazard.
    - skeleton_content_hash = sha256(first 2048 bytes): skip guard prevents
      redundant title re-extraction when file body changes but header doesn't.
    - Bootstrap from DB rows, not file reads: on daemon restart the BM25S index
      is reconstructed from existing rows without touching the file store.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.storage.models._base import Base


class DocumentSkeletonModel(Base):
    """Global file skeleton index for lightweight path+title search.

    One row per live file in every zone, regardless of #3698 indexing scope.
    The row is kept in sync by SkeletonPipeConsumer (create/rename/delete events).
    """

    __tablename__ = "document_skeleton"

    # path_id is both PK and FK; one-to-one with file_paths.
    path_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("file_paths.path_id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
    )

    # Denormalized for zone-scoped queries without a JOIN to file_paths.
    zone_id: Mapped[str] = mapped_column(String(255), nullable=False, default=ROOT_ZONE_ID)

    # First non-blank title extracted from the file header (≤ 2 KB read).
    # NULL for binary files or files with no extractable title.
    title: Mapped[str | None] = mapped_column(Text, nullable=True)

    # sha256(content[:2048]) — skip guard for SkeletonPipeConsumer.
    # If unchanged, title re-extraction is skipped entirely.
    skeleton_content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    indexed_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    __table_args__ = (
        # Primary lookup: all skeleton rows for a zone (used by /locate query)
        Index("idx_document_skeleton_zone", "zone_id"),
        # Covering index for the daemon's bootstrap SELECT
        # (zone_id, path_id, title — no heap fetch needed)
        Index("idx_document_skeleton_zone_path_title", "zone_id", "path_id", "title"),
    )

    def __repr__(self) -> str:
        return (
            f"<DocumentSkeletonModel(path_id={self.path_id}, zone={self.zone_id}, "
            f"title={self.title!r})>"
        )
