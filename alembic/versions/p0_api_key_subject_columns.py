"""P0: Add subject_type and subject_id columns to api_keys

Revision ID: p0_api_key_cols
Revises: a5bf12f44bc8
Create Date: 2025-10-25 00:45:00

This is a manual, focused migration for P0 security fixes.
Only adds the subject_type and subject_id columns needed for P0.
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "p0_api_key_cols"
down_revision: Union[str, Sequence[str], None] = "a5bf12f44bc8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add subject_type and subject_id columns to api_keys table.

    P0-5: API Key Security
    - subject_type: Type of subject (user, agent, service, session)
    - subject_id: Subject identifier (for ReBAC integration)
    """
    # Add subject_type column (nullable, defaults to "user" for backward compat)
    op.add_column(
        "api_keys",
        sa.Column("subject_type", sa.String(length=50), nullable=True, server_default="user"),
    )

    # Add subject_id column (nullable, can be populated from user_id)
    op.add_column("api_keys", sa.Column("subject_id", sa.String(length=255), nullable=True))

    # Make name column NOT NULL (best practice)
    op.alter_column(
        "api_keys",
        "name",
        existing_type=sa.String(length=255),
        nullable=False,
        server_default="API Key",  # Default for existing rows
    )

    # Optional: Populate subject_id from user_id for existing rows
    op.execute("""
        UPDATE api_keys
        SET subject_id = user_id
        WHERE subject_id IS NULL
    """)


def downgrade() -> None:
    """Remove subject_type and subject_id columns."""
    op.alter_column("api_keys", "name", existing_type=sa.String(length=255), nullable=True)

    op.drop_column("api_keys", "subject_id")
    op.drop_column("api_keys", "subject_type")
