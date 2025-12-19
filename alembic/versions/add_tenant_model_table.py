"""add_tenant_model_table

Revision ID: t1234567890a
Revises: u1234567890a
Create Date: 2025-12-19 12:00:00.000000

This migration adds the tenants table for storing tenant metadata.

Key features:
- Stores tenant display name, domain, and description
- Unique domain identifier (company URL, email domain, etc.)
- JSON settings field for extensibility
- Soft delete support (is_active, deleted_at)
- Timestamps for audit trail
- Tenant membership still managed via ReBAC groups (group:tenant-{tenant_id})
"""

from collections.abc import Sequence
from contextlib import suppress
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "t1234567890a"
down_revision: Union[str, Sequence[str], None] = "u1234567890a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add tenants table for tenant metadata."""

    # Create tenants table
    op.create_table(
        "tenants",
        sa.Column("tenant_id", sa.String(255), primary_key=True, nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("domain", sa.String(255), nullable=True, unique=True),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("settings", sa.Text, nullable=True),
        sa.Column("is_active", sa.Integer, nullable=False, server_default="1"),
        sa.Column("deleted_at", sa.DateTime, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
    )

    # Create indexes for tenants table
    op.create_index("idx_tenants_name", "tenants", ["name"])
    op.create_index("idx_tenants_domain", "tenants", ["domain"])
    op.create_index("idx_tenants_active", "tenants", ["is_active"])


def downgrade() -> None:
    """Remove tenants table."""

    with suppress(Exception):
        op.drop_table("tenants")
