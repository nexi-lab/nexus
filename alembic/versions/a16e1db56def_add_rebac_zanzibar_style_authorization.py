"""add_rebac_zanzibar_style_authorization

Revision ID: a16e1db56def
Revises: 777350ff28ce
Create Date: 2025-10-19 11:26:51.174128

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a16e1db56def"
down_revision: Union[str, Sequence[str], None] = "777350ff28ce"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema to add ReBAC tables."""
    # ReBAC Tuples - Store authorization relationships
    op.create_table(
        "rebac_tuples",
        sa.Column("tuple_id", sa.String(36), primary_key=True),
        # Subject (who/what has the relationship)
        sa.Column("subject_type", sa.String(50), nullable=False),  # 'agent', 'group', 'file', etc.
        sa.Column("subject_id", sa.String(36), nullable=False),
        sa.Column("subject_relation", sa.String(50), nullable=True),  # Optional indirect relation
        # Relation type
        sa.Column("relation", sa.String(50), nullable=False),  # 'member-of', 'owner-of', etc.
        # Object (what the subject has relation to)
        sa.Column("object_type", sa.String(50), nullable=False),
        sa.Column("object_id", sa.String(36), nullable=False),
        # Metadata
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=True),  # For temporary access
        sa.Column("conditions", sa.Text(), nullable=True),  # JSON conditions
    )

    # Create indexes for rebac_tuples
    op.create_index("idx_rebac_subject", "rebac_tuples", ["subject_type", "subject_id", "relation"])
    op.create_index("idx_rebac_object", "rebac_tuples", ["object_type", "object_id", "relation"])
    op.create_index("idx_rebac_relation", "rebac_tuples", ["relation"])
    op.create_index("idx_rebac_expires", "rebac_tuples", ["expires_at"])

    # Composite index for Check API (most common query)
    op.create_index(
        "idx_rebac_check",
        "rebac_tuples",
        ["subject_type", "subject_id", "relation", "object_type", "object_id"],
    )

    # Unique constraint to prevent duplicate tuples
    op.create_index(
        "idx_rebac_tuple_unique",
        "rebac_tuples",
        [
            "subject_type",
            "subject_id",
            sa.text("COALESCE(subject_relation, '')"),
            "relation",
            "object_type",
            "object_id",
        ],
        unique=True,
    )

    # ReBAC Namespaces - Define valid relations per object type
    op.create_table(
        "rebac_namespaces",
        sa.Column("namespace_id", sa.String(36), primary_key=True),
        sa.Column("object_type", sa.String(50), nullable=False, unique=True),
        sa.Column("config", sa.Text(), nullable=False),  # JSON config for permission expansion
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )

    # ReBAC Check Cache - Cache computed permission checks
    op.create_table(
        "rebac_check_cache",
        sa.Column("cache_id", sa.String(36), primary_key=True),
        # Cache key
        sa.Column("subject_type", sa.String(50), nullable=False),
        sa.Column("subject_id", sa.String(36), nullable=False),
        sa.Column("permission", sa.String(50), nullable=False),
        sa.Column("object_type", sa.String(50), nullable=False),
        sa.Column("object_id", sa.String(36), nullable=False),
        # Cache value
        sa.Column("result", sa.Boolean(), nullable=False),
        # Cache metadata
        sa.Column("computed_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
    )

    # Create indexes for rebac_check_cache
    op.create_index(
        "idx_rebac_cache_lookup",
        "rebac_check_cache",
        ["subject_type", "subject_id", "permission", "object_type", "object_id"],
    )
    op.create_index("idx_rebac_cache_expires", "rebac_check_cache", ["expires_at"])

    # Unique constraint for cache entries
    op.create_index(
        "idx_rebac_cache_unique",
        "rebac_check_cache",
        ["subject_type", "subject_id", "permission", "object_type", "object_id"],
        unique=True,
    )

    # ReBAC Changelog - Track changes for cache invalidation
    op.create_table(
        "rebac_changelog",
        sa.Column("change_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("change_type", sa.String(20), nullable=False),  # 'INSERT', 'DELETE'
        sa.Column("tuple_id", sa.String(36), nullable=True),
        # Tuple details for cache invalidation
        sa.Column("subject_type", sa.String(50), nullable=False),
        sa.Column("subject_id", sa.String(36), nullable=False),
        sa.Column("relation", sa.String(50), nullable=False),
        sa.Column("object_type", sa.String(50), nullable=False),
        sa.Column("object_id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )

    # Create indexes for rebac_changelog
    op.create_index("idx_rebac_changelog_created", "rebac_changelog", ["created_at"])


def downgrade() -> None:
    """Downgrade schema to remove ReBAC tables."""
    # Drop indexes first
    op.drop_index("idx_rebac_changelog_created", table_name="rebac_changelog")
    op.drop_index("idx_rebac_cache_unique", table_name="rebac_check_cache")
    op.drop_index("idx_rebac_cache_expires", table_name="rebac_check_cache")
    op.drop_index("idx_rebac_cache_lookup", table_name="rebac_check_cache")
    op.drop_index("idx_rebac_tuple_unique", table_name="rebac_tuples")
    op.drop_index("idx_rebac_check", table_name="rebac_tuples")
    op.drop_index("idx_rebac_expires", table_name="rebac_tuples")
    op.drop_index("idx_rebac_relation", table_name="rebac_tuples")
    op.drop_index("idx_rebac_object", table_name="rebac_tuples")
    op.drop_index("idx_rebac_subject", table_name="rebac_tuples")

    # Drop tables
    op.drop_table("rebac_changelog")
    op.drop_table("rebac_check_cache")
    op.drop_table("rebac_namespaces")
    op.drop_table("rebac_tuples")
