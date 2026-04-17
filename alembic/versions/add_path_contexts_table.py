"""Add path_contexts table (Issue #3773).

Creates the path_contexts table used to attach admin-configured, zone-scoped
human-readable descriptions to search result paths via longest-prefix match.

Supported dialects: PostgreSQL (production) and SQLite (embedded/tests).
The ``path_prefix`` column is ``String(1024)``; combined with ``zone_id``
in the unique constraint, this exceeds MySQL's default utf8mb4 index key
limit (3072 bytes). MySQL is not a supported backend — running the
migration there requires either shrinking the columns or adding an index
prefix.

Revision ID: add_path_contexts_table
Revises: add_document_skeleton
Create Date: 2026-04-16
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "add_path_contexts_table"
down_revision: Union[str, Sequence[str], None] = "add_document_skeleton"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create path_contexts table."""
    op.create_table(
        "path_contexts",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "zone_id",
            sa.String(255),
            nullable=False,
            server_default="root",
        ),
        sa.Column("path_prefix", sa.String(1024), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "zone_id",
            "path_prefix",
            name="uq_path_contexts_zone_prefix",
        ),
    )
    op.create_index(
        "ix_path_contexts_zone_updated",
        "path_contexts",
        ["zone_id", "updated_at"],
    )


def downgrade() -> None:
    """Remove path_contexts table."""
    op.drop_index("ix_path_contexts_zone_updated", table_name="path_contexts")
    op.drop_table("path_contexts")
