"""Add persistent_namespace_views table (Issue #1265).

Stores pre-built namespace views for instant agent reconnection.
One row per (subject_type, subject_id, zone_id) — upsert semantics.

Part of the L3 cache layer between in-memory mount table and ReBAC rebuild.

Revision ID: add_persistent_namespace_views
Revises: add_agent_events_table
Create Date: 2026-02-11
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_persistent_namespace_views"
down_revision: Union[str, Sequence[str], None] = "add_agent_events_table"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add persistent_namespace_views table with indexes."""
    op.create_table(
        "persistent_namespace_views",
        sa.Column("id", sa.String(36), primary_key=True, nullable=False),
        sa.Column("subject_type", sa.String(50), nullable=False),
        sa.Column("subject_id", sa.String(255), nullable=False),
        sa.Column(
            "zone_id",
            sa.String(255),
            nullable=False,
            server_default="default",
        ),
        sa.Column("mount_paths_json", sa.Text(), nullable=False),
        sa.Column("grants_hash", sa.String(16), nullable=False),
        sa.Column("revision_bucket", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "subject_type",
            "subject_id",
            "zone_id",
            name="uq_persistent_ns_view_subject",
        ),
    )

    op.create_index(
        "idx_persistent_ns_view_zone",
        "persistent_namespace_views",
        ["zone_id"],
    )


def downgrade() -> None:
    """Remove persistent_namespace_views table."""
    op.drop_index(
        "idx_persistent_ns_view_zone",
        table_name="persistent_namespace_views",
    )
    op.drop_table("persistent_namespace_views")
