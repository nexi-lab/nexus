"""Add access_manifests table (Issue #1754).

Creates the access_manifests table for declarative MCP tool access rules.
The agent_credentials table is managed by the identity module (Issue #1753).

Revision ID: add_credentials_and_manifests
Revises: merge_agent_spec_zone_phase
Create Date: 2026-02-21
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "add_credentials_and_manifests"
down_revision: Union[str, Sequence[str], None] = "merge_agent_spec_zone_phase"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create access_manifests table."""
    op.create_table(
        "access_manifests",
        sa.Column("manifest_id", sa.String(36), primary_key=True, nullable=False),
        sa.Column("agent_id", sa.String(255), nullable=False),
        sa.Column("zone_id", sa.String(255), nullable=False, server_default="root"),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("entries_json", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("valid_from", sa.DateTime(), nullable=False),
        sa.Column("valid_until", sa.DateTime(), nullable=True),
        sa.Column("credential_id", sa.String(36), nullable=True),
        sa.Column("created_by", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("tuple_ids_json", sa.Text(), nullable=True),
    )
    op.create_index(
        "idx_access_manifests_agent_zone_status",
        "access_manifests",
        ["agent_id", "zone_id", "status"],
    )


def downgrade() -> None:
    """Remove access_manifests table."""
    op.drop_index("idx_access_manifests_agent_zone_status", table_name="access_manifests")
    op.drop_table("access_manifests")
