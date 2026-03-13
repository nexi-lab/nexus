"""add_entity_aspects_and_mcl_tables

Issue #2929: DataHub-inspired knowledge platform — entity aspects + MCL.

Revision ID: c4b5ef9d1ff5
Revises: None (standalone, no dependency on existing head)
Create Date: 2026-03-12

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4b5ef9d1ff5"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = ("knowledge_platform",)
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create entity_aspects and metadata_change_log tables."""
    # --- entity_aspects table ---
    op.create_table(
        "entity_aspects",
        sa.Column("aspect_id", sa.String(36), primary_key=True),
        sa.Column("entity_urn", sa.String(512), nullable=False),
        sa.Column("aspect_name", sa.String(128), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("created_by", sa.String(255), nullable=False, server_default="system"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("lock_version", sa.BigInteger(), nullable=False, server_default="0"),
    )

    # Unique constraint on (urn, name, version) for active aspects
    op.create_index(
        "idx_entity_aspects_urn_name_version",
        "entity_aspects",
        ["entity_urn", "aspect_name", "version"],
        unique=True,
    )

    # Fast lookup by URN (list all aspects for an entity)
    op.create_index(
        "idx_entity_aspects_urn",
        "entity_aspects",
        ["entity_urn"],
    )

    # Batch loading index (WHERE aspect_name=? AND version=0)
    op.create_index(
        "idx_entity_aspects_name_version",
        "entity_aspects",
        ["aspect_name", "version"],
    )

    # --- metadata_change_log table ---
    op.create_table(
        "metadata_change_log",
        sa.Column("mcl_id", sa.String(36), primary_key=True),
        sa.Column("sequence_number", sa.BigInteger(), nullable=False, unique=True),
        sa.Column("entity_urn", sa.String(512), nullable=False),
        sa.Column("aspect_name", sa.String(128), nullable=False),
        sa.Column("change_type", sa.String(20), nullable=False),
        sa.Column("aspect_value", sa.Text(), nullable=True),
        sa.Column("previous_value", sa.Text(), nullable=True),
        sa.Column("zone_id", sa.String(255), nullable=True),
        sa.Column("changed_by", sa.String(255), nullable=False, server_default="system"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )

    # Primary replay cursor
    op.create_index("idx_mcl_sequence", "metadata_change_log", ["sequence_number"])

    # Filter by entity
    op.create_index("idx_mcl_entity_urn", "metadata_change_log", ["entity_urn"])

    # Filter by aspect
    op.create_index("idx_mcl_aspect_name", "metadata_change_log", ["aspect_name"])

    # Zone-scoped replay
    op.create_index(
        "idx_mcl_zone_sequence",
        "metadata_change_log",
        ["zone_id", "sequence_number"],
    )


def downgrade() -> None:
    """Drop entity_aspects and metadata_change_log tables."""
    op.drop_table("metadata_change_log")
    op.drop_table("entity_aspects")
