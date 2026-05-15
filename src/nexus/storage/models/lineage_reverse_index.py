"""LineageReverseIndexModel — reverse lookup table for lineage queries (Issue #3417).

Stores denormalized upstream→downstream mappings so that impact analysis
("if X changes, what outputs are stale?") can be answered with a single
indexed query instead of scanning all lineage aspects.

Design decisions:
    - Denormalized from lineage aspects (aspect is source of truth)
    - Stores upstream_version + upstream_content_id for single-query staleness detection
    - Upsert semantics: DELETE old entries on re-write, INSERT new ones
    - Composite indexes for both forward and reverse lookups
    - Rebuildable from aspects if the table is ever corrupted
"""

from datetime import UTC, datetime

from sqlalchemy import DateTime, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from nexus.storage.models._base import Base, _generate_uuid, _get_uuid_server_default


class LineageReverseIndexModel(Base):
    """Reverse lookup index: upstream_path → downstream entities.

    One row per (upstream_path, downstream_urn) pair. On lineage update,
    all rows for the downstream_urn are deleted and re-inserted.
    """

    __tablename__ = "lineage_reverse_index"

    entry_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=_generate_uuid,
        server_default=_get_uuid_server_default(),
    )

    # The upstream file that was read
    upstream_path: Mapped[str] = mapped_column(String(512), nullable=False)

    # The downstream entity (output file) that consumed this upstream
    downstream_urn: Mapped[str] = mapped_column(String(512), nullable=False)

    # Zone isolation
    zone_id: Mapped[str] = mapped_column(String(255), nullable=False, default="root")

    # Denormalized upstream version info for single-query staleness detection
    upstream_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    upstream_content_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")

    # Access type (content, metadata, list, exists)
    access_type: Mapped[str] = mapped_column(String(20), nullable=False, default="content")

    # Agent that created this lineage edge
    agent_id: Mapped[str] = mapped_column(String(255), nullable=False, default="")

    # The downstream file path (denormalized for display/CLI)
    downstream_path: Mapped[str] = mapped_column(Text, nullable=True)

    # Audit
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )

    __table_args__ = (
        # Reverse lookup: "what depends on upstream_path?" (impact analysis)
        Index(
            "idx_lineage_reverse_upstream",
            "upstream_path",
            "zone_id",
            postgresql_where=text("1=1"),  # no-op filter, ensures consistent plan
        ),
        # Forward cleanup: "delete all reverse entries for this downstream"
        Index(
            "idx_lineage_reverse_downstream",
            "downstream_urn",
        ),
        # Staleness query: single indexed scan
        Index(
            "idx_lineage_reverse_staleness",
            "upstream_path",
            "zone_id",
            "upstream_version",
            "upstream_content_id",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<LineageReverseIndex("
            f"upstream={self.upstream_path}, "
            f"downstream={self.downstream_urn}, "
            f"v={self.upstream_version})>"
        )
