"""feat(#2984): Drop context_manifest column from agent_records

Revision ID: drop_context_manifest_col
Revises: add_credentials_and_manifests
Create Date: 2026-03-15

Phase 2 of the context_manifest removal (Issue #2984). Phase 1 stopped
writing to and reading from the column. This migration drops the column.

The context_manifest resolver is now exposed as a stateless MCP tool
(nexus_resolve_context) that accepts sources directly — no DB persistence
is needed.
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "drop_context_manifest_col"
down_revision: Union[str, Sequence[str], None] = "add_credentials_and_manifests"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Column may not exist in databases that were created purely from alembic
    # migrations (no create_all). The column was added to the ORM model without
    # a corresponding ADD COLUMN migration.
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {col["name"] for col in inspector.get_columns("agent_records")}
    if "context_manifest" in columns:
        op.drop_column("agent_records", "context_manifest")


def downgrade() -> None:
    op.add_column(
        "agent_records",
        sa.Column("context_manifest", sa.Text(), nullable=True, server_default="[]"),
    )
