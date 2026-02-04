"""add_filesystem_version_sequences_for_zookie_tokens

Issue #1187: Add per-tenant version sequence table for filesystem consistency tokens.
This enables Zookie-style consistency tokens for read-after-write guarantees.

Revision ID: add_filesystem_version_sequences
Revises: 6e9842c71775
Create Date: 2026-02-04 00:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_filesystem_version_sequences"
down_revision: Union[str, Sequence[str], None] = "6e9842c71775"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    Add per-tenant version sequence table for filesystem consistency tokens (zookies).
    This stores monotonic revision counters for each tenant, enabling clients to
    track consistency across filesystem operations.

    See: nexus.core.zookie.Zookie
    """
    # Create table to store per-tenant revision counters
    op.create_table(
        "filesystem_version_sequences",
        sa.Column("tenant_id", sa.String(255), nullable=False),
        sa.Column("current_revision", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id"),
    )

    # Initialize default tenant with revision 0
    op.execute(
        "INSERT INTO filesystem_version_sequences (tenant_id, current_revision, updated_at) "
        "VALUES ('default', 0, NOW())"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("filesystem_version_sequences")
