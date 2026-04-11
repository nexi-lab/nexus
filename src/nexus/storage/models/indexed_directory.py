"""Indexed directory model — per-zone semantic index scoping (Issue #3698).

Each row registers a directory prefix under which files should be fed into the
embedding pipeline. When a zone has ``indexing_mode == 'scoped'`` (set on
``ZoneModel``), only files matching one of the registered directory prefixes
are embedded. Zones in the default ``'all'`` mode embed every file, preserving
backward compatibility.

See the architecture review in Issue #3698 for rationale:
- No ``recursive`` column — v1 is recursive-only (YAGNI).
- No ``created_by`` column — audit is cross-cutting; add later if needed.
- The (zone_id, directory_path) unique constraint doubles as the composite
  index used by the filter helper and the bootstrap SQL push.
"""

from datetime import UTC, datetime

from sqlalchemy import BigInteger, DateTime, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from nexus.contracts.exceptions import ValidationError
from nexus.storage.models._base import Base


class IndexedDirectoryModel(Base):
    """A directory registered for semantic indexing within a zone.

    When the owning zone's ``indexing_mode`` is ``'scoped'``, only files whose
    virtual path matches one of these directory prefixes are embedded by the
    search daemon. Zones with ``indexing_mode == 'all'`` ignore this table.
    """

    __tablename__ = "indexed_directories"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    # Zone this directory registration belongs to. Not a FK so the search
    # daemon can tolerate zones that get created/deleted without cascading
    # schema pressure; dangling rows are cleaned up by the reconciler.
    zone_id: Mapped[str] = mapped_column(String(255), nullable=False)

    # Canonical virtual path of the directory. Always starts with '/'.
    # Trailing slash is stripped on write to keep prefix matching consistent.
    directory_path: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )

    __table_args__ = (
        UniqueConstraint(
            "zone_id",
            "directory_path",
            name="uq_indexed_directories_zone_path",
        ),
        Index("idx_indexed_directories_zone", "zone_id"),
    )

    def __repr__(self) -> str:
        return f"<IndexedDirectoryModel(zone={self.zone_id}, path={self.directory_path})>"

    def validate(self) -> None:
        """Validate a directory registration before write."""
        if not self.zone_id:
            raise ValidationError("zone_id is required")
        if not self.directory_path:
            raise ValidationError("directory_path is required")
        if not self.directory_path.startswith("/"):
            raise ValidationError(
                f"directory_path must start with '/', got {self.directory_path!r}"
            )
        # Reject trailing slash (except for root '/') to keep prefix matching
        # consistent. Callers should canonicalize with rstrip('/') before write.
        if self.directory_path != "/" and self.directory_path.endswith("/"):
            raise ValidationError(
                "directory_path must not end with '/' "
                f"(got {self.directory_path!r}); canonicalize before write"
            )
