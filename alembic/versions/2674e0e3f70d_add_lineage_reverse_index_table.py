"""add_lineage_reverse_index_table

Revision ID: 2674e0e3f70d
Revises: 928a619dabf4
Create Date: 2026-03-28

Issue #3417: Agent lineage tracking — reverse lookup table for impact analysis.
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2674e0e3f70d"
down_revision: Union[str, Sequence[str], None] = "928a619dabf4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create lineage_reverse_index table for agent lineage tracking."""
    op.create_table(
        "lineage_reverse_index",
        sa.Column("entry_id", sa.String(length=36), nullable=False),
        sa.Column("upstream_path", sa.String(length=512), nullable=False),
        sa.Column("downstream_urn", sa.String(length=512), nullable=False),
        sa.Column("zone_id", sa.String(length=255), nullable=False, server_default="root"),
        sa.Column("upstream_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("upstream_etag", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("access_type", sa.String(length=20), nullable=False, server_default="content"),
        sa.Column("agent_id", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("downstream_path", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("entry_id"),
    )
    # Reverse lookup: "what depends on upstream_path?" (impact analysis)
    op.create_index(
        "idx_lineage_reverse_upstream",
        "lineage_reverse_index",
        ["upstream_path", "zone_id"],
        unique=False,
    )
    # Forward cleanup: "delete all reverse entries for this downstream"
    op.create_index(
        "idx_lineage_reverse_downstream",
        "lineage_reverse_index",
        ["downstream_urn"],
        unique=False,
    )
    # Staleness query: single indexed scan
    op.create_index(
        "idx_lineage_reverse_staleness",
        "lineage_reverse_index",
        ["upstream_path", "zone_id", "upstream_version", "upstream_etag"],
        unique=False,
    )


def downgrade() -> None:
    """Drop lineage_reverse_index table."""
    op.drop_index("idx_lineage_reverse_staleness", table_name="lineage_reverse_index")
    op.drop_index("idx_lineage_reverse_downstream", table_name="lineage_reverse_index")
    op.drop_index("idx_lineage_reverse_upstream", table_name="lineage_reverse_index")
    op.drop_table("lineage_reverse_index")
